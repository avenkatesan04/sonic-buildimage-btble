from __future__ import annotations
"""
ble_switch_peripheral.py — BLE Peripheral for Switch Sensor Telemetry
======================================================================
Advertises as "SwitchMon" and exposes a GATT service covering:
  - CPU / Memory / Temperature
  - Port link status bitmask
  - Per-port stats (chunked bulk)
  - PSU status
  - LED state (per-port + system)
  - Port configuration (admin state, speed, MTU, description, FEC)
  - Live alarms
  - Command channel (write without response + write with response)
  - Auth (HMAC-SHA256 challenge-response)
  - Keepalive (bidirectional heartbeat)
  - Time-left countdown

TUI views (Tab to cycle):
  0 - Telemetry  : sensor values + PSU + port grid + alarm count
  1 - Sessions   : session history table + per-session command log
  2 - Dev Menu   : trigger/clear alarms for testing

Requirements:
    pip install bless prompt_toolkit dbus-fast
    Also needs sensor_simulator.py in the same directory.

Usage:
    sudo python ble_switch_peripheral.py
    sudo python ble_switch_peripheral.py --ports 24 --interval 3
    sudo python ble_switch_peripheral.py --duration 300
"""

import asyncio
import argparse
import hashlib
import hmac as _hmac
import json
import logging
import math
import os as _os
import re
import struct
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from bless import (
    BlessServer,
    BlessGATTCharacteristic,
    GATTCharacteristicProperties,
    GATTAttributePermissions,
)
from mobile_management.sensor_simulator import (
    SwitchSensorSimulator, SwitchSnapshot,
    LED_COLORS, LED_BLINKS, COLOR_CODE, BLINK_CODE, SPEED_OPTIONS,
    FanState,
)

_HEADLESS = False

try:
    from prompt_toolkit import Application
    from prompt_toolkit.filters import Condition
    from prompt_toolkit.formatted_text import ANSI
    from prompt_toolkit.layout import Layout, HSplit, Window
    from prompt_toolkit.layout.controls import FormattedTextControl
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.styles import Style
    _HAS_TUI = True
except ImportError:
    _HAS_TUI = False

log = logging.getLogger(__name__)

# ── UUIDs ─────────────────────────────────────────────────────────────────────

SERVICE_UUID             = "aabb0000-0000-1000-8000-00805f9b34fb"
CHAR_CPU_UUID            = "aabb0001-0000-1000-8000-00805f9b34fb"
CHAR_MEM_UUID            = "aabb0002-0000-1000-8000-00805f9b34fb"
CHAR_TEMP_UUID           = "aabb0003-0000-1000-8000-00805f9b34fb"
CHAR_PORTS_UUID          = "aabb0004-0000-1000-8000-00805f9b34fb"
CHAR_PORTCNT_UUID        = "aabb0005-0000-1000-8000-00805f9b34fb"
CHAR_CMD_WNR_UUID        = "aabb0006-0000-1000-8000-00805f9b34fb"
CHAR_CMD_WR_UUID         = "aabb0007-0000-1000-8000-00805f9b34fb"
CHAR_RESPONSE_UUID       = "aabb0008-0000-1000-8000-00805f9b34fb"
CHAR_TIMELEFT_UUID       = "aabb0009-0000-1000-8000-00805f9b34fb"
CHAR_BULK_UUID           = "aabb000a-0000-1000-8000-00805f9b34fb"
CHAR_KEEPALIVE_UUID      = "aabb000b-0000-1000-8000-00805f9b34fb"
CHAR_AUTH_CHALLENGE_UUID = "aabb000c-0000-1000-8000-00805f9b34fb"
CHAR_AUTH_RESPONSE_UUID  = "aabb000d-0000-1000-8000-00805f9b34fb"
CHAR_AUTH_STATUS_UUID    = "aabb000e-0000-1000-8000-00805f9b34fb"
# ── New characteristics ───────────────────────────────────────────────────────
CHAR_PSU_UUID            = "aabb000f-0000-1000-8000-00805f9b34fb"
CHAR_ALARMS_UUID         = "aabb0010-0000-1000-8000-00805f9b34fb"
CHAR_PORT_CFG_UUID       = "aabb0011-0000-1000-8000-00805f9b34fb"
CHAR_LED_UUID            = "aabb0012-0000-1000-8000-00805f9b34fb"
CHAR_FAN_UUID            = "aabb0013-0000-1000-8000-00805f9b34fb"
CHAR_CLIENT_NAME_UUID    = "aabb0014-0000-1000-8000-00805f9b34fb"
AUTH_OPEN_UUIDS = {
    CHAR_PORTCNT_UUID,
    CHAR_AUTH_CHALLENGE_UUID,
    CHAR_AUTH_RESPONSE_UUID,
    CHAR_AUTH_STATUS_UUID,
    CHAR_KEEPALIVE_UUID,
    CHAR_CLIENT_NAME_UUID,   # name may be set before or after auth
}

PORTS_PER_CHUNK  = 4
STATS_ENTRY_SIZE = 21

# ── Command name table ────────────────────────────────────────────────────────

COMMAND_NAMES = {
    0x01: "Port LED blink/identify",
    0x02: "Clear interface counters",
    0x03: "Adjust notify interval",
    0x04: "Trigger stats snapshot",
    0x05: "LLDP neighbor refresh",
    0x06: "Syslog verbosity change",
    0x10: "Port shutdown",
    0x11: "Port enable (no shutdown)",
    0x12: "VLAN membership change",
    0x13: "Static route add",
    0x14: "Static route remove",
    0x15: "ACL rule apply",
    0x16: "BGP neighbor activate",
    0x17: "BGP neighbor deactivate",
    0x18: "Interface MTU change",
    0x19: "LAG member add",
    0x1A: "LAG member remove",
    0x1B: "Config save",
    # New commands
    0x20: "LED set",
    0x21: "Port admin up",
    0x22: "Port admin down",
    0x23: "Port speed set",
    0x24: "Port MTU set",
    0x25: "Port description set",
    0x26: "Alarm acknowledge",
    0x27: "Request PSU snapshot",
    0x28: "Request port config",
    0x29: "Request LED states",
    0x30: "Client log event",
    0x31: "Port split mode set",
    0x32: "Client disconnect",
}

# ── Dev menu alarm presets ────────────────────────────────────────────────────

DEV_ALARM_PRESETS = [
    # (label, category, severity, message_template)  {port} is replaced if present
    ("Port link down",       "port",    "minor",    "Port {port}: link down unexpectedly"),
    ("Port errors high",     "port",    "major",    "Port {port}: error rate exceeds threshold"),
    ("Port flap storm",      "port",    "major",    "Port {port}: link flapping — possible cable issue"),
    ("PSU 1 failure",        "psu",     "critical", "PSU 1: output failure detected"),
    ("PSU 2 failure",        "psu",     "critical", "PSU 2: output failure detected"),
    ("Fan failure",          "fan",     "major",    "Fan unit 1: RPM below minimum threshold"),
    ("Thermal high",         "thermal", "major",    "CPU temperature exceeds 80°C"),
    ("BGP session down",     "system",  "major",    "BGP peer 10.0.0.1 session dropped"),
    ("Config mismatch",      "system",  "warning",  "Running config differs from startup config"),
]


# ── Client tracking ───────────────────────────────────────────────────────────

@dataclass
class CommandRecord:
    timestamp: datetime
    kind: str
    opcode: int
    decoded: str


@dataclass
class ClientRecord:
    mac: str
    name: str
    connected_at: datetime
    disconnected_at: Optional[datetime] = None
    commands: List[CommandRecord] = field(default_factory=list)
    last_keepalive: Optional[datetime] = None
    is_fallback:      bool = False         # True when registered via keepalive/command fallback (no real MAC)
    username:         Optional[str] = None # app-provided display name (overrides BlueZ name)
    failed_attempt:   bool = False         # True when link dropped before app sent first keepalive
    received_goodbye: bool = False         # True when app sent the 0x32 clean-disconnect command

    @property
    def is_connected(self) -> bool:
        return self.disconnected_at is None

    @property
    def duration_str(self) -> str:
        end = self.disconnected_at or datetime.now()
        s   = int((end - self.connected_at).total_seconds())
        h, m, sec = s // 3600, (s % 3600) // 60, s % 60
        return f"{h:02d}:{m:02d}:{sec:02d}" if h else f"{m:02d}:{sec:02d}"

    @property
    def keepalive_age_str(self) -> str:
        if self.last_keepalive is None:
            return "—"
        secs = int((datetime.now() - self.last_keepalive).total_seconds())
        return f"{secs}s ago"


