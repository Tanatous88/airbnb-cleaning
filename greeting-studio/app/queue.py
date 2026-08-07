"""Day-of-arrival draft queue.

A scheduled morning job (7:00 AM Pacific) drafts a welcome message for every
stay checking in today, fully unattended. The only human gate is the Send
button on the dashboard. Guardrails:
  - a draft with an unresolved merge field can never reach 'ready'
  - every send attempt is logged to the DB AND an append-only flat file
  - failures are visible and retryable, never silent
"""
import json
import os
import re
import subprocess
from datetime import date, datetime, timezone

from . import db
from .claude import ClaudeError, ask_claude, render_prompt

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AUDIT_LOG_PATH = os.path.join(BASE_DIR, "send_audit.log")

MERGE_FIELD_RE = re.compile(r"\{\{\s*([a-zA-Z0-9_]+)\s*\}\}")


# ---------------------------------------------------------------------------
# Merge values + local rendering
# ---------------------------------------------------------------------------

def build_merge_values(conn, unit, stay) -> dict:
    amenities = json.loads(unit["amenities"] or "{}")
    first_name = stay["guest_name"].split()[0] if stay["guest_name"].strip() else "[MISSING]"

    def pretty_date(iso):
        try:
            d = date.fromisoformat(iso)
            # %-d is not supported on Windows; format the day number manually
            return f"{d.strftime('%A, %B')} {d.day}"
        except ValueError:
            return iso

    return {
        "guest_first_name": first_name,
        "checkin_date": pretty_date(stay["checkin_date"]),
        "checkout_date": pretty_date(stay["checkout_date"]),
        "checkin_time": amenities.get("checkin_time")
                        or db.get_setting(conn, "default_checkin_time", "4:00 PM"),
        "checkout_time": amenities.get("checkout_time")
                         or db.get_setting(conn, "default_checkout_time", "11:00 AM"),
        "door_code": amenities.get("door_code") or "[MISSING]",
        "wifi_network": amenities.get("wifi_network") or "[MISSING]",
        "wifi_password": amenities.get("wifi_password") or "[MISSING]",
    }


def render_local(template: str, merge_values: dict) -> str:
    """Deterministic merge-field substitution — used when there's nothing to
    personalize (no booking message), or as fallback if the Claude call fails."""
    def sub(m):
        return str(merge_values.get(m.group(1), m.group(0)))
    return MERGE_FIELD_RE.sub(sub, template)


def missing_fields(template: str, merge_values: dict) -> list:
    """Merge fields the template actually uses that resolve to [MISSING]."""
    used = set(MERGE_FIELD_RE.findall(template))
    return sorted(f for f in used if merge_values.get(f) == "[MISSING]")


# ---------------------------------------------------------------------------
# Audit log (append-only flat file, separate from the DB)
# ---------------------------------------------------------------------------

def audit(event: str, unit_name: str, guest_name: str, detail: str = "") -> None:
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    detail = " ".join(str(detail).split())[:400]  # one line per event, always
    line = f"{stamp}\t{event}\t{unit_name}\t{guest_name}\t{detail}\n"
    with open(AUDIT_LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line)


# ---------------------------------------------------------------------------
# Queue building
# ---------------------------------------------------------------------------

def _personalize(conn, unit, stay, template: str, merge_values: dict, thread_context: str):
    """Returns (message, note). Uses Claude only when there is guest context to
    respond to; otherwise a deterministic local render."""
    if not thread_context.strip():
        return render_local(template, merge_values), ""
    model = db.get_setting(conn, "anthropic_model", "claude-sonnet-4-6")
    prompt = render_prompt(
        "draft_greeting",
        unit_name=unit["name"],
        unit_profile=unit["profile"] or "(no notes)",
        template=template,
        guest_name=stay["guest_name"],
        checkin_date=merge_values["checkin_date"],
        checkout_date=merge_values["checkout_date"],
        merge_values=json.dumps(merge_values, indent=2),
        booking_message=thread_context,
        host_signature=db.get_setting(conn, "host_signature", "— Your hosts"),
    )
    try:
        return ask_claude(prompt, model=model), ""
    except ClaudeError as e:
        # Fail soft at draft time: an un-personalized greeting is still correct.
        return render_local(template, merge_values), f"personalization skipped ({e})"


