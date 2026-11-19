"""Cropland, crop-type exposure, Sentinel-2 NDVI change and damage-risk analysis.

Crop types come from ESA WorldCereal 2021 (10 m). It maps where maize, winter
cereals (wheat/barley), spring cereals and temporary crops were grown in the
2021 seasons; it is not a map of the current season, and it does not separate
rice, sugarcane, cotton or vegetables (they fall under "other temporary crops").
"""
from datetime import date, timedelta
import numpy as np

from src.event_analysis import crop_damage_risk

def ndvi(nir, red):
    nir=np.asarray(nir,dtype=float); red=np.asarray(red,dtype=float); den=nir+red
    return np.divide(nir-red,den,out=np.full_like(den,np.nan),where=den!=0)

def worldcover_cropland(classes): return np.asarray(classes)==40

WORLDCEREAL='ESA/WorldCereal/2021/MODELS/v100'
WORLDCOVER='ESA/WorldCover/v200'
CROP_SOURCE='ESA WorldCereal 2021 v100 (crop types) + ESA WorldCover 2021 v200 class 40 (cropland)'
RISK_CODES={1:"Low",2:"Medium",3:"High",4:"Insufficient Data"}
RISK_COLORS={"High":"d7301f","Medium":"fc8d59","Low":"fdd49e","Insufficient Data":"9e9e9e"}

def is_kharif(day: date) -> bool:
    """Kharif (summer, monsoon) season in KPK ≈ May–October; rabi ≈ November–April."""
    return 5<=day.month<=10

def crop_classes(day: date) -> list[dict]:
    """Mutually exclusive in-season crop classes for an event date (no double counting)."""
    if is_kharif(day):
        return [{"code":1,"crop":"Maize","products":["maize"],"exclude":[]},
                {"code":2,"crop":"Other temporary crops (rice, sugarcane, cotton, vegetables…)","products":["temporarycrops"],"exclude":["maize"]},
                {"code":3,"crop":"Other cropland (type not mapped)","products":["cropland"],"exclude":["temporarycrops","maize"]}]
    return [{"code":1,"crop":"Wheat / winter cereals","products":["wintercereals"],"exclude":[]},
            {"code":2,"crop":"Spring cereals","products":["springcereals"],"exclude":["wintercereals"]},
            {"code":3,"crop":"Other temporary crops","products":["temporarycrops"],"exclude":["wintercereals","springcereals"]},
            {"code":4,"crop":"Other cropland (type not mapped)","products":["cropland"],"exclude":["temporarycrops","wintercereals","springcereals"]}]

def _product_mask(ee, name: str, region):
    if name=="cropland":
        return ee.ImageCollection(WORLDCOVER).first().select('Map').eq(40)
    col=ee.ImageCollection(WORLDCEREAL).filter(ee.Filter.eq('product',name)).filterBounds(region)
    # classification is 100 for the product class and 0 otherwise; missing tiles → 0
    return col.select('classification').max().eq(100).unmask(0)

def crop_class_image(ee, day: date, region):
    """Integer image of in-season crop class codes (0 = not cropland)."""
    cropland=_product_mask(ee,"cropland",region)
    classes=crop_classes(day)
    masks={}
    for c in classes:
        for name in c["products"]+c["exclude"]:
            if name not in masks: masks[name]=_product_mask(ee,name,region)
    result=ee.Image(0)
    for c in reversed(classes):  # earlier classes take priority
        member=masks[c["products"][0]].And(cropland)
        for name in c["exclude"]: member=member.And(masks[name].Not())
        result=result.where(member,c["code"])
    return result.rename('crop_class')

def sentinel2_ndvi(ee, region, start: date, end: date):
    """Cloud Score+ masked Sentinel-2 L2A median NDVI and the number of scenes used."""
    s2=(ee.ImageCollection('COPERNICUS/S2_SR_HARMONIZED').filterBounds(region)
        .filterDate(str(start),str(end)))
    cs=ee.ImageCollection('GOOGLE/CLOUD_SCORE_PLUS/V1/S2_HARMONIZED')
    linked=s2.linkCollection(cs,['cs'])
    def clear(image):
        image=ee.Image(image)
        return image.updateMask(image.select('cs').gte(0.6)).normalizedDifference(['B8','B4']).rename('ndvi')
    return linked.map(clear).median(), s2.size()

def ndvi_windows(before_day: date, after_day: date, today: date | None = None, days: int = 30):
    """Pre-flood window ends at the before acquisition; post window starts at the after one."""
    today=today or date.today()
    pre=(before_day-timedelta(days=days),before_day+timedelta(days=1))
    post_end=min(after_day+timedelta(days=days+1),today+timedelta(days=1))
    post=(after_day,post_end) if post_end>after_day else None
    return pre,post

