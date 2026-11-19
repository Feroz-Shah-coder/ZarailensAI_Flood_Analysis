"""Pure helpers for choosing observed flood events and narrating computed data.

Nothing in this module touches Earth Engine. Functions receive measured rows and
return rankings/classifications, so they can be unit tested and audited.
"""
from __future__ import annotations

from datetime import date
from statistics import median

def choose_peak(rows: list[dict], minimum_ha: float = 1.0):
    """Choose the observed peak only when it exceeds a transparent noise floor."""
    if not rows: return None
    peak=max(rows,key=lambda row:float(row.get('flood_ha',0)))
    return peak if float(peak.get('flood_ha',0)) >= minimum_ha else None

def rule_based_narrative(stats: dict) -> str:
    """Describe only supplied computed values, marking unavailable fields explicitly."""
    name=str(stats.get('location','Selected area'))
    peak=stats.get('peak_date','unavailable')
    sensor=stats.get('sensor','sensor unavailable')
    flood=stats.get('flood_ha')
    pct=stats.get('aoi_percent')
    agriculture=stats.get('ag_ha')
    ag_pct=stats.get('ag_percent')
    confidence=stats.get('confidence','Low')
    reasons='; '.join(stats.get('caveats',[])) or 'No additional quality caveats were supplied.'
    if flood is None: return f"No significant flood signal detected for {name}. Satellite-derived estimates require ground validation."
    requested=stats.get('requested_date')
    date_note=(f" It is {stats.get('day_difference','unavailable')} day(s) from requested date {requested}." if requested else "")
    quality=(f" Scene cloud cover was {float(stats['cloud_percent']):.1f}%." if stats.get('cloud_percent') is not None else "")
    resolution=(f" Native resolution is {int(stats['resolution_m'])} m." if stats.get('resolution_m') else "")
    return (f"For {name}, {sensor} selected the flood peak acquisition on {peak}."+date_note+quality+resolution+f" Detected flooded area: {float(flood):,.2f} ha"
        +(f" ({float(pct):.2f}% of the AOI)" if pct is not None else "")+". "
        +(f"Agricultural land exposed to flooding: {float(agriculture):,.2f} ha" if agriculture is not None else "Agricultural exposure unavailable.")+". "
        +(f"Affected agricultural share: {float(ag_pct):.2f}%. " if ag_pct is not None else "")
        +f"Confidence: {confidence}. Data caveats: {reasons}. Satellite-derived estimates require ground validation; flooded agricultural land does not automatically mean destroyed crops.")


# ---------------------------------------------------------------------------
# Automatic KPK event discovery
# ---------------------------------------------------------------------------
# A screening row is one Sentinel-1 acquisition slice (date + orbit track)
# measured over one district:
#   {"date": date, "district": str, "orbit": str, "relative_orbit": int,
#    "flood_ha": float, "crop_ha": float, "valid_ha": float,
#    "baseline_images": int, "phase": "background" | "monitoring"}
# "flood_ha" is newly dark water relative to a same-track pre-acquisition median.

MIN_BACKGROUND_FRACTION = 0.0005   # 0.05 % of observable land: floor that avoids division by ~0
MIN_VALID_HA = 500                 # slices observing less land than this are too partial to rank

def _as_date(value) -> date:
    return value if isinstance(value, date) else date.fromisoformat(str(value)[:10])

def _fraction(row: dict) -> float:
    valid=float(row.get("valid_ha") or 0)
    return float(row.get("flood_ha") or 0)/valid if valid>0 else 0.0

def district_backgrounds(rows: list[dict]) -> dict[str, dict]:
    """Median newly-dark fraction per district: its normal non-flood SAR change level.

    Irrigation, paddy flooding, river-bed dynamics and soil moisture produce a
    recurring signal in some districts. Rows flagged ``phase == "background"``
    (the period before the monitoring window) are preferred; when a district has
    fewer than three of them, all its rows are used instead.
    """
    by_district: dict[str, list[dict]] = {}
    for row in rows:
        if float(row.get("valid_ha") or 0) < MIN_VALID_HA: continue
        by_district.setdefault(row["district"], []).append(row)
    result={}
    for district, items in by_district.items():
        background=[r for r in items if r.get("phase")=="background"]
        source=background if len(background)>=3 else items
        fractions=[_fraction(r) for r in source]
        result[district]={"fraction":median(fractions) if fractions else 0.0,
                          "observations":len(source),
                          "source":"pre-window acquisitions" if source is background else "all acquisitions in search"}
    return result

