"""Turn raw usage polls into 5-hour sessions, weekly periods and statistics.

Terminology used throughout:

* **session** - one 5-hour limit window. Claude starts it with your first
  message and resets it 5 hours later. Identified by its ``resets_at``.
* **week** - one weekly limit period (``seven_day`` window), identified the
  same way.
* **idle slot** - 5 hours of watched time in which no session was running, i.e.
  a session you could have used but didn't.
* **full-session cost** - how much of the weekly limit one maxed-out 5-hour
  session uses. ``100 / cost`` is how many maxed sessions fill a week.
"""

from __future__ import annotations

import math
import statistics
import time
from dataclasses import dataclass, field
from typing import Iterable, Optional

from .db import Database, Sample

FIVE_HOURS = 5 * 3600.0
WEEK = 7 * 86400.0
# resets_at jitters by milliseconds between polls; readings whose reset times
# are this close belong to the same window.
SESSION_KEY_TOLERANCE = 15 * 60.0
WEEK_KEY_TOLERANCE = 3 * 3600.0
# Sessions whose 5-hour usage grew by less than this are too small to say much
# about the weekly cost of a full session.
MIN_DELTA_FOR_ESTIMATE = 10.0

PRIMARY_SESSION_WINDOW = "five_hour"
PRIMARY_WEEK_WINDOW = "seven_day"


@dataclass
class Poll:
    ts: float
    session: Optional[Sample]
    week: Optional[Sample]


@dataclass
class LimitWindow:
    """One 5-hour session or one weekly period."""

    kind: str
    end: float
    duration: float
    first_index: int
    last_index: int
    first_seen: float
    last_seen: float
    first_util: float
    peak: float
    maxed_at: Optional[float] = None
    # True when a poll just before the window showed it had not started, so we
    # saw all of its usage. False when tracking began mid-window.
    observed_from_start: bool = False
    baseline_index: Optional[int] = None
    # Filled in for sessions only.
    weekly_delta: Optional[float] = None
    # Filled in for weeks only.
    sessions: list["LimitWindow"] = field(default_factory=list)

    @property
    def start(self) -> float:
        return self.end - self.duration

    @property
    def session_delta(self) -> float:
        """5-hour utilization added while we watched (all of it if observed from the start)."""
        return self.peak - (0.0 if self.observed_from_start else self.first_util)

    def complete(self, now: float) -> bool:
        return self.end <= now

    def locked_seconds(self, now: float) -> float:
        if self.maxed_at is None:
            return 0.0
        return max(0.0, min(self.end, now) - self.maxed_at)


def _same_window(a: Optional[Sample], b: Optional[Sample], tolerance: float) -> bool:
    if a is None or b is None or a.resets_at is None or b.resets_at is None:
        return False
    return abs(a.resets_at - b.resets_at) <= tolerance


def _active(sample: Optional[Sample]) -> bool:
    return (
        sample is not None
        and sample.resets_at is not None
        and sample.utilization is not None
        and sample.ts < sample.resets_at
    )


def effective_utilization(sample: Sample) -> Optional[float]:
    """Utilization as of the sample time; a window past its reset counts as 0."""
    if sample.utilization is None:
        return None
    if sample.resets_at is not None and sample.ts >= sample.resets_at:
        return 0.0
    return sample.utilization


def merge_polls(session_samples: list[Sample], week_samples: list[Sample]) -> list[Poll]:
    by_ts: dict[float, Poll] = {}
    for s in session_samples:
        by_ts.setdefault(s.ts, Poll(s.ts, None, None)).session = s
    for s in week_samples:
        by_ts.setdefault(s.ts, Poll(s.ts, None, None)).week = s
    return [by_ts[ts] for ts in sorted(by_ts)]


def build_windows(
    polls: list[Poll],
    attr: str,
    kind: str,
    duration: float,
    tolerance: float,
    max_gap: float,
    maxed_threshold: float,
) -> list[LimitWindow]:
    windows: list[LimitWindow] = []
    current: Optional[LimitWindow] = None
    current_sample: Optional[Sample] = None
    for i, poll in enumerate(polls):
        sample: Optional[Sample] = getattr(poll, attr)
        if not _active(sample):
            continue
        assert sample is not None and sample.utilization is not None and sample.resets_at is not None
        if current is not None and _same_window(current_sample, sample, tolerance):
            current.last_index = i
            current.last_seen = sample.ts
            current.peak = max(current.peak, sample.utilization)
            current.end = sample.resets_at
        else:
            current = LimitWindow(
                kind=kind,
                end=sample.resets_at,
                duration=duration,
                first_index=i,
                last_index=i,
                first_seen=sample.ts,
                last_seen=sample.ts,
                first_util=sample.utilization,
                peak=sample.utilization,
            )
            if i > 0:
                prev = polls[i - 1]
                prev_sample = getattr(prev, attr)
                if poll.ts - prev.ts <= max_gap and prev_sample is not None and not _same_window(prev_sample, sample, tolerance):
                    current.observed_from_start = True
                    current.baseline_index = i - 1
            windows.append(current)
        current_sample = sample
        if current.maxed_at is None and sample.utilization >= maxed_threshold:
            current.maxed_at = sample.ts
    # A window that never showed any usage was not really used.
    return [w for w in windows if w.peak > 0]


