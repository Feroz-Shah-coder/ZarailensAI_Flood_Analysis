"""ZarailensAI automatic flood monitoring service (runs outside Streamlit).

Usage:
    python monitor_service.py            # run forever: a check every MONITOR_INTERVAL_HOURS (default 12)
    python monitor_service.py --once     # one check, then exit (used by the dashboard's Refresh button)

The schedule resumes after a restart: if the last successful check is older than
the interval the service checks immediately, otherwise it waits until it is due.
A database lock prevents overlapping checks (service, dashboard button, second
service). Results, failures and heartbeats are written to the SQLite database
that the Streamlit dashboard reads.
"""
from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT=Path(__file__).resolve().parent
os.chdir(ROOT); sys.path.insert(0,str(ROOT))
from dotenv import load_dotenv
load_dotenv(ROOT/".env")

from config.settings import settings
from src import analysis_history as store
from src.district_monitor import run_monitor_check, next_run_time

LOG_PATH=ROOT/"outputs"/"monitor_service.log"
LOG_PATH.parent.mkdir(exist_ok=True)
logging.basicConfig(level=logging.INFO,format="%(asctime)s %(levelname)s %(name)s %(message)s",
                    handlers=[logging.FileHandler(LOG_PATH,encoding="utf-8"),logging.StreamHandler()],force=True)
log=logging.getLogger("monitor_service")

def _last_success() -> str | None:
    for check in store.list_checks(200,settings.analysis_history_db):
        if check["status"] in ("success","partial") and check["finished_at"]: return check["finished_at"]
    return None

def run_once(trigger: str) -> dict:
    log.info("Monitoring check started (%s) for %s",trigger,", ".join(settings.monitored_districts))
    result=run_monitor_check(settings,settings.ee_project,trigger=trigger,
                             progress=lambda f,t:log.info("%3.0f%% %s",100*f,t))
    log.info("Monitoring check finished: %s – %s",result.get("status"),result.get("message"))
    return result

def main() -> int:
    parser=argparse.ArgumentParser(description=__doc__,formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--once",action="store_true",help="run one check and exit")
    parser.add_argument("--trigger",default=None,help="label stored with the check (e.g. dashboard)")
    parser.add_argument("--interval-hours",type=float,default=settings.monitor_interval_hours)
    args=parser.parse_args()
    if args.once:
        result=run_once(args.trigger or "manual")
        return 0 if result.get("status") in ("success","partial","skipped") else 1

    from apscheduler.schedulers.blocking import BlockingScheduler
    from apscheduler.triggers.interval import IntervalTrigger
    interval=args.interval_hours
    first=next_run_time(_last_success(),interval)
    scheduler=BlockingScheduler(timezone="UTC")

    def scheduled_check():
        run_once("scheduler")
        job=scheduler.get_job("monitor")
        store.update_service_status(settings.analysis_history_db,next_run_at=job.next_run_time.isoformat() if job and job.next_run_time else None)

    def heartbeat():
        job=scheduler.get_job("monitor")
        store.update_service_status(settings.analysis_history_db,pid=os.getpid(),heartbeat_at=datetime.now(timezone.utc).isoformat(),
                                    interval_hours=interval,next_run_at=job.next_run_time.isoformat() if job and job.next_run_time else None)

    # max_instances=1 + coalesce: a slow check never overlaps the next one; missed runs collapse into one.
    scheduler.add_job(scheduled_check,IntervalTrigger(hours=interval,start_date=first-timedelta(hours=interval) if first>datetime.now(timezone.utc) else None),
                      id="monitor",next_run_time=first,max_instances=1,coalesce=True,misfire_grace_time=6*3600)
    scheduler.add_job(heartbeat,IntervalTrigger(minutes=2),id="heartbeat",next_run_time=datetime.now(timezone.utc))
    store.update_service_status(settings.analysis_history_db,pid=os.getpid(),started_at=datetime.now(timezone.utc).isoformat(),
                                interval_hours=interval,next_run_at=first.isoformat())
    log.info("Monitoring service started (pid %s, every %s h). First check at %s UTC.",os.getpid(),interval,first.isoformat()[:19])
    signal.signal(signal.SIGINT,lambda *_:scheduler.shutdown(wait=False))
    try: scheduler.start()
    except (KeyboardInterrupt,SystemExit): pass
    store.update_service_status(settings.analysis_history_db,pid=None,heartbeat_at=None,next_run_at=None)
    log.info("Monitoring service stopped.")
    return 0

if __name__=="__main__":
    sys.exit(main())