def score_slices(rows: list[dict], backgrounds: dict[str, dict]) -> list[dict]:
    """Attach background-relative excess area and anomaly ratio to each slice row."""
    scored=[]
    for row in rows:
        valid=float(row.get("valid_ha") or 0)
        if valid < MIN_VALID_HA: continue
        bg=backgrounds.get(row["district"],{}).get("fraction",0.0)
        frac=_fraction(row)
        item=dict(row)
        item["date"]=_as_date(row["date"])
        item["fraction"]=frac
        item["background_fraction"]=bg
        item["background_ha"]=bg*valid
        item["excess_ha"]=max(0.0,(frac-bg)*valid)
        item["ratio"]=frac/max(bg,MIN_BACKGROUND_FRACTION)
        scored.append(item)
    return scored

def group_events(scored: list[dict], min_excess_ha: float = 300, min_ratio: float = 2.0,
                 gap_days: int = 14, phase: str | None = "monitoring") -> list[dict]:
    """Merge anomalous acquisitions of the same district into flood events.

    Anomalous slices of one district separated by no more than ``gap_days`` are
    one event. The event's peak is the slice with the largest excess area.
    """
    anomalous=[r for r in scored if r["excess_ha"]>=min_excess_ha and r["ratio"]>=min_ratio
               and (phase is None or r.get("phase",phase)==phase)]
    events=[]
    by_district: dict[str, list[dict]] = {}
    for row in anomalous: by_district.setdefault(row["district"],[]).append(row)
    for district, items in by_district.items():
        items.sort(key=lambda r:r["date"])
        current=[items[0]]
        for row in items[1:]:
            if (row["date"]-current[-1]["date"]).days<=gap_days: current.append(row)
            else:
                events.append(_event_from(district,current)); current=[row]
        events.append(_event_from(district,current))
    return events

def _event_from(district: str, slices: list[dict]) -> dict:
    peak=max(slices,key=lambda r:r["excess_ha"])
    return {"district":district,
            "first_detection":min(r["date"] for r in slices),
            "last_detection":max(r["date"] for r in slices),
            "peak_date":peak["date"],"peak_orbit":peak.get("orbit"),
            "peak_relative_orbit":peak.get("relative_orbit"),
            "peak_flood_ha":float(peak["flood_ha"]),"peak_excess_ha":float(peak["excess_ha"]),
            "peak_crop_ha":float(peak.get("crop_ha") or 0),
            "peak_ratio":float(peak["ratio"]),"background_ha":float(peak["background_ha"]),
            "valid_ha":float(peak["valid_ha"]),"baseline_images":int(peak.get("baseline_images") or 0),
            "detections":len(slices),
            "tracks":sorted({f"{r.get('orbit')} {r.get('relative_orbit')}" for r in slices})}

def previous_clear_observation(scored: list[dict], event: dict, min_ratio: float = 2.0) -> dict | None:
    """Latest same-track slice before the event whose signal was at background level."""
    candidates=[r for r in scored if r["district"]==event["district"]
                and r.get("orbit")==event.get("peak_orbit")
                and r.get("relative_orbit")==event.get("peak_relative_orbit")
                and r["date"]<event["first_detection"] and r["ratio"]<min_ratio]
    return max(candidates,key=lambda r:r["date"]) if candidates else None

def add_spatial_context(events: list[dict], window_days: int = 6) -> list[dict]:
    """Count other districts with an event peak within ``window_days`` (regional extent)."""
    for event in events:
        others=[e["district"] for e in events if e is not event
                and abs((e["peak_date"]-event["peak_date"]).days)<=window_days]
        event["concurrent_districts"]=sorted(set(others))
    return events

