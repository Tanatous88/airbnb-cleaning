"""Playwright automation against the Airbnb host inbox.

Three capabilities, all using ONE persistent browser profile (./.pw-profile)
so your Airbnb login sticks between uses — no credentials are ever stored by
the app. Log in manually the first time a window opens.

  fill_airbnb_message  — legacy assist: fills the box and stops (never sends)
  read_thread_messages — best-effort scrape of guest messages (for drafting)
  send_airbnb_message  — fills the box AND submits; only ever called after the
                         host clicks Send on the dashboard

The reservation-details page (/hosting/reservations/details/<confirmation
code>) has NO message box or message history on it — it's a summary page
(dates, guest, "Leave a review," Message/Call buttons). The actual message
thread lives at a completely separate URL keyed by an internal numeric
thread id (/hosting/messages/<thread id>), which isn't derivable from the
confirmation code. So every function here lands on the reservation page
first, then clicks its "Message" button to reach the real thread, before
doing anything else.

Airbnb changes its inbox markup periodically, so all of this can break: every
failure raises with a specific reason (fail loud, never silent), and a login
redirect raises AirbnbSessionExpired so the dashboard can show a banner.

Requires:  pip install playwright && playwright install chromium
"""
import os
import re

from playwright.sync_api import sync_playwright

# Airbnb labels each message bubble with a header line like "Melissa ·
# Booker 11:31 AM" or "Susan · Co-host 2:45 PM" — confirmed against a real
# screenshot of the thread modal. That role label ("Booker" = guest,
# "Co-host"/"Host"/"You" = host side) is far more reliable than guessing at
# CSS class names, so message text is attributed by parsing these headers
# rather than by selector alone.
_SENDER_HEADER_RE = re.compile(
    r"^(?P<name>.+?)\s*[·•]\s*(?P<role>Booker|Guest|Co-host|Cohost|Host|You)\s+"
    r"\d{1,2}:\d{2}\s*(AM|PM)?$", re.IGNORECASE)
_GUEST_ROLES = {"booker", "guest"}
_SKIP_LINE_PREFIXES = (
    "read by", "translation on", "translation off", "how was your guest",
    "write a message", "leave a review",
)

# Verified against real outerHTML pulled from a live thread via DevTools.
# Airbnb's atomic-CSS class names (atm_*, t1w2lm2f, ...) regenerate on every
# build/AB-test and are useless as selectors. Three things are NOT
# generated and ARE stable: the data-testid on the conversation container,
# the accessible aria-label on each sender button ("Sent by <name> ·
# <role> at <time>..."), and the view-transition-name style value that
# marks the actual message-content wrapper. Each message is one child of
# the message-list container, holding both its header (with the sender
# button) and its content wrapper.
_GUEST_MESSAGE_JS = r"""
() => {
  const container = document.querySelector('[data-testid="message-list"]');
  if (!container) return [];
  const out = [];
  for (const group of Array.from(container.children)) {
    const btn = group.querySelector('button[aria-label^="Sent by "]');
    if (!btn) continue;
    const label = btn.getAttribute('aria-label') || '';
    if (!/·\s*(Booker|Guest)\b/i.test(label)) continue;
    const contentEl = group.querySelector('[style*="message-content"]');
    const text = contentEl ? contentEl.innerText.trim() : '';
    if (text) out.push(text);
  }
  return out;
}
"""


def _extract_guest_lines(dialog_text: str) -> list:
    """Parse a message-thread's rendered text into guest-only message lines,
    using the "Name · Role time" header pattern to attribute each line that
    follows it. Order-preserving, dedupes nothing (caller handles that)."""
    lines = [l.strip() for l in (dialog_text or "").splitlines() if l.strip()]
    guest_lines = []
    current_is_guest = False
    for line in lines:
        m = _SENDER_HEADER_RE.match(line)
        if m:
            current_is_guest = m.group("role").lower() in _GUEST_ROLES
            continue
        if any(line.lower().startswith(p) for p in _SKIP_LINE_PREFIXES):
            continue
        if current_is_guest and len(line) > 2:
            guest_lines.append(line)
    return guest_lines

PROFILE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".pw-profile"
)
# Overwritten on every automated thread read — a ground-truth snapshot of
# what the page actually looked like, since this code was written without
# ever being able to load airbnb.com to verify selectors against it.
DEBUG_SCREENSHOT_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "debug_thread_read.png"
)

MESSAGE_THREAD_BUTTON_SELECTORS = [
    "button:has-text('Message')",
    "a:has-text('Message')",
]

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


