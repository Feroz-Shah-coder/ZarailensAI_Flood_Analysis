"""Earth Engine multi-sensor acquisition search, composites and previews."""
from dataclasses import dataclass
from datetime import date, datetime, timezone, timedelta
import logging
import time
import requests
from src.ee_auth import initialize
from src.utils import NoImageryError, ProcessingError
from src.event_analysis import choose_peak
from src.date_selection import available_sensors, EARLIEST_LANDSAT

log=logging.getLogger(__name__)

S1_COLLECTION='COPERNICUS/S1_GRD'
JRC_WATER='JRC/GSW1_4/GlobalSurfaceWater'

def s1_mosaic(collection, ee):
    """Same-date mosaic with bands every frame has (2015-era scenes may lack VH)."""
    dual=collection.filter(ee.Filter.listContains('transmitterReceiverPolarisation','VH'))
    return ee.Image(ee.Algorithms.If(dual.size().eq(collection.size()),
                                     collection.select(['VV','VH']).mosaic(),
                                     collection.select(['VV']).mosaic()))

def s1_vv_median(collection):
    """Same-track baseline: VV-only median (the rule uses only VV from the baseline).

    Selecting VV first keeps the collection homogeneous when single-polarisation
    (VV) and dual-polarisation (VV+VH) scenes are mixed, as over KPK in 2015.
    """
    return collection.select(['VV']).median()

def s1_scene_image(scene: dict, ee):
    """Real Sentinel-1 image for a scene dict; multi-frame scenes (``ids``) are mosaicked.

    A district can span two consecutive frames of the same pass. Frames from one
    date and one relative orbit are acquired seconds apart, so mosaicking them is
    a single observation, not a temporal composite.
    """
    ids=scene.get('ids') or [scene['id']]
    if scene.get('composite')=='median':
        # Same-track multi-date baseline (used as the "before" state of the flood rule).
        return s1_vv_median(ee.ImageCollection.fromImages([ee.Image(i) for i in ids]))
    if len(ids)==1: return ee.Image(ids[0])
    first=ee.Image(ids[0])
    return ee.Image(s1_mosaic(ee.ImageCollection.fromImages([ee.Image(i) for i in ids]),ee).copyProperties(first,['system:time_start']))

def terrain_mask(ee, max_slope_deg: float = 10, max_hand_m: float | None = None,
                 max_elevation_m: float | None = None):
    """Pixels where open floodwater is physically plausible and SAR is not in layover/shadow.

    Slope from SRTM; optional MERIT Hydro height-above-nearest-drainage and an
    elevation ceiling (wet snow in high mountains mimics water in C-band SAR).
    """
    dem=ee.Image('USGS/SRTMGL1_003')
    mask=ee.Terrain.slope(dem).lt(max_slope_deg)
    if max_hand_m is not None: mask=mask.And(ee.Image('MERIT/Hydro/v1_0_1').select('hnd').lt(max_hand_m))
    if max_elevation_m is not None: mask=mask.And(dem.lt(max_elevation_m))
    return mask

def permanent_water_mask(ee, occurrence_percent: float = 90):
    """JRC Global Surface Water pixels that are water in ≥ occurrence_percent of observations."""
    return ee.Image(JRC_WATER).select('occurrence').unmask(0).gte(occurrence_percent)

def s1_new_water(before, after, ee, change_db: float = -3, water_db: float = -15,
                 vh_water_db: float | None = None, max_slope_deg: float = 10,
                 max_hand_m: float | None = None, max_elevation_m: float | None = None,
                 smoothing_m: float | None = None):
    """Shared Sentinel-1 change rule used by every flood calculation in the app.

    New water = VV(after) − VV(before) < change_db AND VV(after) < water_db
    [AND VH(after) < vh_water_db when VH was acquired], minus JRC permanent water
    and terrain where flooding/SAR measurement is implausible. Pixels without a
    valid before and after observation are masked (invalid-pixel filtering).
    ``smoothing_m`` applies a circular focal median (speckle reduction) first.
    Returns a 0/1 image (1 = new water) masked to valid observations.
    """
    if smoothing_m:
        before=before.focalMedian(smoothing_m,'circle','meters')
        after=after.focalMedian(smoothing_m,'circle','meters')
    vv_after=after.select('VV'); vv_before=before.select('VV')
    water=vv_after.subtract(vv_before).lt(change_db).And(vv_after.lt(water_db))
    if vh_water_db is not None:
        has_vh=after.bandNames().contains('VH')
        vh_rule=ee.Image(ee.Algorithms.If(has_vh,after.select(['VH']).lt(vh_water_db),ee.Image(1)))
        water=water.And(vh_rule)
    valid=vv_after.mask().And(vv_before.mask()).And(terrain_mask(ee,max_slope_deg,max_hand_m,max_elevation_m))
    return water.And(permanent_water_mask(ee).Not()).updateMask(valid).rename('new_water')

def ee_tile_url(image, visual) -> str:
    """Live Earth Engine XYZ tile URL for a visualized image."""
    map_info=image.getMapId(visual); fetcher=map_info.get('tile_fetcher')
    url=getattr(fetcher,'url_format',None)
    if not url or not all(token in url for token in ('{z}','{x}','{y}')):
        raise ProcessingError("Earth Engine did not return a usable XYZ tile URL.")
    return url

@dataclass(frozen=True)
class SensorSpec:
    name: str
    collection: str
    start: date
    end: date
    red: str
    green: str
    blue: str | None
    nir: str | None
    swir1: str | None
    scale_factor: float
    offset: float
    resolution_m: int
    cloud_method: str
    false_color_only: bool = False

