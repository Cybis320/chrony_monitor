#!/bin/bash
#
# Initialize PPS on serial port DCD pin
# This script finds the correct serial port and creates /dev/pps0
#

set -e

# Find real serial ports (skip ports that aren't actual serial lines)
PORTS=""
for PORT in /dev/ttyS0 /dev/ttyS1 /dev/ttyS2 /dev/ttyS3 /dev/ttyS4; do
    [ -c "$PORT" ] && PORTS="$PORTS $PORT"
done

if [ -z "$PORTS" ]; then
    echo "No serial ports found"
    exit 1
fi

# Try each serial port — attach PPS line discipline and check for a device
for PORT in $PORTS; do
    echo "Testing $PORT for PPS..."

    # Set serial port for PPS (line discipline 18)
    # This creates a /dev/ppsN device from the DCD pin
    ldattach 18 "$PORT" 2>/dev/null &
    LDATTACH_PID=$!
    sleep 1

    # Find the PPS device created by ldattach (name contains "serial")
    PPS_DEV=""
    for pps in /sys/class/pps/pps*/name; do
        [ -f "$pps" ] || continue
        grep -q "serial" "$pps" 2>/dev/null || continue
        PPS_DEV="/dev/$(basename "$(dirname "$pps")")"
        break
    done

    if [ -n "$PPS_DEV" ] && [ -e "$PPS_DEV" ]; then
        echo "PPS device $PPS_DEV created on $PORT (DCD pin)"

        # Test if pulses are present (GPS may not have fix yet, so don't fail)
        if timeout 3 ppstest "$PPS_DEV" 2>&1 | grep -q "source 0"; then
            echo "PPS pulses confirmed on $PPS_DEV"
        else
            echo "No pulses yet on $PPS_DEV — GPS may still be acquiring fix"
        fi

        # Create stable symlink for chrony
        ln -sf "$PPS_DEV" /dev/serial-pps

        echo "$PORT" > /var/run/pps-serial-port
        echo "$LDATTACH_PID" > /var/run/ldattach.pid
        echo "$PPS_DEV" > /var/run/pps-device
        exit 0
    else
        echo "No PPS device created on $PORT"
        kill $LDATTACH_PID 2>/dev/null || true
    fi
done

echo "No PPS-capable serial port found"
exit 1
