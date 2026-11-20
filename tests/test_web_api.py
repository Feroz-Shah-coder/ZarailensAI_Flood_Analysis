"""Web API tests (no Earth Engine calls): a fresh database is seeded like on a new Vercel instance."""
import pytest
from fastapi.testclient import TestClient

from config.settings import settings
from server import storage
import app as entry


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "analysis_history_db", tmp_path / "fresh.sqlite3")
    monkeypatch.setattr(settings, "admin_token", None)
    monkeypatch.delenv("KV_REST_API_URL", raising=False); monkeypatch.delenv("UPSTASH_REDIS_REST_URL", raising=False)
    storage._state.update(ready=False, version=None)
    with TestClient(entry.app) as c:
        yield c
    storage._state.update(ready=False, version=None)


def test_entry_point_exports_fastapi_app_for_vercel():
    from fastapi import FastAPI
    assert isinstance(entry.app, FastAPI)


def test_web_app_and_assets_are_served(client):
    r = client.get("/")
    assert r.status_code == 200 and "ZarailensAI" in r.text and "/assets/app.js" in r.text
    assert client.get("/assets/app.js").status_code == 200
    assert client.get("/assets/../server/main.py").status_code == 404


def test_fresh_instance_imports_shipped_results(client):
    data = client.get("/api/catalog").json()
    assert len(data["events"]) == 22
    analysed = [a for e in data["events"] for a in e["analyses"]]
    assert analysed, "seed results should be imported on a fresh database"
    assert client.get("/api/dashboard").json()["run"] is not None


def test_meta_lists_kpk_districts(client):
    m = client.get("/api/meta").json()
    assert len(m["districts"]) == 35 and "Nowshera" in m["districts"]
    assert m["params"]["change_db"] == settings.s1_change_db


def test_screen_events_scores_rows_without_earth_engine(client):
    rows = [{"date": f"2022-07-{d:02d}", "district": "Tank", "orbit": "ASCENDING", "relative_orbit": 100, "phase": "background",
             "flood_ha": 20.0, "crop_ha": 5.0, "valid_ha": 100000.0, "baseline_images": 5} for d in (1, 13, 25)]
    rows.append({"date": "2022-08-25", "district": "Tank", "orbit": "ASCENDING", "relative_orbit": 100, "phase": "monitoring",
                 "flood_ha": 5000.0, "crop_ha": 2000.0, "valid_ha": 100000.0, "baseline_images": 5})
    out = client.post("/api/screen/events", json={"rows": rows}).json()
    assert out["events"] and out["events"][0]["district"] == "Tank"
    assert out["district_summary"][0]["max_excess_ha"] > 4000


def test_field_observation_validation_and_csv(client):
    bad = client.post("/api/field", json={"latitude": 10, "longitude": 71})
    assert bad.status_code == 400
    ok = client.post("/api/field", json={"district": "Swat", "latitude": 34.8, "longitude": 72.4, "observed_on": "2026-11-20",
                                         "flooding_observed": "No", "crop": "Maize", "damage_observed": "None"})
    assert ok.status_code == 200
    assert "Swat" in client.get("/api/field.csv").text


def test_analyst_token_protects_runs_and_writes(client, monkeypatch):
    monkeypatch.setattr(settings, "admin_token", "secret-token")
    body = {"district": "Swat", "latitude": 34.8, "longitude": 72.4}
    assert client.post("/api/field", json=body).status_code == 401
    assert client.post("/api/screen/plan", json={"start": "2022-08-01", "end": "2022-08-10"}).status_code == 401
    assert client.post("/api/field", json=body, headers={"x-admin-token": "secret-token"}).status_code == 200


def test_cron_requires_secret(client):
    assert client.get("/api/cron/monitor").status_code == 401


def test_report_for_seeded_reported_event(client):
    events = client.get("/api/catalog").json()["events"]
    e, a = next((e, a) for e in events for a in e["analyses"] if a["headline"])
    r = client.post("/api/report/pdf", json={"event_id": e["id"], "district": a["district"]})
    assert r.status_code == 200 and r.content[:4] == b"%PDF"
    x = client.post("/api/report/xlsx", json={"event_id": e["id"], "district": a["district"]})
    assert x.status_code == 200 and x.content[:2] == b"PK"


def test_durable_sync_restores_database_on_a_new_instance(tmp_path, monkeypatch):
    """Simulated Upstash store: data written by one instance is loaded by the next (empty /tmp)."""
    kv = {}
    def cmd(*args):
        op, key = args[0], args[1]
        if op == "GET": return kv.get(key)
        if op == "SET": kv[key] = args[2]; return "OK"
        if op == "INCR": kv[key] = str(int(kv.get(key) or 0) + 1); return int(kv[key])
    monkeypatch.setattr(storage, "_kv", lambda: ("https://kv.example", "t"))
    monkeypatch.setattr(storage, "_cmd", cmd)
    monkeypatch.setattr(settings, "admin_token", None)
    from src import analysis_history as store
    # instance 1
    monkeypatch.setattr(settings, "analysis_history_db", tmp_path / "a" / "db.sqlite3")
    storage._state.update(ready=False, version=None)
    with TestClient(entry.app) as c:
        assert c.post("/api/field", json={"district": "Swat", "latitude": 34.8, "longitude": 72.4}).status_code == 200
    # instance 2 starts with an empty /tmp
    monkeypatch.setattr(settings, "analysis_history_db", tmp_path / "b" / "db.sqlite3")
    storage._state.update(ready=False, version=None)
    with TestClient(entry.app) as c:
        assert [o["district"] for o in c.get("/api/field").json()] == ["Swat"]
    storage._state.update(ready=False, version=None)
