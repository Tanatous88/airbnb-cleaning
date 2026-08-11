"""Turn a controller snapshot into state, metrics and flagged observations.

Every detector answers one question and emits bare facts only. It never
speculates about causes — Part 2 does that, from the history this module
writes down.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .config import Config, Thresholds
from .db import Database
from .issues import Observation
from .util import LOG, humanize_bps, humanize_duration, normalize_mac, now, to_epoch

# UniFi device state codes (Network application).
DEVICE_STATES = {
    0: "offline",
    1: "online",
    2: "pending_adoption",
    3: "firmware_mismatch",
    4: "upgrading",
    5: "provisioning",
    6: "heartbeat_missed",
    7: "adopting",
    8: "deleting",
    9: "inform_error",
    10: "adoption_failed",
    11: "isolated",
}
OFFLINE_STATES = {"offline", "heartbeat_missed", "isolated"}
TRANSITIONAL_STATES = {"upgrading", "provisioning", "adopting", "deleting"}

DEVICE_KIND = {
    "uap": "access point",
    "usw": "switch",
    "ugw": "gateway",
    "udm": "gateway",
    "uxg": "gateway",
    "ubb": "bridge",
    "ulte": "lte backup",
    "upoe": "switch",
}


@dataclass
class AnalysisResult:
    observations: list[Observation] = field(default_factory=list)
    active_types: set[str] = field(default_factory=set)
    device_count: int = 0
    client_count: int = 0
    notes: list[str] = field(default_factory=list)


class Analyzer:
    """Stateless per-call; all memory lives in the database."""

    def __init__(self, db: Database, cfg: Config):
        self.db = db
        self.cfg = cfg

    # ------------------------------------------------------------------ entry

    def analyze(self, snapshot: dict[str, Any], ts: int | None = None) -> AnalysisResult:
        ts = ts or now()
        result = AnalysisResult()
        errors = snapshot.get("errors") or {}

        devices = snapshot.get("devices") or []
        result.device_count = len(devices)
        result.active_types.update(
            {
                "device_offline",
                "device_flapping",
                "device_high_cpu",
                "device_high_memory",
                "device_not_adopted",
                "port_errors",
                "poe_port_down",
            }
        )
        if self.cfg.thresholds.bandwidth_warning_bps > 0:
            result.active_types.add("high_bandwidth")
        for device in devices:
            self._device(device, ts, result)

        if "clients" not in errors:
            clients = snapshot.get("clients") or []
            result.client_count = len(clients)
            result.active_types.update(
                {"client_offline", "client_flapping", "client_weak_signal"}
            )
            self._clients(clients, snapshot.get("known_clients") or [], ts, result)
        else:
            result.notes.append(f"clients unavailable: {errors['clients']}")

        health = snapshot.get("health") or []
        if health or devices:
            result.active_types.update(
                {"wan_down", "wan_failover", "wan_high_latency", "wan_packet_loss"}
            )
            self._wan(health, devices, ts, result)

        if "alarms" not in errors:
            result.active_types.add("controller_alarm")
            self._alarms(snapshot.get("alarms") or [], ts, result)
        else:
            result.notes.append(f"alarms unavailable: {errors['alarms']}")

        if "events" not in errors:
            self._events(snapshot.get("events") or [], ts)

        return result

    # ---------------------------------------------------------------- devices

    def _device(self, device: dict[str, Any], ts: int, result: AnalysisResult) -> None:
        mac = normalize_mac(device.get("mac"))
        if not mac:
            return
        name = (
            device.get("name")
            or device.get("_name")
            or device.get("hostname")
            or device.get("model")
            or mac
        )
        dev_type = str(device.get("type") or "").lower()
        kind = DEVICE_KIND.get(dev_type, dev_type or "device")
        state_code = device.get("state")
        try:
            state = DEVICE_STATES.get(int(state_code), "unknown")
        except (TypeError, ValueError):
            state = "unknown"

        self.db.upsert_entity(
            "device",
            mac,
            name=str(name),
            kind=kind,
            model=device.get("model"),
            meta={
                "type": dev_type,
                "version": device.get("version"),
                "ip": device.get("ip"),
                "site_id": device.get("site_id"),
            },
            ts=ts,
        )

        simple_state = (
            "online"
            if state == "online"
            else "offline"
            if state in OFFLINE_STATES
            else "transitional"
            if state in TRANSITIONAL_STATES
            else state
        )
        transition = self.db.set_state(
            "device",
            mac,
            simple_state,
            ts=ts,
            entity_name=str(name),
            raw_state=state,
            details={"uptime": device.get("uptime"), "ip": device.get("ip")},
        )

        thresholds = self.cfg.thresholds_for(mac, name)
        ignored = self.cfg.is_ignored(mac, name)

        self._device_metrics(device, mac, ts)

        if not ignored:
            self._device_offline(device, mac, str(name), kind, simple_state, transition, thresholds, ts, result)
            self._device_flapping("device", mac, str(name), kind, thresholds, ts, result)
            self._device_load(device, mac, str(name), thresholds, ts, result)
            self._device_adoption(device, mac, str(name), kind, state, ts, result)
            self._bandwidth("device", mac, str(name), thresholds, result)
            self._ports(device, mac, str(name), thresholds, ts, result)

    def _bandwidth(
        self,
        entity_type: str,
        entity_id: str,
        name: str,
        thresholds: Thresholds,
        result: AnalysisResult,
    ) -> None:
        """Throughput flagging, off by default (threshold 0 disables it).

        Bandwidth is always *recorded*; it is only *flagged* when someone has
        said what "too much" means for this network.
        """
        if thresholds.bandwidth_warning_bps <= 0:
            return
        rx = self.db.recent_metric_values(entity_type, entity_id, "rx_bps", limit=1)
        tx = self.db.recent_metric_values(entity_type, entity_id, "tx_bps", limit=1)
        total = (rx[0] if rx else 0.0) + (tx[0] if tx else 0.0)
        if total < thresholds.bandwidth_warning_bps:
            return
        critical = (
            thresholds.bandwidth_critical_bps > 0 and total >= thresholds.bandwidth_critical_bps
        )
        result.observations.append(
            Observation(
                issue_type="high_bandwidth",
                severity="critical" if critical else "warning",
                entity_type=entity_type,
                entity_id=entity_id,
                entity_name=name,
                summary=f"{name} using {humanize_bps(total)}",
                details={
                    "rx_bps": rx[0] if rx else None,
                    "tx_bps": tx[0] if tx else None,
                    "total_bps": total,
                    "warning_threshold_bps": thresholds.bandwidth_warning_bps,
                    "critical_threshold_bps": thresholds.bandwidth_critical_bps,
                },
                trigger_data={"rx_bps": rx[:1], "tx_bps": tx[:1]},
            )
        )

    def _device_metrics(self, device: dict[str, Any], mac: str, ts: int) -> None:
        rows: list[tuple] = []
        sys_stats = device.get("system-stats") or device.get("sys_stats") or {}
        for key, metric in (("cpu", "cpu_pct"), ("mem", "mem_pct")):
            value = _to_float(sys_stats.get(key))
            if value is not None:
                rows.append((ts, "device", mac, metric, value, None))

        for field_name, metric in (("rx_bytes", "rx_bps"), ("tx_bytes", "tx_bps")):
            total = _to_float(device.get(field_name))
            if total is None:
                continue
            delta, elapsed = self.db.counter_delta("device", mac, field_name, total, ts)
            if delta is not None and elapsed:
                rows.append((ts, "device", mac, metric, delta * 8.0 / elapsed, None))

        for field_name, metric in (
            ("num_sta", "clients"),
            ("satisfaction", "satisfaction"),
            ("uptime", "uptime"),
        ):
            value = _to_float(device.get(field_name))
            if value is not None:
                rows.append((ts, "device", mac, metric, value, None))
        self.db.add_metrics(rows)

    def _device_offline(
        self,
        device: dict[str, Any],
        mac: str,
        name: str,
        kind: str,
        state: str,
        transition: dict[str, Any],
        thresholds: Thresholds,
        ts: int,
        result: AnalysisResult,
    ) -> None:
        if state != "offline":
            return
        # Our own "since" only knows about polls we've run. The controller's
        # last_seen usually reaches further back — take the earlier of the two
        # so a restart of this poller doesn't reset a device's downtime.
        since = int(transition["since"])
        last_seen = to_epoch(device.get("last_seen"))
        if last_seen and last_seen < since and last_seen <= ts:
            since = last_seen
        down_for = max(0, ts - since)

        if down_for < thresholds.device_offline_warning_s:
            return
        severity = (
            "critical" if down_for >= thresholds.device_offline_critical_s else "warning"
        )
        result.observations.append(
            Observation(
                issue_type="device_offline",
                severity=severity,
                entity_type="device",
                entity_id=mac,
                entity_name=name,
                summary=f"{name} ({kind}) offline {humanize_duration(down_for)}",
                details={
                    "down_since": since,
                    "down_for_s": down_for,
                    "warning_threshold_s": thresholds.device_offline_warning_s,
                    "critical_threshold_s": thresholds.device_offline_critical_s,
                    "model": device.get("model"),
                    "ip": device.get("ip"),
                    "last_seen": last_seen,
                },
                trigger_data=_slim_device(device),
            )
        )

    def _device_flapping(
        self,
        entity_type: str,
        entity_id: str,
        name: str,
        kind: str,
        thresholds: Thresholds,
        ts: int,
        result: AnalysisResult,
    ) -> None:
        window_start = ts - thresholds.flap_window_s
        drops = self.db.transitions_since(entity_type, entity_id, window_start, to_state="offline")
        count = len(drops)
        if count < thresholds.flap_warning_count:
            return
        severity = "critical" if count >= thresholds.flap_critical_count else "warning"
        issue_type = "device_flapping" if entity_type == "device" else "client_flapping"
        window = humanize_duration(thresholds.flap_window_s)
        result.observations.append(
            Observation(
                issue_type=issue_type,
                severity=severity,
                entity_type=entity_type,
                entity_id=entity_id,
                entity_name=name,
                summary=f"{name} ({kind}) dropped {count}x in the last {window}",
                details={
                    "disconnects": count,
                    "window_s": thresholds.flap_window_s,
                    "warning_threshold": thresholds.flap_warning_count,
                    "critical_threshold": thresholds.flap_critical_count,
                    "disconnect_times": [int(d["ts"]) for d in drops],
                },
                trigger_data=[
                    {"ts": int(d["ts"]), "from": d["from_state"], "to": d["to_state"]}
                    for d in drops
                ],
            )
        )

    def _device_load(
        self,
        device: dict[str, Any],
        mac: str,
        name: str,
        thresholds: Thresholds,
        ts: int,
        result: AnalysisResult,
    ) -> None:
        sys_stats = device.get("system-stats") or device.get("sys_stats") or {}
        checks = (
            ("cpu_pct", "device_high_cpu", "CPU", thresholds.cpu_warning_pct, thresholds.cpu_critical_pct),
            ("mem_pct", "device_high_memory", "memory", thresholds.mem_warning_pct, None),
        )
        for metric, issue_type, label, warn, crit in checks:
            current = _to_float(sys_stats.get("cpu" if metric == "cpu_pct" else "mem"))
            if current is None:
                continue
            recent = self.db.recent_metric_values(
                "device", mac, metric, limit=thresholds.load_sustained_polls
            )
            # Require the condition to hold across consecutive polls; a single
            # spike during a firmware check is not an incident.
            if len(recent) < thresholds.load_sustained_polls:
                continue
            if not all(value >= warn for value in recent):
                continue
            severity = "critical" if crit and current >= crit else "warning"
            result.observations.append(
                Observation(
                    issue_type=issue_type,
                    severity=severity,
                    entity_type="device",
                    entity_id=mac,
                    entity_name=name,
                    summary=f"{name} {label} at {current:.0f}%",
                    details={
                        "current_pct": current,
                        "recent_pct": recent,
                        "warning_threshold": warn,
                        "critical_threshold": crit,
                        "sustained_polls": thresholds.load_sustained_polls,
                    },
                    trigger_data={"system-stats": sys_stats},
                )
            )

    def _device_adoption(
        self,
        device: dict[str, Any],
        mac: str,
        name: str,
        kind: str,
        state: str,
        ts: int,
        result: AnalysisResult,
    ) -> None:
        if state in {"adoption_failed", "inform_error", "isolated"} or (
            device.get("adopted") is False and state != "pending_adoption"
        ):
            result.observations.append(
                Observation(
                    issue_type="device_not_adopted",
                    severity="warning",
                    entity_type="device",
                    entity_id=mac,
                    entity_name=name,
                    summary=f"{name} ({kind}) in state '{state}'",
                    details={"state": state, "adopted": device.get("adopted")},
                    trigger_data=_slim_device(device),
                )
            )

    # ------------------------------------------------------------------ ports

    def _ports(
        self,
        device: dict[str, Any],
        mac: str,
        device_name: str,
        thresholds: Thresholds,
        ts: int,
        result: AnalysisResult,
    ) -> None:
        for port in device.get("port_table") or []:
            idx = port.get("port_idx")
            if idx is None:
                continue
            port_id = f"{mac}:{idx}"
            port_name = port.get("name") or f"port {idx}"
            label = f"{device_name} {port_name}"
            up = bool(port.get("up"))

            self.db.upsert_entity(
                "port",
                port_id,
                name=label,
                kind="port",
                meta={"device_mac": mac, "port_idx": idx, "poe_mode": port.get("poe_mode")},
                ts=ts,
            )

            poe_power = _to_float(port.get("poe_power"))
            prev_poe = self.db.recent_metric_values("port", port_id, "poe_power", limit=1)
            rows: list[tuple] = []
            # Only record draw while the port has link. A dark port reads 0W,
            # and overwriting the last good reading with that zero would erase
            # the evidence that something powered used to be plugged in here.
            if poe_power is not None and up:
                rows.append((ts, "port", port_id, "poe_power", poe_power, None))

            # Error/drop counters are cumulative; convert to per-minute rates.
            rates: dict[str, float] = {}
            for counter in ("rx_errors", "tx_errors", "rx_dropped", "tx_dropped"):
                total = _to_float(port.get(counter))
                if total is None:
                    continue
                delta, elapsed = self.db.counter_delta("port", port_id, counter, total, ts)
                if delta is None or not elapsed:
                    continue
                per_minute = delta * 60.0 / elapsed
                rates[counter] = per_minute
                rows.append((ts, "port", port_id, f"{counter}_per_min", per_minute, None))
            self.db.add_metrics(rows)

            self.db.set_state("port", port_id, "up" if up else "down", ts=ts, entity_name=label)

            # Re-emitted every poll while the port stays dark, so the issue
            # ages instead of resolving and re-opening on each cycle.
            if not up and prev_poe and prev_poe[0] > 0.5:
                # A PoE port that was delivering power and just went dark: the
                # powered thing on the end of it is the story here.
                result.observations.append(
                    Observation(
                        issue_type="poe_port_down",
                        severity="warning",
                        entity_type="port",
                        entity_id=port_id,
                        entity_name=label,
                        summary=f"{label} lost link (was drawing {prev_poe[0]:.1f}W PoE)",
                        details={
                            "device_mac": mac,
                            "device_name": device_name,
                            "port_idx": idx,
                            "previous_poe_watts": prev_poe[0],
                        },
                        trigger_data=_slim_port(port),
                    )
                )

            error_rate = rates.get("rx_errors", 0.0) + rates.get("tx_errors", 0.0)
            drop_rate = rates.get("rx_dropped", 0.0) + rates.get("tx_dropped", 0.0)
            if error_rate >= thresholds.port_error_rate_warning:
                severity = (
                    "critical" if error_rate >= thresholds.port_error_rate_critical else "warning"
                )
                result.observations.append(
                    Observation(
                        issue_type="port_errors",
                        severity=severity,
                        entity_type="port",
                        entity_id=port_id,
                        entity_name=label,
                        summary=f"{label} logging {error_rate:.0f} errors/min",
                        details={
                            "errors_per_min": error_rate,
                            "drops_per_min": drop_rate,
                            "rates": rates,
                            "warning_threshold": thresholds.port_error_rate_warning,
                            "critical_threshold": thresholds.port_error_rate_critical,
                            "device_mac": mac,
                            "device_name": device_name,
                            "port_idx": idx,
                            "speed": port.get("speed"),
                            "full_duplex": port.get("full_duplex"),
                        },
                        trigger_data=_slim_port(port),
                    )
                )
            elif drop_rate >= thresholds.port_drop_rate_warning:
                result.observations.append(
                    Observation(
                        issue_type="port_errors",
                        severity="warning",
                        entity_type="port",
                        entity_id=port_id,
                        entity_name=label,
                        summary=f"{label} dropping {drop_rate:.0f} frames/min",
                        details={
                            "errors_per_min": error_rate,
                            "drops_per_min": drop_rate,
                            "rates": rates,
                            "warning_threshold": thresholds.port_drop_rate_warning,
                            "device_mac": mac,
                            "device_name": device_name,
                            "port_idx": idx,
                        },
                        trigger_data=_slim_port(port),
                    )
                )

    # ---------------------------------------------------------------- clients

    def _clients(
        self,
        active: list[dict[str, Any]],
        known: list[dict[str, Any]],
        ts: int,
        result: AnalysisResult,
    ) -> None:
        active_by_mac: dict[str, dict[str, Any]] = {}
        for client in active:
            mac = normalize_mac(client.get("mac"))
            if mac:
                active_by_mac[mac] = client

        known_by_mac: dict[str, dict[str, Any]] = {}
        for client in known:
            mac = normalize_mac(client.get("mac"))
            if mac:
                known_by_mac[mac] = client

        for mac, client in active_by_mac.items():
            self._client_online(mac, client, ts, result)

        # Anything we know about that is not currently associated.
        for mac, client in known_by_mac.items():
            if mac in active_by_mac:
                continue
            self._client_missing(mac, client, ts, result)

    def _client_name(self, client: dict[str, Any], mac: str) -> str:
        return str(
            client.get("name")
            or client.get("hostname")
            or client.get("display_name")
            or client.get("oui")
            or mac
        )

    def _client_online(
        self, mac: str, client: dict[str, Any], ts: int, result: AnalysisResult
    ) -> None:
        name = self._client_name(client, mac)
        wired = bool(client.get("is_wired"))
        self.db.upsert_entity(
            "client",
            mac,
            name=name,
            kind="wired client" if wired else "wireless client",
            model=client.get("oui"),
            meta={
                "ip": client.get("ip"),
                "network": client.get("network"),
                "essid": client.get("essid"),
                "ap_mac": normalize_mac(client.get("ap_mac")),
                "sw_mac": normalize_mac(client.get("sw_mac")),
                "sw_port": client.get("sw_port"),
                "is_wired": wired,
            },
            ts=ts,
        )
        self.db.set_state(
            "client",
            mac,
            "online",
            ts=ts,
            entity_name=name,
            details={
                "ap_mac": normalize_mac(client.get("ap_mac")),
                "essid": client.get("essid"),
                "ip": client.get("ip"),
            },
        )

        rows: list[tuple] = []
        for field_name, metric in (("rx_bytes", "rx_bps"), ("tx_bytes", "tx_bps")):
            total = _to_float(client.get(field_name))
            if total is None:
                continue
            delta, elapsed = self.db.counter_delta("client", mac, field_name, total, ts)
            if delta is not None and elapsed:
                rows.append((ts, "client", mac, metric, delta * 8.0 / elapsed, None))
        for field_name, metric in (
            ("signal", "signal_dbm"),
            ("rssi", "rssi"),
            ("satisfaction", "satisfaction"),
            ("tx_retries", "tx_retries"),
            ("uptime", "uptime"),
        ):
            value = _to_float(client.get(field_name))
            if value is not None:
                rows.append((ts, "client", mac, metric, value, None))
        self.db.add_metrics(rows)

        if not self.cfg.is_watched_client(mac, name, client.get("hostname")):
            return

        thresholds = self.cfg.thresholds_for(mac, name)
        self._device_flapping(
            "client", mac, name, "client", thresholds, ts, result
        )
        self._bandwidth("client", mac, name, thresholds, result)

        signal = _to_float(client.get("signal"))
        if not wired and signal is not None and signal <= thresholds.signal_warning_dbm:
            result.observations.append(
                Observation(
                    issue_type="client_weak_signal",
                    severity="warning",
                    entity_type="client",
                    entity_id=mac,
                    entity_name=name,
                    summary=f"{name} signal {signal:.0f} dBm",
                    details={
                        "signal_dbm": signal,
                        "threshold_dbm": thresholds.signal_warning_dbm,
                        "essid": client.get("essid"),
                        "ap_mac": normalize_mac(client.get("ap_mac")),
                        "channel": client.get("channel"),
                        "satisfaction": client.get("satisfaction"),
                    },
                    trigger_data=_slim_client(client),
                )
            )

    def _client_missing(
        self, mac: str, client: dict[str, Any], ts: int, result: AnalysisResult
    ) -> None:
        name = self._client_name(client, mac)
        if not self.cfg.is_watched_client(mac, name, client.get("hostname")):
            # Not on the watchlist: record the state so history exists, but
            # never raise an issue. Untracked phones sleep; that is not news.
            if self.db.get_state("client", mac):
                self.db.set_state("client", mac, "offline", ts=ts, entity_name=name)
            return

        thresholds = self.cfg.thresholds_for(mac, name)
        self.db.upsert_entity("client", mac, name=name, kind="client", ts=ts)
        transition = self.db.set_state("client", mac, "offline", ts=ts, entity_name=name)

        since = int(transition["since"])
        last_seen = to_epoch(client.get("last_seen"))
        if last_seen and last_seen < since and last_seen <= ts:
            since = last_seen
        down_for = max(0, ts - since)
        if down_for < max(thresholds.client_offline_warning_s, thresholds.client_missing_grace_s):
            return

        severity = "critical" if down_for >= thresholds.client_offline_critical_s else "warning"
        result.observations.append(
            Observation(
                issue_type="client_offline",
                severity=severity,
                entity_type="client",
                entity_id=mac,
                entity_name=name,
                summary=f"{name} offline {humanize_duration(down_for)}",
                details={
                    "down_since": since,
                    "down_for_s": down_for,
                    "warning_threshold_s": thresholds.client_offline_warning_s,
                    "critical_threshold_s": thresholds.client_offline_critical_s,
                    "last_seen": last_seen,
                    "is_wired": client.get("is_wired"),
                },
                trigger_data=_slim_client(client),
            )
        )

    # -------------------------------------------------------------------- wan

    def _wan(
        self,
        health: list[dict[str, Any]],
        devices: list[dict[str, Any]],
        ts: int,
        result: AnalysisResult,
    ) -> None:
        samples = _wan_samples(health, devices)
        if not samples:
            return

        for sample in samples:
            wan_id = sample["wan_id"]
            previous = self.db.last_wan_sample(wan_id)
            self.db.add_wan_sample(sample, ts=ts)
            self.db.upsert_entity("wan", wan_id, name=sample.get("label") or wan_id, kind="wan", ts=ts)

            for metric, key in (
                ("latency_ms", "latency_ms"),
                ("loss_pct", "loss_pct"),
                ("xput_down_mbps", "xput_down"),
                ("xput_up_mbps", "xput_up"),
            ):
                self.db.add_metric("wan", wan_id, metric, sample.get(key), ts=ts)

            thresholds = self.cfg.thresholds_for(wan_id, sample.get("label"))
            status_ok = str(sample.get("status") or "").lower() in {"ok", "up", "connected"}
            transition = self.db.set_state(
                "wan",
                wan_id,
                "up" if status_ok else "down",
                ts=ts,
                entity_name=sample.get("label") or wan_id,
                raw_state=sample.get("status"),
                details={"isp": sample.get("isp"), "ip": sample.get("ip")},
            )

            if not status_ok:
                down_for = max(0, ts - int(transition["since"]))
                result.observations.append(
                    Observation(
                        issue_type="wan_down",
                        severity="critical",
                        entity_type="wan",
                        entity_id=wan_id,
                        entity_name=sample.get("label") or wan_id,
                        summary=f"{sample.get('label') or wan_id} down "
                        f"({sample.get('status') or 'unknown'}) {humanize_duration(down_for)}",
                        details={
                            "status": sample.get("status"),
                            "down_since": int(transition["since"]),
                            "down_for_s": down_for,
                            "isp": sample.get("isp"),
                            "ip": sample.get("ip"),
                        },
                        trigger_data=sample.get("raw"),
                    )
                )
                continue

            # Failover / re-address: the WAN is up, but not the same WAN.
            if previous:
                changes: dict[str, Any] = {}
                for key, db_key in (("ip", "ip"), ("isp", "isp"), ("gateway", "gateway")):
                    old, new = previous[db_key], sample.get(key)
                    if old and new and old != new:
                        changes[key] = {"from": old, "to": new}
                was_down = str(previous["status"] or "").lower() not in {"ok", "up", "connected"}
                if changes and not was_down:
                    severity = "warning" if "isp" in changes or "gateway" in changes else "info"
                    what = ", ".join(f"{k} {v['from']} -> {v['to']}" for k, v in changes.items())
                    result.observations.append(
                        Observation(
                            issue_type="wan_failover",
                            severity=severity,
                            entity_type="wan",
                            entity_id=wan_id,
                            entity_name=sample.get("label") or wan_id,
                            summary=f"{sample.get('label') or wan_id} changed: {what}",
                            details={"changes": changes, "previous_seen": int(previous["ts"])},
                            trigger_data=sample.get("raw"),
                        )
                    )

            latency = _to_float(sample.get("latency_ms"))
            if latency is not None and latency >= thresholds.wan_latency_warning_ms:
                severity = (
                    "critical" if latency >= thresholds.wan_latency_critical_ms else "warning"
                )
                result.observations.append(
                    Observation(
                        issue_type="wan_high_latency",
                        severity=severity,
                        entity_type="wan",
                        entity_id=wan_id,
                        entity_name=sample.get("label") or wan_id,
                        summary=f"{sample.get('label') or wan_id} latency {latency:.0f} ms",
                        details={
                            "latency_ms": latency,
                            "warning_threshold_ms": thresholds.wan_latency_warning_ms,
                            "critical_threshold_ms": thresholds.wan_latency_critical_ms,
                            "isp": sample.get("isp"),
                        },
                        trigger_data=sample.get("raw"),
                    )
                )

            loss = _to_float(sample.get("loss_pct"))
            if loss is not None and loss >= thresholds.wan_loss_warning_pct:
                severity = "critical" if loss >= thresholds.wan_loss_critical_pct else "warning"
                result.observations.append(
                    Observation(
                        issue_type="wan_packet_loss",
                        severity=severity,
                        entity_type="wan",
                        entity_id=wan_id,
                        entity_name=sample.get("label") or wan_id,
                        summary=f"{sample.get('label') or wan_id} packet loss {loss:.1f}%",
                        details={
                            "loss_pct": loss,
                            "warning_threshold_pct": thresholds.wan_loss_warning_pct,
                            "critical_threshold_pct": thresholds.wan_loss_critical_pct,
                        },
                        trigger_data=sample.get("raw"),
                    )
                )

    # ----------------------------------------------------------------- alarms

    def _alarms(self, alarms: list[dict[str, Any]], ts: int, result: AnalysisResult) -> None:
        for alarm in alarms:
            if alarm.get("archived"):
                continue
            remote_id = str(alarm.get("_id") or alarm.get("id") or "")
            alarm_ts = to_epoch(alarm.get("time") or alarm.get("datetime")) or ts
            key = str(alarm.get("key") or "alarm")
            entity_id = normalize_mac(alarm.get("ap") or alarm.get("gw") or alarm.get("sw")) or key
            entity_name = (
                alarm.get("ap_name")
                or alarm.get("gw_name")
                or alarm.get("sw_name")
                or alarm.get("device_name")
                or entity_id
            )
            message = str(alarm.get("msg") or key)
            severity = _alarm_severity(key, alarm)

            if remote_id:
                self.db.record_controller_event(
                    "alarm",
                    remote_id,
                    alarm_ts,
                    key=key,
                    subsystem=alarm.get("subsystem"),
                    severity=severity,
                    entity_id=entity_id,
                    entity_name=str(entity_name),
                    message=message,
                    raw=alarm,
                )

            if severity_below(severity, self.cfg.thresholds.alarm_min_severity):
                continue
            if self.cfg.is_ignored(entity_id, entity_name, key):
                continue
            result.observations.append(
                Observation(
                    issue_type="controller_alarm",
                    severity=severity,
                    entity_type="device" if entity_id != key else "site",
                    entity_id=entity_id,
                    entity_name=str(entity_name),
                    summary=f"Controller alarm: {message}",
                    details={
                        "alarm_key": key,
                        "alarm_time": alarm_ts,
                        "subsystem": alarm.get("subsystem"),
                    },
                    trigger_data=alarm,
                )
            )

    # ----------------------------------------------------------------- events

    def _events(self, events: list[dict[str, Any]], ts: int) -> None:
        """Events are context, not issues — recorded for Part 2 to correlate."""
        stored = 0
        for event in events:
            remote_id = str(event.get("_id") or event.get("id") or "")
            if not remote_id:
                continue
            event_ts = to_epoch(event.get("time") or event.get("datetime")) or ts
            entity_id = normalize_mac(
                event.get("ap") or event.get("sw") or event.get("gw") or event.get("user")
            )
            entity_name = (
                event.get("ap_name")
                or event.get("sw_name")
                or event.get("gw_name")
                or event.get("hostname")
            )
            if self.db.record_controller_event(
                "event",
                remote_id,
                event_ts,
                key=str(event.get("key") or ""),
                subsystem=event.get("subsystem"),
                severity=_event_severity(str(event.get("key") or "")),
                entity_id=entity_id,
                entity_name=str(entity_name) if entity_name else None,
                message=str(event.get("msg") or event.get("key") or ""),
                raw=event,
            ):
                stored += 1
        if stored:
            LOG.debug("recorded %d new controller events", stored)


# --------------------------------------------------------------------- helpers


def severity_below(severity: str, minimum: str) -> bool:
    from .issues import severity_rank

    return severity_rank(severity) < severity_rank(minimum)


def _wan_samples(
    health: list[dict[str, Any]], devices: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Build one sample per WAN from the health subsystems and the gateway.

    ``stat/health`` reports aggregate internet health; the gateway device
    carries per-uplink detail (``wan1``/``wan2``) which is what actually
    distinguishes a failover from a blip.
    """
    samples: dict[str, dict[str, Any]] = {}

    for subsystem in health:
        name = str(subsystem.get("subsystem") or "").lower()
        if name not in {"wan", "www"}:
            continue
        # 'wan' carries link status, 'www' carries reachability and latency.
        # Both describe the same uplink, so merge rather than let the later
        # one blank out fields the earlier one filled in.
        gateway_value = subsystem.get("gw_mac") or subsystem.get("gateways")
        if isinstance(gateway_value, list):
            gateway_value = gateway_value[0] if gateway_value else None
        incoming = {
            "wan_id": "wan",
            "label": "WAN",
            "active": True,
            "status": subsystem.get("status"),
            "isp": subsystem.get("isp_name") or subsystem.get("isp_organization"),
            "ip": subsystem.get("wan_ip"),
            "gateway": gateway_value,
            "latency_ms": _to_float(subsystem.get("latency") or subsystem.get("speedtest_ping")),
            "loss_pct": _to_float(subsystem.get("drops")),
            "xput_down": _to_float(subsystem.get("xput_down")),
            "xput_up": _to_float(subsystem.get("xput_up")),
            "uptime": _to_float(subsystem.get("uptime")),
        }
        existing = samples.setdefault("wan", {"raw": {}})
        for key, value in incoming.items():
            if value is not None or key not in existing:
                existing[key] = value
        # A bad status from either subsystem is the one worth keeping.
        if str(subsystem.get("status") or "").lower() not in {"ok", "up", "connected", ""}:
            existing["status"] = subsystem.get("status")
        existing["raw"] = {**(existing.get("raw") or {}), name: subsystem}

    for device in devices:
        dev_type = str(device.get("type") or "").lower()
        if dev_type not in {"ugw", "udm", "uxg"}:
            continue
        for key in ("wan1", "wan2"):
            wan = device.get(key)
            if not isinstance(wan, dict) or not wan.get("enable", True):
                continue
            has_addressing = bool(wan.get("ip")) or wan.get("up") is not None
            if not has_addressing:
                continue
            samples[key] = {
                "wan_id": key,
                "label": wan.get("name") or key.upper(),
                "status": "ok" if wan.get("up") else "down",
                "active": bool(wan.get("up")),
                "isp": wan.get("isp_name") or wan.get("isp_organization"),
                "ip": wan.get("ip"),
                "gateway": wan.get("gateway") or wan.get("gateway_ip"),
                "latency_ms": _to_float(wan.get("latency")),
                "loss_pct": None,
                "xput_down": _to_float(wan.get("rx_bytes-r")),
                "xput_up": _to_float(wan.get("tx_bytes-r")),
                "uptime": _to_float(wan.get("uptime")),
                "raw": wan,
            }
    return list(samples.values())


