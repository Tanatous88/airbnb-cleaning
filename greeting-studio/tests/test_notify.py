"""Morning summary email, weekend-guide link setting, off-screen browser flag."""
import pytest
from datetime import date
from fastapi.testclient import TestClient

from app import browser_assist, db, main, notify, queue
from conftest import add_stay, set_template


@pytest.fixture
def built(conn, monkeypatch):
    monkeypatch.setattr(queue, "sync_calendars", lambda: {"errors": []})
    monkeypatch.setattr(queue, "_notify", lambda summary: None)
    monkeypatch.setattr(browser_assist, "read_thread", lambda *a, **k: {"messages": [], "guest_name": ""})
    set_template(conn, "The Studio", "Hi {{guest_first_name}}! Door code: {{door_code}}.")
    add_stay(conn, "The Studio", "Pam Lee", 1, 3, code="HMPAM123", phone="1234")
    add_stay(conn, "The Cottage", queue.UNKNOWN_GUEST, 0, 2)     # no template -> needs setup
    add_stay(conn, "The Bunkhouse", "Al Roe", -1, 2)             # mid-stay note
    add_stay(conn, "Three Forks Garage", "Old Guest", -4, -2)            # review due
    queue.build_queue_for_today()
    return conn


class FakeSMTP:
    sent = []

    def __init__(self, host, port, timeout=None):
        self.host, self.port = host, port

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def starttls(self):
        pass

    def login(self, user, password):
        self.user = user

    def send_message(self, msg):
        FakeSMTP.sent.append(msg)


def test_summary_lists_everything_waiting(built):
    subject, body, count = notify.build_summary(built, link="http://192.168.1.5:8321")
    assert count == 4
    assert subject == ("Greeting Studio: 2 welcome messages, 1 check-in note, 1 review due "
                       "(1 needs setup)")
    assert "The Studio — Pam Lee (arrives tomorrow): ready to send" in body
    assert "NEEDS SETUP" in body and "has no template yet" in body
    assert "The Bunkhouse — Al Roe" in body
    assert "Three Forks Garage — Old Guest: 12 days left" in body
    assert "http://192.168.1.5:8321" in body


def test_morning_summary_sends_when_on(built, monkeypatch):
    monkeypatch.setenv("SMTP_USER", "studio@example.com")
    monkeypatch.setenv("SMTP_PASSWORD", "app-password")
    monkeypatch.setattr(notify.smtplib, "SMTP", FakeSMTP)
    FakeSMTP.sent = []
    assert notify.send_morning_summary() == {"sent": False, "reason": "off"}
    db.set_setting(built, "summary_email_enabled", "1")
    db.set_setting(built, "summary_email_to", "host@example.com")
    built.commit()
    r = notify.send_morning_summary()
    assert r["sent"] is True and len(FakeSMTP.sent) == 1
    msg = FakeSMTP.sent[0]
    assert msg["To"] == "host@example.com" and msg["From"] == "studio@example.com"
    assert "Pam Lee" in msg.get_content()


def test_nothing_to_do_sends_nothing(conn, monkeypatch):
    monkeypatch.setenv("SMTP_USER", "u@example.com")
    monkeypatch.setenv("SMTP_PASSWORD", "p")
    monkeypatch.setattr(notify.smtplib, "SMTP", FakeSMTP)
    FakeSMTP.sent = []
    db.set_setting(conn, "summary_email_enabled", "1")
    db.set_setting(conn, "summary_email_to", "host@example.com")
    conn.commit()
    assert notify.send_morning_summary() == {"sent": False, "reason": "nothing to do"}
    assert FakeSMTP.sent == []


def test_test_button_explains_missing_login(conn, monkeypatch):
    monkeypatch.delenv("SMTP_USER", raising=False)
    monkeypatch.delenv("SMTP_PASSWORD", raising=False)
    r = TestClient(main.app).post("/api/summary/send-now")
    assert r.status_code == 400 and "SMTP_USER" in r.json()["detail"]


def test_email_and_guide_settings_validate(conn):
    c = TestClient(main.app)
    assert c.put("/api/settings", json={"summary_email_to": "not an email"}).status_code == 400
    assert c.put("/api/settings", json={"weekend_guide_url": "threeforks.com/weekend"}).status_code == 400
    assert c.put("/api/settings", json={"summary_email_to": "host@example.com",
                                        "weekend_guide_url": "https://threeforksaccommodations.com/weekend",
                                        "hide_browser": False}).status_code == 200
    s = c.get("/api/settings").json()
    assert s["summary_email_to"] == "host@example.com"
    assert s["weekend_guide_url"] == "https://threeforksaccommodations.com/weekend"
    assert s["hide_browser"] is False
    c.put("/api/settings", json={"weekend_guide_url": ""})  # blank -> back to the default
    assert c.get("/api/settings").json()["weekend_guide_url"] == queue.WEEKEND_GUIDE_URL


def test_guide_link_setting_reaches_welcome_message(conn):
    db.set_setting(conn, "weekend_guide_url", "https://threeforksaccommodations.com/weekend")
    conn.commit()
    d = date.today()
    while d.weekday() != 4:  # next Friday
        d = date.fromordinal(d.toordinal() + 1)
    offset = (d - date.today()).days
    sid = add_stay(conn, "The Studio", "Pam Lee", offset, offset + 2, phone="1234")
    unit = conn.execute("SELECT * FROM units WHERE name = 'The Studio'").fetchone()
    stay = conn.execute("SELECT * FROM stays WHERE id = ?", (sid,)).fetchone()
    assert "https://threeforksaccommodations.com/weekend" in \
        queue.build_merge_values(conn, unit, stay)["weekend_guide"]


def test_morning_read_runs_off_screen_by_default(conn, monkeypatch):
    seen = {}

    def fake_read(code, max_messages=30, hidden=False):
        seen["hidden"] = hidden
        return {"messages": [], "guest_name": ""}
    monkeypatch.setattr(browser_assist, "read_thread", fake_read)
    sid = add_stay(conn, "The Studio", "Pam Lee", 1, 3, code="HMX1")
    queue.read_thread_context(conn, conn.execute("SELECT * FROM stays WHERE id = ?", (sid,)).fetchone())
    assert seen["hidden"] is True
    db.set_setting(conn, "hide_browser", "0")
    conn.commit()
    queue.read_thread_context(conn, conn.execute("SELECT * FROM stays WHERE id = ?", (sid,)).fetchone())
    assert seen["hidden"] is False
