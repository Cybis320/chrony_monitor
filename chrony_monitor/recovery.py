"""PPS recovery logic for automatic fault recovery."""

import os
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime
from typing import List, Tuple


def is_raspberry_pi() -> bool:
    """Detect if running on a Raspberry Pi."""
    try:
        with open('/proc/device-tree/model', 'r') as f:
            return 'raspberry pi' in f.read().lower()
    except (FileNotFoundError, PermissionError):
        return False


@dataclass
class RecoveryConfig:
    """Configuration for recovery behavior."""
    timeout_seconds: int = 60       # Seconds before attempting recovery
    cooldown_seconds: int = 300     # Seconds between recovery attempts
    enabled: bool = True


class RecoveryManager:
    """Manages automatic PPS recovery attempts."""

    def __init__(self, config: RecoveryConfig = None):
        self.config = config or RecoveryConfig()
        self.lock_lost_time: datetime = None
        self.last_recovery_attempt: datetime = None
        self.logs: List[str] = []
        self.is_recovering: bool = False

    def reset(self):
        """Reset recovery state when lock is restored."""
        if self.lock_lost_time is not None:
            self.logs.append(f"Lock restored at {datetime.now().strftime('%H:%M:%S')}")
        self.lock_lost_time = None
        self.is_recovering = False

    def on_lock_lost(self):
        """Called when lock is first lost."""
        if self.lock_lost_time is None:
            self.lock_lost_time = datetime.now()
            self.logs = [f"Lock lost at {self.lock_lost_time.strftime('%H:%M:%S')}"]

    def get_lock_lost_seconds(self) -> int:
        """Get seconds since lock was lost."""
        if self.lock_lost_time is None:
            return 0
        return int((datetime.now() - self.lock_lost_time).total_seconds())

    def should_attempt_recovery(self) -> bool:
        """Check if recovery should be attempted."""
        if not self.config.enabled:
            return False

        if self.lock_lost_time is None:
            return False

        time_lost = self.get_lock_lost_seconds()
        if time_lost < self.config.timeout_seconds:
            return False

        if self.last_recovery_attempt is not None:
            time_since_last = (datetime.now() - self.last_recovery_attempt).total_seconds()
            if time_since_last < self.config.cooldown_seconds:
                return False

        return True

    def attempt_recovery(self) -> Tuple[bool, List[str]]:
        """
        Attempt to recover PPS connection.
        Returns: (success, log_messages)
        """
        self.is_recovering = True
        self.last_recovery_attempt = datetime.now()
        logs = []

        time_lost = self.get_lock_lost_seconds()
        logs.append(f"Attempting recovery after {time_lost}s...")

        try:
            success, recovery_logs = self._do_recovery()
            logs.extend(recovery_logs)
            self.logs.extend(logs)
            return success, logs
        except Exception as e:
            logs.append(f"Recovery error: {str(e)}")
            self.logs.extend(logs)
            return False, logs

    def _get_sudo_prefix(self) -> List[str]:
        """Get sudo prefix if not running as root."""
        if os.geteuid() == 0:
            return []
        return ["sudo", "-n"]  # -n for non-interactive

    def _check_ldattach(self) -> bool:
        """Check if ldattach process is running."""
        try:
            result = subprocess.run(
                ["pgrep", "-f", "ldattach.*ttyS"],
                capture_output=True,
                text=True
            )
            return result.returncode == 0
        except Exception:
            return False

    def _get_pps_device(self) -> str:
        """Get the PPS device path."""
        if os.path.exists('/dev/gps-pps'):
            return '/dev/gps-pps'
        if os.path.exists('/dev/serial-pps'):
            return '/dev/serial-pps'
        # Find GPIO PPS device by checking sysfs
        import glob as globmod
        for name_path in sorted(globmod.glob('/sys/class/pps/pps*/name')):
            try:
                with open(name_path) as f:
                    name = f.read().strip()
                # Match GPIO PPS: "pps@<pin>.*" on RPi 5, "pps-gpio" on older kernels
                if name.startswith('pps@') or name.startswith('pps-gpio'):
                    return '/dev/' + os.path.basename(os.path.dirname(name_path))
            except (OSError, PermissionError):
                continue
        return '/dev/pps0'

    def _test_pps(self) -> bool:
        """Test if PPS device is working by checking sysfs pulse counter."""
        pps_dev = self._get_pps_device()
        # Resolve symlink to get the ppsN name
        real_path = os.path.realpath(pps_dev)
        pps_name = os.path.basename(real_path)
        assert_path = f"/sys/class/pps/{pps_name}/assert"

        try:
            with open(assert_path) as f:
                count1 = f.read().strip()
            time.sleep(2)
            with open(assert_path) as f:
                count2 = f.read().strip()
            # If the assert timestamp changed, PPS is receiving pulses
            return count1 != count2
        except Exception:
            return False

    def _do_recovery(self) -> Tuple[bool, List[str]]:
        """Perform the actual recovery steps."""
        if is_raspberry_pi():
            return self._do_gpio_recovery()
        return self._do_serial_recovery()

    def _do_gpio_recovery(self) -> Tuple[bool, List[str]]:
        """Recovery for GPIO PPS (Raspberry Pi)."""
        logs = []
        sudo = self._get_sudo_prefix()

        if sudo:
            logs.append("Not running as root, using sudo...")

        # GPIO PPS is kernel-managed — recovery is just restarting services
        logs.append("Restarting gpsd and chrony...")

        try:
            subprocess.run(sudo + ["systemctl", "stop", "chrony.service"], capture_output=True)
            subprocess.run(sudo + ["systemctl", "stop", "gpsd.service"], capture_output=True)
            time.sleep(1)

            subprocess.run(sudo + ["systemctl", "start", "gpsd.service"], capture_output=True)
            time.sleep(2)
            subprocess.run(sudo + ["systemctl", "start", "chrony.service"], capture_output=True)
            time.sleep(3)

            if self._test_pps():
                logs.append("PPS recovered via service restart")
                return True, logs
            else:
                logs.append("PPS still not working after service restart")
                logs.append("Check GPIO wiring and pps-gpio overlay in config.txt")
        except Exception as e:
            logs.append(f"Service restart failed: {e}")

        return False, logs

    def _do_serial_recovery(self) -> Tuple[bool, List[str]]:
        """Recovery for serial PPS (Ubuntu/x86)."""
        logs = []
        sudo = self._get_sudo_prefix()

        if sudo:
            logs.append("Not running as root, using sudo...")

        # Step 1: Try restarting services if serial-pps service exists
        try:
            service_check = subprocess.run(
                sudo + ["systemctl", "is-active", "serial-pps"],
                capture_output=True,
                text=True
            )

            if service_check.returncode == 0:
                logs.append("serial-pps service is active, restarting services...")

                # Stop services in reverse order
                subprocess.run(sudo + ["systemctl", "stop", "chrony.service"], capture_output=True)
                subprocess.run(sudo + ["systemctl", "stop", "gpsd.service"], capture_output=True)
                subprocess.run(sudo + ["systemctl", "stop", "serial-pps.service"], capture_output=True)
                time.sleep(1)

                # Kill stale ldattach
                subprocess.run(sudo + ["pkill", "ldattach"], capture_output=True)
                time.sleep(1)

                # Start services in order
                subprocess.run(sudo + ["systemctl", "start", "serial-pps.service"], capture_output=True)
                time.sleep(2)
                subprocess.run(sudo + ["systemctl", "start", "gpsd.service"], capture_output=True)
                time.sleep(2)
                subprocess.run(sudo + ["systemctl", "start", "chrony.service"], capture_output=True)
                time.sleep(3)

                if self._test_pps():
                    logs.append("PPS recovered via service restart")
                    return True, logs
                else:
                    logs.append("PPS still not working after service restart")

        except Exception as e:
            logs.append(f"Service restart failed: {e}")

        # Step 2: Try manual ldattach if service approach failed
        if not self._check_ldattach():
            logs.append("Attempting manual PPS initialization...")

            subprocess.run(sudo + ["pkill", "ldattach"], capture_output=True)
            time.sleep(1)

            # Try each serial port
            for port in ["/dev/ttyS0", "/dev/ttyS1", "/dev/ttyS2", "/dev/ttyS3", "/dev/ttyS4"]:
                if not os.path.exists(port):
                    continue

                logs.append(f"Trying {port}...")

                try:
                    # Start ldattach in background
                    subprocess.Popen(
                        sudo + ["ldattach", "18", port],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL
                    )
                    time.sleep(2)

                    if self._test_pps():
                        logs.append(f"PPS recovered on {port}")

                        # Restart dependent services
                        subprocess.run(sudo + ["systemctl", "restart", "gpsd.service"], capture_output=True)
                        time.sleep(1)
                        subprocess.run(sudo + ["systemctl", "restart", "chrony.service"], capture_output=True)
                        logs.append("Services restarted")
                        return True, logs

                except Exception as e:
                    logs.append(f"Failed on {port}: {e}")
                    continue

        else:
            logs.append("ldattach is running but PPS not working, restarting all services...")
            subprocess.run(sudo + ["systemctl", "restart", "serial-pps.service"], capture_output=True)
            time.sleep(2)
            subprocess.run(sudo + ["systemctl", "restart", "gpsd.service"], capture_output=True)
            time.sleep(2)
            subprocess.run(sudo + ["systemctl", "restart", "chrony.service"], capture_output=True)
            time.sleep(3)

            if self._test_pps():
                logs.append("PPS recovered via full service restart")
                return True, logs
            else:
                logs.append("PPS still not working after full restart")

        return False, logs

    def get_recent_logs(self, count: int = 5) -> List[str]:
        """Get most recent log entries."""
        return self.logs[-count:] if self.logs else []
