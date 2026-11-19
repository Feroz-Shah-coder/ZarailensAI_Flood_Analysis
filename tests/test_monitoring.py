"""Unit tests for automatic event discovery logic (synthetic inputs, no Earth Engine)."""
from datetime import date
import io
import pytest
from src.event_analysis import (district_backgrounds, score_slices, group_events, add_spatial_context,
    classify_event, rank_events, select_latest_reliable, previous_clear_observation, crop_damage_risk)
from src.agricultural_analysis import crop_classes, is_kharif, ndvi_windows
from src.flood_monitoring import monsoon_windows, _group_frames
from src.report_generator import excel_report, pdf_report
from src.analysis_history import (save_monitoring_run, latest_monitoring_run, save_field_observation,
    list_field_observations)

def row(day, district="A", flood=100, valid=100_000, phase="monitoring", rel=5, orbit="DESCENDING"):
    return {"date":day,"district":district,"flood_ha":flood,"valid_ha":valid,"crop_ha":flood/2,
            "phase":phase,"relative_orbit":rel,"orbit":orbit,"baseline_images":5}

def test_background_prefers_pre_window_and_removes_recurring_signal():
    rows=[row(date(2026,8,d),flood=1000,phase="background") for d in (1,13,25)]
    rows+=[row(date(2026,9,6),flood=1000),row(date(2026,9,18),flood=6000)]
    bg=district_backgrounds(rows)
    assert bg["A"]["fraction"]==pytest.approx(0.01) and bg["A"]["source"]=="pre-window acquisitions"
    scored=score_slices(rows,bg)
    normal=next(r for r in scored if r["date"]==date(2026,9,6))
    flood=next(r for r in scored if r["date"]==date(2026,9,18))
    assert normal["excess_ha"]==pytest.approx(0) and flood["excess_ha"]==pytest.approx(5000)
    assert flood["ratio"]==pytest.approx(6)

def test_partial_slices_are_ignored():
    assert score_slices([row(date(2026,9,1),valid=10)],{"A":{"fraction":0}})==[]

def test_grouping_merges_close_detections_and_splits_gaps():
    bg={"A":{"fraction":0.001}}
    rows=[row(date(2026,8,1),flood=2000),row(date(2026,8,13),flood=4000),row(date(2026,9,30),flood=900)]
    events=group_events(score_slices(rows,bg),min_excess_ha=300)
    assert len(events)==2
    first=min(events,key=lambda e:e["first_detection"])
    assert first["detections"]==2 and first["peak_date"]==date(2026,8,13)

def test_classification_requires_corroboration_and_never_promotes_possible():
    base={"district":"A","peak_date":date(2026,8,13),"first_detection":date(2026,8,13),"peak_excess_ha":4000,
          "peak_ratio":8,"detections":1,"concurrent_districts":[],"baseline_images":5,"valid_ha":50_000}
    lone=classify_event(base,rainfall_mm=2)
    assert lone["status"]=="Possible flood event"
    rainy=classify_event(base,rainfall_mm=60)
    assert rainy["status"]=="Detected flood event" and rainy["confidence"] in {"High","Medium"}
    unknown_rain=classify_event({**base,"detections":2},rainfall_mm=None)
    assert unknown_rain["status"]=="Detected flood event"
    assert any("unavailable" in r for r in unknown_rain["reasons"])
    assert select_latest_reliable([lone])is None
    assert select_latest_reliable([lone,rainy])==rainy

def test_ranking_puts_detected_first_then_most_recent():
    events=[{"status":"Possible flood event","peak_date":date(2026,9,1),"score":90},
            {"status":"Detected flood event","peak_date":date(2026,7,1),"score":50},
            {"status":"Detected flood event","peak_date":date(2026,8,1),"score":40}]
    assert [e["peak_date"].month for e in rank_events(events)]==[8,7,9]

