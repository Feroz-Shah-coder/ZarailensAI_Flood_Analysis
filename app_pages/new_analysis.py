"""New Analysis page: user-selected area, dates/historical events, imagery, flood and crop analysis."""
from datetime import date, timedelta
import hashlib
import json
import logging
import os
import requests
import pandas as pd
import plotly.express as px
import streamlit as st
from folium.plugins import Draw
from streamlit_folium import st_folium
from config.settings import settings
from src.ee_auth import initialize
from src.event_analysis import choose_peak, rule_based_narrative
from src.location_search import (nominatim_search, osm_result_geometry, gaul_boundary,
    validate_polygon, approximate_buffer)
from src.satellite_retrieval import sentinel1_candidates, scene_thumbnail, flood_time_series
from src.date_selection import rank_candidates, validate_dates, available_sensors, EARLIEST_LANDSAT, SENTINEL1_START, compatible_sar_candidates, best_compatible_sar_pair
from src.satellite_retrieval import SensorCatalog, search_historical_candidates, optical_thumbnail, optical_flood_time_series, sentinel1_gallery_tile_layers, sentinel1_pair_agricultural_impact, sentinel1_pair_report_thumbnails, sentinel1_flood_search_year
from src.report_generator import excel_report, pdf_report
from src.analysis_history import save_analysis, list_analyses, history_totals
from src.impact_calculation import inspection_priority
from src.utils import ZarailensError
from src.flood_monitoring import kpk_district_geojson, tehsil_boundaries, rule_params, event_scene_pair, analyze_historical_window
from src.analysis_history import latest_monitoring_run
from src.agricultural_analysis import event_crop_assessment
from src.satellite_retrieval import s1_new_water, s1_scene_image

log=logging.getLogger(__name__)
# One Sentinel-1 flood rule for the whole app (VV/VH, permanent water, terrain, speckle filter).
RULE=rule_params(settings,event_scale=True)
RESET={"candidates":{},"sentinel_gallery":None,"sentinel_gallery_key":None,"gallery_layers":None,"gallery_layers_key":None,
       "gallery_impact":None,"year_search_key":None,"event_result":None,"analysis_summary":None,"crop_assessment":None}

def set_aoi(geometry: dict, source: str, name: str, area_km2: float, **extra):
    """Select a validated AOI and clear every result computed for the previous one."""
    st.session_state.update(aoi=geometry,aoi_source=source,aoi_name=name,area_km2=area_km2,**{**RESET,**extra})

@st.cache_data(show_spinner=False)
def district_options() -> dict:
    data=kpk_district_geojson(settings.boundaries_dir,settings.ee_project)
    return {f["properties"]["district"]:f["geometry"] for f in data.get("features",[])}

@st.cache_data(show_spinner=False)
def tehsil_options(district_geometry_json: str) -> list[dict]:
    return tehsil_boundaries(settings.boundaries_dir,json.loads(district_geometry_json))

candidate_state=st.session_state.candidates
workflow=[("Location",bool(st.session_state.aoi)),("Dates",bool(candidate_state)),
          ("Imagery",bool(candidate_state.get("before") and candidate_state.get("after"))),
          ("Analysis",bool(st.session_state.analysis_summary))]
st.markdown("**Workflow:** " + " → ".join((f"✓ {name}" if done else f"○ {name}") for name,done in workflow))

with st.sidebar:
    st.header("Analysis settings")
    search_window=st.slider("Manual acquisition search window (days)",1,30,10)
    min_coverage=st.slider("Minimum scene coverage (%)",50,100,int(settings.coverage_threshold))
    st.caption(f"Maximum AOI: {settings.max_aoi_km2:,.0f} km²")
    max_cloud=st.slider("Maximum optical cloud cover (%)",0,100,60)

location_tab,date_tab,image_tab,floodmap_tab,analysis_tab=st.tabs(["Location","Dates & imagery search","Satellite imagery","Interactive flood map","Flood & crop analysis"])

