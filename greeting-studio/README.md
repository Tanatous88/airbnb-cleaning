# Guest Greeting & Review Studio

A local web app for managing Airbnb check-in messages and host reviews across your
short-term-rental portfolio. FastAPI + SQLite backend, vanilla-JS single-page frontend.
All text generation runs through the Anthropic API (Claude, model `claude-sonnet-4-6` by
default) — no guest data leaves your machine except the text sent to Anthropic.

## Setup

**Windows (PowerShell or Command Prompt):**

```powershell
cd greeting-studio
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env        # then edit .env and paste your ANTHROPIC_API_KEY
python run.py                 # → http://127.0.0.1:8321
```

If `python` isn't found, install it from https://www.python.org/downloads/ (check
"Add python.exe to PATH" in the installer), or use `py` instead of `python`.

**Mac / Linux:**

```bash
cd greeting-studio
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # then edit .env and paste your ANTHROPIC_API_KEY
python run.py                 # → http://127.0.0.1:8321
```

The SQLite database (`greeting_studio.db`) is created automatically and seeded with your
six units (The Bunkhouse, Three Forks Garage, The Midnight Nook, The Cosmopolitan,
The Studio, The Cottage). Add future properties with the **Add Unit** form on the Units tab.

Optional — browser-automation assist (fills the Airbnb message box, never sends):

```bash
pip install playwright
playwright install chromium
```

## Workflow overview

1. **Units & Templates** — import your past greeting threads per unit, then click
   *Synthesize from history* to have Claude build a base template with merge fields
   (`{{guest_first_name}}`, `{{door_code}}`, `{{checkin_time}}`, …). Edit and save —
   every save is a new version with full history. Fill in each unit's
   **Amenities & operational details** (door code, WiFi, parking, quirks); those become
   the merge-field values used when drafting.
2. **Cold-start (The Cottage)** — a unit with no history can't synthesize, so use
   *Bootstrap from donor unit*: pick The Studio as the donor and Claude keeps its
   structure/tone but rewrites every unit-specific detail for the Cottage (historic-home
   charm, media/rec room, Pioneer Hill near WSU). Fill the amenities form and paste the
   Airbnb listing description first for a richer result; placeholders like
   `[ADD PARKING INSTRUCTIONS]` mark anything Claude couldn't know.
3. **Audit Templates** — a red/yellow/green matrix of units × core-info items
   (entry, parking, WiFi, check-in time, checkout, quirks, contact). Yellow = present but
   hardcoded (stale-info risk — e.g. a door code typed into the template instead of
   `{{door_code}}`). Click any cell to see the template with matching lines highlighted
   and get a Claude-drafted fix. The checklist itself is editable on the Settings page.
4. **Stays** — add stays manually, by CSV, or by importing the Airbnb **.ics** calendar
   (see below). Paste each guest's booking message into the stay. The Dashboard flags any
   stay checking in **today or tomorrow** with no greeting sent, sorted to the top, plus a
   7-day lookahead. Status flow: `pending → drafted → reviewed → sent`.
5. **Draft Greeting** — one click sends Claude the unit template + stay details + the
   guest's booking message. The review screen shows the editable draft side-by-side with
   the template and every merge-field value used (verify the door code and dates!).
   Nothing is finalized without your approval:
   - **Approve → Copy + open Airbnb**: copies the message to your clipboard and opens the
     reservation page (deep link via confirmation code) or the hosting inbox — you paste
     and press send yourself.
   - **Browser assist** (optional, Playwright): opens a Chromium window on the thread and
     fills the message box, then stops. It *never* sends. If Airbnb's DOM defeats it, the
     clipboard path is the fallback. Log in to Airbnb in that window the first time; the
     profile persists in `.pw-profile/`.
   - **Mark as sent & log**: records the final text against the stay.
6. **Reviews** — the morning after checkout, three host-review variants (subtle /
   medium / full whimsy) are drafted automatically from the guest's whole Airbnb thread:
   funny and clever but always kind, never negative or private, closing with a genuine
   recommendation. The Dashboard's **Reviews due** list shows every guest still waiting
   for a review and how many of Airbnb's 14 days are left. On the stay page:
   **Regenerate reviews**, **Copy**, **Open in Airbnb (fill only)** — opens the review
   form and types the text into the public-review box, but never picks star ratings and
   never presses Submit — and **Mark review posted** to clear it from the list.

