"""Analysis History page: saved analyses, impact analytics and automatic monitoring runs."""
import pandas as pd
import streamlit as st
from config.settings import settings
from src.analysis_history import list_analyses, list_monitoring_runs

saved_tab,analytics_tab,runs_tab=st.tabs(["Saved analyses","Impact analytics","Automatic monitoring runs"])

with saved_tab:
    records=list_analyses(200,settings.analysis_history_db)
    if records:
        st.dataframe([{"ID":row["id"],"Created (UTC)":row["created_at"],"Location":row["location"],"Before acquisition":row["acquired_before"],"After acquisition":row["acquired_after"],"Sensor":row["sensor"],"Flood area (ha)":row["flood_ha"],"Cropland exposed (ha)":row["agricultural_exposure_ha"],"Priority":row["summary"].get("inspection_priority") or "Unavailable"} for row in records],width="stretch",hide_index=True)
        selected_id=st.selectbox("Open a saved analysis",[row["id"] for row in records],format_func=lambda value:next(f"#{row['id']} · {row['location']} · {row['created_at'][:10]}" for row in records if row["id"]==value),key="history_select")
        selected_record=next(row for row in records if row["id"]==selected_id)
        st.json(selected_record["summary"])
        st.caption("History stores the selected AOI geometry, actual scene dates, computed summary, and observed time series in local SQLite.")
    else:
        st.info("No saved analyses yet. Completed Earth Engine analyses are stored locally in data/analysis_history.sqlite3.")

with analytics_tab:
    records=list_analyses(500,settings.analysis_history_db)
    if records:
        frame=pd.DataFrame([{"Date":row["created_at"],"Location":row["location"] or "Unknown","Flood area (ha)":row["flood_ha"] or 0,"Cropland exposed (ha)":row["agricultural_exposure_ha"],"Priority":row["summary"].get("inspection_priority") or "Unavailable"} for row in records])
        left,right=st.columns(2)
        left.bar_chart(frame.groupby("Location",as_index=True)["Flood area (ha)"].sum(),x_label="Location",y_label="Flood area (ha)")
        ag_frame=frame.dropna(subset=["Cropland exposed (ha)"])
        if not ag_frame.empty:
            right.bar_chart(ag_frame.groupby("Location",as_index=True)["Cropland exposed (ha)"].sum(),x_label="Location",y_label="Mapped cropland exposed (ha)")
        else:
            right.info("No saved analysis has a valid agricultural exposure result yet.")
        st.dataframe(frame,width="stretch",hide_index=True)
        st.caption("Charts aggregate saved analyses. Repeated locations or time periods may overlap; totals are not unique geographic area.")
    else:
        st.info("Impact charts appear after actual analyses are saved. No sample statistics are shown.")

with runs_tab:
    runs=list_monitoring_runs(100,settings.analysis_history_db)
    if runs:
        st.dataframe([{"Run":r["id"],"Refreshed (UTC)":r["refreshed_at"][:19].replace("T"," "),
                       "Monitoring window":f"{r['window_start'] or '–'} → {r['window_end'] or '–'}",
                       "Latest Sentinel-1 acquisition":(r["latest_acquisition"] or "–")[:19].replace("T"," "),
                       "Outcome":{"event":"Recent event detected","historical_event":"No recent event · historical event shown",
                                  "no_event":"No confirmed event","no_data":"No data","error":"Failed"}.get(r["status"],r["status"])}
                      for r in runs],width="stretch",hide_index=True)
    else:
        st.info("No automatic monitoring runs yet. Open the Dashboard to run the first KPK-wide search.")
