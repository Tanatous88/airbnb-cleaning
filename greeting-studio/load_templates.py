#!/usr/bin/env python3
"""One-shot loader: install the harmonized Three Forks welcome messages as each
unit's template, pre-fill Wi-Fi amenities, and align the audit checklist with
the harmonized section structure.

Run from the greeting-studio folder (venv active):  python load_templates.py

Safe to re-run — each run saves a NEW template version (history is kept) and
merges amenities without clobbering other values you've entered.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from app import db  # noqa: E402

TEMPLATES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")

# unit name -> template file
UNITS = {
    "The Bunkhouse": "bunkhouse.txt",
    "Three Forks Garage": "garage.txt",
    "The Studio": "studio.txt",
    "The Midnight Nook": "midnight_nook.txt",
    "The Cosmopolitan": "cosmopolitan.txt",
    "The Cottage": "cottage.txt",
}

# Wi-Fi credentials (and any other amenity values) live in local_values.json,
# which is gitignored — this repo is public, so secrets never go in git.
# See local_values.example.json for the shape.
LOCAL_VALUES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "local_values.json")

# Audit checklist matching the harmonized welcome-message structure.
# (Checkout lives in the separate standalone checkout reminder, so it is
# deliberately NOT a required item here.)
CORE_ITEMS = [
    {"key": "greeting", "label": "Personalized greeting",
     "keywords": ["hi ", "welcome"], "merge_fields": ["guest_first_name"]},
    {"key": "address", "label": "Address & arrival instructions",
     "keywords": ["address", "arrival", "directions", "entrance"], "merge_fields": []},
    {"key": "parking", "label": "Parking",
     "keywords": ["park", "parking", "driveway", "street parking"], "merge_fields": []},
    {"key": "entry", "label": "Keyless check-in",
     "keywords": ["door code", "entry code", "access code", "keyless", "smart lock", "keypad", "unlock"],
     "merge_fields": []},
    {"key": "wifi", "label": "Wi-Fi",
     "keywords": ["wifi", "wi-fi"], "merge_fields": ["wifi_network", "wifi_password"]},
    {"key": "rules", "label": "Property rules & etiquette",
     "keywords": ["quiet hours", "smoke-free", "non-smoking", "rules", "etiquette", "pet policy"],
     "merge_fields": []},
    {"key": "arrival_time", "label": "Arrival time ask (monkey bread!)",
     "keywords": ["arrival time", "estimated arrival", "monkey bread"], "merge_fields": []},
    {"key": "contact", "label": "Contact info",
     "keywords": ["call", "text", "message us", "contact", "reach out", "questions"], "merge_fields": []},
]

HOST_SIGNATURE = "Warmly,\nBruce & Sue\nThree Forks Accommodations"


def main() -> None:
    db.init_db()
    local_values = {}
    if os.path.exists(LOCAL_VALUES_PATH):
        with open(LOCAL_VALUES_PATH, encoding="utf-8") as f:
            local_values = json.load(f)
    else:
        print("(!) No local_values.json found — templates load, but Wi-Fi values won't be "
              "pre-filled. Copy local_values.example.json to local_values.json and fill it in, "
              "or enter Wi-Fi in each unit's Amenities form in the app.\n")

    conn = db.connect()
    try:
        for unit_name, filename in UNITS.items():
            unit = conn.execute("SELECT * FROM units WHERE name = ?", (unit_name,)).fetchone()
            if not unit:
                print(f"!! Unit not found in DB, skipping: {unit_name}")
                continue
            path = os.path.join(TEMPLATES_DIR, filename)
            with open(path, encoding="utf-8") as f:
                content = f.read().strip()
            version = db.save_template(conn, unit["id"], content, source="manual",
                                       note="Harmonized welcome message")
            wifi_note = ""
            values = local_values.get(unit_name)
            if values:
                amenities = json.loads(unit["amenities"] or "{}")
                amenities.update(values)
                conn.execute("UPDATE units SET amenities = ? WHERE id = ?",
                             (json.dumps(amenities), unit["id"]))
                wifi_note = f", Wi-Fi set to {values.get('wifi_network', '?')!r}"
            print(f"✓ {unit_name}: template v{version} loaded{wifi_note}")

        db.set_setting(conn, "core_items", json.dumps(CORE_ITEMS, indent=2))
        db.set_setting(conn, "host_signature", HOST_SIGNATURE)
        conn.commit()
        print("✓ Audit checklist updated to harmonized structure "
              "(greeting, address, parking, keyless, Wi-Fi, rules, arrival-time ask, contact)")
        print("✓ Host signature set to 'Warmly, Bruce & Sue — Three Forks Accommodations'")
        print("\nDone. Refresh the app and check the Audit Templates tab — all six should be green.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
