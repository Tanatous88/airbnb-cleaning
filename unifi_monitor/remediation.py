"""Part 2: remediation as *proposals*.

Nothing in this module changes the network. It turns a flagged issue into a
list of concrete, reviewable actions — each one carrying the exact controller
call it would make, the risk of making it, and the permission it would need.

Three guards stand between a proposal and execution:

  1. The proposal must be confirmed by a human, by id, with a one-time token.
  2. ``UNIFI_ALLOW_WRITE_ACTIONS`` must be explicitly true.
  3. The controller account must actually have write permission — today's
     account is view-only, so every execution attempt is refused with the
     permission upgrade spelled out.

Auto-remediation is not implemented, and deliberately so: there is no code
path in this repository that executes an action without a confirmation
argument supplied by a caller acting on a human's instruction.
"""

from __future__ import annotations

import os
import secrets
import sqlite3
from typing import Any, Callable

from .util import LOG, dumps, env_bool, load_env_file, loads, normalize_mac, now


def actions_db_path() -> str:
    """Where action proposals are logged, resolved at call time.

    Same reason as ``query.default_db_path``: ``UNIFI_ACTIONS_DB`` usually
    arrives via the env file, which is loaded after import.
    """
    load_env_file()
    return os.environ.get("UNIFI_ACTIONS_DB") or os.path.join(
        os.environ.get("UNIFI_MONITOR_HOME")
        or os.path.join(os.path.expanduser("~"), ".unifi_monitor"),
        "unifi_actions.db",
    )

# Proposals live in their own file. The monitoring database stays a read-only
# surface for Part 2, so nothing here can ever interfere with the poller.
ACTIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS action_proposals (
    id          TEXT PRIMARY KEY,
    created_at  INTEGER NOT NULL,
    issue_id    INTEGER,
    issue_type  TEXT,
    entity_type TEXT,
    entity_id   TEXT,
    entity_name TEXT,
    action_key  TEXT NOT NULL,
    title       TEXT NOT NULL,
    rationale   TEXT,
    risk        TEXT,
    call_spec   TEXT,
    manual_steps TEXT,
    token       TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'proposed',
    decided_at  INTEGER,
    decided_by  TEXT,
    outcome     TEXT
);
CREATE INDEX IF NOT EXISTS idx_proposals_issue ON action_proposals(issue_id, created_at DESC);

