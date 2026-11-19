"""Small local SQLite store for real completed analysis runs."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any


def _connect(path: str | Path | None = None) -> sqlite3.Connection:
    db_path=Path(path) if path else Path(__file__).resolve().parents[1]/"data"/"analysis_history.sqlite3"
    db_path.parent.mkdir(parents=True,exist_ok=True)
    connection=sqlite3.connect(db_path,timeout=20)
    connection.row_factory=sqlite3.Row
    connection.execute("""CREATE TABLE IF NOT EXISTS analysis_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        analysis_key TEXT NOT NULL UNIQUE,
        created_at TEXT NOT NULL,
        location TEXT,
        requested_before TEXT,
        requested_after TEXT,
        acquired_before TEXT,
        acquired_after TEXT,
        sensor TEXT,
        flood_ha REAL,
        agricultural_exposure_ha REAL,
        confidence TEXT,
        geometry_json TEXT,
        summary_json TEXT NOT NULL,
        event_json TEXT
    )""")
    connection.execute("""CREATE TABLE IF NOT EXISTS monitoring_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        refreshed_at TEXT NOT NULL,
        window_start TEXT,
        window_end TEXT,
        latest_acquisition TEXT,
        status TEXT NOT NULL,
        result_json TEXT NOT NULL
    )""")
    connection.execute("""CREATE TABLE IF NOT EXISTS field_observations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at TEXT NOT NULL,
        event_key TEXT,
        district TEXT,
        latitude REAL,
        longitude REAL,
        observed_on TEXT,
        flooding_observed TEXT,
        crop TEXT,
        damage_observed TEXT,
        observer TEXT,
        notes TEXT
    )""")
    _create_monitor_tables(connection)
    connection.commit()
    return connection


# ---------------------------------------------------------------------------
# Automatic district monitoring (additive tables; existing tables unchanged)
# ---------------------------------------------------------------------------
def _create_monitor_tables(connection: sqlite3.Connection) -> None:
    connection.executescript("""
    CREATE TABLE IF NOT EXISTS district_monitor_state (
        district TEXT PRIMARY KEY,
        last_acquisition TEXT,          -- newest processed Sentinel-1 acquisition (UTC ISO)
        last_check_at TEXT,
        last_success_at TEXT,
        last_status TEXT,
        last_error TEXT
    );
    CREATE TABLE IF NOT EXISTS district_slices (
        district TEXT NOT NULL,
        acquisition_date TEXT NOT NULL,
        relative_orbit INTEGER NOT NULL,
        orbit TEXT NOT NULL,
        time_utc TEXT,
        phase TEXT,                      -- background | monitoring
        flood_ha REAL, crop_ha REAL, valid_ha REAL,
        baseline_images INTEGER, dual_pol INTEGER, frames INTEGER,
        quality TEXT,
        processed_at TEXT NOT NULL,
        UNIQUE(district,acquisition_date,relative_orbit,orbit)
    );
    CREATE TABLE IF NOT EXISTS flood_events (
        event_id TEXT PRIMARY KEY,
        district TEXT NOT NULL,
        detected_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        first_detection TEXT, peak_date TEXT, peak_orbit TEXT, peak_relative_orbit INTEGER,
        before_date TEXT, after_date TEXT, acquisitions_json TEXT,
        flood_ha REAL, agricultural_exposure_ha REAL,
        crop_types_json TEXT, ndvi_json TEXT,
        priority TEXT, priority_score REAL, confidence TEXT, score REAL,
        event_status TEXT,               -- Detected flood event | Possible flood event | Rejected – not a flood
        processing_status TEXT,          -- screened | analyzed | analysis_failed
        verification_status TEXT NOT NULL DEFAULT 'Unverified',
        data_refs_json TEXT, event_json TEXT, detail_json TEXT, error TEXT
    );
    CREATE INDEX IF NOT EXISTS flood_events_district ON flood_events(district,peak_date);
    CREATE TABLE IF NOT EXISTS monitor_checks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        started_at TEXT NOT NULL, finished_at TEXT,
        trigger TEXT, status TEXT, new_slices INTEGER, events_created INTEGER, events_updated INTEGER,
        message TEXT, details_json TEXT
    );
    CREATE TABLE IF NOT EXISTS monitor_alerts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at TEXT NOT NULL, event_id TEXT, district TEXT, message TEXT NOT NULL,
        acknowledged INTEGER NOT NULL DEFAULT 0,
        UNIQUE(event_id,message)
    );
    CREATE TABLE IF NOT EXISTS catalog_analyses (
        event_id TEXT NOT NULL, district TEXT NOT NULL, computed_at TEXT NOT NULL, status TEXT,
        result_json TEXT NOT NULL, PRIMARY KEY(event_id,district)
    );
    CREATE TABLE IF NOT EXISTS monitor_service (
        id INTEGER PRIMARY KEY CHECK (id=1),
        lock_owner TEXT, lock_expires_at TEXT,
        pid INTEGER, started_at TEXT, heartbeat_at TEXT, interval_hours REAL, next_run_at TEXT
    );
    """)

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()

def get_monitor_state(path=None) -> dict[str, dict]:
    with _connect(path) as c:
        return {r["district"]:dict(r) for r in c.execute("SELECT * FROM district_monitor_state")}

def update_monitor_state(district: str, path=None, **fields) -> None:
    with _connect(path) as c:
        c.execute("INSERT OR IGNORE INTO district_monitor_state(district) VALUES (?)",(district,))
        if fields:
            c.execute(f"UPDATE district_monitor_state SET {','.join(f'{k}=?' for k in fields)} WHERE district=?",
                      (*fields.values(),district))

def processed_slice_keys(district: str, path=None) -> set[tuple[str,int,str]]:
    with _connect(path) as c:
        return {(r["acquisition_date"],int(r["relative_orbit"]),r["orbit"]) for r in
                c.execute("SELECT acquisition_date,relative_orbit,orbit FROM district_slices WHERE district=?",(district,))}

def save_slices(rows: list[dict], path=None) -> int:
    """Insert measured slices; already-stored acquisitions are ignored (no duplicates)."""
    inserted=0
    with _connect(path) as c:
        for r in rows:
            cur=c.execute("""INSERT OR IGNORE INTO district_slices (district,acquisition_date,relative_orbit,orbit,time_utc,phase,
                flood_ha,crop_ha,valid_ha,baseline_images,dual_pol,frames,quality,processed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (r["district"],str(r["date"])[:10],int(r["relative_orbit"]),r["orbit"],r.get("time_utc"),r.get("phase"),
                 r.get("flood_ha"),r.get("crop_ha"),r.get("valid_ha"),r.get("baseline_images"),int(bool(r.get("dual_pol"))),
                 r.get("frames"),r.get("quality"),_now()))
            inserted+=cur.rowcount
    return inserted

