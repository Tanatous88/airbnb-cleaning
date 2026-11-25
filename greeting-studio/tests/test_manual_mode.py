"""Copy & paste mode: the host pastes messages into Airbnb and clicks "I sent it"."""
import pytest
from fastapi.testclient import TestClient

from app import main, queue
from conftest import add_stay, set_template


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(queue, "sync_calendars", lambda: {"errors": []})
    monkeypatch.setattr(queue, "_notify", lambda summary: None)
    return TestClient(main.app)


def _build(conn, client):
    set_template(conn, "The Studio", "Hi {{guest_first_name}}! Door code: {{door_code}}.")
    arrival = add_stay(conn, "The Studio", "Pam Lee", 0, 2, code="HMPAM123", phone="1234")
    midstay = add_stay(conn, "The Cottage", "Al Roe", -1, 2)
    queue.build_queue_for_today()
    items = {i["stay_id"]: i for i in client.get("/api/queue").json()["items"]}
    return arrival, midstay, items


def test_queue_items_carry_airbnb_link_and_mode(conn, client):
    arrival, midstay, items = _build(conn, client)
    assert items[arrival]["airbnb_url"].endswith("/hosting/reservations/details/HMPAM123")
    assert items[midstay]["airbnb_url"] == main.AIRBNB_INBOX_URL  # no code -> inbox
    assert client.get("/api/queue").json()["manual_mode"] is True  # default on


def test_i_sent_it_records_and_clears_greeting_due(conn, client):
    arrival, _, items = _build(conn, client)
    assert client.get("/api/dashboard").json()["due_count"] == 1
    r = client.post(f"/api/queue/{items[arrival]['id']}/mark-sent",
                    json={"message": "Hi Pam! (edited)"})
    assert r.status_code == 200
    card = conn.execute("SELECT * FROM daily_queue WHERE stay_id = ?", (arrival,)).fetchone()
    assert card["status"] == "sent" and card["message"] == "Hi Pam! (edited)"
    assert conn.execute("SELECT status FROM stays WHERE id = ?", (arrival,)).fetchone()[0] == "sent"
    log = client.get(f"/api/stays/{arrival}/sent").json()
    assert [x["content"] for x in log] == ["Hi Pam! (edited)"]
    assert client.get("/api/dashboard").json()["due_count"] == 0
    # clicking twice doesn't log twice
    client.post(f"/api/queue/{items[arrival]['id']}/mark-sent", json={"message": "Hi Pam! (edited)"})
    assert len(client.get(f"/api/stays/{arrival}/sent").json()) == 1


def test_undo_restores_card_and_log(conn, client):
    arrival, _, items = _build(conn, client)
    qid = items[arrival]["id"]
    client.post(f"/api/queue/{qid}/mark-sent", json={"message": "Hi Pam!"})
    assert client.post(f"/api/queue/{qid}/undo-sent").status_code == 200
    card = conn.execute("SELECT status FROM daily_queue WHERE id = ?", (qid,)).fetchone()
    assert card["status"] == "ready"
    assert client.get(f"/api/stays/{arrival}/sent").json() == []
    assert client.get("/api/dashboard").json()["due_count"] == 1


def test_midstay_sent_does_not_touch_stay_status(conn, client):
    _, midstay, items = _build(conn, client)
    client.post(f"/api/queue/{items[midstay]['id']}/mark-sent", json={"message": "How's it going?"})
    assert conn.execute("SELECT status FROM stays WHERE id = ?", (midstay,)).fetchone()[0] != "sent"


def test_empty_message_rejected(conn, client):
    arrival, _, items = _build(conn, client)
    assert client.post(f"/api/queue/{items[arrival]['id']}/mark-sent",
                       json={"message": "  "}).status_code == 400


def test_manual_mode_setting_round_trip(client):
    client.put("/api/settings", json={"manual_mode": False})
    assert client.get("/api/settings").json()["manual_mode"] is False
    assert client.get("/api/queue").json()["manual_mode"] is False
