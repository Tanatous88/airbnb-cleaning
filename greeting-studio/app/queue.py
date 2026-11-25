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
import sqlite3
import subprocess
from datetime import date, datetime, timedelta, timezone

import urllib.request

from . import db, parsers, safety
from .claude import ClaudeError, ask_claude, render_prompt

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AUDIT_LOG_PATH = os.path.join(BASE_DIR, "send_audit.log")

MERGE_FIELD_RE = re.compile(r"\{\{\s*([a-zA-Z0-9_]+)\s*\}\}")


# ---------------------------------------------------------------------------
# Merge values + local rendering
# ---------------------------------------------------------------------------

WEEKEND_GUIDE_URL = "https://claude.ai/artifact/Aufucypx6pj1QuMLKwd5xq"


def _weekend_guide_blurb(checkin_iso: str, checkout_iso: str,
                         url: str = WEEKEND_GUIDE_URL) -> str:
    """Non-empty whenever the stay covers a Saturday or Sunday night — not
    just a Friday/Saturday check-in, but also a mid-week check-in whose stay
    runs into the weekend. The guide (events, WSU/Idaho sports, restaurants)
    is useful for any of those, not only a classic weekend getaway."""
    try:
        checkin = date.fromisoformat(checkin_iso)
        checkout = date.fromisoformat(checkout_iso)
    except ValueError:
        return ""
    nights = [checkin + timedelta(days=n) for n in range((checkout - checkin).days)]
    if not any(d.weekday() in (4, 5, 6) for d in nights):  # Fri, Sat, Sun nights
        return ""
    return (
        "🏟️ This Weekend on the Palouse\n"
        "Since your stay falls over the weekend, check out our live guide to local events, "
        f"WSU & Idaho sports schedules, and restaurant picks: {url}\n"
    )


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
        # door_code_fallback is for locks synced with Airbnb (Cosmopolitan, Nook):
        # Airbnb shows the guest their code in the app, so a missing phone
        # number shouldn't block the whole greeting.
        "door_code": (phone_last4 or amenities.get("door_code")
                      or amenities.get("door_code_fallback") or "[MISSING]"),
        "wifi_network": amenities.get("wifi_network") or "[MISSING]",
        "wifi_password": amenities.get("wifi_password") or "[MISSING]",
        "weekend_guide": _weekend_guide_blurb(
            stay["checkin_date"], stay["checkout_date"],
            db.get_setting(conn, "weekend_guide_url", "") or WEEKEND_GUIDE_URL),
    }


def render_local(template: str, merge_values: dict) -> str:
    """Deterministic merge-field substitution — used when there's nothing to
    personalize (no booking message), or as fallback if the Claude call fails."""
    def sub(m):
        return str(merge_values.get(m.group(1), m.group(0)))
    rendered = MERGE_FIELD_RE.sub(sub, template)
    # An empty-string field (e.g. weekend_guide on a weekday stay) leaves its
    # own line blank; collapse the resulting run of blank lines back down to
    # one, same as if that line had never been in the template.
    return re.sub(r"\n{3,}", "\n\n", rendered)



