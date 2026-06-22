"""
test_ble_protocol.py — In-process bridge tests for the BLE peripheral protocol

Replaces the bless/bleak transport with in-memory queues so the full
command-dispatch, auth, and notification logic runs without hardware,
root access, or a running bluetoothd.

Architecture
────────────
  ┌─────────────────────────────────────┐
  │  FakeBleakClient                    │   ← what the client code sees
  │  read_gatt_char / write_gatt_char   │
  │  start_notify / stop_notify         │
  └──────────────┬──────────────────────┘
                 │  calls
  ┌──────────────▼──────────────────────┐
  │  GATTBridge  (in-memory transport)  │
  │  _values dict  +  _subscribers dict │
  └──────────────┬──────────────────────┘
                 │  calls / notifies
  ┌──────────────▼──────────────────────┐
  │  FakeBlessServer                    │   ← P.server global
  │  get_characteristic / update_value  │
  └──────────────┬──────────────────────┘
                 │  calls
  ┌──────────────▼──────────────────────┐
  │  ble_switch_peripheral globals      │
  │  read_request / write_request       │
  │  command_loop / auth_response_loop  │
  └─────────────────────────────────────┘

Run:
    pytest test_ble_protocol.py -v

Requires:  pytest>=8  pytest-asyncio>=0.23
"""

import asyncio
import os
import struct
import sys
from typing import Callable, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

# ── Stub Linux-only / platform-specific libraries ─────────────────────────────
# bless (BLE GATT server) requires BlueZ; dbus_fast requires a D-Bus daemon.
# dbus_fast.aio is stubbed with AsyncMock so awaiting its results never hangs.
_dbus_aio_stub = MagicMock()
_dbus_aio_stub.MessageBus = MagicMock(return_value=AsyncMock())

for _lib, _stub in [
    ('bless',               MagicMock()),
    ('dbus_fast',           MagicMock()),
    ('dbus_fast.aio',       _dbus_aio_stub),
    ('dbus_fast.constants', MagicMock()),
]:
    sys.modules.setdefault(_lib, _stub)

import mobile_management.peripheral as P          # noqa: E402 — must come after mocking
from mobile_management.sensor_simulator import SwitchSensorSimulator  # noqa: E402


# ═══════════════════════════════════════════════════════════════════════════════
# Bridge infrastructure
# ═══════════════════════════════════════════════════════════════════════════════

class GATTBridge:
    """
    In-memory GATT transport shared between the fake server and fake client.

    The peripheral calls set_value() / notify() (via FakeBlessServer).
    The client calls get_value() / subscribe() (via FakeBleakClient).
    """

    def __init__(self):
        self._values:      Dict[str, bytearray]      = {}
        self._subscribers: Dict[str, List[Callable]] = {}

    # ── Peripheral side ───────────────────────────────────────────────────────

    def set_value(self, uuid: str, value: bytearray):
        self._values[uuid] = bytearray(value)

    def notify(self, uuid: str, value: bytearray):
        """Called by FakeBlessServer.update_value; dispatches to client callbacks."""
        self._values[uuid] = bytearray(value)
        for cb in list(self._subscribers.get(uuid, [])):
            cb(None, bytearray(value))

    # ── Client side ───────────────────────────────────────────────────────────

    def get_value(self, uuid: str) -> bytearray:
        return bytearray(self._values.get(uuid, b''))

    def subscribe(self, uuid: str, callback: Callable):
        self._subscribers.setdefault(uuid, []).append(callback)

    def unsubscribe(self, uuid: str, callback: Optional[Callable]):
        subs = self._subscribers.get(uuid, [])
        if callback and callback in subs:
            subs.remove(callback)


class _FakeChar:
    """
    Mimics BlessGATTCharacteristic.
    Used as a routing key (read/write_request read .uuid) and as a mutable
    value holder (_push sets .value then calls server.update_value).
    """

    def __init__(self, uuid: str, bridge: GATTBridge):
        self.uuid   = uuid
        self._bridge = bridge

    @property
    def value(self) -> bytearray:
        return self._bridge.get_value(self.uuid)

    @value.setter
    def value(self, v):
        self._bridge.set_value(self.uuid, bytearray(v))


class FakeBlessServer:
    """
    Drop-in for BlessServer injected as P.server.
    _push() calls server.get_characteristic(uuid) → sets .value →
    server.update_value() → bridge.notify() → subscriber callbacks.
    """

    def __init__(self, bridge: GATTBridge):
        self._bridge = bridge
        self._chars: Dict[str, _FakeChar] = {}

    def get_characteristic(self, uuid: str) -> _FakeChar:
        if uuid not in self._chars:
            self._chars[uuid] = _FakeChar(uuid, self._bridge)
        return self._chars[uuid]

    def update_value(self, _service_uuid: str, char_uuid: str):
        value = self._bridge.get_value(char_uuid)
        self._bridge.notify(char_uuid, value)


class FakeBleakClient:
    """
    Drop-in for BleakClient.

    Reads  → calls P.read_request (which reads module globals).
    Writes → calls P.write_request (which feeds cmd_queue / auth_queue).
    Notifications → registered as bridge subscribers.
    """

    def __init__(self, bridge: GATTBridge):
        self._bridge = bridge
        self._notify_cbs: Dict[str, Callable] = {}

    async def read_gatt_char(self, uuid: str) -> bytearray:
        char = _FakeChar(uuid, self._bridge)
        return P.read_request(char)

    async def write_gatt_char(self, uuid: str, data: bytearray, response: bool = False):
        char = _FakeChar(uuid, self._bridge)
        P.write_request(char, data)

    async def start_notify(self, uuid: str, callback: Callable):
        self._notify_cbs[uuid] = callback
        self._bridge.subscribe(uuid, callback)

    async def stop_notify(self, uuid: str):
        cb = self._notify_cbs.pop(uuid, None)
        self._bridge.unsubscribe(uuid, cb)


# ═══════════════════════════════════════════════════════════════════════════════
# Fixture
# ═══════════════════════════════════════════════════════════════════════════════

