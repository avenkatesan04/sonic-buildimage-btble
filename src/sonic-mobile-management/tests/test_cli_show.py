"""
test_cli_show.py — Unit tests for the 'show mobile-management' CLI plugin.

Uses Click's CliRunner with a mock Db object so no real Redis is needed.
SONiC stubs are set up in conftest.py.

Run:  pytest tests/test_cli_show.py -v
"""
import click
import pytest
from click.testing import CliRunner

from tests.conftest import CliStubDb
from show.plugins.mobile_management import MOBILE_MANAGEMENT


# ── Mock Db object ────────────────────────────────────────────────────────────

class MockSV2:
    """In-memory mock for SonicV2Connector used by show CLI."""
    STATE_DB = "STATE_DB"

    def __init__(self):
        self._data = {"STATE_DB": {}}

    def connect(self, db):
        pass

    def get_all(self, db, key):
        return dict(self._data.get(db, {}).get(key, {}))

    def keys(self, db, pattern):
        import fnmatch
        store = self._data.get(db, {})
        return [k for k in store if fnmatch.fnmatch(k, pattern)]


class MockCfgDB:
    """In-memory mock for ConfigDBConnector."""
    def __init__(self):
        self._tables = {}

    def connect(self):
        pass

    def get_entry(self, table, key):
        return dict(self._tables.get(table, {}).get(key, {}))

    def get_table(self, table):
        return dict(self._tables.get(table, {}))


class MockDb(CliStubDb):
    def __init__(self):
        self.cfgdb = MockCfgDB()
        self.db = MockSV2()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _invoke(cmd_args, mock_db=None):
    """Invoke a show mobile-management subcommand with a mock Db."""
    if mock_db is None:
        mock_db = MockDb()
    runner = CliRunner()

    @click.group(invoke_without_command=True)
    @click.pass_context
    def cli(ctx):
        ctx.obj = mock_db

    cli.add_command(MOBILE_MANAGEMENT)
    return runner.invoke(cli, ["mobile-management"] + cmd_args, catch_exceptions=False)


# ── show mobile-management (root, no subcommand) ─────────────────────────────

class TestShowRoot:

    def test_not_configured(self):
        result = _invoke([])
        assert result.exit_code == 0
        assert "not configured" in result.output

    def test_configured_shows_table(self):
        db = MockDb()
        db.cfgdb._tables["MOBILE_MANAGEMENT"] = {
            "GLOBAL": {
                "device_name": "MySwitch",
                "interval": "5",
                "num_ports": "32",
                "session_duration": "0",
                "backend": "sonic",
            }
        }
        result = _invoke([], db)
        assert result.exit_code == 0
        assert "MySwitch" in result.output
        assert "32" in result.output
        assert "unlimited" in result.output
        assert "sonic" in result.output

    def test_nonzero_duration(self):
        db = MockDb()
        db.cfgdb._tables["MOBILE_MANAGEMENT"] = {
            "GLOBAL": {"session_duration": "300"}
        }
        result = _invoke([], db)
        assert "300s" in result.output


# ── show mobile-management status ─────────────────────────────────────────────