def test_spatial_context_and_previous_clear_observation():
    bg={"A":{"fraction":0.001},"B":{"fraction":0.001}}
    rows=[row(date(2026,8,1),flood=50),row(date(2026,8,13),flood=3000),row(date(2026,8,14),"B",flood=3000,rel=71)]
    scored=score_slices(rows,bg)
    events=add_spatial_context(group_events(scored))
    a=next(e for e in events if e["district"]=="A")
    assert a["concurrent_districts"]==["B"]
    assert previous_clear_observation(scored,a)["date"]==date(2026,8,1)

def test_crop_damage_risk_classes():
    assert crop_damage_risk(-0.2,.8,100)=="High"
    assert crop_damage_risk(-0.1,.8,100)=="Medium"
    assert crop_damage_risk(0.02,.8,100)=="Low"
    assert crop_damage_risk(None,None,100)=="Insufficient Data"
    assert crop_damage_risk(-0.3,.1,100)=="Insufficient Data"
    assert crop_damage_risk(None,None,0)=="Low"

def test_season_crop_classes_and_ndvi_windows():
    assert is_kharif(date(2026,8,1)) and not is_kharif(date(2026,12,1))
    assert crop_classes(date(2026,8,1))[0]["crop"]=="Maize"
    assert crop_classes(date(2026,1,5))[0]["crop"].startswith("Wheat")
    pre,post=ndvi_windows(date(2026,8,1),date(2026,8,13),today=date(2026,8,20))
    assert pre==(date(2026,7,2),date(2026,8,2)) and post==(date(2026,8,13),date(2026,8,21))
    assert ndvi_windows(date(2026,8,1),date(2026,8,13),today=date(2026,8,12))[1] is None

def test_monsoon_windows_only_before_monitoring_start():
    windows=monsoon_windows(date(2026,11,19),date(2026,10,5),2)
    assert [w[0] for w in windows]==["Monsoon 2026","Monsoon 2025"]
    partial=monsoon_windows(date(2026,9,1),date(2026,8,1),1)
    assert partial[0][2]==date(2026,8,1)

def test_frames_of_one_pass_become_one_scene():
    frames=[{"id":"a","date":date(2026,8,1),"orbit":"D","relative_orbit":5,"coverage":60},
            {"id":"b","date":date(2026,8,1),"orbit":"D","relative_orbit":5,"coverage":50},
            {"id":"c","date":date(2026,8,13),"orbit":"D","relative_orbit":5,"coverage":100}]
    scenes=_group_frames(frames)
    assert scenes[0]["ids"]==["a","b"] and scenes[0]["coverage"]==100 and len(scenes)==2

def test_reports_include_crop_tables():
    summary={"location":"Test","crop_rows":[{"crop":"Maize","affected_ha":10.5,"risk":"Low","relative_ndvi_change":None}],
             "candidate_events":[{"district":"A","status":"Possible flood event"}],"rainfall_series":[{"date":"2026-08-01","mm":3.2}]}
    xl=io.BytesIO(); excel_report(summary,xl)
    from openpyxl import load_workbook
    wb=load_workbook(io.BytesIO(xl.getvalue()))
    assert {"Crop damage risk","Candidate events","Rainfall"}<=set(wb.sheetnames)
    assert wb["Crop damage risk"]["D2"].value=="Unavailable" or wb["Crop damage risk"]["A2"].value=="Maize"
    pdf=io.BytesIO(); pdf_report(summary,pdf); assert pdf.getvalue().startswith(b"%PDF")

def test_monitoring_and_field_storage(tmp_path):
    db=tmp_path/"h.sqlite3"
    save_monitoring_run({"status":"error","message":"x"},db)
    assert latest_monitoring_run(db) is None
    save_monitoring_run({"status":"no_event","monitoring_window":["2026-10-01","2026-11-15"]},db)
    assert latest_monitoring_run(db)["status"]=="no_event"
    save_field_observation({"district":"Charsadda","latitude":34.1,"longitude":71.7,"flooding_observed":"Yes"},db)
    assert list_field_observations(path=db)[0]["district"]=="Charsadda"

