"""The morning queue: arrivals, name autofill, returning guests, mid-stay notes."""
import os
import pytest

from app import browser_assist, db, queue
from conftest import FakeClaude, add_stay, set_amenities, set_template

TEMPLATE = "Hi {{guest_first_name}}! Door code: {{door_code}}."


@pytest.fixture
def env(monkeypatch):
    """Calendar sync, the Airbnb thread and Claude all faked."""
    fake = FakeClaude()
    thread = {"messages": [], "guest_name": ""}
    monkeypatch.setattr(queue, "sync_calendars", lambda: {"errors": []})
    monkeypatch.setattr(queue, "_notify", lambda summary: None)
    monkeypatch.setattr(queue, "ask_claude", fake)
    monkeypatch.setattr(browser_assist, "read_thread", lambda code, max_messages=30: thread)
    return {"claude": fake, "thread": thread}


def _card(conn, stay_id, kind="arrival"):
    return conn.execute("SELECT * FROM daily_queue WHERE stay_id = ? AND kind = ?",
                        (stay_id, kind)).fetchone()


def test_missing_name_is_filled_from_thread(conn, env):
    set_template(conn, "The Studio", TEMPLATE)
    sid = add_stay(conn, "The Studio", queue.UNKNOWN_GUEST, 0, 2, code="HMNAME", phone="4321")
    env["thread"]["guest_name"] = "Melissa"
    queue.build_queue_for_today()
    card = _card(conn, sid)
    assert card["status"] == "ready"
    assert card["message"] == "Hi Melissa! Door code: 4321."
    assert conn.execute("SELECT guest_name FROM stays WHERE id = ?", (sid,)).fetchone()[0] == "Melissa"
    assert env["claude"].prompts == []  # no guest messages -> no Claude call


def test_no_name_anywhere_needs_setup(conn, env):
    set_template(conn, "The Studio", TEMPLATE)
    sid = add_stay(conn, "The Studio", queue.UNKNOWN_GUEST, 0, 2, code="HMNONE", phone="4321")
    summary = queue.build_queue_for_today()
    card = _card(conn, sid)
    assert card["status"] == "needs_setup"
    assert "guest name unknown" in card["error"]
    assert summary["needs_setup"] == 1


def test_scrape_failure_is_explained(conn, env, monkeypatch):
    def boom(code, max_messages=30):
        raise RuntimeError("Airbnb session expired — log in again")
    monkeypatch.setattr(browser_assist, "read_thread", boom)
    set_template(conn, "The Studio", TEMPLATE)
    sid = add_stay(conn, "The Studio", queue.UNKNOWN_GUEST, 0, 2, code="HMFAIL", phone="4321")
    queue.build_queue_for_today()
    assert "session expired" in _card(conn, sid)["error"]


def test_airbnb_lock_unit_uses_fallback_code(conn, env):
    set_template(conn, "The Cosmopolitan", TEMPLATE)
    set_amenities(conn, "The Cosmopolitan", {"door_code_fallback": "listed in your Airbnb app"})
    sid = add_stay(conn, "The Cosmopolitan", "Dana Fox", 0, 2)
    queue.build_queue_for_today()
    card = _card(conn, sid)
    assert card["status"] == "ready"
    assert card["message"] == "Hi Dana! Door code: listed in your Airbnb app."


def test_returning_guest_reaches_the_prompt(conn, env):
    set_template(conn, "The Studio", TEMPLATE)
    add_stay(conn, "The Cottage", "Dana Fox", -60, -57)
    sid = add_stay(conn, "The Studio", "Dana Fox", 0, 2, phone="1234")
    queue.build_queue_for_today()
    assert _card(conn, sid)["message"] == "DRAFT #1"
    prompt = env["claude"].prompts[0]
    assert "RETURNING GUEST" in prompt and "The Cottage" in prompt


def test_first_name_alone_is_not_a_returning_guest(conn):
    add_stay(conn, "The Cottage", "Robert Smith", -60, -57, phone="1111")
    sid = add_stay(conn, "The Studio", "Robert Jones", 0, 2, phone="2222")
    stay = conn.execute("SELECT * FROM stays WHERE id = ?", (sid,)).fetchone()
    assert queue.prior_stays(conn, stay) == []


def test_midstay_note_for_multi_night_stays_only(conn, env):
    two_nights = add_stay(conn, "The Studio", "Pam Lee", -1, 1)
    one_night = add_stay(conn, "The Cottage", "Al Roe", -1, 0)
    summary = queue.build_queue_for_today()
    card = _card(conn, two_nights, "midstay")
    assert card["status"] == "ready"
    assert card["message"].startswith("Hi Pam, just checking in")
    assert "The Studio" in card["message"]
    assert _card(conn, one_night, "midstay") is None
    assert summary["midstay"] == 1
    assert env["claude"].prompts == []


def test_midstay_can_be_turned_off(conn, env):
    db.set_setting(conn, "midstay_enabled", "0")
    conn.commit()
    sid = add_stay(conn, "The Studio", "Pam Lee", -1, 2)
    queue.build_queue_for_today()
    assert _card(conn, sid, "midstay") is None


def test_rebuild_is_idempotent(conn, env):
    set_template(conn, "The Studio", TEMPLATE)
    add_stay(conn, "The Studio", "Dana Fox", 0, 2, phone="1234")
    add_stay(conn, "The Cottage", "Pam Lee", -1, 2)
    queue.build_queue_for_today()
    second = queue.build_queue_for_today()
    assert second["skipped"] == 2
    queue.build_queue_for_today(force=True)
    assert conn.execute("SELECT COUNT(*) FROM daily_queue").fetchone()[0] == 2


