"""Reported KPK flood events (2010–2026) and their satellite analysis.

The event list (``data/reference/kpk_flood_events.json``) is a user-provided
reference of *reported* floods; it contains no measurements. For a chosen event
and district this module searches the real Earth Engine archive and computes:

* Sentinel era (from 2015): every Sentinel-1 acquisition in the window measured
  against a same-track 60-day median, the strongest anomaly assessed (rainfall,
  Sentinel-2 optical water, spatial pattern, seasonal water) and analysed in full
  (before/after pair, flood + farmland area, crop types, NDVI, tehsils, rainfall).
* Landsat era (2010–2014): cloud-masked Landsat MNDWI water change against a
  pre-event composite, cropland overlap, NDVI change and before/after imagery.

Results (including "no flood signal found") are cached in ``catalog_analyses``.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import json
import logging
from pathlib import Path

log=logging.getLogger(__name__)
CATALOG_PATH=Path(__file__).resolve().parents[1]/"data"/"reference"/"kpk_flood_events.json"
SENTINEL_ERA_START=date(2015,1,1)   # Sentinel-1 over KPK becomes regular from early 2015

def load_catalog(path: Path = CATALOG_PATH) -> dict:
    data=json.loads(Path(path).read_text(encoding="utf-8"))
    for e in data["events"]:
        e["start_date"]=date.fromisoformat(e["start"]); e["end_date"]=date.fromisoformat(e["end"])
        e["era"]="sentinel" if e["start_date"]>=SENTINEL_ERA_START else "landsat"
    return data

def era_label(event: dict) -> str:
    return ("Sentinel-1 radar + Sentinel-2 optical" if event["era"]=="sentinel"
            else "Landsat optical archive (no Sentinel data before 2015)")

def search_window(event: dict) -> tuple[date,date]:
    """Reported period plus 12 days after it, so the first satellite pass after the event is included."""
    return event["start_date"],event["end_date"]+timedelta(days=12)

def landsat_sensor(day: date) -> str:
    if day<date(2011,11,1): return "Landsat 5 TM"
    if day<date(2013,4,15): return "Landsat 7 ETM+"
    return "Landsat 8 OLI"

def _jsonable(value):
    return json.loads(json.dumps(value,default=str))

# ------------------------------------------------------------------ sentinel era
def _analyze_sentinel(event: dict, district: str, cfg, project=None, progress=lambda f,t:None) -> dict:
    from src.flood_monitoring import screen_kpk, assess_event, analyze_event, _chirps_last
    from src.ee_auth import initialize
    from src.event_analysis import district_backgrounds, score_slices, group_events, previous_clear_observation
    start,end=search_window(event)
    progress(0.1,f"Measuring every Sentinel-1 acquisition over {district}, {start} → {end}")
    screened=screen_kpk(start,min(end,date.today()+timedelta(days=1)),start-timedelta(days=cfg.background_days),cfg,project,
                        district_names=[district])
    rows=[r for r in screened["rows"] if r["district"]==district]
    scored=score_slices(rows,district_backgrounds(rows))
    timeline=[{"date":str(r["date"]),"track":f"{r['orbit']} {r['relative_orbit']}","new_water_ha":round(r["flood_ha"],1),
               "excess_ha":round(r["excess_ha"],1),"x_normal":round(r["ratio"],2),"phase":r["phase"]} for r in sorted(scored,key=lambda r:r["date"])]
    result={"era":"sentinel","window":[str(start),str(end)],"timeline":timeline,"errors":screened["errors"],
            "acquisitions_in_window":sum(1 for r in scored if r["phase"]=="monitoring")}
    candidates=group_events(scored,cfg.min_event_excess_ha)
    if not candidates:
        result.update(status="No flood signal detected",
                      message="No Sentinel-1 acquisition in this window showed new water clearly above the district's normal level.")
        return result
    strongest=max(candidates,key=lambda e:e["peak_excess_ha"])
    progress(0.4,"Checking rainfall, Sentinel-2 optical water and seasonal water")
    assessed=assess_event({**strongest,"concurrent_districts":[]},cfg,project,_chirps_last(initialize(project)))
    clear=previous_clear_observation(scored,assessed); assessed["previous_clear_date"]=clear["date"] if clear else None
    result.update(status=assessed["status"],event=_jsonable(assessed),other_candidates=_jsonable(
        [c for c in candidates if c is not strongest]))
    progress(0.6,"Retrieving before/after imagery and measuring flood, farmland, crops and NDVI")
    try:
        result["detail"]=_jsonable(analyze_event(assessed,cfg,project))
    except Exception as exc:
        log.exception("Catalog event analysis failed"); result["detail_error"]=str(exc)
    return result

# ------------------------------------------------------------------ landsat era
def _analyze_landsat(event: dict, district: str, geometry: dict, cfg, project=None, progress=lambda f,t:None) -> dict:
    from src.satellite_retrieval import SensorCatalog, optical_flood_time_series, optical_event_impact
    from src.flood_monitoring import rainfall_daily
    start,end=search_window(event)
    middle=start+(end-start)/2; half=max((end-start).days//2+1,10)
    sensor=landsat_sensor(middle)
    progress(0.1,f"Searching {sensor} scenes over {district}, {start} → {end}")
    series=optical_flood_time_series(geometry,middle,half,sensor,cloud_limit=60,project=project)
    timeline=[{"date":str(r["date"]),"track":sensor,"new_water_ha":round(r["flood_ha"],1),"clear_percent":round(r["clear_percent"],1)}
              for r in sorted(series["rows"],key=lambda r:r["date"])]
    result={"era":"landsat","sensor":sensor,"window":[str(start),str(end)],"timeline":timeline,
            "acquisitions_in_window":len(series.get("candidate_scenes") or []),"baseline_scenes":len(series.get("baseline_scenes") or [])}
    peak=series.get("peak")
    if not peak:
        result.update(status="No flood signal detected",message=series.get("message") or "No clear Landsat scene showed new water ≥ 1 ha.")
        return result
    spec=next(s for s in SensorCatalog.optical_for_date(peak["date"]) if s.name==sensor)
    progress(0.5,"Measuring water change, farmland exposure and NDVI on the real Landsat pair")
    baseline=[s["id"] for s in series["baseline_scenes"]]
    impact=optical_event_impact(baseline,peak["id"],spec,geometry,project)
    before_dates=sorted({str(s["date"]) for s in series["baseline_scenes"]})
    status="Optical water increase observed" if impact["flood_ha"]>=cfg.min_event_excess_ha else "Small optical water change"
    progress(0.8,"Retrieving rainfall (CHIRPS)")
    try: rain=rainfall_daily(geometry,date.fromisoformat(before_dates[-1])-timedelta(days=3),peak["date"],project)
    except Exception as exc: rain={"series":[],"source":f"unavailable: {exc}"}
    result.update(status=status,landsat={**{k:v for k,v in impact.items() if k!="layers"},
        "before_dates":before_dates,"after_date":str(peak["date"]),"after_id":peak["id"],"baseline_ids":baseline,
        "after_clear_percent":peak.get("clear_percent"),"sensor":sensor,"rainfall":rain},
        message=("Landsat revisits every 16 days (8 days with two satellites) and clouds hide many monsoon scenes, "
                 "so short floods are often missed. Landsat 7 scenes after May 2003 have striped data gaps (SLC-off)."))
    return result

# ------------------------------------------------------------------ public
def analyze_catalog_event(event: dict, district: str, cfg, project=None, progress=lambda f,t:None) -> dict:
    from src.flood_monitoring import district_geometry
    geometry=district_geometry(district,cfg.boundaries_dir,project)
    if geometry is None: raise ValueError(f"No boundary for {district}")
    if event["start_date"]>date.today():
        return {"status":"Not yet observable","message":"This period is in the future; no satellite data exists yet.","era":event["era"]}
    try:
        result=(_analyze_sentinel(event,district,cfg,project,progress) if event["era"]=="sentinel"
                else _analyze_landsat(event,district,geometry,cfg,project,progress))
    except Exception as exc:
        log.exception("Catalog analysis failed")
        result={"status":"Analysis failed","message":str(exc),"era":event["era"]}
    result.update(event_id=event["id"],district=district,computed_at=datetime.now(timezone.utc).isoformat(),geometry=geometry)
    return _jsonable(result)

def headline(result: dict) -> dict:
    """Key numbers for a card: underwater area, farmland, risk (None when not measured)."""
    if not result: return {}
    if result.get("era")=="landsat" and result.get("landsat"):
        l=result["landsat"]; rows=l.get("crop_rows") or []
        return {"flood_ha":l["flood_ha"],"farmland_ha":l["agricultural_flood_ha"],"risk":rows[0]["risk"] if rows else None,
                "after":l["after_date"]}
    d=result.get("detail")
    if d:
        from src.flood_monitoring import overall_risk
        return {"flood_ha":d["flood_ha"],"farmland_ha":d["agricultural_flood_ha"],
                "risk":overall_risk((d.get("crops") or {}).get("rows") or []),"after":d["after"]["date"]}
    return {}

# ------------------------------------------------------------------ districts & pictures
# Events reported only as "KPK" / "different parts of KPK" name no district. They are
# measured over the main flood-prone river districts (Kabul, Swat and Indus plains).
DEFAULT_DISTRICTS=["Peshawar","Charsadda","Nowshera","Swat","D. I. Khan"]
THUMB_DIR=Path(__file__).resolve().parents[1]/"data"/"catalog_thumbs"

def analysis_districts(event: dict) -> list[str]:
    return event["districts"] or DEFAULT_DISTRICTS

def thumbnail_path(event_id: str, district: str) -> Path:
    return THUMB_DIR/f"{event_id}__{district.replace(' ','_').replace('.','')}.png"

def _thumbnail_image(result: dict, ee, project=None):
    """Real after-event picture with the measured underwater area drawn in blue."""
    from src.satellite_retrieval import (s1_scene_image, s1_new_water, SensorCatalog, prepared_optical_image,
                                         visualization_bands)
    region=ee.Geometry(result["geometry"]); blue=lambda m:m.selfMask().visualize(palette=["2f7de1"],opacity=.85)
    detail=result.get("detail"); landsat=result.get("landsat")
    if detail:
        parse=lambda s:{**s,"date":date.fromisoformat(str(s["date"]))}
        after=parse(detail["after"]); before=parse(detail["before"])
        baseline={**before,**detail.get("baseline",{}),"composite":"median"} if detail.get("baseline") else before
        a=s1_scene_image(after,ee)
        flood=s1_new_water(s1_scene_image(baseline,ee),a,ee,**detail["rule"])
        base=a.select("VV").visualize(min=-25,max=0,palette=["000000","ffffff"])
        return base.blend(blue(flood))
    if landsat:
        spec=next(s for s in SensorCatalog.optical_for_date(date.fromisoformat(landsat["after_date"])) if s.name==landsat["sensor"])
        b=prepared_optical_image(landsat["baseline_ids"],spec,result["geometry"],project)
        a=prepared_optical_image([landsat["after_id"]],spec,result["geometry"],project)
        mb=b.normalizedDifference([spec.green,spec.swir1]); ma=a.normalizedDifference([spec.green,spec.swir1])
        from src.satellite_retrieval import permanent_water_mask, terrain_mask
        flood=ma.gt(0).And(mb.lte(0)).And(permanent_water_mask(ee).Not()).And(terrain_mask(ee,10))
        base=a.visualize(bands=visualization_bands(spec),min=0,max=.3,gamma=1.3)
        return base.blend(blue(flood))
    # No flood measured: the real Sentinel-1 picture of the window (median), so the card still shows the place.
    if result.get("era")!="sentinel" or not result.get("window"): return None
    s,e=result["window"]
    col=ee.ImageCollection("COPERNICUS/S1_GRD").filterBounds(region).filterDate(s,e).filter(
        ee.Filter.eq("instrumentMode","IW")).filter(ee.Filter.listContains("transmitterReceiverPolarisation","VV")).select("VV")
    return col.median().visualize(min=-25,max=0,palette=["000000","ffffff"])

def save_thumbnail(result: dict, project=None) -> Path | None:
    """Download a 512 px PNG (real Earth Engine thumbnail) for a cards list; None when no imagery."""
    import urllib.request
    from src.ee_auth import initialize
    ee=initialize(project)
    image=_thumbnail_image(result,ee,project)
    if image is None: return None
    region=ee.Geometry(result["geometry"])
    outline=ee.Image().byte().paint(ee.FeatureCollection([ee.Feature(region)]),1,2).visualize(palette=["0f3d2e"])
    bg=ee.Image(0).visualize(palette=["d9dedb"]).clip(region)  # grey = no clear pixel (cloud/gap)
    url=bg.blend(image.clip(region)).blend(outline).getThumbURL(
        {"region":region.bounds(),"dimensions":512,"format":"png"})
    path=thumbnail_path(result["event_id"],result["district"]); path.parent.mkdir(parents=True,exist_ok=True)
    with urllib.request.urlopen(url,timeout=120) as resp: path.write_bytes(resp.read())
    return path

# ------------------------------------------------------------------ deploy seed
# The SQLite database is not committed (it holds local runs and field notes), so the
# already computed event results are shipped as a gzip JSON seed. A fresh server
# (Render, Streamlit Cloud, Docker) imports it on first start; nothing is recomputed.
SEED_PATH=Path(__file__).resolve().parents[1]/"data"/"reference"/"catalog_results.json.gz"

def export_seed(db_path=None, seed_path: Path = SEED_PATH) -> int:
    import gzip
    from src import analysis_history as store
    rows=list(store.catalog_analyses(path=db_path).values())
    seed_path.parent.mkdir(parents=True,exist_ok=True)
    with gzip.open(seed_path,"wt",encoding="utf-8") as f: json.dump(rows,f,default=str)
    return len(rows)

def import_seed(db_path=None, seed_path: Path = SEED_PATH) -> int:
    """Add seed results whose event/district is not in the database yet; returns rows added."""
    import gzip
    from src import analysis_history as store
    if not seed_path.exists(): return 0
    with gzip.open(seed_path,"rt",encoding="utf-8") as f: rows=json.load(f)
    have=store.catalog_analyses(path=db_path); added=0
    for r in rows:
        if (r["event_id"],r["district"]) not in have:
            store.save_catalog_analysis(r,db_path); added+=1
    return added
