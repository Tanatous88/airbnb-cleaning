"""One-off debug helper: for each unit, shows how often phone_last4 (used
to auto-fill the door code) actually came through vs. came back blank,
across all stays with a confirmation code (meaning they went through
calendar sync). Also prints each unit's configured ics_url so a wrong/
missing feed can be ruled out. Safe to delete after use."""
import sqlite3

conn = sqlite3.connect("greeting_studio.db")
conn.row_factory = sqlite3.Row

units = conn.execute("SELECT id, name, ics_url FROM units ORDER BY name").fetchall()
for u in units:
    rows = conn.execute(
        "SELECT phone_last4, confirmation_code FROM stays "
        "WHERE unit_id = ? AND confirmation_code != ''", (u["id"],)
    ).fetchall()
    total = len(rows)
    missing = sum(1 for r in rows if not (r["phone_last4"] or "").strip())
    print(f"{u['name']}:")
    print(f"  ics_url set: {bool((u['ics_url'] or '').strip())}")
    print(f"  stays with a confirmation code: {total}, missing phone_last4: {missing}"
          + (f" ({100*missing//total}%)" if total else ""))

conn.close()
