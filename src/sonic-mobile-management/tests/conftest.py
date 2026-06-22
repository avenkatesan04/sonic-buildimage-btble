"""
conftest.py — Shared fixtures for mobile-management unit tests.

Mocks swsscommon so tests run anywhere without a real SONiC Redis.
"""
import sys
import types
from unittest.mock import MagicMock

import pytest


class FakeSonicV2Connector:
    """In-memory mock of SonicV2Connector using plain dicts."""

    CONFIG_DB = "CONFIG_DB"
    STATE_DB = "STATE_DB"
    COUNTERS_DB = "COUNTERS_DB"
    APPL_DB = "APPL_DB"

    def __init__(self):
        self._data: dict[str, dict[str, dict[str, str]]] = {
            self.CONFIG_DB: {},
            self.STATE_DB: {},
            self.COUNTERS_DB: {},
            self.APPL_DB: {},
        }

    def connect(self, db_name):
        pass

    def keys(self, db, pattern):
        import fnmatch
        store = self._data.get(db, {})
        return [k for k in store if fnmatch.fnmatch(k, pattern)]

    def get_all(self, db, key):
        return dict(self._data.get(db, {}).get(key, {}))

    def set(self, db, key, field, value):
        self._data.setdefault(db, {}).setdefault(key, {})[field] = value

    def delete(self, db, key):
        self._data.get(db, {}).pop(key, None)

    def get(self, db, key, field):
        return self._data.get(db, {}).get(key, {}).get(field)


class FakeConfigDBConnector:
    """In-memory mock of ConfigDBConnector."""

    def __init__(self):
        self._tables: dict[str, dict[str, dict[str, str]]] = {}

    def connect(self):
        pass

    def get_table(self, table_name):
        return dict(self._tables.get(table_name, {}))

    def get_entry(self, table_name, key):
        return dict(self._tables.get(table_name, {}).get(key, {}))

    def set_entry(self, table_name, key, value):
        if value is None:
            self._tables.get(table_name, {}).pop(key, None)
        else:
            self._tables.setdefault(table_name, {})[key] = dict(value)

    def mod_entry(self, table_name, key, value):
        entry = self._tables.setdefault(table_name, {}).setdefault(key, {})
        entry.update(value)


class FakeValidatedConfigDBConnector:
    """Wraps FakeConfigDBConnector, optionally raising ValueError."""

    def __init__(self, raw_connector):
        self._raw = raw_connector
        self._reject_fields: set = set()

    def connect(self):
        self._raw.connect()

    def mod_entry(self, table_name, key, value):
        for field in value:
            if field in self._reject_fields:
                raise ValueError(f"YANG validation failed for {field}")
        self._raw.mod_entry(table_name, key, value)

    def get_table(self, table_name):
        return self._raw.get_table(table_name)

    def get_entry(self, table_name, key):
        return self._raw.get_entry(table_name, key)

    def set_entry(self, table_name, key, value):
        self._raw.set_entry(table_name, key, value)


@pytest.fixture()
def fake_sv2():
    """A fresh FakeSonicV2Connector instance."""
    return FakeSonicV2Connector()


@pytest.fixture()
def fake_cfg():
    """A fresh FakeConfigDBConnector instance."""
    return FakeConfigDBConnector()


@pytest.fixture()
def mock_swsscommon(fake_sv2, fake_cfg, monkeypatch):
    """Install a fake swsscommon.swsscommon module so imports succeed.

    Returns (fake_sv2, fake_cfg) for test assertions.
    """
    mod = types.ModuleType("swsscommon")
    inner = types.ModuleType("swsscommon.swsscommon")
    inner.SonicV2Connector = lambda *a, **kw: fake_sv2
    inner.ConfigDBConnector = lambda *a, **kw: fake_cfg
    mod.swsscommon = inner

    monkeypatch.setitem(sys.modules, "swsscommon", mod)
    monkeypatch.setitem(sys.modules, "swsscommon.swsscommon", inner)

    config_mod = types.ModuleType("config")
    config_validated = types.ModuleType("config.validated_config_db_connector")
    config_validated.ValidatedConfigDBConnector = FakeValidatedConfigDBConnector
    monkeypatch.setitem(sys.modules, "config", config_mod)
    monkeypatch.setitem(sys.modules, "config.validated_config_db_connector", config_validated)

    return fake_sv2, fake_cfg
