"""Dashboard: automatic latest-flood monitoring across Khyber Pakhtunkhwa.

Every number shown here comes from a monitoring run computed on real Earth
Engine data (stored in SQLite) or from an event the user chose to analyze.
"""
from datetime import date, datetime, timezone
import io
import json
import logging

import folium
import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from streamlit_folium import st_folium

from config.settings import settings
from src.analysis_history import latest_monitoring_run, save_monitoring_run
from src.flood_monitoring import (run_monitoring, analyze_event, analyze_historical_window, event_tile_layers,
    latest_s1_acquisition, kpk_district_geojson, monitoring_summary, main_affected_crop, overall_risk)
from src.report_generator import excel_report, pdf_report
from src.satellite_retrieval import sentinel1_pair_report_thumbnails

log=logging.getLogger(__name__)
ESRI='https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}'
ESRI_ATTR='Basemap: Esri, Maxar, Earthstar Geographics'
RISK_COLORS={"High":"#d7301f","Medium":"#fc8d59","Low":"#fdd49e","Insufficient Data":"#9e9e9e"}

# ------------------------------------------------------------------ cached EE helpers
@st.cache_data(ttl="1h",show_spinner=False)
def cached_latest_acquisition() -> str | None:
    return latest_s1_acquisition(settings.ee_project)

@st.cache_data(ttl="3h",show_spinner=False)
def cached_layers(detail_json: str) -> dict:
    """Earth Engine map IDs expire, so tile URLs are regenerated at most every 3 hours."""
    return event_tile_layers(json.loads(detail_json),settings.ee_project)

@st.cache_data(show_spinner=False)
def cached_districts() -> dict:
    return kpk_district_geojson(settings.boundaries_dir,settings.ee_project)

def _hours_since(iso: str | None) -> float:
    if not iso: return 1e9
    return (datetime.now(timezone.utc)-datetime.fromisoformat(iso)).total_seconds()/3600

def _fmt_time(iso: str | None) -> str:
    return iso[:16].replace("T"," ")+" UTC" if iso else "unavailable"

def _ha(value) -> str:
    return "—" if value is None else f"{value:,.0f} ha"

def _run_with_progress(label: str, fn):
    with st.status(label,expanded=True) as status:
        bar=st.progress(0.0); note=st.empty()
        def progress(fraction: float, text: str):
            bar.progress(min(max(fraction,0.0),1.0)); note.caption(text)
        result=fn(progress)
        status.update(label=label.replace("…","")+" – finished",state="complete",expanded=False)
    return result

# ------------------------------------------------------------------ data refresh
run=st.session_state.monitoring_run or latest_monitoring_run(settings.analysis_history_db)
availability_error=None
try:
    latest_available=cached_latest_acquisition()
except Exception as exc:
    latest_available=None; availability_error=str(exc)

top=st.container(border=True)
with top:
    head,actions=st.columns([3,2],vertical_alignment="center")
    head.markdown("#### Latest flood event · Khyber Pakhtunkhwa")
    with actions:
        with st.container(horizontal=True,horizontal_alignment="right"):
            refresh=st.button("Refresh Latest Flood Data",type="primary",icon=":material/refresh:")
            manual=st.button("Analyze Selected Area",icon=":material/travel_explore:")
work_area=st.container()

auto_reason=None
if run is None and not availability_error:
    auto_reason="No monitoring run has been stored yet — searching KPK automatically."
elif run and latest_available and run.get("latest_acquisition") and latest_available>run["latest_acquisition"] \
        and _hours_since(run.get("refreshed_at"))>=settings.auto_refresh_hours:
    auto_reason=f"New Sentinel-1 data ({_fmt_time(latest_available)}) is available since the last search — updating automatically."
