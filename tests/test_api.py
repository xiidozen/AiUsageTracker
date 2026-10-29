import base64

from fastapi.testclient import TestClient

from tracker.config import Settings
from tracker.main import create_app


def make_settings(tmp_path, **kw):
    s = Settings()
    s.data_dir = tmp_path
    s.poller_enabled = False
    s.static_token = ""
    s.credentials_file = ""
    for k, v in kw.items():
        setattr(s, k, v)
    return s


def test_demo_dashboard(tmp_path, monkeypatch):
    monkeypatch.setenv("DEMO_MODE", "true")
    with TestClient(create_app(make_settings(tmp_path))) as client:
        status = client.get("/api/status").json()
        assert status["demo"] and status["polls"]["ok"] > 1000
        dash = client.get("/api/dashboard?days=30").json()
        assert dash["sessions"] and dash["weeks"]
        assert set(dash["timeseries"]) >= {"five_hour", "seven_day"}
        assert dash["summary"]["estimate"]["all_sessions"]["sessions_to_weekly"] > 0
        assert client.get("/api/dashboard?days=0").status_code == 200
        assert client.get("/").status_code == 200
        assert client.get("/static/app.js").status_code == 200
        assert client.get("/api/export/sessions.csv").text.startswith("start,end,peak")
        assert client.post("/api/poll").status_code == 400


def test_empty_database_and_not_connected(tmp_path, monkeypatch):
    monkeypatch.delenv("DEMO_MODE", raising=False)
    with TestClient(create_app(make_settings(tmp_path))) as client:
        status = client.get("/api/status").json()
        assert status["auth"]["connected"] is False and status["current"] is None
        dash = client.get("/api/dashboard").json()
        assert dash["sessions"] == [] and dash["summary"]["sessions"]["used"] == 0
        assert client.post("/api/auth/import", json={"text": "{}"}).status_code == 400
        assert client.post("/api/auth/import", json={"text": "[1, 2]"}).status_code == 400
        login = client.post("/api/auth/login").json()
        assert login["url"].startswith("https://")


def test_basic_auth(tmp_path, monkeypatch):
    monkeypatch.delenv("DEMO_MODE", raising=False)
    with TestClient(create_app(make_settings(tmp_path, ui_password="s3cret"))) as client:
        assert client.get("/healthz").status_code == 200
        assert client.get("/api/status").status_code == 401
        good = base64.b64encode(b"admin:s3cret").decode()
        assert client.get("/api/status", headers={"Authorization": f"Basic {good}"}).status_code == 200
        bad = base64.b64encode(b"admin:nope").decode()
        assert client.get("/api/status", headers={"Authorization": f"Basic {bad}"}).status_code == 401
