"""ZarailensAI web API (FastAPI) and web app host.

Long Earth Engine work is split into short steps that the browser runs one after
another, so every request stays inside a Vercel function's time limit:

    plan  → list Sentinel-1 acquisition slices for a window
    run   → measure new water for a batch of slices (per district)
    events→ background-relative scoring and grouping into candidate events
    assess→ rainfall + Sentinel-2 / pattern / seasonal-water checks for one candidate
    finalize → rank, pick the latest reliable event
    analyze  → full district analysis (pair, flood, farmland, crops, NDVI, tehsils)

Nothing here invents a value: every number comes from Earth Engine through ``src``.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import csv
import io
import json
import logging
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.middleware.gzip import GZipMiddleware

from config.settings import settings, ROOT
from server import storage
from src import analysis_history as store

log=logging.getLogger("zarailens")
logging.basicConfig(level=logging.INFO,format="%(asctime)s %(levelname)s %(name)s: %(message)s")
WEB=ROOT/"webapp"
app=FastAPI(title="ZarailensAI",docs_url="/api/docs",openapi_url="/api/openapi.json",redoc_url=None)
app.add_middleware(GZipMiddleware,minimum_size=2000)
DB=lambda: settings.analysis_history_db

# ------------------------------------------------------------------ helpers
def _json(value):
    return json.loads(json.dumps(value,default=str))

def _day(value) -> date:
    try: return date.fromisoformat(str(value)[:10])
    except Exception: raise HTTPException(400,f"Invalid date: {value!r}")

def _guard(request: Request) -> None:
    """Running Earth Engine jobs and saving results need the analyst token when ADMIN_TOKEN is set."""
    if settings.admin_token and request.headers.get("x-admin-token")!=settings.admin_token:
        raise HTTPException(401,"Analyst token required. Enter it in the sidebar (ADMIN_TOKEN on the server).")

@app.exception_handler(Exception)
async def _errors(request: Request, exc: Exception):
    log.exception("Request failed: %s",request.url.path)
    return JSONResponse({"detail":f"{type(exc).__name__}: {exc}"},status_code=502)

@app.middleware("http")
async def _ready(request: Request, call_next):
    import time
    t0=time.time()
    if request.url.path.startswith("/api/"): storage.ensure_ready()
    response=await call_next(request)
    seconds=time.time()-t0
    response.headers["X-Process-Seconds"]=f"{seconds:.1f}"
    if seconds>5: log.warning("%s %s took %.1f s",request.method,request.url.path,seconds)
    return response

# ------------------------------------------------------------------ web app files
@app.get("/",include_in_schema=False)
def index(): return FileResponse(WEB/"index.html",headers={"Cache-Control":"no-cache"})

@app.get("/assets/{name:path}",include_in_schema=False)
def asset(name: str):
    path=(WEB/"assets"/name).resolve()
    if not str(path).startswith(str((WEB/"assets").resolve())) or not path.is_file(): raise HTTPException(404)
    return FileResponse(path,headers={"Cache-Control":"public, max-age=300"})

# ------------------------------------------------------------------ meta & boundaries
@app.get("/api/health")
def health(): return {"ok":True,"time":datetime.now(timezone.utc).isoformat()}

@app.get("/api/meta")
def meta():
    from src.flood_monitoring import rule_params
    import os
    names=sorted(f["properties"]["district"] for f in _districts()["features"])
    return {"app":settings.app_name,"ee_project":settings.ee_project or os.getenv("EE_PROJECT"),
            "ee_service_account":bool(os.getenv("EE_SERVICE_ACCOUNT_JSON")),"storage":storage.mode(),
            "token_required":bool(settings.admin_token),"districts":names,"monitored_districts":settings.monitored_districts,
            "params":{"monitoring_days":settings.monitoring_days,"background_days":settings.background_days,
                      "screening_scale_m":settings.screening_scale_m,"event_scale_m":settings.event_scale_m,
                      "auto_refresh_hours":settings.auto_refresh_hours,"historical_fallback_years":settings.historical_fallback_years,
                      "min_event_excess_ha":settings.min_event_excess_ha,"confirmed_event_excess_ha":settings.confirmed_event_excess_ha,
                      **rule_params(settings)}}

def _districts() -> dict:
    from src.flood_monitoring import kpk_district_geojson
    return kpk_district_geojson(settings.boundaries_dir,settings.ee_project)

@app.get("/api/geo/districts")
def districts_geojson(): return _districts()

@app.get("/api/geo/district/{name}")
def district_geojson(name: str):
    from src.flood_monitoring import district_geometry
    g=district_geometry(name,settings.boundaries_dir,settings.ee_project)
    if g is None: raise HTTPException(404,f"No boundary for {name}")
    return g

# ------------------------------------------------------------------ KPK screening steps
@app.post("/api/latest")
def latest(request: Request):
    _guard(request)
    from src.flood_monitoring import latest_s1_acquisition, monsoon_windows
    latest=latest_s1_acquisition(settings.ee_project)
    if not latest: return {"latest_acquisition":None}
    end=date.fromisoformat(latest[:10])+timedelta(days=1); start=end-timedelta(days=settings.monitoring_days)
    return {"latest_acquisition":latest,"start":str(start),"end":str(end),"monitoring_window":[str(start),str(end-timedelta(days=1))],
            "fallback_windows":[{"label":l,"start":str(s),"end":str(e)} for l,s,e in monsoon_windows(date.today(),start,settings.historical_fallback_years)]}

@app.post("/api/screen/plan")
def screen_plan(request: Request, body: dict = Body(...)):
    """Sentinel-1 acquisition slices (date, relative orbit, pass) for a window and its background period."""
    _guard(request)
    from src.ee_auth import initialize
    from src.flood_monitoring import kpk_districts, _slice_keys
    start,end=_day(body["start"]),_day(body["end"])
    if end<=start: raise HTTPException(400,"End must be after start.")
    background_start=start-timedelta(days=settings.background_days)
    ee=initialize(settings.ee_project)
    region=kpk_districts(ee)
    if body.get("districts"): region=region.filter(ee.Filter.inList("GAUL2_NAME",list(body["districts"])))
    keys=_slice_keys(ee,region.geometry(),background_start,min(end,date.today()+timedelta(days=1)))
    return {"keys":[list(k) for k in keys],"start":str(start),"end":str(end),"background_start":str(background_start),
            "monitoring_slices":sum(1 for k in keys if date.fromisoformat(k[0])>=start)}

@app.post("/api/screen/run")
def screen_run(request: Request, body: dict = Body(...)):
    """Measure new water per district for a batch of slices (keep batches ≤ 8)."""
    _guard(request)
    from src.flood_monitoring import screen_kpk
    keys=body.get("keys") or []
    if len(keys)>12: raise HTTPException(400,"At most 12 slices per request.")
    out=screen_kpk(_day(body["start"]),_day(body["end"]),_day(body["background_start"]),settings,settings.ee_project,
                   district_names=body.get("districts") or None,keys=keys)
    return _json({"rows":out["rows"],"errors":out["errors"]})

@app.post("/api/screen/events")
def screen_events(body: dict = Body(...)):
    """Score slices against each district's background and group them into candidate events (no Earth Engine)."""
    from src.event_analysis import district_backgrounds, score_slices, group_events, add_spatial_context
    from src.flood_monitoring import _district_summary
    rows=body.get("rows") or []
    backgrounds=district_backgrounds(rows); scored=score_slices(rows,backgrounds)
    events=add_spatial_context(group_events(scored,settings.min_event_excess_ha))
    return _json({"scored":scored,"events":events,"backgrounds":backgrounds,"district_summary":_district_summary(scored)})

