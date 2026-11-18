import base64
import hashlib
import json
import time
from contextlib import asynccontextmanager

import httpx
from fastapi.testclient import TestClient

from tracker.codex import USAGE_URL
from tracker.codex_login import DEVICE_URL
from tracker.config import Settings
from tracker.db import WindowReading
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
        page = client.get("/")
        script = client.get("/static/app.js")
        assert page.status_code == script.status_code == 200
        script_hash = hashlib.sha256(script.content).hexdigest()[:16]
        assert f'src="/static/app.js?v={script_hash}"' in page.text
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


def test_codex_poll_and_history_are_separate(tmp_path, monkeypatch):
    monkeypatch.delenv("DEMO_MODE", raising=False)
    auth_file = tmp_path / "auth.json"
    auth_file.write_text(json.dumps({"tokens": {"access_token": "first", "account_id": "test-account"}}))
    app = create_app(make_settings(tmp_path, codex_auth_file=str(auth_file)))
    now = time.time()
    app.state.db.record_poll(now, [WindowReading("five_hour", 55, now + 100)])
    requests = []

    async def get_usage(self, url, *, headers, timeout):
        assert url == USAGE_URL
        assert headers["ChatGPT-Account-ID"] == "test-account"
        requests.append(headers["Authorization"])
        return httpx.Response(200, json={
            "rate_limit": {
                "primary_window": {"limit_window_seconds": 604800, "used_percent": 12, "reset_at": int(now + 86400)},
                "secondary_window": None,
            },
            "additional_rate_limits": [{
                "metered_feature": "review",
                "rate_limit": {
                    "primary_window": {"limit_window_seconds": 18000, "used_percent": 30, "reset_at": int(now + 100)},
                },
            }],
        })

    monkeypatch.setattr(httpx.AsyncClient, "get", get_usage)
    with TestClient(app) as client:
        assert client.get("/api/status?provider=codex").json()["auth"]["connected"]
        assert client.post("/api/auth/codex/login").status_code == 400
        assert client.get("/api/status?provider=codex").json()["current"] is None
        assert client.post("/api/poll?provider=codex").json() == {"ok": True, "windows": 2}
        auth_file.write_text(json.dumps({"tokens": {"access_token": "second", "account_id": "test-account"}}))
        assert client.post("/api/poll?provider=codex").json()["ok"]
        assert requests == ["Bearer first", "Bearer second"]

        status = client.get("/api/status?provider=codex").json()
        windows = {window["window"]: window["utilization"] for window in status["current"]["windows"]}
        assert windows == {"seven_day": 12, "codex:review:five_hour": 30}
        assert status["polls"]["ok"] == 2
        assert client.get("/api/status").json()["polls"]["ok"] == 1

        dashboard = client.get("/api/dashboard?provider=codex").json()
        assert dashboard["weeks"] and not dashboard["sessions"]
        assert set(dashboard["timeseries"]) == set(windows)
        assert client.get("/api/dashboard").json()["sessions"][0]["peak"] == 55

        export = client.get("/api/export/samples.csv?provider=codex")
        assert "codex-usage-samples.csv" in export.headers["Content-Disposition"]
        assert ",seven_day,12.0," in export.text
        assert ",seven_day," not in client.get("/api/export/samples.csv").text
        assert client.get("/api/status?provider=other").status_code == 422


def test_codex_device_login_and_refresh(tmp_path, monkeypatch):
    monkeypatch.delenv("DEMO_MODE", raising=False)
    calls = []
    auth_file = tmp_path / "codex" / "auth.json"

    def save_auth(token):
        auth_file.parent.mkdir(exist_ok=True)
        auth_file.write_text(json.dumps({"tokens": {"access_token": token}}))

    class Server:
        async def request(self, method, params):
            calls.append((method, params))
            if method == "account/login/start":
                return {"verificationUrl": DEVICE_URL, "userCode": "ABCD-EFGH", "loginId": "test-login"}
            assert method == "account/read"
            save_auth("refreshed")
            return {}

        async def receive(self):
            save_auth("expired")
            return {
                "method": "account/login/completed",
                "params": {"loginId": "test-login", "success": True},
            }

    @asynccontextmanager
    async def fake_app_server(home):
        assert home == auth_file.parent
        yield Server()

    async def get_usage(self, url, *, headers, timeout):
        assert url == USAGE_URL
        if headers["Authorization"] == "Bearer expired":
            return httpx.Response(401)
        assert headers["Authorization"] == "Bearer refreshed"
        return httpx.Response(200, json={"rate_limit": None})

    monkeypatch.setattr("tracker.codex_login.app_server", fake_app_server)
    monkeypatch.setattr(httpx.AsyncClient, "get", get_usage)
    with TestClient(create_app(make_settings(tmp_path, codex_auth_file=""))) as client:
        assert not client.get("/api/status?provider=codex").json()["auth"]["connected"]
        response = client.post("/api/auth/codex/login")
        assert response.status_code == 200
        assert response.headers["Cache-Control"] == "no-store"
        login = response.json()
        assert login["url"] == DEVICE_URL
        assert login["code"] == "ABCD-EFGH"
        assert client.get(f"/api/auth/codex/login/{login['attempt']}").json() == {"status": "connected"}
        assert client.get("/api/status?provider=codex").json()["auth"]["connected"]
        assert client.post("/api/poll?provider=codex").json()["ok"]
        assert calls == [
            ("account/login/start", {"type": "chatgptDeviceCode"}),
            ("account/read", {"refreshToken": True}),
        ]

    with TestClient(create_app(make_settings(tmp_path, codex_auth_file=""))) as client:
        assert client.get("/api/status?provider=codex").json()["auth"]["connected"]