with location_tab:
    st.subheader("Location selection")
    with st.container(border=True):
        st.markdown("**Quick select: KPK district or tehsil**")
        try:
            districts=district_options()
            names=sorted(districts)
            prefill=st.session_state.pop("prefill_district",None)
            if prefill in names: st.session_state["quick_district"]=prefill
            q1,q2,q3=st.columns([2,2,1],vertical_alignment="bottom")
            district_name=q1.selectbox("District (FAO GAUL 2025)",names,key="quick_district")
            try:
                tehsils=tehsil_options(json.dumps(districts[district_name]))
            except Exception as exc:
                tehsils=[]; q2.caption(f"Tehsil boundaries unavailable: {exc}")
            tehsil_names=["Whole district"]+sorted(t["name"].title() for t in tehsils)
            tehsil_name=q2.selectbox("Tehsil (geoBoundaries ADM3)",tehsil_names,key="quick_tehsil")
            if q3.button("Use this area",type="primary",key="use_quick_area"):
                from shapely.geometry import shape
                from pyproj import Geod
                if tehsil_name=="Whole district":
                    geometry=districts[district_name]; source="FAO GAUL 2025 level 2 (simplified)"; label=f"{district_name} district"
                else:
                    match=next(t for t in tehsils if t["name"].title()==tehsil_name)
                    geometry=match["geometry"]; source=match["source"]; label=f"{tehsil_name} tehsil, {district_name}"
                area=abs(Geod(ellps="WGS84").geometry_area_perimeter(shape(geometry))[0])/1e6
                set_aoi(geometry,source,label,area)
                st.rerun()
            st.caption("Authoritative district and tehsil polygons. Village-level areas: use the place search below or draw an area on the map.")
        except Exception as exc:
            st.warning(f"District list unavailable ({exc}); use the search below or draw an area.")
    with st.form("place_search",clear_on_submit=False):
        query=st.text_input("Search a KPK district, tehsil, village, or place",value="")
        do_search=st.form_submit_button("Search",type="primary")
    if do_search and query.strip():
        with st.spinner("Searching OpenStreetMap and administrative boundaries…"):
            matches=[]; failures=[]
            try:
                initialize(settings.ee_project)
                district=gaul_boundary(query.strip(),2,settings.ee_project)
                if district: matches.append({"display_name":f"{district['name']} district, Khyber Pakhtunkhwa, Pakistan","type":"GAUL district","_boundary":district})
            except Exception as exc:
                log.exception("GAUL district search failed"); failures.append(f"Earth Engine GAUL: {exc}")
            try: matches.extend(nominatim_search(query.strip(),settings.nominatim_user_agent))
            except Exception as exc:
                log.exception("Nominatim search failed"); failures.append(f"OpenStreetMap Nominatim: {exc}")
            st.session_state.search_results=matches
            st.session_state.selected_result=None
            if failures and matches: st.warning("Some location sources were unavailable; showing the results that did return. " + " | ".join(failures))
            elif failures: st.error("Location search failed. " + " | ".join(failures))
            elif not matches: st.warning("No location matches returned. Check spelling, include Pakistan, or try a nearby town.")
    results=st.session_state.search_results
    if results:
        labels=[f"{r.get('display_name','Unknown')} · {r.get('type','place')} · {r.get('_boundary',{}).get('source','OpenStreetMap Nominatim')}" for r in results]
        choice=st.selectbox("Search results",range(len(labels)),format_func=lambda i:labels[i],key="location_choice")
        st.session_state.selected_result=results[choice]
        selected=st.session_state.selected_result
        if st.button("Use selected location",key="use_location"):
            try:
                initialize(settings.ee_project)
                boundary=selected.get("_boundary")
                # Try the authoritative district first; detailed admin lookup can fail independently.
                if boundary is None and (selected.get("type") in {"administrative","district","county"} or "district" in selected.get("display_name","").lower()):
                    boundary=gaul_boundary(query.strip(),2,settings.ee_project)
                geometry=(boundary or {}).get("geometry") or osm_result_geometry(selected)
                source=(boundary or {}).get("source") or "OpenStreetMap Nominatim polygon"
                if geometry is None:
                    if selected.get("lat") is None or selected.get("lon") is None:
                        raise ValueError("The selected result has no polygon boundary or usable point coordinates.")
                    st.session_state.point_center=[float(selected["lat"]),float(selected["lon"])]
                    st.warning("This result is a point without a boundary. Draw a polygon/rectangle on the map, or create a clearly labelled approximate buffer below.")
                else:
                    province=gaul_boundary("Khyber Pakhtunkhwa",1,settings.ee_project)
                    if not province: raise ValueError("Could not retrieve KPK province boundary from FAO GAUL level 1.")
                    area=validate_polygon(geometry,province["geometry"],settings.max_aoi_km2)
                    set_aoi(geometry,source,selected.get("display_name",query),area)
            except Exception as exc:
                log.exception("Boundary selection failed")
                st.error(f"Boundary resolution/validation failed ({type(exc).__name__}): {exc}. Confirm Earth Engine access, or draw an AOI inside KPK.")
    selected=st.session_state.selected_result
    preview_geometry=None
    if selected and not st.session_state.aoi:
        preview_geometry=(selected.get("_boundary") or {}).get("geometry") or osm_result_geometry(selected)
        if preview_geometry:
            try:
                from shapely.geometry import shape
                centroid=shape(preview_geometry).centroid
                center=[centroid.y,centroid.x]
            except Exception:
                center=[34.0,72.0]
        elif selected.get("lat") is not None and selected.get("lon") is not None:
            center=[float(selected["lat"]),float(selected["lon"])]
            st.markdown("**Approximate area buffer around point**")
            radius=st.number_input("Approximate buffer (km)",min_value=.1,max_value=50.,value=5.,step=.5)
            if st.button("Use approximate buffer",key="approx_buffer"):
                try:
                    initialize(settings.ee_project); province=gaul_boundary("Khyber Pakhtunkhwa",1,settings.ee_project)
                    geometry=approximate_buffer({"type":"Point","coordinates":[center[1],center[0]]},radius)
                    area=validate_polygon(geometry,province["geometry"],settings.max_aoi_km2)
                    set_aoi(geometry,f"Approximate {radius:g} km point buffer",selected.get("display_name"),area)
                except Exception as exc: st.error(f"Approximate area could not be validated: {exc}")
        else:
            center=[34.0,72.0]
            st.warning("This search result has neither a boundary nor coordinates. Draw a polygon on the map or choose another result.")
    if st.session_state.aoi:
        st.success(f"Selected: {st.session_state.aoi_name} · {st.session_state.area_km2:,.2f} km² · Source: {st.session_state.aoi_source}")
        center=None
        try:
            from shapely.geometry import shape
            c=shape(st.session_state.aoi).centroid; center=[c.y,c.x]
        except Exception: pass
    else:
        center=getattr(st.session_state,"point_center",[34.0,72.0])
    import folium
    fmap=folium.Map(location=center or [34,72],zoom_start=9,control_scale=True)
    if st.session_state.aoi:
        folium.GeoJson(st.session_state.aoi,name="Selected AOI",style_function=lambda _: {"color":"#167d61","weight":3,"fillOpacity":.08}).add_to(fmap)
    elif preview_geometry:
        folium.GeoJson(preview_geometry,name="Boundary preview",style_function=lambda _: {"color":"#167d61","weight":3,"fillOpacity":.08}).add_to(fmap)
    elif selected and selected.get("lat"):
        folium.Marker([float(selected["lat"]),float(selected["lon"])],tooltip="Geocoder point only; not an AOI").add_to(fmap)
    Draw(export=True,draw_options={"polyline":False,"circle":False,"marker":False,"circlemarker":False},edit_options={"edit":True,"remove":True}).add_to(fmap)
    map_result=st_folium(fmap,height=450,width=None,key="aoi_draw_map",returned_objects=["last_active_drawing"])
    drawn=(map_result or {}).get("last_active_drawing")
    if drawn:
        if st.button("Use drawn polygon as AOI",key="use_drawn"):
            try:
                initialize(settings.ee_project); province=gaul_boundary("Khyber Pakhtunkhwa",1,settings.ee_project)
                area=validate_polygon(drawn["geometry"],province["geometry"],settings.max_aoi_km2)
                set_aoi(drawn["geometry"],"User-drawn map polygon","Drawn area",area)
                st.rerun()
            except Exception as exc: st.error(f"Drawn AOI rejected: {exc}")