class ClientRegistry:
    def __init__(self):
        # Each MAC maps to an ordered list of sessions; a new session is
        # appended on every reconnect so history is never lost.
        self._sessions: Dict[str, List[ClientRecord]] = {}
        self._order:    List[str] = []   # MACs in first-seen order
        self._active:   Optional[str] = None

    # ── internal helpers ──────────────────────────────────────────────────────

    def _latest(self, mac: str) -> Optional[ClientRecord]:
        """Return the most-recent session for a MAC, or None."""
        sessions = self._sessions.get(mac)
        return sessions[-1] if sessions else None

    # ── connection lifecycle ──────────────────────────────────────────────────

    def connected(self, mac: str, name: str = "Unknown"):
        if (lat := self._latest(mac)) and lat.is_connected:
            return   # already tracked
        self._sessions.setdefault(mac, []).append(
            ClientRecord(mac=mac, name=name, connected_at=datetime.now())
        )
        if mac not in self._order:
            self._order.append(mac)
        self._active = mac

    def connected_fallback(self, mac: str, name: str = "BLE Client"):
        """Register a client whose real identity could not be determined.
        The synthetic BLE:CLIENT placeholder is always replaced; real MACs
        accumulate a new session instead."""
        if mac == "BLE:CLIENT":
            # placeholder — overwrite rather than accumulate
            self._sessions[mac] = [ClientRecord(
                mac=mac, name=name, connected_at=datetime.now(), is_fallback=True
            )]
        else:
            if (lat := self._latest(mac)) and lat.is_connected:
                return
            self._sessions.setdefault(mac, []).append(ClientRecord(
                mac=mac, name=name, connected_at=datetime.now(), is_fallback=True
            ))
        if mac not in self._order:
            self._order.append(mac)
        self._active = mac

    def disconnected(self, mac: str):
        if lat := self._latest(mac):
            lat.disconnected_at = datetime.now()
            # If no keepalive ever arrived AND no clean-goodbye was sent, the app
            # never properly established the session (e.g. stale pairing keys).
            # A goodbye with no keepalive just means the user disconnected quickly.
            if lat.last_keepalive is None and not lat.received_goodbye and not lat.is_fallback:
                lat.failed_attempt = True
        if self._active == mac:
            remaining = [m for m in self._order
                         if m != mac
                         and (lat2 := self._latest(m)) is not None
                         and lat2.is_connected]
            self._active = remaining[-1] if remaining else None

    # ── per-session updates ───────────────────────────────────────────────────

    def record_keepalive(self, mac: Optional[str] = None):
        target = mac or self._active
        if target and (lat := self._latest(target)):
            lat.last_keepalive = datetime.now()

    def set_client_name(self, mac: Optional[str], username: str):
        target = mac or self._active
        if target and (lat := self._latest(target)):
            lat.username    = username[:64]
            lat.is_fallback = False

    def record_goodbye(self, mac: Optional[str] = None):
        target = mac or self._active
        if target and (lat := self._latest(target)):
            lat.received_goodbye = True

    def record_command(self, kind: str, opcode: int, decoded: str):
        if self._active is None:
            self.connected_fallback("BLE:CLIENT", "BLE Client")
        if lat := self._latest(self._active):
            lat.commands.append(CommandRecord(
                timestamp=datetime.now(), kind=kind, opcode=opcode, decoded=decoded,
            ))

    # ── queries ───────────────────────────────────────────────────────────────

    def all_sessions(self) -> List[Tuple[int, 'ClientRecord']]:
        """Flat list of (1-based global index, session) ordered oldest-first."""
        flat: List[ClientRecord] = []
        for mac in self._order:
            flat.extend(self._sessions.get(mac, []))
        flat.sort(key=lambda s: s.connected_at)
        return [(i + 1, s) for i, s in enumerate(flat)]

    def all_clients(self) -> List[ClientRecord]:
        """Latest session per MAC — used by the stale-client checker."""
        return [lat for mac in self._order if (lat := self._latest(mac)) is not None]

    @property
    def connected_count(self) -> int:
        return sum(
            1 for mac in self._order
            if (lat := self._latest(mac)) is not None and lat.is_connected
        )

    def all_commands(self, limit: int = 50) -> List[Tuple[str, str, CommandRecord]]:
        cmds: List[Tuple[str, str, CommandRecord]] = []
        for mac in self._order:
            for sess in self._sessions.get(mac, []):
                label = sess.username or (sess.name if sess.name != "Unknown" else mac)
                for cmd in sess.commands:
                    cmds.append((mac, label, cmd))
        cmds.sort(key=lambda x: x[2].timestamp)
        return cmds[-limit:]


# ── TUI log handler ───────────────────────────────────────────────────────────

_ANSI_RE = re.compile(r'\x1b\[[0-9;]*[mK]')


class TUILogHandler(logging.Handler):
    def __init__(self, lines: List[str], tick_lines: List[str],
                 app_ref: List, max_lines: int = 300):
        super().__init__()
        self._lines      = lines
        self._tick_lines = tick_lines
        self._app_ref    = app_ref
        self._max        = max_lines

    def emit(self, record: logging.LogRecord):
        try:
            msg    = _ANSI_RE.sub('', self.format(record))
            bucket = self._tick_lines if record.getMessage().startswith("TICK") else self._lines
            bucket.append(msg)
            if len(bucket) > self._max:
                del bucket[:-self._max]
            if self._app_ref and self._app_ref[0] is not None:
                self._app_ref[0].invalidate()
        except Exception:
            self.handleError(record)


# ── TUI state ─────────────────────────────────────────────────────────────────

@dataclass
class PeripheralState:
    view:       int = 0
    snapshot:   Optional[SwitchSnapshot] = None
    registry:   ClientRegistry = field(default_factory=ClientRegistry)
    log_lines:  List[str]      = field(default_factory=list)
    tick_lines: List[str]      = field(default_factory=list)
    secs_left:  int = -1
    num_ports:  int = 48
    interval:   float = 2.0
    app_ref:    List = field(default_factory=list)
    dev_msg:    str  = ""   # status line in dev menu
    cmd_tab:    int  = 0    # 0 = All sessions, 1..N = specific session (1-based, oldest-first)
    show_qr:    bool = False

    def invalidate(self):
        if self.app_ref and self.app_ref[0] is not None:
            try:
                self.app_ref[0].invalidate()
            except Exception:
                pass


# ── Globals ───────────────────────────────────────────────────────────────────

server:               Optional[BlessServer]     = None
simulator:            Optional[SwitchSensorSimulator] = None
latest_snapshot:      Optional[SwitchSnapshot]  = None
configured_num_ports: int                        = 0
cmd_queue:            Optional[asyncio.Queue]    = None
auth_queue:           Optional[asyncio.Queue]    = None
session_end_time:     float                      = 0.0
_shutdown_event:      Optional[asyncio.Event]    = None
_tui_state:           Optional[PeripheralState]  = None

# ── Auth state ────────────────────────────────────────────────────────────────
_users:         dict  = {}   # username → plaintext password
_auth_username: str   = ""   # username written pre-auth; used for HMAC key lookup
_auth_nonce:    bytes = b""
_authenticated: bool  = False
_auth_bypass:   bool  = False  # True = full R/W with no auth (config mobile-management auth-mode bypass)
_audit_log      = None         # AuditLog instance (set in main())
AUTH_TIMEOUT_SECS = 30


# Control opcodes that mutate state — blocked unless authenticated or bypass
_MUTATION_OPCODES = frozenset({
    0x03,  # SET_INTERVAL
    0x20,  # LED_SET
    0x21,  # PORT_ADMIN_UP
    0x22,  # PORT_ADMIN_DOWN
    0x23,  # PORT_SPEED_SET
    0x24,  # PORT_MTU_SET
    0x25,  # PORT_DESC_SET
    0x26,  # ALARM_ACK
    0x31,  # SET_SPLIT
    0x33,  # DEMO_RESET
})

_ble_device_name: str = "SwitchMon"  # set at startup for QR URI encoding


def _new_nonce() -> bytes:
    global _auth_nonce
    _auth_nonce = _os.urandom(16)
    if server is not None:
        char = server.get_characteristic(CHAR_AUTH_CHALLENGE_UUID)
        if char is not None:
            char.value = bytearray(_auth_nonce)
            server.update_value(SERVICE_UUID, CHAR_AUTH_CHALLENGE_UUID)
    return _auth_nonce


def _reset_auth():
    global _authenticated, _auth_username
    _authenticated  = False
    _auth_username  = ""
    _new_nonce()
    log.info("AUTH  session reset — new nonce issued")




# ── GATT callbacks ────────────────────────────────────────────────────────────

def read_request(characteristic: BlessGATTCharacteristic, **kwargs) -> bytearray:
    uuid = characteristic.uuid

    if uuid == CHAR_AUTH_CHALLENGE_UUID:
        return bytearray(_auth_nonce)
    if uuid == CHAR_AUTH_STATUS_UUID:
        # 0x01 = full access (authenticated, or bypass mode)
        # 0x02 = view-only (no users configured, enforce mode)
        # 0x00 = locked (users configured, not yet authenticated)
        if _authenticated or _auth_bypass:
            return bytearray([0x01])
        if not _users:
            return bytearray([0x02])
        return bytearray([0x00])
    if uuid == CHAR_PORTCNT_UUID:
        count = simulator.num_ports if simulator is not None else configured_num_ports
        return bytearray(struct.pack('B', count))

    if latest_snapshot is None:
        return bytearray(b'\x00')

    snap = latest_snapshot

    if uuid == CHAR_CPU_UUID:
        log.info("READ  CPU usage requested")
        return SwitchSensorSimulator.pack_cpu(snap)
    if uuid == CHAR_MEM_UUID:
        log.info("READ  Memory usage requested")
        return SwitchSensorSimulator.pack_memory(snap)
    if uuid == CHAR_TEMP_UUID:
        log.info("READ  Temperatures requested")
        return SwitchSensorSimulator.pack_temperatures(snap)
    if uuid == CHAR_PORTS_UUID:
        log.info("READ  Port status requested")
        return SwitchSensorSimulator.pack_ports(snap)
    if uuid == CHAR_TIMELEFT_UUID:
        return bytearray(struct.pack('>I', _seconds_left()))
    if uuid == CHAR_PSU_UUID:
        log.info("READ  PSU status requested — client opened PSU view")
        return SwitchSensorSimulator.pack_psus(snap)
    if uuid == CHAR_ALARMS_UUID:
        log.info("READ  Alarms requested — client opened Alarms view")
        return SwitchSensorSimulator.pack_alarms(snap)
    if uuid == CHAR_LED_UUID:
        log.info("READ  LED states requested")
        return SwitchSensorSimulator.pack_leds(snap)

    return bytearray()


def write_request(characteristic: BlessGATTCharacteristic, value: Any, **kwargs):
    global _auth_username
    uuid = characteristic.uuid
    data = bytes(value)

    if uuid == CHAR_AUTH_RESPONSE_UUID:
        if not _users:
            return  # auth disabled
        # Look up the password for the pending username.
        # For unknown usernames compute HMAC against a random key so the
        # timing is identical and usernames cannot be enumerated.
        password = _users.get(_auth_username, "")
        hmac_key = (password if password else _os.urandom(32).hex()).encode('utf-8')
        expected = _hmac.new(hmac_key, _auth_nonce, hashlib.sha256).digest()
        if password and _hmac.compare_digest(data, expected):
            auth_queue.put_nowait(("ok", _auth_username))
        else:
            if not password:
                log.warning(f"AUTH  ✗ unknown user '{_auth_username}'")
            auth_queue.put_nowait(("fail",))
        return


    if uuid == CHAR_KEEPALIVE_UUID:
        if _tui_state is not None:
            # If D-Bus missed the connection event, register a placeholder now
            # so the client shows up in the TUI immediately.
            if _tui_state.registry.connected_count == 0:
                _tui_state.registry.connected_fallback("BLE:CLIENT", "BLE Client")
                log.info("CLIENT  registered via keepalive (D-Bus event missed)")
            _tui_state.registry.record_keepalive()
            _tui_state.invalidate()
        return

    if uuid == CHAR_CLIENT_NAME_UUID:
        try:
            username = data.decode('utf-8', errors='replace').strip()
            if username:
                if not _authenticated:
                    # Store as the pending auth identity so the HMAC handler
                    # knows which user's password to verify against.
                    _auth_username = username
                if _tui_state is not None:
                    _tui_state.registry.set_client_name(None, username)
                    _tui_state.invalidate()
                log.info(f"CLIENT  name → '{username}'")
        except Exception as exc:
            log.debug(f"CLIENT NAME  parse error: {exc}")
        return

    # Allow 0x32 (client disconnect) through without auth — it must always
    # be processable so the peripheral can clean up BlueZ state on disconnect.
    if uuid == CHAR_CMD_WR_UUID and data and data[0] == 0x32:
        if cmd_queue is not None:
            cmd_queue.put_nowait(("wr", data))
        # Mark the session as intentionally closed so it is never flagged as a failed attempt.
        if _tui_state is not None:
            _tui_state.registry.record_goodbye()
        return

    # Block mutation commands when not authorized.
    # Read-only request opcodes (0x04, 0x27, 0x28, 0x29) pass through.
    if not _authenticated and not _auth_bypass:
        opcode = data[0] if data else 0x00
        if opcode in _MUTATION_OPCODES:
            log.warning(f"WRITE  {uuid}  opcode 0x{opcode:02X} blocked — not authenticated")
            return

    if uuid == CHAR_CMD_WNR_UUID:
        if cmd_queue is not None:
            cmd_queue.put_nowait(("wnr", data))
    elif uuid == CHAR_CMD_WR_UUID:
        if cmd_queue is not None:
            cmd_queue.put_nowait(("wr", data))
    else:
        log.warning(f"WRITE to {uuid} ignored")


