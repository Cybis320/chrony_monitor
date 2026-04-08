#!/bin/bash
#
# Chrony Monitor Installation Script
# Installs the chrony-monitor package and sets up GPS PPS support
#

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

backup_config() {
    local file="$1"
    if [ -f "$file" ]; then
        local backup="${file}.backup.$(date +%Y%m%d_%H%M%S)"
        cp "$file" "$backup"
        info "Backed up $file -> $backup"
    fi
}

# Detect if running on a Raspberry Pi
is_raspberry_pi() {
    [ -f /proc/device-tree/model ] && grep -qi "raspberry pi" /proc/device-tree/model
}

# Find the correct PPS device by checking sysfs for GPIO PPS source
find_gpio_pps_device() {
    for pps in /sys/class/pps/pps*/name; do
        [ -f "$pps" ] || continue
        # Match GPIO PPS: "pps@<pin>.*" on RPi 5, "pps-gpio" on older kernels
        if grep -qE "^pps[@-]" "$pps" 2>/dev/null; then
            echo "/dev/$(basename "$(dirname "$pps")")"
            return 0
        fi
    done
    return 1
}

# Require root
check_root() {
    if [ "$EUID" -ne 0 ]; then
        error "This script must be run with sudo."
        exit 1
    fi
}

# Install required system packages (requires root)
install_dependencies() {
    info "Checking system dependencies..."

    PACKAGES_NEEDED=""
    for pkg in gpsd gpsd-clients pps-tools chrony; do
        if ! dpkg -s "$pkg" &>/dev/null; then
            PACKAGES_NEEDED="$PACKAGES_NEEDED $pkg"
        fi
    done

    if [ -n "$PACKAGES_NEEDED" ]; then
        info "Installing missing packages:$PACKAGES_NEEDED"
        apt-get install -y $PACKAGES_NEEDED
    else
        info "All dependencies already installed."
    fi
}

# Install systemd services (requires root)
install_systemd_services() {
    info "Installing systemd services..."

    if is_raspberry_pi; then
        info "Raspberry Pi detected — skipping serial-pps service (using GPIO PPS)"
    else
        # Install serial-pps service for GPS PPS initialization (non-RPi only)
        cp "$PROJECT_DIR/systemd/serial-pps.service" /etc/systemd/system/
        cp "$PROJECT_DIR/scripts/init-serial-pps.sh" /usr/local/bin/
        chmod +x /usr/local/bin/init-serial-pps.sh
    fi

    # Create udev rules for PPS device
    cat > /etc/udev/rules.d/99-gps-pps.rules << 'EOF'
# GPS USB device — set permissions and restart services on hotplug
SUBSYSTEM=="tty", KERNEL=="ttyACM[0-9]*", MODE="0666", GROUP="dialout", SYMLINK+="gps0", TAG+="systemd", ENV{SYSTEMD_WANTS}="gps-hotplug.service"

# PPS device permissions
SUBSYSTEM=="pps", MODE="0666", GROUP="dialout"

# GPIO PPS device — create stable symlink (Raspberry Pi)
# Name varies by kernel: "pps-gpio" on older kernels, "pps@<pin>.*" on RPi 5
SUBSYSTEM=="pps", ATTR{name}=="pps@*", SYMLINK+="gps-pps", MODE="0666", GROUP="dialout"
SUBSYSTEM=="pps", ATTR{name}=="pps-gpio*", SYMLINK+="gps-pps", MODE="0666", GROUP="dialout"
EOF

    if is_raspberry_pi; then
        # GPS hotplug service — restarts gpsd and chrony when GPS is plugged in
        cat > /etc/systemd/system/gps-hotplug.service << 'EOF'
[Unit]
Description=Restart GPS services on hotplug

[Service]
Type=oneshot
ExecStart=/bin/systemctl restart gpsd.service
ExecStartPost=/bin/sleep 2
ExecStartPost=/bin/systemctl restart chrony.service
EOF
    else
        # GPS hotplug service — restarts gpsd and serial-pps when GPS is plugged in
        cat > /etc/systemd/system/gps-hotplug.service << 'EOF'
[Unit]
Description=Restart GPS services on hotplug
After=serial-pps.service

[Service]
Type=oneshot
ExecStart=/bin/systemctl restart serial-pps.service
ExecStart=/bin/systemctl restart gpsd.service
ExecStartPost=/bin/sleep 2
ExecStartPost=/bin/systemctl restart chrony.service
EOF
    fi

    # Reload systemd and udev
    systemctl daemon-reload
    udevadm control --reload-rules

    info "Systemd services installed."
}

