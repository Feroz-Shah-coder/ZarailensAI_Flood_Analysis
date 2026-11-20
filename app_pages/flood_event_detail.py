"""Flood event details: real satellite measurement of one reported event in one district."""
from datetime import date
import io
import json

import folium
import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from streamlit_folium import st_folium

from config.settings import settings
from src import analysis_history as store
from src.flood_catalog import (load_catalog, era_label, search_window, analyze_catalog_event, headline, analysis_districts,
                               thumbnail_path, save_thumbnail, DEFAULT_DISTRICTS)
from src.flood_monitoring import event_tile_layers, monitoring_summary, kpk_district_geojson
from src.map_visualization import framed_map
from src.report_generator import pdf_report, excel_report

RISK_COLORS={"High":"#d7301f","Medium":"#fc8d59","Low":"#fdd49e","Insufficient Data":"#9e9e9e"}
catalog={e["id"]:e for e in load_catalog()["events"]}
event=catalog.get(st.session_state.get("catalog_event_id"))
if st.button("Back to all flood events",icon=":material/arrow_back:"):
    st.switch_page("app_pages/flood_events.py")
if event is None:
    st.info("Choose an event on the Flood Events page."); st.stop()

start,end=search_window(event)
st.markdown(f'<div class="fi-panel">{event["year"]} · {event["period"]} · {event["event"]}</div>',unsafe_allow_html=True)
st.markdown(f":material/location_on: **Reported area:** {event['areas']}  \n"
            f":material/satellite_alt: **Satellite data:** {era_label(event)} · searched {start} → {end}")
if event.get("note"): st.caption(event["note"])
st.caption("Reported event from the user-provided list. All numbers below are measured from real Earth Engine imagery for the chosen district.")

all_districts=sorted(f["properties"]["district"] for f in kpk_district_geojson(settings.boundaries_dir,settings.ee_project)["features"])
options=analysis_districts(event)
cached=store.catalog_analyses(event["id"],settings.analysis_history_db)
if not event["districts"]:
    st.caption("The report names no district, so this event is measured over the main flood-prone river districts: "
               +", ".join(DEFAULT_DISTRICTS)+". Any other district can be chosen below.")

# ------------------------------------------------------------------ all districts overview
st.markdown("#### All districts of this event")
overview=[]
for d in options:
    r=cached.get((event["id"],d)); h=headline(r) if r else {}
    overview.append({"District":d,"Satellite result":r.get("status") if r else "Not analysed yet",
                     "Underwater (ha)":h.get("flood_ha"),"Flooded farmland (ha)":h.get("farmland_ha"),
                     "Crop damage risk":h.get("risk"),"After image":h.get("after")})
done=[o for o in overview if o["Underwater (ha)"] is not None]
if done:
    st.markdown('<div class="fi-kpi-grid">'+"".join(
        f'<div class="fi-kpi {cls}"><div class="ic">{icon}</div><div><p class="lb">{label}</p><p class="vl">{value}</p><p class="nt">{note}</p></div></div>'
        for cls,icon,label,value,note in [
            ("k-blue","🌊","Total underwater area",f"{sum(o['Underwater (ha)'] for o in done):,.0f} ha",f"{len(done)} district(s) measured"),
            ("k-green","🌱","Total flooded farmland",f"{sum(o['Flooded farmland (ha)'] for o in done):,.0f} ha","exposure, not loss"),
            ("k-red","⚠️","Highest crop damage risk",next((lvl for lvl in ("High","Medium","Low") if any(o["Crop damage risk"]==lvl for o in done)),"—"),"preliminary · NDVI-based"),
            ("k-violet","🛰️","Districts analysed",f"{sum(1 for d in options if (event['id'],d) in cached)}/{len(options)}","results saved")])+'</div>',unsafe_allow_html=True)
st.dataframe(pd.DataFrame(overview),hide_index=True,width="stretch",
             column_config={k:st.column_config.NumberColumn(format="%.0f") for k in ("Underwater (ha)","Flooded farmland (ha)")})
pics=[d for d in options if thumbnail_path(event["id"],d).exists()]
if pics:
    st.caption("Real satellite pictures after the event (blue = measured underwater area; grey = cloud or no data). Click a district to see its full details.")
    cols=st.columns(4)
    for i,d in enumerate(pics):
        with cols[i%4], st.container(border=True,key=f"dthumb_{i}"):
            st.image(str(thumbnail_path(event["id"],d)),width="stretch")
            if st.button(d,key=f"pick_{d}",width="stretch",icon=":material/zoom_in:"):
                st.session_state[f"district_{event['id']}"]=d; st.rerun()