@pytest_asyncio.fixture
async def ble_session():
    """
    Injects a fresh in-memory BLE session into the peripheral module globals.
    Yields (bridge, client, simulator). Tears down between tests.
    """
    bridge = GATTBridge()
    sim    = SwitchSensorSimulator(num_ports=8)
    snap   = sim.read()

    P.server               = FakeBlessServer(bridge)
    P.simulator            = sim
    P.latest_snapshot      = snap
    P.cmd_queue            = asyncio.Queue()
    P.auth_queue           = asyncio.Queue()
    P._users               = {"testuser": "testpass"}  # multi-user auth
    P._auth_username       = ""
    P._authenticated       = False
    P._auth_bypass         = False
    P._audit_log           = None
    P._auth_nonce          = os.urandom(16)
    P._tui_state           = None
    P.configured_num_ports = sim.num_ports
    P.session_end_time     = 0.0

    yield bridge, FakeBleakClient(bridge), sim

    # Reset all injected globals so tests don't bleed into each other
    P.server          = None
    P.simulator       = None
    P.latest_snapshot = None
    P.cmd_queue       = None
    P.auth_queue      = None
    P._authenticated  = False
    P._auth_bypass    = False
    P._audit_log      = None
    P._users          = {}
    P._auth_username  = ""
    P._tui_state      = None


# ═══════════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════════

async def _run_cmd(client: FakeBleakClient, uuid: str, payload,
                   settle: float = 0.03):
    """
    Write a command to the peripheral and let command_loop process it.

    Starts command_loop as a task, writes the payload, waits `settle` seconds
    for the loop and any ensure_future'd coroutines to run, then cancels.
    """
    cmd_task = asyncio.create_task(P.command_loop())
    await client.write_gatt_char(uuid, bytearray(payload))
    await asyncio.sleep(settle)
    cmd_task.cancel()
    try:
        await cmd_task
    except asyncio.CancelledError:
        pass


async def _do_auth(client: FakeBleakClient, passcode: str = "testpass") -> bool:
    """
    Perform the full HMAC challenge-response auth against the peripheral.
    Runs auth_response_loop as a background task so notifications fire.
    """
    import hmac as _hmac
    import hashlib

    auth_task = asyncio.create_task(P.auth_response_loop())

    try:
        status = await client.read_gatt_char(P.CHAR_AUTH_STATUS_UUID)
        if status and status[0] == 0x01:
            return True

        # Send the username so the peripheral knows which user's password to look up.
        await client.write_gatt_char(P.CHAR_CLIENT_NAME_UUID, b"testuser")

        nonce    = bytes(await client.read_gatt_char(P.CHAR_AUTH_CHALLENGE_UUID))
        response = _hmac.new(passcode.encode(), nonce, hashlib.sha256).digest()

        done   = asyncio.Event()
        result = [False]

        def _on_status(_sender, data):
            result[0] = bool(data and data[0] == 0x01)
            done.set()

        await client.start_notify(P.CHAR_AUTH_STATUS_UUID, _on_status)
        await client.write_gatt_char(P.CHAR_AUTH_RESPONSE_UUID, bytearray(response))
        try:
            await asyncio.wait_for(done.wait(), timeout=2.0)
        except asyncio.TimeoutError:
            pass
        finally:
            await client.stop_notify(P.CHAR_AUTH_STATUS_UUID)

        return result[0]
    finally:
        auth_task.cancel()
        try:
            await auth_task
        except asyncio.CancelledError:
            pass


# ═══════════════════════════════════════════════════════════════════════════════
# Auth tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestAuth:

    async def test_correct_passcode_authenticates(self, ble_session):
        _, client, _ = ble_session
        assert await _do_auth(client, "testpass") is True
        assert P._authenticated is True

    async def test_wrong_passcode_rejected(self, ble_session):
        _, client, _ = ble_session
        assert await _do_auth(client, "wrongpass") is False
        assert P._authenticated is False

    async def test_wrong_passcode_rotates_nonce(self, ble_session):
        _, client, _ = ble_session
        old_nonce = bytes(P._auth_nonce)
        await _do_auth(client, "wrongpass")
        assert bytes(P._auth_nonce) != old_nonce

    async def test_no_users_returns_view_only(self, ble_session):
        _, client, _ = ble_session
        P._users = {}  # no users configured → view-only mode
        status = await client.read_gatt_char(P.CHAR_AUTH_STATUS_UUID)
        assert status[0] == 0x02

    async def test_bypass_returns_full_access(self, ble_session):
        _, client, _ = ble_session
        P._users = {}
        P._auth_bypass = True
        status = await client.read_gatt_char(P.CHAR_AUTH_STATUS_UUID)
        assert status[0] == 0x01

    async def test_auth_status_char_readable_pre_auth(self, ble_session):
        _, client, _ = ble_session
        result = await client.read_gatt_char(P.CHAR_AUTH_STATUS_UUID)
        assert len(result) == 1
        assert result[0] == 0x00  # not yet authenticated

    async def test_challenge_char_readable_pre_auth(self, ble_session):
        _, client, _ = ble_session
        nonce = await client.read_gatt_char(P.CHAR_AUTH_CHALLENGE_UUID)
        assert len(nonce) == 16

    async def test_portcnt_char_readable_pre_auth(self, ble_session):
        """Port count is in AUTH_OPEN_UUIDS — always accessible."""
        _, client, sim = ble_session
        count = await client.read_gatt_char(P.CHAR_PORTCNT_UUID)
        assert count[0] == sim.num_ports

    async def test_second_auth_with_same_nonce_succeeds_before_rotation(self, ble_session):
        """Nonce only rotates on wrong passcode; correct passcode keeps the nonce."""
        _, client, _ = ble_session
        nonce_before = bytes(P._auth_nonce)
        await _do_auth(client, "testpass")
        # Nonce should NOT have rotated on a correct auth
        assert bytes(P._auth_nonce) == nonce_before


# ═══════════════════════════════════════════════════════════════════════════════
# Pre-auth blocking
# ═══════════════════════════════════════════════════════════════════════════════

