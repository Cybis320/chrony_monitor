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

# Check if running as root for system-wide install
check_root() {
    if [ "$EUID" -ne 0 ]; then
        warn "Not running as root. Installing in user mode."
        USER_INSTALL=1
    else
        USER_INSTALL=0
    fi
}

# Install Python package
install_python_package() {
    info "Installing chrony-monitor Python package..."

    cd "$PROJECT_DIR"

    if [ "$USER_INSTALL" -eq 1 ]; then
        pip3 install --user -e .
    else
        pip3 install -e .
    fi

    info "Python package installed."
}

# Install systemd services (requires root)
install_systemd_services() {
    if [ "$USER_INSTALL" -eq 1 ]; then
        warn "Skipping systemd service installation (requires root)"
        return
    fi

    info "Installing systemd services..."

    # Install serial-pps service for GPS PPS initialization
    cp "$PROJECT_DIR/systemd/serial-pps.service" /etc/systemd/system/
    cp "$PROJECT_DIR/scripts/init-serial-pps.sh" /usr/local/bin/
    chmod +x /usr/local/bin/init-serial-pps.sh

    # Create udev rules for PPS device
    cat > /etc/udev/rules.d/99-gps-pps.rules << 'EOF'
# GPS USB device
SUBSYSTEM=="tty", KERNEL=="ttyACM[0-9]*", MODE="0666", GROUP="dialout", SYMLINK+="gps0"

# PPS device permissions
SUBSYSTEM=="pps", MODE="0666", GROUP="dialout"
KERNEL=="pps0", SYMLINK+="gpspps0"
EOF

    # Reload systemd
    systemctl daemon-reload

    info "Systemd services installed."
}

# Configure GPSD for USB GPS
configure_gpsd() {
    if [ "$USER_INSTALL" -eq 1 ]; then
        warn "Skipping GPSD configuration (requires root)"
        return
    fi

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
    if [ "$USER_INSTALL" -eq 1 ]; then
        warn "Skipping chrony.conf configuration (requires root)"
        return
    fi

    info "Writing chrony configuration for GPS+PPS..."

    backup_config /etc/chrony/chrony.conf

    cat > /etc/chrony/chrony.conf << 'EOF'
# Include configuration files from conf.d
confdir /etc/chrony/conf.d

# GPS NMEA data from gpsd (USB GPS)
# This provides the coarse time and second labels
refclock SHM 0 delay 0.2 offset 0.0 poll 4 refid GPS

# PPS signal from serial port DCD pin
# The :clear option uses the DCD deassert edge, which corresponds to the
# true second boundary when PPS passes through a MAX232 RS-232 driver
# (the TX driver inverts the signal, so DCD assert = falling edge of pulse)
refclock PPS /dev/pps0:clear poll 4 refid GPPS lock GPS prefer

# Network time servers as fallback
pool ntp.ubuntu.com iburst maxsources 4
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
    if [ "$USER_INSTALL" -eq 1 ]; then
        warn "Skipping GPS PPS setup (requires root)"
        return
    fi

    info "Setting up GPS PPS support..."

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

    info "GPS PPS support configured."
}

# Install desktop file and autostart
install_desktop_file() {
    info "Installing desktop launcher and autostart..."

    # Desktop launcher (for manual launch)
    DESKTOP_DIR="${HOME}/.local/share/applications"
    mkdir -p "$DESKTOP_DIR"
    cp "$PROJECT_DIR/autostart/chrony-monitor.desktop" "$DESKTOP_DIR/"

    # Autostart (launches on login)
    AUTOSTART_DIR="${HOME}/.config/autostart"
    mkdir -p "$AUTOSTART_DIR"
    cp "$PROJECT_DIR/autostart/chrony-monitor.desktop" "$AUTOSTART_DIR/"

    info "Desktop launcher installed at $DESKTOP_DIR/chrony-monitor.desktop"
    info "Autostart enabled at $AUTOSTART_DIR/chrony-monitor.desktop"
}

# Validate hardware setup
validate_hardware() {
    if [ "$USER_INSTALL" -eq 1 ]; then
        return
    fi

    info "Validating hardware and services..."

    # Check for USB GPS device
    if [ -e /dev/ttyACM0 ]; then
        info "GPS USB device found: /dev/ttyACM0"
    else
        warn "GPS USB device not found at /dev/ttyACM0"
        ls -la /dev/ttyUSB* /dev/ttyACM* 2>/dev/null || true
    fi

    # Check PPS device
    if [ -e /dev/pps0 ]; then
        info "/dev/pps0 exists"
        timeout 2 ppstest /dev/pps0 2>&1 | head -3 || warn "ppstest not available or no pulses"
    else
        warn "/dev/pps0 not found - PPS may not be connected"
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
    if [ "$USER_INSTALL" -eq 0 ]; then
        echo "GPS PPS services have been installed. To start now:"
        echo "  systemctl start serial-pps"
        echo "  systemctl start gpsd"
        echo "  systemctl start chrony"
        echo ""
        echo "If services fail to start due to dependency issues, run:"
        echo "  sudo $SCRIPT_DIR/fix-dependencies.sh"
        echo ""
    fi
}

# Main installation
main() {
    echo "========================================"
    echo "Chrony Monitor Installation"
    echo "========================================"
    echo ""

    check_root
    install_python_package
    install_systemd_services
    configure_gpsd
    configure_chrony
    setup_gps_pps
    install_desktop_file
    validate_hardware
    print_usage

    echo "========================================"
    echo "Installation complete!"
    echo "========================================"
}

main "$@"
