from tracker import analytics
from tracker.analytics import FIVE_HOURS, WEEK
from tracker.db import Database, WindowReading
from tracker.demo import simulate

H = 3600.0
T0 = 1_800_000_000.0  # arbitrary epoch origin for synthetic data
INTERVAL = 300.0


class Recorder:
    """Builds a poll history with explicit 5-hour and weekly state."""

    def __init__(self, db):
        self.db = db
        self.polls = []

    def add(self, t, session_util, session_reset, week_util, week_reset):
        self.polls.append(
            (
                t,
                [
                    WindowReading("five_hour", session_util, session_reset),
                    WindowReading("seven_day", week_util, week_reset),
                ],
                None,
            )
        )

    def flush(self):
        self.db.record_polls(self.polls)
        self.polls = []


def run(start, end, state_at, rec, skip=None):
    t = start
    while t <= end:
        if not (skip and skip[0] <= t < skip[1]):
            rec.add(t, *state_at(t))
        t += INTERVAL
    rec.flush()


def make_db(tmp_path):
    return Database(tmp_path / "usage.db")


def test_sessions_weekly_delta_and_idle_slots(tmp_path):
    db = make_db(tmp_path)
    rec = Recorder(db)
    week_reset = T0 + 3 * 24 * H
    # Session A: starts at T0+2h, climbs to 100% by +4h (maxed), weekly +12.5 over it.
    # Session B: starts at T0+20h, climbs to 40%, weekly +5.
    a_start, b_start = T0 + 2 * H, T0 + 20 * H

    def state(t):
        if a_start <= t < a_start + FIVE_HOURS:
            u = min(100.0, (t - a_start) / (2 * H) * 100.0)
            return u, a_start + FIVE_HOURS, 10.0 + u * 0.125, week_reset
        if b_start <= t < b_start + FIVE_HOURS:
            u = min(40.0, (t - b_start) / H * 40.0)
            return u, b_start + FIVE_HOURS, 22.5 + u * 0.125, week_reset
        weekly = 10.0 if t < a_start else 22.5 if t < b_start else 27.5
        return 0.0, None, weekly, week_reset

    end = T0 + 30 * H
    run(T0, end, state, rec)
    model = analytics.build_model(db, INTERVAL, 99.0, now=end)

    assert len(model.sessions) == 2
    a, b = model.sessions
    assert a.observed_from_start and b.observed_from_start
    assert a.peak == 100.0 and a.maxed_at is not None
    assert abs(a.weekly_delta - 12.5) < 0.01
    assert abs(b.weekly_delta - 5.0) < 0.01
    # Session A was locked from reaching 100% (after 2h) until its reset.
    assert abs(a.locked_seconds(end) - 3 * H) <= INTERVAL

    summary = analytics.summarize(model)
    est = summary["estimate"]["all_sessions"]
    assert est["sessions"] == 2
    assert abs(est["full_session_cost"] - 12.5) < 0.1
    assert abs(est["sessions_to_weekly"] - 8.0) < 0.1
    assert summary["estimate"]["maxed_sessions"]["sessions"] == 1
    assert summary["sessions"]["maxed"] == 1 and summary["sessions"]["partial"] == 1
    # Idle: [T0, T0+2h) = 0 slots, [T0+7h, T0+20h) = 2 slots, [T0+25h, T0+30h) = 1 slot.
    assert summary["sessions"]["idle_slots"] == 3


def test_weekly_reset_inside_session_counts_both_sides(tmp_path):
    db = make_db(tmp_path)
    rec = Recorder(db)
    start = T0 + H
    old_reset, new_reset = start + 2 * H, start + 2 * H + WEEK

    def state(t):
        if t < start:
            return 0.0, None, 50.0, old_reset
        u = min(100.0, (t - start) / (4 * H) * 100.0)
        weekly_before = 50.0 + min(u, 50.0) * 0.1  # 2h in -> u=50 -> +5
        if t < old_reset:
            return u, start + FIVE_HOURS, weekly_before, old_reset
        return u, start + FIVE_HOURS, (u - 50.0) * 0.1, new_reset

    run(T0, start + FIVE_HOURS - 60, state, rec)
    model = analytics.build_model(db, INTERVAL, 99.0, now=start + FIVE_HOURS)
    (session,) = model.sessions
    # 5% before the weekly reset plus 5% after it, less what was used between
    # the last poll before the reset and the reset itself (one poll interval).
    assert abs(session.weekly_delta - 10.0) < 0.25
    assert len(model.weeks) == 2


def test_tracking_started_mid_session_uses_partial_deltas(tmp_path):
    db = make_db(tmp_path)
    rec = Recorder(db)
    start = T0 - 2 * H  # session started before the first poll

    def state(t):
        u = min(100.0, 30.0 + (t - T0) / H * 20.0)
        return u, start + FIVE_HOURS, 40.0 + (u - 30.0) * 0.1, T0 + WEEK

    run(T0, start + FIVE_HOURS - 60, state, rec)
    model = analytics.build_model(db, INTERVAL, 99.0, now=start + FIVE_HOURS)
    (session,) = model.sessions
    assert not session.observed_from_start
    assert abs(session.session_delta - (session.peak - 30.0)) < 1e-6
    d = analytics.session_dict(session, model.now, 99.0)
    assert abs(d["full_session_cost"] - 10.0) < 0.2


def test_long_outage_is_blind_and_not_idle(tmp_path):
    db = make_db(tmp_path)
    rec = Recorder(db)
    outage = (T0 + 2 * H, T0 + 14 * H)
    run(T0, T0 + 20 * H, lambda t: (0.0, None, 5.0, T0 + WEEK), rec, skip=outage)
    model = analytics.build_model(db, INTERVAL, 99.0, now=T0 + 20 * H)
    assert len(model.session_blind) == 1
    summary = analytics.summarize(model)
    # 2h before the outage (0 slots) + 6h after it (1 slot); the 12h outage counts for nothing.
    assert summary["sessions"]["idle_slots"] == 1
    assert summary["coverage"]["blind_hours"] >= 11.9


def test_weekly_lockout_is_not_idle(tmp_path):
    db = make_db(tmp_path)
    rec = Recorder(db)
    week_end = T0 + 30 * H
    run(T0, T0 + 29 * H, lambda t: (0.0, None, 100.0, week_end), rec)
    model = analytics.build_model(db, INTERVAL, 99.0, now=T0 + 29 * H)
    assert analytics.summarize(model)["sessions"]["idle_slots"] == 0


def test_timeseries_breaks_lines_at_gaps(tmp_path):
    db = make_db(tmp_path)
    rec = Recorder(db)
    run(T0, T0 + 10 * H, lambda t: (0.0, None, 5.0, T0 + WEEK), rec, skip=(T0 + 3 * H, T0 + 6 * H))
    series = analytics.timeseries(db, ["seven_day"], T0, T0 + 10 * H, 1000, max_gap=900)
    values = [v for _, v in series["seven_day"]]
    assert values.count(None) == 1
    assert len(series["seven_day"]) > 50


def test_demo_simulation_recovers_session_cost(tmp_path):
    db = make_db(tmp_path)
    simulate(db, days=42, interval=300, seed=3, now=T0)
    model = analytics.build_model(db, 300, 99.0, now=T0)
    summary = analytics.summarize(model)
    est = summary["estimate"]["all_sessions"]
    assert summary["sessions"]["used"] > 30
    # The simulation charges 12.5% x noise x a weekly load factor (0.7-1.4).
    assert 9.0 < est["full_session_cost"] < 18.0
    assert len(model.weeks) >= 6
