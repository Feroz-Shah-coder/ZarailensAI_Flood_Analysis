"""Offline tests for the automatic district monitor (database, scheduling, ranking)."""
from datetime import date, datetime, timedelta, timezone
import json
import pytest
from src import analysis_history as store
from src.district_monitor import (slice_quality, assign_phases, ndvi_drop_from_crops, event_priority,
                                  event_record, alert_message, next_run_time)
from src.impact_calculation import inspection_priority

def _slice(day, flood=100, valid=50_000, rel=5, orbit="DESCENDING", district="Charsadda"):
    return {"district":district,"date":day,"relative_orbit":rel,"orbit":orbit,"flood_ha":flood,"crop_ha":flood/2,
            "valid_ha":valid,"baseline_images":5,"dual_pol":True,"frames":2,"time_utc":f"{day}T01:00:00+00:00"}

def _event(status="Detected flood event", district="Charsadda", first="2026-08-01", peak="2026-08-13"):
    return {"district":district,"first_detection":first,"peak_date":peak,"last_detection":peak,"peak_orbit":"DESCENDING",
            "peak_relative_orbit":5,"peak_flood_ha":4000.0,"peak_crop_ha":1500.0,"peak_excess_ha":3500.0,"status":status,
            "confidence":"High","score":80.0,"tracks":["DESCENDING 5"],"detections":2,"reasons":["x"]}

def test_duplicate_acquisitions_are_stored_once(tmp_path):
    db=tmp_path/"m.sqlite3"
    rows=[_slice("2026-08-01"),_slice("2026-08-13")]
    assert store.save_slices(rows,db)==2
    assert store.save_slices(rows,db)==0                     # same acquisitions again → ignored
    assert store.processed_slice_keys("Charsadda",db)=={("2026-08-01",5,"DESCENDING"),("2026-08-13",5,"DESCENDING")}

def test_overlapping_detections_update_one_event_and_keep_verification(tmp_path):
    db=tmp_path/"m.sqlite3"
    record=event_record(_event(),None,("High",.8,{}),"screened")
    event_id,created=store.upsert_flood_event(record,db)
    assert created
    store.set_verification_status(event_id,"Confirmed flood (field)",db)
    later=event_record(_event(first="2026-08-13",peak="2026-08-25"),None,("High",.9,{}),"screened")
    same_id,created_again=store.upsert_flood_event(later,db)
    assert same_id==event_id and not created_again
    stored=store.list_flood_events(path=db)
    assert len(stored)==1 and stored[0]["first_detection"]=="2026-08-01" and stored[0]["peak_date"]=="2026-08-25"
    assert stored[0]["verification_status"]=="Confirmed flood (field)"
    separate,created3=store.upsert_flood_event(event_record(_event(first="2026-10-20",peak="2026-10-20"),None,("Low",.1,{}),"screened"),db)
    assert created3 and separate!=event_id

def test_alerts_are_not_repeated_for_the_same_event(tmp_path):
    db=tmp_path/"m.sqlite3"
    assert store.add_alert("E1","Swat","flood",db)
    assert not store.add_alert("E1","Swat","flood",db)
    assert len(store.list_alerts(path=db))==1

def test_lock_prevents_overlap_and_expires(tmp_path):
    db=tmp_path/"m.sqlite3"
    assert store.acquire_monitor_lock("a",60,db)
    assert not store.acquire_monitor_lock("b",60,db)          # overlapping run refused
    store.release_monitor_lock("a",db)
    assert store.acquire_monitor_lock("b",60,db)
    store.update_service_status(db,lock_expires_at=(datetime.now(timezone.utc)-timedelta(minutes=1)).isoformat())
    assert store.acquire_monitor_lock("c",60,db)              # abandoned (expired) lock is taken over

def test_check_log_and_district_state(tmp_path):
    db=tmp_path/"m.sqlite3"
    cid=store.start_check("test",db)
    store.finish_check(cid,"success",db,new_slices=3,events_created=1,events_updated=0,message="ok",details={"Swat":{"status":"ok"}})
    check=store.list_checks(path=db)[0]
    assert check["status"]=="success" and json.loads(check["details_json"])["Swat"]["status"]=="ok"
    store.update_monitor_state("Swat",db,last_acquisition="2026-11-15T01:00:00+00:00",last_status="no new imagery")
    assert store.get_monitor_state(db)["Swat"]["last_status"]=="no new imagery"

def test_resume_after_restart_schedules_from_last_success():
    now=datetime(2026,11,20,12,tzinfo=timezone.utc)
    assert next_run_time(None,12,now)==now                                    # never ran → now
    assert next_run_time("2026-11-20T06:00:00+00:00",12,now)==datetime(2026,11,20,18,tzinfo=timezone.utc)
    assert next_run_time("2026-11-19T06:00:00+00:00",12,now)==now             # overdue → now

def test_phase_split_and_quality_flags():
    rows=[_slice(str(date(2026,6,1)+timedelta(days=12*i))) for i in range(12)]
    phased=assign_phases(rows,45,60)
    assert {r["phase"] for r in phased}=={"monitoring","background"}
    assert all(r["date"]>=date(2026,6,1) for r in phased)
    assert slice_quality({"valid_ha":40_000,"baseline_images":5,"dual_pol":True},50_000)=="ok"
    flags=slice_quality({"valid_ha":10_000,"baseline_images":1,"dual_pol":False},50_000)
    assert "partial" in flags and "shallow" in flags and "VV only" in flags

def test_priority_reuses_existing_formula_unchanged():
    detail={"agricultural_flood_ha":800,"total_cropland_ha":4000,
            "crops":{"rows":[{"affected_ha":600,"relative_ndvi_change":-0.2},{"affected_ha":200,"relative_ndvi_change":0.0}]}}
    assert ndvi_drop_from_crops(detail["crops"]["rows"])==pytest.approx(0.15)
    label,score,inputs=event_priority(_event(),detail)
    assert label==inspection_priority(800,20,0.15,0.8)
    assert inputs["source"].startswith("event analysis")
    assert event_priority(_event(status="Rejected – not a flood"),None)[0].startswith("Not prioritised")

def test_alert_message_contains_required_fields():
    record=event_record(_event(),None,("High",.8,{}),"screened")
    msg=alert_message(record)
    for part in ("Charsadda","2026-08-13","2026-08-01","confidence High","Unverified","priority: High"):
        assert part in msg

def test_binary_metrics():
    from src.evaluation import binary_metrics
    m=binary_metrics(80,20,20)
    assert m["precision"]==pytest.approx(.8) and m["recall"]==pytest.approx(.8) and m["f1"]==pytest.approx(.8)
    assert m["iou"]==pytest.approx(80/120)
    none=binary_metrics(0,0,0)
    assert none["precision"] is None and none["iou"] is None
    false_alarm=binary_metrics(0,50,0)
    assert false_alarm["precision"]==0 and false_alarm["f1"]==0
