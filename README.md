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

### Quick Install (User Mode)

```bash
cd chrony_monitor
pip install -e .
```

### Full Install (Root, with GPS PPS support)

```bash
sudo ./scripts/install.sh
```

This installs:
- Python package
- Systemd services for PPS initialization
- Udev rules for GPS/PPS devices
- Desktop launcher

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
