#!/bin/bash
#
# Initialize PPS on serial port DCD pin
# This script finds the correct serial port and creates /dev/pps0
#

set -e

# Try common serial ports for PPS signal
for PORT in /dev/ttyS0 /dev/ttyS1 /dev/ttyS2 /dev/ttyS3; do
    if [ ! -c "$PORT" ]; then
        continue
    fi

    echo "Testing $PORT for PPS..."

    # Set serial port for PPS (line discipline 18)
    # This creates /dev/pps0 from the DCD pin
    ldattach 18 "$PORT" &
    LDATTACH_PID=$!
    sleep 1

    # Check if PPS device was created
    if [ -e /dev/pps0 ]; then
        # Test if PPS is actually receiving pulses
        if timeout 2 ppstest /dev/pps0 2>&1 | grep -q "source 0"; then
            echo "PPS working on $PORT (DCD pin)"
            echo "$PORT" > /var/run/pps-serial-port
            echo "$LDATTACH_PID" > /var/run/ldattach.pid
            exit 0
        else
            echo "PPS device created but no pulses detected on $PORT"
            kill $LDATTACH_PID 2>/dev/null || true
        fi
    else
        echo "Failed to create PPS device on $PORT"
        kill $LDATTACH_PID 2>/dev/null || true
    fi
done

echo "No working PPS found on serial ports"
exit 1
