"""One-off debug helper: dumps every stay checking in today/tomorrow, with the
raw fields the dashboard doesn't show (id, unit_id, confirmation_code), so
duplicate rows can be told apart. Safe to delete after use."""
import sqlite3
from datetime import date, timedelta

today = date.today().isoformat()
tomorrow = (date.today() + timedelta(days=1)).isoformat()
print(f"Looking for checkin_date in ({today!r}, {tomorrow!r})")

conn = sqlite3.connect("greeting_studio.db")
conn.row_factory = sqlite3.Row
rows = conn.execute(
    "SELECT s.id, s.unit_id, u.name AS unit_name, s.guest_name, s.checkin_date, "
    "s.checkout_date, s.confirmation_code, s.phone_last4, s.status, s.created_at "
    "FROM stays s JOIN units u ON u.id = s.unit_id "
    "WHERE s.checkin_date IN (?, ?) "
    "ORDER BY s.checkin_date, s.id",
    (today, tomorrow),
).fetchall()
if not rows:
    print("No rows found. Printing every stay in the table instead:")
    rows = conn.execute(
        "SELECT s.id, s.unit_id, u.name AS unit_name, s.guest_name, s.checkin_date, "
        "s.checkout_date, s.confirmation_code, s.phone_last4, s.status, s.created_at "
        "FROM stays s JOIN units u ON u.id = s.unit_id ORDER BY s.checkin_date, s.id"
    ).fetchall()
for r in rows:
    print(dict(r))
conn.close()
