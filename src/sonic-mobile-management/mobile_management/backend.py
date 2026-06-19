"""
backend.py — Pluggable sensor data backends for the BLE peripheral.

Two backends are provided:
  - SimBackend:   wraps SwitchSensorSimulator (fake data, works everywhere)
  - SonicBackend: reads real telemetry from SONiC Redis databases

The daemon selects one at startup based on CLI arg / CONFIG_DB setting.
"""

from __future__ import annotations

import logging
import time
from typing import Dict, List, Optional

from mobile_management.sensor_simulator import (
    SwitchSensorSimulator, SwitchSnapshot, TemperatureSensors,
    PortStats, PSUState, FanState, LEDState, PortConfig, Alarm,
)

log = logging.getLogger(__name__)


class SimBackend:
    """Wraps SwitchSensorSimulator — fully functional simulated data."""

    def __init__(self, num_ports: int = 48, flap_rate: float = 0.005):
        self.sim = SwitchSensorSimulator(num_ports=num_ports, flap_rate=flap_rate)

    @property
    def num_ports(self) -> int:
        return self.sim.num_ports

    def read(self) -> SwitchSnapshot:
        return self.sim.read()

    def trigger_alarm(self, category, severity, message) -> str:
        return self.sim.trigger_alarm(category, severity, message)

    def acknowledge_alarm(self, alarm_id):
        self.sim.acknowledge_alarm(alarm_id)

    def clear_alarms(self):
        self.sim.clear_alarms()

    def set_led(self, port, color, blink):
        self.sim.set_led(port, color, blink)

    def set_port_admin(self, port, up):
        self.sim.set_port_admin(port, up)

    def set_port_speed(self, port, speed_mbps):
        self.sim.set_port_speed(port, speed_mbps)

    def set_port_mtu(self, port, mtu):
        self.sim.set_port_mtu(port, mtu)

    def set_port_desc(self, port, description):
        self.sim.set_port_desc(port, description)

    def set_split_mode(self, port, mode) -> bool:
        return self.sim.set_split_mode(port, mode)

    def set_fan_failure(self, fan_id):
        self.sim.set_fan_failure(fan_id)

    def demo_reset(self):
        self.sim.demo_reset()

    @property
    def _port_configs(self):
        return self.sim._port_configs

    @property
    def _port_stats(self):
        return self.sim._port_stats

    @property
    def _leds(self):
        return self.sim._leds

    @property
    def _psus(self):
        return self.sim._psus

    @property
    def _fans(self):
        return self.sim._fans


