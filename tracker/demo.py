"""Fill a database with simulated usage so the dashboard can be explored.

    python -m tracker.demo --db data/demo.db --days 56

The simulation follows the real rules: a 5-hour session starts with the first
message and resets 5 hours later, the weekly limit resets on a fixed schedule,
and hitting either limit blocks usage until it resets.
"""

from __future__ import annotations

import argparse
import random
import time
from datetime import datetime, timezone
from pathlib import Path

from .analytics import FIVE_HOURS, WEEK
from .db import Database, WindowReading


def _iso(ts: float | None) -> str | None:
    return None if ts is None else datetime.fromtimestamp(ts, timezone.utc).isoformat()


def _work_blocks(start: float, days: int, rng: random.Random) -> list[tuple[float, float, float]]:
    """(start, end, intensity) blocks; intensity is 5-hour % per hour of work."""
    blocks = []
    day0 = start - start % 86400
    for d in range(days + 1):
        day = day0 + d * 86400
        weekday = datetime.fromtimestamp(day, timezone.utc).weekday()
        weekend = weekday >= 5
        plan = [
            (0.35 if weekend else 0.85, 8.5, 10.0, 1.5, 4.0),  # morning
            (0.2 if weekend else 0.45, 14.0, 15.5, 1.0, 3.0),  # afternoon
            (0.45, 19.0, 21.0, 1.0, 3.0),  # evening
        ]
        for prob, h0, h1, len0, len1 in plan:
            if rng.random() > prob:
                continue
            b0 = day + rng.uniform(h0, h1) * 3600
            hours = rng.uniform(len0, len1)
            # About a third of work blocks are heavy enough to hit the 5-hour limit.
            target = rng.uniform(110, 160) if rng.random() < 0.33 else rng.uniform(8, 85)
            blocks.append((b0, b0 + hours * 3600, target / hours))
    return blocks


def simulate(db: Database, days: int = 56, interval: float = 300, seed: int = 7, now: float | None = None) -> int:
    rng = random.Random(seed)
    now = time.time() if now is None else now
    start = now - days * 86400
    blocks = _work_blocks(start, days, rng)
    # Weekly resets on a fixed weekday/time, like real plans.
    week_reset = start + rng.uniform(1, 6) * 86400
    week_util = rng.uniform(5, 40)
    week_load = rng.uniform(0.7, 1.4)
    session_util, session_reset, session_cost = 0.0, None, 0.0
    # One long outage to show how gaps in tracking are handled.
    outage = (start + days * 0.6 * 86400, start + days * 0.6 * 86400 + 9 * 3600)

    polls: list = []
    t = start
    bi = 0
    while t <= now:
        if t >= week_reset:
            week_util = 0.0
            week_reset += WEEK
            week_load = rng.uniform(0.7, 1.4)
        if session_reset is not None and t >= session_reset:
            session_util, session_reset = 0.0, None

        while bi < len(blocks) and blocks[bi][1] < t:
            bi += 1
        working = [b for b in blocks[bi : bi + 3] if b[0] <= t < b[1]]
        if working and session_util < 100 and week_util < 100:
            if session_reset is None:
                session_reset = t + FIVE_HOURS
                # A full session costs ~12.5% of the week (8 sessions), with noise.
                session_cost = 0.125 * rng.uniform(0.85, 1.15) * week_load
            added = min(100.0 - session_util, working[0][2] * interval / 3600.0)
            session_util += added
            week_util = min(100.0, week_util + added * session_cost)

        if not (outage[0] <= t < outage[1]):
            five = {"utilization": round(session_util), "resets_at": _iso(session_reset)}
            seven = {"utilization": round(week_util), "resets_at": _iso(week_reset)}
            body = {"five_hour": five, "seven_day": seven, "seven_day_opus": {"utilization": 0.0, "resets_at": None}}
            readings = [
                WindowReading("five_hour", five["utilization"], session_reset),
                WindowReading("seven_day", seven["utilization"], week_reset),
                WindowReading("seven_day_opus", 0.0, None),
            ]
            polls.append((t + rng.uniform(-5, 5), readings, body))
        t += interval
    db.record_polls(polls)
    return len(polls)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default="data/demo.db", help="SQLite file to create (default: data/demo.db)")
    parser.add_argument("--days", type=int, default=56)
    parser.add_argument("--interval", type=float, default=300, help="simulated poll interval in seconds")
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args()
    path = Path(args.db)
    if path.exists():
        parser.error(f"{path} already exists; pick another --db or delete it first")
    polls = simulate(Database(path), args.days, args.interval, args.seed)
    print(f"Wrote {polls} simulated polls to {path}")


if __name__ == "__main__":
    main()