def strip_subject(text: str) -> str:
    """Airbnb chat has no subject field, so a leading "Subject: ..." line
    (from older templates) would show up literally in the guest's thread."""
    lines = (text or "").lstrip("\n").split("\n")
    if lines and lines[0].strip().lower().startswith("subject:"):
        lines = lines[1:]
        while lines and not lines[0].strip():
            lines = lines[1:]
    return "\n".join(lines)


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
    try:
        conn.execute(
            "INSERT INTO stays (unit_id, guest_name, checkin_date, checkout_date, "
            "booking_message, confirmation_code, phone_last4, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (unit_id, s["guest_name"], s["checkin_date"], s["checkout_date"],
             s.get("booking_message", ""), code, s.get("phone_last4", ""), db.now()))
    except sqlite3.IntegrityError:
        # The DB's unique-confirmation-code guard caught a case the matching
        # above missed (same reservation, no match found above for some
        # reason) — self-heal by updating the row that already holds this
        # code instead of crashing the whole sync over one stay.
        row = conn.execute(
            "SELECT * FROM stays WHERE confirmation_code = ?", (code,)).fetchone()
        if not row:
            raise
        updates, values = [], []
        if s["guest_name"] != UNKNOWN_GUEST and s["guest_name"] != row["guest_name"]:
            updates.append("guest_name = ?"); values.append(s["guest_name"])
        for col in ("checkin_date", "checkout_date"):
            if s[col] != row[col]:
                updates.append(f"{col} = ?"); values.append(s[col])
        if updates:
            values.append(row["id"])
            conn.execute(f"UPDATE stays SET {', '.join(updates)} WHERE id = ?", values)
            return "updated"
        return "unchanged"
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
        return strip_subject(render_local(template, merge_values)), ""
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
        return strip_subject(ask_claude(prompt, model=model)), ""
    except ClaudeError as e:
        # Fail soft at draft time: an un-personalized greeting is still correct.
        return strip_subject(render_local(template, merge_values)), f"personalization skipped ({e})"


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


def is_placeholder_name(name) -> bool:
    """True when the calendar sync couldn't get a real guest name."""
    n = " ".join((name or "").lower().split())
    return not n or n == UNKNOWN_GUEST.lower() or n.split()[0] in ("guest", "reserved")


def prior_stays(conn, stay) -> list:
    """Earlier stays by the same guest, at any unit. A match is the same full
    name (two or more words), or the same first name AND the same phone last
    4 — a first name alone ("Robert") is far too common to trust."""
    name = " ".join((stay["guest_name"] or "").lower().split())
    if is_placeholder_name(name):
        return []
    phone = (stay["phone_last4"] or "").strip()
    rows = conn.execute(
        "SELECT s.id, s.guest_name, s.phone_last4, s.checkin_date, u.name AS unit_name "
        "FROM stays s JOIN units u ON u.id = s.unit_id "
        "WHERE s.id != ? AND s.checkout_date <= ? ORDER BY s.checkin_date DESC",
        (stay["id"], stay["checkin_date"])).fetchall()
    found = []
    for r in rows:
        other = " ".join((r["guest_name"] or "").lower().split())
        if is_placeholder_name(other):
            continue
        same_full_name = len(name.split()) >= 2 and other == name
        same_phone = (bool(phone) and (r["phone_last4"] or "").strip() == phone
                      and other.split()[0] == name.split()[0])
        if same_full_name or same_phone:
            found.append({"stay_id": r["id"], "unit_name": r["unit_name"],
                          "checkin_date": r["checkin_date"]})
    return found


def returning_note(prior: list) -> str:
    """One line for the greeting prompt, or '' for a first-time guest."""
    if not prior:
        return ""
    visits = []
    for p in prior[:3]:
        try:
            when = date.fromisoformat(p["checkin_date"]).strftime("%B %Y")
        except ValueError:
            when = p["checkin_date"]
        visits.append(f"{p['unit_name']} ({when})")
    return ("RETURNING GUEST: they have stayed with us before — " + "; ".join(visits)
            + ". Welcome them back warmly in one natural sentence near the top.")


def _short_reason(e: Exception) -> str:
    """One readable line from a browser error. Playwright appends a box of
    ASCII art after the first line; keep only the part a person can use."""
    first = (str(e).strip().splitlines() or [type(e).__name__])[0]
    first = first.split(": ", 1)[-1] if first.startswith(("BrowserType.", "Page.")) else first
    if "Executable doesn't exist" in first:
        return "the automated browser isn't installed: run 'playwright install chromium'"
    return " ".join(first.split())[:160]