# ── Keepalive ─────────────────────────────────────────────────────────────────

KEEPALIVE_INTERVAL = 5.0
KEEPALIVE_TIMEOUT  = 15.0


async def keepalive_loop():
    seq = 0
    await asyncio.sleep(2.0)
    while True:
        _push(CHAR_KEEPALIVE_UUID, bytearray([seq]))
        seq = (seq + 1) & 0xFF
        await asyncio.sleep(KEEPALIVE_INTERVAL)


async def stale_client_checker(registry: ClientRegistry, state: PeripheralState):
    while True:
        await asyncio.sleep(KEEPALIVE_INTERVAL)
        now = datetime.now()
        for client in registry.all_clients():
            if not client.is_connected or client.last_keepalive is None:
                continue
            age = (now - client.last_keepalive).total_seconds()
            if age > KEEPALIVE_TIMEOUT:
                registry.disconnected(client.mac)
                log.warning(f"CLIENT  unresponsive  mac={client.mac}")
                state.invalidate()


# ── Connection monitor ────────────────────────────────────────────────────────

async def _remove_device(device_path: str):
    """Remove a disconnected device from BlueZ so it can reconnect immediately."""
    try:
        from dbus_fast.aio import MessageBus
        from dbus_fast.constants import BusType
        await asyncio.sleep(2.0)  # wait for iOS to fully release the connection
        bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
        intr  = await bus.introspect('org.bluez', '/')
        proxy = bus.get_proxy_object('org.bluez', '/', intr)
        om    = proxy.get_interface('org.freedesktop.DBus.ObjectManager')
        objs  = await om.call_get_managed_objects()

        adapter_path = None
        for path, ifaces in objs.items():
            if 'org.bluez.Adapter1' in ifaces:
                adapter_path = path
                break

        if adapter_path is None:
            await bus.disconnect()
            return

        a_intr  = await bus.introspect('org.bluez', adapter_path)
        a_proxy = bus.get_proxy_object('org.bluez', adapter_path, a_intr)
        a_iface = a_proxy.get_interface('org.bluez.Adapter1')
        await a_iface.call_remove_device(device_path)
        log.info(f"CLIENT  removed device from BlueZ: {device_path}")
        await bus.disconnect()
    except Exception as exc:
        log.debug(f"_remove_device: {exc}")


async def _force_remove_connected():
    """Remove all currently-connected devices from BlueZ.

    Called on clean disconnect (0x32 command) and on D-Bus disconnect events
    to ensure iOS doesn't cache a stale pairing entry.
    """
    try:
        from dbus_fast.aio import MessageBus
        from dbus_fast.constants import BusType
        bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
        intr  = await bus.introspect('org.bluez', '/')
        proxy = bus.get_proxy_object('org.bluez', '/', intr)
        om    = proxy.get_interface('org.freedesktop.DBus.ObjectManager')
        objs  = await om.call_get_managed_objects()
        adapter_path = None
        for path, ifaces in objs.items():
            if 'org.bluez.Adapter1' in ifaces:
                adapter_path = path
                break
        if adapter_path is None:
            await bus.disconnect()
            return
        a_intr  = await bus.introspect('org.bluez', adapter_path)
        a_proxy = bus.get_proxy_object('org.bluez', adapter_path, a_intr)
        a_iface = a_proxy.get_interface('org.bluez.Adapter1')
        for path, ifaces in objs.items():
            if 'org.bluez.Device1' in ifaces and path.startswith(adapter_path + '/dev_'):
                props     = ifaces['org.bluez.Device1']
                connected = props.get('Connected')
                if connected and hasattr(connected, 'value') and connected.value:
                    try:
                        await a_iface.call_remove_device(path)
                        log.info(f"CLIENT  force-removed {path} from BlueZ")
                    except Exception:
                        pass
        await bus.disconnect()
    except Exception as exc:
        log.debug(f"_force_remove_connected: {exc}")


async def _monitor_connections(registry: ClientRegistry, state: PeripheralState):
    try:
        from dbus_fast.aio import MessageBus
        from dbus_fast.constants import BusType
        from dbus_fast.message import Message
        from dbus_fast.service import ServiceInterface, method as dbus_method
    except ImportError:
        log.warning("dbus-fast not installed — client tracking disabled")
        return

    try:
        bus = await MessageBus(bus_type=BusType.SYSTEM).connect()

        _AGENT_PATH = "/org/bluez/agent/switchmon"

        class _PairingAgent(ServiceInterface):
            def __init__(self):
                super().__init__('org.bluez.Agent1')

            @dbus_method()
            def Release(self): pass

            @dbus_method()
            def RequestPinCode(self, device: 'o') -> 's':   # type: ignore
                return "0000"

            @dbus_method()
            def DisplayPinCode(self, device: 'o', pincode: 's'): pass  # type: ignore

            @dbus_method()
            def RequestPasskey(self, device: 'o') -> 'u':  # type: ignore
                return 0

            @dbus_method()
            def DisplayPasskey(self, device: 'o', passkey: 'u', entered: 'q'): pass  # type: ignore

            @dbus_method()
            def RequestConfirmation(self, device: 'o', passkey: 'u'): pass  # type: ignore

            @dbus_method()
            def RequestAuthorization(self, device: 'o'): pass  # type: ignore

            @dbus_method()
            def AuthorizeService(self, device: 'o', uuid: 's'): pass  # type: ignore

            @dbus_method()
            def Cancel(self): pass

        try:
            agent = _PairingAgent()
            bus.export(_AGENT_PATH, agent)
            intr      = await bus.introspect('org.bluez', '/org/bluez')
            proxy     = bus.get_proxy_object('org.bluez', '/org/bluez', intr)
            agent_mgr = proxy.get_interface('org.bluez.AgentManager1')
            await agent_mgr.call_register_agent(_AGENT_PATH, 'NoInputNoOutput')
            await agent_mgr.call_request_default_agent(_AGENT_PATH)
        except Exception as exc:
            log.warning(f"Pairing agent: {exc}")

        device_names: Dict[str, str] = {}

        try:
            intr  = await bus.introspect('org.bluez', '/')
            proxy = bus.get_proxy_object('org.bluez', '/', intr)
            om    = proxy.get_interface('org.freedesktop.DBus.ObjectManager')
            objs  = await om.call_get_managed_objects()
            for path, ifaces in objs.items():
                if 'org.bluez.Device1' not in ifaces:
                    continue
                props = ifaces['org.bluez.Device1']
                def _val(k):
                    v = props.get(k)
                    return v.value if (v is not None and hasattr(v, 'value')) else None
                mac = _val('Address')
                if mac:
                    mac = mac.upper()
                    device_names[mac] = _val('Name') or 'Unknown'
                    if _val('Connected'):
                        registry.connected(mac, device_names[mac])
                        state.invalidate()
        except Exception as exc:
            log.debug(f"D-Bus seed scan skipped: {exc}")

        for rule in [
            "type='signal',sender='org.bluez',interface='org.freedesktop.DBus.Properties',member='PropertiesChanged'",
            "type='signal',sender='org.bluez',interface='org.freedesktop.DBus.ObjectManager',member='InterfacesAdded'",
        ]:
            await bus.call(Message(
                destination='org.freedesktop.DBus', path='/org/freedesktop/DBus',
                interface='org.freedesktop.DBus', member='AddMatch',
                signature='s', body=[rule],
            ))

        def _on_message(msg: Message):
            try:
                if msg.member == 'PropertiesChanged' and msg.body and msg.body[0] == 'org.bluez.Device1':
                    changed = msg.body[1]
                    if 'Connected' not in changed:
                        return
                    part = msg.path.split('/')[-1]
                    if not part.startswith('dev_'):
                        return
                    mac  = part[4:].replace('_', ':').upper()
                    name = device_names.get(mac, 'Unknown')
                    if changed['Connected'].value:
                        registry.connected(mac, name)
                        log.info(f"CLIENT  connected   mac={mac}")
                    else:
                        registry.disconnected(mac)
                        log.info(f"CLIENT  disconnected  mac={mac}")
                        _reset_auth()
                        # Remove from BlueZ so iOS doesn't cache the pairing
                        asyncio.ensure_future(_remove_device(msg.path))
                        asyncio.ensure_future(_force_remove_connected())
                    state.invalidate()
                elif msg.member == 'InterfacesAdded' and msg.body and len(msg.body) >= 2:
                    _, ifaces = msg.body
                    if 'org.bluez.Device1' in ifaces:
                        props = ifaces['org.bluez.Device1']
                        def _v(k):
                            v = props.get(k)
                            return v.value if (v is not None and hasattr(v, 'value')) else None
                        mac = _v('Address')
                        if mac:
                            mac = mac.upper()
                            device_names[mac] = _v('Name') or 'Unknown'
                            # When a device is removed and reconnects, BlueZ fires
                            # InterfacesAdded with Connected=True already set rather
                            # than a separate PropertiesChanged event.
                            if _v('Connected'):
                                registry.connected(mac, device_names[mac])
                                log.info(f"CLIENT  connected (via InterfacesAdded)  mac={mac}")
                                state.invalidate()
            except Exception as exc:
                log.debug(f"D-Bus handler error: {exc}")

        bus.add_message_handler(_on_message)
        while True:
            await asyncio.sleep(60)

    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log.warning(f"D-Bus monitor error: {exc}")


# ── TUI helpers ───────────────────────────────────────────────────────────────

def _bar(pct: float, width: int = 20) -> str:
    filled = int(pct / 100.0 * width)
    return '█' * filled + '░' * (width - filled)


