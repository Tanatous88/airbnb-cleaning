"""One-off debug helper: prints the hosting_ics_url config, listing_aliases,
and the last calendar sync's error list, so a missing guest name can be
traced back to a specific cause. Safe to delete after use."""
import json
import sqlite3

conn = sqlite3.connect("greeting_studio.db")
conn.row_factory = sqlite3.Row


def setting(key, default=""):
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


hosting_url = setting("hosting_ics_url", "")
print(f"hosting_ics_url configured: {bool(hosting_url.strip())}")
if hosting_url.strip():
    print(f"  {hosting_url}")

print("\nlisting_aliases:")
try:
    aliases = json.loads(setting("listing_aliases", "{}"))
    for k, v in aliases.items():
        print(f"  {k!r} -> {v!r}")
except json.JSONDecodeError:
    print("  (invalid JSON in listing_aliases setting)")

print(f"\ncalendar_last_sync: {setting('calendar_last_sync', '(never)')}")

summary_raw = setting("calendar_last_sync_summary", "")
print("\nLast sync summary:")
if summary_raw:
    summary = json.loads(summary_raw)
    print(f"  added={summary.get('added')} updated={summary.get('updated')} "
          f"unchanged={summary.get('unchanged')}")
    errors = summary.get("errors", [])
    if errors:
        print(f"  {len(errors)} error(s):")
        for e in errors:
            print(f"    - {e}")
    else:
        print("  no errors")
else:
    print("  (no sync has run yet)")

conn.close()
