"""Guest Greeting & Review Studio — FastAPI backend."""
import json
import os
import re
from datetime import date, timedelta

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import audit as audit_mod
from . import db, parsers
from .claude import ClaudeError, ask_claude, render_prompt, set_api_key

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC_DIR = os.path.join(BASE_DIR, "static")

app = FastAPI(title="Guest Greeting & Review Studio")
db.init_db()


def claude_call(conn, prompt_name: str, max_tokens: int = 4000, **variables) -> str:
    model = db.get_setting(conn, "anthropic_model", "claude-sonnet-4-6")
    prompt = render_prompt(prompt_name, **variables)
    try:
        return ask_claude(prompt, model=model, max_tokens=max_tokens)
    except ClaudeError as e:
        raise HTTPException(status_code=502, detail=str(e))


def get_unit_or_404(conn, unit_id: int):
    unit = conn.execute("SELECT * FROM units WHERE id = ?", (unit_id,)).fetchone()
    if not unit:
        raise HTTPException(status_code=404, detail="Unit not found")
    return unit


def get_stay_or_404(conn, stay_id: int):
    stay = conn.execute("SELECT * FROM stays WHERE id = ?", (stay_id,)).fetchone()
    if not stay:
        raise HTTPException(status_code=404, detail="Stay not found")
    return stay


# ---------------------------------------------------------------------------
# Units
# ---------------------------------------------------------------------------

class UnitIn(BaseModel):
    name: str
    airbnb_listing_id: str = ""
    listing_title: str = ""
    profile: str = ""


class UnitUpdate(BaseModel):
    name: str | None = None
    listing_title: str | None = None
    profile: str | None = None
    amenities: dict | None = None
    listing_description: str | None = None


@app.get("/api/units")
def list_units():
    conn = db.connect()
    try:
        units = []
        for row in conn.execute("SELECT * FROM units ORDER BY name").fetchall():
            u = dict(row)
            u["amenities"] = json.loads(u.get("amenities") or "{}")
            tpl = db.latest_template(conn, row["id"])
            u["template_version"] = tpl["version"] if tpl else 0
            u["template_source"] = tpl["source"] if tpl else ""
            u["greeting_count"] = conn.execute(
                "SELECT COUNT(*) c FROM thread_messages WHERE unit_id = ? AND sender='host' AND is_greeting=1",
                (row["id"],)).fetchone()["c"]
            u["message_count"] = conn.execute(
                "SELECT COUNT(*) c FROM thread_messages WHERE unit_id = ?", (row["id"],)).fetchone()["c"]
            units.append(u)
        return units
    finally:
        conn.close()


@app.post("/api/units")
def create_unit(unit: UnitIn):
    if not unit.name.strip():
        raise HTTPException(status_code=400, detail="Unit name is required")
    conn = db.connect()
    try:
        cur = conn.execute(
            "INSERT INTO units (name, airbnb_listing_id, listing_title, profile, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (unit.name.strip(), unit.airbnb_listing_id.strip() or None,
             unit.listing_title.strip(), unit.profile.strip(), db.now()))
        conn.commit()
        return {"id": cur.lastrowid}
    except Exception as e:
        if "UNIQUE" in str(e):
            raise HTTPException(status_code=400, detail="A unit with that Airbnb listing ID already exists")
        raise
    finally:
        conn.close()


@app.put("/api/units/{unit_id}")
def update_unit(unit_id: int, body: UnitUpdate):
    conn = db.connect()
    try:
        unit = get_unit_or_404(conn, unit_id)
        fields, values = [], []
        for col in ("name", "listing_title", "profile", "listing_description"):
            val = getattr(body, col)
            if val is not None:
                fields.append(f"{col} = ?")
                values.append(val)
        if body.amenities is not None:
            fields.append("amenities = ?")
            values.append(json.dumps(body.amenities))
        if fields:
            values.append(unit_id)
            conn.execute(f"UPDATE units SET {', '.join(fields)} WHERE id = ?", values)
            conn.commit()
        return {"ok": True}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Feature 1: imports + template synthesis