CROP_PALETTE=['f2c12e','2e9e5b','8fd6a5','b7a3e6']

def _ndvi_delta(ee, region, before_day: date, after_day: date):
    pre_window,post_window=ndvi_windows(before_day,after_day)
    pre_ndvi,pre_count=sentinel2_ndvi(ee,region,*pre_window)
    if post_window:
        post_ndvi,post_count=sentinel2_ndvi(ee,region,*post_window)
        return post_ndvi.subtract(pre_ndvi).rename('delta'),pre_count,post_count,pre_window,post_window
    return ee.Image(0).selfMask().rename('delta'),pre_count,ee.Number(0),pre_window,None

def _risk_image(ee, crop, flood, delta, classes, unflooded_means):
    """1 Low, 2 Medium, 3 High, 4 Insufficient NDVI — on flooded cropland only."""
    codes=[c["code"] for c in classes]
    relative=delta.subtract(crop.remap(codes,[float(unflooded_means.get(k) or 0) for k in codes],0))
    flooded_crop=flood.And(crop.gt(0))
    risk=(ee.Image(4).where(relative.gt(-0.07),1).where(relative.lte(-0.07),2)
          .where(relative.lte(-0.15),3))
    return risk.updateMask(flooded_crop).selfMask().rename('risk')

def crop_risk_layers(flood_image, geometry: dict, before_day: date, after_day: date,
                     unflooded_means: dict, project=None) -> dict:
    """Fresh live tile URLs for crop types, flooded cropland and the risk map."""
    from src.ee_auth import initialize
    from src.satellite_retrieval import ee_tile_url
    ee=initialize(project); region=ee.Geometry(geometry)
    classes=crop_classes(after_day)
    crop=crop_class_image(ee,after_day,region).clip(region)
    flood=ee.Image(flood_image).unmask(0).gt(0)
    delta=_ndvi_delta(ee,region,before_day,after_day)[0]
    means={int(k):v for k,v in unflooded_means.items()}
    risk=_risk_image(ee,crop,flood,delta,classes,means)
    palette=[RISK_COLORS[RISK_CODES[i]] for i in (1,2,3,4)]
    return {"risk":ee_tile_url(risk.clip(region),{'min':1,'max':4,'palette':palette}),
            "crop_types":ee_tile_url(crop.updateMask(crop.gt(0)).clip(region),{'min':1,'max':len(classes),'palette':CROP_PALETTE[:len(classes)]}),
            "flooded_cropland":ee_tile_url(flood.And(crop.gt(0)).selfMask().clip(region),{'min':1,'max':1,'palette':['f26b1d']})}

def risk_by_zone(flood_image, geometry: dict, before_day: date, after_day: date,
                 unflooded_means: dict, zones: list[dict], scale: int = 30, project=None) -> list[dict]:
    """Flooded cropland and risk-class area (ha) per zone (e.g. tehsil), highest priority first."""
    from src.ee_auth import initialize
    if not zones: return []
    ee=initialize(project); region=ee.Geometry(geometry)
    classes=crop_classes(after_day)
    crop=crop_class_image(ee,after_day,region)
    flood=ee.Image(flood_image).unmask(0).gt(0)
    delta=_ndvi_delta(ee,region,before_day,after_day)[0]
    risk=_risk_image(ee,crop,flood,delta,classes,{int(k):v for k,v in unflooded_means.items()})
    pa=ee.Image.pixelArea()
    image=ee.Image.cat([pa.updateMask(flood.selfMask()).rename('flood'),
                        pa.updateMask(risk.gt(0)).rename('crop_flood'),
                        pa.updateMask(risk.eq(3)).rename('high'),pa.updateMask(risk.eq(2)).rename('medium')])
    fc=ee.FeatureCollection([ee.Feature(ee.Geometry(z['geometry']),{'name':z['name']}) for z in zones])
    out=image.reduceRegions(fc,ee.Reducer.sum(),scale=scale,tileScale=8).getInfo()
    rows=[{"area":f['properties']['name'],"flood_ha":float(f['properties'].get('flood') or 0)/1e4,
           "flooded_cropland_ha":float(f['properties'].get('crop_flood') or 0)/1e4,
           "high_risk_ha":float(f['properties'].get('high') or 0)/1e4,
           "medium_risk_ha":float(f['properties'].get('medium') or 0)/1e4} for f in out.get('features',[])]
    for r in rows:
        r["priority"]=("High" if r["high_risk_ha"]>=50 else "Medium" if r["high_risk_ha"]+r["medium_risk_ha"]>=50
                       or r["flooded_cropland_ha"]>=500 else "Low")
    order={"High":0,"Medium":1,"Low":2}
    return sorted(rows,key=lambda r:(order[r["priority"]],-r["high_risk_ha"],-r["flooded_cropland_ha"]))

