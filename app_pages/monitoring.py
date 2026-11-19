"""Automatic Flood Monitoring: status and results of the background district monitor.

The monitor itself runs in a separate process (monitor_service.py); this page only
reads the database, starts a one-off check on request, and records verification.
"""
from datetime import datetime, timezone
import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pandas as pd
import streamlit as st

from config.settings import settings
from src import analysis_history as store
from src.district_monitor import REVISIT_NOTE, analyze_stored_event
from src.flood_monitoring import monitoring_summary
from src.report_generator import pdf_report, excel_report

DB=settings.analysis_history_db
ROOT=Path(__file__).resolve().parents[1]
VERIFICATION=["Unverified","Field visit planned","Confirmed flood (field)","Partially confirmed","Not a flood (field)"]

def _ago(iso: str | None) -> str:
    if not iso: return "never"
    delta=datetime.now(timezone.utc)-datetime.fromisoformat(iso)
    hours=delta.total_seconds()/3600
    return f"{iso[:16].replace('T',' ')} UTC ({hours:.1f} h ago)" if hours<48 else f"{iso[:16].replace('T',' ')} UTC ({hours/24:.0f} days ago)"

def _start_check() -> None:
    """Run one check in a separate background process (never inside the Streamlit rerun)."""
    kwargs={"cwd":str(ROOT),"stdout":subprocess.DEVNULL,"stderr":subprocess.DEVNULL,"env":os.environ.copy()}
    if os.name=="nt": kwargs["creationflags"]=subprocess.CREATE_NEW_PROCESS_GROUP|subprocess.DETACHED_PROCESS
    else: kwargs["start_new_session"]=True
    subprocess.Popen([sys.executable,str(ROOT/"monitor_service.py"),"--once","--trigger","dashboard"],**kwargs)

service=store.service_status(DB)
checks=store.list_checks(50,DB)
state=store.get_monitor_state(DB)
events=store.list_flood_events(path=DB)
alerts=store.list_alerts(unacknowledged_only=True,path=DB)
running_lock=bool(service.get("lock_owner")) and service.get("lock_expires_at") and \
    datetime.fromisoformat(service["lock_expires_at"])>datetime.now(timezone.utc)
service_alive=bool(service.get("heartbeat_at")) and \
    (datetime.now(timezone.utc)-datetime.fromisoformat(service["heartbeat_at"])).total_seconds()<600
last_success=next((c for c in checks if c["status"] in ("success","partial") and c["finished_at"]),None)

st.markdown('<div class="fi-panel">Automatic Flood Monitoring – Lakki Marwat · Charsadda · Nowshera · Swat · Peshawar</div>',unsafe_allow_html=True)
st.info(REVISIT_NOTE,icon=":material/schedule:")

for alert in alerts:
    st.error(alert["message"],icon=":material/notifications_active:")
if alerts and st.button("Mark alerts as read",icon=":material/done_all:"):
    store.acknowledge_alerts(DB); st.rerun()

st.markdown('<div class="fi-kpi-grid">'+"".join(
    f'<div class="fi-kpi {cls}"><div class="ic">{icon}</div><div><p class="lb">{label}</p><p class="vl">{value}</p><p class="nt">{note}</p></div></div>'
    for cls,icon,label,value,note in [
        ("k-green" if service_alive else "k-grey","🛰️","Monitoring service","Running" if service_alive else "Not running",
         f"every {service.get('interval_hours') or settings.monitor_interval_hours:g} h" if service_alive else "start monitor_service.py"),
        ("k-blue","✅","Last successful check",(last_success["finished_at"][:16].replace("T"," ") if last_success else "Never"),
         _ago(last_success["finished_at"]).split("(")[-1].rstrip(")") if last_success else "no check yet"),
        ("k-violet","⏱️","Next scheduled check",(service["next_run_at"][:16].replace("T"," ") if service_alive and service.get("next_run_at") else "—"),
         "UTC" if service_alive else "service not running"),
        ("k-yellow","📍","Districts monitored",str(len(settings.monitored_districts)),", ".join(settings.monitored_districts)),
        ("k-red","⚠️","Detected events",str(sum(1 for e in events if e["event_status"]=="Detected flood event")),
         f"{sum(1 for e in events if e['event_status']=='Possible flood event')} possible · {sum(1 for e in events if str(e['event_status']).startswith('Rejected'))} rejected")])
    +'</div>',unsafe_allow_html=True)

