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

    def _activate(self):
        self.c._config = tempcomp.TempCompConfig(
            sensor_path="/run/x", interval=30, T0=75290, k0=0.4, k1=8e-5, k2=0.0,
            is_active=True, conf_path="/etc/chrony/chrony.conf")
        self.c._cal_range = (65.0, 87.0)
        self.c._save_cal_range()
        open(self.c._cal_reversed_path, "w").close()

    def test_disables_the_fit_made_from_the_data(self):
        self._activate()
        inactive = tempcomp.TempCompConfig(**{**vars(self.c._config), "is_active": False})
        with mock.patch.object(self.c, "_run_apply_helper", return_value=None) as helper, \
                mock.patch.object(tempcomp, "parse_chrony_tempcomp", return_value=inactive):
            self.assertIsNone(self.c.reset_data())
        helper.assert_called_once_with("--disable")
        self.assertFalse(self.c._config.is_active)
        self.assertIsNone(self.c._cal_range)
        self.assertFalse(os.path.exists(self.c._cal_range_path))
        self.assertFalse(os.path.exists(self.c._cal_reversed_path))

    def test_helper_failure_resets_data_but_keeps_calibration_state(self):
        # e.g. a station whose installed helper predates --disable
        self._activate()
        with mock.patch.object(self.c, "_run_apply_helper",
                               return_value="proposed file not found: --disable"):
            self.assertIn("--disable", self.c.reset_data())
        self.assertTrue(self.c._config.is_active)
        self.assertEqual(self.c._cal_range, (65.0, 87.0))
        self.assertEqual(len(self.c._temps), 0)
        self.assertIn("left on", self.c.recal_logs[-1])

    def _passes_cooldown(self) -> bool:
        """Does _check_recalibration get past its cooldown to the sample count?"""
        self.c._temps = mock.MagicMock()
        self.c._temps.__len__.return_value = 0
        self.c._check_recalibration()
        return self.c._temps.__len__.called

    def test_no_cooldown_without_an_active_calibration(self):
        # A recalibration logged an hour ago must not keep tempcomp off for a day.
        self.c._last_recal_time = time.time() - 3600
        self.assertTrue(self._passes_cooldown())
        self._activate()
        os.remove(self.c._cal_reversed_path)
        self.assertFalse(self._passes_cooldown())


class TestMinuteSamples(unittest.TestCase):
    def test_a_slow_loop_still_samples_once_a_minute(self):
        # The monitor loop runs ~1.3 s per pass; 60 passes must not make a
        # "minute" sample, or MIN_SAMPLES_FIT's 6 hours stretches to 8.
        with tempfile.TemporaryDirectory() as tmp:
            c = tempcomp.TempCompCollector(sensor_path=os.path.join(tmp, "temp"),
                                           data_dir=tmp, auto_recal=False)
            clock = [1790000000.0]
            with mock.patch.object(tempcomp.time, "time", lambda: clock[0]):
                for _ in range(10 * 47):        # 10 min at 1.3 s per pass
                    c.record(70000, -1.0)
                    clock[0] += 1.3
            self.assertIn(len(c._temps), (9, 10))


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
