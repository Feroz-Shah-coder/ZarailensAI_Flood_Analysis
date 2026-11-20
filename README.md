# ZarailensAI

Dynamic satellite-based agricultural flood detection and impact assessment for Khyber Pakhtunkhwa (KPK).

ZarailensAI is a web app (FastAPI + HTML/JavaScript) that measures floods and crop damage risk with real
Google Earth Engine data — Sentinel-1 radar, Sentinel-2 optical, Landsat 5/7/8 — and deploys on **Vercel**.
No sample or invented numbers are ever shown: when Earth Engine returns nothing, the app says so.

## Pages

| Page | What it does |
|---|---|
| Dashboard | Latest KPK flood event: status, KPIs, flood & crop impact map, crop-wise damage risk, donut, risk map, key findings, before/after Sentinel-1 and Sentinel-2 images, KPK overview, rainfall, candidate events, historical search, PDF/Excel, full-screen map |
| Automatic Flood Monitoring | Lakki Marwat, Charsadda, Nowshera, Swat, Peshawar: new acquisitions only, candidate events, inspection order, verification status, alerts (Vercel Cron) |
| Flood Events 2010–2026 | Reported KPK floods as picture cards in tabs; each opens a page with every district's underwater area, flooded farmland, crop damage, images, timeline and rainfall |
| New Analysis | Any KPK district and period (Sentinel from 2015, Landsat before) |
| Analysis History | Saved analyses, impact charts, analysed reported events, dashboard runs |
| Field Verification | Tehsil inspection priorities; ground observations (map click, CSV export) |
| Reports | PDF / Excel for the dashboard event and reported events |
| About | Method, data sources, limitations |

## Project layout

```
app.py              FastAPI entry point (Vercel runs this; `python app.py` locally)
server/main.py      API + serves the web app
server/storage.py   SQLite location, seed import, optional Upstash Redis persistence
webapp/             index.html + assets (app.js router, pages/*.js, styles.css; Leaflet + Plotly from CDN)
src/                Earth Engine analysis (unchanged method): flood_monitoring, event_analysis,
                    satellite_retrieval, agricultural_analysis, district_monitor, flood_catalog, report_generator …
config/settings.py  All thresholds and options (override with environment variables / .env)
data/               KPK boundaries, reported-event list, shipped results seed, card pictures
scripts/            analyze_flood_catalog.py (pre-compute all reported events)
monitor_service.py  Optional local 12-hour scheduler (APScheduler) for the district monitor
```

Long Earth Engine work is split into short API steps that the browser runs in order (plan → screen batches →
score/group → assess each candidate → rank → analyse), so every request fits inside a Vercel function's time limit.

## Run locally

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements-dev.txt
Copy-Item .env.example .env        # set EE_PROJECT; run `earthengine authenticate` once
python app.py                      # opens http://localhost:8000
```

On phones/tablets on the same Wi-Fi: `$env:HOST="0.0.0.0"; python app.py`, then open `http://<PC-IP>:8000`.
The layout is responsive (sidebar becomes a ☰ menu; cards and maps stack) and can be added to the home screen.

## Deploy on Vercel

See **DEPLOYMENT.md** (import the GitHub repo, set `EE_PROJECT` and `EE_SERVICE_ACCOUNT_JSON`, optionally connect
Upstash Redis so new results are kept). Before pushing, refresh the shipped results:

```powershell
python -c "from src.flood_catalog import export_seed; print(export_seed())"
```

## Method (summary)

* **KPK screening:** every Sentinel-1 IW slice (date + track) over the 35 FAO GAUL 2025 districts at 200 m against a
  same-track 60-day median; new water = VV change < −3 dB, VV < −15 dB, VH < −22 dB, excluding JRC permanent water,
  slopes > 5°, HAND > 20 m and elevation > 2000 m. Each district's normal level is learned from the 60 days before the
  window; **excess** water is scored and grouped into events.
* **Classification:** *Detected* needs ≥ 1,000 ha excess, ≥ 3× normal and corroboration (rain ≥ 20 mm, repeat or
  concurrent district); *Possible* ≥ 300 ha. Strong candidates are verified: cloud-free Sentinel-2 MNDWI water on the
  radar pixels, connected flood patches, and seasonal water (reservoirs). Optically dry signals are
  *Rejected – not a flood*. If nothing is detected the dashboard says *No confirmed recent flood event detected* and
  searches recent monsoon seasons as labelled history.
* **Event analysis (20 m):** real same-track before/after pair, flood and cropland (ESA WorldCover) area, crop types
  (ESA WorldCereal 2021), Cloud Score+ Sentinel-2 NDVI change (flooded minus unflooded fields of the same crop) →
  damage risk Low / Medium / High / Insufficient Data, tehsil inspection priority (geoBoundaries ADM3), CHIRPS/IMERG rainfall.
* **Before 2015:** Landsat MNDWI water change against a pre-event composite, cropland overlap and NDVI change.
* **No crop-destruction percentage or financial loss is estimated** — there is no validated model or ground truth.

## Automatic district monitoring

`src/district_monitor.py` measures only Sentinel-1 acquisitions not yet stored (no duplicates), assesses candidates
with the same checks, raises alerts only for *Detected* events and ranks them with the existing `inspection_priority`
formula (0.4 farmland area + 0.3 farmland share + 0.2 NDVI loss + 0.1 confidence). On Vercel it runs from
**Vercel Cron** (`/api/cron/monitor`, daily on the Hobby plan; every 12 h on Pro — edit `vercel.json`) and from the
page's *Run check now* button. Locally you can also run `python monitor_service.py` (every 12 h).
The `RandomForestFloodDetector` class remains untrained and unused (no labels); monitoring uses the threshold rule.

## Reported flood events 2010–2026

The list in `data/reference/kpk_flood_events.json` is a reference of *reported* floods; every number on the pages
comes from satellite analysis. Events reported only as "KPK" are measured over Peshawar, Charsadda, Nowshera, Swat and
D. I. Khan. Pre-compute everything (resumable):

```powershell
python scripts/analyze_flood_catalog.py              # all event/district pairs not yet analysed
python scripts/analyze_flood_catalog.py --event 2022-jun-aug
python scripts/analyze_flood_catalog.py --thumbs-only  # redraw card pictures
```

## Testing

```powershell
pytest -m "not integration"     # unit + web API tests (no Earth Engine)
pytest -m integration           # live Earth Engine checks (credentials and network required)
```

## Limitations

* Satellite data are not live: Sentinel-1 revisits each track every 6–12 days; short floods between passes are missed.
* Thresholds are heuristic and unvalidated for KPK; urban areas, wet soil, narrow-valley flash floods and GLOFs are poorly detected.
* WorldCereal/WorldCover describe 2021, not the current season; rice, sugarcane and cotton are not separated.
* Risk classes are inspection priorities, not measured losses. Field observations are the first step toward validation.
* All visitors share one Earth Engine project quota.