# Configure GPSD for USB GPS
configure_gpsd() {
    info "Configuring GPSD for USB GPS..."

    backup_config /etc/default/gpsd

    cat > /etc/default/gpsd << 'EOF'
# GPS device connected via USB
DEVICES="/dev/ttyACM0"

# Enable immediate GPS startup
START_DAEMON="true"
GPSD_OPTIONS="-n -b"

# GPSD socket for chrony SHM
GPSD_SOCKET="/var/run/gpsd.sock"
EOF

    info "GPSD configured."
}

# Write chrony.conf with GPS+PPS refclocks
configure_chrony() {
    info "Writing chrony configuration for GPS+PPS..."

    backup_config /etc/chrony/chrony.conf

    if is_raspberry_pi; then
        # GPIO PPS on Raspberry Pi — use /dev/gps-pps symlink (created by udev rule)
        # No :clear flag needed since GPIO PPS signal is not inverted
        PPS_REFCLOCK="refclock PPS /dev/gps-pps poll 4 refid GPPS lock GPS prefer"
        PPS_COMMENT="# PPS signal from GPIO pin (Raspberry Pi pps-gpio overlay)"
    else
        # Serial PPS on Ubuntu/x86 — use /dev/serial-pps symlink
        # The :clear option uses the DCD deassert edge, which corresponds to the
        # true second boundary when PPS passes through a MAX232 RS-232 driver
        PPS_REFCLOCK="refclock PPS /dev/serial-pps:clear poll 4 refid GPPS lock GPS prefer"
        PPS_COMMENT="# PPS signal from serial port DCD pin"
    fi

    cat > /etc/chrony/chrony.conf << EOF
# Include configuration files from conf.d
confdir /etc/chrony/conf.d

# GPS NMEA data from gpsd (USB GPS)
# This provides the coarse time and second labels
refclock SHM 0 delay 0.2 offset 0.0 poll 4 refid GPS

${PPS_COMMENT}
${PPS_REFCLOCK}

# Network time servers as fallback
pool pool.ntp.org iburst maxsources 4
pool us.pool.ntp.org iburst maxsources 2

# Standard chrony settings
sourcedir /run/chrony-dhcp
sourcedir /etc/chrony/sources.d
keyfile /etc/chrony/chrony.keys
driftfile /var/lib/chrony/chrony.drift
ntsdumpdir /var/lib/chrony
logdir /var/log/chrony
rtcsync
makestep 1 3
leapsectz right/UTC

# Optimize for GPS/PPS
maxupdateskew 100.0
maxclockerror 0.001
maxsamples 32
EOF

    info "Chrony configuration written."
}

# Set up GPS PPS (requires root)
setup_gps_pps() {
    info "Setting up GPS PPS support..."

    if is_raspberry_pi; then
        setup_gpio_pps
    else
        setup_serial_pps
    fi

    info "GPS PPS support configured."
}