class TestPreAuthBlocking:

    async def test_cmd_wr_write_blocked_before_auth(self, ble_session):
        _, client, _ = ble_session
        await client.write_gatt_char(P.CHAR_CMD_WR_UUID, bytearray([0x22, 1]))
        assert P.cmd_queue.empty()

    async def test_mutation_via_wnr_blocked_before_auth(self, ble_session):
        """Mutation opcode (0x21 = port admin up) via WNR is blocked pre-auth."""
        _, client, _ = ble_session
        await client.write_gatt_char(P.CHAR_CMD_WNR_UUID, bytearray([0x21, 1]))
        assert P.cmd_queue.empty()

    async def test_read_request_opcode_allowed_before_auth(self, ble_session):
        """Read-only opcode (0x04 = stats snapshot) passes through in view-only mode."""
        _, client, _ = ble_session
        await client.write_gatt_char(P.CHAR_CMD_WNR_UUID, bytearray([0x04]))
        assert not P.cmd_queue.empty()

    async def test_telemetry_reads_allowed_in_view_only(self, ble_session):
        """View-only mode (users configured, not authenticated) still allows reads."""
        _, client, _ = ble_session
        result = await client.read_gatt_char(P.CHAR_CPU_UUID)
        assert len(result) == 1
        assert 0 <= result[0] <= 100

    async def test_0x32_disconnect_bypasses_auth_check(self, ble_session):
        """Client disconnect must always reach cmd_queue regardless of auth state."""
        _, client, _ = ble_session
        await client.write_gatt_char(P.CHAR_CMD_WR_UUID, bytearray([0x32]))
        assert not P.cmd_queue.empty()
        kind, data = P.cmd_queue.get_nowait()
        assert data[0] == 0x32

    async def test_keepalive_write_never_enters_cmd_queue(self, ble_session):
        _, client, _ = ble_session
        await client.write_gatt_char(P.CHAR_KEEPALIVE_UUID, bytearray([0x05]))
        assert P.cmd_queue.empty()


# ═══════════════════════════════════════════════════════════════════════════════
# Authenticated reads
# ═══════════════════════════════════════════════════════════════════════════════

class TestAuthenticatedReads:

    @pytest.fixture(autouse=True)
    def set_authenticated(self, ble_session):
        P._authenticated = True

    async def test_cpu_read_is_single_byte_0_to_100(self, ble_session):
        _, client, _ = ble_session
        result = await client.read_gatt_char(P.CHAR_CPU_UUID)
        assert len(result) == 1
        assert 0 <= result[0] <= 100

    async def test_memory_read_is_single_byte_0_to_100(self, ble_session):
        _, client, _ = ble_session
        result = await client.read_gatt_char(P.CHAR_MEM_UUID)
        assert len(result) == 1
        assert 0 <= result[0] <= 100

    async def test_temperature_read_is_8_bytes(self, ble_session):
        _, client, _ = ble_session
        result = await client.read_gatt_char(P.CHAR_TEMP_UUID)
        assert len(result) == 8

    async def test_psu_read_header_matches_psu_count(self, ble_session):
        _, client, sim = ble_session
        result = await client.read_gatt_char(P.CHAR_PSU_UUID)
        assert result[0] == sim.NUM_PSUS
        assert len(result) == 1 + sim.NUM_PSUS * 14  # 14 bytes/PSU

    async def test_alarms_read_reflects_triggered_alarm(self, ble_session):
        _, client, sim = ble_session
        sim.trigger_alarm("psu", "critical", "PSU overtemp")
        P.latest_snapshot = sim.read()
        result = await client.read_gatt_char(P.CHAR_ALARMS_UUID)
        assert result[0] == 1

    async def test_led_read_header_covers_system_plus_port_leds(self, ble_session):
        _, client, sim = ble_session
        result = await client.read_gatt_char(P.CHAR_LED_UUID)
        # System LED (port 0) + one LED per port
        assert result[0] == sim.num_ports + 1

    async def test_time_left_read_when_unlimited(self, ble_session):
        _, client, _ = ble_session
        # session_end_time = 0.0 → _seconds_left() returns 0
        result = await client.read_gatt_char(P.CHAR_TIMELEFT_UUID)
        assert len(result) == 4


# ═══════════════════════════════════════════════════════════════════════════════
# Command dispatch — simulator mutations
# ═══════════════════════════════════════════════════════════════════════════════

