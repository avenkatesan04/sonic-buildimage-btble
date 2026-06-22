"""
test_ble_dbus.py — D-Bus integration tests for Linux-specific peripheral code

Tests _monitor_connections, _configure_adapter, _remove_device, and
_force_remove_connected using a FakeBlueZBus that simulates the dbus_fast API.
No BlueZ daemon or real hardware required — runs on any platform.

Run:
    pytest test_ble_dbus.py -v
"""

import asyncio
import os
import sys
import types
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

# ── Stub bless / dbus at module level ─────────────────────────────────────────
# (same pattern as test_ble_protocol.py — safe to run both together)
for _lib in ['bless', 'dbus_fast', 'dbus_fast.aio', 'dbus_fast.constants']:
    sys.modules.setdefault(_lib, MagicMock())

import mobile_management.peripheral as P  # noqa: E402


# ═══════════════════════════════════════════════════════════════════════════════
# Fake D-Bus infrastructure
# ═══════════════════════════════════════════════════════════════════════════════

class FakeVariant:
    """
    Mimics dbus_fast.Variant — a typed D-Bus value with a .value attribute.

    Supports both single-arg (test helper) and two-arg (peripheral code) usage:
        FakeVariant(True)          → value = True
        FakeVariant('s', 'hello')  → value = 'hello'
    """

    def __init__(self, type_sig_or_value, value=None):
        if value is not None:
            self.value    = value
            self.type_sig = type_sig_or_value
        else:
            self.value    = type_sig_or_value
            self.type_sig = None


class FakeMessage:
    """
    Mimics dbus_fast.message.Message.

    Accepts all fields as keyword args so it can substitute for:
      • Our test helper calls: FakeMessage(member=..., body=..., path=...)
      • Peripheral bus.call() calls: Message(destination=..., interface=..., ...)
    """

    def __init__(self, member='', body=None, path='',
                 destination='', interface='', signature='s', **_rest):
        self.member      = member
        self.body        = body if body is not None else []
        self.path        = path
        self.destination = destination
        self.interface   = interface
        self.signature   = signature


class _FakeSvcInterface:
    """Minimal ServiceInterface base so _PairingAgent can subclass it."""
    def __init__(self, iface_name): pass


def _fake_dbus_method(*args, **kwargs):
    """No-op replacement for @dbus_fast.service.method decorator."""
    return lambda f: f


class _FakeInterface:
    """Simulates a D-Bus proxy interface."""

    def __init__(self, bus, path, iface_name):
        self._bus       = bus
        self._path      = path
        self._iface     = iface_name

    async def call_get_managed_objects(self):
        return self._bus._managed_objects

    async def call_set(self, interface, key, value):
        self._bus._props_sets.append((interface, key, value))

    async def call_remove_device(self, device_path):
        self._bus._removed_devices.append(device_path)

    async def call_register_agent(self, path, capability):
        self._bus._registered_agent = (path, capability)

    async def call_request_default_agent(self, path):
        pass

    def on_properties_changed(self, cb):
        pass


class _FakeProxy:
    """Simulates a D-Bus proxy object."""

    def __init__(self, bus, path):
        self._bus  = bus
        self._path = path

    def get_interface(self, iface_name):
        return _FakeInterface(self._bus, self._path, iface_name)


