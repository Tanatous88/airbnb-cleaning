"""Playwright automation against the Airbnb host inbox.

Three capabilities, all using ONE persistent browser profile (./.pw-profile)
so your Airbnb login sticks between uses — no credentials are ever stored by
the app. Log in manually the first time a window opens.

  fill_airbnb_message  — legacy assist: fills the box and stops (never sends)
  read_thread_messages — best-effort scrape of guest messages (for drafting)
  send_airbnb_message  — fills the box AND submits; only ever called after the
                         host clicks Send on the dashboard

Airbnb changes its inbox markup periodically, so all of this can break: every
failure raises with a specific reason (fail loud, never silent), and a login
redirect raises AirbnbSessionExpired so the dashboard can show a banner.

Requires:  pip install playwright && playwright install chromium
"""
import os

from playwright.sync_api import sync_playwright

PROFILE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".pw-profile"
)

MESSAGE_BOX_SELECTORS = [
    "textarea[placeholder*='message' i]",
    "textarea[aria-label*='message' i]",
    "div[contenteditable='true'][aria-label*='message' i]",
    "div[contenteditable='true']",
    "textarea",
]

SEND_BUTTON_SELECTORS = [
    "button[aria-label*='send' i]",
    "button[data-testid*='send' i]",
    "button[type='submit']",
    "button:has-text('Send')",
]

GUEST_MESSAGE_SELECTORS = [
    "[data-testid*='message-text']",
    "[class*='message'] p",
]


class AirbnbSessionExpired(Exception):
    """Raised when Airbnb bounces us to a login page."""


def _thread_url(confirmation_code: str) -> str:
    return (f"https://www.airbnb.com/hosting/reservations/details/{confirmation_code}"
            if confirmation_code else "https://www.airbnb.com/hosting/inbox")


def _check_logged_in(page) -> None:
    url = page.url.lower()
    if "/login" in url or "/signup" in url or page.query_selector("input[type='password']"):
        raise AirbnbSessionExpired(
            "Airbnb session expired — open the browser assist once and log in again.")


def _find_message_box(page, timeout_ms: int = 8_000):
    for selector in MESSAGE_BOX_SELECTORS:
        try:
            box = page.wait_for_selector(selector, timeout=timeout_ms)
            if box:
                return box, selector
        except Exception:
            continue
    return None, None


def fill_airbnb_message(confirmation_code: str, text: str, wait_seconds: int = 300) -> str:
    """Legacy assist: open a VISIBLE window, fill the box, never send.
    Leaves the window open so the host can review and press Send themselves."""
    with sync_playwright() as pw:
        context = pw.chromium.launch_persistent_context(PROFILE_DIR, headless=False)
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(_thread_url(confirmation_code), wait_until="domcontentloaded", timeout=60_000)
        _check_logged_in(page)
        box, selector = _find_message_box(page)
        if box:
            box.click()
            box.fill(text) if selector.startswith("textarea") else box.type(text)
            detail = ("Message box filled — review it in the browser window and press Send "
                      "yourself.")
        else:
            detail = ("Could not locate the message box (Airbnb DOM may have changed, or the "
                      "thread isn't open). The text is on your clipboard — paste it manually.")
        try:
            page.wait_for_timeout(wait_seconds * 1000)
        except Exception:
            pass
        context.close()
        return detail


def read_thread_messages(confirmation_code: str, max_messages: int = 10) -> list:
    """Best-effort scrape of the guest's messages for draft personalization.
    Runs headless with the persistent profile. Returns [] on anything
    unexpected — the caller falls back to the stay's stored booking message."""
    if not confirmation_code:
        return []
    with sync_playwright() as pw:
        context = pw.chromium.launch_persistent_context(PROFILE_DIR, headless=True)
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(_thread_url(confirmation_code), wait_until="domcontentloaded",
                      timeout=45_000)
            _check_logged_in(page)
            page.wait_for_timeout(3_000)  # let the thread hydrate
            texts = []
            for selector in GUEST_MESSAGE_SELECTORS:
                for el in page.query_selector_all(selector)[:max_messages * 2]:
                    t = (el.inner_text() or "").strip()
                    if t and t not in texts and len(t) > 2:
                        texts.append(t)
                if texts:
                    break
            return texts[:max_messages]
        finally:
            context.close()


def send_airbnb_message(confirmation_code: str, text: str) -> str:
    """Fill the message box and SUBMIT. Called only from the dashboard Send
    button — that click is the human approval. Raises with a specific reason
    on any failure so the queue can mark the row 'failed' and show it."""
    if not confirmation_code:
        raise RuntimeError(
            "Stay has no Airbnb confirmation code — import it from the .ics feed or add it "
            "to the stay, so the right thread can be opened.")
    with sync_playwright() as pw:
        context = pw.chromium.launch_persistent_context(PROFILE_DIR, headless=False)
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(_thread_url(confirmation_code), wait_until="domcontentloaded",
                      timeout=60_000)
            _check_logged_in(page)
            box, selector = _find_message_box(page, timeout_ms=15_000)
            if not box:
                raise RuntimeError(
                    "Could not find the message box on the reservation page — Airbnb's markup "
                    "may have changed, or the thread didn't load. Use the fill-only assist or "
                    "clipboard instead.")
            box.click()
            box.fill(text) if selector.startswith("textarea") else box.type(text)
            page.wait_for_timeout(500)
            sent = False
            for sel in SEND_BUTTON_SELECTORS:
                try:
                    btn = page.query_selector(sel)
                    if btn and btn.is_enabled():
                        btn.click()
                        sent = True
                        break
                except Exception:
                    continue
            if not sent:
                raise RuntimeError(
                    "Message box was filled but no Send button was found — Airbnb's markup may "
                    "have changed. The window stayed open briefly; nothing was sent.")
            page.wait_for_timeout(2_500)  # let the submit go through
            return "Message submitted to the Airbnb thread."
        finally:
            context.close()
