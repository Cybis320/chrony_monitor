"""The learning phase logs every candidate sensor, chooses the one (and the
smoothing) that best predicts held-out frequency, and keeps it until a reset."""
import math
import os
import subprocess
import tempfile
import unittest
from collections import deque
from unittest import mock

from chrony_monitor import display, tempcomp

SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(__file__)), "scripts")
START = 1790000000
NOW = START + 3000 * 60


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text + "\n")


def temp_at(i):
    """Board temperature, °C, minute i: a daily swing plus a slower drift."""
    return 38.0 + 3.0 * math.sin(2 * math.pi * i / 1440) + 1.5 * math.sin(i / 3000)


def history(minutes=3000, lag_min=10):
    """Rows where the crystal follows sensor 'good' with a lag, and 'bad'
    only partly tracks the board, plus a load-driven swing of its own."""
    rows, crystal = [], temp_at(0)
    for i in range(minutes):
        t = temp_at(i)
        crystal += (1 - math.exp(-1 / lag_min)) * (t - crystal)
        freq = 3.0 - 0.1 * (crystal - 38) + 0.004 * (crystal - 38) ** 2
        bad = 40.0 + 0.3 * (t - 38) + 2.0 * math.sin(i / 37.0)
        rows.append((START + 60 * i, freq, [t * 1000, bad * 1000]))
    return rows


class IdentityTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.thermal = os.path.join(self.tmp.name, "thermal")
        self.hwmon = os.path.join(self.tmp.name, "hwmon")

    def zone(self, n, ztype, millideg):
        zdir = os.path.join(self.thermal, f"thermal_zone{n}")
        write(os.path.join(zdir, "type"), ztype)
        write(os.path.join(zdir, "temp"), str(millideg))
        return zdir

    def hwmon_dev(self, n, name, device, channels):
        hdir = os.path.join(self.hwmon, f"hwmon{n}")
        write(os.path.join(hdir, "name"), name)
        dev = os.path.join(self.tmp.name, "devices", device)
        os.makedirs(dev, exist_ok=True)
        os.symlink(dev, os.path.join(hdir, "device"))
        for k, (label, millideg) in channels.items():
            write(os.path.join(hdir, f"temp{k}_input"), str(millideg))
            if label:
                write(os.path.join(hdir, f"temp{k}_label"), label)
        return hdir

    def test_second_zone_of_a_type_is_told_apart(self):
        z0 = self.zone(0, "x86_pkg_temp", 42000)
        z1 = self.zone(1, "x86_pkg_temp", 40000)
        self.assertEqual(tempcomp.sensor_identity(os.path.join(z0, "temp"), self.thermal),
                         "type:x86_pkg_temp")
        self.assertEqual(tempcomp.sensor_identity(os.path.join(z1, "temp"), self.thermal),
                         "type:x86_pkg_temp#1")

    def test_zone_order_is_numeric(self):
        self.zone(2, "x86_pkg_temp", 42000)
        z10 = self.zone(10, "x86_pkg_temp", 40000)
        self.assertEqual(tempcomp.sensor_identity(os.path.join(z10, "temp"), self.thermal),
                         "type:x86_pkg_temp#1")

    def test_hwmon_identity_names_device_and_label(self):
        hdir = self.hwmon_dev(2, "coretemp", "coretemp.1",
                              {1: ("Package id 1", 40000), 5: ("Core 3", 38000),
                               9: (None, 37000)})
        ident = tempcomp.sensor_identity
        self.assertEqual(ident(os.path.join(hdir, "temp5_input"), self.thermal),
                         "hwmon:coretemp.1:Core 3")
        self.assertEqual(ident(os.path.join(hdir, "temp9_input"), self.thermal),
                         "hwmon:coretemp.1:temp9")

    def test_list_covers_zones_and_hwmon_but_not_mirrors_or_implausible(self):
        self.zone(0, "x86_pkg_temp", 42000)
        self.zone(1, "acpitz", 0)                           # implausible
        self.hwmon_dev(1, "coretemp", "coretemp.0",
                       {1: ("Package id 0", 42000), 2: ("Core 0", 39000)})
        # A zone's hwmon mirror, reached through /sys/class/hwmon
        mirror = os.path.join(self.thermal, "thermal_zone0", "hwmon0")
        write(os.path.join(mirror, "name"), "x86_pkg_temp")
        write(os.path.join(mirror, "temp1_input"), "42000")
        os.symlink(mirror, os.path.join(self.hwmon, "hwmon0"))
        found = tempcomp.list_temp_sensors(self.thermal, self.hwmon)
        self.assertEqual(sorted(found), ["hwmon:coretemp.0:Core 0",
                                         "hwmon:coretemp.0:Package id 0",
                                         "type:x86_pkg_temp"])

    def test_resolver_prefers_the_pinned_sensor(self):
        found = {"type:x86_pkg_temp": "/z0", "type:x86_pkg_temp#1": "/z1"}
        with mock.patch.object(tempcomp, "read_sensor_pin", return_value="type:x86_pkg_temp#1"), \
             mock.patch.object(tempcomp, "list_temp_sensors", return_value=found), \
             mock.patch("builtins.print") as out:
            self.assertEqual(tempcomp._cli([]), 0)
        out.assert_called_once_with("/z1")

    def test_resolver_ranks_when_the_pinned_sensor_is_gone(self):
        with mock.patch.object(tempcomp, "read_sensor_pin", return_value="hwmon:gone:x"), \
             mock.patch.object(tempcomp, "list_temp_sensors", return_value={}), \
             mock.patch.object(tempcomp, "rank_temp_sensors", return_value=[(10, "/pch")]), \
             mock.patch("builtins.print") as out:
            self.assertEqual(tempcomp._cli([]), 0)
        self.assertEqual(out.call_args_list[-1], mock.call("/pch"))


