# Proactive alerts via an OpenClaw cron

Part 1 already writes flagged issues to SQLite every 5 minutes. This is the
push side: something that notices a *new* critical and tells a human, without
waking a model.

## Why a cron and not a webhook

OpenClaw's `webhook` is **outbound only** — a cron job POSTs its finished
result to a URL you nominate. There is no inbound receiver for Part 1 to post
an alert *into*, so the "generic webhook to the gateway" shape in
`.env.example` has nothing to talk to on an OpenClaw box. (Port 8787 belongs to
the Telegram channel's own listener, not a general endpoint.)

The direction that works is the gateway pulling: a scheduled job reads the
database Part 1 already wrote.

## Shape

A **command** cron, not an agent turn — `payload.kind: "command"` runs a script
in the Gateway process and records stdout, with no model in the path and no
token cost on the (overwhelmingly common) quiet run.

```bash
openclaw cron create "unifi-critical-alert" \
  --cron "*/5 * * * *" --tz "America/Los_Angeles" --exact \
  --description "Emails newly-opened critical UniFi issues. No model in the path." \
  --command-argv '["C:/Program Files/Git/bin/bash.exe","C:/Users/gocou/.openclaw/workspace/scripts/unifi-critical-alert.sh"]' \
  --command-cwd 'C:\Users\gocou\.openclaw\workspace' \
  --timeout-seconds 120

# Delivery defaults to `announce`, which would post the script's stdout to chat
# on every run, including the quiet ones. The script mails for itself:
openclaw cron edit <job-id> --no-deliver
```

Two quoting notes, both learned the hard way:

- Run `openclaw cron create` from **bash, not PowerShell 5.1**. PowerShell
  mangles the inner quotes of `--command-argv`'s JSON when passing it to a
  native executable, and the CLI rejects it with "must be a JSON array of
  strings". Escaping does not reliably fix it.
- `--command-argv` takes an explicit argv array, which avoids `sh -lc` and the
  space in `C:/Program Files/...`.

## The script

`unifi-critical-alert.example.sh` in this directory is the template; the live
copy sits in the OpenClaw workspace (`scripts/unifi-critical-alert.sh`) with
real paths and a real mail address filled in. It:

1. Snapshots the watermark file.
2. Runs `python -m unifi_monitor.cli critical --since-file <mark> --minutes 30`.
3. Exits quietly if there is no output.
4. Otherwise mails the list plus a `cli status` block via himalaya.
5. **Rolls the watermark back if the mail fails**, and exits non-zero. Without
   that, a delivery failure would consume the alert permanently — the
   watermark would have advanced past an issue nobody was ever told about.

It exports `UNIFI_DB_PATH` itself: the Gateway launches command payloads with
almost no environment, exactly like Task Scheduler does for Part 1.

## Verifying

```bash
openclaw cron run <job-id>      # fire once now
openclaw cron show <job-id>     # status: ok, and `diagnostic:` holds stdout
openclaw cron list
```

A quiet run reports `diagnostic: No new critical issues.` — that string in the
cron history is the evidence the job ran and found nothing, which is different
from the job not running at all.
