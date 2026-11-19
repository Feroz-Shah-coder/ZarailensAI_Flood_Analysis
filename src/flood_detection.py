"""Local SAR threshold detector and optional trainable random forest."""
from abc import ABC, abstractmethod
from dataclasses import dataclass
import numpy as np

@dataclass
class FloodResult:
    mask: np.ndarray
    area_ha: float
    method: str = "Threshold-based SAR change detection"

class BaseFloodDetector(ABC):
    @abstractmethod
    def detect(self, before, after, aoi=None, params=None) -> FloodResult: ...

class ThresholdChangeDetector(BaseFloodDetector):
    def detect(self, before, after, aoi=None, params=None) -> FloodResult:
        p=params or {}; b=np.asarray(before,dtype=float); a=np.asarray(after,dtype=float)
        if b.shape != a.shape: raise ValueError("Before and after rasters must have the same shape.")
        valid=np.isfinite(b)&np.isfinite(a)
        if aoi is not None: valid &= np.asarray(aoi,dtype=bool)
        mask=valid & ((a-b) < float(p.get('change_db',-3))) & (a < float(p.get('water_db',-15)))
        if p.get('permanent_water') is not None: mask &= ~np.asarray(p['permanent_water'],dtype=bool)
        if p.get('slope') is not None: mask &= np.asarray(p['slope']) < float(p.get('max_slope',10))
        pixel=float(p.get('pixel_area_m2',100))
        return FloodResult(mask, float(mask.sum()*pixel/10000))

class OpticalWaterChangeDetector(BaseFloodDetector):
    """MNDWI-based optical flood change detector with explicit valid-pixel masking."""
    def detect(self, before, after, aoi=None, params=None) -> FloodResult:
        p=params or {}; b=np.asarray(before,dtype=float); a=np.asarray(after,dtype=float)
        if b.shape!=a.shape: raise ValueError("Before and after optical rasters must align.")
        valid=np.isfinite(b)&np.isfinite(a)
        if aoi is not None: valid &= np.asarray(aoi,dtype=bool)
        water_after=a>float(p.get('mndwi_threshold',0)); water_before=b>float(p.get('mndwi_threshold',0))
        mask=valid & water_after & ~water_before
        if p.get('permanent_water') is not None: mask &= ~np.asarray(p['permanent_water'],dtype=bool)
        if p.get('slope') is not None: mask &= np.asarray(p['slope'])<float(p.get('max_slope',10))
        pixel=float(p.get('pixel_area_m2',100))
        minimum=float(p.get('minimum_component_area_m2',0))
        if minimum>0 and mask.any():
            from scipy.ndimage import label
            labels,count=label(mask,structure=np.ones((3,3),dtype=bool))
            sizes=np.bincount(labels.ravel())
            keep=np.flatnonzero(sizes*pixel>=minimum); keep=keep[keep!=0]
            mask=np.isin(labels,keep)
        return FloodResult(mask,float(mask.sum()*pixel/10000),"Threshold-based change detection")

class RandomForestFloodDetector(BaseFloodDetector):
    """Optional detector; requires user-provided binary labels."""
    def __init__(self): self.model=None
    def train(self, features, labels):
        from sklearn.ensemble import RandomForestClassifier
        self.model=RandomForestClassifier(n_estimators=150,class_weight='balanced',random_state=42,n_jobs=-1).fit(np.asarray(features),np.asarray(labels))
        return self
    def detect(self, before, after, aoi=None, params=None):
        if self.model is None: raise ValueError("Train with user labels before prediction.")
        b=np.asarray(before); a=np.asarray(after); x=np.column_stack([b.ravel(),a.ravel(),(a-b).ravel()]); m=self.model.predict(x).reshape(a.shape).astype(bool)
        if aoi is not None: m &= np.asarray(aoi,dtype=bool)
        return FloodResult(m,float(m.sum()*(params or {}).get('pixel_area_m2',100)/10000),"User-trained Random Forest")
