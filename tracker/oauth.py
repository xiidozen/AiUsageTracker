"""Claude OAuth credentials: sign-in (PKCE), import, refresh and storage.

The usage endpoint accepts the same OAuth access token Claude Code uses, as long
as it carries the ``user:profile`` scope. Long-lived tokens from
``claude setup-token`` are inference-only and are rejected.

Credential sources, in order of precedence:

1. ``signin``/``import``: the tracker's own token, stored in DATA_DIR/oauth.json.
   Created through the web UI, refreshed automatically.
2. ``file``: a Claude Code ``.credentials.json`` mounted into the container
   (CLAUDE_CREDENTIALS_FILE). Read on every poll; only refreshed when
   CLAUDE_CREDENTIALS_FILE_REFRESH is on, because refreshing rotates the token
   Claude Code itself is using.
3. ``env``: a bare access token in CLAUDE_OAUTH_TOKEN. Never refreshed.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import secrets
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode

import httpx

from .config import Settings

# Refresh this many seconds before the access token expires.
REFRESH_MARGIN = 300
# Pending sign-ins are only valid for a while.
LOGIN_TTL = 15 * 60


class AuthError(Exception):
    """No usable credentials; the user needs to (re)connect."""


@dataclass
class Credentials:
    access_token: str
    refresh_token: Optional[str] = None
    expires_at: Optional[float] = None  # unix seconds
    scopes: list[str] = field(default_factory=list)
    source: str = "signin"
    account: Optional[str] = None
    obtained_at: float = field(default_factory=time.time)

    def expired(self, margin: float = REFRESH_MARGIN) -> bool:
        return self.expires_at is not None and time.time() >= self.expires_at - margin


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _parse_claude_code_credentials(data: dict, source: str) -> Credentials:
    """Parse Claude Code's ``.credentials.json`` (or just its inner object)."""
    oauth = data.get("claudeAiOauth", data)
    token = oauth.get("accessToken") or oauth.get("access_token")
    if not token:
        raise ValueError("No accessToken found. Expected Claude Code's .credentials.json contents.")
    expires_at = oauth.get("expiresAt") or oauth.get("expires_at")
    if expires_at is not None:
        expires_at = float(expires_at)
        if expires_at > 1e11:  # Claude Code stores milliseconds
            expires_at /= 1000.0
    scopes = oauth.get("scopes") or []
    if isinstance(scopes, str):
        scopes = scopes.split()
    return Credentials(
        access_token=token,
        refresh_token=oauth.get("refreshToken") or oauth.get("refresh_token"),
        expires_at=expires_at,
        scopes=list(scopes),
        source=source,
        account=oauth.get("subscriptionType"),
    )


def _credentials_from_token_response(body: dict, source: str, previous: Optional[Credentials] = None) -> Credentials:
    if "access_token" not in body:
        raise AuthError(f"Token endpoint returned no access_token: {sorted(body)}")
    expires_in = body.get("expires_in")
    account = None
    if isinstance(body.get("account"), dict):
        account = body["account"].get("email_address") or body["account"].get("uuid")
    scope = body.get("scope")
    return Credentials(
        access_token=body["access_token"],
        # Some refresh responses omit the refresh token; keep the old one then.
        refresh_token=body.get("refresh_token") or (previous.refresh_token if previous else None),
        expires_at=time.time() + float(expires_in) if expires_in else None,
        scopes=scope.split() if isinstance(scope, str) else (previous.scopes if previous else []),
        source=source,
        account=account or (previous.account if previous else None),
    )


