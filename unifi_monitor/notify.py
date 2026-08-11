"""Alert delivery for Part 1.

Deliberately dumb: an alert carries the bare fact and nothing else — "AP-3
offline 12 min". No diagnosis, no suggested fix, no LLM in the path. If the
model is down, alerts still go out.

Channels: slack (incoming webhook), webhook (generic JSON POST), email
(SMTP), stdout (for cron mail / testing). Set ``UNIFI_ALERT_CHANNELS`` to a
comma-separated list; failures on one channel never block another.
"""

from __future__ import annotations

import json
import smtplib
import ssl
import urllib.error
import urllib.request
from email.message import EmailMessage
from typing import Any

from .config import AlertConfig, Config
from .issues import IssueChange, severity_rank
from .util import LOG, dumps, humanize_duration, now, redact

SEVERITY_EMOJI = {"info": "ℹ️", "warning": "⚠️", "critical": "🚨"}
SEVERITY_COLOR = {"info": "#5b9bd5", "warning": "#f2b705", "critical": "#d93025"}


class Notifier:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.alerts: AlertConfig = cfg.alerts
        self._secrets = cfg.secrets()

    # ------------------------------------------------------------- filtering

    def should_send(self, change: IssueChange, issue_row: dict[str, Any] | None) -> bool:
        """Rate-limit and severity-filter, before any network call."""
        if not self.alerts.channels:
            return False
        if change.kind == "resolved":
            if not self.alerts.notify_on_resolve:
                return False
            # Only announce the clear if we announced the problem.
            return bool(issue_row and issue_row.get("notified_at"))
        if severity_rank(change.severity) < severity_rank(self.alerts.min_severity):
            return False
        if change.kind == "escalated":
            return True  # escalation always breaks through the cooldown
        if issue_row and issue_row.get("notified_at"):
            age = now() - int(issue_row["notified_at"])
            if age < self.alerts.resend_cooldown_s:
                return False
        return True

    # -------------------------------------------------------------- dispatch

    def send(self, change: IssueChange) -> list[tuple[str, bool, str]]:
        """Deliver to every configured channel. Returns (channel, ok, detail)."""
        results: list[tuple[str, bool, str]] = []
        text = format_alert(change)
        for channel in self.alerts.channels:
            try:
                detail = self._send_one(channel, change, text)
                results.append((channel, True, detail))
            except Exception as exc:  # noqa: BLE001 - a broken alert channel
                # must never take down the poller.
                message = redact(f"{type(exc).__name__}: {exc}", self._secrets)
                LOG.error("alert via %s failed: %s", channel, message)
                results.append((channel, False, message))
        return results

    def _send_one(self, channel: str, change: IssueChange, text: str) -> str:
        if channel == "stdout":
            print(text, flush=True)
            return "printed"
        if channel == "slack":
            return self._slack(change, text)
        if channel == "webhook":
            return self._webhook(change, text)
        if channel == "email":
            return self._email(change, text)
        raise ValueError(f"unknown alert channel {channel!r}")

    # -------------------------------------------------------------- channels

    def _slack(self, change: IssueChange, text: str) -> str:
        payload = {
            "text": text,
            "attachments": [
                {
                    "color": SEVERITY_COLOR.get(change.severity, "#888888"),
                    "fields": [
                        {"title": "Issue", "value": change.issue_type, "short": True},
                        {"title": "Severity", "value": change.severity, "short": True},
                        {
                            "title": "Device",
                            "value": change.entity_name or change.entity_id,
                            "short": True,
                        },
                        {
                            "title": "Ongoing",
                            "value": humanize_duration(change.last_seen - change.first_seen),
                            "short": True,
                        },
                    ],
                    "footer": f"unifi-monitor · issue #{change.issue_id}",
                    "ts": change.last_seen,
                }
            ],
        }
        return _post_json(self.alerts.slack_webhook_url, payload)

    def _webhook(self, change: IssueChange, text: str) -> str:
        headers = {}
        if self.alerts.webhook_auth_header:
            name, _, value = self.alerts.webhook_auth_header.partition(":")
            headers[name.strip() or "Authorization"] = value.strip()
        payload = {
            "source": "unifi-monitor",
            "kind": change.kind,
            "issue_id": change.issue_id,
            "issue_type": change.issue_type,
            "severity": change.severity,
            "entity_type": change.entity_type,
            "entity_id": change.entity_id,
            "entity_name": change.entity_name,
            "summary": change.summary,
            "text": text,
            "first_seen": change.first_seen,
            "last_seen": change.last_seen,
            "details": change.details,
        }
        return _post_json(self.alerts.webhook_url, payload, headers=headers)

    def _email(self, change: IssueChange, text: str) -> str:
        message = EmailMessage()
        prefix = "RESOLVED" if change.kind == "resolved" else change.severity.upper()
        message["Subject"] = f"[UniFi {prefix}] {change.summary}"[:200]
        message["From"] = self.alerts.email_from
        message["To"] = ", ".join(self.alerts.email_to)
        message.set_content(
            f"{text}\n\n"
            f"issue id : {change.issue_id}\n"
            f"type     : {change.issue_type}\n"
            f"entity   : {change.entity_name or change.entity_id} ({change.entity_id})\n"
            f"first seen: {change.first_seen}\n"
            f"last seen : {change.last_seen}\n\n"
            f"details:\n{json.dumps(change.details, indent=2, default=str)}\n"
        )

        if self.alerts.smtp_port == 465:
            server: smtplib.SMTP = smtplib.SMTP_SSL(
                self.alerts.smtp_host, self.alerts.smtp_port, timeout=20,
                context=ssl.create_default_context(),
            )
        else:
            server = smtplib.SMTP(self.alerts.smtp_host, self.alerts.smtp_port, timeout=20)
        try:
            if self.alerts.smtp_port != 465 and self.alerts.smtp_starttls:
                server.starttls(context=ssl.create_default_context())
            if self.alerts.smtp_user:
                server.login(self.alerts.smtp_user, self.alerts.smtp_password)
            server.send_message(message)
        finally:
            try:
                server.quit()
            except smtplib.SMTPException:
                pass
        return f"emailed {len(self.alerts.email_to)} recipient(s)"

    # ------------------------------------------------------- self-monitoring

    def send_raw(self, subject: str, body: str, severity: str = "critical") -> None:
        """Escape hatch for poller-level failures that have no issue row."""
        change = IssueChange(
            kind="opened",
            issue_id=0,
            issue_type="monitor_self",
            severity=severity,
            entity_type="site",
            entity_id="unifi-monitor",
            entity_name="unifi-monitor",
            summary=subject,
            first_seen=now(),
            last_seen=now(),
            details={"body": body},
        )
        for channel, ok, detail in self.send(change):
            if not ok:
                LOG.error("self-alert via %s failed: %s", channel, detail)


def format_alert(change: IssueChange) -> str:
    """The whole message. One line of fact, plus how long it has been true."""
    if change.kind == "resolved":
        return f"✅ RESOLVED: {change.summary}"
    emoji = SEVERITY_EMOJI.get(change.severity, "")
    prefix = "ESCALATED" if change.kind == "escalated" else change.severity.upper()
    line = f"{emoji} {prefix}: {change.summary}".strip()
    if change.kind == "escalated" and change.previous_severity:
        line += f" (was {change.previous_severity})"
    return line


def _post_json(url: str, payload: dict[str, Any], headers: dict[str, str] | None = None) -> str:
    if not url:
        raise ValueError("no URL configured for this channel")
    data = dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "User-Agent": "unifi-monitor/1.0",
            **(headers or {}),
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as resp:
            return f"HTTP {resp.status}"
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:200]
        raise RuntimeError(f"HTTP {exc.code}: {body}") from exc
