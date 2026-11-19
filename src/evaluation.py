"""Accuracy assessment of the Sentinel-1 flood rule against Sentinel-2 reference water masks.

No ground-truth flood masks exist for these districts in the project, so the
reference is an *independent satellite* product: cloud-free Sentinel-2 MNDWI
water (> 0) on the radar date (−3/+6 days) that was not water in the pre-event
Sentinel-2 median, excluding JRC permanent water. Scores are computed only on
pixels that are cloud-free in Sentinel-2 and valid for the radar rule. Optical
and radar dates differ by up to a few days and turbid shallow water or flooded
vegetation can be missed by either sensor, so these are indicative metrics,
not validation against field data.
"""
from __future__ import annotations

from datetime import date, timedelta

def binary_metrics(tp: float, fp: float, fn: float) -> dict:
    """Precision, recall, F1 and IoU from areas (any consistent unit)."""
    precision=tp/(tp+fp) if tp+fp else None
    recall=tp/(tp+fn) if tp+fn else None
    f1=2*precision*recall/(precision+recall) if precision and recall else (0.0 if precision==0 or recall==0 else None)
    iou=tp/(tp+fp+fn) if tp+fp+fn else None
    return {"precision":precision,"recall":recall,"f1":f1,"iou":iou}

def evaluate_against_sentinel2(district: str, day: str, relative_orbit: int, orbit: str, cfg, project=None,
                               scale: int = 20) -> dict:
    from src.ee_auth import initialize
    from src.flood_monitoring import kpk_districts, rule_params
    from src.satellite_retrieval import S1_COLLECTION, s1_new_water, s1_mosaic, s1_vv_median, permanent_water_mask
    ee=initialize(project)
    region=kpk_districts(ee).filter(ee.Filter.eq('GAUL2_NAME',district)).geometry()
    d0=ee.Date(day)
    track=(ee.ImageCollection(S1_COLLECTION).filterBounds(region).filter(ee.Filter.eq('instrumentMode','IW'))
           .filter(ee.Filter.listContains('transmitterReceiverPolarisation','VV'))
           .filter(ee.Filter.eq('relativeOrbitNumber_start',int(relative_orbit))).filter(ee.Filter.eq('orbitProperties_pass',orbit)))
    sar=s1_new_water(s1_vv_median(track.filterDate(d0.advance(-60,'day'),d0)),s1_mosaic(track.filterDate(d0,d0.advance(1,'day')),ee),
                     ee,**rule_params(cfg,event_scale=True))
    cs=ee.ImageCollection('GOOGLE/CLOUD_SCORE_PLUS/V1/S2_HARMONIZED')
    def mndwi(start,end):
        col=(ee.ImageCollection('COPERNICUS/S2_SR_HARMONIZED').filterBounds(region).filterDate(start,end)
             .linkCollection(cs,['cs']))
        return col.map(lambda i:ee.Image(i).normalizedDifference(['B3','B11']).updateMask(ee.Image(i).select('cs').gte(0.6))).median(),col.size()
    after,n_after=mndwi(d0.advance(-3,'day'),d0.advance(7,'day'))
    before,n_before=mndwi(d0.advance(-60,'day'),d0.advance(-5,'day'))
    reference=after.gt(0).And(before.lte(0)).And(permanent_water_mask(ee).Not())
    valid=sar.mask().And(after.mask()).And(before.mask())
    predicted=sar.unmask(0).gt(0)
    pa=ee.Image.pixelArea()
    sums=ee.Image.cat([pa.updateMask(valid.And(predicted).And(reference)).rename('tp'),
                       pa.updateMask(valid.And(predicted).And(reference.Not())).rename('fp'),
                       pa.updateMask(valid.And(predicted.Not()).And(reference)).rename('fn'),
                       pa.updateMask(valid).rename('evaluated')]).reduceRegion(ee.Reducer.sum(),region,scale,maxPixels=1e10,bestEffort=True,tileScale=8)
    info=ee.Dictionary(sums).set('s2_after',n_after).set('s2_before',n_before).getInfo()
    ha={k:float(info.get(k) or 0)/1e4 for k in ("tp","fp","fn","evaluated")}
    return {"district":district,"date":day,"track":f"{orbit} {relative_orbit}",**{f"{k}_ha":round(v,1) for k,v in ha.items()},
            "s2_scenes_after":int(info.get("s2_after") or 0),"s2_scenes_before":int(info.get("s2_before") or 0),
            **binary_metrics(ha["tp"],ha["fp"],ha["fn"])}
