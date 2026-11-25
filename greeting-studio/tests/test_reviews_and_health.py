"""Reviews (drafting, due list, auto-draft, posted) and the Health page, via the API."""
import pytest
from fastapi.testclient import TestClient

from app import browser_assist, main, queue
from conftest import FakeClaude, add_stay, set_template


@pytest.fixture
def client(monkeypatch):
    fake = FakeClaude()
    monkeypatch.setattr(main, "ask_claude", fake)
    monkeypatch.setattr(browser_assist, "read_thread",
                        lambda code, max_messages=30: {"messages": [], "guest_name": ""})
    c = TestClient(main.app)  # no `with`: startup (scheduler) does not run
    c.fake = fake
    return c


def _review_count(conn, stay_id):
    return conn.execute("SELECT COUNT(*) FROM reviews WHERE stay_id = ?", (stay_id,)).fetchone()[0]


def test_generate_reviews_makes_three_variants(conn, client):
    add_stay(conn, "The Cottage", "Dana Fox", -100, -97)  # earlier visit
    sid = add_stay(conn, "The Studio", "Dana Fox", -3, -1)
    r = client.post(f"/api/stays/{sid}/reviews")
    assert r.status_code == 200, r.text
    assert len(r.json()["variants"]) == 3
    assert _review_count(conn, sid) == 3
    assert "Returning guest" in client.fake.prompts[0]


def test_failed_regeneration_keeps_old_drafts(conn, client, monkeypatch):
    sid = add_stay(conn, "The Studio", "Dana Fox", -3, -1)
    client.post(f"/api/stays/{sid}/reviews")
    monkeypatch.setattr(main, "ask_claude", FakeClaude(fail_on_call=2))
    r = client.post(f"/api/stays/{sid}/reviews")
    assert r.status_code == 502
    texts = [row[0] for row in conn.execute("SELECT content FROM reviews WHERE stay_id = ?", (sid,))]
    assert texts == ["DRAFT #1", "DRAFT #2", "DRAFT #3"]  # the first run's drafts survive


def test_reviews_due_window_and_mark_posted(conn, client):
    recent = add_stay(conn, "The Studio", "Pam Lee", -5, -2)
    add_stay(conn, "The Cottage", "Old Guest", -30, -20)       # window closed
    add_stay(conn, "The Bunkhouse", "Future Guest", 2, 4)      # not checked out
    due = client.get("/api/dashboard").json()["reviews_due"]
    assert [(d["guest_name"], d["days_left"]) for d in due] == [("Pam Lee", 12)]
    client.post(f"/api/stays/{recent}/review-posted", json={"posted": True})
    assert client.get("/api/dashboard").json()["reviews_due"] == []
    client.post(f"/api/stays/{recent}/review-posted", json={"posted": False})
    assert len(client.get("/api/dashboard").json()["reviews_due"]) == 1


def test_auto_draft_only_recent_checkouts_without_drafts(conn, client):
    yesterday = add_stay(conn, "The Studio", "Pam Lee", -3, -1)
    today = add_stay(conn, "The Cottage", "Still Here", -2, 0)      # checks out today: wait
    stale = add_stay(conn, "The Bunkhouse", "Long Gone", -12, -10)  # too old for auto
    result = main.auto_draft_reviews()
    assert result == {"drafted": 1, "errors": []}
    assert _review_count(conn, yesterday) == 3
    assert _review_count(conn, today) == 0 and _review_count(conn, stale) == 0
    assert main.auto_draft_reviews()["drafted"] == 0  # already drafted -> not redone


def test_auto_draft_can_be_turned_off(conn, client):
    client.put("/api/settings", json={"auto_reviews_enabled": False})
    add_stay(conn, "The Studio", "Pam Lee", -3, -1)
    assert main.auto_draft_reviews()["drafted"] == 0


def test_review_assist_requires_code(conn, client):
    sid = add_stay(conn, "The Studio", "Pam Lee", -3, -1)
    r = client.post(f"/api/stays/{sid}/review-assist", json={"content": "Great guest!"})
    assert r.status_code == 400 and "confirmation code" in r.json()["detail"]


def test_settings_round_trip(client):
    client.put("/api/settings", json={"midstay_enabled": False,
                                      "midstay_template": "Hi {{guest_first_name}}"})
    s = client.get("/api/settings").json()
    assert s["midstay_enabled"] is False and s["midstay_template"] == "Hi {{guest_first_name}}"


def test_health_flags_upcoming_problems(conn, client):
    set_template(conn, "The Studio", "Hi {{guest_first_name}}, code {{door_code}}")
    add_stay(conn, "The Studio", queue.UNKNOWN_GUEST, 3, 5, code="HM1")      # name + code
    add_stay(conn, "The Studio", "Pam Lee", 4, 6, code="HM2", phone="1234")  # fine
    report = client.get("/api/health").json()
    assert len(report["attention"]) == 1
    problems = report["attention"][0]["problems"]
    assert "guest name unknown" in problems
    assert any("door code missing" in p for p in problems)
    studio = next(u for u in report["units"] if u["name"] == "The Studio")
    assert studio["template_version"] == 1 and studio["stays_with_code"] == 2
    assert studio["phone_missing"] == 1
    assert report["duplicates"] == {"stays": 0, "queue": 0}
    assert report["safeguards"] == {"stays_unique": True, "queue_unique": True}


def test_health_merge_endpoint(conn, client):
    from test_dedupe import _make_duplicates
    _make_duplicates(conn)
    assert client.get("/api/health").json()["duplicates"]["stays"] == 1
    r = client.post("/api/health/merge-duplicates").json()
    assert r["stays_removed"] == 2
    after = client.get("/api/health").json()
    assert after["duplicates"] == {"stays": 0, "queue": 0}
    assert after["safeguards"] == {"stays_unique": True, "queue_unique": True}
