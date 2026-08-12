"""Part 2: the explain layer.

Read-only queries over the same SQLite file Part 1 writes. Everything here
returns plain JSON-serializable dicts, so it works equally well as a Python
import, an MCP tool, or a CLI producing JSON for a gateway.

This module never polls, never writes to the monitoring tables, and never
touches the controller. It answers three shapes of question:

  1. What is wrong right now?              -> recent_issues, network_overview
  2. Tell me everything about this issue.  -> explain_issue
  3. Is this a pattern?                    -> issue_frequency, device_history

The structured output is deliberately free of diagnosis. It assembles the
evidence — what changed, when, how often it has recurred, how it differs from
baseline, what else moved at the same moment — and leaves the "why" to the
model reading it.
"""

from __future__ import annotations

import os
import re
import statistics
from collections import Counter
from datetime import datetime, timezone
from typing import Any

from .db import Database
from .util import humanize_bps, humanize_duration, load_env_file, loads, normalize_mac, now


def default_db_path() -> str:
    """Where the monitoring database lives, resolved at call time.

    Deliberately not a module constant: the env file is what carries
    ``UNIFI_DB_PATH`` on a scheduled or gateway-launched run, and it is loaded
    after this module is imported. Freezing the value at import time made Part 2
    look in ``~/.unifi_monitor`` while the poller wrote to the configured path.

    ``load_env_file`` is shared plumbing from ``util`` — reading it here does not
    make Part 2 depend on the polling path.
    """
    load_env_file()
    return os.environ.get("UNIFI_DB_PATH") or os.path.join(
        os.environ.get("UNIFI_MONITOR_HOME")
        or os.path.join(os.path.expanduser("~"), ".unifi_monitor"),
        "unifi_monitor.db",
    )

ISSUE_JSON_FIELDS = ("details", "trigger_data")

# How long around an incident to look for things that moved with it.
CORRELATION_WINDOW_S = 900


def open_db(db_path: str | None = None) -> Database:
    """Open the monitoring database read-only.

    Read-only is enforced at the connection level, not by convention: Part 2
    physically cannot corrupt the poller's state.
    """
    return Database(db_path or default_db_path(), read_only=True)


# --------------------------------------------------------------------- basics