def district_slices(district: str, since: str | None = None, path=None) -> list[dict]:
    query="SELECT * FROM district_slices WHERE district=?"+(" AND acquisition_date>=?" if since else "")+" ORDER BY acquisition_date"
    with _connect(path) as c:
        rows=[dict(r) for r in c.execute(query,(district,since) if since else (district,))]
    for r in rows: r["date"]=r["acquisition_date"]
    return rows

def find_overlapping_event(district: str, first_detection: str, peak_date: str, gap_days: int = 14, path=None) -> dict | None:
    """An existing event of the same district whose detection period is within ``gap_days``."""
    with _connect(path) as c:
        row=c.execute("""SELECT * FROM flood_events WHERE district=? AND
            julianday(?) <= julianday(peak_date)+? AND julianday(?) >= julianday(first_detection)-?
            ORDER BY peak_date DESC LIMIT 1""",(district,first_detection,gap_days,peak_date,gap_days)).fetchone()
    return dict(row) if row else None

def upsert_flood_event(record: dict, path=None) -> tuple[str,bool]:
    """Create an event, or update the overlapping one for the same district. Returns (event_id, created)."""
    existing=find_overlapping_event(record["district"],record["first_detection"],record["peak_date"],path=path)
    columns=["district","first_detection","peak_date","peak_orbit","peak_relative_orbit","before_date","after_date",
             "acquisitions_json","flood_ha","agricultural_exposure_ha","crop_types_json","ndvi_json","priority",
             "priority_score","confidence","score","event_status","processing_status","data_refs_json",
             "event_json","detail_json","error"]
    values=[record.get(k) for k in columns]
    with _connect(path) as c:
        if existing:
            event_id=existing["event_id"]
            # Keep the earliest first detection; never overwrite a ground-verification result.
            values[columns.index("first_detection")]=min(existing["first_detection"],record["first_detection"])
            c.execute(f"UPDATE flood_events SET {','.join(f'{k}=?' for k in columns)},updated_at=? WHERE event_id=?",
                      (*values,_now(),event_id))
            return event_id,False
        event_id=f"{record['district'].replace(' ','').replace('.','')[:12].upper()}-{record['peak_date']}-{record.get('peak_relative_orbit')}"
        c.execute(f"INSERT INTO flood_events (event_id,detected_at,updated_at,{','.join(columns)}) VALUES (?,?,?{',?'*len(columns)})",
                  (event_id,_now(),_now(),*values))
        return event_id,True

