"""Thread reading and review filling against saved copies of Airbnb's markup."""
import pytest

pytest.importorskip("playwright")
from playwright.sync_api import sync_playwright  # noqa: E402

from app import browser_assist as ba  # noqa: E402
from airbnb_fixtures import REVIEW_FORM, launch_kwargs, reservation_html, thread_html  # noqa: E402


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as pw:
        b = pw.chromium.launch(**launch_kwargs())
        yield b
        b.close()


@pytest.fixture
def page(browser):
    p = browser.new_page()
    yield p
    p.close()


def show_thread(page, html):
    page.set_content(f"<body>{html}</body>")
    page.evaluate("document.getElementById('thread').style.display = 'block'")


def test_reads_only_guest_messages_and_name(page):
    show_thread(page, thread_html())
    out = ba._collect_thread(page)
    assert out["guest_name"] == "Melissa"
    assert out["messages"] == ["Can we check in a little early?",
                               "We had a wonderful time, thank you!"]


def test_keeps_most_recent_messages_when_capped(page):
    show_thread(page, thread_html(guest_msgs=[f"message {i}" for i in range(35)]))
    msgs = ba._collect_thread(page, max_messages=30)["messages"]
    assert len(msgs) == 30 and msgs[0] == "message 5" and msgs[-1] == "message 34"


def test_text_fallback_when_markup_changes(page):
    page.set_content(
        "<div role='dialog'><div>Melissa · Booker 11:31 AM</div><div>Is there parking?</div>"
        "<div>Kyle · Host 11:40 AM</div><div>Yes, in the driveway.</div>"
        "<div>Read by Melissa</div></div>")
    out = ba._collect_thread(page)
    assert out == {"messages": ["Is there parking?"], "guest_name": "Melissa"}


def test_click_into_thread_skips_promo_card(page):
    page.set_content(reservation_html())
    assert ba._click_into_message_thread(page) is True
    assert page.evaluate("window.clicked") == ["real"]


def test_review_fill_never_touches_message_box(page):
    page.set_content(reservation_html())
    assert ba._open_review_form(page) is True
    assert ba._fill_review_text(page, "Melissa was a wonderful guest.") is True
    assert page.input_value("#review") == "Melissa was a wonderful guest."
    assert page.input_value("#msg") == ""


def test_review_fill_refuses_when_only_message_box(page):
    show_thread(page, thread_html())  # message box visible, no review form
    assert ba._fill_review_text(page, "text", timeout_ms=1000) is False
    assert page.input_value("#msg") == ""


def test_review_fill_contenteditable_box(page):
    page.set_content('<div contenteditable="true" aria-label="Public review" id="r" '
                     'style="width:300px;height:80px"></div>'
                     '<div contenteditable="true" aria-label="Write a message" id="m" '
                     'style="width:300px;height:80px"></div>')
    assert ba._fill_review_text(page, "Great stay", timeout_ms=1000) is True
    assert page.inner_text("#r") == "Great stay" and page.inner_text("#m") == ""


def test_message_box_mentioning_experience_is_still_refused(page):
    page.set_content('<textarea id="m" aria-label="Message your guest about their experience">'
                     '</textarea>')
    assert ba._fill_review_text(page, "text", timeout_ms=1000) is False
    assert page.input_value("#m") == ""


def test_hidden_review_form_is_not_filled(page):
    page.set_content(REVIEW_FORM)  # present in the DOM but display:none
    assert ba._fill_review_text(page, "text", timeout_ms=1000) is False
