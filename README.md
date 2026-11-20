# ZarailensAI

Dynamic Satellite-Based Agricultural Flood Detection and Impact Assessment for KPK.

This Streamlit application provides real Sentinel-1 flood-signal analysis, Earth Engine imagery, generic cropland exposure estimates, local analysis history, and PDF/Excel exports. Results remain unavailable until real satellite observations and a validated boundary are supplied; the app does not substitute demo measurements.

## Pages

`app.py` is the entry point (header, theme, navigation). Pages live in `app_pages/`:

| Page | What it does |
|---|---|
| Dashboard | Automatic latest-flood monitoring across KPK: event status, KPIs, flood & crop impact map, crop-wise damage risk, risk map, before/after Sentinel-1 images, KPK overview, rainfall evidence, candidate events, historical exploration, PDF/Excel export |
| New Analysis | User-selected district / tehsil / place / drawn area; manual dates, auto-detect near a date, whole-year scan, or a detected KPK event; crop damage-risk assessment |
| Analysis History | Saved analyses, impact analytics, automatic monitoring runs |
| Field Verification | Tehsil inspection priorities and a form for ground observations (stored in SQLite, CSV export) |
| Reports | PDF/Excel for the dashboard event or the manual analysis |
| About | Method, data sources, limitations |

## Automatic KPK flood monitoring

`src/flood_monitoring.py` screens every Sentinel-1 IW acquisition slice (date + track) over the 35 FAO GAUL 2025 KPK districts at 200 m against a same-track 60-day median, learns each district's normal "new water" level from the preceding 60 days (irrigation, paddies and river beds produce a recurring signal), scores background-relative excess water, groups anomalies into events, and adds 7-day rainfall (CHIRPS; GPM IMERG where CHIRPS is not yet published). Pure ranking/classification logic is in `src/event_analysis.py`. An event is **Detected** only with ≥ 1,000 ha excess, ≥ 3× normal and corroboration (rainfall ≥ 20 mm, repeat detection or a concurrent district); otherwise **Possible**. Strong candidates are verified against cloud-free Sentinel-2 MNDWI (−3/+6 days) and the share of radar water in connected patches ≥ 10 ha; optically dry signals are **Rejected – not a flood** (calibrated on D.I. Khan 29 Aug 2022, a real flood, and a Lakki Marwat 10 Oct 2026 false alarm caused by wet/ploughed fields). If no detected event exists in the monitoring window the dashboard says *No confirmed recent flood event detected* and searches recent monsoon seasons as labelled history. The selected event is analysed at 20 m: actual same-track before/after pair, flood and cropland area, ESA WorldCereal 2021 crop types, Cloud Score+ masked Sentinel-2 NDVI change (flooded minus unflooded fields of the same crop) and damage-risk classes (Low / Medium / High / Insufficient Data), plus tehsil priorities from geoBoundaries ADM3. Runs are stored in the `monitoring_runs` table; the dashboard re-runs automatically when Earth Engine has newer Sentinel-1 data and the last run is older than `AUTO_REFRESH_HOURS`. A full KPK run takes about 1.5–3 minutes (longer when historical fallback is needed). All thresholds are configurable in `.env` (see `config/settings.py`).

No crop-destruction percentage or financial loss is produced: there is no validated model or ground-truth data for KPK. Field observations collected in the app are the first step toward validation.

## Architecture

`app.py` → `location_search` / `date_selection` → `ee_auth` / `satellite_retrieval` → `image_preprocessing` / `flood_detection` → `agricultural_analysis` / `impact_calculation` → `map_visualization` / `report_generator`.

