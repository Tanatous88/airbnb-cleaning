"""Configuration for the poller.

Everything is driven by environment variables, with an optional JSON file
(``UNIFI_MONITOR_CONFIG``) overlaid on top for the fiddly bits — per-device
threshold overrides and the watchlist — that are painful to express in env.

Precedence: JSON file > environment > built-in default.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields, is_dataclass
from typing import Any

from .util import (
    LOG,
    env_bool,
    env_float,
    env_int,
    env_list,
    env_str,
    load_env_file,
    loads,
    normalize_mac,
)

DEFAULT_DB_PATH = os.path.join(
    os.environ.get("UNIFI_MONITOR_HOME") or os.path.join(os.path.expanduser("~"), ".unifi_monitor"),
    "unifi_monitor.db",
)


@dataclass
class ControllerConfig:
    host: str = "192.168.1.1"
    port: int = 443
    site: str = "default"
    username: str = ""
    password: str = ""
    api_key: str = ""
    # "proxy" = UniFi OS console (UDM/UDR/UNVR/Cloud Key gen2+): the Network
    # app lives behind /proxy/network. "direct" = standalone controller.
    controller_type: str = "proxy"
    verify_ssl: bool = False
    timeout: float = 20.0
    max_retries: int = 2

    @property
    def base_url(self) -> str:
        scheme = "https" if self.port != 80 else "http"
        return f"{scheme}://{self.host}:{self.port}"

    def secrets(self) -> list[str]:
        return [s for s in (self.password, self.api_key) if s]


@dataclass
class Thresholds:
    """Everything a detector compares against. All times in seconds."""

    # Device / client reachability
    device_offline_warning_s: int = 300  # 5 min offline -> warning
    device_offline_critical_s: int = 900  # 15 min offline -> critical
    client_offline_warning_s: int = 600
    client_offline_critical_s: int = 1800
    # A client is "gone" rather than merely idle only after this long. Guards
    # against phones that doze off Wi-Fi.
    client_missing_grace_s: int = 120

    # Flapping: N transitions to offline inside a rolling window
    flap_window_s: int = 3600
    flap_warning_count: int = 3
    flap_critical_count: int = 6

    # WAN
    wan_latency_warning_ms: float = 120.0
    wan_latency_critical_ms: float = 400.0
    wan_loss_warning_pct: float = 2.0
    wan_loss_critical_pct: float = 10.0

    # Switch ports
    port_error_rate_warning: float = 10.0  # errors per minute
    port_error_rate_critical: float = 100.0
    port_drop_rate_warning: float = 500.0  # dropped frames per minute

    # Device load
    cpu_warning_pct: float = 85.0
    cpu_critical_pct: float = 95.0
    mem_warning_pct: float = 90.0
    load_sustained_polls: int = 3  # must hold this many consecutive polls

    # Wireless quality
    satisfaction_warning: float = 60.0  # UniFi 0-100 "experience" score
    signal_warning_dbm: float = -75.0
    tx_retry_warning_pct: float = 25.0

    # Bandwidth (bits/sec, computed from byte-counter deltas between polls)
    bandwidth_warning_bps: float = 0.0  # 0 disables
    bandwidth_critical_bps: float = 0.0

    # Controller alarms/events considered noteworthy
    alarm_min_severity: str = "warning"


@dataclass
class AlertConfig:
    # Any of: slack, webhook, email, stdout
    channels: list[str] = field(default_factory=lambda: ["stdout"])
    min_severity: str = "warning"
    # Don't re-alert on an issue that is already open and unchanged.
    resend_cooldown_s: int = 3600
    # Tell people when it clears, too.
    notify_on_resolve: bool = True
    max_alerts_per_run: int = 12

    slack_webhook_url: str = ""
    webhook_url: str = ""
    webhook_auth_header: str = ""

    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_starttls: bool = True
    email_from: str = ""
    email_to: list[str] = field(default_factory=list)

    def secrets(self) -> list[str]:
        return [
            s
            for s in (
                self.slack_webhook_url,
                self.webhook_url,
                self.webhook_auth_header,
                self.smtp_password,
            )
            if s
        ]


@dataclass
class Config:
    controller: ControllerConfig = field(default_factory=ControllerConfig)
    thresholds: Thresholds = field(default_factory=Thresholds)
    alerts: AlertConfig = field(default_factory=AlertConfig)

    db_path: str = DEFAULT_DB_PATH
    poll_interval_s: int = 300
    log_level: str = "INFO"
    log_file: str = ""

    # Clients are noisy. Only these get offline/flap issues raised; everything
    # else is still recorded, just not alerted on. Accepts MACs, hostnames or
    # names (case-insensitive substring match on name/hostname).
    client_watchlist: list[str] = field(default_factory=list)
    # Raise offline issues for *every* client that has been seen before.
    watch_all_clients: bool = False
    # Never raise issues for these (MAC or name substring).
    ignore_list: list[str] = field(default_factory=list)

    # Retention (days). Issues are kept far longer than raw samples because
    # Part 2 reasons over issue history.
    retain_metrics_days: int = 14
    retain_transitions_days: int = 90
    retain_events_days: int = 30
    retain_issues_days: int = 365

    # Per-entity threshold overrides: {"AP-3": {"device_offline_critical_s": 300}}
    overrides: dict[str, dict[str, Any]] = field(default_factory=dict)

    def secrets(self) -> list[str]:
        return self.controller.secrets() + self.alerts.secrets()

    def thresholds_for(self, *names: str | None) -> Thresholds:
        """Thresholds with per-entity overrides applied.

        ``names`` are the identities an entity answers to (mac, name); the
        first one with an override block wins.
        """
        if not self.overrides:
            return self.thresholds
        lowered = {str(n).lower(): n for n in names if n}
        for key, patch in self.overrides.items():
            if key.lower() in lowered:
                merged = {f.name: getattr(self.thresholds, f.name) for f in fields(Thresholds)}
                merged.update({k: v for k, v in patch.items() if k in merged})
                return Thresholds(**merged)
        return self.thresholds

    def is_ignored(self, *names: str | None) -> bool:
        if not self.ignore_list:
            return False
        haystack = " ".join(str(n).lower() for n in names if n)
        return any(pat.lower() in haystack for pat in self.ignore_list)

    def is_watched_client(self, mac: str | None, *names: str | None) -> bool:
        if self.is_ignored(mac, *names):
            return False
        if self.watch_all_clients:
            return True
        if not self.client_watchlist:
            return False
        mac_norm = normalize_mac(mac)
        haystack = " ".join(str(n).lower() for n in names if n)
        for pattern in self.client_watchlist:
            pat = pattern.strip().lower()
            if not pat:
                continue
            if normalize_mac(pat) == mac_norm and mac_norm:
                return True
            if pat in haystack:
                return True
        return False


def _apply_overlay(obj: Any, patch: dict[str, Any]) -> None:
    """Recursively apply a JSON overlay onto a dataclass instance."""
    for key, value in patch.items():
        if not hasattr(obj, key):
            LOG.warning("config: ignoring unknown key %r", key)
            continue
        current = getattr(obj, key)
        if is_dataclass(current) and isinstance(value, dict):
            _apply_overlay(current, value)
        else:
            setattr(obj, key, value)


def load_config(config_path: str | None = None) -> Config:
    """Build config from env, then overlay the optional JSON file."""
    loaded = load_env_file()
    if loaded:
        LOG.debug("config: loaded env file %s", loaded)
    cfg = Config()

    c = cfg.controller
    c.host = env_str("UNIFI_HOST", c.host)
    c.port = env_int("UNIFI_PORT", c.port)
    c.site = env_str("UNIFI_SITE", c.site)
    c.username = env_str("UNIFI_USERNAME", c.username)
    c.password = env_str("UNIFI_PASSWORD", c.password)
    c.api_key = env_str("UNIFI_API_KEY", c.api_key)
    c.controller_type = env_str("UNIFI_CONTROLLER_TYPE", c.controller_type).lower()
    c.verify_ssl = env_bool("UNIFI_VERIFY_SSL", c.verify_ssl)
    c.timeout = env_float("UNIFI_TIMEOUT", c.timeout)
    c.max_retries = env_int("UNIFI_MAX_RETRIES", c.max_retries)

    cfg.db_path = env_str("UNIFI_DB_PATH", cfg.db_path)
    cfg.poll_interval_s = env_int("UNIFI_POLL_INTERVAL", cfg.poll_interval_s)
    cfg.log_level = env_str("UNIFI_LOG_LEVEL", cfg.log_level)
    cfg.log_file = env_str("UNIFI_LOG_FILE", cfg.log_file)

    cfg.client_watchlist = env_list("UNIFI_CLIENT_WATCHLIST", cfg.client_watchlist)
    cfg.watch_all_clients = env_bool("UNIFI_WATCH_ALL_CLIENTS", cfg.watch_all_clients)
    cfg.ignore_list = env_list("UNIFI_IGNORE_LIST", cfg.ignore_list)

    cfg.retain_metrics_days = env_int("UNIFI_RETAIN_METRICS_DAYS", cfg.retain_metrics_days)
    cfg.retain_transitions_days = env_int(
        "UNIFI_RETAIN_TRANSITIONS_DAYS", cfg.retain_transitions_days
    )
    cfg.retain_events_days = env_int("UNIFI_RETAIN_EVENTS_DAYS", cfg.retain_events_days)
    cfg.retain_issues_days = env_int("UNIFI_RETAIN_ISSUES_DAYS", cfg.retain_issues_days)

    t = cfg.thresholds
    for f in fields(Thresholds):
        env_name = "UNIFI_" + f.name.upper()
        if env_name not in os.environ:
            continue
        if f.type == "int":
            setattr(t, f.name, env_int(env_name, getattr(t, f.name)))
        elif f.type == "float":
            setattr(t, f.name, env_float(env_name, getattr(t, f.name)))
        else:
            setattr(t, f.name, env_str(env_name, getattr(t, f.name)))

    a = cfg.alerts
    a.channels = [ch.lower() for ch in env_list("UNIFI_ALERT_CHANNELS", a.channels)]
    a.min_severity = env_str("UNIFI_ALERT_MIN_SEVERITY", a.min_severity).lower()
    a.resend_cooldown_s = env_int("UNIFI_ALERT_COOLDOWN", a.resend_cooldown_s)
    a.notify_on_resolve = env_bool("UNIFI_ALERT_ON_RESOLVE", a.notify_on_resolve)
    a.max_alerts_per_run = env_int("UNIFI_ALERT_MAX_PER_RUN", a.max_alerts_per_run)
    a.slack_webhook_url = env_str("SLACK_WEBHOOK_URL", a.slack_webhook_url)
    a.webhook_url = env_str("ALERT_WEBHOOK_URL", a.webhook_url)
    a.webhook_auth_header = env_str("ALERT_WEBHOOK_AUTH", a.webhook_auth_header)
    a.smtp_host = env_str("SMTP_HOST", a.smtp_host)
    a.smtp_port = env_int("SMTP_PORT", a.smtp_port)
    a.smtp_user = env_str("SMTP_USER", a.smtp_user)
    a.smtp_password = env_str("SMTP_PASSWORD", a.smtp_password)
    a.smtp_starttls = env_bool("SMTP_STARTTLS", a.smtp_starttls)
    a.email_from = env_str("ALERT_EMAIL_FROM", a.email_from)
    a.email_to = env_list("ALERT_EMAIL_TO", a.email_to)

    path = config_path or env_str("UNIFI_MONITOR_CONFIG")
    if path:
        if not os.path.exists(path):
            LOG.warning("config file %s not found, using env only", path)
        else:
            with open(path, encoding="utf-8") as fh:
                patch = loads(fh.read())
            if isinstance(patch, dict):
                _apply_overlay(cfg, patch)
                LOG.info("config: overlaid %s", path)
            else:
                LOG.warning("config file %s is not a JSON object, ignoring", path)

    return cfg


def validate(cfg: Config) -> list[str]:
    """Return a list of fatal problems; empty means good to run."""
    problems: list[str] = []
    c = cfg.controller
    if not c.host:
        problems.append("UNIFI_HOST is empty")
    if not c.api_key and not (c.username and c.password):
        problems.append(
            "no credentials: set UNIFI_USERNAME + UNIFI_PASSWORD (local console "
            "account) or UNIFI_API_KEY"
        )
    if c.controller_type not in {"proxy", "direct"}:
        problems.append(f"UNIFI_CONTROLLER_TYPE must be proxy or direct, got {c.controller_type!r}")
    if cfg.poll_interval_s < 30:
        problems.append("UNIFI_POLL_INTERVAL below 30s will hammer the controller")

    a = cfg.alerts
    for channel in a.channels:
        if channel == "slack" and not a.slack_webhook_url:
            problems.append("alert channel 'slack' selected but SLACK_WEBHOOK_URL is unset")
        elif channel == "webhook" and not a.webhook_url:
            problems.append("alert channel 'webhook' selected but ALERT_WEBHOOK_URL is unset")
        elif channel == "email" and not (a.smtp_host and a.email_from and a.email_to):
            problems.append(
                "alert channel 'email' selected but SMTP_HOST / ALERT_EMAIL_FROM / "
                "ALERT_EMAIL_TO are incomplete"
            )
        elif channel not in {"slack", "webhook", "email", "stdout"}:
            problems.append(f"unknown alert channel {channel!r}")
    return problems
