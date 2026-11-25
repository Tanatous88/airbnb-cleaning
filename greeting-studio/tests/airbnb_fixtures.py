"""Stand-in Airbnb pages for the browser tests, modelled on real markup
captured from a live thread (October 2026): the message-list container,
"Sent by <name> · <role> at <time>" aria-labels, view-transition-name
message-content wrappers, the promo-card wrapper that swallows clicks, and
the thread's "Write a message..." box. Class names are deliberately absent
— Airbnb's are generated, and the app must not depend on them."""
import os
from types import SimpleNamespace

CHROMIUM = "/opt/pw-browsers/chromium"


def launch_kwargs() -> dict:
    """Use the sandbox's pre-installed Chromium if present, else Playwright's own."""
    return {"executable_path": CHROMIUM} if os.path.exists(CHROMIUM) else {}


def _group(sender: str, role: str, time: str, text: str, n: int) -> str:
    return (f'<div><div><button aria-label="Sent by {sender} · {role} at {time}. '
            f'Tap to go to profile">{sender[0]}</button><span>{sender} · {role}</span></div>'
            f'<div style="view-transition-name: message-content-{n}"><div>{text}</div></div></div>')


HOST_GREETING = ("Welcome! 📍 Address: 123 Main St 🔑 Door code: 1575 📶 Wi-Fi: Studio5G")


def thread_html(guest="Melissa", guest_msgs=("Can we check in a little early?",
                                              "We had a wonderful time, thank you!")) -> str:
    groups = [_group("Kyle", "Host", "9:02 AM", HOST_GREETING, 0)]
    for i, m in enumerate(guest_msgs, start=1):
        groups.append(_group(guest, "Booker", f"11:{i:02d} AM", m, i))
    groups.append(_group("Kyle", "Co-host", "12:00 PM", "Sure thing!", 99))
    return (
        '<div role="dialog" id="thread" style="display:none">'
        f'<div data-testid="message-list">{"".join(groups)}</div>'
        '<div><span>How was your guest?</span><button onclick="openReview()">Leave a review'
        '</button></div>'
        '<textarea id="msg" placeholder="Write a message..."></textarea>'
        '</div>')


REVIEW_FORM = (
    '<div id="review-form" style="display:none">'
    '<h2>Write a public review</h2>'
    '<textarea id="review" aria-label="Write a public review"></textarea>'
    '<button>Submit</button></div>')


def reservation_html(review_card=True, thread=True, guest="Melissa") -> str:
    """The /hosting/reservations/details/<code> page. The promo card sits
    ABOVE the real Message button and matches its selector first."""
    card = ('<div><p>Leave a review for Melissa</p><button onclick="openReview()">'
            'Leave a review</button></div>') if review_card else ""
    message_btn = '<button onclick="openThread()">Message</button>' if thread else ""
    return f"""<!doctype html><html><head><title>Reservation</title></head><body>
<script>
window.clicked = [];
function openThread() {{ window.clicked.push('real');
  document.getElementById('thread').style.display = 'block'; }}
function openReview() {{ document.getElementById('review-form').style.display = 'block'; }}
</script>
<div data-testid="hosting-details-contextual-action" style="position:relative">
  <button aria-label="Message" onclick="window.clicked.push('promo')">Send a checkout message</button>
  <div style="position:absolute;inset:0"></div>
</div>
{message_btn}
{card}
{thread_html(guest) if thread else ''}
{REVIEW_FORM}
</body></html>"""


class LocalAirbnb:
    """Drop-in for browser_assist.sync_playwright: same calls, but headless,
    a throwaway profile, and every airbnb.com request answered with `html`."""

    def __init__(self, html: str, profile_dir: str):
        self.html = html
        self.profile_dir = profile_dir

    def __call__(self):
        return self

    def __enter__(self):
        from playwright.sync_api import sync_playwright
        self._cm = sync_playwright()
        pw = self._cm.__enter__()
        outer = self

        def launch_persistent_context(_profile, headless=False, **_):
            ctx = pw.chromium.launch_persistent_context(
                outer.profile_dir, headless=True, **launch_kwargs())
            ctx.route("**/*", lambda route: route.fulfill(
                status=200, content_type="text/html; charset=utf-8", body=outer.html))
            return ctx

        return SimpleNamespace(chromium=SimpleNamespace(
            launch_persistent_context=launch_persistent_context))

    def __exit__(self, *exc):
        return self._cm.__exit__(*exc)