class SensorCatalog:
    """Supported real Earth Engine optical and radar archives by date."""
    _mss=[
        SensorSpec("Landsat 1 MSS","LANDSAT/LM01/C02/T1",date(1972,7,23),date(1978,1,7),"B5","B4",None,"B6",None,1,0,60,"QA_PIXEL",True),
        SensorSpec("Landsat 2 MSS","LANDSAT/LM02/C02/T1",date(1975,1,31),date(1982,2,4),"B5","B4",None,"B6",None,1,0,60,"QA_PIXEL",True),
        SensorSpec("Landsat 3 MSS","LANDSAT/LM03/C02/T1",date(1978,6,3),date(1983,2,24),"B5","B4",None,"B6",None,1,0,60,"QA_PIXEL",True),
    ]
    _l4=SensorSpec("Landsat 4 TM","LANDSAT/LT04/C02/T1_L2",date(1982,8,22),date(1993,6,25),"SR_B3","SR_B2","SR_B1","SR_B4","SR_B5",.0000275,-.2,30,"QA_PIXEL")
    _l5=SensorSpec("Landsat 5 TM","LANDSAT/LT05/C02/T1_L2",date(1984,3,16),date(2012,5,6),"SR_B3","SR_B2","SR_B1","SR_B4","SR_B5",.0000275,-.2,30,"QA_PIXEL")
    _l7=SensorSpec("Landsat 7 ETM+","LANDSAT/LE07/C02/T1_L2",date(1999,5,28),date(2024,1,20),"SR_B3","SR_B2","SR_B1","SR_B4","SR_B5",.0000275,-.2,30,"QA_PIXEL; SLC-off after 2003-05-31")
    _l8=SensorSpec("Landsat 8 OLI","LANDSAT/LC08/C02/T1_L2",date(2013,3,18),date(2100,1,1),"SR_B4","SR_B3","SR_B2","SR_B5","SR_B6",.0000275,-.2,30,"QA_PIXEL")
    _l9=SensorSpec("Landsat 9 OLI-2","LANDSAT/LC09/C02/T1_L2",date(2021,10,31),date(2100,1,1),"SR_B4","SR_B3","SR_B2","SR_B5","SR_B6",.0000275,-.2,30,"QA_PIXEL")
    _s2toa=SensorSpec("Sentinel-2 MSI L1C","COPERNICUS/S2_HARMONIZED",date(2015,6,27),date(2100,1,1),"B4","B3","B2","B8","B11",.0001,0,10,"QA60")
    _s2sr=SensorSpec("Sentinel-2 MSI L2A","COPERNICUS/S2_SR_HARMONIZED",date(2017,3,28),date(2100,1,1),"B4","B3","B2","B8","B11",.0001,0,10,"SCL")

    @classmethod
    def for_date(cls, day: date, aoi_area_km2: float | None = None) -> list[SensorSpec]:
        if day < EARLIEST_LANDSAT: return []
        if day < date(1999,1,1):
            result=[s for s in cls._mss if s.start<=day<s.end]
            if cls._l4.start<=day<cls._l4.end: result.append(cls._l4)
            if cls._l5.start<=day<cls._l5.end: result.append(cls._l5)
            return result
        if day < date(2013,1,1):
            result=[s for s in (cls._l5,cls._l7) if s.start<=day<s.end]
            if aoi_area_km2 and aoi_area_km2>=5000 and day>=date(2000,2,24): result.append(SensorSpec("MODIS MOD09GA (coarse, last resort)","MODIS/061/MOD09GA",date(2000,2,24),date(2100,1,1),"sur_refl_b01","sur_refl_b04","sur_refl_b03","sur_refl_b02","sur_refl_b06",.0001,0,500,"state_1km"))
            return result
        if day<cls._l8.start: return [cls._l7] if cls._l7.start<=day<cls._l7.end else []
        result=[cls._l8]
        if day>=cls._l9.start: result.append(cls._l9)
        if day>=cls._s2toa.start: result.append(cls._s2toa)
        if day>=cls._s2sr.start: result.append(cls._s2sr)
        if day>=date(2014,10,3): result.append(SensorSpec("Sentinel-1 SAR","COPERNICUS/S1_GRD",date(2014,10,3),date(2100,1,1),"VV","VV","VH",None,None,1,0,10,"IW VV/VH",True))
        return result

    @classmethod
    def optical_for_date(cls, day: date, aoi_area_km2: float | None = None) -> list[SensorSpec]:
        return [s for s in cls.for_date(day,aoi_area_km2) if "SAR" not in s.name]

def scale_landsat_reflectance(value):
    """Apply Collection 2 L2 optical scale and offset."""
    return value*0.0000275-0.2

def sensor_band_map(spec: SensorSpec) -> dict[str,str | None]:
    return {"red":spec.red,"green":spec.green,"blue":spec.blue,"nir":spec.nir,"swir1":spec.swir1}

def visualization_bands(spec: SensorSpec, view="true_color") -> list[str]:
    """Return image bands in Earth Engine's red, green, blue channel order."""
    if view=="mndwi": return ["MNDWI"]
    if spec.false_color_only: return [spec.nir,spec.red,spec.green]
    return [spec.red,spec.green,spec.blue]

def _clear_mask(image, spec: SensorSpec, ee):
    if spec.cloud_method=="state_1km":
        state=image.select("state_1km")
        cloud_state=state.bitwiseAnd(3)
        shadow=state.bitwiseAnd(1<<2).neq(0)
        internal_cloud=state.bitwiseAnd(1<<10).neq(0)
        return cloud_state.eq(0).And(shadow.Not()).And(internal_cloud.Not())
    if spec.cloud_method.startswith("QA_PIXEL"):
        qa=image.select("QA_PIXEL")
        bad=(1<<0)|(1<<1)|(1<<2)|(1<<3)|(1<<4)
        return qa.bitwiseAnd(bad).eq(0)
    if spec.cloud_method=="SCL":
        scl=image.select("SCL")
        return scl.neq(0).And(scl.neq(1)).And(scl.neq(3)).And(scl.neq(8)).And(scl.neq(9)).And(scl.neq(10)).And(scl.neq(11))
    if spec.cloud_method=="QA60":
        qa=image.select("QA60")
        return qa.bitwiseAnd(1<<10).eq(0).And(qa.bitwiseAnd(1<<11).eq(0))
    return image.select(spec.red).mask()