with st.container(horizontal=True):
    if st.button("Refresh monitoring",type="primary",icon=":material/refresh:",disabled=bool(running_lock),
                 help="Runs one check now in a background process. The page does not need to stay open."):
        _start_check()
        st.toast("Monitoring check started in the background. Refresh this page in a few minutes.",icon=":material/satellite_alt:")
    if st.button("Reload page",icon=":material/sync:"): st.rerun()
if running_lock:
    st.caption(f"A monitoring check is running now (started by {service.get('lock_owner','?').split(':')[0]}). Results appear when it finishes.")
if not service_alive:
    st.caption("To check automatically every 12 hours, keep this running in a separate terminal: "
               "`.venv\\Scripts\\python.exe monitor_service.py` (see README → Automatic monitoring service).")

districts_tab,events_tab,log_tab=st.tabs(["Districts","Candidate events","Check log"])

with districts_tab:
    rows=[]
    for district in settings.monitored_districts:
        s=state.get(district) or {}
        stored=store.district_slices(district,path=DB)
        latest=stored[-1] if stored else {}
        rows.append({"District":district,"Last processed acquisition":(s.get("last_acquisition") or "—")[:16].replace("T"," "),
                     "Last check":(s.get("last_check_at") or "—")[:16].replace("T"," "),"Status":s.get("last_status") or "not checked yet",
                     "Acquisitions stored":len(stored),"Latest acquisition quality":latest.get("quality") or "—",
                     "Last error":s.get("last_error") or ""})
    st.dataframe(pd.DataFrame(rows),hide_index=True,width="stretch")
    st.caption("Each Sentinel-1 acquisition (date + orbit track) is measured once and stored; checks only process acquisitions "
               "newer than the last processed one. Quality flags: partial district coverage, shallow baseline, VV-only polarisation.")