def _fmt_time(secs: int) -> str:
    if secs < 0:
        return "unlimited"
    h, m, s = secs // 3600, (secs % 3600) // 60, secs % 60
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def _ports_grid(snap: SwitchSnapshot, cols: int = 12) -> str:
    """Port grid showing link state, admin state, and LED color."""
    rows = []
    for i in range(0, snap.num_ports, cols):
        cells = []
        for j in range(min(cols, snap.num_ports - i)):
            p   = i + j + 1
            up  = snap.port_up[p - 1]
            cfg = snap.port_configs.get(p)
            led = snap.leds.get(p)
            adm = cfg.admin_up if cfg else True
            col = led.color if led else ("green" if up else "off")
            LED_ICONS = {"green": "●", "amber": "◐", "red": "●", "blue": "●", "white": "●", "off": "○"}
            icon = LED_ICONS.get(col, "○")
            sym  = "A" if not adm else ("▲" if up else "▼")
            cells.append(f"P{p:02d}{sym}{icon}")
        rows.append("  " + "  ".join(cells))
    return "\n".join(rows)


def _alarm_summary(snap: Optional[SwitchSnapshot]) -> str:
    if snap is None or not snap.alarms:
        return "none"
    counts: Dict[str, int] = {}
    for a in snap.alarms:
        if not a.acknowledged:
            counts[a.severity] = counts.get(a.severity, 0) + 1
    if not counts:
        return f"{len(snap.alarms)} (all acked)"
    parts = [f"{n} {s}" for s, n in sorted(counts.items(), key=lambda x: -["warning","minor","major","critical"].index(x[0]) if x[0] in ["warning","minor","major","critical"] else 0)]
    return ", ".join(parts)


# ── View 0: Telemetry ─────────────────────────────────────────────────────────

def _telemetry_text(state: PeripheralState) -> str:
    snap = state.snapshot
    lines: List[str] = [""]

    if snap is None:
        lines.append("  Waiting for first sensor tick…")
    else:
        t = snap.temperatures
        lines += [
            f"  CPU Usage   : {snap.cpu_pct:5.1f}%  {_bar(snap.cpu_pct)}",
            f"  Memory      : {snap.mem_pct:5.1f}%  {_bar(snap.mem_pct)}",
            f"  Temp CPU die: {t.cpu_die:5.1f}°C   Board: {t.board:.1f}°C   "
            f"Inlet: {t.inlet:.1f}°C   Outlet: {t.outlet:.1f}°C",
            "",
        ]

        # PSU status
        lines.append("  ── PSU ──────────────────────────────────────────────────")
        for p in snap.psus:
            ok_str  = "✓ OK  " if (p.input_ok and p.output_ok) else "✗ FAIL"
            pres    = "present" if p.present else "absent "
            lines.append(
                f"  PSU{p.psu_id}  {ok_str}  {pres}  "
                f"Vin={p.voltage_in:.1f}V  Vout={p.voltage_out:.2f}V  "
                f"{p.current:.1f}A  {p.power:.0f}W  {p.temperature:.1f}°C  {p.fan_rpm}rpm"
            )

        # Port grid
        lines += [
            "",
            f"  ── Ports ({snap.ports_up_count}/{snap.num_ports} up) — ▲=up ▼=down A=admin-down ●=LED ──",
            _ports_grid(snap),
            "",
            f"  ── Alarms ─────────────  {_alarm_summary(snap)}",
        ]
        for a in (snap.alarms or [])[-5:]:
            ack = " [ack]" if a.acknowledged else ""
            lines.append(f"  [{a.severity.upper():8s}] {a.category}: {a.message}{ack}")

    lines += ["", "  ── Ticks ────────────────────────────────────────────────", ""]
    for line in state.tick_lines[-5:]:
        lines.append(f"  {line}")

    lines += ["", "  ── Events ───────────────────────────────────────────────", ""]
    for line in state.log_lines[-12:]:
        lines.append(f"  {line}")
    return "\n".join(lines)


# ANSI colour codes used in the clients view
_R  = "\033[0m"          # reset
_B  = "\033[1m"          # bold
_Y  = "\033[33m"         # yellow
_RD = "\033[31m"         # red
_DM = "\033[2m"          # dim
_UL = "\033[4m"          # underline


# ── View 1: Sessions ──────────────────────────────────────────────────────────

def _clients_text(state: PeripheralState) -> str:
    all_sess = state.registry.all_sessions()   # [(global_idx, ClientRecord), ...]
    flash_on = int(time.time() * 2) % 2 == 0

    # Assign a per-MAC session number to each session (S1, S2, …)
    mac_counter: Dict[str, int] = {}
    per_mac_num: Dict[int, int] = {}   # global_idx → per-MAC session number
    for global_idx, sess in all_sess:
        mac_counter[sess.mac] = mac_counter.get(sess.mac, 0) + 1
        per_mac_num[global_idx] = mac_counter[sess.mac]

    out = ["\n"]

    # ── Session history table (newest-first) ──────────────────────────────────
    out.append(f"  {_B}── Sessions ────────────────────────────────────────────────────────{_R}\n")
    if not all_sess:
        out.append("  No sessions yet.\n")
    else:
        hdr = (f"  {'Sess':<6} {'MAC Address':<20} {'Name':<18} "
               f"{'Connected':<10} {'Ended':<10} {'Duration':<10} {'Cmds':<6} Status\n")
        sep = "  " + "─" * 86 + "\n"
        out += [hdr, sep]
        for global_idx, sess in reversed(all_sess):
            s_num        = per_mac_num[global_idx]
            display_name = (sess.username or sess.name)[:17]
            conn_str     = sess.connected_at.strftime("%H:%M:%S")
            disc_str     = sess.disconnected_at.strftime("%H:%M:%S") if sess.disconnected_at else "—"

            if sess.failed_attempt:
                status_str = f"{_RD}✗ failed{_R}"
                name_col   = f"{_DM}{display_name}{_R}"
            elif sess.is_fallback:
                status_str = f"{_B}{_RD}⚠ UNKNOWN{_R}" if flash_on else f"{_Y}⚠ unknown{_R}"
                name_col   = f"{_Y}{display_name}{_R}"
            elif sess.is_connected:
                status_str = "● active"
                name_col   = display_name
            else:
                status_str = "○ ended"
                name_col   = display_name

            out.append(
                f"  S{s_num:<5} {sess.mac:<20} {name_col:<18} "
                f"{conn_str:<10} {disc_str:<10} {sess.duration_str:<10} "
                f"{len(sess.commands):<6} {status_str}\n"
            )
            if sess.is_fallback:
                warn_color = f"{_B}{_RD}" if flash_on else f"{_Y}"
                out.append(
                    f"  {warn_color}     ↳ Identification failed — real MAC unknown."
                    f" Connection status cannot be determined.{_R}\n"
                )
        out.append(sep)

    # ── Command log with per-session tabs ─────────────────────────────────────
    # Failed attempts are excluded — they carry no commands and would clutter tabs.
    tabbed_sess = [(gi, s) for gi, s in all_sess if not s.failed_attempt]

    def _tab_label(global_idx: int, sess: ClientRecord) -> str:
        s_num      = per_mac_num.get(global_idx, 1)
        short_name = (sess.username or sess.name)[:8].rstrip()
        if sess.is_fallback:
            return f"{_Y}?S{s_num}{_DM}"
        return f"{short_name} S{s_num}"

    tab_labels = ["All"] + [_tab_label(gi, s) for gi, s in tabbed_sess]
    n_tabs = len(tab_labels)
    tab    = max(0, min(state.cmd_tab, n_tabs - 1))
    tabs   = "  "
    for idx, label in enumerate(tab_labels):
        if idx == tab:
            tabs += f"{_B}{_UL}[{label}]{_R}  "
        else:
            tabs += f"{_DM}[{label}]{_R}  "
    tabs += f"  {_DM}← → switch{_R}"

    out.append(f"\n  {_B}── Command Log ────────────────────────────────────────────────{_R}\n")
    out.append(tabs + "\n\n")

    if tab == 0:
        cmds = state.registry.all_commands(limit=40)
        if not cmds:
            out.append("  No commands received yet.\n")
        else:
            for _, label, cmd in cmds:
                ts      = cmd.timestamp.strftime("%H:%M:%S")
                tag     = f"[{cmd.kind:3s}]"
                decoded = cmd.decoded[:52]
                out.append(f"  {ts}  {tag}  {decoded:<52}  {_DM}← {label}{_R}\n")
    else:
        _, sess    = tabbed_sess[tab - 1]
        sess_cmds  = list(sess.commands)[-40:]
        label      = sess.username or sess.name
        if not sess_cmds:
            out.append(f"  No commands in this session ({label}).\n")
        else:
            for cmd in sess_cmds:
                ts      = cmd.timestamp.strftime("%H:%M:%S")
                tag     = f"[{cmd.kind:3s}]"
                decoded = cmd.decoded[:60]
                out.append(f"  {ts}  {tag}  {decoded}\n")

    out.append("\n")
    return "".join(out)


# ── View 2: Dev Menu ──────────────────────────────────────────────────────────

def _render_qr_ascii(data: str) -> List[str]:
    """Render a QR code as indented ASCII art lines."""
    try:
        import qrcode
        import io
        qr = qrcode.QRCode(border=1, error_correction=qrcode.constants.ERROR_CORRECT_M)
        qr.add_data(data)
        qr.make(fit=True)
        buf = io.StringIO()
        qr.print_ascii(out=buf, invert=True)
        return [f"  {line}" for line in buf.getvalue().splitlines()]
    except Exception as exc:
        return [f"  [QR render failed: {exc}]"]


def _dev_menu_text(state: PeripheralState) -> str:
    snap = state.snapshot
    lines: List[str] = [
        "",
        "  ── Developer Menu ─────────────────────────────────────────",
        "  Trigger alarms for testing. Client will see them in real time.",
        "",
        "  ── Trigger Alarm ──────────────────────────────────────────",
    ]
    for i, (label, cat, sev, _) in enumerate(DEV_ALARM_PRESETS, 1):
        lines.append(f"  {i:2d}  [{sev.upper():8s}] {label}")

    n_alarms = len(snap.alarms) if snap else 0

    # QR connect section
    if state.show_qr:
        uri = f"switchmonapp://pair?name={_ble_device_name}"
        lines += [
            "",
            "  ── QR Connect ───────────────────────────────────────────────",
            "  Scan with SwitchMon app to auto-connect  (P to hide)",
            "",
        ]
        lines += _render_qr_ascii(uri)
        lines.append("")
    else:
        lines += [
            "",
            "  ── QR Connect ───────────────────────────────────────────────",
            "  P   Show connect QR code",
            "",
        ]

    lines += [
        "  ── Alarm Management ───────────────────────────────────────",
        f"  C   Clear all alarms  ({n_alarms} active)",
        "",
        "  ── PSU Override ────────────────────────────────────────────",
        "  F1  Toggle PSU 1 failure",
        "  F2  Toggle PSU 2 failure",
        "",
        "  ── Fan Override ─────────────────────────────────────────────",
        "  F3  Toggle Fan 1 failure",
        "  F4  Toggle Fan 2 failure",
        "",
    ]
    if state.dev_msg:
        lines += [f"  → {state.dev_msg}", ""]
    return "\n".join(lines)


