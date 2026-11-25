"""Merge stay rows that share an Airbnb confirmation code (the same real
reservation split across several rows). The Health page in the app does the
same thing with one click; this script is kept for command-line use.

    python dedupe_stays.py            # preview
    python dedupe_stays.py --apply    # actually merge and delete
"""
import sys

sys.path.insert(0, ".")
from app import db, maintenance  # noqa: E402

conn = db.connect()
plan = maintenance.plan_stay_dedupe(conn)
if not plan:
    print("No duplicate confirmation codes found. Nothing to do.")
    sys.exit(0)

for g in plan:
    print(f"Code {g['code']} — {g['unit_name']}, checkin {g['checkin_date']}:")
    print(f"  KEEP    id={g['keep']['id']}  guest={g['keep']['guest_name']!r}")
    for r in g["remove"]:
        print(f"  REMOVE  id={r['id']}  guest={r['guest_name']!r}")

if "--apply" in sys.argv:
    n = maintenance.apply_stay_dedupe(conn, plan)
    conn.commit()
    print(f"\nDone — {n} duplicate row(s) merged and removed.")
else:
    print("\nThis was a DRY RUN — nothing was changed. Run with --apply to merge.")
conn.close()