CREATE TABLE IF NOT EXISTS action_audit (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          INTEGER NOT NULL,
    proposal_id TEXT,
    event       TEXT NOT NULL,
    actor       TEXT,
    detail      TEXT
);
"""


# --------------------------------------------------------------- catalogue

def _poe_cycle_spec(device_mac: str | None, port_idx: Any) -> dict[str, Any]:
    """The controller call that power-cycles one PoE port."""
    return {
        "method": "POST",
        "path": "/proxy/network/api/s/{site}/cmd/devmgr",
        "body": {"cmd": "power-cycle", "mac": device_mac, "port_idx": port_idx},
        "effect": "Drops power on the port for ~5s, rebooting whatever it feeds.",
        "reversible": True,
    }


def _restart_spec(device_mac: str | None) -> dict[str, Any]:
    return {
        "method": "POST",
        "path": "/proxy/network/api/s/{site}/cmd/devmgr",
        "body": {"cmd": "restart", "mac": device_mac},
        "effect": "Reboots the device; everything attached to it drops for 1-3 min.",
        "reversible": True,
    }


def _speedtest_spec() -> dict[str, Any]:
    return {
        "method": "POST",
        "path": "/proxy/network/api/s/{site}/cmd/devmgr",
        "body": {"cmd": "speedtest"},
        "effect": "Runs a WAN speed test; saturates the uplink for ~30s.",
        "reversible": True,
    }


def _archive_alarm_spec(alarm_id: str | None) -> dict[str, Any]:
    return {
        "method": "POST",
        "path": "/proxy/network/api/s/{site}/cmd/evtmgr",
        "body": {"cmd": "archive-alarm", "_id": alarm_id},
        "effect": "Clears the alarm from the controller. Does not fix anything.",
        "reversible": False,
    }


def propose_actions(issue: dict[str, Any], report: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Candidate actions for one issue. Nothing is executed or persisted here.

    Every entry is a *suggestion for a human*, ordered least invasive first.
    """
    report = report or {}
    issue_type = str(issue.get("issue_type") or "")
    details = issue.get("details") or {}
    trigger = issue.get("trigger_data") or {}
    entity_id = str(issue.get("entity_id") or "")
    entity_name = issue.get("entity_name") or entity_id
    entity_meta = ((report.get("entity") or {}).get("meta")) or {}

    actions: list[dict[str, Any]] = []

    def add(
        key: str,
        title: str,
        rationale: str,
        *,
        risk: str = "low",
        call: dict[str, Any] | None = None,
        manual: list[str] | None = None,
        permission: str = "network-write",
    ) -> None:
        actions.append(
            {
                "action_key": key,
                "title": title,
                "rationale": rationale,
                "risk": risk,
                "controller_call": call,
                "manual_steps": manual or [],
                "requires_permission": permission if call else "none",
                "requires_confirmation": True,
                "auto_execute": False,
            }
        )

    if issue_type in {"device_offline", "device_flapping", "device_not_adopted"}:
        uplink = trigger.get("uplink") or {}
        upstream_mac = normalize_mac(uplink.get("uplink_mac") or uplink.get("uplink_device_mac"))
        upstream_port = uplink.get("uplink_remote_port") or uplink.get("port_idx")
        if upstream_mac and upstream_port:
            add(
                "poe_cycle_uplink",
                f"Power-cycle the switch port feeding {entity_name} "
                f"(port {upstream_port} on {upstream_mac})",
                "Re-powers the device without anyone walking to the closet. Standard "
                "first move for an AP that stopped informing.",
                risk="medium",
                call=_poe_cycle_spec(upstream_mac, upstream_port),
                manual=[f"UniFi UI: Devices -> upstream switch -> Ports -> {upstream_port} -> Power Cycle"],
            )
        add(
            "restart_device",
            f"Restart {entity_name}",
            "Clears a wedged device that is powered but not reporting.",
            risk="medium",
            call=_restart_spec(entity_id),
            manual=[f"UniFi UI: Devices -> {entity_name} -> Settings -> Restart"],
        )
        add(
            "inspect_physical",
            f"Check power and cabling for {entity_name}",
            "If the device never comes back after a power cycle, the fault is "
            "physical: PoE injector, cable run, or the device itself.",
            risk="none",
            manual=[
                "Confirm the switch port shows link and PoE draw",
                "Re-seat both ends of the cable",
                "Move to a known-good port to isolate port vs device",
            ],
            permission="none",
        )

    if issue_type in {"client_offline", "client_flapping", "client_weak_signal"}:
        sw_mac = normalize_mac(entity_meta.get("sw_mac") or trigger.get("sw_mac"))
        sw_port = entity_meta.get("sw_port") or trigger.get("sw_port")
        wired = entity_meta.get("is_wired") or trigger.get("is_wired")
        if wired and sw_mac and sw_port:
            add(
                "poe_cycle_client_port",
                f"Power-cycle PoE on port {sw_port} of {sw_mac} (feeds {entity_name})",
                "Reboots the endpoint itself — the usual fix for a PoE camera that "
                "has stopped responding but still draws power.",
                risk="medium",
                call=_poe_cycle_spec(sw_mac, sw_port),
                manual=[f"UniFi UI: Devices -> switch {sw_mac} -> Ports -> {sw_port} -> Power Cycle"],
            )
        if not wired:
            add(
                "check_wireless_coverage",
                f"Review AP coverage and band for {entity_name}",
                "Repeated wireless drops with weak signal usually mean the client is "
                "at the edge of its AP's range or roaming between APs.",
                risk="none",
                manual=[
                    "Check signal history in the issue report (signal_dbm baseline)",
                    "Consider band steering off / minimum RSSI for this client",
                    "Relocate the client or add an AP if signal is consistently below -75 dBm",
                ],
                permission="none",
            )
        add(
            "reserve_dhcp",
            f"Give {entity_name} a fixed IP reservation",
            "Rules out DHCP lease churn as the reason a device appears to vanish.",
            risk="low",
            manual=["UniFi UI: Client Devices -> " + str(entity_name) + " -> Settings -> Fixed IP"],
        )

    if issue_type == "poe_port_down":
        device_mac = details.get("device_mac")
        port_idx = details.get("port_idx")
        add(
            "poe_cycle_port",
            f"Power-cycle PoE on port {port_idx} of {details.get('device_name')}",
            f"The port was delivering {details.get('previous_poe_watts')}W and lost link; "
            "re-powering restarts the attached device.",
            risk="medium",
            call=_poe_cycle_spec(device_mac, port_idx),
            manual=[f"UniFi UI: Devices -> {details.get('device_name')} -> Ports -> {port_idx} -> Power Cycle"],
        )

    if issue_type == "port_errors":
        add(
            "replace_cable",
            f"Re-seat or replace the cable on {entity_name}",
            f"{details.get('errors_per_min', 0):.0f} errors/min at "
            f"{details.get('speed')} Mbps is a physical-layer fault, not a config one.",
            risk="none",
            manual=[
                "Re-seat both ends; look for kinks, staples, and runs alongside mains cable",
                "Swap to a known-good patch lead first — it is the cheapest test",
            ],
            permission="none",
        )
        add(
            "force_port_speed",
            f"Pin {entity_name} to 100 Mbps full duplex",
            "If errors persist on a good cable, forcing a slower speed often stabilises "
            "a marginal run until it can be replaced.",
            risk="medium",
            manual=[
                f"UniFi UI: Devices -> {details.get('device_name')} -> Ports -> "
                f"{details.get('port_idx')} -> Link Speed -> 100 Mbps"
            ],
        )

    if issue_type in {"wan_down", "wan_high_latency", "wan_packet_loss", "wan_failover"}:
        add(
            "run_speedtest",
            "Run a controller speed test",
            "Establishes whether the uplink is degraded or simply idle.",
            risk="low",
            call=_speedtest_spec(),
            manual=["UniFi UI: Internet -> Speed Test"],
        )
        add(
            "check_modem",
            "Power-cycle the modem/ONT and check the ISP status page",
            "A WAN fault upstream of the gateway cannot be fixed from the controller.",
            risk="medium",
            manual=[
                "Note the WAN IP and ISP from the issue details before rebooting",
                "Power the modem off for 30s, then back on",
                "If the IP changes but latency stays high, escalate to the ISP with the "
                "timestamps from this issue",
            ],
            permission="none",
        )

    if issue_type in {"device_high_cpu", "device_high_memory"}:
        add(
            "restart_device",
            f"Restart {entity_name}",
            "Frees memory or a runaway process; also the fastest way to confirm the "
            "load is a leak rather than real traffic.",
            risk="medium",
            call=_restart_spec(entity_id),
            manual=[f"UniFi UI: Devices -> {entity_name} -> Settings -> Restart"],
        )
        add(
            "check_firmware",
            f"Check firmware level on {entity_name}",
            "Sustained high load on an out-of-date device is often a known, fixed bug.",
            risk="low",
            manual=["UniFi UI: Devices -> select device -> Update firmware (schedule a window)"],
        )

    if issue_type == "controller_alarm":
        add(
            "archive_alarm",
            "Archive this controller alarm",
            "Only do this once the underlying condition has been confirmed resolved — "
            "archiving hides the alarm, it does not fix it.",
            risk="low",
            call=_archive_alarm_spec((trigger or {}).get("_id")),
            manual=["UniFi UI: Alerts -> select alarm -> Archive"],
        )

    if issue_type in {"controller_unreachable", "controller_auth_failed", "controller_error"}:
        add(
            "check_monitor_host",
            "Check the poller host and controller reachability",
            "This issue is about the monitoring path, not the network itself.",
            risk="none",
            manual=[
                f"From the poller host: curl -k https://{details.get('host')}/ ",
                "Confirm UNIFI_USERNAME/UNIFI_PASSWORD are still valid (local console account)",
                "Confirm the account has not been locked out by repeated failures",
            ],
            permission="none",
        )

    if not actions:
        add(
            "investigate",
            f"Review {entity_name} history before acting",
            "No pre-canned remediation matches this issue type.",
            risk="none",
            manual=["Use device_history() for the full timeline around the flag"],
            permission="none",
        )
    return actions


