#!/bin/bash
#
# Configure u-blox NEO-7M GPS for optimal time precision
# Sets stationary mode, antenna cable delay, and saves to battery-backed RAM.
#
# Must be run as root (or via sudo) since it restarts gpsd.
#

set -e

GPS_DEV="/dev/ttyACM0"
GPS_BAUD=9600

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

info()  { echo -e "${GREEN}[INFO]${NC} $1"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $1"; }
error() { echo -e "${RED}[ERROR]${NC} $1"; }

if [ "$EUID" -ne 0 ]; then
    error "This script must be run with sudo."
    exit 1
fi

if [ ! -c "$GPS_DEV" ]; then
    error "GPS device $GPS_DEV not found"
    exit 1
fi

# Stop gpsd so we can talk to the GPS directly (gpsd uses -b read-only mode)
info "Stopping gpsd..."
systemctl stop gpsd.service 2>/dev/null || true
sleep 1

info "Configuring $GPS_DEV at ${GPS_BAUD} baud..."

# Send UBX commands via Python (raw binary protocol)
python3 << 'PYEOF'
import serial
import struct
import time
import sys

def ubx_checksum(cls, mid, payload):
    """Fletcher-8 checksum over class, id, length, and payload."""
    data = bytes([cls, mid]) + struct.pack('<H', len(payload)) + payload
    ck_a = ck_b = 0
    for b in data:
        ck_a = (ck_a + b) & 0xFF
        ck_b = (ck_b + ck_a) & 0xFF
    return bytes([ck_a, ck_b])

def ubx_message(cls, mid, payload):
    """Build a complete UBX message."""
    return b'\xB5\x62' + bytes([cls, mid]) + struct.pack('<H', len(payload)) + payload + ubx_checksum(cls, mid, payload)

def read_until_ubx(ser, target_cls, target_mid, timeout=3):
    """Read serial data until we find a specific UBX message, ignoring NMEA."""
    deadline = time.time() + timeout
    buf = b''
    while time.time() < deadline:
        chunk = ser.read(ser.in_waiting or 1)
        if chunk:
            buf += chunk
        marker = bytes([0xB5, 0x62, target_cls, target_mid])
        idx = buf.find(marker)
        if idx >= 0:
            while len(buf) < idx + 6 and time.time() < deadline:
                buf += ser.read(ser.in_waiting or 1)
            if len(buf) >= idx + 6:
                payload_len = struct.unpack_from('<H', buf, idx + 4)[0]
                total = idx + 6 + payload_len + 2
                while len(buf) < total and time.time() < deadline:
                    buf += ser.read(ser.in_waiting or 1)
                if len(buf) >= total:
                    return buf[idx:total]
        time.sleep(0.05)
    return None

def send_ubx(ser, cls, mid, payload, description):
    """Send UBX command and wait for ACK/NACK."""
    ser.reset_input_buffer()
    ser.write(ubx_message(cls, mid, payload))
    ser.flush()

    # Read all responses for a few seconds, looking for ACK or NACK
    deadline = time.time() + 3
    buf = b''
    while time.time() < deadline:
        chunk = ser.read(ser.in_waiting or 1)
        if chunk:
            buf += chunk

        # ACK-ACK: class=0x05 id=0x01, payload=[acked_cls, acked_id]
        ack_marker = bytes([0xB5, 0x62, 0x05, 0x01, 0x02, 0x00, cls, mid])
        if ack_marker in buf:
            print(f"  OK: {description}")
            return True

        # ACK-NACK: class=0x05 id=0x00, payload=[nacked_cls, nacked_id]
        nack_marker = bytes([0xB5, 0x62, 0x05, 0x00, 0x02, 0x00, cls, mid])
        if nack_marker in buf:
            print(f"  NACK: {description}")
            return False

        time.sleep(0.05)

    print(f"  TIMEOUT: {description} (no ACK/NACK received)")
    return False

def poll_ubx(ser, cls, mid, description, timeout=3):
    """Poll a UBX message and return the payload."""
    ser.reset_input_buffer()
    ser.write(ubx_message(cls, mid, b''))
    ser.flush()

    resp = read_until_ubx(ser, cls, mid, timeout=timeout)
    if resp and len(resp) >= 8:
        payload_len = struct.unpack_from('<H', resp, 4)[0]
        return resp[6:6+payload_len]
    print(f"  Could not read {description}")
    return None

MODELS = {0: 'Portable', 2: 'Stationary', 3: 'Pedestrian',
          4: 'Automotive', 5: 'Sea', 6: 'Airborne <1g',
          7: 'Airborne <2g', 8: 'Airborne <4g'}

ok = True

try:
    ser = serial.Serial('/dev/ttyACM0', 9600, timeout=2)
    time.sleep(0.5)
    ser.reset_input_buffer()

    # --- 1. Read current NAV5 config ---
    print("Reading current navigation model...")
    nav5_data = poll_ubx(ser, 0x06, 0x24, "CFG-NAV5")
    if nav5_data and len(nav5_data) >= 36:
        current_model = nav5_data[2]
        print(f"  Current dynamic model: {MODELS.get(current_model, current_model)}")

    # --- 2. Set dynamic model to Stationary ---
    print("Setting dynamic model to Stationary...")
    # Read-modify-write: poll current config, change only dynModel
    if nav5_data and len(nav5_data) >= 36:
        nav5_payload = bytearray(nav5_data[:36])
        struct.pack_into('<H', nav5_payload, 0, 0x0001)  # mask: apply dynModel
        nav5_payload[2] = 2  # dynModel: Stationary
    else:
        nav5_payload = bytearray(36)
        struct.pack_into('<H', nav5_payload, 0, 0x0001)
        nav5_payload[2] = 2
    if not send_ubx(ser, 0x06, 0x24, bytes(nav5_payload), "CFG-NAV5 → Stationary"):
        ok = False

    # --- 3. Set antenna cable delay (3m RG-174, velocity factor 0.66 ≈ 15ns) ---
    # CFG-TP5 offsets: 4=antCableDelay(I2), 6=rfGroupDelay(I2), 8=freqPeriod(U4),
    #   12=freqPeriodLock(U4), 16=pulseLenRatio(U4), 20=pulseLenRatioLock(U4),
    #   24=userConfigDelay(I4), 28=flags(X4)
    print("Reading current time pulse (TP5) configuration...")
    tp5_data = poll_ubx(ser, 0x06, 0x31, "CFG-TP5")
    if tp5_data and len(tp5_data) >= 32:
        tp5_payload = bytearray(tp5_data[:32])
        ant_cable_delay = struct.unpack_from('<h', tp5_payload, 4)[0]
        print(f"  Current antenna cable delay: {ant_cable_delay}ns")

        if ant_cable_delay != 15:
            print(f"  Setting antenna cable delay: {ant_cable_delay}ns → 15ns...")
            struct.pack_into('<h', tp5_payload, 4, 15)
            if not send_ubx(ser, 0x06, 0x31, bytes(tp5_payload), "CFG-TP5 → antCableDelay=15ns"):
                ok = False
        else:
            print("  Already set to 15ns")

    # --- 4. Save configuration to BBR ---
    print("Saving configuration to battery-backed RAM...")
    cfg_payload = struct.pack('<IIIB',
        0x00000000,  # clearMask
        0x0000FFFF,  # saveMask (all sections)
        0x00000000,  # loadMask
        0x01)        # deviceMask: BBR only
    if not send_ubx(ser, 0x06, 0x09, cfg_payload, "CFG-CFG → Save to BBR"):
        ok = False

    # --- 5. Verify ---
    print("Verifying configuration...")
    time.sleep(0.5)

    nav5_data = poll_ubx(ser, 0x06, 0x24, "CFG-NAV5")
    if nav5_data and len(nav5_data) >= 36:
        m = nav5_data[2]
        if m == 2:
            print(f"  OK: dynamic model = Stationary")
        else:
            print(f"  FAIL: dynamic model = {MODELS.get(m, m)} (expected Stationary)")
            ok = False

    tp5_data = poll_ubx(ser, 0x06, 0x31, "CFG-TP5")
    if tp5_data and len(tp5_data) >= 32:
        d = struct.unpack_from('<h', tp5_data, 4)[0]
        if d == 15:
            print(f"  OK: antCableDelay = {d}ns")
        else:
            print(f"  FAIL: antCableDelay = {d}ns (expected 15)")
            ok = False

    ser.close()

    if ok:
        print("\nAll settings applied and verified.")
    else:
        print("\nWARNING: Some settings may not have been applied correctly.")
        sys.exit(1)

except serial.SerialException as e:
    print(f"Serial error: {e}", file=sys.stderr)
    sys.exit(1)
PYEOF

# Restart gpsd and chrony
info "Restarting gpsd..."
systemctl start gpsd.service
sleep 2

info "Restarting chrony..."
systemctl restart chrony.service

info "Done. GPS configured: Stationary mode, 15ns antenna cable delay, saved to BBR."