class FakeBlueZBus:
    """
    Full simulation of the dbus_fast MessageBus + BlueZ object hierarchy.

    Creates a virtual BlueZ tree with one adapter and optional pre-existing
    devices. Supports injecting PropertiesChanged and InterfacesAdded messages
    directly into the registered handler.

    Usage:
        bus = FakeBlueZBus(devices=[
            (path, mac, name, connected),
            ...
        ])

    Inspection attributes:
        bus._props_sets       — list of (interface, key, value) from call_set
        bus._removed_devices  — list of device paths from call_remove_device
        bus._registered_agent — (path, capability) from call_register_agent
    """

    ADAPTER_PATH = '/org/bluez/hci0'
    DEVICE_PATH  = '/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF'
    DEVICE_MAC   = 'AA:BB:CC:DD:EE:FF'

    def __init__(self, *, devices=None, adapter_path=None):
        if adapter_path:
            self.ADAPTER_PATH = adapter_path

        self._managed_objects = {
            self.ADAPTER_PATH: {'org.bluez.Adapter1': {}}
        }

        for dev_path, mac, name, connected in (devices or []):
            self._managed_objects[dev_path] = {
                'org.bluez.Device1': {
                    'Address':   FakeVariant(mac),
                    'Name':      FakeVariant(name),
                    'Connected': FakeVariant(connected),
                }
            }

        self._message_handler   = None
        self.handler_registered = asyncio.Event()

        self._props_sets      = []
        self._removed_devices = []
        self._registered_agent = None
        self._calls            = []

    # ── Bus methods ───────────────────────────────────────────────────────────

    async def connect(self):
        return self

    async def disconnect(self):
        pass

    async def introspect(self, service, path):
        return MagicMock()

    def get_proxy_object(self, service, path, introspection):
        return _FakeProxy(self, path)

    def add_message_handler(self, handler):
        self._message_handler = handler
        self.handler_registered.set()

    def export(self, path, service):
        pass

    async def call(self, msg):
        self._calls.append(msg)

    # ── Test helper: emit a fake message into the registered handler ──────────

    def emit(self, message: FakeMessage):
        if self._message_handler:
            self._message_handler(message)

    def emit_connected(self, mac=DEVICE_MAC,
                       path=DEVICE_PATH):
        self.emit(FakeMessage(
            member='PropertiesChanged',
            body=['org.bluez.Device1', {'Connected': FakeVariant(True)}, []],
            path=path,
        ))

    def emit_disconnected(self, mac=DEVICE_MAC,
                          path=DEVICE_PATH):
        self.emit(FakeMessage(
            member='PropertiesChanged',
            body=['org.bluez.Device1', {'Connected': FakeVariant(False)}, []],
            path=path,
        ))

    def emit_interfaces_added(self, mac, name, connected, path):
        self.emit(FakeMessage(
            member='InterfacesAdded',
            body=[path, {
                'org.bluez.Device1': {
                    'Address':   FakeVariant(mac),
                    'Name':      FakeVariant(name),
                    'Connected': FakeVariant(connected),
                }
            }],
            path=path,
        ))


# ── Build per-test sys.modules patch dict ─────────────────────────────────────

def _make_dbus_modules(fake_bus: FakeBlueZBus) -> dict:
    """
    Returns a dict suitable for patch.dict(sys.modules, ...) that installs
    fake dbus_fast modules wired to `fake_bus`.

    MessageBus(bus_type=...) → instance → .connect() → fake_bus
    Variant('s', value)      → FakeVariant with .value = value
    ServiceInterface         → _FakeSvcInterface (subclass-able)
    @method()                → no-op decorator
    """
    bus_instance       = MagicMock()
    bus_instance.connect = AsyncMock(return_value=fake_bus)

    mock_aio               = MagicMock()
    mock_aio.MessageBus    = MagicMock(return_value=bus_instance)

    mock_dbus              = MagicMock()
    mock_dbus.Variant      = FakeVariant

    mock_const             = MagicMock()
    mock_const.BusType     = MagicMock(SYSTEM=1)

    mock_msg               = MagicMock()
    mock_msg.Message       = FakeMessage

    mock_service = types.SimpleNamespace(
        ServiceInterface=_FakeSvcInterface,
        method=_fake_dbus_method,
    )

    return {
        'dbus_fast':           mock_dbus,
        'dbus_fast.aio':       mock_aio,
        'dbus_fast.constants': mock_const,
        'dbus_fast.message':   mock_msg,
        'dbus_fast.service':   mock_service,
    }


# ── Fixture: reset peripheral globals between tests ───────────────────────────

