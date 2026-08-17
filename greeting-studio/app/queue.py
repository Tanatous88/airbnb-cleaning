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

import urllib.request

from . import db, parsers
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
    # Per-stay door code: smart locks on several units use the last 4 digits of
    # the guest's phone number, which the Airbnb calendar feed provides.
    try:
        phone_last4 = (stay["phone_last4"] or "").strip()
    except (KeyError, IndexError):
        phone_last4 = ""

    def pretty_date(iso):
        try:
            d = date.fromisoformat(iso)
            # %-d is not supported on Windows; format the day number manually
            return f"{d.strftime('%A, %B')} {d.day}"
        except ValueError:
            return iso

    if first_name.lower() in ("guest", "reserved"):  # calendar sync couldn't get a name
        first_name = "[MISSING]"
    return {
        "guest_first_name": first_name,
        "checkin_date": pretty_date(stay["checkin_date"]),
        "checkout_date": pretty_date(stay["checkout_date"]),
        "checkin_time": amenities.get("checkin_time")
                        or db.get_setting(conn, "default_checkin_time", "4:00 PM"),
        "checkout_time": amenities.get("checkout_time")
                         or db.get_setting(conn, "default_checkout_time", "11:00 AM"),
        "door_code": phone_last4 or amenities.get("door_code") or "[MISSING]",
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
# Calendar auto-sync (runs before every queue build)
# ---------------------------------------------------------------------------

UNKNOWN_GUEST = "Guest (see Airbnb)"
SYNC_LOOKBACK_DAYS = 45  # keep recent past stays so the review generator has them


def _fetch(url: str) -> str:
    with urllib.request.urlopen(url, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _upsert_stay(conn, unit_id: int, s: dict) -> str:
    """Insert or update a stay, matching by confirmation code first (stable
    across guest-name/date corrections), else unit+checkin+guest."""
    code = (s.get("confirmation_code") or "").strip()
    row = None
    if code:
        row = conn.execute("SELECT * FROM stays WHERE confirmation_code = ?", (code,)).fetchone()
    if not row:
        row = conn.execute(
            "SELECT * FROM stays WHERE unit_id = ? AND checkin_date = ? AND guest_name = ?",
            (unit_id, s["checkin_date"], s["guest_name"])).fetchone()
    if not row:
        # Absorb name/no-name pairs for the same unit+check-in: a nameless
        # raw-feed stay matches an incoming named entry, and vice versa.
        if s["guest_name"] == UNKNOWN_GUEST:
            row = conn.execute(
                "SELECT * FROM stays WHERE unit_id = ? AND checkin_date = ?",
                (unit_id, s["checkin_date"])).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM stays WHERE unit_id = ? AND checkin_date = ? AND guest_name = ?",
                (unit_id, s["checkin_date"], UNKNOWN_GUEST)).fetchone()
    if row:
        updates, values = [], []
        if s["guest_name"] != UNKNOWN_GUEST and s["guest_name"] != row["guest_name"]:
            updates.append("guest_name = ?"); values.append(s["guest_name"])
        for col in ("checkin_date", "checkout_date"):
            if s[col] != row[col]:
                updates.append(f"{col} = ?"); values.append(s[col])
        if code and not row["confirmation_code"]:
            updates.append("confirmation_code = ?"); values.append(code)
        if s.get("phone_last4") and not (row["phone_last4"] or "").strip():
            updates.append("phone_last4 = ?"); values.append(s["phone_last4"])
        if s.get("booking_message") and not (row["booking_message"] or "").strip():
            updates.append("booking_message = ?"); values.append(s["booking_message"])
        if updates:
            values.append(row["id"])
            conn.execute(f"UPDATE stays SET {', '.join(updates)} WHERE id = ?", values)
            return "updated"
        return "unchanged"
    conn.execute(
        "INSERT INTO stays (unit_id, guest_name, checkin_date, checkout_date, booking_message, "
        "confirmation_code, phone_last4, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (unit_id, s["guest_name"], s["checkin_date"], s["checkout_date"],
         s.get("booking_message", ""), code, s.get("phone_last4", ""), db.now()))
    return "added"