def search_historical_candidates(geometry: dict, requested: date, window_days=10,
                                 aoi_area_km2: float | None = None, project=None) -> dict[str,list[dict]]:
    """Search all catalog sensors overlapping a request and return true scene metadata."""
    ee=initialize(project); region=ee.Geometry(geometry)
    start=requested-timedelta(days=window_days); end=requested+timedelta(days=window_days+1)
    specs=SensorCatalog.for_date(requested,aoi_area_km2)
    found={}; errors=[]
    for spec in specs:
        if "SAR" in spec.name: continue
        lower=max(start,spec.start); upper=min(end,spec.end)
        if lower>=upper: continue
        try:
            collection=ee.ImageCollection(spec.collection).filterBounds(region).filterDate(str(lower),str(upper))
            def meta(image):
                image=ee.Image(image); clear=_clear_mask(image,spec,ee)
                cloud=clear.Not().rename("cloud").reduceRegion(reducer=ee.Reducer.mean(),geometry=region,scale=spec.resolution_m,bestEffort=True,maxPixels=1e9,tileScale=4).get("cloud")
                cloud=ee.Algorithms.If(ee.Algorithms.IsEqual(cloud,None),100,ee.Number(cloud).multiply(100))
                coverage=image.geometry().intersection(region,maxError=1).area(maxError=1).divide(region.area(maxError=1)).multiply(100)
                d=image.toDictionary(["system:index","system:time_start","SPACECRAFT_ID","SPACECRAFT_NAME","CLOUD_COVER","CLOUDY_PIXEL_PERCENTAGE"]).set("cloud_percent_aoi",cloud).set("coverage_percent_aoi",coverage)
                return ee.Feature(None,d.set("scene_id",ee.String(spec.collection+"/").cat(image.get("system:index"))))
            raw=ee.FeatureCollection(collection.sort("system:time_start").limit(100).map(meta)).getInfo().get("features",[])
            scenes=[]
            for f in raw:
                p=f["properties"]; ts=p.get("system:time_start")
                if ts is None: continue
                cloud_pct=float(p.get("cloud_percent_aoi",100)); coverage=float(p.get("coverage_percent_aoi",0))
                scenes.append({"id":p["scene_id"],"date":datetime.fromtimestamp(ts/1000,timezone.utc).date(),"time_utc":datetime.fromtimestamp(ts/1000,timezone.utc).isoformat(),"sensor":spec.name,"collection":spec.collection,"cloud_percent":cloud_pct,"coverage_percent":coverage,"cloud_free_aoi_percent":coverage*(100-cloud_pct)/100,"spacecraft":p.get("SPACECRAFT_ID") or p.get("SPACECRAFT_NAME") or spec.name,"resolution_m":spec.resolution_m,"scene_index":p.get("system:index"),"spec":spec})
            found[spec.name]=scenes
        except Exception as exc:
            log.warning("Could not query %s for %s: %s",spec.name,requested,exc)
            found[spec.name]=[]
            errors.append({"sensor":spec.name,"error":str(exc)})
    found["_errors"]=errors
    return found

def _prepare_sensor_image(image, spec: SensorSpec, ee):
    image=ee.Image(image).updateMask(_clear_mask(ee.Image(image),spec,ee))
    if spec.false_color_only: return image
    selected=[b for b in [spec.red,spec.green,spec.blue,spec.nir,spec.swir1] if b]
    scaled=image.select(selected).multiply(spec.scale_factor).add(spec.offset)
    return scaled.updateMask(scaled.select(spec.red).gte(0))

def prepared_optical_image(scene_ids: list[str], spec: SensorSpec, geometry: dict, project=None):
    """Cloud mask, scale, median mosaic and clip scenes from one sensor."""
    if not scene_ids: raise NoImageryError("No real scenes were supplied; no image will be generated.")
    ee=initialize(project)
    images=ee.ImageCollection.fromImages([ee.Image(scene_id) for scene_id in scene_ids])
    return images.map(lambda image:_prepare_sensor_image(image,spec,ee)).median().clip(ee.Geometry(geometry))

def optical_thumbnail(scene_ids: list[str], spec: SensorSpec, geometry: dict,
                      dimensions=1024, project=None, view="true_color") -> bytes:
    """Build a real shared-stretch optical preview and download its PNG bytes."""
    if not scene_ids: raise NoImageryError("No real scenes were supplied; no image will be generated.")
    ee=initialize(project); image=prepared_optical_image(scene_ids,spec,geometry,project)
    if view=="mndwi":
        if not spec.swir1: raise ProcessingError(f"{spec.name} has no SWIR1 band; MNDWI is unavailable.")
        image=image.normalizedDifference([spec.green,spec.swir1]).rename("MNDWI")
        bands=visualization_bands(spec,"mndwi"); vis={"min":-1,"max":1,"palette":["8c510a","f6e8c3","01665e"]}
    elif spec.false_color_only:
        bands=visualization_bands(spec,"false_color"); vis={"min":0,"max":3000,"gamma":1.3}
    else:
        bands=visualization_bands(spec); vis={"min":0,"max":.3,"gamma":1.3}
    last=None
    for size in (dimensions,512,256):
        try:
            params={"region":geometry,"dimensions":size,"format":"png","crs":"EPSG:4326","bands":bands,"min":vis["min"],"max":vis["max"]}
            if vis.get("gamma") is not None: params["gamma"]=vis["gamma"]
            if vis.get("palette") is not None: params["palette"]=vis["palette"]
            url=image.getThumbURL(params)
            response=requests.get(url,timeout=60); response.raise_for_status()
            if not response.content.startswith(b"\x89PNG"): raise ProcessingError("Earth Engine thumbnail did not return PNG image bytes.")
            return response.content
        except Exception as exc:
            last=exc; time.sleep(.5)
    raise ProcessingError(f"No real optical preview could be downloaded after smaller-size retries: {last}") from last

