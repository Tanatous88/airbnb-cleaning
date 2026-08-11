"""Part 2 as an MCP server (stdio, JSON-RPC 2.0, zero dependencies).

Point the OpenClaw gateway — or any MCP client — at:

    python -m unifi_monitor.cli mcp

Every tool is read-only against the monitoring database except the two
action tools, and those only ever *propose* or *record a decision*. The one
that sounds like it executes (``unifi_confirm_action``) still refuses while
the controller account is view-only, and reports exactly which permission
would be required.

If you would rather call this in-process, skip MCP entirely and import
``unifi_monitor.query`` — the tool handlers below are thin wrappers over it.
"""

from __future__ import annotations

import json
import sys
from typing import Any, Callable

from . import query, remediation
from .util import LOG

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "unifi-monitor", "version": "1.0.0"}

_DB_PATH_PROP = {
    "db_path": {
        "type": "string",
        "description": "Override the monitoring database path (defaults to UNIFI_DB_PATH).",
    }
}


def _schema(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {**properties, **_DB_PATH_PROP},
        "required": required or [],
        "additionalProperties": False,
    }


# ------------------------------------------------------------------- handlers


def _tool_network_status(args: dict[str, Any]) -> Any:
    return query.network_overview(db_path=args.get("db_path"))


def _tool_recent_issues(args: dict[str, Any]) -> Any:
    return query.recent_issues(
        status=args.get("status", "open"),
        severity=args.get("severity"),
        issue_type=args.get("issue_type"),
        entity=args.get("entity"),
        hours=args.get("hours"),
        limit=int(args.get("limit", 25)),
        db_path=args.get("db_path"),
    )


def _tool_explain_issue(args: dict[str, Any]) -> Any:
    report = query.explain_issue(int(args["issue_id"]), db_path=args.get("db_path"))
    if args.get("format") == "text":
        return query.summarize_for_llm(report)
    return report


def _tool_investigate_device(args: dict[str, Any]) -> Any:
    return query.explain_entity(
        str(args["entity"]), hours=int(args.get("hours", 168)), db_path=args.get("db_path")
    )


def _tool_device_history(args: dict[str, Any]) -> Any:
    return query.device_history(
        str(args["entity"]), hours=int(args.get("hours", 168)), db_path=args.get("db_path")
    )


def _tool_issue_patterns(args: dict[str, Any]) -> Any:
    return query.issue_frequency(
        entity=args.get("entity"),
        issue_type=args.get("issue_type"),
        days=int(args.get("days", 30)),
        db_path=args.get("db_path"),
    )


def _tool_new_critical(args: dict[str, Any]) -> Any:
    return query.new_critical_issues(
        since_ts=args.get("since_ts"),
        minutes=int(args.get("minutes", 30)),
        db_path=args.get("db_path"),
    )


def _tool_propose_remediation(args: dict[str, Any]) -> Any:
    """Persist proposals and hand back confirmation tokens.

    The tokens exist so that a later ``unifi_confirm_action`` call cannot be
    synthesised from the issue id alone — the user has to have been shown the
    proposal first.
    """
    report = query.explain_issue(int(args["issue_id"]), db_path=args.get("db_path"))
    if "error" in report:
        return report
    actions = remediation.record_proposals(
        report["issue"], remediation.propose_actions(report["issue"], report)
    )
    return {
        "issue": report["issue"],
        "proposed_actions": actions,
        "notice": (
            "These are proposals only. Nothing has been executed. Present them to the "
            "user and call unifi_confirm_action with the proposal id and its token "
            "only after the user explicitly picks one."
        ),
    }


def _tool_confirm_action(args: dict[str, Any]) -> Any:
    return remediation.confirm_and_execute(
        str(args["proposal_id"]),
        str(args["confirmation_token"]),
        actor=str(args.get("actor") or "user"),
    )


def _tool_reject_action(args: dict[str, Any]) -> Any:
    return remediation.reject_proposal(
        str(args["proposal_id"]), note=str(args.get("note") or ""), actor=str(args.get("actor") or "user")
    )


