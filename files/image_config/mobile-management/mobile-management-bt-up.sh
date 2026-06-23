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

# Check if any Bluetooth USB hardware is present before waiting
if ! lsusb 2>/dev/null | grep -qi "bluetooth\|0a12:\|0cf3:\|0bda:\|8087:"; then
    echo "mobile-management: no BT USB hardware detected — skipping adapter wait"
    echo "mobile-management: daemon will start but BLE advertising may fail"
    exit 0
fi

# Wait for an HCI adapter (firmware push can take 10-15s on Realtek adapters).
# Every hciconfig call is wrapped in `timeout` so a wedged controller cannot
# block the loop. BT_CMD_TIMEOUT bounds each individual BlueZ call; the loop
# counts keep the worst-case total runtime well under the unit's
# TimeoutStartSec so systemd never has to kill a hung start-pre.
BT_CMD_TIMEOUT=5
echo "mobile-management: waiting for HCI adapter..."
TIMEOUT=20
for i in $(seq 1 $TIMEOUT); do
    if timeout "$BT_CMD_TIMEOUT" hciconfig 2>/dev/null | grep -q "^hci"; then
        HCI_DEV=$(timeout "$BT_CMD_TIMEOUT" hciconfig 2>/dev/null | grep "^hci" | head -1 | cut -d: -f1)
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

# Power on the adapter. A wedged or non-responsive controller (e.g. a USB
# adapter whose HCI_Reset fails with -EPIPE under QEMU passthrough, or a slow
# firmware load) makes hciconfig/btmgmt block indefinitely. Without a timeout
# the call hangs forever, ExecStartPre never returns, and systemd eventually
# kills the whole unit — preventing the daemon (which supports a no-adapter
# mode) from ever starting. The timeout guarantees this script always finishes.
echo "mobile-management: powering on $HCI_DEV..."
timeout "$BT_CMD_TIMEOUT" hciconfig "$HCI_DEV" up 2>/dev/null || true
timeout "$BT_CMD_TIMEOUT" btmgmt --index 0 power on 2>/dev/null || true

# Wait for the adapter to actually come UP. Poll status only (the power-on was
# already issued above) with a short per-call timeout, so the worst-case wait is
# bounded to UP_TIMEOUT seconds even if the controller is wedged.
echo "mobile-management: waiting for $HCI_DEV to come UP..."
UP_TIMEOUT=10
adapter_up=0
for i in $(seq 1 $UP_TIMEOUT); do
    if timeout 2 hciconfig "$HCI_DEV" 2>/dev/null | grep -q "UP RUNNING"; then
        echo "  $HCI_DEV is UP after ${i}s"
        adapter_up=1
        break
    fi
    sleep 1
done

if [ "$adapter_up" -ne 1 ]; then
    echo "mobile-management: WARNING: $HCI_DEV did not come UP after ${UP_TIMEOUT}s"
    echo "mobile-management: daemon will start in no-adapter mode (BLE advertising disabled)"
fi

# Restrict to BLE-only (disable BR/EDR classic bluetooth)
timeout "$BT_CMD_TIMEOUT" btmgmt --index 0 bredr off 2>/dev/null || true

echo "mobile-management: BT stack ready"
exit 0
