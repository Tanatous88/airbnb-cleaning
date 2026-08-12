#!/bin/bash
# Emails newly-opened critical UniFi issues, and says nothing when there is
# nothing new. Driven by an OpenClaw command cron — see deploy/openclaw-cron.md.
#
# There is no model in this path. Part 1 (the poller) writes issues to SQLite on
# its own schedule; this reads the flagged results through Part 2's read-only
# query layer and mails whatever is new since the last successful send.
#
# Copy somewhere local and edit the paths and MAILTO below. It is an example
# because the mail transport is site-specific: swap the himalaya call for
# sendmail, msmtp, curl to a Slack webhook, or anything else that exits non-zero
# when it fails to deliver.

PROJECT="C:/opt/unifi-monitor"
PYTHON="C:/Python311/python.exe"
HIMALAYA="C:/Users/you/AppData/Roaming/npm/himalaya"
MARK="C:/ProgramData/unifi-monitor/last_critical"
MAILTO="you@example.com"

# The gateway launches command payloads with almost no environment, and Part 2
# needs to know which database the poller is writing to.
export UNIFI_DB_PATH="C:/ProgramData/unifi-monitor/unifi_monitor.db"

cd "$PROJECT" || { echo "project root $PROJECT missing" >&2; exit 1; }

# Snapshot the watermark so a failed send can be undone.
if [ -f "$MARK" ]; then
  cp "$MARK" "$MARK.prev"
else
  rm -f "$MARK.prev"
fi

NEW=$("$PYTHON" -m unifi_monitor.cli critical --since-file "$MARK" --minutes 30)
RC=$?
if [ $RC -ne 0 ]; then
  echo "unifi_monitor.cli critical failed (exit $RC)" >&2
  exit 1
fi

if [ -z "$NEW" ]; then
  rm -f "$MARK.prev"
  echo "No new critical issues."
  exit 0
fi

COUNT=$(printf '%s\n' "$NEW" | grep -c .)
STATUS=$("$PYTHON" -m unifi_monitor.cli status 2>/dev/null)

TEMPLATE="From: ${MAILTO}
To: ${MAILTO}
Subject: [UniFi] ${COUNT} new critical issue(s)

${NEW}

--- network status ---
${STATUS}

Investigate:  python -m unifi_monitor.cli ask \"<device name>\"
"

if printf '%s\n' "$TEMPLATE" | "$HIMALAYA" template send 2>/dev/null; then
  rm -f "$MARK.prev"
  echo "Mailed ${COUNT} new critical issue(s)."
  exit 0
fi

# Delivery failed: put the watermark back so the next run retries these.
# Without this a failed send would consume the alert permanently.
if [ -f "$MARK.prev" ]; then
  mv -f "$MARK.prev" "$MARK"
else
  rm -f "$MARK"
fi
echo "FAILED to mail ${COUNT} critical issue(s); watermark rolled back for retry." >&2
exit 1
