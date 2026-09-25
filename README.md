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
| Green  | GPS PPS locked    | GPPS is the selected source         |
| Blue   | NTP synced        | NTP source selected, offset < 50ms  |
| Yellow | Degraded/Warning  | Recovering, high offset, PPS issue  |
| Red    | Error             | No sync, daemon down                |

## Installation

### One-line install (recommended)

```bash
curl -fsSL https://raw.githubusercontent.com/Cybis320/chrony_monitor/main/install.sh | bash
```

Run it from your normal user account. It re-runs itself under `sudo`, so you
get a single password prompt for the whole install and the monitor is set up
for the right login user. `| sudo bash` works too, and so does the older
`.../main/install | sudo bash` one-liner.

The install creates two checkouts, on purpose:

| Checkout | Owner | Role |
|---|---|---|
| `/opt/chrony_monitor` | root | The daily root updater pulls it from GitHub and installs the root-side pieces from it (systemd units, sudoers, tempcomp helper). Root never runs code from a directory a user can write to. |
| `~/source/CC_Utils/chrony_monitor` | you | The desktop launcher and autostart run the monitor from here. Every update moves it to the same commit as `/opt`, and the updater runs git there as you, not as root. |

On installs that predate the second checkout, the daily updater creates it and
repoints the launchers there, once.

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
- NTP server for the LAN: `/etc/chrony/conf.d/ntp-server.conf` allows clients
  from private ranges (RFC 1918, IPv6 ULA/link-local), and UDP 123 in ufw if it
  is active. It lives outside `chrony.conf`, so re-provisioning keeps it, and the
  daily updater re-asserts it. Check with `sudo chronyc serverstats`.

`chrony.conf` is the same template on every station; re-provisioning rewrites
it, keeping only the fitted `tempcomp` line. Put site time servers (a LAN
stratum 1, an institutional server behind a firewall) in
`/etc/chrony/sources.d/<name>.sources`, which is never touched, e.g.
`server 192.168.50.127 iburst prefer minpoll 4 maxpoll 6`.

### Stable temperature sensor

`/sys/class/thermal/thermal_zoneN` indices are **not stable** — a kernel update
can renumber them, so a raw zone path baked into `chrony.conf` silently starts
reading the wrong sensor after a reboot. To avoid this, `chrony-tempcomp-sensor.service`
runs at boot (before chrony), detects the best sensor *by type* (chipset/PCH on
Intel, the SoC sensor on Pi), and points a fixed symlink
`/run/chrony-monitor/tempcomp-sensor` at the correct zone. `chrony.conf`, the
monitor and the auto-recalibrator all reference that symlink instead of a zone
number. Existing installs are migrated automatically on update
(`migrate-tempcomp-sensor.sh`).

Because thermal drivers are loaded by udev, the right zone may not exist the
instant the service runs, so it waits up to 15s for a real board/SoC sensor
before settling for `acpitz`. If no usable sensor is found it leaves the symlink
alone and fails, and the migration refuses to repoint `chrony.conf` at a link
that isn't published — a stale sensor beats a missing one.

The link points at the zone's **hwmon mirror**
(`thermal_zoneN/hwmonM/temp1_input`) rather than its `temp` node. Both report
the same value, but chronyd is confined by AppArmor on Debian/Ubuntu and the
stock profile only allows the hwmon form. Zones without a mirror (e.g.
`x86_pkg_temp`) fall back to `temp`, and `setup-chronyd-apparmor.sh` (run by
`install.sh` and `update.sh`) adds a managed block to
`/etc/apparmor.d/local/usr.sbin.chronyd` so chronyd can read whichever node was
picked. A sensor chronyd cannot read is a silent failure — it just logs
`Could not read temperature` and applies no compensation — so the monitor
watches the journal and shows `chronyd can't read sensor!` in the
TempComp line when that happens.

To see what it picked, or to check a machine by hand:

```bash
readlink /run/chrony-monitor/tempcomp-sensor
sudo PYTHONPATH=/usr/local/lib/chrony-monitor \
    python3 -m chrony_monitor.tempcomp --verbose
journalctl -u chrony --since -10min | grep "Could not read temperature"   # must be empty
sudo scripts/setup-chronyd-apparmor.sh --dry-run
```

The resolver imports a root-owned copy of the package at
`/usr/local/lib/chrony-monitor`, refreshed by `install.sh` and `update.sh`, so a
root service at boot never executes code from the user-writable git checkout.

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

The monitor expects GPS PPS only when a receiver is actually present **and**
`chrony.conf` declares a `refclock`:

1. **Receiver present** → GPS PPS mode expected
   - PPS working → Green display
   - PPS not working → Yellow "PPS ISSUE" warning with auto-recovery
2. **No receiver** → NTP-only mode
   - Blue display is normal operation
   - If the machine is set up for GPS (gpsd `DEVICES` or a `refclock`) but the
     receiver is missing, a dim `GPS` line says so — without auto-recovery,
     since restarting services cannot plug a receiver back in.

"Receiver present" is keyed on gpsd's configuration, not on whatever serial
gadget happens to be plugged in (a USB-serial adapter or an LED flasher is not a
GPS, and used to put the monitor into a restart loop):

- `DEVICES="..."` set in `/etc/default/gpsd` → present iff one of those device
  nodes exists right now.
- `DEVICES` empty (gpsd hotplug mode) → present iff udev has published a
  `/dev/gpsN` symlink for a recognized receiver.

`python -m chrony_monitor --status` prints the decision and the reason.

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