def read_thread_context(conn, stay):
    """Best-effort: read the GUEST's messages from the Airbnb thread via the
    persistent Playwright profile, filtering out anything that looks like the
    host's own sent greeting. Falls back to the stay's stored booking message
    on any failure — Airbnb markup changes often.

    Side effect: if the stay only has a placeholder guest name and the thread
    shows the guest's name ("Sent by Melissa · Booker"), the stay is updated
    with it — callers should re-read the stay afterwards.

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
        from .browser_assist import read_thread
        thread = read_thread(code, hidden=db.get_setting(conn, "hide_browser", "1") == "1")
    except Exception as e:
        note = "couldn't read the Airbnb thread, so this is the plain template (" \
            + _short_reason(e) + ")"
        return stored, ("stored" if stored else "none"), note
    scraped = thread.get("messages") or []
    found_name = (thread.get("guest_name") or "").strip()
    if found_name and not is_placeholder_name(found_name) \
            and is_placeholder_name(stay["guest_name"]):
        conn.execute("UPDATE stays SET guest_name = ? WHERE id = ?", (found_name, stay["id"]))
        conn.commit()
        audit("name_filled", "", found_name, f"stay {stay['id']}, from the Airbnb thread")
    known_host_texts = _known_host_texts(conn, stay)
    scraped = [t for t in scraped if not _looks_like_host_text(t, known_host_texts)]
    if scraped:
        context = (stored + "\n" if stored else "") + "\n".join(scraped)
        return context, "scraped", ""
    return stored, ("stored" if stored else "none"), ""


def trusted_texts(conn, template: str, merge_values: dict) -> list:
    """Text the host wrote or configured: contact details in a draft that
    appear in none of these are flagged (see safety.py)."""
    return [render_local(template, merge_values),
            db.get_setting(conn, "host_signature", ""),
            db.get_setting(conn, "weekend_guide_url", "") or WEEKEND_GUIDE_URL,
            "https://www.airbnb.com"]


def draft_warning(conn, unit, stay, message: str) -> str:
    """Warning for a welcome draft (or a host's edit of one), '' if clean."""
    tpl = db.latest_template(conn, unit["id"])
    if tpl is None or not message:
        return ""
    trusted = trusted_texts(conn, tpl["content"], build_merge_values(conn, unit, stay))
    return safety.warning_text(safety.unexpected(message, *trusted))


def _save_queue_item(conn, stay, day: str, kind: str, message: str, status: str,
                     error: str, source: str, warning: str = "") -> None:
    """Insert or update the one queue row for (stay, day). The DB's unique
    index on (stay_id, checkin_date) guards against concurrent builds racing;
    if another build won, update its row instead of crashing."""
    code = stay["confirmation_code"] or ""
    row = conn.execute("SELECT id FROM daily_queue WHERE stay_id = ? AND checkin_date = ?",
                       (stay["id"], day)).fetchone()
    if row is None:
        try:
            conn.execute(
                "INSERT INTO daily_queue (stay_id, unit_id, guest_name, checkin_date, kind, "
                "message, status, error, thread_ref, personalization_source, warning, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (stay["id"], stay["unit_id"], stay["guest_name"], day, kind, message, status,
                 error, code, source, warning, db.now()))
            return
        except sqlite3.IntegrityError:
            row = conn.execute("SELECT id FROM daily_queue WHERE stay_id = ? AND checkin_date = ?",
                               (stay["id"], day)).fetchone()
            if row is None:
                raise
    conn.execute(
        "UPDATE daily_queue SET guest_name=?, kind=?, message=?, status=?, error=?, thread_ref=?, "
        "personalization_source=?, warning=?, created_at=? WHERE id=?",
        (stay["guest_name"], kind, message, status, error, code, source, warning, db.now(),
         row["id"]))


def _skip_existing(conn, stay_id: int, day: str, force: bool) -> bool:
    existing = conn.execute("SELECT status FROM daily_queue WHERE stay_id = ? AND checkin_date = ?",
                            (stay_id, day)).fetchone()
    return bool(existing) and (not force or existing["status"] == "sent")


