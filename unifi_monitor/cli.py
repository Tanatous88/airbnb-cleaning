"""Command line entry point for both halves.

  Part 1:  check | poll | run | prune | status
  Part 2:  issues | explain | history | patterns | ask | actions | confirm | reject

Part 2 commands never import the poller, and vice versa — the imports are
local to each handler on purpose.

Every command takes ``--json`` so a gateway can shell out and parse.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

from .util import humanize_duration, setup_logging


def _force_utf8_output() -> None:
    """Make stdout/stderr able to carry what we actually print.

    Under Task Scheduler on Windows these streams default to a legacy codepage
    (cp1252) that can encode neither the severity emoji nor a device named with
    a curly apostrophe. The default behaviour is UnicodeEncodeError mid-alert;
    ``errors="replace"`` keeps the message.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:  # already replaced by a plain object
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass


def main(argv: list[str] | None = None) -> int:
    _force_utf8_output()
    parser = argparse.ArgumentParser(prog="unifi-monitor", description=__doc__)
    parser.add_argument("--config", help="path to JSON config overlay")
    parser.add_argument("--db", help="path to the monitoring database")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--log-level", default=None)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("check", help="[part 1] verify controller connectivity and config")
    sub.add_parser("poll", help="[part 1] run exactly one poll cycle (cron mode)")
    sub.add_parser("run", help="[part 1] run the polling loop forever (service mode)")
    sub.add_parser("prune", help="[part 1] apply retention policy now")
    sub.add_parser("status", help="[part 2] current network overview")

    p_issues = sub.add_parser("issues", help="[part 2] list flagged issues")
    p_issues.add_argument("--status", default="open", choices=["open", "resolved", "acked", "any"])
    p_issues.add_argument("--severity", choices=["info", "warning", "critical"])
    p_issues.add_argument("--type", dest="issue_type")
    p_issues.add_argument("--entity")
    p_issues.add_argument("--hours", type=int)
    p_issues.add_argument("--limit", type=int, default=25)

    p_critical = sub.add_parser(
        "critical", help="[part 2] criticals opened since a watermark (proactive hook)"
    )
    p_critical.add_argument(
        "--minutes", type=int, default=30, help="window used when there is no watermark yet"
    )
    p_critical.add_argument(
        "--since-file", help="watermark file; advanced past whatever is reported"
    )

    p_explain = sub.add_parser("explain", help="[part 2] full evidence package for an issue")
    p_explain.add_argument("issue_id", type=int)

    p_ask = sub.add_parser("ask", help="[part 2] explain by name, e.g. ask 'cow cam'")
    p_ask.add_argument("entity")
    p_ask.add_argument("--hours", type=int, default=168)

    p_history = sub.add_parser("history", help="[part 2] history for one device/client")
    p_history.add_argument("entity")
    p_history.add_argument("--hours", type=int, default=168)

    p_patterns = sub.add_parser("patterns", help="[part 2] recurrence statistics")
    p_patterns.add_argument("--entity")
    p_patterns.add_argument("--type", dest="issue_type")
    p_patterns.add_argument("--days", type=int, default=30)

    p_actions = sub.add_parser("actions", help="[part 2] propose remediation for an issue")
    p_actions.add_argument("issue_id", type=int)
    p_actions.add_argument(
        "--record", action="store_true", help="persist proposals and mint confirmation tokens"
    )

    p_confirm = sub.add_parser("confirm", help="[part 2] confirm a proposed action (still blocked)")
    p_confirm.add_argument("proposal_id")
    p_confirm.add_argument("token")

    p_reject = sub.add_parser("reject", help="[part 2] reject a proposed action")
    p_reject.add_argument("proposal_id")
    p_reject.add_argument("--note", default="")

    sub.add_parser("mcp", help="[part 2] serve the MCP tool interface on stdio")

    args = parser.parse_args(argv)
    handler = HANDLERS[args.command]
    return handler(args)


# ------------------------------------------------------------------- part 1


def _load(args: argparse.Namespace):
    from .config import load_config, validate

    cfg = load_config(args.config)
    if args.db:
        cfg.db_path = args.db
    if args.log_level:
        cfg.log_level = args.log_level
    setup_logging(cfg.log_level, cfg.log_file)
    return cfg, validate(cfg)


def cmd_check(args: argparse.Namespace) -> int:
    from .unifi_client import UniFiClient, UniFiError

    cfg, problems = _load(args)
    report: dict[str, Any] = {
        "host": cfg.controller.host,
        "site": cfg.controller.site,
        "controller_type": cfg.controller.controller_type,
        "verify_ssl": cfg.controller.verify_ssl,
        "db_path": cfg.db_path,
        "poll_interval_s": cfg.poll_interval_s,
        "alert_channels": cfg.alerts.channels,
        "config_problems": problems,
    }
    if problems:
        _emit(args, report, _lines(report) + ["", "FAILED: fix the problems above."])
        return 2

    client = UniFiClient(cfg.controller)
    try:
        client.login()
        sysinfo = client.sysinfo()
        devices = client.devices()
        clients = client.active_clients()
        health = client.health()
        report.update(
            {
                "connected": True,
                "controller_version": sysinfo.get("version"),
                "console_version": sysinfo.get("consoleVersion") or sysinfo.get("ubnt_device_type"),
                "devices": len(devices),
                "clients": len(clients),
                "health_subsystems": [h.get("subsystem") for h in health],
                "device_names": [d.get("name") or d.get("mac") for d in devices][:20],
            }
        )
    except UniFiError as exc:
        report.update({"connected": False, "error": str(exc)})
        _emit(args, report, _lines(report))
        return 1
    finally:
        client.logout()

    _emit(args, report, _lines(report) + ["", "OK: controller reachable."])
    return 0