class TestCommandDispatch:

    @pytest.fixture(autouse=True)
    def set_authenticated(self, ble_session):
        P._authenticated = True

    # ── Port admin ────────────────────────────────────────────────────────────

    async def test_0x22_admin_down_mutates_simulator(self, ble_session):
        _, client, sim = ble_session
        await _run_cmd(client, P.CHAR_CMD_WR_UUID, [0x22, 1])
        assert sim._port_configs[1].admin_up is False

    async def test_0x21_admin_up_mutates_simulator(self, ble_session):
        _, client, sim = ble_session
        sim.set_port_admin(1, False)
        await _run_cmd(client, P.CHAR_CMD_WR_UUID, [0x21, 1])
        assert sim._port_configs[1].admin_up is True

    async def test_admin_down_sets_led_amber(self, ble_session):
        _, client, sim = ble_session
        await _run_cmd(client, P.CHAR_CMD_WR_UUID, [0x22, 1])
        assert sim._leds[1].color == "amber"

    # ── Speed / MTU / description ─────────────────────────────────────────────

    async def test_0x23_speed_set_mutates_simulator(self, ble_session):
        _, client, sim = ble_session
        # speed code 0 → 1000 Mbps
        await _run_cmd(client, P.CHAR_CMD_WR_UUID, [0x23, 1, 0])
        assert sim._port_configs[1].speed == 1000

    async def test_0x24_mtu_set_mutates_simulator(self, ble_session):
        _, client, sim = ble_session
        mtu_bytes = list(struct.pack('>H', 9216))
        await _run_cmd(client, P.CHAR_CMD_WR_UUID, [0x24, 1] + mtu_bytes)
        assert sim._port_configs[1].mtu == 9216

    async def test_0x25_desc_set_mutates_simulator(self, ble_session):
        _, client, sim = ble_session
        desc    = b"spine-link"
        payload = [0x25, 1, len(desc)] + list(desc)
        await _run_cmd(client, P.CHAR_CMD_WR_UUID, payload, settle=0.05)
        assert sim._port_configs[1].description == "spine-link"

    async def test_0x25_desc_set_empty_string(self, ble_session):
        _, client, sim = ble_session
        payload = [0x25, 2, 0]  # port 2, length 0, no desc bytes
        await _run_cmd(client, P.CHAR_CMD_WR_UUID, payload, settle=0.05)
        assert sim._port_configs[2].description == ""

    # ── LED ───────────────────────────────────────────────────────────────────

    async def test_0x20_led_set_mutates_simulator(self, ble_session):
        _, client, sim = ble_session
        # port=1, color=blue(3), blink=slow(1)
        await _run_cmd(client, P.CHAR_CMD_WR_UUID, [0x20, 1, 3, 1])
        assert sim._leds[1].color == "blue"
        assert sim._leds[1].blink == "slow"

    async def test_0x20_led_set_system_led_port_0(self, ble_session):
        _, client, sim = ble_session
        await _run_cmd(client, P.CHAR_CMD_WR_UUID, [0x20, 0, 4, 0])  # amber, solid
        assert sim._leds[0].color == "amber"

    # ── Alarm ack ─────────────────────────────────────────────────────────────

    async def test_0x26_alarm_ack_marks_acknowledged(self, ble_session):
        _, client, sim = ble_session
        alarm_id = sim.trigger_alarm("psu", "critical", "PSU fail")
        P.latest_snapshot = sim.read()
        id_bytes = alarm_id.encode()
        payload  = [0x26, len(id_bytes)] + list(id_bytes)
        await _run_cmd(client, P.CHAR_CMD_WR_UUID, payload)
        alarm = next(a for a in sim._alarms if a.alarm_id == alarm_id)
        assert alarm.acknowledged is True

    async def test_0x26_alarm_ack_unknown_id_is_noop(self, ble_session):
        _, client, sim = ble_session
        sim.trigger_alarm("fan", "major", "Fan fail")
        bad_id   = b"doesnotexist"
        payload  = [0x26, len(bad_id)] + list(bad_id)
        await _run_cmd(client, P.CHAR_CMD_WR_UUID, payload)
        assert all(not a.acknowledged for a in sim._alarms)

    # ── Split mode ────────────────────────────────────────────────────────────

    async def test_0x31_split_creates_sub_ports(self, ble_session):
        _, client, sim = ble_session
        # Port 1 is split-capable (odd/400G); mode code 1 = "4x100G" → 4 sub-ports at 65–68
        await _run_cmd(client, P.CHAR_CMD_WR_UUID, [0x31, 1, 1])
        assert all(p in sim._port_configs for p in [65, 66, 67, 68])

    async def test_0x31_split_disables_physical_port(self, ble_session):
        _, client, sim = ble_session
        await _run_cmd(client, P.CHAR_CMD_WR_UUID, [0x31, 1, 1])
        assert sim._port_configs[1].admin_up is False

    async def test_0x31_sub_ports_have_correct_parent(self, ble_session):
        _, client, sim = ble_session
        await _run_cmd(client, P.CHAR_CMD_WR_UUID, [0x31, 1, 1])
        assert all(sim._port_configs[p].parent_port == 1 for p in [65, 66, 67, 68])

    async def test_0x31_sub_port_speed_matches_mode(self, ble_session):
        _, client, sim = ble_session
        # Port 1 is odd/400G; mode code 1 = "4x100G" → sub-port speed = 100000 Mbps
        await _run_cmd(client, P.CHAR_CMD_WR_UUID, [0x31, 1, 1])
        assert sim._port_configs[65].speed == 100000

    async def test_0x31_unsplit_removes_sub_ports(self, ble_session):
        _, client, sim = ble_session
        await _run_cmd(client, P.CHAR_CMD_WR_UUID, [0x31, 1, 1])
        assert 65 in sim._port_configs
        await _run_cmd(client, P.CHAR_CMD_WR_UUID, [0x31, 1, 0])  # mode 0 = "none"
        assert 65 not in sim._port_configs

    async def test_0x31_split_non_capable_port_is_noop(self, ble_session):
        _, client, sim = ble_session
        # Port 2 is even/800G but not split-capable (2 % 8 != 0)
        await _run_cmd(client, P.CHAR_CMD_WR_UUID, [0x31, 2, 1])
        assert not any(cfg.parent_port == 2 for cfg in sim._port_configs.values())


# ═══════════════════════════════════════════════════════════════════════════════
# Notifications pushed after commands
# ═══════════════════════════════════════════════════════════════════════════════

class TestNotifications:

    @pytest.fixture(autouse=True)
    def set_authenticated(self, ble_session):
        P._authenticated = True

    async def test_admin_down_pushes_response_notification(self, ble_session):
        bridge, client, _ = ble_session
        received = []
        bridge.subscribe(P.CHAR_RESPONSE_UUID,
                         lambda _s, d: received.append(bytes(d)))
        await _run_cmd(client, P.CHAR_CMD_WR_UUID, [0x22, 1])
        assert received
        assert any(b"admin" in r or b"ok" in r for r in received)

    async def test_admin_up_pushes_response_notification(self, ble_session):
        bridge, client, sim = ble_session
        sim.set_port_admin(1, False)
        received = []
        bridge.subscribe(P.CHAR_RESPONSE_UUID, lambda _s, d: received.append(bytes(d)))
        await _run_cmd(client, P.CHAR_CMD_WR_UUID, [0x21, 1])
        assert received

    async def test_admin_down_pushes_port_config_notification(self, ble_session):
        bridge, client, _ = ble_session
        received = []
        bridge.subscribe(P.CHAR_PORT_CFG_UUID, lambda _s, d: received.append(bytes(d)))
        await _run_cmd(client, P.CHAR_CMD_WR_UUID, [0x22, 1], settle=0.05)
        assert received
        assert received[0][0] == 1  # first byte = port number

    async def test_desc_set_pushes_port_config_notification(self, ble_session):
        bridge, client, _ = ble_session
        received = []
        bridge.subscribe(P.CHAR_PORT_CFG_UUID, lambda _s, d: received.append(bytes(d)))
        desc = b"test-uplink"
        payload = [0x25, 2, len(desc)] + list(desc)
        await _run_cmd(client, P.CHAR_CMD_WR_UUID, payload, settle=0.05)
        assert received

    async def test_led_set_pushes_led_notification(self, ble_session):
        bridge, client, _ = ble_session
        received = []
        bridge.subscribe(P.CHAR_LED_UUID, lambda _s, d: received.append(bytes(d)))
        await _run_cmd(client, P.CHAR_CMD_WR_UUID, [0x20, 1, 3, 1])
        assert received
        assert received[-1][0] > 0  # header byte = count of LED entries

    async def test_led_set_notification_contains_updated_color(self, ble_session):
        bridge, client, sim = ble_session
        received = []
        bridge.subscribe(P.CHAR_LED_UUID, lambda _s, d: received.append(bytes(d)))
        # Set port 1 to blue (code 3)
        await _run_cmd(client, P.CHAR_CMD_WR_UUID, [0x20, 1, 3, 0])
        # Find the entry for port 1 in the packed LED notification
        data = received[-1]
        count = data[0]
        found = False
        for i in range(count):
            offset = 1 + i * 3
            if data[offset] == 1:   # port 1
                assert data[offset + 1] == 3  # color code = blue
                found = True
        assert found

    async def test_psu_request_0x27_pushes_psu_notification(self, ble_session):
        bridge, client, sim = ble_session
        received = []
        bridge.subscribe(P.CHAR_PSU_UUID, lambda _s, d: received.append(bytes(d)))
        await _run_cmd(client, P.CHAR_CMD_WR_UUID, [0x27], settle=0.05)
        assert received
        assert received[-1][0] == sim.NUM_PSUS

    async def test_stats_snapshot_0x04_sends_correct_chunk_count(self, ble_session):
        bridge, client, sim = ble_session
        chunks = []
        bridge.subscribe(P.CHAR_BULK_UUID, lambda _s, d: chunks.append(bytes(d)))
        await _run_cmd(client, P.CHAR_CMD_WNR_UUID, [0x04], settle=0.2)
        assert chunks
        # All chunks declare the same total; count of received == declared total
        total = chunks[0][1]
        assert len(chunks) == total

    async def test_stats_snapshot_chunk_payload_is_multiple_of_21(self, ble_session):
        bridge, client, _ = ble_session
        chunks = []
        bridge.subscribe(P.CHAR_BULK_UUID, lambda _s, d: chunks.append(bytes(d)))
        await _run_cmd(client, P.CHAR_CMD_WNR_UUID, [0x04], settle=0.2)
        for chunk in chunks:
            payload = chunk[2:]  # strip seq + total header bytes
            assert len(payload) % 21 == 0


