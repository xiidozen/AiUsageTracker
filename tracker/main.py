"""Web server: JSON API, dashboard and the background poller."""

from __future__ import annotations

import base64
import csv
import hashlib
import io
import logging
import os
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import __version__, analytics, codex
from .codex_login import CodexLogin
from .config import Settings
from .db import Database, Sample
from .oauth import AuthError, CredentialStore
from .poller import Poller

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("tracker")

STATIC_DIR = Path(__file__).parent / "static"
Provider = Literal["claude", "codex"]
PRIMARY_WINDOWS = [analytics.PRIMARY_SESSION_WINDOW, analytics.PRIMARY_WEEK_WINDOW]


class CodeBody(BaseModel):
    code: str
    state: Optional[str] = None


class ImportBody(BaseModel):
    text: str


def create_app(settings: Optional[Settings] = None) -> FastAPI:
    settings = settings or Settings()
    db = Database(settings.db_path)
    credentials = CredentialStore(settings)
    poller = Poller(settings, db, credentials)
    demo = os.environ.get("DEMO_MODE", "").lower() in {"1", "true", "yes", "on"}
    codex_db = Database(settings.data_dir / "codex-usage.db")
    def codex_connected() -> None:
        if settings.poller_enabled:
            if codex_poller.status()["enabled"]:
                codex_poller.trigger()
            else:
                codex_poller.start()

    codex_home = settings.data_dir / "codex"
    codex_login = None if settings.codex_auth_file or demo else CodexLogin(codex_home, codex_connected)
    codex_credentials = codex.CodexCredentials(
        settings.codex_auth_file or codex_home / "auth.json",
        refresh=codex_login.refresh if codex_login else None,
    )
    codex_poller = Poller(settings, codex_db, codex_credentials)
    providers = {
        "claude": (db, credentials, poller),
        "codex": (codex_db, codex_credentials, codex_poller),
    }
    caches: dict = {}

    def model(provider: Provider) -> analytics.Model:
        db, _, _ = providers[provider]
        cache = caches.get(provider, {})
        key = (db.revision(), int(time.time() // 60))
        if cache.get("key") != key:
            cache = {
                "key": key,
                "model": analytics.build_model(
                    db, settings.poll_interval, settings.maxed_threshold,
                    count_idle_slots=provider == "claude",
                ),
            }
            caches[provider] = cache
        return cache["model"]

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if demo:
            if db.revision() == 0:
                from .demo import simulate

                log.info("DEMO_MODE: generating simulated usage")
                simulate(db, interval=settings.poll_interval)
            log.info("DEMO_MODE: the poller is disabled")
        elif settings.poller_enabled:
            poller.start()
            if settings.codex_auth_file or (codex_home / "auth.json").is_file():
                codex_poller.start()
        try:
            yield
        finally:
            if codex_login:
                await codex_login.stop()
            await poller.stop()
            await codex_poller.stop()

    app = FastAPI(title="AI Usage Tracker", version=__version__, lifespan=lifespan)
    app.state.settings, app.state.db, app.state.poller, app.state.credentials = settings, db, poller, credentials

    if settings.ui_password:
        expected = f"{settings.ui_username}:{settings.ui_password}".encode()

        @app.middleware("http")
        async def basic_auth(request: Request, call_next):
            if request.url.path == "/healthz":
                return await call_next(request)
            header = request.headers.get("authorization", "")
            if header.lower().startswith("basic "):
                try:
                    supplied = base64.b64decode(header[6:])
                except ValueError:
                    supplied = b""
                if secrets.compare_digest(supplied, expected):
                    return await call_next(request)
            return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="AI Usage Tracker"'})

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    @app.get("/api/status")
    def status(provider: Provider = "claude"):
        db, credentials, poller = providers[provider]
        latest = db.latest_poll(ok_only=True)
        now = time.time()
        current = None
        if latest:
            windows = []
            for reading in latest["readings"]:
                window = {
                    "window": reading.window, "utilization": reading.utilization, "resets_at": reading.resets_at,
                }
                if reading.window == analytics.PRIMARY_WEEK_WINDOW or (
                    provider == "claude" and reading.window == analytics.PRIMARY_SESSION_WINDOW
                ):
                    sample = Sample(latest["ts"], reading.utilization, reading.resets_at)
                    window["projected_exhaustion_at"] = analytics.project_exhaustion(
                        db, reading.window, sample, now, settings.poll_interval,
                    )
                windows.append(window)
            current = {
                "ts": latest["ts"],
                "windows": windows,
                "extra_usage": (latest["raw"] or {}).get("extra_usage"),
            }
        last_any = db.latest_poll()
        return {
            "version": __version__,
            "now": now,
            "demo": demo,
            "auth": credentials.status(),
            "codex_device_login": provider == "codex" and codex_login is not None,
            "poller": poller.status(),
            "last_poll": None
            if last_any is None
            else {"ts": last_any["ts"], "ok": last_any["ok"], "error": last_any["error"]},
            "current": current,
            "polls": db.poll_counts(),
            "settings": {"poll_interval": settings.poll_interval, "maxed_threshold": settings.maxed_threshold},
        }

    @app.get("/api/dashboard")
    def dashboard(days: float = 30, points: int = 600, provider: Provider = "claude"):
        db, _, _ = providers[provider]
        m = model(provider)
        now = m.now
        start = None if days <= 0 else now - days * 86400
        summary = analytics.summarize(m, start=start)
        range_start = summary["range"]["start"]
        sessions = [
            analytics.session_dict(s, now, settings.maxed_threshold)
            for s in m.sessions
            if s.end > range_start
        ]
        weeks = [analytics.week_dict(w, m) for w in m.weeks if w.end > range_start]
        order = {w: i for i, w in enumerate(PRIMARY_WINDOWS)}
        windows = sorted(db.windows(), key=lambda w: (order.get(w, len(order)), w))
        series = analytics.timeseries(db, windows, range_start, now, min(max(points, 50), 2000), m.max_gap)
        return {
            "now": now,
            "summary": summary,
            "sessions": sessions[::-1],
            "weeks": weeks[::-1],
            "timeseries": series,
            "blind_spots": [[a, b] for a, b in m.session_blind if b > range_start],
            "maxed_threshold": settings.maxed_threshold,
        }

    @app.post("/api/poll")
    async def poll_now(provider: Provider = "claude"):
        if demo:
            raise HTTPException(400, "Polling is disabled in demo mode")
        return await providers[provider][2].poll_once()

    @app.post("/api/auth/codex/login")
    async def codex_login_start():
        if codex_login is None:
            raise HTTPException(400, "Codex sign-in is unavailable in demo mode or with CODEX_AUTH_FILE set.")
        return JSONResponse(await codex_login.start(), headers={"Cache-Control": "no-store"})

    @app.get("/api/auth/codex/login/{attempt}")
    async def codex_login_status(attempt: str):
        if codex_login is None or attempt != codex_login.attempt:
            raise HTTPException(404, "Codex sign-in session expired. Start again to get a new code.")
        return JSONResponse(codex_login.state, headers={"Cache-Control": "no-store"})

    @app.post("/api/auth/login")
    def auth_login():
        return credentials.start_login()

    @app.post("/api/auth/complete")
    async def auth_complete(body: CodeBody):
        try:
            result = await credentials.complete_login(poller.client, body.code, body.state)
        except AuthError as exc:
            raise HTTPException(400, str(exc)) from exc
        poller.trigger()
        return result

    @app.post("/api/auth/import")
    def auth_import(body: ImportBody):
        try:
            result = credentials.import_credentials(body.text)
        except (ValueError, TypeError) as exc:
            raise HTTPException(400, str(exc)) from exc
        poller.trigger()
        return result

    @app.post("/api/auth/logout")
    def auth_logout():
        credentials.disconnect()
        return credentials.status()

    @app.get("/api/export/samples.csv")
    def export_samples(provider: Provider = "claude"):
        db, _, _ = providers[provider]
        def rows():
            buf = io.StringIO()
            writer = csv.writer(buf)
            writer.writerow(["timestamp_utc", "unix_ts", "window", "utilization", "resets_at_utc"])
            for ts, window, util, resets in db.iter_samples_csv_rows():
                writer.writerow([_iso(ts), ts, window, util, _iso(resets)])
                if buf.tell() > 64_000:
                    yield buf.getvalue()
                    buf.seek(0)
                    buf.truncate()
            yield buf.getvalue()

        return StreamingResponse(
            rows(), media_type="text/csv", headers={"Content-Disposition": _attachment(provider, "usage-samples.csv")}
        )

    @app.get("/api/export/sessions.csv")
    def export_sessions(provider: Provider = "claude"):
        m = model(provider)
        buf = io.StringIO()
        writer = csv.writer(buf)
        fields = ["start", "end", "peak", "maxed", "locked_minutes", "session_delta", "weekly_delta", "full_session_cost"]
        writer.writerow(fields)
        for s in m.sessions:
            d = analytics.session_dict(s, m.now, settings.maxed_threshold)
            writer.writerow([_iso(d["start"]), _iso(d["end"])] + [d[f] for f in fields[2:]])
        return Response(
            buf.getvalue(), media_type="text/csv", headers={"Content-Disposition": _attachment(provider, "sessions.csv")}
        )

    @app.exception_handler(AuthError)
    async def auth_error(request: Request, exc: AuthError):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    # A cached script from a previous image can leave controls in the new HTML inert.
    script_hash = hashlib.sha256((STATIC_DIR / "app.js").read_bytes()).hexdigest()[:16]
    dashboard_html = (STATIC_DIR / "index.html").read_text(encoding="utf-8").replace(
        'src="/static/app.js"', f'src="/static/app.js?v={script_hash}"'
    )

    @app.get("/")
    def index():
        return HTMLResponse(dashboard_html, headers={"Cache-Control": "no-cache"})

    return app


def _attachment(provider: Provider, name: str) -> str:
    prefix = "codex-" if provider == "codex" else ""
    return f'attachment; filename="{prefix}{name}"'


def _iso(ts: Optional[float]) -> str:
    if ts is None:
        return ""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def main() -> None:
    import uvicorn

    uvicorn.run(
        "tracker.main:create_app",
        factory=True,
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "8080")),
        proxy_headers=True,
        log_level=os.environ.get("LOG_LEVEL", "info").lower(),
    )


if __name__ == "__main__":
    main()