with date_tab:
    st.subheader("Date selection and acquisition search")
    st.caption("Landsat archive begins 1972-07-23; Sentinel-1 begins in October 2014; Sentinel-2 begins in mid-2015. The newest imagery may lag by several days.")
    mode=st.radio("Date mode",["Manual dates","Auto-detect flood near a date","Search a whole year","Detected KPK flood event"],horizontal=True)
    if st.session_state.imagery_mode is not None and st.session_state.imagery_mode!=mode:
        st.session_state.update(candidates={},sentinel_gallery=None,sentinel_gallery_key=None,gallery_layers=None,gallery_layers_key=None,gallery_impact=None,year_search_key=None,event_result=None,analysis_summary=None)
    st.session_state.imagery_mode=mode
    if mode=="Manual dates":
        c1,c2=st.columns(2)
        before_day=c1.date_input("Before-flood requested date",value=max(date(1900,1,1),date.today()-timedelta(days=30)),min_value=date(1900,1,1),max_value=date.today(),key="before_date")
        after_day=c2.date_input("After-flood requested date",value=max(date(1900,1,2),date.today()-timedelta(days=15)),min_value=date(1900,1,1),max_value=date.today(),key="after_date")
        area_km2=float(st.session_state.area_km2 or 0)
        before_sensors=available_sensors(before_day,area_km2)
        after_sensors=available_sensors(after_day,area_km2)
        st.caption(f"Before date sensors: {', '.join(before_sensors['optical']+before_sensors['radar']) or 'none in the satellite archive'}. After date sensors: {', '.join(after_sensors['optical']+after_sensors['radar']) or 'none in the satellite archive'}.")
        if st.session_state.aoi:
            gallery_fingerprint=hashlib.sha256(json.dumps({
                "geometry":st.session_state.aoi,"before":before_day.isoformat(),
                "after":after_day.isoformat(),"window":search_window,
                "minimum_coverage":min_coverage,
            },sort_keys=True,separators=(",",":")).encode("utf-8")).hexdigest()
            if st.session_state.sentinel_gallery_key!=gallery_fingerprint:
                st.session_state.sentinel_gallery=None
                st.session_state.candidates={}
                st.session_state.gallery_layers=None
                st.session_state.gallery_layers_key=None
                st.session_state.gallery_impact=None
                st.session_state.event_result=None
                st.session_state.analysis_summary=None
                st.session_state.sentinel_gallery_key=gallery_fingerprint
                try:
                    validate_dates(before_day,after_day)
                    if before_day<SENTINEL1_START or after_day<SENTINEL1_START:
                        raise ValueError("Sentinel-1 starts in October 2014. Choose before and after dates on or after 2014-10-03 for this radar gallery.")
                    with st.spinner("Automatically retrieving real Sentinel-1 acquisitions for the selected location and dates…"):
                        before_scenes=sentinel1_candidates(st.session_state.aoi,before_day-timedelta(days=search_window),before_day+timedelta(days=search_window+1),settings.ee_project)
                        after_scenes=sentinel1_candidates(st.session_state.aoi,after_day-timedelta(days=search_window),after_day+timedelta(days=search_window+1),settings.ee_project)
                    pair=best_compatible_sar_pair(before_scenes,after_scenes,before_day,after_day,min_coverage)
                    if pair is None:
                        raise ValueError("No chronological before/after Sentinel-1 scenes share the same orbit track and meet the coverage setting. Widen the search window or lower the minimum scene coverage.")
                    selected_before,selected_after=pair
                    st.session_state.sentinel_gallery={"before":selected_before,"after":selected_after,"error":None}
                    st.session_state.candidates={"before":[selected_before],"after":[selected_after],"radar_compatible":True,"requested_before":before_day,"requested_after":after_day}
                except Exception as exc:
                    st.session_state.sentinel_gallery={"before":None,"after":None,"error":str(exc)}
            gallery_state=st.session_state.sentinel_gallery or {}
            if gallery_state.get("before") and gallery_state.get("after"):
                st.success(f"Live Sentinel-1 imagery selected automatically · before {gallery_state['before']['date']} · after {gallery_state['after']['date']} · same {gallery_state['before']['orbit'].lower()} relative orbit {gallery_state['before']['relative_orbit']} track.")
            elif gallery_state.get("error"):
                st.warning(f"Automatic Sentinel-1 gallery: {gallery_state['error']}")
        if st.button("Search satellite imagery",type="primary"):
            if not st.session_state.aoi:
                st.warning("Select and validate an AOI first: open the Location tab, search for a district or place, then click ‘Use selected location’ (or draw an area). The workflow should show Location ✓.")
            else:
                try:
                    validate_dates(before_day,after_day)
                    if not available_sensors(before_day)["optical"] and not available_sensors(before_day)["radar"]:
                        st.warning("No satellite imagery exists for this date (earliest Landsat: 1972-07-23). Please choose a later date.")
                        st.stop()
                    if not available_sensors(after_day)["optical"] and not available_sensors(after_day)["radar"]:
                        st.warning("No satellite imagery exists for this date (earliest Landsat: 1972-07-23). Please choose a later date.")
                        st.stop()
                    with st.spinner("Searching Sentinel-1 acquisitions…"):
                        initialize(settings.ee_project)
                        area_km2=float(st.session_state.area_km2 or 0)
                        opt_b=search_historical_candidates(st.session_state.aoi,before_day,search_window,area_km2,settings.ee_project)
                        opt_a=search_historical_candidates(st.session_state.aoi,after_day,search_window,area_km2,settings.ee_project)
                        b=[]; a=[]
                        if before_day>=SENTINEL1_START:
                            try: b=sentinel1_candidates(st.session_state.aoi,before_day-timedelta(days=search_window),before_day+timedelta(days=search_window+1),settings.ee_project)
                            except Exception as exc: log.info("No Sentinel-1 before scenes: %s",exc)
                        if after_day>=SENTINEL1_START:
                            try: a=sentinel1_candidates(st.session_state.aoi,after_day-timedelta(days=search_window),after_day+timedelta(days=search_window+1),settings.ee_project)
                            except Exception as exc: log.info("No Sentinel-1 after scenes: %s",exc)
                        has_opt_b=any(scenes for sensor,scenes in opt_b.items() if sensor!="_errors")
                        has_opt_a=any(scenes for sensor,scenes in opt_a.items() if sensor!="_errors")
                        if not has_opt_b and not b: raise ValueError("No satellite scenes were returned for the before-date window. Try a wider window or another date.")
                        if not has_opt_a and not a: raise ValueError("No satellite scenes were returned for the after-date window. Try a wider window or another date.")
                        b=rank_candidates(b,before_day); a=rank_candidates(a,after_day)
                        compatible_b,compatible_a=compatible_sar_candidates(b,a)
                        radar_compatible=bool(compatible_b and compatible_a)
                        if radar_compatible: b,a=compatible_b,compatible_a
                        optical_errors=opt_b.get("_errors",[])+opt_a.get("_errors",[])
                        st.session_state.candidates={"before_optical":opt_b,"after_optical":opt_a,"before":b,"after":a,"radar_compatible":radar_compatible,"optical_errors":optical_errors,"requested_before":before_day,"requested_after":after_day}
                        st.session_state.event_result=None
                except Exception as exc: st.error(f"Imagery search failed: {exc}. Check dates, Earth Engine access, and try a wider search window.")
    elif mode=="Auto-detect flood near a date":
        approx=st.date_input("Approximate flood date",value=max(date(1900,1,1),date.today()-timedelta(days=30)),min_value=date(1900,1,1),max_value=date.today(),key="approx_date")
        scan_days=st.slider("Flood scan window (± days)",1,90,30,key="scan_window")
        auto_specs=SensorCatalog.for_date(approx,float(st.session_state.area_km2 or 0))
        auto_sensors=[spec.name for spec in auto_specs]
        preferred="Sentinel-1 SAR" if "Sentinel-1 SAR" in auto_sensors else ("Sentinel-2 MSI L2A" if "Sentinel-2 MSI L2A" in auto_sensors else auto_sensors[-1] if auto_sensors else "")
        if auto_sensors:
            auto_sensor=st.selectbox("Flood scan sensor",auto_sensors,index=auto_sensors.index(preferred),key="auto_sensor")
            selected_spec=next((spec for spec in auto_specs if spec.name==auto_sensor),None)
            if selected_spec: st.caption(f"Selected sensor: {auto_sensor} · native resolution {selected_spec.resolution_m} m · {selected_spec.cloud_method}. MSS scenes are false-colour only and have no SWIR for MNDWI flood analysis.")
        else:
            auto_sensor=""
            st.warning("No satellite sensors exist for this date. Earliest Landsat archive begins 1972-07-23.")
        if st.button("Auto-detect flood event",type="primary"):
            if not st.session_state.aoi:
                st.warning("Select and validate an AOI first in the Location tab. The workflow should show Location ✓.")
            elif not auto_sensors:
                st.warning("No satellite imagery exists for this date (earliest Landsat: 1972-07-23). Please choose a later date.")
            else:
                try:
                    with st.spinner("Comparing compatible Sentinel-1 acquisitions and calculating flooded hectares…"):
                        if auto_sensor=="Sentinel-1 SAR":
                            st.session_state.event_result=flood_time_series(st.session_state.aoi,approx,scan_days,project=settings.ee_project)
                        else:
                            st.session_state.event_result=optical_flood_time_series(st.session_state.aoi,approx,scan_days,auto_sensor,max_cloud,project=settings.ee_project)
                        event=st.session_state.event_result
                        if auto_sensor!="Sentinel-1 SAR":
                            before_scenes=[s for s in event.get("baseline_scenes",[]) if s['cloud_percent']<=max_cloud]
                            after_scenes=[s for s in event.get("candidate_scenes",[]) if s['cloud_percent']<=max_cloud]
                            st.session_state.candidates={"before_optical":{auto_sensor:before_scenes},"after_optical":{auto_sensor:after_scenes},"before":[],"after":[],"requested_before":approx-timedelta(days=scan_days),"requested_after":approx}
                        elif event.get("peak"):
                            peak=event["peak"]; before=event.get("before")
                            # Resolve scene IDs from actual acquisition metadata for image display.
                            start=approx-timedelta(days=scan_days); end=approx+timedelta(days=scan_days+1)
                            all_candidates=sentinel1_candidates(st.session_state.aoi,start,end,settings.ee_project)
                            p=[x for x in all_candidates if x['date']==peak['date'] and x['orbit']==peak['orbit'] and x['relative_orbit']==peak['relative_orbit']]
                            b=[x for x in all_candidates if before and x['date']==before['date'] and x['orbit']==before['orbit'] and x['relative_orbit']==before['relative_orbit']]
                            b=[x for x in b if x['coverage']>=min_coverage]; p=[x for x in p if x['coverage']>=min_coverage]
                            st.session_state.candidates={"before":b,"after":p,"requested_before":before['date'] if before else approx,"requested_after":peak['date']}
                except Exception as exc:
                    log.exception("Event scan failed")
                    st.error(f"Automatic event scan failed: {exc}. Verify Earth Engine permissions or widen the window/change AOI.")
        event=st.session_state.event_result
        if event and event.get("rows"):
            sensor_title=event['rows'][0].get('sensor','Sentinel-1') if event['rows'] else 'Satellite'
            chart=px.line(pd.DataFrame(event['rows']),x="date",y="flood_ha",markers=True,title=f"Observed {sensor_title} flooded area time series (ha)")
            if event.get('peak'): chart.add_scatter(x=[event['peak']['date']],y=[event['peak']['flood_ha']],mode="markers",name="Selected flood peak",marker={"size":13,"symbol":"star"})
            st.plotly_chart(chart,width="stretch")
            if event.get("peak"):
                peak=event['peak']; detail=f" · orbit {peak['orbit']} relative orbit {peak['relative_orbit']}" if peak.get('orbit') else ""
                st.success(f"Flood peak acquisition: {peak['date']} · {peak.get('sensor','Sentinel-1 SAR')} · {peak['flood_ha']:,.2f} ha{detail}")
            else: st.warning(event.get("message") or "No significant flood signal detected in this window for this area.")
    elif mode=="Search a whole year":
        current_year=date.today().year
        years=list(range(current_year,2014,-1))
        preferred_year=current_year-1 if current_year>2015 else current_year
        year=st.selectbox("Year to search",years,index=years.index(preferred_year),key="flood_search_year")
        st.caption("Scans the selected calendar year at the current AOI using real Sentinel-1 acquisitions and same-orbit recent baselines. No random date is needed; the year controls the full scan.")
        if year==current_year:
            st.caption("The current year is incomplete; only acquisitions already available in Earth Engine can be searched.")
        year_fingerprint=hashlib.sha256(json.dumps({"geometry":st.session_state.aoi,"year":year},sort_keys=True,separators=(",",":")).encode("utf-8")).hexdigest() if st.session_state.aoi else None
        scan_year_automatically=bool(year_fingerprint and st.session_state.year_search_key!=year_fingerprint)
        if scan_year_automatically:
            st.session_state.year_search_key=year_fingerprint
        if st.button("Search for flood in this year",type="primary",key="search_full_year") or scan_year_automatically:
            if not st.session_state.aoi:
                st.warning("Select and validate a KPK area in the Location tab first.")
            else:
                st.session_state.update(candidates={},sentinel_gallery=None,sentinel_gallery_key=None,gallery_layers=None,gallery_layers_key=None,gallery_impact=None,event_result=None,analysis_summary=None)
                try:
                    with st.spinner(f"Scanning all available Sentinel-1 acquisitions in {year} and comparing same-orbit baselines…"):
                        yearly_event=sentinel1_flood_search_year(st.session_state.aoi,year,project=settings.ee_project)
                    st.session_state.event_result=yearly_event
                    if yearly_event.get("peak"):
                        peak=yearly_event["peak"]
                        scene_start=max(SENTINEL1_START,peak["date"]-timedelta(days=90))
                        scene_end=peak["date"]+timedelta(days=1)
                        scenes=sentinel1_candidates(st.session_state.aoi,scene_start,scene_end,settings.ee_project)
                        before_scenes=[scene for scene in scenes if scene["date"]<peak["date"] and scene["date"]>=peak["date"]-timedelta(days=90)]
                        after_scenes=[scene for scene in scenes if scene["date"]==peak["date"]]
                        before_scenes=[scene for scene in before_scenes if scene["orbit"]==peak["orbit"] and scene["relative_orbit"]==peak["relative_orbit"]]
                        after_scenes=[scene for scene in after_scenes if scene["orbit"]==peak["orbit"] and scene["relative_orbit"]==peak["relative_orbit"]]
                        pair=best_compatible_sar_pair(before_scenes,after_scenes,peak["date"],peak["date"],min_coverage)
                        if pair:
                            before_scene,after_scene=pair
                            st.session_state.sentinel_gallery={"before":before_scene,"after":after_scene,"error":None}
                            st.session_state.candidates={"before":[before_scene],"after":[after_scene],"radar_compatible":True,"requested_before":before_scene["date"],"requested_after":after_scene["date"]}
                        else:
                            st.session_state.sentinel_gallery={"before":None,"after":None,"error":"A flood signal was detected, but no matching before/after scene pair met the current coverage setting for its orbit."}
                        st.success(f"Flood signal detected on {peak['date']} · {peak['flood_ha']:,.2f} ha · {peak['orbit']} relative orbit {peak['relative_orbit']}. See the imagery and analysis tabs for the real images and method details.")
                    else:
                        st.warning(yearly_event.get("message") or f"No flood detected in {year} for this area.")
                except Exception as exc:
                    log.exception("Yearly Sentinel-1 flood search failed")
                    st.error(f"Could not complete the {year} flood search: {exc}. Check Earth Engine access and try again.")
    else:
        st.caption("Pick a flood event found by the automatic KPK search (Dashboard), or search a past period. The event's district becomes the AOI and its actual same-track before/after Sentinel-1 acquisitions are loaded into the imagery, map and analysis tabs.")
        run=st.session_state.get("monitoring_run") or latest_monitoring_run(settings.analysis_history_db)
        options=[]
        for e in ((run or {}).get("recent") or {}).get("events") or []: options.append(("Latest monitoring window",e))
        for h in (run or {}).get("historical") or []:
            for e in h.get("events") or []: options.append((h.get("label","Historical"),e))
        found=st.session_state.get("manual_event_search")
        if found:
            for e in found.get("events") or []: options.append((f"Search {found['window'][0]} → {found['window'][1]}",e))
        with st.form("kpk_period_search"):
            p1,p2,p3=st.columns(3,vertical_alignment="bottom")
            period_start=p1.date_input("Search from",value=date(date.today().year-1,7,1),min_value=date(2015,1,1),max_value=date.today())
            period_end=p2.date_input("to",value=date(date.today().year-1,10,1),min_value=date(2015,1,2),max_value=date.today())
            searched=p3.form_submit_button("Search KPK for events",icon=":material/search:")
        if searched:
            if period_end<=period_start or (period_end-period_start).days>185:
                st.warning("Choose an end date after the start date and a period of at most 6 months.")
            else:
                try:
                    from src.flood_monitoring import discover_events
                    bar=st.progress(0.0,"Screening KPK…")
                    result=discover_events(period_start,period_end,settings,settings.ee_project,lambda f,t:bar.progress(min(f,1.0),t))
                    result.pop("rows",None)
                    st.session_state.manual_event_search=json.loads(json.dumps(result,default=str))
                    st.rerun()
                except Exception as exc:
                    st.error(f"KPK event search failed: {exc}")
        if options:
            event_labels=[f"{src} · {e['status']} · {e['district']} · peak {e['peak_date']} · {float(e['peak_excess_ha']):,.0f} ha above normal" for src,e in options]
            index=st.selectbox("Flood event",range(len(event_labels)),format_func=event_labels.__getitem__)
            if st.button("Load this event",type="primary",key="load_kpk_event"):
                chosen=options[index][1]
                try:
                    from shapely.geometry import shape
                    from pyproj import Geod
                    geometry=district_options().get(chosen["district"])
                    with st.spinner("Retrieving the event's actual same-track Sentinel-1 acquisitions…"):
                        before_scene,after_scene,baseline=event_scene_pair(chosen,geometry,settings.ee_project)
                    area=abs(Geod(ellps="WGS84").geometry_area_perimeter(shape(geometry))[0])/1e6
                    set_aoi(geometry,"FAO GAUL 2025 level 2 (simplified)",f"{chosen['district']} district",area,
                            sentinel_gallery={"before":before_scene,"after":after_scene,"baseline":baseline,"error":None},
                            candidates={"before":[before_scene],"after":[after_scene],"radar_compatible":True,
                                        "requested_before":before_scene["date"],"requested_after":after_scene["date"]})
                    st.success(f"Loaded {chosen['district']}: before {before_scene['date']} → after {after_scene['date']} ({after_scene['orbit'].lower()} track {after_scene['relative_orbit']}). Open the imagery, map and analysis tabs.")
                except Exception as exc:
                    st.error(f"Could not load the event imagery: {exc}")
        else:
            st.info("No KPK events are stored yet. Open the Dashboard to run the automatic search, or search a period above.")
    if st.session_state.candidates:
        cs=st.session_state.candidates
        before_optical=sum(len(v) for k,v in cs.get('before_optical',{}).items() if k!="_errors"); after_optical=sum(len(v) for k,v in cs.get('after_optical',{}).items() if k!="_errors")
        st.write(f"Optical scenes: {before_optical} before / {after_optical} after across matching sensors. Sentinel-1 scenes: {len(cs.get('before',[]))} before / {len(cs.get('after',[]))} after.")
        if cs.get('before') and cs.get('after') and not cs.get('radar_compatible',True):
            st.warning("No before/after Sentinel-1 pair shares both orbit direction and relative orbit. These radar scenes are not comparable; widen the date windows or use an optical pair.")

