"""Calendar-sync matching and duplicate merging."""
import sqlite3

from app import db, maintenance, queue
from conftest import add_stay, day, unit_id


def test_upsert_matches_by_code_and_absorbs_placeholder(conn):
    uid = unit_id(conn, "Three Forks Garage")
    raw = {"guest_name": queue.UNKNOWN_GUEST, "checkin_date": day(3), "checkout_date": day(5),
           "confirmation_code": "HM1", "phone_last4": "6854"}
    named = dict(raw, guest_name="Jason Luft", phone_last4="")
    assert queue._upsert_stay(conn, uid, raw) == "added"
    assert queue._upsert_stay(conn, uid, named) == "updated"
    assert queue._upsert_stay(conn, uid, raw) == "unchanged"
    rows = conn.execute("SELECT guest_name, phone_last4 FROM stays").fetchall()
    assert [(r["guest_name"], r["phone_last4"]) for r in rows] == [("Jason Luft", "6854")]


def _make_duplicates(conn):
    """Recreate the real Jason Luft case: three rows for one reservation, each
    with its own queue card on the same day. The unique indexes have to be
    dropped first — exactly the state a DB was in before they were added."""
    conn.execute("DROP INDEX IF EXISTS idx_stays_confirmation_code")
    conn.execute("DROP INDEX IF EXISTS idx_daily_queue_stay_checkin")
    ids = [add_stay(conn, "Three Forks Garage", name, 0, 2, code="HM4AESQ4HE", phone="6854")
           for name in ("Jason Luft", queue.UNKNOWN_GUEST, queue.UNKNOWN_GUEST)]
    for i, sid in enumerate(ids):
        conn.execute(
            "INSERT INTO daily_queue (stay_id, unit_id, guest_name, checkin_date, status, "
            "personalization_source, created_at) VALUES (?, ?, 'x', ?, 'ready', ?, ?)",
            (sid, unit_id(conn, "Three Forks Garage"), day(0),
             "scraped" if i == 2 else "stored", db.now()))
    conn.commit()
    return ids


def test_merge_duplicates_keeps_named_row_and_moves_cards_without_clash(conn):
    ids = _make_duplicates(conn)
    plan = maintenance.plan_stay_dedupe(conn)
    assert plan[0]["keep"]["id"] == ids[0]  # the row with the real name
    result = maintenance.merge_duplicates(conn)
    assert result["stays_removed"] == 2
    assert conn.execute("SELECT COUNT(*) c FROM stays").fetchone()["c"] == 1
    cards = conn.execute("SELECT stay_id FROM daily_queue").fetchall()
    assert [c["stay_id"] for c in cards] == [ids[0]]  # one card, on the keeper
    assert result["safeguards"] == {"stays_unique": True, "queue_unique": True}


def test_safeguard_blocks_new_duplicate_codes(conn):
    add_stay(conn, "The Studio", "A B", 1, 2, code="HMX")
    try:
        add_stay(conn, "The Studio", "C D", 1, 2, code="HMX")
        raise AssertionError("duplicate confirmation code was allowed")
    except sqlite3.IntegrityError:
        pass
