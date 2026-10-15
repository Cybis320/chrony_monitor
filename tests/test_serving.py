"""Tests for the NTP-serving status line."""

import os
import tempfile
import unittest
from unittest import mock

from chrony_monitor import serving
from chrony_monitor.display import format_serving_line
from chrony_monitor.serving import (ServingStatus, ServingTracker, count_active_clients,
                                    ntp_allowed, parse_last_seconds, parse_ntp_received)
from chrony_monitor.status import ChronyStatus, SyncQuality, SyncState, TrackingInfo

CLIENTS = """\
Hostname                      NTP   Drop Int IntL Last     Cmd   Drop Int  Last
===============================================================================
127.0.0.1                       0      0   -   -     -      15      0  -1     7
192.168.50.20                 120      0   4   -     9       0      0   -     -
192.168.50.21                 300      0   6   -   14m       0      0   -     -
192.168.50.22                  80      0   6   -    16m      0      0   -     -
10.0.0.5                        3      0   -   -    2h       0      0   -     -
"""

SERVERSTATS = """\
NTP packets received       : 1598
NTP packets dropped        : 0
Command packets received   : 19
Command packets dropped    : 0
Client log records dropped : 0
"""


class TestParsing(unittest.TestCase):
    def test_last_column(self):
        self.assertEqual(parse_last_seconds("45"), 45)
        self.assertEqual(parse_last_seconds("14m"), 840)
        self.assertEqual(parse_last_seconds("3h"), 10800)
        self.assertIsNone(parse_last_seconds("-"))

    def test_active_clients_skip_command_only_and_stale(self):
        # 127.0.0.1 only ran chronyc; .22 and 10.0.0.5 are older than 15 min.
        self.assertEqual(count_active_clients(CLIENTS), 2)

    def test_header_only(self):
        self.assertEqual(count_active_clients("\n".join(CLIENTS.splitlines()[:2])), 0)

    def test_serverstats(self):
        self.assertEqual(parse_ntp_received(SERVERSTATS), 1598)
        self.assertIsNone(parse_ntp_received("501 Not authorised"))


class TestNtpAllowed(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _write(self, name, text):
        path = os.path.join(self.tmp.name, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)
        return path

    def test_allow_in_confdir(self):
        d = os.path.join(self.tmp.name, "conf.d")
        self._write("conf.d/ntp-server.conf", "allow 192.168.0.0/16\n")
        conf = self._write("chrony.conf", f"pool pool.ntp.org\nconfdir {d}\n")
        self.assertTrue(ntp_allowed([conf]))

    def test_client_only(self):
        conf = self._write("chrony.conf", "pool pool.ntp.org\n# allow 10.0.0.0/8\n")
        self.assertFalse(ntp_allowed([conf]))

    def test_port_zero_closes_server(self):
        conf = self._write("chrony.conf", "allow all\nport 0\n")
        self.assertFalse(ntp_allowed([conf]))

    def test_unreadable(self):
        self.assertIsNone(ntp_allowed([os.path.join(self.tmp.name, "missing")]))


class TestTracker(unittest.TestCase):
    def _tracker(self, outputs):
        self.now = 1000.0
        runs = iter(outputs)
        t = ServingTracker(run=lambda cmd: next(runs), clock=lambda: self.now)
        self.enterContext(mock.patch.object(serving, "ntp_allowed", return_value=True))
        return t

    def test_rate_from_counter(self):
        stats2 = SERVERSTATS.replace("1598", "1618")
        t = self._tracker([CLIENTS, SERVERSTATS, CLIENTS, stats2])
        first = t.get()
        self.assertEqual(first.clients, 2)
        self.assertIsNone(first.requests_per_hour)   # needs two samples
        self.now += 60
        self.assertAlmostEqual(t.get().requests_per_hour, 1200)

    def test_polls_at_most_once_per_interval(self):
        t = self._tracker([CLIENTS, SERVERSTATS])   # a second poll would exhaust this
        t.get()
        self.now += 30
        self.assertEqual(t.get().clients, 2)

    def test_chronyd_restart_resets_rate(self):
        stats2 = SERVERSTATS.replace("1598", "5")
        t = self._tracker([CLIENTS, SERVERSTATS, CLIENTS, stats2])
        t.get()
        self.now += 60
        self.assertIsNone(t.get().requests_per_hour)

    def test_no_sudo_rule(self):
        t = self._tracker([None, None])
        s = t.get()
        self.assertTrue(s.allowed)
        self.assertIsNone(s.clients)
        self.assertIsNone(s.requests_per_hour)


class TestServingLine(unittest.TestCase):
    def _status(self, selected=True, stratum=1):
        return ChronyStatus(sources=[], selected_source=object() if selected else None,
                            sync_state=SyncState.GPPS_LOCKED, sync_quality=SyncQuality.EXCELLENT,
                            offset_ms=0.0, error_message=None, usb_gps_detected=True,
                            pps_expected=True, tracking=TrackingInfo(stratum=stratum))

    def test_locked_server(self):
        line = format_serving_line(self._status(), ServingStatus(True, 4, 1234.0))
        self.assertEqual(line, "Stratum 1 | 4 clients (15m) | 1.2k req/h")

    def test_one_client_without_rule(self):
        self.assertEqual(format_serving_line(self._status(), ServingStatus(True, 1)),
                         "Stratum 1 | 1 client (15m)")
        self.assertIn("clients n/a", format_serving_line(self._status(), ServingStatus(True)))

    def test_unsynchronised_warns(self):
        line = format_serving_line(self._status(selected=False), ServingStatus(True, 0))
        self.assertIn("clients will reject", line)

    def test_not_serving(self):
        self.assertIn("Not serving", format_serving_line(self._status(), ServingStatus(False)))


if __name__ == "__main__":
    unittest.main()