## Install and run

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
Copy-Item .env.example .env
streamlit run app.py
```

## Automatic monitoring service (5 districts)

`monitor_service.py` is a separate background process (APScheduler) that checks **Lakki Marwat, Charsadda, Nowshera,
Swat and Peshawar** every 12 hours. It never runs inside Streamlit's rerun cycle. The Streamlit page
**Automatic Flood Monitoring** shows service status, last and next check, per-district acquisitions, candidate events,
inspection order, verification status, maps and reports.

```powershell
.\.venv\Scripts\Activate.ps1
python monitor_service.py            # keep running (separate terminal); checks every 12 h
python monitor_service.py --once     # one check now (the dashboard's "Refresh monitoring" does this)
```

How a check works (`src/district_monitor.py`): only Sentinel-1 IW acquisitions not yet stored in `district_slices`
are measured (no duplicates). New water is measured against a same-track 60-day median and compared with each
district's normal level; candidates are assessed with rainfall, Sentinel-2 optical water, spatial pattern and
seasonal river/reservoir water. **A new satellite image alone never creates an event**; only *Detected* events raise
alerts (stored in `monitor_alerts`, shown on the page, optionally POSTed to `ALERT_WEBHOOK_URL`). Detected events get
the full analysis (before/after pair, flood and farmland area, crop types, NDVI change, tehsil ranking) and are ranked
with the existing `inspection_priority` formula (unchanged: 0.4 farmland area + 0.3 farmland share + 0.2 NDVI loss +
0.1 evidence confidence). Overlapping detections update one event (`flood_events`); ground verification status set on
the page is never overwritten by later checks.

Reliability: a database lock prevents overlapping checks (service, button, second process; abandoned locks expire
after `MONITOR_LOCK_MINUTES`). Failed districts are recorded in `monitor_checks` and retried next time; Earth Engine
429 quota errors are retried with back-off. After a restart the schedule resumes from the last successful check.
To start the service with Windows, create a Task Scheduler task "At log on" running
`F:\ZarailensAI\.venv\Scripts\pythonw.exe F:\ZarailensAI\monitor_service.py` with start-in folder `F:\ZarailensAI`.
Configure in `.env`: `MONITORED_DISTRICTS='["Lakki Marwat","Charsadda","Nowshera","Swat","Peshawar"]'`,
`MONITOR_INTERVAL_HOURS=12`, `ALERT_WEBHOOK_URL=`. Logs: `outputs/monitor_service.log`.

Flood detection model: the project contains an untrained `RandomForestFloodDetector` class but no trained model or
labels, so monitoring uses the validated threshold rule; the class is unchanged.

## Use on phones, tablets and other computers

The interface is responsive: KPI cards reflow (5 → 3 → 2 per row), multi-column rows stack into full-width cards
below 1200 px, and the sidebar collapses to a menu button on phones. Tested at 390 px (phone), 820 px (tablet) and
1280 px+ (laptop/desktop) with no horizontal scrolling.

**Same Wi-Fi (no hosting needed):** run `streamlit run app.py --server.address 0.0.0.0`, then open the printed
*Network URL* (e.g. `http://192.168.x.x:8501`) on the phone/tablet. Allow Python through Windows Firewall when asked.
On a phone, use the browser menu → **Add to Home screen** to open ZarailensAI like an app.

## Deploy as a public web app

Online servers cannot use your personal `earthengine authenticate` login, so create a **service account**:
Google Cloud Console → IAM & Admin → Service accounts → Create (role *Earth Engine Resource Viewer* or
*Writer*), then Keys → Add key → JSON, and register it for Earth Engine for your project. Keep the key secret.

**Option A – Streamlit Community Cloud (free):**
1. Push the project to a GitHub repository (`.gitignore` already excludes `.env`, `.venv`, secrets and the local database).
2. On https://share.streamlit.io → *Create app*, pick the repo, branch and `app.py`; choose Python 3.12.
3. In *Advanced settings → Secrets*, paste the contents of `.streamlit/secrets.toml.example` filled with your
   `EE_PROJECT` and the service-account JSON. Deploy; you receive a public `https://<name>.streamlit.app` URL.

**Option B – Docker (Google Cloud Run, Render, Railway, a VPS):**
```powershell
docker build -t zarailensai .
docker run -p 8501:8501 -e EE_PROJECT=your-project -e EE_SERVICE_ACCOUNT_JSON="$(Get-Content key.json -Raw)" zarailensai
```
The container honours the `$PORT` variable that Cloud Run/Render provide.

Hosted-platform notes: the SQLite history (`data/analysis_history.sqlite3`) is stored on the server's disk; on
Streamlit Cloud and Cloud Run that disk is reset on restart, so the dashboard re-runs the KPK search (≈2–25 min,
depending on whether historical fallback is needed) after a restart. Use a persistent volume (Docker/VPS) to keep history
and field observations. All visitors share one Earth Engine project quota.

## Earth Engine setup

1. Create a Google Cloud project and enable the Earth Engine API.
2. Register the project for Earth Engine access (commercial or non-commercial as applicable).
3. With the project environment active, run `python -c "import ee; ee.Authenticate()"` and complete browser sign-in.
4. Set `EE_PROJECT` in `.env`. For deployment, configure `EE_SERVICE_ACCOUNT_JSON` as a JSON object and grant the service account Earth Engine access.
5. Restart Streamlit and verify `initialize()` succeeds. Live searches require network and project authorization.

## Testing

```powershell
pytest -m "not integration"
pytest -m integration
```

The integration suite includes a live check that the automatic KPK screening recovers the documented late-August 2022 D.I. Khan flood.

The ordinary suite uses synthetic arrays and local files. Integration tests are excluded by default; configure credentials and network access before running them.

## Data sources and methodology

Earth Engine collections: Copernicus Sentinel-1 GRD and Sentinel-2 SR Harmonized. Intended ancillary layers include ESA WorldCover cropland class 40, JRC Global Surface Water, and SRTM. Location source is OpenStreetMap Nominatim; authoritative KPK boundaries should be sourced from GAUL / geoBoundaries before accepting AOIs in production. Follow each source's terms and attribution requirements. Nominatim requires a descriptive User-Agent and no more than one request per second.

