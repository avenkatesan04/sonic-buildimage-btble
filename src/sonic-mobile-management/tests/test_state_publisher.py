"""
test_state_publisher.py — Unit tests for StatePublisher with mocked STATE_DB.

Run:  pytest tests/test_state_publisher.py -v
"""
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional
from unittest.mock import patch

import pytest


# Minimal stand-ins for the types StatePublisher reads from peripheral.py.
# These avoid importing the real peripheral module (which pulls in bless, etc.)

@dataclass
class FakeCommandRecord:
    timestamp: datetime = field(default_factory=datetime.now)
    kind: str = "wr"
    opcode: int = 0x21
    decoded: str = "ok"


@dataclass
class FakeClientRecord:
    mac: str = "AA:BB:CC:DD:EE:FF"
    name: str = "TestClient"
    username: str = "admin"
    connected_at: datetime = field(default_factory=datetime.now)
    disconnected_at: Optional[datetime] = None
    last_keepalive: Optional[datetime] = field(default_factory=datetime.now)
    is_connected: bool = True
    is_fallback: bool = False
    failed_attempt: bool = False
    received_goodbye: bool = False
    commands: List[FakeCommandRecord] = field(default_factory=list)

    @property
    def duration_str(self):
        return "0m 5s"


class FakeRegistry:
    def __init__(self, sessions=None):
        self._sessions = sessions or {}
        self._order = list(self._sessions.keys())
        self._active = self._order[0] if self._order else None

    @property
    def connected_count(self):
        count = 0
        for mac in self._order:
            slist = self._sessions.get(mac, [])
            if slist and slist[-1].is_connected:
                count += 1
        return count


@dataclass
class FakeSnapshot:
    cpu_pct: float = 45.2
    mem_pct: float = 62.1
    temperatures: object = None
    port_up: list = field(default_factory=lambda: [True, True, False, False])
    psus: list = field(default_factory=list)
    fans: list = field(default_factory=list)

    def __post_init__(self):
        if self.temperatures is None:
            self.temperatures = type('T', (), {
                'cpu_die': 55.0, 'board': 42.0, 'inlet': 30.0, 'outlet': 38.0
            })()
        if not self.psus:
            self.psus = [
                type('P', (), {'present': True, 'output_ok': True})(),
                type('P', (), {'present': True, 'output_ok': False})(),
            ]
        if not self.fans:
            self.fans = [
                type('F', (), {'present': True, 'ok': True})(),
                type('F', (), {'present': True, 'ok': True})(),
            ]


class FakePeripheralState:
    def __init__(self, registry=None, snapshot=None):
        self.registry = registry or FakeRegistry()
        self.snapshot = snapshot

    def invalidate(self):
        pass


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestPublishDaemonStatus:

    def test_writes_status_fields(self, mock_swsscommon):
        fake_sv2, _ = mock_swsscommon

        import mobile_management.peripheral as periph
        periph.server = None
        periph._authenticated = False
        periph._users = {}
        periph._auth_bypass = False

        from mobile_management.state_publisher import StatePublisher
        pub = StatePublisher()

        state = FakePeripheralState(registry=FakeRegistry())
        pub._publish_daemon_status(state)

        data = fake_sv2._data["STATE_DB"]["MOBILE_MANAGEMENT_DAEMON|status"]
        assert data["state"] == "running_no_adapter"
        assert "pid" in data
        assert data["advertising"] == "false"
        assert data["connected_clients"] == "0"

    def test_auth_mode_bypass(self, mock_swsscommon):
        fake_sv2, _ = mock_swsscommon

        import mobile_management.peripheral as periph
        periph.server = None
        periph._users = {}
        periph._auth_bypass = True

        from mobile_management.state_publisher import StatePublisher
        pub = StatePublisher()
        pub._publish_daemon_status(FakePeripheralState())

        data = fake_sv2._data["STATE_DB"]["MOBILE_MANAGEMENT_DAEMON|status"]
        assert data["auth_mode"] == "bypass"

    def test_auth_mode_enabled(self, mock_swsscommon):
        fake_sv2, _ = mock_swsscommon

        import mobile_management.peripheral as periph
        periph.server = None
        periph._users = {"admin": "pass"}
        periph._auth_bypass = False

        from mobile_management.state_publisher import StatePublisher
        pub = StatePublisher()
        pub._publish_daemon_status(FakePeripheralState())

        data = fake_sv2._data["STATE_DB"]["MOBILE_MANAGEMENT_DAEMON|status"]
        assert data["auth_mode"] == "enabled"

    def test_auth_mode_disabled(self, mock_swsscommon):
        fake_sv2, _ = mock_swsscommon

        import mobile_management.peripheral as periph
        periph.server = None
        periph._users = {}
        periph._auth_bypass = False

        from mobile_management.state_publisher import StatePublisher
        pub = StatePublisher()
        pub._publish_daemon_status(FakePeripheralState())

        data = fake_sv2._data["STATE_DB"]["MOBILE_MANAGEMENT_DAEMON|status"]
        assert data["auth_mode"] == "disabled"