class CredentialStore:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._lock = asyncio.Lock()
        self._pending: dict[str, dict] = {}
        self.last_error: Optional[str] = None

    # -- loading and saving ---------------------------------------------

    def _load_own(self) -> Optional[Credentials]:
        path = self.settings.oauth_path
        if not path.exists():
            return None
        data = json.loads(path.read_text())
        return Credentials(**data)

    def _save_own(self, creds: Credentials) -> None:
        path = self.settings.oauth_path
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump(asdict(creds), fh, indent=2)
        os.replace(tmp, path)

    def _load_file(self) -> Optional[Credentials]:
        path = self.settings.credentials_file
        if not path:
            return None
        p = Path(path)
        if not p.exists():
            raise AuthError(f"CLAUDE_CREDENTIALS_FILE {path} does not exist")
        return _parse_claude_code_credentials(json.loads(p.read_text()), source="file")

    def _write_back_file(self, creds: Credentials) -> None:
        p = Path(self.settings.credentials_file)
        data = json.loads(p.read_text())
        oauth = data.setdefault("claudeAiOauth", {})
        oauth["accessToken"] = creds.access_token
        if creds.refresh_token:
            oauth["refreshToken"] = creds.refresh_token
        if creds.expires_at:
            oauth["expiresAt"] = int(creds.expires_at * 1000)
        p.write_text(json.dumps(data))

    def current(self) -> Optional[Credentials]:
        own = self._load_own()
        if own:
            return own
        from_file = self._load_file()
        if from_file:
            return from_file
        if self.settings.static_token:
            return Credentials(access_token=self.settings.static_token, source="env")
        return None

    def status(self) -> dict:
        try:
            creds = self.current()
        except (AuthError, ValueError, OSError) as exc:
            return {"connected": False, "error": str(exc)}
        if creds is None:
            return {"connected": False, "error": self.last_error}
        return {
            "connected": True,
            "source": creds.source,
            "account": creds.account,
            "scopes": creds.scopes,
            "expires_at": creds.expires_at,
            "can_refresh": bool(creds.refresh_token) and self._may_refresh(creds),
            "error": self.last_error,
        }

    def _may_refresh(self, creds: Credentials) -> bool:
        if creds.source == "env":
            return False
        if creds.source == "file":
            return self.settings.credentials_file_refresh
        return True

    # -- tokens -----------------------------------------------------------

    async def access_token(self, client: httpx.AsyncClient, force_refresh: bool = False) -> str:
        async with self._lock:
            try:
                creds = self.current()
            except (ValueError, OSError) as exc:
                raise AuthError(str(exc)) from exc
            if creds is None:
                raise AuthError("Not connected. Open the web UI and connect your Claude account.")
            if (force_refresh or creds.expired()) and creds.refresh_token and self._may_refresh(creds):
                creds = await self._refresh(client, creds)
            elif creds.expired(margin=0):
                if creds.source == "file":
                    raise AuthError(
                        "The access token in CLAUDE_CREDENTIALS_FILE has expired. Claude Code refreshes it "
                        "the next time it runs, or set CLAUDE_CREDENTIALS_FILE_REFRESH=true."
                    )
                raise AuthError("The access token has expired and cannot be refreshed. Reconnect your account.")
            return creds.access_token

    async def _refresh(self, client: httpx.AsyncClient, creds: Credentials) -> Credentials:
        payload = {
            "grant_type": "refresh_token",
            "refresh_token": creds.refresh_token,
            "client_id": self.settings.client_id,
            "scope": self.settings.scopes,
        }
        try:
            resp = await client.post(self.settings.token_url, json=payload, timeout=30)
        except httpx.HTTPError as exc:
            raise AuthError(f"Token refresh failed: {exc}") from exc
        if resp.status_code != 200:
            self.last_error = f"Token refresh failed (HTTP {resp.status_code}). Reconnect your account."
            raise AuthError(f"{self.last_error} {resp.text[:200]}")
        fresh = _credentials_from_token_response(resp.json(), creds.source, previous=creds)
        if creds.source == "file":
            self._write_back_file(fresh)
        else:
            self._save_own(fresh)
        self.last_error = None
        return fresh

    # -- sign-in ----------------------------------------------------------

    def start_login(self) -> dict:
        now = time.time()
        self._pending = {k: v for k, v in self._pending.items() if now - v["created"] < LOGIN_TTL}
        verifier = _b64url(secrets.token_bytes(32))
        challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
        state = _b64url(secrets.token_bytes(32))
        self._pending[state] = {"verifier": verifier, "created": now}
        params = {
            "code": "true",
            "client_id": self.settings.client_id,
            "response_type": "code",
            "redirect_uri": self.settings.redirect_uri,
            "scope": self.settings.scopes,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": state,
        }
        return {"url": f"{self.settings.authorize_url}?{urlencode(params)}", "state": state}

    async def complete_login(self, client: httpx.AsyncClient, pasted: str, state: Optional[str] = None) -> dict:
        """Exchange the ``code#state`` string shown after authorizing for tokens."""
        pasted = pasted.strip()
        code, _, pasted_state = pasted.partition("#")
        state = pasted_state or state
        pending = self._pending.pop(state, None) if state else None
        if not code or pending is None:
            raise AuthError("Unknown or expired sign-in. Click Connect again and paste the new code.")
        payload = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self.settings.redirect_uri,
            "client_id": self.settings.client_id,
            "code_verifier": pending["verifier"],
            "state": state,
        }
        try:
            resp = await client.post(self.settings.token_url, json=payload, timeout=30)
        except httpx.HTTPError as exc:
            raise AuthError(f"Token exchange failed: {exc}") from exc
        if resp.status_code != 200:
            raise AuthError(f"Token exchange failed (HTTP {resp.status_code}): {resp.text[:300]}")
        creds = _credentials_from_token_response(resp.json(), source="signin")
        async with self._lock:
            self._save_own(creds)
            self.last_error = None
        return self.status()

    def import_credentials(self, text: str) -> dict:
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            # A bare access token pasted on its own.
            token = text.strip()
            if not token or " " in token:
                raise ValueError("Paste the JSON from ~/.claude/.credentials.json or an access token.")
            data = {"accessToken": token}
        if not isinstance(data, dict):
            raise ValueError("Paste the JSON from ~/.claude/.credentials.json or an access token.")
        creds = _parse_claude_code_credentials(data, source="import")
        self._save_own(creds)
        self.last_error = None
        return self.status()

    def disconnect(self) -> None:
        self.settings.oauth_path.unlink(missing_ok=True)
        self.last_error = None