# ------------------------------------------------------------ persistence

def _actions_db(path: str | None = None) -> sqlite3.Connection:
    db_path = path or actions_db_path()
    parent = os.path.dirname(os.path.abspath(db_path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.executescript(ACTIONS_SCHEMA)
    return conn


def record_proposals(
    issue: dict[str, Any], actions: list[dict[str, Any]], *, db_path: str | None = None
) -> list[dict[str, Any]]:
    """Persist proposals so a later confirmation can refer to one by id.

    Each gets a single-use token; confirming requires quoting it back, which
    makes an accidental or model-initiated execution impossible to fake from
    the issue id alone.
    """
    conn = _actions_db(db_path)
    stored: list[dict[str, Any]] = []
    ts = now()
    try:
        with conn:
            for action in actions:
                proposal_id = f"act_{secrets.token_hex(6)}"
                token = secrets.token_urlsafe(12)
                conn.execute(
                    """
                    INSERT INTO action_proposals(id, created_at, issue_id, issue_type,
                        entity_type, entity_id, entity_name, action_key, title, rationale,
                        risk, call_spec, manual_steps, token, status)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,'proposed')
                    """,
                    (
                        proposal_id,
                        ts,
                        issue.get("id"),
                        issue.get("issue_type"),
                        issue.get("entity_type"),
                        issue.get("entity_id"),
                        issue.get("entity_name"),
                        action["action_key"],
                        action["title"],
                        action.get("rationale"),
                        action.get("risk"),
                        dumps(action.get("controller_call")),
                        dumps(action.get("manual_steps")),
                        token,
                    ),
                )
                conn.execute(
                    "INSERT INTO action_audit(ts, proposal_id, event, actor, detail) "
                    "VALUES(?,?,?,?,?)",
                    (ts, proposal_id, "proposed", "part2", action["title"]),
                )
                stored.append(
                    {
                        **action,
                        "id": proposal_id,
                        "confirmation_token": token,
                        "status": "proposed",
                        "issue_id": issue.get("id"),
                    }
                )
    finally:
        conn.close()
    return stored


def list_proposals(
    *, issue_id: int | None = None, status: str | None = None, limit: int = 50,
    db_path: str | None = None,
) -> list[dict[str, Any]]:
    conn = _actions_db(db_path)
    try:
        sql = ["SELECT * FROM action_proposals WHERE 1=1"]
        params: list[Any] = []
        if issue_id is not None:
            sql.append("AND issue_id=?")
            params.append(issue_id)
        if status:
            sql.append("AND status=?")
            params.append(status)
        sql.append("ORDER BY created_at DESC LIMIT ?")
        params.append(limit)
        rows = conn.execute(" ".join(sql), params).fetchall()
        return [
            {
                **{k: row[k] for k in row.keys() if k != "token"},
                "call_spec": loads(row["call_spec"]),
                "manual_steps": loads(row["manual_steps"]),
            }
            for row in rows
        ]
    finally:
        conn.close()


# -------------------------------------------------------------- execution

class ActionBlocked(RuntimeError):
    """Raised when execution is attempted without every guard satisfied."""


def confirm_and_execute(
    proposal_id: str,
    confirmation_token: str,
    *,
    actor: str = "user",
    db_path: str | None = None,
    executor: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Attempt to run a previously proposed action.

    Returns a result dict rather than raising for the expected refusals, so a
    chat surface can relay *why* nothing happened. As shipped this always
    refuses: the controller account is view-only and no executor is wired up.
    """
    conn = _actions_db(db_path)
    try:
        row = conn.execute("SELECT * FROM action_proposals WHERE id=?", (proposal_id,)).fetchone()
        if row is None:
            return {"executed": False, "reason": "unknown_proposal", "proposal_id": proposal_id}
        if row["status"] != "proposed":
            return {
                "executed": False,
                "reason": "already_decided",
                "status": row["status"],
                "decided_at": row["decided_at"],
            }
        if not secrets.compare_digest(str(row["token"]), str(confirmation_token or "")):
            _audit(conn, proposal_id, "confirmation_rejected", actor, "token mismatch")
            return {"executed": False, "reason": "bad_confirmation_token"}

        call_spec = loads(row["call_spec"])
        if not call_spec:
            _audit(conn, proposal_id, "acknowledged_manual", actor, "manual-only action")
            _decide(conn, proposal_id, "manual", actor, "manual steps acknowledged")
            return {
                "executed": False,
                "reason": "manual_action_only",
                "manual_steps": loads(row["manual_steps"]),
                "message": "This action has no controller call — it is done by hand.",
            }

        if not env_bool("UNIFI_ALLOW_WRITE_ACTIONS", False):
            _audit(conn, proposal_id, "blocked", actor, "write actions disabled")
            return {
                "executed": False,
                "reason": "write_actions_disabled",
                "message": (
                    "Execution is disabled. The monitoring account is view-only; running "
                    "this needs (1) a controller account with write permission, (2) "
                    "UNIFI_ALLOW_WRITE_ACTIONS=true, and (3) an executor wired up."
                ),
                "would_call": call_spec,
                "manual_steps": loads(row["manual_steps"]),
                "required_permission": "UniFi Network: Site Admin (write) on the target site",
            }

        if executor is None:
            _audit(conn, proposal_id, "blocked", actor, "no executor configured")
            return {
                "executed": False,
                "reason": "no_executor_configured",
                "message": "Write actions are enabled but no executor was supplied.",
                "would_call": call_spec,
            }

        LOG.warning("executing confirmed action %s (%s) for %s", proposal_id, row["action_key"], actor)
        outcome = executor(call_spec)
        _decide(conn, proposal_id, "executed", actor, dumps(outcome))
        _audit(conn, proposal_id, "executed", actor, dumps(outcome))
        return {"executed": True, "proposal_id": proposal_id, "result": outcome}
    finally:
        conn.close()


def reject_proposal(
    proposal_id: str, *, actor: str = "user", note: str = "", db_path: str | None = None
) -> dict[str, Any]:
    conn = _actions_db(db_path)
    try:
        _decide(conn, proposal_id, "rejected", actor, note)
        _audit(conn, proposal_id, "rejected", actor, note)
        return {"proposal_id": proposal_id, "status": "rejected"}
    finally:
        conn.close()


def _decide(conn: sqlite3.Connection, proposal_id: str, status: str, actor: str, outcome: str) -> None:
    with conn:
        conn.execute(
            "UPDATE action_proposals SET status=?, decided_at=?, decided_by=?, outcome=? WHERE id=?",
            (status, now(), actor, outcome[:2000], proposal_id),
        )


def _audit(conn: sqlite3.Connection, proposal_id: str, event: str, actor: str, detail: str) -> None:
    with conn:
        conn.execute(
            "INSERT INTO action_audit(ts, proposal_id, event, actor, detail) VALUES(?,?,?,?,?)",
            (now(), proposal_id, event, actor, detail[:2000]),
        )