def optical_flood_time_series(geometry: dict, approximate_date: date, scan_days: int,
                              sensor_name: str, cloud_limit: float=60,
                              mndwi_threshold: float=0, project=None) -> dict:
    """Use one real optical sensor to score cloud-masked water change through time."""
    ee=initialize(project); area_km2=None
    spec=next((s for s in SensorCatalog.optical_for_date(approximate_date,area_km2) if s.name==sensor_name),None)
    if spec is None: raise NoImageryError(f"{sensor_name} is not available for {approximate_date}.")
    if not spec.swir1: raise NoImageryError(f"{sensor_name} has no SWIR band; MNDWI flood detection is unavailable for this sensor. Real imagery can still be previewed.")
    current=search_historical_candidates(geometry,approximate_date,scan_days,None,project).get(sensor_name,[])
    baseline_center=approximate_date-timedelta(days=scan_days+30)
    baseline_candidates=search_historical_candidates(geometry,baseline_center,30,None,project).get(sensor_name,[])
    scan_start=approximate_date-timedelta(days=scan_days)
    baseline_candidates=[s for s in baseline_candidates if s['date']<scan_start and s['cloud_percent']<=cloud_limit]
    current=[s for s in current if s['cloud_percent']<=cloud_limit]
    if not current: raise NoImageryError(f"No {sensor_name} scenes meet the {cloud_limit:.0f}% cloud limit in this window. Widen the window or change sensors.")
    if not baseline_candidates:
        return {"rows":[],"peak":None,"before":None,"candidate_scenes":current,"baseline_scenes":[],"requested_date":approximate_date,"message":"No clear pre-event optical baseline was available for this window."}
    baseline=prepared_optical_image([s['id'] for s in baseline_candidates],spec,geometry,project)
    base_index=baseline.normalizedDifference([spec.green,spec.swir1]).rename('mndwi')
    event_images=ee.ImageCollection.fromImages([ee.Image(s['id']) for s in current])
    permanent=permanent_water_mask(ee)  # unmasked: JRC occurrence is empty where water was never seen
    slope=ee.Terrain.slope(ee.Image('USGS/SRTMGL1_003')).lt(10)
    region=ee.Geometry(geometry)
    def measure(raw_image):
        image=_prepare_sensor_image(raw_image,spec,ee)
        index=image.normalizedDifference([spec.green,spec.swir1]).rename('mndwi')
        valid=base_index.mask().And(index.mask())
        flood=index.gt(mndwi_threshold).And(base_index.lte(mndwi_threshold)).And(permanent.Not()).And(slope).updateMask(valid).selfMask()
        area=ee.Image.pixelArea().updateMask(flood).reduceRegion(ee.Reducer.sum(),region,scale=spec.resolution_m,bestEffort=True,maxPixels=1e10,tileScale=4).get('area')
        clear=valid.reduceRegion(ee.Reducer.mean(),region,scale=spec.resolution_m,bestEffort=True,maxPixels=1e10,tileScale=4).get('mndwi')
        ha=ee.Algorithms.If(ee.Algorithms.IsEqual(area,None),0,ee.Number(area).divide(10000))
        clear_pct=ee.Algorithms.If(ee.Algorithms.IsEqual(clear,None),0,ee.Number(clear).multiply(100))
        return ee.Feature(None,{'scene_index':raw_image.get('system:index'),'system:time_start':raw_image.get('system:time_start'),'flood_ha':ha,'clear_percent':clear_pct})
    raw=ee.FeatureCollection(event_images.map(measure)).getInfo().get('features',[])
    # ImageCollection.fromImages renames system:index to "0","1",…, so match scenes by acquisition time.
    scene_by_time={round(datetime.fromisoformat(s['time_utc']).timestamp()*1000):s for s in current}
    rows=[]
    for f in raw:
        p=f['properties']; ts=p.get('system:time_start')
        scene=scene_by_time.get(round(ts)) if ts is not None else None
        if not scene: continue
        clear=float(p.get('clear_percent',0))
        rows.append({'id':scene['id'],'date':scene['date'],'time_utc':scene['time_utc'],'sensor':sensor_name,'flood_ha':float(p.get('flood_ha',0)),'cloud_percent':scene['cloud_percent'],'clear_percent':clear,'orbit':None,'relative_orbit':None,'resolution_m':spec.resolution_m})
    peak=choose_peak([r for r in rows if r['clear_percent']>=30],minimum_ha=1)
    before_scenes=sorted(baseline_candidates,key=lambda s:(s['cloud_percent'],abs((scan_start-s['date']).days)))
    before=before_scenes[0] if before_scenes else None
    return {'rows':rows,'peak':peak,'before':before,'candidate_scenes':current,'baseline_scenes':baseline_candidates,'requested_date':approximate_date,
            'message':None if peak else 'No significant flood signal detected in this window for this area.'}

