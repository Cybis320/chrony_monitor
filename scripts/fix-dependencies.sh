#!/bin/bash
#
# Fix systemd service dependency issues for GPS/PPS/chrony
#
# Resolves circular dependencies and ordering cycles between
# serial-pps, gpsd, and chrony services. This script is idempotent.
#

set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

info()  { echo -e "${GREEN}[INFO]${NC} $1"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $1"; }
error() { echo -e "${RED}[ERROR]${NC} $1"; }

if [ "$EUID" -ne 0 ]; then
    echo "Please run as root (use sudo)"
    exit 1
fi

echo "========================================"
echo "Fix GPS/PPS Service Dependencies"
echo "========================================"
echo ""

# Step 1: Remove stale service files from previous install attempts
info "Step 1: Removing stale service files..."

for f in /etc/systemd/system/pps-setup.service \
         /etc/systemd/system/chrony.service.d/override.conf; do
    if [ -f "$f" ]; then
        rm "$f"
        info "Removed $f"
    fi
done

# Step 2: Write correct chrony drop-in
# Does NOT list gpsd.service to avoid circular dependency
info "Step 2: Writing chrony service drop-in..."

mkdir -p /etc/systemd/system/chrony.service.d/
cat > /etc/systemd/system/chrony.service.d/gps-pps.conf << 'EOF'
[Unit]
After=network.target serial-pps.service
Wants=serial-pps.service

[Service]
ExecStartPre=/bin/sleep 2
EOF

# Step 3: Write correct gpsd drop-in
# Clears default After=chronyd.service to break circular dependency
info "Step 3: Writing gpsd service drop-in..."

mkdir -p /etc/systemd/system/gpsd.service.d/
cat > /etc/systemd/system/gpsd.service.d/serial-pps.conf << 'EOF'
[Unit]
After=
After=network.target serial-pps.service
Wants=serial-pps.service

[Service]
ExecStartPre=/bin/sleep 1
EOF

# Step 4: Reload systemd
info "Step 4: Reloading systemd daemon..."
systemctl daemon-reload

# Step 5: Restart services in correct order
info "Step 5: Restarting services..."

echo "  Stopping all services..."
systemctl stop chrony 2>/dev/null || true
systemctl stop gpsd 2>/dev/null || true
systemctl stop serial-pps 2>/dev/null || true
pkill ldattach 2>/dev/null || true

echo "  Starting serial-pps..."
systemctl start serial-pps
sleep 2

echo "  Starting gpsd..."
systemctl start gpsd
sleep 2

echo "  Starting chrony..."
systemctl start chrony
sleep 3

# Step 6: Validate
info "Step 6: Validating services..."
echo ""

FAIL=0
for svc in serial-pps gpsd chrony; do
    if systemctl is-active --quiet "$svc"; then
        info "$svc: running"
    else
        error "$svc: FAILED"
        FAIL=1
    fi
done

echo ""
info "Waiting 10 seconds for GPPS lock..."
sleep 10

echo ""
echo "Chrony sources:"
chronyc sources

echo ""
echo "========================================"
if [ "$FAIL" -eq 0 ]; then
    info "All services running. Look for '#* GPPS' above to confirm PPS lock."
else
    error "One or more services failed. Check: journalctl -u serial-pps -u gpsd -u chrony -n 50"
fi
echo "========================================"
echo ""
echo "Correct ordering: serial-pps -> gpsd -> chrony (no circular deps)"
