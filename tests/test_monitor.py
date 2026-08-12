"""Tests for both halves, driven by a fake controller.

Run: python -m unittest discover -s tests -v
"""

from __future__ import annotations

import argparse
import contextlib
import io
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

from unifi_monitor import query, remediation  # noqa: E402
from unifi_monitor.cli import cmd_critical  # noqa: E402
from unifi_monitor.config import Config  # noqa: E402
from unifi_monitor.db import Database  # noqa: E402
from unifi_monitor.detectors import Analyzer  # noqa: E402
from unifi_monitor.issues import IssueStore, Observation  # noqa: E402
from unifi_monitor.notify import Notifier, _print_safe, format_alert  # noqa: E402
from unifi_monitor.poller import Poller  # noqa: E402
from unifi_monitor.query import _flatten_name  # noqa: E402
from unifi_monitor.unifi_client import UniFiUnavailable, _adapt_v2_alert, _unwrap  # noqa: E402
from unifi_monitor.util import load_env_file, now  # noqa: E402

BASE_TS = now() - 86400


def device(
    mac="aa:bb:cc:dd:ee:01",
    name="AP-3",
    state=1,
    dev_type="uap",
    last_seen=None,
    ports=None,
    cpu=5,
    mem=40,
    rx=1_000_000,
    tx=2_000_000,
):
    return {
        "mac": mac,
        "name": name,
        "model": "U6LR",
        "type": dev_type,
        "state": state,
        "adopted": True,
        "ip": "192.168.1.20",
        "version": "6.6.65",
        "uptime": 100000,
        "last_seen": last_seen,
        "num_sta": 7,
        "satisfaction": 96,
        "rx_bytes": rx,
        "tx_bytes": tx,
        "system-stats": {"cpu": str(cpu), "mem": str(mem)},
        "port_table": ports or [],
        "uplink": {"uplink_mac": "aa:bb:cc:dd:ee:99", "uplink_remote_port": 4},
    }


def gateway(wan1_up=True, ip="203.0.113.10", isp="Example ISP"):
    return {
        "mac": "aa:bb:cc:dd:ee:00",
        "name": "UDM",
        "model": "UDMPRO",
        "type": "udm",
        "state": 1,
        "adopted": True,
        "system-stats": {"cpu": "10", "mem": "50"},
        "port_table": [],
        "wan1": {
            "enable": True,
            "up": wan1_up,
            "ip": ip,
            "isp_name": isp,
            "gateway": "203.0.113.1",
            "latency": 15,
            "uptime": 50000,
            "name": "WAN1",
        },
    }


def health(status="ok", ip="203.0.113.10", isp="Example ISP", latency=15):
    return [
        {
            "subsystem": "wan",
            "status": status,
            "wan_ip": ip,
            "isp_name": isp,
            "latency": latency,
            "gw_mac": "aa:bb:cc:dd:ee:00",
            "uptime": 50000,
        },
        {"subsystem": "wlan", "status": "ok"},
    ]


def client(mac="11:22:33:44:55:66", name="Cow Cam", wired=True, signal=None, last_seen=None):
    return {
        "mac": mac,
        "name": name,
        "hostname": "cow-cam",
        "oui": "Ubiquiti",
        "ip": "192.168.1.55",
        "is_wired": wired,
        "sw_mac": "aa:bb:cc:dd:ee:99",
        "sw_port": 4,
        "signal": signal,
        "satisfaction": 90,
        "uptime": 5000,
        "rx_bytes": 500,
        "tx_bytes": 900,
        "last_seen": last_seen,
    }


def snapshot(devices=None, clients=None, known=None, subsystems=None, alarms=None, events=None):
    return {
        "devices": devices if devices is not None else [device()],
        "clients": clients if clients is not None else [],
        "known_clients": known if known is not None else [],
        "health": subsystems if subsystems is not None else health(),
        "alarms": alarms or [],
        "events": events or [],
        "errors": {},
    }


