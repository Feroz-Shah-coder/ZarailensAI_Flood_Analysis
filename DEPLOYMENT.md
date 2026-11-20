# Deploying ZarailensAI (Vercel + Python host)

ZarailensAI is a Streamlit (Python) app. Streamlit keeps a live WebSocket open to every
browser and runs Earth Engine analyses that take minutes, which Vercel's serverless
functions cannot do. The deployment therefore has two parts:

| Part | Where | What it does |
|---|---|---|
| **Website** (`web/`, `vercel.json`) | **Vercel** | Public ZarailensAI site; shows the app full-screen and has an "Open full app" button |
| **App** (`app.py`, `Dockerfile`, `render.yaml`) | **Render** *or* **Streamlit Community Cloud** | Runs the Python app and talks to Google Earth Engine |

## 1. Put the project on GitHub

```bash
git init
git add .
git commit -m "ZarailensAI"
git remote add origin https://github.com/<you>/zarailensai.git
git push -u origin main
```

`.gitignore` keeps `.env`, `.streamlit/secrets.toml`, service-account keys, `.venv` and the local
database out of the repository. Computed flood-event results travel in
`data/reference/catalog_results.json.gz` and the card pictures in `data/catalog_thumbs/`;
a fresh server imports them on first start.

## 2. Earth Engine service account (needed by any host)

1. Google Cloud Console → your Earth Engine project → IAM & Admin → Service accounts → *Create*.
2. Grant it the **Earth Engine Resource Viewer** role (and *Service Usage Consumer*),
   then register it for Earth Engine at https://signup.earthengine.google.com/#!/service_accounts
   (or in the Earth Engine project settings).
3. Keys → *Add key* → JSON. Keep this file private; you paste its whole content as a secret below.

## 3a. App on Render (Docker, recommended)

1. https://dashboard.render.com → **New → Blueprint** → choose the GitHub repo (it reads `render.yaml`).
2. Set the secrets:
   * `EE_PROJECT` = your Google Cloud project id
   * `EE_SERVICE_ACCOUNT_JSON` = the whole JSON key file content
3. Deploy. The app is at `https://zarailensai-app.onrender.com` (name may differ).

The free plan sleeps after ~15 min idle (first visit then takes about a minute) and its disk
is temporary: new analyses and field notes are lost on redeploy. For permanent storage add a
Render disk mounted at `/app/data/db` and set `ANALYSIS_HISTORY_DB=/app/data/db/analysis_history.sqlite3`.

## 3b. App on Streamlit Community Cloud (alternative, free)

1. https://share.streamlit.io → *Create app* → repo, branch `main`, main file `app.py`,
   Advanced settings → Python 3.12.
2. Secrets: paste the content of `.streamlit/secrets.toml.example` with your real values.
3. The app is at `https://<name>.streamlit.app`.

## 4. Website on Vercel

1. https://vercel.com/new → import the same GitHub repo.
   **Root Directory → `web`** (click *Edit* next to it). Framework Preset → *Other*.
   This keeps `app.py` / `requirements.txt` out of Vercel's view; otherwise Vercel tries to run
   the Streamlit app as a Python function and fails with `PYTHON_ENTRYPOINT_NOT_FOUND`.
   (`web/vercel.json` sets the build `node build.mjs` → `dist`. If you leave the root as the repo
   root, `.vercelignore` and the root `vercel.json` do the same job.)
2. Settings → Environment Variables:
   * `ZARAILENS_APP_URL` = the app address from step 3 (e.g. `https://zarailensai-app.onrender.com`)
3. Deploy. Your site is `https://<project>.vercel.app`; a custom domain can be added in Vercel.

If `ZARAILENS_APP_URL` is missing the site shows setup instructions instead of the app.
Changing it needs a redeploy (Deployments → ⋯ → Redeploy).

CLI alternative: `npm i -g vercel`, then `vercel` (preview) and `vercel --prod` in the project folder.

## 5. Automatic monitoring service (optional)

`monitor_service.py` (12-hourly checks) is a separate long-running process. On Render add a
**Background Worker** with the same Docker image and start command
`python monitor_service.py` (paid plan; it must share the database disk with the app), or keep
running it on your own computer.

## Local check before deploying

```bash
streamlit run app.py                                   # the app
ZARAILENS_APP_URL=http://localhost:8501 node web/build.mjs
python -m http.server 8080 --directory web/dist        # the Vercel site → http://localhost:8080
```
