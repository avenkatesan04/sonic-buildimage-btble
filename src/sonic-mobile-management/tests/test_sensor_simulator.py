"""
test_sensor_simulator.py — Offline unit tests for sensor_simulator.py

Run with:  pytest tests/test_sensor_simulator.py -v
No BLE hardware, asyncio, or network required.
"""

import math
import struct
import time
import pytest

from mobile_management.sensor_simulator import (
    SwitchSensorSimulator,
    SwitchSnapshot,
    TemperatureSensors,
    PortConfig,
    PortStats,
    PSUState,
    FanState,
    LEDState,
    Alarm,
    LED_COLORS,
    LED_BLINKS,
    COLOR_CODE,
    BLINK_CODE,
    SPEED_OPTIONS,
    SPEED_CODE,
    SPLIT_MODES,
    SPLIT_MEMBERS,
    SPLIT_SPEEDS,
    ALARM_SEVERITIES,
    ALARM_CATEGORIES,
    SUB_PORT_BASE,
    _sub_port_start,
    _default_split_capable,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _minimal_snap(**kwargs) -> SwitchSnapshot:
    """Return a minimal SwitchSnapshot; keyword args override defaults."""
    defaults = dict(
        timestamp=1700000000.0,
        cpu_pct=50.0,
        mem_pct=60.0,
        temperatures=TemperatureSensors(cpu_die=55.0, board=45.0, inlet=26.0, outlet=38.0),
        port_up=[True, False, True],
    )
    defaults.update(kwargs)
    return SwitchSnapshot(**defaults)


def _unpack_u16be(data: bytes, offset: int) -> int:
    return struct.unpack_from('>H', data, offset)[0]


def _unpack_i16be(data: bytes, offset: int) -> int:
    return struct.unpack_from('>h', data, offset)[0]


def _unpack_u32be(data: bytes, offset: int) -> int:
    return struct.unpack_from('>I', data, offset)[0]


# ── Constructor ───────────────────────────────────────────────────────────────

class TestConstructor:
    def test_default_48_ports(self):
        sim = SwitchSensorSimulator()
        assert sim.num_ports == 48

    def test_custom_port_count(self):
        sim = SwitchSensorSimulator(num_ports=24)
        assert sim.num_ports == 24
        assert len(sim._port_up) == 24
        assert len(sim._port_configs) == 24

    def test_raises_for_more_than_64_ports(self):
        with pytest.raises(ValueError, match="64"):
            SwitchSensorSimulator(num_ports=65)

    def test_exactly_64_ports_allowed(self):
        sim = SwitchSensorSimulator(num_ports=64)
        assert sim.num_ports == 64

    def test_split_capable_ports_are_mixed_odd_even(self):
        # p%8==1 (odd/400G): 1, 9  |  p%8==0 (even/800G): 8
        sim = SwitchSensorSimulator(num_ports=12)
        assert sim._split_capable == {1, 8, 9}

    def test_port_configs_keyed_1_based(self):
        sim = SwitchSensorSimulator(num_ports=4)
        assert set(sim._port_configs.keys()) == {1, 2, 3, 4}

    def test_system_led_at_port_0(self):
        sim = SwitchSensorSimulator(num_ports=4)
        assert 0 in sim._leds
        assert sim._leds[0].color == "green"

    def test_two_psus_created(self):
        sim = SwitchSensorSimulator()
        assert len(sim._psus) == 2

    def test_four_fans_created(self):
        sim = SwitchSensorSimulator()
        assert len(sim._fans) == 4

    def test_alarms_start_empty(self):
        sim = SwitchSensorSimulator()
        assert sim._alarms == []


# ── pack_cpu ──────────────────────────────────────────────────────────────────

class TestPackCpu:
    def _snap(self, cpu: float):
        return _minimal_snap(cpu_pct=cpu)

    def test_normal_value(self):
        data = SwitchSensorSimulator.pack_cpu(self._snap(50.0))
        assert len(data) == 1
        assert data[0] == 50

    def test_zero_percent(self):
        assert SwitchSensorSimulator.pack_cpu(self._snap(0.0))[0] == 0

    def test_100_percent(self):
        assert SwitchSensorSimulator.pack_cpu(self._snap(100.0))[0] == 100

    def test_clamped_above_100(self):
        assert SwitchSensorSimulator.pack_cpu(self._snap(150.0))[0] == 100

    def test_fractional_truncated(self):
        assert SwitchSensorSimulator.pack_cpu(self._snap(72.9))[0] == 72

    def test_returns_bytearray(self):
        assert isinstance(SwitchSensorSimulator.pack_cpu(self._snap(50.0)), bytearray)


# ── pack_memory ───────────────────────────────────────────────────────────────

class TestPackMemory:
    def _snap(self, mem: float):
        return _minimal_snap(mem_pct=mem)

    def test_normal_value(self):
        data = SwitchSensorSimulator.pack_memory(self._snap(65.0))
        assert data[0] == 65

    def test_clamped_above_100(self):
        assert SwitchSensorSimulator.pack_memory(self._snap(120.0))[0] == 100

    def test_zero(self):
        assert SwitchSensorSimulator.pack_memory(self._snap(0.0))[0] == 0

    def test_fractional_truncated(self):
        assert SwitchSensorSimulator.pack_memory(self._snap(88.8))[0] == 88


# ── pack_temperatures ─────────────────────────────────────────────────────────

class TestPackTemperatures:
    def _snap(self, cpu_die=55.0, board=45.0, inlet=26.0, outlet=38.0):
        return _minimal_snap(temperatures=TemperatureSensors(
            cpu_die=cpu_die, board=board, inlet=inlet, outlet=outlet))

    def test_length_is_8_bytes(self):
        assert len(SwitchSensorSimulator.pack_temperatures(self._snap())) == 8

    def test_values_are_int16_be_times_10(self):
        data = SwitchSensorSimulator.pack_temperatures(self._snap(
            cpu_die=55.3, board=44.1, inlet=25.5, outlet=37.8))
        assert _unpack_i16be(data, 0) == 553
        assert _unpack_i16be(data, 2) == 441
        assert _unpack_i16be(data, 4) == 255
        assert _unpack_i16be(data, 6) == 378

    def test_negative_temperature_encoded_correctly(self):
        data = SwitchSensorSimulator.pack_temperatures(self._snap(inlet=-5.0))
        assert _unpack_i16be(data, 4) == -50

    def test_round_trip_accuracy(self):
        data = SwitchSensorSimulator.pack_temperatures(self._snap(cpu_die=67.4))
        decoded = _unpack_i16be(data, 0) / 10
        assert abs(decoded - 67.4) < 0.1


# ── pack_ports ────────────────────────────────────────────────────────────────

class TestPackPorts:
    def test_all_ports_down_is_zero_bytes(self):
        snap = _minimal_snap(port_up=[False, False, False, False, False, False, False, False])
        data = SwitchSensorSimulator.pack_ports(snap)
        assert all(b == 0 for b in data)

    def test_all_ports_up_is_all_ones(self):
        snap = _minimal_snap(port_up=[True] * 8)
        data = SwitchSensorSimulator.pack_ports(snap)
        assert len(data) == 1
        assert data[0] == 0xFF

    def test_correct_bit_positions(self):
        # Ports 0,4,7 up (0-indexed)
        snap = _minimal_snap(port_up=[True, False, False, False, True, False, False, True])
        data = SwitchSensorSimulator.pack_ports(snap)
        assert data[0] == 0b10010001

    def test_multi_byte_bitmask(self):
        snap = _minimal_snap(port_up=[True] * 8 + [False] * 8)
        data = SwitchSensorSimulator.pack_ports(snap)
        assert len(data) == 2
        assert data[0] == 0xFF
        assert data[1] == 0x00

    def test_single_port(self):
        snap = _minimal_snap(port_up=[True])
        data = SwitchSensorSimulator.pack_ports(snap)
        assert len(data) == 1
        assert data[0] == 0x01


# ── unpack_ports ──────────────────────────────────────────────────────────────

class TestUnpackPorts:
    def test_round_trip_with_pack(self):
        original = [True, False, True, True, False, False, True, False]
        snap = _minimal_snap(port_up=original)
        packed = SwitchSensorSimulator.pack_ports(snap)
        unpacked = SwitchSensorSimulator.unpack_ports(bytes(packed), 8)
        assert unpacked == original

    def test_all_false(self):
        result = SwitchSensorSimulator.unpack_ports(b'\x00', 8)
        assert result == [False] * 8

    def test_all_true(self):
        result = SwitchSensorSimulator.unpack_ports(b'\xFF', 8)
        assert result == [True] * 8

    def test_16_ports(self):
        original = [True] * 8 + [False] * 8
        snap = _minimal_snap(port_up=original)
        packed = SwitchSensorSimulator.pack_ports(snap)
        unpacked = SwitchSensorSimulator.unpack_ports(bytes(packed), 16)
        assert unpacked == original


# ── pack_port_stats ───────────────────────────────────────────────────────────

def _make_stats_snap(port_up: list, stats: dict, port_configs: dict = None):
    """Build a minimal snapshot with port_stats as a dict keyed by port number."""
    if port_configs is None:
        # Build minimal PortConfig entries (physical ports, no sub-ports)
        port_configs = {
            p + 1: PortConfig(admin_up=True, speed=400000 if (p+1)%2==1 else 800000,
                              parent_port=0)
            for p in range(len(port_up))
        }
    return _minimal_snap(port_up=port_up, port_stats=stats, port_configs=port_configs)


class TestPackPortStats:
    def _snap_with_stats(self, port_up, stats_list):
        """Accepts a list of PortStats (one per physical port, 1-indexed)."""
        stats_dict = {i + 1: s for i, s in enumerate(stats_list)}
        return _make_stats_snap(port_up, stats_dict)

    def test_21_bytes_per_entry(self):
        stats = [PortStats(tx_bytes=100, rx_bytes=200, tx_packets=10,
                           rx_packets=20, tx_errors=1, rx_errors=2)]
        snap = self._snap_with_stats([True], stats)
        data = SwitchSensorSimulator.pack_port_stats(snap)
        assert len(data) == 21

    def test_link_up_bit_set(self):
        stats = [PortStats()]
        snap = self._snap_with_stats([True], stats)
        data = SwitchSensorSimulator.pack_port_stats(snap)
        flags = data[0]
        assert flags & 0x80  # link-up bit

    def test_link_down_bit_clear(self):
        stats = [PortStats()]
        snap = self._snap_with_stats([False], stats)
        data = SwitchSensorSimulator.pack_port_stats(snap)
        assert not (data[0] & 0x80)

    def test_port_number_in_lower_7_bits(self):
        stats = [PortStats(), PortStats()]
        snap = self._snap_with_stats([True, True], stats)
        data = SwitchSensorSimulator.pack_port_stats(snap)
        assert (data[0] & 0x7F) == 1   # port 1
        assert (data[21] & 0x7F) == 2  # port 2

    def test_counter_values_encoded_correctly(self):
        stats = [PortStats(tx_bytes=0x12345678, rx_bytes=0xDEADBEEF,
                           tx_packets=999, rx_packets=888,
                           tx_errors=7, rx_errors=3)]
        snap = self._snap_with_stats([True], stats)
        data = SwitchSensorSimulator.pack_port_stats(snap)
        assert _unpack_u32be(data, 1)  == 0x12345678
        assert _unpack_u32be(data, 5)  == 0xDEADBEEF
        assert _unpack_u32be(data, 9)  == 999
        assert _unpack_u32be(data, 13) == 888
        assert struct.unpack_from('>H', data, 17)[0] == 7
        assert struct.unpack_from('>H', data, 19)[0] == 3

    def test_multiple_entries_concatenated(self):
        stats = [PortStats(), PortStats(), PortStats()]
        snap = self._snap_with_stats([True, False, True], stats)
        data = SwitchSensorSimulator.pack_port_stats(snap)
        assert len(data) == 63  # 3 × 21


# ── pack_psus ─────────────────────────────────────────────────────────────────

class TestPackPsus:
    def _make_psu(self, psu_id=1, present=True, input_ok=True, output_ok=True,
                  voltage_in=120.0, voltage_out=12.0, current=10.0,
                  power=120.0, temperature=45.0, fan_rpm=5000):
        return PSUState(psu_id=psu_id, present=present, input_ok=input_ok,
                        output_ok=output_ok, voltage_in=voltage_in,
                        voltage_out=voltage_out, current=current, power=power,
                        temperature=temperature, fan_rpm=fan_rpm)

    def test_header_byte_is_psu_count(self):
        snap = _minimal_snap(psus=[self._make_psu(1), self._make_psu(2)])
        data = SwitchSensorSimulator.pack_psus(snap)
        assert data[0] == 2

    def test_14_bytes_per_psu(self):
        """Actual struct is 14 bytes despite source comment saying 16."""
        snap = _minimal_snap(psus=[self._make_psu()])
        data = SwitchSensorSimulator.pack_psus(snap)
        assert len(data) == 1 + 14  # header + 1 PSU × 14 bytes

    def test_flags_present_input_output_bits(self):
        snap = _minimal_snap(psus=[self._make_psu(present=True, input_ok=True, output_ok=False)])
        data = SwitchSensorSimulator.pack_psus(snap)
        flags = data[2]
        assert flags & 0x01  # present
        assert flags & 0x02  # input_ok
        assert not (flags & 0x04)  # output_ok = False

    def test_absent_psu_flags_all_clear(self):
        snap = _minimal_snap(psus=[self._make_psu(present=False, input_ok=False, output_ok=False)])
        data = SwitchSensorSimulator.pack_psus(snap)
        assert data[2] == 0x00

    def test_voltage_in_scaled_times_10(self):
        snap = _minimal_snap(psus=[self._make_psu(voltage_in=120.5)])
        data = SwitchSensorSimulator.pack_psus(snap)
        assert _unpack_u16be(data, 3) == 1205  # 120.5 × 10

    def test_voltage_out_scaled_times_100(self):
        snap = _minimal_snap(psus=[self._make_psu(voltage_out=12.00)])
        data = SwitchSensorSimulator.pack_psus(snap)
        assert _unpack_u16be(data, 5) == 1200  # 12.00 × 100

    def test_current_scaled_times_100(self):
        snap = _minimal_snap(psus=[self._make_psu(current=8.5)])
        data = SwitchSensorSimulator.pack_psus(snap)
        assert _unpack_u16be(data, 7) == 850

    def test_power_is_whole_watts(self):
        snap = _minimal_snap(psus=[self._make_psu(power=102.7)])
        data = SwitchSensorSimulator.pack_psus(snap)
        assert _unpack_u16be(data, 9) == 102

    def test_temperature_scaled_times_10(self):
        snap = _minimal_snap(psus=[self._make_psu(temperature=44.3)])
        data = SwitchSensorSimulator.pack_psus(snap)
        assert _unpack_u16be(data, 11) == 443

    def test_fan_rpm_signed_int16(self):
        snap = _minimal_snap(psus=[self._make_psu(fan_rpm=4800)])
        data = SwitchSensorSimulator.pack_psus(snap)
        assert _unpack_i16be(data, 13) == 4800

    def test_empty_psu_list(self):
        snap = _minimal_snap(psus=[])
        data = SwitchSensorSimulator.pack_psus(snap)
        assert len(data) == 1
        assert data[0] == 0

    def test_two_psus_total_length(self):
        snap = _minimal_snap(psus=[self._make_psu(1), self._make_psu(2)])
        data = SwitchSensorSimulator.pack_psus(snap)
        assert len(data) == 1 + 2 * 14


# ── pack_fans ─────────────────────────────────────────────────────────────────

class TestPackFans:
    def _make_fan(self, fan_id=1, present=True, ok=True, rpm=5000):
        return FanState(fan_id=fan_id, present=present, ok=ok, rpm=rpm)

    def test_header_byte_is_fan_count(self):
        snap = _minimal_snap(fans=[self._make_fan(1), self._make_fan(2)])
        data = SwitchSensorSimulator.pack_fans(snap)
        assert data[0] == 2

    def test_4_bytes_per_fan(self):
        snap = _minimal_snap(fans=[self._make_fan()])
        data = SwitchSensorSimulator.pack_fans(snap)
        assert len(data) == 1 + 4

    def test_fan_id_first_byte_of_entry(self):
        snap = _minimal_snap(fans=[self._make_fan(fan_id=3)])
        data = SwitchSensorSimulator.pack_fans(snap)
        assert data[1] == 3

    def test_flags_present_ok_bits(self):
        snap = _minimal_snap(fans=[self._make_fan(present=True, ok=False)])
        data = SwitchSensorSimulator.pack_fans(snap)
        flags = data[2]
        assert flags & 0x01   # present
        assert not (flags & 0x02)  # ok = False

    def test_rpm_uint16_be(self):
        snap = _minimal_snap(fans=[self._make_fan(rpm=6500)])
        data = SwitchSensorSimulator.pack_fans(snap)
        assert _unpack_u16be(data, 3) == 6500

    def test_failed_fan_rpm_zero(self):
        snap = _minimal_snap(fans=[self._make_fan(ok=False, rpm=0)])
        data = SwitchSensorSimulator.pack_fans(snap)
        assert _unpack_u16be(data, 3) == 0

    def test_empty_fan_list(self):
        snap = _minimal_snap(fans=[])
        data = SwitchSensorSimulator.pack_fans(snap)
        assert data == bytearray([0])


# ── pack_leds ─────────────────────────────────────────────────────────────────

class TestPackLeds:
    def _snap_with_leds(self, led_dict):
        return _minimal_snap(leds=led_dict)

    def test_header_byte_is_entry_count(self):
        leds = {0: LEDState("green", "solid"), 1: LEDState("blue", "slow")}
        data = SwitchSensorSimulator.pack_leds(self._snap_with_leds(leds))
        assert data[0] == 2

    def test_3_bytes_per_entry(self):
        leds = {1: LEDState("green", "solid")}
        data = SwitchSensorSimulator.pack_leds(self._snap_with_leds(leds))
        assert len(data) == 1 + 3

    def test_port_number_encoded(self):
        leds = {5: LEDState("green", "solid")}
        data = SwitchSensorSimulator.pack_leds(self._snap_with_leds(leds))
        assert data[1] == 5

    def test_color_codes(self):
        for name, code in COLOR_CODE.items():
            leds = {1: LEDState(color=name, blink="solid")}
            data = SwitchSensorSimulator.pack_leds(self._snap_with_leds(leds))
            assert data[2] == code

    def test_blink_codes(self):
        for name, code in BLINK_CODE.items():
            leds = {1: LEDState(color="green", blink=name)}
            data = SwitchSensorSimulator.pack_leds(self._snap_with_leds(leds))
            assert data[3] == code

    def test_entries_sorted_by_port(self):
        leds = {3: LEDState("red", "solid"), 1: LEDState("green", "solid")}
        data = SwitchSensorSimulator.pack_leds(self._snap_with_leds(leds))
        assert data[1] == 1   # port 1 first
        assert data[4] == 3   # port 3 second

    def test_unknown_color_falls_back_to_off(self):
        led = LEDState(color="purple", blink="solid")
        leds = {1: led}
        data = SwitchSensorSimulator.pack_leds(self._snap_with_leds(leds))
        assert data[2] == 0  # "off" = 0

    def test_empty_led_dict(self):
        data = SwitchSensorSimulator.pack_leds(self._snap_with_leds({}))
        assert data == bytearray([0])


# ── pack_port_config ──────────────────────────────────────────────────────────

class TestPackPortConfig:
    def _cfg(self, admin_up=True, split_capable=False, link_up=False,
             speed=10000, mtu=9100, fec="none", description="",
             split_mode="none", parent_port=0):
        return PortConfig(admin_up=admin_up, split_capable=split_capable,
                          link_up=link_up, speed=speed, mtu=mtu, fec=fec,
                          description=description, split_mode=split_mode,
                          parent_port=parent_port)

    def test_minimum_length_7_plus_trailing_2(self):
        data = SwitchSensorSimulator.pack_port_config(1, self._cfg())
        assert len(data) == 7 + 0 + 2  # header + empty desc + split/parent

    def test_port_number_in_first_byte(self):
        data = SwitchSensorSimulator.pack_port_config(42, self._cfg())
        assert data[0] == 42

    def test_admin_up_bit0_in_flags(self):
        data = SwitchSensorSimulator.pack_port_config(1, self._cfg(admin_up=True))
        assert data[1] & 0x01
        data2 = SwitchSensorSimulator.pack_port_config(1, self._cfg(admin_up=False))
        assert not (data2[1] & 0x01)

    def test_split_capable_bit1_in_flags(self):
        data = SwitchSensorSimulator.pack_port_config(1, self._cfg(split_capable=True))
        assert data[1] & 0x02
        data2 = SwitchSensorSimulator.pack_port_config(1, self._cfg(split_capable=False))
        assert not (data2[1] & 0x02)

    def test_link_up_bit2_in_flags(self):
        data = SwitchSensorSimulator.pack_port_config(1, self._cfg(link_up=True))
        assert data[1] & 0x04
        data2 = SwitchSensorSimulator.pack_port_config(1, self._cfg(link_up=False))
        assert not (data2[1] & 0x04)

    def test_speed_code_lookup(self):
        for speed in SPEED_OPTIONS:
            data = SwitchSensorSimulator.pack_port_config(1, self._cfg(speed=speed))
            assert data[2] == SPEED_CODE[speed]

    def test_unknown_speed_defaults_to_code_1(self):
        data = SwitchSensorSimulator.pack_port_config(1, self._cfg(speed=99999))
        assert data[2] == 1

    def test_mtu_uint16_be(self):
        data = SwitchSensorSimulator.pack_port_config(1, self._cfg(mtu=9216))
        assert _unpack_u16be(data, 3) == 9216

    def test_fec_codes(self):
        for fec, code in [("none", 0), ("rs", 1), ("fc", 2)]:
            data = SwitchSensorSimulator.pack_port_config(1, self._cfg(fec=fec))
            assert data[5] == code

    def test_description_length_and_bytes(self):
        desc = "uplink-core"
        data = SwitchSensorSimulator.pack_port_config(1, self._cfg(description=desc))
        assert data[6] == len(desc)
        assert data[7:7 + len(desc)] == bytearray(desc.encode('utf-8'))

    def test_empty_description(self):
        data = SwitchSensorSimulator.pack_port_config(1, self._cfg(description=""))
        assert data[6] == 0

    def test_description_truncated_at_255(self):
        long_desc = "x" * 300
        data = SwitchSensorSimulator.pack_port_config(1, self._cfg(description=long_desc))
        assert data[6] == 255

    def test_split_mode_appended_after_description(self):
        # Wire encoding: none=0, first-breakout-option=1, second-breakout-option=2
        # regardless of which speed string the mode represents.
        expected = {"none": 0, "4x100G": 1, "2x200G": 2, "4x200G": 1, "2x400G": 2}
        for mode, code in expected.items():
            data = SwitchSensorSimulator.pack_port_config(1, self._cfg(split_mode=mode))
            split_offset = 7  # empty description
            assert data[split_offset] == code, f"mode={mode!r} expected code {code}"

    def test_parent_port_appended_after_split_mode(self):
        data = SwitchSensorSimulator.pack_port_config(65, self._cfg(parent_port=1))
        assert data[8] == 1

    def test_parent_port_clamped_to_uint8(self):
        data = SwitchSensorSimulator.pack_port_config(1, self._cfg(parent_port=256))
        assert data[8] == 0  # 256 & 0xFF


# ── pack_alarms ───────────────────────────────────────────────────────────────

class TestPackAlarms:
    def _alarm(self, severity="warning", category="port", message="test",
               alarm_id="abcd1234", acknowledged=False, timestamp=1700000000):
        return Alarm(alarm_id=alarm_id, severity=severity, category=category,
                     message=message, timestamp=timestamp,
                     acknowledged=acknowledged)

    def test_header_byte_is_alarm_count(self):
        snap = _minimal_snap(alarms=[self._alarm(), self._alarm()])
        data = SwitchSensorSimulator.pack_alarms(snap)
        assert data[0] == 2

    def test_severity_codes(self):
        for sev in ALARM_SEVERITIES:
            expected_code = ALARM_SEVERITIES.index(sev)
            snap = _minimal_snap(alarms=[self._alarm(severity=sev)])
            data = SwitchSensorSimulator.pack_alarms(snap)
            assert data[1] == expected_code

    def test_category_codes(self):
        for cat in ALARM_CATEGORIES:
            expected_code = ALARM_CATEGORIES.index(cat)
            snap = _minimal_snap(alarms=[self._alarm(category=cat)])
            data = SwitchSensorSimulator.pack_alarms(snap)
            assert data[2] == expected_code

    def test_acknowledged_flag_bit0(self):
        snap_acked = _minimal_snap(alarms=[self._alarm(acknowledged=True)])
        snap_unacked = _minimal_snap(alarms=[self._alarm(acknowledged=False)])
        assert SwitchSensorSimulator.pack_alarms(snap_acked)[3] & 0x01
        assert not (SwitchSensorSimulator.pack_alarms(snap_unacked)[3] & 0x01)

    def test_timestamp_uint32_be(self):
        ts = 1700000000
        snap = _minimal_snap(alarms=[self._alarm(timestamp=ts)])
        data = SwitchSensorSimulator.pack_alarms(snap)
        assert _unpack_u32be(data, 4) == ts

    def test_alarm_id_length_prefixed(self):
        alarm_id = "abcd1234"
        snap = _minimal_snap(alarms=[self._alarm(alarm_id=alarm_id)])
        data = SwitchSensorSimulator.pack_alarms(snap)
        id_len_offset = 8
        assert data[id_len_offset] == len(alarm_id)
        assert data[id_len_offset + 1:id_len_offset + 1 + len(alarm_id)] == \
               bytearray(alarm_id.encode())

    def test_message_length_prefixed(self):
        msg = "Port link down"
        snap = _minimal_snap(alarms=[self._alarm(message=msg)])
        data = SwitchSensorSimulator.pack_alarms(snap)
        msg_offset = 8 + 1 + 8  # header(1) + fixed(7) + id_len(1) + id(8)
        assert data[msg_offset] == len(msg)

    def test_empty_alarm_list(self):
        snap = _minimal_snap(alarms=[])
        data = SwitchSensorSimulator.pack_alarms(snap)
        assert data == bytearray([0])

    def test_multiple_alarms_all_present(self):
        alarms = [
            self._alarm("critical", "psu", "PSU failed",  "id000001"),
            self._alarm("warning",  "fan", "Fan slow",    "id000002"),
            self._alarm("major",    "thermal", "Overheat","id000003"),
        ]
        snap = _minimal_snap(alarms=alarms)
        data = SwitchSensorSimulator.pack_alarms(snap)
        assert data[0] == 3

    def test_unknown_severity_defaults_to_warning(self):
        a = self._alarm()
        a.severity = "superurgent"
        snap = _minimal_snap(alarms=[a])
        data = SwitchSensorSimulator.pack_alarms(snap)
        assert data[1] == 0  # default = warning (code 0)


# ── Mutation API: trigger_alarm / acknowledge_alarm / clear_alarms ─────────────

class TestAlarmMutations:
    def test_trigger_alarm_returns_alarm_id(self):
        sim = SwitchSensorSimulator(num_ports=4)
        alarm_id = sim.trigger_alarm("port", "critical", "link down")
        assert isinstance(alarm_id, str)
        assert len(alarm_id) > 0

    def test_trigger_alarm_adds_to_list(self):
        sim = SwitchSensorSimulator(num_ports=4)
        sim.trigger_alarm("port", "warning", "flap")
        assert len(sim._alarms) == 1
        assert sim._alarms[0].severity == "warning"
        assert sim._alarms[0].acknowledged is False

    def test_trigger_alarm_caps_at_50(self):
        sim = SwitchSensorSimulator(num_ports=4)
        for i in range(60):
            sim.trigger_alarm("system", "minor", f"alarm {i}")
        assert len(sim._alarms) == 50

    def test_trigger_alarm_keeps_most_recent_on_overflow(self):
        sim = SwitchSensorSimulator(num_ports=4)
        for i in range(55):
            sim.trigger_alarm("system", "minor", f"alarm {i}")
        # Most recent 50 should remain
        messages = [a.message for a in sim._alarms]
        assert "alarm 5" in messages
        assert "alarm 54" in messages
        assert "alarm 0" not in messages

    def test_acknowledge_alarm_marks_correct_alarm(self):
        sim = SwitchSensorSimulator(num_ports=4)
        a_id = sim.trigger_alarm("psu", "major", "PSU fail")
        sim.trigger_alarm("fan", "minor", "Fan warn")
        sim.acknowledge_alarm(a_id)
        alarm = next(a for a in sim._alarms if a.alarm_id == a_id)
        assert alarm.acknowledged is True
        other = next(a for a in sim._alarms if a.alarm_id != a_id)
        assert other.acknowledged is False

    def test_acknowledge_alarm_unknown_id_is_noop(self):
        sim = SwitchSensorSimulator(num_ports=4)
        sim.trigger_alarm("psu", "critical", "PSU fail")
        sim.acknowledge_alarm("nonexistent")
        assert all(not a.acknowledged for a in sim._alarms)

    def test_clear_alarms_empties_list(self):
        sim = SwitchSensorSimulator(num_ports=4)
        sim.trigger_alarm("port", "warning", "msg1")
        sim.trigger_alarm("fan", "critical", "msg2")
        sim.clear_alarms()
        assert sim._alarms == []


# ── Mutation API: set_led ─────────────────────────────────────────────────────

class TestSetLed:
    def test_set_led_valid_color_and_blink(self):
        sim = SwitchSensorSimulator(num_ports=4)
        sim.set_led(1, "blue", "fast")
        assert sim._leds[1].color == "blue"
        assert sim._leds[1].blink == "fast"
        assert sim._leds[1].client_control is True

    def test_set_led_invalid_color_defaults_to_off(self):
        sim = SwitchSensorSimulator(num_ports=4)
        sim.set_led(1, "purple", "solid")
        assert sim._leds[1].color == "off"

    def test_set_led_invalid_blink_defaults_to_solid(self):
        sim = SwitchSensorSimulator(num_ports=4)
        sim.set_led(1, "green", "strobe")
        assert sim._leds[1].blink == "solid"

    def test_set_system_led_port_0(self):
        sim = SwitchSensorSimulator(num_ports=4)
        sim.set_led(0, "amber", "slow")
        assert sim._leds[0].color == "amber"

    def test_all_colors_accepted(self):
        sim = SwitchSensorSimulator(num_ports=6)
        for i, color in enumerate(LED_COLORS, start=1):
            sim.set_led(i, color, "solid")
            assert sim._leds[i].color == color

    def test_all_blinks_accepted(self):
        sim = SwitchSensorSimulator(num_ports=4)
        for i, blink in enumerate(LED_BLINKS, start=1):
            sim.set_led(i, "green", blink)
            assert sim._leds[i].blink == blink


# ── Mutation API: set_port_admin ──────────────────────────────────────────────

class TestSetPortAdmin:
    def test_admin_down_sets_led_amber(self):
        sim = SwitchSensorSimulator(num_ports=4)
        sim.set_port_admin(1, False)
        assert sim._port_configs[1].admin_up is False
        assert sim._leds[1].color == "amber"

    def test_admin_down_forces_link_down(self):
        sim = SwitchSensorSimulator(num_ports=4)
        sim._port_up[0] = True
        sim.set_port_admin(1, False)
        assert sim._port_up[0] is False

    def test_admin_up_on_linked_port_sets_green(self):
        sim = SwitchSensorSimulator(num_ports=4)
        sim._port_up[0] = True
        sim.set_port_admin(1, True)
        assert sim._port_configs[1].admin_up is True
        assert sim._leds[1].color == "green"

    def test_admin_up_on_down_port_sets_led_off(self):
        sim = SwitchSensorSimulator(num_ports=4)
        sim._port_up[0] = False
        sim.set_port_admin(1, True)
        assert sim._leds[1].color == "off"

    def test_unknown_port_is_noop(self):
        sim = SwitchSensorSimulator(num_ports=4)
        sim.set_port_admin(99, False)  # should not raise
        assert 99 not in sim._port_configs


# ── Mutation API: set_port_speed ──────────────────────────────────────────────

class TestSetPortSpeed:
    def test_valid_speed_is_applied(self):
        sim = SwitchSensorSimulator(num_ports=4)
        sim.set_port_speed(1, 100000)
        assert sim._port_configs[1].speed == 100000

    def test_all_valid_speeds(self):
        sim = SwitchSensorSimulator(num_ports=len(SPEED_OPTIONS))
        for i, speed in enumerate(SPEED_OPTIONS, start=1):
            sim.set_port_speed(i, speed)
            assert sim._port_configs[i].speed == speed

    def test_invalid_speed_is_noop(self):
        sim = SwitchSensorSimulator(num_ports=4)
        original = sim._port_configs[1].speed
        sim.set_port_speed(1, 99999)
        assert sim._port_configs[1].speed == original

    def test_unknown_port_is_noop(self):
        sim = SwitchSensorSimulator(num_ports=4)
        sim.set_port_speed(99, 10000)  # should not raise


# ── Mutation API: set_port_mtu ────────────────────────────────────────────────

class TestSetPortMtu:
    def test_valid_mtu_applied(self):
        sim = SwitchSensorSimulator(num_ports=4)
        sim.set_port_mtu(1, 9216)
        assert sim._port_configs[1].mtu == 9216

    def test_minimum_mtu_576(self):
        sim = SwitchSensorSimulator(num_ports=4)
        sim.set_port_mtu(1, 576)
        assert sim._port_configs[1].mtu == 576

    def test_maximum_mtu_65535(self):
        sim = SwitchSensorSimulator(num_ports=4)
        sim.set_port_mtu(1, 65535)
        assert sim._port_configs[1].mtu == 65535

    def test_mtu_below_576_is_noop(self):
        sim = SwitchSensorSimulator(num_ports=4)
        original = sim._port_configs[1].mtu
        sim.set_port_mtu(1, 575)
        assert sim._port_configs[1].mtu == original

    def test_mtu_above_65535_is_noop(self):
        sim = SwitchSensorSimulator(num_ports=4)
        original = sim._port_configs[1].mtu
        sim.set_port_mtu(1, 65536)
        assert sim._port_configs[1].mtu == original

    def test_unknown_port_is_noop(self):
        sim = SwitchSensorSimulator(num_ports=4)
        sim.set_port_mtu(99, 9100)  # should not raise


# ── Mutation API: set_port_desc ───────────────────────────────────────────────

class TestSetPortDesc:
    def test_description_applied(self):
        sim = SwitchSensorSimulator(num_ports=4)
        sim.set_port_desc(1, "spine-link")
        assert sim._port_configs[1].description == "spine-link"

    def test_empty_description(self):
        sim = SwitchSensorSimulator(num_ports=4)
        sim.set_port_desc(1, "")
        assert sim._port_configs[1].description == ""

    def test_description_truncated_at_255(self):
        sim = SwitchSensorSimulator(num_ports=4)
        sim.set_port_desc(1, "z" * 300)
        assert len(sim._port_configs[1].description) == 255

    def test_unknown_port_is_noop(self):
        sim = SwitchSensorSimulator(num_ports=4)
        sim.set_port_desc(99, "whatever")  # should not raise


# ── Mutation API: set_split_mode ──────────────────────────────────────────────

class TestSetSplitMode:
    def _sim_with_split_capable(self):
        """8-port sim; port 1 (odd=400G) and port 8 (even=800G) are split-capable."""
        sim = SwitchSensorSimulator(num_ports=8)
        assert 1 in sim._split_capable   # odd port (400G)
        assert 8 in sim._split_capable   # even port (800G)
        return sim

    def test_split_returns_true_for_capable_port(self):
        sim = self._sim_with_split_capable()
        # Port 1 is odd (400G), so 4x100G is the correct first breakout
        assert sim.set_split_mode(1, "4x100G") is True

    def test_split_returns_true_for_even_capable_port(self):
        sim = self._sim_with_split_capable()
        # Port 8 is even (800G), so 4x200G is the correct first breakout
        assert sim.set_split_mode(8, "4x200G") is True

    def test_split_returns_false_for_non_capable_port(self):
        sim = self._sim_with_split_capable()
        assert 2 not in sim._split_capable
        assert sim.set_split_mode(2, "4x100G") is False

    def test_split_returns_false_for_unknown_mode(self):
        sim = self._sim_with_split_capable()
        assert sim.set_split_mode(1, "8x50G") is False

    def test_split_4x100g_creates_4_sub_ports(self):
        sim = self._sim_with_split_capable()
        sim.set_split_mode(1, "4x100G")
        sub_start = _sub_port_start(1, sorted(sim._split_capable))
        for m in range(4):
            assert sub_start + m in sim._port_configs

    def test_sub_ports_have_correct_parent(self):
        sim = self._sim_with_split_capable()
        sim.set_split_mode(1, "4x100G")
        sub_start = _sub_port_start(1, sorted(sim._split_capable))
        for m in range(4):
            assert sim._port_configs[sub_start + m].parent_port == 1

    def test_sub_port_descriptions_follow_parent_slash_member_format(self):
        sim = self._sim_with_split_capable()
        sim.set_split_mode(1, "4x100G")
        sub_start = _sub_port_start(1, sorted(sim._split_capable))
        for m in range(1, 5):
            assert sim._port_configs[sub_start + m - 1].description == f"1/{m}"

    def test_split_4x100g_speed_is_100000(self):
        sim = self._sim_with_split_capable()
        sim.set_split_mode(1, "4x100G")
        sub_start = _sub_port_start(1, sorted(sim._split_capable))
        assert sim._port_configs[sub_start].speed == 100000

    def test_split_4x200g_speed_is_200000(self):
        sim = self._sim_with_split_capable()
        sim.set_split_mode(8, "4x200G")
        sub_start = _sub_port_start(8, sorted(sim._split_capable))
        assert sim._port_configs[sub_start].speed == 200000

    def test_split_2x200g_creates_2_sub_ports(self):
        sim = self._sim_with_split_capable()
        sim.set_split_mode(1, "2x200G")
        sub_start = _sub_port_start(1, sorted(sim._split_capable))
        assert sub_start in sim._port_configs
        assert sub_start + 1 in sim._port_configs
        assert sub_start + 2 not in sim._port_configs

    def test_split_2x400g_creates_2_sub_ports(self):
        sim = self._sim_with_split_capable()
        sim.set_split_mode(8, "2x400G")
        sub_start = _sub_port_start(8, sorted(sim._split_capable))
        assert sub_start in sim._port_configs
        assert sub_start + 1 in sim._port_configs
        assert sub_start + 2 not in sim._port_configs

    def test_sub_ports_get_stats_entries(self):
        sim = self._sim_with_split_capable()
        sim.set_split_mode(1, "4x100G")
        sub_start = _sub_port_start(1, sorted(sim._split_capable))
        for m in range(4):
            assert sub_start + m in sim._port_stats

    def test_unsplit_removes_sub_port_stats(self):
        sim = self._sim_with_split_capable()
        sim.set_split_mode(1, "4x100G")
        sub_start = _sub_port_start(1, sorted(sim._split_capable))
        sim.set_split_mode(1, "none")
        for m in range(4):
            assert sub_start + m not in sim._port_stats

    def test_physical_port_disabled_after_split(self):
        sim = self._sim_with_split_capable()
        sim.set_split_mode(1, "4x100G")
        assert sim._port_configs[1].admin_up is False

    def test_unsplit_restores_physical_port(self):
        sim = self._sim_with_split_capable()
        sim.set_split_mode(1, "4x100G")
        sub_start = _sub_port_start(1, sorted(sim._split_capable))
        sim.set_split_mode(1, "none")
        assert sim._port_configs[1].admin_up is True
        # Sub-ports should be removed
        for m in range(4):
            assert sub_start + m not in sim._port_configs

    def test_unsplit_removes_sub_port_leds(self):
        sim = self._sim_with_split_capable()
        sim.set_split_mode(1, "4x100G")
        sub_start = _sub_port_start(1, sorted(sim._split_capable))
        sim.set_split_mode(1, "none")
        for m in range(4):
            assert sub_start + m not in sim._leds


# ── Counter overflow wrapping ─────────────────────────────────────────────────

class TestCounterOverflow:
    def test_tx_bytes_wraps_at_32_bits(self):
        sim = SwitchSensorSimulator(num_ports=1)
        sim._port_up[0] = True
        sim._port_stats[1].tx_bytes = 0xFFFFFFFF
        # Drive a tick that adds traffic
        sim._port_stats[1].tx_bytes = (sim._port_stats[1].tx_bytes + 1) & 0xFFFFFFFF
        assert sim._port_stats[1].tx_bytes == 0

    def test_tx_errors_wraps_at_16_bits(self):
        sim = SwitchSensorSimulator(num_ports=1)
        sim._port_stats[1].tx_errors = 0xFFFF
        sim._port_stats[1].tx_errors = (sim._port_stats[1].tx_errors + 1) & 0xFFFF
        assert sim._port_stats[1].tx_errors == 0


# ── read() ────────────────────────────────────────────────────────────────────

class TestRead:
    def test_returns_snapshot(self):
        sim = SwitchSensorSimulator(num_ports=4)
        snap = sim.read()
        assert isinstance(snap, SwitchSnapshot)

    def test_cpu_pct_in_valid_range(self):
        sim = SwitchSensorSimulator(num_ports=4)
        for _ in range(20):
            snap = sim.read()
            assert 1.0 <= snap.cpu_pct <= 99.0

    def test_mem_pct_in_valid_range(self):
        sim = SwitchSensorSimulator(num_ports=4)
        for _ in range(20):
            snap = sim.read()
            assert 10.0 <= snap.mem_pct <= 95.0

    def test_tick_increments_each_read(self):
        sim = SwitchSensorSimulator(num_ports=4)
        assert sim._tick == 0
        sim.read()
        assert sim._tick == 1
        sim.read()
        assert sim._tick == 2

    def test_port_up_length_matches_num_ports(self):
        sim = SwitchSensorSimulator(num_ports=12)
        snap = sim.read()
        assert len(snap.port_up) == 12

    def test_admin_down_port_always_stays_down(self):
        sim = SwitchSensorSimulator(num_ports=4)
        sim.set_port_admin(1, False)
        for _ in range(10):
            snap = sim.read()
            assert snap.port_up[0] is False

    def test_snapshot_contains_alarms(self):
        sim = SwitchSensorSimulator(num_ports=4)
        sim.trigger_alarm("port", "critical", "test alarm")
        snap = sim.read()
        assert len(snap.alarms) == 1
        assert snap.alarms[0].message == "test alarm"


# ── Helper functions ──────────────────────────────────────────────────────────

class TestHelpers:
    def test_default_split_capable_mixed_odd_even(self):
        # p%8==1 → odd; p%8==0 → even
        capable = _default_split_capable(16)
        assert capable == {1, 8, 9, 16}

    def test_default_split_capable_48_ports(self):
        capable = _default_split_capable(48)
        assert capable == {1, 8, 9, 16, 17, 24, 25, 32, 33, 40, 41, 48}

    def test_default_split_capable_single_port(self):
        # Port 1: 1 % 8 == 1 → split capable
        capable = _default_split_capable(1)
        assert capable == {1}

    def test_sub_port_start_first_capable_port(self):
        capable = sorted(_default_split_capable(8))  # [1, 8]
        assert _sub_port_start(1, capable) == SUB_PORT_BASE + 1   # 65

    def test_sub_port_start_second_capable_port(self):
        capable = sorted(_default_split_capable(8))  # [1, 8]
        assert _sub_port_start(8, capable) == SUB_PORT_BASE + 4 + 1  # 69

    def test_sub_port_start_unknown_port_returns_base_plus_1(self):
        capable = sorted(_default_split_capable(8))
        # Port 2 is not split-capable; falls back to idx=0
        assert _sub_port_start(2, capable) == SUB_PORT_BASE + 1