class TestShowStatus:

    def test_not_running(self):
        db = MockDb()
        db.cfgdb._tables["FEATURE"] = {
            "mobile-management": {"state": "disabled"}
        }
        result = _invoke(["status"], db)
        assert result.exit_code == 0
        assert "disabled" in result.output
        assert "not running" in result.output

    def test_running_with_daemon(self):
        db = MockDb()
        db.cfgdb._tables["FEATURE"] = {
            "mobile-management": {"state": "enabled"}
        }
        db.cfgdb._tables["MOBILE_MANAGEMENT"] = {
            "GLOBAL": {"auth_mode": "enforce"}
        }
        db.db._data["STATE_DB"]["FEATURE|mobile-management"] = {
            "current_state": "enabled"
        }
        db.db._data["STATE_DB"]["MOBILE_MANAGEMENT_DAEMON|status"] = {
            "state": "running",
            "pid": "1234",
            "uptime": "160",
            "advertising": "true",
            "auth_mode": "enabled",
            "connected_clients": "1",
        }
        result = _invoke(["status"], db)
        assert "enabled" in result.output
        assert "running" in result.output
        assert "1234" in result.output
        assert "2m 40s" in result.output
        assert "1" in result.output

    def test_no_adapter_state(self):
        db = MockDb()
        db.cfgdb._tables["FEATURE"] = {
            "mobile-management": {"state": "enabled"}
        }
        db.cfgdb._tables["MOBILE_MANAGEMENT"] = {"GLOBAL": {}}
        db.db._data["STATE_DB"]["MOBILE_MANAGEMENT_DAEMON|status"] = {
            "state": "running_no_adapter",
        }
        result = _invoke(["status"], db)
        assert "no BT adapter" in result.output

    def test_bypass_auth_mode(self):
        db = MockDb()
        db.cfgdb._tables["FEATURE"] = {
            "mobile-management": {"state": "enabled"}
        }
        db.cfgdb._tables["MOBILE_MANAGEMENT"] = {
            "GLOBAL": {"auth_mode": "bypass"}
        }
        db.db._data["STATE_DB"]["MOBILE_MANAGEMENT_DAEMON|status"] = {
            "state": "running",
            "auth_mode": "bypass",
        }
        result = _invoke(["status"], db)
        assert "bypass" in result.output
        assert "no auth required" in result.output

    def test_view_only_auth_mode(self):
        db = MockDb()
        db.cfgdb._tables["FEATURE"] = {
            "mobile-management": {"state": "enabled"}
        }
        db.cfgdb._tables["MOBILE_MANAGEMENT"] = {
            "GLOBAL": {"auth_mode": "enforce"}
        }
        db.db._data["STATE_DB"]["MOBILE_MANAGEMENT_DAEMON|status"] = {
            "state": "running",
            "auth_mode": "disabled",
        }
        result = _invoke(["status"], db)
        assert "view-only" in result.output


# ── show mobile-management telemetry ──────────────────────────────────────────

class TestShowTelemetry:

    def test_no_data(self):
        result = _invoke(["telemetry"])
        assert "No telemetry data" in result.output

    def test_with_data(self):
        db = MockDb()
        db.db._data["STATE_DB"]["MOBILE_MANAGEMENT_TELEMETRY|snapshot"] = {
            "cpu_percent": "45.2",
            "mem_percent": "62.1",
            "cpu_temp": "55.0",
            "ports_up": "30",
            "ports_total": "48",
            "psu_ok": "2",
            "psu_total": "2",
            "fan_ok": "4",
            "fan_total": "4",
            "last_update": "2026-06-22T22:00:00",
        }
        result = _invoke(["telemetry"], db)
        assert result.exit_code == 0
        assert "45.2%" in result.output
        assert "62.1%" in result.output
        assert "55.0" in result.output
        assert "30 / 48" in result.output
        assert "2 / 2" in result.output
        assert "4 / 4" in result.output


# ── show mobile-management sessions ───────────────────────────────────────────

class TestShowSessions:

    def test_no_sessions(self):
        result = _invoke(["sessions"])
        assert "No active BLE sessions" in result.output

    def test_with_sessions(self):
        db = MockDb()
        db.db._data["STATE_DB"]["MOBILE_MANAGEMENT_SESSION|AA:BB:CC:DD:EE:FF"] = {
            "name": "iPhone",
            "username": "admin",
            "connected_at": "2026-06-22T22:00:00",
            "duration": "5m 30s",
            "authenticated": "true",
            "command_count": "12",
        }
        result = _invoke(["sessions"], db)
        assert result.exit_code == 0
        assert "AA:BB:CC:DD:EE:FF" in result.output
        assert "iPhone" in result.output
        assert "admin" in result.output
        assert "true" in result.output

    def test_multiple_sessions_sorted(self):
        db = MockDb()
        db.db._data["STATE_DB"]["MOBILE_MANAGEMENT_SESSION|ZZ:ZZ:ZZ:ZZ:ZZ:ZZ"] = {
            "name": "Second",
        }
        db.db._data["STATE_DB"]["MOBILE_MANAGEMENT_SESSION|AA:AA:AA:AA:AA:AA"] = {
            "name": "First",
        }
        result = _invoke(["sessions"], db)
        lines = result.output.split('\n')
        first_idx = next(i for i, l in enumerate(lines) if "First" in l)
        second_idx = next(i for i, l in enumerate(lines) if "Second" in l)
        assert first_idx < second_idx