def verification_verdict(verification: dict | None, min_clear_pct: float = 30,
                         confirm_pct: float = 30, refute_pct: float = 10,
                         min_large_patch_pct: float = 35, max_historical_water_pct: float = 60) -> dict:
    """Interpret independent checks of a radar 'new water' signal.

    * Optical: of the radar water pixels with cloud-free Sentinel-2 within a few
      days, the share that is also water by MNDWI (> 0). ≥ confirm_pct confirms,
      < refute_pct refutes (smooth/wet bare soil and freshly sown fields are dark
      in radar but dry in optical imagery).
    * Structure: share of radar water in connected patches ≥ 10 ha. Floods form
      sheets; field-by-field wetting forms many small strips.
    * Seasonal water: share of radar water where JRC Global Surface Water has
      recorded water in ≥ 10 % of observations since 1984. Reservoirs refilling
      (e.g. Tarbela) and normal river rises are real water but not floods;
      ≥ max_historical_water_pct rejects the event.
    Calibrated on D.I. Khan 25/29 Aug 2022 (real flood: 84 % optical water, 53 %
    large patches, 4–24 % historical water), Lakki Marwat 10 Oct 2026 (wet/ploughed
    fields: 1 % optical water, 22 % large patches) and Haripur Aug 2026 (Tarbela
    reservoir refilling: 95 % historical water).
    """
    if not verification:
        return {"optical":"not checked","structure":"not checked","refuted":False,"confirmed":False,"compact":None,"rejection":None}
    historical=verification.get("historical_water_pct")
    if historical is not None and historical>=max_historical_water_pct:
        return {"optical":"not used","structure":"normally-wet river/reservoir area","refuted":True,"confirmed":False,
                "compact":None,"rejection":f"{historical:.0f}% of the water lies where rivers/reservoirs normally hold water (seasonal filling, not a flood)"}
    clear=verification.get("optical_clear_pct"); water=verification.get("optical_water_pct")
    large=verification.get("large_patch_pct")
    if clear is None or clear<min_clear_pct or water is None: optical="unavailable (cloud or no Sentinel-2 pass)"
    elif water>=confirm_pct: optical="confirmed"
    elif water<refute_pct: optical="refuted"
    else: optical="inconclusive"
    compact=None if large is None else large>=min_large_patch_pct
    structure=("not checked" if large is None else "flood-like (large connected water bodies)" if compact
               else "fragmented, field-scale pattern")
    return {"optical":optical,"structure":structure,"refuted":optical=="refuted",
            "confirmed":optical=="confirmed","compact":compact,
            "rejection":"cloud-free Sentinel-2 shows dry land where radar shows water (wet or freshly ploughed soil)" if optical=="refuted" else None}