def cmd_poll(args: argparse.Namespace) -> int:
    from .poller import Poller

    cfg, problems = _load(args)
    if problems:
        _emit(args, {"config_problems": problems}, [f"config error: {p}" for p in problems])
        return 2
    poller = Poller(cfg)
    try:
        summary = poller.poll_once()
    finally:
        poller.close()
    _emit(args, summary, _lines(summary))
    return 0 if summary.get("ok") else 1


def cmd_run(args: argparse.Namespace) -> int:
    from .poller import Poller

    cfg, problems = _load(args)
    if problems:
        for problem in problems:
            print(f"config error: {problem}", file=sys.stderr)
        return 2
    poller = Poller(cfg)
    try:
        poller.run_forever()
    finally:
        poller.close()
    return 0


def cmd_prune(args: argparse.Namespace) -> int:
    from .db import Database

    cfg, _ = _load(args)
    with Database(cfg.db_path) as db:
        deleted = db.prune(
            metrics_days=cfg.retain_metrics_days,
            transitions_days=cfg.retain_transitions_days,
            events_days=cfg.retain_events_days,
            issues_days=cfg.retain_issues_days,
        )
    _emit(args, deleted, [f"{table}: {count} row(s) deleted" for table, count in deleted.items()])
    return 0


# ------------------------------------------------------------------- part 2


def cmd_status(args: argparse.Namespace) -> int:
    from .query import network_overview

    data = network_overview(db_path=args.db)
    lines = [
        f"devices  : {data['devices']['online']}/{data['devices']['total']} online",
        f"clients  : {data['clients_online']} online",
        f"open     : {data['open_issues']['critical']} critical, "
        f"{data['open_issues']['warning']} warning, {data['open_issues']['info']} info",
    ]
    for wan in data["wan"]:
        lines.append(f"wan      : {wan['name']} {wan['state']}")
    for device in data["devices"]["offline"]:
        lines.append(
            f"  OFFLINE {device['name']} ({humanize_duration(device['offline_for_s'])})"
        )
    poller = data["poller"]
    lines.append(
        f"poller   : last run {humanize_duration(poller['seconds_since_last_run'])} ago, "
        f"ok={poller['last_run_ok']}"
        + (" [STALLED]" if poller.get("possibly_stalled") else "")
    )
    _emit(args, data, lines)
    return 0


def cmd_issues(args: argparse.Namespace) -> int:
    from .query import recent_issues

    rows = recent_issues(
        status=args.status,
        severity=args.severity,
        issue_type=args.issue_type,
        entity=args.entity,
        hours=args.hours,
        limit=args.limit,
        db_path=args.db,
    )
    lines = [
        f"#{r['id']:<5} {r['severity']:<8} {r['issue_type']:<22} {r['summary']} "
        f"({r['duration_human']}, {r['occurrences']}x)"
        for r in rows
    ] or ["no issues matched"]
    _emit(args, rows, lines)
    return 0


def cmd_critical(args: argparse.Namespace) -> int:
    """Critical issues opened since a watermark — the proactive hook.

    Meant to be run on a short interval by something that notifies. Prints
    nothing when there is nothing new, so a scheduler can treat any output as
    "say something".
    """
    from .query import new_critical_issues

    since = _read_watermark(args.since_file)
    rows = new_critical_issues(since_ts=since, minutes=args.minutes, db_path=args.db)

    if rows and args.since_file:
        # Advance only past what was actually reported, and only when there was
        # something to report: a quiet hour must not skip an issue that lands
        # between the query and the write.
        _write_watermark(args.since_file, max(int(r["first_seen"]) for r in rows) + 1)

    lines = [
        f"{r['severity'].upper()}: {r['summary']} (issue #{r['id']}, {r['entity_name'] or r['entity_id']})"
        for r in rows
    ]
    if args.json:
        _emit(args, rows, lines)
    elif lines:
        print("\n".join(lines))
    return 0


def _read_watermark(path: str | None) -> int | None:
    if not path or not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            return int(fh.read().strip())
    except (ValueError, OSError):
        # A corrupt watermark must not wedge the alert path — fall back to the
        # --minutes window rather than exiting.
        return None


def _write_watermark(path: str, value: int) -> None:
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(str(value))
    os.replace(tmp, path)  # atomic: a crash mid-write cannot truncate the mark


def cmd_explain(args: argparse.Namespace) -> int:
    from .query import explain_issue, summarize_for_llm

    report = explain_issue(args.issue_id, db_path=args.db)
    _emit(args, report, [summarize_for_llm(report)])
    return 0 if "error" not in report else 1


