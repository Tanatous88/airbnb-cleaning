# airbnb-cleaning

Two unrelated things live here.

## `index.html`

A single-page cleaning sign-up sheet for the rental. Static, no build step —
open it in a browser or serve the directory.

## `unifi_monitor/` — UniFi network monitoring

A polling service that watches the UniFi controller at `192.168.1.1` and writes
flagged issues to SQLite, plus a separate read-only layer that explains those
issues to an LLM and proposes (never executes) fixes.

The two halves are deliberately decoupled: the poller runs from cron or Task
Scheduler with no LLM in the path, so a model outage can never cause a missed
poll or a missed alert.

- **Docs:** [`unifi_monitor/README.md`](unifi_monitor/README.md)
- **Config:** [`unifi_monitor/.env.example`](unifi_monitor/.env.example),
  [`deploy/config.example.json`](deploy/config.example.json)
- **Deployment:** [`deploy/`](deploy/) — cron, systemd, Windows Task Scheduler,
  and the MCP server config for the gateway

```bash
python3 -m unifi_monitor.cli check      # verify controller connectivity
python3 -m unifi_monitor.cli poll       # one polling cycle
python3 -m unifi_monitor.cli status     # what's wrong right now
python3 -m unifi_monitor.cli ask "cow cam"

python3 -m unittest discover -s tests   # 49 tests, no controller needed
```

Python 3.9+, standard library only.