@pytest.fixture(autouse=True)
def reset_peripheral_globals():
    """Isolate module-level globals from test to test."""
    P.server          = None
    P.simulator       = None
    P._authenticated  = False
    P._auth_bypass    = False
    P._audit_log      = None
    P._auth_nonce     = os.urandom(16)
    P._tui_state      = None
    yield
    P.server          = None
    P.simulator       = None
    P._authenticated  = False
    P._auth_bypass    = False
    P._audit_log      = None
    P._tui_state      = None


# ── Helper: start _monitor_connections and wait until handler is registered ───

async def _start_monitor(registry, state, fake_bus, *, timeout=2.0):
    task = asyncio.create_task(P._monitor_connections(registry, state))
    await asyncio.wait_for(fake_bus.handler_registered.wait(), timeout=timeout)
    return task


async def _stop(task):
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


# ═══════════════════════════════════════════════════════════════════════════════
# _monitor_connections
# ═══════════════════════════════════════════════════════════════════════════════

class TestMonitorConnections:

    async def test_connect_event_registers_client(self):
        bus      = FakeBlueZBus()
        registry = P.ClientRegistry()
        state    = P.PeripheralState(registry=registry)

        with patch.dict(sys.modules, _make_dbus_modules(bus)):
            task = await _start_monitor(registry, state, bus)
            bus.emit_connected()
            await asyncio.sleep(0)
            await _stop(task)

        assert registry.connected_count == 1
        assert registry._active == 'AA:BB:CC:DD:EE:FF'

    async def test_disconnect_event_marks_client_gone(self):
        bus      = FakeBlueZBus()
        registry = P.ClientRegistry()
        state    = P.PeripheralState(registry=registry)

        with patch.dict(sys.modules, _make_dbus_modules(bus)):
            task = await _start_monitor(registry, state, bus)
            bus.emit_connected()
            await asyncio.sleep(0)
            bus.emit_disconnected()
            await asyncio.sleep(0)
            await _stop(task)

        assert registry.connected_count == 0

    async def test_disconnect_event_resets_authentication(self):
        bus      = FakeBlueZBus()
        registry = P.ClientRegistry()
        state    = P.PeripheralState(registry=registry)
        P._authenticated = True

        with patch.dict(sys.modules, _make_dbus_modules(bus)):
            task = await _start_monitor(registry, state, bus)
            bus.emit_disconnected()
            await asyncio.sleep(0)
            await _stop(task)

        assert P._authenticated is False

    async def test_disconnect_event_rotates_nonce(self):
        bus       = FakeBlueZBus()
        registry  = P.ClientRegistry()
        state     = P.PeripheralState(registry=registry)
        old_nonce = bytes(P._auth_nonce)

        with patch.dict(sys.modules, _make_dbus_modules(bus)):
            task = await _start_monitor(registry, state, bus)
            bus.emit_disconnected()
            await asyncio.sleep(0)
            await _stop(task)

        assert bytes(P._auth_nonce) != old_nonce

    async def test_interfaces_added_connected_registers_client(self):
        bus      = FakeBlueZBus()
        registry = P.ClientRegistry()
        state    = P.PeripheralState(registry=registry)

        with patch.dict(sys.modules, _make_dbus_modules(bus)):
            task = await _start_monitor(registry, state, bus)
            bus.emit_interfaces_added(
                mac='BB:BB:BB:BB:BB:BB',
                name='iPad',
                connected=True,
                path='/org/bluez/hci0/dev_BB_BB_BB_BB_BB_BB',
            )
            await asyncio.sleep(0)
            await _stop(task)

        macs = [c.mac for c in registry.all_clients()]
        assert 'BB:BB:BB:BB:BB:BB' in macs

    async def test_interfaces_added_not_connected_is_not_registered(self):
        bus      = FakeBlueZBus()
        registry = P.ClientRegistry()
        state    = P.PeripheralState(registry=registry)

        with patch.dict(sys.modules, _make_dbus_modules(bus)):
            task = await _start_monitor(registry, state, bus)
            bus.emit_interfaces_added(
                mac='CC:CC:CC:CC:CC:CC',
                name='Watch',
                connected=False,
                path='/org/bluez/hci0/dev_CC_CC_CC_CC_CC_CC',
            )
            await asyncio.sleep(0)
            await _stop(task)

        assert registry.connected_count == 0

    async def test_seed_scan_registers_already_connected_device(self):
        bus = FakeBlueZBus(devices=[
            (FakeBlueZBus.DEVICE_PATH, 'AA:BB:CC:DD:EE:FF', 'iPhone', True),
        ])
        registry = P.ClientRegistry()
        state    = P.PeripheralState(registry=registry)

        with patch.dict(sys.modules, _make_dbus_modules(bus)):
            task = await _start_monitor(registry, state, bus)
            await asyncio.sleep(0)
            await _stop(task)

        assert registry.connected_count == 1
        assert registry.all_clients()[0].mac == 'AA:BB:CC:DD:EE:FF'

    async def test_seed_scan_ignores_not_connected_device(self):
        bus = FakeBlueZBus(devices=[
            (FakeBlueZBus.DEVICE_PATH, 'AA:BB:CC:DD:EE:FF', 'iPhone', False),
        ])
        registry = P.ClientRegistry()
        state    = P.PeripheralState(registry=registry)

        with patch.dict(sys.modules, _make_dbus_modules(bus)):
            task = await _start_monitor(registry, state, bus)
            await asyncio.sleep(0)
            await _stop(task)

        assert registry.connected_count == 0

    async def test_unrelated_member_is_ignored(self):
        bus      = FakeBlueZBus()
        registry = P.ClientRegistry()
        state    = P.PeripheralState(registry=registry)

        with patch.dict(sys.modules, _make_dbus_modules(bus)):
            task = await _start_monitor(registry, state, bus)
            bus.emit(FakeMessage(
                member='SomethingElse',
                body=['org.bluez.Device1', {'Connected': FakeVariant(True)}, []],
            ))
            await asyncio.sleep(0)
            await _stop(task)

        assert registry.connected_count == 0

    async def test_properties_changed_without_connected_key_is_ignored(self):
        bus      = FakeBlueZBus()
        registry = P.ClientRegistry()
        state    = P.PeripheralState(registry=registry)

        with patch.dict(sys.modules, _make_dbus_modules(bus)):
            task = await _start_monitor(registry, state, bus)
            # PropertiesChanged but 'Connected' is absent
            bus.emit(FakeMessage(
                member='PropertiesChanged',
                body=['org.bluez.Device1', {'Alias': FakeVariant('Foo')}, []],
            ))
            await asyncio.sleep(0)
            await _stop(task)

        assert registry.connected_count == 0

    async def test_non_device1_properties_changed_is_ignored(self):
        bus      = FakeBlueZBus()
        registry = P.ClientRegistry()
        state    = P.PeripheralState(registry=registry)

        with patch.dict(sys.modules, _make_dbus_modules(bus)):
            task = await _start_monitor(registry, state, bus)
            # body[0] is not 'org.bluez.Device1'
            bus.emit(FakeMessage(
                member='PropertiesChanged',
                body=['org.bluez.Adapter1', {'Connected': FakeVariant(True)}, []],
            ))
            await asyncio.sleep(0)
            await _stop(task)

        assert registry.connected_count == 0

    async def test_path_without_dev_prefix_is_ignored(self):
        bus      = FakeBlueZBus()
        registry = P.ClientRegistry()
        state    = P.PeripheralState(registry=registry)

        with patch.dict(sys.modules, _make_dbus_modules(bus)):
            task = await _start_monitor(registry, state, bus)
            bus.emit(FakeMessage(
                member='PropertiesChanged',
                body=['org.bluez.Device1', {'Connected': FakeVariant(True)}, []],
                path='/org/bluez/hci0/adapter',  # no 'dev_' prefix
            ))
            await asyncio.sleep(0)
            await _stop(task)

        assert registry.connected_count == 0

    async def test_malformed_empty_body_does_not_crash(self):
        bus      = FakeBlueZBus()
        registry = P.ClientRegistry()
        state    = P.PeripheralState(registry=registry)

        with patch.dict(sys.modules, _make_dbus_modules(bus)):
            task = await _start_monitor(registry, state, bus)
            bus.emit(FakeMessage(member='PropertiesChanged', body=[]))
            await asyncio.sleep(0)
            await _stop(task)
        # passes if no exception is raised

    async def test_two_connect_disconnect_cycles(self):
        bus      = FakeBlueZBus()
        registry = P.ClientRegistry()
        state    = P.PeripheralState(registry=registry)

        with patch.dict(sys.modules, _make_dbus_modules(bus)):
            task = await _start_monitor(registry, state, bus)
            bus.emit_connected()
            await asyncio.sleep(0)
            bus.emit_disconnected()
            await asyncio.sleep(0)
            bus.emit_connected()
            await asyncio.sleep(0)
            await _stop(task)

        # Second reconnect re-registers the same client
        assert registry.connected_count == 1