def weekly_increase(polls: list[Poll], first: int, last: int) -> Optional[float]:
    """Weekly utilization added between two polls, across weekly resets."""
    total = 0.0
    prev: Optional[Sample] = None
    seen = False
    for i in range(first, last + 1):
        week = polls[i].week
        if week is None or week.utilization is None:
            continue
        util = effective_utilization(week) or 0.0
        if prev is not None:
            seen = True
            prev_util = effective_utilization(prev) or 0.0
            if _same_window(prev, week, WEEK_KEY_TOLERANCE):
                total += max(0.0, util - prev_util)
            else:
                total += util  # a new weekly period started from zero
        prev = week
    return total if seen else None


def subtract_intervals(base: list[tuple[float, float]], holes: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Remove ``holes`` from the sorted, non-overlapping ``base`` intervals."""
    result = []
    for a, b in base:
        pieces = [(a, b)]
        for h0, h1 in holes:
            next_pieces = []
            for p0, p1 in pieces:
                if h1 <= p0 or h0 >= p1:
                    next_pieces.append((p0, p1))
                    continue
                if h0 > p0:
                    next_pieces.append((p0, h0))
                if h1 < p1:
                    next_pieces.append((h1, p1))
            pieces = next_pieces
        result.extend(pieces)
    return [(a, b) for a, b in result if b > a]


def idle_intervals(
    windows: list[LimitWindow], watched: tuple[float, float], blind: list[tuple[float, float]]
) -> list[tuple[float, float]]:
    """Watched time not covered by any window, excluding blind spots."""
    start, end = watched
    if end <= start:
        return []
    busy = sorted((w.start, w.end) for w in windows)
    free = []
    cursor = start
    for a, b in busy:
        if a > cursor:
            free.append((cursor, min(a, end)))
        cursor = max(cursor, b)
        if cursor >= end:
            break
    if cursor < end:
        free.append((cursor, end))
    free = [(a, b) for a, b in free if b > a]
    return subtract_intervals(free, blind)


def count_slots(intervals: Iterable[tuple[float, float]], length: float, clip: Optional[tuple[float, float]] = None) -> int:
    total = 0
    for a, b in intervals:
        if clip:
            a, b = max(a, clip[0]), min(b, clip[1])
        if b > a:
            total += int((b - a) // length)
    return total


def _percentile(values: list[float], q: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    pos = (len(ordered) - 1) * q
    lo, hi = math.floor(pos), math.ceil(pos)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (pos - lo)


@dataclass
class Model:
    polls: list[Poll]
    sessions: list[LimitWindow]
    weeks: list[LimitWindow]
    watched: tuple[float, float]
    session_blind: list[tuple[float, float]]
    week_blind: list[tuple[float, float]]
    session_idle: list[tuple[float, float]]
    week_idle: list[tuple[float, float]]
    max_gap: float
    maxed_threshold: float
    now: float


def build_model(db: Database, poll_interval: float, maxed_threshold: float, now: Optional[float] = None) -> Model:
    now = time.time() if now is None else now
    max_gap = max(3 * poll_interval, 15 * 60.0)
    polls = merge_polls(db.samples(PRIMARY_SESSION_WINDOW), db.samples(PRIMARY_WEEK_WINDOW))

    sessions = build_windows(
        polls, "session", "session", FIVE_HOURS, SESSION_KEY_TOLERANCE, max_gap, maxed_threshold
    )
    weeks = build_windows(polls, "week", "week", WEEK, WEEK_KEY_TOLERANCE, max_gap, maxed_threshold)

    for s in sessions:
        first = s.baseline_index if s.baseline_index is not None else s.first_index
        s.weekly_delta = weekly_increase(polls, first, s.last_index)

    for w in weeks:
        w.sessions = [s for s in sessions if w.start <= s.start < w.end]

    ok_times = db.poll_times(ok_only=True)
    if ok_times:
        last = ok_times[-1]
        # Treat the tracker as still watching if the last poll is recent.
        watched_end = now if now - last <= max_gap else last
        watched = (ok_times[0], watched_end)
    else:
        watched = (now, now)
    gaps = [(a, b) for a, b in zip(ok_times, ok_times[1:])]
    if ok_times and watched[1] > ok_times[-1]:
        gaps.append((ok_times[-1], watched[1]))
    # A session that started while the tracker was down for less than 5 hours
    # is still running when it comes back, so only longer outages hide usage.
    session_blind = [(a, b) for a, b in gaps if b - a > FIVE_HOURS]
    week_blind = [(a, b) for a, b in gaps if b - a > WEEK]
    # While the weekly limit is exhausted no session can be started, so that
    # time does not count as idle.
    week_locks = [(w.maxed_at, w.end) for w in weeks if w.maxed_at is not None]

    return Model(
        polls=polls,
        sessions=sessions,
        weeks=weeks,
        watched=watched,
        session_blind=session_blind,
        week_blind=week_blind,
        session_idle=idle_intervals(sessions, watched, session_blind + week_locks),
        week_idle=idle_intervals(weeks, watched, week_blind),
        max_gap=max_gap,
        maxed_threshold=maxed_threshold,
        now=now,
    )


# -- presentation -------------------------------------------------------------


def session_dict(s: LimitWindow, now: float, threshold: float) -> dict:
    delta = s.session_delta
    cost = None
    if s.weekly_delta is not None and delta >= MIN_DELTA_FOR_ESTIMATE:
        cost = 100.0 * s.weekly_delta / delta
    return {
        "start": s.start,
        "end": s.end,
        "first_seen": s.first_seen,
        "last_seen": s.last_seen,
        "peak": round(s.peak, 2),
        "maxed": s.peak >= threshold,
        "maxed_at": s.maxed_at,
        "locked_minutes": round(s.locked_seconds(now) / 60.0, 1),
        "active": not s.complete(now),
        "observed_from_start": s.observed_from_start,
        "session_delta": round(delta, 2),
        "weekly_delta": None if s.weekly_delta is None else round(s.weekly_delta, 2),
        "full_session_cost": None if cost is None else round(cost, 2),
    }


def week_dict(w: LimitWindow, model: Model) -> dict:
    now, threshold = model.now, model.maxed_threshold
    clip = (w.start, min(w.end, model.watched[1]))
    used = len(w.sessions)
    maxed = sum(1 for s in w.sessions if s.peak >= threshold)
    equivalents = sum(min(s.peak, 100.0) for s in w.sessions) / 100.0
    to_limit = None
    if w.maxed_at is not None:
        before = [s for s in w.sessions if s.start <= w.maxed_at]
        to_limit = {
            "sessions": len(before),
            "maxed_sessions": sum(1 for s in before if s.peak >= threshold),
            "session_equivalents": round(sum(min(s.peak, 100.0) for s in before) / 100.0, 2),
            "hours_after_start": round((w.maxed_at - w.start) / 3600.0, 1),
        }
    return {
        "start": w.start,
        "end": w.end,
        "peak": round(w.peak, 2),
        "maxed": w.peak >= threshold,
        "maxed_at": w.maxed_at,
        "locked_hours": round(w.locked_seconds(now) / 3600.0, 1),
        "active": not w.complete(now),
        "observed_from_start": w.observed_from_start,
        "unused": None if not w.complete(now) else round(max(0.0, 100.0 - w.peak), 2),
        "sessions_used": used,
        "sessions_maxed": maxed,
        "sessions_partial": used - maxed,
        "session_equivalents": round(equivalents, 2),
        "idle_slots": count_slots(model.session_idle, FIVE_HOURS, clip),
        "to_limit": to_limit,
    }


def cost_estimate(sessions: list[LimitWindow], threshold: float, maxed_only: bool) -> Optional[dict]:
    eligible = [
        s
        for s in sessions
        if s.weekly_delta is not None
        and s.session_delta >= MIN_DELTA_FOR_ESTIMATE
        and (not maxed_only or (s.peak >= threshold and s.observed_from_start))
    ]
    if not eligible:
        return None
    total_week = sum(s.weekly_delta or 0.0 for s in eligible)
    total_session = sum(s.session_delta for s in eligible)
    cost = 100.0 * total_week / total_session if total_session else None
    per_session = [100.0 * (s.weekly_delta or 0.0) / s.session_delta for s in eligible]
    return {
        "sessions": len(eligible),
        "full_session_cost": None if cost is None else round(cost, 2),
        "sessions_to_weekly": None if not cost else round(100.0 / cost, 2),
        "median_cost": round(statistics.median(per_session), 2),
        "p25_cost": round(_percentile(per_session, 0.25) or 0.0, 2),
        "p75_cost": round(_percentile(per_session, 0.75) or 0.0, 2),
    }


def peak_histogram(sessions: list[LimitWindow], threshold: float) -> list[dict]:
    buckets = [{"label": f"{lo}–{lo + 10}%", "lo": lo, "hi": lo + 10, "count": 0} for lo in range(0, 100, 10)]
    buckets.append({"label": "Maxed", "lo": threshold, "hi": None, "count": 0})
    for s in sessions:
        if s.peak >= threshold:
            buckets[-1]["count"] += 1
        else:
            buckets[min(int(s.peak // 10), 9)]["count"] += 1
    return buckets


def summarize(model: Model, start: Optional[float] = None, end: Optional[float] = None) -> dict:
    now, threshold = model.now, model.maxed_threshold
    start = model.watched[0] if start is None else max(start, model.watched[0])
    end = now if end is None else end
    clip = (start, min(end, model.watched[1]))

    sessions = [s for s in model.sessions if s.end > start and s.start < end]
    weeks = [w for w in model.weeks if w.end > start and w.start < end]
    complete_sessions = [s for s in sessions if s.complete(now)]
    complete_weeks = [w for w in weeks if w.complete(now)]
    maxed = [s for s in sessions if s.peak >= threshold]

    idle_weeks = count_slots(model.week_idle, WEEK, clip)
    weeks_maxed = [w for w in complete_weeks if w.peak >= threshold]
    to_limit = [week_dict(w, model)["to_limit"] for w in weeks if w.maxed_at is not None]
    watched_seconds = sum(b - a for a, b in subtract_intervals([clip], model.session_blind)) if clip[1] > clip[0] else 0

    return {
        "range": {"start": start, "end": end},
        "sessions": {
            "used": len(sessions),
            "maxed": len(maxed),
            "partial": len(sessions) - len(maxed),
            "idle_slots": count_slots(model.session_idle, FIVE_HOURS, clip),
            "avg_peak": round(statistics.fmean(s.peak for s in sessions), 1) if sessions else None,
            "median_peak": round(statistics.median(s.peak for s in sessions), 1) if sessions else None,
            # Capacity left on the table in sessions you did start, in sessions.
            "unused_capacity": round(sum(max(0.0, 100.0 - s.peak) for s in complete_sessions) / 100.0, 2),
            "locked_hours": round(sum(s.locked_seconds(now) for s in sessions) / 3600.0, 1),
            "max_possible": int(watched_seconds // FIVE_HOURS),
        },
        "weeks": {
            "completed": len(complete_weeks),
            "maxed": len(weeks_maxed),
            "not_maxed": len(complete_weeks) - len(weeks_maxed),
            "idle_weeks": idle_weeks,
            "avg_final": round(statistics.fmean(w.peak for w in complete_weeks), 1) if complete_weeks else None,
            # Weekly limits you could have used but didn't, in weeks.
            "unused_capacity": round(
                sum(max(0.0, 100.0 - w.peak) for w in complete_weeks) / 100.0 + idle_weeks, 2
            ),
            "locked_hours": round(sum(w.locked_seconds(now) for w in weeks) / 3600.0, 1),
        },
        "estimate": {
            "all_sessions": cost_estimate(sessions, threshold, maxed_only=False),
            "maxed_sessions": cost_estimate(sessions, threshold, maxed_only=True),
            "observed_weeks": {
                "weeks": len(to_limit),
                "avg_sessions": round(statistics.fmean(t["sessions"] for t in to_limit), 2) if to_limit else None,
                "avg_session_equivalents": round(statistics.fmean(t["session_equivalents"] for t in to_limit), 2)
                if to_limit
                else None,
            },
        },
        "histogram": peak_histogram(sessions, threshold),
        "coverage": {
            "first_poll": model.watched[0] if model.polls else None,
            "watched_hours": round(watched_seconds / 3600.0, 1),
            "blind_hours": round(sum(b - a for a, b in model.session_blind) / 3600.0, 1),
        },
    }


def timeseries(
    db: Database, windows: list[str], start: float, end: float, max_points: int, max_gap: float
) -> dict[str, list[list[Optional[float]]]]:
    """Utilization per window, bucketed to at most ``max_points`` (max per bucket).

    A ``null`` value marks a gap in polling so charts break the line there.
    """
    width = max((end - start) / max(max_points, 1), 1.0)
    result: dict[str, list[list[Optional[float]]]] = {}
    for window in windows:
        points: list[list[Optional[float]]] = []
        bucket_key = None
        bucket_ts = 0.0
        bucket_val: Optional[float] = None
        prev_ts: Optional[float] = None
        for s in db.samples(window, start, end):
            util = effective_utilization(s)
            if util is None:
                continue
            key = int((s.ts - start) // width)
            gap = prev_ts is not None and s.ts - prev_ts > max_gap
            if key != bucket_key or gap:
                if bucket_key is not None:
                    points.append([bucket_ts, bucket_val])
                if gap:
                    points.append([prev_ts + 1, None])
                bucket_key, bucket_val = key, util
            else:
                bucket_val = max(bucket_val or 0.0, util)
            bucket_ts = s.ts
            prev_ts = s.ts
        if bucket_key is not None:
            points.append([bucket_ts, bucket_val])
        result[window] = points
    return result