class Harness(unittest.TestCase):
    """One temp database, one analyzer, one issue store, explicit clock."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "test.db")
        self.db = Database(self.db_path)
        self.cfg = Config()
        self.cfg.db_path = self.db_path
        self.analyzer = Analyzer(self.db, self.cfg)
        self.issues = IssueStore(self.db)

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def cycle(self, snap, ts):
        result = self.analyzer.analyze(snap, ts=ts)
        changes = self.issues.sync(result.observations, active_types=result.active_types, ts=ts)
        return result, changes

    def open_types(self):
        return {i["issue_type"] for i in self.issues.open_issues()}


class TestDeviceOffline(Harness):
    def test_offline_crosses_thresholds_then_resolves(self):
        self.cycle(snapshot([device()]), BASE_TS)

        # Just offline: below the warning threshold, so nothing is flagged.
        result, _ = self.cycle(
            snapshot([device(state=0, last_seen=BASE_TS)]), BASE_TS + 60
        )
        self.assertEqual([], result.observations)

        # Past the warning threshold.
        result, changes = self.cycle(
            snapshot([device(state=0, last_seen=BASE_TS)]), BASE_TS + 400
        )
        self.assertEqual(1, len(result.observations))
        obs = result.observations[0]
        self.assertEqual("device_offline", obs.issue_type)
        self.assertEqual("warning", obs.severity)
        self.assertIn("AP-3", obs.summary)
        self.assertIn("offline", obs.summary)
        self.assertEqual("opened", changes[0].kind)

        # Past the critical threshold: same issue escalates, not a second row.
        result, changes = self.cycle(
            snapshot([device(state=0, last_seen=BASE_TS)]), BASE_TS + 1000
        )
        self.assertEqual("critical", result.observations[0].severity)
        self.assertEqual(["escalated"], [c.kind for c in changes])
        open_rows = self.issues.open_issues()
        self.assertEqual(1, len(open_rows))
        self.assertEqual(2, open_rows[0]["occurrences"])  # flagged at +400 and +1000

        # Back online: resolved automatically.
        _, changes = self.cycle(snapshot([device()]), BASE_TS + 1200)
        self.assertEqual(["resolved"], [c.kind for c in changes])
        self.assertEqual([], self.issues.open_issues())

    def test_summary_is_a_bare_fact(self):
        self.cycle(snapshot([device()]), BASE_TS)
        result, _ = self.cycle(snapshot([device(state=0, last_seen=BASE_TS)]), BASE_TS + 800)
        summary = result.observations[0].summary
        for word in ("because", "likely", "probably", "caused"):
            self.assertNotIn(word, summary.lower())

    def test_trigger_data_is_persisted_with_the_issue(self):
        self.cycle(snapshot([device()]), BASE_TS)
        self.cycle(snapshot([device(state=0, last_seen=BASE_TS)]), BASE_TS + 800)
        row = self.db.query_one("SELECT trigger_data, details FROM issues LIMIT 1")
        self.assertIn("U6LR", row["trigger_data"])
        self.assertIn("down_for_s", row["details"])

    def test_controller_last_seen_extends_downtime_across_a_poller_restart(self):
        # First ever poll already shows the device down for an hour.
        result, _ = self.cycle(
            snapshot([device(state=0, last_seen=BASE_TS - 3600)]), BASE_TS
        )
        self.assertEqual(1, len(result.observations))
        self.assertEqual("critical", result.observations[0].severity)


class TestFlapping(Harness):
    def test_repeated_drops_in_window_flag_flapping(self):
        ts = BASE_TS
        for _ in range(3):
            self.cycle(snapshot([device()]), ts)
            ts += 60
            self.cycle(snapshot([device(state=0, last_seen=ts)]), ts)
            ts += 60
        result, _ = self.cycle(snapshot([device()]), ts)
        flaps = [o for o in result.observations if o.issue_type == "device_flapping"]
        self.assertEqual(1, len(flaps))
        self.assertEqual(3, flaps[0].details["disconnects"])
        self.assertIn("3x", flaps[0].summary)

    def test_old_drops_fall_out_of_the_rolling_window(self):
        ts = BASE_TS
        for _ in range(4):
            self.cycle(snapshot([device()]), ts)
            ts += 60
            self.cycle(snapshot([device(state=0, last_seen=ts)]), ts)
            ts += 60
        # Well past flap_window_s (1h) with no further drops.
        result, _ = self.cycle(snapshot([device()]), ts + 7200)
        self.assertEqual([], [o for o in result.observations if o.issue_type == "device_flapping"])


class TestPorts(Harness):
    def _switch(self, errors, ts_ports_up=True, poe=6.5):
        port = {
            "port_idx": 4,
            "name": "Cow Cam",
            "up": ts_ports_up,
            "speed": 1000,
            "full_duplex": True,
            "poe_enable": True,
            "poe_power": poe,
            "rx_errors": errors,
            "tx_errors": 0,
            "rx_dropped": 0,
            "tx_dropped": 0,
        }
        return device(mac="aa:bb:cc:dd:ee:99", name="SW-1", dev_type="usw", ports=[port])

    def test_error_rate_is_computed_from_counter_deltas(self):
        self.cycle(snapshot([self._switch(0)]), BASE_TS)
        # 1200 errors over 60s = 1200/min, past the critical threshold.
        result, _ = self.cycle(snapshot([self._switch(1200)]), BASE_TS + 60)
        errs = [o for o in result.observations if o.issue_type == "port_errors"]
        self.assertEqual(1, len(errs))
        self.assertEqual("critical", errs[0].severity)
        self.assertAlmostEqual(1200.0, errs[0].details["errors_per_min"], places=1)

    def test_counter_reset_does_not_produce_a_negative_rate(self):
        self.cycle(snapshot([self._switch(5000)]), BASE_TS)
        result, _ = self.cycle(snapshot([self._switch(0)]), BASE_TS + 60)
        self.assertEqual([], [o for o in result.observations if o.issue_type == "port_errors"])

    def test_poe_port_losing_link_is_flagged(self):
        self.cycle(snapshot([self._switch(0)]), BASE_TS)
        result, _ = self.cycle(
            snapshot([self._switch(0, ts_ports_up=False, poe=0)]), BASE_TS + 60
        )
        poe = [o for o in result.observations if o.issue_type == "poe_port_down"]
        self.assertEqual(1, len(poe))
        self.assertIn("PoE", poe[0].summary)
        self.assertEqual(4, poe[0].details["port_idx"])

    def test_poe_issue_stays_open_while_the_port_is_dark(self):
        self.cycle(snapshot([self._switch(0)]), BASE_TS)
        _, changes = self.cycle(
            snapshot([self._switch(0, ts_ports_up=False, poe=0)]), BASE_TS + 60
        )
        self.assertEqual(["opened"], [c.kind for c in changes])

        # Still down two polls later: the same issue ages, it does not resolve
        # and re-open (the port now reads 0W, which must not erase the memory
        # of what it used to draw).
        _, changes = self.cycle(
            snapshot([self._switch(0, ts_ports_up=False, poe=0)]), BASE_TS + 660
        )
        self.assertEqual([], changes)
        open_rows = [i for i in self.issues.open_issues() if i["issue_type"] == "poe_port_down"]
        self.assertEqual(1, len(open_rows))
        self.assertEqual(2, open_rows[0]["occurrences"])

        # Link and power return: resolved.
        _, changes = self.cycle(snapshot([self._switch(0)]), BASE_TS + 960)
        self.assertEqual(["resolved"], [c.kind for c in changes])


class TestWan(Harness):
    def test_wan_down_is_critical(self):
        self.cycle(snapshot([gateway()], subsystems=health()), BASE_TS)
        result, changes = self.cycle(
            snapshot([gateway(wan1_up=False)], subsystems=health(status="error")),
            BASE_TS + 300,
        )
        downs = [o for o in result.observations if o.issue_type == "wan_down"]
        self.assertTrue(downs)
        self.assertTrue(all(o.severity == "critical" for o in downs))
        self.assertTrue(any(c.kind == "opened" for c in changes))

    def test_isp_change_is_flagged_as_failover(self):
        self.cycle(snapshot([gateway()], subsystems=health()), BASE_TS)
        result, _ = self.cycle(
            snapshot(
                [gateway(ip="198.51.100.7", isp="Backup LTE")],
                subsystems=health(ip="198.51.100.7", isp="Backup LTE"),
            ),
            BASE_TS + 300,
        )
        failovers = [o for o in result.observations if o.issue_type == "wan_failover"]
        self.assertTrue(failovers)
        self.assertEqual("warning", failovers[0].severity)
        self.assertIn("isp", failovers[0].details["changes"])

    def test_wan_and_www_subsystems_are_merged(self):
        # 'wan' knows the link and the address; 'www' knows reachability and
        # latency. Neither must blank out the other's fields.
        subsystems = [
            {"subsystem": "wan", "status": "ok", "wan_ip": "203.0.113.10", "isp_name": "Example ISP"},
            {"subsystem": "www", "status": "error", "latency": 250},
        ]
        self.cycle(snapshot([gateway()], subsystems=subsystems), BASE_TS)
        sample = self.db.last_wan_sample("wan")
        self.assertEqual("error", sample["status"])
        self.assertEqual("203.0.113.10", sample["ip"])
        self.assertEqual("Example ISP", sample["isp"])
        self.assertEqual(250.0, sample["latency_ms"])

    def test_high_latency_warns(self):
        self.cycle(snapshot([gateway()], subsystems=health()), BASE_TS)
        result, _ = self.cycle(
            snapshot([gateway()], subsystems=health(latency=250)), BASE_TS + 300
        )
        latency = [o for o in result.observations if o.issue_type == "wan_high_latency"]
        self.assertEqual("warning", latency[0].severity)


class TestClients(Harness):
    def test_only_watchlisted_clients_raise_offline_issues(self):
        watched = client()
        ignored = client(mac="99:88:77:66:55:44", name="Someones Phone", wired=False)

        # Nothing on the watchlist: no client issues at all.
        self.cycle(snapshot(clients=[watched, ignored], known=[watched, ignored]), BASE_TS)
        result, _ = self.cycle(
            snapshot(clients=[], known=[watched, ignored]), BASE_TS + 3600
        )
        self.assertEqual([], [o for o in result.observations if o.entity_type == "client"])

        # Watchlist the camera by name; now it counts.
        self.cfg.client_watchlist = ["cow cam"]
        result, _ = self.cycle(
            snapshot(clients=[], known=[watched, ignored]), BASE_TS + 7200
        )
        offline = [o for o in result.observations if o.issue_type == "client_offline"]
        self.assertEqual(1, len(offline))
        self.assertEqual("Cow Cam", offline[0].entity_name)
        self.assertEqual("critical", offline[0].severity)

    def test_watchlist_matches_on_mac_too(self):
        self.cfg.client_watchlist = ["11:22:33:44:55:66"]
        watched = client()
        self.cycle(snapshot(clients=[watched], known=[watched]), BASE_TS)
        # The poll that first sees it gone starts the clock; the next one, an
        # hour later, is what crosses the threshold.
        self.cycle(snapshot(clients=[], known=[watched]), BASE_TS + 300)
        result, _ = self.cycle(snapshot(clients=[], known=[watched]), BASE_TS + 3600)
        self.assertTrue([o for o in result.observations if o.issue_type == "client_offline"])

    def test_ignore_list_wins(self):
        self.cfg.client_watchlist = ["cow cam"]
        self.cfg.ignore_list = ["cow cam"]
        watched = client()
        self.cycle(snapshot(clients=[watched], known=[watched]), BASE_TS)
        result, _ = self.cycle(snapshot(clients=[], known=[watched]), BASE_TS + 7200)
        self.assertEqual([], result.observations)

    def test_weak_signal_flagged_for_watched_wireless_client(self):
        self.cfg.client_watchlist = ["cow cam"]
        wireless = client(wired=False, signal=-82)
        result, _ = self.cycle(snapshot(clients=[wireless], known=[wireless]), BASE_TS)
        weak = [o for o in result.observations if o.issue_type == "client_weak_signal"]
        self.assertEqual(1, len(weak))


class TestThresholdOverrides(Harness):
    def test_per_device_override_applies(self):
        self.cfg.overrides = {"AP-3": {"device_offline_warning_s": 60}}
        self.cycle(snapshot([device()]), BASE_TS)
        result, _ = self.cycle(snapshot([device(state=0, last_seen=BASE_TS)]), BASE_TS + 90)
        self.assertEqual(1, len(result.observations))


class TestAlarms(Harness):
    def test_alarm_becomes_an_issue_and_is_deduped(self):
        alarm = {
            "_id": "alarm-1",
            "key": "EVT_AP_Lost_Contact",
            "msg": "AP-3 was disconnected",
            "time": (BASE_TS) * 1000,
            "ap": "aa:bb:cc:dd:ee:01",
            "ap_name": "AP-3",
            "archived": False,
        }
        result, _ = self.cycle(snapshot(alarms=[alarm]), BASE_TS)
        alarms = [o for o in result.observations if o.issue_type == "controller_alarm"]
        self.assertEqual(1, len(alarms))
        self.assertEqual("critical", alarms[0].severity)

        self.cycle(snapshot(alarms=[alarm]), BASE_TS + 300)
        rows = self.db.query("SELECT * FROM issues WHERE issue_type='controller_alarm'")
        self.assertEqual(1, len(rows))
        self.assertEqual(2, rows[0]["occurrences"])
        stored = self.db.query("SELECT * FROM controller_events WHERE source='alarm'")
        self.assertEqual(1, len(stored))


class TestIssueLifecycle(Harness):
    def test_partial_snapshot_failure_does_not_resolve_unrelated_issues(self):
        self.cycle(snapshot([device()]), BASE_TS)
        self.cycle(snapshot([device(state=0, last_seen=BASE_TS)]), BASE_TS + 400)
        self.assertEqual({"device_offline"}, self.open_types())

        # A cycle where the analyzer never ran (controller unreachable).
        self.issues.sync([], active_types=None, ts=BASE_TS + 700)
        self.assertEqual({"device_offline"}, self.open_types())


class TestPollerSelfMonitoring(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "test.db")
        self.cfg = Config()
        self.cfg.db_path = self.db_path
        self.cfg.alerts.channels = []

    def tearDown(self):
        self.tmp.cleanup()

    def test_unreachable_controller_is_recorded_and_then_cleared(self):
        class FailingClient:
            mode = "fail"

            def snapshot(self):
                if self.mode == "fail":
                    raise UniFiUnavailable("connection refused")
                return snapshot([device()])

            def logout(self):
                pass

        fake = FailingClient()
        poller = Poller(self.cfg, client=fake)
        try:
            summary = poller.poll_once()
            self.assertFalse(summary["ok"])
            open_issues = poller.issues.open_issues()
            self.assertEqual(["controller_unreachable"], [i["issue_type"] for i in open_issues])
            self.assertEqual("critical", open_issues[0]["severity"])

            run = poller.db.query_one("SELECT * FROM poll_runs ORDER BY id DESC LIMIT 1")
            self.assertEqual(0, run["ok"])
            self.assertIn("connection refused", run["error"])

            fake.mode = "ok"
            summary = poller.poll_once()
            self.assertTrue(summary["ok"])
            self.assertEqual([], poller.issues.open_issues())
        finally:
            poller.db.close()

    def test_credentials_never_reach_the_database(self):
        self.cfg.controller.password = "hunter2-secret"

        class LeakyClient:
            def snapshot(self):
                raise UniFiUnavailable("login failed for password hunter2-secret")

            def logout(self):
                pass

        poller = Poller(self.cfg, client=LeakyClient())
        try:
            poller.poll_once()
            row = poller.db.query_one("SELECT error FROM poll_runs ORDER BY id DESC LIMIT 1")
            self.assertNotIn("hunter2-secret", row["error"])
            issue = poller.db.query_one("SELECT summary, details FROM issues LIMIT 1")
            self.assertNotIn("hunter2-secret", issue["summary"])
            self.assertNotIn("hunter2-secret", issue["details"])
        finally:
            poller.db.close()


class TestNotifications(unittest.TestCase):
    def setUp(self):
        self.cfg = Config()
        self.cfg.alerts.channels = ["stdout"]
        self.notifier = Notifier(self.cfg)

    def _change(self, kind="opened", severity="warning"):
        from unifi_monitor.issues import IssueChange

        return IssueChange(
            kind=kind,
            issue_id=1,
            issue_type="device_offline",
            severity=severity,
            entity_type="device",
            entity_id="aa:bb:cc:dd:ee:01",
            entity_name="AP-3",
            summary="AP-3 (access point) offline 12m",
            first_seen=now() - 720,
            last_seen=now(),
            details={},
        )

    def test_info_is_below_the_alert_floor(self):
        self.assertFalse(self.notifier.should_send(self._change(severity="info"), None))

    def test_cooldown_suppresses_a_repeat(self):
        recent = {"notified_at": now() - 60}
        self.assertFalse(self.notifier.should_send(self._change(), recent))

    def test_escalation_breaks_through_the_cooldown(self):
        recent = {"notified_at": now() - 60}
        self.assertTrue(
            self.notifier.should_send(self._change(kind="escalated", severity="critical"), recent)
        )

    def test_resolution_only_announced_if_the_problem_was(self):
        self.assertFalse(self.notifier.should_send(self._change(kind="resolved"), {}))
        self.assertTrue(
            self.notifier.should_send(self._change(kind="resolved"), {"notified_at": now() - 600})
        )

    def test_alert_text_is_one_bare_line(self):
        text = format_alert(self._change())
        self.assertIn("AP-3 (access point) offline 12m", text)
        self.assertEqual(1, len(text.splitlines()))


class TestQueryLayer(Harness):
    def _make_history(self):
        """Three offline episodes for AP-3, plus a live one."""
        ts = BASE_TS
        for _ in range(3):
            self.cycle(snapshot([device()]), ts)
            ts += 300
            self.cycle(snapshot([device(state=0, last_seen=ts)]), ts)
            self.cycle(snapshot([device(state=0, last_seen=ts)]), ts + 400)
            ts += 3700
        self.cycle(snapshot([device()]), ts)
        self.cycle(snapshot([device(state=0, last_seen=ts + 300)]), ts + 300)
        self.cycle(snapshot([device(state=0, last_seen=ts + 300)]), ts + 1000)

    def test_recent_issues_and_overview(self):
        self._make_history()
        issues = query.recent_issues(db_path=self.db_path)
        self.assertTrue(issues)
        self.assertEqual("device_offline", issues[0]["issue_type"])
        self.assertIsInstance(issues[0]["details"], dict)

        overview = query.network_overview(db_path=self.db_path)
        self.assertEqual(1, overview["devices"]["total"])
        self.assertEqual(1, len(overview["devices"]["offline"]))
        self.assertGreaterEqual(overview["open_issues"]["total"], 1)

    def test_explain_issue_assembles_evidence_without_diagnosing(self):
        self._make_history()
        issue_id = query.recent_issues(db_path=self.db_path)[0]["id"]
        report = query.explain_issue(issue_id, db_path=self.db_path)

        self.assertIn("issue", report)
        self.assertTrue(report["what_changed"]["known"])
        self.assertEqual("offline", report["what_changed"]["to_state"])
        self.assertGreaterEqual(report["recurrence"]["total_occurrences"], 4)
        self.assertTrue(report["recurrence"]["is_recurring"])
        self.assertIsNotNone(report["recurrence"]["median_interval_hours"])
        self.assertIn("timeline", report)
        self.assertIn("poller_health", report)
        self.assertTrue(report["facts"])
        self.assertTrue(report["proposed_actions"])

        text = query.summarize_for_llm(report)
        self.assertIn("PROPOSED ACTIONS", text)
        self.assertIn("require explicit user confirmation", text)

    def test_find_entity_resolves_a_human_phrase(self):
        self.cfg.client_watchlist = ["cow cam"]
        cam = client()
        self.cycle(snapshot(clients=[cam], known=[cam]), BASE_TS)
        matches = query.find_entity("cow cam", db_path=self.db_path)
        self.assertTrue(matches)
        self.assertEqual("Cow Cam", matches[0]["name"])
        self.assertEqual("client", matches[0]["entity_type"])

    def test_explain_entity_end_to_end(self):
        self.cfg.client_watchlist = ["cow cam"]
        cam = client()
        ts = BASE_TS
        for _ in range(2):
            self.cycle(snapshot(clients=[cam], known=[cam]), ts)
            ts += 300
            self.cycle(snapshot(clients=[], known=[cam]), ts)
            self.cycle(snapshot(clients=[], known=[cam]), ts + 2000)
            ts += 4000
        report = query.explain_entity("cow cam", db_path=self.db_path)
        self.assertEqual("Cow Cam", report["resolved_to"]["name"])
        self.assertTrue(report["facts"])
        self.assertGreaterEqual(report["history"]["availability"]["disconnects"], 2)
        self.assertIsNotNone(report["latest_issue_explained"])

    def test_device_history_and_patterns(self):
        self._make_history()
        history = query.device_history("AP-3", hours=48, db_path=self.db_path)
        self.assertEqual("AP-3", history["entity"]["name"])
        self.assertTrue(history["state_transitions"])
        self.assertLess(history["availability"]["uptime_pct"], 100.0)

        patterns = query.issue_frequency(entity="AP-3", days=7, db_path=self.db_path)
        self.assertGreaterEqual(patterns["total"], 3)
        self.assertIn("device_offline", patterns["by_issue_type"])

    def test_query_layer_cannot_write(self):
        self._make_history()
        with query.open_db(self.db_path) as db:
            with self.assertRaises(Exception):
                db.execute("DELETE FROM issues")

    def test_missing_database_is_a_clear_error(self):
        with self.assertRaises(FileNotFoundError):
            query.network_overview(db_path=os.path.join(self.tmp.name, "nope.db"))


class TestPollerStaleness(Harness):
    def _runs(self, count, interval, end_offset):
        start = now() - end_offset - (count - 1) * interval
        for i in range(count):
            run_id = self.db.start_run(start + i * interval)
            self.db.finish_run(run_id, ok=True)

    def test_stall_is_judged_against_the_observed_cadence(self):
        # Part 2 never reads the poller's config, so it infers the interval.
        # 30 min since the last poll would be a stall at a 5-min cadence, but
        # this poller runs every 15 min, so it is still within tolerance.
        self._runs(count=6, interval=900, end_offset=1800)
        poller = query.network_overview(db_path=self.db_path)["poller"]
        self.assertEqual(900, poller["expected_interval_s"])
        self.assertFalse(poller["possibly_stalled"])

    def test_a_long_silence_is_flagged(self):
        self._runs(count=6, interval=300, end_offset=7200)
        poller = query.network_overview(db_path=self.db_path)["poller"]
        self.assertEqual(300, poller["expected_interval_s"])
        self.assertTrue(poller["possibly_stalled"])


class TestRemediation(Harness):
    def setUp(self):
        super().setUp()
        self.actions_db = os.path.join(self.tmp.name, "actions.db")

    def _an_issue(self):
        self.cycle(snapshot([device()]), BASE_TS)
        self.cycle(snapshot([device(state=0, last_seen=BASE_TS)]), BASE_TS + 1000)
        return query.recent_issues(db_path=self.db_path)[0]

    def test_proposals_are_never_auto_executed(self):
        issue = self._an_issue()
        report = query.explain_issue(int(issue["id"]), db_path=self.db_path)
        actions = remediation.propose_actions(report["issue"], report)
        self.assertTrue(actions)
        for action in actions:
            self.assertTrue(action["requires_confirmation"])
            self.assertFalse(action["auto_execute"])

        keys = {a["action_key"] for a in actions}
        self.assertIn("restart_device", keys)
        self.assertIn("poe_cycle_uplink", keys)  # uplink port from the device record

    def test_execution_is_blocked_for_a_view_only_account(self):
        issue = self._an_issue()
        report = query.explain_issue(int(issue["id"]), db_path=self.db_path)
        stored = remediation.record_proposals(
            report["issue"],
            remediation.propose_actions(report["issue"], report),
            db_path=self.actions_db,
        )
        executable = next(a for a in stored if a.get("controller_call"))

        result = remediation.confirm_and_execute(
            executable["id"], executable["confirmation_token"], db_path=self.actions_db
        )
        self.assertFalse(result["executed"])
        self.assertEqual("write_actions_disabled", result["reason"])
        self.assertIn("view-only", result["message"])
        self.assertIn("required_permission", result)
        self.assertEqual("power-cycle", result["would_call"]["body"]["cmd"])

    def test_a_wrong_token_is_rejected(self):
        issue = self._an_issue()
        report = query.explain_issue(int(issue["id"]), db_path=self.db_path)
        stored = remediation.record_proposals(
            report["issue"],
            remediation.propose_actions(report["issue"], report),
            db_path=self.actions_db,
        )
        result = remediation.confirm_and_execute(
            stored[0]["id"], "not-the-token", db_path=self.actions_db
        )
        self.assertEqual("bad_confirmation_token", result["reason"])

    def test_rejecting_a_proposal_is_recorded(self):
        issue = self._an_issue()
        report = query.explain_issue(int(issue["id"]), db_path=self.db_path)
        stored = remediation.record_proposals(
            report["issue"],
            remediation.propose_actions(report["issue"], report),
            db_path=self.actions_db,
        )
        remediation.reject_proposal(stored[0]["id"], db_path=self.actions_db)
        rows = remediation.list_proposals(db_path=self.actions_db, status="rejected")
        self.assertEqual(1, len(rows))
        self.assertNotIn("token", rows[0])

    def test_proposals_live_outside_the_monitoring_database(self):
        issue = self._an_issue()
        report = query.explain_issue(int(issue["id"]), db_path=self.db_path)
        remediation.record_proposals(
            report["issue"],
            remediation.propose_actions(report["issue"], report),
            db_path=self.actions_db,
        )
        tables = {
            row["name"]
            for row in self.db.query("SELECT name FROM sqlite_master WHERE type='table'")
        }
        self.assertNotIn("action_proposals", tables)


class TestMcpServer(Harness):
    def test_initialize_and_tools_list(self):
        from unifi_monitor import mcp_server

        response = mcp_server.handle_message({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        self.assertEqual("unifi-monitor", response["result"]["serverInfo"]["name"])

        response = mcp_server.handle_message({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        names = {tool["name"] for tool in response["result"]["tools"]}
        self.assertIn("unifi_explain_issue", names)
        self.assertIn("unifi_investigate_device", names)
        self.assertIn("unifi_confirm_action", names)
        for tool in response["result"]["tools"]:
            self.assertIn("inputSchema", tool)
            self.assertNotIn("handler", tool)

    def test_tools_call_reads_the_database(self):
        from unifi_monitor import mcp_server

        self.cycle(snapshot([device()]), BASE_TS)
        response = mcp_server.handle_message(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "unifi_network_status",
                    "arguments": {"db_path": self.db_path},
                },
            }
        )
        payload = response["result"]["content"][0]["text"]
        self.assertIn("devices", payload)
        self.assertNotIn("isError", response["result"])

    def test_notifications_get_no_response(self):
        from unifi_monitor import mcp_server

        self.assertIsNone(
            mcp_server.handle_message({"jsonrpc": "2.0", "method": "notifications/initialized"})
        )

    def test_tool_errors_are_reported_not_raised(self):
        from unifi_monitor import mcp_server

        response = mcp_server.handle_message(
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {"name": "unifi_explain_issue", "arguments": {"db_path": self.db_path}},
            }
        )
        self.assertTrue(response["result"]["isError"])


class TestUnifiClientParsing(unittest.TestCase):
    def test_unwrap_classic_envelope(self):
        payload = {"meta": {"rc": "ok"}, "data": [{"mac": "a"}]}
        self.assertEqual([{"mac": "a"}], _unwrap(payload, "/x"))

    def test_unwrap_raises_on_controller_error(self):
        with self.assertRaises(Exception):
            _unwrap({"meta": {"rc": "error", "msg": "api.err.NoSiteContext"}, "data": []}, "/x")

    def test_unwrap_bare_list_and_empty(self):
        self.assertEqual([{"a": 1}], _unwrap([{"a": 1}], "/x"))
        self.assertEqual([], _unwrap(None, "/x"))


class TestEnvFileParsing(unittest.TestCase):
    """The shipped .env.example documents settings with trailing comments, so
    the loader has to agree with the file it ships."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "env")
        self._saved = dict(os.environ)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._saved)
        self.tmp.cleanup()

    def _load(self, text):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write(text)
        for key in ("UNIFI_CONTROLLER_TYPE", "UNIFI_PASSWORD", "UNIFI_HOST", "UNIFI_SITE"):
            os.environ.pop(key, None)
        load_env_file(self.path)

    def test_inline_comment_is_stripped(self):
        self._load("UNIFI_CONTROLLER_TYPE=proxy      # UniFi OS console\n")
        self.assertEqual("proxy", os.environ["UNIFI_CONTROLLER_TYPE"])

    def test_hash_inside_a_password_survives(self):
        # No whitespace before the '#', so it is part of the value, not a comment.
        self._load("UNIFI_PASSWORD=pa#ssw0rd\n")
        self.assertEqual("pa#ssw0rd", os.environ["UNIFI_PASSWORD"])

    def test_quoted_value_keeps_its_spaces_and_hash(self):
        self._load('UNIFI_PASSWORD="a b # c"   # trailing note\n')
        self.assertEqual("a b # c", os.environ["UNIFI_PASSWORD"])

    def test_full_line_comment_and_blank_lines_ignored(self):
        self._load("# a comment\n\nUNIFI_HOST=192.168.1.1\n")
        self.assertEqual("192.168.1.1", os.environ["UNIFI_HOST"])
        self.assertNotIn("# a comment", os.environ)

    def test_shipped_example_file_parses_into_a_valid_controller_type(self):
        example = os.path.join(ROOT, "unifi_monitor", ".env.example")
        with open(example, encoding="utf-8") as fh:
            text = fh.read()
        self._load(text)
        self.assertEqual("proxy", os.environ["UNIFI_CONTROLLER_TYPE"])


