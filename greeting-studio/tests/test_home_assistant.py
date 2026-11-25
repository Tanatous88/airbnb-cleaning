"""Morning alert to Home Assistant via webhook (outbound only)."""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from fastapi.testclient import TestClient

from app import browser_assist, db, main, notify, queue
from conftest import add_stay, set_template


@pytest.fixture
def ha_server():
    """A stand-in Home Assistant that records webhook POSTs."""
    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            received.append({"path": self.path, "json": json.loads(body)})
            self.send_response(200)
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}", received
    srv.shutdown()


@pytest.fixture
def queued(conn, monkeypatch):
    monkeypatch.setattr(queue, "sync_calendars", lambda: {"errors": []})
    monkeypatch.setattr(queue, "_notify", lambda summary: None)
    monkeypatch.setattr(browser_assist, "read_thread", lambda *a, **k: {"messages": [], "guest_name": ""})
    set_template(conn, "The Studio", "Hi {{guest_first_name}}! Door code: {{door_code}}.")
    add_stay(conn, "The Studio", "Pam Lee", 1, 3, phone="1234")
    queue.build_queue_for_today()
    return conn


def test_each_install_gets_its_own_webhook_id(conn):
    hook = db.get_setting(conn, "ha_webhook_id")
    assert hook.startswith("greeting-studio-") and len(hook) > 30
    db.init_db()  # restarting the app keeps the same id
    assert db.get_setting(conn, "ha_webhook_id") == hook


def test_morning_alert_reaches_home_assistant(queued, ha_server):
    base, received = ha_server
    db.set_setting(queued, "ha_url", base)
    db.set_setting(queued, "dashboard_link", "https://ai-pc.example.ts.net:8443/")
    queued.commit()
    r = notify.send_morning_summary()
    assert r["sent"] is True and r["channels"] == ["ha"]
    assert len(received) == 1
    hit = received[0]
    assert hit["path"] == "/api/webhook/" + db.get_setting(queued, "ha_webhook_id")
    assert hit["json"]["title"] == "Greeting Studio"
    assert hit["json"]["message"] == "1 welcome message"
    assert hit["json"]["link"] == "https://ai-pc.example.ts.net:8443/"
    assert "Pam Lee" in hit["json"]["details"]


def test_nothing_waiting_sends_nothing(conn, ha_server):
    base, received = ha_server
    db.set_setting(conn, "ha_url", base)
    conn.commit()
    assert notify.send_morning_summary() == {"sent": False, "reason": "nothing to do"}
    assert received == []


def test_test_button_and_errors(conn, ha_server):
    base, received = ha_server
    c = TestClient(main.app)
    r = c.post("/api/summary/send-now?to=ha")
    assert r.status_code == 400 and "Home Assistant" in r.json()["detail"]   # not set up yet
    c.put("/api/settings", json={"ha_url": base + "/"})
    assert c.post("/api/summary/send-now?to=ha").status_code == 200        # empty summary still sent
    assert received and received[0]["json"]["message"] == "nothing to send today"
    c.put("/api/settings", json={"ha_url": "http://127.0.0.1:9"})          # nothing listening
    r = c.post("/api/summary/send-now?to=ha")
    assert r.status_code == 502 and "Home Assistant" in r.json()["detail"]
    assert c.get("/api/health").json()["home_assistant"]["last_error"]


def test_ha_address_validation(conn):
    c = TestClient(main.app)
    assert c.put("/api/settings", json={"ha_url": "homeassistant.local:8123"}).status_code == 400
    assert c.put("/api/settings", json={"ha_url": "http://ha:8123/api/webhook/x"}).status_code == 400
    assert c.put("/api/settings", json={"ha_url": "http://homeassistant.local:8123/"}).status_code == 200
    assert c.get("/api/settings").json()["ha_url"] == "http://homeassistant.local:8123"


def test_email_failure_does_not_block_home_assistant(queued, ha_server, monkeypatch):
    base, received = ha_server
    monkeypatch.delenv("SMTP_USER", raising=False)
    db.set_setting(queued, "ha_url", base)
    db.set_setting(queued, "summary_email_enabled", "1")
    queued.commit()
    r = notify.send_morning_summary()
    assert r["channels"] == ["ha"] and len(received) == 1
    assert "SMTP_USER" in db.get_setting(queued, "summary_email_last_error")
