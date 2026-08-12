# UniFi network monitoring

Two programs that share one SQLite file and nothing else.

```
                        ┌──────────────────────────────────────┐
  UniFi OS 5.1.26       │  PART 1 — poller (no LLM anywhere)   │
  Network app 10.5.67   │                                      │
  192.168.1.1  ────────►│  unifi_client → detectors → issues   │
       (proxy, no TLS   │                     │                │
        verification)   │                     ▼                │
                        │              alert channel ──────────┼──► Slack
                        └─────────────────────┬────────────────┘
                                              │ writes
                                     ┌────────▼─────────┐
                                     │ unifi_monitor.db │  ◄── the only interface
                                     │  issues          │
                                     │  issue_events    │
                                     │  state_transitions
                                     │  metrics, wan_samples,
                                     │  controller_events
                                     └────────▲─────────┘
                                              │ reads (read-only, enforced)
                        ┌─────────────────────┴────────────────┐
  OpenClaw gateway ────►│  PART 2 — explain / propose          │
  "why does the cow     │  query.py  ·  remediation.py         │
   cam keep dropping"   │  mcp_server.py (stdio MCP)           │
                        └──────────────────────────────────────┘
```

Part 1 has no import of Part 2. Part 2 has no import of Part 1's polling path,
and opens the database with `mode=ro` — it is *physically* unable to disturb
the poller. A model outage, a slow gateway, or a crashed MCP server cannot
cause a missed poll or a missed alert.

Stdlib only. No pip install, no virtualenv, nothing to break at 3am.

---

## Part 1 — the poller

### What it polls (every 5 minutes by default)

| Data | Endpoint | Used for |
|---|---|---|
| Devices (APs, switches, gateway) | `stat/device` | online/offline, state, CPU/memory, per-port counters, uplink |
| Active clients | `stat/sta` | who is connected, signal, throughput |
| Known clients | `rest/user` | names, and detecting a watched client that has *stopped* being connected |
| Subsystem health | `stat/health` | WAN/WWW status, latency, ISP, WAN IP |
| Events | `stat/event`, or v2 `alert` | context for Part 2 to correlate against |
| Alarms | `stat/alarm`, or v2 `alert` | controller's own complaints |

On Network 9+ (verified on 10.5.67 / UDM Pro SE) `stat/event` and `stat/alarm`
are gone — both 404. The two feeds were merged into one paginated resource,
`GET /proxy/network/v2/api/site/<site>/alert`. The client tries the classic
endpoints first and falls back automatically, translating the v2 rows into the
classic field names, so the detectors only ever see one vocabulary. The
fallback partitions that single feed: rows the controller rates HIGH or above
become alarms, the rest become events. Nothing arrives down both paths, because
`controller_events` is unique on `(source, remote_id)`.

Its `pageSize`/`pageNumber` parameters work; `category`, `severity`, `type` and
`status` are accepted and then ignored, so filtering has to happen client-side.

Devices and health are load-bearing; the rest degrade independently. If the
events endpoint fails, the poll still records device state — and issue types
whose data source failed are *never* auto-resolved, so a partial outage cannot
silently clear a real problem.

### What it detects

State lives in SQLite, so these are all changes over time, not snapshots.

| Issue type | Trips when | Default severity |
|---|---|---|
| `device_offline` | device not reporting | warning ≥5 min, critical ≥15 min |
| `device_flapping` | ≥3 disconnects in a rolling 1h window | warning, critical at ≥6 |
| `client_offline` | *watchlisted* client gone | warning ≥10 min, critical ≥30 min |
| `client_flapping` | watchlisted client reconnecting repeatedly | warning / critical |
| `client_weak_signal` | wireless client ≤ −75 dBm | warning |
| `wan_down` | health subsystem not `ok` | critical |
| `wan_failover` | active WAN's ISP / gateway / IP changed while up | warning (info for IP-only) |
| `wan_high_latency` | ≥120 ms | warning, critical ≥400 ms |
| `wan_packet_loss` | ≥2% | warning, critical ≥10% |
| `port_errors` | ≥10 errors/min on a port | warning, critical ≥100 |
| `poe_port_down` | a port that was delivering PoE lost link | warning |
| `device_high_cpu` / `device_high_memory` | ≥85% / ≥90% sustained over 3 polls | warning, CPU critical ≥95% |
| `device_not_adopted` | adoption failure, inform error, isolated | warning |
| `controller_alarm` | unarchived controller alarm | mapped from alarm key |
| `high_bandwidth` | above a configured ceiling (off by default) | warning / critical |
| `controller_unreachable` / `controller_auth_failed` | the poller itself cannot see the network | critical |

