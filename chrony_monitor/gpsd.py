"""
Read gpsd's JSON stream directly over its socket.

Stands in for `gpspipe -w -n N`. gpspipe ships in Ubuntu's gpsd-clients, which
hard-depends on python3-matplotlib; RMS's vRMS venv sees system packages, so
that apt matplotlib clashes with the one pip put there.
"""

import socket
import time

GPSD_HOST = "127.0.0.1"
GPSD_PORT = 2947


def gpsd_lines(count: int, timeout: float,
               host: str = GPSD_HOST, port: int = GPSD_PORT) -> str:
    """
    Return up to `count` JSON lines from gpsd, newline-joined like gpspipe -w.

    Raises OSError if gpsd can't be reached. Stops early at `timeout` seconds,
    returning whatever arrived.
    """
    deadline = time.monotonic() + timeout
    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.sendall(b'?WATCH={"enable":true,"json":true};\n')
        buf = b""
        lines = []
        while len(lines) < count:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            sock.settimeout(remaining)
            try:
                chunk = sock.recv(4096)
            except socket.timeout:
                break
            if not chunk:
                break
            buf += chunk
            *complete, buf = buf.split(b"\n")
            lines.extend(complete)
    return "\n".join(l.decode("utf-8", "replace") for l in lines[:count])
