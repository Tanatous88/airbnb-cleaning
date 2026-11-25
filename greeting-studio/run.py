#!/usr/bin/env python3
"""Start Guest Greeting & Review Studio:  python run.py

Listens on this computer only (127.0.0.1) by default. The app has no login,
holds a logged-in Airbnb host session and can send messages to guests, so it
must not be open to every device on the home network.

To use it from your phone or another computer, put it behind Tailscale Serve
(reachable only by your own Tailscale devices, with HTTPS), e.g.:
    tailscale serve --bg --https=8443 http://127.0.0.1:8321
then open https://<this-pc>.<your-tailnet>.ts.net:8443/

GREETING_STUDIO_HOST overrides the address it listens on (e.g. 0.0.0.0 for the
whole LAN). Don't set it to the Tailscale IP: the watchdog checks the app at
http://127.0.0.1:8321 and would keep restarting it.

The auto-reloader is off, so after `git pull` restart the app with
start_app.ps1 (Windows) for new code to take effect.
"""
import os

import uvicorn

HOST = os.environ.get("GREETING_STUDIO_HOST", "127.0.0.1").strip() or "127.0.0.1"
PORT = 8321

if __name__ == "__main__":
    print("Guest Greeting & Review Studio")
    print(f"  Listening on: http://{HOST}:{PORT}")
    uvicorn.run("app.main:app", host=HOST, port=PORT, reload=False)
