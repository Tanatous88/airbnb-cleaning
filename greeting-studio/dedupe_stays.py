"""One-off cleanup: merges stay rows that share the same Airbnb confirmation
code (the same real reservation split across multiple DB rows by a matching
bug — see queue._upsert_stay). For each group of duplicates, keeps one
"keeper" row and re-points any sent_greetings/reviews/daily_queue rows from
the duplicates onto it before deleting the duplicates.

Dry run by default — prints what it would do without changing anything.
Run again with --apply to actually make the changes.

    python dedupe_stays.py            # preview
    python dedupe_stays.py --apply    # actually merge and delete
"""
import sqlite3
import sys

UNKNOWN_GUEST = "Guest (see Airbnb)"
APPLY = "--apply" in sys.argv

conn = sqlite3.connect("greeting_studio.db")
conn.row_factory = sqlite3.Row
conn.execute("PRAGMA foreign_keys = OFF")  # we reparent children ourselves, in order

codes = [r["confirmation_code"] for r in conn.execute(
    "SELECT confirmation_code FROM stays WHERE confirmation_code != '' "
    "GROUP BY confirmation_code HAVING COUNT(*) > 1"
).fetchall()]

if not codes:
    print("No duplicate confirmation codes found. Nothing to do.")
    sys.exit(0)

print(f"Found {len(codes)} confirmation code(s) with duplicate stay rows.\n")

for code in codes:
    rows = conn.execute(
        "SELECT s.*, u.name AS unit_name FROM stays s JOIN units u ON u.id = s.unit_id "
        "WHERE s.confirmation_code = ? ORDER BY s.id", (code,)
    ).fetchall()

    def score(r):
        sent = conn.execute("SELECT COUNT(*) c FROM sent_greetings WHERE stay_id = ?",
                             (r["id"],)).fetchone()["c"]
        rev = conn.execute("SELECT COUNT(*) c FROM reviews WHERE stay_id = ?",
                            (r["id"],)).fetchone()["c"]
        named = 1 if r["guest_name"] != UNKNOWN_GUEST else 0
        # Prefer: has a real name, has sent greetings, has reviews, is not
        # 'pending', then lowest id (oldest / most likely to be referenced).
        return (named, sent, rev, 0 if r["status"] == "pending" else 1, -r["id"])

    keeper = max(rows, key=score)
    dups = [r for r in rows if r["id"] != keeper["id"]]

    print(f"Code {code} — {keeper['unit_name']}, checkin {keeper['checkin_date']}:")
    print(f"  KEEP    id={keeper['id']}  guest={keeper['guest_name']!r}  "
          f"status={keeper['status']}")
    for d in dups:
        print(f"  REMOVE  id={d['id']}  guest={d['guest_name']!r}  status={d['status']}")

    if APPLY:
        for d in dups:
            for table in ("sent_greetings", "reviews", "daily_queue"):
                conn.execute(f"UPDATE {table} SET stay_id = ? WHERE stay_id = ?",
                             (keeper["id"], d["id"]))
            conn.execute("DELETE FROM stays WHERE id = ?", (d["id"],))
    print()

if APPLY:
    conn.commit()
    print("Done — duplicates merged and removed.")
else:
    print("This was a DRY RUN — nothing was changed.")
    print("Review the plan above, then run:  python dedupe_stays.py --apply")

conn.close()
