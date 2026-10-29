"""SQLite storage for poll results.

Every poll is a row in ``polls``. A successful poll also writes one row per
usage window (``five_hour``, ``seven_day``, ...) into ``samples``. Everything
else the UI shows is derived from these two tables.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS polls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    ok INTEGER NOT NULL,
    error TEXT,
    raw TEXT
);
CREATE INDEX IF NOT EXISTS polls_ts ON polls(ts);

CREATE TABLE IF NOT EXISTS samples (
    poll_id INTEGER NOT NULL REFERENCES polls(id) ON DELETE CASCADE,
    ts REAL NOT NULL,
    window TEXT NOT NULL,
    utilization REAL,
    resets_at REAL,
    PRIMARY KEY (poll_id, window)
);
CREATE INDEX IF NOT EXISTS samples_window_ts ON samples(window, ts);
"""


@dataclass(frozen=True)
class Sample:
    ts: float
    utilization: Optional[float]
    resets_at: Optional[float]


@dataclass(frozen=True)
class WindowReading:
    window: str
    utilization: Optional[float]
    resets_at: Optional[float]


class Database:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._write_lock = threading.Lock()
        with self.connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # -- writes -----------------------------------------------------------

    def record_poll(
        self,
        ts: float,
        readings: list[WindowReading] | None = None,
        raw: dict | None = None,
        error: str | None = None,
    ) -> int:
        with self._write_lock, self.connect() as conn:
            return self._insert_poll(conn, ts, readings, raw, error)

    def record_polls(self, polls: list[tuple[float, list[WindowReading], Optional[dict]]]) -> None:
        """Insert many successful polls in one transaction (used for imports and the demo)."""
        with self._write_lock, self.connect() as conn:
            for ts, readings, raw in polls:
                self._insert_poll(conn, ts, readings, raw, None)

    @staticmethod
    def _insert_poll(conn, ts, readings, raw, error) -> int:
        ok = error is None
        cur = conn.execute(
            "INSERT INTO polls (ts, ok, error, raw) VALUES (?, ?, ?, ?)",
            (ts, 1 if ok else 0, error, json.dumps(raw) if raw is not None else None),
        )
        poll_id = cur.lastrowid
        if ok and readings:
            conn.executemany(
                "INSERT INTO samples (poll_id, ts, window, utilization, resets_at) VALUES (?, ?, ?, ?, ?)",
                [(poll_id, ts, r.window, r.utilization, r.resets_at) for r in readings],
            )
        return poll_id

    def prune_raw(self, older_than_ts: float) -> None:
        """Drop raw JSON bodies of old polls; the parsed samples are kept forever."""
        with self._write_lock, self.connect() as conn:
            conn.execute("UPDATE polls SET raw = NULL WHERE ts < ? AND raw IS NOT NULL", (older_than_ts,))

    # -- reads ------------------------------------------------------------

    def revision(self) -> int:
        """Changes whenever a poll is recorded; used to cache derived analytics."""
        with self.connect() as conn:
            row = conn.execute("SELECT COALESCE(MAX(id), 0) AS id FROM polls").fetchone()
            return int(row["id"])

    def windows(self) -> list[str]:
        with self.connect() as conn:
            return [r["window"] for r in conn.execute("SELECT DISTINCT window FROM samples ORDER BY window")]

    def samples(self, window: str, start: float | None = None, end: float | None = None) -> list[Sample]:
        query = "SELECT ts, utilization, resets_at FROM samples WHERE window = ?"
        params: list = [window]
        if start is not None:
            query += " AND ts >= ?"
            params.append(start)
        if end is not None:
            query += " AND ts <= ?"
            params.append(end)
        query += " ORDER BY ts"
        with self.connect() as conn:
            return [Sample(r["ts"], r["utilization"], r["resets_at"]) for r in conn.execute(query, params)]

    def poll_times(self, ok_only: bool = True) -> list[float]:
        query = "SELECT ts FROM polls" + (" WHERE ok = 1" if ok_only else "") + " ORDER BY ts"
        with self.connect() as conn:
            return [r["ts"] for r in conn.execute(query)]

    def latest_poll(self, ok_only: bool = False) -> Optional[dict]:
        query = "SELECT id, ts, ok, error, raw FROM polls" + (" WHERE ok = 1" if ok_only else "")
        query += " ORDER BY ts DESC, id DESC LIMIT 1"
        with self.connect() as conn:
            row = conn.execute(query).fetchone()
            if row is None:
                return None
            readings = [
                WindowReading(r["window"], r["utilization"], r["resets_at"])
                for r in conn.execute(
                    "SELECT window, utilization, resets_at FROM samples WHERE poll_id = ? ORDER BY window",
                    (row["id"],),
                )
            ]
            return {
                "id": row["id"],
                "ts": row["ts"],
                "ok": bool(row["ok"]),
                "error": row["error"],
                "raw": json.loads(row["raw"]) if row["raw"] else None,
                "readings": readings,
            }

    def poll_counts(self) -> dict:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS total, COALESCE(SUM(ok), 0) AS ok, MIN(ts) AS first_ts FROM polls"
            ).fetchone()
            return {"total": row["total"], "ok": row["ok"], "first_ts": row["first_ts"]}

    def iter_samples_csv_rows(self) -> Iterator[tuple]:
        with self.connect() as conn:
            yield from conn.execute("SELECT ts, window, utilization, resets_at FROM samples ORDER BY ts, window")