def _read_thread_context(stay) -> str:
    """Best-effort: read the guest's messages from the Airbnb thread via the
    persistent Playwright profile (headless). Falls back to the stay's stored
    booking message on any failure — Airbnb markup changes often."""
    context = (stay["booking_message"] or "").strip()
    code = (stay["confirmation_code"] or "").strip()
    if not code:
        return context
    try:
        from .browser_assist import read_thread_messages
        scraped = read_thread_messages(code)
        if scraped:
            context = (context + "\n" if context else "") + "\n".join(scraped)
    except Exception:
        pass  # fall back to stored booking message
    return context


def build_queue_for_today(force: bool = False) -> dict:
    """Draft a welcome message for every stay checking in today. Idempotent:
    stays already queued today are skipped unless force=True."""
    today = date.today().isoformat()
    conn = db.connect()
    summary = {"date": today, "queued": 0, "skipped": 0, "needs_setup": 0, "errors": []}
    try:
        stays = conn.execute(
            "SELECT * FROM stays WHERE checkin_date = ? AND status != 'sent'", (today,)
        ).fetchall()
        for stay in stays:
            existing = conn.execute(
                "SELECT id, status FROM daily_queue WHERE stay_id = ? AND checkin_date = ?",
                (stay["id"], today)).fetchone()
            if existing and not force:
                summary["skipped"] += 1
                continue
            if existing and existing["status"] == "sent":
                summary["skipped"] += 1
                continue

            unit = conn.execute("SELECT * FROM units WHERE id = ?", (stay["unit_id"],)).fetchone()
            tpl = db.latest_template(conn, unit["id"])
            merge_values = build_merge_values(conn, unit, stay) if unit else {}

            if tpl is None:
                status, message, error = "needs_setup", "", f"{unit['name']} has no template yet."
            else:
                missing = missing_fields(tpl["content"], merge_values)
                if missing:
                    status, message = "needs_setup", ""
                    error = ("Missing amenity value(s): " + ", ".join(missing) +
                             " — fill them in on the unit's Amenities form, then Rebuild.")
                else:
                    thread_context = _read_thread_context(stay)
                    message, note = _personalize(conn, unit, stay, tpl["content"], merge_values,
                                                 thread_context)
                    status, error = "ready", note

            if existing:
                conn.execute(
                    "UPDATE daily_queue SET message=?, status=?, error=?, thread_ref=?, created_at=? "
                    "WHERE id=?",
                    (message, status, error, stay["confirmation_code"] or "", db.now(),
                     existing["id"]))
            else:
                conn.execute(
                    "INSERT INTO daily_queue (stay_id, unit_id, guest_name, checkin_date, message, "
                    "status, error, thread_ref, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (stay["id"], stay["unit_id"], stay["guest_name"], today, message, status,
                     error, stay["confirmation_code"] or "", db.now()))
            summary["queued"] += 1
            if status == "needs_setup":
                summary["needs_setup"] += 1
            audit("draft", unit["name"] if unit else "?", stay["guest_name"], status)
        conn.commit()
        db.set_setting(conn, "queue_last_run", db.now())
        db.set_setting(conn, "queue_last_summary", json.dumps(summary))
        conn.commit()
    finally:
        conn.close()
    _notify(summary)
    return summary


def _notify(summary: dict) -> None:
    """Desktop notification (macOS) + the dashboard badge reads queue_last_summary."""
    if summary["queued"] == 0:
        return
    text = f"{summary['queued']} arrival(s) queued for today"
    if summary["needs_setup"]:
        text += f" — {summary['needs_setup']} need setup"
    try:
        subprocess.run(
            ["osascript", "-e",
             f'display notification "{text}" with title "Greeting Studio"'],
            timeout=10, capture_output=True)
    except Exception:
        pass  # non-macOS or no osascript — the dashboard badge still shows it
