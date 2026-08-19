"""One-off cleanup: merges daily_queue rows that duplicate the same
(stay_id, checkin_date) — caused by a race between overlapping queue-build
triggers (cron, startup catch-up, "Rebuild queue now"). Keeps the best row
per group and deletes the rest.

Dry run by default — prints what it would do without changing anything.
Run again with --apply to actually make the changes.

    python dedupe_queue.py            # preview
    python dedupe_queue.py --apply    # actually merge and delete
"""
import sqlite3
import sys

APPLY = "--apply" in sys.argv

conn = sqlite3.connect("greeting_studio.db")
conn.row_factory = sqlite3.Row
conn.execute("PRAGMA foreign_keys = OFF")

groups = conn.execute(
    "SELECT stay_id, checkin_date, COUNT(*) c FROM daily_queue "
    "GROUP BY stay_id, checkin_date HAVING COUNT(*) > 1"
).fetchall()

if not groups:
    print("No duplicate queue rows found. Nothing to do.")
    sys.exit(0)

print(f"Found {len(groups)} (stay, day) pair(s) with duplicate queue rows.\n")

for g in groups:
    rows = conn.execute(
        "SELECT * FROM daily_queue WHERE stay_id = ? AND checkin_date = ? ORDER BY id",
        (g["stay_id"], g["checkin_date"])
    ).fetchall()

    def score(r):
        # Prefer: actually sent (never discard a real send record), then
        # live-scraped personalization over stored/none, then most recent.
        sent = 1 if r["status"] == "sent" else 0
        src = {"scraped": 2, "stored": 1}.get(r["personalization_source"], 0)
        return (sent, src, r["id"])

    keeper = max(rows, key=score)
    dups = [r for r in rows if r["id"] != keeper["id"]]

    print(f"stay_id={g['stay_id']}  {keeper['guest_name']}  checkin {g['checkin_date']}:")
    print(f"  KEEP    daily_queue id={keeper['id']}  status={keeper['status']}  "
          f"source={keeper['personalization_source']!r}")
    for d in dups:
        print(f"  REMOVE  daily_queue id={d['id']}  status={d['status']}  "
              f"source={d['personalization_source']!r}")

    if APPLY:
        for d in dups:
            conn.execute("DELETE FROM daily_queue WHERE id = ?", (d["id"],))
    print()

if APPLY:
    conn.commit()
    print("Done — duplicate queue rows merged and removed.")
else:
    print("This was a DRY RUN — nothing was changed.")
    print("Review the plan above, then run:  python dedupe_queue.py --apply")

conn.close()
