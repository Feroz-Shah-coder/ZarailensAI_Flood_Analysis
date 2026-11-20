"""Analyse every reported KPK flood event (2010–2026) for each of its districts with real
Earth Engine data, save results to the database and save a real thumbnail per result.

Resumable: already analysed event/district pairs are skipped (use --force to redo).
    python scripts/analyze_flood_catalog.py            # all missing
    python scripts/analyze_flood_catalog.py --event 2022-jun-aug
    python scripts/analyze_flood_catalog.py --thumbs-only
"""
import argparse, logging, sys, time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
try:
    from dotenv import load_dotenv; load_dotenv()
except ImportError: pass
from config.settings import settings
from src import analysis_history as store
from src.flood_catalog import export_seed, load_catalog, analysis_districts, analyze_catalog_event, save_thumbnail, thumbnail_path

log_path=Path(__file__).resolve().parents[1]/"outputs"/"catalog_batch.log"; log_path.parent.mkdir(exist_ok=True)
logging.basicConfig(level=logging.INFO,format="%(asctime)s %(levelname)s %(message)s",
                    handlers=[logging.FileHandler(log_path,encoding="utf-8"),logging.StreamHandler()])
log=logging.getLogger("catalog")

def main():
    ap=argparse.ArgumentParser(); ap.add_argument("--event"); ap.add_argument("--force",action="store_true")
    ap.add_argument("--thumbs-only",action="store_true"); a=ap.parse_args()
    events=[e for e in load_catalog()["events"] if not a.event or e["id"]==a.event]
    events.sort(key=lambda e:(e["era"]!="landsat",(e["end_date"]-e["start_date"]).days))   # fast Landsat + short windows first
    db=settings.analysis_history_db
    jobs=[(e,d) for e in events for d in analysis_districts(e)]
    log.info("%d event/district pairs",len(jobs))
    for i,(e,d) in enumerate(jobs,1):
        cached=store.catalog_analyses(e["id"],db).get((e["id"],d))
        result=cached
        if a.thumbs_only and not cached: continue
        if not a.thumbs_only and (not cached or a.force or cached.get("status")=="Analysis failed"):
            t=time.time(); log.info("[%d/%d] %s · %s ...",i,len(jobs),e["id"],d)
            try:
                result=analyze_catalog_event(e,d,settings,settings.ee_project)
                store.save_catalog_analysis(result,db)
                log.info("    -> %s (%.0fs)",result.get("status"),time.time()-t)
            except Exception as exc:
                log.exception("    failed: %s",exc); continue
        if result and (a.force or a.thumbs_only or not thumbnail_path(e["id"],d).exists()):
            try: log.info("    thumbnail: %s",save_thumbnail(result,settings.ee_project))
            except Exception as exc: log.warning("    thumbnail failed: %s",exc)
        export_seed(db)   # keep the deploy seed (data/reference/catalog_results.json.gz) current

if __name__=="__main__": main()