# ---------------------------------------------------------------------------

class ImportIn(BaseModel):
    raw_text: str
    format: str = "auto"          # auto | text | csv | json
    guest_name: str = ""
    stay_dates: str = ""


@app.post("/api/units/{unit_id}/imports")
def import_thread(unit_id: int, body: ImportIn):
    if not body.raw_text.strip():
        raise HTTPException(status_code=400, detail="Nothing to import")
    conn = db.connect()
    try:
        get_unit_or_404(conn, unit_id)
        try:
            messages = parsers.parse_thread(body.raw_text, body.format, body.guest_name)
        except (ValueError, json.JSONDecodeError) as e:
            raise HTTPException(status_code=400, detail=f"Could not parse import: {e}")
        if not messages:
            raise HTTPException(
                status_code=400,
                detail="No messages recognized. Prefix each message with 'Host:' or 'Guest:' "
                       "(or use CSV with sender,message columns).")
        cur = conn.execute(
            "INSERT INTO imports (unit_id, guest_name, stay_dates, format, raw_text, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (unit_id, body.guest_name, body.stay_dates, body.format, body.raw_text, db.now()))
        import_id = cur.lastrowid
        host_count = guest_count = greeting_count = 0
        for msg in messages:
            is_greeting = 1 if (msg["sender"] == "host" and parsers.looks_like_greeting(msg["body"])) else 0
            conn.execute(
                "INSERT INTO thread_messages (import_id, unit_id, sender, body, is_greeting) "
                "VALUES (?, ?, ?, ?, ?)",
                (import_id, unit_id, msg["sender"], msg["body"], is_greeting))
            if msg["sender"] == "host":
                host_count += 1
            else:
                guest_count += 1
            greeting_count += is_greeting
        conn.commit()
        return {"import_id": import_id, "messages": len(messages),
                "host_messages": host_count, "guest_messages": guest_count,
                "greetings_detected": greeting_count}
    finally:
        conn.close()


@app.get("/api/units/{unit_id}/messages")
def list_messages(unit_id: int):
    conn = db.connect()
    try:
        get_unit_or_404(conn, unit_id)
        rows = conn.execute(
            "SELECT m.*, i.guest_name, i.stay_dates FROM thread_messages m "
            "JOIN imports i ON i.id = m.import_id WHERE m.unit_id = ? ORDER BY m.id",
            (unit_id,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


class MessageUpdate(BaseModel):
    sender: str | None = None
    is_greeting: bool | None = None


@app.put("/api/messages/{message_id}")
def update_message(message_id: int, body: MessageUpdate):
    conn = db.connect()
    try:
        row = conn.execute("SELECT * FROM thread_messages WHERE id = ?", (message_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Message not found")
        if body.sender in ("host", "guest"):
            conn.execute("UPDATE thread_messages SET sender = ? WHERE id = ?", (body.sender, message_id))
        if body.is_greeting is not None:
            conn.execute("UPDATE thread_messages SET is_greeting = ? WHERE id = ?",
                         (1 if body.is_greeting else 0, message_id))
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()


@app.post("/api/units/{unit_id}/synthesize")
def synthesize_template(unit_id: int):
    conn = db.connect()
    try:
        unit = get_unit_or_404(conn, unit_id)
        greetings = conn.execute(
            "SELECT body FROM thread_messages WHERE unit_id = ? AND sender='host' AND is_greeting=1 "
            "ORDER BY id", (unit_id,)).fetchall()
        if not greetings:
            raise HTTPException(
                status_code=400,
                detail="No greetings on file for this unit. Import message history first, or use "
                       "'Bootstrap from another unit' for a brand-new listing.")
        content = claude_call(
            conn, "synthesize_template",
            unit_name=unit["name"],
            unit_profile=unit["profile"] or "(no notes)",
            greetings="\n\n---\n\n".join(g["body"] for g in greetings))
        version = db.save_template(conn, unit_id, content, source="synthesized",
                                   note=f"Synthesized from {len(greetings)} greeting(s)")
        conn.commit()
        return {"version": version, "content": content}
    finally:
        conn.close()


class TemplateIn(BaseModel):
    content: str
    note: str = ""


@app.get("/api/units/{unit_id}/template")
def get_template(unit_id: int):
    conn = db.connect()
    try:
        get_unit_or_404(conn, unit_id)
        tpl = db.latest_template(conn, unit_id)
        return dict(tpl) if tpl else None
    finally:
        conn.close()


@app.post("/api/units/{unit_id}/template")
def save_template(unit_id: int, body: TemplateIn):
    if not body.content.strip():
        raise HTTPException(status_code=400, detail="Template cannot be empty")
    conn = db.connect()
    try:
        get_unit_or_404(conn, unit_id)
        version = db.save_template(conn, unit_id, body.content, source="manual",
                                   note=body.note or "Manual edit")
        conn.commit()
        return {"version": version}
    finally:
        conn.close()


@app.get("/api/units/{unit_id}/template/versions")
def template_versions(unit_id: int):
    conn = db.connect()
    try:
        get_unit_or_404(conn, unit_id)
        rows = conn.execute(
            "SELECT * FROM templates WHERE unit_id = ? ORDER BY version DESC", (unit_id,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


class BootstrapIn(BaseModel):
    donor_unit_id: int


@app.post("/api/units/{unit_id}/bootstrap")
def bootstrap_template(unit_id: int, body: BootstrapIn):
    conn = db.connect()
    try:
        unit = get_unit_or_404(conn, unit_id)
        donor = get_unit_or_404(conn, body.donor_unit_id)
        donor_tpl = db.latest_template(conn, body.donor_unit_id)
        if not donor_tpl:
            raise HTTPException(
                status_code=400,
                detail=f"{donor['name']} has no template yet — synthesize one for it first.")
        amenities = json.loads(unit["amenities"] or "{}")
        content = claude_call(
            conn, "bootstrap_template",
            donor_name=donor["name"],
            donor_template=donor_tpl["content"],
            unit_name=unit["name"],
            listing_title=unit["listing_title"] or unit["name"],
            unit_profile=unit["profile"] or "(no profile)",
            listing_description=unit["listing_description"] or "(not provided)",
            amenities=json.dumps(amenities, indent=2) if amenities else "(none entered yet)")
        version = db.save_template(conn, unit_id, content, source="bootstrap",
                                   note=f"Bootstrapped from {donor['name']}")
        conn.commit()
        return {"version": version, "content": content}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Feature 2: consistency audit
# ---------------------------------------------------------------------------

@app.get("/api/audit")
def get_audit():
    conn = db.connect()
    try:
        core_items = db.get_core_items(conn)
        entries = []
        for unit in conn.execute("SELECT * FROM units ORDER BY name").fetchall():
            tpl = db.latest_template(conn, unit["id"])
            entries.append({"unit": dict(unit), "template": tpl["content"] if tpl else None})
        result = audit_mod.run_audit(entries, core_items)
        result["core_items"] = core_items
        return result
    finally:
        conn.close()


class AuditFixIn(BaseModel):
    unit_id: int
    item_key: str
    finding: str = ""


@app.post("/api/audit/fix")
def audit_fix(body: AuditFixIn):
    conn = db.connect()
    try:
        unit = get_unit_or_404(conn, body.unit_id)
        tpl = db.latest_template(conn, body.unit_id)
        if not tpl:
            raise HTTPException(status_code=400, detail="This unit has no template to fix yet.")
        core_items = db.get_core_items(conn)
        item = next((i for i in core_items if i["key"] == body.item_key), None)
        if not item:
            raise HTTPException(status_code=404, detail="Unknown core item")
        # Collect how other units cover this item, as reference
        examples = []
        for other in conn.execute("SELECT * FROM units WHERE id != ? ORDER BY name",
                                  (body.unit_id,)).fetchall():
            other_tpl = db.latest_template(conn, other["id"])
            if not other_tpl:
                continue
            status = audit_mod.check_item(other_tpl["content"], item)
            if status["status"] == "green":
                examples.append(f"[{other['name']}]\n{other_tpl['content']}")
            if len(examples) >= 2:
                break
        suggestion = claude_call(
            conn, "audit_fix",
            unit_name=unit["name"],
            finding=body.finding or f"Missing or weak: {item['label']}",
            item_label=item["label"],
            template=tpl["content"],
            examples="\n\n---\n\n".join(examples) or "(no reference examples available)")
        return {"suggestion": suggestion}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Feature 3: stays + dashboard
# ---------------------------------------------------------------------------

class StayIn(BaseModel):
    unit_id: int
    guest_name: str
    checkin_date: str
    checkout_date: str
    booking_message: str = ""
    confirmation_code: str = ""


class StayUpdate(BaseModel):
    guest_name: str | None = None
    checkin_date: str | None = None
    checkout_date: str | None = None
    booking_message: str | None = None
    guest_messages: str | None = None
    status: str | None = None
    draft_greeting: str | None = None
    confirmation_code: str | None = None


VALID_STATUSES = ("pending", "drafted", "reviewed", "sent")


def stay_dict(conn, row) -> dict:
    s = dict(row)
    unit = conn.execute("SELECT name FROM units WHERE id = ?", (row["unit_id"],)).fetchone()
    s["unit_name"] = unit["name"] if unit else "?"
    s["merge_values"] = json.loads(s.get("merge_values") or "{}")
    today = date.today()
    try:
        ci = date.fromisoformat(row["checkin_date"])
        s["days_until_checkin"] = (ci - today).days
        s["greeting_due"] = (ci - today).days <= 1 and row["status"] != "sent" and ci >= today
    except ValueError:
        s["days_until_checkin"] = None
        s["greeting_due"] = False
    return s


@app.get("/api/stays")
def list_stays():
    conn = db.connect()
    try:
        rows = conn.execute("SELECT * FROM stays ORDER BY checkin_date").fetchall()
        return [stay_dict(conn, r) for r in rows]
    finally:
        conn.close()


@app.post("/api/stays")
def create_stay(body: StayIn):
    ci = parsers.normalize_date(body.checkin_date)
    co = parsers.normalize_date(body.checkout_date)
    if not (body.guest_name.strip() and ci and co):
        raise HTTPException(status_code=400, detail="Guest name and valid check-in/checkout dates required")
    conn = db.connect()
    try:
        get_unit_or_404(conn, body.unit_id)
        cur = conn.execute(
            "INSERT INTO stays (unit_id, guest_name, checkin_date, checkout_date, booking_message, "
            "confirmation_code, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (body.unit_id, body.guest_name.strip(), ci, co, body.booking_message,
             body.confirmation_code.strip(), db.now()))
        conn.commit()
        return {"id": cur.lastrowid}
    finally:
        conn.close()


@app.put("/api/stays/{stay_id}")
def update_stay(stay_id: int, body: StayUpdate):
    conn = db.connect()
    try:
        get_stay_or_404(conn, stay_id)
        fields, values = [], []
        for col in ("guest_name", "booking_message", "guest_messages", "draft_greeting",
                    "confirmation_code"):
            val = getattr(body, col)
            if val is not None:
                fields.append(f"{col} = ?")
                values.append(val)
        for col in ("checkin_date", "checkout_date"):
            val = getattr(body, col)
            if val is not None:
                norm = parsers.normalize_date(val)
                if not norm:
                    raise HTTPException(status_code=400, detail=f"Invalid date: {val}")
                fields.append(f"{col} = ?")
                values.append(norm)
        if body.status is not None:
            if body.status not in VALID_STATUSES:
                raise HTTPException(status_code=400, detail=f"Status must be one of {VALID_STATUSES}")
            fields.append("status = ?")
            values.append(body.status)
        if fields:
            values.append(stay_id)
            conn.execute(f"UPDATE stays SET {', '.join(fields)} WHERE id = ?", values)
            conn.commit()
        return {"ok": True}
    finally:
        conn.close()


@app.delete("/api/stays/{stay_id}")
def delete_stay(stay_id: int):
    conn = db.connect()
    try:
        get_stay_or_404(conn, stay_id)
        conn.execute("DELETE FROM stays WHERE id = ?", (stay_id,))
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()


class StayImportIn(BaseModel):
    unit_id: int
    kind: str          # 'csv' | 'ics'
    content: str


@app.post("/api/stays/import")
def import_stays(body: StayImportIn):
    conn = db.connect()
    try:
        get_unit_or_404(conn, body.unit_id)
        try:
            if body.kind == "ics":
                stays = parsers.parse_ics(body.content)
            elif body.kind == "csv":
                stays = parsers.parse_stays_csv(body.content)
            else:
                raise HTTPException(status_code=400, detail="kind must be 'csv' or 'ics'")
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        if not stays:
            raise HTTPException(status_code=400, detail="No reservations found in the import.")
        added = skipped = 0
        for s in stays:
            dup = conn.execute(
                "SELECT id FROM stays WHERE unit_id=? AND guest_name=? AND checkin_date=?",
                (body.unit_id, s["guest_name"], s["checkin_date"])).fetchone()
            if dup:
                skipped += 1
                continue
            conn.execute(
                "INSERT INTO stays (unit_id, guest_name, checkin_date, checkout_date, "
                "booking_message, confirmation_code, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (body.unit_id, s["guest_name"], s["checkin_date"], s["checkout_date"],
                 s.get("booking_message", ""), s.get("confirmation_code", ""), db.now()))
            added += 1
        conn.commit()
        return {"added": added, "skipped_duplicates": skipped}
    finally:
        conn.close()


@app.get("/api/dashboard")
def dashboard():
    conn = db.connect()
    try:
        today = date.today()
        horizon = (today + timedelta(days=7)).isoformat()
        rows = conn.execute(
            "SELECT * FROM stays WHERE checkin_date >= ? AND checkin_date <= ? "
            "ORDER BY checkin_date", (today.isoformat(), horizon)).fetchall()
        stays = [stay_dict(conn, r) for r in rows]
        # Greeting-due first, then by date
        stays.sort(key=lambda s: (not s["greeting_due"], s["checkin_date"]))
        return {"today": today.isoformat(), "stays": stays,
                "due_count": sum(1 for s in stays if s["greeting_due"])}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Feature 4: greeting drafting + approval
# ---------------------------------------------------------------------------

def build_merge_values(conn, unit, stay) -> dict:
    amenities = json.loads(unit["amenities"] or "{}")
    first_name = stay["guest_name"].split()[0] if stay["guest_name"].strip() else "[MISSING]"

    def pretty_date(iso):
        try:
            return date.fromisoformat(iso).strftime("%A, %B %-d")
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


@app.post("/api/stays/{stay_id}/draft")
def draft_greeting(stay_id: int):
    conn = db.connect()
    try:
        stay = get_stay_or_404(conn, stay_id)
        unit = get_unit_or_404(conn, stay["unit_id"])
        tpl = db.latest_template(conn, unit["id"])
        if not tpl:
            raise HTTPException(
                status_code=400,
                detail=f"{unit['name']} has no template yet. Synthesize or bootstrap one first.")
        merge_values = build_merge_values(conn, unit, stay)
        draft = claude_call(
            conn, "draft_greeting",
            unit_name=unit["name"],
            unit_profile=unit["profile"] or "(no notes)",
            template=tpl["content"],
            guest_name=stay["guest_name"],
            checkin_date=merge_values["checkin_date"],
            checkout_date=merge_values["checkout_date"],
            merge_values=json.dumps(merge_values, indent=2),
            booking_message=stay["booking_message"] or "(empty)",
            host_signature=db.get_setting(conn, "host_signature", "— Your hosts"))
        conn.execute(
            "UPDATE stays SET draft_greeting = ?, merge_values = ?, status = 'drafted' WHERE id = ?",
            (draft, json.dumps(merge_values), stay_id))
        conn.commit()
        return {"draft": draft, "template": tpl["content"], "merge_values": merge_values,
                "missing_values": [k for k, v in merge_values.items() if v == "[MISSING]"]}
    finally:
        conn.close()


class ApproveIn(BaseModel):
    content: str


def airbnb_links(stay) -> dict:
    links = {"inbox": "https://www.airbnb.com/hosting/inbox"}
    code = (stay["confirmation_code"] or "").strip()
    if code:
        links["reservation"] = f"https://www.airbnb.com/hosting/reservations/details/{code}"
    return links


@app.post("/api/stays/{stay_id}/approve")
def approve_greeting(stay_id: int, body: ApproveIn):
    """Host reviewed and approved the draft. Store final text, return deep links.
    The frontend copies to clipboard; nothing is ever sent automatically."""
    if not body.content.strip():
        raise HTTPException(status_code=400, detail="Approved greeting is empty")
    conn = db.connect()
    try:
        stay = get_stay_or_404(conn, stay_id)
        conn.execute("UPDATE stays SET draft_greeting = ?, status = 'reviewed' WHERE id = ?",
                     (body.content, stay_id))
        conn.commit()
        return {"ok": True, "links": airbnb_links(stay)}
    finally:
        conn.close()


@app.post("/api/stays/{stay_id}/mark-sent")
def mark_sent(stay_id: int, body: ApproveIn):
    conn = db.connect()
    try:
        get_stay_or_404(conn, stay_id)
        conn.execute("INSERT INTO sent_greetings (stay_id, content, sent_at) VALUES (?, ?, ?)",
                     (stay_id, body.content, db.now()))
        conn.execute("UPDATE stays SET status = 'sent' WHERE id = ?", (stay_id,))
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()


@app.get("/api/stays/{stay_id}/sent")
def sent_log(stay_id: int):
    conn = db.connect()
    try:
        get_stay_or_404(conn, stay_id)
        rows = conn.execute(
            "SELECT * FROM sent_greetings WHERE stay_id = ? ORDER BY sent_at DESC", (stay_id,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@app.post("/api/stays/{stay_id}/assist")
def playwright_assist(stay_id: int, body: ApproveIn):
    """Optional browser-automation assist: opens the Airbnb inbox in a visible
    Chromium window and fills (never sends) the message box. Requires
    `pip install playwright && playwright install chromium` and an active
    Airbnb login in that browser profile. Falls back gracefully."""
    conn = db.connect()
    try:
        stay = get_stay_or_404(conn, stay_id)
    finally:
        conn.close()
    try:
        from .browser_assist import fill_airbnb_message
    except ImportError:
        raise HTTPException(status_code=501, detail="Playwright is not installed. "
                            "Use Copy + Open Airbnb instead (pip install playwright to enable).")
    try:
        result = fill_airbnb_message(stay["confirmation_code"], body.content)
        return {"ok": True, "detail": result}
    except Exception as e:
        raise HTTPException(
            status_code=502,
            detail=f"Browser assist failed ({e}). Use Copy + Open Airbnb instead.")


# ---------------------------------------------------------------------------
# Feature 5: review generator
# ---------------------------------------------------------------------------

HUMOR_LEVELS = [("subtle", "subtle"), ("medium", "medium"), ("full_whimsy", "full whimsy")]


@app.post("/api/stays/{stay_id}/reviews")
def generate_reviews(stay_id: int):
    conn = db.connect()
    try:
        stay = get_stay_or_404(conn, stay_id)
        unit = get_unit_or_404(conn, stay["unit_id"])
        # Pull any imported thread messages tagged with this guest, plus pasted notes
        guest_msgs = [stay["guest_messages"]] if stay["guest_messages"].strip() else []
        rows = conn.execute(
            "SELECT m.body FROM thread_messages m JOIN imports i ON i.id = m.import_id "
            "WHERE m.unit_id = ? AND m.sender = 'guest' AND i.guest_name != '' "
            "AND lower(i.guest_name) = lower(?) ORDER BY m.id",
            (unit["id"], stay["guest_name"])).fetchall()
        guest_msgs.extend(r["body"] for r in rows)
        if stay["booking_message"].strip():
            guest_msgs.insert(0, f"(booking message) {stay['booking_message']}")

        variants = []
        conn.execute("DELETE FROM reviews WHERE stay_id = ?", (stay_id,))
        for key, label in HUMOR_LEVELS:
            content = claude_call(
                conn, "generate_review", max_tokens=1000,
                unit_name=unit["name"],
                unit_profile=unit["profile"] or "(no notes)",
                guest_name=stay["guest_name"],
                checkin_date=stay["checkin_date"],
                checkout_date=stay["checkout_date"],
                guest_messages="\n---\n".join(guest_msgs) or "(no messages on file)",
                humor_level=label)
            conn.execute(
                "INSERT INTO reviews (stay_id, humor_level, content, created_at) VALUES (?, ?, ?, ?)",
                (stay_id, key, content, db.now()))
            variants.append({"humor_level": key, "content": content})
        conn.commit()
        return {"variants": variants}
    finally:
        conn.close()


@app.get("/api/stays/{stay_id}/reviews")
def list_reviews(stay_id: int):
    conn = db.connect()
    try:
        get_stay_or_404(conn, stay_id)
        rows = conn.execute(
            "SELECT * FROM reviews WHERE stay_id = ? ORDER BY id", (stay_id,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

class SettingsIn(BaseModel):
    host_signature: str | None = None
    anthropic_model: str | None = None
    default_checkin_time: str | None = None
    default_checkout_time: str | None = None
    core_items: list | None = None
    api_key: str | None = None


@app.get("/api/settings")
def get_settings():
    conn = db.connect()
    try:
        return {
            "host_signature": db.get_setting(conn, "host_signature"),
            "anthropic_model": db.get_setting(conn, "anthropic_model"),
            "default_checkin_time": db.get_setting(conn, "default_checkin_time"),
            "default_checkout_time": db.get_setting(conn, "default_checkout_time"),
            "core_items": db.get_core_items(conn),
            "api_key_set": bool(os.environ.get("ANTHROPIC_API_KEY", "").strip()),
        }
    finally:
        conn.close()


@app.put("/api/settings")
def update_settings(body: SettingsIn):
    conn = db.connect()
    try:
        for key in ("host_signature", "anthropic_model", "default_checkin_time", "default_checkout_time"):
            val = getattr(body, key)
            if val is not None:
                db.set_setting(conn, key, val)
        if body.core_items is not None:
            for item in body.core_items:
                if not (isinstance(item, dict) and item.get("key") and item.get("label")):
                    raise HTTPException(status_code=400,
                                        detail="Each core item needs at least 'key' and 'label'")
                item.setdefault("keywords", [])
                item.setdefault("merge_fields", [])
            db.set_setting(conn, "core_items", json.dumps(body.core_items, indent=2))
        if body.api_key is not None and body.api_key.strip():
            set_api_key(body.api_key)
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Static frontend
# ---------------------------------------------------------------------------

@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
