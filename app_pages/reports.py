"""Reports page: PDF/Excel exports of the dashboard event and the current manual analysis."""
import hashlib
import io
import json
import streamlit as st
from config.settings import settings
from src.report_generator import excel_report, pdf_report
from src.satellite_retrieval import sentinel1_pair_report_thumbnails
from src.flood_monitoring import monitoring_summary
from src.analysis_history import latest_monitoring_run

dashboard_tab,manual_tab=st.tabs(["Latest dashboard event","Current manual analysis"])

with dashboard_tab:
    run=st.session_state.get("monitoring_run") or latest_monitoring_run(settings.analysis_history_db)
    if not run:
        st.info("No automatic monitoring run is available yet. Open the Dashboard to run one.")
    else:
        event=st.session_state.get("dashboard_event") or run.get("selected")
        detail=st.session_state.get("dashboard_detail") if st.session_state.get("dashboard_event") else run.get("detail")
        summary=monitoring_summary(run,event,detail)
        st.write({k:summary[k] for k in ("monitoring_window","location","event_status","flood_ha","ag_ha","main_affected_crop","overall_damage_risk") if k in summary})
        if st.button("Prepare dashboard PDF and Excel",type="primary",key="prepare_dashboard_reports"):
            try:
                images={}
                if detail:
                    with st.spinner("Retrieving real Sentinel-1 previews for the report…"):
                        from datetime import date
                        b={**detail["before"],"date":date.fromisoformat(detail["before"]["date"])}
                        a={**detail["after"],"date":date.fromisoformat(detail["after"]["date"])}
                        images=sentinel1_pair_report_thumbnails(b,a,detail["geometry"],settings.ee_project,**detail["rule"])
                pdf=io.BytesIO(); pdf_report(summary,pdf,images=images)
                xl=io.BytesIO(); excel_report(summary,xl)
                st.session_state.dashboard_report={"pdf":pdf.getvalue(),"excel":xl.getvalue()}
            except Exception as exc:
                st.error(f"Report preparation failed: {exc}")
        files=st.session_state.get("dashboard_report")
        if files:
            c1,c2=st.columns(2)
            c1.download_button("Download PDF report",files["pdf"],file_name="ZarailensAI - KPK event.pdf",mime="application/pdf",icon=":material/download:")
            c2.download_button("Export Excel",files["excel"],file_name="ZarailensAI - KPK event.xlsx",mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",icon=":material/table_view:")

with manual_tab:
    summary=st.session_state.analysis_summary
    if summary:
        st.write({"Detected flooded area (ha)":summary.get("flood_ha"),"WorldCover cropland exposed (ha)":summary.get("ag_ha") if summary.get("ag_ha") is not None else "Unavailable","Confirmed crop damage":"Unavailable · field data required","Inspection priority":summary.get("inspection_priority") or "Unavailable"})
        report_key=hashlib.sha256(json.dumps(summary,default=str,sort_keys=True).encode("utf-8")).hexdigest()
        if st.session_state.report_artifacts_key!=report_key:
            st.session_state.report_artifacts=None
            st.session_state.report_artifacts_key=report_key
        if st.button("Prepare PDF and Excel reports",type="primary",key="prepare_reports"):
            try:
                report_images={}
                before_scene=(st.session_state.sentinel_gallery or {}).get("before")
                after_scene=(st.session_state.sentinel_gallery or {}).get("after")
                if before_scene and after_scene and st.session_state.aoi:
                    with st.spinner("Retrieving real Earth Engine report previews and preparing exports…"):
                        report_images=sentinel1_pair_report_thumbnails(before_scene,after_scene,st.session_state.aoi,project=settings.ee_project)
                pdf_buffer=io.BytesIO(); pdf_report(summary,pdf_buffer,images=report_images)
                excel_buffer=io.BytesIO(); excel_report(summary,excel_buffer)
                st.session_state.report_artifacts={"pdf":pdf_buffer.getvalue(),"excel":excel_buffer.getvalue()}
                st.success("Reports are ready. Satellite panels in the PDF come from the selected real Earth Engine scenes.")
            except Exception as exc:
                st.error(f"Report preparation failed: {exc}")
        artifacts=st.session_state.report_artifacts or {}
        if artifacts:
            c1,c2=st.columns(2)
            c1.download_button("Download PDF report",artifacts["pdf"],file_name="ZarailensAI report.pdf",mime="application/pdf",key="download_pdf")
            c2.download_button("Download Excel report",artifacts["excel"],file_name="ZarailensAI report.xlsx",mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",key="download_excel")
    else: st.info("Reports become available after a real analysis completes on the New Analysis page.")
