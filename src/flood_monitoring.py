"""Automatic KPK-wide flood event discovery and event analysis on real Earth Engine data.

Workflow (``run_monitoring``):
1. Find the latest Sentinel-1 IW acquisition available over Khyber Pakhtunkhwa.
2. List every acquisition slice (date + orbit track) in the monitoring window
   and in an earlier background window.
3. For each slice, compare it with a same-track median of the preceding 60 days
   (``s1_new_water``) and measure new water per KPK district at screening scale.
4. Learn each district's normal SAR change level from the background window,
   score background-relative excess water, group anomalies into events, add
   rainfall (CHIRPS, or GPM IMERG where CHIRPS has not been published yet).
5. Select the latest *detected* event. If none exists in the recent window,
   say so and search recent monsoon seasons as clearly-labelled history.
6. For the selected event retrieve the real before/after Sentinel-1 pair,
   measure flood, cropland, crop types, Sentinel-2 NDVI change and damage risk.
No value is produced unless Earth Engine returns it.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
import json
import logging
import time
from pathlib import Path
from typing import Callable

from src.ee_auth import initialize
from src.event_analysis import (district_backgrounds, score_slices, group_events, add_spatial_context,
    classify_event, rank_events, select_latest_reliable, previous_clear_observation)
from src.satellite_retrieval import (S1_COLLECTION, s1_new_water, s1_scene_image, ee_tile_url, s1_mosaic, s1_vv_median,
    sentinel1_candidates, sentinel1_pair_agricultural_impact, sentinel1_gallery_tile_layers)
from src.utils import NoImageryError

log=logging.getLogger(__name__)
GAUL_L2='FAO/GAUL/2025/level2'
KPK_NAME='Khyber Pakhtunkhwa'
Progress=Callable[[float,str],None]

def _noop(_fraction: float, _text: str) -> None: pass

def rule_params(cfg, event_scale: bool = False) -> dict:
    """Sentinel-1 rule keyword arguments derived from settings.

    Screening at 200 m already averages speckle through Earth Engine's image
    pyramid; event-scale (20 m) maps add a focal-median speckle filter.
    """
    rule={"change_db":cfg.s1_change_db,"water_db":cfg.s1_water_db,"vh_water_db":cfg.s1_vh_water_db,
          "max_slope_deg":cfg.max_slope_deg,"max_hand_m":cfg.max_hand_m,"max_elevation_m":cfg.max_elevation_m}
    if event_scale and cfg.s1_smoothing_m: rule["smoothing_m"]=cfg.s1_smoothing_m
    return rule

# ---------------------------------------------------------------- boundaries
def kpk_districts(ee):
    return (ee.FeatureCollection(GAUL_L2).filter(ee.Filter.eq('GAUL0_NAME','Pakistan'))
            .filter(ee.Filter.eq('GAUL1_NAME',KPK_NAME)).select(['GAUL2_NAME']))

def kpk_district_geojson(cache_dir: Path, project=None) -> dict:
    """Simplified FAO GAUL 2025 KPK district polygons, cached locally for map display."""
    cache_dir.mkdir(parents=True,exist_ok=True)
    path=cache_dir/'kpk_districts_gaul2025.geojson'
    if path.exists(): return json.loads(path.read_text(encoding='utf-8'))
    ee=initialize(project)
    fc=kpk_districts(ee).map(lambda f:f.simplify(250))
    data=fc.getInfo()
    from shapely.geometry import shape, mapping, MultiPolygon, Polygon
    for feature in data.get('features',[]):
        feature['properties']={'district':feature['properties'].get('GAUL2_NAME')}
        geom=shape(feature['geometry'])
        if geom.geom_type=='GeometryCollection':  # keep polygonal parts only (map libraries reject collections)
            parts=[p for g in geom.geoms for p in (g.geoms if isinstance(g,MultiPolygon) else [g]) if isinstance(p,Polygon)]
            feature['geometry']=mapping(MultiPolygon(parts))
    path.write_text(json.dumps(data),encoding='utf-8')
    return data

def district_geometry(name: str, cache_dir: Path, project=None) -> dict | None:
    for feature in kpk_district_geojson(cache_dir,project).get('features',[]):
        if feature['properties'].get('district')==name: return feature['geometry']
    return None

def tehsil_boundaries(cache_dir: Path, district_geometry_json: dict | None = None) -> list[dict]:
    """geoBoundaries PAK ADM3 tehsils (ODbL, 2017 census units) inside a district.

    Downloaded once from the geoBoundaries API and cached. Returns [] if the
    service is unreachable; callers must then offer district or drawn AOIs.
    """
    import requests
    from shapely.geometry import shape
    cache_dir.mkdir(parents=True,exist_ok=True)
    path=cache_dir/'geoBoundaries-PAK-ADM3-simplified.geojson'
    if not path.exists():
        meta=requests.get('https://www.geoboundaries.org/api/current/gbOpen/PAK/ADM3/',timeout=30,
                          headers={'User-Agent':'ZarailensAI/1.0'}).json()
        url=meta.get('simplifiedGeometryGeoJSON') or meta['gjDownloadURL']
        response=requests.get(url,timeout=120,headers={'User-Agent':'ZarailensAI/1.0'}); response.raise_for_status()
        path.write_bytes(response.content)
    data=json.loads(path.read_text(encoding='utf-8'))
    features=data.get('features',[])
    if district_geometry_json:
        district=shape(district_geometry_json).buffer(0)
        features=[f for f in features if district.contains(shape(f['geometry']).representative_point())]
    return [{'name':f['properties'].get('shapeName'),'geometry':f['geometry'],
             'source':'geoBoundaries PAK ADM3 (Tehsil, 2017) · ODbL 1.0'} for f in features]

# ---------------------------------------------------------------- availability
def latest_s1_acquisition(project=None) -> str | None:
    """ISO timestamp of the newest Sentinel-1 IW scene over KPK in Earth Engine."""
    ee=initialize(project)
    region=kpk_districts(ee).geometry()
    since=ee.Date(datetime.now(timezone.utc).isoformat()).advance(-60,'day')
    latest=(ee.ImageCollection(S1_COLLECTION).filterBounds(region).filterDate(since,ee.Date(datetime.now(timezone.utc).isoformat()).advance(1,'day'))
            .filter(ee.Filter.eq('instrumentMode','IW')).aggregate_max('system:time_start').getInfo())
    return datetime.fromtimestamp(latest/1000,timezone.utc).isoformat() if latest else None

def _slice_keys(ee, region, start: date, end: date) -> list[tuple[str,int,str]]:
    col=(ee.ImageCollection(S1_COLLECTION).filterBounds(region).filterDate(str(start),str(end))
         .filter(ee.Filter.eq('instrumentMode','IW')).filter(ee.Filter.listContains('transmitterReceiverPolarisation','VV')))
    info=col.reduceColumns(ee.Reducer.toList(3),['system:time_start','relativeOrbitNumber_start','orbitProperties_pass']).get('list').getInfo()
    return sorted({(datetime.fromtimestamp(t/1000,timezone.utc).date().isoformat(),int(r),p) for t,r,p in info})

# ---------------------------------------------------------------- screening
def screen_kpk(start: date, end: date, background_start: date, cfg, project=None,
               progress: Progress = _noop, workers: int = 3,
               district_names: list[str] | None = None, skip_keys: set | None = None,
               keys: list | None = None) -> dict:
    """Measure new water per district for every Sentinel-1 slice from background_start to end.

    ``district_names`` limits the search to those districts (automatic 5-district
    monitoring); ``skip_keys`` holds (date, relative_orbit, orbit) slices that were
    already processed so the same acquisition is never measured twice.
    """
    ee=initialize(project)
    districts=kpk_districts(ee)
    if district_names: districts=districts.filter(ee.Filter.inList('GAUL2_NAME',list(district_names)))
    region=districts.geometry()
    keys=[tuple(k) for k in keys] if keys is not None else _slice_keys(ee,region,background_start,end)
    keys=[k for k in keys if not skip_keys or k not in skip_keys]
    if not keys:
        return {"rows":[],"slices":0,"monitoring_slices":0,"errors":[]}
    base=(ee.ImageCollection(S1_COLLECTION).filterBounds(region).filter(ee.Filter.eq('instrumentMode','IW'))
          .filter(ee.Filter.listContains('transmitterReceiverPolarisation','VV')))
    crop=ee.ImageCollection('ESA/WorldCover/v200').first().select('Map').eq(40)
    rule=rule_params(cfg)
    def measure(key):
        day,relative_orbit,orbit=key
        d0=ee.Date(day)
        track=base.filter(ee.Filter.eq('relativeOrbitNumber_start',relative_orbit)).filter(ee.Filter.eq('orbitProperties_pass',orbit))
        frames=track.filterDate(d0,d0.advance(1,'day'))
        baseline_col=track.filterDate(d0.advance(-60,'day'),d0)
        after=s1_mosaic(frames,ee); before=s1_vv_median(baseline_col)
        water=s1_new_water(before,after,ee,**rule)
        pa=ee.Image.pixelArea()
        flood=water.selfMask()
        image=ee.Image.cat([pa.updateMask(flood).rename('flood_m2'),
                            pa.updateMask(flood.And(crop)).rename('crop_m2'),
                            pa.updateMask(water.mask()).rename('valid_m2')])
        fc=image.reduceRegions(districts.filterBounds(frames.geometry()),ee.Reducer.sum(),
                               scale=cfg.screening_scale_m,tileScale=4)
        dual=frames.filter(ee.Filter.listContains('transmitterReceiverPolarisation','VH')).size().eq(frames.size())
        fc=fc.filter(ee.Filter.gt('valid_m2',0)).map(lambda f:f.set({'baseline_images':baseline_col.size(),
            'dual_pol':dual,'frames':frames.size(),'time_start':frames.aggregate_min('system:time_start')}).setGeometry(None))
        last=None
        for attempt in range(4):
            try:
                features=fc.getInfo().get('features',[])
                phase="monitoring" if date.fromisoformat(day)>=start else "background"
                return [{"date":day,"district":f['properties']['GAUL2_NAME'],"orbit":orbit,
                         "relative_orbit":relative_orbit,"phase":phase,
                         "flood_ha":float(f['properties'].get('flood_m2') or 0)/1e4,
                         "crop_ha":float(f['properties'].get('crop_m2') or 0)/1e4,
                         "valid_ha":float(f['properties'].get('valid_m2') or 0)/1e4,
                         "baseline_images":int(f['properties'].get('baseline_images') or 0),
                         "dual_pol":bool(f['properties'].get('dual_pol')),"frames":int(f['properties'].get('frames') or 0),
                         "time_utc":datetime.fromtimestamp(f['properties']['time_start']/1000,timezone.utc).isoformat() if f['properties'].get('time_start') else None}
                        for f in features],None
            except Exception as exc:   # EE 429 "too many concurrent aggregations" is transient
                last=exc; time.sleep(3*(attempt+1))
        return [],f"{day} {orbit} {relative_orbit}: {last}"
    rows=[]; errors=[]; done=0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for part,error in pool.map(measure,keys):
            done+=1; rows.extend(part)
            if error: errors.append(error)
            progress(done/len(keys),f"Screened {done}/{len(keys)} Sentinel-1 acquisition slices")
    return {"rows":rows,"slices":len(keys),
            "monitoring_slices":sum(1 for k in keys if date.fromisoformat(k[0])>=start),"errors":errors}

# ---------------------------------------------------------------- rainfall
def _chirps_last(ee) -> date:
    t=ee.ImageCollection('UCSB-CHG/CHIRPS/DAILY').aggregate_max('system:time_start').getInfo()
    return datetime.fromtimestamp(t/1000,timezone.utc).date()

def rainfall_total(geometry, end_day: date, days: int = 7, project=None, chirps_last: date | None = None) -> tuple[float|None,str]:
    """Area-mean precipitation (mm) over ``days`` ending on end_day; CHIRPS, else GPM IMERG."""
    ee=initialize(project); region=ee.Geometry(geometry) if isinstance(geometry,dict) else geometry
    start=end_day-timedelta(days=days-1); stop=end_day+timedelta(days=1)
    chirps_last=chirps_last or _chirps_last(ee)
    if end_day<=chirps_last:
        image=ee.ImageCollection('UCSB-CHG/CHIRPS/DAILY').filterDate(str(start),str(stop)).select('precipitation').sum()
        source='CHIRPS Daily v2 (UCSB-CHG/CHIRPS/DAILY)'
    else:  # IMERG is mm/hr per half-hour step
        image=ee.ImageCollection('NASA/GPM_L3/IMERG_V07').filterDate(str(start),str(stop)).select('precipitation').sum().multiply(0.5)
        source='NASA GPM IMERG V07 (CHIRPS not yet published for these dates)'
    value=image.reduceRegion(ee.Reducer.mean(),region,scale=5000,bestEffort=True,maxPixels=1e9).get('precipitation').getInfo()
    return (float(value) if value is not None else None),source

def rainfall_daily(geometry: dict, start: date, end: date, project=None) -> dict:
    """Daily area-mean rainfall series for the event period."""
    ee=initialize(project); region=ee.Geometry(geometry)
    chirps_last=_chirps_last(ee)
    use_chirps=end<=chirps_last
    days=[start+timedelta(days=i) for i in range((end-start).days+1)]
    def daily(day):
        d=ee.Date(str(day))
        if use_chirps:
            image=ee.ImageCollection('UCSB-CHG/CHIRPS/DAILY').filterDate(d,d.advance(1,'day')).select('precipitation').sum()
        else:
            image=ee.ImageCollection('NASA/GPM_L3/IMERG_V07').filterDate(d,d.advance(1,'day')).select('precipitation').sum().multiply(0.5)
        return ee.Feature(None,{'date':str(day),'mm':image.reduceRegion(ee.Reducer.mean(),region,scale=5000,bestEffort=True,maxPixels=1e9).get('precipitation')})
    info=ee.FeatureCollection([daily(d) for d in days]).getInfo()
    series=[{"date":f['properties']['date'],"mm":f['properties'].get('mm')} for f in info.get('features',[])]
    return {"series":series,"source":"CHIRPS Daily v2" if use_chirps else "NASA GPM IMERG V07 (CHIRPS not yet published for these dates)"}

# ---------------------------------------------------------------- verification
def verify_event(event: dict, cfg, project=None) -> dict:
    """Independent checks of an event's peak radar signal (see ``verification_verdict``).

    Rebuilds the peak slice's new-water mask exactly as screening did, at 20 m,
    then measures (a) the share in connected patches ≥ 10 ha and (b) for pixels
    with cloud-free Sentinel-2 (Cloud Score+ ≥ 0.6) from 3 days before to 6 days
    after the acquisition, the share with MNDWI > 0 (open water).
    """
    ee=initialize(project)
    region=kpk_districts(ee).filter(ee.Filter.eq('GAUL2_NAME',event["district"])).geometry()
    day=ee.Date(str(event["peak_date"])[:10])
    track=(ee.ImageCollection(S1_COLLECTION).filterBounds(region).filter(ee.Filter.eq('instrumentMode','IW'))
           .filter(ee.Filter.listContains('transmitterReceiverPolarisation','VV'))
           .filter(ee.Filter.eq('relativeOrbitNumber_start',int(event["peak_relative_orbit"])))
           .filter(ee.Filter.eq('orbitProperties_pass',event["peak_orbit"])))
    after=s1_mosaic(track.filterDate(day,day.advance(1,'day')),ee)
    before=s1_vv_median(track.filterDate(day.advance(-60,'day'),day))
    flood=s1_new_water(before,after,ee,**rule_params(cfg,event_scale=True)).selfMask()
    large=flood.connectedPixelCount(256,True).reproject('EPSG:32642',None,20).gte(250)  # 250 px × 400 m² ≈ 10 ha
    s2=(ee.ImageCollection('COPERNICUS/S2_SR_HARMONIZED').filterBounds(region)
        .filterDate(day.advance(-3,'day'),day.advance(7,'day')))
    def mndwi(image):
        image=ee.Image(image)
        return image.normalizedDifference(['B3','B11']).rename('mndwi').updateMask(image.select('cs').gte(0.6))
    optical=s2.linkCollection(ee.ImageCollection('GOOGLE/CLOUD_SCORE_PLUS/V1/S2_HARMONIZED'),['cs']).map(mndwi).median()
    pa=ee.Image.pixelArea()
    historical=ee.Image('JRC/GSW1_4/GlobalSurfaceWater').select('occurrence').unmask(0).gte(10)
    sums=ee.Image.cat([pa.updateMask(flood).rename('sar'),pa.updateMask(flood.And(large)).rename('large'),
                       pa.updateMask(flood.And(historical)).rename('historical'),
                       pa.updateMask(flood.And(optical.mask())).rename('clear'),
                       pa.updateMask(flood.And(optical.gt(0))).rename('water')]).reduceRegion(
        ee.Reducer.sum(),region,20,maxPixels=1e10,bestEffort=True,tileScale=8)
    info=ee.Dictionary(sums).set('s2_scenes',s2.size()).getInfo()
    sar=float(info.get('sar') or 0); clear=float(info.get('clear') or 0)
    return {"radar_water_ha":sar/1e4,
            "large_patch_pct":100*float(info.get('large') or 0)/sar if sar else None,
            "historical_water_pct":100*float(info.get('historical') or 0)/sar if sar else None,
            "optical_clear_pct":100*clear/sar if sar else None,
            "optical_water_pct":100*float(info.get('water') or 0)/clear if clear else None,
            "s2_scenes":int(info.get('s2_scenes') or 0),
            "optical_window":[str(event["peak_date"])[:10]+" −3 d","+6 d"]}

# ---------------------------------------------------------------- discovery
def assess_event(event: dict, cfg, project=None, chirps_last: date | None = None) -> dict:
    """Rainfall evidence + independent verification + classification for one candidate event.

    Shared by the KPK-wide dashboard search and the automatic district monitor.
    Verification (Sentinel-2 optical water, spatial pattern, seasonal water) runs
    only for candidates strong enough to become a detected event.
    """
    ee=initialize(project)
    try:
        geom=kpk_districts(ee).filter(ee.Filter.eq('GAUL2_NAME',event["district"])).geometry()
        mm,source=rainfall_total(geom,event["peak_date"],7,project,chirps_last)
    except Exception as exc:
        log.warning("Rainfall unavailable for %s: %s",event["district"],exc); mm,source=None,None
    verification=None
    if event["peak_excess_ha"]>=cfg.confirmed_event_excess_ha and event["peak_ratio"]>=3:
        try: verification=verify_event(event,cfg,project)
        except Exception as exc:
            log.warning("Verification failed for %s: %s",event["district"],exc)
            verification={"error":str(exc)}
    result=classify_event(event,mm,cfg.confirmed_event_excess_ha,cfg.min_event_excess_ha,
                          verification if verification and "error" not in verification else None)
    if verification and "error" in verification: result["reasons"].append(f"verification failed: {verification['error'][:120]}")
    result["rainfall_source"]=source
    return result

def discover_events(start: date, end: date, cfg, project=None, progress: Progress = _noop) -> dict:
    """Screen KPK from start to end and return ranked, classified events."""
    background_start=start-timedelta(days=cfg.background_days)
    screening=screen_kpk(start,end,background_start,cfg,project,lambda f,t:progress(0.85*f,t))
    rows=screening["rows"]
    backgrounds=district_backgrounds(rows)
    scored=score_slices(rows,backgrounds)
    events=add_spatial_context(group_events(scored,cfg.min_event_excess_ha))
    chirps_last=_chirps_last(initialize(project)) if events else None
    progress(0.88,f"Checking rainfall and Sentinel-2 optical evidence for {len(events)} candidate event(s)")
    with_rain=lambda event:assess_event(event,cfg,project,chirps_last)
    with ThreadPoolExecutor(max_workers=3) as pool:
        classified=list(pool.map(with_rain,events))
    ranked=rank_events(classified)
    for event in ranked:
        clear=previous_clear_observation(scored,event)
        event["previous_clear_date"]=clear["date"] if clear else None
    progress(0.92,"Ranking candidate events")
    return {"window":[str(start),str(end)],"background_window":[str(background_start),str(start)],
            "slices":screening["slices"],"monitoring_slices":screening["monitoring_slices"],
            "errors":screening["errors"],"events":ranked,"selected":select_latest_reliable(ranked),
            "backgrounds":backgrounds,
            "district_summary":_district_summary(scored),"rows":scored}

def _district_summary(scored: list[dict]) -> list[dict]:
    """Per-district maximum monitoring-window excess, for the KPK overview map."""
    summary: dict[str,dict] = {}
    for row in scored:
        if row.get("phase")!="monitoring": continue
        item=summary.setdefault(row["district"],{"district":row["district"],"max_excess_ha":0.0,"max_flood_ha":0.0,"observations":0})
        item["observations"]+=1
        item["max_excess_ha"]=max(item["max_excess_ha"],row["excess_ha"])
        item["max_flood_ha"]=max(item["max_flood_ha"],row["flood_ha"])
    return sorted(summary.values(),key=lambda r:-r["max_excess_ha"])

def monsoon_windows(today: date, before: date, years: int) -> list[tuple[str,date,date]]:
    """Recent monsoon seasons (1 Jul – 30 Sep) ending before ``before``, newest first."""
    windows=[]; year=today.year
    while len(windows)<years and year>=2015:
        start,end=date(year,7,1),date(year,10,1)
        if end<=before: windows.append((f"Monsoon {year}",start,end))
        elif start<before: windows.append((f"Monsoon {year} (to {before})",start,before))
        year-=1
    return windows

# ---------------------------------------------------------------- event analysis
def _group_frames(frames: list[dict]) -> list[dict]:
    """Combine frames of one date and track into a single multi-frame scene."""
    groups: dict[tuple,dict] = {}
    for frame in frames:
        key=(frame['date'],frame['orbit'],frame['relative_orbit'])
        group=groups.setdefault(key,{**frame,'ids':[],'coverage':0.0})
        group['ids'].append(frame['id']); group['coverage']=min(100.0,group['coverage']+float(frame.get('coverage',0)))
    return sorted(groups.values(),key=lambda s:s['date'])

def event_scene_pair(event: dict, geometry: dict, project=None) -> tuple[dict,dict,dict]:
    """Actual same-track before/after Sentinel-1 scenes and the baseline used by the rule.

    ``before`` is the latest real acquisition on the same track before the first
    detection (displayed to the user). ``baseline`` is the same-track median of
    the 60 days before the peak — the identical reference used by KPK screening.
    """
    peak=date.fromisoformat(str(event["peak_date"])[:10])
    first=date.fromisoformat(str(event["first_detection"])[:10])
    frames=sentinel1_candidates(geometry,first-timedelta(days=60),peak+timedelta(days=1),project)
    same=[f for f in frames if f['orbit']==event['peak_orbit'] and int(f['relative_orbit'])==int(event['peak_relative_orbit'])]
    scenes=_group_frames(same)
    after=next((s for s in scenes if s['date']==peak),None)
    if after is None: raise NoImageryError(f"The peak acquisition {peak} could not be retrieved for {event['district']}.")
    clear=event.get("previous_clear_date")
    clear=date.fromisoformat(str(clear)[:10]) if clear else None
    before_options=[s for s in scenes if s['date']<first]
    before=next((s for s in before_options if s['date']==clear),None) or (before_options[-1] if before_options else None)
    if before is None: raise NoImageryError("No same-track Sentinel-1 acquisition precedes this event within 60 days.")
    history=[s for s in scenes if peak-timedelta(days=60)<=s['date']<peak]
    baseline={**before,'ids':[i for s in history for i in s['ids']],'composite':'median',
              'dates':[str(s['date']) for s in history]}
    return before,after,baseline

def analyze_event(event: dict, cfg, project=None, progress: Progress = _noop) -> dict:
    """Full district analysis for one event: pair, flood, cropland, crops, NDVI, rainfall."""
    from src.agricultural_analysis import event_crop_assessment
    geometry=district_geometry(event["district"],cfg.boundaries_dir,project)
    if geometry is None: raise NoImageryError(f"No boundary found for {event['district']}.")
    progress(0.93,"Retrieving the actual before/after Sentinel-1 acquisitions")
    before,after,baseline=event_scene_pair(event,geometry,project)
    rule=rule_params(cfg,event_scale=True)
    progress(0.95,"Measuring flooded and agricultural area")
    impact=sentinel1_pair_agricultural_impact(baseline,after,geometry,project,scale=cfg.event_scale_m,**rule)
    impact.pop("layers",None)
    ee=initialize(project)
    flood_image=s1_new_water(s1_scene_image(baseline,ee),s1_scene_image(after,ee),ee,**rule)
    progress(0.97,"Overlaying crop types and Sentinel-2 NDVI")
    crops=event_crop_assessment(flood_image,geometry,before['date'],after['date'],cfg.event_scale_m,project)
    crops.pop("layers",None)
    progress(0.98,"Ranking tehsils for field inspection")
    try:
        from src.agricultural_analysis import risk_by_zone
        tehsils=tehsil_boundaries(cfg.boundaries_dir,geometry)
        priorities=risk_by_zone(flood_image,geometry,before['date'],after['date'],
                                crops.get("unflooded_ndvi_change",{}),tehsils,max(cfg.event_scale_m,30),project)
        priority_note=None if tehsils else "No geoBoundaries tehsil polygons fall inside this district."
    except Exception as exc:
        log.warning("Tehsil priorities unavailable: %s",exc); priorities=[]; priority_note=f"Tehsil boundaries unavailable: {exc}"
    rain=rainfall_daily(geometry,before['date']-timedelta(days=7),after['date'],project)
    onset=[r for r in rain["series"] if r["mm"] is not None and before['date']<date.fromisoformat(r["date"])<=after['date']]
    wettest=max(onset,key=lambda r:r["mm"]) if onset else None
    def scene_meta(s):
        return {k:(str(v) if isinstance(v,date) else v) for k,v in s.items() if k in ('id','ids','date','time_utc','orbit','relative_orbit','polarizations','coverage','platform')}
    flood_ha=impact['flood_ha']; ag=impact['exposed_cropland_ha']
    return {"district":event["district"],"geometry":geometry,"before":scene_meta(before),"after":scene_meta(after),
            "baseline":{"ids":baseline["ids"],"dates":baseline["dates"],"orbit":baseline["orbit"],
                        "relative_orbit":baseline["relative_orbit"],"method":"same-track median of the 60 days before the peak"},
            "flood_ha":flood_ha,"agricultural_flood_ha":ag,"non_agricultural_flood_ha":max(flood_ha-ag,0.0),
            "total_cropland_ha":impact['total_cropland_ha'],"exposed_cropland_percent":impact['exposed_cropland_percent'],
            "cropland_source":impact['cropland_source'],"permanent_water_source":impact['permanent_water_source'],
            "crops":crops,"tehsil_priorities":priorities,"tehsil_priority_note":priority_note,"rainfall":rain,"wettest_day_in_onset_window":wettest,
            "onset_window":[str(before['date']),str(after['date'])],"scale_m":cfg.event_scale_m,
            "rule":rule,"satellite_source":"Copernicus Sentinel-1 GRD (COPERNICUS/S1_GRD) via Google Earth Engine"}

def event_tile_layers(detail: dict, project=None) -> dict:
    """Regenerate live XYZ tile URLs (EE map IDs expire) for an analyzed event."""
    from src.agricultural_analysis import crop_risk_layers
    ee=initialize(project)
    before={**detail["before"],"date":date.fromisoformat(detail["before"]["date"])}
    after={**detail["after"],"date":date.fromisoformat(detail["after"]["date"])}
    rule=detail["rule"]
    baseline={**detail["before"],**detail.get("baseline",{}),"composite":"median"} if detail.get("baseline") else before
    layers=sentinel1_gallery_tile_layers(before,after,detail["geometry"],project,**rule)
    region=ee.Geometry(detail["geometry"])
    flood_image=s1_new_water(s1_scene_image(baseline,ee),s1_scene_image(after,ee),ee,**rule)
    layers["flood"]=ee_tile_url(flood_image.selfMask().clip(region),{'min':1,'max':1,'palette':['18d69e']})
    worldcover=ee.ImageCollection('ESA/WorldCover/v200').first().select('Map').clip(region)
    layers["cropland"]=ee_tile_url(worldcover.eq(40).selfMask(),{'min':1,'max':1,'palette':['f2c12e']})
    layers["flood_blue"]=ee_tile_url(flood_image.selfMask().clip(region),{'min':1,'max':1,'palette':['2f7de1']})
    from src.satellite_retrieval import sentinel2_true_color_layer
    for key,(start,end) in {"s2_before":(before["date"]-timedelta(days=20),before["date"]+timedelta(days=1)),
                            "s2_after":(after["date"],min(after["date"]+timedelta(days=10),date.today()+timedelta(days=1)))}.items():
        try: layers[key]=sentinel2_true_color_layer(detail["geometry"],start,end,project)
        except Exception as exc: layers[key]=None; log.warning("Sentinel-2 layer unavailable: %s",exc)
    layers.update(crop_risk_layers(flood_image,detail["geometry"],before["date"],after["date"],
                                   detail["crops"].get("unflooded_ndvi_change",{}),project))
    return layers

def main_affected_crop(crop_rows: list[dict]) -> str | None:
    """Mapped crop type with the largest flooded area; unmapped 'other' classes excluded."""
    named=[r for r in crop_rows if not r["crop"].startswith("Other") and r.get("affected_ha",0)>=1]
    return max(named,key=lambda r:r["affected_ha"])["crop"] if named else None

def overall_risk(crop_rows: list[dict]) -> str:
    """Highest evidence-based risk among crops with ≥ 1 ha flooded."""
    order=["High","Medium","Low"]
    exposed=[r for r in crop_rows if r.get("affected_ha",0)>=1]
    for level in order:
        if any(r["risk"]==level for r in exposed): return level
    return "Insufficient Data" if exposed else "Low"

def monitoring_summary(run: dict, event: dict | None = None, detail: dict | None = None) -> dict:
    """Flat report summary of a monitoring run's selected (or given) event."""
    event=event or run.get("selected"); detail=detail if detail is not None else run.get("detail")
    summary={"report":"ZarailensAI automatic KPK flood monitoring",
             "data_refreshed_utc":run.get("refreshed_at"),"latest_sentinel1_acquisition":run.get("latest_acquisition"),
             "monitoring_window":" → ".join(run.get("monitoring_window") or []) or "unavailable",
             "event_source":run.get("selected_source")}
    if not event:
        summary["result"]=run.get("message") or "No confirmed recent flood event detected"
    else:
        summary.update({"location":f"{event['district']} district, Khyber Pakhtunkhwa","event_status":event["status"],
            "event_confidence":event["confidence"],"event_score":event["score"],
            "first_detection":event["first_detection"],"peak_acquisition":event["peak_date"],
            "screening_excess_new_water_ha":round(float(event["peak_excess_ha"]),1),
            "rainfall_7d_mm":event.get("rainfall_7d_mm"),"rainfall_source":event.get("rainfall_source"),
            "evidence":"; ".join(event.get("reasons",[]))})
    if detail:
        crops=detail.get("crops",{})
        summary.update({"before_acquisition":detail["before"].get("time_utc"),"after_acquisition":detail["after"].get("time_utc"),
            "flood_onset_window":" → ".join(detail.get("onset_window",[])),
            "orbit":detail["after"].get("orbit"),"relative_orbit":detail["after"].get("relative_orbit"),
            "satellite_source":detail.get("satellite_source"),"resolution_m":detail.get("scale_m"),
            "flood_ha":detail["flood_ha"],"ag_ha":detail["agricultural_flood_ha"],
            "non_agricultural_flood_ha":detail["non_agricultural_flood_ha"],"total_cropland_ha":detail["total_cropland_ha"],
            "cropland_source":detail.get("cropland_source"),"crop_type_source":crops.get("source"),
            "ndvi_source":crops.get("ndvi_source"),"season":crops.get("season"),
            "main_affected_crop":main_affected_crop(crops.get("rows",[])) or "Not identified in mapped crop types",
            "overall_damage_risk":overall_risk(crops.get("rows",[])),
            "crop_rows":crops.get("rows",[]),"rainfall_series":(detail.get("rainfall") or {}).get("series",[])})
    events=(run.get("recent") or {}).get("events",[])
    if events: summary["candidate_events"]=events[:25]
    summary["caveats"]=["Flood extent is a Sentinel-1 threshold indicator; ground validation is required",
        "Crop types are ESA WorldCereal 2021 maps, not current-season observations",
        "Damage risk is a preliminary class from flood exposure and NDVI change, not measured crop loss",
        "No crop destruction percentage or financial loss is estimated (no validated model or ground truth)"]
    return summary

