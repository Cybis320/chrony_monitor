#!/bin/bash
#
# Chrony Monitor Installation Script
# Installs the chrony-monitor package and optionally sets up GPS PPS support
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
    if [ -f /etc/modules-load.d ]; then
        echo "pps_ldisc" > /etc/modules-load.d/pps.conf
    fi

    # Configure chrony service dependencies
    mkdir -p /etc/systemd/system/chrony.service.d/
    cat > /etc/systemd/system/chrony.service.d/gps-pps.conf << 'EOF'
[Unit]
After=network.target serial-pps.service
Wants=serial-pps.service

[Service]
ExecStartPre=/bin/sleep 2
EOF

    # Enable serial-pps service
    systemctl enable serial-pps.service || warn "Could not enable serial-pps service"

    systemctl daemon-reload

    info "GPS PPS support configured."
}

# Install desktop file (user mode)
install_desktop_file() {
    info "Installing desktop launcher..."

    DESKTOP_DIR="${HOME}/.local/share/applications"
    mkdir -p "$DESKTOP_DIR"

    cat > "$DESKTOP_DIR/chrony-monitor.desktop" << EOF
[Desktop Entry]
Name=Chrony Monitor
Type=Application
Exec=gnome-terminal --title="Chrony Monitor" -- python3 -m chrony_monitor
Hidden=false
NoDisplay=false
Icon=utilities-system-monitor
Comment=Monitor chrony time synchronization
Categories=System;Monitor;
EOF

    info "Desktop launcher installed at $DESKTOP_DIR/chrony-monitor.desktop"
}

# Print usage information
print_usage() {
    echo ""
    echo "Chrony Monitor has been installed!"
    echo ""
    echo "Usage:"
    echo "  python3 -m chrony_monitor          # Run the monitor"
    echo "  python3 -m chrony_monitor --help   # Show all options"
    echo "  python3 -m chrony_monitor --status # Print status and exit"
    echo ""
    echo "Options:"
    echo "  --ntp-only      Force NTP-only mode"
    echo "  --no-recovery   Disable auto-recovery"
    echo "  --interval N    Set polling interval (default: 1s)"
    echo ""
    if [ "$USER_INSTALL" -eq 0 ]; then
        echo "GPS PPS services have been installed. To start:"
        echo "  systemctl start serial-pps"
        echo "  systemctl start chrony"
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
    setup_gps_pps
    install_desktop_file
    print_usage

    echo "========================================"
    echo "Installation complete!"
    echo "========================================"
}

main "$@"