class TestV2AlertFeed(unittest.TestCase):
    """Network 9+ dropped stat/event and stat/alarm for a merged v2 feed."""

    def _alert(self, event="CLIENT_CONNECTED_WIRELESS", severity="LOW", category="CLIENT_DEVICES"):
        return {
            "id": "abc123",
            "event": event,
            "key": event + "_2",
            "category": category,
            "severity": severity,
            "status": "NEW",
            "timestamp": BASE_TS * 1000,
            "message": "something happened",
            "parameters": {
                "DEVICE": {"id": "f4:e2:c6:f2:33:31", "name": "Dream Machine"},
                "CLIENT": {"id": "11:22:33:44:55:66", "name": "cow-cam"},
            },
        }

    def test_adapter_maps_classic_field_names(self):
        out = _adapt_v2_alert(self._alert())
        self.assertEqual("abc123", out["_id"])
        self.assertEqual("CLIENT_CONNECTED_WIRELESS", out["key"])
        self.assertEqual("something happened", out["msg"])
        self.assertEqual(BASE_TS * 1000, out["time"])
        self.assertEqual("f4:e2:c6:f2:33:31", out["device_mac"])
        self.assertEqual("Dream Machine", out["device_name"])
        self.assertEqual("11:22:33:44:55:66", out["user"])
        self.assertFalse(out["archived"])

    def test_severity_vocabulary_is_translated(self):
        self.assertEqual("critical", _adapt_v2_alert(self._alert(severity="VERY_HIGH"))["severity"])
        self.assertEqual("warning", _adapt_v2_alert(self._alert(severity="HIGH"))["severity"])
        self.assertEqual("info", _adapt_v2_alert(self._alert(severity="LOW"))["severity"])

    def test_wan_restored_does_not_become_an_alarm(self):
        # MEDIUM maps to info on purpose: the WAN coming *back* is context.
        row = self._alert(
            event="NETWORK_WAN_RESTORED", severity="MEDIUM", category="INTERNET_AND_WAN"
        )
        out = _adapt_v2_alert(row)
        self.assertEqual("info", out["severity"])
        self.assertEqual("wan", out["subsystem"])

    def test_archived_reflects_non_new_status(self):
        row = self._alert()
        row["status"] = "ARCHIVED"
        self.assertTrue(_adapt_v2_alert(row)["archived"])

    def test_detectors_read_the_neutral_device_slot(self):
        """A UDM arrives in one DEVICE slot, not ap/sw/gw."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db = Database(os.path.join(tmp.name, "t.db"))
        self.addCleanup(db.close)
        cfg = Config()
        analyzer = Analyzer(db, cfg)
        alarm = _adapt_v2_alert(
            self._alert(event="DEVICE_LOST_CONTACT", severity="VERY_HIGH", category="DEVICES")
        )
        snap = snapshot(alarms=[alarm])
        result = analyzer.analyze(snap, ts=BASE_TS)
        alarms = [o for o in result.observations if o.issue_type == "controller_alarm"]
        self.assertEqual(1, len(alarms))
        self.assertEqual("f4:e2:c6:f2:33:31", alarms[0].entity_id)
        self.assertEqual("Dream Machine", alarms[0].entity_name)
        self.assertEqual("critical", alarms[0].severity)


class TestMirroredPortErrorCounters(Harness):
    """UDM gateway LAN ports copy rx_dropped into rx_errors."""

    def _gw_port(self, errors, dropped):
        port = {
            "port_idx": 5,
            "name": "Port 5",
            "up": True,
            "speed": 1000,
            "full_duplex": True,
            "rx_errors": errors,
            "tx_errors": 0,
            "rx_dropped": dropped,
            "tx_dropped": 0,
        }
        return device(mac="aa:bb:cc:dd:ee:77", name="UDM", dev_type="ugw", ports=[port])

    def test_mirrored_counter_is_not_reported_as_errors(self):
        self.cycle(snapshot([self._gw_port(0, 0)]), BASE_TS)
        # Identical counters: normal filtered frames, not 1200 physical errors.
        result, _ = self.cycle(snapshot([self._gw_port(1200, 1200)]), BASE_TS + 60)
        errs = [o for o in result.observations if o.issue_type == "port_errors"]
        self.assertEqual(1, len(errs))
        self.assertIn("dropping", errs[0].summary)
        self.assertEqual("warning", errs[0].severity)
        self.assertAlmostEqual(0.0, errs[0].details.get("errors_per_min", 0.0), places=1)

    def test_genuinely_diverging_counters_still_flag_errors(self):
        self.cycle(snapshot([self._gw_port(0, 0)]), BASE_TS)
        result, _ = self.cycle(snapshot([self._gw_port(1200, 50)]), BASE_TS + 60)
        errs = [o for o in result.observations if o.issue_type == "port_errors"]
        self.assertEqual(1, len(errs))
        self.assertEqual("critical", errs[0].severity)
        self.assertAlmostEqual(1200.0, errs[0].details["errors_per_min"], places=1)


class TestAlertEncoding(unittest.TestCase):
    def test_stdout_alert_survives_a_console_that_cannot_encode_emoji(self):
        """A cp1252 console must not turn a decoration into a lost alert."""

        class Cp1252Stream(io.StringIO):
            encoding = "cp1252"

            def write(self, text):
                text.encode("cp1252")  # raises UnicodeEncodeError on the emoji
                return super().write(text)

        stream = Cp1252Stream()
        with contextlib.redirect_stdout(stream):
            _print_safe("\U0001f6a8 CRITICAL: AP-3 offline 12m")
        self.assertIn("CRITICAL: AP-3 offline 12m", stream.getvalue())


class TestPart2PathResolution(unittest.TestCase):
    """Part 2 must honour UNIFI_DB_PATH from the env file, which is loaded
    after import — so the path cannot be frozen in a module constant."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._saved = dict(os.environ)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._saved)
        self.tmp.cleanup()

    def test_db_path_follows_a_later_env_change(self):
        os.environ["UNIFI_DB_PATH"] = os.path.join(self.tmp.name, "one.db")
        self.assertTrue(query.default_db_path().endswith("one.db"))
        os.environ["UNIFI_DB_PATH"] = os.path.join(self.tmp.name, "two.db")
        self.assertTrue(query.default_db_path().endswith("two.db"))

    def test_actions_db_path_follows_a_later_env_change(self):
        os.environ["UNIFI_ACTIONS_DB"] = os.path.join(self.tmp.name, "a.db")
        self.assertTrue(remediation.actions_db_path().endswith("a.db"))
        os.environ["UNIFI_ACTIONS_DB"] = os.path.join(self.tmp.name, "b.db")
        self.assertTrue(remediation.actions_db_path().endswith("b.db"))