# GPIO PPS setup for Raspberry Pi
setup_gpio_pps() {
    info "Configuring GPIO PPS (Raspberry Pi)..."

    # Find config.txt location (varies by RPi OS version)
    if [ -f /boot/firmware/config.txt ]; then
        CONFIG_TXT="/boot/firmware/config.txt"
    elif [ -f /boot/config.txt ]; then
        CONFIG_TXT="/boot/config.txt"
    else
        error "Cannot find config.txt"
        return 1
    fi

    # Add pps-gpio overlay if not already present
    if ! grep -q "^dtoverlay=pps-gpio" "$CONFIG_TXT"; then
        backup_config "$CONFIG_TXT"
        # Add under [all] section, or append if no [all]
        if grep -q "^\[all\]" "$CONFIG_TXT"; then
            sed -i '/^\[all\]/a dtoverlay=pps-gpio,gpiopin=18' "$CONFIG_TXT"
        else
            echo -e "\n[all]\ndtoverlay=pps-gpio,gpiopin=18" >> "$CONFIG_TXT"
        fi
        info "Added pps-gpio overlay (GPIO 18) to $CONFIG_TXT"
        warn "A reboot is required for the PPS overlay to take effect"
    else
        info "pps-gpio overlay already configured in $CONFIG_TXT"
    fi

    # Load pps-gpio module now if possible (may fail without reboot)
    if ! lsmod | grep -q pps_gpio; then
        modprobe pps-gpio 2>/dev/null || warn "pps-gpio module not loaded — reboot required"
    fi

    # Configure chrony service dependencies (no serial-pps dependency on RPi)
    mkdir -p /etc/systemd/system/chrony.service.d/
    cat > /etc/systemd/system/chrony.service.d/gps-pps.conf << 'EOF'
[Unit]
After=network.target

[Service]
ExecStartPre=/bin/sleep 2
EOF

    # Configure gpsd service dependencies
    mkdir -p /etc/systemd/system/gpsd.service.d/
    cat > /etc/systemd/system/gpsd.service.d/gps-pps.conf << 'EOF'
[Unit]
After=
After=network.target

[Service]
ExecStartPre=/bin/sleep 1
EOF

    systemctl daemon-reload
}

# Serial PPS setup for Ubuntu/x86
setup_serial_pps() {
    info "Configuring serial PPS..."

    # Load pps_ldisc module
    if ! lsmod | grep -q pps_ldisc; then
        modprobe pps_ldisc || warn "Could not load pps_ldisc module"
    fi

    # Add to modules to load at boot
    if [ -d /etc/modules-load.d ]; then
        echo "pps_ldisc" > /etc/modules-load.d/pps.conf
    fi

    # Configure chrony service dependencies
    # NOTE: intentionally does not list gpsd.service to avoid circular dependency
    mkdir -p /etc/systemd/system/chrony.service.d/
    cat > /etc/systemd/system/chrony.service.d/gps-pps.conf << 'EOF'
[Unit]
After=network.target serial-pps.service
Wants=serial-pps.service

[Service]
ExecStartPre=/bin/sleep 2
EOF

    # Configure gpsd service dependencies
    # Clear default After=chronyd.service to break circular dependency
    mkdir -p /etc/systemd/system/gpsd.service.d/
    cat > /etc/systemd/system/gpsd.service.d/serial-pps.conf << 'EOF'
[Unit]
After=
After=network.target serial-pps.service
Wants=serial-pps.service

[Service]
ExecStartPre=/bin/sleep 1
EOF

    # Enable serial-pps service
    systemctl enable serial-pps.service || warn "Could not enable serial-pps service"

    systemctl daemon-reload
}

