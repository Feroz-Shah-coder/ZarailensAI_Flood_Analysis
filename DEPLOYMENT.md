# Deploying ZarailensAI on Vercel

ZarailensAI is a FastAPI app. Vercel's Python runtime finds the `app` object in `app.py` and serves the whole
site — web pages and API — from it. No build step and no Node.js are needed.

## 1. Earth Engine service account (once)

A server cannot use your personal `earthengine authenticate` login.

1. Google Cloud Console → your Earth Engine project → **IAM & Admin → Service accounts → Create**.
2. Give it the roles **Earth Engine Resource Viewer** and **Service Usage Consumer**, and register it for Earth Engine
   (Earth Engine project settings / https://signup.earthengine.google.com/#!/service_accounts).
3. **Keys → Add key → JSON**. Keep this file private; you paste its whole content into Vercel below.

## 2. Push to GitHub

```powershell
git add -A
git commit -m "ZarailensAI web app for Vercel"
git push
```

`.gitignore` keeps `.env`, keys, `.venv` and the local database out of the repository. Computed results travel in
`data/reference/catalog_results.json.gz` and card pictures in `data/catalog_thumbs/`; refresh them before pushing:
`python -c "from src.flood_catalog import export_seed; print(export_seed())"`.

## 3. Create the Vercel project

1. https://vercel.com/new → import the GitHub repository.
2. **Framework Preset:** FastAPI (or *Other*). **Root Directory:** the repository root (leave empty).
   Build and install commands: leave the defaults (Vercel installs `requirements.txt`).
3. **Environment Variables:**

   | Name | Value |
   |---|---|
   | `EE_PROJECT` | your Google Cloud project id |
   | `EE_SERVICE_ACCOUNT_JSON` | the whole JSON key file content |
   | `ADMIN_TOKEN` | *(recommended)* a password; visitors can view, but running analyses and saving data need it (enter it in the sidebar) |
   | `CRON_SECRET` | *(recommended)* a random string; protects the scheduled monitoring endpoint |

4. **Deploy.** The site is at `https://<project>.vercel.app`.

## 4. Keep new results (recommended)

Vercel functions can only write to a temporary folder, so analyses run on the site would be lost when the instance
stops. Connect a free Redis store and ZarailensAI saves its database there automatically:

Vercel project → **Storage → Create / Connect → Upstash for Redis** (free plan) → connect to this project.
This adds `KV_REST_API_URL` and `KV_REST_API_TOKEN`; redeploy. The sidebar then shows *Storage: durable (Upstash Redis)*.

Without it the shipped results (reported events, latest dashboard run) are always available, but new runs, saved
analyses and field observations are temporary.

## 5. Scheduled monitoring

`vercel.json` registers a **Vercel Cron** job calling `/api/cron/monitor` once a day (05:00 UTC) — the Hobby plan
allows daily jobs. On the Pro plan you can change the schedule to every 12 hours: `"schedule": "0 */12 * * *"`.
Each run checks the monitored districts with the oldest last check first, inside the function time limit.
The *Run check now* button on the Automatic Flood Monitoring page works at any time.

## Limits to know

* **Function time:** each step is designed to finish within Vercel's 300-second limit; the slowest steps
  (verification and full event analysis) normally take 1–3 minutes. If Earth Engine is very busy a step can time out —
  press the button again.
* **Long jobs run from the browser:** a dashboard refresh or a Sentinel-era analysis is a chain of requests, so keep
  the page open until it finishes (2–15 minutes).
* **Function size:** dependencies are kept small (about 150 MB). `google-api-python-client` is pinned to 1.12.11,
  which works with Earth Engine and avoids ~110 MB of unused discovery files.
* **Quota:** all visitors share one Earth Engine project quota. Set `ADMIN_TOKEN` on a public site.

## Run the same app elsewhere (optional)

* Locally: `python app.py` → http://localhost:8000
* Docker (Cloud Run, Render, Railway, VPS): `docker build -t zarailensai .` then
  `docker run -p 8000:8000 -e EE_PROJECT=… -e EE_SERVICE_ACCOUNT_JSON="$(Get-Content key.json -Raw)" zarailensai`