class TestEntityPhraseResolution(Harness):
    """People say "cow cam"; the controller stores "cow-cam"."""

    def _seed(self):
        for mac, name in (
            ("11:22:33:44:55:66", "cow-cam"),
            ("11:22:33:44:55:77", "chicken-cam"),
            ("11:22:33:44:55:88", "CowFeederCam"),
            ("11:22:33:44:55:99", "guest-laptop"),
        ):
            self.db.upsert_entity("client", mac, name=name, kind="wireless client", ts=BASE_TS)

    def test_spaces_match_a_hyphenated_name(self):
        self._seed()
        hits = query.find_entity("cow cam", db_path=self.db_path)
        self.assertTrue(hits)
        self.assertEqual("cow-cam", hits[0]["name"])

    def test_camelcase_name_is_reachable_by_words(self):
        self._seed()
        hits = query.find_entity("cow feeder cam", db_path=self.db_path)
        self.assertTrue(hits)
        self.assertEqual("CowFeederCam", hits[0]["name"])

    def test_unrelated_entities_are_not_returned(self):
        self._seed()
        names = {h["name"] for h in query.find_entity("cow cam", db_path=self.db_path)}
        self.assertNotIn("guest-laptop", names)
        self.assertNotIn("chicken-cam", names)

    def test_exact_name_still_wins(self):
        self._seed()
        hits = query.find_entity("chicken-cam", db_path=self.db_path)
        self.assertEqual("chicken-cam", hits[0]["name"])

    def test_flatten_handles_separator_styles(self):
        for raw in ("cow-cam", "cow_cam", "cow.cam", "Cow Cam", "CowCam"):
            self.assertEqual("cow cam", _flatten_name(raw))


