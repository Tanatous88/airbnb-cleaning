"""Merge values, local rendering, and the weekend-guide rule."""
from datetime import date, timedelta

from app import queue
from conftest import add_stay, set_amenities

TEMPLATE = "Hi {{guest_first_name}}, your code is {{door_code}}.\n\n{{weekend_guide}}\nBye"


def _stay(conn, stay_id):
    return conn.execute("SELECT * FROM stays WHERE id = ?", (stay_id,)).fetchone()


def _unit(conn, name):
    return conn.execute("SELECT * FROM units WHERE name = ?", (name,)).fetchone()


def test_door_code_priority(conn):
    set_amenities(conn, "The Cosmopolitan", {"door_code_fallback": "listed in your Airbnb app"})
    unit = _unit(conn, "The Cosmopolitan")
    with_phone = _stay(conn, add_stay(conn, "The Cosmopolitan", "Pam Lee", 1, 3, phone="1575"))
    without = _stay(conn, add_stay(conn, "The Cosmopolitan", "Al Roe", 5, 7))
    assert queue.build_merge_values(conn, unit, with_phone)["door_code"] == "1575"
    assert queue.build_merge_values(conn, unit, without)["door_code"] == "listed in your Airbnb app"


def test_door_code_missing_without_fallback_blocks(conn):
    unit = _unit(conn, "The Studio")
    stay = _stay(conn, add_stay(conn, "The Studio", "Al Roe", 5, 7))
    values = queue.build_merge_values(conn, unit, stay)
    assert values["door_code"] == "[MISSING]"
    assert queue.missing_fields(TEMPLATE, values) == ["door_code"]


def test_placeholder_name_is_missing(conn):
    unit = _unit(conn, "The Studio")
    stay = _stay(conn, add_stay(conn, "The Studio", queue.UNKNOWN_GUEST, 1, 2, phone="1111"))
    assert queue.build_merge_values(conn, unit, stay)["guest_first_name"] == "[MISSING]"


def test_weekend_guide_rules():
    def nxt(weekday):  # next date with that weekday (0=Mon)
        d = date.today() + timedelta(days=1)
        while d.weekday() != weekday:
            d += timedelta(days=1)
        return d

    fri, mon = nxt(4), nxt(0)
    assert queue._weekend_guide_blurb(fri.isoformat(), (fri + timedelta(1)).isoformat())
    # Monday -> Thursday: no weekend night
    assert queue._weekend_guide_blurb(mon.isoformat(), (mon + timedelta(3)).isoformat()) == ""
    # Monday through the following Monday crosses a weekend
    assert queue._weekend_guide_blurb(mon.isoformat(), (mon + timedelta(7)).isoformat())


def test_render_local_collapses_blank_line_from_empty_field():
    out = queue.render_local(TEMPLATE, {"guest_first_name": "Pam", "door_code": "1575",
                                        "weekend_guide": ""})
    assert out == "Hi Pam, your code is 1575.\n\nBye"