if refresh or auto_reason:
    with work_area:
        if auto_reason: st.info(auto_reason)
        new_run=_run_with_progress("Searching KPK for flood events in real Sentinel-1 data…",
                                   lambda p:run_monitoring(settings,settings.ee_project,p))
    save_monitoring_run(new_run,settings.analysis_history_db)
    if new_run.get("status")=="error":
        st.error(f"The refresh failed and the previous result is still shown: {new_run.get('message')}")
    else:
        run=new_run
        st.session_state.update(monitoring_run=run,dashboard_event=None,dashboard_detail=None,dashboard_report=None)
        cached_layers.clear()

if manual:
    event_for_prefill=st.session_state.dashboard_event or (run or {}).get("selected")
    st.session_state.prefill_district=(event_for_prefill or {}).get("district")
    st.switch_page("app_pages/new_analysis.py")

if run is None:
    with top:
        if availability_error: st.error(f"Earth Engine is unavailable, so no flood data can be shown: {availability_error}")
        else: st.warning("No monitoring result is available yet.")
    st.stop()

# ------------------------------------------------------------------ which event is shown
event=st.session_state.dashboard_event or run.get("selected")
detail=st.session_state.dashboard_detail if st.session_state.dashboard_event else run.get("detail")
recent=run.get("recent") or {}
historical=run.get("historical") or []
source=("user-selected event" if st.session_state.dashboard_event else run.get("selected_source"))

with top:
    if event:
        is_recent=source=="recent"
        pill=("p-detected","Detected flood event") if event["status"]=="Detected flood event" else ("p-possible",event["status"])
        verdict=event.get("verdict") or {}
        hist_pill='' if is_recent else f'<span class="fi-pill p-hist">{"Historical · "+source if source!="user-selected event" else "Selected by user"}</span>'
        st.markdown(f'<div class="fi-status"><span class="fi-pill {pill[0]}">{pill[1]}</span>{hist_pill}'
                    f'<span class="fi-pill p-none">Confidence: {event["confidence"]} · score {event["score"]:.0f}/100</span>'
                    f'<span class="fi-pill p-hist">Optical check: {verdict.get("optical","not checked")}</span>'
                    f'<div style="margin-top:.45rem;font-size:1.05rem"><b>{event["district"]} district</b> · first detected on the '
                    f'Sentinel-1 acquisition of <b>{event["first_detection"]}</b>, largest extent observed <b>{event["peak_date"]}</b></div></div>',
                    unsafe_allow_html=True)
        with st.expander("Why this classification? (evidence checked)"):
            for reason in event.get("reasons",[]): st.markdown(f"- {reason}")
        if not is_recent and run.get("message"):
            st.warning(f"{run['message']} The most recent detected event from earlier seasons is shown below.")
    else:
        st.markdown('<div class="fi-status"><span class="fi-pill p-none">No confirmed recent flood event detected</span>'
                    f'<div style="margin-top:.4rem">No district showed a corroborated flood signal in the monitoring window'
                    f'{" or in the historical monsoon seasons searched" if historical else ""}. Possible events (if any) are listed below.</div></div>',
                    unsafe_allow_html=True)
    a1,a2,a3,a4=st.container(key="fi_compact").columns(4)
    a1.caption(f"**Last successful refresh**  \n{_fmt_time(run.get('refreshed_at'))}")
    a2.caption(f"**Latest Sentinel-1 acquisition used**  \n{_fmt_time(run.get('latest_acquisition'))}")
    window=run.get("monitoring_window") or ["–","–"]
    a3.caption(f"**Monitoring window**  \n{window[0]} → {window[1]} · {recent.get('monitoring_slices','–')} acquisition slices")
    if availability_error: status_text=f"Earth Engine check failed: {availability_error[:80]}"
    elif latest_available and run.get("latest_acquisition") and latest_available>run["latest_acquisition"]:
        status_text=f"Newer data available ({_fmt_time(latest_available)}) — press Refresh"
    else: status_text="Up to date with Earth Engine"
    if recent.get("errors"): status_text+=f" · {len(recent['errors'])} slice(s) failed"
    a4.caption(f"**Data availability**  \n{status_text}")
    if detail is None and event and run.get("detail_error"):
        st.error(f"The event was found but its detailed analysis failed: {run['detail_error']}")

