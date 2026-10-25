"""One-off debug helper: finds a stay by guest name (partial match) and
unit name (partial match), then prints the raw stay/unit data plus the
ACTUAL merge values the app would compute for it (imports queue.py
directly, so this reflects real app logic, not a re-guess of it).

Usage: python check_guest.py "Pamela" "Cosmo"
(unit name arg is optional — omit to search all units)
"""
import json
import os
import sys

sys.path.insert(0, ".")
from app import db, queue  # noqa: E402

print(f"DB_PATH in use: {db.DB_PATH}")
print(f"GREETING_STUDIO_DB env override: {os.environ.get('GREETING_STUDIO_DB')!r}")
print(f"file exists: {os.path.exists(db.DB_PATH)}")

guest_query = (sys.argv[1] if len(sys.argv) > 1 else "").lower()
unit_query = (sys.argv[2] if len(sys.argv) > 2 else "").lower()

conn = db.connect()
total_stays = conn.execute("SELECT COUNT(*) c FROM stays").fetchone()["c"]
total_units = conn.execute("SELECT COUNT(*) c FROM units").fetchone()["c"]
print(f"total stays in DB: {total_stays}, total units: {total_units}")
print("unit names:", [r["name"] for r in conn.execute("SELECT name FROM units").fetchall()])
print()

rows = conn.execute(
    "SELECT s.*, u.name AS unit_name FROM stays s JOIN units u ON u.id = s.unit_id "
    "WHERE lower(s.guest_name) LIKE ? AND lower(u.name) LIKE ?",
    (f"%{guest_query}%", f"%{unit_query}%")
).fetchall()

if not rows:
    print("No matching stay found.")
    sys.exit(0)

for stay in rows:
    unit = conn.execute("SELECT * FROM units WHERE id = ?", (stay["unit_id"],)).fetchone()
    print(f"=== Stay id={stay['id']} — {stay['guest_name']!r} @ {stay['unit_name']} ===")
    print(f"checkin={stay['checkin_date']} checkout={stay['checkout_date']} "
          f"status={stay['status']} confirmation_code={stay['confirmation_code']!r} "
          f"phone_last4={stay['phone_last4']!r}")
    print(f"unit amenities: {unit['amenities']}")
    print(f"unit ics_url set: {bool((unit['ics_url'] or '').strip())}")

    tpl = db.latest_template(conn, unit["id"])
    print(f"has template: {tpl is not None}" + (f" (v{tpl['version']})" if tpl else ""))

    merge_values = queue.build_merge_values(conn, unit, stay)
    print("computed merge_values:")
    print(json.dumps(merge_values, indent=2))

    if tpl:
        missing = queue.missing_fields(tpl["content"], merge_values)
        print(f"missing_fields (would block drafting): {missing}")
    print()

conn.close()