@app.post("/api/events/assess")
def events_assess(request: Request, body: dict = Body(...)):
    """Rainfall, Sentinel-2 optical water, flood pattern and seasonal-water checks + classification."""
    _guard(request)
    from src.flood_monitoring import assess_event
    event=dict(body["event"])
    for k in ("first_detection","last_detection","peak_date"):
        if k in event and event[k]: event[k]=_day(event[k])
    return _json(assess_event(event,settings,settings.ee_project))

@app.post("/api/screen/finalize")
def screen_finalize(body: dict = Body(...)):
    from src.event_analysis import rank_events, select_latest_reliable, previous_clear_observation
    ranked=rank_events(body.get("events") or []); scored=body.get("scored") or []
    for e in ranked:
        clear=previous_clear_observation([r for r in scored if r["district"]==e["district"]],e)
        e["previous_clear_date"]=clear["date"] if clear else None
    return _json({"events":ranked,"selected":select_latest_reliable(ranked)})

@app.post("/api/events/analyze")
def events_analyze(request: Request, body: dict = Body(...)):
    """Full analysis of one event (1–4 minutes)."""
    _guard(request)
    from src.flood_monitoring import analyze_event
    event=dict(body["event"])
    for k in ("first_detection","last_detection","peak_date"):
        if k in event and event[k]: event[k]=_day(event[k])
    return _json(analyze_event(event,settings,settings.ee_project))

