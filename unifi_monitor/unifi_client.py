"""UniFi Network API client.

Stdlib only (urllib) on purpose: this runs from cron / Task Scheduler on a
box where "pip install broke" must never be the reason a poll was missed.

Targets a UniFi OS console (``controller_type=proxy``) — UDM/UDR/Cloud Key
gen2+, where the Network application is reverse-proxied under
``/proxy/network``. ``controller_type=direct`` speaks to a standalone
software controller instead.

Auth: a local console account (username/password) is preferred because the
classic ``/api/s/<site>/...`` endpoints it unlocks carry the interesting
telemetry — subsystem health, events, alarms, per-port counters. An API key
(``X-API-KEY``) is also sent when configured, but Ubiquiti's Integration API
exposes a much thinner slice, so anything it cannot answer is reported as
unavailable rather than silently empty.
"""

from __future__ import annotations

import gzip
import json
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar
from typing import Any

from .config import ControllerConfig
from .util import LOG, redact


class UniFiError(RuntimeError):
    """Any failure talking to the controller."""


class UniFiAuthError(UniFiError):
    """Credentials rejected, or session could not be established."""


class UniFiUnavailable(UniFiError):
    """Controller unreachable — network down, box rebooting, DNS, timeouts."""


class UniFiClient:
    def __init__(self, cfg: ControllerConfig):
        self.cfg = cfg
        self._jar = CookieJar()
        self._csrf: str | None = None
        self._logged_in = False
        self._secrets = cfg.secrets()

        ctx = ssl.create_default_context()
        if not cfg.verify_ssl:
            # Consoles ship a self-signed cert on the LAN address. This is a
            # deliberate, configured choice — never flip it on silently.
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=ctx),
            urllib.request.HTTPCookieProcessor(self._jar),
            _NoRedirect(),
        )

    # ------------------------------------------------------------------ paths

    @property
    def _api_prefix(self) -> str:
        return "/proxy/network" if self.cfg.controller_type == "proxy" else ""

    def _site_url(self, path: str) -> str:
        path = path.lstrip("/")
        return f"{self.cfg.base_url}{self._api_prefix}/api/s/{self.cfg.site}/{path}"

    def _api_url(self, path: str) -> str:
        path = path.lstrip("/")
        return f"{self.cfg.base_url}{self._api_prefix}/api/{path}"

    # ------------------------------------------------------------- transport

    def _request(
        self,
        method: str,
        url: str,
        body: Any = None,
        *,
        headers: dict[str, str] | None = None,
        allow_retry: bool = True,
    ) -> tuple[int, dict[str, str], Any]:
        data = None
        req_headers = {
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
            "User-Agent": "unifi-monitor/1.0",
        }
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            req_headers["Content-Type"] = "application/json"
        if self._csrf:
            req_headers["X-CSRF-Token"] = self._csrf
        if self.cfg.api_key:
            req_headers["X-API-KEY"] = self.cfg.api_key
        if headers:
            req_headers.update(headers)

        request = urllib.request.Request(url, data=data, method=method, headers=req_headers)

        attempt = 0
        while True:
            attempt += 1
            try:
                with self._opener.open(request, timeout=self.cfg.timeout) as resp:
                    raw = resp.read()
                    if resp.headers.get("Content-Encoding") == "gzip":
                        raw = gzip.decompress(raw)
                    resp_headers = {k.lower(): v for k, v in resp.headers.items()}
                    self._capture_csrf(resp_headers)
                    return resp.status, resp_headers, _decode(raw)
            except urllib.error.HTTPError as exc:
                raw = exc.read()
                if exc.headers is not None and exc.headers.get("Content-Encoding") == "gzip":
                    try:
                        raw = gzip.decompress(raw)
                    except OSError:
                        pass
                resp_headers = {k.lower(): v for k, v in (exc.headers or {}).items()}
                self._capture_csrf(resp_headers)
                return exc.code, resp_headers, _decode(raw)
            except (urllib.error.URLError, socket.timeout, TimeoutError, ssl.SSLError) as exc:
                if allow_retry and attempt <= max(0, self.cfg.max_retries):
                    backoff = min(8.0, 2.0**attempt)
                    LOG.warning(
                        "unifi: %s %s failed (%s), retry %s in %.0fs",
                        method,
                        _safe_url(url),
                        exc,
                        attempt,
                        backoff,
                    )
                    time.sleep(backoff)
                    continue
                raise UniFiUnavailable(
                    f"{method} {_safe_url(url)} failed after {attempt} attempt(s): {exc}"
                ) from exc

    def _capture_csrf(self, headers: dict[str, str]) -> None:
        token = headers.get("x-csrf-token") or headers.get("x-updated-csrf-token")
        if token:
            self._csrf = token

    # ------------------------------------------------------------------ auth

    def login(self) -> None:
        if self._logged_in:
            return
        if not (self.cfg.username and self.cfg.password):
            if self.cfg.api_key:
                # API-key-only mode: no session to establish.
                self._logged_in = True
                return
            raise UniFiAuthError("no username/password and no API key configured")

        # UniFi OS authenticates at the console root; the standalone
        # controller authenticates inside the Network app.
        if self.cfg.controller_type == "proxy":
            url = f"{self.cfg.base_url}/api/auth/login"
        else:
            url = f"{self.cfg.base_url}/api/login"

        # Prime the cookie jar / CSRF token the way a browser would.
        try:
            self._request("GET", f"{self.cfg.base_url}/", allow_retry=False)
        except UniFiUnavailable:
            pass

        status, _headers, payload = self._request(
            "POST",
            url,
            {"username": self.cfg.username, "password": self.cfg.password, "rememberMe": True},
        )
        if status in (400, 401, 403):
            raise UniFiAuthError(
                f"login rejected (HTTP {status}): {redact(str(payload), self._secrets)[:300]}"
            )
        if status == 429:
            raise UniFiUnavailable("login rate-limited by controller (HTTP 429)")
        if status >= 500:
            raise UniFiUnavailable(f"controller error during login (HTTP {status})")
        if status not in (200, 201):
            raise UniFiAuthError(f"unexpected login status {status}")

        if not any(c.name in ("TOKEN", "unifises") for c in self._jar):
            raise UniFiAuthError("login returned no session cookie")

        self._logged_in = True
        LOG.info("unifi: authenticated to %s as %s", self.cfg.host, self.cfg.username)

    def logout(self) -> None:
        if not self._logged_in or not self.cfg.username:
            return
        url = (
            f"{self.cfg.base_url}/api/auth/logout"
            if self.cfg.controller_type == "proxy"
            else f"{self.cfg.base_url}/api/logout"
        )
        try:
            self._request("POST", url, {}, allow_retry=False)
        except UniFiError:
            pass
        finally:
            self._logged_in = False
            self._csrf = None
            self._jar.clear()

    # ------------------------------------------------------------------ core

    def _call(self, url: str, method: str = "GET", body: Any = None) -> list[dict[str, Any]]:
        """Call an endpoint, re-authenticating once if the session expired."""
        self.login()
        status, _headers, payload = self._request(method, url, body)

        if status in (401, 403) and self.cfg.username:
            LOG.info("unifi: session expired, re-authenticating")
            self._logged_in = False
            self._csrf = None
            self._jar.clear()
            self.login()
            status, _headers, payload = self._request(method, url, body)

        if status == 404:
            raise UniFiError(f"endpoint not found: {_safe_url(url)}")
        if status in (401, 403):
            raise UniFiAuthError(f"access denied for {_safe_url(url)} (HTTP {status})")
        if status >= 500:
            raise UniFiUnavailable(f"controller error {status} for {_safe_url(url)}")
        if status != 200:
            raise UniFiError(f"unexpected status {status} for {_safe_url(url)}")

        return _unwrap(payload, url)

    # --------------------------------------------------------------- polling

    def sysinfo(self) -> dict[str, Any]:
        rows = self._call(self._site_url("stat/sysinfo"))
        return rows[0] if rows else {}

    def sites(self) -> list[dict[str, Any]]:
        return self._call(self._api_url("self/sites"))

    def devices(self) -> list[dict[str, Any]]:
        """APs, switches, gateway — the full record including port_table."""
        return self._call(self._site_url("stat/device"))

    def active_clients(self) -> list[dict[str, Any]]:
        """Currently associated/connected clients."""
        return self._call(self._site_url("stat/sta"))

    def known_clients(self) -> list[dict[str, Any]]:
        """Every client the controller has ever seen, with user-set names."""
        return self._call(self._site_url("rest/user"))

    def health(self) -> list[dict[str, Any]]:
        """Per-subsystem health: wan, www, wlan, lan, vpn."""
        return self._call(self._site_url("stat/health"))

    def events(self, within_hours: int = 2, limit: int = 200) -> list[dict[str, Any]]:
        return self._call(
            self._site_url("stat/event"),
            method="POST",
            body={"_limit": limit, "within": within_hours, "_sort": "-time"},
        )

    def alarms(self, archived: bool = False) -> list[dict[str, Any]]:
        query = urllib.parse.urlencode({"archived": "true" if archived else "false"})
        return self._call(f"{self._site_url('stat/alarm')}?{query}")

    def snapshot(self) -> dict[str, Any]:
        """One poll's worth of data.

        Sub-collections are fetched independently: losing events must not cost
        us device state, so each optional section degrades to an empty list
        with the failure recorded under ``errors``.
        """
        snap: dict[str, Any] = {"errors": {}}
        # Devices and health are the backbone — let their failures propagate.
        snap["devices"] = self.devices()
        snap["health"] = self.health()

        for key, fetch in (
            ("clients", self.active_clients),
            ("known_clients", self.known_clients),
            ("events", self.events),
            ("alarms", self.alarms),
        ):
            try:
                snap[key] = fetch()
            except UniFiError as exc:
                snap[key] = []
                snap["errors"][key] = redact(str(exc), self._secrets)
                LOG.warning("unifi: %s unavailable: %s", key, exc)
        return snap

    def __enter__(self) -> UniFiClient:
        self.login()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.logout()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """UniFi OS bounces unauthenticated calls to the login page; a 302 is
    information, not something to follow."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        if code in (301, 302, 303, 307, 308) and "/login" in (newurl or ""):
            return None
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _decode(raw: bytes) -> Any:
    if not raw:
        return None
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        return raw.decode("utf-8", "replace")


def _unwrap(payload: Any, url: str) -> list[dict[str, Any]]:
    """Classic endpoints answer ``{"meta": {"rc": "ok"}, "data": [...]}``;
    Integration API endpoints answer ``{"data": [...]}`` or a bare list."""
    if payload is None:
        return []
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        meta = payload.get("meta") or {}
        if isinstance(meta, dict) and meta.get("rc") not in (None, "ok"):
            raise UniFiError(f"{_safe_url(url)}: {meta.get('msg') or meta.get('rc')}")
        data = payload.get("data")
        if isinstance(data, list):
            return [row for row in data if isinstance(row, dict)]
        if isinstance(data, dict):
            return [data]
        return []
    raise UniFiError(f"{_safe_url(url)}: unexpected payload type {type(payload).__name__}")


def _safe_url(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    return f"{parts.path}?{parts.query}" if parts.query else parts.path
