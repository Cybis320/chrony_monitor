"""Tests for the operator's tempcomp data reset (the monitor's 'r' key)."""

import glob
import os
import tempfile
import time
import unittest
from unittest import mock

from chrony_monitor import monitor, tempcomp


class TestResetData(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.c = tempcomp.TempCompCollector(
            sensor_path=os.path.join(self.tmp.name, "temp"),
            data_dir=self.tmp.name, auto_recal=False)
        for i in range(100):
            ts = 1790000000 + 60 * i
            self.c._timestamps.append(ts)
            self.c._temps.append(70000 + 50 * i)
            self.c._freqs.append(-11.0 + 0.001 * i)
            self.c._append_csv(ts, 70000 + 50 * i, -11.0)
        self.c._append_reported(1790006000, 75000, -0.8, 0.3)
        self.c._minute_temps.append(75000)
        self.c._minute_count = 1
        self.c._fit = (75000, -11.0, 0.0, 0.0)
        self.c._cached_correlation = 0.61

    def test_archives_csv_and_starts_clean(self):
        self.c.reset_data()
        self.assertFalse(os.path.exists(self.c._csv_path))
        archived = glob.glob(self.c._csv_path + ".reset-*.old")
        self.assertEqual(len(archived), 1)
        with open(archived[0]) as f:
            self.assertEqual(len(f.readlines()), 100)
        self.assertFalse(os.path.exists(self.c._reported_path))
        self.assertEqual(len(self.c._temps), 0)
        self.assertEqual(self.c._minute_count, 0)
        self.assertIsNone(self.c._fit)
        self.assertIsNone(self.c.get_status().correlation)

    def test_fresh_rows_are_not_repaired_on_next_load(self):
        # Without a current format marker, load() would treat new rows as
        # pre-fix data and shift them by a comp.
        self.c.reset_data()
        self.assertEqual(self.c._csv_format(), tempcomp.CSV_FORMAT)

    def test_logged_but_not_counted_as_recalibration(self):
        before = self.c._recal_times()
        self.c.reset_data()
        self.assertEqual(self.c._recal_times(), before)
        self.assertIn("reset", self.c.recal_logs[-1])


class TestResetKey(unittest.TestCase):
    def _monitor(self):
        m = monitor.Monitor.__new__(monitor.Monitor)
        m.tempcomp = mock.Mock()
        m._confirm_reset_until = 0.0
        m._notice = None
        return m

    def test_r_then_y_resets(self):
        m = self._monitor()
        m._handle_key(ord('r'))
        self.assertIn("y = yes", m._prompt())
        m._handle_key(-1)               # interval passes with no key
        m.tempcomp.reset_data.assert_not_called()
        m._handle_key(ord('y'))
        m.tempcomp.reset_data.assert_called_once()
        self.assertIn("reset", m._prompt())

    def test_other_key_cancels(self):
        m = self._monitor()
        m._handle_key(ord('r'))
        m._handle_key(ord('n'))
        m._handle_key(ord('y'))         # a late 'y' is not a confirmation
        m.tempcomp.reset_data.assert_not_called()

    def test_prompt_expires(self):
        m = self._monitor()
        m._handle_key(ord('r'))
        m._confirm_reset_until = time.time() - 1
        m._handle_key(ord('y'))
        m.tempcomp.reset_data.assert_not_called()

    def test_y_alone_does_nothing(self):
        m = self._monitor()
        m._handle_key(ord('y'))
        m.tempcomp.reset_data.assert_not_called()


if __name__ == "__main__":
    unittest.main()