def classify_event(event: dict, rainfall_mm: float | None = None,
                   confirmed_excess_ha: float = 1000, min_excess_ha: float = 300,
                   verification: dict | None = None) -> dict:
    """Score an event 0–100 and label it detected or possible.

    Components (transparent heuristic, not a trained model):
      * water change   40 pts – background-relative excess area (5,000 ha = full)
      * anomaly ratio  20 pts – peak fraction / district background (5x = full)
      * spatial extent 10 pts – repeat detections and concurrent districts
      * data quality   15 pts – same-track baseline depth and observed land area
      * rainfall       15 pts – supporting precipitation before the peak
    A *detected* event needs excess ≥ ``confirmed_excess_ha``, ratio ≥ 3, and
    at least one corroboration: rainfall ≥ 20 mm, a repeat detection, or a
    concurrent district. Everything else above ``min_excess_ha`` is *possible*.
    """
    excess=float(event.get("peak_excess_ha") or 0); ratio=float(event.get("peak_ratio") or 0)
    concurrent=len(event.get("concurrent_districts") or []); repeats=int(event.get("detections") or 1)
    baseline=int(event.get("baseline_images") or 0); valid=float(event.get("valid_ha") or 0)
    water=40*min(excess/5000,1)
    anomaly=20*min(max(ratio-1,0)/4,1)
    extent=10*min((repeats-1+concurrent)/3,1)
    quality=7.5*min(baseline/4,1)+7.5*min(valid/20000,1)
    rain_points=0.0 if rainfall_mm is None else 15*min(float(rainfall_mm)/50,1)
    score=round(water+anomaly+extent+quality+rain_points,1)
    corroboration=[]
    if rainfall_mm is not None and rainfall_mm>=20: corroboration.append(f"{rainfall_mm:.0f} mm rainfall in the 7 days before the peak")
    if repeats>1: corroboration.append(f"detected on {repeats} acquisitions")
    if concurrent: corroboration.append(f"concurrent signal in {concurrent} other district(s)")
    verdict=verification_verdict(verification)
    radar_detected=excess>=confirmed_excess_ha and ratio>=3 and bool(corroboration)
    if verdict["refuted"]:
        status="Rejected – not a flood"; score=min(score,20.0)
    elif radar_detected and verdict["confirmed"]:
        status="Detected flood event"
    elif radar_detected and verdict["compact"] is not False:
        status="Detected flood event"   # cloudy: radar evidence with flood-like structure
    elif excess>=min_excess_ha:
        status="Possible flood event"
    else:
        status="Below detection threshold"
    if verdict["compact"] is False and not verdict["confirmed"]: score=min(score,40.0)
    detected=status=="Detected flood event"
    confidence=("High" if detected and verdict["confirmed"] else "Medium" if detected else "Low")
    reasons=[f"{excess:,.0f} ha of new radar-dark surface above the district's normal level ({ratio:.1f}× background)"]
    reasons+=corroboration or ["no rainfall, repeat or regional corroboration"]
    if verdict.get("rejection"): reasons.insert(0,"REJECTED: "+verdict["rejection"])
    if verification and verification.get("historical_water_pct") is not None:
        reasons.append(f"{verification['historical_water_pct']:.0f}% of the water is on normally-wet river/reservoir ground (JRC)")
    if verification:
        reasons.append(f"Sentinel-2 optical check: {verdict['optical']}"
                       +(f" ({verification['optical_water_pct']:.0f}% of radar water is optically water, "
                         f"{verification['optical_clear_pct']:.0f}% cloud-free)" if verification.get("optical_clear_pct") is not None and verification.get("optical_water_pct") is not None else ""))
        if verification.get("large_patch_pct") is not None:
            reasons.append(f"spatial pattern: {verdict['structure']} ({verification['large_patch_pct']:.0f}% in patches ≥ 10 ha)")
    if rainfall_mm is None: reasons.append("rainfall evidence unavailable for these dates")
    if baseline<3: reasons.append(f"shallow baseline ({baseline} same-track images)")
    return {**event,"rainfall_7d_mm":rainfall_mm,"score":round(score,1),"status":status,
            "confidence":confidence,"reasons":reasons,"verification":verification,"verdict":verdict,
            "score_parts":{"water_change":round(water,1),"anomaly":round(anomaly,1),
                           "extent":round(extent,1),"data_quality":round(quality,1),
                           "rainfall":round(rain_points,1)}}

def rank_events(events: list[dict]) -> list[dict]:
    """Detected before possible; within each, most recent peak first, then score."""
    order={"Detected flood event":0,"Possible flood event":1,"Rejected – not a flood":3}
    return sorted(events,key=lambda e:(order.get(e.get("status"),2),
                                       -_as_date(e["peak_date"]).toordinal(),-float(e.get("score",0))))

def select_latest_reliable(events: list[dict]) -> dict | None:
    """Latest *detected* event, or None. Possible events are never auto-promoted."""
    detected=[e for e in events if e.get("status")=="Detected flood event"]
    return max(detected,key=lambda e:(_as_date(e["peak_date"]),float(e.get("score",0)))) if detected else None

def crop_damage_risk(relative_ndvi_change: float | None, valid_fraction: float | None,
                     flooded_ha: float, min_valid_fraction: float = 0.3) -> str:
    """Preliminary damage-risk class from flood exposure and NDVI evidence.

    ``relative_ndvi_change`` is the NDVI change of flooded fields minus the NDVI
    change of unflooded fields of the same crop (difference-in-differences), so
    normal seasonal greening or harvest does not by itself raise the risk.
    """
    if flooded_ha<=0: return "Low"
    if relative_ndvi_change is None or valid_fraction is None or valid_fraction<min_valid_fraction:
        return "Insufficient Data"
    if relative_ndvi_change<=-0.15: return "High"
    if relative_ndvi_change<=-0.07: return "Medium"
    return "Low"