# ---------------------------------------------------------------- full run
def _jsonable(value):
    return json.loads(json.dumps(value,default=str))

def run_monitoring(cfg, project=None, progress: Progress = _noop, today: date | None = None) -> dict:
    """Automatic monitoring run. Always returns a result; status 'error' on failure."""
    today=today or date.today()
    result={"refreshed_at":datetime.now(timezone.utc).isoformat(),
            "params":{"monitoring_days":cfg.monitoring_days,"background_days":cfg.background_days,
                      "screening_scale_m":cfg.screening_scale_m,"event_scale_m":cfg.event_scale_m,
                      **rule_params(cfg),"min_event_excess_ha":cfg.min_event_excess_ha,
                      "confirmed_event_excess_ha":cfg.confirmed_event_excess_ha}}
    try:
        progress(0.01,"Checking the latest Sentinel-1 data available in Earth Engine")
        latest=latest_s1_acquisition(project)
        result["latest_acquisition"]=latest
        if not latest:
            result.update(status="no_data",message="No Sentinel-1 acquisitions over KPK were returned for the last 60 days.")
            return result
        end=date.fromisoformat(latest[:10])+timedelta(days=1)
        start=end-timedelta(days=cfg.monitoring_days)
        result["monitoring_window"]=[str(start),str(end-timedelta(days=1))]
        recent=discover_events(start,end,cfg,project,lambda f,t:progress(0.02+0.6*f,t))
        result["recent"]=recent
        selected=recent["selected"]; source="recent"
        result["historical"]=[]
        if selected is None:
            for label,h_start,h_end in monsoon_windows(today,start,cfg.historical_fallback_years):
                progress(0.65,f"No confirmed recent event — searching {label}")
                found=discover_events(h_start,h_end,cfg,project,lambda f,t,l=label:progress(0.65+0.25*f,f"{l}: {t}"))
                found["label"]=label; found.pop("rows",None)
                result["historical"].append(found)
                if found["selected"]:
                    selected=found["selected"]; source=label; break
        result["selected"]=selected; result["selected_source"]=source
        if selected:
            try:
                result["detail"]=analyze_event(selected,cfg,project,progress)
            except Exception as exc:
                log.exception("Event analysis failed")
                result["detail"]=None; result["detail_error"]=str(exc)
        result["status"]="event" if selected and source=="recent" else "historical_event" if selected else "no_event"
        result["message"]=(None if source=="recent" and selected else
                           "No confirmed recent flood event detected in the monitoring window.")
        recent.pop("rows",None)
        progress(1.0,"Monitoring run complete")
    except Exception as exc:
        log.exception("Monitoring run failed")
        result.update(status="error",message=str(exc))
    return _jsonable(result)

def analyze_historical_window(start: date, end: date, cfg, project=None, progress: Progress = _noop,
                              event_index: int | None = None) -> dict:
    """User-requested KPK-wide search of a past period (same method, labelled historical)."""
    found=discover_events(start,end,cfg,project,progress)
    found.pop("rows",None)
    chosen=found["events"][event_index] if event_index is not None and event_index<len(found["events"]) else found["selected"]
    if chosen:
        try: found["detail"]=analyze_event(chosen,cfg,project,progress)
        except Exception as exc: found["detail"]=None; found["detail_error"]=str(exc)
    found["chosen"]=chosen
    return _jsonable(found)