# ── View 2: Port Details ──────────────────────────────────────────────────────

# ANSI LED colour codes for the port detail view
_LED_COLOR_ANSI = {
    "off":   "\033[2m",        # dim
    "green": "\033[32m",       # green
    "red":   "\033[31m",       # red
    "blue":  "\033[34m",       # blue
    "amber": "\033[33m",       # yellow (closest to amber)
    "white": "\033[97m",       # bright white
}

def _port_detail_view(state: PeripheralState) -> str:
    if simulator is None:
        return "\n  Simulator not initialised.\n"

    flash_on  = int(time.time() * 2) % 2 == 0
    snap      = state.snapshot
    port_up   = (snap.port_up   if snap else [])
    port_cfgs = simulator._port_configs
    leds      = simulator._leds
    num       = simulator.num_ports

    COLS      = 4   # ports per row in the detail table
    lines: List[str] = [""]

    lines.append(
        f"  {_B}── Port Detail ──────────────────────────────────────────────────{_R}\n"
    )
    lines.append(
        f"  {'Port':<6} {'Admin':<7} {'Link':<6} {'Speed':>8}  "
        f"{'MTU':>5}  {'LED':<14}  {'Description'}\n"
    )
    lines.append("  " + "─" * 78)

    t = time.time()

    def _blink_lit(blink_name: str) -> bool:
        if blink_name == "solid":   return True
        if blink_name == "slow":    return int(t * 2) % 2 == 0
        if blink_name == "fast":    return int(t * 8) % 2 == 0
        if blink_name == "pattern":
            return int(t * 6) % 6 in (0, 2)
        return True

    def _port_label(p: int, cfg) -> str:
        if cfg and cfg.parent_port > 0:
            # Use the description set at split time (e.g. "1/1", "1/2").
            # The old formula (p - parent*100) was wrong for the SUB_PORT_BASE=64
            # numbering scheme where sub-ports are 65+ rather than 101+.
            label = cfg.description or f"{cfg.parent_port}/{p}"
            return f"  └ {label}"
        return f"P{p:02d}  "

    def _port_sort_key(p: int) -> tuple:
        cfg = port_cfgs.get(p)
        if cfg and cfg.parent_port > 0:
            return (cfg.parent_port, p % 10)   # sub-port: group after parent
        return (p, 0)                           # physical port: sort by number

    for p in sorted(port_cfgs.keys(), key=_port_sort_key):
        cfg   = port_cfgs.get(p)
        led   = leds.get(p)
        # Link state: sub-ports use their own index; physical ports use port_up list
        if cfg and cfg.parent_port > 0:
            up = led.color == "green" if led else False
        else:
            up = port_up[p - 1] if p - 1 < len(port_up) else False

        admin_up   = cfg.admin_up   if cfg else True
        speed      = cfg.speed      if cfg else 10000
        mtu        = cfg.mtu        if cfg else 1500
        split_mode = cfg.split_mode if cfg else "none"
        desc       = cfg.description[:20] if cfg else ""

        color_name = led.color if led else ("green" if up else "off")
        blink_name = led.blink if led else "solid"
        color_ansi = _LED_COLOR_ANSI.get(color_name, _R)
        lit        = _blink_lit(blink_name)

        if lit:
            led_str = f"{color_ansi}{_B}◉{_R}{color_ansi} {color_name:<6} {blink_name}{_R}"
        else:
            led_str = f"{_DM}○ {color_name:<6} {blink_name}{_R}"

        admin_str = f"{_B}up{_R}  " if admin_up else f"{_Y}down{_R}"
        if cfg and cfg.split_mode != "none":
            link_str = f"{_Y}[{cfg.split_mode}]{_R}      "
        else:
            link_str = (
                f"{_B}\033[32m▲ up{_R}  " if up
                else (f"{_RD}▼ down{_R}" if admin_up else f"{_DM}A down{_R}")
            )
        speed_str   = f"{speed:>6}M"
        label       = _port_label(p, cfg)

        lines.append(
            f"  {label:<8} {admin_str:<14} {link_str:<17} {speed_str}  "
            f"{mtu:>5}  {led_str:<36}  {desc}"
        )

    lines.append("")
    return "\n".join(lines)


# ── Header bar ────────────────────────────────────────────────────────────────

VIEW_NAMES = {0: "TELEMETRY", 1: "SESSIONS", 2: "PORTS", 3: "DEV MENU"}

def _header_text(state: PeripheralState) -> str:
    view_name = VIEW_NAMES.get(state.view, "?")
    n_clients = state.registry.connected_count
    cl_str    = f"{n_clients} client{'s' if n_clients != 1 else ''}"
    tl_str    = f"session: {_fmt_time(state.secs_left)}"
    auth_str  = f"auth: {'✓' if _authenticated else ('locked' if _users else 'off')}"
    snap      = state.snapshot
    n_alarms  = sum(1 for a in (snap.alarms if snap else []) if not a.acknowledged)
    alm_str   = f"⚠ {n_alarms}" if n_alarms else ""
    return (f" SwitchMon ▸ {view_name}  │ [Tab] view │ [q] quit │  "
            f"{tl_str}  │  {auth_str}  │  {cl_str}  {alm_str}")


# ── Application ───────────────────────────────────────────────────────────────

def _build_app(state: PeripheralState) -> Application:
    kb = KeyBindings()

    @kb.add('tab')
    def _toggle(event):
        state.view = (state.view + 1) % 4
        event.app.invalidate()

    @kb.add('q')
    @kb.add('c-c')
    def _quit(event):
        if _shutdown_event is not None:
            _shutdown_event.set()
        event.app.exit()

    # Dev menu numeric keys — trigger presets
    for _n in range(1, len(DEV_ALARM_PRESETS) + 1):
        def _make_alarm_handler(n):
            def _handler(event):
                if state.view != 3 or simulator is None:
                    return
                idx    = n - 1
                label, cat, sev, msg_tpl = DEV_ALARM_PRESETS[idx]
                port   = 1   # default port for port-related alarms
                msg    = msg_tpl.format(port=port)
                alarm_id = simulator.trigger_alarm(cat, sev, msg)
                state.dev_msg = f"Triggered: {label}  (id={alarm_id})"
                log.info(f"DEV  triggered alarm: {label}  sev={sev}  id={alarm_id}")
                event.app.invalidate()
            return _handler
        kb.add(str(_n))(_make_alarm_handler(_n))

    @kb.add('c')
    def _clear_alarms(event):
        if state.view != 3 or simulator is None:
            return
        simulator.clear_alarms()
        state.dev_msg = "All alarms cleared."
        log.info("DEV  all alarms cleared")
        event.app.invalidate()

    @kb.add('p')
    def _toggle_qr(event):
        if state.view != 3:
            return
        state.show_qr = not state.show_qr
        log.info(f"QR  {'shown' if state.show_qr else 'hidden'}")
        event.app.invalidate()

    @kb.add('f1')
    def _psu1_fail(event):
        if state.view != 3 or simulator is None:
            return
        p = simulator._psus[0]
        p.output_ok = not p.output_ok
        action = "failed" if not p.output_ok else "restored"
        if not p.output_ok:
            simulator.trigger_alarm("psu", "critical", f"PSU 1: output {action}")
        state.dev_msg = f"PSU 1 output {action}"
        # Push immediate PSU notification so app updates without waiting for next tick
        _push(CHAR_PSU_UUID, SwitchSensorSimulator.pack_psus(
            type('Snap', (), {'psus': list(simulator._psus)})()))
        event.app.invalidate()

    @kb.add('f2')
    def _psu2_fail(event):
        if state.view != 3 or simulator is None:
            return
        p = simulator._psus[1]
        p.output_ok = not p.output_ok
        action = "failed" if not p.output_ok else "restored"
        if not p.output_ok:
            simulator.trigger_alarm("psu", "critical", f"PSU 2: output {action}")
        state.dev_msg = f"PSU 2 output {action}"
        _push(CHAR_PSU_UUID, SwitchSensorSimulator.pack_psus(
            type('Snap', (), {'psus': list(simulator._psus)})()))
        event.app.invalidate()

    @kb.add('f3')
    def _fan1_fail(event):
        if state.view != 3 or simulator is None:
            return
        simulator.set_fan_failure(1)
        f = simulator._fans[0]
        action = "failed" if not f.ok else "restored"
        if not f.ok:
            simulator.trigger_alarm("fan", "major", f"Fan 1: RPM below minimum threshold")
        state.dev_msg = f"Fan 1 {action}"
        _push(CHAR_FAN_UUID, SwitchSensorSimulator.pack_fans(
            type('Snap', (), {'fans': list(simulator._fans)})()))
        event.app.invalidate()

    @kb.add('f4')
    def _fan2_fail(event):
        if state.view != 3 or simulator is None:
            return
        simulator.set_fan_failure(2)
        f = simulator._fans[1]
        action = "failed" if not f.ok else "restored"
        if not f.ok:
            simulator.trigger_alarm("fan", "major", f"Fan 2: RPM below minimum threshold")
        state.dev_msg = f"Fan 2 {action}"
        _push(CHAR_FAN_UUID, SwitchSensorSimulator.pack_fans(
            type('Snap', (), {'fans': list(simulator._fans)})()))
        event.app.invalidate()

    # ── Command-log tab navigation (← / → while in Clients view) ─────────────
    _in_clients = Condition(lambda: state.view == 1)

    @kb.add('left', filter=_in_clients)
    def _cmd_tab_prev(event):
        # Only count sessions that appear as tabs (failed attempts are excluded)
        n_tabs = sum(1 for _, s in state.registry.all_sessions() if not s.failed_attempt) + 1
        state.cmd_tab = (state.cmd_tab - 1) % n_tabs
        event.app.invalidate()

    @kb.add('right', filter=_in_clients)
    def _cmd_tab_next(event):
        n_tabs = sum(1 for _, s in state.registry.all_sessions() if not s.failed_attempt) + 1
        state.cmd_tab = (state.cmd_tab + 1) % n_tabs
        event.app.invalidate()

    layout = Layout(
        HSplit([
            Window(FormattedTextControl(lambda: _header_text(state)),
                   height=1, style="class:header"),
            Window(
                FormattedTextControl(lambda: ANSI(
                    _telemetry_text(state)   if state.view == 0
                    else _clients_text(state)     if state.view == 1
                    else _port_detail_view(state) if state.view == 2
                    else _dev_menu_text(state)
                )),
                wrap_lines=True,
            ),
        ])
    )

    style = Style.from_dict({
        "header": "bg:#005f87 #ffffff bold",
    })

    return Application(
        layout=layout, key_bindings=kb, style=style,
        full_screen=True, mouse_support=False, refresh_interval=0.5,
    )


