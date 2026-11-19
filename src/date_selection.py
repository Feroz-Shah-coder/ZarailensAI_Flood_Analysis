"""Date validation and candidate ranking independent of Earth Engine."""
from datetime import date, timedelta
from src.utils import InvalidDatesError

EARLIEST_LANDSAT = date(1972, 7, 23)
SENTINEL1_START = date(2014, 10, 3)
SENTINEL2_START = date(2015, 6, 27)

def available_sensors(day: date, aoi_area_km2: float | None = None) -> dict[str, list[str]]:
    """Return real sensor families whose archive overlaps the requested day."""
    if day < EARLIEST_LANDSAT: return {"optical": [], "radar": []}
    optical=[]; radar=[]
    if date(1972,7,23)<=day<date(1984,1,1):
        optical=[]
        if day<date(1978,1,7): optical.append("Landsat 1 MSS")
        if date(1975,1,31)<=day<date(1982,2,4): optical.append("Landsat 2 MSS")
        if date(1978,6,3)<=day<date(1983,2,24): optical.append("Landsat 3 MSS")
        if date(1982,8,22)<=day<date(1993,6,25): optical.append("Landsat 4 TM")
    elif day < date(1999, 1, 1):
        optical=[]
        if day<date(1993,6,25): optical.append("Landsat 4 TM")
        if day>=date(1984,3,16): optical.append("Landsat 5 TM")
    elif day < date(2013, 1, 1):
        optical=["Landsat 5 TM", "Landsat 7 ETM+"]
        if aoi_area_km2 and aoi_area_km2>=5000 and day >= date(2000, 2, 24): optical.append("MODIS MOD09GA (coarse, last resort)")
    else:
        optical=["Landsat 8 OLI"] if day>=date(2013,3,18) else ["Landsat 7 ETM+"]
        if day >= date(2014, 10, 1):
            if day >= SENTINEL2_START: optical.append("Sentinel-2 MSI L1C")
            if day >= date(2017, 3, 28): optical.append("Sentinel-2 MSI L2A")
        if day >= date(2021, 10, 31): optical.append("Landsat 9 OLI-2")
        if day >= date(2014, 10, 3): radar=["Sentinel-1 SAR"]
    return {"optical": optical, "radar": radar}

def validate_dates(before: date, after: date, today: date | None = None) -> None:
    today = today or date.today()
    if before >= after: raise InvalidDatesError("Before date must be earlier than after date.")
    if after > today: raise InvalidDatesError("Dates cannot be in the future.")
    if before < date(1900, 1, 1): raise InvalidDatesError("Dates must be 1900-01-01 or later.")

def search_window(day: date, days: int = 10) -> tuple[date, date]:
    if not 1 <= days <= 30: raise InvalidDatesError("Search window must be 1–30 days.")
    return day - timedelta(days=days), day + timedelta(days=days + 1)

def rank_candidates(candidates: list[dict], requested: date) -> list[dict]:
    """Order candidates by date distance, coverage, then usable polarization."""
    return sorted(candidates, key=lambda c: (abs((c['date']-requested).days),
        -float(c.get('coverage', 0)), -int('VV' in c.get('polarizations', []))))

def compatible(a: dict, b: dict) -> bool:
    return a.get('orbit') == b.get('orbit') and a.get('relative_orbit') == b.get('relative_orbit')

def compatible_sar_candidates(before: list[dict], after: list[dict]) -> tuple[list[dict],list[dict]]:
    """Keep only Sentinel-1 scenes sharing orbit direction and relative track."""
    pairs={(item.get('orbit'),item.get('relative_orbit')) for item in before} & {(item.get('orbit'),item.get('relative_orbit')) for item in after}
    if not pairs: return [],[]
    return ([item for item in before if (item.get('orbit'),item.get('relative_orbit')) in pairs],
            [item for item in after if (item.get('orbit'),item.get('relative_orbit')) in pairs])

def best_compatible_sar_pair(before: list[dict], after: list[dict],
                             requested_before: date, requested_after: date,
                             minimum_coverage: float = 0) -> tuple[dict,dict] | None:
    """Choose closest chronological before/after scenes on the same Sentinel-1 track."""
    before,after=compatible_sar_candidates(before,after)
    before=[scene for scene in before if float(scene.get('coverage',0))>=minimum_coverage]
    after=[scene for scene in after if float(scene.get('coverage',0))>=minimum_coverage]
    pairs=[(b,a) for b in before for a in after if b['date']<a['date']]
    if not pairs: return None
    return min(pairs,key=lambda pair:(
        abs((pair[0]['date']-requested_before).days)+abs((pair[1]['date']-requested_after).days),
        -min(float(pair[0].get('coverage',0)),float(pair[1].get('coverage',0))),
        -float(pair[0].get('coverage',0))-float(pair[1].get('coverage',0)),
    ))
