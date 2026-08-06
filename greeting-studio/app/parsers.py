"""Parsers: message-thread imports (text/CSV/JSON), stay CSV import, and .ics calendar import."""
import csv
import io
import json
import re
from datetime import date, datetime

# ---------------------------------------------------------------------------
# Thread parsing
# ---------------------------------------------------------------------------

HOST_ALIASES = {"host", "me", "you", "owner"}
GUEST_ALIASES = {"guest", "them", "visitor"}

GREETING_KEYWORDS = [
    "wifi", "wi-fi", "door code", "keypad", "lockbox", "check-in", "check in",
    "checkin", "checkout", "check-out", "check out", "parking", "park in",
    "welcome", "address", "entry", "front door", "smart lock", "thermostat",
    "arrival", "looking forward to hosting",
]


def _sender_from_label(label: str, host_names: set, guest_names: set) -> str:
    norm = label.strip().lower().rstrip(":")
    if norm in HOST_ALIASES or norm in host_names:
        return "host"
    if norm in GUEST_ALIASES or norm in guest_names:
        return "guest"
    return ""


def parse_thread_text(raw: str, guest_name: str = "") -> list:
    """Parse a pasted thread. Recognizes 'Host:'/'Guest:'/'Me:' prefixes and, when a
    guest name is supplied, lines starting with that name. Multi-line messages are
    accumulated until the next sender label."""
    guest_names = set()
    if guest_name.strip():
        gn = guest_name.strip().lower()
        guest_names.add(gn)
        guest_names.add(gn.split()[0])  # first name alone

    label_re = re.compile(r"^([A-Za-z][A-Za-z .'-]{0,40}):\s*(.*)$")
    messages = []
    current = None
    for line in raw.splitlines():
        m = label_re.match(line)
        sender = None
        if m:
            sender = _sender_from_label(m.group(1), set(), guest_names)
        if sender:
            if current and current["body"].strip():
                messages.append(current)
            current = {"sender": sender, "body": m.group(2)}
        elif current is not None:
            current["body"] += "\n" + line
        elif line.strip():
            # Text before any label — treat as a guest message blob
            current = {"sender": "guest", "body": line}
    if current and current["body"].strip():
        messages.append(current)
    for msg in messages:
        msg["body"] = msg["body"].strip()
    return [m for m in messages if m["body"]]


def parse_thread_csv(raw: str) -> list:
    """CSV with columns: sender, message (extra columns ignored)."""
    reader = csv.DictReader(io.StringIO(raw))
    if not reader.fieldnames:
        return []
    fields = {f.strip().lower(): f for f in reader.fieldnames}
    sender_col = fields.get("sender") or fields.get("from") or fields.get("role")
    body_col = fields.get("message") or fields.get("body") or fields.get("text")
    if not sender_col or not body_col:
        raise ValueError("CSV must have 'sender' and 'message' columns.")
    messages = []
    for row in reader:
        sender_raw = (row.get(sender_col) or "").strip().lower()
        body = (row.get(body_col) or "").strip()
        if not body:
            continue
        sender = "host" if sender_raw in HOST_ALIASES else "guest"
        messages.append({"sender": sender, "body": body})
    return messages


def parse_thread_json(raw: str) -> list:
    """JSON list of objects with sender/message keys."""
    data = json.loads(raw)
    if isinstance(data, dict):
        data = data.get("messages", [])
    messages = []
    for item in data:
        sender_raw = str(item.get("sender") or item.get("from") or item.get("role") or "").lower()
        body = str(item.get("message") or item.get("body") or item.get("text") or "").strip()
        if not body:
            continue
        sender = "host" if sender_raw in HOST_ALIASES else "guest"
        messages.append({"sender": sender, "body": body})
    return messages


def parse_thread(raw: str, fmt: str = "auto", guest_name: str = "") -> list:
    fmt = (fmt or "auto").lower()
    if fmt == "auto":
        stripped = raw.lstrip()
        if stripped.startswith("[") or stripped.startswith("{"):
            fmt = "json"
        elif re.match(r"^\s*sender\s*,", raw, re.IGNORECASE):
            fmt = "csv"
        else:
            fmt = "text"
    if fmt == "json":
        return parse_thread_json(raw)
    if fmt == "csv":
        return parse_thread_csv(raw)
    return parse_thread_text(raw, guest_name)


