"""SQLite storage — and the contract between Part 1 and Part 2.

Part 1 writes everything here. Part 2 opens the same file read-only and never
imports anything from the polling path. The ``issues`` / ``issue_events``
tables are the interface: every flagged issue lands there with a timestamp,
the device it concerns, the issue type, and the raw data that triggered it.

All timestamps are UTC epoch seconds.
"""

from __future__ import annotations

import os
import sqlite3
from typing import Any, Iterable, Sequence

from .util import LOG, dumps, now

SCHEMA_VERSION = 1

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- One row per poll attempt. Gaps and failures here are themselves signal.
CREATE TABLE IF NOT EXISTS poll_runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at   INTEGER NOT NULL,
    finished_at  INTEGER,
    ok           INTEGER NOT NULL DEFAULT 0,
    duration_ms  INTEGER,
    device_count INTEGER,
    client_count INTEGER,
    issue_count  INTEGER,
    error        TEXT
);
CREATE INDEX IF NOT EXISTS idx_poll_runs_started ON poll_runs(started_at DESC);

-- Every monitored thing: device | client | wan | port | site.
CREATE TABLE IF NOT EXISTS entities (
    entity_type TEXT NOT NULL,
    entity_id   TEXT NOT NULL,
    name        TEXT,
    kind        TEXT,
    model       TEXT,
    first_seen  INTEGER NOT NULL,
    last_seen   INTEGER NOT NULL,
    meta        TEXT,
    PRIMARY KEY (entity_type, entity_id)
);
CREATE INDEX IF NOT EXISTS idx_entities_name ON entities(name);

-- Current state per entity, plus when that state started.
CREATE TABLE IF NOT EXISTS entity_state (
    entity_type TEXT NOT NULL,
    entity_id   TEXT NOT NULL,
    state       TEXT NOT NULL,
    since       INTEGER NOT NULL,
    updated_at  INTEGER NOT NULL,
    raw_state   TEXT,
    details     TEXT,
    PRIMARY KEY (entity_type, entity_id)
);

-- Append-only state changes. Flap detection reads this.
CREATE TABLE IF NOT EXISTS state_transitions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            INTEGER NOT NULL,
    entity_type   TEXT NOT NULL,
    entity_id     TEXT NOT NULL,
    entity_name   TEXT,
    from_state    TEXT,
    to_state      TEXT NOT NULL,
    prev_duration INTEGER,
    raw           TEXT
);
CREATE INDEX IF NOT EXISTS idx_transitions_entity
    ON state_transitions(entity_type, entity_id, ts DESC);
CREATE INDEX IF NOT EXISTS idx_transitions_ts ON state_transitions(ts DESC);

-- Numeric time series: bandwidth, cpu, latency, port errors, signal...
CREATE TABLE IF NOT EXISTS metrics (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          INTEGER NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id   TEXT NOT NULL,
    metric      TEXT NOT NULL,
    value       REAL,
    extra       TEXT
);
CREATE INDEX IF NOT EXISTS idx_metrics_lookup
    ON metrics(entity_type, entity_id, metric, ts DESC);
CREATE INDEX IF NOT EXISTS idx_metrics_ts ON metrics(ts DESC);

-- Raw counters from the previous poll, so deltas survive process restarts.
CREATE TABLE IF NOT EXISTS counters (
    entity_type TEXT NOT NULL,
    entity_id   TEXT NOT NULL,
    counter     TEXT NOT NULL,
    value       REAL NOT NULL,
    ts          INTEGER NOT NULL,
    PRIMARY KEY (entity_type, entity_id, counter)
);

-- WAN / uplink health, one row per WAN per poll.
CREATE TABLE IF NOT EXISTS wan_samples (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          INTEGER NOT NULL,
    wan_id      TEXT NOT NULL,
    status      TEXT,
    active      INTEGER,
    isp         TEXT,
    ip          TEXT,
    gateway     TEXT,
    latency_ms  REAL,
    loss_pct    REAL,
    xput_down   REAL,
    xput_up     REAL,
    uptime      INTEGER,
    raw         TEXT
);
CREATE INDEX IF NOT EXISTS idx_wan_samples ON wan_samples(wan_id, ts DESC);

