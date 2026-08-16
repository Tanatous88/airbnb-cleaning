#!/usr/bin/env python3
"""Start Guest Greeting & Review Studio:  python run.py

Listens on the network (not just this machine) so it can be reached from
other devices on your home Wi-Fi, e.g. http://<this-PC's-LAN-IP>:8321.
There is no login on this app — anyone on the same network can open it,
see guest names/phone digits, and click Send. Fine for a trusted home
network; never expose this port to the internet (no router port-forwarding).
"""
import socket

import uvicorn


def _lan_ip() -> str:
    """Best-effort local network IP (doesn't actually send anything)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "<this-computer's-LAN-IP>"
    finally:
        s.close()


if __name__ == "__main__":
    lan_ip = _lan_ip()
    print("Guest Greeting & Review Studio")
    print(f"  On this computer:      http://127.0.0.1:8321")
    print(f"  From other devices:    http://{lan_ip}:8321")
    print("  (Windows Firewall may prompt to allow Python on first run — allow it")
    print("   for Private networks only.)")
    uvicorn.run("app.main:app", host="0.0.0.0", port=8321, reload=True)