# ═══════════════════════════════════════════════════════════════════════════════
# _configure_adapter
# ═══════════════════════════════════════════════════════════════════════════════

class TestConfigureAdapter:
    """Tests for _configure_adapter_security (sets adapter alias, disables pairing, removes devices)."""

    async def test_sets_alias_on_adapter(self):
        bus = FakeBlueZBus()

        with patch.dict(sys.modules, _make_dbus_modules(bus)):
            with patch('subprocess.run', return_value=MagicMock()):
                await P._configure_adapter_security("SwitchMon-Test")

        keys = [k for (_, k, _) in bus._props_sets]
        assert 'Alias' in keys

    async def test_disables_pairing_on_adapter(self):
        bus = FakeBlueZBus()

        with patch.dict(sys.modules, _make_dbus_modules(bus)):
            with patch('subprocess.run', return_value=MagicMock()):
                await P._configure_adapter_security("SwitchMon-Test")

        keys = [k for (_, k, _) in bus._props_sets]
        assert 'Pairable' in keys

    async def test_alias_value_matches_provided_name(self):
        bus = FakeBlueZBus()

        with patch.dict(sys.modules, _make_dbus_modules(bus)):
            with patch('subprocess.run', return_value=MagicMock()):
                await P._configure_adapter_security("MySwitch-007")

        alias_entry = next(e for e in bus._props_sets if e[1] == 'Alias')
        # Variant('s', "MySwitch-007") → FakeVariant.value = "MySwitch-007"
        assert alias_entry[2].value == "MySwitch-007"

    async def test_pairable_set_to_false(self):
        bus = FakeBlueZBus()

        with patch.dict(sys.modules, _make_dbus_modules(bus)):
            with patch('subprocess.run', return_value=MagicMock()):
                await P._configure_adapter_security("SwitchMon")

        pair_entry = next(e for e in bus._props_sets if e[1] == 'Pairable')
        assert pair_entry[2].value is False

    async def test_removes_known_devices(self):
        bus = FakeBlueZBus(devices=[
            (FakeBlueZBus.DEVICE_PATH, 'AA:BB:CC:DD:EE:FF', 'iPhone', False),
        ])

        with patch.dict(sys.modules, _make_dbus_modules(bus)):
            with patch('subprocess.run', return_value=MagicMock()):
                await P._configure_adapter_security("SwitchMon")

        assert FakeBlueZBus.DEVICE_PATH in bus._removed_devices

    async def test_no_adapter_is_silent_noop(self):
        bus = FakeBlueZBus()
        bus._managed_objects = {}  # no Adapter1

        with patch.dict(sys.modules, _make_dbus_modules(bus)):
            await P._configure_adapter_security("SwitchMon")  # must not raise

        assert not bus._props_sets

    async def test_subprocess_failure_does_not_raise(self):
        """hcitool is optional — its failure must be swallowed."""
        bus = FakeBlueZBus()

        with patch.dict(sys.modules, _make_dbus_modules(bus)):
            with patch('subprocess.run', side_effect=FileNotFoundError("hcitool not found")):
                await P._configure_adapter_security("SwitchMon")  # must not raise


