#!/usr/bin/env python3
"""Dump all configuration from a u-blox NEO-7M GPS receiver."""

import serial
import struct
import time
import sys

def ubx_checksum(cls, mid, payload):
    data = bytes([cls, mid]) + struct.pack('<H', len(payload)) + payload
    ck_a = ck_b = 0
    for b in data:
        ck_a = (ck_a + b) & 0xFF
        ck_b = (ck_b + ck_a) & 0xFF
    return bytes([ck_a, ck_b])

def ubx_message(cls, mid, payload):
    return b'\xB5\x62' + bytes([cls, mid]) + struct.pack('<H', len(payload)) + payload + ubx_checksum(cls, mid, payload)

def read_until_ubx(ser, target_cls, target_mid, timeout=3):
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
                    return buf[idx+6:idx+6+payload_len]
        time.sleep(0.05)
    return None

def poll(ser, cls, mid, name):
    ser.reset_input_buffer()
    ser.write(ubx_message(cls, mid, b''))
    ser.flush()
    data = read_until_ubx(ser, cls, mid)
    if not data:
        print(f"  [{name}] No response\n")
    return data

def main():
    dev = sys.argv[1] if len(sys.argv) > 1 else '/dev/ttyACM0'
    print(f"Connecting to {dev}...\n")
    ser = serial.Serial(dev, 9600, timeout=2)
    time.sleep(0.5)
    ser.reset_input_buffer()

    # --- MON-VER: firmware version ---
    print("=" * 60)
    print("MON-VER — Firmware Version")
    print("=" * 60)
    data = poll(ser, 0x0A, 0x04, "MON-VER")
    if data:
        sw = data[0:30].split(b'\x00')[0].decode('ascii', errors='replace')
        hw = data[30:40].split(b'\x00')[0].decode('ascii', errors='replace')
        print(f"  Software: {sw}")
        print(f"  Hardware: {hw}")
        # Extensions
        for i in range(40, len(data), 30):
            ext = data[i:i+30].split(b'\x00')[0].decode('ascii', errors='replace')
            if ext:
                print(f"  Extension: {ext}")
    print()

    # --- CFG-NAV5: navigation engine ---
    print("=" * 60)
    print("CFG-NAV5 — Navigation Engine Settings")
    print("=" * 60)
    data = poll(ser, 0x06, 0x24, "CFG-NAV5")
    if data and len(data) >= 36:
        mask = struct.unpack_from('<H', data, 0)[0]
        dyn_model = data[2]
        fix_mode = data[3]
        fixed_alt = struct.unpack_from('<i', data, 4)[0] / 100.0
        fixed_alt_var = struct.unpack_from('<I', data, 8)[0] / 10000.0
        min_elev = struct.unpack_from('<b', data, 12)[0]
        pdop = struct.unpack_from('<H', data, 14)[0] / 10.0
        tdop = struct.unpack_from('<H', data, 16)[0] / 10.0
        p_acc = struct.unpack_from('<H', data, 18)[0]
        t_acc = struct.unpack_from('<H', data, 20)[0]
        static_hold = data[22]
        dgps_timeout = data[23]
        cno_thresh_svs = data[24]
        cno_thresh = data[25]
        static_hold_dist = struct.unpack_from('<H', data, 28)[0]
        utc_standard = data[30]

        models = {0: 'Portable', 2: 'Stationary', 3: 'Pedestrian',
                  4: 'Automotive', 5: 'Sea', 6: 'Airborne <1g',
                  7: 'Airborne <2g', 8: 'Airborne <4g'}
        fix_modes = {1: '2D only', 2: '3D only', 3: 'Auto 2D/3D'}
        utc_stds = {0: 'Auto', 1: 'USNO (GPS)', 2: 'GLONASS', 3: 'BeiDou'}

        print(f"  Dynamic model:    {models.get(dyn_model, dyn_model)}")
        print(f"  Fix mode:         {fix_modes.get(fix_mode, fix_mode)}")
        print(f"  Fixed altitude:   {fixed_alt}m (var: {fixed_alt_var}m²)")
        print(f"  Min elevation:    {min_elev}°")
        print(f"  PDOP mask:        {pdop}")
        print(f"  TDOP mask:        {tdop}")
        print(f"  Position acc:     {p_acc}m")
        print(f"  Time acc:         {t_acc}m")
        print(f"  Static hold thr:  {static_hold} cm/s")
        print(f"  Static hold dist: {static_hold_dist}m")
        print(f"  DGPS timeout:     {dgps_timeout}s")
        print(f"  C/NO threshold:   {cno_thresh} dBHz (min {cno_thresh_svs} SVs)")
        print(f"  UTC standard:     {utc_stds.get(utc_standard, utc_standard)}")
    print()

    # --- CFG-NAVX5: expert navigation settings ---
    print("=" * 60)
    print("CFG-NAVX5 — Expert Navigation Settings")
    print("=" * 60)
    data = poll(ser, 0x06, 0x23, "CFG-NAVX5")
    if data and len(data) >= 40:
        min_svs = data[10]
        max_svs = data[11]
        min_cno = data[12]
        ini_fix_3d = data[14]
        ack_aiding = data[17]
        wkn_rollover = struct.unpack_from('<H', data, 18)[0]
        use_ppp = data[26]
        aop_cfg = data[27]
        print(f"  Min SVs:          {min_svs}")
        print(f"  Max SVs:          {max_svs}")
        print(f"  Min C/NO:         {min_cno} dBHz")
        print(f"  Initial 3D fix:   {'yes' if ini_fix_3d else 'no'}")
        print(f"  Ack aiding:       {'yes' if ack_aiding else 'no'}")
        print(f"  WKN rollover:     {wkn_rollover}")
        print(f"  Use PPP:          {'yes' if use_ppp else 'no'}")
        print(f"  AssistNow Auto:   {'enabled' if aop_cfg else 'disabled'}")
    print()

    # --- CFG-TP5: time pulse ---
    print("=" * 60)
    print("CFG-TP5 — Time Pulse Configuration")
    print("=" * 60)
    data = poll(ser, 0x06, 0x31, "CFG-TP5")
    if data and len(data) >= 32:
        ant_delay = struct.unpack_from('<h', data, 4)[0]
        rf_delay = struct.unpack_from('<h', data, 6)[0]
        freq = struct.unpack_from('<I', data, 8)[0]
        freq_lock = struct.unpack_from('<I', data, 12)[0]
        pulse = struct.unpack_from('<I', data, 16)[0]
        pulse_lock = struct.unpack_from('<I', data, 20)[0]
        user_delay = struct.unpack_from('<i', data, 24)[0]
        flags = struct.unpack_from('<I', data, 28)[0]

        is_freq = bool(flags & 0x08)
        is_len = bool(flags & 0x10)
        grid = (flags >> 7) & 0x0F
        grid_names = {0: "UTC", 1: "GPS", 2: "GLONASS", 3: "BeiDou"}

        print(f"  Antenna cable delay: {ant_delay}ns")
        print(f"  RF group delay:      {rf_delay}ns")
        print(f"  User config delay:   {user_delay}ns")
        if is_freq:
            print(f"  Frequency:           {freq}Hz (unlocked), {freq_lock}Hz (locked)")
        else:
            print(f"  Period:              {freq}μs (unlocked), {freq_lock}μs (locked)")
        if is_len:
            print(f"  Pulse length:        {pulse}μs (unlocked), {pulse_lock}μs (locked)")
        else:
            print(f"  Duty cycle:          {pulse}/2^32 (unlocked), {pulse_lock}/2^32 (locked)")
        print(f"  Time grid:           {grid_names.get(grid, grid)}")
        print(f"  Active:              {'yes' if flags & 0x01 else 'no'}")
        print(f"  Lock GNSS freq:      {'yes' if flags & 0x02 else 'no'}")
        print(f"  Locked other set:    {'yes' if flags & 0x04 else 'no'}")
        print(f"  Align to TOW:        {'yes' if flags & 0x20 else 'no'}")
        print(f"  Polarity:            {'rising' if flags & 0x40 else 'falling'} edge")
    print()

    # --- CFG-PRT: port configuration ---
    print("=" * 60)
    print("CFG-PRT — Port Configuration (USB)")
    print("=" * 60)
    # Poll USB port (portID=3)
    data = poll(ser, 0x06, 0x00, "CFG-PRT")
    if data and len(data) >= 20:
        port_id = data[0]
        port_names = {0: 'I2C/DDC', 1: 'UART1', 2: 'UART2', 3: 'USB', 4: 'SPI'}
        in_mask = struct.unpack_from('<H', data, 12)[0]
        out_mask = struct.unpack_from('<H', data, 14)[0]
        protos_in = []
        protos_out = []
        if in_mask & 0x01: protos_in.append("UBX")
        if in_mask & 0x02: protos_in.append("NMEA")
        if in_mask & 0x04: protos_in.append("RTCM")
        if out_mask & 0x01: protos_out.append("UBX")
        if out_mask & 0x02: protos_out.append("NMEA")
        if out_mask & 0x04: protos_out.append("RTCM")
        print(f"  Port:     {port_names.get(port_id, port_id)}")
        print(f"  In proto: {', '.join(protos_in)}")
        print(f"  Out proto:{', '.join(protos_out)}")
    print()

    # --- CFG-RATE: measurement rate ---
    print("=" * 60)
    print("CFG-RATE — Measurement Rate")
    print("=" * 60)
    data = poll(ser, 0x06, 0x08, "CFG-RATE")
    if data and len(data) >= 6:
        meas_rate = struct.unpack_from('<H', data, 0)[0]
        nav_rate = struct.unpack_from('<H', data, 2)[0]
        time_ref = struct.unpack_from('<H', data, 4)[0]
        time_refs = {0: 'UTC', 1: 'GPS'}
        print(f"  Measurement rate: {meas_rate}ms ({1000/meas_rate:.1f}Hz)")
        print(f"  Navigation rate:  every {nav_rate} measurement(s)")
        print(f"  Time reference:   {time_refs.get(time_ref, time_ref)}")
    print()

    # --- CFG-SBAS: SBAS (WAAS/EGNOS) ---
    print("=" * 60)
    print("CFG-SBAS — SBAS Configuration (WAAS/EGNOS)")
    print("=" * 60)
    data = poll(ser, 0x06, 0x16, "CFG-SBAS")
    if data and len(data) >= 8:
        mode = data[0]
        usage = data[1]
        max_sbas = data[2]
        scanmode1 = struct.unpack_from('<I', data, 4)[0]
        enabled = bool(mode & 0x01)
        test_mode = bool(mode & 0x02)
        uses = []
        if usage & 0x01: uses.append("range")
        if usage & 0x02: uses.append("diffCorr")
        if usage & 0x04: uses.append("integrity")
        print(f"  SBAS enabled:   {'yes' if enabled else 'no'}")
        print(f"  Test mode:      {'yes' if test_mode else 'no'}")
        print(f"  Usage:          {', '.join(uses) if uses else 'none'}")
        print(f"  Max SBAS SVs:   {max_sbas}")
        print(f"  Scan mode:      0x{scanmode1:08X}")
    print()

    # --- CFG-GNSS: GNSS system config ---
    print("=" * 60)
    print("CFG-GNSS — GNSS System Configuration")
    print("=" * 60)
    data = poll(ser, 0x06, 0x3E, "CFG-GNSS")
    if data and len(data) >= 4:
        num_trk = data[1]
        num_use = data[2]
        num_blocks = data[3]
        print(f"  Tracking channels: {num_trk}")
        print(f"  Used channels:     {num_use}")
        gnss_names = {0: 'GPS', 1: 'SBAS', 2: 'Galileo', 3: 'BeiDou',
                      5: 'QZSS', 6: 'GLONASS'}
        for i in range(num_blocks):
            off = 4 + i * 8
            if off + 8 > len(data):
                break
            gnss_id = data[off]
            res_trk = data[off+1]
            max_trk = data[off+2]
            flags = struct.unpack_from('<I', data, off+4)[0]
            enabled = bool(flags & 0x01)
            name = gnss_names.get(gnss_id, f'ID{gnss_id}')
            print(f"  {name:10s} enabled={enabled}  resTrk={res_trk}  maxTrk={max_trk}")
    print()

    # --- CFG-ANT: antenna config ---
    print("=" * 60)
    print("CFG-ANT — Antenna Configuration")
    print("=" * 60)
    data = poll(ser, 0x06, 0x13, "CFG-ANT")
    if data and len(data) >= 4:
        flags = struct.unpack_from('<H', data, 0)[0]
        pins = struct.unpack_from('<H', data, 2)[0]
        print(f"  Supply voltage ctrl: {'yes' if flags & 0x01 else 'no'}")
        print(f"  Short detection:     {'yes' if flags & 0x02 else 'no'}")
        print(f"  Open detection:      {'yes' if flags & 0x04 else 'no'}")
        print(f"  Power down on short: {'yes' if flags & 0x08 else 'no'}")
        print(f"  Auto recovery:       {'yes' if flags & 0x10 else 'no'}")
    print()

    # --- CFG-PM2: power management ---
    print("=" * 60)
    print("CFG-PM2 — Power Management")
    print("=" * 60)
    data = poll(ser, 0x06, 0x3B, "CFG-PM2")
    if data and len(data) >= 44:
        flags = struct.unpack_from('<I', data, 4)[0]
        update_period = struct.unpack_from('<I', data, 8)[0]
        search_period = struct.unpack_from('<I', data, 12)[0]
        on_time = struct.unpack_from('<I', data, 20)[0]
        mode = (flags >> 17) & 0x03
        modes = {0: 'ON/OFF', 1: 'Cyclic tracking'}
        print(f"  Mode:           {modes.get(mode, mode)}")
        print(f"  Update period:  {update_period}ms")
        print(f"  Search period:  {search_period}ms")
        print(f"  On time:        {on_time}ms")
    print()

    # --- NAV-STATUS: current receiver status ---
    print("=" * 60)
    print("NAV-STATUS — Current Receiver Status")
    print("=" * 60)
    data = poll(ser, 0x01, 0x03, "NAV-STATUS")
    if data and len(data) >= 16:
        gps_fix = data[4]
        flags = data[5]
        fix_stat = data[6]
        ttff = struct.unpack_from('<I', data, 8)[0]
        msss = struct.unpack_from('<I', data, 12)[0]
        fix_types = {0: 'No fix', 1: 'Dead reckoning', 2: '2D', 3: '3D',
                     4: 'GPS+DR', 5: 'Time only'}
        print(f"  Fix type:       {fix_types.get(gps_fix, gps_fix)}")
        print(f"  Fix valid:      {'yes' if flags & 0x01 else 'no'}")
        print(f"  DGPS used:      {'yes' if flags & 0x02 else 'no'}")
        print(f"  Time of first fix: {ttff}ms")
        print(f"  Uptime:         {msss/1000:.1f}s")
    print()

    ser.close()

if __name__ == '__main__':
    main()