st.markdown("#### District details")
c1,c2=st.columns([2,1],vertical_alignment="bottom")
district_choices=options+[d for d in all_districts if d not in options]
sel_key=f"district_{event['id']}"
if sel_key not in st.session_state:   # open on the district with the largest measured flood, else the first analysed one
    ranked=sorted(done,key=lambda o:-o["Underwater (ha)"])
    st.session_state[sel_key]=(ranked[0]["District"] if ranked else
                              next((d for d in district_choices if (event["id"],d) in cached),district_choices[0]))
district=c1.selectbox("District",district_choices,
                      format_func=lambda d:d+("  ✓ analysed" if (event["id"],d) in cached else ""),key=sel_key)
result=cached.get((event["id"],district))
if c2.button("Re-run analysis" if result else "Analyse with real satellite data",type="primary",icon=":material/play_arrow:",width="stretch"):
    with st.status(f"Analysing {district} with real satellite data…",expanded=True) as status:
        bar=st.progress(0.0); note=st.empty()
        result=analyze_catalog_event(event,district,settings,settings.ee_project,
                                     lambda f,t:(bar.progress(min(f,1.0)),note.caption(t)))
        store.save_catalog_analysis(result,settings.analysis_history_db)
        try: save_thumbnail(result,settings.ee_project)
        except Exception as exc: st.caption(f"Picture for the card list could not be saved: {exc}")
        status.update(label=f"{district}: {result.get('status')}",state="complete",expanded=False)
if not result:
    st.info("This district has not been analysed for this event yet. Sentinel-era analyses take about 1–8 minutes; "
            "Landsat-era analyses about 30 seconds. Results are saved."); st.stop()

# ------------------------------------------------------------------ headline
h=headline(result); landsat=result.get("landsat"); detail=result.get("detail")
crop_rows=(landsat or {}).get("crop_rows") or ((detail or {}).get("crops") or {}).get("rows") or []
status_text=result.get("status","")
total_crop=(landsat or detail or {}).get("total_cropland_ha")
st.markdown('<div class="fi-kpi-grid">'+"".join(
    f'<div class="fi-kpi {cls}"><div class="ic">{icon}</div><div><p class="lb">{label}</p><p class="vl">{value}</p><p class="nt">{note}</p></div></div>'
    for cls,icon,label,value,note in [
        ("k-blue","🌊","Underwater area (new water)",f"{h['flood_ha']:,.0f} ha" if h else "—",f"image {h['after']}" if h else "not measured"),
        ("k-green","🌱","Flooded farmland",f"{h['farmland_ha']:,.0f} ha" if h else "—",
         f"{100*h['farmland_ha']/total_crop:.1f}% of district cropland" if h and total_crop else "exposure, not loss"),
        ("k-red","⚠️","Crop damage risk",(h or {}).get("risk") or "—","preliminary · NDVI-based"),
        ("k-violet","🛰️","Satellite result",status_text,f"{result.get('acquisitions_in_window','–')} acquisitions in window"),
        ("k-yellow","🗓️","Searched window",f"{start:%d %b %Y}",f"to {end:%d %b %Y}")])+'</div>',unsafe_allow_html=True)
if result.get("message"): st.info(result["message"])
if result.get("event"):
    with st.expander("Evidence checked",expanded=status_text.startswith("Rejected")):
        for reason in result["event"].get("reasons",[]): st.markdown(f"- {reason}")
if result.get("detail_error"): st.error(f"Detailed analysis failed: {result['detail_error']}")

# ------------------------------------------------------------------ pictures
@st.cache_data(ttl="3h",show_spinner="Loading live satellite images…")
def sentinel_layers(detail_json: str) -> dict:
    return event_tile_layers(json.loads(detail_json),settings.ee_project)

@st.cache_data(ttl="3h",show_spinner="Loading live Landsat images…")
def landsat_layers(result_json: str) -> dict:
    from src.satellite_retrieval import SensorCatalog, optical_event_impact
    r=json.loads(result_json); l=r["landsat"]
    spec=next(s for s in SensorCatalog.optical_for_date(date.fromisoformat(l["after_date"])) if s.name==l["sensor"])
    return optical_event_impact(l["baseline_ids"],l["after_id"],spec,r["geometry"],settings.ee_project)["layers"]