@app.post("/api/events/layers")
def events_layers(body: dict = Body(...)):
    """Fresh Earth Engine map tiles (tile links expire after a few hours) for an analysed event."""
    from src.flood_monitoring import event_tile_layers
    return _json(event_tile_layers(body["detail"],settings.ee_project))

# ------------------------------------------------------------------ dashboard runs
@app.get("/api/dashboard")
def dashboard():
    return _json({"run":store.latest_monitoring_run(DB()),"runs":store.list_monitoring_runs(20,DB())})

@app.post("/api/runs")
def save_run(request: Request, body: dict = Body(...)):
    _guard(request)
    run=body["run"]
    if run.get("status") not in ("event","historical_event","no_event","no_data","error"): raise HTTPException(400,"Invalid run status")
    with storage.writing(): run_id=store.save_monitoring_run(run,DB())
    return {"run_id":run_id}

@app.get("/api/runs/{run_id}")
def get_run(run_id: int):
    import sqlite3
    with sqlite3.connect(DB()) as c:
        row=c.execute("SELECT result_json FROM monitoring_runs WHERE id=?",(run_id,)).fetchone()
    if not row: raise HTTPException(404)
    run=json.loads(row[0]); run["run_id"]=run_id; return run

# ------------------------------------------------------------------ reported events 2010–2026
def _catalog_rows(event_id: str | None = None) -> dict:
    return {f"{k[0]}|{k[1]}":v for k,v in store.catalog_analyses(event_id,DB()).items()}

def _thumb_url(event_id: str, district: str) -> str | None:
    from src.flood_catalog import thumbnail_path
    return f"/api/thumb/{event_id}/{district}" if thumbnail_path(event_id,district).exists() else None

def _brief(r: dict) -> dict:
    from src.flood_catalog import headline
    return {"district":r["district"],"status":r.get("status"),"headline":headline(r) or None,
            "computed_at":r.get("computed_at"),"thumb":_thumb_url(r["event_id"],r["district"])}

def _event_public(e: dict) -> dict:
    from src.flood_catalog import era_label, search_window, analysis_districts
    s,t=search_window(e)
    return {k:v for k,v in e.items() if k not in ("start_date","end_date")}|{"era_label":era_label(e),
            "search_window":[str(s),str(t)],"analysis_districts":analysis_districts(e)}

@app.get("/api/catalog")
def catalog():
    from src.flood_catalog import load_catalog
    data=load_catalog(); rows=_catalog_rows()
    events=[]
    for e in data["events"]:
        done=[_brief(r) for k,r in rows.items() if k.startswith(e["id"]+"|")]
        events.append(_event_public(e)|{"analyses":done})
    return _json({"source":data.get("source"),"events":events})

@app.get("/api/catalog/{event_id}")
def catalog_event(event_id: str):
    from src.flood_catalog import load_catalog
    e=next((x for x in load_catalog()["events"] if x["id"]==event_id),None)
    if not e: raise HTTPException(404,"Unknown event")
    rows=_catalog_rows(event_id)
    return _json(_event_public(e)|{"results":{r["district"]:r|{"thumb":_thumb_url(event_id,r["district"])} for r in rows.values()}})

def _catalog_event(event_id: str) -> dict:
    from src.flood_catalog import load_catalog
    e=next((x for x in load_catalog()["events"] if x["id"]==event_id),None)
    if not e: raise HTTPException(404,"Unknown event")
    return e