def sync_calendars() -> dict:
    """Pull bookings from (1) each unit's Airbnb iCal feed (dates + confirmation
    codes; Airbnb omits guest names) and (2) the enriched 'Hosting Schedules'
    feed if configured (guest names, party size), merged by confirmation code."""
    from datetime import timedelta
    cutoff = (date.today() - timedelta(days=SYNC_LOOKBACK_DAYS)).isoformat()
    summary = {"added": 0, "updated": 0, "unchanged": 0, "errors": []}
    conn = db.connect()
    try:
        units = conn.execute("SELECT * FROM units").fetchall()
        for unit in units:
            url = (unit["ics_url"] or "").strip()
            if not url:
                continue
            try:
                stays = parsers.parse_ics(_fetch(url))
            except Exception as e:
                summary["errors"].append(f"{unit['name']} feed: {e}")
                continue
            for s in stays:
                if s["checkin_date"] < cutoff:
                    continue
                if not s["guest_name"] or s["guest_name"].lower().startswith("reserved"):
                    s["guest_name"] = UNKNOWN_GUEST
                summary[_upsert_stay(conn, unit["id"], s)] += 1

        hosting_url = db.get_setting(conn, "hosting_ics_url", "").strip()
        if hosting_url:
            try:
                aliases = json.loads(db.get_setting(conn, "listing_aliases", "{}"))
            except json.JSONDecodeError:
                aliases = {}
            unit_by_name = {u["name"]: u["id"] for u in units}
            try:
                entries = parsers.parse_hosting_ics(_fetch(hosting_url))
            except Exception as e:
                entries = []
                summary["errors"].append(f"Hosting Schedules feed: {e}")
            for s in entries:
                if s["checkin_date"] < cutoff:
                    continue
                listing = (s.get("listing") or "").lower()
                unit_id = None
                for needle, unit_name in aliases.items():
                    if needle.lower() in listing and unit_name in unit_by_name:
                        unit_id = unit_by_name[unit_name]
                        break
                if unit_id is None:
                    summary["errors"].append(
                        f"No unit alias matches listing {s.get('listing', '?')!r} "
                        f"(guest {s['guest_name']}) — add it to listing_aliases in Settings.")
                    continue
                if not s["guest_name"]:
                    s["guest_name"] = UNKNOWN_GUEST
                if s.get("party"):
                    s["booking_message"] = f"(from calendar) Party: {s['party']}"
                summary[_upsert_stay(conn, unit_id, s)] += 1
        conn.commit()
        db.set_setting(conn, "calendar_last_sync", db.now())
        db.set_setting(conn, "calendar_last_sync_summary", json.dumps(summary))
        conn.commit()
    finally:
        conn.close()
    return summary


# ---------------------------------------------------------------------------
# Queue building
# ---------------------------------------------------------------------------

def _personalize(conn, unit, stay, template: str, merge_values: dict, thread_context: str):
    """Returns (message, note). Uses Claude only when there is guest context to
    respond to; otherwise a deterministic local render."""
    if not thread_context.strip():
        return render_local(template, merge_values), ""
    model = db.get_setting(conn, "queue_model", "claude-haiku-4-5")
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


# Section-header emoji used across every harmonized welcome template. A
# scraped message bubble containing 2+ of these is almost certainly the
# host's own greeting, not something the guest wrote — the generic thread
# scraper has no way to tell sender apart from bubble markup alone.
_HOST_SECTION_MARKERS = ("📍", "🚗", "🔑", "📶", "🛏️", "🔥", "🐄", "🏡", "🍞", "📱")


def _known_host_texts(conn, stay) -> list:
    """Text we know FOR CERTAIN the host sent for this stay — the approved
    draft and anything logged as actually sent — used to recognize the
    host's own voice inside a scraped thread."""
    texts = []
    if (stay["draft_greeting"] or "").strip():
        texts.append(stay["draft_greeting"])
    rows = conn.execute("SELECT content FROM sent_greetings WHERE stay_id = ?",
                        (stay["id"],)).fetchall()
    texts.extend(r["content"] for r in rows)
    return texts


def _looks_like_host_text(text: str, known_host_texts: list) -> bool:
    if sum(1 for m in _HOST_SECTION_MARKERS if m in text) >= 2:
        return True
    norm = " ".join(text.split()).lower()
    if len(norm) < 20:
        return False
    for known in known_host_texts:
        known_norm = " ".join(known.split()).lower()
        if len(known_norm) < 20:
            continue  # too short/blank to trust — "" is a substring of everything
        if norm in known_norm or known_norm in norm:
            return True
    return False