class SonicBackend:
    """
    Reads real switch telemetry from SONiC Redis databases via swsscommon
    and writes port configuration changes back to CONFIG_DB.

    Data sources:
      CPU/mem        /proc/loadavg, /proc/meminfo
      Temperatures   STATE_DB TEMPERATURE_INFO, fallback to /sys/class/thermal
      Port oper      APPL_DB PORT_TABLE
      Port counters  COUNTERS_DB via COUNTERS_PORT_NAME_MAP
      Port config    CONFIG_DB PORT
      PSU            STATE_DB PSU_INFO
      Fans           STATE_DB FAN_INFO

    Write path:
      set_port_admin/speed/mtu/desc write to CONFIG_DB PORT via ConfigDBConnector.
      set_led is a no-op (platform-specific, no generic SONiC API).
      set_split_mode is a no-op (breakout requires orchagent restart).
    """

    _DEFAULT_PSU = [PSUState(psu_id=i+1, present=True, input_ok=True,
                             output_ok=True, voltage_in=120.0, voltage_out=12.0,
                             current=10.0, power=120.0, temperature=40.0,
                             fan_rpm=5000) for i in range(2)]
    _DEFAULT_FAN = [FanState(fan_id=i+1, present=True, ok=True, rpm=5000)
                    for i in range(4)]

    def __init__(self, num_ports: int = 48):
        self._num_ports = num_ports
        self._alarms: List[Alarm] = []
        self._leds: Dict[int, LEDState] = {}
        self._port_configs: Dict[int, PortConfig] = {}
        self._port_stats: Dict[int, PortStats] = {}
        self._psus: List[PSUState] = []
        self._fans: List[FanState] = []

        # BLE port index (1-based) → SONiC interface name (e.g. "Ethernet0")
        self._port_name_map: Dict[int, str] = {}
        # SONiC interface name → BLE port index
        self._name_to_port: Dict[str, int] = {}
        # SONiC interface name → COUNTERS_DB OID
        self._counter_oid_map: Dict[str, str] = {}

        self._db = None
        self._cfg_db = None

        try:
            from swsscommon.swsscommon import SonicV2Connector, ConfigDBConnector
            self._db = SonicV2Connector()
            self._db.connect(self._db.CONFIG_DB)
            self._db.connect(self._db.STATE_DB)
            self._db.connect(self._db.COUNTERS_DB)
            self._db.connect(self._db.APPL_DB)

            self._cfg_db = ConfigDBConnector()
            self._cfg_db.connect()

            log.info("SonicBackend: connected to Redis databases")
        except Exception as exc:
            log.warning(f"SonicBackend: Redis connection failed: {exc}")
            self._db = None
            self._cfg_db = None

        self._init_port_map()
        self._init_counter_oid_map()

    def _init_port_map(self):
        """Build BLE port index ↔ EthernetN mapping from CONFIG_DB PORT table."""
        if self._db is None:
            for p in range(1, self._num_ports + 1):
                self._port_configs[p] = PortConfig()
                self._leds[p] = LEDState()
                self._port_stats[p] = PortStats()
            return

        try:
            keys = self._db.keys(self._db.CONFIG_DB, "PORT|*") or []
            iface_names = sorted(
                [k.split("|", 1)[1] for k in keys if "|" in k],
                key=self._iface_sort_key,
            )

            port_num = 0
            for iface in iface_names:
                port_num += 1
                if port_num > self._num_ports:
                    break

                self._port_name_map[port_num] = iface
                self._name_to_port[iface] = port_num

                data = self._db.get_all(self._db.CONFIG_DB, f"PORT|{iface}") or {}
                self._port_configs[port_num] = PortConfig(
                    admin_up=data.get("admin_status", "up") == "up",
                    speed=int(data.get("speed", "10000")),
                    mtu=int(data.get("mtu", "9100")),
                    description=data.get("description", ""),
                    fec=data.get("fec", "none"),
                )
                self._leds[port_num] = LEDState()
                self._port_stats[port_num] = PortStats()

            for p in range(port_num + 1, self._num_ports + 1):
                self._port_configs[p] = PortConfig()
                self._leds[p] = LEDState()
                self._port_stats[p] = PortStats()

            log.info(f"SonicBackend: mapped {port_num} ports from CONFIG_DB")
        except Exception as exc:
            log.warning(f"SonicBackend: port init failed: {exc}")
            for p in range(1, self._num_ports + 1):
                self._port_configs[p] = PortConfig()
                self._leds[p] = LEDState()
                self._port_stats[p] = PortStats()

    @staticmethod
    def _iface_sort_key(name: str):
        """Sort Ethernet0, Ethernet4, ... Ethernet120 numerically."""
        import re
        m = re.match(r"Ethernet(\d+)", name)
        return int(m.group(1)) if m else 0

    def _init_counter_oid_map(self):
        """Load COUNTERS_PORT_NAME_MAP from COUNTERS_DB."""
        if self._db is None:
            return
        try:
            mapping = self._db.get_all(
                self._db.COUNTERS_DB, "COUNTERS_PORT_NAME_MAP") or {}
            self._counter_oid_map = dict(mapping)
            log.info(f"SonicBackend: loaded {len(self._counter_oid_map)} counter OIDs")
        except Exception as exc:
            log.warning(f"SonicBackend: counter OID map failed: {exc}")

    @property
    def num_ports(self) -> int:
        return self._num_ports

    def port_name(self, ble_port: int) -> Optional[str]:
        """Return the SONiC interface name for a BLE port index."""
        return self._port_name_map.get(ble_port)

    def _read_cpu_mem(self) -> tuple:
        cpu_pct, mem_pct = 0.0, 0.0
        try:
            with open("/proc/loadavg") as f:
                load1 = float(f.read().split()[0])
                import os
                cpu_pct = min(100.0, load1 / os.cpu_count() * 100.0)
        except Exception:
            pass
        try:
            with open("/proc/meminfo") as f:
                lines = f.readlines()
            info = {}
            for line in lines:
                parts = line.split(":")
                if len(parts) == 2:
                    info[parts[0].strip()] = int(parts[1].strip().split()[0])
            total = info.get("MemTotal", 1)
            avail = info.get("MemAvailable", total)
            mem_pct = (1.0 - avail / total) * 100.0
        except Exception:
            pass
        return cpu_pct, mem_pct

    def _read_temperatures(self) -> TemperatureSensors:
        """Read from STATE_DB TEMPERATURE_INFO, fallback to /sys/class/thermal."""
        temps = TemperatureSensors(cpu_die=0.0, board=0.0, inlet=0.0, outlet=0.0)

        if self._db is not None:
            try:
                keys = self._db.keys(self._db.STATE_DB, "TEMPERATURE_INFO|*") or []
                sensor_vals = {}
                for key in sorted(keys):
                    data = self._db.get_all(self._db.STATE_DB, key) or {}
                    name = key.split("|", 1)[1].lower() if "|" in key else ""
                    temp_val = float(data.get("temperature", "0"))
                    sensor_vals[name] = temp_val

                if sensor_vals:
                    for name, val in sensor_vals.items():
                        if "cpu" in name or "core" in name:
                            temps.cpu_die = max(temps.cpu_die, val)
                        elif "board" in name or "switch" in name or "asic" in name:
                            temps.board = max(temps.board, val)
                        elif "inlet" in name or "intake" in name or "front" in name:
                            temps.inlet = max(temps.inlet, val)
                        elif "outlet" in name or "exhaust" in name or "rear" in name:
                            temps.outlet = max(temps.outlet, val)
                    if temps.cpu_die > 0:
                        return temps
            except Exception:
                pass

        try:
            import glob as _glob
            zones = sorted(_glob.glob("/sys/class/thermal/thermal_zone*/temp"))
            if zones:
                temps.cpu_die = int(open(zones[0]).read().strip()) / 1000.0
            if len(zones) > 1:
                temps.board = int(open(zones[1]).read().strip()) / 1000.0
        except Exception:
            pass
        return temps

    def _read_port_states(self) -> List[bool]:
        """Read port oper status from APPL_DB, mapped by interface name."""
        port_up = [False] * self._num_ports
        if self._db is None:
            return port_up
        try:
            for ble_port, iface in self._port_name_map.items():
                data = self._db.get_all(
                    self._db.APPL_DB, f"PORT_TABLE:{iface}") or {}
                if data.get("oper_status", "down") == "up":
                    port_up[ble_port - 1] = True
        except Exception:
            pass
        return port_up

    def _read_port_counters(self):
        """Read per-port counters from COUNTERS_DB."""
        if self._db is None:
            return
        try:
            for ble_port, iface in self._port_name_map.items():
                oid = self._counter_oid_map.get(iface)
                if not oid:
                    continue
                data = self._db.get_all(
                    self._db.COUNTERS_DB, f"COUNTERS:{oid}") or {}
                if not data:
                    continue
                self._port_stats[ble_port] = PortStats(
                    tx_bytes=int(data.get("SAI_PORT_STAT_IF_OUT_OCTETS", "0")) & 0xFFFFFFFF,
                    rx_bytes=int(data.get("SAI_PORT_STAT_IF_IN_OCTETS", "0")) & 0xFFFFFFFF,
                    tx_packets=int(data.get("SAI_PORT_STAT_IF_OUT_UCAST_PKTS", "0")) & 0xFFFFFFFF,
                    rx_packets=int(data.get("SAI_PORT_STAT_IF_IN_UCAST_PKTS", "0")) & 0xFFFFFFFF,
                    tx_errors=int(data.get("SAI_PORT_STAT_IF_OUT_ERRORS", "0")) & 0xFFFF,
                    rx_errors=int(data.get("SAI_PORT_STAT_IF_IN_ERRORS", "0")) & 0xFFFF,
                )
        except Exception as exc:
            log.debug(f"Counter read failed: {exc}")

    def _refresh_port_configs(self):
        """Re-read port admin/speed/mtu/desc from CONFIG_DB (catches CLI changes)."""
        if self._db is None:
            return
        try:
            for ble_port, iface in self._port_name_map.items():
                data = self._db.get_all(
                    self._db.CONFIG_DB, f"PORT|{iface}") or {}
                if not data:
                    continue
                cfg = self._port_configs.get(ble_port)
                if cfg is None:
                    continue
                cfg.admin_up = data.get("admin_status", "up") == "up"
                cfg.speed = int(data.get("speed", str(cfg.speed)))
                cfg.mtu = int(data.get("mtu", str(cfg.mtu)))
                cfg.description = data.get("description", cfg.description)
                cfg.fec = data.get("fec", cfg.fec)
        except Exception:
            pass

    def _read_psu_state(self) -> List[PSUState]:
        psus = []
        if self._db is None:
            self._psus = [PSUState(**vars(p)) for p in self._DEFAULT_PSU]
            return self._psus
        try:
            keys = self._db.keys(self._db.STATE_DB, "PSU_INFO|*") or []
            for i, key in enumerate(sorted(keys)):
                data = self._db.get_all(self._db.STATE_DB, key) or {}
                psus.append(PSUState(
                    psu_id=i + 1,
                    present=data.get("presence", "true") == "true",
                    input_ok=data.get("status", "true") == "true",
                    output_ok=data.get("status", "true") == "true",
                    voltage_in=float(data.get("input_voltage", "0")),
                    voltage_out=float(data.get("output_voltage", "0")),
                    current=float(data.get("output_current", "0")),
                    power=float(data.get("output_power", "0")),
                    temperature=float(data.get("temp", "0")),
                    fan_rpm=0,
                ))
        except Exception:
            pass
        if not psus:
            psus = [PSUState(**vars(p)) for p in self._DEFAULT_PSU]
        self._psus = psus
        return psus

    def _read_fan_state(self) -> List[FanState]:
        fans = []
        if self._db is None:
            self._fans = [FanState(**vars(f)) for f in self._DEFAULT_FAN]
            return self._fans
        try:
            keys = self._db.keys(self._db.STATE_DB, "FAN_INFO|*") or []
            for i, key in enumerate(sorted(keys)):
                data = self._db.get_all(self._db.STATE_DB, key) or {}
                fans.append(FanState(
                    fan_id=i + 1,
                    present=data.get("presence", "true") == "true",
                    ok=data.get("status", "true") == "true",
                    rpm=int(float(data.get("speed", "0"))),
                ))
        except Exception:
            pass
        if not fans:
            fans = [FanState(**vars(f)) for f in self._DEFAULT_FAN]
        self._fans = fans
        return fans

    def read(self) -> SwitchSnapshot:
        self._refresh_port_configs()
        self._read_port_counters()

        cpu, mem = self._read_cpu_mem()
        temps = self._read_temperatures()
        port_up = self._read_port_states()
        psus = self._read_psu_state()
        fans = self._read_fan_state()

        for i, up in enumerate(port_up):
            p = i + 1
            if p in self._leds:
                cfg = self._port_configs.get(p)
                if cfg and not cfg.admin_up:
                    self._leds[p] = LEDState(color="amber", blink="solid")
                else:
                    self._leds[p] = LEDState(
                        color="green" if up else "off", blink="solid")

        return SwitchSnapshot(
            timestamp=time.time(),
            cpu_pct=cpu,
            mem_pct=mem,
            temperatures=temps,
            port_up=port_up,
            port_stats=dict(self._port_stats),
            psus=psus,
            fans=fans,
            leds=dict(self._leds),
            port_configs={k: PortConfig(
                admin_up=v.admin_up, speed=v.speed, mtu=v.mtu,
                description=v.description, fec=v.fec,
                split_capable=v.split_capable, split_mode=v.split_mode,
                parent_port=v.parent_port, link_up=v.link_up,
            ) for k, v in self._port_configs.items()},
            alarms=list(self._alarms),
        )

    # ── Write path ────────────────────────────────────────────────────────────

    def _write_port_field(self, ble_port: int, field: str, value: str) -> bool:
        """Write a single field to CONFIG_DB PORT|<iface>."""
        iface = self._port_name_map.get(ble_port)
        if not iface or self._cfg_db is None:
            return False
        try:
            self._cfg_db.mod_entry("PORT", iface, {field: value})
            log.info(f"SonicBackend: {iface} {field}={value}")
            return True
        except Exception as exc:
            log.warning(f"SonicBackend: write {iface}.{field} failed: {exc}")
            return False

    def set_port_admin(self, port, up):
        status = "up" if up else "down"
        if self._write_port_field(port, "admin_status", status):
            cfg = self._port_configs.get(port)
            if cfg:
                cfg.admin_up = up

    def set_port_speed(self, port, speed_mbps):
        from mobile_management.sensor_simulator import SPEED_OPTIONS
        if speed_mbps not in SPEED_OPTIONS:
            log.warning(f"SonicBackend: invalid speed {speed_mbps}")
            return
        if self._write_port_field(port, "speed", str(speed_mbps)):
            cfg = self._port_configs.get(port)
            if cfg:
                cfg.speed = speed_mbps

    def set_port_mtu(self, port, mtu):
        if not (576 <= mtu <= 9216):
            log.warning(f"SonicBackend: invalid MTU {mtu}")
            return
        if self._write_port_field(port, "mtu", str(mtu)):
            cfg = self._port_configs.get(port)
            if cfg:
                cfg.mtu = mtu

    def set_port_desc(self, port, description):
        desc = str(description)[:255]
        if self._write_port_field(port, "description", desc):
            cfg = self._port_configs.get(port)
            if cfg:
                cfg.description = desc

    def set_led(self, port, color, blink):
        log.debug(f"SonicBackend: set_led({port}, {color}, {blink}) — no-op (platform-specific)")

    def set_split_mode(self, port, mode) -> bool:
        log.warning(f"SonicBackend: set_split_mode({port}, {mode}) — "
                     "requires orchagent restart, not supported via BLE")
        return False

    def trigger_alarm(self, category, severity, message) -> str:
        return ""

    def acknowledge_alarm(self, alarm_id):
        pass

    def clear_alarms(self):
        pass

    def set_fan_failure(self, fan_id):
        pass

    def demo_reset(self):
        pass


def create_backend(kind: str = "auto", num_ports: int = 48):
    """Factory: create the appropriate sensor backend.

    kind: "sim", "sonic", or "auto" (try sonic, fall back to sim).
    """
    if kind == "sim":
        log.info("Backend: using SimBackend (simulated data)")
        return SimBackend(num_ports=num_ports)

    if kind == "sonic":
        log.info("Backend: using SonicBackend (real switch data)")
        return SonicBackend(num_ports=num_ports)

    # Auto: try sonic, fall back to sim
    try:
        from swsscommon.swsscommon import SonicV2Connector  # noqa: F401
        backend = SonicBackend(num_ports=num_ports)
        log.info("Backend: auto-selected SonicBackend (swsscommon available)")
        return backend
    except ImportError:
        log.info("Backend: auto-selected SimBackend (swsscommon not available)")
        return SimBackend(num_ports=num_ports)
