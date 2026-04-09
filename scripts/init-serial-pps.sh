#!/bin/bash
#
# Initialize PPS on serial port DCD pin
# Scans serial ports with real UARTs, prefers ports with active PPS pulses.
#

set -e

# Clean up any leftover ldattach from previous runs
pkill ldattach 2>/dev/null || true
sleep 0.5

# Find serial ports that have real UART hardware (skip ghost ports)
PORTS=""
for PORT in /dev/ttyS*; do
    [ -c "$PORT" ] || continue
    if setserial -g "$PORT" 2>/dev/null | grep -qv "UART: unknown"; then
        PORTS="$PORTS $PORT"
    fi
done

# Also include USB serial adapters (FTDI, etc. support DCD/PPS)
for PORT in /dev/ttyUSB*; do
    [ -c "$PORT" ] && PORTS="$PORTS $PORT"
done

if [ -z "$PORTS" ]; then
    echo "No serial ports found"
    exit 1
fi

echo "Scanning ports:$PORTS"

# Test each port one at a time
BEST_PORT=""
BEST_PPS=""
FALLBACK_PORT=""
FALLBACK_PPS=""

for PORT in $PORTS; do
    echo "Testing $PORT for PPS..."

    # Attach PPS line discipline — creates a /dev/ppsN from the DCD pin
    ldattach 18 "$PORT" 2>/dev/null &
    sleep 1

    # Find the PPS device for this port
    PPS_DEV=""
    for pps in /sys/class/pps/pps*/path; do
        [ -f "$pps" ] || continue
        if [ "$(cat "$pps" 2>/dev/null)" = "$PORT" ]; then
            PPS_DEV="/dev/$(basename "$(dirname "$pps")")"
            break
        fi
    done

    if [ -z "$PPS_DEV" ] || [ ! -e "$PPS_DEV" ]; then
        echo "  No PPS device created"
        pkill -f "ldattach 18 $PORT" 2>/dev/null || true
        sleep 0.5
        continue
    fi

    echo "  PPS device $PPS_DEV created"

    # Check for actual pulses
    if timeout 3 ppstest "$PPS_DEV" 2>&1 | grep -q "assert"; then
        echo "  PPS pulses confirmed — using this port"
        BEST_PORT="$PORT"
        BEST_PPS="$PPS_DEV"
        break
    fi

    echo "  No pulses yet"

    # Keep first PPS-capable port as fallback, clean up the rest
    if [ -z "$FALLBACK_PORT" ]; then
        FALLBACK_PORT="$PORT"
        FALLBACK_PPS="$PPS_DEV"
    else
        pkill -f "ldattach 18 $PORT" 2>/dev/null || true
        sleep 0.5
    fi
done

# Use best match, or fall back to first PPS-capable port
if [ -z "$BEST_PORT" ] && [ -n "$FALLBACK_PORT" ]; then
    echo "No pulses detected — falling back to $FALLBACK_PORT"
    BEST_PORT="$FALLBACK_PORT"
    BEST_PPS="$FALLBACK_PPS"
fi

if [ -z "$BEST_PORT" ]; then
    echo "No PPS-capable serial port found"
    exit 1
fi

# Kill all ldattach except the one for our chosen port
for PORT in $PORTS; do
    [ "$PORT" = "$BEST_PORT" ] && continue
    pkill -f "ldattach 18 $PORT" 2>/dev/null || true
done

echo "Using PPS device $BEST_PPS on $BEST_PORT (DCD pin)"

# Create stable symlink for chrony
ln -sf "$BEST_PPS" /dev/serial-pps

BEST_PID=$(pgrep -f "ldattach 18 $BEST_PORT" 2>/dev/null || echo "unknown")
echo "$BEST_PORT" > /var/run/pps-serial-port
echo "$BEST_PID" > /var/run/ldattach.pid
echo "$BEST_PPS" > /var/run/pps-device
exit 0
