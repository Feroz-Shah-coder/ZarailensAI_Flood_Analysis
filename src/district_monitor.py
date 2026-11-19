"""Automatic flood monitoring for selected KPK districts (default: Lakki Marwat,
Charsadda, Nowshera, Swat, Peshawar).

One *check* (``run_monitor_check``) does, for every district:
1. Find Sentinel-1 IW acquisitions (date + orbit track) that are not yet stored
   in ``district_slices`` – a new acquisition is processed exactly once.
2. Measure new water against a same-track 60-day median (shared rule
   ``s1_new_water``: VV/VH thresholds, JRC permanent water, terrain masks) and
   record per-acquisition quality (coverage, baseline depth, polarisation).
3. Compare with the district's own normal signal learnt from older stored
   acquisitions and group anomalies into candidate events.
4. Assess candidates with rainfall, Sentinel-2 optical water, spatial pattern
   and seasonal (river/reservoir) water checks – a new image alone never makes
   an event, and only *Detected* events trigger alerts.
5. Run the full event analysis (before/after pair, flood + cropland area, crop
   types, Sentinel-2 NDVI change, tehsil ranking) for detected events, rank them
   with the existing ``inspection_priority`` formula, and upsert them into
   ``flood_events`` (overlapping detections update the same event).

Flood detection uses the project's threshold rule. ``RandomForestFloodDetector``
in ``flood_detection.py`` is an untrained, unused class (no model file or
training labels exist), so it is not part of this pipeline.

Satellite data are not real time: each Sentinel-1 track revisits every 6–12
days and scenes appear in Earth Engine hours to days after acquisition.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import json
import logging
import os
import socket
import uuid
from typing import Callable

from src import analysis_history as store
from src.event_analysis import (district_backgrounds, score_slices, group_events, add_spatial_context,
                                previous_clear_observation)
from src.impact_calculation import inspection_priority, inspection_priority_score

log=logging.getLogger(__name__)
Progress=Callable[[float,str],None]
DETECTED="Detected flood event"
REVISIT_NOTE=("Satellite data are not real time: each Sentinel-1 orbit track over KPK is revisited every 6–12 days and "
              "scenes reach Google Earth Engine hours to a few days after acquisition. Floods that rise and recede "
              "between two passes can be missed.")

def _noop(_f: float, _t: str) -> None: pass

# ------------------------------------------------------------------ pure helpers
def slice_quality(row: dict, district_max_valid_ha: float) -> str:
    """Human-readable quality flags for one acquisition over one district."""
    flags=[]
    if district_max_valid_ha and row.get("valid_ha",0)<0.5*district_max_valid_ha: flags.append("partial district coverage")
    if int(row.get("baseline_images") or 0)<3: flags.append(f"shallow baseline ({row.get('baseline_images')} images)")
    if not row.get("dual_pol"): flags.append("VV only (no VH)")
    return "ok" if not flags else "; ".join(flags)

def assign_phases(rows: list[dict], monitoring_days: int, background_days: int) -> list[dict]:
    """Split a district's stored acquisitions into background and monitoring periods.

    The newest ``monitoring_days`` are 'monitoring'; the ``background_days`` before
    them teach the district's normal signal. Older rows are dropped.
    """
    if not rows: return []
    newest=max(date.fromisoformat(str(r["date"])[:10]) for r in rows)
    split=newest-timedelta(days=monitoring_days); oldest=split-timedelta(days=background_days)
    out=[]
    for r in rows:
        d=date.fromisoformat(str(r["date"])[:10])
        if d<oldest: continue
        out.append({**r,"date":d,"phase":"monitoring" if d>split else "background"})
    return out

def ndvi_drop_from_crops(crop_rows: list[dict]) -> float:
    """Area-weighted NDVI loss on flooded farmland (relative to unflooded fields), ≥ 0."""
    weighted=[(r["affected_ha"],r["relative_ndvi_change"]) for r in crop_rows
              if r.get("relative_ndvi_change") is not None and r.get("affected_ha",0)>0]
    area=sum(a for a,_ in weighted)
    return max(0.0,-sum(a*c for a,c in weighted)/area) if area else 0.0

def event_priority(event: dict, detail: dict | None) -> tuple[str,float,dict]:
    """Existing inspection_priority formula, fed with the best available evidence.

    Inputs: flooded farmland (ha), share of district cropland flooded (%), NDVI
    loss and event confidence (classification score / 100). The formula and its
    High/Medium/Low thresholds are unchanged from impact_calculation.py.
    """
    if event.get("status","").startswith("Rejected"):
        return "Not prioritised (rejected)",0.0,{}
    if detail:
        ag=float(detail.get("agricultural_flood_ha") or 0); total=float(detail.get("total_cropland_ha") or 0)
        pct=100*ag/total if total else 0.0
        drop=ndvi_drop_from_crops((detail.get("crops") or {}).get("rows") or [])
    else:  # screening only (possible events): 200 m cropland overlap, no NDVI yet
        ag=float(event.get("peak_crop_ha") or 0); pct=0.0; drop=0.0
    confidence=float(event.get("score") or 0)/100
    inputs={"flooded_farmland_ha":round(ag,1),"farmland_share_pct":round(pct,2),"ndvi_loss":round(drop,3),
            "evidence_confidence":round(confidence,2),"source":"event analysis (20 m)" if detail else "screening (200 m)"}
    return inspection_priority(ag,pct,drop,confidence),round(inspection_priority_score(ag,pct,drop,confidence),3),inputs

def event_record(event: dict, detail: dict | None, priority: tuple, processing_status: str, error: str | None = None) -> dict:
    """Flatten an assessed event (+ optional analysis) into a flood_events row."""
    label,score,inputs=priority
    crops=(detail or {}).get("crops") or {}
    acquisitions={"tracks":event.get("tracks"),"detections":event.get("detections"),
                  "first_detection":str(event["first_detection"]),"peak":str(event["peak_date"]),
                  "last_detection":str(event.get("last_detection"))}
    if detail:
        acquisitions.update(before=detail["before"],after=detail["after"],baseline_dates=(detail.get("baseline") or {}).get("dates"))
    refs={"sentinel1_collection":"COPERNICUS/S1_GRD","sentinel2_collection":"COPERNICUS/S2_SR_HARMONIZED",
          "before_scene_ids":(detail or {}).get("before",{}).get("ids"),"after_scene_ids":(detail or {}).get("after",{}).get("ids"),
          "crop_maps":crops.get("source"),"rainfall":event.get("rainfall_source")}
    return {"district":event["district"],"first_detection":str(event["first_detection"]),"peak_date":str(event["peak_date"]),
            "peak_orbit":event.get("peak_orbit"),"peak_relative_orbit":event.get("peak_relative_orbit"),
            "before_date":(detail or {}).get("before",{}).get("date") or (str(event["previous_clear_date"]) if event.get("previous_clear_date") else None),
            "after_date":(detail or {}).get("after",{}).get("date") or str(event["peak_date"]),
            "acquisitions_json":json.dumps(acquisitions,default=str),
            "flood_ha":(detail or {}).get("flood_ha",event.get("peak_flood_ha")),
            "agricultural_exposure_ha":(detail or {}).get("agricultural_flood_ha",event.get("peak_crop_ha")),
            "crop_types_json":json.dumps([{"crop":r["crop"],"affected_ha":r["affected_ha"],"risk":r["risk"]} for r in crops.get("rows",[])]) if crops else None,
            "ndvi_json":json.dumps({"rows":[{"crop":r["crop"],"flooded":r["ndvi_change_flooded"],"unflooded":r["ndvi_change_unflooded"],
                                             "relative":r["relative_ndvi_change"],"valid_fraction":r["ndvi_valid_fraction"]} for r in crops.get("rows",[])],
                                    "pre_window":crops.get("ndvi_pre_window"),"post_window":crops.get("ndvi_post_window")},default=str) if crops else None,
            "priority":label,"priority_score":score,"confidence":event.get("confidence"),"score":event.get("score"),
            "event_status":event.get("status"),"processing_status":processing_status,
            "data_refs_json":json.dumps({**refs,"priority_inputs":inputs},default=str),
            "event_json":json.dumps(event,default=str),"detail_json":json.dumps(detail,default=str) if detail else None,
            "error":error}

def alert_message(record: dict) -> str:
    return (f"ZarailensAI flood alert – {record['district']}: {record['event_status']} "
            f"(confidence {record['confidence']}, verification: Unverified). Sentinel-1 before {record.get('before_date') or 'n/a'}, "
            f"after/peak {record['peak_date']}; first detected {record['first_detection']}. "
            f"New water ≈ {float(record.get('flood_ha') or 0):,.0f} ha, farmland ≈ {float(record.get('agricultural_exposure_ha') or 0):,.0f} ha. "
            f"Field inspection priority: {record['priority']}.")

def next_run_time(last_success_iso: str | None, interval_hours: float, now: datetime | None = None) -> datetime:
    """When the scheduler should run next: now if overdue, else last success + interval (resume after restart)."""
    now=now or datetime.now(timezone.utc)
    if not last_success_iso: return now
    due=datetime.fromisoformat(last_success_iso)+timedelta(hours=interval_hours)
    return max(now,due)

# ------------------------------------------------------------------ the check
def _send_webhook(url: str, message: str) -> None:
    import requests
    try:
        requests.post(url,json={"text":message},timeout=15).raise_for_status()
    except Exception as exc:
        log.warning("Alert webhook failed: %s",exc)

def run_monitor_check(cfg, project=None, trigger: str = "scheduler", path=None,
                      progress: Progress = _noop, today: date | None = None) -> dict:
    """One monitoring check for all configured districts. Safe to call concurrently (DB lock)."""
    from src.flood_monitoring import screen_kpk, assess_event, analyze_event, _chirps_last
    from src.ee_auth import initialize
    path=path or cfg.analysis_history_db
    owner=f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"
    if not store.acquire_monitor_lock(owner,cfg.monitor_lock_minutes,path):
        log.info("Monitoring check skipped: another check holds the lock")
        return {"status":"skipped","message":"Another monitoring check is already running."}
    check_id=store.start_check(trigger,path)
    today=today or date.today()
    summary={"status":"running","districts":{},"new_slices":0,"events_created":0,"events_updated":0,"alerts":[]}
    try:
        initialize(project)
        state=store.get_monitor_state(path)
        districts=list(cfg.monitored_districts)
        # 1–2: measure new acquisitions per district
        for i,district in enumerate(districts):
            progress(0.05+0.45*i/len(districts),f"{district}: searching new Sentinel-1 acquisitions")
            info={"new_slices":0,"status":"ok","message":None}
            try:
                processed=store.processed_slice_keys(district,path)
                last=(state.get(district) or {}).get("last_acquisition")
                if processed and last:
                    fetch_from=date.fromisoformat(last[:10])-timedelta(days=1)
                    start=fetch_from
                else:   # first run for this district: learn its normal signal first
                    start=today-timedelta(days=cfg.monitoring_days)
                    fetch_from=start-timedelta(days=cfg.background_days)
                screened=screen_kpk(start,today+timedelta(days=1),fetch_from,cfg,project,
                                    district_names=[district],skip_keys=processed)
                rows=[r for r in screened["rows"] if r["district"]==district]
                history_max=max([r["valid_ha"] for r in rows]+[float(s.get("valid_ha") or 0) for s in store.district_slices(district,path=path)]+[0])
                for r in rows: r["quality"]=slice_quality(r,history_max)
                info["new_slices"]=store.save_slices(rows,path)
                summary["new_slices"]+=info["new_slices"]
                if screened["errors"]: info["message"]=f"{len(screened['errors'])} acquisition(s) failed: {screened['errors'][0][:120]}"
                newest=max([r["time_utc"] for r in rows if r.get("time_utc")]+([last] if last else []),default=None)
                info["status"]="new imagery" if info["new_slices"] else "no new imagery"
                store.update_monitor_state(district,path,last_acquisition=newest,last_check_at=datetime.now(timezone.utc).isoformat(),
                                           last_success_at=datetime.now(timezone.utc).isoformat(),last_status=info["status"],
                                           last_error=info["message"])
            except Exception as exc:
                log.exception("Monitoring failed for %s",district)
                info.update(status="error",message=str(exc))
                store.update_monitor_state(district,path,last_check_at=datetime.now(timezone.utc).isoformat(),
                                           last_status="error",last_error=str(exc)[:500])
            summary["districts"][district]=info
        # 3: candidate events from stored history (only districts with new acquisitions)
        candidates=[]; scored_by_district={}
        for district,info in summary["districts"].items():
            if not info["new_slices"]: continue
            history=assign_phases(store.district_slices(district,path=path),cfg.monitoring_days,cfg.background_days)
            scored=score_slices(history,district_backgrounds(history))
            scored_by_district[district]=scored
            new_dates=[r["date"] for r in history if r["phase"]=="monitoring"]
            for event in group_events(scored,cfg.min_event_excess_ha):
                # only events touched by this check's new acquisitions (older ones are already stored)
                if new_dates and event["last_detection"]>=min(new_dates): candidates.append(event)
        add_spatial_context(candidates)
        # 4–5: assess, analyse, rank, store, alert
        chirps_last=_chirps_last(initialize(project)) if candidates else None
        for j,event in enumerate(candidates):
            progress(0.55+0.4*j/max(len(candidates),1),f"{event['district']}: assessing candidate event of {event['peak_date']}")
            assessed=assess_event(event,cfg,project,chirps_last)
            clear=previous_clear_observation(scored_by_district[event["district"]],assessed)
            assessed["previous_clear_date"]=clear["date"] if clear else None
            detail=None; status="screened"; error=None
            if assessed["status"]==DETECTED:
                try:
                    detail=json.loads(json.dumps(analyze_event(assessed,cfg,project),default=str)); status="analyzed"
                except Exception as exc:
                    log.exception("Event analysis failed"); status="analysis_failed"; error=str(exc)[:500]
            record=event_record(assessed,detail,event_priority(assessed,detail),status,error)
            event_id,created=store.upsert_flood_event(record,path)
            summary["events_created" if created else "events_updated"]+=1
            if assessed["status"]==DETECTED:
                message=alert_message(record)
                if store.add_alert(event_id,record["district"],message,path):
                    summary["alerts"].append(message); log.warning(message)
                    if cfg.alert_webhook_url: _send_webhook(cfg.alert_webhook_url,message)
        errors=[d for d,i in summary["districts"].items() if i["status"]=="error"]
        summary["status"]="partial" if errors and len(errors)<len(summary["districts"]) else "failed" if errors else "success"
        summary["message"]=(f"{summary['new_slices']} new acquisition(s); {summary['events_created']} new and "
                            f"{summary['events_updated']} updated event(s); {len(summary['alerts'])} alert(s)"
                            +(f"; errors in {', '.join(errors)}" if errors else ""))
    except Exception as exc:
        log.exception("Monitoring check failed")
        summary.update(status="failed",message=str(exc)[:500])
    finally:
        store.finish_check(check_id,summary["status"],path,new_slices=summary["new_slices"],
                           events_created=summary["events_created"],events_updated=summary["events_updated"],
                           message=summary.get("message"),details=summary["districts"])
        store.release_monitor_lock(owner,path)
        progress(1.0,"Monitoring check finished")
    return summary

def analyze_stored_event(event_id: str, cfg, project=None, path=None) -> dict:
    """On-demand full analysis (maps, crops, NDVI) for a stored possible event."""
    from src.flood_monitoring import analyze_event
    path=path or cfg.analysis_history_db
    row=next((e for e in store.list_flood_events(path=path,limit=1000) if e["event_id"]==event_id),None)
    if row is None: raise KeyError(event_id)
    event=row["event"]
    detail=json.loads(json.dumps(analyze_event(event,cfg,project),default=str))
    record=event_record(event,detail,event_priority(event,detail),"analyzed")
    store.upsert_flood_event(record,path)
    return detail
