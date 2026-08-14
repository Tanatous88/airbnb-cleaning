"""SQLite storage for Guest Greeting & Review Studio."""
import json
import os
import sqlite3
from datetime import datetime, timezone

DB_PATH = os.environ.get("GREETING_STUDIO_DB") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "greeting_studio.db"
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS units (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    airbnb_listing_id TEXT UNIQUE,
    listing_title TEXT DEFAULT '',
    profile TEXT DEFAULT '',
    amenities TEXT DEFAULT '{}',          -- JSON: door code, wifi, parking, quirks...
    listing_description TEXT DEFAULT '',  -- pasted Airbnb listing description
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS imports (
    id INTEGER PRIMARY KEY,
    unit_id INTEGER NOT NULL REFERENCES units(id) ON DELETE CASCADE,
    guest_name TEXT DEFAULT '',
    stay_dates TEXT DEFAULT '',
    format TEXT DEFAULT 'text',
    raw_text TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS thread_messages (
    id INTEGER PRIMARY KEY,
    import_id INTEGER NOT NULL REFERENCES imports(id) ON DELETE CASCADE,
    unit_id INTEGER NOT NULL REFERENCES units(id) ON DELETE CASCADE,
    sender TEXT NOT NULL,                 -- 'host' | 'guest'
    body TEXT NOT NULL,
    is_greeting INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS templates (
    id INTEGER PRIMARY KEY,
    unit_id INTEGER NOT NULL REFERENCES units(id) ON DELETE CASCADE,
    version INTEGER NOT NULL,
    content TEXT NOT NULL,
    source TEXT DEFAULT 'manual',         -- 'synthesized' | 'bootstrap' | 'manual'
    note TEXT DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS stays (
    id INTEGER PRIMARY KEY,
    unit_id INTEGER NOT NULL REFERENCES units(id) ON DELETE CASCADE,
    guest_name TEXT NOT NULL,
    checkin_date TEXT NOT NULL,           -- YYYY-MM-DD
    checkout_date TEXT NOT NULL,
    booking_message TEXT DEFAULT '',
    guest_messages TEXT DEFAULT '',       -- pasted messages from the stay (for reviews)
    status TEXT DEFAULT 'pending',        -- pending | drafted | reviewed | sent
    draft_greeting TEXT DEFAULT '',
    merge_values TEXT DEFAULT '{}',       -- JSON of merge-field values used in the draft
    confirmation_code TEXT DEFAULT '',    -- Airbnb reservation code (for deep link)
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sent_greetings (
    id INTEGER PRIMARY KEY,
    stay_id INTEGER NOT NULL REFERENCES stays(id) ON DELETE CASCADE,
    content TEXT NOT NULL,
    sent_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reviews (
    id INTEGER PRIMARY KEY,
    stay_id INTEGER NOT NULL REFERENCES stays(id) ON DELETE CASCADE,
    humor_level TEXT NOT NULL,            -- subtle | medium | full_whimsy
    content TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS daily_queue (
    id INTEGER PRIMARY KEY,
    stay_id INTEGER NOT NULL REFERENCES stays(id) ON DELETE CASCADE,
    unit_id INTEGER NOT NULL REFERENCES units(id) ON DELETE CASCADE,
    guest_name TEXT NOT NULL,
    checkin_date TEXT NOT NULL,
    message TEXT DEFAULT '',
    status TEXT DEFAULT 'ready',          -- ready | needs_setup | sent | failed
    error TEXT DEFAULT '',
    personalization_source TEXT DEFAULT '',  -- scraped | stored | none
    thread_ref TEXT DEFAULT '',           -- Airbnb confirmation code used for the thread
    created_at TEXT NOT NULL,
    sent_at TEXT DEFAULT ''
);
"""

DEFAULT_CORE_ITEMS = [
    {"key": "entry", "label": "Door code / entry instructions",
     "keywords": ["door code", "keypad", "lockbox", "entry", "front door", "smart lock", "unlock"],
     "merge_fields": ["door_code"]},
    {"key": "parking", "label": "Parking instructions",
     "keywords": ["park", "parking", "driveway", "street parking"], "merge_fields": []},
    {"key": "wifi", "label": "WiFi network & password",
     "keywords": ["wifi", "wi-fi", "network", "internet"], "merge_fields": ["wifi_password", "wifi_network"]},
    {"key": "checkin_time", "label": "Check-in time",
     "keywords": ["check-in", "check in", "checkin"], "merge_fields": ["checkin_time"]},
    {"key": "checkout", "label": "Checkout basics",
     "keywords": ["checkout", "check-out", "check out", "departure"], "merge_fields": ["checkout_time"]},
    {"key": "quirks", "label": "House quirks / special notes",
     "keywords": ["quirk", "note that", "please note", "thermostat", "heads up", "keep in mind"],
     "merge_fields": []},
    {"key": "contact", "label": "Contact info / how to reach us",
     "keywords": ["reach", "contact", "text me", "call", "message us", "questions"], "merge_fields": []},
]

SEED_UNITS = [
    {
        "name": "The Bunkhouse",
        "airbnb_listing_id": "766905045971023541",
        "listing_title": "The Bunkhouse",
        "profile": ("Rustic cabin next to the stables with mini Highland cattle, fainting goats, "
                    "and mini pigs. Guests can feed and interact with the animals. "
                    "Full kitchen, WiFi, smart TV."),
    },
    {
        "name": "Three Forks Garage",
        "airbnb_listing_id": "771277515503423456",
        "listing_title": "Three Forks Garage",
        "profile": "",
    },
    {
        "name": "The Midnight Nook",
        "airbnb_listing_id": "1609878324522289416",
        "listing_title": "The Midnight Nook",
        "profile": "",
    },
    {
        "name": "The Cosmopolitan",
        "airbnb_listing_id": "1610535426810066322",
        "listing_title": "The Cosmopolitan",
        "profile": "",
    },
    {
        "name": "The Studio",
        "airbnb_listing_id": "1502983207159805344",
        "listing_title": "The Studio",
        "profile": "",
    },
    {
        "name": "The Cottage",
        "airbnb_listing_id": "1745268151326412418",
        "listing_title": "Historic Cottage on Pioneer Hill",
        "profile": (
            "Historic Cottage on Pioneer Hill — 510 SE High Street, Pullman, WA 99163 "
            "(Pioneer Hill neighborhood, City of Pullman). 3 bedrooms / 2 bathrooms, entire home. "
            "Renovated 1930s home: full 6-month remodel blending historic charm with modern finishes. "
            "Bonus media room plus an entertainment/recreation area. Near WSU; guest demand follows "
            "WSU event cycles (game weekends, graduation, move-in). Newly listed — no completed stays "
            "or message history yet."
        ),
    },
]


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


DEFAULT_LISTING_ALIASES = {
    "bunkhouse": "The Bunkhouse",
    "mini cows": "The Bunkhouse",
    "rustic retreat": "The Bunkhouse",
    "garage studio": "Three Forks Garage",
    "garage": "Three Forks Garage",
    "fun music": "The Studio",
    "karaoke": "The Studio",
    "midnight nook": "The Midnight Nook",
    "cosmopolitan": "The Cosmopolitan",
    "modern 2br": "The Cosmopolitan",
    "chef's kitchen": "The Cosmopolitan",
    "cottage": "The Cottage",
    "pioneer hill": "The Cottage",
}


def init_db() -> None:
    conn = connect()
    conn.executescript(SCHEMA)
    # Lightweight migrations for DBs created before these columns existed
    try:
        conn.execute("ALTER TABLE units ADD COLUMN ics_url TEXT DEFAULT ''")
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute("ALTER TABLE stays ADD COLUMN phone_last4 TEXT DEFAULT ''")
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute("ALTER TABLE daily_queue ADD COLUMN personalization_source TEXT DEFAULT ''")
    except sqlite3.OperationalError:
        pass
    # Seed units (idempotent — keyed on airbnb_listing_id)
    for u in SEED_UNITS:
        existing = conn.execute(
            "SELECT id FROM units WHERE airbnb_listing_id = ?", (u["airbnb_listing_id"],)
        ).fetchone()
        if not existing:
            conn.execute(
                "INSERT INTO units (name, airbnb_listing_id, listing_title, profile, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (u["name"], u["airbnb_listing_id"], u["listing_title"], u["profile"], now()),
            )
    # Seed settings
    defaults = {
        "core_items": json.dumps(DEFAULT_CORE_ITEMS, indent=2),
        "host_signature": "— Your hosts",
        "anthropic_model": os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6"),
        # Cheaper/faster model for the automated daily draft queue (runs
        # unattended every morning, indefinitely — this is where API cost
        # actually accumulates). Template synthesis, bootstrap, audit fixes,
        # and reviews stay on anthropic_model since those are click-triggered,
        # infrequent, and quality-sensitive.
        "queue_model": os.environ.get("ANTHROPIC_QUEUE_MODEL", "claude-haiku-4-5"),
        "default_checkin_time": "3:00 PM",
        "default_checkout_time": "11:00 AM",
        "hosting_ics_url": "",
        "listing_aliases": json.dumps(DEFAULT_LISTING_ALIASES, indent=2),
    }
    for k, v in defaults.items():
        conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (k, v))
    conn.commit()
    conn.close()


def get_setting(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def get_core_items(conn: sqlite3.Connection) -> list:
    try:
        return json.loads(get_setting(conn, "core_items", "[]"))
    except json.JSONDecodeError:
        return DEFAULT_CORE_ITEMS


def latest_template(conn: sqlite3.Connection, unit_id: int):
    return conn.execute(
        "SELECT * FROM templates WHERE unit_id = ? ORDER BY version DESC LIMIT 1", (unit_id,)
    ).fetchone()


def save_template(conn: sqlite3.Connection, unit_id: int, content: str,
                  source: str = "manual", note: str = "") -> int:
    prev = latest_template(conn, unit_id)
    version = (prev["version"] + 1) if prev else 1
    conn.execute(
        "INSERT INTO templates (unit_id, version, content, source, note, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (unit_id, version, content, source, note, now()),
    )
    return version