def _strong_event():
    return {"district":"A","peak_date":date(2026,10,10),"first_detection":date(2026,10,9),"peak_excess_ha":5000,
            "peak_ratio":6.7,"detections":4,"concurrent_districts":["B"],"baseline_images":25,"valid_ha":268000}

def test_optical_refutation_rejects_field_wetting_false_alarm():
    # Values measured for Lakki Marwat 10 Oct 2026 (dry fields in Sentinel-2).
    result=classify_event(_strong_event(),36,verification={"optical_clear_pct":96,"optical_water_pct":1,"large_patch_pct":22})
    assert result["status"]=="Rejected – not a flood" and result["score"]<=20
    assert select_latest_reliable([result]) is None

def test_optical_confirmation_gives_high_confidence():
    # Values measured for D.I. Khan 29 Aug 2022 (documented flood).
    result=classify_event(_strong_event(),60,verification={"optical_clear_pct":97,"optical_water_pct":84,"large_patch_pct":53})
    assert result["status"]=="Detected flood event" and result["confidence"]=="High"

def test_cloudy_event_needs_flood_like_structure():
    cloudy_compact=classify_event(_strong_event(),60,verification={"optical_clear_pct":0,"optical_water_pct":None,"large_patch_pct":52})
    assert cloudy_compact["status"]=="Detected flood event" and cloudy_compact["confidence"]=="Medium"
    cloudy_fragmented=classify_event(_strong_event(),60,verification={"optical_clear_pct":5,"optical_water_pct":None,"large_patch_pct":12})
    assert cloudy_fragmented["status"]=="Possible flood event"

def test_reservoir_refilling_is_rejected_even_when_optically_water():
    # Values measured for Haripur Aug 2026 (Tarbela reservoir arm refilling).
    result=classify_event(_strong_event(),99,verification={"optical_clear_pct":98,"optical_water_pct":98,
                                                          "large_patch_pct":77,"historical_water_pct":95})
    assert result["status"]=="Rejected – not a flood"
    assert result["reasons"][0].startswith("REJECTED")
    real=classify_event(_strong_event(),60,verification={"optical_clear_pct":97,"optical_water_pct":84,
                                                        "large_patch_pct":53,"historical_water_pct":24})
    assert real["status"]=="Detected flood event"

def test_service_account_key_file_is_parsed_for_hosted_deployments():
    import json
    from src.ee_auth import service_account_credentials
    from src.utils import EarthEngineError
    calls={}
    class FakeEE:
        @staticmethod
        def ServiceAccountCredentials(email,key_data=None):
            calls.update(email=email,key_data=key_data); return "creds"
    key=json.dumps({"type":"service_account","client_email":"svc@proj.iam.gserviceaccount.com","private_key":"-----BEGIN PRIVATE KEY-----x"})
    assert service_account_credentials(FakeEE,key)=="creds"
    assert calls["email"]=="svc@proj.iam.gserviceaccount.com" and calls["key_data"]==key
    with pytest.raises(EarthEngineError): service_account_credentials(FakeEE,"{}")

def test_flood_catalog_loads_all_reported_events_with_valid_districts():
    import json
    from src.flood_catalog import load_catalog, search_window, landsat_sensor
    catalog=load_catalog()
    assert len(catalog["events"])==22
    names={f["properties"]["district"] for f in json.load(open("data/boundaries/kpk_districts_gaul2025.geojson"))["features"]}
    assert all(d in names for e in catalog["events"] for d in e["districts"])
    era={e["id"]:e["era"] for e in catalog["events"]}
    assert era["2010-jul-aug"]=="landsat" and era["2022-jun-aug"]=="sentinel"
    first=catalog["events"][0]
    assert search_window(first)==(date(2010,7,25),date(2010,9,1))
    assert landsat_sensor(date(2010,8,1))=="Landsat 5 TM" and landsat_sensor(date(2013,8,1))=="Landsat 8 OLI"
