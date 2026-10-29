"""Background loop that polls the usage endpoint and stores the readings."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

import httpx

from .config import Settings
from .db import Database
from .oauth import AuthError, CredentialStore
from .usage_api import UsageError, fetch_usage, parse_usage

log = logging.getLogger("tracker.poller")


class Poller:
    def __init__(self, settings: Settings, db: Database, credentials: CredentialStore):
        self.settings = settings
        self.db = db
        self.credentials = credentials
        self.client = httpx.AsyncClient(follow_redirects=False)
        self.last_attempt: Optional[float] = None
        self.last_success: Optional[float] = None
        self.last_error: Optional[str] = None
        self.next_poll_at: Optional[float] = None
        self._wake = asyncio.Event()
        self._task: Optional[asyncio.Task] = None
        self._poll_lock = asyncio.Lock()

    async def poll_once(self) -> dict:
        async with self._poll_lock:
            now = time.time()
            self.last_attempt = now
            try:
                body = await fetch_usage(self.client, self.credentials, self.settings.usage_url)
            except AuthError as exc:
                self.last_error = str(exc)
                self.credentials.last_error = str(exc)
                # Only record attempts that actually reached the API.
                try:
                    has_credentials = self.credentials.current() is not None
                except (AuthError, ValueError, OSError):
                    has_credentials = False
                if has_credentials:
                    self.db.record_poll(now, error=str(exc))
                log.warning("poll failed: %s", exc)
                return {"ok": False, "error": str(exc)}
            except (UsageError, httpx.HTTPError, OSError, ValueError) as exc:
                self.last_error = str(exc)
                self.db.record_poll(now, error=str(exc))
                log.warning("poll failed: %s", exc)
                return {"ok": False, "error": str(exc)}

            readings = parse_usage(body)
            self.db.record_poll(now, readings=readings, raw=body)
            self.last_success = now
            self.last_error = None
            self.credentials.last_error = None
            summary = ", ".join(
                f"{r.window}={r.utilization:.0f}%" for r in readings if r.utilization is not None and ":" not in r.window
            )
            log.info("poll ok: %s", summary or "no windows")
            return {"ok": True, "windows": len(readings)}

    def trigger(self) -> None:
        self._wake.set()

    async def _run(self) -> None:
        last_prune = 0.0
        while True:
            try:
                await self.poll_once()
            except Exception:  # never let the loop die
                log.exception("unexpected error while polling")
            now = time.time()
            if now - last_prune > 3600:
                self.db.prune_raw(now - self.settings.raw_retention_days * 86400)
                last_prune = now
            self.next_poll_at = time.time() + self.settings.poll_interval
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self.settings.poll_interval)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="usage-poller")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        await self.client.aclose()

    def status(self) -> dict:
        return {
            "enabled": self._task is not None,
            "interval": self.settings.poll_interval,
            "last_attempt": self.last_attempt,
            "last_success": self.last_success,
            "last_error": self.last_error,
            "next_poll_at": self.next_poll_at,
        }