def optical_event_impact(baseline_ids: list[str], after_id: str, spec: SensorSpec, geometry: dict,
                         project=None, mndwi_threshold: float = 0, scale: int | None = None) -> dict:
    """Water change, cropland exposure, NDVI change and live tiles for a real optical (Landsat) pair.

    Same rule as ``optical_flood_time_series``: MNDWI > threshold after and ≤
    threshold in the pre-event composite, minus JRC permanent water and slopes
    ≥ 10°. Cropland is ESA WorldCover 2021 class 40 – a later map, so for older
    events it only approximates where farmland was. NDVI change is reported for
    flooded vs unflooded cropland (difference-in-differences).
    """
    from src.event_analysis import crop_damage_risk
    if not baseline_ids or not after_id: raise NoImageryError("A real optical before/after pair is required.")
    if not spec.swir1 or not spec.nir: raise NoImageryError(f"{spec.name} lacks SWIR/NIR bands for water and NDVI analysis.")
    ee=initialize(project); region=ee.Geometry(geometry); scale=scale or spec.resolution_m
    before=prepared_optical_image(baseline_ids,spec,geometry,project)
    after=prepared_optical_image([after_id],spec,geometry,project)
    mndwi_b=before.normalizedDifference([spec.green,spec.swir1]); mndwi_a=after.normalizedDifference([spec.green,spec.swir1])
    ndvi_b=before.normalizedDifference([spec.nir,spec.red]); ndvi_a=after.normalizedDifference([spec.nir,spec.red])
    valid=mndwi_b.mask().And(mndwi_a.mask()).And(terrain_mask(ee,10))
    flood=mndwi_a.gt(mndwi_threshold).And(mndwi_b.lte(mndwi_threshold)).And(permanent_water_mask(ee).Not()).updateMask(valid)
    crop=ee.ImageCollection('ESA/WorldCover/v200').first().select('Map').eq(40)
    delta=ndvi_a.subtract(ndvi_b)
    pa=ee.Image.pixelArea()
    f=flood.unmask(0).gt(0)
    sums=ee.Image.cat([pa.updateMask(f).rename('flood'),pa.updateMask(f.And(crop)).rename('crop_flood'),
                       pa.updateMask(crop).rename('cropland'),pa.updateMask(valid).rename('valid'),pa.rename('area'),
                       delta.multiply(pa).updateMask(f.And(crop)).rename('d_f'),pa.updateMask(f.And(crop)).updateMask(delta.mask()).rename('v_f'),
                       delta.multiply(pa).updateMask(f.Not().And(crop)).rename('d_u'),pa.updateMask(f.Not().And(crop)).updateMask(delta.mask()).rename('v_u')]
                      ).reduceRegion(ee.Reducer.sum(),region,scale,bestEffort=True,maxPixels=1e10,tileScale=8).getInfo()
    g=lambda k:float(sums.get(k) or 0)
    change_f=g('d_f')/g('v_f') if g('v_f') else None; change_u=g('d_u')/g('v_u') if g('v_u') else None
    relative=change_f-change_u if change_f is not None and change_u is not None else None
    valid_fraction=g('v_f')/g('crop_flood') if g('crop_flood') else None
    bands=visualization_bands(spec); vis={'bands':bands,'min':0,'max':.3,'gamma':1.3}
    return {"flood_ha":g('flood')/1e4,"agricultural_flood_ha":g('crop_flood')/1e4,"total_cropland_ha":g('cropland')/1e4,
            "clear_percent":100*g('valid')/g('area') if g('area') else None,
            "crop_rows":[{"crop":"Cropland (type not mapped for this year)","affected_ha":g('crop_flood')/1e4,"crop_area_ha":g('cropland')/1e4,
                          "ndvi_change_flooded":change_f,"ndvi_change_unflooded":change_u,"relative_ndvi_change":relative,
                          "ndvi_valid_fraction":valid_fraction,"risk":crop_damage_risk(relative,valid_fraction,g('crop_flood')/1e4)}],
            "layers":{"before":ee_tile_url(before.clip(region),vis),"after":ee_tile_url(after.clip(region),vis),
                      "flood":ee_tile_url(f.selfMask().clip(region),{'min':1,'max':1,'palette':['2f7de1']}),
                      "flooded_cropland":ee_tile_url(f.And(crop).selfMask().clip(region),{'min':1,'max':1,'palette':['f26b1d']}),
                      "cropland":ee_tile_url(crop.selfMask().clip(region),{'min':1,'max':1,'palette':['f2c12e']})},
            "rule":f"MNDWI({spec.green},{spec.swir1}) > {mndwi_threshold} after and ≤ {mndwi_threshold} before; JRC permanent water and slopes ≥ 10° excluded",
            "source":f"{spec.name} ({spec.collection}) via Google Earth Engine","cropland_source":"ESA WorldCover 2021 class 40 (later map; approximate for this year)"}

def sentinel1_candidates(geometry, start, end, project=None):
    ee=initialize(project)
    collection=(ee.ImageCollection('COPERNICUS/S1_GRD').filterBounds(ee.Geometry(geometry)).filterDate(str(start),str(end)).filter(ee.Filter.eq('instrumentMode','IW')).filter(ee.Filter.listContains('transmitterReceiverPolarisation','VV')))
    region=ee.Geometry(geometry)
    def metadata(image):
        image=ee.Image(image)
        coverage=image.geometry().intersection(region,maxError=1).area(maxError=1).divide(region.area(maxError=1)).multiply(100)
        props=image.toDictionary(['system:index','system:time_start','orbitProperties_pass','relativeOrbitNumber_start','transmitterReceiverPolarisation','platform_number']).set('coverage',coverage).set('scene_id',ee.String('COPERNICUS/S1_GRD/').cat(image.get('system:index')))
        return ee.Feature(None,props)
    info=ee.FeatureCollection(collection.sort('system:time_start').map(metadata)).getInfo()
    if not info.get('features'): raise NoImageryError("No Sentinel-1 scenes in this date window. Widen the window or choose another date.")
    out=[]
    for f in info['features']:
        p=f['properties']; ts=p['system:time_start']; out.append({'id':p['scene_id'],'date':datetime.fromtimestamp(ts/1000,timezone.utc).date(),'time_utc':datetime.fromtimestamp(ts/1000,timezone.utc).isoformat(),'orbit':p.get('orbitProperties_pass'),'relative_orbit':p.get('relativeOrbitNumber_start'),'polarizations':p.get('transmitterReceiverPolarisation',[]),'coverage':float(p.get('coverage',0)),'platform':p.get('platform_number')})
    return out