# ------------------------------------------------------------------ KPI cards
crops=(detail or {}).get("crops") or {}
crop_rows=crops.get("rows") or []
main_crop=main_affected_crop(crop_rows) if detail else None
risk_level=overall_risk(crop_rows) if detail else None
crop_pct=(100*detail["agricultural_flood_ha"]/detail["total_cropland_ha"]) if detail and detail.get("total_cropland_ha") else None
cards=[("k-blue","🌊","Total flooded area",_ha(detail["flood_ha"]) if detail else "—",
        f"New water vs same-track baseline · {detail['scale_m']} m" if detail else "No analyzed event"),
       ("k-green","🌱","Affected farmland",_ha(detail["agricultural_flood_ha"]) if detail else "—",
        f"{crop_pct:.1f}% of district cropland (WorldCover)" if crop_pct is not None else "ESA WorldCover 2021"),
       ("k-yellow","🌽","Main affected crop",(main_crop or "Not identified") if detail else "—",
        "ESA WorldCereal 2021 crop types" if detail else ""),
       ("k-red","⚠️","Crop damage risk",risk_level or "—","Preliminary · flood exposure + NDVI change"),
       ("k-violet","🛰️","Event confidence",event["confidence"] if event else "—",
        f"{event['status']}" if event else "No confirmed event")]
# One CSS grid: 5 cards per row on desktop, reflowing to 3/2/1 on tablets and phones.
st.markdown('<div class="fi-kpi-grid">'+"".join(
    f'<div class="fi-kpi {cls}"><div class="ic">{icon}</div><div><p class="lb">{label}</p>'
    f'<p class="vl">{value}</p><p class="nt">{note}</p></div></div>' for cls,icon,label,value,note in cards)+'</div>',
    unsafe_allow_html=True)

# ------------------------------------------------------------------ maps & tables
layers=None
if detail:
    try:
        layers=cached_layers(json.dumps(detail,sort_keys=True,default=str))
    except Exception as exc:
        st.error(f"Live Earth Engine map layers could not be generated: {exc}")

def base_map(geometry: dict, height_zoom: int = 10) -> folium.Map:
    from shapely.geometry import shape
    shp=shape(geometry); c=shp.centroid
    fmap=folium.Map(location=[c.y,c.x],zoom_start=height_zoom,tiles=None,control_scale=True)
    folium.TileLayer(tiles=ESRI,attr=ESRI_ATTR,name="Satellite basemap",overlay=False,control=False).add_to(fmap)
    fmap.get_root().header.add_child(folium.Element("<style>.leaflet-control-attribution{font-size:9px!important;line-height:1.2}</style>"))
    minx,miny,maxx,maxy=shp.bounds; fmap.fit_bounds([[miny,minx],[maxy,maxx]],padding=(10,10))
    return fmap

def boundary(fmap, geometry, name="District boundary"):
    folium.GeoJson(geometry,name=name,style_function=lambda _:{"color":"#ffffff","weight":2.5,"fillOpacity":0}).add_to(fmap)

def legend(items):
    st.markdown('<div class="fi-legend">'+"".join(f'<div><span style="background:{c};border:1px solid #888"></span>{t}</div>' for t,c in items)+'</div>',unsafe_allow_html=True)

def impact_map(height: int):
    fmap=base_map(detail["geometry"])
    folium.TileLayer(tiles=layers["flood_blue"],attr="Sentinel-1",name="Flooded area",overlay=True,opacity=.85).add_to(fmap)
    folium.TileLayer(tiles=layers["cropland"],attr="ESA WorldCover",name="Farmland (crops)",overlay=True,opacity=.55).add_to(fmap)
    folium.TileLayer(tiles=layers["flooded_cropland"],attr="Sentinel-1 × WorldCover",name="Flooded farmland",overlay=True,opacity=.95).add_to(fmap)
    boundary(fmap,detail["geometry"])
    folium.LayerControl(collapsed=True).add_to(fmap)
    st_folium(fmap,height=height,width=None,returned_objects=[],key=f"impact_map_{height}")
    legend([("District boundary","#ffffff"),("Flooded area","#2f7de1"),("Farmland (crops)","#f2c12e"),("Flooded farmland","#f26b1d")])