## Day-of-arrival queue + one-click send

Every morning at **7:00 AM Pacific** (and on app start, if the 7 AM run was missed —
e.g. the machine was asleep), a scheduled job first **auto-syncs the calendars**,
then drafts a welcome message for every stay checking in **today**, fully unattended:

**Calendar auto-sync** replaces manual .ics imports. Two feed types combine:
- Each unit's **Airbnb iCal feed URL** (unit page → "Airbnb calendar feed URL";
  from Airbnb → Calendar → Availability → Connect to another website). These give
  dates + confirmation codes, but Airbnb omits guest names.
- Optionally, the **"Airbnb Hosting Schedules"** Google calendar's secret iCal
  address (Settings → Calendar auto-sync). Its enriched events supply guest names
  and party size, merged into the same stays by confirmation code. Listing-name →
  unit mapping is the editable `listing_aliases` JSON in Settings.

If the calendar feeds left out a guest's name, the queue reads it from the Airbnb
message thread ("Sent by Melissa · Booker") and fills it in on the stay. A stay whose
name still can't be found is queued as **needs setup** — the app will never send
"Hi Guest," to anyone.

The Cosmopolitan and The Midnight Nook use Airbnb-synced locks, so when a guest has no
phone number on file the door code reads "listed in your Airbnb app" instead of
blocking the message (`door_code_fallback` in the unit's amenities, set by
`load_templates.py`).

**Returning guests** (same full name, or same first name + phone last 4, at any unit)
get a "returning guest" badge, and the drafted greeting welcomes them back.

**Mid-stay check-in:** the day after arrival, stays of two nights or more also get a
short "how's everything?" note in the queue — a chance to fix a problem before it
becomes a review. Edit the wording or turn it off under Settings → Automatic drafts.
Like everything else, it only goes out when you click Send.

- Uses the unit's latest template with all merge fields substituted; if the guest
  left a booking message (or the best-effort Airbnb thread reader finds their
  messages), Claude weaves in one personalized opening line in your host voice.