class TestCriticalWatermark(Harness):
    """The proactive hook must report a critical once, not once per run."""

    def setUp(self):
        super().setUp()
        self.mark = os.path.join(self.tmp.name, "last_critical")

    def _args(self, **over):
        ns = argparse.Namespace(
            db=self.db_path, since_file=self.mark, minutes=60, json=False, command="critical"
        )
        for k, v in over.items():
            setattr(ns, k, v)
        return ns

    def _open_critical(self, ts, mac="aa:bb:cc:dd:ee:01", name="AP-3"):
        obs = [
            Observation(
                issue_type="device_offline",
                severity="critical",
                entity_type="device",
                entity_id=mac,
                entity_name=name,
                summary=f"{name} offline",
                details={},
                trigger_data={},
            )
        ]
        self.issues.sync(obs, active_types={"device_offline"}, ts=ts)

    def _run(self, **over):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cmd_critical(self._args(**over))
        return rc, buf.getvalue()

    def test_reports_once_then_stays_quiet(self):
        self._open_critical(now() - 300)
        rc, first = self._run()
        self.assertEqual(0, rc)
        self.assertIn("AP-3 offline", first)
        # Same issue, second run: the watermark has moved past it.
        _rc, second = self._run()
        self.assertEqual("", second.strip())

    def test_a_later_critical_still_gets_through(self):
        self._open_critical(now() - 600)
        self._run()
        self._open_critical(now() - 60, mac="aa:bb:cc:dd:ee:09", name="AP-9")
        _rc, out = self._run()
        self.assertIn("AP-9 offline", out)

    def _mark(self):
        with open(self.mark, encoding="utf-8") as fh:
            return fh.read()

    def test_watermark_is_not_advanced_when_nothing_is_reported(self):
        self._open_critical(now() - 300)
        self._run()
        mark_after_report = self._mark()
        self._run()  # quiet run
        self.assertEqual(mark_after_report, self._mark())

    def test_corrupt_watermark_falls_back_instead_of_crashing(self):
        """A wedged alert path is worse than a duplicate alert."""
        self._open_critical(now() - 300)
        with open(self.mark, "w", encoding="utf-8") as fh:
            fh.write("not-a-number")
        rc, out = self._run()
        self.assertEqual(0, rc)
        self.assertIn("AP-3 offline", out)

    def test_missing_watermark_uses_the_minutes_window(self):
        self._open_critical(now() - 120)
        _rc, out = self._run(minutes=10)
        self.assertIn("AP-3 offline", out)

    def test_warnings_are_never_reported_as_critical(self):
        obs = [
            Observation(
                issue_type="device_high_memory",
                severity="warning",
                entity_type="device",
                entity_id="aa:bb:cc:dd:ee:02",
                entity_name="AP-4",
                summary="AP-4 memory at 92%",
                details={},
                trigger_data={},
            )
        ]
        self.issues.sync(obs, active_types={"device_high_memory"}, ts=now() - 300)
        _rc, out = self._run()
        self.assertEqual("", out.strip())


if __name__ == "__main__":
    unittest.main()