def sentinel1_gallery_tile_layers(before_scene: dict, after_scene: dict, geometry: dict,
                                  project=None, change_db: float=-3,
                                  water_db: float=-15, **rule) -> dict:
    """Return live Earth Engine tile URLs for a same-track Sentinel-1 pair and flood mask.

    Flood pixels use the shared rule in :func:`s1_new_water`; extra keyword
    arguments (``vh_water_db``, ``max_hand_m`` …) are passed through to it.
    """
    if not before_scene or not after_scene or not before_scene.get('id') or not after_scene.get('id'):
        raise NoImageryError("A real Sentinel-1 before/after pair is required to build the gallery.")
    same_track=(before_scene.get('orbit')==after_scene.get('orbit') and
                before_scene.get('relative_orbit')==after_scene.get('relative_orbit'))
    if not same_track:
        raise ProcessingError("Sentinel-1 scenes must share orbit direction and relative orbit for a valid flood comparison.")
    if before_scene.get('date') and after_scene.get('date') and before_scene['date']>=after_scene['date']:
        raise ProcessingError("The selected Sentinel-1 acquisitions are not chronological.")

    ee=initialize(project); region=ee.Geometry(geometry)
    before_raw=s1_scene_image(before_scene,ee); after_raw=s1_scene_image(after_scene,ee)
    before=before_raw.select('VV').clip(region)
    after=after_raw.select('VV').clip(region)
    flood_image=s1_new_water(before_raw,after_raw,ee,change_db,water_db,**rule).selfMask().clip(region)

    return {
        'before':ee_tile_url(before,{'min':-25,'max':0,'palette':['05070b','f7f8fa']}),
        'after':ee_tile_url(after,{'min':-25,'max':0,'palette':['05070b','f7f8fa']}),
        'flood':ee_tile_url(flood_image,{'min':0,'max':1,'palette':['18d69e'],'opacity':0.82}),
        'change_db':change_db,'water_db':water_db,
        'source':'Copernicus Sentinel-1 GRD via Google Earth Engine',
    }

def sentinel1_pair_agricultural_impact(before_scene: dict, after_scene: dict,
                                       geometry: dict, project=None,
                                       change_db: float=-3,
                                       water_db: float=-15, scale: int=10,
                                       **rule) -> dict:
    """Measure real flood/cropland overlap and return Earth Engine land-cover tiles.

    Cropland is ESA WorldCover 2021 v200 class 40. It is a generic land-cover
    exposure estimate, not a crop-type map or a crop-damage assessment.
    """
    if not before_scene or not after_scene or not before_scene.get('id') or not after_scene.get('id'):
        raise NoImageryError("A real Sentinel-1 pair is required to calculate agricultural exposure.")
    if before_scene.get('orbit')!=after_scene.get('orbit') or before_scene.get('relative_orbit')!=after_scene.get('relative_orbit'):
        raise ProcessingError("Sentinel-1 scenes must share orbit direction and relative orbit for an agricultural comparison.")
    ee=initialize(project); region=ee.Geometry(geometry)
    flood=s1_new_water(s1_scene_image(before_scene,ee),s1_scene_image(after_scene,ee),ee,change_db,water_db,**rule).selfMask().clip(region)
    worldcover=ee.ImageCollection('ESA/WorldCover/v200').first().select('Map').clip(region)
    cropland=worldcover.eq(40).selfMask()
    exposed=flood.And(worldcover.eq(40)).selfMask()
    pixel_area=ee.Image.pixelArea()
    areas=ee.Image.cat([
        pixel_area.updateMask(flood).rename('flood_m2'),
        pixel_area.updateMask(cropland).rename('cropland_m2'),
        pixel_area.updateMask(exposed).rename('exposed_m2'),
    ]).reduceRegion(reducer=ee.Reducer.sum(),geometry=region,scale=scale,
                    bestEffort=True,maxPixels=1e10,tileScale=8).getInfo()

    tile=ee_tile_url

    flood_m2=areas.get('flood_m2'); cropland_m2=areas.get('cropland_m2'); exposed_m2=areas.get('exposed_m2')
    flood_ha=float(flood_m2 or 0)/10000
    total_crop_ha=float(cropland_m2 or 0)/10000
    exposed_crop_ha=float(exposed_m2 or 0)/10000
    return {
        'flood_ha':flood_ha,'total_cropland_ha':total_crop_ha,
        'exposed_cropland_ha':exposed_crop_ha,
        'exposed_cropland_percent':100*exposed_crop_ha/total_crop_ha if total_crop_ha else None,
        'cropland_source':'ESA WorldCover 2021 v200 · Map class 40 (Cropland)',
        'permanent_water_source':'JRC Global Surface Water v1.4 · occurrence ≥ 90%',
        'layers':{
            'cropland':tile(cropland,{'min':1,'max':1,'palette':['d946ef'],'opacity':0.72}),
            'exposed_cropland':tile(flood.And(worldcover.eq(40)).selfMask(),{'min':1,'max':1,'palette':['ffb000'],'opacity':0.92}),
        },
    }

