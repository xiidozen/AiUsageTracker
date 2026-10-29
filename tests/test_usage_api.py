import asyncio
import json
import time

import httpx
import pytest

from tracker.config import Settings
from tracker.oauth import AuthError, CredentialStore
from tracker.usage_api import fetch_usage, parse_timestamp, parse_usage

BODY = {
    "five_hour": {"utilization": 42.0, "resets_at": "2026-10-29T10:00:00.123456+00:00"},
    "seven_day": {"utilization": 17, "resets_at": "2026-11-02T18:00:00Z"},
    "seven_day_opus": {"utilization": 0.0, "resets_at": None},
    "seven_day_sonnet": None,
    "extra_usage": {"is_enabled": False, "monthly_limit": None, "used_credits": None, "utilization": None},
    "limits": [
        {"kind": "weekly", "group": "model", "percent": 12, "resets_at": None, "severity": "normal",
         "is_active": True, "scope": {"model": {"display_name": "Fable"}}},
    ],
}


def settings(tmp_path, **kw):
    s = Settings()
    s.data_dir = tmp_path
    s.static_token = ""
    s.credentials_file = ""
    for k, v in kw.items():
        setattr(s, k, v)
    return s


def test_parse_usage_windows():
    readings = {r.window: r for r in parse_usage(BODY)}
    assert set(readings) == {"five_hour", "seven_day", "seven_day_opus", "limits:weekly:model:Fable"}
    assert readings["five_hour"].utilization == 42.0
    assert readings["seven_day"].resets_at == parse_timestamp("2026-11-02T18:00:00+00:00")
    assert readings["seven_day_opus"].resets_at is None
    assert readings["limits:weekly:model:Fable"].utilization == 12.0


def test_parse_timestamp_formats():
    assert parse_timestamp("2026-01-01T00:00:00Z") == parse_timestamp("2026-01-01T00:00:00+00:00")
    assert parse_timestamp(1_700_000_000_000) == 1_700_000_000.0
    assert parse_timestamp(None) is None
    assert parse_timestamp("garbage") is None


def test_import_claude_code_credentials(tmp_path):
    store = CredentialStore(settings(tmp_path))
    text = json.dumps({"claudeAiOauth": {"accessToken": "at", "refreshToken": "rt", "expiresAt": 1_900_000_000_000,
                                         "scopes": ["user:inference", "user:profile"], "subscriptionType": "max"}})
    status = store.import_credentials(text)
    assert status["connected"] and status["source"] == "import" and status["can_refresh"]
    assert status["expires_at"] == 1_900_000_000.0
    assert (tmp_path / "oauth.json").stat().st_mode & 0o777 == 0o600


def test_login_flow_and_refresh(tmp_path):
    calls = []

    def handler(request: httpx.Request):
        body = json.loads(request.content)
        calls.append(body)
        if body["grant_type"] == "authorization_code":
            return httpx.Response(200, json={"access_token": "at1", "refresh_token": "rt1", "expires_in": 1,
                                             "scope": "user:profile", "account": {"email_address": "me@example.com"}})
        assert body["grant_type"] == "refresh_token" and body["refresh_token"] == "rt1"
        return httpx.Response(200, json={"access_token": "at2", "expires_in": 28800})

    store = CredentialStore(settings(tmp_path))
    start = store.start_login()
    assert "code_challenge_method=S256" in start["url"] and "scope=user%3Aprofile" in start["url"]

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            status = await store.complete_login(client, f"thecode#{start['state']}")
            assert status["account"] == "me@example.com"
            # expires_in=1 means the token is already inside the refresh margin.
            return await store.access_token(client)

    assert asyncio.run(go()) == "at2"
    assert calls[0]["code"] == "thecode" and len(calls[0]["code_verifier"]) >= 43
    saved = json.loads((tmp_path / "oauth.json").read_text())
    assert saved["refresh_token"] == "rt1"  # kept when the refresh response omits it


def test_login_rejects_unknown_state(tmp_path):
    store = CredentialStore(settings(tmp_path))

    async def go():
        async with httpx.AsyncClient() as client:
            await store.complete_login(client, "code#not-a-real-state")

    with pytest.raises(AuthError):
        asyncio.run(go())


def test_fetch_usage_retries_after_401(tmp_path):
    seen = []

    def handler(request: httpx.Request):
        if request.url.path.endswith("/token"):
            return httpx.Response(200, json={"access_token": "fresh", "refresh_token": "rt2", "expires_in": 28800})
        seen.append(request.headers["authorization"])
        assert request.headers["anthropic-beta"] == "oauth-2025-04-20"
        if request.headers["authorization"] == "Bearer stale":
            return httpx.Response(401, json={"error": "expired"})
        return httpx.Response(200, json=BODY)

    s = settings(tmp_path)
    store = CredentialStore(s)
    store.import_credentials(json.dumps({"accessToken": "stale", "refreshToken": "rt", "expiresAt": time.time() + 3600}))

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await fetch_usage(client, store, s.usage_url)

    assert asyncio.run(go()) == BODY
    assert seen == ["Bearer stale", "Bearer fresh"]


def test_env_token_is_never_refreshed(tmp_path):
    store = CredentialStore(settings(tmp_path, static_token="envtoken"))
    assert store.status()["source"] == "env" and not store.status()["can_refresh"]

    def handler(request):
        return httpx.Response(403, text="missing scope")

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await fetch_usage(client, store, "https://example.invalid/usage")

    with pytest.raises(AuthError, match="user:profile"):
        asyncio.run(go())
