"""
sensor_simulator.py — Fake Switch Sensor Data Generator
========================================================
Simulates telemetry a real network switch would expose:
  - CPU utilisation (%)
  - Memory utilisation (%)
  - Temperature sensors: CPU die, board, air inlet, air outlet
  - Port link status, admin state, speed, MTU, description, FEC
  - Port LED state (color + blink pattern) — per port + system LED
  - PSU status (two supplies: present, voltages, current, power, temp, fan)
  - Live alarms (severity, category, message, timestamp)
  - Per-port counters (tx/rx bytes/packets/errors)

Public mutation API (called by the peripheral to honour client commands):
  sim.trigger_alarm(category, severity, message) → alarm_id
  sim.acknowledge_alarm(alarm_id)
  sim.clear_alarms()
  sim.set_led(port, color, blink)       port 0 = system LED
  sim.set_port_admin(port, up: bool)    port is 1-based
  sim.set_port_speed(port, speed_mbps)
  sim.set_port_mtu(port, mtu)
  sim.set_port_desc(port, description)

Standalone usage:
    python sensor_simulator.py
    python sensor_simulator.py --ports 24 --interval 0.5
"""

import argparse
import asyncio
import math
import random
import struct
import time
import uuid as _uuid
from dataclasses import dataclass, field
from typing import Dict, List, Optional

# ── Constants ─────────────────────────────────────────────────────────────────

LED_COLORS = ["off", "green", "red", "blue", "amber", "white"]
LED_BLINKS = ["solid", "slow", "fast", "pattern"]
COLOR_CODE = {c: i for i, c in enumerate(LED_COLORS)}
BLINK_CODE = {b: i for i, b in enumerate(LED_BLINKS)}

SPEED_OPTIONS = [1000, 10000, 25000, 40000, 50000, 100000, 200000, 400000, 800000]  # Mbps
SPEED_CODE    = {s: i for i, s in enumerate(SPEED_OPTIONS)}

ALARM_SEVERITIES = ["warning", "minor", "major", "critical"]
ALARM_CATEGORIES = ["port", "psu", "fan", "thermal", "system"]


# ── Data model ────────────────────────────────────────────────────────────────

@dataclass
class PortStats:
    """Cumulative interface counters for a single port."""
    tx_bytes:   int = 0
    rx_bytes:   int = 0
    tx_packets: int = 0
    rx_packets: int = 0
    tx_errors:  int = 0
    rx_errors:  int = 0


@dataclass
class TemperatureSensors:
    cpu_die:  float   # °C — hottest sensor, varies most
    board:    float   # °C — PCB near ASIC
    inlet:    float   # °C — air coming in
    outlet:   float   # °C — air going out

    def as_dict(self):
        return {
            "cpu_die": round(self.cpu_die, 1),
            "board":   round(self.board,   1),
            "inlet":   round(self.inlet,   1),
            "outlet":  round(self.outlet,  1),
        }


@dataclass
class FanState:
    """State of one system fan."""
    fan_id:  int
    present: bool  = True
    ok:      bool  = True
    rpm:     int   = 5000


@dataclass
class PSUState:
    """State of one power supply unit."""
    psu_id:      int
    present:     bool
    input_ok:    bool    # AC input healthy
    output_ok:   bool    # DC output healthy
    voltage_in:  float   # Volts AC  (e.g. 120.0 or 240.0)
    voltage_out: float   # Volts DC  (e.g. 12.0)
    current:     float   # Amps
    power:       float   # Watts
    temperature: float   # °C
    fan_rpm:     int


@dataclass
class LEDState:
    """LED color and blink pattern for one port (or system LED at port 0)."""
    color:          str  = "green"   # off green amber red blue white
    blink:          str  = "solid"   # off solid slow fast
    client_control: bool = False     # True = set by a client command; tick won't override


# Odd ports are 400G (4x100G / 2x200G); even ports are 800G (4x200G / 2x400G).
# Wire encoding is the same index (0=none, 1=first option, 2=second option)
# regardless of port parity — the app selects the correct label set.
SPLIT_MODES   = ["none", "4x100G", "2x200G", "4x200G", "2x400G"]
SPLIT_SPEEDS  = {"4x100G": 100000, "2x200G": 200000, "4x200G": 200000, "2x400G": 400000}
SPLIT_MEMBERS = {"4x100G": 4, "2x200G": 2, "4x200G": 4, "2x400G": 2}

# Sub-port numbering: sub-ports live above the max physical port count (64).
# Port p splits into sub-ports starting at SUB_PORT_BASE + (split_index * 4).
# split_index is the 0-based index of p within the split-capable port list.
# Example (64-port switch, capable ports 1,5,9,13,...):
#   port 1  (idx 0) → 65,66,67,68
#   port 5  (idx 1) → 69,70,71,72
#   port 9  (idx 2) → 73,74,75,76
# All sub-port numbers are > 64 and < 129 (safe for uint8 with ≤16 split ports).
SUB_PORT_BASE = 64