# ── Sensor → BLE notify loop ──────────────────────────────────────────────────

async def sensor_loop(sim: SwitchSensorSimulator, interval: float):
    global latest_snapshot
    await asyncio.sleep(1.5)

    async for snap in sim.stream(interval):
        latest_snapshot = snap
        if _tui_state is not None:
            _tui_state.snapshot = snap
            _tui_state.invalidate()

        t = snap.temperatures
        log.info(
            f"TICK  cpu={snap.cpu_pct:.1f}%  mem={snap.mem_pct:.1f}%  "
            f"temp_cpu={t.cpu_die:.1f}C  ports={snap.ports_up_count}/{snap.num_ports}  "
            f"alarms={len(snap.alarms)}"
        )

        if server is None:
            continue

        _push(CHAR_CPU_UUID,   SwitchSensorSimulator.pack_cpu(snap))
        _push(CHAR_MEM_UUID,   SwitchSensorSimulator.pack_memory(snap))
        _push(CHAR_TEMP_UUID,  SwitchSensorSimulator.pack_temperatures(snap))
        _push(CHAR_PORTS_UUID, SwitchSensorSimulator.pack_ports(snap))
        _push(CHAR_PSU_UUID,   SwitchSensorSimulator.pack_psus(snap))
        _push(CHAR_FAN_UUID,   SwitchSensorSimulator.pack_fans(snap))
        _push(CHAR_LED_UUID,   SwitchSensorSimulator.pack_leds(snap))
        _push(CHAR_ALARMS_UUID, SwitchSensorSimulator.pack_alarms(snap))


def _push(char_uuid: str, value: bytearray):
    char = server.get_characteristic(char_uuid)
    if char is not None:
        char.value = value
        server.update_value(SERVICE_UUID, char_uuid)


# ── Command encoding helpers ──────────────────────────────────────────────────

def _format_command_response(kind: str, opcode: int, data: bytes) -> str:
    name   = COMMAND_NAMES.get(opcode, f"unknown 0x{opcode:02X}")
    params = data[1:]
    try:
        if opcode in (0x01, 0x02, 0x05, 0x10, 0x11) and params:
            detail = f"port={params[0]}"
        elif opcode == 0x03 and len(params) >= 2:
            ms = struct.unpack('>H', params[:2])[0]
            detail = f"interval={ms}ms"
        elif opcode == 0x06 and params:
            detail = f"level={params[0]}"
        elif opcode == 0x12 and len(params) >= 3:
            vlan = struct.unpack('>H', params[1:3])[0]
            detail = f"port={params[0]} vlan={vlan}"
        elif opcode in (0x13, 0x14) and len(params) >= 5:
            ip = f"{params[0]}.{params[1]}.{params[2]}.{params[3]}"
            detail = f"dest={ip}/{params[4]}"
        elif opcode == 0x18 and len(params) >= 3:
            mtu = struct.unpack('>H', params[1:3])[0]
            detail = f"port={params[0]} mtu={mtu}"
        elif opcode in (0x19, 0x1A) and len(params) >= 2:
            detail = f"port={params[0]} lag={params[1]}"
        elif opcode == 0x20 and len(params) >= 3:
            color = LED_COLORS[params[1]] if params[1] < len(LED_COLORS) else "?"
            blink = LED_BLINKS[params[2]] if params[2] < len(LED_BLINKS) else "?"
            detail = f"port={params[0]} color={color} blink={blink}"
        elif opcode in (0x21, 0x22) and params:
            detail = f"port={params[0]} admin={'up' if opcode == 0x21 else 'down'}"
        elif opcode == 0x23 and len(params) >= 2:
            speed = SPEED_OPTIONS[params[1]] if params[1] < len(SPEED_OPTIONS) else "?"
            detail = f"port={params[0]} speed={speed}Mbps"
        elif opcode == 0x24 and len(params) >= 3:
            mtu = struct.unpack('>H', params[1:3])[0]
            detail = f"port={params[0]} mtu={mtu}"
        elif opcode == 0x25 and len(params) >= 2:
            desc_len = params[1]
            desc = params[2:2 + desc_len].decode('utf-8', errors='replace')
            detail = f"port={params[0]} desc={desc!r}"
        elif opcode == 0x26 and params:
            id_len = params[0]
            alarm_id = params[1:1 + id_len].decode('utf-8', errors='replace')
            detail = f"alarm_id={alarm_id}"
        else:
            detail = ""
    except Exception:
        detail = ""

    suffix = f"  [{detail}]" if detail else ""
    return f"ok ({kind}): {name}{suffix}"


# ── Stats snapshot ────────────────────────────────────────────────────────────

async def _send_stats_snapshot():
    if latest_snapshot is None:
        _push(CHAR_RESPONSE_UUID, b"error: no snapshot yet")
        return
    raw    = SwitchSensorSimulator.pack_port_stats(latest_snapshot)
    cp     = PORTS_PER_CHUNK * STATS_ENTRY_SIZE
    pieces = [raw[i:i + cp] for i in range(0, len(raw), cp)]
    total  = len(pieces)
    _push(CHAR_RESPONSE_UUID, f"stats: sending {total} chunks…".encode())
    for seq, payload in enumerate(pieces):
        _push(CHAR_BULK_UUID, bytearray([seq, total]) + bytearray(payload))
        await asyncio.sleep(0.02)
    _push(CHAR_RESPONSE_UUID,
          f"stats: {total} chunks sent ({latest_snapshot.num_ports} ports)".encode())


async def _send_port_config(port: int):
    """Push port config notification for a single port (or all if port==0xFF).

    Reads directly from simulator._port_configs (live state) so mutations from
    commands (set_port_desc, set_port_admin, etc.) are visible immediately
    without waiting for the next sensor tick.
    """
    if simulator is None:
        return
    if port == 0xFF:
        # Send all ports — physical and sub-ports
        ports_to_send = sorted(simulator._port_configs.keys())
    else:
        ports_to_send = [port]

    for p in ports_to_send:
        cfg = simulator._port_configs.get(p)
        if cfg:
            _push(CHAR_PORT_CFG_UUID, SwitchSensorSimulator.pack_port_config(p, cfg))
            await asyncio.sleep(0.01)


# ── Command loop ──────────────────────────────────────────────────────────────

def _audit(opcode: int, data: bytes, result: str = "success"):
    """Record a mutation command in the audit log."""
    if _audit_log is None or opcode not in _MUTATION_OPCODES:
        return
    name = COMMAND_NAMES.get(opcode, f"0x{opcode:02X}")
    detail = _format_command_response("wr", opcode, data)
    username = _auth_username if _authenticated else ("bypass" if _auth_bypass else "anonymous")
    _audit_log.record(username=username, command=name, detail=detail, result=result)


