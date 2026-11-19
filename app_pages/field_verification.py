"""Field Verification: inspection priorities from the current event and ground observations.

Recorded observations are the ground truth needed before any damage model or
loss estimate could be validated; they are stored locally in SQLite.
"""
from datetime import date
import pandas as pd
import streamlit as st
from config.settings import settings
from src.analysis_history import latest_monitoring_run, save_field_observation, list_field_observations

run=st.session_state.get("monitoring_run") or latest_monitoring_run(settings.analysis_history_db)
event=st.session_state.get("dashboard_event") or (run or {}).get("selected")
detail=st.session_state.get("dashboard_detail") if st.session_state.get("dashboard_event") else (run or {}).get("detail")

st.markdown("#### Inspection priorities")
if detail and event:
    st.caption(f"Event: {event['district']} · {event['status']} · peak acquisition {event['peak_date']}. "
               "Priority = High when ≥ 50 ha of flooded farmland shows a high-risk NDVI drop; Medium when ≥ 50 ha is medium/high risk or ≥ 500 ha of farmland is flooded.")
    rows=detail.get("tehsil_priorities") or []
    if rows:
        st.dataframe(pd.DataFrame([{"Tehsil":r["area"].title(),"Priority":r["priority"],"Flooded area (ha)":r["flood_ha"],
                                    "Flooded farmland (ha)":r["flooded_cropland_ha"],"High-risk farmland (ha)":r["high_risk_ha"],
                                    "Medium-risk farmland (ha)":r["medium_risk_ha"]} for r in rows]),
                     hide_index=True,width="stretch",column_config={c:st.column_config.NumberColumn(format="%.0f") for c in
                     ("Flooded area (ha)","Flooded farmland (ha)","High-risk farmland (ha)","Medium-risk farmland (ha)")})
        st.caption("Tehsil boundaries: geoBoundaries PAK ADM3 (2017 census units, ODbL 1.0).")
    else:
        st.info(detail.get("tehsil_priority_note") or "No tehsil-level priorities are available for this event.")
else:
    st.info("No analyzed event is available. Open the Dashboard first.")

st.markdown("#### Record a field observation")
with st.form("field_observation",clear_on_submit=True):
    c1,c2,c3=st.columns(3)
    district=c1.text_input("District",value=(event or {}).get("district",""))
    latitude=c2.number_input("Latitude",min_value=29.0,max_value=37.5,value=34.0,format="%.5f")
    longitude=c3.number_input("Longitude",min_value=69.0,max_value=74.5,value=71.5,format="%.5f")
    c4,c5,c6=st.columns(3)
    observed_on=c4.date_input("Observation date",value=date.today(),max_value=date.today())
    flooding=c5.selectbox("Flooding observed?",["Yes – standing water","Yes – recent water marks/silt","No"])
    crop=c6.selectbox("Crop in field",["Maize","Rice","Sugarcane","Wheat","Cotton","Vegetables","Fodder","Orchard","Fallow/none","Other"])
    c7,c8=st.columns(2)
    damage=c7.selectbox("Damage observed",["None","Low (<25% of plants affected)","Medium (25–50%)","High (>50%)","Total loss"])
    observer=c8.text_input("Observer")
    notes=st.text_area("Notes")
    if st.form_submit_button("Save observation",type="primary"):
        save_field_observation({"event_key":f"{(event or {}).get('district')}|{(event or {}).get('peak_date')}" if event else None,
                                "district":district,"latitude":latitude,"longitude":longitude,"observed_on":observed_on,
                                "flooding_observed":flooding,"crop":crop,"damage_observed":damage,"observer":observer,"notes":notes},
                               settings.analysis_history_db)
        st.success("Observation saved.")

observations=list_field_observations(path=settings.analysis_history_db)
st.markdown("#### Recorded observations")
if observations:
    frame=pd.DataFrame(observations)
    st.map(frame.rename(columns={"latitude":"lat","longitude":"lon"})[["lat","lon"]],height=300)
    st.dataframe(frame.drop(columns=["id"]),hide_index=True,width="stretch")
    st.download_button("Download observations (CSV)",frame.to_csv(index=False).encode("utf-8"),file_name="field_observations.csv",mime="text/csv",icon=":material/download:")
else:
    st.caption("No field observations recorded yet.")