def _sub_port_start(parent: int, capable_ports: list) -> int:
    """Return the first sub-port number for this parent port."""
    idx = capable_ports.index(parent) if parent in capable_ports else 0
    return SUB_PORT_BASE + idx * 4 + 1

# Ports that support breakout (1-based).
# p%8==1 → odd ports (400G): 1, 9, 17, 25, 33, 41
# p%8==0 → even ports (800G): 8, 16, 24, 32, 40, 48
# Gives equal mix of 400G and 800G breakout-capable ports (12 total for 48-port).
def _default_split_capable(num_ports: int) -> set:
    return {p for p in range(1, num_ports + 1) if p % 8 == 1 or p % 8 == 0}


@dataclass
class PortConfig:
    """Administrative configuration for one port."""
    admin_up:     bool  = True
    speed:        int   = 10000   # Mbps
    mtu:          int   = 9100
    description:  str   = ""
    fec:          str   = "none"  # none rs fc
    split_capable: bool = False
    split_mode:   str   = "none"  # none 4x10G 4x25G 2x50G
    parent_port:  int   = 0       # 0 = physical port; >0 = sub-port of parent
    link_up:      bool  = False   # current link state (sub-ports only; physical ports use port_up list)


@dataclass
class Alarm:
    """One active alarm entry."""
    alarm_id:     str
    severity:     str    # warning minor major critical
    category:     str    # port psu fan thermal system
    message:      str
    timestamp:    float
    acknowledged: bool = False


@dataclass
class SwitchSnapshot:
    """One complete reading from all sensors."""
    timestamp:    float
    cpu_pct:      float
    mem_pct:      float
    temperatures: TemperatureSensors
    port_up:      List[bool]
    port_stats:   Dict[int, PortStats]        = field(default_factory=dict)  # port number → stats
    psus:         List[PSUState]             = field(default_factory=list)
    fans:         List[FanState]             = field(default_factory=list)
    leds:         Dict[int, LEDState]        = field(default_factory=dict)   # port→LED (0=system)
    port_configs: Dict[int, PortConfig]      = field(default_factory=dict)   # port 1-based
    alarms:       List[Alarm]               = field(default_factory=list)

    @property
    def ports_up_count(self):
        return sum(self.port_up)

    @property
    def num_ports(self):
        return len(self.port_up)


# ── Simulator ─────────────────────────────────────────────────────────────────

