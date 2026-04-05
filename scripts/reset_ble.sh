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
bluetoothctl remove "$DEVICE_ADDR" 2>&1 | grep -E "removed|not available" || true

# 2. Clear device-side bonds via USB
echo "[2/6] Clearing device-side bonds..."
chameleon-cli -c "hw connect; hw settings bleclearbonds --force; hw settings store" 2>&1 | grep -iE "clear|store|success|connected" || true
sleep 1

# 3. Reset USB adapter back to hci0
echo "[3/6] Resetting BT adapter..."
CURRENT_HCI=$(hciconfig -a 2>&1 | grep "^hci" | head -1 | cut -d: -f1)
echo "  Current adapter: ${CURRENT_HCI:-none}"
usbreset "$BT_USB_ID" 2>&1 | grep -v "^$" || true
sleep 3
NEW_HCI=$(hciconfig -a 2>&1 | grep "^hci" | head -1 | cut -d: -f1)
echo "  Adapter after reset: ${NEW_HCI:-none}"

# 4. Restart bluetooth service
echo "[4/6] Restarting bluetooth service..."
systemctl restart bluetooth
sleep 2

# 5. Verify adapter state
echo "[5/6] Verifying adapter..."
HCI=$(hciconfig -a 2>&1 | grep "^hci" | head -1 | cut -d: -f1)
echo "  Adapter: $HCI"
if [ "$HCI" != "hci0" ]; then
    echo "  WARNING: adapter is $HCI, not hci0"
fi

# 6. Wake device and verify it's advertising
echo "[6/6] Waking device and scanning..."
chameleon-cli -c "hw connect" 2>&1 | grep -i "connected" || echo "  WARNING: could not wake device via USB"
sleep 2
timeout 5 bluetoothctl scan on 2>&1 | grep -c "$DEVICE_ADDR" | xargs -I{} echo "  Device found: {} times" || echo "  WARNING: device not found in scan"

echo ""
echo "=== Reset complete ==="
echo "Both sides cleared. Device should be ready for fresh pairing."
