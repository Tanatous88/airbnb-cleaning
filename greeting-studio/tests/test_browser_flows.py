"""End-to-end browser flows (read_thread, fill_airbnb_review) with every
airbnb.com request answered by a local fixture page."""
import threading

import pytest

pytest.importorskip("playwright")

from app import browser_assist as ba  # noqa: E402
from airbnb_fixtures import LocalAirbnb, reservation_html  # noqa: E402


@pytest.fixture
def airbnb(monkeypatch, tmp_path):
    monkeypatch.setattr(ba, "DEBUG_SCREENSHOT_PATH", str(tmp_path / "thread.png"))
    monkeypatch.setattr(ba, "DEBUG_REVIEW_SCREENSHOT_PATH", str(tmp_path / "review.png"))

    def serve(html):
        monkeypatch.setattr(ba, "sync_playwright", LocalAirbnb(html, str(tmp_path / "profile")))
    return serve


def test_read_thread_end_to_end(airbnb):
    airbnb(reservation_html())
    out = ba.read_thread("HMTEST123")
    assert out["guest_name"] == "Melissa"
    assert out["messages"][-1] == "We had a wonderful time, thank you!"


def _capture_on_close(monkeypatch):
    """fill_airbnb_review leaves the window open for the host; grab the
    page's state at that moment instead of waiting."""
    state, seen = {}, threading.Event()

    def capture(page, seconds):
        state["review"] = page.input_value("#review")
        state["msg"] = page.input_value("#msg") if page.query_selector("#msg") else None
        state["submitted"] = page.evaluate("window.clicked")
        seen.set()
    monkeypatch.setattr(ba, "_wait_until_closed", capture)
    return state, seen


def test_fill_review_from_reservation_card(airbnb, monkeypatch):
    airbnb(reservation_html())
    state, seen = _capture_on_close(monkeypatch)
    detail = ba.fill_airbnb_review("HMTEST123", "Melissa left the place spotless.")
    assert detail.startswith("Review text filled in")
    assert seen.wait(15)
    assert state["review"] == "Melissa left the place spotless."
    assert state["msg"] == ""  # the chat box was never touched


def test_fill_review_via_thread_when_no_card(airbnb, monkeypatch):
    airbnb(reservation_html(review_card=False))
    state, seen = _capture_on_close(monkeypatch)
    detail = ba.fill_airbnb_review("HMTEST123", "Lovely guests.")
    assert detail.startswith("Review text filled in")
    assert seen.wait(15)
    assert state["review"] == "Lovely guests." and state["msg"] == ""


def test_fill_review_reports_missing_button(airbnb, monkeypatch):
    airbnb(reservation_html(review_card=False, thread=False))
    state, seen = _capture_on_close(monkeypatch)
    detail = ba.fill_airbnb_review("HMTEST123", "text")
    assert "Couldn't find" in detail
    assert seen.wait(15) and state["review"] == ""


def test_login_page_is_reported(airbnb, monkeypatch):
    airbnb("<html><body><form><input type='password'></form></body></html>")
    state, seen = _capture_on_close(monkeypatch)
    monkeypatch.setattr(ba, "_wait_until_closed", lambda page, s: seen.set())
    assert "login page" in ba.fill_airbnb_review("HMTEST123", "text")
