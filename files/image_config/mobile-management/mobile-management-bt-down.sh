#!/bin/bash
#
# mobile-management-bt-down.sh — Tear down the Bluetooth stack.
#
# Called as ExecStopPost by mobile-management.service.
# Returns the system to the BT-dark state (Phase 0 default).
#
set -e

BT_MODULES_REVERSE="btusb btmtk btintel btbcm btrtl bluetooth"

echo "mobile-management: blocking radio..."
rfkill block bluetooth 2>/dev/null || true

echo "mobile-management: stopping bluetooth.service..."
systemctl stop bluetooth.service 2>/dev/null || true
systemctl mask bluetooth.service 2>/dev/null || true

echo "mobile-management: unloading BT kernel modules..."
for mod in $BT_MODULES_REVERSE; do
    if modprobe -r "$mod" 2>/dev/null; then
        echo "  - $mod"
    else
        echo "  ~ $mod (not loaded)"
    fi
done

echo "mobile-management: BT stack torn down"
