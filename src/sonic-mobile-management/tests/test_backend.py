"""
test_backend.py — Unit tests for SonicBackend with mocked Redis.

Run:  pytest tests/test_backend.py -v
"""
import asyncio
from unittest.mock import patch, mock_open

import pytest


def _seed_ports(fake_sv2, n=4):
    """Populate CONFIG_DB with n fake Ethernet ports."""
    for i in range(n):
        iface = f"Ethernet{i * 4}"
        fake_sv2._data["CONFIG_DB"][f"PORT|{iface}"] = {
            "admin_status": "up",
            "speed": "100000",
            "mtu": "9100",
            "description": f"port-{i}",
            "fec": "rs",
        }


def _seed_counters(fake_sv2, n=4):
    """Populate COUNTERS_DB with fake OID map and counter data."""
    oid_map = {}
    for i in range(n):
        iface = f"Ethernet{i * 4}"
        oid = f"oid:0x100000000{i+1:04x}"
        oid_map[iface] = oid
        fake_sv2._data["COUNTERS_DB"][f"COUNTERS:{oid}"] = {
            "SAI_PORT_STAT_IF_OUT_OCTETS": str(1000 * (i + 1)),
            "SAI_PORT_STAT_IF_IN_OCTETS": str(2000 * (i + 1)),
            "SAI_PORT_STAT_IF_OUT_UCAST_PKTS": str(100 * (i + 1)),
            "SAI_PORT_STAT_IF_IN_UCAST_PKTS": str(200 * (i + 1)),
            "SAI_PORT_STAT_IF_OUT_ERRORS": "0",
            "SAI_PORT_STAT_IF_IN_ERRORS": "0",
        }
    fake_sv2._data["COUNTERS_DB"]["COUNTERS_PORT_NAME_MAP"] = oid_map


def _seed_appl(fake_sv2, n=4, up_ports=None):
    """Populate APPL_DB with port oper status."""
    if up_ports is None:
        up_ports = set(range(n))
    for i in range(n):
        iface = f"Ethernet{i * 4}"
        status = "up" if i in up_ports else "down"
        fake_sv2._data["APPL_DB"][f"PORT_TABLE:{iface}"] = {
            "oper_status": status,
        }


def _seed_temps(fake_sv2):
    fake_sv2._data["STATE_DB"]["TEMPERATURE_INFO|CPU Core 0"] = {"temperature": "55.5"}
    fake_sv2._data["STATE_DB"]["TEMPERATURE_INFO|Board Sensor"] = {"temperature": "42.0"}
    fake_sv2._data["STATE_DB"]["TEMPERATURE_INFO|Inlet Temp"] = {"temperature": "30.0"}
    fake_sv2._data["STATE_DB"]["TEMPERATURE_INFO|Outlet Exhaust"] = {"temperature": "38.0"}


def _seed_psus(fake_sv2):
    fake_sv2._data["STATE_DB"]["PSU_INFO|PSU 1"] = {
        "presence": "true", "status": "true",
        "input_voltage": "120.0", "output_voltage": "12.0",
        "output_current": "10.0", "output_power": "120.0",
        "temp": "40.0",
    }
    fake_sv2._data["STATE_DB"]["PSU_INFO|PSU 2"] = {
        "presence": "true", "status": "true",
        "input_voltage": "120.0", "output_voltage": "12.0",
        "output_current": "8.0", "output_power": "96.0",
        "temp": "38.0",
    }


def _seed_fans(fake_sv2):
    for i in range(4):
        fake_sv2._data["STATE_DB"][f"FAN_INFO|Fan {i+1}"] = {
            "presence": "true", "status": "true", "speed": "6000",
        }


# ── Port mapping ──────────────────────────────────────────────────────────────

class TestPortMapping:

    def test_init_maps_ports_from_config_db(self, mock_swsscommon):
        fake_sv2, fake_cfg = mock_swsscommon
        _seed_ports(fake_sv2, n=4)

        from mobile_management.backend import SonicBackend
        be = SonicBackend(num_ports=4)

        assert len(be._port_name_map) == 4
        assert be._port_name_map[1] == "Ethernet0"
        assert be._port_name_map[4] == "Ethernet12"
        assert be._name_to_port["Ethernet0"] == 1

    def test_port_configs_populated(self, mock_swsscommon):
        fake_sv2, _ = mock_swsscommon
        _seed_ports(fake_sv2, n=4)

        from mobile_management.backend import SonicBackend
        be = SonicBackend(num_ports=4)

        cfg = be._port_configs[1]
        assert cfg.admin_up is True
        assert cfg.speed == 100000
        assert cfg.mtu == 9100
        assert cfg.description == "port-0"

    def test_extra_ports_get_defaults(self, mock_swsscommon):
        fake_sv2, _ = mock_swsscommon
        _seed_ports(fake_sv2, n=2)

        from mobile_management.backend import SonicBackend
        be = SonicBackend(num_ports=4)

        assert 3 in be._port_configs
        assert be._port_configs[3].speed == 10000  # default

    def test_num_ports_property(self, mock_swsscommon):
        fake_sv2, _ = mock_swsscommon
        _seed_ports(fake_sv2, n=4)

        from mobile_management.backend import SonicBackend
        be = SonicBackend(num_ports=8)
        assert be.num_ports == 8