async def command_loop():
    while True:
        kind, data = await cmd_queue.get()
        opcode = data[0] if data else 0x00
        params = data[1:]
        name   = COMMAND_NAMES.get(opcode, f"unknown 0x{opcode:02X}")
        log.info(f"CMD  0x{opcode:02X} {kind}  {params.hex() if params else '—'}  → {name}")

        # ── New commands that mutate simulator state ───────────────────────
        # After any port config mutation, we immediately push a fresh port config
        # notification so the client's cached state updates without needing a
        # manual 0x28 request.
        if opcode == 0x20 and len(params) >= 3 and simulator is not None:
            port  = params[0]
            # Port 0 is the system LED — it has no port config entry but is valid.
            if port != 0 and port not in simulator._port_configs:
                _push(CHAR_RESPONSE_UUID, f"error: port {port} does not exist".encode())
                continue
            color = LED_COLORS[params[1]] if params[1] < len(LED_COLORS) else "off"
            blink = LED_BLINKS[params[2]] if params[2] < len(LED_BLINKS) else "solid"
            simulator.set_led(port, color, blink)
            decoded = _format_command_response(kind, opcode, data)
            _push(CHAR_RESPONSE_UUID, decoded.encode())
            _audit(opcode, data)
            # Push updated LED state immediately so client sees the change
            entries = sorted(simulator._leds.items())
            led_buf = bytearray([len(entries)])
            for _p, _led in entries:
                led_buf += bytes([_p,
                                  COLOR_CODE.get(_led.color, 0),
                                  BLINK_CODE.get(_led.blink, 0)])
            _push(CHAR_LED_UUID, led_buf)

        elif opcode == 0x02 and params and simulator is not None:
            port = params[0]
            if port in simulator._port_stats:
                s = simulator._port_stats[port]
                s.tx_bytes = s.rx_bytes = s.tx_packets = s.rx_packets = 0
                s.tx_errors = s.rx_errors = 0
                decoded = f"Counters cleared: port={port}"
            else:
                decoded = f"Clear counters failed: port={port} not found"
            _push(CHAR_RESPONSE_UUID, decoded.encode())

        elif opcode == 0x21 and params and simulator is not None:
            port = params[0]
            if port not in simulator._port_configs:
                _push(CHAR_RESPONSE_UUID, f"error: port {port} does not exist".encode())
                continue
            simulator.set_port_admin(port, True)
            decoded = _format_command_response(kind, opcode, data)
            _push(CHAR_RESPONSE_UUID, decoded.encode())
            _audit(opcode, data)
            asyncio.ensure_future(_send_port_config(port))

        elif opcode == 0x22 and params and simulator is not None:
            port = params[0]
            if port not in simulator._port_configs:
                _push(CHAR_RESPONSE_UUID, f"error: port {port} does not exist".encode())
                continue
            simulator.set_port_admin(port, False)
            decoded = _format_command_response(kind, opcode, data)
            _push(CHAR_RESPONSE_UUID, decoded.encode())
            _audit(opcode, data)
            asyncio.ensure_future(_send_port_config(port))

        elif opcode == 0x23 and len(params) >= 2 and simulator is not None:
            port  = params[0]
            if port not in simulator._port_configs:
                _push(CHAR_RESPONSE_UUID, f"error: port {port} does not exist".encode())
                continue
            speed = SPEED_OPTIONS[params[1]] if params[1] < len(SPEED_OPTIONS) else 10000
            simulator.set_port_speed(port, speed)
            decoded = _format_command_response(kind, opcode, data)
            _push(CHAR_RESPONSE_UUID, decoded.encode())
            _audit(opcode, data)
            asyncio.ensure_future(_send_port_config(port))

        elif opcode == 0x24 and len(params) >= 3 and simulator is not None:
            port = params[0]
            if port not in simulator._port_configs:
                _push(CHAR_RESPONSE_UUID, f"error: port {port} does not exist".encode())
                continue
            mtu  = struct.unpack('>H', params[1:3])[0]
            simulator.set_port_mtu(port, mtu)
            decoded = _format_command_response(kind, opcode, data)
            _push(CHAR_RESPONSE_UUID, decoded.encode())
            _audit(opcode, data)
            asyncio.ensure_future(_send_port_config(port))

        elif opcode == 0x25 and len(params) >= 2 and simulator is not None:
            port     = params[0]
            if port not in simulator._port_configs:
                _push(CHAR_RESPONSE_UUID, f"error: port {port} does not exist".encode())
                continue
            desc_len = params[1]
            desc     = params[2:2 + desc_len].decode('utf-8', errors='replace')
            simulator.set_port_desc(port, desc)
            decoded = _format_command_response(kind, opcode, data)
            _push(CHAR_RESPONSE_UUID, decoded.encode())
            _audit(opcode, data)
            asyncio.ensure_future(_send_port_config(port))

        elif opcode == 0x26 and params and simulator is not None:
            id_len   = params[0]
            alarm_id = params[1:1 + id_len].decode('utf-8', errors='replace')
            simulator.acknowledge_alarm(alarm_id)
            decoded = _format_command_response(kind, opcode, data)
            _push(CHAR_RESPONSE_UUID, decoded.encode())
            _audit(opcode, data)

        elif opcode == 0x27:
            asyncio.ensure_future(_send_psu_snapshot())
            decoded = "Triggered PSU snapshot"

        elif opcode == 0x28 and params:
            port_req = params[0]
            if port_req == 0xFF:
                log.info("READ  All port configs requested — client opened port overview")
            else:
                log.info(f"READ  Port {port_req} config requested — client opened port detail")
            asyncio.ensure_future(_send_port_config(port_req))
            decoded = f"Port config request port={port_req}"

        elif opcode == 0x29:
            # Read LEDs directly from live simulator._leds (not the stale snapshot)
            # so set_led mutations are visible immediately without waiting for the
            # next sensor tick.
            if simulator is not None:
                entries = sorted(simulator._leds.items())
                led_buf = bytearray([len(entries)])
                for _p, _led in entries:
                    led_buf += bytes([_p,
                                      COLOR_CODE.get(_led.color, 0),
                                      BLINK_CODE.get(_led.blink, 0)])
                _push(CHAR_LED_UUID, led_buf)
            decoded = "LED states pushed"

        elif opcode == 0x04:
            asyncio.ensure_future(_send_stats_snapshot())
            decoded = "Trigger stats snapshot"

        elif opcode == 0x32:
            # Client is cleanly disconnecting — remove device from BlueZ immediately
            # so iOS doesn't keep a stale connection open in Bluetooth settings.
            log.info("CLIENT  sent disconnect notification — removing device from BlueZ")
            if _tui_state is not None and _tui_state.registry._active:
                mac = _tui_state.registry._active
                _tui_state.registry.disconnected(mac)
                _reset_auth()
                _tui_state.invalidate()
            asyncio.ensure_future(_force_remove_connected())
            # Don't log 0x32 as a command — the client is already gone
            continue

        elif opcode == 0x33 and simulator is not None:
            simulator.demo_reset()
            log.info("DEMO_RESET  all ports unsplit, admin-up, counters zeroed, alarms cleared")
            asyncio.ensure_future(_push_demo_reset_state())
            decoded = "Demo reset complete"
            _push(CHAR_RESPONSE_UUID, decoded.encode())
            _audit(opcode, data)

        elif opcode == 0x30 and params:
            msg_len = params[0]
            msg     = params[1:1 + msg_len].decode('utf-8', errors='replace')
            log.info(f"CLIENT  {msg}")
            decoded = f"Log event: {msg}"

        elif opcode == 0x31 and len(params) >= 2 and simulator is not None:
            port      = params[0]
            if port not in simulator._port_configs:
                decoded = f"Split failed: port {port} does not exist"
                _push(CHAR_RESPONSE_UUID, decoded.encode())
                break
            mode_code = params[1]
            # Wire encoding: 0=none, 1=first-breakout, 2=second-breakout.
            # Odd ports are 400G (4x100G / 2x200G); even ports are 800G (4x200G / 2x400G).
            _port_modes = (["none", "4x100G", "2x200G"]
                           if port % 2 == 1
                           else ["none", "4x200G", "2x400G"])
            if mode_code >= len(_port_modes):
                decoded = f"Split failed: invalid mode code {mode_code} for port {port}"
                _push(CHAR_RESPONSE_UUID, decoded.encode())
                break
            mode      = _port_modes[mode_code]
            success   = simulator.set_split_mode(port, mode)
            if success:
                log.info(f"SPLIT  port={port}  mode={mode}")
                async def _push_split_configs(p):
                    await asyncio.sleep(0.05)
                    await _send_port_config(p)
                    # Push configs for all sub-ports of this parent
                    for sub, sub_cfg in list(simulator._port_configs.items()):
                        if sub_cfg.parent_port == p:
                            await _send_port_config(sub)
                asyncio.ensure_future(_push_split_configs(port))
                # Push physical port count only (sub-ports are not counted)
                char  = server.get_characteristic(CHAR_PORTCNT_UUID)
                if char is not None:
                    char.value = bytearray(struct.pack('B', simulator.num_ports))
                    server.update_value(SERVICE_UUID, CHAR_PORTCNT_UUID)
                decoded = f"Split port={port} mode={mode}"
                _push(CHAR_RESPONSE_UUID, decoded.encode())
                _audit(opcode, data)
            else:
                decoded = f"Split failed: port={port} not split-capable"
                _push(CHAR_RESPONSE_UUID, decoded.encode())
                _audit(opcode, data, result="failed")

        else:
            decoded = _format_command_response(kind, opcode, data)
            _push(CHAR_RESPONSE_UUID, bytearray(decoded.encode("utf-8")))
            _audit(opcode, data)

        if _tui_state is not None:
            _tui_state.registry.record_command(kind, opcode, decoded)
            _tui_state.invalidate()


async def _push_demo_reset_state():
    """Push all updated state to the client after a demo reset."""
    if simulator is None or latest_snapshot is None:
        return
    await asyncio.sleep(0.05)
    # Push all physical port configs
    for port in range(1, simulator.num_ports + 1):
        await _send_port_config(port)
        await asyncio.sleep(0.01)
    # Push updated LED states
    entries = sorted(simulator._leds.items())
    led_buf = bytearray([len(entries)])
    for _p, _led in entries:
        led_buf += bytes([_p, COLOR_CODE.get(_led.color, 0), BLINK_CODE.get(_led.blink, 0)])
    _push(CHAR_LED_UUID, led_buf)
    # Push cleared alarms
    _push(CHAR_ALARMS_UUID, b'\x00')


async def _send_psu_snapshot():
    if latest_snapshot is None:
        _push(CHAR_RESPONSE_UUID, b"error: no snapshot yet")
        return
    _push(CHAR_PSU_UUID, SwitchSensorSimulator.pack_psus(latest_snapshot))
    _push(CHAR_FAN_UUID, SwitchSensorSimulator.pack_fans(latest_snapshot))
    _push(CHAR_RESPONSE_UUID, f"Health snapshot sent ({len(latest_snapshot.psus)} PSUs, {len(latest_snapshot.fans)} fans)".encode())


# ── Auth response loop ────────────────────────────────────────────────────────

async def auth_response_loop():
    global _authenticated
    while True:
        event = await auth_queue.get()
        result = event[0]
        if result == "ok":
            authed_user = event[1] if len(event) > 1 else _auth_username
            _authenticated = True
            _push(CHAR_AUTH_STATUS_UUID, bytearray([0x01]))
            log.info(f"AUTH  ✓ user '{authed_user}' authenticated")
        else:
            _authenticated = False
            log.warning("AUTH  ✗ wrong credentials — new nonce issued")
            _push(CHAR_AUTH_STATUS_UUID, bytearray([0x00]))
            _new_nonce()
        if _tui_state is not None:
            _tui_state.invalidate()


# ── Time-left ─────────────────────────────────────────────────────────────────

def _seconds_left() -> int:
    if session_end_time == 0.0:
        return 0
    remaining = session_end_time - asyncio.get_event_loop().time()
    return max(0, int(remaining))


async def countdown_loop(duration: float, tick: float = 1.0):
    global session_end_time
    session_end_time = asyncio.get_event_loop().time() + duration
    while True:
        secs = _seconds_left()
        _push(CHAR_TIMELEFT_UUID, bytearray(struct.pack('>I', secs)))
        if _tui_state is not None:
            _tui_state.secs_left = secs
            _tui_state.invalidate()
        if secs == 0:
            log.info("Session expired")
            if _shutdown_event is not None:
                _shutdown_event.set()
            return
        await asyncio.sleep(tick)


# ── Adapter security ──────────────────────────────────────────────────────────