# ── show mobile-management auth ───────────────────────────────────────────────

class TestShowAuth:

    def test_no_users(self):
        result = _invoke(["auth"])
        assert "No authentication users" in result.output

    def test_with_users(self):
        db = MockDb()
        db.cfgdb._tables["MOBILE_MANAGEMENT_AUTH"] = {
            "admin": {"enabled": "true"},
            "viewer": {"enabled": "true"},
        }
        result = _invoke(["auth"], db)
        assert result.exit_code == 0
        assert "admin" in result.output
        assert "viewer" in result.output
        assert "secrets.json" in result.output


# ── show mobile-management audit ──────────────────────────────────────────────

class TestShowAudit:

    def test_no_entries(self):
        result = _invoke(["audit"])
        assert "No audit entries" in result.output

    def test_with_entries(self):
        db = MockDb()
        db.db._data["STATE_DB"]["MOBILE_MANAGEMENT_AUDIT|0"] = {
            "timestamp": "2026-06-22T22:00:00",
            "username": "admin",
            "command": "Port admin up",
            "detail": "port=1",
            "result": "success",
        }
        db.db._data["STATE_DB"]["MOBILE_MANAGEMENT_AUDIT|1"] = {
            "timestamp": "2026-06-22T22:00:05",
            "username": "admin",
            "command": "Port speed set",
            "detail": "port=2 speed=100000",
            "result": "success",
        }
        result = _invoke(["audit"], db)
        assert result.exit_code == 0
        assert "Port admin up" in result.output
        assert "Port speed set" in result.output
        assert "2 of 2" in result.output

    def test_lines_limit(self):
        db = MockDb()
        for i in range(10):
            db.db._data["STATE_DB"][f"MOBILE_MANAGEMENT_AUDIT|{i}"] = {
                "timestamp": f"2026-06-22T22:00:{i:02d}",
                "username": "admin",
                "command": f"cmd{i}",
                "detail": "",
                "result": "success",
            }
        result = _invoke(["audit", "-n", "3"], db)
        assert "3 of 10" in result.output

    def test_entries_ordered_by_sequence(self):
        db = MockDb()
        db.db._data["STATE_DB"]["MOBILE_MANAGEMENT_AUDIT|5"] = {
            "timestamp": "T5", "username": "u", "command": "fifth",
            "detail": "", "result": "ok",
        }
        db.db._data["STATE_DB"]["MOBILE_MANAGEMENT_AUDIT|3"] = {
            "timestamp": "T3", "username": "u", "command": "third",
            "detail": "", "result": "ok",
        }
        result = _invoke(["audit"], db)
        lines = result.output.split('\n')
        third_idx = next(i for i, l in enumerate(lines) if "third" in l)
        fifth_idx = next(i for i, l in enumerate(lines) if "fifth" in l)
        assert third_idx < fifth_idx


# ── _fmt_uptime helper ────────────────────────────────────────────────────────

class TestFmtUptime:

    def test_seconds_only(self):
        from show.plugins.mobile_management import _fmt_uptime
        assert _fmt_uptime("45") == "45s"

    def test_minutes_and_seconds(self):
        from show.plugins.mobile_management import _fmt_uptime
        assert _fmt_uptime("160") == "2m 40s"

    def test_hours(self):
        from show.plugins.mobile_management import _fmt_uptime
        assert _fmt_uptime("3661") == "1h 1m 1s"

    def test_invalid_input(self):
        from show.plugins.mobile_management import _fmt_uptime
        assert _fmt_uptime("foo") == "foo"