def _reload_stay(conn, stay_id: int):
    return conn.execute("SELECT * FROM stays WHERE id = ?", (stay_id,)).fetchone()


def _setup_problems(missing: list, scrape_note: str = "") -> str:
    problems = []
    if "guest_first_name" in missing:
        problems.append("guest name unknown (not in Airbnb's calendar feed or message thread) "
                        "— open the stay and set the guest's name")
    amen = [f for f in missing if f != "guest_first_name"]
    if amen:
        problems.append("missing value(s): " + ", ".join(amen) +
                        " — door code / name go on the stay, Wi-Fi on the unit's Amenities form")
    if scrape_note:
        problems.append(scrape_note)
    return "; ".join(problems) + ". Fix it on the stay page and save; this card redrafts itself."


def welcome_lead_days(conn) -> int:
    """How many days before check-in the welcome message is drafted."""
    try:
        return max(0, min(3, int(db.get_setting(conn, "welcome_days_before", "1"))))
    except ValueError:
        return 1


def _build_arrivals(conn, today: str, force: bool, summary: dict) -> None:
    """Welcome messages for guests arriving between today and the lead day
    (default: tomorrow). Today's arrivals are included too, so a last-minute
    booking or a message not sent yesterday still shows up."""
    last = (date.fromisoformat(today) + timedelta(days=welcome_lead_days(conn))).isoformat()
    stays = conn.execute(
        "SELECT * FROM stays WHERE checkin_date >= ? AND checkin_date <= ? AND status != 'sent' "
        "ORDER BY checkin_date", (today, last)).fetchall()
    for stay in stays:
        if _skip_existing(conn, stay["id"], today, force):
            summary["skipped"] += 1
            continue
        status = _draft_arrival(conn, stay, today, live_thread=True)
        summary["queued"] += 1
        if status == "needs_setup":
            summary["needs_setup"] += 1


def _draft_arrival(conn, stay, day: str, live_thread: bool) -> str:
    """Draft (or redraft) one guest's welcome card for `day`. live_thread=False
    skips opening Airbnb in the browser and uses only what's stored — used
    when the host fixes a stay and the card should update right away."""
    unit = conn.execute("SELECT * FROM units WHERE id = ?", (stay["unit_id"],)).fetchone()
    tpl = db.latest_template(conn, unit["id"])
    source, message, warning = "", "", ""
    if tpl is None:
        status, error = "needs_setup", f"{unit['name']} has no template yet."
    else:
        merge_values = build_merge_values(conn, unit, stay)
        missing = missing_fields(tpl["content"], merge_values)
        thread = None
        if live_thread and "guest_first_name" in missing \
                and (stay["confirmation_code"] or "").strip():
            # The thread often knows the name the calendar feed left out.
            thread = read_thread_context(conn, stay)
            stay = _reload_stay(conn, stay["id"])
            merge_values = build_merge_values(conn, unit, stay)
            missing = missing_fields(tpl["content"], merge_values)
        if missing:
            status = "needs_setup"
            error = _setup_problems(missing, thread[2] if thread else "")
        else:
            if thread or live_thread:
                context, source, scrape_note = thread or read_thread_context(conn, stay)
            else:
                context = (stay["booking_message"] or "").strip()
                source, scrape_note = ("stored" if context else "none"), ""
            welcome_back = returning_note(prior_stays(conn, stay))
            if welcome_back:
                context = (welcome_back + "\n" + context).strip()
            message, note = _personalize(conn, unit, stay, tpl["content"], merge_values, context)
            status = "ready"
            error = "; ".join(x for x in (scrape_note, note) if x)
            warning = safety.warning_text(safety.unexpected(
                message, *trusted_texts(conn, tpl["content"], merge_values)))
    _save_queue_item(conn, stay, day, "arrival", message, status, error, source, warning)
    audit("draft", unit["name"], stay["guest_name"], status)
    return status


