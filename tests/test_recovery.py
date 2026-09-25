"""Tests for the recovery gates that skip restarts which can't help."""

import os
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest import mock

from chrony_monitor import recovery
from chrony_monitor.recovery import GpsFix, RecoveryManager, pps_never_pulsed


class PpsSysfs:
    """A fake /sys/class/pps with one device and a /dev symlink to it."""

    def __init__(self, tmp, assert_val, clear_val, name="pps0"):
        self.sysfs = os.path.join(tmp, "sys")
        os.makedirs(os.path.join(self.sysfs, name))
        for edge, val in (("assert", assert_val), ("clear", clear_val)):
            with open(os.path.join(self.sysfs, name, edge), "w") as f:
                f.write(val + "\n")
        dev = os.path.join(tmp, "dev")
        os.makedirs(dev)
        self.target = os.path.join(dev, name)
        open(self.target, "w").close()
        self.link = os.path.join(dev, "serial-pps")
        os.symlink(self.target, self.link)


class TestPpsNeverPulsed(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_fresh_device_with_no_edges(self):
        s = PpsSysfs(self.tmp.name, "0.000000000#0", "0.000000000#0")
        self.assertTrue(pps_never_pulsed(s.link, sysfs=s.sysfs))

    def test_assert_edges_seen(self):
        s = PpsSysfs(self.tmp.name, "1790000000.000001234#42", "0.000000000#0")
        self.assertFalse(pps_never_pulsed(s.link, sysfs=s.sysfs))

    def test_clear_edges_seen(self):
        # Serial PPS through a MAX232 is read on the clear edge.
        s = PpsSysfs(self.tmp.name, "0.000000000#0", "1790000000.999998000#17")
        self.assertFalse(pps_never_pulsed(s.link, sysfs=s.sysfs))

    def test_missing_device_is_unknown(self):
        self.assertFalse(pps_never_pulsed("/dev/does-not-exist",
                                          sysfs=os.path.join(self.tmp.name, "sys")))

    def test_garbled_counter_is_unknown(self):
        s = PpsSysfs(self.tmp.name, "garbage", "0.000000000#0")
        self.assertFalse(pps_never_pulsed(s.link, sysfs=s.sysfs))


class TestRecoveryGate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _manager(self, fix, sysfs):
        m = RecoveryManager()
        m.lock_lost_time = datetime.now() - timedelta(seconds=600)
        m._get_pps_device = lambda: sysfs.link
        self.enterContext(mock.patch.object(recovery, "query_gps_fix", return_value=fix))
        self.enterContext(mock.patch.object(
            recovery, "pps_never_pulsed",
            side_effect=lambda dev: pps_never_pulsed(dev, sysfs=sysfs.sysfs)))
        return m

    def test_gps_without_pps_wire_skips_restart(self):
        # USB-only receiver with a fix; nothing on the serial DCD pin.
        s = PpsSysfs(self.tmp.name, "0.000000000#0", "0.000000000#0")
        m = self._manager(GpsFix(reachable=True, mode=3, satellites_used=6,
                                 satellites_visible=12), s)
        self.assertFalse(m.should_attempt_recovery())
        self.assertIn("No PPS pulses", m.reception_fault)

    def test_pulses_seen_but_lock_lost_restarts(self):
        s = PpsSysfs(self.tmp.name, "0.000000000#0", "1790000000.999998000#300")
        m = self._manager(GpsFix(reachable=True, mode=3, satellites_used=6,
                                 satellites_visible=12), s)
        self.assertTrue(m.should_attempt_recovery())
        self.assertIsNone(m.reception_fault)

    def test_no_fix_reported_before_missing_pulses(self):
        # No fix also stops the timepulse; blame the antenna, not the wiring.
        s = PpsSysfs(self.tmp.name, "0.000000000#0", "0.000000000#0")
        m = self._manager(GpsFix(reachable=True, mode=1, satellites_used=0,
                                 satellites_visible=5), s)
        self.assertFalse(m.should_attempt_recovery())
        self.assertIn("no fix", m.reception_fault)


if __name__ == "__main__":
    unittest.main()