class ScoringTest(unittest.TestCase):
    def test_the_sensor_the_crystal_follows_wins_with_about_its_lag(self):
        ranking = tempcomp.score_sensors(history(), ["good", "bad"])
        r2, ident, tau = ranking[0][:3]
        self.assertEqual(ident, "good")
        self.assertGreater(r2, 0.95)
        self.assertIn(tau, (5, 10, 20))
        self.assertLess(ranking[1][0], 0.5)

    def test_a_sensor_missing_too_often_is_not_scored(self):
        rows = [(ts, f, [t[0], t[1] if i % 2 else None])
                for i, (ts, f, t) in enumerate(history(600))]
        self.assertEqual([r[1] for r in tempcomp.score_sensors(rows, ["good", "bad"])],
                         ["good"])


class LearnerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.found = {"type:a": os.path.join(self.tmp.name, "a"),
                      "type:b": os.path.join(self.tmp.name, "b")}
        write(self.found["type:a"], "40000")
        write(self.found["type:b"], "38000")
        p = mock.patch.object(tempcomp, "list_temp_sensors", return_value=self.found)
        p.start()
        self.addCleanup(p.stop)

    def test_minutes_survive_a_restart(self):
        lr = tempcomp.SensorLearner(self.tmp.name)
        lr.load()
        for ts in (START, START + 60):
            lr.sample()
            lr.sample()
            lr.close_minute(ts, 3.0, keep=True)
        lr.close_minute(START + 120, 9.0, keep=False)       # untrusted: dropped
        again = tempcomp.SensorLearner(self.tmp.name)
        with mock.patch.object(tempcomp.time, "time", return_value=START + 180):
            again.load()
        self.assertEqual([r[0] for r in again.rows], [START, START + 60])
        self.assertEqual(list(again.rows[0][2]), [40000, 38000])
        self.assertEqual(again.rows[0][2].typecode, "f")      # compact

    def test_a_new_sensor_gets_a_column_old_rows_lack(self):
        lr = tempcomp.SensorLearner(self.tmp.name)
        lr.load()
        lr.sample()
        lr.close_minute(START, 3.0, keep=True)
        self.found["type:c"] = self.found["type:a"]
        again = tempcomp.SensorLearner(self.tmp.name)
        with mock.patch.object(tempcomp.time, "time", return_value=START + 60):
            again.load()
        self.assertEqual(list(again.rows[0][2])[:2], [40000, 38000])
        self.assertTrue(math.isnan(again.rows[0][2][2]))
        with open(os.path.join(self.tmp.name, "learning.csv")) as f:
            self.assertEqual(f.readline().strip(), "ts,freq,type:a,type:b,type:c")

    def test_no_winner_before_enough_evidence(self):
        lr = tempcomp.SensorLearner(self.tmp.name)
        lr.sensors = ["type:a", "type:b"]
        lr.rows = deque(history(1500))                       # 25 h
        lr.score(0, background=False)
        self.assertIsNone(lr.winner("type:b"))
        lr.rows = deque(history(3000))
        lr.score(0, background=False)
        self.assertEqual(lr.winner("type:b")[0], "type:a")

    def test_the_sensor_in_use_is_kept_when_nearly_as_good(self):
        lr = tempcomp.SensorLearner(self.tmp.name)
        lr.sensors = ["type:a", "type:b"]
        lr.rows = deque(history(3000))
        lr.ranking = [(0.80, "type:a", 5, 3.0, 9.0), (0.78, "type:b", 2, 3.0, 9.0)]
        self.assertEqual(lr.winner("type:b")[0], "type:b")
        lr.ranking = [(0.80, "type:a", 5, 3.0, 9.0), (0.70, "type:b", 2, 3.0, 9.0)]
        self.assertEqual(lr.winner("type:b")[0], "type:a")

    def test_a_package_sensor_is_taken_over_a_core_when_close(self):
        lr = tempcomp.SensorLearner(self.tmp.name)
        lr.rows = deque(history(3000))
        core, pkg = "hwmon:coretemp.1:Core 3", "hwmon:coretemp.1:Package id 1"
        lr.ranking = [(0.815, core, 5, 3.6, 9.2), (0.775, pkg, 2, 3.8, 9.4),
                      (0.774, "type:x86_pkg_temp#1", 2, 3.7, 9.3)]
        self.assertEqual(lr.winner("type:x86_pkg_temp")[0], pkg)
        lr.ranking[1] = (0.70, pkg, 2, 3.8, 9.4)
        lr.ranking[2] = (0.69, "type:x86_pkg_temp#1", 2, 3.7, 9.3)
        self.assertEqual(lr.winner("type:x86_pkg_temp")[0], core)

    def test_a_better_sensor_is_reported_after_a_week_but_not_switched_to(self):
        lr = tempcomp.SensorLearner(self.tmp.name)
        lr.choice = ("type:b", 2, 0.7)
        lr.rows = deque(history(10080 - 120))           # a week less 2 h
        lr.sensors = ["type:a", "type:b"]
        lr.ranking = [(0.85, "type:a", 5, 3.0, 9.0), (0.60, "type:b", 2, 3.0, 9.0)]
        self.assertIsNone(lr.rival())
        self.assertFalse(lr.due(0))
        lr.rows = deque(history(10080))
        self.assertEqual(lr.rival(), ("type:a", 0.85, 0.60))
        self.assertTrue(lr.due(1e10))
        lr.ranking[1] = (0.80, "type:b", 2, 3.0, 9.0)       # within the margin
        self.assertIsNone(lr.rival())
        self.assertEqual(lr.choice[0], "type:b")

    def test_logging_continues_after_the_choice(self):
        lr = tempcomp.SensorLearner(self.tmp.name)
        lr.load()
        lr.decide("type:a", 5, 0.8)
        lr.sample()
        lr.close_minute(START, 3.0, keep=True)
        again = tempcomp.SensorLearner(self.tmp.name)
        with mock.patch.object(tempcomp.time, "time", return_value=START + 60):
            again.load()
        self.assertEqual(again.choice[0], "type:a")
        self.assertEqual(len(again.rows), 1)


