"""About: method, data sources and limitations."""
import streamlit as st
from config.settings import settings

st.markdown("""
#### How ZarailensAI works

**A. Automatic latest-flood monitoring (Dashboard)**
1. Finds the newest Sentinel-1 IW acquisition over Khyber Pakhtunkhwa in Google Earth Engine.
2. Lists every acquisition *slice* (date + orbit track) in the monitoring window and in an earlier background window.
3. Compares each slice with the median of the same track over the previous 60 days and maps **new water**:
   VV change < {cd} dB, VV < {wd} dB and VH < {vh} dB, excluding JRC permanent water (≥ 90 % occurrence), slopes > {sl}°,
   HAND > {hand} m and elevation > {el} m (wet snow mimics water in radar).
4. Measures new water per district at {sc} m, learns each district's normal level from the background window
   (irrigation, rice paddies and river beds produce a recurring signal) and scores **excess** water.
5. Groups anomalous acquisitions into events, adds rainfall (CHIRPS daily, or GPM IMERG while CHIRPS is not yet published) and
   classifies each as **Detected** (≥ {ce:,.0f} ha excess, ≥ 3× normal, plus rainfall/repeat/regional corroboration) or **Possible**.
   Strong candidates are then **verified independently**: (a) cloud-free Sentinel-2 imagery from 3 days before to 6 days after
   the radar pass must show open water (MNDWI > 0) on the radar "water" pixels — if ≥ 30 % of them are clear and < 10 % look like water,
   the event is **Rejected – not a flood** (wet or freshly ploughed soil is radar-dark but dry in optical images); (b) when it is cloudy,
   at least 35 % of the radar water must lie in connected patches ≥ 10 ha, because floods form sheets while field-by-field wetting
   forms small strips. These rules were calibrated on the real D.I. Khan flood of 29 Aug 2022 (84 % optically water) and a false
   alarm in Lakki Marwat on 10 Oct 2026 (1 % optically water).
6. Shows the latest *detected* event. If there is none, it states *"No confirmed recent flood event detected"* and searches
   the most recent {hy} monsoon season(s) as clearly labelled history.
7. For the event district it retrieves the actual same-track before/after acquisitions, maps flooding at {es} m (30 m speckle filter),
   overlays ESA WorldCover cropland and ESA WorldCereal crop types, and compares Cloud Score+ masked Sentinel-2 NDVI before and after.

**B. User-selected analysis (New Analysis)** — any district, tehsil, place or drawn area, manual dates, a whole-year scan,
or an event from the KPK search, using the same Sentinel-1 rule.

#### Damage-risk classes
Risk is assigned per crop from the **relative NDVI change** — the change on flooded fields minus the change on unflooded fields of
the same crop — so normal growth or harvest does not count as damage: High ≤ −0.15, Medium ≤ −0.07, otherwise Low;
*Insufficient Data* when fewer than 30 % of flooded fields have cloud-free Sentinel-2 observations.

#### Data sources
Copernicus Sentinel-1 GRD and Sentinel-2 L2A (ESA), Google Cloud Score+, JRC Global Surface Water v1.4, SRTM, MERIT Hydro,
ESA WorldCover 2021, ESA WorldCereal 2021, CHIRPS v2, NASA GPM IMERG V07, FAO GAUL 2025 districts, geoBoundaries tehsils (ODbL),
OpenStreetMap Nominatim, Esri World Imagery basemap.

#### Limitations
* Satellite data are not live: Sentinel-1 revisits each track every 6–12 days and appears in Earth Engine after a delay.
  Short floods that recede between passes can be missed (e.g. Charsadda, late August 2022).
* The detection date is an acquisition date. The flood began between the *before* and *after* acquisitions.
* Thresholds are heuristic and unvalidated for KPK; urban areas, wet soil, flash floods in narrow valleys and vegetation-covered
  water are poorly detected.
* WorldCereal/WorldCover describe 2021; they do not show the current season's crops. Rice, sugarcane and cotton are not separated.
* Risk classes are inspection priorities, not measured losses. **No crop-destruction percentage or financial loss is estimated**
  because no validated model or ground-truth dataset is available. Field observations recorded on the Field Verification page
  are the first step toward such validation.
""".format(cd=settings.s1_change_db,wd=settings.s1_water_db,vh=settings.s1_vh_water_db,sl=settings.max_slope_deg,
           hand=settings.max_hand_m,el=settings.max_elevation_m,sc=settings.screening_scale_m,ce=settings.confirmed_event_excess_ha,
           hy=settings.historical_fallback_years,es=settings.event_scale_m))
