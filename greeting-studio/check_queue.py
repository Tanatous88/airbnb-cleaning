"""One-off debug helper: dumps daily_queue rows for today, to check for
duplicate queue cards (a stay_id appearing more than once for one date).
Safe to delete after use."""
import sqlite3
from datetime import date

today = date.today().isoformat()
print(f"Today (local): {today!r}")

conn = sqlite3.connect("greeting_studio.db")
conn.row_factory = sqlite3.Row
rows = conn.execute(
    "SELECT id, stay_id, unit_id, guest_name, checkin_date, status, "
    "personalization_source, created_at FROM daily_queue "
    "WHERE checkin_date = ? ORDER BY stay_id, id", (today,)
).fetchall()
for r in rows:
    print(dict(r))

print("\nDuplicate stay_ids in today's queue:")
seen = {}
for r in rows:
    seen.setdefault(r["stay_id"], []).append(r["id"])
for stay_id, ids in seen.items():
    if len(ids) > 1:
        print(f"  stay_id={stay_id} appears {len(ids)} times: daily_queue ids {ids}")
conn.close()
