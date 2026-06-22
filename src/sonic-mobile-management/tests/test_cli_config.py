"""
test_cli_config.py — Unit tests for the 'config mobile-management' CLI plugin.

Uses Click's CliRunner with a mock Db object so no real Redis is needed.
File system operations (_load_secrets/_save_secrets) are patched.
SONiC stubs are set up in conftest.py.

Run:  pytest tests/test_cli_config.py -v
"""
from unittest.mock import patch

import click
import pytest
from click.testing import CliRunner

from tests.conftest import CliStubDb
import config.plugins.mobile_management as cfg_mod
from config.plugins.mobile_management import MOBILE_MANAGEMENT


# ── Mock Db ───────────────────────────────────────────────────────────────────

class MockSV2:
    STATE_DB = "STATE_DB"

    def __init__(self):
        self._data = {"STATE_DB": {}}

    def connect(self, db):
        pass

    def keys(self, db, pattern):
        import fnmatch
        store = self._data.get(db, {})
        return [k for k in store if fnmatch.fnmatch(k, pattern)]

    def delete(self, db, key):
        self._data.get(db, {}).pop(key, None)


class MockCfgDB:
    def __init__(self):
        self._tables = {}

    def connect(self):
        pass

    def get_entry(self, table, key):
        return dict(self._tables.get(table, {}).get(key, {}))

    def get_table(self, table):
        return dict(self._tables.get(table, {}))

    def set_entry(self, table, key, value):
        if value is None:
            self._tables.get(table, {}).pop(key, None)
        else:
            self._tables.setdefault(table, {})[key] = dict(value)

    def mod_entry(self, table, key, value):
        entry = self._tables.setdefault(table, {}).setdefault(key, {})
        entry.update(value)


class MockDb(CliStubDb):
    def __init__(self):
        self.cfgdb = MockCfgDB()
        self.db = MockSV2()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _invoke(cmd_args, mock_db=None):
    if mock_db is None:
        mock_db = MockDb()
    runner = CliRunner()

    @click.pass_context
    def _set_ctx(ctx):
        ctx.ensure_object(_StubDb)
        ctx.obj = mock_db

    # Wrap in a parent group so pass_db finds MockDb in the context
    @click.group(invoke_without_command=True)
    @click.pass_context
    def cli(ctx):
        ctx.obj = mock_db

    cli.add_command(MOBILE_MANAGEMENT)
    return runner.invoke(cli, ["mobile-management"] + cmd_args, catch_exceptions=False)


# ── config mobile-management device-name ──────────────────────────────────────

class TestDeviceName:

    def test_set_name(self):
        db = MockDb()
        result = _invoke(["device-name", "MySwitch"], db)
        assert result.exit_code == 0
        assert "MySwitch" in result.output
        assert db.cfgdb._tables["MOBILE_MANAGEMENT"]["GLOBAL"]["device_name"] == "MySwitch"

    def test_too_long_rejected(self):
        result = _invoke(["device-name", "x" * 27])
        assert result.exit_code != 0
        assert "26 characters" in result.output

    def test_ensures_table_created(self):
        db = MockDb()
        _invoke(["device-name", "Test"], db)
        assert "GLOBAL" in db.cfgdb._tables.get("MOBILE_MANAGEMENT", {})


# ── config mobile-management interval ─────────────────────────────────────────

class TestInterval:

    def test_set_interval(self):
        db = MockDb()
        result = _invoke(["interval", "10"], db)
        assert result.exit_code == 0
        assert "10s" in result.output
        assert db.cfgdb._tables["MOBILE_MANAGEMENT"]["GLOBAL"]["interval"] == "10"

    def test_min_value(self):
        db = MockDb()
        result = _invoke(["interval", "1"], db)
        assert result.exit_code == 0

    def test_zero_rejected(self):
        result = _invoke(["interval", "0"])
        assert result.exit_code != 0

    def test_over_300_rejected(self):
        result = _invoke(["interval", "301"])
        assert result.exit_code != 0


# ── config mobile-management ports ────────────────────────────────────────────

class TestPorts:

    def test_set_ports(self):
        db = MockDb()
        result = _invoke(["ports", "32"], db)
        assert result.exit_code == 0
        assert "32" in result.output
        assert db.cfgdb._tables["MOBILE_MANAGEMENT"]["GLOBAL"]["num_ports"] == "32"

    def test_over_64_rejected(self):
        result = _invoke(["ports", "65"])
        assert result.exit_code != 0

    def test_zero_rejected(self):
        result = _invoke(["ports", "0"])
        assert result.exit_code != 0


# ── config mobile-management duration ─────────────────────────────────────────

class TestDuration:

    def test_set_duration(self):
        db = MockDb()
        result = _invoke(["duration", "600"], db)
        assert result.exit_code == 0
        assert "600s" in result.output

    def test_unlimited(self):
        db = MockDb()
        result = _invoke(["duration", "0"], db)
        assert result.exit_code == 0
        assert "unlimited" in result.output
        assert db.cfgdb._tables["MOBILE_MANAGEMENT"]["GLOBAL"]["session_duration"] == "0"


# ── config mobile-management backend ──────────────────────────────────────────