That last row matters: a monitor that goes blind and says nothing is worse
than no monitor. Failed polls are recorded as issues in the same table, and
Part 2 surfaces them in `poller_health` on every explanation so the model can
tell "AP-3 was down" apart from "we couldn't see AP-3".

Every threshold is configurable globally (env or JSON) and per-device via
`overrides` — `cow cam` can have a 3-minute fuse while `guest-ap` has 30.

Clients are opt-in by design (`UNIFI_CLIENT_WATCHLIST`). Every client's state
and throughput is still *recorded*; only watchlisted ones can raise an issue.
Phones sleeping off Wi-Fi is not an incident.

### Alerts — use Slack

**Recommendation: a Slack incoming webhook.** One URL, no SMTP relay to
maintain, no auth to expire, push notifications on a phone for free, and a
failed post is a plain HTTP error you can see. Set:

```
UNIFI_ALERT_CHANNELS=slack
SLACK_WEBHOOK_URL=https://hooks.slack.com/services/XXX/YYY/ZZZ
```

Also available, and combinable (`UNIFI_ALERT_CHANNELS=slack,webhook`):

- `webhook` — generic JSON POST. Use this as the *proactive* path to the
  gateway: Part 1 posts the bare fact, the gateway decides whether to wake the
  model. Part 1 does not know or care what is on the other end.
- `email` — SMTP, for when Slack is down or someone wants it in a mailbox.
- `stdout` — cron mails it; useful while setting up.

An alert is one line, always: `⚠️ WARNING: AP-3 (access point) offline 12m`.
No diagnosis — that is Part 2's job, and it must not be on the critical path.

Noise control: issues are deduplicated by fingerprint while open, re-alerts are
suppressed for an hour (`UNIFI_ALERT_COOLDOWN`), severity escalation always
breaks through, a resolve notice is only sent if the problem was announced,
and no more than 12 alerts leave per poll.

### Running it

```bash
python3 -m unifi_monitor.cli check    # verify credentials + connectivity
python3 -m unifi_monitor.cli poll     # one cycle, then exit  (cron / Task Scheduler)
python3 -m unifi_monitor.cli run      # loop forever          (systemd)
python3 -m unifi_monitor.cli status   # what's wrong right now
```

Scheduling (`deploy/`): `crontab.example`, `unifi-monitor.service`,
`unifi-monitor-poll.{timer,service}`, and `install-task-scheduler.ps1` for
ai-pc. Cron/Task Scheduler mode is preferred over the loop: each poll is a
process that exits, so a wedged run cannot cause a silent gap.

### Credentials

Use a **local console account** on the UniFi OS device (Settings → Admins →
Add Admin → *Local Access Only*, role Viewer). A Ubiquiti SSO/cloud account
with MFA cannot log into the local API.

Put them in an env file with `chmod 600` and point `UNIFI_MONITOR_ENV` at it;
cron and Task Scheduler start with almost no environment. Passwords are
scrubbed from anything written to the database or an alert — there is a test
for that.

`UNIFI_API_KEY` is supported but not preferred: the Integration API it unlocks
does not expose health, events, alarms or per-port counters, which is most of
what makes this useful.

---

## Part 2 — explain and propose

Same data, read-only, from the other side.

### As an MCP server

```bash
python3 -m unifi_monitor.cli mcp        # stdio JSON-RPC, zero dependencies
```

See `deploy/mcp-config.example.json`. Eleven tools:

**OpenClaw does not use the `mcpServers` key.** It keeps servers under
`mcp.servers` inside its own config file (`~/.openclaw/openclaw.json`), so the
generic snippet most MCP docs give you will silently do nothing. Register it
with the CLI, which validates and probes before saving:

```bash
openclaw mcp add unifi-monitor --command C:\Python311\python.exe \
  --arg -m --arg unifi_monitor.cli --arg mcp \
  --cwd C:\opt\unifi-monitor \
  --env UNIFI_DB_PATH=C:\ProgramData\unifi-monitor\unifi_monitor.db \
  --env UNIFI_ACTIONS_DB=C:\ProgramData\unifi-monitor\unifi_actions.db \
  --env UNIFI_ALLOW_WRITE_ACTIONS=false
openclaw mcp probe unifi-monitor   # -> unifi-monitor: 11 tools
```

`UNIFI_DB_PATH` has to be in the server's `env` block. The gateway launches the
MCP server directly, so there is no shell to source an env file from.

| Tool | Answers |
|---|---|
| `unifi_network_status` | "is anything wrong right now?" |
| `unifi_recent_issues` | the flagged-issue list, filterable |
| `unifi_explain_issue` | full evidence package for one issue |
| `unifi_investigate_device` | "why does the cow cam keep dropping" |
| `unifi_device_history` | raw timeline for one thing |
| `unifi_issue_patterns` | "is this getting worse / always at night?" |
| `unifi_new_critical_issues` | the proactive hook |
| `unifi_propose_remediation` | options + one-time confirmation tokens |
| `unifi_confirm_action` | execute a user-chosen action (currently refuses) |
| `unifi_reject_action` | dismiss a proposal |
| `unifi_list_proposals` | audit trail of what was offered and decided |

### As plain Python

If the gateway runs in-process, skip MCP entirely:

```python
from unifi_monitor import query

query.network_overview()
query.recent_issues(status="open", severity="critical")
query.explain_entity("cow cam")              # resolves the phrase to a MAC
report = query.explain_issue(42)
print(query.summarize_for_llm(report))       # compact text for a chat reply

query.new_critical_issues(minutes=30)        # poll this for proactive nudges
```

### The proactive path, without a model in it

`cli critical` is `new_critical_issues` with a watermark, and it prints nothing
when nothing is new — so a scheduler can treat *any* output as "say something":

```bash
python3 -m unifi_monitor.cli critical --since-file /var/lib/unifi-monitor/last_critical
```

The watermark advances only past what was actually reported, and only when
something was reported, so a quiet hour cannot skip an issue that lands between
the query and the write. A corrupt watermark falls back to `--minutes` rather
than exiting: a wedged alert path is worse than a duplicate alert.

On ai-pc this is driven by an OpenClaw **command** cron every 5 minutes
(`deploy/openclaw-cron.md`), which runs a shell script that mails anything new.
`payload.kind` is `command`, not an agent turn, and delivery is `none` — the
script does its own notifying. No model is involved unless a human asks a
question afterwards, which is the whole point of the split.

Note what this does *not* catch: `new_critical_issues` filters on
`first_seen`, so an issue that opened as a warning and later **escalated** to
critical will not appear — its `first_seen` is older than any watermark you are
likely to hold. Escalations are visible in `issue_events` (`kind='escalated'`)
if you want them too.

### What `explain_issue` returns

Facts, assembled — never a diagnosis. The model does the reasoning.

- **`what_changed`** — the transition that started it, and how long the good
  state had held beforehand.
- **`timeline`** — every observation of the issue, with long runs of repeats
  elided rather than dumped.
- **`recurrence`** — total occurrences, counts over 24h/7d/30d, median gap,
  median duration, and clustering by hour-of-day and weekday. This is what
  turns "the camera dropped" into "it drops every night around 02:00".
- **`baseline_comparison`** — each metric around the incident against that
  entity's own 7-day mean and stdev, with a `notable` flag so trivia stays
  quiet. "High" is meaningless; "higher than this device's normal" is not.
- **`correlated_activity`** — everything else that changed state, every
  controller event, every WAN sample and every other issue opened in the same
  ±15 minutes. This is where "the AP and the camera dropped together" and "the
  WAN flapped at the same moment" come from.
- **`poller_health`** — whether we actually had eyes on the network during the
  window, so a monitoring gap is never mistaken for an outage.
- **`facts`** — the above as quotable one-liners.
- **`proposed_actions`** — see below.

### Remediation is proposals only

