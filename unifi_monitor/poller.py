"""The polling service — Part 1.

Runs one cycle (``poll_once``) or forever (``run_forever``). It knows about
the controller, SQLite and alert channels. It knows nothing about LLMs,
gateways or MCP, and it never imports from Part 2.
"""

from __future__ import annotations

import signal
import time
from typing import Any

from .config import Config
from .db import Database
from .detectors import Analyzer
from .issues import IssueChange, IssueStore, Observation
from .notify import Notifier
from .unifi_client import UniFiAuthError, UniFiClient, UniFiError, UniFiUnavailable
from .util import LOG, humanize_duration, now, redact

PRUNE_INTERVAL_S = 6 * 3600
UNREACHABLE_FINGERPRINT = "controller_unreachable:site:controller"


class Poller:
    def __init__(self, cfg: Config, db: Database | None = None, client: UniFiClient | None = None):
        self.cfg = cfg
        self.db = db or Database(cfg.db_path)
        self.client = client or UniFiClient(cfg.controller)
        self.analyzer = Analyzer(self.db, cfg)
        self.issues = IssueStore(self.db)
        self.notifier = Notifier(cfg)
        self._stop = False

    # ------------------------------------------------------------ one cycle

    def poll_once(self) -> dict[str, Any]:
        """Poll, analyze, persist, alert. Never raises for controller trouble —
        that is itself recorded as an issue."""
        ts = now()
        run_id = self.db.start_run(ts)
        summary: dict[str, Any] = {"run_id": run_id, "ts": ts, "ok": False}

        try:
            snapshot = self.client.snapshot()
        except UniFiError as exc:
            message = redact(str(exc), self.cfg.secrets())
            LOG.error("poll failed: %s", message)
            self.db.finish_run(run_id, ok=False, error=message)
            changes = self._flag_unreachable(exc, message, ts)
            self._dispatch(changes)
            summary.update({"error": message, "changes": len(changes)})
            return summary

        result = self.analyzer.analyze(snapshot, ts=ts)
        observations = list(result.observations)

        # Controller is answering again: clear any unreachable issue, and let
        # the normal resolution path handle everything else.
        active_types = set(result.active_types) | {
            "controller_unreachable",
            "controller_auth_failed",
            "controller_error",
        }

        changes = self.issues.sync(observations, active_types=active_types, ts=ts)
        self._dispatch(changes)

        self.db.finish_run(
            run_id,
            ok=True,
            device_count=result.device_count,
            client_count=result.client_count,
            issue_count=len(observations),
        )
        self._maybe_prune()

        LOG.info(
            "poll ok: %d devices, %d clients, %d flagged, %d change(s)%s",
            result.device_count,
            result.client_count,
            len(observations),
            len(changes),
            f" | notes: {'; '.join(result.notes)}" if result.notes else "",
        )
        summary.update(
            {
                "ok": True,
                "devices": result.device_count,
                "clients": result.client_count,
                "observations": len(observations),
                "changes": [c.kind for c in changes],
                "notes": result.notes,
            }
        )
        return summary

    # ------------------------------------------------------------------ loop

    def run_forever(self) -> None:
        """Fixed-interval loop with drift correction.

        The service supervisor (systemd/Task Scheduler) restarts us if the
        process dies; cron-mode (``poll_once``) is the alternative for hosts
        without one.
        """
        self._install_signal_handlers()
        interval = max(30, self.cfg.poll_interval_s)
        LOG.info(
            "starting poll loop: %s every %s",
            self.cfg.controller.host,
            humanize_duration(interval),
        )
        while not self._stop:
            started = time.monotonic()
            try:
                self.poll_once()
            except Exception as exc:  # noqa: BLE001 - the loop outlives any bug
                LOG.exception("unhandled error during poll: %s", exc)
            elapsed = time.monotonic() - started
            sleep_for = max(1.0, interval - elapsed)
            deadline = time.monotonic() + sleep_for
            while not self._stop and time.monotonic() < deadline:
                time.sleep(min(1.0, deadline - time.monotonic()))
        LOG.info("poll loop stopped")

    def stop(self, *_args: object) -> None:
        self._stop = True

    def _install_signal_handlers(self) -> None:
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, self.stop)
            except (ValueError, OSError, AttributeError):
                pass  # not the main thread, or Windows without SIGTERM

    def close(self) -> None:
        self.client.logout()
        self.db.close()

    # -------------------------------------------------------------- internals

    def _flag_unreachable(self, exc: UniFiError, message: str, ts: int) -> list[IssueChange]:
        """A controller we cannot reach is an issue in its own right — and the
        one failure mode that would otherwise leave the db silent."""
        if isinstance(exc, UniFiAuthError):
            issue_type, severity, what = (
                "controller_auth_failed",
                "critical",
                "authentication to controller failed",
            )
        elif isinstance(exc, UniFiUnavailable):
            issue_type, severity, what = (
                "controller_unreachable",
                "critical",
                f"controller {self.cfg.controller.host} unreachable",
            )
        else:
            issue_type, severity, what = ("controller_error", "warning", "controller API error")

        observation = Observation(
            issue_type=issue_type,
            severity=severity,
            entity_type="site",
            entity_id="controller",
            entity_name=self.cfg.controller.host,
            summary=f"{what}: {message[:160]}",
            details={
                "host": self.cfg.controller.host,
                "site": self.cfg.controller.site,
                "error": message,
                "consecutive_failures": self._consecutive_failures(),
            },
            trigger_data={"error": message},
        )
        # No active_types: a failed poll must never resolve device issues just
        # because this cycle produced no observations for them.
        return self.issues.sync([observation], active_types=None, ts=ts)

    def _consecutive_failures(self) -> int:
        rows = self.db.query("SELECT ok FROM poll_runs ORDER BY id DESC LIMIT 20")
        count = 0
        for row in rows:
            if row["ok"]:
                break
            count += 1
        return count

    def _dispatch(self, changes: list[IssueChange]) -> None:
        sent = 0
        for change in changes:
            if sent >= self.cfg.alerts.max_alerts_per_run:
                LOG.warning(
                    "alert cap (%d) reached; %d change(s) recorded but not sent",
                    self.cfg.alerts.max_alerts_per_run,
                    len(changes) - sent,
                )
                break
            issue_row = self.issues.get(change.issue_id) if change.issue_id else None
            if not self.notifier.should_send(change, issue_row):
                continue
            for channel, ok, detail in self.notifier.send(change):
                self.issues.record_notification(
                    change.issue_id or None,
                    channel,
                    "resolve" if change.kind == "resolved" else "alert",
                    ok,
                    detail,
                )
            if change.kind != "resolved" and change.issue_id:
                self.issues.mark_notified(change.issue_id, change.severity)
            sent += 1

    def _maybe_prune(self) -> None:
        row = self.db.query_one("SELECT value FROM schema_meta WHERE key='last_prune'")
        last = int(row["value"]) if row and str(row["value"]).isdigit() else 0
        if now() - last < PRUNE_INTERVAL_S:
            return
        self.db.prune(
            metrics_days=self.cfg.retain_metrics_days,
            transitions_days=self.cfg.retain_transitions_days,
            events_days=self.cfg.retain_events_days,
            issues_days=self.cfg.retain_issues_days,
        )
        self.db.execute(
            "INSERT INTO schema_meta(key, value) VALUES('last_prune', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(now()),),
        )