def read_thread_context(conn, stay):
    """Best-effort: read the GUEST's messages from the Airbnb thread via the
    persistent Playwright profile (headless), filtering out anything that
    looks like the host's own sent greeting. Falls back to the stay's stored
    booking message on any failure — Airbnb markup changes often.

    Returns (context, source, scrape_note):
      source       — 'scraped' (live thread contributed text), 'stored'
                      (booking message only), or 'none' (no context at all)
      scrape_note  — non-empty only when the scrape was attempted and failed,
                      so a 'stored'/'none' result can say WHY it fell back
                      (session expired, DOM changed, Playwright missing, ...)
                      rather than just silently not having live context.
    """
    stored = (stay["booking_message"] or "").strip()
    code = (stay["confirmation_code"] or "").strip()
    if not code:
        return stored, ("stored" if stored else "none"), ""
    try:
        from .browser_assist import read_thread_messages
        scraped = read_thread_messages(code)
    except Exception as e:
        reason = " ".join(str(e).split())[:200]  # one line, no ASCII-art tool output
        note = f"couldn't read the Airbnb thread ({reason})"
        return stored, ("stored" if stored else "none"), note
    known_host_texts = _known_host_texts(conn, stay)
    scraped = [t for t in scraped if not _looks_like_host_text(t, known_host_texts)]
    if scraped:
        context = (stored + "\n" if stored else "") + "\n".join(scraped)
        return context, "scraped", ""
    return stored, ("stored" if stored else "none"), ""


def build_queue_for_today(force: bool = False) -> dict:
    """Sync calendars, then draft a welcome message for every stay checking in
    today. Idempotent: stays already queued today are skipped unless force=True."""
    sync = sync_calendars()
    today = date.today().isoformat()
    conn = db.connect()
    summary = {"date": today, "queued": 0, "skipped": 0, "needs_setup": 0,
               "errors": list(sync["errors"]), "sync": sync}
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

            source = ""
            if tpl is None:
                status, message, error = "needs_setup", "", f"{unit['name']} has no template yet."
            else:
                missing = missing_fields(tpl["content"], merge_values)
                if missing:
                    status, message = "needs_setup", ""
                    problems = []
                    if "guest_first_name" in missing:
                        problems.append("guest name unknown (Airbnb's calendar feed omits names) "
                                        "— open the stay and set the guest's name")
                    amen = [f for f in missing if f != "guest_first_name"]
                    if amen:
                        problems.append("missing amenity value(s): " + ", ".join(amen) +
                                        " — fill them in on the unit's Amenities form")
                    error = "; ".join(problems) + ". Then click Rebuild."
                else:
                    thread_context, source, scrape_note = read_thread_context(conn, stay)
                    message, note = _personalize(conn, unit, stay, tpl["content"], merge_values,
                                                 thread_context)
                    status = "ready"
                    error = "; ".join(x for x in (scrape_note, note) if x)

            if existing:
                conn.execute(
                    "UPDATE daily_queue SET message=?, status=?, error=?, thread_ref=?, "
                    "personalization_source=?, created_at=? WHERE id=?",
                    (message, status, error, stay["confirmation_code"] or "", source, db.now(),
                     existing["id"]))
            else:
                conn.execute(
                    "INSERT INTO daily_queue (stay_id, unit_id, guest_name, checkin_date, message, "
                    "status, error, thread_ref, personalization_source, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (stay["id"], stay["unit_id"], stay["guest_name"], today, message, status,
                     error, stay["confirmation_code"] or "", source, db.now()))
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
    """Desktop notification (macOS or Windows); the dashboard badge always shows
    the same summary regardless."""
    if summary["queued"] == 0:
        return
    text = f"{summary['queued']} arrival(s) queued for today"
    if summary["needs_setup"]:
        text += f" — {summary['needs_setup']} need setup"
    import sys
    try:
        if sys.platform == "darwin":
            subprocess.run(
                ["osascript", "-e",
                 f'display notification "{text}" with title "Greeting Studio"'],
                timeout=10, capture_output=True)
        elif sys.platform == "win32":
            ps = (
                "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, "
                "ContentType = WindowsRuntime] > $null;"
                "$t = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent("
                "[Windows.UI.Notifications.ToastTemplateType]::ToastText02);"
                "$n = $t.GetElementsByTagName('text');"
                "$n.Item(0).AppendChild($t.CreateTextNode('Greeting Studio')) > $null;"
                f"$n.Item(1).AppendChild($t.CreateTextNode('{text}')) > $null;"
                "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("
                "'Greeting Studio').Show([Windows.UI.Notifications.ToastNotification]::new($t))"
            )
            subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                           timeout=15, capture_output=True)
    except Exception:
        pass  # notification is best-effort — the dashboard badge still shows it
