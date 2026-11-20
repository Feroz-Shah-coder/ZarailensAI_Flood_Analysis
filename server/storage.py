"""Database location, first-start seeding and optional durable sync for Vercel.

Vercel functions have a read-only file system except ``/tmp``, and ``/tmp`` is
lost when an instance stops. The SQLite database therefore lives in ``/tmp``
and is seeded from ``data/reference/catalog_results.json.gz`` (the computed
flood-event results shipped with the code).

If an Upstash Redis store is connected (Vercel → Storage → Upstash for Redis,
which sets ``KV_REST_API_URL`` / ``KV_REST_API_TOKEN``), the whole database
file is stored there (gzip) after every change and loaded on start, so new
analyses, monitoring runs and field observations are kept. A version counter
makes an instance reload the newest copy before it writes.
"""
from __future__ import annotations

import base64
import contextlib
import gzip
import logging
import os
import threading

import requests

from config.settings import settings

log=logging.getLogger(__name__)
DB_KEY="zarailens:db"; VER_KEY="zarailens:db:version"
_lock=threading.Lock()
_state={"version":None,"ready":False}

def _kv() -> tuple[str,str] | None:
    url=os.getenv("KV_REST_API_URL") or os.getenv("UPSTASH_REDIS_REST_URL")
    token=os.getenv("KV_REST_API_TOKEN") or os.getenv("UPSTASH_REDIS_REST_TOKEN")
    return (url.rstrip("/"),token) if url and token else None

def mode() -> str:
    from config.settings import ON_VERCEL
    if _kv(): return "durable (Upstash Redis)"
    return "temporary (Vercel /tmp – connect Upstash Redis to keep new results)" if ON_VERCEL else "local SQLite file"

def _cmd(*args):
    url,token=_kv()
    r=requests.post(url,json=list(args),headers={"Authorization":f"Bearer {token}"},timeout=30)
    r.raise_for_status(); return r.json().get("result")

def _pull() -> None:
    version=_cmd("GET",VER_KEY)
    if version is None or version==_state["version"]: return
    blob=_cmd("GET",DB_KEY)
    if blob:
        path=settings.analysis_history_db; path.parent.mkdir(parents=True,exist_ok=True)
        path.write_bytes(gzip.decompress(base64.b64decode(blob)))
    _state["version"]=version

def _push() -> None:
    data=base64.b64encode(gzip.compress(settings.analysis_history_db.read_bytes())).decode()
    _cmd("SET",DB_KEY,data)
    _state["version"]=str(_cmd("INCR",VER_KEY))

def ensure_ready() -> None:
    """Load the durable copy (if any) and import the shipped seed, once per instance."""
    if _state["ready"]: return
    with _lock:
        if _state["ready"]: return
        if _kv():
            try: _pull()
            except Exception as exc: log.warning("Durable database not loaded: %s",exc)
        from src.flood_catalog import import_seed
        try:
            if import_seed(settings.analysis_history_db) and _kv(): _push()
        except Exception as exc: log.warning("Seed import failed: %s",exc)
        _state["ready"]=True

@contextlib.contextmanager
def writing():
    """Wrap every database write: refresh from the durable copy first, save it back afterwards."""
    ensure_ready()
    with _lock:
        if _kv():
            try: _pull()
            except Exception as exc: log.warning("Durable database refresh failed: %s",exc)
        yield
        if _kv():
            try: _push()
            except Exception as exc: log.warning("Durable database save failed: %s",exc)
