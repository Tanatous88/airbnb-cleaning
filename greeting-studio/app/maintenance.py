"""Health checks and database upkeep behind the Health page (and the
dedupe_*.py scripts, which are now thin wrappers around this)."""
import json
import os
import subprocess
from datetime import date, timedelta

from . import db
from .queue import build_merge_values, is_placeholder_name, missing_fields

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ATTENTION_DAYS = 14


# ---------------------------------------------------------------------------
# Duplicate merging
# ---------------------------------------------------------------------------

def plan_stay_dedupe(conn) -> list:
    """Groups of stay rows sharing one Airbnb confirmation code (the same real
    reservation). Keeper = has a real name, then sent greetings, then reviews,
    then not 'pending', then oldest."""
    codes = [r["confirmation_code"] for r in conn.execute(
        "SELECT confirmation_code FROM stays WHERE confirmation_code != '' "
        "GROUP BY confirmation_code HAVING COUNT(*) > 1").fetchall()]
    plan = []
    for code in codes:
        rows = conn.execute(
            "SELECT s.*, u.name AS unit_name FROM stays s JOIN units u ON u.id = s.unit_id "
            "WHERE s.confirmation_code = ? ORDER BY s.id", (code,)).fetchall()

        def score(r):
            sent = conn.execute("SELECT COUNT(*) c FROM sent_greetings WHERE stay_id = ?",
                                (r["id"],)).fetchone()["c"]
            rev = conn.execute("SELECT COUNT(*) c FROM reviews WHERE stay_id = ?",
                               (r["id"],)).fetchone()["c"]
            return (0 if is_placeholder_name(r["guest_name"]) else 1, sent, rev,
                    0 if r["status"] == "pending" else 1, -r["id"])

        keeper = max(rows, key=score)
        plan.append({"code": code, "unit_name": keeper["unit_name"],
                     "checkin_date": keeper["checkin_date"],
                     "keep": {"id": keeper["id"], "guest_name": keeper["guest_name"]},
                     "remove": [{"id": r["id"], "guest_name": r["guest_name"]}
                                for r in rows if r["id"] != keeper["id"]]})
    return plan


def apply_stay_dedupe(conn, plan: list) -> int:
    removed = 0
    for group in plan:
        keep = group["keep"]["id"]
        keeper_has_reviews = conn.execute(
            "SELECT 1 FROM reviews WHERE stay_id = ?", (keep,)).fetchone() is not None
        for dup in group["remove"]:
            dup_id = dup["id"]
            conn.execute("UPDATE sent_greetings SET stay_id = ? WHERE stay_id = ?", (keep, dup_id))
            if keeper_has_reviews:
                conn.execute("DELETE FROM reviews WHERE stay_id = ?", (dup_id,))
            else:
                conn.execute("UPDATE reviews SET stay_id = ? WHERE stay_id = ?", (keep, dup_id))
                keeper_has_reviews = conn.execute(
                    "SELECT 1 FROM reviews WHERE stay_id = ?", (keep,)).fetchone() is not None
            # A queue row can only move if the keeper has none for that day —
            # otherwise it's a duplicate of the keeper's own row.
            for q in conn.execute("SELECT id, checkin_date FROM daily_queue WHERE stay_id = ?",
                                  (dup_id,)).fetchall():
                clash = conn.execute(
                    "SELECT 1 FROM daily_queue WHERE stay_id = ? AND checkin_date = ?",
                    (keep, q["checkin_date"])).fetchone()
                if clash:
                    conn.execute("DELETE FROM daily_queue WHERE id = ?", (q["id"],))
                else:
                    conn.execute("UPDATE daily_queue SET stay_id = ? WHERE id = ?", (keep, q["id"]))
            conn.execute("DELETE FROM stays WHERE id = ?", (dup_id,))
            removed += 1
    return removed


def plan_queue_dedupe(conn) -> list:
    """Groups of queue rows for the same (stay, day). Keeper = sent (never
    discard a real send record), then live-scraped personalization, then newest."""
    groups = conn.execute(
        "SELECT stay_id, checkin_date FROM daily_queue "
        "GROUP BY stay_id, checkin_date HAVING COUNT(*) > 1").fetchall()
    plan = []
    for g in groups:
        rows = conn.execute(
            "SELECT * FROM daily_queue WHERE stay_id = ? AND checkin_date = ? ORDER BY id",
            (g["stay_id"], g["checkin_date"])).fetchall()

        def score(r):
            return (1 if r["status"] == "sent" else 0,
                    {"scraped": 2, "stored": 1}.get(r["personalization_source"], 0), r["id"])

        keeper = max(rows, key=score)
        plan.append({"stay_id": g["stay_id"], "day": g["checkin_date"],
                     "guest_name": keeper["guest_name"], "keep": keeper["id"],
                     "remove": [r["id"] for r in rows if r["id"] != keeper["id"]]})
    return plan


def apply_queue_dedupe(conn, plan: list) -> int:
    removed = 0
    for group in plan:
        for qid in group["remove"]:
            conn.execute("DELETE FROM daily_queue WHERE id = ?", (qid,))
            removed += 1
    return removed