def network_overview(db_path: str | None = None) -> dict[str, Any]:
    """Current health at a glance, plus whether the poller itself is alive."""
    with open_db(db_path) as db:
        last_run = db.query_one("SELECT * FROM poll_runs ORDER BY id DESC LIMIT 1")
        devices = db.query(
            """
            SELECT e.entity_id, e.name, e.kind, e.model, s.state, s.since
            FROM entities e LEFT JOIN entity_state s
              ON s.entity_type=e.entity_type AND s.entity_id=e.entity_id
            WHERE e.entity_type='device'
            ORDER BY e.name
            """
        )
        wans = db.query(
            """
            SELECT e.entity_id, e.name, s.state, s.since, s.details
            FROM entities e LEFT JOIN entity_state s
              ON s.entity_type=e.entity_type AND s.entity_id=e.entity_id
            WHERE e.entity_type='wan'
            """
        )
        open_by_severity = {
            row["severity"]: int(row["n"])
            for row in db.query(
                "SELECT severity, COUNT(*) n FROM issues WHERE status='open' GROUP BY severity"
            )
        }
        clients_online = db.query_one(
            "SELECT COUNT(*) n FROM entity_state WHERE entity_type='client' AND state='online'"
        )

        # Part 2 does not read Part 1's config, so infer the expected cadence
        # from how often polls have actually been landing.
        stale = None
        expected_interval = None
        if last_run:
            recent = [
                int(r["started_at"])
                for r in db.query("SELECT started_at FROM poll_runs ORDER BY id DESC LIMIT 12")
            ]
            gaps = sorted(a - b for a, b in zip(recent, recent[1:]) if a > b)
            expected_interval = gaps[len(gaps) // 2] if gaps else 300
            stale = (now() - int(last_run["started_at"])) > 3 * expected_interval

        return {
            "generated_at": now(),
            "poller": {
                "last_run_at": int(last_run["started_at"]) if last_run else None,
                "last_run_ok": bool(last_run["ok"]) if last_run else None,
                "last_run_error": last_run["error"] if last_run else None,
                "seconds_since_last_run": (now() - int(last_run["started_at"]))
                if last_run
                else None,
                "possibly_stalled": stale,
                "expected_interval_s": expected_interval,
            },
            "devices": {
                "total": len(devices),
                "online": sum(1 for d in devices if d["state"] == "online"),
                "offline": [
                    {
                        "id": d["entity_id"],
                        "name": d["name"],
                        "kind": d["kind"],
                        "offline_for_s": now() - int(d["since"]) if d["since"] else None,
                    }
                    for d in devices
                    if d["state"] and d["state"] != "online"
                ],
            },
            "clients_online": int(clients_online["n"]) if clients_online else 0,
            "wan": [
                {
                    "id": w["entity_id"],
                    "name": w["name"],
                    "state": w["state"],
                    "since": int(w["since"]) if w["since"] else None,
                    "details": loads(w["details"]),
                }
                for w in wans
            ],
            "open_issues": {
                "critical": open_by_severity.get("critical", 0),
                "warning": open_by_severity.get("warning", 0),
                "info": open_by_severity.get("info", 0),
                "total": sum(open_by_severity.values()),
            },
        }


def recent_issues(
    *,
    status: str | None = "open",
    severity: str | None = None,
    issue_type: str | None = None,
    entity: str | None = None,
    hours: int | None = None,
    limit: int = 25,
    db_path: str | None = None,
) -> list[dict[str, Any]]:
    """Flagged issues, newest activity first.

    ``entity`` accepts a MAC or a name fragment ("cow cam", "AP-3").
    """
    sql = ["SELECT * FROM issues WHERE 1=1"]
    params: list[Any] = []
    if status and status != "any":
        sql.append("AND status=?")
        params.append(status)
    if severity:
        sql.append("AND severity=?")
        params.append(severity)
    if issue_type:
        sql.append("AND issue_type=?")
        params.append(issue_type)
    if hours:
        sql.append("AND last_seen >= ?")
        params.append(now() - hours * 3600)
    if entity:
        mac = normalize_mac(entity)
        sql.append("AND (entity_id=? OR LOWER(entity_name) LIKE ? OR LOWER(entity_id) LIKE ?)")
        params.extend([mac or entity, f"%{entity.lower()}%", f"%{entity.lower()}%"])
    sql.append("ORDER BY last_seen DESC LIMIT ?")
    params.append(max(1, min(limit, 500)))

    with open_db(db_path) as db:
        rows = db.query(" ".join(sql), params)
        return [_issue_row(row) for row in rows]


def get_issue(issue_id: int, db_path: str | None = None) -> dict[str, Any] | None:
    with open_db(db_path) as db:
        row = db.query_one("SELECT * FROM issues WHERE id=?", (issue_id,))
        if row is None:
            return None
        issue = _issue_row(row)
        issue["timeline"] = _timeline(db, issue_id)
        return issue


def _flatten_name(value: str) -> str:
    """Reduce a name to words, so separator style stops mattering.

    ``cow-cam``, ``cow_cam``, ``cow.cam`` and ``Cow Cam`` all flatten to
    ``cow cam``. CamelCase is split too, since controller clients often arrive
    as ``CowCam``.
    """
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", value or "")
    return re.sub(r"[^a-z0-9]+", " ", spaced.lower()).strip()


def find_entity(query: str, db_path: str | None = None, limit: int = 10) -> list[dict[str, Any]]:
    """Resolve a human phrase to monitored entities.

    "why does the cow cam keep dropping" arrives as the words a person uses,
    not a MAC — so match names, hostnames, MACs and IPs, and rank exact hits
    above substring hits.
    """
    needle = (query or "").strip().lower()
    if not needle:
        return []
    mac = normalize_mac(needle)
    flat_needle = _flatten_name(needle)
    tokens = [t for t in flat_needle.split(" ") if t]
    # Controller names are written "cow-cam" / "cow_cam" / "CowCam"; people say
    # "cow cam". Prefilter on the longest word so the scan stays selective, then
    # compare separator-insensitively below.
    anchor = max(tokens, key=len) if tokens else needle
    with open_db(db_path) as db:
        rows = db.query(
            """
            SELECT e.*, s.state, s.since
            FROM entities e LEFT JOIN entity_state s
              ON s.entity_type=e.entity_type AND s.entity_id=e.entity_id
            WHERE LOWER(e.name) LIKE ? OR LOWER(e.entity_id) LIKE ? OR e.entity_id=?
               OR LOWER(COALESCE(e.meta,'')) LIKE ?
               OR LOWER(e.name) LIKE ?
            """,
            (
                f"%{needle}%",
                f"%{needle}%",
                mac or needle,
                f'%"ip":"{needle}"%',
                f"%{anchor}%",
            ),
        )
        results = []
        for row in rows:
            name = (row["name"] or "").lower()
            flat_name = _flatten_name(name)
            is_mac_hit = row["entity_id"] == (mac or needle)
            exact = name == needle or flat_name == flat_needle or is_mac_hit
            if not exact:
                # The anchor prefilter is deliberately loose; drop rows that do
                # not actually contain every word the person said.
                phrase_hit = flat_needle and flat_needle in flat_name
                token_hit = tokens and all(t in flat_name for t in tokens)
                if not (phrase_hit or token_hit or needle in name or needle in row["entity_id"]):
                    continue
            results.append(
                {
                    "entity_type": row["entity_type"],
                    "entity_id": row["entity_id"],
                    "name": row["name"],
                    "kind": row["kind"],
                    "model": row["model"],
                    "state": row["state"],
                    "state_since": int(row["since"]) if row["since"] else None,
                    "last_seen": int(row["last_seen"]),
                    "meta": loads(row["meta"]),
                    "_score": (0 if exact else 1, len(name or "")),
                }
            )
        results.sort(key=lambda r: r.pop("_score"))
        return results[:limit]


# ------------------------------------------------------------------- history


def device_history(
    entity: str,
    *,
    hours: int = 168,
    db_path: str | None = None,
) -> dict[str, Any]:
    """Everything known about one device/client/port/WAN over a window."""
    since = now() - hours * 3600
    with open_db(db_path) as db:
        resolved = _resolve(db, entity)
        if not resolved:
            return {"error": f"no monitored entity matches {entity!r}", "query": entity}
        entity_type, entity_id, name = resolved

        transitions = db.query(
            "SELECT * FROM state_transitions WHERE entity_type=? AND entity_id=? AND ts>=? "
            "ORDER BY ts DESC LIMIT 500",
            (entity_type, entity_id, since),
        )
        issues = db.query(
            "SELECT * FROM issues WHERE entity_id=? AND last_seen>=? ORDER BY last_seen DESC "
            "LIMIT 100",
            (entity_id, since),
        )
        events = db.query(
            "SELECT * FROM controller_events WHERE entity_id=? AND ts>=? ORDER BY ts DESC LIMIT 100",
            (entity_id, since),
        )
        state = db.get_state(entity_type, entity_id)

        return {
            "entity": {
                "entity_type": entity_type,
                "entity_id": entity_id,
                "name": name,
                "state": state["state"] if state else None,
                "state_since": int(state["since"]) if state else None,
                "state_for": humanize_duration(now() - int(state["since"])) if state else None,
            },
            "window_hours": hours,
            "availability": _availability(db, entity_type, entity_id, since),
            "state_transitions": [
                {
                    "ts": int(t["ts"]),
                    "at": _iso(int(t["ts"])),
                    "from": t["from_state"],
                    "to": t["to_state"],
                    "previous_state_lasted_s": t["prev_duration"],
                }
                for t in transitions
            ],
            "issues": [_issue_row(i) for i in issues],
            "controller_events": [
                {
                    "ts": int(e["ts"]),
                    "at": _iso(int(e["ts"])),
                    "source": e["source"],
                    "key": e["key"],
                    "message": e["message"],
                }
                for e in events
            ],
            "metrics": _metric_summary(db, entity_type, entity_id, since),
        }


def issue_frequency(
    *,
    entity: str | None = None,
    issue_type: str | None = None,
    days: int = 30,
    db_path: str | None = None,
) -> dict[str, Any]:
    """Is this a one-off or a pattern? Counts, intervals, and time-of-day."""
    since = now() - days * 86400
    sql = ["SELECT * FROM issues WHERE first_seen >= ?"]
    params: list[Any] = [since]
    if entity:
        mac = normalize_mac(entity)
        sql.append("AND (entity_id=? OR LOWER(entity_name) LIKE ?)")
        params.extend([mac or entity, f"%{entity.lower()}%"])
    if issue_type:
        sql.append("AND issue_type=?")
        params.append(issue_type)
    sql.append("ORDER BY first_seen")

    with open_db(db_path) as db:
        rows = db.query(" ".join(sql), params)
        return _frequency_stats([dict(r) for r in rows], days)


# ------------------------------------------------------------------- explain


def explain_issue(issue_id: int, db_path: str | None = None) -> dict[str, Any]:
    """The full evidence package for one flagged issue.

    Returns facts only: what changed and when, how often it has recurred,
    how the entity's metrics compare to its own baseline, and what else on
    the network moved inside the same window. Root cause is the model's job.
    """
    with open_db(db_path) as db:
        row = db.query_one("SELECT * FROM issues WHERE id=?", (issue_id,))
        if row is None:
            return {"error": f"no issue with id {issue_id}"}
        issue = _issue_row(row)
        entity_type = str(row["entity_type"])
        entity_id = str(row["entity_id"])
        first_seen = int(row["first_seen"])

        prior = db.query(
            "SELECT * FROM issues WHERE entity_id=? AND issue_type=? AND id!=? "
            "ORDER BY first_seen DESC LIMIT 50",
            (entity_id, row["issue_type"], issue_id),
        )
        prior_rows = [dict(p) for p in prior]

        window_start = first_seen - CORRELATION_WINDOW_S
        window_end = int(row["last_seen"]) + CORRELATION_WINDOW_S

        report = {
            "issue": issue,
            "entity": _entity_card(db, entity_type, entity_id),
            "what_changed": _what_changed(db, entity_type, entity_id, first_seen),
            "timeline": _timeline(db, issue_id),
            "recurrence": _recurrence(prior_rows, current=dict(row)),
            "baseline_comparison": _baseline(db, entity_type, entity_id, first_seen),
            "correlated_activity": _correlations(db, entity_type, entity_id, window_start, window_end),
            "related_open_issues": [
                _issue_row(r)
                for r in db.query(
                    "SELECT * FROM issues WHERE status='open' AND id!=? "
                    "ORDER BY severity='critical' DESC, last_seen DESC LIMIT 10",
                    (issue_id,),
                )
            ],
            "poller_health": _poller_health(db, window_start, window_end),
        }

    # Remediation lives in its own module so the catalogue can grow without
    # touching the query surface. Imported late to keep query.py dependency-free.
    from .remediation import propose_actions

    report["proposed_actions"] = propose_actions(issue, report)
    report["facts"] = _fact_bullets(report)
    return report


def explain_entity(entity: str, hours: int = 168, db_path: str | None = None) -> dict[str, Any]:
    """Chat entry point: "why does the cow cam keep dropping".

    Resolves the phrase to an entity, then hands back its most recent issue
    fully explained, plus the pattern across the window.
    """
    matches = find_entity(entity, db_path=db_path)
    if not matches:
        return {"error": f"nothing monitored matches {entity!r}", "query": entity}

    best = matches[0]
    history = device_history(best["entity_id"], hours=hours, db_path=db_path)
    frequency = issue_frequency(entity=best["entity_id"], days=max(1, hours // 24), db_path=db_path)

    latest = history.get("issues") or []
    explanation = explain_issue(int(latest[0]["id"]), db_path=db_path) if latest else None

    return {
        "query": entity,
        "resolved_to": best,
        "other_matches": matches[1:5],
        "history": history,
        "frequency": frequency,
        "latest_issue_explained": explanation,
        "facts": (explanation or {}).get("facts")
        or _entity_fact_bullets(best, history, frequency),
    }


def new_critical_issues(
    since_ts: int | None = None, *, minutes: int = 30, db_path: str | None = None
) -> list[dict[str, Any]]:
    """For proactive push: critical issues opened since a watermark.

    The gateway polls this (or is handed a webhook by Part 1) and decides
    whether to say something unprompted.
    """
    cutoff = since_ts if since_ts is not None else now() - minutes * 60
    with open_db(db_path) as db:
        rows = db.query(
            "SELECT * FROM issues WHERE severity='critical' AND first_seen >= ? "
            "ORDER BY first_seen DESC LIMIT 50",
            (cutoff,),
        )
        return [_issue_row(r) for r in rows]


def summarize_for_llm(report: dict[str, Any]) -> str:
    """Compact text rendering of an explain report, for a chat reply."""
    if "error" in report:
        return str(report["error"])
    issue = report.get("issue") or {}
    lines = [
        f"ISSUE #{issue.get('id')} [{issue.get('severity')}] {issue.get('issue_type')}",
        f"  {issue.get('summary')}",
        f"  entity     : {issue.get('entity_name')} ({issue.get('entity_id')})",
        f"  first seen : {issue.get('first_seen_iso')}",
        f"  last seen  : {issue.get('last_seen_iso')}",
        f"  status     : {issue.get('status')} after {humanize_duration(issue.get('duration_s'))}",
        "",
        "FACTS",
    ]
    lines.extend(f"  - {fact}" for fact in report.get("facts", []))

    actions = report.get("proposed_actions") or []
    if actions:
        lines.append("")
        lines.append("PROPOSED ACTIONS (require explicit user confirmation; none executed)")
        for action in actions:
            label = action.get("id") or action.get("action_key")
            lines.append(f"  - [{label}] {action['title']} — {action['rationale']}")
    return "\n".join(lines)


# ------------------------------------------------------------------ internals


def _issue_row(row: Any) -> dict[str, Any]:
    data = dict(row)
    for field in ISSUE_JSON_FIELDS:
        if field in data:
            data[field] = loads(data[field])
    first, last = int(data.get("first_seen") or 0), int(data.get("last_seen") or 0)
    data["first_seen_iso"] = _iso(first)
    data["last_seen_iso"] = _iso(last)
    end = int(data["resolved_at"]) if data.get("resolved_at") else now()
    data["duration_s"] = max(0, end - first)
    data["duration_human"] = humanize_duration(data["duration_s"])
    return data


def _timeline(db: Database, issue_id: int) -> list[dict[str, Any]]:
    rows = db.query(
        "SELECT * FROM issue_events WHERE issue_id=? ORDER BY ts ASC LIMIT 500", (issue_id,)
    )
    timeline = []
    for row in rows:
        # 'observed' rows repeat every poll; keep the shape but drop the bulk.
        timeline.append(
            {
                "ts": int(row["ts"]),
                "at": _iso(int(row["ts"])),
                "kind": row["kind"],
                "severity": row["severity"],
                "summary": row["summary"],
                "details": loads(row["details"]),
            }
        )
    if len(timeline) > 40:
        head, tail = timeline[:15], timeline[-15:]
        skipped = len(timeline) - 30
        return head + [{"kind": "elided", "summary": f"... {skipped} repeat observations ..."}] + tail
    return timeline


def _resolve(db: Database, entity: str) -> tuple[str, str, str] | None:
    mac = normalize_mac(entity)
    row = db.query_one(
        "SELECT entity_type, entity_id, name FROM entities WHERE entity_id=? OR entity_id=? "
        "OR LOWER(name)=? ORDER BY last_seen DESC LIMIT 1",
        (entity, mac or entity, entity.lower()),
    )
    if row is None:
        row = db.query_one(
            "SELECT entity_type, entity_id, name FROM entities WHERE LOWER(name) LIKE ? "
            "ORDER BY last_seen DESC LIMIT 1",
            (f"%{entity.lower()}%",),
        )
    if row is None:
        return None
    return str(row["entity_type"]), str(row["entity_id"]), str(row["name"] or row["entity_id"])


def _entity_card(db: Database, entity_type: str, entity_id: str) -> dict[str, Any]:
    entity = db.get_entity(entity_type, entity_id) or {}
    state = db.get_state(entity_type, entity_id) or {}
    return {
        "entity_type": entity_type,
        "entity_id": entity_id,
        "name": entity.get("name"),
        "kind": entity.get("kind"),
        "model": entity.get("model"),
        "meta": loads(entity.get("meta")) if entity.get("meta") else None,
        "first_seen": entity.get("first_seen"),
        "current_state": state.get("state"),
        "state_since": state.get("since"),
        "state_for": humanize_duration(now() - int(state["since"])) if state.get("since") else None,
    }


def _what_changed(
    db: Database, entity_type: str, entity_id: str, first_seen: int
) -> dict[str, Any]:
    """The transition that started this, and how long the good state held."""
    row = db.query_one(
        "SELECT * FROM state_transitions WHERE entity_type=? AND entity_id=? AND ts<=? "
        "ORDER BY ts DESC LIMIT 1",
        (entity_type, entity_id, first_seen + 60),
    )
    if row is None:
        return {"known": False}
    return {
        "known": True,
        "at": _iso(int(row["ts"])),
        "ts": int(row["ts"]),
        "from_state": row["from_state"],
        "to_state": row["to_state"],
        "previous_state_held_s": row["prev_duration"],
        "previous_state_held": humanize_duration(row["prev_duration"])
        if row["prev_duration"]
        else None,
    }


def _availability(db: Database, entity_type: str, entity_id: str, since: int) -> dict[str, Any]:
    """Rough uptime over the window, from transition history."""
    rows = db.query(
        "SELECT ts, to_state, prev_duration FROM state_transitions "
        "WHERE entity_type=? AND entity_id=? AND ts>=? ORDER BY ts ASC",
        (entity_type, entity_id, since),
    )
    down_total = 0
    drops = 0
    for row in rows:
        if row["to_state"] in ("online", "up") and row["prev_duration"]:
            down_total += int(row["prev_duration"])
        if row["to_state"] in ("offline", "down"):
            drops += 1
    state = db.get_state(entity_type, entity_id)
    if state and state["state"] in ("offline", "down"):
        down_total += now() - int(state["since"])
    window = max(1, now() - since)
    return {
        "window_s": window,
        "downtime_s": down_total,
        "uptime_pct": round(100.0 * (1 - min(1.0, down_total / window)), 3),
        "disconnects": drops,
    }


def _recurrence(prior: list[dict[str, Any]], current: dict[str, Any]) -> dict[str, Any]:
    """How often has this exact issue hit this entity before?"""
    all_rows = sorted(prior + [current], key=lambda r: int(r["first_seen"]))
    starts = [int(r["first_seen"]) for r in all_rows]
    durations = [
        max(0, int(r["resolved_at"] or r["last_seen"]) - int(r["first_seen"]))
        for r in all_rows
        if r["status"] != "open"
    ]
    intervals = [round((b - a) / 3600.0, 2) for a, b in zip(starts, starts[1:])]
    ts_now = now()

    by_hour = Counter(_dt(s).hour for s in starts)
    by_weekday = Counter(_dt(s).strftime("%a") for s in starts)

    return {
        "total_occurrences": len(all_rows),
        "occurrences_24h": sum(1 for s in starts if s >= ts_now - 86400),
        "occurrences_7d": sum(1 for s in starts if s >= ts_now - 7 * 86400),
        "occurrences_30d": sum(1 for s in starts if s >= ts_now - 30 * 86400),
        "first_ever": _iso(starts[0]) if starts else None,
        "previous_occurrence": _iso(starts[-2]) if len(starts) > 1 else None,
        "hours_since_previous": intervals[-1] if intervals else None,
        "interval_hours": intervals[-20:],
        "median_interval_hours": round(statistics.median(intervals), 2) if intervals else None,
        "median_duration_s": int(statistics.median(durations)) if durations else None,
        "median_duration": humanize_duration(statistics.median(durations)) if durations else None,
        "clustering_by_hour_of_day": dict(sorted(by_hour.items())),
        "clustering_by_weekday": dict(by_weekday),
        "is_recurring": len(all_rows) >= 3,
    }


def _baseline(
    db: Database, entity_type: str, entity_id: str, first_seen: int
) -> dict[str, Any]:
    """Compare the hour around the incident against the prior week.

    The interesting metric is rarely "high" in absolute terms — it is "higher
    than this device's own normal".
    """
    metrics = db.query(
        "SELECT DISTINCT metric FROM metrics WHERE entity_type=? AND entity_id=?",
        (entity_type, entity_id),
    )
    comparison: dict[str, Any] = {}
    for row in metrics:
        metric = str(row["metric"])
        recent = [
            float(r["value"])
            for r in db.query(
                "SELECT value FROM metrics WHERE entity_type=? AND entity_id=? AND metric=? "
                "AND ts BETWEEN ? AND ?",
                (entity_type, entity_id, metric, first_seen - 3600, first_seen + 900),
            )
            if r["value"] is not None
        ]
        historical = [
            float(r["value"])
            for r in db.query(
                "SELECT value FROM metrics WHERE entity_type=? AND entity_id=? AND metric=? "
                "AND ts BETWEEN ? AND ?",
                (entity_type, entity_id, metric, first_seen - 7 * 86400, first_seen - 3600),
            )
            if r["value"] is not None
        ]
        if not recent or len(historical) < 3:
            continue
        recent_mean = statistics.mean(recent)
        base_mean = statistics.mean(historical)
        delta_pct = (
            round(100.0 * (recent_mean - base_mean) / abs(base_mean), 1) if base_mean else None
        )
        entry = {
            "around_incident": round(recent_mean, 2),
            "baseline_7d": round(base_mean, 2),
            "baseline_stdev": round(statistics.pstdev(historical), 2) if len(historical) > 1 else 0.0,
            "delta_pct": delta_pct,
            "samples": {"recent": len(recent), "baseline": len(historical)},
        }
        if metric.endswith("_bps"):
            entry["around_incident_human"] = humanize_bps(recent_mean)
            entry["baseline_human"] = humanize_bps(base_mean)
        # Flag only moves that clear both a relative and an absolute-ish bar,
        # so noisy near-zero metrics don't shout.
        entry["notable"] = bool(
            delta_pct is not None
            and abs(delta_pct) >= 30
            and abs(recent_mean - base_mean) > (entry["baseline_stdev"] or 0)
        )
        comparison[metric] = entry
    return comparison


def _correlations(
    db: Database, entity_type: str, entity_id: str, window_start: int, window_end: int
) -> dict[str, Any]:
    """What else on the network moved inside the same window?"""
    other_transitions = db.query(
        "SELECT * FROM state_transitions WHERE ts BETWEEN ? AND ? "
        "AND NOT (entity_type=? AND entity_id=?) ORDER BY ts LIMIT 100",
        (window_start, window_end, entity_type, entity_id),
    )
    events = db.query(
        "SELECT * FROM controller_events WHERE ts BETWEEN ? AND ? ORDER BY ts LIMIT 100",
        (window_start, window_end),
    )
    wan = db.query(
        "SELECT * FROM wan_samples WHERE ts BETWEEN ? AND ? ORDER BY ts LIMIT 50",
        (window_start, window_end),
    )
    other_issues = db.query(
        "SELECT * FROM issues WHERE first_seen BETWEEN ? AND ? AND entity_id!=? "
        "ORDER BY first_seen LIMIT 50",
        (window_start, window_end, entity_id),
    )
    return {
        "window": {"from": _iso(window_start), "to": _iso(window_end)},
        "other_entities_changed_state": [
            {
                "at": _iso(int(t["ts"])),
                "entity_type": t["entity_type"],
                "entity_id": t["entity_id"],
                "name": t["entity_name"],
                "from": t["from_state"],
                "to": t["to_state"],
            }
            for t in other_transitions
        ],
        "controller_events": [
            {
                "at": _iso(int(e["ts"])),
                "source": e["source"],
                "key": e["key"],
                "entity": e["entity_name"] or e["entity_id"],
                "message": e["message"],
            }
            for e in events
        ],
        "wan_samples": [
            {
                "at": _iso(int(w["ts"])),
                "wan": w["wan_id"],
                "status": w["status"],
                "isp": w["isp"],
                "ip": w["ip"],
                "latency_ms": w["latency_ms"],
            }
            for w in wan
        ],
        "other_issues_opened": [
            {
                "id": int(i["id"]),
                "at": _iso(int(i["first_seen"])),
                "severity": i["severity"],
                "type": i["issue_type"],
                "summary": i["summary"],
            }
            for i in other_issues
        ],
    }


def _poller_health(db: Database, window_start: int, window_end: int) -> dict[str, Any]:
    """Did we actually have eyes on the network during this window?

    A "device offline" that coincides with three failed polls may be the
    monitor's problem, not the network's — the model needs to know that.
    """
    rows = db.query(
        "SELECT ok, started_at, error FROM poll_runs WHERE started_at BETWEEN ? AND ? "
        "ORDER BY started_at",
        (window_start, window_end),
    )
    failures = [dict(r) for r in rows if not r["ok"]]
    return {
        "polls_in_window": len(rows),
        "failed_polls": len(failures),
        "failure_samples": [
            {"at": _iso(int(f["started_at"])), "error": (f["error"] or "")[:200]}
            for f in failures[:5]
        ],
        "coverage_complete": len(failures) == 0 and len(rows) > 0,
    }


def _metric_summary(
    db: Database, entity_type: str, entity_id: str, since: int
) -> dict[str, Any]:
    rows = db.query(
        """
        SELECT metric, COUNT(*) n, AVG(value) avg_v, MIN(value) min_v, MAX(value) max_v
        FROM metrics WHERE entity_type=? AND entity_id=? AND ts>=?
        GROUP BY metric
        """,
        (entity_type, entity_id, since),
    )
    summary: dict[str, Any] = {}
    for row in rows:
        metric = str(row["metric"])
        entry = {
            "samples": int(row["n"]),
            "avg": round(float(row["avg_v"]), 2) if row["avg_v"] is not None else None,
            "min": round(float(row["min_v"]), 2) if row["min_v"] is not None else None,
            "max": round(float(row["max_v"]), 2) if row["max_v"] is not None else None,
        }
        if metric.endswith("_bps") and entry["avg"] is not None:
            entry["avg_human"] = humanize_bps(entry["avg"])
            entry["max_human"] = humanize_bps(entry["max"])
        summary[metric] = entry
    return summary


def _frequency_stats(rows: list[dict[str, Any]], days: int) -> dict[str, Any]:
    starts = [int(r["first_seen"]) for r in rows]
    by_day = Counter(_dt(s).strftime("%Y-%m-%d") for s in starts)
    by_hour = Counter(_dt(s).hour for s in starts)
    by_type = Counter(str(r["issue_type"]) for r in rows)
    by_entity = Counter(str(r["entity_name"] or r["entity_id"]) for r in rows)
    intervals = [round((b - a) / 3600.0, 2) for a, b in zip(starts, starts[1:])]
    return {
        "window_days": days,
        "total": len(rows),
        "per_day_average": round(len(rows) / max(1, days), 2),
        "by_day": dict(sorted(by_day.items())),
        "by_hour_of_day": dict(sorted(by_hour.items())),
        "by_issue_type": dict(by_type.most_common()),
        "by_entity": dict(by_entity.most_common(20)),
        "median_interval_hours": round(statistics.median(intervals), 2) if intervals else None,
        "worst_day": by_day.most_common(1)[0] if by_day else None,
        "issues": [
            {
                "id": int(r["id"]),
                "at": _iso(int(r["first_seen"])),
                "severity": r["severity"],
                "type": r["issue_type"],
                "entity": r["entity_name"] or r["entity_id"],
                "summary": r["summary"],
                "status": r["status"],
            }
            for r in rows[-100:]
        ],
    }


def _fact_bullets(report: dict[str, Any]) -> list[str]:
    """Plain statements a model can quote. No causal claims."""
    issue = report.get("issue") or {}
    facts: list[str] = [
        f"{issue.get('summary')} (issue #{issue.get('id')}, {issue.get('severity')}, "
        f"{issue.get('status')})",
        f"First flagged {issue.get('first_seen_iso')}, still counted as ongoing for "
        f"{issue.get('duration_human')}."
        if issue.get("status") == "open"
        else f"Ran from {issue.get('first_seen_iso')} for {issue.get('duration_human')}.",
    ]

    changed = report.get("what_changed") or {}
    if changed.get("known"):
        facts.append(
            f"State went {changed.get('from_state')} -> {changed.get('to_state')} at "
            f"{changed.get('at')}; the previous state had held for "
            f"{changed.get('previous_state_held') or 'an unknown time'}."
        )

    rec = report.get("recurrence") or {}
    if rec.get("total_occurrences", 0) > 1:
        facts.append(
            f"This has happened {rec['total_occurrences']}x total "
            f"({rec.get('occurrences_24h', 0)}x in 24h, {rec.get('occurrences_7d', 0)}x in 7d), "
            f"median gap {rec.get('median_interval_hours')}h, median duration "
            f"{rec.get('median_duration') or 'n/a'}."
        )
        hours = rec.get("clustering_by_hour_of_day") or {}
        if hours:
            peak_hour, peak_count = max(hours.items(), key=lambda kv: kv[1])
            if peak_count >= 2 and peak_count >= 0.4 * rec["total_occurrences"]:
                facts.append(
                    f"Occurrences cluster around {int(peak_hour):02d}:00 UTC "
                    f"({peak_count} of {rec['total_occurrences']})."
                )
    else:
        facts.append("No prior occurrence of this issue type on this entity in the retained history.")

    for metric, entry in (report.get("baseline_comparison") or {}).items():
        if entry.get("notable"):
            facts.append(
                f"{metric} around the incident was {entry.get('around_incident_human') or entry['around_incident']} "
                f"vs a 7-day baseline of {entry.get('baseline_human') or entry['baseline_7d']} "
                f"({entry['delta_pct']:+}%)."
            )

    corr = report.get("correlated_activity") or {}
    others = corr.get("other_entities_changed_state") or []
    if others:
        names = sorted({str(o["name"] or o["entity_id"]) for o in others})[:6]
        facts.append(
            f"{len(others)} other state change(s) in the same +/-15 min window: {', '.join(names)}."
        )
    else:
        facts.append("Nothing else on the network changed state in the same +/-15 min window.")

    wan_samples = corr.get("wan_samples") or []
    bad_wan = [w for w in wan_samples if str(w.get("status") or "").lower() not in ("ok", "up")]
    if bad_wan:
        facts.append(f"WAN was not healthy during the window ({len(bad_wan)} bad sample(s)).")

    health = report.get("poller_health") or {}
    if health.get("failed_polls"):
        facts.append(
            f"Caution: {health['failed_polls']} of {health['polls_in_window']} polls in the window "
            "failed, so the timeline may have gaps."
        )
    return facts


def _entity_fact_bullets(
    entity: dict[str, Any], history: dict[str, Any], frequency: dict[str, Any]
) -> list[str]:
    availability = history.get("availability") or {}
    return [
        f"{entity.get('name')} ({entity.get('entity_type')}) is currently "
        f"{entity.get('state') or 'unknown'}.",
        f"Over the last {history.get('window_hours')}h: {availability.get('disconnects', 0)} "
        f"disconnect(s), {availability.get('uptime_pct')}% up, "
        f"{humanize_duration(availability.get('downtime_s'))} down.",
        f"{frequency.get('total')} issue(s) flagged in {frequency.get('window_days')} days.",
        "No issues have been flagged for it — the drops may be below threshold or "
        "outside what the poller records."
        if not frequency.get("total")
        else f"Most common: {next(iter((frequency.get('by_issue_type') or {}).items()), ('none', 0))}.",
    ]


def _iso(ts: int | None) -> str | None:
    if not ts:
        return None
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")


def _dt(ts: int) -> datetime:
    return datetime.fromtimestamp(int(ts), tz=timezone.utc)
