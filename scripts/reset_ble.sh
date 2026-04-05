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
echo "[1] Removing PC-side bond..."
if bluetoothctl remove "$DEVICE_ADDR" 2>&1 | grep -q "removed"; then
    echo "  Removed"
else
    echo "  No bond to remove"
fi

# 2. Clear device-side bonds via USB
echo "[2] Clearing device-side bonds..."
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

# 3. Fix adapter if not hci0 (usbreset crashes Intel firmware, so only do it when needed)
HCI=$(hciconfig -a 2>&1 | grep "^hci" | head -1 | cut -d: -f1 || true)
if [ "${HCI:-}" = "hci0" ]; then
    echo "[3] Adapter already hci0, skipping USB reset"
    systemctl restart bluetooth
    sleep 2
elif [ -n "${HCI:-}" ]; then
    echo "[3] Adapter is $HCI, USB resetting to get hci0..."
    usbreset "$BT_USB_ID" 2>&1 || true
    sleep 3
    systemctl restart bluetooth
    sleep 2
    HCI=$(hciconfig -a 2>&1 | grep "^hci" | head -1 | cut -d: -f1 || true)
    if [ "${HCI:-}" != "hci0" ]; then
        echo "  WARNING: still ${HCI:-missing}, reboot may be needed"
    fi
else
    echo "[3] No adapter found, USB resetting..."
    usbreset "$BT_USB_ID" 2>&1 || true
    sleep 3
    systemctl restart bluetooth
    sleep 2
    HCI=$(hciconfig -a 2>&1 | grep "^hci" | head -1 | cut -d: -f1 || true)
fi
echo "  Adapter: ${HCI:-NOT FOUND}"

# 4. Wake device, toggle pairing to force fresh BLE advertising
echo "[4] Waking device..."

# Try connecting via USB first — only usbreset if that fails
WAKE_OUTPUT=$({
    sleep 1; echo "hw connect"
    sleep 3; echo "hw settings blepair -d"
    sleep 1; echo "hw settings blepair -e"
    sleep 1; echo "hw settings store"
    sleep 1; echo "exit"
} | timeout 15 chameleon-cli 2>&1) || true

if echo "$WAKE_OUTPUT" | grep -qi "connected.*v[0-9]"; then
    echo "  Device connected via USB"
else
    echo "  USB connect failed, resetting ChameleonUltra..."
    usbreset 6868:8686 2>&1 || true
    sleep 3
    ({
        sleep 1; echo "hw connect"
        sleep 3; echo "hw settings blepair -d"
        sleep 1; echo "hw settings blepair -e"
        sleep 1; echo "hw settings store"
        sleep 1; echo "exit"
    } | timeout 15 chameleon-cli 2>&1) || true
fi
sleep 2

# 5. Verify device is advertising
echo "[5] Scanning for device..."
SCAN_COUNT=$(bluetoothctl --timeout 5 scan on 2>&1 | grep -c "$DEVICE_ADDR" || true)
if [ "$SCAN_COUNT" -gt 0 ]; then
    echo "  Device found ($SCAN_COUNT times)"
else
    echo "  WARNING: device not found in scan"
fi

echo ""
echo "=== Reset complete ==="
