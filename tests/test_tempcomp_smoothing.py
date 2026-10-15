"""chronyd compensates with a smoothed temperature the monitor publishes, and
falls back to the raw sensor when the monitor stops; a noisy input warns."""
import math
import os
import subprocess
import tempfile
import time
import unittest
from unittest import mock

from chrony_monitor import tempcomp

SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(__file__)), "scripts")
T0 = 40000


def jitter(i):
    """A 1 °C-step sensor hopping between 40 and 41 °C, as x86_pkg_temp does."""
    return 40000 + 1000 * ((i * 7919) % 3 == 0)


class SmootherTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "temp")

    def tearDown(self):
        self.tmp.cleanup()

    def test_quantized_jitter_is_smoothed_and_published(self):
        s = tempcomp.SensorSmoother(self.path)
        values = [s.update(jitter(i), now=1000.0 + i) for i in range(1200)]
        tail = values[600:]
        mean = sum(tail) / len(tail)
        std = math.sqrt(sum((v - mean) ** 2 for v in tail) / len(tail))
        self.assertLess(std, 50)                # 0.05 °C, from ±0.5 °C
        self.assertAlmostEqual(mean, 40333, delta=50)
        self.assertTrue(s.publishing(now=1000.0 + 1199))
        with open(self.path) as f:
            # written every few seconds, so within a few seconds of the latest
            self.assertAlmostEqual(float(f.read()), values[-1], delta=10)

    def test_first_minutes_are_the_plain_mean(self):
        s = tempcomp.SensorSmoother(self.path)
        s.update(41000, now=0.0)
        s.update(40000, now=1.0)
        self.assertAlmostEqual(s.value, 40500)

    def test_gap_restarts_the_smoothing(self):
        s = tempcomp.SensorSmoother(self.path)
        for i in range(300):
            s.update(40000, now=float(i))
        s.update(45000, now=300.0 + tempcomp.SMOOTH_GAP_S + 1)
        self.assertEqual(s.value, 45000)

    def test_unwritable_directory_is_not_publishing(self):
        s = tempcomp.SensorSmoother(os.path.join(self.tmp.name, "missing", "temp"))
        s.update(40000, now=0.0)
        self.assertFalse(s.publishing(now=0.0))

    def test_input_link_is_the_stable_sensor(self):
        stable = tempcomp.sensor_identity(tempcomp.STABLE_SENSOR_LINK)
        self.assertEqual(tempcomp.sensor_identity(tempcomp.TEMPCOMP_INPUT_LINK), stable)
        self.assertEqual(tempcomp.sensor_identity(tempcomp.SMOOTHED_TEMP_PATH), stable)


class CompNoiseTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.sensor = os.path.join(self.tmp.name, "temp")
        self.c = tempcomp.TempCompCollector(sensor_path=self.sensor,
                                            data_dir=self.tmp.name,
                                            auto_recal=False)
        self.c._start_time = time.time()
        self.c._config = tempcomp.TempCompConfig(
            sensor_path=self.sensor, interval=30, T0=T0,
            k0=0.0, k1=0.2e-3, k2=0.0, is_active=True)
        patcher = mock.patch.object(tempcomp, "chrony_sensor_read_failing",
                                    return_value=False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def feed(self, temp_of, minutes=40):
        for i in range(minutes * 60):
            t = temp_of(i)
            with open(self.sensor, "w") as f:
                f.write(f"{t}\n")
            self.c.record(t, 3.0)

    def test_raw_quantized_sensor_warns(self):
        self.feed(jitter)
        self.assertEqual(self.c.get_status().warning, "noisy sensor, comp jitters!")

    def test_smoothed_sensor_is_quiet(self):
        s = tempcomp.SensorSmoother(os.path.join(self.tmp.name, "smoothed"))
        self.feed(lambda i: round(s.update(jitter(i), now=float(i))))
        self.assertIsNone(self.c.get_status().warning)

    def test_too_few_minutes_are_not_judged(self):
        self.feed(jitter, minutes=10)
        self.assertIsNone(self.c.get_status().warning)


class InputSwitchTest(unittest.TestCase):
    """update-tempcomp-input.sh against a fixture /run/chrony-monitor."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.run_dir = self.tmp.name
        os.makedirs(os.path.join(self.run_dir, "smoothed"))
        raw = os.path.join(self.run_dir, "raw")
        with open(raw, "w") as f:
            f.write("41000\n")
        os.symlink(raw, os.path.join(self.run_dir, "tempcomp-sensor"))
        self.smoothed = os.path.join(self.run_dir, "smoothed", "temp")

    def tearDown(self):
        self.tmp.cleanup()

    def switch(self):
        env = dict(os.environ, RUN_DIR=self.run_dir, MONITOR_USER="",
                   ENV_FILE=os.path.join(self.run_dir, "none"))
        subprocess.run(["bash", os.path.join(SCRIPTS, "update-tempcomp-input.sh")],
                       env=env, capture_output=True, check=True)
        return os.path.basename(os.readlink(os.path.join(self.run_dir, "tempcomp-input")))

    def publish(self, text, age=0):
        with open(self.smoothed, "w") as f:
            f.write(text)
        t = time.time() - age
        os.utime(self.smoothed, (t, t))

    def test_fresh_smoothed_value_is_used(self):
        self.publish("40321\n")
        self.assertEqual(self.switch(), "temp")

    def test_no_smoothed_value_falls_back(self):
        self.assertEqual(self.switch(), "tempcomp-sensor")

    def test_stale_smoothed_value_falls_back(self):
        self.publish("40321\n")
        self.assertEqual(self.switch(), "temp")
        self.publish("40321\n", age=300)
        self.assertEqual(self.switch(), "tempcomp-sensor")

    def test_implausible_value_falls_back(self):
        for text in ("garbage\n", "0\n", "200000\n"):
            self.publish(text)
            self.assertEqual(self.switch(), "tempcomp-sensor", text)

    def test_symlink_is_not_followed(self):
        os.symlink(os.path.join(self.run_dir, "raw"), self.smoothed)
        self.assertEqual(self.switch(), "tempcomp-sensor")


class MigrationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.link = os.path.join(self.tmp.name, "tempcomp-input")
        with open(self.link, "w") as f:
            f.write("40000\n")

    def tearDown(self):
        self.tmp.cleanup()

    def migrate(self, line):
        conf = os.path.join(self.tmp.name, "chrony.conf")
        with open(conf, "w") as f:
            f.write("driftfile /var/lib/chrony/chrony.drift\n" + line + "\n")
        # No chronyd or systemctl, so nothing real is validated or restarted
        bindir = os.path.join(self.tmp.name, "bin")
        os.makedirs(bindir, exist_ok=True)
        for tool in ("chronyd", "systemctl"):
            p = os.path.join(bindir, tool)
            with open(p, "w") as f:
                f.write("#!/bin/sh\nexit 0\n")
            os.chmod(p, 0o755)
        env = dict(os.environ, CHRONY_CONF=conf, LINK=self.link,
                   PATH=bindir + ":" + os.environ["PATH"])
        subprocess.run(["bash", os.path.join(SCRIPTS, "migrate-tempcomp-sensor.sh")],
                       env=env, capture_output=True)
        with open(conf) as f:
            return f.read().splitlines()[1]

    def test_stable_link_moves_to_input(self):
        self.assertEqual(
            self.migrate("tempcomp /run/chrony-monitor/tempcomp-sensor 30 36968 -0.04 0.0002 0"),
            f"tempcomp {self.link} 30 36968 -0.04 0.0002 0")

    def test_raw_zone_moves_to_input(self):
        self.assertEqual(
            self.migrate("#tempcomp /sys/class/thermal/thermal_zone3/hwmon2/temp1_input 30 1 2 3 4"),
            f"#tempcomp {self.link} 30 1 2 3 4")

    def test_other_path_is_left_alone(self):
        line = "tempcomp /some/other/sensor 30 1 2 3 4"
        self.assertEqual(self.migrate(line), line)


if __name__ == "__main__":
    unittest.main()