def _tool_list_proposals(args: dict[str, Any]) -> Any:
    return remediation.list_proposals(
        issue_id=args.get("issue_id"), status=args.get("status"), limit=int(args.get("limit", 25))
    )


TOOLS: list[dict[str, Any]] = [
    {
        "name": "unifi_network_status",
        "description": (
            "Current network health: devices online/offline, WAN state, open issue counts, "
            "and whether the poller itself is running. Start here for 'is anything wrong'."
        ),
        "inputSchema": _schema({}),
        "handler": _tool_network_status,
    },
    {
        "name": "unifi_recent_issues",
        "description": (
            "List flagged issues from the monitoring database. Filter by status "
            "(open/resolved/any), severity, issue type, entity name or MAC, and age."
        ),
        "inputSchema": _schema(
            {
                "status": {"type": "string", "enum": ["open", "resolved", "acked", "any"]},
                "severity": {"type": "string", "enum": ["info", "warning", "critical"]},
                "issue_type": {"type": "string"},
                "entity": {"type": "string", "description": "MAC or name fragment"},
                "hours": {"type": "integer"},
                "limit": {"type": "integer"},
            }
        ),
        "handler": _tool_recent_issues,
    },
    {
        "name": "unifi_explain_issue",
        "description": (
            "Full evidence package for one flagged issue: what changed and when, the "
            "timeline, how often it has recurred, how metrics compare to the entity's own "
            "7-day baseline, what else on the network moved in the same window, and whether "
            "the poller had full coverage. Facts only — reason over them to explain the "
            "likely root cause."
        ),
        "inputSchema": _schema(
            {
                "issue_id": {"type": "integer"},
                "format": {"type": "string", "enum": ["json", "text"]},
            },
            ["issue_id"],
        ),
        "handler": _tool_explain_issue,
    },
    {
        "name": "unifi_investigate_device",
        "description": (
            "Answer a question phrased about a thing rather than an issue id — "
            "'why does the cow cam keep dropping'. Resolves the name to a monitored "
            "entity, then returns its history, recurrence pattern, and its most recent "
            "issue fully explained."
        ),
        "inputSchema": _schema(
            {
                "entity": {"type": "string", "description": "Device or client name, or MAC"},
                "hours": {"type": "integer", "description": "Lookback window, default 168"},
            },
            ["entity"],
        ),
        "handler": _tool_investigate_device,
    },
    {
        "name": "unifi_device_history",
        "description": (
            "Raw history for one device/client/port/WAN: state transitions, issues, "
            "controller events and metric summaries over a window."
        ),
        "inputSchema": _schema(
            {"entity": {"type": "string"}, "hours": {"type": "integer"}}, ["entity"]
        ),
        "handler": _tool_device_history,
    },
    {
        "name": "unifi_issue_patterns",
        "description": (
            "Frequency analysis: how often issues fire, by day, by hour of day, by type "
            "and by entity. Use to answer 'is this getting worse' or 'does it always "
            "happen at night'."
        ),
        "inputSchema": _schema(
            {
                "entity": {"type": "string"},
                "issue_type": {"type": "string"},
                "days": {"type": "integer"},
            }
        ),
        "handler": _tool_issue_patterns,
    },
    {
        "name": "unifi_new_critical_issues",
        "description": (
            "Critical issues opened since a watermark. Poll this to decide whether to "
            "raise something with the user unprompted."
        ),
        "inputSchema": _schema(
            {"since_ts": {"type": "integer"}, "minutes": {"type": "integer"}}
        ),
        "handler": _tool_new_critical,
    },
    {
        "name": "unifi_propose_remediation",
        "description": (
            "Propose remediation for an issue. Returns concrete actions, each with the "
            "controller call it would make, its risk, the permission it needs, and a "
            "one-time confirmation token. NOTHING IS EXECUTED. Show the options to the "
            "user and let them choose."
        ),
        "inputSchema": _schema({"issue_id": {"type": "integer"}}, ["issue_id"]),
        "handler": _tool_propose_remediation,
    },
    {
        "name": "unifi_confirm_action",
        "description": (
            "Execute a proposed action the user has explicitly confirmed, quoting its "
            "proposal id and token. Only call this after the user has chosen a specific "
            "action in this conversation — never on your own initiative. With the current "
            "view-only account this refuses and reports the permission upgrade required."
        ),
        "inputSchema": _schema(
            {
                "proposal_id": {"type": "string"},
                "confirmation_token": {"type": "string"},
                "actor": {"type": "string"},
            },
            ["proposal_id", "confirmation_token"],
        ),
        "handler": _tool_confirm_action,
    },
    {
        "name": "unifi_reject_action",
        "description": "Mark a proposed action as rejected, so it is not offered again.",
        "inputSchema": _schema(
            {"proposal_id": {"type": "string"}, "note": {"type": "string"}, "actor": {"type": "string"}},
            ["proposal_id"],
        ),
        "handler": _tool_reject_action,
    },
    {
        "name": "unifi_list_proposals",
        "description": "List previously proposed actions and what was decided about them.",
        "inputSchema": _schema(
            {
                "issue_id": {"type": "integer"},
                "status": {"type": "string", "enum": ["proposed", "executed", "rejected", "manual"]},
                "limit": {"type": "integer"},
            }
        ),
        "handler": _tool_list_proposals,
    },
]

