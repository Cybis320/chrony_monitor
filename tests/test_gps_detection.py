"""GPS receiver detection must key on gpsd's configured device, not on any
serial gadget that happens to be plugged in."""
import os
import tempfile
import unittest

from chrony_monitor.status import (
    chrony_has_refclock, gps_receiver_state, gpsd_configured_devices)


class GpsReceiverStateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dev = os.path.join(self.tmp.name, "dev")
        os.makedirs(self.dev)
        self.defaults = os.path.join(self.tmp.name, "gpsd")

    def tearDown(self):
        self.tmp.cleanup()

    def _write_defaults(self, body):
        with open(self.defaults, "w") as f:
            f.write(body)

    def _touch(self, name):
        path = os.path.join(self.dev, name)
        open(path, "w").close()
        return path

    def test_configured_device_present(self):
        acm = self._touch("ttyACM0")
        self._write_defaults(f'START_DAEMON="true"\nDEVICES="{acm}"\n')
        present, configured, reason = gps_receiver_state(self.defaults, [])
        self.assertTrue(present)
        self.assertTrue(configured)
        self.assertIn("ttyACM0", reason)

    def test_configured_device_missing_is_not_a_receiver(self):
        # The regression: DEVICES names a receiver that is unplugged, while an
        # unrelated USB-serial gadget (LED flasher) sits on ttyUSB1.
        self._touch("ttyUSB1")
        acm = os.path.join(self.dev, "ttyACM0")
        self._write_defaults(f'DEVICES="{acm}"\n')
        present, configured, reason = gps_receiver_state(
            self.defaults, [os.path.join(self.dev, "ttyUSB*")])
        self.assertFalse(present)
        self.assertTrue(configured)
        self.assertIn("not present", reason)

    def test_hotplug_mode_uses_udev_symlink_only(self):
        self._write_defaults('DEVICES=""\n')
        self._touch("ttyUSB0")
        present, configured, _ = gps_receiver_state(
            self.defaults, [os.path.join(self.dev, "gps[0-9]*")])
        self.assertFalse(present)
        self.assertFalse(configured)
        self._touch("gps0")
        present, configured, reason = gps_receiver_state(
            self.defaults, [os.path.join(self.dev, "gps[0-9]*")])
        self.assertTrue(present)
        self.assertTrue(configured)
        self.assertIn("gps0", reason)

    def test_no_gpsd_config_at_all(self):
        present, configured, _ = gps_receiver_state(
            os.path.join(self.tmp.name, "missing"), [])
        self.assertFalse(present)
        self.assertFalse(configured)

    def test_devices_parsing(self):
        self._write_defaults(
            '# comment\nDEVICES=""\nDEVICES="/dev/ttyAMA0 /dev/pps0"  # trailing\n')
        self.assertEqual(gpsd_configured_devices(self.defaults),
                         ["/dev/ttyAMA0", "/dev/pps0"])
        self._write_defaults("DEVICES=/dev/ttyS0\n")
        self.assertEqual(gpsd_configured_devices(self.defaults), ["/dev/ttyS0"])
        self._write_defaults("export DEVICES='/dev/serial0'\n")
        self.assertEqual(gpsd_configured_devices(self.defaults), ["/dev/serial0"])


class ChronyRefclockTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def _conf(self, body):
        path = os.path.join(self.tmp.name, "chrony.conf")
        with open(path, "w") as f:
            f.write(body)
        return path

    def test_refclock_present(self):
        conf = self._conf("pool pool.ntp.org iburst\nrefclock PPS /dev/pps0 lock GPS\n")
        self.assertIs(chrony_has_refclock([conf]), True)

    def test_refclock_absent_or_commented(self):
        conf = self._conf("pool pool.ntp.org iburst\n# refclock PPS /dev/pps0\n")
        self.assertIs(chrony_has_refclock([conf]), False)

    def test_unreadable_config_is_unknown(self):
        self.assertIsNone(chrony_has_refclock([os.path.join(self.tmp.name, "nope")]))


if __name__ == "__main__":
    unittest.main()