def cmd_ask(args: argparse.Namespace) -> int:
    from .query import explain_entity

    report = explain_entity(args.entity, hours=args.hours, db_path=args.db)
    if "error" in report:
        _emit(args, report, [report["error"]])
        return 1
    lines = [f"resolved '{args.entity}' -> {report['resolved_to']['name']}", ""]
    lines += [f"- {fact}" for fact in report.get("facts", [])]
    _emit(args, report, lines)
    return 0


def cmd_history(args: argparse.Namespace) -> int:
    from .query import device_history

    report = device_history(args.entity, hours=args.hours, db_path=args.db)
    if "error" in report:
        _emit(args, report, [report["error"]])
        return 1
    availability = report["availability"]
    lines = [
        f"{report['entity']['name']} ({report['entity']['entity_type']}) "
        f"currently {report['entity']['state']} for {report['entity']['state_for']}",
        f"last {args.hours}h: {availability['disconnects']} disconnect(s), "
        f"{availability['uptime_pct']}% up",
        "",
        "recent transitions:",
    ]
    lines += [
        f"  {t['at']}  {t['from'] or '?'} -> {t['to']}"
        for t in report["state_transitions"][:20]
    ]
    _emit(args, report, lines)
    return 0


def cmd_patterns(args: argparse.Namespace) -> int:
    from .query import issue_frequency

    report = issue_frequency(
        entity=args.entity, issue_type=args.issue_type, days=args.days, db_path=args.db
    )
    lines = [
        f"{report['total']} issue(s) in {report['window_days']}d "
        f"({report['per_day_average']}/day), median gap "
        f"{report['median_interval_hours']}h",
        "",
        "by type:",
    ]
    lines += [f"  {name:<24} {count}" for name, count in report["by_issue_type"].items()]
    lines += ["", "by entity:"]
    lines += [f"  {name:<24} {count}" for name, count in report["by_entity"].items()]
    _emit(args, report, lines)
    return 0


def cmd_actions(args: argparse.Namespace) -> int:
    from .query import explain_issue
    from .remediation import propose_actions, record_proposals

    report = explain_issue(args.issue_id, db_path=args.db)
    if "error" in report:
        _emit(args, report, [report["error"]])
        return 1
    actions = report["proposed_actions"]
    if args.record:
        actions = record_proposals(report["issue"], propose_actions(report["issue"], report))

    lines = ["PROPOSED ACTIONS — none of these have been executed.", ""]
    for action in actions:
        lines.append(f"  {action.get('id', action['action_key'])}: {action['title']}")
        lines.append(f"    why  : {action['rationale']}")
        lines.append(f"    risk : {action['risk']}  permission: {action['requires_permission']}")
        if action.get("controller_call"):
            call = action["controller_call"]
            lines.append(f"    call : {call['method']} {call['path']} {json.dumps(call['body'])}")
        for step in action.get("manual_steps") or []:
            lines.append(f"    step : {step}")
        if action.get("confirmation_token"):
            lines.append(
                f"    confirm: unifi-monitor confirm {action['id']} {action['confirmation_token']}"
            )
        lines.append("")
    _emit(args, actions, lines)
    return 0


def cmd_confirm(args: argparse.Namespace) -> int:
    from .remediation import confirm_and_execute

    result = confirm_and_execute(args.proposal_id, args.token)
    lines = [result.get("message") or f"result: {result.get('reason') or 'executed'}"]
    if result.get("would_call"):
        lines.append(f"would have called: {json.dumps(result['would_call'])}")
    for step in result.get("manual_steps") or []:
        lines.append(f"manual step: {step}")
    _emit(args, result, lines)
    return 0 if result.get("executed") or result.get("reason") == "manual_action_only" else 3


def cmd_reject(args: argparse.Namespace) -> int:
    from .remediation import reject_proposal

    result = reject_proposal(args.proposal_id, note=args.note)
    _emit(args, result, [f"{args.proposal_id} rejected"])
    return 0


def cmd_mcp(_args: argparse.Namespace) -> int:
    from .mcp_server import serve_stdio

    serve_stdio()
    return 0


HANDLERS = {
    "check": cmd_check,
    "poll": cmd_poll,
    "run": cmd_run,
    "prune": cmd_prune,
    "status": cmd_status,
    "issues": cmd_issues,
    "critical": cmd_critical,
    "explain": cmd_explain,
    "ask": cmd_ask,
    "history": cmd_history,
    "patterns": cmd_patterns,
    "actions": cmd_actions,
    "confirm": cmd_confirm,
    "reject": cmd_reject,
    "mcp": cmd_mcp,
}


def _emit(args: argparse.Namespace, data: Any, lines: list[str]) -> None:
    if getattr(args, "json", False):
        print(json.dumps(data, indent=2, default=str))
    else:
        print("\n".join(lines))


def _lines(data: dict[str, Any]) -> list[str]:
    return [f"{key:<20} {value}" for key, value in data.items()]


if __name__ == "__main__":
    raise SystemExit(main())