left,middle,right=st.columns([1.3,1.05,1])
with left:
    with st.container(border=True):
        st.markdown(f'<div class="fi-panel">Flood & Crop Impact Map{" – "+detail["district"] if detail else ""}</div>',unsafe_allow_html=True)
        if detail and layers: impact_map(460)
        elif detail: st.info("Map layers unavailable.")
        else: st.info("No analyzed flood event to map.")

with middle:
    with st.container(border=True):
        st.markdown('<div class="fi-panel">Crop-wise Damage Estimate</div>',unsafe_allow_html=True)
        if crop_rows:
            frame=pd.DataFrame([{"Crop":r["crop"].split(" (")[0].replace("Other temporary crops","Other seasonal").replace("Wheat / winter cereals","Wheat"),"Area (ha)":r["affected_ha"],
                                 "ΔNDVI":r["relative_ndvi_change"],"Risk":r["risk"]} for r in crop_rows])
            styled=frame.style.map(lambda v:f"background-color:{RISK_COLORS.get(v,'')}33;font-weight:600" if v in RISK_COLORS else "",subset=["Risk"])
            st.dataframe(styled,hide_index=True,width="stretch",column_config={
                "Crop":st.column_config.TextColumn(width="small"),
                "Risk":st.column_config.TextColumn(width="small"),
                "Area (ha)":st.column_config.NumberColumn(format="%.0f",width="small"),
                "ΔNDVI":st.column_config.NumberColumn(format="%+.2f",width="small",help="NDVI change of flooded fields minus unflooded fields of the same crop (Sentinel-2)")})
            st.caption(f"Total flooded farmland: **{detail['agricultural_flood_ha']:,.0f} ha** · season: {crops.get('season')} · "
                       f"S2 scenes pre/post: {crops.get('s2_pre_scenes')}/{crops.get('s2_post_scenes')}. Risk = preliminary class, not a loss percentage.")
        else: st.info("Crop-wise results appear when an event has been analyzed.")
    with st.container(border=True):
        st.markdown('<div class="fi-panel">Affected Farmland by Crop</div>',unsafe_allow_html=True)
        affected=[r for r in crop_rows if r["affected_ha"]>=1]
        if affected:
            total=sum(r["affected_ha"] for r in affected)
            palette=crops.get("crop_palette",{})
            fig=go.Figure(go.Pie(labels=[r["crop"].split(" (")[0] for r in affected],values=[r["affected_ha"] for r in affected],hole=.6,
                                 marker={"colors":[f"#{palette.get(r['crop'],'9e9e9e')}" for r in affected]},sort=False,textinfo="percent"))
            fig.update_layout(height=250,margin=dict(l=0,r=0,t=0,b=0),legend=dict(orientation="v",font=dict(size=11)),
                              annotations=[dict(text=f"<b>{total:,.0f} ha</b><br>affected",showarrow=False,font=dict(size=13))])
            st.plotly_chart(fig,width="stretch",config={"displayModeBar":False})
        else: st.info("No flooded farmland to break down.")