class SwitchSensorSimulator:
    """
    Generates realistic-looking switch sensor readings.

    CPU:    Sine-wave baseline with random spikes.
    Memory: Slow drift with small noise.
    Temps:  Correlated with CPU load + ambient noise.
    Ports:  Mostly stable; each port has a small per-tick flap chance.
    PSUs:   Two supplies; values drift slightly each tick.
    LEDs:   Reflect link/admin state unless overridden by the client.
    Alarms: Accumulated via trigger_alarm(); cleared via clear_alarms().
    """

    NUM_PSUS = 2
    NUM_FANS = 4

    def __init__(self, num_ports: int = 48, flap_rate: float = 0.005):
        if num_ports > 64:
            raise ValueError("num_ports must be ≤ 64")

        self.num_ports = num_ports
        self.flap_rate = flap_rate
        self._tick = 0

        # ── CPU / memory ──────────────────────────────────────────────────────
        self._cpu_base    = random.uniform(15, 30)
        self._mem_base    = random.uniform(35, 55)
        self._spike_prob  = 0.04
        self._spike_val   = 0.0
        self._spike_decay = 0.0

        # ── Ports ─────────────────────────────────────────────────────────────
        self._port_up: List[bool] = [random.random() < 0.8 for _ in range(num_ports)]
        # Keyed by port number (physical ports initially; sub-ports added on split)
        self._port_stats: Dict[int, PortStats] = {p + 1: PortStats() for p in range(num_ports)}

        # ── Port configs (1-based keys) ───────────────────────────────────────
        _mtu_pool    = [1500, 4096, 9100, 9216]
        _desc_pool   = [
            "uplink-core", "server-rack-a", "server-rack-b",
            "storage-fabric", "mgmt", "backup-link",
            "spine-1", "spine-2", "leaf-peer", "",
        ]
        _fec_pool    = ["none", "rs", "fc"]

        self._split_capable: set = _default_split_capable(num_ports)
        self._port_configs: Dict[int, PortConfig] = {}
        for p in range(num_ports):
            port      = p + 1
            admin_up  = random.random() > 0.10
            # Odd ports are 400G, even ports are 800G — matches app display labels
            speed     = 400000 if port % 2 == 1 else 800000
            self._port_configs[port] = PortConfig(
                admin_up      = admin_up,
                speed         = speed,
                mtu           = random.choice(_mtu_pool),
                description   = random.choice(_desc_pool),
                fec           = random.choice(_fec_pool),
                split_capable = port in self._split_capable,
                split_mode    = "none",
                parent_port   = 0,
            )

        # ── LEDs: port 0 = system LED, 1..N = per-port ───────────────────────
        # Color weights: mostly green/off matching link state,
        # but a healthy sprinkling of amber, blue, red, white for variety.
        _all_colors  = ["off", "green", "red", "blue", "amber", "white"]
        _all_blinks  = ["solid", "slow", "fast", "pattern"]

        self._leds: Dict[int, LEDState] = {0: LEDState(color="green", blink="solid")}
        for p in range(1, num_ports + 1):
            up = self._port_up[p - 1]
            cfg_p = self._port_configs.get(p)
            init_color = "green" if up else "off"
            if cfg_p and not cfg_p.admin_up:
                init_color = "amber"
            self._leds[p] = LEDState(
                color          = init_color,
                blink          = "solid",
                client_control = False,  # allow auto-sync with link/admin state
            )

        # ── PSUs ──────────────────────────────────────────────────────────────
        self._psus: List[PSUState] = [
            PSUState(
                psu_id      = i + 1,
                present     = True,
                input_ok    = True,
                output_ok   = True,
                voltage_in  = random.uniform(118.0, 122.0),
                voltage_out = random.uniform(11.9,  12.1),
                current     = random.uniform(8.0,   12.0),
                power       = 0.0,
                temperature = random.uniform(35.0,  45.0),
                fan_rpm     = random.randint(4000,  6000),
            )
            for i in range(self.NUM_PSUS)
        ]
        for p in self._psus:
            p.power = round(p.voltage_out * p.current, 1)

        # ── Fans ──────────────────────────────────────────────────────────────
        self._fans: List[FanState] = [
            FanState(
                fan_id  = i + 1,
                present = True,
                ok      = True,
                rpm     = random.randint(3000, 7000),
            )
            for i in range(self.NUM_FANS)
        ]

        # ── Alarms ────────────────────────────────────────────────────────────
        self._alarms: List[Alarm] = []

    # ── Public mutation API ───────────────────────────────────────────────────

    def trigger_alarm(self, category: str, severity: str, message: str) -> str:
        """Add a new alarm; returns its alarm_id."""
        alarm_id = str(_uuid.uuid4())[:8]
        self._alarms.append(Alarm(
            alarm_id     = alarm_id,
            severity     = severity,
            category     = category,
            message      = message,
            timestamp    = time.time(),
            acknowledged = False,
        ))
        # Keep only the 50 most recent alarms
        if len(self._alarms) > 50:
            self._alarms = self._alarms[-50:]
        return alarm_id

    def acknowledge_alarm(self, alarm_id: str):
        for a in self._alarms:
            if a.alarm_id == alarm_id:
                a.acknowledged = True
                break

    def clear_alarms(self):
        self._alarms.clear()

    def set_led(self, port: int, color: str, blink: str):
        """Set LED for port (0 = system LED). Values must be in LED_COLORS/BLINKS."""
        if color not in LED_COLORS:
            color = "off"
        if blink not in LED_BLINKS:
            blink = "solid"
        self._leds[port] = LEDState(color=color, blink=blink, client_control=True)

    def set_port_admin(self, port: int, up: bool):
        """Set admin state for port (1-based, or sub-port). Updates LED to reflect state."""
        if port not in self._port_configs:
            return
        cfg = self._port_configs[port]
        # Block admin-up on a split parent: sub-ports carry the traffic
        if up and cfg.split_mode != "none":
            return
        self._port_configs[port].admin_up = up
        if not up:
            # Admin-down forces LED amber and link down
            self._leds[port] = LEDState(color="amber", blink="solid",
                                        client_control=True)
            if 1 <= port <= self.num_ports:
                self._port_up[port - 1] = False
            else:
                self._port_configs[port].link_up = False
        else:
            # Admin-up
            if 1 <= port <= self.num_ports:
                color = "green" if self._port_up[port - 1] else "off"
            else:
                # Sub-port: restore previous link state
                color = "green" if self._port_configs[port].link_up else "off"
            self._leds[port] = LEDState(color=color, blink="solid",
                                        client_control=True)

    def demo_reset(self):
        """Unsplit all ports, restore admin-up, clear alarms, zero counters, clear descriptions."""
        # Unsplit all split ports first
        for port in list(self._port_configs.keys()):
            cfg = self._port_configs.get(port)
            if cfg and cfg.split_capable and cfg.split_mode != "none":
                self.set_split_mode(port, "none")
        # Restore all physical ports to admin-up and clear descriptions
        for port in range(1, self.num_ports + 1):
            cfg = self._port_configs.get(port)
            if cfg:
                cfg.admin_up   = True
                cfg.description = ""
        # Reset all LEDs to reflect current link state (clear client control)
        for port, cfg in self._port_configs.items():
            if 1 <= port <= self.num_ports:
                up = self._port_up[port - 1] if port - 1 < len(self._port_up) else False
                self._leds[port] = LEDState(
                    color         = "green" if up else "off",
                    blink         = "solid",
                    client_control= False,
                )
        # Zero all counters
        for port in self._port_stats:
            s = self._port_stats[port]
            s.tx_bytes = s.rx_bytes = s.tx_packets = s.rx_packets = 0
        # Clear all alarms
        self.clear_alarms()

    def set_port_speed(self, port: int, speed_mbps: int):
        if port in self._port_configs and speed_mbps in SPEED_OPTIONS:
            self._port_configs[port].speed = speed_mbps

    def set_port_mtu(self, port: int, mtu: int):
        if port in self._port_configs and 576 <= mtu <= 65535:
            self._port_configs[port].mtu = mtu

    def set_port_desc(self, port: int, description: str):
        if port in self._port_configs:
            self._port_configs[port].description = description[:255]

    def set_split_mode(self, port: int, mode: str) -> bool:
        """
        Split or unsplit a breakout-capable physical port.
        Sub-ports are numbered port*100+member (e.g. port 1 → 101, 102, 103, 104).
        Displayed externally as "1/1", "1/2", etc.
        Returns True on success, False if port is not split-capable.
        """
        cfg = self._port_configs.get(port)
        if cfg is None or not cfg.split_capable:
            return False
        if mode not in SPLIT_MODES:
            return False

        # Sub-port numbering: above SUB_PORT_BASE, grouped by split index
        capable_list = sorted(self._split_capable)
        sub_start = _sub_port_start(port, capable_list)
        for m in range(4):
            self._port_configs.pop(sub_start + m, None)
            self._leds.pop(sub_start + m, None)
            self._port_stats.pop(sub_start + m, None)

        cfg.split_mode = mode

        if mode == "none":
            # Restore the physical port — re-enable it
            cfg.admin_up = True
        else:
            # Disable the physical port (it no longer carries traffic directly)
            cfg.admin_up = False
            n_members = SPLIT_MEMBERS.get(mode, 4)
            sub_speed  = SPLIT_SPEEDS.get(mode, 10000)
            for m in range(1, n_members + 1):
                sub_port = sub_start + (m - 1)
                self._port_configs[sub_port] = PortConfig(
                    admin_up      = True,
                    speed         = sub_speed,
                    mtu           = cfg.mtu,
                    description   = f"{port}/{m}",
                    fec           = cfg.fec,
                    split_capable = False,
                    split_mode    = "none",
                    parent_port   = port,
                    link_up       = random.random() > 0.3,
                )
                sub_cfg = self._port_configs[sub_port]
                self._leds[sub_port] = LEDState(
                    color          = "green" if sub_cfg.link_up else "off",
                    blink          = "solid",
                    client_control = False,
                )
                self._port_stats[sub_port] = PortStats()
        return True

    @property
    def active_ports(self) -> List[int]:
        """Sorted list of all currently active port numbers (physical + sub-ports)."""
        return sorted(self._port_configs.keys())

    # ── Public read API ───────────────────────────────────────────────────────

    def read(self) -> SwitchSnapshot:
        """Advance simulation by one tick and return a full sensor snapshot."""
        self._tick += 1
        cpu   = self._next_cpu()
        mem   = self._next_mem()
        temps = self._next_temps(cpu)
        ports = self._next_ports()
        stats = self._next_port_stats(ports, cpu)
        psus  = self._next_psus(cpu)
        fans  = self._next_fans()
        leds  = self._current_leds(ports)

        return SwitchSnapshot(
            timestamp    = time.time(),
            cpu_pct      = cpu,
            mem_pct      = mem,
            temperatures = temps,
            port_up      = ports,
            port_stats   = stats,
            psus         = psus,
            fans         = fans,
            leds         = dict(leds),
            port_configs = {k: PortConfig(
                               admin_up     = v.admin_up,
                               speed        = v.speed,
                               mtu          = v.mtu,
                               description  = v.description,
                               fec          = v.fec,
                               split_capable= v.split_capable,
                               split_mode   = v.split_mode,
                               parent_port  = v.parent_port,
                               link_up      = v.link_up,
                           ) for k, v in self._port_configs.items()},
            alarms       = list(self._alarms),
        )

    async def stream(self, interval: float = 2.0):
        """Async generator — yields a SwitchSnapshot every `interval` seconds."""
        while True:
            yield self.read()
            await asyncio.sleep(interval)

    # ── Encoding helpers ──────────────────────────────────────────────────────

    @staticmethod
    def pack_cpu(snap: 'SwitchSnapshot') -> bytearray:
        return bytearray(struct.pack('B', min(100, int(snap.cpu_pct))))

    @staticmethod
    def pack_memory(snap: 'SwitchSnapshot') -> bytearray:
        return bytearray(struct.pack('B', min(100, int(snap.mem_pct))))

    @staticmethod
    def pack_temperatures(snap: 'SwitchSnapshot') -> bytearray:
        t = snap.temperatures
        return bytearray(struct.pack('>hhhh',
            int(t.cpu_die * 10), int(t.board  * 10),
            int(t.inlet   * 10), int(t.outlet * 10),
        ))

    @staticmethod
    def pack_ports(snap: 'SwitchSnapshot') -> bytearray:
        bitmask = 0
        for i, up in enumerate(snap.port_up):
            if up:
                bitmask |= (1 << i)
        num_bytes = math.ceil(snap.num_ports / 8)
        return bytearray(bitmask.to_bytes(num_bytes, 'little'))

    @staticmethod
    def unpack_ports(data: bytes, num_ports: int) -> List[bool]:
        bitmask = int.from_bytes(data, 'little')
        return [(bitmask >> i) & 1 == 1 for i in range(num_ports)]

    @staticmethod
    def pack_port_stats(snap: 'SwitchSnapshot') -> bytearray:
        """
        Pack all port stats into a flat binary blob for chunked BLE delivery.
        Includes both physical ports and any active sub-ports (numbered 65+).
        Each 21-byte entry:
          flags   : uint8  — bits[6:0]=port number (1-based, max 127), bit[7]=link up
          tx_bytes: uint32 BE
          rx_bytes: uint32 BE
          tx_pkts : uint32 BE
          rx_pkts : uint32 BE
          tx_errs : uint16 BE
          rx_errs : uint16 BE
        """
        buf = bytearray()
        for port, s in sorted(snap.port_stats.items()):
            cfg = snap.port_configs.get(port)
            if cfg is None:
                continue
            # Determine link state: physical ports use port_up list, sub-ports use cfg.link_up
            if cfg.parent_port == 0:
                idx     = port - 1
                link_up = snap.port_up[idx] if idx < len(snap.port_up) else False
            else:
                link_up = cfg.link_up
            flags = (port & 0x7F) | (0x80 if link_up else 0x00)
            buf += struct.pack('>BIIIIHH',
                               flags,
                               s.tx_bytes,   s.rx_bytes,
                               s.tx_packets, s.rx_packets,
                               s.tx_errors,  s.rx_errors)
        return buf

    @staticmethod
    def pack_psus(snap: 'SwitchSnapshot') -> bytearray:
        """
        Pack PSU states into binary.
        Header: num_psus (1 byte)
        Per PSU (16 bytes):
          psu_id      : uint8
          flags       : uint8  bit0=present bit1=input_ok bit2=output_ok
          voltage_in  : uint16 BE  tenths of volt  (e.g. 1200 = 120.0 V)
          voltage_out : uint16 BE  hundredths of volt (e.g. 1200 = 12.00 V)
          current     : uint16 BE  hundredths of amp
          power       : uint16 BE  whole watts
          temperature : uint16 BE  tenths of °C
          fan_rpm     : uint16 BE
        """
        buf = bytearray([len(snap.psus)])
        for p in snap.psus:
            flags = (
                (0x01 if p.present    else 0) |
                (0x02 if p.input_ok   else 0) |
                (0x04 if p.output_ok  else 0)
            )
            buf += struct.pack('>BBHHHHHh',
                               p.psu_id, flags,
                               int(p.voltage_in  * 10),
                               int(p.voltage_out * 100),
                               int(p.current     * 100),
                               int(p.power),
                               int(p.temperature * 10),
                               p.fan_rpm)
        return buf

    @staticmethod
    def pack_leds(snap: 'SwitchSnapshot') -> bytearray:
        """
        Pack LED states.
        Header: num_entries (1 byte)
        Per entry (3 bytes):
          port  : uint8  (0 = system LED)
          color : uint8  (index into LED_COLORS)
          blink : uint8  (index into LED_BLINKS)
        """
        entries = sorted(snap.leds.items())
        buf = bytearray([len(entries)])
        for port, led in entries:
            buf += struct.pack('BBB',
                               port,
                               COLOR_CODE.get(led.color, 0),
                               BLINK_CODE.get(led.blink, 0))
        return buf

    @staticmethod
    def pack_port_config(port: int, cfg: 'PortConfig') -> bytearray:
        """
        Pack one port's config (variable length due to description string).
        Layout:
          port          : uint8
          flags         : uint8  bit0=admin_up  bit1=split_capable
          speed_code    : uint8  (index into SPEED_OPTIONS)
          mtu           : uint16 BE
          fec_code      : uint8  (0=none 1=rs 2=fc)
          desc_len      : uint8
          desc          : utf-8 bytes (up to 255)
          split_mode    : uint8  (index into SPLIT_MODES)
          parent_port   : uint8  (0 = physical port)
        """
        fec_codes  = {"none": 0, "rs": 1, "fc": 2}
        flags      = (0x01 if cfg.admin_up     else 0x00) | \
                     (0x02 if cfg.split_capable else 0x00) | \
                     (0x04 if cfg.link_up       else 0x00)
        speed_code = SPEED_CODE.get(cfg.speed, 1)
        fec_code   = fec_codes.get(cfg.fec, 0)
        desc_bytes = cfg.description.encode('utf-8')[:255]
        # Wire encoding: 0=none, 1=first-breakout, 2=second-breakout regardless of speed tier.
        # Odd-port first options: 4x100G; even-port first options: 4x200G (both → index 1).
        _split_first  = {"4x100G", "4x200G"}
        _split_second = {"2x200G", "2x400G"}
        split_code = (1 if cfg.split_mode in _split_first
                      else 2 if cfg.split_mode in _split_second
                      else 0)
        # parent_port clamped to uint8 (sub-ports have parent ≤ 64)
        parent_u8  = cfg.parent_port & 0xFF
        buf  = struct.pack('>BBBHBB',
                           port, flags, speed_code, cfg.mtu,
                           fec_code, len(desc_bytes))
        buf += desc_bytes
        buf += struct.pack('>BB', split_code, parent_u8)
        return bytearray(buf)

    @staticmethod
    def pack_alarms(snap: 'SwitchSnapshot') -> bytearray:
        """
        Pack alarms as a simple binary structure.
        Header: num_alarms (1 byte)
        Per alarm (variable):
          severity    : uint8  (0=warning 1=minor 2=major 3=critical)
          category    : uint8  (0=port 1=psu 2=fan 3=thermal 4=system)
          flags       : uint8  bit0=acknowledged
          timestamp   : uint32 BE  unix epoch seconds
          id_len      : uint8
          alarm_id    : utf-8 bytes (id_len)
          msg_len     : uint8
          message     : utf-8 bytes (msg_len, up to 128)
        """
        sev_codes = {s: i for i, s in enumerate(ALARM_SEVERITIES)}
        cat_codes = {c: i for i, c in enumerate(ALARM_CATEGORIES)}
        buf = bytearray([len(snap.alarms)])
        for a in snap.alarms:
            sev = sev_codes.get(a.severity, 0)
            cat = cat_codes.get(a.category, 4)
            flg = 0x01 if a.acknowledged else 0x00
            ts  = int(a.timestamp)
            id_b  = a.alarm_id.encode('utf-8')[:32]
            msg_b = a.message.encode('utf-8')[:128]
            buf += struct.pack('>BBBI', sev, cat, flg, ts)
            buf += struct.pack('>B', len(id_b))  + id_b
            buf += struct.pack('>B', len(msg_b)) + msg_b
        return buf

    # ── Internal simulation ───────────────────────────────────────────────────

    def _next_cpu(self) -> float:
        wave = 12 * math.sin(self._tick * 2 * math.pi / 120)
        if random.random() < self._spike_prob and self._spike_val < 5:
            self._spike_val   = random.uniform(20, 50)
            self._spike_decay = random.uniform(0.7, 0.9)
        self._spike_val *= self._spike_decay
        noise = random.gauss(0, 3)
        return max(1.0, min(99.0, self._cpu_base + wave + self._spike_val + noise))

    def _next_mem(self) -> float:
        drift = random.gauss(0, 0.3)
        if random.random() < 0.01:
            drift += random.uniform(2, 8) * random.choice([-1, 1])
        self._mem_base = max(20.0, min(90.0, self._mem_base + drift))
        return max(10.0, min(95.0, self._mem_base + random.gauss(0, 0.5)))

    def _next_temps(self, cpu_pct: float) -> TemperatureSensors:
        cpu_heat = cpu_pct * 0.3
        return TemperatureSensors(
            cpu_die = 38 + cpu_heat + random.gauss(0, 0.8),
            board   = 35 + cpu_heat * 0.6 + random.gauss(0, 0.4),
            inlet   = 26 + random.gauss(0, 0.3),
            outlet  = 26 + 12 + cpu_heat * 0.4 + random.gauss(0, 0.4),
        )

    def _next_ports(self) -> List[bool]:
        for i in range(self.num_ports):
            cfg = self._port_configs.get(i + 1)
            if cfg and not cfg.admin_up:
                self._port_up[i] = False   # admin-down = always down
                continue
            if random.random() < self.flap_rate:
                self._port_up[i] = not self._port_up[i]

        # Tick sub-port link_up with the same flap rate
        for port, cfg in self._port_configs.items():
            if cfg.parent_port == 0 or not cfg.admin_up:
                continue
            if random.random() < self.flap_rate:
                cfg.link_up = not cfg.link_up
                # Sync auto-managed sub-port LED to match new link state
                led = self._leds.get(port)
                if led and not led.client_control:
                    self._leds[port] = LEDState(
                        color="green" if cfg.link_up else "off", blink="solid"
                    )

        return list(self._port_up)

    def _current_leds(self, ports: List[bool]) -> Dict[int, LEDState]:
        """Auto-update LEDs for ports whose state changed (unless client-controlled)."""
        for i, up in enumerate(ports):
            port = i + 1
            cfg  = self._port_configs.get(port)
            led  = self._leds.get(port, LEDState())
            # Never override LEDs that were explicitly set by a client command
            if led.client_control:
                continue
            # Auto-manage: amber=admin-down, green=up, off=down
            if cfg and not cfg.admin_up:
                self._leds[port] = LEDState(color="amber", blink="solid")
            else:
                self._leds[port] = LEDState(color="green" if up else "off", blink="solid")
        return dict(self._leds)

    def _next_port_stats(self, port_up: List[bool], cpu_pct: float) -> Dict[int, PortStats]:
        load_scale = 0.5 + (cpu_pct / 100.0)
        for port, s in self._port_stats.items():
            cfg = self._port_configs.get(port)
            if cfg is None:
                continue
            # Physical ports use the port_up bitmask; sub-ports use cfg.link_up
            if cfg.parent_port == 0:
                up = port_up[port - 1] if port - 1 < len(port_up) else False
            else:
                up = cfg.link_up
            if not up:
                s.tx_bytes = s.rx_bytes = s.tx_packets = s.rx_packets = 0
                continue
            tx_pkts = int(random.randint(500, 15_000) * load_scale)
            rx_pkts = int(random.randint(500, 15_000) * load_scale)
            tx_size = random.randint(64, 1_500)
            rx_size = random.randint(64, 1_500)
            s.tx_bytes   = (s.tx_bytes   + tx_pkts * tx_size) & 0xFFFFFFFF
            s.rx_bytes   = (s.rx_bytes   + rx_pkts * rx_size) & 0xFFFFFFFF
            s.tx_packets = (s.tx_packets + tx_pkts)           & 0xFFFFFFFF
            s.rx_packets = (s.rx_packets + rx_pkts)           & 0xFFFFFFFF
            if random.random() < 0.005:
                s.tx_errors = (s.tx_errors + random.randint(1, 3)) & 0xFFFF
            if random.random() < 0.005:
                s.rx_errors = (s.rx_errors + random.randint(1, 3)) & 0xFFFF
        return {port: PortStats(s.tx_bytes, s.rx_bytes, s.tx_packets, s.rx_packets,
                                s.tx_errors, s.rx_errors)
                for port, s in self._port_stats.items()}

    def _next_psus(self, cpu_pct: float) -> List[PSUState]:
        load_factor = 0.5 + cpu_pct / 200.0
        for p in self._psus:
            if not p.present:
                continue
            # Slight drift in all values
            p.voltage_in  = max(105.0, min(135.0, p.voltage_in  + random.gauss(0, 0.05)))
            p.voltage_out = max(11.5,  min(12.5,  p.voltage_out + random.gauss(0, 0.02)))
            p.current     = max(1.0,   min(25.0,  p.current * load_factor + random.gauss(0, 0.2)))
            p.power       = round(p.voltage_out * p.current, 1)
            p.temperature = max(25.0,  min(80.0,  p.temperature + random.gauss(0, 0.3)))
            p.fan_rpm     = max(1000,  min(8000,  p.fan_rpm + random.randint(-50, 50)))
        return [
            PSUState(
                psu_id=p.psu_id, present=p.present,
                input_ok=p.input_ok, output_ok=p.output_ok,
                voltage_in=round(p.voltage_in, 1),
                voltage_out=round(p.voltage_out, 2),
                current=round(p.current, 2),
                power=p.power,
                temperature=round(p.temperature, 1),
                fan_rpm=p.fan_rpm,
            )
            for p in self._psus
        ]

    def set_fan_failure(self, fan_id: int):
        """Toggle fan failure state. Triggers/clears an alarm automatically."""
        for f in self._fans:
            if f.fan_id == fan_id:
                f.ok = not f.ok
                if not f.ok:
                    f.rpm = 0
                else:
                    f.rpm = random.randint(3000, 7000)
                return

    def _next_fans(self) -> List[FanState]:
        for f in self._fans:
            if not f.present or not f.ok:
                continue
            f.rpm = max(1000, min(8000, f.rpm + random.randint(-100, 100)))
        return [
            FanState(fan_id=f.fan_id, present=f.present, ok=f.ok, rpm=f.rpm)
            for f in self._fans
        ]

    @staticmethod
    def pack_fans(snap: 'SwitchSnapshot') -> bytearray:
        """
        Pack fan states into binary.
        Header: num_fans (1 byte)
        Per fan (4 bytes):
          fan_id : uint8
          flags  : uint8  bit0=present  bit1=ok
          rpm    : uint16 BE
        """
        buf = bytearray([len(snap.fans)])
        for f in snap.fans:
            flags = (0x01 if f.present else 0) | (0x02 if f.ok else 0)
            buf += struct.pack('>BBH', f.fan_id, flags, f.rpm)
        return buf


