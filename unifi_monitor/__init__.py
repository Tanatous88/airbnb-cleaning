"""UniFi network monitoring.

Two loosely-coupled halves that share nothing but a SQLite file:

  Part 1 (poller)  : config, unifi_client, db, detectors, issues, notify, poller
                     Knows nothing about LLMs, gateways or MCP.
  Part 2 (explain) : query, remediation, mcp_server
                     Reads the same SQLite db read-only. Never touches the poll loop.
"""

__version__ = "1.0.0"