class TestBackend:

    def test_set_sim(self):
        db = MockDb()
        result = _invoke(["backend", "sim"], db)
        assert result.exit_code == 0
        assert "sim" in result.output
        assert db.cfgdb._tables["MOBILE_MANAGEMENT"]["GLOBAL"]["backend"] == "sim"

    def test_set_sonic(self):
        db = MockDb()
        result = _invoke(["backend", "sonic"], db)
        assert result.exit_code == 0

    def test_set_auto(self):
        db = MockDb()
        result = _invoke(["backend", "auto"], db)
        assert result.exit_code == 0

    def test_invalid_rejected(self):
        result = _invoke(["backend", "bogus"])
        assert result.exit_code != 0


# ── config mobile-management auth add/del ─────────────────────────────────────

class TestAuthAdd:

    @patch.object(cfg_mod, '_save_secrets')
    @patch.object(cfg_mod, '_load_secrets', return_value={})
    def test_add_user(self, mock_load, mock_save):
        db = MockDb()
        result = _invoke(["auth", "add", "admin", "secret123"], db)
        assert result.exit_code == 0
        assert "admin" in result.output
        assert "added" in result.output
        assert db.cfgdb._tables["MOBILE_MANAGEMENT_AUTH"]["admin"]["enabled"] == "true"
        mock_save.assert_called_once()
        saved = mock_save.call_args[0][0]
        assert saved["admin"] == "secret123"

    @patch.object(cfg_mod, '_save_secrets')
    @patch.object(cfg_mod, '_load_secrets', return_value={})
    def test_username_too_long_rejected(self, mock_load, mock_save):
        result = _invoke(["auth", "add", "x" * 65, "pass"])
        assert result.exit_code != 0
        assert "64 characters" in result.output


class TestAuthDel:

    @patch.object(cfg_mod, '_save_secrets')
    @patch.object(cfg_mod, '_load_secrets', return_value={"admin": "pass"})
    def test_del_user(self, mock_load, mock_save):
        db = MockDb()
        db.cfgdb._tables["MOBILE_MANAGEMENT_AUTH"] = {
            "admin": {"enabled": "true"}
        }
        result = _invoke(["auth", "del", "admin"], db)
        assert result.exit_code == 0
        assert "removed" in result.output
        assert "admin" not in db.cfgdb._tables.get("MOBILE_MANAGEMENT_AUTH", {})
        mock_save.assert_called_once()
        saved = mock_save.call_args[0][0]
        assert "admin" not in saved

    def test_del_nonexistent_user_fails(self):
        db = MockDb()
        result = _invoke(["auth", "del", "ghost"], db)
        assert result.exit_code != 0
        assert "not found" in result.output


# ── config mobile-management auth-mode ────────────────────────────────────────

class TestAuthMode:

    def test_set_enforce(self):
        db = MockDb()
        result = _invoke(["auth-mode", "enforce"], db)
        assert result.exit_code == 0
        assert "enforce" in result.output

    def test_set_bypass_warns(self):
        db = MockDb()
        result = _invoke(["auth-mode", "bypass"], db)
        assert result.exit_code == 0
        assert "bypass" in result.output
        assert "Warning" in result.output

    def test_enforce_no_users_warns(self):
        db = MockDb()
        result = _invoke(["auth-mode", "enforce"], db)
        assert result.exit_code == 0
        assert "no auth users" in result.output

    def test_enforce_with_users_no_warning(self):
        db = MockDb()
        db.cfgdb._tables["MOBILE_MANAGEMENT_AUTH"] = {
            "admin": {"enabled": "true"}
        }
        result = _invoke(["auth-mode", "enforce"], db)
        assert result.exit_code == 0
        assert "no auth users" not in result.output

    def test_invalid_mode_rejected(self):
        result = _invoke(["auth-mode", "open"])
        assert result.exit_code != 0


# ── config mobile-management audit clear ──────────────────────────────────────

class TestAuditClear:

    def test_clear_no_entries(self):
        db = MockDb()
        result = _invoke(["audit", "clear"], db)
        assert result.exit_code == 0
        assert "No audit entries" in result.output

    def test_clear_entries(self):
        db = MockDb()
        db.db._data["STATE_DB"]["MOBILE_MANAGEMENT_AUDIT|0"] = {"command": "test"}
        db.db._data["STATE_DB"]["MOBILE_MANAGEMENT_AUDIT|1"] = {"command": "test2"}
        result = _invoke(["audit", "clear"], db)
        assert result.exit_code == 0
        assert "Cleared 2" in result.output
        keys = [k for k in db.db._data["STATE_DB"]
                if k.startswith("MOBILE_MANAGEMENT_AUDIT|")]
        assert len(keys) == 0


# ── _ensure_table ─────────────────────────────────────────────────────────────

class TestEnsureTable:

    def test_creates_defaults(self):
        db = MockDb()
        cfg_mod._ensure_table(db)
        entry = db.cfgdb._tables["MOBILE_MANAGEMENT"]["GLOBAL"]
        assert entry["device_name"] == "SwitchMon"
        assert entry["interval"] == "2"
        assert entry["num_ports"] == "48"
        assert entry["session_duration"] == "0"
        assert entry["backend"] == "auto"
        assert entry["auth_mode"] == "enforce"

    def test_does_not_overwrite_existing(self):
        db = MockDb()
        db.cfgdb._tables["MOBILE_MANAGEMENT"] = {
            "GLOBAL": {"device_name": "Custom", "interval": "10"}
        }
        cfg_mod._ensure_table(db)
        assert db.cfgdb._tables["MOBILE_MANAGEMENT"]["GLOBAL"]["device_name"] == "Custom"
