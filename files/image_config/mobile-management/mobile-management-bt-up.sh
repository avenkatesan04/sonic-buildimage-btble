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
set +e

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
systemctl unmask bluetooth.service 2>/dev/null || true
systemctl start bluetooth.service 2>/dev/null || true

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

# Give BlueZ a moment to claim the adapter
sleep 2

# Power on via multiple methods for robustness
echo "mobile-management: powering on $HCI_DEV..."
hciconfig "$HCI_DEV" up 2>/dev/null || true
sleep 1
btmgmt --index 0 power on 2>/dev/null || true

# Wait for adapter to actually come UP
echo "mobile-management: waiting for $HCI_DEV to come UP..."
UP_TIMEOUT=15
for i in $(seq 1 $UP_TIMEOUT); do
    if hciconfig "$HCI_DEV" 2>/dev/null | grep -q "UP RUNNING"; then
        echo "  $HCI_DEV is UP after ${i}s"
        break
    fi
    # Retry power-on each iteration in case BlueZ wasn't ready
    hciconfig "$HCI_DEV" up 2>/dev/null || true
    sleep 1
done

if ! hciconfig "$HCI_DEV" 2>/dev/null | grep -q "UP RUNNING"; then
    echo "mobile-management: WARNING: $HCI_DEV did not come UP after ${UP_TIMEOUT}s"
fi

# Restrict to BLE-only (disable BR/EDR classic bluetooth)
btmgmt --index 0 bredr off 2>/dev/null || true

echo "mobile-management: BT stack ready"
