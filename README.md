# Chrony Monitor

A visual monitor for chrony time synchronization with automatic GPS PPS detection and NTP fallback support.

## Features

- **Auto-detection**: Automatically detects whether GPS PPS hardware is present
- **Accuracy-based colors**: Visual feedback based on sync quality, not just sync method
- **Auto-recovery**: Automatic PPS recovery when GPS sync is lost
- **NTP-only mode**: Clean visual feedback for stations without GPS hardware

## Color Scheme

| Color  | Meaning           | Condition                           |
|--------|-------------------|-------------------------------------|
| Green  | Excellent sync    | GPPS locked OR NTP offset < 1ms     |
| Blue   | Good NTP sync     | NTP synced, offset < 50ms           |
| Yellow | Degraded/Warning  | Recovering, high offset, PPS issue  |
| Red    | Error             | No sync, daemon down                |

## Installation

### One-line install (recommended)

```bash
curl -fsSL https://raw.githubusercontent.com/Cybis320/chrony_monitor/main/install | sudo bash
```

This clones the repo to `/opt/chrony_monitor`, runs the full provisioner, and
enables a daily auto-updater. Run it from your normal user account (via `sudo`)
so the monitor is set up for the right login user. The single `sudo` password
prompt covers the whole install.

The monitor keeps itself current: a daily systemd timer pulls the latest code,
and the running monitor re-execs into it automatically — no manual updates. It
never rewrites `chrony.conf`, so live tempcomp calibration is preserved.

### Manual full install (from a checkout)

```bash
sudo ./scripts/install.sh
```

Both paths install:
- Python package (runs directly from the git checkout)
- Systemd services for PPS initialization
- Udev rules for GPS/PPS devices
- Desktop launcher and autostart (launches on login)
- Passwordless sudo for service recovery and tempcomp recalibration
- Daily auto-updater (`chrony-monitor-update.timer`)
- Stable tempcomp sensor symlink service (`chrony-tempcomp-sensor.service`)

### Stable temperature sensor

`/sys/class/thermal/thermal_zoneN` indices are **not stable** — a kernel update
can renumber them, so a raw zone path baked into `chrony.conf` silently starts
reading the wrong sensor after a reboot. To avoid this, `chrony-tempcomp-sensor.service`
runs at boot (before chrony), detects the best sensor *by type* (chipset/PCH on
Intel, the SoC sensor on Pi), and points a fixed symlink
`/run/chrony-monitor/tempcomp-sensor` at the correct zone. `chrony.conf` and the
auto-recalibrator reference that symlink instead of a zone number. Existing
installs are migrated automatically on update (`migrate-tempcomp-sensor.sh`).

## Usage

### Run the Monitor

```bash
# Auto-detect mode (GPS PPS or NTP)
python -m chrony_monitor

# Force NTP-only mode
python -m chrony_monitor --ntp-only

# Print current status and exit
python -m chrony_monitor --status
```

### Command-Line Options

```
--ntp-only          Force NTP-only mode (ignore GPS/PPS hardware)
--no-recovery       Disable automatic PPS recovery
--interval SECONDS  Polling interval (default: 1.0)
--recovery-timeout  Seconds before recovery attempt (default: 60)
--recovery-cooldown Seconds between recovery attempts (default: 300)
--status            Print status and exit (no UI)
--help              Show all options
```

## Mode Detection

The monitor automatically detects the expected mode:

1. **USB GPS detected** (`/dev/ttyACM*` or similar) → GPS PPS mode expected
   - PPS working → Green display
   - PPS not working → Yellow "PPS ISSUE" warning with auto-recovery
2. **No USB GPS** → NTP-only mode
   - Blue display is normal operation

## GPS PPS Hardware Setup

For GPS PPS to work, you need:

1. **USB GPS receiver** connected (provides NMEA time data)
2. **PPS signal** connected to a serial port's DCD pin (provides precise timing)

The `serial-pps` systemd service handles PPS initialization. It:
- Scans serial ports for PPS signal
- Creates `/dev/pps0` device
- Runs before chrony starts

### Chrony Configuration

Example `/etc/chrony/chrony.conf` for GPS PPS:

```conf
# GPS NMEA data from gpsd (USB GPS)
refclock SHM 0 delay 0.2 offset 0.0 poll 4 refid GPS trust

# PPS signal from serial port DCD pin
refclock PPS /dev/pps0 poll 4 refid GPPS lock GPS trust prefer

# Network fallback
pool ntp.ubuntu.com iburst maxsources 4
```

## Troubleshooting

### Check Current Status

```bash
python -m chrony_monitor --status
chronyc sources -v
```

### Check PPS Device

```bash
ls -la /dev/pps0
ppstest /dev/pps0
```

### Check Services

```bash
systemctl status serial-pps
systemctl status gpsd
systemctl status chrony
```

### View Logs

```bash
journalctl -u serial-pps -u gpsd -u chrony -n 50
```

## System Requirements

- Python 3.8+
- chrony (for `chronyc` command)
- For GPS PPS mode:
  - util-linux (for `ldattach`)
  - pps-tools (for `ppstest`)
  - gpsd (optional, for NMEA time source)

## License

MIT