def sentinel1_pair_report_thumbnails(before_scene: dict, after_scene: dict,
                                     geometry: dict, project=None,
                                     dimensions=768, change_db: float=-3,
                                     water_db: float=-15, **rule) -> dict[str,bytes]:
    """Download compact real Earth Engine PNGs for the report's selected pair."""
    if before_scene.get('orbit')!=after_scene.get('orbit') or before_scene.get('relative_orbit')!=after_scene.get('relative_orbit'):
        raise ProcessingError("Report images require a compatible Sentinel-1 orbit pair.")
    ee=initialize(project); region=ee.Geometry(geometry)
    before_raw=s1_scene_image(before_scene,ee); after_raw=s1_scene_image(after_scene,ee)
    before=before_raw.select('VV').clip(region)
    after=after_raw.select('VV').clip(region)
    flood=s1_new_water(before_raw,after_raw,ee,change_db,water_db,**rule).selfMask().clip(region)
    grayscale={'min':-25,'max':0,'palette':['05070b','f7f8fa']}
    flood_composite=after.visualize(**grayscale).blend(flood.visualize(min=0,max=1,palette=['18d69e'],opacity=.9))
    outputs={}
    for name,image,visual in [('Before',before,grayscale),('After',after,grayscale),('Flood detection',flood_composite,None)]:
        params={'region':geometry,'dimensions':dimensions,'format':'png'}
        if visual: params.update(visual)
        url=image.getThumbURL(params)
        response=requests.get(url,timeout=60); response.raise_for_status()
        if not response.content.startswith(b'\x89PNG'):
            raise ProcessingError(f"Earth Engine returned an invalid {name.lower()} report preview.")
        outputs[name]=response.content
    return outputs

def sentinel2_true_color_layer(geometry: dict, start: date, end: date, project=None) -> dict | None:
    """Live natural-colour Sentinel-2 L2A tiles (Cloud Score+ masked median) for a date window.

    Returns None when Earth Engine has no Sentinel-2 scene over the area in the
    window. ``dates`` lists the real acquisition dates that were composited.
    """
    ee=initialize(project); region=ee.Geometry(geometry)
    s2=(ee.ImageCollection('COPERNICUS/S2_SR_HARMONIZED').filterBounds(region)
        .filterDate(str(start),str(end)).filter(ee.Filter.lt('CLOUDY_PIXEL_PERCENTAGE',80)))
    timestamps=s2.aggregate_array('system:time_start').getInfo()
    if not timestamps: return None
    linked=s2.linkCollection(ee.ImageCollection('GOOGLE/CLOUD_SCORE_PLUS/V1/S2_HARMONIZED'),['cs'])
    image=linked.map(lambda i:ee.Image(i).updateMask(ee.Image(i).select('cs').gte(0.6))).median().clip(region)
    url=ee_tile_url(image,{'bands':['B4','B3','B2'],'min':0,'max':3000,'gamma':1.2})
    dates=sorted({datetime.fromtimestamp(t/1000,timezone.utc).date().isoformat() for t in timestamps})
    return {"url":url,"dates":dates,"scenes":len(timestamps),
            "source":"Copernicus Sentinel-2 L2A (Cloud Score+ masked median) via Google Earth Engine"}

def sentinel2_collection(geometry, start, end, project=None):
    ee=initialize(project)
    return ee.ImageCollection('COPERNICUS/S2_SR_HARMONIZED').filterBounds(ee.Geometry(geometry)).filterDate(str(start),str(end))

def scene_thumbnail(scene_id: str, geometry: dict, bands=None, vis=None, dimensions=1024, project=None):
    """Request a real clipped EE thumbnail, retrying smaller on failure."""
    ee=initialize(project); image=ee.Image(scene_id)
    if bands==["VV","VH","VV-VH"]:
        image=image.addBands(image.select("VV").subtract(image.select("VH")).rename("VV-VH"))
        vis=vis or {"min":[-25,-30,-10],"max":[0,-5,10]}
    last=None
    for size in (dimensions,512,256):
        try:
            selected_bands=bands or ["VV"]
            params={"region":geometry,"dimensions":size,"format":"png","bands":selected_bands,"min":(vis or {}).get("min",-25),"max":(vis or {}).get("max",0)}
            if len(selected_bands)==1: params["palette"]=(vis or {}).get("palette",["000000","ffffff"])
            return image.getThumbURL(params)
        except Exception as exc: last=exc
    raise ProcessingError(f"Earth Engine thumbnail unavailable after smaller-size retries: {last}") from last

def flood_time_series(geometry: dict, approximate_date: date, scan_days=30, change_db=-3, water_db=-15, project=None):
    """Compute a server-side Sentinel-1 event series against a same-orbit pre-event median."""
    ee=initialize(project)
    start=approximate_date-timedelta(days=scan_days); end=approximate_date+timedelta(days=scan_days+1)
    region=ee.Geometry(geometry)
    all_s1=(ee.ImageCollection('COPERNICUS/S1_GRD').filterBounds(region).filterDate(str(start-timedelta(days=90)),str(end)).filter(ee.Filter.eq('instrumentMode','IW')).filter(ee.Filter.listContains('transmitterReceiverPolarisation','VV')))
    features=all_s1.filterDate(str(start),str(end)).aggregate_array('orbitProperties_pass').distinct().getInfo()
    if not features: raise NoImageryError("No Sentinel-1 acquisitions in the scan window. Widen the window or change AOI.")
    # Process each pass and relative orbit independently, then merge real observed measurements.
    rows=[]
    for pass_name in features:
        relative=all_s1.filter(ee.Filter.eq('orbitProperties_pass',pass_name)).filterDate(str(start),str(end)).aggregate_array('relativeOrbitNumber_start').distinct().getInfo()
        for rel in relative:
            compatible=all_s1.filter(ee.Filter.eq('orbitProperties_pass',pass_name)).filter(ee.Filter.eq('relativeOrbitNumber_start',rel))
            event=compatible.filterDate(str(start),str(end)).sort('system:time_start')
            before=s1_vv_median(compatible.filterDate(str(start-timedelta(days=90)),str(start)))
            if before is None: continue
            def measure(image):
                image=ee.Image(image)
                flooded=s1_new_water(before,image,ee,change_db,water_db).selfMask()
                area=ee.Image.pixelArea().updateMask(flooded).reduceRegion(reducer=ee.Reducer.sum(),geometry=region,scale=30,maxPixels=1e10,bestEffort=True,tileScale=4).get('area')
                hectares=ee.Algorithms.If(ee.Algorithms.IsEqual(area,None),0,ee.Number(area).divide(10000))
                return image.set('flood_ha',hectares)
            # Retrieve only compact properties after reductions.
            raw=event.map(measure).aggregate_array('system:time_start').getInfo()
            vals=event.map(measure).aggregate_array('flood_ha').getInfo()
            for ts,ha in zip(raw,vals):
                if ha is not None: rows.append({'date':datetime.fromtimestamp(ts/1000,timezone.utc).date(),'time_utc':datetime.fromtimestamp(ts/1000,timezone.utc).isoformat(),'flood_ha':float(ha),'orbit':pass_name,'relative_orbit':rel,'sensor':'Sentinel-1 SAR','resolution_m':10})
    rows.sort(key=lambda r:r['date'])
    if not rows: raise NoImageryError("No compatible same-orbit baseline and event acquisitions were available for this window.")
    peak=choose_peak(rows,minimum_ha=1.0)
    if peak is None:
        return {'rows':rows,'peak':None,'before':None,'requested_date':approximate_date,'message':'No significant flood signal detected in this window for this area.'}
    baseline=[r for r in rows if r['date']<peak['date']]
    before=baseline[-1] if baseline else None
    return {'rows':rows,'peak':peak,'before':before,'requested_date':approximate_date,'message':None}