# ═══════════════════════════════════════════════════════════════════════════════
# cmd_queue routing (low-level)
# ═══════════════════════════════════════════════════════════════════════════════

class TestCmdQueueRouting:

    @pytest.fixture(autouse=True)
    def set_authenticated(self, ble_session):
        P._authenticated = True

    async def test_wr_write_queued_as_wr(self, ble_session):
        _, client, _ = ble_session
        await client.write_gatt_char(P.CHAR_CMD_WR_UUID, bytearray([0x22, 3]))
        kind, data = await P.cmd_queue.get()
        assert kind == "wr"
        assert data[0] == 0x22
        assert data[1] == 3

    async def test_wnr_write_queued_as_wnr(self, ble_session):
        _, client, _ = ble_session
        await client.write_gatt_char(P.CHAR_CMD_WNR_UUID, bytearray([0x04]))
        kind, data = await P.cmd_queue.get()
        assert kind == "wnr"
        assert data[0] == 0x04

    async def test_multiple_commands_queued_in_order(self, ble_session):
        _, client, _ = ble_session
        await client.write_gatt_char(P.CHAR_CMD_WR_UUID, bytearray([0x21, 1]))
        await client.write_gatt_char(P.CHAR_CMD_WR_UUID, bytearray([0x22, 2]))
        _, d1 = await P.cmd_queue.get()
        _, d2 = await P.cmd_queue.get()
        assert d1[0] == 0x21
        assert d2[0] == 0x22


# ═══════════════════════════════════════════════════════════════════════════════
# Client disconnect (0x32)
# ═══════════════════════════════════════════════════════════════════════════════

class TestDisconnect:

    async def test_0x32_resets_auth_when_tui_active(self, ble_session):
        _, client, _ = ble_session
        P._authenticated = True

        # Inject a minimal mock TUI state so the auth-reset branch executes
        mock_registry = MagicMock()
        mock_registry._active = "AA:BB:CC"
        mock_state = MagicMock()
        mock_state.registry = mock_registry
        P._tui_state = mock_state

        old_nonce = bytes(P._auth_nonce)
        await _run_cmd(client, P.CHAR_CMD_WR_UUID, [0x32], settle=0.05)
        assert P._authenticated is False
        assert bytes(P._auth_nonce) != old_nonce

    async def test_0x32_without_tui_does_not_crash(self, ble_session):
        _, client, _ = ble_session
        P._authenticated = True
        P._tui_state = None
        # _force_remove_connected will fail silently (no D-Bus)
        await _run_cmd(client, P.CHAR_CMD_WR_UUID, [0x32], settle=0.05)

    async def test_0x32_does_not_emit_tui_command_record(self, ble_session):
        """The 0x32 branch uses `continue` — command is not recorded in TUI."""
        _, client, _ = ble_session
        P._authenticated = True
        recorded_commands = []
        mock_registry = MagicMock()
        mock_registry._active = "AA:BB:CC"
        mock_registry.record_command = lambda *a, **kw: recorded_commands.append(a)
        mock_state = MagicMock()
        mock_state.registry = mock_registry
        P._tui_state = mock_state

        await _run_cmd(client, P.CHAR_CMD_WR_UUID, [0x32], settle=0.05)
        assert not recorded_commands  # `continue` skips the record_command call


# ═══════════════════════════════════════════════════════════════════════════════
# Edge cases
# ═══════════════════════════════════════════════════════════════════════════════