-- Controller events and alarms, de-duplicated by their controller-side id.
CREATE TABLE IF NOT EXISTS controller_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    source     TEXT NOT NULL,            -- event | alarm
    remote_id  TEXT NOT NULL,
    ts         INTEGER NOT NULL,
    key        TEXT,
    subsystem  TEXT,
    severity   TEXT,
    entity_id  TEXT,
    entity_name TEXT,
    message    TEXT,
    raw        TEXT,
    UNIQUE (source, remote_id)
);
CREATE INDEX IF NOT EXISTS idx_controller_events_ts ON controller_events(ts DESC);
CREATE INDEX IF NOT EXISTS idx_controller_events_entity ON controller_events(entity_id, ts DESC);

-- THE INTERFACE TABLE. One row per distinct ongoing problem.
CREATE TABLE IF NOT EXISTS issues (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint   TEXT NOT NULL,         -- issue_type + entity, dedupes while open
    issue_type    TEXT NOT NULL,
    severity      TEXT NOT NULL,         -- info | warning | critical
    max_severity  TEXT NOT NULL,
    status        TEXT NOT NULL,         -- open | resolved | acked
    entity_type   TEXT NOT NULL,
    entity_id     TEXT NOT NULL,
    entity_name   TEXT,
    summary       TEXT NOT NULL,         -- bare fact, no diagnosis
    first_seen    INTEGER NOT NULL,
    last_seen     INTEGER NOT NULL,
    resolved_at   INTEGER,
    occurrences   INTEGER NOT NULL DEFAULT 1,
    notified_at   INTEGER,
    notified_severity TEXT,
    details       TEXT,                  -- JSON: threshold, measured value, context
    trigger_data  TEXT                   -- JSON: raw controller payload that tripped it
);
CREATE INDEX IF NOT EXISTS idx_issues_open ON issues(status, severity, last_seen DESC);
CREATE INDEX IF NOT EXISTS idx_issues_entity ON issues(entity_id, first_seen DESC);
CREATE INDEX IF NOT EXISTS idx_issues_type ON issues(issue_type, first_seen DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_issues_open_fingerprint
    ON issues(fingerprint) WHERE status = 'open';

-- Append-only log of every observation of an issue, including re-flags.
CREATE TABLE IF NOT EXISTS issue_events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    issue_id  INTEGER NOT NULL REFERENCES issues(id) ON DELETE CASCADE,
    ts        INTEGER NOT NULL,
    kind      TEXT NOT NULL,             -- opened | observed | escalated | resolved
    severity  TEXT,
    summary   TEXT,
    details   TEXT,
    trigger_data TEXT
);
CREATE INDEX IF NOT EXISTS idx_issue_events ON issue_events(issue_id, ts DESC);
CREATE INDEX IF NOT EXISTS idx_issue_events_ts ON issue_events(ts DESC);