def looks_like_greeting(body: str) -> bool:
    """Heuristic: a host message is a routine greeting/check-in message if it hits
    enough of the standard-info keywords."""
    lower = body.lower()
    hits = sum(1 for kw in GREETING_KEYWORDS if kw in lower)
    return hits >= 2 or (hits >= 1 and len(body) > 300)


# ---------------------------------------------------------------------------
# Stays: CSV import
# ---------------------------------------------------------------------------

def parse_stays_csv(raw: str) -> list:
    """CSV with columns: guest_name, checkin_date, checkout_date[, booking_message][, unit]"""
    reader = csv.DictReader(io.StringIO(raw))
    if not reader.fieldnames:
        return []
    fields = {f.strip().lower(): f for f in reader.fieldnames}

    def col(*names):
        for n in names:
            if n in fields:
                return fields[n]
        return None

    guest_col = col("guest_name", "guest", "name")
    ci_col = col("checkin_date", "checkin", "check-in", "start", "start_date")
    co_col = col("checkout_date", "checkout", "check-out", "end", "end_date")
    msg_col = col("booking_message", "message", "note")
    unit_col = col("unit", "unit_name", "listing")
    if not (guest_col and ci_col and co_col):
        raise ValueError("CSV needs guest_name, checkin_date, checkout_date columns.")
    stays = []
    for row in reader:
        guest = (row.get(guest_col) or "").strip()
        ci = normalize_date((row.get(ci_col) or "").strip())
        co = normalize_date((row.get(co_col) or "").strip())
        if not (guest and ci and co):
            continue
        stays.append({
            "guest_name": guest,
            "checkin_date": ci,
            "checkout_date": co,
            "booking_message": (row.get(msg_col) or "").strip() if msg_col else "",
            "unit": (row.get(unit_col) or "").strip() if unit_col else "",
        })
    return stays


def normalize_date(value: str) -> str:
    value = value.strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%Y%m%d", "%b %d, %Y", "%B %d, %Y"):
        try:
            return datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            continue
    return ""


# ---------------------------------------------------------------------------
# .ics import (Airbnb → Google Calendar export)
# ---------------------------------------------------------------------------

def _unfold_ics(text: str) -> list:
    """RFC 5545 line unfolding: continuation lines start with a space or tab."""
    lines = []
    for raw_line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw_line[:1] in (" ", "\t") and lines:
            lines[-1] += raw_line[1:]
        else:
            lines.append(raw_line)
    return lines


def _ics_date(value: str) -> str:
    value = value.strip()
    m = re.match(r"^(\d{4})(\d{2})(\d{2})", value)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat()
        except ValueError:
            return ""
    return ""


SKIP_SUMMARIES = ("not available", "blocked", "airbnb (not available)")


def parse_ics(text: str) -> list:
    """Extract reservation events. Airbnb calendar events use SUMMARY like
    'Reserved - Jane Doe' or just 'Reserved', with the reservation code in the
    DESCRIPTION ('Reservation URL: https://www.airbnb.com/hosting/reservations/details/HMXYZ...')."""
    events = []
    current = None
    for line in _unfold_ics(text):
        if line.startswith("BEGIN:VEVENT"):
            current = {}
        elif line.startswith("END:VEVENT"):
            if current is not None:
                events.append(current)
            current = None
        elif current is not None and ":" in line:
            key, _, value = line.partition(":")
            key = key.split(";")[0].upper()
            current[key] = value

    stays = []
    for ev in events:
        summary = ev.get("SUMMARY", "").strip()
        if summary.lower() in SKIP_SUMMARIES or "not available" in summary.lower():
            continue
        checkin = _ics_date(ev.get("DTSTART", ""))
        checkout = _ics_date(ev.get("DTEND", ""))
        if not (checkin and checkout):
            continue
        guest = summary
        m = re.match(r"^reserved\s*[-–—:]?\s*(.*)$", summary, re.IGNORECASE)
        if m:
            guest = m.group(1).strip() or "Guest (see Airbnb)"
        desc = ev.get("DESCRIPTION", "")
        code = ""
        code_m = re.search(r"reservations/details/([A-Z0-9]+)", desc)
        if code_m:
            code = code_m.group(1)
        stays.append({
            "guest_name": guest,
            "checkin_date": checkin,
            "checkout_date": checkout,
            "booking_message": "",
            "confirmation_code": code,
        })
    return stays
