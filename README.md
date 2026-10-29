# AiUsageTracker

Track your Claude plan's usage limits over time. It polls the same usage data Claude Code's `/usage` command shows, stores every reading, and serves a dashboard with the answers to:

- How many 5-hour sessions did I use, and how many did I max out?
- How many 5-hour sessions went unused?
- How much of each weekly limit did I use, and how much went unused?
- **How many maxed-out 5-hour sessions does it take to reach the weekly limit?**

It runs as one small container with a SQLite database on a volume. Leave it running on a server and it builds up a history.

## Quick start

A prebuilt image for `linux/amd64` and `linux/arm64` is published to GitHub Container Registry:

```
ghcr.io/xiidozen/aiusagetracker:latest
```

Run it in any container environment (Docker, Podman, Portainer, Unraid, TrueNAS, Kubernetes, …). It needs port `8080` and a persistent volume at `/data`:

```bash
docker run -d --name ai-usage-tracker --restart unless-stopped \
  -p 8080:8080 -v ai-usage-tracker:/data \
  ghcr.io/xiidozen/aiusagetracker:latest
```

Or with the included compose file:

```bash
docker compose up -d
```

Open `http://<server>:8080`, click **Connect**, and sign in with your Claude account (see [Connecting your account](#connecting-your-account)). Readings start right away. Statistics fill in as sessions and weeks complete.

To look around before connecting a real account, start it with simulated data:

```bash
docker run --rm -p 8080:8080 -e DEMO_MODE=true ghcr.io/xiidozen/aiusagetracker:latest
```

If the dashboard is reachable by anyone else, set `TRACKER_PASSWORD` in `docker-compose.yml`. That turns on HTTP Basic auth for the UI and the API. Put it behind a reverse proxy with HTTPS if it is exposed to the internet.

## The dashboard

- **Current usage**: the active 5-hour session and weekly limit, with time to reset, plus any other limits your plan reports (for example, per-model weekly limits).
- **Headline number**: how many maxed-out 5-hour sessions fill a weekly limit, with the range across sessions and two cross-checks.
- **Summary tiles**: sessions used, maxed out and idle; average session peak; weeks at the limit; unused weekly capacity; time locked out.
- **Charts**: usage over time; sessions per weekly period (maxed, partly used, idle); weekly limit used at each reset; the weekly cost of each session; distribution of session peaks; when your sessions start (weekday × hour).
- **Tables**: every weekly period and every 5-hour session. Every chart also has a table view.
- **CSV export** of the raw readings and of the derived sessions.

Times display in your browser's time zone. The range selector (7, 30, 90 days, all) applies to everything below it.

## Connecting your account

The usage endpoint accepts a Claude OAuth token with the `user:profile` scope. There are three ways to provide one.

1. **Sign in through the tracker (recommended).** Click **Account → Open the sign-in page**, approve access, and paste the code shown back into the dialog. The tracker gets its own token with only the `user:profile` scope (it never requests inference access). The token is stored in `/data/oauth.json` with mode `600` and refreshed automatically. Because it is a separate login, it does not interfere with Claude Code on your laptop.
2. **Import Claude Code's credentials.** Paste the contents of `~/.claude/.credentials.json` (Linux) or the output of `security find-generic-password -s "Claude Code-credentials" -w` (macOS) into **Account → Import**. This shares one login between both programs. When either one refreshes the token, the other may get signed out.
3. **Mount the credentials file** of a Claude Code install on the same machine: set `CLAUDE_CREDENTIALS_FILE=/claude/.credentials.json` and mount `~/.claude` read-only at `/claude`. The file is re-read on every poll and never refreshed by the tracker, so polling only works while Claude Code keeps the token fresh. The container runs as UID 10001, so either make the file readable to it or set `user:` in the compose file to your UID.

`CLAUDE_OAUTH_TOKEN` also accepts a raw access token, which is never refreshed. Tokens from `claude setup-token` **won't work**: they only carry the `user:inference` scope.

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `POLL_INTERVAL_SECONDS` | `300` | How often to poll (minimum 60). Shorter catches a session's last few minutes before it resets. |
| `TRACKER_PASSWORD` | unset | Enables HTTP Basic auth for everything except `/healthz`. |
| `TRACKER_USERNAME` | `admin` | Username for Basic auth. |
| `MAXED_THRESHOLD` | `99` | Utilization (%) at which a session or week counts as maxed out. |
| `RAW_RETENTION_DAYS` | `30` | Keep the raw JSON responses this long. Parsed readings are kept forever. |
| `DATA_DIR` | `/data` | Where the database and token live. |
| `PORT` | `8080` | HTTP port. |
| `CLAUDE_CREDENTIALS_FILE` | unset | Path to a Claude Code `.credentials.json` to read tokens from. |
| `CLAUDE_CREDENTIALS_FILE_REFRESH` | `false` | Let the tracker refresh that file's token and write it back (rotates Claude Code's token). |
| `CLAUDE_OAUTH_TOKEN` | unset | A raw access token (not refreshed). |
| `DEMO_MODE` | `false` | Fill an empty database with simulated usage and disable polling. |
| `LOG_LEVEL` | `info` | Server log level. |
| `USAGE_URL`, `OAUTH_AUTHORIZE_URL`, `OAUTH_TOKEN_URL`, `OAUTH_REDIRECT_URI`, `OAUTH_CLIENT_ID`, `OAUTH_SCOPES` | Claude Code's values | Overrides in case the endpoints move. |

A token you sign in with is used before a credentials file, and a credentials file before `CLAUDE_OAUTH_TOKEN`. **Account → Disconnect** removes a signed-in or imported token.

## How the numbers are calculated

Each poll stores the utilization (0–100%) and reset time of every limit window the endpoint returns. Everything else is derived from those readings:

- **5-hour session**: one 5-hour limit window, identified by its reset time. Claude starts it with your first message and resets it 5 hours later. Its **peak** is the highest utilization seen. It counts as **maxed out** when the peak reaches `MAXED_THRESHOLD`. **Locked out** is the time from hitting the limit until the reset.
- **Idle 5-hour slots**: watched time with no session running, cut into whole 5-hour blocks. These are sessions you could have started but didn't. Time when the weekly limit was exhausted doesn't count, since no session could start then.
- **Unused in started sessions**: the sum of `100% − peak` over finished sessions, in sessions.
- **Weekly period**: one weekly limit window. **Used** is its utilization when it reset, **unused** is `100% − used`. **Unused weekly capacity** adds those up in weeks, plus any whole weeks with no weekly period at all.
- **Weekly cost of a session**: how much weekly utilization rose during the session, from the last poll before it started to its last poll. Rises across a weekly reset are counted on both sides.
- **Maxed sessions to fill a week**: weekly percentage used ÷ 5-hour percentage used, summed over all sessions that used at least 10% of their 5-hour limit. This gives the weekly cost of one full session. `100 ÷ cost` is the headline number. Two cross-checks are shown next to it:
  - the same estimate using only sessions that were maxed out, and
  - in weeks where you actually hit the weekly limit, how many sessions (and full-session equivalents) it took.

Some care is taken with gaps in the data:

- If tracking starts in the middle of a session, only the usage seen afterwards is compared, on both sides of the ratio.
- If the tracker is offline for more than 5 hours, a whole session could have come and gone unseen, so that stretch is excluded from the idle-slot count. It is shaded on the usage chart. Shorter outages lose nothing, because a session started during the outage is still running when polling resumes.

Things to keep in mind:

- The limits are shared across claude.ai, the desktop app, and Claude Code. The tracker sees the combined usage, whichever app it came from.
- Utilization is reported in whole percents, so a single small session's weekly cost is rough. The ratio across many sessions is much steadier.
- Anthropic changes limits from time to time (and may weight usage differently at peak hours). Per-session costs over time show when that happens. Use the range selector to compare periods.
- The usage endpoint is the one Claude Code uses, not a documented public API. If its format changes, unknown fields are still stored raw in the database (for `RAW_RETENTION_DAYS`) and new limit windows are picked up automatically.

## Image tags

The [workflow](.github/workflows/docker-publish.yml) runs the tests and builds the image on every pull request. It publishes on every push to `main` and on version tags:

| Tag | Points to |
| --- | --- |
| `latest` | The newest commit on `main`. |
| `sha-<commit>` | One specific commit, for pinning. |
| `1.2.3`, `1.2` | A release, when a `v1.2.3` git tag is pushed. |

To build the image yourself instead, run `docker build -t ai-usage-tracker .` or uncomment `build: .` in `docker-compose.yml`.

## Development

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
pytest

# Run locally with simulated data
DEMO_MODE=true DATA_DIR=./data python -m tracker.main

# Or generate a demo database to inspect
python -m tracker.demo --db data/demo.db --days 56
```

The layout:

| Path | What it does |
| --- | --- |
| `tracker/usage_api.py` | Calls the usage endpoint and parses its windows. |
| `tracker/oauth.py` | Sign-in (OAuth PKCE), import, token refresh and storage. |
| `tracker/poller.py` | Background polling loop. |
| `tracker/db.py` | SQLite storage (`polls` and `samples` tables). |
| `tracker/analytics.py` | Sessions, weekly periods, idle slots and the estimates. |
| `tracker/main.py` | FastAPI app: JSON API and static dashboard. |
| `tracker/static/` | The dashboard (plain JS and SVG, no build step, no CDN). |
| `tracker/demo.py` | Usage simulator for demo mode and tests. |

The JSON API behind the dashboard: `GET /api/status`, `GET /api/dashboard?days=30`, `POST /api/poll`, `GET /api/export/samples.csv`, `GET /api/export/sessions.csv`.