class TestEdgeCases:

    @pytest.fixture(autouse=True)
    def set_authenticated(self, ble_session):
        P._authenticated = True

    async def test_unknown_opcode_does_not_crash(self, ble_session):
        _, client, _ = ble_session
        await _run_cmd(client, P.CHAR_CMD_WR_UUID, [0xAB, 0x00])

    async def test_empty_payload_does_not_crash(self, ble_session):
        _, client, _ = ble_session
        # opcode falls back to 0x00 (unknown)
        await _run_cmd(client, P.CHAR_CMD_WR_UUID, [])

    async def test_truncated_led_payload_does_not_crash(self, ble_session):
        _, client, _ = ble_session
        # 0x20 needs 3 params; only 1 provided
        await _run_cmd(client, P.CHAR_CMD_WR_UUID, [0x20, 1])

    async def test_truncated_mtu_payload_does_not_crash(self, ble_session):
        _, client, _ = ble_session
        # 0x24 needs port + 2-byte MTU; only port provided
        await _run_cmd(client, P.CHAR_CMD_WR_UUID, [0x24, 1])

    async def test_out_of_bounds_speed_code_defaults_to_10g(self, ble_session):
        _, client, sim = ble_session
        # speed code 99 → out of bounds → peripheral falls back to 10000
        await _run_cmd(client, P.CHAR_CMD_WR_UUID, [0x23, 1, 99])
        assert sim._port_configs[1].speed == 10000

    async def test_unknown_opcode_pushes_generic_response(self, ble_session):
        bridge, client, _ = ble_session
        received = []
        bridge.subscribe(P.CHAR_RESPONSE_UUID, lambda _s, d: received.append(bytes(d)))
        await _run_cmd(client, P.CHAR_CMD_WR_UUID, [0xAB])
        assert received
        assert b"ok" in received[0].lower() or b"unknown" in received[0].lower()

    async def test_alarm_ack_with_empty_id_does_not_crash(self, ble_session):
        _, client, _ = ble_session
        payload = [0x26, 0]  # length 0, no id bytes
        await _run_cmd(client, P.CHAR_CMD_WR_UUID, payload)

    async def test_speed_set_unknown_port_is_noop(self, ble_session):
        _, client, sim = ble_session
        await _run_cmd(client, P.CHAR_CMD_WR_UUID, [0x23, 99, 0])
        assert 99 not in sim._port_configs

    async def test_desc_set_long_description_truncated_to_255(self, ble_session):
        _, client, sim = ble_session
        long_desc = b"x" * 300
        payload   = [0x25, 1, 255] + list(long_desc[:255])
        await _run_cmd(client, P.CHAR_CMD_WR_UUID, payload, settle=0.05)
        assert len(sim._port_configs[1].description) <= 255


# ═══════════════════════════════════════════════════════════════════════════════
# Sensor loop
# ═══════════════════════════════════════════════════════════════════════════════

class TestSensorLoop:
    """
    sensor_loop has a 1.5 s startup sleep we can't realistically wait out.
    We replace every asyncio.sleep(n) with a zero-duration real yield using a
    `fast_sleep` helper — AsyncMock returns synchronously without yielding to
    the event loop, so real sleep(0) is required.
    """

    @staticmethod
    def _fast_sleep_patch():
        """
        Returns (real_sleep, fast_sleep).
        fast_sleep(n) calls real_sleep(0), so it always yields once to the
        event loop while never actually waiting.
        """
        real_sleep = asyncio.sleep

        async def fast_sleep(n):
            await real_sleep(0)

        return real_sleep, fast_sleep

    async def test_sensor_loop_pushes_all_characteristics(self, ble_session):
        bridge, _, sim = ble_session
        notified = set()
        for uuid in [P.CHAR_CPU_UUID, P.CHAR_MEM_UUID, P.CHAR_TEMP_UUID,
                     P.CHAR_FAN_UUID, P.CHAR_LED_UUID, P.CHAR_ALARMS_UUID]:
            bridge.subscribe(uuid, lambda _s, _d, u=uuid: notified.add(u))

        real_sleep, fast_sleep = self._fast_sleep_patch()
        with patch('asyncio.sleep', fast_sleep):
            task = asyncio.create_task(P.sensor_loop(sim, 0.01))
            await real_sleep(0.05)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        assert P.CHAR_CPU_UUID    in notified
        assert P.CHAR_MEM_UUID    in notified
        assert P.CHAR_TEMP_UUID   in notified
        assert P.CHAR_FAN_UUID    in notified
        assert P.CHAR_LED_UUID    in notified
        assert P.CHAR_ALARMS_UUID in notified

    async def test_sensor_loop_updates_latest_snapshot(self, ble_session):
        _, _, sim = ble_session
        P.latest_snapshot = None

        real_sleep, fast_sleep = self._fast_sleep_patch()
        with patch('asyncio.sleep', fast_sleep):
            task = asyncio.create_task(P.sensor_loop(sim, 0.01))
            await real_sleep(0.05)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        assert P.latest_snapshot is not None
        assert P.latest_snapshot.num_ports == sim.num_ports

    async def test_sensor_loop_skips_push_when_server_is_none(self, ble_session):
        bridge, _, sim = ble_session
        P.server = None
        notified = []
        bridge.subscribe(P.CHAR_CPU_UUID, lambda _s, _d: notified.append(True))

        real_sleep, fast_sleep = self._fast_sleep_patch()
        with patch('asyncio.sleep', fast_sleep):
            task = asyncio.create_task(P.sensor_loop(sim, 0.01))
            await real_sleep(0.05)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        # server is None → _push block skipped → no GATT notifications
        assert not notified

    async def test_sensor_loop_still_updates_snapshot_when_server_is_none(self, ble_session):
        _, _, sim = ble_session
        P.server = None
        P.latest_snapshot = None

        real_sleep, fast_sleep = self._fast_sleep_patch()
        with patch('asyncio.sleep', fast_sleep):
            task = asyncio.create_task(P.sensor_loop(sim, 0.01))
            await real_sleep(0.05)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        # latest_snapshot updated even without a server
        assert P.latest_snapshot is not None


# ═══════════════════════════════════════════════════════════════════════════════
# TUI helper functions (pure — no BLE, no event loop required)
# ═══════════════════════════════════════════════════════════════════════════════

