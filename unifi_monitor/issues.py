"""Issue lifecycle: observations in, open/escalated/resolved issues out.

A detector emits an :class:`Observation` every poll for as long as the
condition holds. This module collapses that stream into stable issue rows so
that "AP-3 has been offline for 12 minutes" is one issue that ages, not a new
issue every five minutes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from .db import Database
from .util import LOG, dumps, loads, now, slugify

SEVERITY_ORDER = {"info": 0, "warning": 1, "critical": 2}


def severity_rank(severity: str | None) -> int:
    return SEVERITY_ORDER.get((severity or "info").lower(), 0)


def max_severity(a: str | None, b: str | None) -> str:
    return a if severity_rank(a) >= severity_rank(b) else b  # type: ignore[return-value]


@dataclass
class Observation:
    """One detector saying "this is wrong, right now"."""

    issue_type: str
    severity: str
    entity_type: str
    entity_id: str
    summary: str  # bare fact only — diagnosis is Part 2's job
    entity_name: str | None = None
    details: dict[str, Any] = field(default_factory=dict)
    trigger_data: Any = None

    @property
    def fingerprint(self) -> str:
        return f"{self.issue_type}:{self.entity_type}:{slugify(self.entity_id) or self.entity_id}"


@dataclass
class IssueChange:
    """Something worth telling a human about."""

    kind: str  # opened | escalated | resolved
    issue_id: int
    issue_type: str
    severity: str
    entity_type: str
    entity_id: str
    entity_name: str | None
    summary: str
    first_seen: int
    last_seen: int
    details: dict[str, Any]
    previous_severity: str | None = None


class IssueStore:
    def __init__(self, db: Database):
        self.db = db

    # ---------------------------------------------------------------- lookup

    def open_issues(self) -> list[dict[str, Any]]:
        return [
            dict(r) for r in self.db.query("SELECT * FROM issues WHERE status='open' ORDER BY id")
        ]

    def get(self, issue_id: int) -> dict[str, Any] | None:
        row = self.db.query_one("SELECT * FROM issues WHERE id=?", (issue_id,))
        return dict(row) if row else None

    # ----------------------------------------------------------------- write

    def sync(
        self,
        observations: Iterable[Observation],
        *,
        active_types: set[str] | None = None,
        ts: int | None = None,
    ) -> list[IssueChange]:
        """Reconcile this poll's observations against currently-open issues.

        ``active_types`` names the issue types whose detectors actually ran
        with good data this cycle. Open issues of those types that were *not*
        observed are resolved; issue types whose data source failed are left
        alone rather than being falsely cleared.
        """
        ts = ts or now()
        observations = list(observations)
        changes: list[IssueChange] = []
        seen_fingerprints: set[str] = set()

        for obs in observations:
            seen_fingerprints.add(obs.fingerprint)
            change = self._record(obs, ts)
            if change:
                changes.append(change)

        if active_types is not None:
            changes.extend(self._resolve_missing(seen_fingerprints, active_types, ts))
        return changes

    def _record(self, obs: Observation, ts: int) -> IssueChange | None:
        existing = self.db.query_one(
            "SELECT * FROM issues WHERE fingerprint=? AND status='open'", (obs.fingerprint,)
        )
        details_json = dumps(obs.details) if obs.details else None
        trigger_json = dumps(obs.trigger_data) if obs.trigger_data is not None else None

        if existing is None:
            cur = self.db.execute(
                """
                INSERT INTO issues(fingerprint, issue_type, severity, max_severity, status,
                                   entity_type, entity_id, entity_name, summary,
                                   first_seen, last_seen, occurrences, details, trigger_data)
                VALUES(?,?,?,?, 'open', ?,?,?,?,?,?,1,?,?)
                """,
                (
                    obs.fingerprint,
                    obs.issue_type,
                    obs.severity,
                    obs.severity,
                    obs.entity_type,
                    obs.entity_id,
                    obs.entity_name,
                    obs.summary,
                    ts,
                    ts,
                    details_json,
                    trigger_json,
                ),
            )
            issue_id = int(cur.lastrowid or 0)
            self._log_event(issue_id, ts, "opened", obs.severity, obs.summary, details_json, trigger_json)
            LOG.info("issue opened [%s] %s: %s", obs.severity, obs.issue_type, obs.summary)
            return IssueChange(
                kind="opened",
                issue_id=issue_id,
                issue_type=obs.issue_type,
                severity=obs.severity,
                entity_type=obs.entity_type,
                entity_id=obs.entity_id,
                entity_name=obs.entity_name,
                summary=obs.summary,
                first_seen=ts,
                last_seen=ts,
                details=obs.details,
            )

        issue_id = int(existing["id"])
        prev_severity = str(existing["severity"])
        escalated = severity_rank(obs.severity) > severity_rank(prev_severity)
        new_max = max_severity(existing["max_severity"], obs.severity)

        self.db.execute(
            "UPDATE issues SET last_seen=?, occurrences=occurrences+1, severity=?, "
            "max_severity=?, summary=?, details=?, trigger_data=? WHERE id=?",
            (ts, obs.severity, new_max, obs.summary, details_json, trigger_json, issue_id),
        )
        self._log_event(
            issue_id,
            ts,
            "escalated" if escalated else "observed",
            obs.severity,
            obs.summary,
            details_json,
            trigger_json,
        )

        if not escalated:
            return None
        LOG.info(
            "issue escalated %s -> %s [%s] %s", prev_severity, obs.severity, obs.issue_type, obs.summary
        )
        return IssueChange(
            kind="escalated",
            issue_id=issue_id,
            issue_type=obs.issue_type,
            severity=obs.severity,
            entity_type=obs.entity_type,
            entity_id=obs.entity_id,
            entity_name=obs.entity_name,
            summary=obs.summary,
            first_seen=int(existing["first_seen"]),
            last_seen=ts,
            details=obs.details,
            previous_severity=prev_severity,
        )

    def _resolve_missing(
        self, seen: set[str], active_types: set[str], ts: int
    ) -> list[IssueChange]:
        changes: list[IssueChange] = []
        for issue in self.open_issues():
            if issue["fingerprint"] in seen or issue["issue_type"] not in active_types:
                continue
            changes.append(self.resolve(int(issue["id"]), ts=ts))
        return changes

    def resolve(self, issue_id: int, *, ts: int | None = None, note: str = "") -> IssueChange:
        ts = ts or now()
        issue = self.get(issue_id)
        if issue is None:
            raise KeyError(f"no such issue: {issue_id}")
        duration = ts - int(issue["first_seen"])
        summary = f"{issue['summary']} — cleared after {_duration(duration)}"
        self.db.execute(
            "UPDATE issues SET status='resolved', resolved_at=?, last_seen=? WHERE id=?",
            (ts, ts, issue_id),
        )
        self._log_event(
            issue_id,
            ts,
            "resolved",
            issue["severity"],
            note or summary,
            dumps({"duration_s": duration}),
            None,
        )
        LOG.info("issue resolved: %s", summary)
        return IssueChange(
            kind="resolved",
            issue_id=issue_id,
            issue_type=str(issue["issue_type"]),
            severity=str(issue["severity"]),
            entity_type=str(issue["entity_type"]),
            entity_id=str(issue["entity_id"]),
            entity_name=issue["entity_name"],
            summary=summary,
            first_seen=int(issue["first_seen"]),
            last_seen=ts,
            details={"duration_s": duration, **(loads(issue["details"]) or {})},
        )

    def mark_notified(self, issue_id: int, severity: str, ts: int | None = None) -> None:
        self.db.execute(
            "UPDATE issues SET notified_at=?, notified_severity=? WHERE id=?",
            (ts or now(), severity, issue_id),
        )

    def record_notification(
        self, issue_id: int | None, channel: str, kind: str, ok: bool, detail: str = ""
    ) -> None:
        self.db.execute(
            "INSERT INTO notifications(issue_id, ts, channel, kind, ok, detail) VALUES(?,?,?,?,?,?)",
            (issue_id, now(), channel, kind, 1 if ok else 0, detail[:1000]),
        )

    def _log_event(
        self,
        issue_id: int,
        ts: int,
        kind: str,
        severity: str | None,
        summary: str | None,
        details_json: str | None,
        trigger_json: str | None,
    ) -> None:
        self.db.execute(
            "INSERT INTO issue_events(issue_id, ts, kind, severity, summary, details, trigger_data) "
            "VALUES(?,?,?,?,?,?,?)",
            (issue_id, ts, kind, severity, summary, details_json, trigger_json),
        )


def _duration(seconds: int) -> str:
    from .util import humanize_duration

    return humanize_duration(seconds)
