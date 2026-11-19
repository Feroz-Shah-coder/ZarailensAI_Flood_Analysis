"""Agricultural impact metrics and transparent inspection scoring."""
import numpy as np

def calculate_impact(flood, cropland, pixel_area_m2=100):
    f=np.asarray(flood,dtype=bool); c=np.asarray(cropland,dtype=bool)
    if f.shape != c.shape: raise ValueError("Flood and cropland grids must align.")
    total=float(f.sum()*pixel_area_m2/10000); ag=float((f&c).sum()*pixel_area_m2/10000); ag_total=float(c.sum()*pixel_area_m2/10000)
    return {"Detected flooded area":total,"Agricultural land exposed to flooding":ag,"total_agricultural_ha":ag_total,"agricultural_affected_percent":100*ag/ag_total if ag_total else 0}

def damage_indicator(ndvi_before, ndvi_after, flooded_cropland):
    mask=np.asarray(flooded_cropland,dtype=bool)
    if ndvi_before is None or ndvi_after is None or not mask.any(): return "indicator unavailable"
    drop=float(np.nanmean(np.asarray(ndvi_before)[mask]-np.asarray(ndvi_after)[mask]))
    return "severe drop" if drop>=0.3 else "moderate drop" if drop>=0.1 else "no significant change"

def inspection_priority_score(flooded_ag_ha, affected_percent, ndvi_drop=0, confidence=1):
    """Score = 0.4 normalized area + 0.3 affected share + 0.2 NDVI loss + 0.1 confidence (0–1)."""
    return .4*min(max(flooded_ag_ha,0)/1000,1)+.3*min(max(affected_percent,0)/100,1)+.2*min(max(ndvi_drop,0)/.5,1)+.1*min(max(confidence,0),1)

def inspection_priority(flooded_ag_ha, affected_percent, ndvi_drop=0, confidence=1):
    """Score = 0.4 normalized area + 0.3 affected share + 0.2 NDVI loss + 0.1 confidence."""
    score=inspection_priority_score(flooded_ag_ha,affected_percent,ndvi_drop,confidence)
    return "High" if score>=.65 else "Medium" if score>=.35 else "Low"