def event_crop_assessment(flood_image, geometry: dict, before_day: date, after_day: date,
                          scale: int = 20, project=None) -> dict:
    """Crop-wise flood exposure, NDVI change and preliminary damage-risk classes.

    ``flood_image`` is the 0/1 new-water image from ``s1_new_water``. For each
    in-season crop class the function measures flooded area and the Sentinel-2
    NDVI change (post − pre) of flooded and unflooded fields. The relative
    change (flooded − unflooded) removes the normal seasonal trend (growth or
    harvest) before risk classes are assigned. Thresholds are heuristic; risk
    is an inspection prioritization aid, not a measured crop loss.
    """
    from src.ee_auth import initialize
    ee=initialize(project); region=ee.Geometry(geometry)
    classes=crop_classes(after_day)
    crop=crop_class_image(ee,after_day,region).clip(region)
    flood=ee.Image(flood_image).unmask(0).gt(0)
    delta,pre_count,post_count,pre_window,post_window=_ndvi_delta(ee,region,before_day,after_day)
    pa=ee.Image.pixelArea()
    bands=[]
    for c in classes:
        member=crop.eq(c["code"])
        for flag,name in ((flood,"f"),(flood.Not(),"u")):
            m=member.And(flag)
            bands+=[pa.updateMask(m).rename(f"a{c['code']}{name}"),
                    pa.updateMask(m).updateMask(delta.mask()).rename(f"v{c['code']}{name}"),
                    delta.multiply(pa).updateMask(m).rename(f"d{c['code']}{name}")]
    stats=ee.Dictionary(ee.Image.cat(bands).reduceRegion(ee.Reducer.sum(),region,scale=scale,
            bestEffort=True,maxPixels=1e10,tileScale=8)).set('pre_count',pre_count).set('post_count',post_count).getInfo()
    rows=[]; unflooded_means={}
    for c in classes:
        k=c["code"]
        def g(key): return float(stats.get(key) or 0)
        flooded_ha=g(f"a{k}f")/1e4; total_ha=(g(f"a{k}f")+g(f"a{k}u"))/1e4
        valid_f=g(f"v{k}f"); valid_u=g(f"v{k}u")
        change_f=g(f"d{k}f")/valid_f if valid_f else None
        change_u=g(f"d{k}u")/valid_u if valid_u else None
        unflooded_means[k]=change_u if change_u is not None else 0.0
        relative=(change_f-change_u) if change_f is not None and change_u is not None else None
        valid_fraction=valid_f/g(f"a{k}f") if g(f"a{k}f") else None
        rows.append({"crop":c["crop"],"code":k,"affected_ha":flooded_ha,"crop_area_ha":total_ha,
                     "affected_percent":100*flooded_ha/total_ha if total_ha else None,
                     "ndvi_change_flooded":change_f,"ndvi_change_unflooded":change_u,
                     "relative_ndvi_change":relative,"ndvi_valid_fraction":valid_fraction,
                     "risk":crop_damage_risk(relative,valid_fraction,flooded_ha)})
    risk=_risk_image(ee,crop,flood,delta,classes,unflooded_means)
    risk_stats=ee.Image.pixelArea().addBands(risk).reduceRegion(
        ee.Reducer.sum().group(groupField=1,groupName='risk'),region,scale=scale,
        bestEffort=True,maxPixels=1e10,tileScale=8).getInfo()
    risk_area={RISK_CODES[int(g['risk'])]:float(g['sum'])/1e4 for g in risk_stats.get('groups',[]) if int(g['risk']) in RISK_CODES}
    return {"rows":rows,"risk_area_ha":risk_area,
            "unflooded_ndvi_change":{str(k):v for k,v in unflooded_means.items()},
            "layers":crop_risk_layers(flood_image,geometry,before_day,after_day,unflooded_means,project),
            "crop_palette":dict(zip([c["crop"] for c in classes],CROP_PALETTE)),
            "season":"Kharif (May–Oct)" if is_kharif(after_day) else "Rabi (Nov–Apr)",
            "ndvi_pre_window":[str(d) for d in pre_window],
            "ndvi_post_window":[str(d) for d in post_window] if post_window else None,
            "s2_pre_scenes":int(stats.get('pre_count') or 0),"s2_post_scenes":int(stats.get('post_count') or 0),
            "source":CROP_SOURCE,"ndvi_source":"Copernicus Sentinel-2 L2A (COPERNICUS/S2_SR_HARMONIZED), Cloud Score+ cs ≥ 0.6"}