# ── Counter OID mapping ──────────────────────────────────────────────────────

class TestCounterOIDMapping:

    def test_oid_map_loaded(self, mock_swsscommon):
        fake_sv2, _ = mock_swsscommon
        _seed_ports(fake_sv2, n=4)
        _seed_counters(fake_sv2, n=4)

        from mobile_management.backend import SonicBackend
        be = SonicBackend(num_ports=4)

        assert len(be._counter_oid_map) == 4
        assert "Ethernet0" in be._counter_oid_map


# ── Port states (APPL_DB) ────────────────────────────────────────────────────

class TestPortStates:

    def test_read_port_states(self, mock_swsscommon):
        fake_sv2, _ = mock_swsscommon
        _seed_ports(fake_sv2, n=4)
        _seed_appl(fake_sv2, n=4, up_ports={0, 2})

        from mobile_management.backend import SonicBackend
        be = SonicBackend(num_ports=4)
        states = be._read_port_states()

        assert states[0] is True   # Ethernet0 up
        assert states[1] is False  # Ethernet4 down
        assert states[2] is True   # Ethernet8 up
        assert states[3] is False  # Ethernet12 down


# ── Port counters ─────────────────────────────────────────────────────────────

class TestPortCounters:

    def test_read_port_counters(self, mock_swsscommon):
        fake_sv2, _ = mock_swsscommon
        _seed_ports(fake_sv2, n=4)
        _seed_counters(fake_sv2, n=4)

        from mobile_management.backend import SonicBackend
        be = SonicBackend(num_ports=4)
        be._read_port_counters()

        stats = be._port_stats[1]
        assert stats.tx_bytes == 1000
        assert stats.rx_bytes == 2000
        assert stats.tx_packets == 100
        assert stats.rx_packets == 200


# ── Temperatures ──────────────────────────────────────────────────────────────

class TestTemperatures:

    def test_read_from_state_db(self, mock_swsscommon):
        fake_sv2, _ = mock_swsscommon
        _seed_ports(fake_sv2, n=4)
        _seed_temps(fake_sv2)

        from mobile_management.backend import SonicBackend
        be = SonicBackend(num_ports=4)
        temps = be._read_temperatures()

        assert temps.cpu_die == 55.5
        assert temps.board == 42.0
        assert temps.inlet == 30.0
        assert temps.outlet == 38.0


# ── PSU / Fan ─────────────────────────────────────────────────────────────────

class TestPSUFan:

    def test_read_psu_state(self, mock_swsscommon):
        fake_sv2, _ = mock_swsscommon
        _seed_ports(fake_sv2, n=4)
        _seed_psus(fake_sv2)

        from mobile_management.backend import SonicBackend
        be = SonicBackend(num_ports=4)
        psus = be._read_psu_state()

        assert len(psus) == 2
        assert psus[0].present is True
        assert psus[0].voltage_in == 120.0
        assert psus[1].power == 96.0

    def test_read_fan_state(self, mock_swsscommon):
        fake_sv2, _ = mock_swsscommon
        _seed_ports(fake_sv2, n=4)
        _seed_fans(fake_sv2)

        from mobile_management.backend import SonicBackend
        be = SonicBackend(num_ports=4)
        fans = be._read_fan_state()

        assert len(fans) == 4
        assert all(f.present for f in fans)
        assert fans[0].rpm == 6000

    def test_default_psus_when_no_data(self, mock_swsscommon):
        fake_sv2, _ = mock_swsscommon
        _seed_ports(fake_sv2, n=4)

        from mobile_management.backend import SonicBackend
        be = SonicBackend(num_ports=4)
        psus = be._read_psu_state()

        assert len(psus) == 2  # defaults

    def test_default_fans_when_no_data(self, mock_swsscommon):
        fake_sv2, _ = mock_swsscommon
        _seed_ports(fake_sv2, n=4)

        from mobile_management.backend import SonicBackend
        be = SonicBackend(num_ports=4)
        fans = be._read_fan_state()

        assert len(fans) == 4  # defaults


# ── Config refresh ────────────────────────────────────────────────────────────

class TestConfigRefresh:

    def test_refresh_picks_up_changes(self, mock_swsscommon):
        fake_sv2, _ = mock_swsscommon
        _seed_ports(fake_sv2, n=4)

        from mobile_management.backend import SonicBackend
        be = SonicBackend(num_ports=4)
        assert be._port_configs[1].admin_up is True

        fake_sv2._data["CONFIG_DB"]["PORT|Ethernet0"]["admin_status"] = "down"
        be._refresh_port_configs()
        assert be._port_configs[1].admin_up is False


# ── Full read() ───────────────────────────────────────────────────────────────