def test_browser_error_is_one_readable_line():
    err = RuntimeError("BrowserType.launch_persistent_context: Executable doesn't exist at /x/chrome\n"
                       "╔════════════╗\n║ Looks like Playwright was just installed ║\n╚════╝")
    assert queue._short_reason(err) == ("the automated browser isn't installed: "
                                        "run 'playwright install chromium'")
    other = RuntimeError("Page.goto: net::ERR_INTERNET_DISCONNECTED at https://www.airbnb.com\n╔══╗")
    assert queue._short_reason(other) == "net::ERR_INTERNET_DISCONNECTED at https://www.airbnb.com"


def test_welcome_drafted_the_day_before_by_default(conn, env):
    set_template(conn, "The Studio", TEMPLATE)
    tomorrow = add_stay(conn, "The Studio", "Pam Lee", 1, 3, phone="1234")
    later = add_stay(conn, "The Cottage", "Al Roe", 2, 4, phone="5678")
    summary = queue.build_queue_for_today()
    assert _card(conn, tomorrow)["message"] == "Hi Pam! Door code: 1234."
    assert _card(conn, later) is None
    assert summary["queued"] == 1


def test_today_arrival_not_sent_yesterday_still_shows(conn, env):
    set_template(conn, "The Studio", TEMPLATE)
    missed = add_stay(conn, "The Studio", "Pam Lee", 0, 2, phone="1234")
    done = add_stay(conn, "The Studio", "Al Roe", 0, 2, phone="5678")
    conn.execute("UPDATE stays SET status = 'sent' WHERE id = ?", (done,))
    conn.commit()
    queue.build_queue_for_today()
    assert _card(conn, missed)["status"] == "ready"
    assert _card(conn, done) is None  # already welcomed yesterday


def test_same_day_setting(conn, env):
    db.set_setting(conn, "welcome_days_before", "0")
    conn.commit()
    set_template(conn, "The Studio", TEMPLATE)
    tomorrow = add_stay(conn, "The Studio", "Pam Lee", 1, 3, phone="1234")
    queue.build_queue_for_today()
    assert _card(conn, tomorrow) is None


def test_subject_line_never_reaches_guests(conn, env):
    set_template(conn, "The Studio", "Subject: Welcome!\n\nHi {{guest_first_name}}! Code {{door_code}}.")
    sid = add_stay(conn, "The Studio", "Pam Lee", 1, 3, phone="1234")
    queue.build_queue_for_today()
    assert _card(conn, sid)["message"] == "Hi Pam! Code 1234."
    assert queue.strip_subject("Hi there\nSubject: not a header") == "Hi there\nSubject: not a header"


def test_shipped_templates_have_no_subject_and_ask_arrival_early():
    import glob
    for path in glob.glob(os.path.join(os.path.dirname(queue.__file__), "..", "templates", "*.txt")):
        text = open(path, encoding="utf-8").read()
        assert not text.lower().startswith("subject:"), path
        ask = text.index("estimated arrival time")
        assert ask < text.index("📍"), f"{path}: arrival-time ask should come before the address"


def test_prompt_forbids_new_sections_and_subject():
    from app.claude import render_prompt
    p = render_prompt("draft_greeting", unit_name="U", unit_profile="", template="T", guest_name="G",
                      checkin_date="", checkout_date="", merge_values="{}", booking_message="",
                      host_signature="")
    assert "Do NOT add sections" in p and "no checkout details" in p
    assert 'never start with a "Subject:" line' in p


def test_saving_stay_redrafts_needs_setup_card(conn, env):
    from fastapi.testclient import TestClient
    from app import main
    set_template(conn, "The Studio", TEMPLATE)
    sid = add_stay(conn, "The Studio", queue.UNKNOWN_GUEST, 1, 3, phone="1234")
    queue.build_queue_for_today()
    assert _card(conn, sid)["status"] == "needs_setup"
    calls = []
    env_thread = browser_assist.read_thread
    browser_assist.read_thread = lambda *a, **k: calls.append(a) or {"messages": [], "guest_name": ""}
    try:
        r = TestClient(main.app).put(f"/api/stays/{sid}", json={"guest_name": "Melissa Hart"})
    finally:
        browser_assist.read_thread = env_thread
    assert r.json()["redrafted"] == ["ready"]
    assert _card(conn, sid)["message"] == "Hi Melissa! Door code: 1234."
    assert calls == []  # no Airbnb browser window for a quick fix


def test_unrelated_save_keeps_hand_edited_ready_card(conn, env):
    from fastapi.testclient import TestClient
    from app import main
    set_template(conn, "The Studio", TEMPLATE)
    sid = add_stay(conn, "The Studio", "Pam Lee", 1, 3, phone="1234")
    queue.build_queue_for_today()
    conn.execute("UPDATE daily_queue SET message = 'my edit' WHERE stay_id = ?", (sid,))
    conn.commit()
    r = TestClient(main.app).put(f"/api/stays/{sid}", json={"guest_messages": "loved the goats"})
    assert r.json()["redrafted"] == []
    assert _card(conn, sid)["message"] == "my edit"
    TestClient(main.app).put(f"/api/stays/{sid}", json={"phone_last4": "9999"})
    assert _card(conn, sid)["message"] == "Hi Pam! Door code: 9999."


def test_stay_page_sent_closes_dashboard_card(conn, env):
    from fastapi.testclient import TestClient
    from app import main
    set_template(conn, "The Studio", TEMPLATE)
    sid = add_stay(conn, "The Studio", "Pam Lee", 1, 3, phone="1234")
    queue.build_queue_for_today()
    TestClient(main.app).post(f"/api/stays/{sid}/mark-sent", json={"content": "Hi Pam!"})
    assert _card(conn, sid)["status"] == "sent"