def _finish_result(result: dict, event: dict, district: str, save: bool) -> dict:
    from src.flood_catalog import save_thumbnail
    from src.flood_monitoring import district_geometry
    result.update(event_id=event["id"],district=district,computed_at=datetime.now(timezone.utc).isoformat(),
                  geometry=district_geometry(district,settings.boundaries_dir,settings.ee_project))
    result=_json(result)
    if save:
        with storage.writing(): store.save_catalog_analysis(result,DB())
        try: save_thumbnail(result,settings.ee_project)
        except Exception as exc: log.warning("Picture not saved: %s",exc)
    result["thumb"]=_thumb_url(event["id"],district)
    return result

@app.post("/api/catalog/{event_id}/landsat")
def catalog_landsat(event_id: str, request: Request, body: dict = Body(...)):
    """Landsat-era (2010–2014) analysis in one request (about 30 s)."""
    _guard(request)
    from src.flood_catalog import _analyze_landsat
    from src.flood_monitoring import district_geometry
    e=_catalog_event(event_id); district=body["district"]
    geometry=district_geometry(district,settings.boundaries_dir,settings.ee_project)
    if geometry is None: raise HTTPException(404,f"No boundary for {district}")
    return _finish_result(_analyze_landsat(e,district,geometry,settings,settings.ee_project),e,district,save=True)

@app.post("/api/catalog/{event_id}/save")
def catalog_save(event_id: str, request: Request, body: dict = Body(...)):
    """Store a Sentinel-era result assembled from the steps above (timeline, assessed event, detail)."""
    _guard(request)
    e=_catalog_event(event_id); district=body["district"]
    result={k:body.get(k) for k in ("era","window","timeline","errors","acquisitions_in_window","status","message",
                                    "event","other_candidates","detail","detail_error")}
    result["era"]="sentinel"
    return _finish_result(result,e,district,save=True)

@app.post("/api/catalog/layers")
def catalog_layers(body: dict = Body(...)):
    """Fresh map tiles for a stored result (Sentinel detail or Landsat pair)."""
    r=body["result"]
    if r.get("detail"):
        from src.flood_monitoring import event_tile_layers
        return _json(event_tile_layers(r["detail"],settings.ee_project))
    if r.get("landsat"):
        from src.satellite_retrieval import SensorCatalog, optical_event_impact
        l=r["landsat"]
        spec=next(s for s in SensorCatalog.optical_for_date(_day(l["after_date"])) if s.name==l["sensor"])
        return _json(optical_event_impact(l["baseline_ids"],l["after_id"],spec,r["geometry"],settings.ee_project)["layers"])
    return {}

@app.get("/api/thumb/{event_id}/{district}")
def thumb(event_id: str, district: str):
    from src.flood_catalog import thumbnail_path
    path=thumbnail_path(event_id,district)
    if not path.exists(): raise HTTPException(404)
    return FileResponse(path,media_type="image/png",headers={"Cache-Control":"public, max-age=3600"})

# ------------------------------------------------------------------ New Analysis (user window)
@app.post("/api/analysis/landsat")
def analysis_landsat(request: Request, body: dict = Body(...)):
    """User window before 2015: Landsat water change for a district."""
    _guard(request)
    from src.flood_catalog import _analyze_landsat, custom_event
    from src.flood_monitoring import district_geometry
    district=body["district"]; e=custom_event(district,_day(body["start"]),_day(body["end"]))
    geometry=district_geometry(district,settings.boundaries_dir,settings.ee_project)
    if geometry is None: raise HTTPException(404,f"No boundary for {district}")
    return _finish_result(_analyze_landsat(e,district,geometry,settings,settings.ee_project),e,district,save=False)

@app.post("/api/analyses")
def save_analysis(request: Request, body: dict = Body(...)):
    """Save a completed New Analysis result to Analysis History."""
    _guard(request)
    r=body["result"]; s=result_summary(r,r.get("label") or "New analysis")
    before=((r.get("detail") or {}).get("before")) or ({"date":(r.get("landsat") or {}).get("before_dates",[None])[-1]} if r.get("landsat") else None)
    after=((r.get("detail") or {}).get("after")) or ({"date":(r.get("landsat") or {}).get("after_date")} if r.get("landsat") else None)
    record={"location":f"{r['district']} district, Khyber Pakhtunkhwa","geometry":r.get("geometry"),
            "requested_before":(r.get("window") or [None])[0],"requested_after":(r.get("window") or [None,None])[1],
            "before":before,"after":after,"summary":s|{"sensor":s.get("sensor") or "Sentinel-1","confidence":(r.get("event") or {}).get("confidence")},
            "event":r.get("event") or {}}
    with storage.writing(): rid=store.save_analysis(record,DB())
    return {"id":rid}