def _reservation_url(confirmation_code: str) -> str:
    return (f"https://www.airbnb.com/hosting/reservations/details/{confirmation_code}"
            if confirmation_code else "https://www.airbnb.com/hosting/inbox")


def _check_logged_in(page) -> None:
    url = page.url.lower()
    # A password <input> can exist in the DOM (a hidden login modal a modern
    # SPA preloads) without ever being shown — only a VISIBLE one is real
    # evidence of a login page. DOM-presence alone was a likely false
    # positive here, since this selector was never verified against a real
    # Airbnb page.
    pw_input = page.query_selector("input[type='password']")
    pw_visible = bool(pw_input and pw_input.is_visible())
    if "/login" in url or "/signup" in url or pw_visible:
        raise AirbnbSessionExpired(
            f"Airbnb session expired or blocked — open the browser assist once and log in "
            f"again. (landed on: {page.url})")


def _open_reservation(page, confirmation_code: str) -> None:
    page.goto(_reservation_url(confirmation_code), wait_until="domcontentloaded", timeout=60_000)
    page.wait_for_timeout(2_000)  # let the SPA hydrate before judging the page


def _click_into_message_thread(page) -> bool:
    """From the reservation page, follow the "Message" button to the real
    thread. Returns True if a button was found and clicked."""
    for selector in MESSAGE_THREAD_BUTTON_SELECTORS:
        btn = page.query_selector(selector)
        if btn:
            btn.click()
            page.wait_for_timeout(2_500)
            return True
    return False


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
    Leaves the window open so the host can review and press Send themselves.

    This is also the ONLY chance to log the automation's persistent browser
    profile into Airbnb for the first time — so if we land on a login page,
    we must NOT raise/abort here. Doing that used to close the window before
    the host had any chance to type a password, which silently defeated the
    entire point of this function."""
    with sync_playwright() as pw:
        context = pw.chromium.launch_persistent_context(PROFILE_DIR, headless=False)
        page = context.pages[0] if context.pages else context.new_page()
        _open_reservation(page, confirmation_code)
        try:
            _check_logged_in(page)
        except AirbnbSessionExpired:
            detail = ("Landed on an Airbnb login page. Log in in this window now — once you do, "
                      "the login is saved for next time. This window will stay open for a few "
                      "minutes so you have time; nothing else happens automatically.")
            try:
                page.wait_for_timeout(wait_seconds * 1000)
            except Exception:
                pass
            context.close()
            return detail
        if not _click_into_message_thread(page):
            detail = ("Could not find the 'Message' button on the reservation page — Airbnb's "
                      "markup may have changed. The text is on your clipboard — paste it "
                      "manually.")
            try:
                page.wait_for_timeout(wait_seconds * 1000)
            except Exception:
                pass
            context.close()
            return detail
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
    """Best-effort scrape of the guest's messages for draft/review personalization.
    Returns [] on anything unexpected — the caller falls back to the stay's
    stored booking message."""
    if not confirmation_code:
        return []
    with sync_playwright() as pw:
        context = pw.chromium.launch_persistent_context(PROFILE_DIR, headless=False)
        try:
            page = context.pages[0] if context.pages else context.new_page()
            _open_reservation(page, confirmation_code)
            _check_logged_in(page)
            _click_into_message_thread(page)
            try:
                page.screenshot(path=DEBUG_SCREENSHOT_PATH)
            except Exception:
                pass  # diagnostic aid only, never fatal
            _check_logged_in(page)
            texts = []
            try:
                for t in (page.evaluate(_GUEST_MESSAGE_JS) or []):
                    t = (t or "").strip()
                    if t and t not in texts:
                        texts.append(t)
            except Exception:
                pass  # markup may not match — fall through to weaker methods below
            if not texts:
                # Fallback 1: parse the dialog's rendered text by sender-header line.
                dialog = page.query_selector("[role='dialog']")
                raw_text = (dialog.inner_text() if dialog else page.inner_text("body")) or ""
                for t in _extract_guest_lines(raw_text):
                    if t not in texts:
                        texts.append(t)
            if not texts:
                # Fallback 2: the older class/testid-based guess.
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
            _open_reservation(page, confirmation_code)
            _check_logged_in(page)
            if not _click_into_message_thread(page):
                raise RuntimeError(
                    "Could not find the 'Message' button on the reservation page — Airbnb's "
                    "markup may have changed. Use the fill-only assist or clipboard instead.")
            _check_logged_in(page)
            box, selector = _find_message_box(page, timeout_ms=15_000)
            if not box:
                raise RuntimeError(
                    "Could not find the message box on the message thread page — Airbnb's "
                    "markup may have changed, or the thread didn't load. Use the fill-only "
                    "assist or clipboard instead.")
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
