"""Read Codex subscription limits using an existing ChatGPT CLI sign-in."""

from __future__ import annotations

import json
import math
from pathlib import Path

import httpx

from .db import WindowReading
from .oauth import AuthError
from .usage_api import UsageError

USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
WINDOW_NAMES = {18000: "five_hour", 604800: "seven_day"}


class CodexCredentials:
    def __init__(self, path: str | Path, *, refresh=None):
        self.path = Path(path)
        self.refresh = refresh
        self.last_error: str | None = None

    def current(self) -> dict | None:
        try:
            body = json.loads(self.path.read_text())
        except FileNotFoundError:
            if self.refresh is not None:
                return None
            raise AuthError("Could not find CODEX_AUTH_FILE. Check the path and file permissions.") from None
        except (OSError, ValueError) as exc:
            raise AuthError("Could not read the Codex sign-in. Check the file and its permissions.") from exc
        tokens = body.get("tokens") if isinstance(body, dict) else None
        token = tokens.get("access_token") if isinstance(tokens, dict) else None
        if not isinstance(token, str) or not token.strip():
            raise AuthError("CODEX_AUTH_FILE needs a Codex CLI sign-in with ChatGPT.")
        return tokens

    def status(self) -> dict:
        try:
            connected = self.current() is not None
            error = self.last_error
        except AuthError as exc:
            connected, error = False, str(exc)
        return {
            "connected": connected,
            "source": "codex_managed" if self.refresh else "codex_file",
            "can_refresh": self.refresh is not None,
            "error": error,
        }

    async def fetch_usage(self, client: httpx.AsyncClient) -> dict:
        async def get():
            tokens = self.current()
            if tokens is None:
                raise AuthError("Sign in to ChatGPT from the Codex account dialog.")
            headers = {"Authorization": f"Bearer {tokens['access_token']}"}
            account = tokens.get("account_id")
            if isinstance(account, str) and account:
                headers["ChatGPT-Account-ID"] = account
            return await client.get(USAGE_URL, headers=headers, timeout=30)

        try:
            response = await get()
            if response.status_code == 401 and self.refresh is not None:
                await self.refresh()
                response = await get()
        except (httpx.HTTPError, UnicodeError) as exc:
            raise UsageError("Could not request usage from ChatGPT.") from exc
        if response.status_code in (401, 403):
            raise AuthError(f"ChatGPT rejected the Codex sign-in (HTTP {response.status_code}). Sign in again.")
        if response.status_code != 200:
            raise UsageError(f"ChatGPT usage returned HTTP {response.status_code}.")
        try:
            body = response.json()
        except ValueError as exc:
            raise UsageError("ChatGPT usage returned invalid JSON.") from exc
        if not isinstance(body, dict) or not {"rate_limit", "rate_limits", "additional_rate_limits"}.intersection(body):
            raise UsageError("ChatGPT usage returned an unexpected response.")
        return body


def parse_usage(body: dict) -> list[WindowReading]:
    readings: dict[str, WindowReading] = {}

    def add(group: object, prefix: str = "") -> None:
        if not isinstance(group, dict):
            return
        for slot in ("primary_window", "secondary_window"):
            window = group.get(slot)
            if not isinstance(window, dict):
                continue
            seconds = window.get("limit_window_seconds")
            used = window.get("used_percent")
            reset = window.get("reset_at")
            if type(seconds) is not int or seconds <= 0:
                continue
            if type(used) not in (int, float) or not 0 <= used <= 100:
                continue
            if type(reset) not in (int, float) or not math.isfinite(reset):
                reset = None
            name = prefix + WINDOW_NAMES.get(seconds, f"{seconds}s")
            readings.setdefault(name, WindowReading(name, float(used), reset))

    add(body.get("rate_limit") or body.get("rate_limits"))
    add(body.get("code_review_rate_limit"), "code_review:")
    additional = body.get("additional_rate_limits")
    if isinstance(additional, list):
        for group in additional:
            if isinstance(group, dict):
                feature = group.get("metered_feature") or group.get("limit_name")
                if isinstance(feature, str) and feature:
                    add(group.get("rate_limit"), f"codex:{feature}:")
    return list(readings.values())
