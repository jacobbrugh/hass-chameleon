#!/usr/bin/env bash
# Reset BLE connection state on both PC and ChameleonUltra.
# Run this when pairing is broken, bonds are stale, or the adapter crashed.
#
# Usage: sudo ./scripts/reset_ble.sh

set -euo pipefail

DEVICE_ADDR="FE:6A:52:FA:98:62"
BT_USB_ID="8087:0032"

echo "=== ChameleonUltra BLE Full Reset ==="

# 1. Clear PC-side bond
echo "[1/6] Removing PC-side bond..."
if bluetoothctl remove "$DEVICE_ADDR" 2>&1 | grep -q "removed"; then
    echo "  Removed"
else
    echo "  No bond to remove"
fi

# 2. Clear device-side bonds via USB
echo "[2/6] Clearing device-side bonds..."
OUTPUT=$({
    sleep 1; echo "hw connect"
    sleep 3; echo "hw settings bleclearbonds --force"
    sleep 2; echo "hw settings store"
    sleep 1; echo "exit"
} | timeout 12 chameleon-cli 2>&1) || true
if echo "$OUTPUT" | grep -qi "Successfully clear"; then
    echo "  Bonds cleared"
else
    echo "  WARNING: bond clear may have failed"
fi
if echo "$OUTPUT" | grep -qi "Store success"; then
    echo "  Settings stored"
fi
sleep 1

# 3. Reset USB adapter back to hci0
echo "[3/6] Resetting BT adapter..."
CURRENT_HCI=$(hciconfig -a 2>&1 | grep "^hci" | head -1 | cut -d: -f1 || true)
echo "  Before: ${CURRENT_HCI:-none}"
usbreset "$BT_USB_ID" 2>&1 || true
sleep 3

# 4. Restart bluetooth service
echo "[4/6] Restarting bluetooth service..."
systemctl restart bluetooth
sleep 2

# 5. Verify adapter state
echo "[5/6] Verifying adapter..."
HCI=$(hciconfig -a 2>&1 | grep "^hci" | head -1 | cut -d: -f1 || true)
echo "  Adapter: ${HCI:-NOT FOUND}"
if [ "${HCI:-}" != "hci0" ]; then
    echo "  ERROR: adapter is ${HCI:-missing}, not hci0"
    exit 1
fi

# 6. Wake device via USB, toggle pairing to force fresh BLE advertising
echo "[6/6] Waking device and restarting BLE advertising..."
usbreset 6868:8686 2>&1 || true
sleep 3
({
    sleep 1; echo "hw connect"
    sleep 3; echo "hw settings blepair -d"
    sleep 1; echo "hw settings blepair -e"
    sleep 1; echo "hw settings store"
    sleep 1; echo "exit"
} | timeout 15 chameleon-cli 2>&1) || true
# USB reset to force BLE stack restart with clean state, then wake via USB
usbreset 6868:8686 2>&1 || true
sleep 3
({
    sleep 1; echo "hw connect"
    sleep 2; echo "exit"
} | timeout 8 chameleon-cli 2>&1) || true
sleep 3

# Verify device is advertising
echo "  Scanning for device..."
SCAN_COUNT=$(bluetoothctl --timeout 5 scan on 2>&1 | grep -c "$DEVICE_ADDR" || true)
if [ "$SCAN_COUNT" -gt 0 ]; then
    echo "  Device found ($SCAN_COUNT times)"
else
    echo "  ERROR: device not advertising after 8s scan"
    exit 1
fi

echo ""
echo "=== Reset complete ==="
echo "Adapter: hci0, bonds cleared, device advertising."