def _sentinel1_interval_rows(geometry: dict, start: date, end: date,
                             change_db: float, water_db: float,
                             baseline_days: int, project=None) -> list[dict]:
    """Measure one bounded event interval against a prior same-orbit baseline."""
    ee=initialize(project); region=ee.Geometry(geometry)
    all_s1=(ee.ImageCollection('COPERNICUS/S1_GRD').filterBounds(region)
        .filterDate(str(start-timedelta(days=baseline_days)),str(end))
        .filter(ee.Filter.eq('instrumentMode','IW'))
        .filter(ee.Filter.listContains('transmitterReceiverPolarisation','VV')))
    event_all=all_s1.filterDate(str(start),str(end))
    passes=event_all.aggregate_array('orbitProperties_pass').distinct().getInfo()
    if not passes: return []
    rows=[]
    for pass_name in passes:
        pass_events=event_all.filter(ee.Filter.eq('orbitProperties_pass',pass_name))
        relatives=pass_events.aggregate_array('relativeOrbitNumber_start').distinct().getInfo()
        for relative_orbit in relatives:
            compatible=all_s1.filter(ee.Filter.eq('orbitProperties_pass',pass_name)).filter(ee.Filter.eq('relativeOrbitNumber_start',relative_orbit))
            baseline=s1_vv_median(compatible.filterDate(str(start-timedelta(days=baseline_days)),str(start)))
            event=compatible.filterDate(str(start),str(end)).sort('system:time_start')
            def measure(raw_image):
                image=ee.Image(raw_image)
                flood=s1_new_water(baseline,image,ee,change_db,water_db).selfMask()
                area=ee.Image.pixelArea().updateMask(flood).reduceRegion(
                    reducer=ee.Reducer.sum(),geometry=region,scale=10,
                    bestEffort=True,maxPixels=1e10,tileScale=4).get('area')
                hectares=ee.Algorithms.If(ee.Algorithms.IsEqual(area,None),0,ee.Number(area).divide(10000))
                return image.set('flood_ha',hectares)
            measured=event.map(measure)
            timestamps=measured.aggregate_array('system:time_start').getInfo()
            areas=measured.aggregate_array('flood_ha').getInfo()
            for timestamp,hectares in zip(timestamps,areas):
                if timestamp is None: continue
                acquired=datetime.fromtimestamp(timestamp/1000,timezone.utc)
                rows.append({'date':acquired.date(),'time_utc':acquired.isoformat(),
                    'flood_ha':float(hectares or 0),'orbit':pass_name,
                    'relative_orbit':int(relative_orbit),'sensor':'Sentinel-1 SAR',
                    'resolution_m':10})
    return rows

def sentinel1_flood_search_year(geometry: dict, year: int, change_db=-3,
                               water_db=-15, baseline_days=90, project=None) -> dict:
    """Search a calendar year in quarterly windows using actual Sentinel-1 scenes.

    Each quarter uses a pre-event median from the same orbit track. Splitting the
    year keeps Earth Engine aggregation batches small and helps avoid one season's
    flood observations contaminating the next season's baseline.
    """
    year=int(year)
    if year<2015:
        raise NoImageryError("Full-year Sentinel-1 flood searches start in 2015 so each acquisition can use a pre-event baseline. The Sentinel-1 archive starts in October 2014.")
    if year>date.today().year:
        raise NoImageryError("Cannot search a future year.")
    boundaries=[date(year,1,1),date(year,4,1),date(year,7,1),date(year,10,1),date(year+1,1,1)]
    rows=[]
    for start,end in zip(boundaries,boundaries[1:]):
        rows.extend(_sentinel1_interval_rows(geometry,start,end,change_db,water_db,baseline_days,project))
    rows.sort(key=lambda row:(row['date'],row['orbit'],row['relative_orbit']))
    peak=choose_peak(rows,minimum_ha=1.0)
    if not rows:
        message=f"No Sentinel-1 acquisitions were returned for {year} in this area."
    elif peak is None:
        message=f"No flood detected in {year} for this area by the Sentinel-1 change thresholds (minimum detected area: 1 ha)."
    else:
        message=None
    return {'year':year,'rows':rows,'peak':peak,'requested_date':None,'message':message,
            'change_db':change_db,'water_db':water_db,'baseline_days':baseline_days}