def show_map(layers_spec, key, height=300):
    fmap=framed_map(result["geometry"],width_px=420,height_px=height)
    for url,name,opacity in layers_spec:
        folium.TileLayer(tiles=url,name=name,attr="Google Earth Engine",overlay=True,control=len(layers_spec)>1,opacity=opacity).add_to(fmap)
    folium.GeoJson(result["geometry"],style_function=lambda _:{"color":"#ffffff","weight":2,"fillOpacity":0}).add_to(fmap)
    if len(layers_spec)>1: folium.LayerControl(collapsed=True).add_to(fmap)
    st_folium(fmap,height=height,width=None,returned_objects=[],key=key)

try:
    if detail:
        L=sentinel_layers(json.dumps(detail,sort_keys=True,default=str))
        st.markdown("#### Satellite pictures – before and after (actual acquisitions)")
        cols=st.columns(3)
        for col,(title,specs,caption) in zip(cols,[
            ("Before flood · Sentinel-1",[(L["before"],"Before",1)],f"{detail['before']['time_utc'][:16].replace('T',' ')} UTC"),
            ("After flood · Sentinel-1",[(L["after"],"After",1)],f"{detail['after']['time_utc'][:16].replace('T',' ')} UTC"),
            ("Underwater area detected",[(L["after"],"After",1),(L["flood"],"New water",.9)],"green = new water")]):
            with col:
                st.markdown(f"**{title}**"); show_map(specs,f"s1_{title}"); st.caption(caption)
        cols=st.columns(3)
        for col,key,title in zip(cols,["s2_before","s2_after"],["Before · Sentinel-2 natural colour","After · Sentinel-2 natural colour"]):
            with col:
                st.markdown(f"**{title}**")
                if L.get(key): show_map([(L[key]["url"],title,1)],f"s2_{key}"); st.caption("dates: "+", ".join(L[key]["dates"]))
                else: st.info("No Sentinel-2 image in this window.")
        with cols[2]:
            st.markdown("**Flood & farmland map**")
            show_map([(L["flood_blue"],"Underwater area",.85),(L["cropland"],"Farmland",.5),(L["flooded_cropland"],"Flooded farmland",.95),(L["risk"],"Damage risk",.95)],"impact_map")
            st.caption("blue = underwater · yellow = farmland · orange = flooded farmland · layers: damage risk")
    elif landsat:
        L=landsat_layers(json.dumps(result,sort_keys=True,default=str))
        st.markdown(f"#### Satellite pictures – {landsat['sensor']} (actual acquisitions)")
        cols=st.columns(3)
        for col,(title,specs,caption) in zip(cols,[
            ("Before flood",[(L["before"],"Before",1)],"composite of "+", ".join(landsat["before_dates"][-4:])),
            ("After flood",[(L["after"],"After",1)],f"{landsat['after_date']} · {landsat.get('after_clear_percent',0):.0f}% of district clear"),
            ("Underwater area & farmland",[(L["after"],"After",1),(L["flood"],"Underwater",.85),(L["flooded_cropland"],"Flooded farmland",.95)],
             "blue = new water · orange = flooded farmland")]):
            with col:
                st.markdown(f"**{title}**"); show_map(specs,f"ls_{title}"); st.caption(caption)
        st.caption(landsat["rule"]+" · "+landsat["cropland_source"])
except Exception as exc:
    st.warning(f"Live satellite images could not be loaded: {exc}")

# ------------------------------------------------------------------ crop damage
if crop_rows:
    st.markdown("#### Crop damage assessment")
    frame=pd.DataFrame([{"Crop":r["crop"],"Flooded area (ha)":r["affected_ha"],"Crop area in district (ha)":r.get("crop_area_ha"),
        "NDVI change – flooded":r.get("ndvi_change_flooded"),"NDVI change – unflooded":r.get("ndvi_change_unflooded"),
        "Relative NDVI change":r.get("relative_ndvi_change"),"Damage risk":r["risk"]} for r in crop_rows])
    st.dataframe(frame.style.map(lambda v:f"background-color:{RISK_COLORS.get(v,'')}33;font-weight:600" if v in RISK_COLORS else "",subset=["Damage risk"]),
                 hide_index=True,width="stretch",column_config={**{k:st.column_config.NumberColumn(format="%.0f") for k in ("Flooded area (ha)","Crop area in district (ha)")},
                 **{k:st.column_config.NumberColumn(format="%+.3f") for k in ("NDVI change – flooded","NDVI change – unflooded","Relative NDVI change")}})
    st.caption("Flooded farmland = exposure. NDVI change = observed vegetation change (flooded minus unflooded fields). "
               "Damage risk = preliminary class. Confirmed crop damage requires field verification; no loss percentage is estimated.")

