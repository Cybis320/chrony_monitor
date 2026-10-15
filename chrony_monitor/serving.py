"""Is chronyd serving time to the network, and to how many clients?"""

import glob
import os
import re
import subprocess
import time
from dataclasses import dataclass
from typing import Optional

from .status import CHRONY_CONF_PATHS

# chronyd answers these only over its root-only command socket, so they run
# through sudo -n. The sudoers rules (install.sh, setup-serving-sudo.sh)
# authorize exactly these argument lists; anything else would prompt.
CLIENTS_CMD = ["sudo", "-n", "/usr/bin/chronyc", "-n", "clients"]
SERVERSTATS_CMD = ["sudo", "-n", "/usr/bin/chronyc", "serverstats"]

ACTIVE_CLIENT_SECONDS = 900     # a client counts as active if heard within this
POLL_SECONDS = 60               # how often to ask chronyd

_UNITS = {'m': 60, 'h': 3600, 'd': 86400, 'y': 365 * 86400}


def _conf_files(path: str) -> list:
    """path plus every file it pulls in through confdir/include, in parse order."""
    files = [path]
    try:
        with open(path) as f:
            lines = f.readlines()
    except OSError:
        return []
    for line in lines:
        words = line.split()
        if len(words) < 2:
            continue
        if words[0] == 'confdir':
            # Later dirs only add names the earlier ones lack; the union is
            # all that matters for "is there an allow anywhere".
            for d in words[1:]:
                for f in sorted(glob.glob(os.path.join(d, '*.conf'))):
                    files.extend(_conf_files(f))
        elif words[0] == 'include':
            for f in sorted(glob.glob(words[1])):
                files.extend(_conf_files(f))
    return files


def ntp_allowed(conf_paths: list = None) -> Optional[bool]:
    """Does chrony's config let NTP clients in? None if no config could be read.

    chronyd answers no client until an allow directive opens it up, and
    `port 0` closes the server port outright.
    """
    for path in conf_paths or CHRONY_CONF_PATHS:
        files = _conf_files(path)
        if not files:
            continue
        allowed = False
        for conf in files:
            try:
                with open(conf) as f:
                    for line in f:
                        words = line.split()
                        if not words:
                            continue
                        if words[0] == 'allow':
                            allowed = True
                        elif words[0] == 'port' and words[1:2] == ['0']:
                            return False
            except OSError:
                continue
        return allowed
    return None


def parse_last_seconds(val: str) -> Optional[int]:
    """chronyc's interval column: '45', '14m', '3h', '2d', '1y'; '-' is never."""
    m = re.fullmatch(r'(\d+)([mhdy]?)', val)
    if not m:
        return None
    return int(m.group(1)) * _UNITS.get(m.group(2), 1)


def count_active_clients(text: str, within_s: int = ACTIVE_CLIENT_SECONDS) -> int:
    """NTP clients in `chronyc -n clients` output heard from within within_s.

    Columns: Hostname NTP Drop Int IntL Last Cmd Drop Int Last. Hosts that only
    sent commands (chronyc on localhost) have an NTP count of 0.
    """
    count = 0
    for line in text.splitlines():
        cols = line.split()
        if len(cols) < 6 or not cols[1].isdigit() or int(cols[1]) == 0:
            continue
        last = parse_last_seconds(cols[5])
        if last is not None and last <= within_s:
            count += 1
    return count


def parse_ntp_received(text: str) -> Optional[int]:
    """'NTP packets received : N' from `chronyc serverstats`."""
    m = re.search(r'^NTP packets received\s*:\s*(\d+)', text, re.MULTILINE)
    return int(m.group(1)) if m else None


def _run(cmd: list) -> Optional[str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout if r.returncode == 0 else None


@dataclass
class ServingStatus:
    allowed: Optional[bool]               # config lets clients in (None = unknown)
    clients: Optional[int] = None         # active clients; None = couldn't ask
    requests_per_hour: Optional[float] = None


class ServingTracker:
    """Polls chronyd every POLL_SECONDS and turns its counter into a rate."""

    def __init__(self, run=_run, clock=time.monotonic):
        self._run = run
        self._clock = clock
        self._last_poll = None
        self._prev = None       # (time, NTP packets received)
        self._rate = None
        self._status = None

    def get(self) -> ServingStatus:
        now = self._clock()
        if self._last_poll is not None and now - self._last_poll < POLL_SECONDS:
            return self._status
        self._last_poll = now

        allowed = ntp_allowed()
        if not allowed:
            self._prev = self._rate = None
            self._status = ServingStatus(allowed=allowed)
            return self._status

        out = self._run(CLIENTS_CMD)
        clients = count_active_clients(out) if out is not None else None

        out = self._run(SERVERSTATS_CMD)
        received = parse_ntp_received(out) if out is not None else None
        if received is None:
            self._prev = self._rate = None
        else:
            # A smaller count means chronyd restarted; start the rate over.
            if self._prev is not None and received >= self._prev[1] and now > self._prev[0]:
                self._rate = (received - self._prev[1]) * 3600 / (now - self._prev[0])
            elif self._prev is not None:
                self._rate = None
            self._prev = (now, received)

        self._status = ServingStatus(allowed=True, clients=clients,
                                     requests_per_hour=self._rate)
        return self._status
