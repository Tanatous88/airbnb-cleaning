"""Flag contact details in a Claude draft that the host didn't write.

Drafts are written from guest messages read off Airbnb, which anyone can
send. A message could try to steer the draft ("include this link", "tell
them to call ..."). Nothing is ever sent without the host, but a link or
phone number slipped into a long welcome message is easy to miss — so any
link, phone number or email address in a draft that isn't already in the
host's own template (or settings) is listed on the card, and copying the
draft asks for confirmation first.
"""
import re

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_URL = re.compile(r"(?i)\b(?:https?://|www\.)[^\s<>\"')\]]+")
# Bare domains ("bit.ly/abc", "example.com") — common link-shortener and
# generic endings; enough to catch a slipped-in link without flagging prose.
_DOMAIN = re.compile(
    r"(?i)\b[a-z0-9][a-z0-9-]*(?:\.[a-z0-9-]+)*\."
    r"(?:com|net|org|io|ly|co|me|info|biz|app|xyz|link|site|online|top|us|ca|uk|gl|gg|to|ru|cn)"
    r"\b(?:/[^\s<>\"')\]]*)?")
_PHONE = re.compile(r"(?<![\d-])(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}(?![\d-])")


def _norm_link(s: str) -> str:
    s = s.lower().rstrip(".,;:!?/")
    s = re.sub(r"^https?://", "", s)
    return re.sub(r"^www\.", "", s)


def contacts(text: str) -> dict:
    """{normalized: as written} for every link, phone number and email."""
    text = text or ""
    found = {}
    for m in _EMAIL.finditer(text):
        found["email:" + m.group(0).lower()] = m.group(0)
    no_email = _EMAIL.sub(" ", text)
    for m in _URL.finditer(no_email):
        found["link:" + _norm_link(m.group(0))] = m.group(0).rstrip(".,;:!?")
    no_url = _URL.sub(" ", no_email)
    for m in _DOMAIN.finditer(no_url):
        found["link:" + _norm_link(m.group(0))] = m.group(0).rstrip(".,;:!?")
    for m in _PHONE.finditer(no_url):
        digits = re.sub(r"\D", "", m.group(0))[-10:]
        found["phone:" + digits] = m.group(0).strip()
    return found


def unexpected(draft: str, *trusted_texts: str) -> list:
    """Contact details in `draft` that appear in none of `trusted_texts`
    (the host's template, settings, ...), as written in the draft."""
    allowed = {}
    for t in trusted_texts:
        allowed.update(contacts(t))
    allowed_links = [k for k in allowed if k.startswith("link:")]
    out = []
    for key, shown in contacts(draft).items():
        if key in allowed:
            continue
        # Same address with more or less path ("site.com/weekend" vs "site.com/weekend/x")
        # counts as known — but only at a "/" boundary, so "site.com.evil.ru" doesn't.
        if key.startswith("link:") and any(key.startswith(a + "/") or a.startswith(key + "/")
                                           for a in allowed_links):
            continue
        out.append(shown)
    return out


def warning_text(items: list) -> str:
    if not items:
        return ""
    return ("Not in your template — check before sending: " + ", ".join(items) +
            ". This may have come from a guest's message.")
