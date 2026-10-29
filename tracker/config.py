"""Runtime configuration, read from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env(name: str, default: str) -> str:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


def _env_float(name: str, default: float) -> float:
    try:
        return float(_env(name, str(default)))
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    return _env(name, "1" if default else "0").strip().lower() in {"1", "true", "yes", "on"}


# Endpoints and OAuth client used by Claude Code itself. They are overridable in
# case Anthropic moves them; the usage endpoint is not a documented public API.
DEFAULT_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
DEFAULT_AUTHORIZE_URL = "https://claude.com/cai/oauth/authorize"
DEFAULT_TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
DEFAULT_REDIRECT_URI = "https://platform.claude.com/oauth/code/callback"
DEFAULT_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
# The usage endpoint only needs the profile scope; inference is never requested.
DEFAULT_SCOPES = "user:profile"


@dataclass
class Settings:
    data_dir: Path = field(default_factory=lambda: Path(_env("DATA_DIR", "./data")))
    poll_interval: float = field(default_factory=lambda: max(60.0, _env_float("POLL_INTERVAL_SECONDS", 300)))
    # A 5-hour session whose peak reaches this percentage counts as "maxed out".
    maxed_threshold: float = field(default_factory=lambda: _env_float("MAXED_THRESHOLD", 99.0))
    raw_retention_days: float = field(default_factory=lambda: _env_float("RAW_RETENTION_DAYS", 30))

    # Credentials: a static access token, a Claude Code credentials file, or the
    # tracker's own sign-in (stored in data_dir/oauth.json).
    static_token: str = field(default_factory=lambda: _env("CLAUDE_OAUTH_TOKEN", ""))
    credentials_file: str = field(default_factory=lambda: _env("CLAUDE_CREDENTIALS_FILE", ""))
    # When reading a Claude Code credentials file, refresh expired tokens and write
    # them back. Off by default because it rotates the token Claude Code is using.
    credentials_file_refresh: bool = field(default_factory=lambda: _env_bool("CLAUDE_CREDENTIALS_FILE_REFRESH", False))

    usage_url: str = field(default_factory=lambda: _env("USAGE_URL", DEFAULT_USAGE_URL))
    authorize_url: str = field(default_factory=lambda: _env("OAUTH_AUTHORIZE_URL", DEFAULT_AUTHORIZE_URL))
    token_url: str = field(default_factory=lambda: _env("OAUTH_TOKEN_URL", DEFAULT_TOKEN_URL))
    redirect_uri: str = field(default_factory=lambda: _env("OAUTH_REDIRECT_URI", DEFAULT_REDIRECT_URI))
    client_id: str = field(default_factory=lambda: _env("OAUTH_CLIENT_ID", DEFAULT_CLIENT_ID))
    scopes: str = field(default_factory=lambda: _env("OAUTH_SCOPES", DEFAULT_SCOPES))

    # Optional HTTP Basic auth for the web UI and API.
    ui_username: str = field(default_factory=lambda: _env("TRACKER_USERNAME", "admin"))
    ui_password: str = field(default_factory=lambda: _env("TRACKER_PASSWORD", ""))

    # Disable the background poller (used by tests and the demo).
    poller_enabled: bool = field(default_factory=lambda: _env_bool("POLLER_ENABLED", True))

    @property
    def db_path(self) -> Path:
        return self.data_dir / "usage.db"

    @property
    def oauth_path(self) -> Path:
        return self.data_dir / "oauth.json"