def list_flood_events(district: str | None = None, limit: int = 200, path=None) -> list[dict]:
    query="SELECT * FROM flood_events"+(" WHERE district=?" if district else "")+" ORDER BY peak_date DESC,updated_at DESC LIMIT ?"
    with _connect(path) as c:
        rows=[dict(r) for r in c.execute(query,(district,limit) if district else (limit,))]
    for r in rows:
        for k in ("acquisitions_json","crop_types_json","ndvi_json","data_refs_json","event_json","detail_json"):
            r[k[:-5]]=json.loads(r.pop(k)) if r.get(k) else None
    return rows

def set_verification_status(event_id: str, status: str, path=None) -> None:
    with _connect(path) as c:
        c.execute("UPDATE flood_events SET verification_status=?,updated_at=? WHERE event_id=?",(status,_now(),event_id))

def add_alert(event_id: str, district: str, message: str, path=None) -> bool:
    """Store an alert once per (event, message). Returns True when it is new."""
    with _connect(path) as c:
        return c.execute("INSERT OR IGNORE INTO monitor_alerts(created_at,event_id,district,message) VALUES (?,?,?,?)",
                         (_now(),event_id,district,message)).rowcount==1

def list_alerts(unacknowledged_only: bool = False, limit: int = 50, path=None) -> list[dict]:
    query="SELECT * FROM monitor_alerts"+(" WHERE acknowledged=0" if unacknowledged_only else "")+" ORDER BY created_at DESC LIMIT ?"
    with _connect(path) as c:
        return [dict(r) for r in c.execute(query,(limit,))]

def acknowledge_alerts(path=None) -> None:
    with _connect(path) as c: c.execute("UPDATE monitor_alerts SET acknowledged=1")

def start_check(trigger: str, path=None) -> int:
    with _connect(path) as c:
        return int(c.execute("INSERT INTO monitor_checks(started_at,trigger,status) VALUES (?,?,'running')",(_now(),trigger)).lastrowid)

def finish_check(check_id: int, status: str, path=None, **fields) -> None:
    details=fields.pop("details",None)
    with _connect(path) as c:
        c.execute("""UPDATE monitor_checks SET finished_at=?,status=?,new_slices=?,events_created=?,events_updated=?,
                  message=?,details_json=? WHERE id=?""",(_now(),status,fields.get("new_slices"),fields.get("events_created"),
                  fields.get("events_updated"),fields.get("message"),json.dumps(details,default=str) if details else None,check_id))

def list_checks(limit: int = 50, path=None) -> list[dict]:
    with _connect(path) as c:
        return [dict(r) for r in c.execute("SELECT * FROM monitor_checks ORDER BY id DESC LIMIT ?",(limit,))]