-- Alert delivery audit.
CREATE TABLE IF NOT EXISTS notifications (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    issue_id INTEGER REFERENCES issues(id) ON DELETE SET NULL,
    ts       INTEGER NOT NULL,
    channel  TEXT NOT NULL,
    kind     TEXT NOT NULL,              -- alert | resolve
    ok       INTEGER NOT NULL,
    detail   TEXT
);
CREATE INDEX IF NOT EXISTS idx_notifications_issue ON notifications(issue_id, ts DESC);
"""


class Database:
    """Thin, explicit SQLite wrapper. No ORM, no magic."""

    def __init__(self, path: str, read_only: bool = False):
        self.path = path
        self.read_only = read_only
        if read_only:
            if not os.path.exists(path):
                raise FileNotFoundError(f"database not found: {path}")
            uri = f"file:{_uri_path(path)}?mode=ro"
            self.conn = sqlite3.connect(uri, uri=True, timeout=15)
        else:
            parent = os.path.dirname(os.path.abspath(path))
            if parent:
                os.makedirs(parent, exist_ok=True)
            self.conn = sqlite3.connect(path, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA busy_timeout=15000")
        if not read_only:
            self.migrate()

    # ----------------------------------------------------------------- basics

    def migrate(self) -> None:
        with self.conn:
            self.conn.executescript(SCHEMA)
            self.conn.execute(
                "INSERT INTO schema_meta(key, value) VALUES('schema_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(SCHEMA_VERSION),),
            )

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        return list(self.conn.execute(sql, params))

    def query_one(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        cur = self.conn.execute(sql, params)
        return cur.fetchone()

    def execute(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
        with self.conn:
            return self.conn.execute(sql, params)

    def close(self) -> None:
        try:
            self.conn.close()
        except sqlite3.Error:
            pass

    def __enter__(self) -> Database:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # ------------------------------------------------------------- poll runs

    def start_run(self, ts: int | None = None) -> int:
        cur = self.execute("INSERT INTO poll_runs(started_at) VALUES(?)", (ts or now(),))
        return int(cur.lastrowid or 0)

    def finish_run(
        self,
        run_id: int,
        ok: bool,
        *,
        device_count: int = 0,
        client_count: int = 0,
        issue_count: int = 0,
        error: str | None = None,
    ) -> None:
        row = self.query_one("SELECT started_at FROM poll_runs WHERE id=?", (run_id,))
        started = int(row["started_at"]) if row else now()
        finished = now()
        self.execute(
            "UPDATE poll_runs SET finished_at=?, ok=?, duration_ms=?, device_count=?, "
            "client_count=?, issue_count=?, error=? WHERE id=?",
            (
                finished,
                1 if ok else 0,
                max(0, (finished - started) * 1000),
                device_count,
                client_count,
                issue_count,
                error,
                run_id,
            ),
        )

    # -------------------------------------------------------------- entities

    def upsert_entity(
        self,
        entity_type: str,
        entity_id: str,
        *,
        name: str | None = None,
        kind: str | None = None,
        model: str | None = None,
        meta: dict[str, Any] | None = None,
        ts: int | None = None,
    ) -> None:
        ts = ts or now()
        self.execute(
            """
            INSERT INTO entities(entity_type, entity_id, name, kind, model,
                                 first_seen, last_seen, meta)
            VALUES(?,?,?,?,?,?,?,?)
            ON CONFLICT(entity_type, entity_id) DO UPDATE SET
                name      = COALESCE(excluded.name, entities.name),
                kind      = COALESCE(excluded.kind, entities.kind),
                model     = COALESCE(excluded.model, entities.model),
                last_seen = excluded.last_seen,
                meta      = COALESCE(excluded.meta, entities.meta)
            """,
            (entity_type, entity_id, name, kind, model, ts, ts, dumps(meta) if meta else None),
        )

    def get_entity(self, entity_type: str, entity_id: str) -> dict[str, Any] | None:
        row = self.query_one(
            "SELECT * FROM entities WHERE entity_type=? AND entity_id=?", (entity_type, entity_id)
        )
        return dict(row) if row else None

    def entities(self, entity_type: str | None = None) -> list[dict[str, Any]]:
        if entity_type:
            rows = self.query("SELECT * FROM entities WHERE entity_type=?", (entity_type,))
        else:
            rows = self.query("SELECT * FROM entities")
        return [dict(r) for r in rows]

    # ----------------------------------------------------------------- state

    def get_state(self, entity_type: str, entity_id: str) -> dict[str, Any] | None:
        row = self.query_one(
            "SELECT * FROM entity_state WHERE entity_type=? AND entity_id=?",
            (entity_type, entity_id),
        )
        return dict(row) if row else None

    def set_state(
        self,
        entity_type: str,
        entity_id: str,
        state: str,
        *,
        ts: int | None = None,
        entity_name: str | None = None,
        raw_state: Any = None,
        details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Record current state and, when it changed, log a transition.

        Returns ``{"changed": bool, "previous": str|None, "since": int}`` —
        detectors need "how long has it been like this", not just "what is it".
        """
        ts = ts or now()
        prev = self.get_state(entity_type, entity_id)
        changed = prev is None or prev["state"] != state
        since = ts if changed else int(prev["since"])

        self.execute(
            """
            INSERT INTO entity_state(entity_type, entity_id, state, since, updated_at,
                                     raw_state, details)
            VALUES(?,?,?,?,?,?,?)
            ON CONFLICT(entity_type, entity_id) DO UPDATE SET
                state=excluded.state, since=excluded.since, updated_at=excluded.updated_at,
                raw_state=excluded.raw_state, details=excluded.details
            """,
            (
                entity_type,
                entity_id,
                state,
                since,
                ts,
                str(raw_state) if raw_state is not None else None,
                dumps(details) if details else None,
            ),
        )

        if changed:
            self.execute(
                """
                INSERT INTO state_transitions(ts, entity_type, entity_id, entity_name,
                                              from_state, to_state, prev_duration, raw)
                VALUES(?,?,?,?,?,?,?,?)
                """,
                (
                    ts,
                    entity_type,
                    entity_id,
                    entity_name,
                    prev["state"] if prev else None,
                    state,
                    (ts - int(prev["since"])) if prev else None,
                    dumps(details) if details else None,
                ),
            )
        return {"changed": changed, "previous": prev["state"] if prev else None, "since": since}

    def transitions_since(
        self, entity_type: str, entity_id: str, since_ts: int, to_state: str | None = None
    ) -> list[dict[str, Any]]:
        sql = (
            "SELECT * FROM state_transitions WHERE entity_type=? AND entity_id=? AND ts>=? "
        )
        params: list[Any] = [entity_type, entity_id, since_ts]
        if to_state:
            sql += "AND to_state=? "
            params.append(to_state)
        sql += "ORDER BY ts DESC"
        return [dict(r) for r in self.query(sql, params)]

    # --------------------------------------------------------------- metrics

    def add_metric(
        self,
        entity_type: str,
        entity_id: str,
        metric: str,
        value: float | None,
        *,
        ts: int | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        if value is None:
            return
        self.execute(
            "INSERT INTO metrics(ts, entity_type, entity_id, metric, value, extra) "
            "VALUES(?,?,?,?,?,?)",
            (ts or now(), entity_type, entity_id, metric, float(value), dumps(extra) if extra else None),
        )

    def add_metrics(self, rows: Iterable[tuple]) -> None:
        """Bulk insert of ``(ts, entity_type, entity_id, metric, value, extra)``."""
        payload = list(rows)
        if not payload:
            return
        with self.conn:
            self.conn.executemany(
                "INSERT INTO metrics(ts, entity_type, entity_id, metric, value, extra) "
                "VALUES(?,?,?,?,?,?)",
                payload,
            )

    def recent_metric_values(
        self, entity_type: str, entity_id: str, metric: str, limit: int = 10
    ) -> list[float]:
        rows = self.query(
            "SELECT value FROM metrics WHERE entity_type=? AND entity_id=? AND metric=? "
            "ORDER BY ts DESC LIMIT ?",
            (entity_type, entity_id, metric, limit),
        )
        return [float(r["value"]) for r in rows if r["value"] is not None]

    # -------------------------------------------------------------- counters

    def counter_delta(
        self, entity_type: str, entity_id: str, counter: str, value: float, ts: int | None = None
    ) -> tuple[float | None, float | None]:
        """Store a monotonic counter and return ``(delta, seconds_elapsed)``.

        Returns ``(None, None)`` on the first sample or when the counter went
        backwards (device rebooted, controller reset stats) — a negative rate
        is worse than no rate.
        """
        ts = ts or now()
        prev = self.query_one(
            "SELECT value, ts FROM counters WHERE entity_type=? AND entity_id=? AND counter=?",
            (entity_type, entity_id, counter),
        )
        self.execute(
            "INSERT INTO counters(entity_type, entity_id, counter, value, ts) VALUES(?,?,?,?,?) "
            "ON CONFLICT(entity_type, entity_id, counter) DO UPDATE SET "
            "value=excluded.value, ts=excluded.ts",
            (entity_type, entity_id, counter, float(value), ts),
        )
        if prev is None:
            return None, None
        delta = float(value) - float(prev["value"])
        elapsed = ts - int(prev["ts"])
        if delta < 0 or elapsed <= 0:
            return None, None
        return delta, float(elapsed)

    # ------------------------------------------------------------------- wan

    def add_wan_sample(self, sample: dict[str, Any], ts: int | None = None) -> None:
        self.execute(
            """
            INSERT INTO wan_samples(ts, wan_id, status, active, isp, ip, gateway,
                                    latency_ms, loss_pct, xput_down, xput_up, uptime, raw)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                ts or now(),
                sample.get("wan_id", "wan"),
                sample.get("status"),
                1 if sample.get("active") else 0,
                sample.get("isp"),
                sample.get("ip"),
                sample.get("gateway"),
                sample.get("latency_ms"),
                sample.get("loss_pct"),
                sample.get("xput_down"),
                sample.get("xput_up"),
                sample.get("uptime"),
                dumps(sample.get("raw")) if sample.get("raw") is not None else None,
            ),
        )

    def last_wan_sample(self, wan_id: str) -> dict[str, Any] | None:
        row = self.query_one(
            "SELECT * FROM wan_samples WHERE wan_id=? ORDER BY ts DESC LIMIT 1", (wan_id,)
        )
        return dict(row) if row else None

    # -------------------------------------------------- controller events

    def record_controller_event(
        self,
        source: str,
        remote_id: str,
        ts: int,
        *,
        key: str | None = None,
        subsystem: str | None = None,
        severity: str | None = None,
        entity_id: str | None = None,
        entity_name: str | None = None,
        message: str | None = None,
        raw: Any = None,
    ) -> bool:
        """Returns True if this is the first time we've seen this event."""
        cur = self.execute(
            """
            INSERT INTO controller_events(source, remote_id, ts, key, subsystem, severity,
                                          entity_id, entity_name, message, raw)
            VALUES(?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(source, remote_id) DO NOTHING
            """,
            (
                source,
                remote_id,
                ts,
                key,
                subsystem,
                severity,
                entity_id,
                entity_name,
                message,
                dumps(raw) if raw is not None else None,
            ),
        )
        return cur.rowcount > 0

    # ---------------------------------------------------------------- pruning

    def prune(
        self,
        *,
        metrics_days: int,
        transitions_days: int,
        events_days: int,
        issues_days: int,
    ) -> dict[str, int]:
        ts = now()
        deleted: dict[str, int] = {}
        plan = [
            ("metrics", "DELETE FROM metrics WHERE ts < ?", metrics_days),
            ("wan_samples", "DELETE FROM wan_samples WHERE ts < ?", metrics_days),
            ("state_transitions", "DELETE FROM state_transitions WHERE ts < ?", transitions_days),
            ("controller_events", "DELETE FROM controller_events WHERE ts < ?", events_days),
            (
                "issues",
                "DELETE FROM issues WHERE status != 'open' AND COALESCE(resolved_at, last_seen) < ?",
                issues_days,
            ),
            ("poll_runs", "DELETE FROM poll_runs WHERE started_at < ?", transitions_days),
        ]
        for name, sql, days in plan:
            if days <= 0:
                continue
            cur = self.execute(sql, (ts - days * 86400,))
            if cur.rowcount > 0:
                deleted[name] = cur.rowcount
        # issue_events cascade with their issue; orphans can only come from
        # older schema versions.
        self.execute(
            "DELETE FROM issue_events WHERE issue_id NOT IN (SELECT id FROM issues)"
        )
        if deleted:
            LOG.info("pruned: %s", deleted)
        return deleted


def _uri_path(path: str) -> str:
    """Make an absolute path safe inside a sqlite file: URI."""
    absolute = os.path.abspath(path).replace("?", "%3f").replace("#", "%23")
    if os.name == "nt":
        absolute = "/" + absolute.replace("\\", "/")
    return absolute
