"""Small shared helpers. Stdlib only."""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
from typing import Any

LOG = logging.getLogger("unifi_monitor")


def now() -> int:
    """Current UTC epoch seconds. Every timestamp in this project is this."""
    return int(time.time())


def to_epoch(value: Any) -> int | None:
    """Coerce a UniFi timestamp to epoch seconds.

    The controller mixes seconds (``time``, ``last_seen``) and milliseconds
    (``datetime``-adjacent fields, ``*_ts`` on some endpoints) in the same
    payload, so normalise by magnitude.
    """
    if value is None:
        return None
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    if n <= 0:
        return None
    if n > 1e11:  # milliseconds
        n /= 1000.0
    return int(n)


def humanize_duration(seconds: float | None) -> str:
    if seconds is None:
        return "unknown"
    seconds = int(max(0, seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m" if sec == 0 else f"{minutes}m{sec}s"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h" if minutes == 0 else f"{hours}h{minutes}m"
    days, hours = divmod(hours, 24)
    return f"{days}d" if hours == 0 else f"{days}d{hours}h"


def humanize_bps(bits_per_second: float | None) -> str:
    if bits_per_second is None:
        return "unknown"
    v = float(bits_per_second)
    for unit in ("bps", "Kbps", "Mbps", "Gbps"):
        if v < 1000 or unit == "Gbps":
            return f"{v:.1f} {unit}"
        v /= 1000.0
    return f"{v:.1f} Gbps"


def dumps(obj: Any) -> str:
    """JSON dump that never explodes on odd controller payloads."""
    return json.dumps(obj, default=str, separators=(",", ":"), sort_keys=True)


def loads(text: str | None) -> Any:
    if not text:
        return None
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return None


def normalize_mac(mac: str | None) -> str | None:
    if not mac:
        return None
    cleaned = re.sub(r"[^0-9a-fA-F]", "", mac)
    if len(cleaned) != 12:
        return mac.strip().lower()
    return ":".join(cleaned[i : i + 2] for i in range(0, 12, 2)).lower()


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")


def env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(float(raw.strip()))
    except ValueError:
        LOG.warning("env %s=%r is not a number, using %s", name, raw, default)
        return default


def env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw.strip())
    except ValueError:
        LOG.warning("env %s=%r is not a number, using %s", name, raw, default)
        return default


def env_str(name: str, default: str = "") -> str:
    raw = os.environ.get(name)
    return default if raw is None or raw.strip() == "" else raw.strip()


def env_list(name: str, default: list[str] | None = None) -> list[str]:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return list(default or [])
    return [part.strip() for part in raw.split(",") if part.strip()]


def load_env_file(path: str | None = None) -> str | None:
    """Load ``KEY=VALUE`` lines into the environment.

    cron and Task Scheduler start with almost no environment, so the practical
    way to hand a scheduled poll its credentials is a file with tight
    permissions. Existing variables always win, so a shell export or a service
    unit can still override the file.
    """
    candidates = [path] if path else [os.environ.get("UNIFI_MONITOR_ENV"), ".env"]
    for candidate in candidates:
        if not candidate or not os.path.isfile(candidate):
            continue
        with open(candidate, encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip("'\"")
                if key and key not in os.environ:
                    os.environ[key] = value
        return candidate
    return None


def setup_logging(level: str = "INFO", logfile: str = "") -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    if logfile:
        os.makedirs(os.path.dirname(os.path.abspath(logfile)) or ".", exist_ok=True)
        handlers.append(logging.FileHandler(logfile, encoding="utf-8"))
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        handlers=handlers,
        force=True,
    )


def redact(text: str, secrets: list[str]) -> str:
    """Strip credentials out of anything that may reach a log or an alert."""
    out = text
    for secret in secrets:
        if secret and len(secret) >= 4:
            out = out.replace(secret, "***")
    return out