def _build_midstays(conn, today_date: date, force: bool, summary: dict) -> None:
    """A short "how's everything?" note the day after arrival, for stays of
    two nights or more — a chance to fix a problem before it becomes a bad
    review. Deterministic (no Claude call) and, like every queue item, only
    goes out when the host clicks Send."""
    if db.get_setting(conn, "midstay_enabled", "1") != "1":
        return
    today = today_date.isoformat()
    yesterday = (today_date - timedelta(days=1)).isoformat()
    template = db.get_setting(conn, "midstay_template", db.DEFAULT_MIDSTAY_TEMPLATE)
    signature = db.get_setting(conn, "host_signature", "— Your hosts")
    stays = conn.execute("SELECT * FROM stays WHERE checkin_date = ? AND checkout_date > ?",
                         (yesterday, today)).fetchall()
    for stay in stays:
        if _skip_existing(conn, stay["id"], today, force):
            summary["skipped"] += 1
            continue
        status = _draft_midstay(conn, stay, today, template, signature)
        summary["midstay"] += 1
        if status == "needs_setup":
            summary["needs_setup"] += 1


def _draft_midstay(conn, stay, day: str, template: str, signature: str) -> str:
    unit = conn.execute("SELECT * FROM units WHERE id = ?", (stay["unit_id"],)).fetchone()
    merge_values = dict(build_merge_values(conn, unit, stay),
                        unit_name=unit["name"], host_signature=signature)
    missing = missing_fields(template, merge_values)
    if missing:
        status, message, error = "needs_setup", "", _setup_problems(missing)
    else:
        status, message, error = "ready", render_local(template, merge_values).strip(), ""
    _save_queue_item(conn, stay, day, "midstay", message, status, error, "")
    audit("draft_midstay", unit["name"], stay["guest_name"], status)
    return status


def redraft_cards_for_stay(conn, stay_id: int, redraft_ready: bool) -> list:
    """After the host edits a stay: redraft today's unsent cards for it.
    Cards needing setup are always redrafted; ready cards only when
    redraft_ready (name or door code changed), so hand edits on a card
    aren't thrown away for an unrelated save. Returns the new statuses."""
    today = date.today().isoformat()
    rows = conn.execute(
        "SELECT * FROM daily_queue WHERE stay_id = ? AND checkin_date = ? AND status != 'sent'",
        (stay_id, today)).fetchall()
    stay = _reload_stay(conn, stay_id)
    out = []
    for row in rows:
        if row["status"] != "needs_setup" and not redraft_ready:
            continue
        if (row["kind"] or "arrival") == "midstay":
            out.append(_draft_midstay(
                conn, stay, today,
                db.get_setting(conn, "midstay_template", db.DEFAULT_MIDSTAY_TEMPLATE),
                db.get_setting(conn, "host_signature", "— Your hosts")))
        else:
            out.append(_draft_arrival(conn, stay, today, live_thread=False))
    conn.commit()
    return out


def build_queue_for_today(force: bool = False) -> dict:
    """Sync calendars, then draft a welcome message for every stay checking in
    soon (see welcome_lead_days) and a check-in note for every stay that
    arrived yesterday.
    Idempotent: stays already queued today are skipped unless force=True."""
    sync = sync_calendars()
    today_date = date.today()
    today = today_date.isoformat()
    conn = db.connect()
    summary = {"date": today, "queued": 0, "midstay": 0, "skipped": 0, "needs_setup": 0,
               "errors": list(sync["errors"]), "sync": sync}
    try:
        _build_arrivals(conn, today, force, summary)
        _build_midstays(conn, today_date, force, summary)
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
    if summary["queued"] == 0 and not summary.get("midstay"):
        return
    text = f"{summary['queued']} welcome message(s) ready to send"
    if summary.get("midstay"):
        text += f", {summary['midstay']} mid-stay check-in(s)"
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