with right:
    with st.container(border=True):
        st.markdown('<div class="fi-panel">Damage Risk Map</div>',unsafe_allow_html=True)
        if detail and layers:
            fmap=base_map(detail["geometry"])
            folium.TileLayer(tiles=layers["risk"],attr="Sentinel-1 × Sentinel-2 NDVI",name="Damage risk",overlay=True,opacity=.95).add_to(fmap)
            boundary(fmap,detail["geometry"])
            st_folium(fmap,height=230,width=None,returned_objects=[],key="risk_map")
            risk_area=crops.get("risk_area_ha") or {}
            legend([(f"{k} · {risk_area.get(k,0):,.0f} ha",RISK_COLORS[k]) for k in ("High","Medium","Low","Insufficient Data")])
        else: st.info("Risk map appears when an event has been analyzed.")
    with st.container(border=True):
        st.markdown('<div class="fi-panel">Key Findings & Recommendation</div>',unsafe_allow_html=True)
        if detail and event:
            findings=[f"🌊 ~{detail['flood_ha']:,.0f} ha of new surface water in {detail['district']} between {detail['onset_window'][0]} and {detail['onset_window'][1]} (Sentinel-1).",
                      f"🌱 ~{detail['agricultural_flood_ha']:,.0f} ha of farmland inside the flood extent"+(f" ({crop_pct:.1f}% of district cropland)." if crop_pct is not None else ".")]
            findings.append(f"🌽 Most affected mapped crop: {main_crop}." if main_crop else
                            "🌽 Mapped crop types (WorldCereal) cover little of the flooded farmland; crop type is not identified.")
            priorities=[p for p in detail.get("tehsil_priorities") or [] if p["priority"] in ("High","Medium")]
            if priorities:
                findings.append("📍 Priority areas for field inspection: "+", ".join(f"{p['area'].title()} ({p['priority']})" for p in priorities[:3])+".")
            wet=detail.get("wettest_day_in_onset_window")
            if wet: findings.append(f"🌧️ Wettest day in the onset window: {wet['date']} ({wet['mm']:.0f} mm, {detail['rainfall']['source'].split(' (')[0]}).")
            if detail["district"] in ("Tank","D. I. Khan","Lakki Marwat","Bannu","South Waziristan"):
                findings.append("⛰️ This district uses rod-kohi (hill-torrent spate) irrigation: torrent water is deliberately spread on bunded fields. "
                                "Satellites confirm water on farmland but cannot tell planned spate irrigation from a damaging flood — check local reports/field visits.")
            if event.get("concurrent_districts"): findings.append("🗺️ Concurrent flood signal in: "+", ".join(event["concurrent_districts"])+".")
            advice={"High":"✅ Field verification recommended in high-risk patches before any compensation decision.",
                    "Medium":"✅ Field verification recommended for medium/high-risk patches.",
                    "Low":"✅ NDVI shows no clear vegetation loss on flooded fields; spot-check flooded farmland.",
                    "Insufficient Data":"✅ Cloud-free Sentinel-2 data are insufficient; field inspection is needed to assess crops."}
            findings.append(advice.get(risk_level,""))
            for f in findings: st.markdown(f)
        else:
            st.markdown("No confirmed event, so no damage assessment is produced. Review possible events below or analyze a historical period.")