def _alarm_severity(key: str, alarm: dict[str, Any]) -> str:
    lowered = key.lower()
    explicit = str(alarm.get("severity") or "").lower()
    if explicit in {"info", "warning", "critical"}:
        return explicit
    if any(token in lowered for token in ("lost_contact", "disconnected", "wan_transition", "isolated")):
        return "critical"
    if any(token in lowered for token in ("restarted", "poe_disconnect", "upgrade_failed", "error")):
        return "warning"
    return "info"


def _event_severity(key: str) -> str:
    lowered = key.lower()
    if any(token in lowered for token in ("lost_contact", "wan_transition", "disconnected")):
        return "warning"
    return "info"


def _to_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result == result else None  # drop NaN


def _slim_device(device: dict[str, Any]) -> dict[str, Any]:
    """The raw fields worth storing with an issue. The full device record is
    tens of kilobytes; this keeps the trigger data readable."""
    keys = (
        "mac", "name", "model", "type", "state", "adopted", "ip", "version",
        "uptime", "last_seen", "num_sta", "satisfaction", "upgradable",
        "uplink", "system-stats", "disconnection_reason",
    )
    return {k: device.get(k) for k in keys if k in device}


def _slim_client(client: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "mac", "name", "hostname", "oui", "ip", "is_wired", "network", "essid",
        "ap_mac", "sw_mac", "sw_port", "signal", "rssi", "noise", "channel",
        "satisfaction", "tx_retries", "uptime", "last_seen", "first_seen",
        "disconnect_reason",
    )
    return {k: client.get(k) for k in keys if k in client}


def _slim_port(port: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "port_idx", "name", "up", "enable", "speed", "full_duplex", "media",
        "poe_enable", "poe_mode", "poe_power", "poe_voltage", "poe_good",
        "rx_errors", "tx_errors", "rx_dropped", "tx_dropped", "rx_bytes", "tx_bytes",
    )
    return {k: port.get(k) for k in keys if k in port}