with events_tab:
    if not events:
        st.info("No candidate events yet. A new satellite image alone never creates an event: new water must exceed the district's "
                "normal level and pass rainfall, optical (Sentinel-2), spatial-pattern and seasonal-water checks.")
    else:
        frame=pd.DataFrame([{"Event":e["event_id"],"District":e["district"],"Status":e["event_status"],"Confidence":e["confidence"],
            "Detected at (UTC)":e["detected_at"][:16].replace("T"," "),"First detection":e["first_detection"],"Before image":e["before_date"],
            "After image":e["after_date"],"Flooded area (ha)":e["flood_ha"],"Farmland exposed (ha)":e["agricultural_exposure_ha"],
            "Priority":e["priority"],"Verification":e["verification_status"],"Processing":e["processing_status"]} for e in events])
        st.dataframe(frame,hide_index=True,width="stretch",column_config={
            "Flooded area (ha)":st.column_config.NumberColumn(format="%.0f"),"Farmland exposed (ha)":st.column_config.NumberColumn(format="%.0f")})
        ranked=[e for e in events if e["event_status"]!="Rejected – not a flood"]
        if ranked:
            st.markdown("**Field inspection order** (existing `inspection_priority` formula: 0.4 farmland area + 0.3 farmland share + 0.2 NDVI loss + 0.1 evidence confidence)")
            st.dataframe(pd.DataFrame([{"Rank":i+1,"Event":e["event_id"],"District":e["district"],"Priority":e["priority"],
                "Score":e["priority_score"],"Why":", ".join(f"{k.replace('_',' ')} {v}" for k,v in ((e.get("data_refs") or {}).get("priority_inputs") or {}).items())}
                for i,e in enumerate(sorted(ranked,key=lambda e:-(e["priority_score"] or 0)))]),hide_index=True,width="stretch")

        st.divider()
        labels={e["event_id"]:f"{e['event_id']} · {e['event_status']} · {e['district']} · peak {e['peak_date']}" for e in events}
        chosen_id=st.selectbox("Event details",list(labels),format_func=labels.get)
        e=next(x for x in events if x["event_id"]==chosen_id)
        detail=e.get("detail"); event=e.get("event") or {}
        c1,c2=st.columns([2,1])
        with c1:
            st.markdown(f"**{e['district']}** – {e['event_status']} · confidence {e['confidence']} · score {e['score']}")
            st.caption(f"Sentinel-1 before: {e['before_date'] or 'n/a'} · after (peak): {e['after_date']} · first detection {e['first_detection']} · "
                       f"tracks {', '.join((e.get('acquisitions') or {}).get('tracks') or [])}")
            for reason in event.get("reasons",[]): st.markdown(f"- {reason}")
        with c2:
            status=st.selectbox("Ground verification status",VERIFICATION,index=VERIFICATION.index(e["verification_status"]) if e["verification_status"] in VERIFICATION else 0,key=f"verif_{chosen_id}")
            if status!=e["verification_status"] and st.button("Save verification status",key=f"save_{chosen_id}"):
                store.set_verification_status(chosen_id,status,DB); st.rerun()
            st.caption("Field records can be added on the Field Verification page.")
        if detail:
            m1,m2,m3=st.columns(3)
            m1.metric("Flooded area",f"{detail['flood_ha']:,.0f} ha"); m2.metric("Flooded farmland (exposure)",f"{detail['agricultural_flood_ha']:,.0f} ha")
            m3.metric("Inspection priority",e["priority"])
            crops=(detail.get("crops") or {}).get("rows") or []
            if crops:
                st.dataframe(pd.DataFrame([{"Crop":r["crop"],"Flooded area (ha)":r["affected_ha"],"NDVI change flooded":r["ndvi_change_flooded"],
                    "NDVI change unflooded":r["ndvi_change_unflooded"],"Relative change":r["relative_ndvi_change"],"Damage risk":r["risk"]} for r in crops]),
                    hide_index=True,width="stretch",column_config={k:st.column_config.NumberColumn(format="%+.3f") for k in ("NDVI change flooded","NDVI change unflooded","Relative change")}|{"Flooded area (ha)":st.column_config.NumberColumn(format="%.0f")})
                st.caption("Flooded farmland = exposure. NDVI change = observed vegetation change. Damage risk = preliminary class. "
                           "Confirmed crop damage requires field verification.")
            tehsils=[t for t in detail.get("tehsil_priorities") or []]
            if tehsils:
                st.dataframe(pd.DataFrame([{"Tehsil":t["area"].title(),"Priority":t["priority"],"Flooded farmland (ha)":t["flooded_cropland_ha"],
                    "High-risk farmland (ha)":t["high_risk_ha"]} for t in tehsils]),hide_index=True,width="stretch")
        elif e["event_status"]!="Rejected – not a flood":
            st.caption("Screening result only (200 m). Use 'View satellite maps' to run the full analysis (≈1 minute).")
        b1,b2,b3=st.columns(3)
        if b1.button("View satellite maps",icon=":material/map:",disabled=e["event_status"]=="Rejected – not a flood"):
            try:
                if not detail:
                    with st.spinner("Running the full event analysis on real Earth Engine data…"):
                        detail=analyze_stored_event(chosen_id,settings,settings.ee_project,DB)
                st.session_state.update(dashboard_event=event,dashboard_detail=detail,dashboard_report=None)
                st.switch_page("app_pages/dashboard.py")
            except Exception as exc:
                st.error(f"Event analysis failed: {exc}")
        run_like={"refreshed_at":e["updated_at"],"monitoring_window":[e["first_detection"],e["peak_date"]],
                  "selected_source":"automatic district monitoring"}
        summary=monitoring_summary(run_like,event,detail)
        summary.update({"event_id":chosen_id,"verification_status":e["verification_status"],"inspection_priority":e["priority"]})
        def _pdf():
            buffer=io.BytesIO(); pdf_report(summary,buffer); return buffer.getvalue()
        def _xlsx():
            buffer=io.BytesIO(); excel_report(summary,buffer); return buffer.getvalue()
        b2.download_button("Event report (PDF)",_pdf,file_name=f"ZarailensAI - {chosen_id}.pdf",mime="application/pdf",icon=":material/download:")
        b3.download_button("Event report (Excel)",_xlsx,file_name=f"ZarailensAI - {chosen_id}.xlsx",
                           mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",icon=":material/table_view:")

with log_tab:
    if checks:
        st.dataframe(pd.DataFrame([{"Check":c["id"],"Started (UTC)":c["started_at"][:16].replace("T"," "),"Trigger":c["trigger"],
            "Status":c["status"],"New acquisitions":c["new_slices"],"New events":c["events_created"],"Updated events":c["events_updated"],
            "Message":c["message"]} for c in checks]),hide_index=True,width="stretch")
    else:
        st.caption("No checks recorded yet.")