async def _configure_adapter_security(name: str = "SwitchMon"):
    try:
        from dbus_fast.aio import MessageBus
        from dbus_fast.constants import BusType
        from dbus_fast import Variant
    except ImportError:
        return

    try:
        bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
        root_intr  = await bus.introspect('org.bluez', '/')
        root_proxy = bus.get_proxy_object('org.bluez', '/', root_intr)
        om         = root_proxy.get_interface('org.freedesktop.DBus.ObjectManager')
        objs       = await om.call_get_managed_objects()

        adapter_path: Optional[str] = None
        for path, ifaces in objs.items():
            if 'org.bluez.Adapter1' in ifaces:
                adapter_path = path
                break

        if adapter_path is None:
            await bus.disconnect()
            return

        a_intr  = await bus.introspect('org.bluez', adapter_path)
        a_proxy = bus.get_proxy_object('org.bluez', adapter_path, a_intr)
        a_props = a_proxy.get_interface('org.freedesktop.DBus.Properties')

        # Set the adapter alias so iOS sees the correct device name
        # (iOS trusts the GAP adapter name over the advertisement name)
        await a_props.call_set('org.bluez.Adapter1', 'Alias', Variant('s', name))

        # Disable pairing so no bond is ever stored on the client side
        await a_props.call_set('org.bluez.Adapter1', 'Pairable', Variant('b', False))

        # Remove all previously known devices so iOS doesn't reuse stale keys
        a_iface = a_proxy.get_interface('org.bluez.Adapter1')
        for path, ifaces in objs.items():
            if 'org.bluez.Device1' in ifaces and path.startswith(adapter_path + '/dev_'):
                try:
                    await a_iface.call_remove_device(path)
                except Exception:
                    pass

        # Set a random private address so iOS sees a fresh device each session.
        # This prevents CoreBluetooth from matching the peripheral to a cached
        # entry in the phone's Bluetooth list, eliminating the stale-pairing
        # connection failure entirely.
        try:
            import _os as os_mod
            rand_bytes = _os.urandom(6)
            # Mark as random static address (top 2 bits = 11)
            rand_bytes = bytearray(rand_bytes)
            rand_bytes[5] = (rand_bytes[5] | 0xC0)
            rand_addr = ':'.join(f'{b:02X}' for b in reversed(rand_bytes))
            import subprocess as _sub
            hci = adapter_path.split('/')[-1]   # e.g. "hci0"
            _sub.run(['hcitool', '-i', hci, 'cmd', '0x08', '0x0005',
                      *[f'0x{b:02X}' for b in rand_bytes]],
                     capture_output=True)
            log.info(f"Adapter random address set: {rand_addr}")
        except Exception as exc:
            log.debug(f"Random address: {exc}")

        await bus.disconnect()
    except Exception as exc:
        log.warning(f"Adapter security config failed: {exc}")


# ── Server setup ──────────────────────────────────────────────────────────────

async def setup_server(num_ports: int, name: str = "SwitchMon") -> BlessServer:
    srv = BlessServer(name=name, loop=asyncio.get_event_loop())
    srv.read_request_func  = read_request
    srv.write_request_func = write_request

    await srv.add_new_service(SERVICE_UUID)

    async def add_char(uuid, props, perms, initial_value, label):
        await srv.add_new_characteristic(SERVICE_UUID, uuid, props, initial_value, perms)
        log.info(f"  + {label:20s} {uuid}")

    RN  = GATTCharacteristicProperties.read | GATTCharacteristicProperties.notify
    R   = GATTCharacteristicProperties.read
    W   = GATTCharacteristicProperties.write
    WNR = GATTCharacteristicProperties.write_without_response
    RP  = GATTAttributePermissions.readable
    WP  = GATTAttributePermissions.writeable

    await add_char(CHAR_CPU_UUID,            RN,      RP,      bytearray(b'\x00'),                  "CPU")
    await add_char(CHAR_MEM_UUID,            RN,      RP,      bytearray(b'\x00'),                  "Memory")
    await add_char(CHAR_TEMP_UUID,           RN,      RP,      bytearray(8),                        "Temps")
    await add_char(CHAR_PORTS_UUID,          RN,      RP,      bytearray(math.ceil(num_ports / 8)), "Port status")
    await add_char(CHAR_PORTCNT_UUID,        R,       RP,      bytearray([num_ports]),              "Port count")
    await add_char(CHAR_CMD_WNR_UUID,        WNR,     WP,      None,                               "Cmd (no-resp)")
    await add_char(CHAR_CMD_WR_UUID,         W,       WP,      None,                               "Cmd (wr-resp)")
    await add_char(CHAR_RESPONSE_UUID,       RN,      RP,      bytearray(b''),                      "Response")
    await add_char(CHAR_TIMELEFT_UUID,       RN,      RP,      bytearray(4),                        "Time left")
    await add_char(CHAR_BULK_UUID,           RN,      RP,      bytearray(b''),                      "Bulk data")
    await add_char(CHAR_KEEPALIVE_UUID,      RN | W,  RP | WP, bytearray(b'\x00'),                 "Keepalive")
    await add_char(CHAR_AUTH_CHALLENGE_UUID, RN,      RP,      bytearray(16),                       "Auth challenge")
    await add_char(CHAR_AUTH_RESPONSE_UUID,  W | WNR, WP,      None,                               "Auth response")
    await add_char(CHAR_AUTH_STATUS_UUID,    RN,      RP,      bytearray(b'\x00'),                  "Auth status")
    await add_char(CHAR_PSU_UUID,            RN,      RP,      bytearray(b'\x00'),                  "PSU status")
    await add_char(CHAR_ALARMS_UUID,         RN,      RP,      bytearray(b'\x00'),                  "Alarms")
    await add_char(CHAR_PORT_CFG_UUID,       RN,      RP,      bytearray(b'\x00'),                  "Port config")
    await add_char(CHAR_LED_UUID,            RN,      RP,      bytearray(b'\x00'),                  "LED states")
    await add_char(CHAR_FAN_UUID,            RN,      RP,      bytearray(b'\x00'),                  "Fan states")
    await add_char(CHAR_CLIENT_NAME_UUID,    W | WNR, WP,      None,                               "Client name")

    return srv


# ── Main ──────────────────────────────────────────────────────────────────────

BLE_NAME_MAX = 26   # BLE advertisement packet limit (~31 bytes - flags - UUID overhead)


async def main(num_ports: int, interval: float, duration: float,
               users: dict = None, name: str = "SwitchMon",
               headless: bool = False, state_publisher=None,
               auth_bypass: bool = False, backend: str = "auto",
               audit_log=None):
    global server, simulator, configured_num_ports
    global cmd_queue, auth_queue, _shutdown_event, _tui_state, _users
    global _ble_device_name, _HEADLESS, _auth_bypass, _audit_log

    _HEADLESS = headless

    # Enforce BLE advertisement name length limit
    if len(name) > BLE_NAME_MAX:
        name = name[:BLE_NAME_MAX]
        print(f"Warning: device name truncated to {BLE_NAME_MAX} chars: '{name}'")
    _ble_device_name = name

    _users                = users or {}
    _auth_bypass          = auth_bypass
    _audit_log            = audit_log
    configured_num_ports  = num_ports
    cmd_queue             = asyncio.Queue()
    auth_queue            = asyncio.Queue()
    _shutdown_event       = asyncio.Event()

    state      = PeripheralState(num_ports=num_ports, interval=interval)
    _tui_state = state
    if duration > 0:
        state.secs_left = int(duration)

    app_ref       = [None]
    state.app_ref = app_ref

    if headless or not _HAS_TUI:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)-5s %(message)s", datefmt="%Y-%m-%dT%H:%M:%S"))
        logging.basicConfig(level=logging.INFO, handlers=[handler])
    else:
        tui_handler = TUILogHandler(state.log_lines, state.tick_lines, app_ref)
        tui_handler.setFormatter(logging.Formatter("%(levelname)-5s %(message)s"))
        logging.basicConfig(level=logging.INFO, handlers=[tui_handler])

    from mobile_management.backend import create_backend
    simulator = create_backend(kind=backend, num_ports=num_ports)

    try:
        await _configure_adapter_security(name=name)
        server = await setup_server(num_ports, name=name)
        await server.start()
        _new_nonce()
        log.info(f"Advertising as '{name}'  {num_ports} ports  {interval}s interval"
                 f"  mode={'headless' if headless else 'TUI'}")
    except Exception as exc:
        log.warning(f"BLE server failed to start: {exc}")
        log.info("Running in no-adapter mode — sensor loop and state publisher active, BLE disabled")
        server = None

    if _auth_bypass:
        log.info("Auth: BYPASS — full R/W access without authentication")
    elif _users:
        log.info(f"Auth: ENABLED ({len(_users)} user{'s' if len(_users) != 1 else ''}: {', '.join(_users.keys())})")
    else:
        log.info("Auth: VIEW-ONLY — no users configured, controls locked")
    log.info(f"Session: {'unlimited' if duration == 0 else f'{int(duration)}s'}")

    sensor_task    = asyncio.create_task(sensor_loop(simulator, interval))
    command_task   = asyncio.create_task(command_loop())
    auth_task      = asyncio.create_task(auth_response_loop())
    monitor_task   = asyncio.create_task(_monitor_connections(state.registry, state))
    keepalive_task = asyncio.create_task(keepalive_loop())
    stale_task     = asyncio.create_task(stale_client_checker(state.registry, state))
    countdown_task = asyncio.create_task(countdown_loop(duration)) if duration > 0 else None
    publish_task   = asyncio.create_task(state_publisher.run(state)) if state_publisher else None

    if headless or not _HAS_TUI:
        # Headless: block on shutdown event (systemd sends SIGTERM → event is set)
        import signal
        loop = asyncio.get_event_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, _shutdown_event.set)
        try:
            await _shutdown_event.wait()
        except (asyncio.CancelledError, KeyboardInterrupt):
            pass
    else:
        app        = _build_app(state)
        app_ref[0] = app

        async def _watch_shutdown():
            await _shutdown_event.wait()
            app.exit()

        shutdown_watcher = asyncio.create_task(_watch_shutdown())

        try:
            await app.run_async()
        except (asyncio.CancelledError, KeyboardInterrupt):
            pass

    _shutdown_event.set()
    tasks = [sensor_task, command_task, auth_task, monitor_task,
             keepalive_task, stale_task]
    if not headless and _HAS_TUI:
        tasks.append(shutdown_watcher)
    if countdown_task is not None:
        tasks.append(countdown_task)
    if publish_task is not None:
        tasks.append(publish_task)
    for task in tasks:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
    if server is not None:
        await server.stop()
    log.info("SwitchMon stopped.")


def _parse_users(raw: str) -> dict:
    """Parse 'user1:pass1,user2:pass2' into {user1: pass1, user2: pass2}."""
    result = {}
    for pair in raw.split(","):
        pair = pair.strip()
        if ":" in pair:
            username, password = pair.split(":", 1)
            username = username.strip()
            password = password.strip()
            if username:
                result[username] = password
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="BLE switch monitor peripheral")
    parser.add_argument("--ports",    type=int,   default=48)
    parser.add_argument("--interval", type=float, default=2.0)
    parser.add_argument("--duration", type=float, default=0)
    parser.add_argument("--users",    type=str,   default="",
                        help="Comma-separated user:pass pairs, e.g. admin:secret,viewer:readonly")
    parser.add_argument("--name",     type=str,   default="SwitchMon",
                        help=f"Advertised BLE device name (max {BLE_NAME_MAX} chars, default: SwitchMon)")
    parser.add_argument("--headless", action="store_true",
                        help="Run without TUI (for systemd / daemon operation)")
    args = parser.parse_args()
    parsed_users = _parse_users(args.users)

    try:
        asyncio.run(main(args.ports, args.interval, args.duration,
                         parsed_users, args.name, headless=args.headless))
    except KeyboardInterrupt:
        pass