# Install desktop file and autostart
install_desktop_file() {
    info "Installing desktop launcher and autostart..."

    # Use the invoking user's home when run via sudo
    if [ -n "$SUDO_USER" ]; then
        TARGET_HOME="$(getent passwd "$SUDO_USER" | cut -d: -f6)"
    else
        TARGET_HOME="$HOME"
    fi

    # Detect terminal emulator
    TERM_CMD=""
    if command -v gnome-terminal &>/dev/null; then
        TERM_CMD="gnome-terminal --title=\"Chrony Monitor\" --geometry=80x24 --"
    elif command -v lxterminal &>/dev/null; then
        TERM_CMD="lxterminal --title=\"Chrony Monitor\" --geometry=80x24 -e"
    elif command -v xfce4-terminal &>/dev/null; then
        TERM_CMD="xfce4-terminal --title=\"Chrony Monitor\" --geometry=80x24 -e"
    elif command -v xterm &>/dev/null; then
        TERM_CMD="xterm -title \"Chrony Monitor\" -geometry 80x24 -e"
    else
        warn "No terminal emulator found — desktop shortcut will run without a terminal window"
    fi

    # Generate desktop file with correct project path and terminal
    DESKTOP_FILE="$(mktemp)"
    if [ -n "$TERM_CMD" ]; then
        sed "s|^Exec=.*|Exec=$TERM_CMD python3 -m chrony_monitor\nPath=$PROJECT_DIR\nTerminal=false|" \
            "$PROJECT_DIR/autostart/chrony-monitor.desktop" > "$DESKTOP_FILE"
    else
        sed "s|^Exec=.*|Exec=python3 -m chrony_monitor\nPath=$PROJECT_DIR\nTerminal=true|" \
            "$PROJECT_DIR/autostart/chrony-monitor.desktop" > "$DESKTOP_FILE"
    fi

    # Desktop launcher (for manual launch)
    DESKTOP_DIR="${TARGET_HOME}/.local/share/applications"
    mkdir -p "$DESKTOP_DIR"
    cp "$DESKTOP_FILE" "$DESKTOP_DIR/chrony-monitor.desktop"

    # Autostart (launches on login)
    AUTOSTART_DIR="${TARGET_HOME}/.config/autostart"
    mkdir -p "$AUTOSTART_DIR"
    cp "$DESKTOP_FILE" "$AUTOSTART_DIR/chrony-monitor.desktop"

    # Desktop shortcut
    DESKTOP_SHORTCUT="${TARGET_HOME}/Desktop/chrony-monitor.desktop"
    if [ -d "${TARGET_HOME}/Desktop" ]; then
        cp "$DESKTOP_FILE" "$DESKTOP_SHORTCUT"
        chmod +x "$DESKTOP_SHORTCUT"
        info "Desktop shortcut installed at $DESKTOP_SHORTCUT"
    fi

    # Ensure the invoking user owns the directories and files
    if [ -n "$SUDO_USER" ]; then
        chown "$SUDO_USER:$SUDO_USER" "$DESKTOP_DIR" "$AUTOSTART_DIR"
        chown "$SUDO_USER:$SUDO_USER" "$DESKTOP_DIR/chrony-monitor.desktop" "$AUTOSTART_DIR/chrony-monitor.desktop"
        [ -f "$DESKTOP_SHORTCUT" ] && chown "$SUDO_USER:$SUDO_USER" "$DESKTOP_SHORTCUT"

        # Add user to dialout group for serial/GPS device access
        if ! id -nG "$SUDO_USER" | grep -qw dialout; then
            usermod -aG dialout "$SUDO_USER"
            info "Added $SUDO_USER to dialout group (re-login required)"
        fi

        # Allow passwordless sudo for GPS/PPS service recovery
        cat > /etc/sudoers.d/chrony-monitor << SUDOEOF
$SUDO_USER ALL=(root) NOPASSWD: /usr/bin/systemctl restart gpsd.service
$SUDO_USER ALL=(root) NOPASSWD: /usr/bin/systemctl restart chrony.service
$SUDO_USER ALL=(root) NOPASSWD: /usr/bin/systemctl stop gpsd.service
$SUDO_USER ALL=(root) NOPASSWD: /usr/bin/systemctl stop chrony.service
$SUDO_USER ALL=(root) NOPASSWD: /usr/bin/systemctl start gpsd.service
$SUDO_USER ALL=(root) NOPASSWD: /usr/bin/systemctl start chrony.service
SUDOEOF
        if ! is_raspberry_pi; then
            cat >> /etc/sudoers.d/chrony-monitor << SUDOEOF
$SUDO_USER ALL=(root) NOPASSWD: /usr/bin/systemctl restart serial-pps.service
$SUDO_USER ALL=(root) NOPASSWD: /usr/bin/systemctl stop serial-pps.service
$SUDO_USER ALL=(root) NOPASSWD: /usr/bin/systemctl start serial-pps.service
$SUDO_USER ALL=(root) NOPASSWD: /usr/bin/systemctl is-active serial-pps
$SUDO_USER ALL=(root) NOPASSWD: /usr/bin/pkill ldattach
SUDOEOF
        fi
        chmod 440 /etc/sudoers.d/chrony-monitor
        info "Sudoers rules installed for passwordless service recovery"
    fi

    rm -f "$DESKTOP_FILE"

    info "Desktop launcher installed at $DESKTOP_DIR/chrony-monitor.desktop"
    info "Autostart enabled at $AUTOSTART_DIR/chrony-monitor.desktop"
}

# Start or restart services
start_services() {
    info "Starting services..."

    if ! is_raspberry_pi; then
        systemctl enable --now serial-pps.service || warn "Could not start serial-pps"
    fi
    systemctl enable --now gpsd.service || warn "Could not start gpsd"
    systemctl restart chrony.service || warn "Could not restart chrony"

    # Give services a moment to settle
    sleep 2
}