# ═══════════════════════════════════════════════════════════════════════════════
# _remove_device
# ═══════════════════════════════════════════════════════════════════════════════

class TestRemoveDevice:

    async def test_calls_remove_on_adapter(self):
        bus    = FakeBlueZBus()
        target = FakeBlueZBus.DEVICE_PATH

        with patch.dict(sys.modules, _make_dbus_modules(bus)):
            with patch('asyncio.sleep', AsyncMock(return_value=None)):
                await P._remove_device(target)

        assert target in bus._removed_devices

    async def test_no_crash_when_adapter_not_found(self):
        bus = FakeBlueZBus()
        bus._managed_objects = {}

        with patch.dict(sys.modules, _make_dbus_modules(bus)):
            with patch('asyncio.sleep', AsyncMock(return_value=None)):
                await P._remove_device('/some/path')

        # passes if no exception

    async def test_skips_initial_sleep_for_testing(self):
        """asyncio.sleep(2.0) initial delay is transparent when patched."""
        bus    = FakeBlueZBus()
        slept  = []

        async def capture_sleep(n):
            slept.append(n)

        with patch.dict(sys.modules, _make_dbus_modules(bus)):
            with patch('asyncio.sleep', capture_sleep):
                await P._remove_device(FakeBlueZBus.DEVICE_PATH)

        # The 2.0 s initial sleep must be the first call
        assert slept and slept[0] == 2.0