with image_tab:
    st.subheader("Satellite imagery")
    cs=st.session_state.candidates
    gallery=st.session_state.sentinel_gallery or {}
    before_scene=gallery.get("before"); after_scene=gallery.get("after")
    if before_scene and after_scene:
        st.markdown("### Real Sentinel-1 flood imagery")
        st.caption("Live XYZ map tiles are rendered directly from Google Earth Engine. Dates below are the actual acquisition times, not the requested dates.")
        try:
            import folium
            from shapely.geometry import shape
            layers_key=hashlib.sha256(json.dumps({"aoi":st.session_state.aoi,"before":before_scene["id"],"after":after_scene["id"]},sort_keys=True,separators=(",",":")).encode("utf-8")).hexdigest()
            if st.session_state.gallery_layers_key!=layers_key:
                tile_layers=sentinel1_gallery_tile_layers(before_scene,after_scene,st.session_state.aoi,project=settings.ee_project,**RULE)
                if gallery.get("baseline"):
                    # KPK-event mode: flood is measured against the same-track 60-day median baseline.
                    from src.satellite_retrieval import ee_tile_url
                    ee_mod=initialize(settings.ee_project)
                    flood_img=s1_new_water(s1_scene_image(gallery["baseline"],ee_mod),s1_scene_image(after_scene,ee_mod),ee_mod,**RULE)
                    tile_layers["flood"]=ee_tile_url(flood_img.selfMask().clip(ee_mod.Geometry(st.session_state.aoi)),{"min":1,"max":1,"palette":["18d69e"]})
                st.session_state.gallery_impact=None
                try:
                    impact=sentinel1_pair_agricultural_impact(gallery.get("baseline") or before_scene,after_scene,st.session_state.aoi,project=settings.ee_project,scale=settings.event_scale_m,**RULE)
                    tile_layers.update(impact.pop("layers"))
                    st.session_state.gallery_impact=impact
                except Exception as impact_error:
                    log.warning("ESA WorldCover exposure unavailable: %s",impact_error)
                    st.info(f"Cropland exposure could not be calculated from ESA WorldCover for this pair: {impact_error}")
                from src.satellite_retrieval import sentinel2_true_color_layer
                for key,(start,end) in {"s2_before":(before_scene["date"]-timedelta(days=20),before_scene["date"]+timedelta(days=1)),
                                        "s2_after":(after_scene["date"],min(after_scene["date"]+timedelta(days=10),date.today()+timedelta(days=1)))}.items():
                    try:
                        tile_layers[key]=sentinel2_true_color_layer(st.session_state.aoi,start,end,settings.ee_project)
                        if tile_layers[key] is None: tile_layers[f"{key}_note"]=f"No Sentinel-2 scene between {start} and {end}."
                    except Exception as s2_error:
                        tile_layers[key]=None; tile_layers[f"{key}_note"]=f"Sentinel-2 unavailable: {s2_error}"
                st.session_state.gallery_layers=tile_layers
                st.session_state.gallery_layers_key=layers_key
            tile_layers=st.session_state.gallery_layers
            from src.map_visualization import framed_map
            def gallery_map(layer_specs, map_key):
                fmap=framed_map(st.session_state.aoi,width_px=420,height_px=310)
                for url,name,attr,opacity in layer_specs:
                    folium.TileLayer(tiles=url,name=name,attr=attr,overlay=True,control=False,opacity=opacity,max_zoom=19).add_to(fmap)
                folium.GeoJson(st.session_state.aoi,name="Selected AOI",style_function=lambda _: {"color":"#f6c85f","weight":2,"fillOpacity":0}).add_to(fmap)
                st_folium(fmap,height=310,width=None,key=map_key,returned_objects=[])
            st.markdown("**Sentinel-1 radar (VV backscatter: dark = smooth surface or water)**")
            cards=st.columns(3,gap="small")
            for col,title,key,scene in zip(cards,["Before flood","After flood","Detected flood change"],["before","after","flood"],[before_scene,after_scene,after_scene]):
                with col.container(border=True,key=f"sar_{key}_card"):
                    st.markdown(f"**{title}**")
                    if key=="flood":
                        specs=[(tile_layers["after"],"Sentinel-1 after",tile_layers["source"],1.0),(tile_layers["flood"],"Detected new water","Sentinel-1 change",.9)]
                        if tile_layers.get("exposed_cropland"): specs.append((tile_layers["exposed_cropland"],"New water over cropland","ESA WorldCover 2021",.95))
                    else:
                        specs=[(tile_layers[key],f"Sentinel-1 {key}",tile_layers["source"],1.0)]
                    gallery_map(specs,f"sar_gallery_map_{key}")
                    if key=="flood":
                        st.caption(f"Green = new radar-dark surface ({before_scene['date']} → {after_scene['date']}), orange = on cropland · rule: VV change < {RULE['change_db']} dB, VV < {RULE['water_db']} dB, VH < {RULE['vh_water_db']} dB; permanent water and steep/high terrain masked")
                        impact=st.session_state.gallery_impact or {}
                        if impact:
                            st.caption(f"New water: {impact['flood_ha']:,.1f} ha · on WorldCover cropland: {impact['exposed_cropland_ha']:,.1f} ha")
                    else:
                        st.caption(f"Acquired {scene['time_utc'].replace('+00:00',' UTC')} · {scene.get('platform') or 'Sentinel-1'} · {scene['orbit']} orbit {scene['relative_orbit']} · AOI coverage {scene.get('coverage',0):.0f}%")
            st.markdown("**Sentinel-2 natural colour (what the ground looked like; water appears dark blue/brown)**")
            st.caption("Radar can confuse wet or freshly ploughed fields with water. Compare with these real optical images: a real flood is visible as water here when skies are clear.")
            ocols=st.columns(3,gap="small")
            for col,key,title in zip(ocols,["s2_before","s2_after"],["Before (optical)","After (optical)"]):
                with col.container(border=True,key=f"{key}_card"):
                    st.markdown(f"**{title}**")
                    layer=tile_layers.get(key)
                    if layer:
                        gallery_map([(layer["url"],title,layer["source"],1.0)],f"{key}_map")
                        st.caption(f"Sentinel-2 dates composited: {', '.join(layer['dates'])} · clouds masked (Cloud Score+)")
                    else:
                        st.info(tile_layers.get(f"{key}_note") or "No Sentinel-2 image is available in this window.")
            with ocols[2].container(border=True,key="s2_note_card"):
                st.markdown("**How to read these**")
                st.markdown("- Green/orange in the radar map marks surfaces that became radar-dark.\n"
                            "- If the optical *after* image shows dry fields there, the signal is soil moisture or field preparation, **not a flood**.\n"
                            "- Monsoon floods are often under clouds; then only radar is available.")
        except Exception as exc:
            log.exception("Earth Engine Sentinel-1 gallery rendering failed")
            st.error(f"Could not build the real Earth Engine satellite gallery: {exc}")
    elif gallery.get("error"):
        st.warning(f"Sentinel-1 gallery unavailable: {gallery['error']}")
    if not cs:
        if not gallery.get("error"):
            st.warning("No acquisitions selected yet. Select an AOI and before/after dates to retrieve the Sentinel-1 gallery.")
    else:
        st.markdown("### Other available sensor imagery")
        left,right=st.columns(2)
        if cs.get("optical_errors"):
            with st.expander("Why optical scenes were unavailable"):
                for item in cs["optical_errors"]: st.caption(f"{item['sensor']}: {item['error']}")
        optical_cards={}
        for col,key,title,requested in [(left,"before","Before",cs.get("requested_before")),(right,"after","After",cs.get("requested_after"))]:
            optical=cs.get(f"{key}_optical",{})
            sensors=[name for name,scenes in optical.items() if name!="_errors" and scenes]
            if sensors:
                chosen_sensor=col.selectbox(f"{title} optical sensor",sensors,key=f"sensor_{key}")
                all_scenes=optical[chosen_sensor]
                usable=[s for s in all_scenes if s["cloud_percent"]<=max_cloud]
                if not usable:
                    best=min(all_scenes,key=lambda s:s["cloud_percent"])
                    col.warning(f"Optical imagery unavailable (cloud cover {best['cloud_percent']:.1f}%). Widen the date window, increase the cloud limit, or use radar if available.")
                else:
                    display_mode=col.radio(f"{title} rendering",["Cloud-masked composite","Single least-cloudy scene"],horizontal=True,key=f"render_{key}")
                    if display_mode=="Single least-cloudy scene": used=[min(usable,key=lambda s:(s['cloud_percent'],abs((s['date']-requested).days if requested else 0)))]
                    else: used=sorted(usable,key=lambda s:s["date"])
                    spec=all_scenes[0]["spec"]
                    try:
                        view_options=["Natural/false colour"]+["MNDWI water index"] if (key=="after" and spec.swir1) else ["Natural/false colour"]
                        view=col.radio(f"{title} optical view",view_options,horizontal=True,key=f"view_{key}")
                        png=optical_thumbnail([s["id"] for s in used],spec,st.session_state.aoi,project=settings.ee_project,view="mndwi" if view=="MNDWI water index" else "true_color")
                        view_caption="MNDWI (Green − SWIR1) / (Green + SWIR1)" if view=="MNDWI water index" else ("false colour (MSS has no blue band)" if spec.false_color_only else "natural colour true composite")
                        optical_cards[key]={"png":png,"sensor":chosen_sensor,"spec":spec,"used":used,"caption":view_caption,"requested":requested}
                        dates=", ".join(f"{s['date']} {s['time_utc'][11:19]} UTC" for s in used)
                        difference=min(abs((s["date"]-requested).days) for s in used) if requested else 0
                        scene_quality="; ".join(f"{s['date']}: cloud {s['cloud_percent']:.1f}%, footprint {s['coverage_percent']:.1f}%, cloud-free AOI approx {s['cloud_free_aoi_percent']:.1f}%" for s in used)
                        col.caption(f"{dates} · {difference} day(s) from requested date · resolution {spec.resolution_m} m")
                        if spec.name.startswith("Sentinel-2"): reason="Selected for its 10 m multispectral optical bands and frequent revisit."
                        elif spec.name.startswith("Landsat 1") or spec.name.startswith("Landsat 2") or spec.name.startswith("Landsat 3"): reason="Only the historical MSS archive is available; it lacks a blue band, so this is labelled false colour."
                        elif spec.name.startswith("Landsat 7"): reason="Selected historical 30 m surface-reflectance data; Landsat 7 has SLC-off gaps after 2003-05-31."
                        else: reason=f"Selected real {spec.name} surface-reflectance imagery at {spec.resolution_m} m."
                        col.caption(reason)
                        if chosen_sensor=="Landsat 7 ETM+" and requested and requested>=date(2003,5,31): col.caption("Caveat: Landsat 7 SLC-off stripes may leave gaps in the composite.")
                    except Exception as exc: col.warning(f"Real optical thumbnail could not be retrieved: {exc}")
            else:
                col.warning(f"No optical acquisitions were returned for the {title.lower()} date window.")
        if optical_cards:
            st.markdown("#### Real satellite observations")
            cards=st.columns(3)
            for card,title,key in zip(cards,["Before flood","After flood","Water signal (after)"],["before","after","after"]):
                data=optical_cards.get(key)
                with card.container(border=True):
                    st.markdown(f"**{title}**")
                    if data:
                        image=data["png"]
                        caption=f"{data['sensor']} · {data['caption']}"
                        if title=="Water signal (after)" and data["spec"].swir1:
                            try:
                                image=optical_thumbnail([s["id"] for s in data["used"]],data["spec"],st.session_state.aoi,project=settings.ee_project,view="mndwi")
                                caption="Earth Engine MNDWI water-index visualization; not a ground-level photo"
                            except Exception as exc:
                                st.caption(f"Water-index preview unavailable: {exc}")
                        elif title=="Water signal (after)":
                            st.caption("Water-index view requires a sensor with a SWIR band.")
                        st.image(image,width="stretch")
                        st.caption(caption)
                    else:
                        st.info(f"No {title.lower()} optical image is available for this date window.")
        radar_before=cs.get("before",[]); radar_after=cs.get("after",[])
        if radar_before or radar_after:
            with st.expander("Radar view (Sentinel-1; separate from natural-colour optical view)"):
                if not cs.get("radar_compatible",True):
                    st.warning("These before/after radar scenes come from different orbits. They are real Earth Engine images, but should not be interpreted as a valid change pair.")
                l,r=st.columns(2)
                for col,key,title,options,requested in [(l,"before","Before",radar_before,cs.get("requested_before")),(r,"after","After",radar_after,cs.get("requested_after"))]:
                    if not options: col.caption(f"No Sentinel-1 acquisition available for {title.lower()} date."); continue
                    idx=col.selectbox(f"{title} Sentinel-1 scene",range(len(options)),format_func=lambda i,o=options:f"{o[i]['date']} · {o[i]['orbit']} · rel {o[i]['relative_orbit']}",key=f"radar_{key}")
                    scene=options[idx]
                    try:
                        radar_view=col.radio(f"{title} radar display",["VV grayscale","VV/VH false colour"],horizontal=True,key=f"radar_view_{key}")
                        if radar_view=="VV/VH false colour":
                            url=scene_thumbnail(scene["id"],st.session_state.aoi,bands=["VV","VH","VV-VH"],project=settings.ee_project)
                            label="False colour: R=VV, G=VH, B=VV−VH"
                        else:
                            url=scene_thumbnail(scene["id"],st.session_state.aoi,project=settings.ee_project)
                            label="VV backscatter, dB"
                        col.image(url,caption=f"{label} · {scene['time_utc']} UTC",width="stretch")
                        col.caption(f"Sentinel-1{scene.get('platform') or ''} · {scene.get('orbit')} · relative orbit {scene.get('relative_orbit')} · polarizations {', '.join(scene.get('polarizations',[]))} · coverage {scene.get('coverage',0):.1f}%")
                    except Exception as exc: col.warning(f"Real radar thumbnail unavailable: {exc}")