# Validate hardware setup
validate_hardware() {
    info "Validating hardware and services..."

    # Check for USB GPS device
    if [ -e /dev/ttyACM0 ]; then
        info "GPS USB device found: /dev/ttyACM0"
    else
        warn "GPS USB device not found at /dev/ttyACM0"
        ls -la /dev/ttyUSB* /dev/ttyACM* 2>/dev/null || true
    fi

    # Check PPS device
    if is_raspberry_pi; then
        PPS_DEV="$(find_gpio_pps_device)"
        if [ -n "$PPS_DEV" ]; then
            info "GPIO PPS device found: $PPS_DEV"
        elif [ -L /dev/gps-pps ]; then
            info "PPS symlink /dev/gps-pps exists (awaiting overlay activation)"
        else
            warn "GPIO PPS device not found — reboot required for pps-gpio overlay"
        fi
    else
        if [ -L /dev/serial-pps ]; then
            PPS_TARGET="$(readlink -f /dev/serial-pps)"
            info "PPS symlink /dev/serial-pps -> $PPS_TARGET"
        else
            warn "/dev/serial-pps not found - PPS may not be connected"
        fi
    fi

    # Check gpsd
    if systemctl is-active --quiet gpsd; then
        info "GPSD is running"
        timeout 2 gpspipe -w -n 5 2>/dev/null | grep -q "TPV" && info "GPS has fix" || warn "Waiting for GPS fix..."
    else
        warn "GPSD is not running"
    fi

    # Check chrony
    if systemctl is-active --quiet chrony; then
        info "Chrony is running"
        echo ""
        echo "Chrony sources:"
        chronyc sources | grep -E "(GPS|GPPS)" || warn "No GPS/GPPS sources visible yet"
    else
        warn "Chrony is not running"
    fi
}

# Print usage information
print_usage() {
    echo ""
    echo "Chrony Monitor has been installed!"
    echo ""
    echo "The monitor will start automatically on login."
    echo ""
    echo "Manual usage:"
    echo "  python3 -m chrony_monitor          # Run the monitor"
    echo "  python3 -m chrony_monitor --help   # Show all options"
    echo "  python3 -m chrony_monitor --status # Print status and exit"
    echo ""
    echo "If services fail to start due to dependency issues, run:"
    echo "  sudo $SCRIPT_DIR/fix-dependencies.sh"
    echo ""
}

# Remove serial-PPS artifacts that don't apply on Raspberry Pi
cleanup_serial_pps() {
    if ! is_raspberry_pi; then
        return
    fi

    info "Cleaning up serial-PPS artifacts from previous install..."

    # Stop and disable serial-pps service
    systemctl stop serial-pps.service 2>/dev/null || true
    systemctl disable serial-pps.service 2>/dev/null || true
    rm -f /etc/systemd/system/serial-pps.service

    # Remove serial-pps init script
    rm -f /usr/local/bin/init-serial-pps.sh

    # Remove pps_ldisc module config (not needed for GPIO PPS)
    rm -f /etc/modules-load.d/pps.conf

    # Remove stale serial-pps systemd drop-in for gpsd
    rm -f /etc/systemd/system/gpsd.service.d/serial-pps.conf
    rmdir /etc/systemd/system/gpsd.service.d 2>/dev/null || true

    # Remove stale symlink and runtime files
    rm -f /dev/serial-pps
    rm -f /var/run/pps-serial-port /var/run/ldattach.pid /var/run/pps-device

    # Kill any leftover ldattach processes
    pkill ldattach 2>/dev/null || true

    systemctl daemon-reload

    info "Serial-PPS cleanup complete."
}

# Main installation
main() {
    echo "========================================"
    echo "Chrony Monitor Installation"
    echo "========================================"
    echo ""

    check_root
    cleanup_serial_pps
    install_dependencies
    install_systemd_services
    configure_gpsd
    configure_chrony
    setup_gps_pps
    install_desktop_file
    start_services
    validate_hardware
    print_usage

    echo "========================================"
    echo "Installation complete!"
    echo "========================================"
}

main "$@"