class TestPublishTelemetry:

    def test_writes_telemetry_snapshot(self, mock_swsscommon):
        fake_sv2, _ = mock_swsscommon

        from mobile_management.state_publisher import StatePublisher
        pub = StatePublisher()

        snap = FakeSnapshot()
        state = FakePeripheralState(snapshot=snap)
        pub._publish_telemetry(state)

        data = fake_sv2._data["STATE_DB"]["MOBILE_MANAGEMENT_TELEMETRY|snapshot"]
        assert data["cpu_percent"] == "45.2"
        assert data["mem_percent"] == "62.1"
        assert data["cpu_temp"] == "55.0"
        assert data["ports_up"] == "2"
        assert data["ports_total"] == "4"
        assert data["psu_ok"] == "1"
        assert data["psu_total"] == "2"
        assert data["fan_ok"] == "2"
        assert data["fan_total"] == "2"

    def test_no_crash_when_no_snapshot(self, mock_swsscommon):
        fake_sv2, _ = mock_swsscommon

        from mobile_management.state_publisher import StatePublisher
        pub = StatePublisher()

        state = FakePeripheralState(snapshot=None)
        pub._publish_telemetry(state)

        assert "MOBILE_MANAGEMENT_TELEMETRY|snapshot" not in fake_sv2._data.get("STATE_DB", {})


class TestPublishSessions:

    def test_writes_session_data(self, mock_swsscommon):
        fake_sv2, _ = mock_swsscommon

        import mobile_management.peripheral as periph
        periph._authenticated = True

        from mobile_management.state_publisher import StatePublisher
        pub = StatePublisher()

        client = FakeClientRecord(mac="AA:BB:CC:DD:EE:FF", name="iPhone", username="admin")
        registry = FakeRegistry(sessions={"AA:BB:CC:DD:EE:FF": [client]})
        state = FakePeripheralState(registry=registry)
        pub._publish_sessions(state)

        key = "MOBILE_MANAGEMENT_SESSION|AA:BB:CC:DD:EE:FF"
        data = fake_sv2._data["STATE_DB"][key]
        assert data["name"] == "iPhone"
        assert data["username"] == "admin"
        assert data["authenticated"] == "true"

    def test_removes_stale_sessions(self, mock_swsscommon):
        fake_sv2, _ = mock_swsscommon

        import mobile_management.peripheral as periph
        periph._authenticated = False

        from mobile_management.state_publisher import StatePublisher
        pub = StatePublisher()

        client = FakeClientRecord(mac="AA:BB:CC:DD:EE:FF")
        registry = FakeRegistry(sessions={"AA:BB:CC:DD:EE:FF": [client]})
        state = FakePeripheralState(registry=registry)
        pub._publish_sessions(state)

        key = "MOBILE_MANAGEMENT_SESSION|AA:BB:CC:DD:EE:FF"
        assert key in fake_sv2._data["STATE_DB"]

        client.is_connected = False
        pub._publish_sessions(state)

        assert key not in fake_sv2._data["STATE_DB"]


class TestCleanup:

    def test_cleanup_sets_stopped(self, mock_swsscommon):
        fake_sv2, _ = mock_swsscommon

        import mobile_management.peripheral as periph
        periph.server = None
        periph._users = {}
        periph._auth_bypass = False

        from mobile_management.state_publisher import StatePublisher
        pub = StatePublisher()

        pub._publish_daemon_status(FakePeripheralState())
        pub.cleanup()

        data = fake_sv2._data["STATE_DB"]["MOBILE_MANAGEMENT_DAEMON|status"]
        assert data["state"] == "stopped"
        assert data["connected_clients"] == "0"
        assert data["advertising"] == "false"
