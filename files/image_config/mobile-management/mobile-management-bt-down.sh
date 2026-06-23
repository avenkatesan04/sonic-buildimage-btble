#!/bin/bash
#
# mobile-management-bt-down.sh — Tear down the Bluetooth stack.
#
# Called as ExecStopPost by mobile-management.service.
# Returns the system to the BT-dark state (Phase 0 default).
#
set +e

# Bound every potentially-blocking call: stopping BlueZ or removing btusb can
# hang if the USB controller is wedged (the same -EPIPE condition that hangs the
# bring-up). A timeout keeps ExecStopPost from stalling unit teardown.
BT_CMD_TIMEOUT=10
BT_MODULES_REVERSE="btusb btmtk btintel btbcm btrtl bluetooth"

echo "mobile-management: blocking radio..."
timeout "$BT_CMD_TIMEOUT" rfkill block bluetooth 2>/dev/null || true

echo "mobile-management: stopping bluetooth.service..."
timeout "$BT_CMD_TIMEOUT" systemctl stop bluetooth.service 2>/dev/null || true
timeout "$BT_CMD_TIMEOUT" systemctl mask bluetooth.service 2>/dev/null || true

echo "mobile-management: unloading BT kernel modules..."
for mod in $BT_MODULES_REVERSE; do
    if timeout "$BT_CMD_TIMEOUT" modprobe -r "$mod" 2>/dev/null; then
        echo "  - $mod"
    else
        echo "  ~ $mod (not loaded)"
    fi
done

echo "mobile-management: BT stack torn down"
exit 0
