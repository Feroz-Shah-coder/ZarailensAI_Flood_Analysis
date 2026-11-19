"""Small-area local SAR utilities; EE processing remains server-side."""
import numpy as np

def mndwi(green, swir1):
    """Modified NDWI, with invalid zero-denominator pixels marked NaN."""
    green=np.asarray(green,dtype=float); swir1=np.asarray(swir1,dtype=float)
    denominator=green+swir1
    return np.divide(green-swir1,denominator,out=np.full_like(denominator,np.nan),where=denominator!=0)

def ndwi(green, nir):
    """McFeeters NDWI from green and near-infrared reflectance bands."""
    green=np.asarray(green,dtype=float); nir=np.asarray(nir,dtype=float); denominator=green+nir
    return np.divide(green-nir,denominator,out=np.full_like(denominator,np.nan),where=denominator!=0)

def speckle_median(image, radius=1):
    """3x3 neighborhood median without converting/averaging dB values."""
    from scipy.ndimage import median_filter
    if radius < 0 or radius > 10: raise ValueError("Speckle radius must be 0–10 pixels.")
    return median_filter(np.asarray(image), size=2*radius+1) if radius else np.asarray(image).copy()