class TestTUIHelpers:

    # ── _bar ─────────────────────────────────────────────────────────────────

    def test_bar_zero_pct_all_empty(self):
        result = P._bar(0, 20)
        assert result == '░' * 20
        assert len(result) == 20

    def test_bar_100_pct_all_filled(self):
        result = P._bar(100, 20)
        assert result == '█' * 20

    def test_bar_50_pct_half(self):
        result = P._bar(50, 20)
        assert result.count('█') == 10
        assert result.count('░') == 10
        assert len(result) == 20

    def test_bar_default_width_is_20(self):
        assert len(P._bar(0)) == 20

    def test_bar_custom_width(self):
        assert len(P._bar(75, 8)) == 8

    # ── _fmt_time ─────────────────────────────────────────────────────────────

    def test_fmt_time_negative_is_unlimited(self):
        assert P._fmt_time(-1) == "unlimited"

    def test_fmt_time_zero(self):
        assert P._fmt_time(0) == "00:00"

    def test_fmt_time_under_one_hour(self):
        assert P._fmt_time(125) == "02:05"

    def test_fmt_time_exactly_one_minute(self):
        assert P._fmt_time(60) == "01:00"

    def test_fmt_time_with_hours(self):
        assert P._fmt_time(3661) == "01:01:01"

    # ── _alarm_summary ────────────────────────────────────────────────────────

    def test_alarm_summary_none_snap(self):
        assert P._alarm_summary(None) == "none"

    def test_alarm_summary_no_alarms(self):
        snap = SwitchSensorSimulator(num_ports=4).read()
        assert P._alarm_summary(snap) == "none"

    def test_alarm_summary_all_acked(self):
        sim = SwitchSensorSimulator(num_ports=4)
        aid = sim.trigger_alarm("psu", "critical", "PSU fail")
        sim.acknowledge_alarm(aid)
        snap = sim.read()
        assert "all acked" in P._alarm_summary(snap)

    def test_alarm_summary_counts_unacked(self):
        sim = SwitchSensorSimulator(num_ports=4)
        sim.trigger_alarm("psu", "critical", "PSU fail")
        sim.trigger_alarm("fan", "major",    "Fan fail")
        snap = sim.read()
        summary = P._alarm_summary(snap)
        assert "critical" in summary
        assert "major"    in summary

    def test_alarm_summary_mixed_acked_and_unacked(self):
        sim = SwitchSensorSimulator(num_ports=4)
        aid = sim.trigger_alarm("psu", "critical", "PSU fail")
        sim.trigger_alarm("fan", "major", "Fan fail")
        sim.acknowledge_alarm(aid)
        snap = sim.read()
        summary = P._alarm_summary(snap)
        # One unacked (major) should appear in summary
        assert "major" in summary

    # ── _ports_grid ───────────────────────────────────────────────────────────

    def test_ports_grid_contains_all_ports(self):
        sim  = SwitchSensorSimulator(num_ports=8)
        snap = sim.read()
        grid = P._ports_grid(snap, cols=4)
        for p in range(1, 9):
            assert f"P{p:02d}" in grid

    def test_ports_grid_admin_down_shows_A_marker(self):
        sim = SwitchSensorSimulator(num_ports=4)
        sim.set_port_admin(1, False)
        snap = sim.read()
        grid = P._ports_grid(snap, cols=4)
        assert "A" in grid

    def test_ports_grid_default_cols(self):
        sim  = SwitchSensorSimulator(num_ports=4)
        snap = sim.read()
        grid = P._ports_grid(snap)   # cols=12 default
        assert "P01" in grid

    # ── _telemetry_text ───────────────────────────────────────────────────────

    def test_telemetry_text_no_snap_shows_waiting(self):
        state = P.PeripheralState()
        state.snapshot = None
        text = P._telemetry_text(state)
        assert "Waiting" in text

    def test_telemetry_text_with_snap_contains_key_labels(self):
        sim = SwitchSensorSimulator(num_ports=4)
        state = P.PeripheralState()
        state.snapshot = sim.read()
        text = P._telemetry_text(state)
        assert "CPU"    in text
        assert "Memory" in text
        assert "PSU"    in text
        assert "Alarms" in text

    def test_telemetry_text_shows_alarm_list(self):
        sim = SwitchSensorSimulator(num_ports=4)
        sim.trigger_alarm("psu", "critical", "PSU overtemp")
        state = P.PeripheralState()
        state.snapshot = sim.read()
        text = P._telemetry_text(state)
        assert "PSU overtemp" in text

    def test_telemetry_text_renders_tick_and_event_lines(self):
        state = P.PeripheralState()
        state.tick_lines = ["tick line 1"]
        state.log_lines  = ["log line A"]
        text = P._telemetry_text(state)
        assert "tick line 1" in text
        assert "log line A"  in text


# ═══════════════════════════════════════════════════════════════════════════════
# ClientRegistry
# ═══════════════════════════════════════════════════════════════════════════════

class TestClientRegistry:

    def test_connected_increments_count(self):
        reg = P.ClientRegistry()
        reg.connected("AA:BB:CC", "iPhone")
        assert reg.connected_count == 1

    def test_connected_sets_active(self):
        reg = P.ClientRegistry()
        reg.connected("AA:BB:CC", "iPhone")
        assert reg._active == "AA:BB:CC"

    def test_disconnected_decrements_count(self):
        reg = P.ClientRegistry()
        reg.connected("AA:BB:CC", "iPhone")
        reg.disconnected("AA:BB:CC")
        assert reg.connected_count == 0

    def test_disconnected_clears_active(self):
        reg = P.ClientRegistry()
        reg.connected("AA:BB:CC", "iPhone")
        reg.disconnected("AA:BB:CC")
        assert reg._active is None

    def test_second_client_connected_becomes_active(self):
        reg = P.ClientRegistry()
        reg.connected("AA:AA:AA", "Dev A")
        reg.connected("BB:BB:BB", "Dev B")
        assert reg._active == "BB:BB:BB"

    def test_disconnect_first_falls_back_to_second(self):
        reg = P.ClientRegistry()
        reg.connected("AA:AA:AA", "Dev A")
        reg.connected("BB:BB:BB", "Dev B")
        reg.disconnected("BB:BB:BB")
        assert reg._active == "AA:AA:AA"

    def test_connected_fallback_marks_is_fallback(self):
        reg = P.ClientRegistry()
        reg.connected_fallback("BLE:CLIENT", "BLE Client")
        assert reg.all_clients()[0].is_fallback is True

    def test_duplicate_connect_is_ignored(self):
        reg = P.ClientRegistry()
        reg.connected("AA:BB:CC", "iPhone")
        reg.connected("AA:BB:CC", "iPhone")  # second call must be a no-op
        assert len(reg.all_clients()) == 1

    def test_record_keepalive_sets_timestamp(self):
        reg = P.ClientRegistry()
        reg.connected("AA:BB:CC", "iPhone")
        assert reg.all_clients()[0].last_keepalive is None
        reg.record_keepalive()
        assert reg.all_clients()[0].last_keepalive is not None

    def test_record_command_creates_fallback_if_no_active(self):
        reg = P.ClientRegistry()
        reg.record_command("wr", 0x22, "admin down port=1")
        assert len(reg.all_clients()) == 1
        assert reg.all_clients()[0].is_fallback is True

    def test_all_commands_sorted_chronologically(self):
        reg = P.ClientRegistry()
        reg.connected("AA:BB:CC", "iPhone")
        reg.record_command("wr", 0x21, "admin up")
        reg.record_command("wr", 0x22, "admin down")
        cmds = reg.all_commands()
        assert cmds[0][2].opcode == 0x21
        assert cmds[1][2].opcode == 0x22

    def test_all_clients_ordered_by_connect_time(self):
        reg = P.ClientRegistry()
        reg.connected("AA:AA:AA", "Dev A")
        reg.connected("BB:BB:BB", "Dev B")
        clients = reg.all_clients()
        assert clients[0].mac == "AA:AA:AA"
        assert clients[1].mac == "BB:BB:BB"


