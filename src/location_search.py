"""Nominatim search and strict polygon-based KPK validation."""
import json, time, hashlib, logging, difflib
import requests
from shapely.geometry import shape, mapping
from shapely.ops import transform
from pyproj import Geod
from src.utils import InvalidLocationError, MissingBoundaryError, OutsideKPKError

_last_request = 0.0
_cache: dict[str, list[dict]] = {}
KPK_NAMES = {"khyber pakhtunkhwa", "north-west frontier", "north west frontier province", "nwfp", "n w f p"}
log = logging.getLogger(__name__)

def _normal_name(value: str) -> str:
    return " ".join("".join(ch if ch.isalnum() else " " for ch in value.lower()).split())

def match_admin_name(query: str, names: list[str], threshold: float = .62) -> str | None:
    """Match exact/fuzzy GAUL administrative names without guessing weak matches."""
    q=_normal_name(query)
    compact=lambda text: "".join(_normal_name(text).split())
    for name in names:
        if _normal_name(name)==q or compact(name)==compact(query): return name
    if not names: return None
    scores=[difflib.SequenceMatcher(None,q,_normal_name(name)).ratio() for name in names]
    index=max(range(len(scores)),key=scores.__getitem__)
    return names[index] if scores[index]>=threshold else None

def nominatim_search(query: str, user_agent: str, session=requests) -> list[dict]:
    global _last_request
    key = query.strip().lower()
    if key in _cache: return _cache[key]
    time.sleep(max(0, 1 - (time.monotonic() - _last_request)))
    try:
        if not user_agent or "configure in .env" in user_agent:
            raise InvalidLocationError("Set a descriptive NOMINATIM_USER_AGENT in .env (include a contact address) before searching.")
        res = session.get("https://nominatim.openstreetmap.org/search", params={"q":query,"format":"jsonv2","polygon_geojson":1,"countrycodes":"pk","viewbox":"69.0,37.2,74.5,31.0","bounded":0,"limit":10}, headers={"User-Agent":user_agent}, timeout=25)
        _last_request=time.monotonic()
        if res.status_code == 429: raise InvalidLocationError("Nominatim rate limit reached; wait at least one minute and retry.")
        if res.status_code == 403: raise InvalidLocationError("Nominatim denied this request (HTTP 403). Check the descriptive User-Agent/contact in .env; the public service may block this network. GAUL district search can still provide administrative boundaries.")
        res.raise_for_status(); data=res.json()
    except InvalidLocationError: raise
    except requests.RequestException as exc:
        log.exception("Nominatim search failed for %r", query)
        raise InvalidLocationError(f"Location service/network error: {exc}. Check internet access and retry after a minute.") from exc
    if not data: log.info("Nominatim returned no matches for %r", query)
    _cache[key]=data
    return data

def validate_polygon(geometry: dict, kpk_geometry: dict, max_area_km2: float = 5000) -> float:
    if not geometry or geometry.get('type') == 'Point': raise MissingBoundaryError("No boundary polygon found. Draw an area or explicitly use an approximate buffer.")
    geom=shape(geometry); province=shape(kpk_geometry)
    if geom.is_empty or not geom.is_valid: raise InvalidLocationError("The selected boundary is invalid.")
    if not geom.intersects(province): raise OutsideKPKError("Selected area does not intersect KPK.")
    ratio=geom.intersection(province).area / geom.area if geom.area else 0
    if ratio < .8: raise OutsideKPKError(f"Only {ratio:.0%} of the selected geometry falls inside KPK; at least 80% must be inside.")
    area=abs(Geod(ellps="WGS84").geometry_area_perimeter(geom)[0])/1_000_000
    if area > max_area_km2: raise InvalidLocationError(f"AOI is {area:.0f} km²; limit is {max_area_km2:.0f} km². Narrow the area.")
    return area

def osm_result_geometry(result: dict) -> dict | None:
    """Return a polygon result, never turning a geocoder point into an AOI."""
    geo=result.get("geojson")
    if geo and geo.get("type") in {"Polygon","MultiPolygon"}: return geo
    return None

def approximate_buffer(point: dict, radius_km: float) -> dict:
    """Create a clearly approximate geodesic buffer around an OSM point."""
    if not 0.1 <= radius_km <= 50: raise InvalidLocationError("Approximate buffer radius must be 0.1–50 km.")
    from shapely.geometry import Point
    from pyproj import CRS, Transformer
    p=shape(point)
    local=CRS.from_proj4(f"+proj=aeqd +lat_0={p.y} +lon_0={p.x} +datum=WGS84 +units=m")
    forward=Transformer.from_crs("EPSG:4326",local,always_xy=True).transform
    backward=Transformer.from_crs(local,"EPSG:4326",always_xy=True).transform
    return mapping(transform(backward,transform(forward,p).buffer(radius_km*1000,resolution=48)))

def gaul_boundary(query: str, level: int = 2, project: str | None = None) -> dict | None:
    """Look up a GAUL administrative polygon; use fuzzy match only when decisive."""
    import ee
    from src.ee_auth import initialize
    initialize(project)
    level=1 if level==1 else 2
    year=2025
    fc=ee.FeatureCollection(f"FAO/GAUL/{year}/level{level}")
    props_name=("GAUL1_NAME" if level==1 else "GAUL2_NAME")
    country_prop="GAUL0_NAME"
    q=_normal_name(query)
    try: names=fc.aggregate_array(props_name).distinct().getInfo()
    except Exception:
        # GAUL 2015 is retained as a compatibility fallback for projects where
        # the newer collection has not propagated to the configured EE endpoint.
        year=2015
        fc=ee.FeatureCollection(f"FAO/GAUL/{year}/level{level}")
        props_name="ADM1_NAME" if level==1 else "ADM2_NAME"
        country_prop="ADM0_NAME"
        names=fc.aggregate_array(props_name).distinct().getInfo()
    # Limit the candidate set to Pakistan where the schema supports it.
    try:
        national=fc.filter(ee.Filter.eq(country_prop,"Pakistan"))
        national_names=national.aggregate_array(props_name).distinct().getInfo()
        if national_names: names=national_names
        else: national=fc
    except Exception: national=fc
    names=[str(n) for n in (names or [])]
    if not names: return None
    if level==1:
        aliases={_normal_name(n) for n in KPK_NAMES}
        name=next((n for n in names if _normal_name(n) in aliases),None)
        if name is None:
            name=max(names,key=lambda n:max(difflib.SequenceMatcher(None,_normal_name(n),a).ratio() for a in aliases))
    else:
        name=match_admin_name(query,names)
    if not name: return None
    feature=national.filter(ee.Filter.eq(props_name,name)).first().getInfo()
    if not feature: return None
    return {"geometry":feature["geometry"],"name":feature.get("properties",{}).get(props_name),"source":f"FAO GAUL {year} level {level}"}

def geometry_hash(geometry: dict) -> str:
    return hashlib.sha256(json.dumps(geometry,sort_keys=True).encode()).hexdigest()
