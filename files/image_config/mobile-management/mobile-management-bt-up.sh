#!/bin/bash
#
# mobile-management-bt-up.sh — Bring up the Bluetooth stack for mobile-management.
#
# Called as ExecStartPre by mobile-management.service.
# Reversal is handled by mobile-management-bt-down.sh (ExecStopPost).
#
# Steps:
#   1. Load kernel modules (bluetooth → firmware helpers → btusb)
#   2. Unmask and start bluetooth.service (BlueZ)
#   3. Wait for an HCI adapter to appear
#   4. Power on the adapter and restrict to BLE-only
#
set -e

BT_MODULES="bluetooth btrtl btbcm btintel btmtk btusb"

echo "mobile-management: loading BT kernel modules..."
for mod in $BT_MODULES; do
    if modprobe "$mod" 2>/dev/null; then
        echo "  + $mod"
    else
        echo "  ! $mod unavailable (skipped)"
    fi
done

echo "mobile-management: unblocking radio..."
rfkill unblock bluetooth 2>/dev/null || true

echo "mobile-management: starting bluetooth.service..."
systemctl unmask bluetooth.service
systemctl start bluetooth.service

# Wait for an HCI adapter (firmware push can take 10-15s on Realtek adapters)
echo "mobile-management: waiting for HCI adapter..."
TIMEOUT=30
for i in $(seq 1 $TIMEOUT); do
    if hciconfig 2>/dev/null | grep -q "^hci"; then
        HCI_DEV=$(hciconfig 2>/dev/null | grep "^hci" | head -1 | cut -d: -f1)
        echo "  found $HCI_DEV after ${i}s"
        break
    fi
    sleep 1
done

if [ -z "$HCI_DEV" ]; then
    echo "mobile-management: WARNING: no HCI adapter found after ${TIMEOUT}s"
    echo "mobile-management: daemon will start but BLE advertising may fail"
    exit 0
fi

# Power on via btmgmt (preferred over hciconfig for BlueZ 5.x)
echo "mobile-management: powering on $HCI_DEV..."
btmgmt --index 0 power on 2>/dev/null || hciconfig "$HCI_DEV" up 2>/dev/null || true

# Restrict to BLE-only (disable BR/EDR classic bluetooth)
btmgmt --index 0 bredr off 2>/dev/null || true

echo "mobile-management: BT stack ready"