class ChoiceTest(unittest.TestCase):
    """The collector stays off while learning, then pins, refits and keeps."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.c = tempcomp.TempCompCollector(
            sensor_path=os.path.join(self.tmp.name, "link"), data_dir=self.tmp.name)
        self.c.learner = tempcomp.SensorLearner(self.tmp.name)
        self.c.learner.sensors = ["type:a", "type:b"]
        self.c.learner.rows = deque(history(3000))
        self.link_names = "type:b"
        self.helper = []
        self.applied = []
        self.c._apply_calibration = lambda *k: self.applied.append(k)

        def helper(*args):
            self.helper.append(args)
            if self.helper_error:
                return self.helper_error
            self.link_names = args[1]
        self.helper_error = None
        self.c._run_apply_helper = helper
        for name, value in (
                ("sensor_identity", lambda p, *a: self.link_names),
                ("preferred_sensor_path", lambda: self.c.sensor_path)):
            p = mock.patch.object(tempcomp, name, side_effect=value)
            p.start()
            self.addCleanup(p.stop)

    def score(self):
        self.c.learner.score(NOW, background=False)
        self.c._check_learning(NOW)

    def log(self):
        with open(os.path.join(self.tmp.name, "recalibrations.log")) as f:
            return f.read()

    def test_no_recalibration_while_learning(self):
        for i in range(tempcomp.MIN_SAMPLES_FIT * 2):
            self.c._timestamps.append(START + 60 * i)
            self.c._temps.append(30000 + 10 * i)
            self.c._freqs.append(3.0 - 0.0001 * i)
        self.c._check_recalibration()
        self.assertEqual(self.applied, [])

    def test_choice_pins_refits_and_sticks(self):
        self.score()
        self.assertEqual(self.helper, [("--sensor", "type:a")])
        self.assertEqual(self.c.learner.choice[0], "type:a")
        self.assertIn(self.c._smooth_tau, (300, 600, 1200))
        # Fit data is the chosen sensor's logged minutes, and refit at once
        self.assertEqual(len(self.c._temps), 3000)
        self.assertEqual(len(self.applied), 1)
        self.assertIn("Sensor chosen: a, smoothed", self.log())
        with open(os.path.join(self.tmp.name, "sensor")) as f:
            self.assertEqual(f.read().strip(), "type:a")
        # Kept across a restart: no more learning
        again = tempcomp.SensorLearner(self.tmp.name)
        again.load()
        self.assertFalse(again.learning)
        self.assertEqual(again.choice[0], "type:a")

    def test_failed_pin_is_logged_once_and_retried(self):
        self.helper_error = "sudo: a password is required"
        self.score()
        self.score()
        self.assertTrue(self.c.learner.learning)
        self.assertEqual(self.log().count("Sensor choice a pending"), 1)
        self.c._last_attempt_time -= tempcomp.LEARN_SCORE_S
        self.helper_error = None
        self.score()
        self.assertFalse(self.c.learner.learning)

    def test_reset_learns_again(self):
        self.score()
        with mock.patch.object(tempcomp, "list_temp_sensors",
                               return_value={"type:a": "/a", "type:b": "/b"}):
            self.c.reset_data()
        self.assertTrue(self.c.learner.learning)
        self.assertEqual(len(self.c.learner.rows), 0)
        self.assertFalse(os.path.exists(os.path.join(self.tmp.name, "sensor_choice")))

    def test_a_rival_is_logged_once_and_shown(self):
        self.score()
        self.c.learner.rows = deque(history(10080))
        self.c.learner.ranking = [(0.9, "type:b", 2, 3.0, 9.0),
                                  (0.6, "type:a", 5, 3.0, 9.0)]
        self.c.learner._scored_time = NOW
        self.c._check_learning(NOW)
        self.c._check_learning(NOW)
        self.assertEqual(self.log().count("Sensor b fits better"), 1)
        self.assertEqual(self.c.learner.choice[0], "type:a")
        lines = display.format_tempcomp_lines(self.c.get_status())
        self.assertIn("better sensor found", lines[0])
        self.assertEqual(lines[2], "better: b R2=0.90 vs 0.60 (r to relearn)")

    def test_status_shows_learning_then_the_choice(self):
        self.c.learner.score(NOW, background=False)
        line2 = display.format_tempcomp_lines(self.c.get_status())[1]
        self.assertTrue(line2.startswith("learning 49h: best a"), line2)
        self.c._check_learning(NOW)
        line2 = display.format_tempcomp_lines(self.c.get_status())[1]
        self.assertRegex(line2, r"^sensor a, smoothed (5|10|20)m$")


class KeepDataTest(unittest.TestCase):
    """Two ways the chosen sensor's data was being thrown away."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def collector(self):
        c = tempcomp.TempCompCollector(
            sensor_path=os.path.join(self.tmp.name, "link"),
            data_dir=self.tmp.name, auto_recal=False)
        return c

    def test_a_chosen_hwmon_sensor_is_not_taken_for_a_change(self):
        write(os.path.join(self.tmp.name, "sensor"), "hwmon:coretemp.0:Core 8")
        write(os.path.join(self.tmp.name, "tempcomp.csv"), f"{START},33000,3.2")
        with mock.patch.object(tempcomp, "sensor_identity",
                               return_value="hwmon:coretemp.0:Core 8"):
            self.collector()._invalidate_data_if_sensor_changed()
        self.assertTrue(os.path.exists(os.path.join(self.tmp.name, "tempcomp.csv")))

    def test_a_cold_day_is_kept_and_a_glitch_is_not(self):
        # Five steady days at 3.2 ppm, then a day the room cools and the
        # frequency climbs smoothly by 1.3 ppm, with a one-minute glitch
        ts = [START + 60 * i for i in range(7200)]
        freqs = [3.2] * 5760 + [3.2 + 1.3 * min(1, i / 600) for i in range(1440)]
        freqs = [f + 0.003 * math.sin(i) for i, f in enumerate(freqs)]
        freqs[3000] -= 0.5
        keep = tempcomp._outlier_mask(ts, freqs)
        self.assertFalse(keep[3000])
        self.assertTrue(all(keep[5760:]))
        # and none of it is deleted from disk at startup
        with open(os.path.join(self.tmp.name, "tempcomp.csv"), "w") as f:
            for t, fr in zip(ts, freqs):
                f.write(f"{t},30000,{fr:.6f}\n")
        tempcomp.TempCompCollector._mark_csv_format(self.collector())
        with mock.patch.object(tempcomp.time, "time", return_value=ts[-1]):
            c = self.collector()
            c._load_csv()
        self.assertEqual(len(c._freqs), 7200)
        with open(os.path.join(self.tmp.name, "tempcomp.csv")) as f:
            self.assertEqual(sum(1 for _ in f), 7200)


class PinHelperTest(unittest.TestCase):
    def run_helper(self, ident):
        return subprocess.run(["bash", os.path.join(SCRIPTS, "apply-tempcomp.sh"),
                               "--sensor", ident], capture_output=True, text=True)

    def test_identities_that_are_not_names_are_refused(self):
        for bad in ("/etc/shadow", "type:../../x", "hwmon:a\ntype:b", "", "path:/x"):
            proc = self.run_helper(bad)
            self.assertEqual(proc.returncode, 1, bad)
            self.assertIn("invalid sensor identity", proc.stderr)


if __name__ == "__main__":
    unittest.main()