# ═══════════════════════════════════════════════════════════════════════════════
# _force_remove_connected
# ═══════════════════════════════════════════════════════════════════════════════

class TestForceRemoveConnected:

    async def test_removes_connected_device(self):
        bus = FakeBlueZBus(devices=[
            (FakeBlueZBus.DEVICE_PATH, 'AA:BB:CC:DD:EE:FF', 'iPhone', True),
        ])

        with patch.dict(sys.modules, _make_dbus_modules(bus)):
            await P._force_remove_connected()

        assert FakeBlueZBus.DEVICE_PATH in bus._removed_devices

    async def test_skips_disconnected_device(self):
        bus = FakeBlueZBus(devices=[
            (FakeBlueZBus.DEVICE_PATH, 'AA:BB:CC:DD:EE:FF', 'iPhone', False),
        ])

        with patch.dict(sys.modules, _make_dbus_modules(bus)):
            await P._force_remove_connected()

        assert FakeBlueZBus.DEVICE_PATH not in bus._removed_devices

    async def test_removes_multiple_connected_devices(self):
        path_a = '/org/bluez/hci0/dev_AA_AA_AA_AA_AA_AA'
        path_b = '/org/bluez/hci0/dev_BB_BB_BB_BB_BB_BB'
        bus    = FakeBlueZBus(devices=[
            (path_a, 'AA:AA:AA:AA:AA:AA', 'Dev A', True),
            (path_b, 'BB:BB:BB:BB:BB:BB', 'Dev B', True),
        ])

        with patch.dict(sys.modules, _make_dbus_modules(bus)):
            await P._force_remove_connected()

        assert path_a in bus._removed_devices
        assert path_b in bus._removed_devices

    async def test_device_outside_adapter_path_is_not_removed(self):
        """Devices not under the adapter path must be skipped."""
        bad_path = '/org/bluez/hci1/dev_AA_BB_CC_DD_EE_FF'
        bus      = FakeBlueZBus(devices=[
            (bad_path, 'AA:BB:CC:DD:EE:FF', 'iPhone', True),
        ])

        with patch.dict(sys.modules, _make_dbus_modules(bus)):
            await P._force_remove_connected()

        # bad_path is under hci1, adapter is hci0 → must NOT be removed
        assert bad_path not in bus._removed_devices

    async def test_no_crash_when_no_adapter(self):
        bus = FakeBlueZBus()
        bus._managed_objects = {}

        with patch.dict(sys.modules, _make_dbus_modules(bus)):
            await P._force_remove_connected()

    async def test_no_crash_when_no_devices(self):
        bus = FakeBlueZBus()  # adapter only, no devices

        with patch.dict(sys.modules, _make_dbus_modules(bus)):
            await P._force_remove_connected()

        assert not bus._removed_devices