# ------------------------------------------------------------------ automatic district monitoring
@app.get("/api/monitor/status")
def monitor_status():
    from src.district_monitor import next_run_time, REVISIT_NOTE
    svc=store.service_status(DB()); checks=store.list_checks(30,DB())
    last_ok=next((c["finished_at"] for c in checks if c.get("status") in ("success","partial")),None)
    state=store.get_monitor_state(DB()); rows=[]
    for d in settings.monitored_districts:
        st=state.get(d) or {}; slices=store.district_slices(d,path=DB())
        rows.append({"district":d,"last_acquisition":st.get("last_acquisition"),"last_check_at":st.get("last_check_at"),
                     "status":st.get("last_status") or "not checked yet","stored":len(slices),
                     "quality":(slices[-1] if slices else {}).get("quality"),"error":st.get("last_error")})
    return _json({"service":svc,"state":state,"district_rows":rows,"checks":checks,
                  "events":store.list_flood_events(limit=200,path=DB()),"alerts":store.list_alerts(limit=50,path=DB()),
                  "districts":settings.monitored_districts,"interval_hours":settings.monitor_interval_hours,
                  "next_run":str(next_run_time(last_ok,settings.monitor_interval_hours)) if last_ok else None,"note":REVISIT_NOTE})

@app.post("/api/monitor/check")
def monitor_check(request: Request, body: dict = Body(default={})):
    """Check one monitored district (new Sentinel-1 acquisitions only); the page calls this per district."""
    _guard(request)
    from src.district_monitor import run_monitor_check
    district=body.get("district")
    if district and district not in settings.monitored_districts: raise HTTPException(400,"Not a monitored district")
    with storage.writing():
        return _json(run_monitor_check(settings,settings.ee_project,trigger=f"web:{district or 'all'}",path=DB(),
                                       districts=[district] if district else None))

@app.post("/api/monitor/events/{event_id}/analyze")
def monitor_analyze(event_id: str, request: Request):
    _guard(request)
    from src.district_monitor import analyze_stored_event
    with storage.writing(): return _json(analyze_stored_event(event_id,settings,settings.ee_project,DB()))

@app.post("/api/monitor/events/{event_id}/verification")
def monitor_verification(event_id: str, request: Request, body: dict = Body(...)):
    _guard(request)
    allowed={"Unverified","Field visit planned","Confirmed flood (field)","Partially confirmed","Not a flood (field)"}
    if body.get("status") not in allowed: raise HTTPException(400,"Invalid status")
    with storage.writing(): store.set_verification_status(event_id,body["status"],DB())
    return {"ok":True}

@app.post("/api/monitor/alerts/ack")
def monitor_ack(request: Request):
    _guard(request)
    with storage.writing(): store.acknowledge_alerts(DB())
    return {"ok":True}

@app.get("/api/cron/monitor")
def cron_monitor(request: Request):
    """Vercel Cron: check the monitored districts whose last check is oldest, within the time limit."""
    import os, time
    secret=settings.cron_secret or os.getenv("CRON_SECRET")
    if not secret or request.headers.get("authorization")!=f"Bearer {secret}": raise HTTPException(401)
    from src.district_monitor import run_monitor_check
    state=store.get_monitor_state(DB()); t0=time.time(); done=[]
    order=sorted(settings.monitored_districts,key=lambda d:(state.get(d) or {}).get("last_check_at") or "")
    for d in order:
        if time.time()-t0>150: break   # leave room for the last district inside the function limit
        with storage.writing(): done.append(_json(run_monitor_check(settings,settings.ee_project,"cron",DB(),districts=[d])))
    return {"checked":[d for d in order][:len(done)],"results":done}

# ------------------------------------------------------------------ history & field verification
@app.get("/api/history")
def history():
    rows=store.list_analyses(200,DB())
    for r in rows: r.pop("geometry",None)
    cat=[_brief(r)|{"event_id":r["event_id"]} for r in store.catalog_analyses(path=DB()).values()]
    return _json({"analyses":rows,"runs":store.list_monitoring_runs(100,DB()),"catalog":cat})

