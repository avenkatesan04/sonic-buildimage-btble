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
    Reads real switch telemetry from SONiC Redis databases via swsscommon.

    Falls back gracefully when tables are empty (e.g. on a VS image).
    Mutation methods (set_led, set_port_admin, etc.) are no-ops in this
    backend — real config changes go through CONFIG_DB / SONiC CLI, not
    through BLE commands. The BLE app will show real state but cannot
    mutate it (read-only on production switches).
    """

    def __init__(self, num_ports: int = 48):
        self._num_ports = num_ports
        self._alarms: List[Alarm] = []
        self._leds: Dict[int, LEDState] = {}
        self._port_configs: Dict[int, PortConfig] = {}
        self._port_stats: Dict[int, PortStats] = {}
        self._psus: List[PSUState] = []
        self._fans: List[FanState] = []

        try:
            from swsscommon.swsscommon import SonicV2Connector
            self._db = SonicV2Connector()
            self._db.connect(self._db.CONFIG_DB)
            self._db.connect(self._db.STATE_DB)
            self._db.connect(self._db.COUNTERS_DB)
            self._db.connect(self._db.APPL_DB)
            log.info("SonicBackend: connected to Redis databases")
        except Exception as exc:
            log.warning(f"SonicBackend: Redis connection failed: {exc}")
            self._db = None

        self._init_port_configs()

    def _init_port_configs(self):
        """Seed port configs from CONFIG_DB PORT table."""
        if self._db is None:
            for p in range(1, self._num_ports + 1):
                self._port_configs[p] = PortConfig()
                self._leds[p] = LEDState()
                self._port_stats[p] = PortStats()
            return

        try:
            keys = self._db.keys(self._db.CONFIG_DB, "PORT|*") or []
            port_num = 0
            for key in sorted(keys):
                port_num += 1
                if port_num > self._num_ports:
                    break
                data = self._db.get_all(self._db.CONFIG_DB, key) or {}
                admin_up = data.get("admin_status", "up") == "up"
                speed = int(data.get("speed", "10000"))
                mtu = int(data.get("mtu", "9100"))
                desc = data.get("description", "")
                fec = data.get("fec", "none")

                self._port_configs[port_num] = PortConfig(
                    admin_up=admin_up, speed=speed, mtu=mtu,
                    description=desc, fec=fec,
                )
                self._leds[port_num] = LEDState()
                self._port_stats[port_num] = PortStats()

            if port_num < self._num_ports:
                for p in range(port_num + 1, self._num_ports + 1):
                    self._port_configs[p] = PortConfig()
                    self._leds[p] = LEDState()
                    self._port_stats[p] = PortStats()

            log.info(f"SonicBackend: loaded {port_num} ports from CONFIG_DB")
        except Exception as exc:
            log.warning(f"SonicBackend: port init failed: {exc}")
            for p in range(1, self._num_ports + 1):
                self._port_configs[p] = PortConfig()
                self._leds[p] = LEDState()
                self._port_stats[p] = PortStats()

    @property
    def num_ports(self) -> int:
        return self._num_ports

    def _read_cpu_mem(self) -> tuple:
        """Read CPU and memory from /proc."""
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
        """Read thermal sensors from STATE_DB or /sys/class/thermal."""
        temps = TemperatureSensors(cpu_die=0.0, board=0.0, inlet=0.0, outlet=0.0)
        try:
            import glob
            zones = sorted(glob.glob("/sys/class/thermal/thermal_zone*/temp"))
            if zones:
                temps.cpu_die = int(open(zones[0]).read().strip()) / 1000.0
            if len(zones) > 1:
                temps.board = int(open(zones[1]).read().strip()) / 1000.0
        except Exception:
            pass
        return temps

    def _read_port_states(self) -> List[bool]:
        """Read port oper status from APPL_DB."""
        port_up = [False] * self._num_ports
        if self._db is None:
            return port_up
        try:
            keys = self._db.keys(self._db.APPL_DB, "PORT_TABLE:*") or []
            for i, key in enumerate(sorted(keys)):
                if i >= self._num_ports:
                    break
                data = self._db.get_all(self._db.APPL_DB, key) or {}
                port_up[i] = data.get("oper_status", "down") == "up"
        except Exception:
            pass
        return port_up

    def _read_psu_state(self) -> List[PSUState]:
        """Read PSU data from STATE_DB."""
        psus = []
        if self._db is None:
            return [PSUState(psu_id=i+1, present=True, input_ok=True,
                           output_ok=True, voltage_in=120.0, voltage_out=12.0,
                           current=10.0, power=120.0, temperature=40.0,
                           fan_rpm=5000) for i in range(2)]
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
            psus = [PSUState(psu_id=i+1, present=True, input_ok=True,
                           output_ok=True, voltage_in=120.0, voltage_out=12.0,
                           current=10.0, power=120.0, temperature=40.0,
                           fan_rpm=5000) for i in range(2)]
        self._psus = psus
        return psus

    def _read_fan_state(self) -> List[FanState]:
        """Read fan data from STATE_DB."""
        fans = []
        if self._db is None:
            return [FanState(fan_id=i+1, present=True, ok=True, rpm=5000) for i in range(4)]
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
            fans = [FanState(fan_id=i+1, present=True, ok=True, rpm=5000) for i in range(4)]
        self._fans = fans
        return fans

    def read(self) -> SwitchSnapshot:
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

    # Mutation methods are no-ops on real switches (read-only BLE view)
    def trigger_alarm(self, category, severity, message) -> str:
        return ""

    def acknowledge_alarm(self, alarm_id):
        pass

    def clear_alarms(self):
        pass

    def set_led(self, port, color, blink):
        pass

    def set_port_admin(self, port, up):
        pass

    def set_port_speed(self, port, speed_mbps):
        pass

    def set_port_mtu(self, port, mtu):
        pass

    def set_port_desc(self, port, description):
        pass

    def set_split_mode(self, port, mode) -> bool:
        return False

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