def acquire_monitor_lock(owner: str, ttl_minutes: float = 120, path=None) -> bool:
    """Database lock so two monitoring runs (service, button, second process) never overlap.

    A lock older than ``ttl_minutes`` is treated as abandoned (e.g. a crashed process).
    """
    from datetime import timedelta
    now=datetime.now(timezone.utc)
    with _connect(path) as c:
        c.execute("BEGIN IMMEDIATE")
        c.execute("INSERT OR IGNORE INTO monitor_service(id) VALUES (1)")
        row=c.execute("SELECT lock_owner,lock_expires_at FROM monitor_service WHERE id=1").fetchone()
        if row["lock_owner"] and row["lock_expires_at"] and datetime.fromisoformat(row["lock_expires_at"])>now:
            c.execute("COMMIT"); return False
        c.execute("UPDATE monitor_service SET lock_owner=?,lock_expires_at=? WHERE id=1",
                  (owner,(now+timedelta(minutes=ttl_minutes)).isoformat()))
        c.execute("COMMIT"); return True

def release_monitor_lock(owner: str, path=None) -> None:
    with _connect(path) as c:
        c.execute("UPDATE monitor_service SET lock_owner=NULL,lock_expires_at=NULL WHERE id=1 AND lock_owner=?",(owner,))

def update_service_status(path=None, **fields) -> None:
    with _connect(path) as c:
        c.execute("INSERT OR IGNORE INTO monitor_service(id) VALUES (1)")
        if fields: c.execute(f"UPDATE monitor_service SET {','.join(f'{k}=?' for k in fields)} WHERE id=1",tuple(fields.values()))

def save_catalog_analysis(result: dict, path=None) -> None:
    with _connect(path) as c:
        c.execute("INSERT OR REPLACE INTO catalog_analyses(event_id,district,computed_at,status,result_json) VALUES (?,?,?,?,?)",
                  (result["event_id"],result["district"],result["computed_at"],result.get("status"),json.dumps(result,default=str)))

def catalog_analyses(event_id: str | None = None, path=None) -> dict[tuple[str,str],dict]:
    query="SELECT result_json FROM catalog_analyses"+(" WHERE event_id=?" if event_id else "")
    with _connect(path) as c:
        rows=c.execute(query,(event_id,) if event_id else ()).fetchall()
    out={}
    for (raw,) in rows:
        r=json.loads(raw); out[(r["event_id"],r["district"])]=r
    return out

def service_status(path=None) -> dict:
    with _connect(path) as c:
        c.execute("INSERT OR IGNORE INTO monitor_service(id) VALUES (1)")
        return dict(c.execute("SELECT * FROM monitor_service WHERE id=1").fetchone())


def _json(value: Any) -> str | None:
    return None if value is None else json.dumps(value,default=str,sort_keys=True)


