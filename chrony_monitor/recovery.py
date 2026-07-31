"""PPS recovery logic for automatic fault recovery."""

import json
import os
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional, Tuple


def is_raspberry_pi() -> bool:
    """Detect if running on a Raspberry Pi."""
    try:
        with open('/proc/device-tree/model', 'r') as f:
            return 'raspberry pi' in f.read().lower()
    except (FileNotFoundError, PermissionError):
        return False


@dataclass
class GpsFix:
    """Snapshot of what gpsd reports about the receiver."""
    reachable: bool = False         # gpsd answered at all
    mode: int = 0                   # TPV mode: 0/1 = no fix, 2 = 2D, 3 = 3D
    satellites_used: int = 0
    satellites_visible: int = 0

    @property
    def has_fix(self) -> bool:
        """Receiver has a solution, so it will drive the timepulse."""
        return self.mode >= 2 and self.satellites_used > 0

    @property
    def receiver_talking(self) -> bool:
        """gpsd is parsing sentences from the receiver — the service chain is alive."""
        return self.reachable and self.satellites_visible > 0


def query_gps_fix() -> GpsFix:
    """
    Ask gpsd for the current fix state.

    An unreachable gpsd (dead, or gpspipe not installed) yields reachable=False,
    which callers must treat as "unknown" rather than "no fix".
    """
    try:
        out = subprocess.check_output(
            ["gpspipe", "-w", "-n", "15"],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=8
        )
    except Exception:
        return GpsFix()

    fix = GpsFix(reachable=True)
    for line in out.splitlines():
        try:
            msg = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue

        cls = msg.get('class')
        # Take the best value seen in the window, so one stale message can't
        # make a working receiver look dead.
        if cls == 'TPV':
            fix.mode = max(fix.mode, int(msg.get('mode', 0)))
        elif cls == 'SKY':
            if 'uSat' in msg:
                fix.satellites_used = max(fix.satellites_used, int(msg['uSat']))
            if 'nSat' in msg:
                fix.satellites_visible = max(fix.satellites_visible, int(msg['nSat']))

    return fix


@dataclass
class RecoveryConfig:
    """Configuration for recovery behavior."""
    timeout_seconds: int = 60       # Seconds before attempting recovery
    cooldown_seconds: int = 300     # Seconds between recovery attempts
    fix_check_seconds: int = 60     # Seconds between gpsd fix checks
    enabled: bool = True
    log_ttl_seconds: int = 600      # Hide recovery-log entries older than this


class RecoveryManager:
    """Manages automatic PPS recovery attempts."""

    def __init__(self, config: RecoveryConfig = None):
        self.config = config or RecoveryConfig()
        self.lock_lost_time: datetime = None
        self.last_recovery_attempt: datetime = None
        # Each entry is (timestamp, message) so stale logs can be aged out of the display.
        self.logs: List[Tuple[datetime, str]] = []
        self.is_recovering: bool = False
        self.reception_fault: str = None    # Set while recovery is suppressed
        self._fix_check_time: datetime = None
        self._fix_fault: str = None
        self._fault_logged_time: datetime = None

    def _log(self, message: str):
        """Append a timestamped recovery-log entry."""
        self.logs.append((datetime.now(), message))
        if len(self.logs) > 50:
            self.logs = self.logs[-50:]

    def reset(self):
        """Reset recovery state when lock is restored."""
        if self.lock_lost_time is not None:
            self._log(f"Lock restored at {datetime.now().strftime('%H:%M:%S')}")
        self.lock_lost_time = None
        self.is_recovering = False
        self.reception_fault = None
        self._fix_check_time = None
        self._fix_fault = None
        self._fault_logged_time = None

    def on_lock_lost(self):
        """Called when lock is first lost."""
        if self.lock_lost_time is None:
            self.lock_lost_time = datetime.now()
            self.logs = [(self.lock_lost_time, f"Lock lost at {self.lock_lost_time.strftime('%H:%M:%S')}")]

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

        # Last gate: don't restart services for a fault restarting can't fix.
        fault = self._reception_fault()
        if fault is not None:
            self._note_reception_fault(fault)
            self.reception_fault = fault
            return False

        if self.reception_fault is not None:
            self._log("GPS fix restored — recovery re-armed")
            self.reception_fault = None
            self._fault_logged_time = None

        return True

    def _check_reception_fault(self) -> Optional[str]:
        """
        Return a reason string when the fault is reception rather than services.

        A receiver that is talking to gpsd but has no fix has a dead antenna or
        has lost sky view. The timepulse only runs while the receiver is locked,
        so no amount of restarting brings PPS back — and every restart wipes
        chrony's NTP fallback reach and tempcomp state, making things worse.

        Returns None when a restart is still worth trying: gpsd unreachable
        (it may itself be wedged) or a receiver that has a fix but no pulses.
        """
        fix = query_gps_fix()

        if not fix.receiver_talking or fix.has_fix:
            return None

        return (f"GPS has no fix ({fix.satellites_used}/{fix.satellites_visible} "
                f"sats used) — check antenna; skipping service restart")

    def _reception_fault(self) -> Optional[str]:
        """Cached _check_reception_fault, so gpsd is polled at most once per interval."""
        now = datetime.now()
        if (self._fix_check_time is None
                or (now - self._fix_check_time).total_seconds() >= self.config.fix_check_seconds):
            self._fix_check_time = now
            self._fix_fault = self._check_reception_fault()
        return self._fix_fault

    def _note_reception_fault(self, fault: str):
        """
        Log the suppression reason, refreshing it before it ages out.

        get_recent_logs() drops entries older than log_ttl_seconds, so logging
        this once would leave the display with no explanation for why recovery
        sits idle. Re-logging one entry per TTL keeps exactly one fresh line
        visible without filling the panel with repeats.
        """
        now = datetime.now()
        if (self._fault_logged_time is None
                or (now - self._fault_logged_time).total_seconds() >= self.config.log_ttl_seconds):
            self._log(fault)
            self._fault_logged_time = now

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
            for msg in logs:
                self._log(msg)
            return success, logs
        except Exception as e:
            logs.append(f"Recovery error: {str(e)}")
            for msg in logs:
                self._log(msg)
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
        """Get recent log messages, dropping entries older than the TTL.

        Aging out stale entries keeps the display from showing old
        "Lock lost / restored" events long after the system has recovered.
        """
        if not self.logs:
            return []
        now = datetime.now()
        ttl = self.config.log_ttl_seconds
        fresh = [msg for ts, msg in self.logs
                 if (now - ts).total_seconds() <= ttl]
        return fresh[-count:]