FIELD_KEYS=("event_key","district","latitude","longitude","observed_on","flooding_observed","crop","damage_observed","observer","notes")

@app.get("/api/field")
def field_list(): return _json(store.list_field_observations(path=DB()))

@app.post("/api/field")
def field_save(request: Request, body: dict = Body(...)):
    _guard(request)
    try:
        lat,lon=float(body["latitude"]),float(body["longitude"])
    except Exception: raise HTTPException(400,"Latitude and longitude are required numbers.")
    if not (29<=lat<=37.5 and 69<=lon<=74.5): raise HTTPException(400,"Location must be inside Khyber Pakhtunkhwa (lat 29–37.5, lon 69–74.5).")
    obs={k:body.get(k) for k in FIELD_KEYS}|{"latitude":lat,"longitude":lon}
    obs={k:(str(v)[:2000] if isinstance(v,str) else v) for k,v in obs.items()}
    with storage.writing(): oid=store.save_field_observation(obs,DB())
    return {"id":oid}

@app.get("/api/field.csv")
def field_csv():
    rows=store.list_field_observations(path=DB()); out=io.StringIO()
    if rows:
        w=csv.DictWriter(out,fieldnames=list(rows[0])); w.writeheader(); w.writerows(rows)
    return Response(out.getvalue(),media_type="text/csv",headers={"Content-Disposition":'attachment; filename="field_observations.csv"'})

# ------------------------------------------------------------------ reports
def result_summary(r: dict, title: str) -> dict:
    """Flat report summary for a district result (reported event or New Analysis)."""
    from src.flood_monitoring import monitoring_summary
    s={"report":f"ZarailensAI – {title}","district":r.get("district"),"satellite_result":r.get("status"),
       "search_window":" → ".join(r.get("window") or []),"computed_at":r.get("computed_at")}
    if r.get("detail"):
        s.update(monitoring_summary({"refreshed_at":r.get("computed_at"),"monitoring_window":r.get("window"),
                                     "selected_source":title},r.get("event"),r["detail"]))
    elif r.get("landsat"):
        l=r["landsat"]
        s.update({"sensor":l["sensor"],"before_images":", ".join(l.get("before_dates",[])),"after_image":l["after_date"],
                  "flood_ha":l["flood_ha"],"ag_ha":l["agricultural_flood_ha"],"total_cropland_ha":l["total_cropland_ha"],
                  "method":l.get("rule"),"cropland_source":l.get("cropland_source"),"crop_rows":l.get("crop_rows",[]),
                  "rainfall_series":(l.get("rainfall") or {}).get("series",[])})
    if r.get("message"): s["note"]=r["message"]
    return s

@app.post("/api/report/{fmt}")
def report(fmt: str, body: dict = Body(...)):
    """PDF or Excel report. Body: {"run_id"} (dashboard), {"event_id","district"} (reported event),
    or {"result","title"} (New Analysis result)."""
    from src.report_generator import pdf_report, excel_report
    if fmt not in ("pdf","xlsx"): raise HTTPException(404)
    if body.get("run_id") is not None:
        from src.flood_monitoring import monitoring_summary
        run=get_run(int(body["run_id"])); summary=monitoring_summary(run); name="KPK event"
    elif body.get("event_id"):
        r=store.catalog_analyses(body["event_id"],DB()).get((body["event_id"],body["district"]))
        if not r: raise HTTPException(404,"Not analysed yet")
        e=_catalog_event(body["event_id"])
        summary=result_summary(r,f"reported flood event {e['year']} {e['period']}")|{"reported_area":e["areas"],"event":e["event"]}
        name=f"{e['id']} - {r['district']}"
    elif body.get("result"):
        summary=result_summary(body["result"],body.get("title") or "New analysis"); name=f"{body['result'].get('district','analysis')}"
    else: raise HTTPException(400,"Nothing to report")
    buf=io.BytesIO()
    (pdf_report if fmt=="pdf" else excel_report)(_json(summary),buf)
    media="application/pdf" if fmt=="pdf" else "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    return Response(buf.getvalue(),media_type=media,headers={"Content-Disposition":f'attachment; filename="ZarailensAI - {name}.{fmt}"'})