The local SAR detector flags pixels with after-minus-before backscatter below a configurable dB threshold and after VV below a water threshold. It reports exposed cropland only when a valid cropland raster is supplied. NDVI bins are indicators, not crop damage claims. Crop type remains unavailable unless a validated dataset is provided. Flooded agricultural land does not automatically mean destroyed crops; field validation is required.

The dashboard searches districts from FAO GAUL 2025 and OSM Nominatim, displays and validates polygon boundaries, supports user-drawn areas and explicitly approximate point buffers, and searches Landsat MSS/TM/ETM+/OLI, Sentinel-2, and Sentinel-1 collections according to date. In manual date mode, selecting an AOI and before/after dates automatically retrieves the nearest chronological Sentinel-1 pair that shares orbit direction and relative orbit and meets the coverage setting. The imagery tab displays that pair as live Google Earth Engine XYZ tiles in rounded before, after, and detected-change map cards; the third card overlays a change mask on the actual after acquisition. Each card reports the actual acquisition time and data source. Changing the AOI, dates, search window, or coverage setting refreshes the pair. The **Search a whole year** mode scans each quarter of a selected year using same-orbit pre-event baselines and reports the strongest observed signal, or explicitly reports when no signal meets the 1 ha detection threshold. Selecting a year triggers the scan automatically when an AOI is selected. Year scans start in 2015; the current year includes only acquisitions already available in Earth Engine. Sentinel-1 comparison requires dates from 2014-10-03 onward and a compatible pair within manual search windows. Optical previews remain available for historical context and use cloud-masked composites or a least-cloudy scene, fixed reflectance stretches, AOI clipping, and real Earth Engine imagery; MSS is labelled false colour. The detector applies an after-minus-before VV threshold, an after-image water threshold, a JRC permanent-water mask, and an SRTM slope mask. Scene footprint coverage and cloud percentage are shown separately.

The Dashboard, Impact analytics, and Analysis history tabs summarize actual saved analyses stored locally in `data/analysis_history.sqlite3`; the history is local to this installation. The interactive map combines the selected actual Sentinel-1 acquisitions with the threshold flood signal, ESA WorldCover 2021 cropland class 40, and exposed-cropland overlay. PDF exports can embed real Earth Engine previews for a selected Sentinel-1 pair; Excel exports include acquisition metadata, parameters, agricultural exposure, priority rows, and limitations.

Known limits: WorldCover is a generic 2021 land-cover map and may not reflect current planting. It does not identify crop types or prove crop damage; field validation is required. Flood area and inspection priorities are threshold-based satellite indicators. The app does not yet provide geoBoundaries ADM3 tehsil lookup, Sentinel-2 Cloud Score+ integration (S2 uses SCL and L1C QA60), server-side flood-mask vectorization or GeoTIFF export, or satellite-derived crop-damage estimates. The public Nominatim endpoint may deny some networks (HTTP 403); district search remains available through Earth Engine GAUL, while village search needs accessible OSM/geoBoundaries. Large administrative areas can exceed the configured AOI limit. Live integration checks cover representative locations and dates; they do not validate every location/date or establish ground truth.

## Add a detector

Implement `BaseFloodDetector.detect(before, after, aoi, params) -> FloodResult`. The optional `RandomForestFloodDetector` accepts labeled feature arrays via `train`; labels must come from the user. Add a validation dataset and document its provenance before using it operationally.

## Troubleshooting

- Authentication error: set `EE_PROJECT`, enable API/access, and run `earthengine authenticate`.
- No images: broaden the date window, choose another date, or check project permissions.
- No boundary / point result: draw a polygon; a point is never an AOI.
- Local package installation: use Python 3.11/3.12 and install the pinned requirements in a clean environment.

## Reported flood events 2010–2026

Sidebar → **Flood Events 2010–2026** shows each reported KPK flood (list in
`data/reference/kpk_flood_events.json`) as a card with a real satellite picture. Open a
card for every district's underwater area, flooded farmland, crop damage risk, before/after
images, acquisition timeline and rainfall. Events reported only as "KPK" are measured over
Peshawar, Charsadda, Nowshera, Swat and D. I. Khan.

Pre-compute all events (resumable; results go to the database, pictures to `data/catalog_thumbs/`):

    python scripts/analyze_flood_catalog.py              # everything not yet analysed
    python scripts/analyze_flood_catalog.py --event 2022-jun-aug
    python scripts/analyze_flood_catalog.py --thumbs-only  # redraw card pictures

Progress log: `outputs/catalog_batch.log`.

## Deployment (Vercel)

See **DEPLOYMENT.md**. Vercel hosts the public website (`web/`, `vercel.json`); the Streamlit
app itself runs on Render (`render.yaml`, `Dockerfile`) or Streamlit Community Cloud, because
Vercel's serverless functions cannot keep Streamlit's WebSocket connection or run multi-minute
Earth Engine jobs. Before pushing, refresh the shipped event results:
`python -c "from src.flood_catalog import export_seed; print(export_seed())"`.