# ═══════════════════════════════════════════════════════════════════════════════
# _format_command_response (pure function — called directly)
# ═══════════════════════════════════════════════════════════════════════════════

class TestFormatCommandResponse:

    def test_unknown_opcode_returns_ok(self):
        result = P._format_command_response("wr", 0xAB, bytes([0xAB]))
        assert result.startswith("ok (wr)")
        assert "0xab" in result.lower()

    def test_port_opcodes_include_port_number(self):
        # opcodes 0x01, 0x02, 0x05, 0x10, 0x11 → detail = "port=N"
        for op in (0x01, 0x02, 0x05, 0x10, 0x11):
            result = P._format_command_response("wr", op, bytes([op, 7]))
            assert "port=7" in result, f"opcode 0x{op:02X} missing port detail"

    def test_interval_opcode_0x03(self):
        ms_bytes = struct.pack('>H', 500)
        result   = P._format_command_response("wr", 0x03, bytes([0x03]) + ms_bytes)
        assert "interval=500ms" in result

    def test_level_opcode_0x06(self):
        result = P._format_command_response("wr", 0x06, bytes([0x06, 3]))
        assert "level=3" in result

    def test_led_set_opcode_0x20(self):
        # color=blue(3), blink=slow(1)
        result = P._format_command_response("wr", 0x20, bytes([0x20, 1, 3, 1]))
        assert "color=blue" in result
        assert "blink=slow" in result

    def test_admin_down_opcode_0x22(self):
        result = P._format_command_response("wr", 0x22, bytes([0x22, 5]))
        assert "port=5" in result
        assert "down" in result

    def test_admin_up_opcode_0x21(self):
        result = P._format_command_response("wr", 0x21, bytes([0x21, 3]))
        assert "port=3" in result
        assert "up" in result

    def test_speed_opcode_0x23(self):
        result = P._format_command_response("wr", 0x23, bytes([0x23, 1, 0]))
        assert "speed" in result.lower()
        assert "port=1" in result

    def test_mtu_opcode_0x24(self):
        mtu_bytes = struct.pack('>H', 9216)
        result = P._format_command_response("wr", 0x24, bytes([0x24, 2]) + mtu_bytes)
        assert "mtu=9216" in result

    def test_desc_opcode_0x25(self):
        desc = b"spine-link"
        result = P._format_command_response("wr", 0x25, bytes([0x25, 1, len(desc)]) + desc)
        assert "spine-link" in result

    def test_alarm_ack_opcode_0x26(self):
        aid   = b"alarm-001"
        result = P._format_command_response("wr", 0x26, bytes([0x26, len(aid)]) + aid)
        assert "alarm_id=alarm-001" in result

    def test_truncated_mtu_params_does_not_crash(self):
        # Only 1 byte of params when 3 are needed
        result = P._format_command_response("wr", 0x24, bytes([0x24, 1]))
        assert "ok" in result

    def test_wnr_kind_appears_in_response(self):
        result = P._format_command_response("wnr", 0x04, bytes([0x04]))
        assert "wnr" in result


# ═══════════════════════════════════════════════════════════════════════════════
# Previously uncovered command opcodes (0x28, 0x29, 0x30)
# ═══════════════════════════════════════════════════════════════════════════════

class TestUncoveredOpcodes:

    @pytest.fixture(autouse=True)
    def set_authenticated(self, ble_session):
        P._authenticated = True

    async def test_0x28_single_port_pushes_port_config(self, ble_session):
        bridge, client, _ = ble_session
        received = []
        bridge.subscribe(P.CHAR_PORT_CFG_UUID, lambda _s, d: received.append(bytes(d)))
        await _run_cmd(client, P.CHAR_CMD_WR_UUID, [0x28, 2], settle=0.05)
        assert received
        assert received[0][0] == 2  # first byte of port config = port number

    async def test_0x28_all_ports_0xFF_pushes_all_configs(self, ble_session):
        bridge, client, sim = ble_session
        received = []
        bridge.subscribe(P.CHAR_PORT_CFG_UUID, lambda _s, d: received.append(bytes(d)))
        await _run_cmd(client, P.CHAR_CMD_WR_UUID, [0x28, 0xFF], settle=0.15)
        assert len(received) == sim.num_ports

    async def test_0x29_led_refresh_pushes_led_char(self, ble_session):
        bridge, client, sim = ble_session
        received = []
        bridge.subscribe(P.CHAR_LED_UUID, lambda _s, d: received.append(bytes(d)))
        await _run_cmd(client, P.CHAR_CMD_WR_UUID, [0x29])
        assert received
        # Header byte = total LED entries (system LED + one per port)
        assert received[-1][0] == sim.num_ports + 1

    async def test_0x29_led_data_encodes_port_and_color(self, ble_session):
        bridge, client, sim = ble_session
        sim.set_led(1, "green", "solid")
        received = []
        bridge.subscribe(P.CHAR_LED_UUID, lambda _s, d: received.append(bytes(d)))
        await _run_cmd(client, P.CHAR_CMD_WR_UUID, [0x29])
        data  = received[-1]
        count = data[0]
        found = False
        for i in range(count):
            offset = 1 + i * 3
            if data[offset] == 1:  # port 1
                assert data[offset + 1] == P.COLOR_CODE.get("green", 0)
                found = True
        assert found

    async def test_0x30_log_event_does_not_crash(self, ble_session):
        _, client, _ = ble_session
        msg     = b"hello from client"
        payload = [0x30, len(msg)] + list(msg)
        await _run_cmd(client, P.CHAR_CMD_WR_UUID, payload)
        # 0x30 only writes to the log — no GATT notification expected

    async def test_0x30_empty_params_does_not_crash(self, ble_session):
        _, client, _ = ble_session
        # params is empty (opcode only) — elif branch skipped, falls to else
        await _run_cmd(client, P.CHAR_CMD_WR_UUID, [0x30])