class TestFullRead:

    def test_read_returns_snapshot(self, mock_swsscommon):
        fake_sv2, _ = mock_swsscommon
        _seed_ports(fake_sv2, n=4)
        _seed_counters(fake_sv2, n=4)
        _seed_appl(fake_sv2, n=4, up_ports={0, 1})
        _seed_temps(fake_sv2)
        _seed_psus(fake_sv2)
        _seed_fans(fake_sv2)

        from mobile_management.backend import SonicBackend
        be = SonicBackend(num_ports=4)

        with patch("builtins.open", mock_open(read_data="0.50 0.40 0.30 1/200 12345\n")):
            with patch("os.cpu_count", return_value=4):
                snap = be.read()

        assert snap.temperatures.cpu_die == 55.5
        assert len(snap.psus) == 2
        assert len(snap.fans) == 4
        assert snap.port_up[0] is True
        assert snap.port_up[2] is False
        assert 1 in snap.port_configs


# ── Write path ────────────────────────────────────────────────────────────────

class TestWritePath:

    def test_set_port_admin_up(self, mock_swsscommon):
        fake_sv2, fake_cfg = mock_swsscommon
        _seed_ports(fake_sv2, n=4)

        from mobile_management.backend import SonicBackend
        be = SonicBackend(num_ports=4)
        be.set_port_admin(1, True)

        entry = fake_cfg._tables.get("PORT", {}).get("Ethernet0", {})
        assert entry.get("admin_status") == "up"

    def test_set_port_admin_down(self, mock_swsscommon):
        fake_sv2, fake_cfg = mock_swsscommon
        _seed_ports(fake_sv2, n=4)

        from mobile_management.backend import SonicBackend
        be = SonicBackend(num_ports=4)
        be.set_port_admin(1, False)

        entry = fake_cfg._tables.get("PORT", {}).get("Ethernet0", {})
        assert entry.get("admin_status") == "down"

    def test_set_port_speed(self, mock_swsscommon):
        fake_sv2, fake_cfg = mock_swsscommon
        _seed_ports(fake_sv2, n=4)

        from mobile_management.backend import SonicBackend
        be = SonicBackend(num_ports=4)
        be.set_port_speed(1, 25000)

        entry = fake_cfg._tables.get("PORT", {}).get("Ethernet0", {})
        assert entry.get("speed") == "25000"

    def test_set_port_mtu(self, mock_swsscommon):
        fake_sv2, fake_cfg = mock_swsscommon
        _seed_ports(fake_sv2, n=4)

        from mobile_management.backend import SonicBackend
        be = SonicBackend(num_ports=4)
        be.set_port_mtu(2, 1500)

        entry = fake_cfg._tables.get("PORT", {}).get("Ethernet4", {})
        assert entry.get("mtu") == "1500"

    def test_set_port_desc(self, mock_swsscommon):
        fake_sv2, fake_cfg = mock_swsscommon
        _seed_ports(fake_sv2, n=4)

        from mobile_management.backend import SonicBackend
        be = SonicBackend(num_ports=4)
        be.set_port_desc(1, "uplink-to-spine")

        entry = fake_cfg._tables.get("PORT", {}).get("Ethernet0", {})
        assert entry.get("description") == "uplink-to-spine"

    def test_write_invalid_port_returns_false(self, mock_swsscommon):
        fake_sv2, _ = mock_swsscommon
        _seed_ports(fake_sv2, n=4)

        from mobile_management.backend import SonicBackend
        be = SonicBackend(num_ports=4)
        result = be._write_port_field(99, "admin_status", "up")
        assert result is False

    def test_yang_rejection(self, mock_swsscommon):
        fake_sv2, fake_cfg = mock_swsscommon
        _seed_ports(fake_sv2, n=4)

        from mobile_management.backend import SonicBackend
        be = SonicBackend(num_ports=4)

        be._cfg_db._reject_fields.add("speed")
        result = be._write_port_field(1, "speed", "999999")
        assert result is False


# ── stream() ──────────────────────────────────────────────────────────────────

class TestStream:

    def test_stream_yields_snapshots(self, mock_swsscommon):
        fake_sv2, _ = mock_swsscommon
        _seed_ports(fake_sv2, n=4)

        from mobile_management.backend import SonicBackend
        be = SonicBackend(num_ports=4)

        async def _collect():
            results = []
            async for snap in be.stream(interval=0.01):
                results.append(snap)
                if len(results) >= 3:
                    break
            return results

        with patch("builtins.open", mock_open(read_data="0.10 0.10 0.10 1/100 999\n")):
            with patch("os.cpu_count", return_value=2):
                snaps = asyncio.run(_collect())

        assert len(snaps) == 3
        assert all(hasattr(s, 'cpu_pct') for s in snaps)


# ── create_backend factory ────────────────────────────────────────────────────

class TestCreateBackend:

    def test_sim_backend(self, mock_swsscommon):
        from mobile_management.backend import create_backend
        be = create_backend(kind="sim", num_ports=8)
        assert be.num_ports == 8
        assert type(be).__name__ == "SimBackend"

    def test_auto_selects_sonic(self, mock_swsscommon):
        fake_sv2, _ = mock_swsscommon
        _seed_ports(fake_sv2, n=4)

        from mobile_management.backend import create_backend
        be = create_backend(kind="auto", num_ports=4)
        assert type(be).__name__ == "SonicBackend"