def merge_duplicates(conn) -> dict:
    """Merge every duplicate, then switch on the DB's duplicate safeguards
    (unique indexes), which can't be created while duplicates exist."""
    stays_removed = apply_stay_dedupe(conn, plan_stay_dedupe(conn))
    queue_removed = apply_queue_dedupe(conn, plan_queue_dedupe(conn))
    conn.commit()
    db.init_db()  # retries creating the unique indexes
    return {"stays_removed": stays_removed, "queue_removed": queue_removed,
            "safeguards": safeguards(conn)}


def safeguards(conn) -> dict:
    names = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'index'").fetchall()}
    return {"stays_unique": "idx_stays_confirmation_code" in names,
            "queue_unique": "idx_daily_queue_stay_checkin" in names}


# ---------------------------------------------------------------------------
# Health report
# ---------------------------------------------------------------------------

def uncommitted_changes():
    """Files in the app folder that differ from git — e.g. another Claude
    session edited code here without committing. None if git isn't usable."""
    try:
        out = subprocess.run(["git", "status", "--porcelain", "--", "."], cwd=BASE_DIR,
                             capture_output=True, text=True, timeout=10)
    except Exception:
        return None
    if out.returncode != 0:
        return None
    return [line.strip() for line in out.stdout.splitlines() if line.strip()]


def _json_setting(conn, key):
    raw = db.get_setting(conn, key, "")
    try:
        return json.loads(raw) if raw else None
    except json.JSONDecodeError:
        return None


def _stay_problems(conn, stay, unit, tpl) -> list:
    problems = []
    if tpl is None:
        problems.append(f"{unit['name']} has no greeting template")
    else:
        missing = missing_fields(tpl["content"], build_merge_values(conn, unit, stay))
        if "guest_first_name" in missing:
            problems.append("guest name unknown")
        if "door_code" in missing:
            problems.append("door code missing (no phone number from Airbnb)")
        other = [f for f in missing if f not in ("guest_first_name", "door_code")]
        if other:
            problems.append("missing " + ", ".join(other))
    if not (stay["confirmation_code"] or "").strip():
        problems.append("no Airbnb confirmation code (can't open the thread or send)")
    return problems


def health_report(conn, started_at: str) -> dict:
    today = date.today()
    units = []
    unit_rows = conn.execute("SELECT * FROM units ORDER BY name").fetchall()
    templates = {u["id"]: db.latest_template(conn, u["id"]) for u in unit_rows}
    for u in unit_rows:
        amen = json.loads(u["amenities"] or "{}")
        coded = conn.execute(
            "SELECT COUNT(*) c, SUM(CASE WHEN TRIM(COALESCE(phone_last4, '')) = '' "
            "THEN 1 ELSE 0 END) m FROM stays WHERE unit_id = ? AND confirmation_code != ''",
            (u["id"],)).fetchone()
        tpl = templates[u["id"]]
        units.append({
            "id": u["id"], "name": u["name"],
            "feed_set": bool((u["ics_url"] or "").strip()),
            "template_version": tpl["version"] if tpl else None,
            "stays_with_code": coded["c"] or 0,
            "phone_missing": coded["m"] or 0,
            "door_code_fallback": bool(amen.get("door_code_fallback") or amen.get("door_code")),
        })

    attention = []
    upcoming = conn.execute(
        "SELECT * FROM stays WHERE checkin_date >= ? AND checkin_date <= ? ORDER BY checkin_date",
        (today.isoformat(), (today + timedelta(days=ATTENTION_DAYS)).isoformat())).fetchall()
    by_id = {u["id"]: u for u in unit_rows}
    for s in upcoming:
        unit = by_id.get(s["unit_id"])
        if unit is None:
            continue
        problems = _stay_problems(conn, s, unit, templates[unit["id"]])
        if problems:
            attention.append({"stay_id": s["id"], "unit_name": unit["name"],
                              "guest_name": s["guest_name"], "checkin_date": s["checkin_date"],
                              "problems": problems})

    return {
        "app": {"started_at": started_at, "uncommitted_changes": uncommitted_changes()},
        "calendar": {"last_sync": db.get_setting(conn, "calendar_last_sync", ""),
                     "summary": _json_setting(conn, "calendar_last_sync_summary"),
                     "hosting_feed_set": bool(db.get_setting(conn, "hosting_ics_url", "").strip())},
        "queue": {"last_run": db.get_setting(conn, "queue_last_run", ""),
                  "summary": _json_setting(conn, "queue_last_summary")},
        "reviews": {"last_auto_run": db.get_setting(conn, "reviews_last_auto_run", "")},
        "home_assistant": {"enabled": bool(db.get_setting(conn, "ha_url", "").strip()),
                           "last_sent": db.get_setting(conn, "ha_last_sent", ""),
                           "last_error": db.get_setting(conn, "ha_last_error", "")},
        "summary_email": {"enabled": db.get_setting(conn, "summary_email_enabled", "0") == "1",
                          "last_sent": db.get_setting(conn, "summary_email_last_sent", ""),
                          "last_error": db.get_setting(conn, "summary_email_last_error", "")},
        "airbnb_session_expired": db.get_setting(conn, "airbnb_session_expired", "") == "1",
        "units": units,
        "attention": attention,
        "duplicates": {"stays": len(plan_stay_dedupe(conn)), "queue": len(plan_queue_dedupe(conn))},
        "safeguards": safeguards(conn),
    }
