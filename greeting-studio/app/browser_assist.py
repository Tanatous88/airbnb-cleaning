"""Optional Playwright assist: open the Airbnb inbox, fill the message box, STOP.

Never sends. The host presses Send themselves. If Airbnb's DOM changes and the
fill fails, the caller falls back to clipboard + deep link.

Requires:  pip install playwright && playwright install chromium
Uses a persistent browser profile (./.pw-profile) so your Airbnb login sticks
between uses — log in manually the first time the window opens.
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


def fill_airbnb_message(confirmation_code: str, text: str, wait_seconds: int = 300) -> str:
    url = (f"https://www.airbnb.com/hosting/reservations/details/{confirmation_code}"
           if confirmation_code else "https://www.airbnb.com/hosting/inbox")
    with sync_playwright() as pw:
        context = pw.chromium.launch_persistent_context(PROFILE_DIR, headless=False)
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=60_000)

        filled = False
        for selector in MESSAGE_BOX_SELECTORS:
            try:
                box = page.wait_for_selector(selector, timeout=8_000)
                if box:
                    box.click()
                    box.fill(text) if selector.startswith("textarea") else box.type(text)
                    filled = True
                    break
            except Exception:
                continue

        detail = ("Message box filled — review it in the browser window and press Send yourself."
                  if filled else
                  "Could not locate the message box (Airbnb DOM may have changed, or you need to "
                  "log in / open the thread). The text is on your clipboard — paste it manually.")
        # Leave the window open for the host to review and send.
        try:
            page.wait_for_timeout(wait_seconds * 1000)
        except Exception:
            pass
        context.close()
        return detail