HANDLERS: dict[str, Callable[[dict[str, Any]], Any]] = {
    tool["name"]: tool["handler"] for tool in TOOLS
}
TOOL_SPECS = [{k: v for k, v in tool.items() if k != "handler"} for tool in TOOLS]


# -------------------------------------------------------------- MCP plumbing


def handle_message(message: dict[str, Any]) -> dict[str, Any] | None:
    """Dispatch one JSON-RPC message. Returns None for notifications."""
    method = message.get("method")
    msg_id = message.get("id")
    params = message.get("params") or {}

    if method == "initialize":
        return _ok(
            msg_id,
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": SERVER_INFO,
            },
        )
    if method in ("notifications/initialized", "initialized", "notifications/cancelled"):
        return None
    if method == "ping":
        return _ok(msg_id, {})
    if method == "tools/list":
        return _ok(msg_id, {"tools": TOOL_SPECS})
    if method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments") or {}
        handler = HANDLERS.get(name)
        if handler is None:
            return _error(msg_id, -32601, f"unknown tool: {name}")
        try:
            result = handler(arguments)
        except KeyError as exc:
            return _ok(msg_id, _content(f"missing required argument: {exc}"), is_error=True)
        except FileNotFoundError as exc:
            return _ok(
                msg_id,
                _content(
                    f"monitoring database not found ({exc}). The poller has not run yet, or "
                    "UNIFI_DB_PATH points somewhere else."
                ),
                is_error=True,
            )
        except Exception as exc:  # noqa: BLE001 - report, never kill the server
            LOG.exception("tool %s failed", name)
            return _ok(msg_id, _content(f"{type(exc).__name__}: {exc}"), is_error=True)
        text = result if isinstance(result, str) else json.dumps(result, indent=2, default=str)
        return _ok(msg_id, _content(text))

    if msg_id is None:
        return None
    return _error(msg_id, -32601, f"method not found: {method}")


def _content(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}]}


def _ok(msg_id: Any, result: dict[str, Any], is_error: bool = False) -> dict[str, Any]:
    if is_error:
        result = {**result, "isError": True}
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _error(msg_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def serve_stdio(stdin: Any = None, stdout: Any = None) -> None:
    """Newline-delimited JSON-RPC over stdio, per the MCP stdio transport."""
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    LOG.info("unifi-monitor MCP server ready on stdio")
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            _write(stdout, _error(None, -32700, "parse error"))
            continue
        response = handle_message(message)
        if response is not None:
            _write(stdout, response)


def _write(stream: Any, payload: dict[str, Any]) -> None:
    stream.write(json.dumps(payload, default=str) + "\n")
    stream.flush()


if __name__ == "__main__":
    serve_stdio()