def save_analysis(record: dict, path: str | Path | None = None) -> int:
    """Idempotently save a real analysis record and return its local database ID."""
    summary=record.get("summary") or {}
    event=record.get("event") or {}
    key_data={"location":record.get("location"),"geometry":record.get("geometry"),
              "summary":summary,"event":event}
    analysis_key=hashlib.sha256(_json(key_data).encode("utf-8")).hexdigest()
    before=record.get("before") or {}; after=record.get("after") or {}
    with _connect(path) as connection:
        connection.execute("""INSERT OR IGNORE INTO analysis_history
            (analysis_key,created_at,location,requested_before,requested_after,
             acquired_before,acquired_after,sensor,flood_ha,agricultural_exposure_ha,
             confidence,geometry_json,summary_json,event_json)
             VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (analysis_key,datetime.now(timezone.utc).isoformat(),record.get("location"),
             str(record.get("requested_before")) if record.get("requested_before") else None,
             str(record.get("requested_after")) if record.get("requested_after") else None,
             str(before.get("time_utc") or before.get("date")) if before else None,
             str(after.get("time_utc") or after.get("date")) if after else None,
             summary.get("sensor"),summary.get("flood_ha"),summary.get("ag_ha"),
             summary.get("confidence"),_json(record.get("geometry")),_json(summary),_json(event)))
        row=connection.execute("SELECT id FROM analysis_history WHERE analysis_key=?",(analysis_key,)).fetchone()
        return int(row["id"])


def list_analyses(limit: int = 200, path: str | Path | None = None) -> list[dict]:
    with _connect(path) as connection:
        rows=connection.execute("SELECT * FROM analysis_history ORDER BY created_at DESC,id DESC LIMIT ?",(int(limit),)).fetchall()
    results=[]
    for row in rows:
        item=dict(row)
        item["summary"]=json.loads(item.pop("summary_json") or "{}")
        item["event"]=json.loads(item.pop("event_json") or "{}")
        item["geometry"]=json.loads(item.pop("geometry_json") or "null")
        results.append(item)
    return results


def save_monitoring_run(result: dict, path: str | Path | None = None) -> int:
    """Store one automatic KPK monitoring run (including failed/no-event runs)."""
    window=result.get("monitoring_window") or [None,None]
    with _connect(path) as connection:
        cursor=connection.execute("""INSERT INTO monitoring_runs
            (refreshed_at,window_start,window_end,latest_acquisition,status,result_json)
            VALUES (?,?,?,?,?,?)""",
            (result.get("refreshed_at") or datetime.now(timezone.utc).isoformat(),
             str(window[0]) if window[0] else None,str(window[1]) if window[1] else None,
             str(result.get("latest_acquisition")) if result.get("latest_acquisition") else None,
             result.get("status","unknown"),_json(result)))
        return int(cursor.lastrowid)


def latest_monitoring_run(path: str | Path | None = None, successful_only: bool = True) -> dict | None:
    query="SELECT * FROM monitoring_runs"+(" WHERE status!='error'" if successful_only else "")+" ORDER BY refreshed_at DESC,id DESC LIMIT 1"
    with _connect(path) as connection:
        row=connection.execute(query).fetchone()
    if not row: return None
    result=json.loads(row["result_json"]); result["run_id"]=int(row["id"])
    return result


def list_monitoring_runs(limit: int = 50, path: str | Path | None = None) -> list[dict]:
    with _connect(path) as connection:
        rows=connection.execute("SELECT id,refreshed_at,window_start,window_end,latest_acquisition,status FROM monitoring_runs ORDER BY refreshed_at DESC,id DESC LIMIT ?",(int(limit),)).fetchall()
    return [dict(row) for row in rows]


def save_field_observation(observation: dict, path: str | Path | None = None) -> int:
    fields=("event_key","district","latitude","longitude","observed_on","flooding_observed",
            "crop","damage_observed","observer","notes")
    with _connect(path) as connection:
        cursor=connection.execute(f"INSERT INTO field_observations (created_at,{','.join(fields)}) VALUES (?{',?'*len(fields)})",
            (datetime.now(timezone.utc).isoformat(),*[None if observation.get(f) is None else str(observation.get(f)) if f not in ("latitude","longitude") else float(observation[f]) for f in fields]))
        return int(cursor.lastrowid)


def list_field_observations(limit: int = 500, path: str | Path | None = None) -> list[dict]:
    with _connect(path) as connection:
        rows=connection.execute("SELECT * FROM field_observations ORDER BY created_at DESC,id DESC LIMIT ?",(int(limit),)).fetchall()
    return [dict(row) for row in rows]


def history_totals(path: str | Path | None = None) -> dict:
    with _connect(path) as connection:
        row=connection.execute("""SELECT COUNT(*) AS runs,
            COALESCE(SUM(flood_ha),0) AS flood_ha,
            COALESCE(SUM(agricultural_exposure_ha),0) AS agricultural_ha
            FROM analysis_history""").fetchone()
    return {"runs":int(row["runs"]),"flood_ha":float(row["flood_ha"]),
            "agricultural_ha":float(row["agricultural_ha"])}