# ------------------------------------------------------------------ time data
st.markdown("#### Time data")
t1,t2=st.columns(2)
with t1:
    timeline=pd.DataFrame(result.get("timeline") or [])
    if not timeline.empty:
        if "phase" in timeline: timeline=timeline[timeline["phase"]=="monitoring"]
        fig=go.Figure(go.Bar(x=timeline["date"],y=timeline["new_water_ha"],marker_color="#2f7de1",
                             hovertext=timeline["track"],name="New water (ha)"))
        fig.update_layout(height=280,margin=dict(l=0,r=0,t=30,b=0),title="New water on each satellite acquisition (ha)",yaxis_title="ha")
        st.plotly_chart(fig,width="stretch",config={"displayModeBar":False})
        st.caption(f"{len(timeline)} acquisition(s) in the window. Gaps between bars are days with no satellite pass.")
    else: st.info("No usable acquisition in this window.")
with t2:
    rain=((detail or {}).get("rainfall") or (landsat or {}).get("rainfall") or {})
    series=pd.DataFrame(rain.get("series") or []).dropna() if rain.get("series") else pd.DataFrame()
    if not series.empty:
        fig=go.Figure(go.Bar(x=series["date"],y=series["mm"],marker_color="#1f6b45"))
        fig.update_layout(height=280,margin=dict(l=0,r=0,t=30,b=0),title="Daily rainfall, district mean (mm)")
        st.plotly_chart(fig,width="stretch",config={"displayModeBar":False})
        st.caption(f"Source: {rain.get('source')}")
    else: st.info("Rainfall series not available for this result.")

if detail and detail.get("tehsil_priorities"):
    st.markdown("#### Field inspection priority by tehsil")
    st.dataframe(pd.DataFrame([{"Tehsil":t["area"].title(),"Priority":t["priority"],"Underwater (ha)":t["flood_ha"],
        "Flooded farmland (ha)":t["flooded_cropland_ha"],"High-risk farmland (ha)":t["high_risk_ha"]} for t in detail["tehsil_priorities"]]),
        hide_index=True,width="stretch",column_config={k:st.column_config.NumberColumn(format="%.0f") for k in ("Underwater (ha)","Flooded farmland (ha)","High-risk farmland (ha)")})

# ------------------------------------------------------------------ report
summary={"report":"ZarailensAI reported flood event analysis","event":f"{event['year']} {event['period']} – {event['event']}",
         "reported_area":event["areas"],"district":district,"satellite_result":status_text,"search_window":f"{start} → {end}",
         "computed_at":result.get("computed_at")}
if detail:
    summary.update(monitoring_summary({"refreshed_at":result.get("computed_at"),"monitoring_window":[str(start),str(end)],
                                       "selected_source":"reported event list"},result.get("event"),detail))
elif landsat:
    summary.update({"sensor":landsat["sensor"],"before_images":", ".join(landsat["before_dates"]),"after_image":landsat["after_date"],
                    "flood_ha":landsat["flood_ha"],"ag_ha":landsat["agricultural_flood_ha"],"total_cropland_ha":landsat["total_cropland_ha"],
                    "method":landsat["rule"],"cropland_source":landsat["cropland_source"],"crop_rows":crop_rows,
                    "rainfall_series":(landsat.get("rainfall") or {}).get("series",[])})
def _pdf():
    b=io.BytesIO(); pdf_report(summary,b); return b.getvalue()
def _xlsx():
    b=io.BytesIO(); excel_report(summary,b); return b.getvalue()
with st.container(horizontal=True):
    st.download_button("Download PDF report",_pdf,file_name=f"ZarailensAI - {event['id']} - {district}.pdf",mime="application/pdf",icon=":material/download:")
    st.download_button("Export Excel",_xlsx,file_name=f"ZarailensAI - {event['id']} - {district}.xlsx",
                       mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",icon=":material/table_view:")