with floodmap_tab:
    st.subheader("Interactive flood and agricultural exposure map")
    gallery=st.session_state.sentinel_gallery or {}
    layers=st.session_state.gallery_layers or {}
    if gallery.get("before") and gallery.get("after") and layers:
        import folium
        from shapely.geometry import shape
        aoi_shape=shape(st.session_state.aoi); centroid=aoi_shape.centroid
        from src.map_visualization import zoom_for_bounds, SMALL_ATTRIBUTION
        fmap=folium.Map(location=[centroid.y,centroid.x],zoom_start=zoom_for_bounds(aoi_shape.bounds,1100,620),tiles=None,control_scale=True)
        fmap.get_root().header.add_child(folium.Element(SMALL_ATTRIBUTION))
        folium.TileLayer(tiles="https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",attr="Basemap: Esri, Maxar",name="Satellite basemap",overlay=False,control=False).add_to(fmap)
        folium.TileLayer(tiles=layers["after"],name=f"After imagery · {gallery['after']['date']}",attr=layers["source"],overlay=False,control=False,max_zoom=19).add_to(fmap)
        folium.TileLayer(tiles=layers["before"],name=f"Before imagery · {gallery['before']['date']}",attr=layers["source"],overlay=True,show=False,control=True,max_zoom=19).add_to(fmap)
        folium.TileLayer(tiles=layers["flood"],name="Detected flood signal",attr=layers["source"],overlay=True,show=True,control=True,opacity=.85,max_zoom=19).add_to(fmap)
        if layers.get("cropland"):
            folium.TileLayer(tiles=layers["cropland"],name="ESA WorldCover 2021 cropland",attr="ESA WorldCover 2021 · class 40",overlay=True,show=False,control=True,opacity=.8,max_zoom=19).add_to(fmap)
        if layers.get("exposed_cropland"):
            folium.TileLayer(tiles=layers["exposed_cropland"],name="Flood signal over cropland",attr="Sentinel-1 change × ESA WorldCover",overlay=True,show=True,control=True,opacity=.95,max_zoom=19).add_to(fmap)
        folium.GeoJson(st.session_state.aoi,name="Selected AOI boundary",style_function=lambda _: {"color":"#f6c85f","weight":2,"fillOpacity":0}).add_to(fmap)
        folium.LayerControl(collapsed=False).add_to(fmap)
        st_folium(fmap,height=620,width=None,key="flood_impact_layer_map",returned_objects=[])
        impact=st.session_state.gallery_impact or {}
        if impact:
            st.caption(f"Detected flood signal: {impact['flood_ha']:,.1f} ha · Mapped cropland exposed: {impact['exposed_cropland_ha']:,.1f} ha · Source: {impact['cropland_source']}. Crop type and confirmed crop damage are unavailable.")
    else:
        st.info("Select a location and compatible before/after Sentinel-1 dates to load real imagery and impact layers.")

