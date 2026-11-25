"""Morning summary email: what's waiting on the dashboard today.

Sent through the host's own mail account over SMTP. The login lives only in
.env (SMTP_USER / SMTP_PASSWORD, e.g. a Gmail app password); the recipient
address is a setting in the local database. Nothing here sends anything to a
guest — it's a note to the host, with links back to the app.
"""
import ipaddress
import os
import smtplib
import socket
from datetime import date, timedelta
from email.message import EmailMessage

from . import db

APP_PORT = 8321


class NotConfigured(Exception):
    """Email isn't set up (no login in .env or no recipient in Settings)."""


def smtp_configured() -> bool:
    return bool(os.environ.get("SMTP_USER", "").strip() and os.environ.get("SMTP_PASSWORD", "").strip())


TAILSCALE_NET = ipaddress.ip_network("100.64.0.0/10")
LOCAL_ONLY_HOSTS = ("127.0.0.1", "localhost", "::1")


def listen_host() -> str:
    """The address run.py listens on (see GREETING_STUDIO_HOST)."""
    return os.environ.get("GREETING_STUDIO_HOST", "127.0.0.1").strip() or "127.0.0.1"


def tailscale_ip() -> str:
    """This PC's Tailscale address (100.64.0.0/10), or '' if it has none."""
    try:
        infos = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
    except OSError:
        return ""
    for info in infos:
        ip = info[4][0]
        try:
            if ipaddress.ip_address(ip) in TAILSCALE_NET:
                return ip
        except ValueError:
            continue
    return ""


def app_url(conn=None) -> str:
    """The dashboard link for the summary email. In order: the link set in
    Settings (e.g. the Tailscale Serve https address); if the app only listens
    on this PC (the default), the local address; otherwise this PC's
    Tailscale address, else its home Wi-Fi address."""
    if conn is not None:
        custom = db.get_setting(conn, "dashboard_link", "").strip()
        if custom:
            return custom
    if listen_host() in LOCAL_ONLY_HOSTS:
        return f"http://127.0.0.1:{APP_PORT}"
    ts = tailscale_ip()
    if ts:
        return f"http://{ts}:{APP_PORT}"
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))  # no packet is sent; just picks the LAN interface
        ip = s.getsockname()[0]
    except OSError:
        ip = "127.0.0.1"
    finally:
        s.close()
    return f"http://{ip}:{APP_PORT}"


def _arrives(iso: str, today: date) -> str:
    try:
        d = date.fromisoformat(iso)
    except (TypeError, ValueError):
        return ""
    if d == today:
        return "arrives today"
    if d == today + timedelta(days=1):
        return "arrives tomorrow"
    return f"arrives {d.strftime('%a %b')} {d.day}"


def build_summary(conn, today: date = None, link: str = None):
    """(subject, body, count). count is the number of things needing the host."""
    today = today or date.today()
    link = link or app_url(conn)
    rows = conn.execute(
        "SELECT q.kind, q.status, q.error, q.guest_name, u.name AS unit_name, "
        "s.checkin_date AS arrival_date FROM daily_queue q JOIN units u ON u.id = q.unit_id "
        "LEFT JOIN stays s ON s.id = q.stay_id WHERE q.checkin_date = ? AND q.status != 'sent' "
        "ORDER BY s.checkin_date, u.name", (today.isoformat(),)).fetchall()
    welcome = [r for r in rows if (r["kind"] or "arrival") == "arrival"]
    midstay = [r for r in rows if r["kind"] == "midstay"]
    from .main import reviews_due  # local import: main imports this module's caller
    due = reviews_due(conn, today)

    lines = []
    if welcome:
        lines.append(f"WELCOME MESSAGES ({len(welcome)})")
        for r in welcome:
            state = "ready to send" if r["status"] == "ready" else "NEEDS SETUP"
            lines.append(f"  • {r['unit_name']} — {r['guest_name']} ({_arrives(r['arrival_date'], today)}): {state}")
            if r["status"] == "needs_setup" and r["error"]:
                lines.append(f"      {r['error']}")
        lines.append("")
    if midstay:
        lines.append(f"MID-STAY CHECK-INS ({len(midstay)})")
        for r in midstay:
            lines.append(f"  • {r['unit_name']} — {r['guest_name']}")
        lines.append("")
    if due:
        lines.append(f"REVIEWS DUE ({len(due)})")
        for r in due:
            left = "last day!" if r["days_left"] <= 0 else f"{r['days_left']} days left"
            drafts = "drafts ready" if r["has_drafts"] else "no drafts yet"
            lines.append(f"  • {r['unit_name']} — {r['guest_name']}: {left}, {drafts}")
        lines.append("")

    needs = sum(1 for r in welcome if r["status"] != "ready")
    parts = []
    if welcome:
        parts.append(f"{len(welcome)} welcome message{'s' if len(welcome) != 1 else ''}")
    if midstay:
        parts.append(f"{len(midstay)} check-in note{'s' if len(midstay) != 1 else ''}")
    if due:
        parts.append(f"{len(due)} review{'s' if len(due) != 1 else ''} due")
    subject = "Greeting Studio: " + (", ".join(parts) if parts else "nothing to send today")
    if needs:
        subject += f" ({needs} need{'s' if needs == 1 else ''} setup)"
    body = "\n".join(lines) if lines else "Nothing waiting today.\n"
    if ".ts.net" in link or "://100." in link:
        where = "with Tailscale on"
    elif "127.0.0.1" in link or "localhost" in link:
        where = "on the AI PC; set a Tailscale link under Settings to open it from your phone"
    else:
        where = "from home"
    body += (f"\nOpen the dashboard ({where}): {link}\n"
             "Copy & open each message there, paste it into Airbnb, then click \"I sent it\".\n")
    return subject, body, len(welcome) + len(midstay) + len(due)


def send_email(subject: str, body: str, to: str) -> None:
    user = os.environ.get("SMTP_USER", "").strip()
    password = os.environ.get("SMTP_PASSWORD", "").strip()
    if not (user and password):
        raise NotConfigured("Email login isn't set: add SMTP_USER and SMTP_PASSWORD to the .env "
                            "file (see .env.example), then restart the app.")
    if not to.strip():
        raise NotConfigured("No address to send to: set it under Settings → Morning summary email.")
    host = os.environ.get("SMTP_HOST", "smtp.gmail.com").strip()
    port = int(os.environ.get("SMTP_PORT", "587"))
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = to.strip()
    msg.set_content(body)
    with smtplib.SMTP(host, port, timeout=30) as s:
        s.starttls()
        s.login(user, password)
        s.send_message(msg)


def send_morning_summary(force: bool = False) -> dict:
    """Called after the morning job. Skips quietly when switched off or when
    there's nothing to do (unless force, e.g. the Settings test button)."""
    conn = db.connect()
    try:
        if not force and db.get_setting(conn, "summary_email_enabled", "0") != "1":
            return {"sent": False, "reason": "off"}
        subject, body, count = build_summary(conn)
        if count == 0 and not force:
            return {"sent": False, "reason": "nothing to do"}
        try:
            send_email(subject, body, db.get_setting(conn, "summary_email_to", ""))
        except Exception as e:
            db.set_setting(conn, "summary_email_last_error", f"{db.now()} {e}")
            conn.commit()
            raise
        db.set_setting(conn, "summary_email_last_sent", db.now())
        db.set_setting(conn, "summary_email_last_error", "")
        conn.commit()
        return {"sent": True, "subject": subject}
    finally:
        conn.close()
