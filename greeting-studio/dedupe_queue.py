"""Merge daily_queue rows that duplicate the same (stay, day). The Health page
in the app does the same thing with one click; this script is kept for
command-line use.

    python dedupe_queue.py            # preview
    python dedupe_queue.py --apply    # actually merge and delete
"""
import sys

sys.path.insert(0, ".")
from app import db, maintenance  # noqa: E402

conn = db.connect()
plan = maintenance.plan_queue_dedupe(conn)
if not plan:
    print("No duplicate queue rows found. Nothing to do.")
    sys.exit(0)

for g in plan:
    print(f"stay_id={g['stay_id']}  {g['guest_name']}  day {g['day']}: "
          f"KEEP {g['keep']}, REMOVE {g['remove']}")

if "--apply" in sys.argv:
    n = maintenance.apply_queue_dedupe(conn, plan)
    conn.commit()
    print(f"\nDone — {n} duplicate queue row(s) removed.")
else:
    print("\nThis was a DRY RUN — nothing was changed. Run with --apply to merge.")
conn.close()