# ------------------------------------------------------------------ before / after imagery
if detail and layers:
    with st.container(border=True):
        st.markdown('<div class="fi-panel">Satellite images – before and after (actual acquisitions)</div>',unsafe_allow_html=True)
        cols=st.columns(3)
        for col,key,title,scene in zip(cols,["before","after","flood"],["Before flood","After flood","Flood detection result"],
                                       [detail["before"],detail["after"],detail["after"]]):
            with col:
                st.markdown(f"**{title}**")
                fmap=base_map(detail["geometry"]); fmap.options["zoomControl"]=False
                if key=="flood":
                    folium.TileLayer(tiles=layers["after"],attr=layers["source"],overlay=True,control=False).add_to(fmap)
                    folium.TileLayer(tiles=layers["flood"],attr=layers["source"],overlay=True,control=False,opacity=.9).add_to(fmap)
                else:
                    folium.TileLayer(tiles=layers[key],attr=layers["source"],overlay=True,control=False).add_to(fmap)
                boundary(fmap,detail["geometry"])
                st_folium(fmap,height=260,width=None,returned_objects=[],key=f"pair_{key}")
                if key=="flood":
                    st.caption(f"Green = new water · rule: VV change < {detail['rule']['change_db']} dB, VV < {detail['rule']['water_db']} dB, "
                               f"VH < {detail['rule'].get('vh_water_db')} dB; permanent water (JRC ≥90%), slope > {detail['rule']['max_slope_deg']}°, "
                               f"HAND > {detail['rule']['max_hand_m']} m and elevation > {detail['rule']['max_elevation_m']} m excluded · baseline: "
                               f"{len((detail.get('baseline') or {}).get('dates',[]))}-date same-track median")
                else:
                    st.caption(f"Sentinel-1 VV · acquired {scene['time_utc'][:16].replace('T',' ')} UTC · {scene['orbit'].title()} orbit "
                               f"{scene['relative_orbit']} · {len(scene.get('ids') or [1])} frame(s) · AOI coverage {scene.get('coverage',0):.0f}%")
        st.markdown("**Sentinel-2 natural colour – what the ground looked like**")
        ocols=st.columns(3)
        for col,key,title in zip(ocols,["s2_before","s2_after"],["Before (optical)","After (optical)"]):
            with col:
                st.markdown(f"**{title}**")
                layer=layers.get(key)
                if layer:
                    fmap=base_map(detail["geometry"]); fmap.options["zoomControl"]=False
                    folium.TileLayer(tiles=layer["url"],attr="Sentinel-2",overlay=True,control=False).add_to(fmap)
                    boundary(fmap,detail["geometry"])
                    st_folium(fmap,height=260,width=None,returned_objects=[],key=f"pair_{key}")
                    st.caption(f"Sentinel-2 dates: {', '.join(layer['dates'])} · clouds masked")
                else:
                    st.info("No Sentinel-2 image in this window (clouds or no pass).")
        ocols[2].markdown("**How to read these**")
        ocols[2].markdown("Radar marks surfaces that became dark. If the optical *after* image shows dry fields where radar shows new water, "
                          "the signal is wet soil or field preparation — the app then rejects it as a flood. Monsoon floods are often under clouds; "
                          "then only radar and its spatial pattern are used.")

# ------------------------------------------------------------------ KPK overview, rainfall, candidates
ov_left,ov_right=st.columns([1,1.25])
with ov_left:
    with st.container(border=True):
        st.markdown('<div class="fi-panel">KPK overview – flood signal by district</div>',unsafe_allow_html=True)
        try:
            districts=cached_districts()
            summary={d["district"]:d for d in recent.get("district_summary",[])}
            status_by={}
            for e in recent.get("events",[]):
                status_by.setdefault(e["district"],e["status"])
            fmap=folium.Map(location=[33.9,71.6],zoom_start=6,tiles=None,control_scale=True)
            folium.TileLayer(tiles="https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Light_Gray_Base/MapServer/tile/{z}/{y}/{x}",
                             attr="Basemap: Esri, HERE, Garmin",control=False).add_to(fmap)
            def style(feature):
                name=feature["properties"]["district"]; st_=status_by.get(name)
                color="#d7301f" if st_=="Detected flood event" else "#fc8d59" if st_=="Possible flood event" else "#a9a9a9" if st_ and st_.startswith("Rejected") else "#cfe3d6"
                selected=event and name==event["district"]
                return {"fillColor":color,"color":"#0f3d2e" if selected else "#5b7a6a","weight":3 if selected else .8,"fillOpacity":.75}
            for feature in districts["features"]:
                name=feature["properties"]["district"]; s=summary.get(name)
                feature["properties"]["signal"]=(f"{s['max_excess_ha']:,.0f} ha above normal (max)" if s else "no observation in window")
                feature["properties"]["status"]=status_by.get(name,"No event")
            folium.GeoJson(districts,style_function=style,tooltip=folium.GeoJsonTooltip(["district","status","signal"],aliases=["District","Status","Signal"])).add_to(fmap)
            fmap.fit_bounds([[31.2,69.2],[36.9,74.0]])
            st_folium(fmap,height=380,width=None,returned_objects=[],key="kpk_overview")
            legend([("Detected flood event","#d7301f"),("Possible flood event","#fc8d59"),("Signal rejected (not a flood)","#a9a9a9"),("No event in monitoring window","#cfe3d6")])
        except Exception as exc:
            st.warning(f"KPK overview map unavailable: {exc}")