Nothing in this repository executes a network change without a caller passing a
confirmation token that was minted when the proposal was shown to a human.
There is no auto-remediation code path, and no way to reach one from a chat
turn alone.

Each proposal carries: the exact controller call it *would* make, its blast
radius, whether it is reversible, the permission it needs, and the manual UI
steps to do it by hand instead.

Three independent guards, all of which must pass:

1. The proposal id **and** its one-time token, quoted back.
2. `UNIFI_ALLOW_WRITE_ACTIONS=true`.
3. A controller account with write permission — today's account is view-only,
   so execution refuses and says exactly which upgrade is required.

```
$ python3 -m unifi_monitor.cli confirm act_9f2a1c token…
Execution is disabled. The monitoring account is view-only; running this
needs (1) a controller account with write permission, (2)
UNIFI_ALLOW_WRITE_ACTIONS=true, and (3) an executor wired up.
would have called: POST /proxy/network/api/s/default/cmd/devmgr
                   {"cmd": "power-cycle", "mac": "…", "port_idx": 4}
```

Proposals and decisions are stored in a **separate** SQLite file
(`UNIFI_ACTIONS_DB`) so Part 2 never opens the monitoring database writable.

---

## The interface table

Part 2 depends on exactly this, and nothing else:

```sql
issues(id, fingerprint, issue_type, severity, max_severity, status,
       entity_type, entity_id, entity_name, summary,
       first_seen, last_seen, resolved_at, occurrences,
       notified_at, notified_severity,
       details,        -- JSON: thresholds, measured values, context
       trigger_data)   -- JSON: the raw controller payload that tripped it

issue_events(id, issue_id, ts, kind, severity, summary, details, trigger_data)
       -- append-only: opened | observed | escalated | resolved
```

Supporting history — `state_transitions`, `metrics`, `wan_samples`,
`controller_events`, `entity_state`, `poll_runs` — is what makes the "how often
/ vs baseline / what else moved" analysis possible. Retention is configurable
(metrics 14d, transitions 90d, issues 365d by default); the poller prunes every
six hours.

---

## Tests

```bash
python3 -m unittest discover -s tests -v
```

69 tests, no network, no controller. They drive the detectors through a fake
controller: threshold crossing and escalation, automatic resolution, rolling
window flap detection, counter resets, PoE port loss, WAN failover, watchlist
behaviour, alert cooldown/escalation rules, credential scrubbing, the read-only
guarantee, the remediation guards, and the MCP protocol handlers — plus the
env-file parser, the v2 alert translation, the mirrored port-counter rule,
alert encoding on a legacy console, and phrase-to-entity resolution.

---

## Known limits

- Client-side downtime starts from `rest/user.last_seen` when the controller
  provides it, otherwise from the first poll that noticed — so a watched client
  that vanishes right as the poller starts may under-report its first outage by
  up to one interval.
- `loss_pct` comes from the health subsystem's `drops` field, which some
  firmware reports as a counter rather than a percentage; if your controller
  does that, raise `UNIFI_WAN_LOSS_WARNING_PCT` or ignore `wan_packet_loss`.
- Per-WAN detail depends on the gateway exposing `wan1`/`wan2`. Single-WAN
  setups get the aggregate `wan` subsystem, which is enough for up/down and
  latency but cannot distinguish a failover.
- Everything is UTC, including the hour-of-day clustering in Part 2's output.
- UDM gateway LAN ports report `rx_errors` as a verbatim copy of `rx_dropped`,
  so ordinary filtered frames look like physical errors — enough to trip the
  critical threshold on a healthy link. When the two raw counters are exactly
  equal the error counter is treated as the mirror it is and the traffic is
  counted once, as drops. A port whose counters diverge is flagged normally
  from that poll on.
- The v2 alert feed is site-wide and busy (~1k rows/hour here), and it cannot
  be filtered server-side, so the fallback pages through it and stops at the
  requested window rather than reading all of it.
- On Windows, stdout and stderr are forced to UTF-8 with `errors="replace"`.
  Task Scheduler hands the process a cp1252 console, which cannot encode the
  severity emoji; without this an alert raises `UnicodeEncodeError` instead of
  being delivered.