- A draft with any unresolved value (e.g. Wi-Fi missing from the unit's Amenities)
  is flagged **needs setup** and physically cannot be sent until fixed + requeued.
- The **Today's Arrivals** section at the top of the Dashboard shows one card per
  guest: editable draft + a single **Send** button. A desktop notification summarizes
  the morning run.

**Send** is the only human gate — clicking it is your approval. It drives the
persistent Playwright browser (same `.pw-profile/` login you use for the fill-only
assist) to open the reservation thread, paste the message, and submit it. On
success the row is marked sent and logged to the stay's history; on failure
(Airbnb changes its markup periodically) the card shows the actual error with a
**Retry** button — failures are never silent. If your Airbnb login lapses, a
"session expired" banner appears; open the fill-only assist once and log back in.
Every send attempt is also appended to `send_audit.log`, a plain-text record
independent of the database.

The app must be running for the morning job — see **Keeping it running** below (the
startup catch-up builds the queue immediately if 7 AM was missed). Sends require
Playwright: `pip install playwright && playwright install chromium`.

## Keeping it running (Windows)

Run once, in PowerShell **as Administrator**, from this folder:

```powershell
powershell -ExecutionPolicy Bypass -File .\install_autostart.ps1
```

This registers a Task Scheduler task that starts the app hidden when you log in, and
re-checks every 10 minutes. The launcher (`start_hidden.ps1`) is a watchdog: if the app
stops answering for 3 minutes it is killed (including uvicorn's child process, which
used to keep port 8321 locked) and restarted. Output goes to `app.log` / `app_error.log`.

| Script | Does |
|---|---|
| `start_app.ps1` | Restart the app now (re-enables the task) |
| `stop_app.ps1` | Stop the app and pause auto-start until `start_app.ps1` |
| `uninstall_autostart.ps1` | Remove the task completely |

## Health page

The **Health** tab replaces the old `check_*.py` / PowerShell debugging scripts:

- whether the Airbnb login is still valid, and when the calendars last synced (with a
  **Sync now** button) and the morning queue / review auto-draft last ran;
- per unit: calendar feed set, template version, and how many stays came with a
  phone number for the door code;
- **Needs attention** — stays in the next 14 days missing a name, door code or
  confirmation code, each with a Fix link;
- duplicate stays / queue cards, with a one-click **Merge duplicates**, and whether
  the database safeguards against new duplicates are on;
- a warning if files in the app folder were changed but never committed (for example
  by another Claude session) — `git pull` refuses to run until that's resolved.

## Exporting / pasting Airbnb threads

Airbnb has no host-messaging API, so imports are copy/paste:

- **Plain text**: open the thread in the Airbnb inbox, copy it, and prefix each message
  with `Host:` or `Guest:` (multi-line messages are fine — everything until the next
  prefix belongs to the same message). If you fill in the *Guest name* field, lines
  starting with that name are recognized too.
- **CSV**: columns `sender,message` (sender = `host` or `guest`).
- **JSON**: a list of `{"sender": "host", "message": "..."}` objects.

Host messages that look like routine check-in greetings (WiFi, door code, parking, etc.)
are auto-flagged ✅; you can toggle any message's classification before synthesizing.
Sample files to try are in `sample_data/`.

## Importing stays from Google Calendar (.ics)

Your Airbnb bookings sync to Google Calendar, and Airbnb itself exposes an iCal feed:

1. Easiest: Airbnb → Calendar → Availability settings → **Connect to another website** →
   copy the export link, open it (or `curl` it) and save as a `.ics` file.
   Or from Google Calendar: Settings → your Airbnb calendar → **Export**.
2. On the Stays tab, choose the unit, kind = `.ics`, pick the file (or paste its
   contents), and Import. `Reserved - <guest>` events become stays; "Not available"
   blocks are skipped; the reservation confirmation code is captured from the event
   description and powers the deep link back to the reservation.
3. Booking messages aren't in the calendar feed — paste each guest's booking message
   into the stay afterward.

`sample_data/sample_calendar.ics` shows the expected shape.

## Prompts

Every Claude prompt is a plain text file in `prompts/` — edit them to tune the voice
without touching code:

| File | Used for |
|---|---|
| `synthesize_template.txt` | Building a base template from greeting history |
| `bootstrap_template.txt` | Cold-start template for a new unit from a donor unit |
| `draft_greeting.txt` | Personalized per-stay check-in message |
| `audit_fix.txt` | Suggested fix for an audit finding |
| `generate_review.txt` | Guest review variants (3 humor levels) |

`{{placeholders}}` in the files are filled by the app; unknown placeholders are left
as-is (that's how merge-field names survive into templates).

## Settings

The Settings tab covers the API key (status + session override; the durable place is
`.env`), the Claude models, your host signature, default check-in/checkout times,
**Automatic drafts** (auto-drafted reviews on/off, mid-stay message on/off + wording),
calendar auto-sync, and the core-information checklist that drives the audit.

## Tests

```bash
pip install -r requirements-dev.txt
python -m pytest tests
```

The tests never call Claude or Airbnb: each uses a throwaway database, Claude is
replaced by a fake, and the browser tests run against local copies of Airbnb's
thread / reservation markup. They can't prove Airbnb hasn't changed its pages since —
if the review assist stops finding the review box, `debug_review_form.png` shows
what the page looked like.

## Project layout

```
greeting-studio/
  run.py               # start here: python run.py
  app/
    main.py            # FastAPI routes
    db.py              # SQLite schema + seed data
    claude.py          # Anthropic API wrapper + prompt loader
    parsers.py         # thread / CSV / JSON / .ics parsing
    audit.py           # consistency-audit engine
    browser_assist.py  # Playwright: read thread, send, fill-only assists
    queue.py           # calendar sync + morning queue
    maintenance.py     # Health page report + duplicate merging
  prompts/             # editable prompt files
  static/              # single-page frontend
  sample_data/         # example imports to try
  tests/               # pytest suite (python -m pytest tests)
  *.ps1                # Windows auto-start / watchdog scripts
```
