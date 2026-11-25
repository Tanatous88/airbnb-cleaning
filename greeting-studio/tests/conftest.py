"""Shared test setup. Tests never call Claude or Airbnb: each test gets a
fresh temporary database, Claude calls are replaced with a fake, and the API
key is blanked so an accidental real call fails instead of costing money."""
import os
import sys
import tempfile
from datetime import date, timedelta

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ["GREETING_STUDIO_DB"] = os.path.join(tempfile.mkdtemp(), "import.db")
os.environ["ANTHROPIC_API_KEY"] = ""

from app import db  # noqa: E402
from app import queue as queue_mod  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setattr(queue_mod, "AUDIT_LOG_PATH", str(tmp_path / "audit.log"))
    db.init_db()


@pytest.fixture
def conn():
    c = db.connect()
    yield c
    c.close()


def day(offset: int) -> str:
    return (date.today() + timedelta(days=offset)).isoformat()


def unit_id(conn, name: str) -> int:
    return conn.execute("SELECT id FROM units WHERE name = ?", (name,)).fetchone()["id"]


def add_stay(conn, unit: str, guest: str, checkin: int, checkout: int, code: str = "",
             phone: str = "", booking_message: str = "") -> int:
    cur = conn.execute(
        "INSERT INTO stays (unit_id, guest_name, checkin_date, checkout_date, booking_message, "
        "confirmation_code, phone_last4, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (unit_id(conn, unit), guest, day(checkin), day(checkout), booking_message, code, phone,
         db.now()))
    conn.commit()
    return cur.lastrowid


def set_template(conn, unit: str, content: str) -> None:
    db.save_template(conn, unit_id(conn, unit), content)
    conn.commit()


def set_amenities(conn, unit: str, amenities: dict) -> None:
    import json
    conn.execute("UPDATE units SET amenities = ? WHERE id = ?",
                 (json.dumps(amenities), unit_id(conn, unit)))
    conn.commit()


class FakeClaude:
    """Stands in for ask_claude: records prompts, returns canned text."""

    def __init__(self, fail_on_call: int = 0):
        self.prompts = []
        self.fail_on_call = fail_on_call

    def __call__(self, prompt, model=None, max_tokens=None, **_):
        self.prompts.append(prompt)
        if self.fail_on_call and len(self.prompts) == self.fail_on_call:
            from app.claude import ClaudeError
            raise ClaudeError("simulated API failure")
        return f"DRAFT #{len(self.prompts)}"