# ── Standalone pretty-printer ─────────────────────────────────────────────────

def _render_ports(port_up: List[bool], port_configs: Dict[int, PortConfig],
                  leds: Dict[int, LEDState], cols: int = 12) -> str:
    lines = []
    for i in range(0, len(port_up), cols):
        row = []
        for j, up in enumerate(port_up[i:i+cols]):
            p   = i + j + 1
            cfg = port_configs.get(p, PortConfig())
            led = leds.get(p, LEDState())
            if not cfg.admin_up:
                color = "\033[33m"   # amber = admin down
                sym   = "A"
            elif up:
                color = "\033[32m"   # green
                sym   = "▲"
            else:
                color = "\033[31m"   # red
                sym   = "▼"
            row.append(f"{color}P{p:02d}{sym}\033[0m")
        lines.append("  " + "  ".join(row))
    return "\n".join(lines)


async def _live_display(sim: SwitchSensorSimulator, interval: float):
    import os
    async for snap in sim.stream(interval):
        os.system("clear")
        t = snap.temperatures
        print("═" * 65)
        print("  SWITCH SENSOR SIMULATOR  —  live readings")
        print("═" * 65)
        print(f"  CPU Usage   : {snap.cpu_pct:5.1f}%  {'█' * int(snap.cpu_pct / 5):<20}")
        print(f"  Memory      : {snap.mem_pct:5.1f}%  {'█' * int(snap.mem_pct / 5):<20}")
        print(f"  Temp CPU die: {t.cpu_die:5.1f}°C   Board: {t.board:.1f}°C   "
              f"Inlet: {t.inlet:.1f}°C   Outlet: {t.outlet:.1f}°C")
        print()
        print("  PSUs:")
        for p in snap.psus:
            ok  = "✓" if (p.input_ok and p.output_ok) else "✗"
            print(f"    PSU{p.psu_id} {ok}  Vin={p.voltage_in:.1f}V  "
                  f"Vout={p.voltage_out:.2f}V  {p.current:.1f}A  "
                  f"{p.power:.0f}W  {p.temperature:.1f}°C  {p.fan_rpm}rpm")
        print()
        print(f"  Ports ({snap.ports_up_count}/{snap.num_ports} up):")
        print(_render_ports(snap.port_up, snap.port_configs, snap.leds))
        print()
        if snap.alarms:
            print(f"  Alarms ({len(snap.alarms)}):")
            for a in snap.alarms[-5:]:
                ack = " [ack]" if a.acknowledged else ""
                print(f"    [{a.severity.upper():8s}] {a.category}: {a.message}{ack}")
        else:
            print("  Alarms: none")
        print("═" * 65)
        print("  Ctrl+C to stop")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Switch sensor simulator")
    parser.add_argument("--ports",    type=int,   default=48,  help="Number of ports (max 64)")
    parser.add_argument("--interval", type=float, default=2.0, help="Refresh interval in seconds")
    args = parser.parse_args()

    sim = SwitchSensorSimulator(num_ports=args.ports)
    print(f"Starting simulator with {args.ports} ports, {args.interval}s interval")
    print("Press Ctrl+C to stop\n")

    try:
        asyncio.run(_live_display(sim, args.interval))
    except KeyboardInterrupt:
        print("\nStopped.")
