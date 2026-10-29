"""Client for Claude's plan usage endpoint (the data behind Claude Code's /usage).

Response shape, as of late 2026::

    {
      "five_hour":        {"utilization": 42.0, "resets_at": "2026-10-29T10:00:00+00:00"},
      "seven_day":        {"utilization": 17.0, "resets_at": "2026-11-02T18:00:00+00:00"},
      "seven_day_opus":   {"utilization": 0.0,  "resets_at": null},
      "seven_day_sonnet": null,
      "extra_usage":      {"is_enabled": false, ...},
      "limits": [{"kind": ..., "group": ..., "percent": 12, "resets_at": ..., "scope": {...}}, ...]
    }

Any top-level object with a ``utilization`` field is stored as a window, so new
windows are picked up without a code change. Rows in ``limits[]`` are stored
too, under ``limits:<kind>:<group>[:<scope>]``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

import httpx

from . import __version__
from .db import WindowReading
from .oauth import AuthError, CredentialStore

OAUTH_BETA = "oauth-2025-04-20"


class UsageError(Exception):
    pass


def parse_timestamp(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return float(value) / 1000.0 if value > 1e11 else float(value)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _limit_key(row: dict) -> str:
    parts = ["limits", str(row.get("kind") or "?"), str(row.get("group") or "?")]
    scope = row.get("scope") or {}
    for name in ("model", "surface"):
        label = (scope.get(name) or {}).get("display_name") if isinstance(scope.get(name), dict) else None
        if label:
            parts.append(str(label))
    return ":".join(parts)


def parse_usage(body: dict) -> list[WindowReading]:
    readings: list[WindowReading] = []
    for key, value in body.items():
        if isinstance(value, dict) and "utilization" in value and key != "extra_usage":
            readings.append(WindowReading(key, _number(value.get("utilization")), parse_timestamp(value.get("resets_at"))))
    limits = body.get("limits")
    if isinstance(limits, list):
        seen: set[str] = set()
        for row in limits:
            if not isinstance(row, dict):
                continue
            key = _limit_key(row)
            if key in seen:
                continue
            seen.add(key)
            readings.append(WindowReading(key, _number(row.get("percent")), parse_timestamp(row.get("resets_at"))))
    return readings


async def fetch_usage(client: httpx.AsyncClient, credentials: CredentialStore, url: str) -> dict:
    async def get(token: str) -> httpx.Response:
        return await client.get(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "anthropic-beta": OAUTH_BETA,
                "Content-Type": "application/json",
                "User-Agent": f"ai-usage-tracker/{__version__}",
            },
            timeout=30,
        )

    token = await credentials.access_token(client)
    try:
        resp = await get(token)
        if resp.status_code == 401:
            # The token may have been revoked or rotated early; refresh once.
            token = await credentials.access_token(client, force_refresh=True)
            resp = await get(token)
    except httpx.HTTPError as exc:
        raise UsageError(f"Request failed: {exc}") from exc

    if resp.status_code in (401, 403):
        hint = ""
        if resp.status_code == 403:
            hint = " The token may be missing the user:profile scope (setup-token tokens are inference-only)."
        raise AuthError(f"Usage endpoint rejected the token (HTTP {resp.status_code}).{hint} {resp.text[:200]}")
    if resp.status_code != 200:
        raise UsageError(f"Usage endpoint returned HTTP {resp.status_code}: {resp.text[:300]}")
    try:
        body = resp.json()
    except ValueError as exc:
        raise UsageError("Usage endpoint returned invalid JSON") from exc
    if not isinstance(body, dict):
        raise UsageError("Usage endpoint returned an unexpected body")
    return body