with ov_right:
    with st.container(border=True):
        st.markdown('<div class="fi-panel">Rainfall evidence for the event</div>',unsafe_allow_html=True)
        rain=(detail or {}).get("rainfall") or {}
        if rain.get("series"):
            frame=pd.DataFrame(rain["series"]).dropna()
            fig=go.Figure(go.Bar(x=frame["date"],y=frame["mm"],marker_color="#2f7de1",name="Rainfall"))
            for d,label in ((detail["before"]["date"],"Before image"),(detail["after"]["date"],"After image")):
                fig.add_vline(x=d,line_dash="dash",line_color="#0f3d2e")
                fig.add_annotation(x=d,y=1,yref="paper",text=label,showarrow=False,yanchor="bottom",font=dict(size=10))
            fig.update_layout(height=230,margin=dict(l=0,r=0,t=20,b=0),yaxis_title="mm / day (district mean)")
            st.plotly_chart(fig,width="stretch",config={"displayModeBar":False})
            st.caption(f"Source: {rain['source']}. Rainfall is supporting evidence only; riverine floods can come from rain far upstream.")
        else: st.info("Rainfall series appears for an analyzed event.")
    with st.container(border=True):
        st.markdown('<div class="fi-panel">Candidate flood events (monitoring window)</div>',unsafe_allow_html=True)
        events=recent.get("events") or []
        if events:
            st.dataframe(pd.DataFrame([{"District":e["district"],"Status":e["status"],"Confidence":e["confidence"],
                "First detection":e["first_detection"],"Peak":e["peak_date"],"Excess new water (ha)":e["peak_excess_ha"],
                "× normal":e["peak_ratio"],"7-day rain (mm)":e.get("rainfall_7d_mm"),"Score":e["score"]} for e in events]),
                hide_index=True,width="stretch",height=210,column_config={
                    "Excess new water (ha)":st.column_config.NumberColumn(format="%.0f"),"× normal":st.column_config.NumberColumn(format="%.1f"),
                    "7-day rain (mm)":st.column_config.NumberColumn(format="%.0f"),"Score":st.column_config.ProgressColumn(min_value=0,max_value=100,format="%.0f")})
            st.caption("Screening at "+str(run.get("params",{}).get("screening_scale_m"))+" m. 'Excess' = new water above each district's normal level learned from the "
                       +str(run.get("params",{}).get("background_days"))+" days before the window (irrigation, paddies and river beds create a recurring signal).")
        else:
            st.info("No district exceeded the possible-event threshold in the monitoring window.")