with analysis_tab:
    st.subheader("Flood detection and agricultural exposure")
    st.info("Method: Sentinel-1 VV change thresholds with permanent-water and slope masks. This is a transparent rule-based detector, not a trained AI model.")
    st.caption(f"Pairwise rule: after-minus-before VV < {RULE['change_db']} dB, after VV < {RULE['water_db']} dB and VH < {RULE['vh_water_db']} dB (when acquired), {RULE.get('smoothing_m',0):.0f} m speckle filter; JRC permanent water, slopes > {RULE['max_slope_deg']}°, HAND > {RULE['max_hand_m']} m and elevation > {RULE['max_elevation_m']} m are excluded. ESA WorldCover 2021 class 40 indicates generic cropland.")
    event=st.session_state.event_result
    impact=st.session_state.gallery_impact or {}
    gallery=st.session_state.sentinel_gallery or {}
    before_scene=gallery.get("before"); after_scene=gallery.get("after")
    peak=event.get("peak") if event else None
    if peak or impact:
        flood=float(impact.get("flood_ha",peak.get("flood_ha",0) if peak else 0))
        ag_ha=impact.get("exposed_cropland_ha")
        ag_pct=impact.get("exposed_cropland_percent")
        pct=100*flood/(float(st.session_state.area_km2 or 0)*100) if st.session_state.area_km2 else None
        requested_after=event.get("requested_date") if event else None
        if requested_after is None: requested_after=st.session_state.candidates.get("requested_after")
        actual_after=after_scene.get("date") if after_scene else (peak.get("date") if peak else None)
        difference=abs((actual_after-requested_after).days) if actual_after and requested_after else None
        coverage=min([float(scene.get("coverage",0)) for scene in (before_scene,after_scene) if scene] or [0])
        confidence="Medium" if coverage>=85 and before_scene and after_scene else "Low"
        caveats=["Floodwater is a satellite-derived indicator, not field confirmation","WorldCover is a 2021 generic land-cover map and may not reflect current planting","Crop types and confirmed crop damage are unavailable","Ground validation required"]
        if before_scene and after_scene: caveats.append(f"SAR orbit: {after_scene.get('orbit')} relative orbit {after_scene.get('relative_orbit')}")
        priority=inspection_priority(ag_ha or 0,ag_pct or 0,confidence=.65 if confidence=="Medium" else .35) if ag_ha is not None else None
        summary={"location":st.session_state.aoi_name,"requested_before":str(st.session_state.candidates.get("requested_before") or "unavailable"),"requested_after":str(requested_after) if requested_after else "unavailable","before_acquisition":str(before_scene.get("time_utc")) if before_scene else "unavailable","after_acquisition":str(after_scene.get("time_utc")) if after_scene else str(actual_after or "unavailable"),"peak_date":str(actual_after or "unavailable"),"day_difference":difference,"sensor":"Sentinel-1 SAR","satellite_source":"Copernicus Sentinel-1 GRD via Google Earth Engine","resolution_m":10,"orbit":after_scene.get("orbit") if after_scene else (peak.get("orbit") if peak else None),"relative_orbit":after_scene.get("relative_orbit") if after_scene else (peak.get("relative_orbit") if peak else None),"flood_ha":flood,"yearly_peak_flood_ha":float(peak["flood_ha"]) if peak else None,"aoi_percent":pct,"ag_ha":ag_ha,"total_cropland_ha":impact.get("total_cropland_ha"),"ag_pct":ag_pct,"cropland_source":impact.get("cropland_source","unavailable"),"inspection_priority":priority,"confidence":confidence,"caveats":caveats}
        narrative=rule_based_narrative(summary)
        summary["Analysis summary"]=narrative
        if event and event.get("rows"):
            summary["Observed time series (date: flooded ha)"]="; ".join(f"{row['date']}: {row['flood_ha']:.2f}" for row in event['rows'])
        crop_key=hashlib.sha256(json.dumps({"aoi":st.session_state.aoi,"before":(before_scene or {}).get("id"),"after":(after_scene or {}).get("id")},sort_keys=True).encode()).hexdigest()
        crop_result=st.session_state.crop_assessment if (st.session_state.crop_assessment or {}).get("key")==crop_key else None
        if crop_result:
            summary["crop_rows"]=crop_result["rows"]; summary["crop_type_source"]=crop_result["source"]
            summary["ndvi_source"]=crop_result["ndvi_source"]; summary["season"]=crop_result["season"]
        st.session_state.analysis_summary=summary
        try:
            save_analysis({"location":st.session_state.aoi_name,"geometry":st.session_state.aoi,"summary":summary,"event":event or {},"before":before_scene,"after":after_scene,"requested_before":st.session_state.candidates.get("requested_before"),"requested_after":requested_after},settings.analysis_history_db)
        except Exception as exc: st.warning(f"Analysis was computed but could not be saved to local history: {exc}")
        st.markdown("### Analysis summary"); st.write(narrative)
        m1,m2,m3=st.columns(3)
        m1.metric("Detected flooded area",f"{flood:,.2f} ha")
        m2.metric("Mapped cropland exposed",f"{ag_ha:,.2f} ha" if ag_ha is not None else "Unavailable")
        m3.metric("Inspection priority",priority or "Unavailable")
        if ag_pct is not None: st.caption(f"{ag_pct:.2f}% of WorldCover-mapped cropland within the AOI overlaps the detected flood mask.")
        st.metric("Confirmed crop damage","Unavailable · field data required")
        st.markdown("### Crop-type exposure and damage risk")
        if before_scene and after_scene:
            st.caption("Overlays ESA WorldCereal 2021 crop types on the flood extent and compares Cloud Score+ masked Sentinel-2 NDVI before and after. Risk uses NDVI change on flooded fields minus unflooded fields of the same crop.")
            if st.button("Run crop damage-risk assessment",type="primary",key="run_crop_assessment"):
                try:
                    with st.spinner("Measuring crop-wise exposure and Sentinel-2 NDVI change…"):
                        ee_mod=initialize(settings.ee_project)
                        reference=gallery.get("baseline") or before_scene
                        flood_img=s1_new_water(s1_scene_image(reference,ee_mod),s1_scene_image(after_scene,ee_mod),ee_mod,**RULE)
                        result=event_crop_assessment(flood_img,st.session_state.aoi,before_scene["date"],after_scene["date"],settings.event_scale_m,settings.ee_project)
                    result.pop("layers",None)
                    st.session_state.crop_assessment={**result,"key":crop_key}
                    st.rerun()
                except Exception as exc:
                    st.error(f"Crop assessment failed: {exc}")
            if crop_result:
                st.dataframe(pd.DataFrame([{"Crop":r["crop"],"Affected area (ha)":r["affected_ha"],"Crop area in AOI (ha)":r["crop_area_ha"],
                    "NDVI change (flooded − unflooded)":r["relative_ndvi_change"],"Damage risk":r["risk"]} for r in crop_result["rows"]]),
                    hide_index=True,width="stretch",column_config={"Affected area (ha)":st.column_config.NumberColumn(format="%.1f"),
                    "Crop area in AOI (ha)":st.column_config.NumberColumn(format="%.0f"),"NDVI change (flooded − unflooded)":st.column_config.NumberColumn(format="%+.3f")})
                st.caption(f"Season: {crop_result['season']} · Sentinel-2 scenes pre/post: {crop_result['s2_pre_scenes']}/{crop_result['s2_post_scenes']} · {crop_result['source']}. Risk classes are preliminary, not loss percentages.")
        else:
            st.caption("Crop assessment needs a Sentinel-1 before/after pair.")
        if os.getenv("ANTHROPIC_API_KEY") and st.button("Generate AI explanation"):
            try:
                response=requests.post("https://api.anthropic.com/v1/messages",headers={"x-api-key":os.getenv("ANTHROPIC_API_KEY"),"anthropic-version":"2023-06-01","content-type":"application/json"},json={"model":"claude-3-5-haiku-latest","max_tokens":400,"system":"Explain only supplied statistics. Do not invent numbers, crop types, damage percentages, or facts. State unavailable values as unavailable.","messages":[{"role":"user","content":json.dumps(summary)}]},timeout=30)
                response.raise_for_status(); st.markdown("**AI-generated explanation of computed results**"); st.write(response.json()['content'][0]['text'])
            except Exception: st.caption("AI explanation unavailable; the rule-based summary above remains available.")
    elif event: st.warning(event.get("message","No significant flood signal detected in this window for this area."))
    else: st.warning("Select before/after imagery or run a year/event search to calculate flood and agricultural exposure.")
    st.caption("Satellite-derived estimates require ground validation. Flooded agricultural land does not automatically mean destroyed crops.")