# ------------------------------------------------------------------ explore other / historical events
with st.expander("Explore other and historical flood events",icon=":material/manage_search:"):
    options=[]
    for e in recent.get("events") or []: options.append(("Monitoring window",e))
    for h in historical:
        for e in h.get("events") or []: options.append((h.get("label","Historical"),e))
    search=st.session_state.historical_search
    if search:
        for e in search.get("events") or []: options.append((f"Search {search['window'][0]} → {search['window'][1]}",e))
    c1,c2=st.columns([3,1],vertical_alignment="bottom")
    if options:
        event_labels=[f"{src} · {e['status']} · {e['district']} · peak {e['peak_date']} · {float(e['peak_excess_ha']):,.0f} ha above normal" for src,e in options]
        index=c1.selectbox("Event",range(len(event_labels)),format_func=event_labels.__getitem__)
        if c2.button("Show this event",icon=":material/visibility:"):
            chosen=options[index][1]
            try:
                new_detail=_run_with_progress(f"Analyzing {chosen['district']} ({chosen['peak_date']})…",
                                              lambda p:json.loads(json.dumps(analyze_event(chosen,settings,settings.ee_project,p),default=str)))
                st.session_state.update(dashboard_event=chosen,dashboard_detail=new_detail,dashboard_report=None)
                st.rerun()
            except Exception as exc:
                st.error(f"Event analysis failed: {exc}")
    else: c1.caption("No candidate events stored yet.")
    if st.session_state.dashboard_event and st.button("Back to the automatically selected event"):
        st.session_state.update(dashboard_event=None,dashboard_detail=None,dashboard_report=None); st.rerun()
    st.divider()
    st.markdown("**Search a past period across KPK** (same automatic method)")
    h1,h2,h3=st.columns([1,1,1],vertical_alignment="bottom")
    year=h1.selectbox("Year",list(range(date.today().year,2015,-1)),index=1)
    season=h2.selectbox("Period",["Monsoon (1 Jul – 30 Sep)","Pre-monsoon (1 Apr – 30 Jun)","Post-monsoon (1 Oct – 30 Nov)","Winter (1 Dec – 31 Mar)"])
    if h3.button("Search period",icon=":material/search:"):
        start,end={"Monsoon":(date(year,7,1),date(year,10,1)),"Pre-monsoon":(date(year,4,1),date(year,7,1)),
                   "Post-monsoon":(date(year,10,1),date(year,12,1)),"Winter":(date(year,12,1),date(year+1,4,1))}[season.split(" (")[0]]
        end=min(end,date.today())
        if start>=end: st.warning("That period has not started yet.")
        else:
            try:
                found=_run_with_progress(f"Searching KPK {start} → {end}…",lambda p:analyze_historical_window(start,end,settings,settings.ee_project,p))
                st.session_state.historical_search=found
                if found.get("chosen") and found.get("detail"):
                    st.session_state.update(dashboard_event=found["chosen"],dashboard_detail=found["detail"],dashboard_report=None)
                    st.rerun()
                else:
                    st.info(f"No detected flood event in {start} → {end}. {len(found.get('events') or [])} possible event(s) are listed in the selector above.")
            except Exception as exc:
                st.error(f"Historical search failed: {exc}")

# ------------------------------------------------------------------ exports
summary=monitoring_summary(run,event,detail)
def build_pdf() -> bytes:
    images={}
    if detail:
        try:
            b={**detail["before"],"date":date.fromisoformat(detail["before"]["date"])}
            a={**detail["after"],"date":date.fromisoformat(detail["after"]["date"])}
            images=sentinel1_pair_report_thumbnails(b,a,detail["geometry"],settings.ee_project,**detail["rule"])
        except Exception as exc:
            log.warning("Report thumbnails unavailable: %s",exc)
    buffer=io.BytesIO(); pdf_report(summary,buffer,images=images); return buffer.getvalue()
def build_excel() -> bytes:
    buffer=io.BytesIO(); excel_report(summary,buffer); return buffer.getvalue()

@st.dialog("Flood & crop impact map",width="large")
def full_map():
    impact_map(640)

with st.container(horizontal=True,horizontal_alignment="center"):
    st.download_button("Download PDF Report",build_pdf,file_name=f"ZarailensAI - {summary.get('location','KPK')}.pdf",mime="application/pdf",icon=":material/download:")
    st.download_button("Export Excel",build_excel,file_name=f"ZarailensAI - {summary.get('location','KPK')}.xlsx",
                       mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",icon=":material/table_view:")
    if st.button("View Full Map",icon=":material/map:",disabled=not (detail and layers)): full_map()
st.caption("Satellite-derived indicators require ground validation. Flooded farmland does not automatically mean destroyed crops; "
           "no crop-loss percentage or financial loss is estimated because no validated model or ground-truth data is available.")
