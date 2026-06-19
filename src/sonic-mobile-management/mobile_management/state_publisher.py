"""
state_publisher.py — Publish daemon status, sessions, and telemetry to STATE_DB.

Runs as an async task inside the peripheral's event loop.  Every
`interval` seconds it reads the peripheral's in-memory state and
writes it to STATE_DB so that `show mobile-management` subcommands
can display live data without talking to the daemon directly.

STATE_DB tables written:
    MOBILE_MANAGEMENT_DAEMON|status   — daemon health / adapter / client count
    MOBILE_MANAGEMENT_SESSION|<mac>   — one row per active BLE session
    MOBILE_MANAGEMENT_TELEMETRY|snapshot — latest telemetry summary
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mobile_management.peripheral import PeripheralState

log = logging.getLogger("mobile_management.state_publisher")

STATE_DB_ID = 6

_DAEMON_KEY = "MOBILE_MANAGEMENT_DAEMON|status"
_TELEMETRY_KEY = "MOBILE_MANAGEMENT_TELEMETRY|snapshot"
_SESSION_PREFIX = "MOBILE_MANAGEMENT_SESSION|"


class StatePublisher:
    """Periodically publish peripheral state to SONiC STATE_DB."""

    def __init__(self, publish_interval: float = 5.0):
        self._interval = publish_interval
        self._db = None
        self._start_time = time.time()
        self._prev_session_keys: set = set()

        try:
            from swsscommon.swsscommon import SonicV2Connector
            self._db = SonicV2Connector()
            self._db.connect(STATE_DB_ID)
            log.info("StatePublisher: connected to STATE_DB")
        except Exception as exc:
            log.warning(f"StatePublisher: STATE_DB connect failed: {exc}")
            self._db = None

    def _set(self, key: str, field: str, value: str):
        if self._db:
            self._db.set(STATE_DB_ID, key, field, value)

    def _delete(self, key: str):
        if self._db:
            self._db.delete(STATE_DB_ID, key)

    def _publish_daemon_status(self, state: PeripheralState):
        from mobile_management.peripheral import (
            server, _authenticated, _users,
        )
        uptime = int(time.time() - self._start_time)
        has_server = server is not None
        n_connected = state.registry.connected_count

        self._set(_DAEMON_KEY, "state", "running" if has_server else "starting")
        self._set(_DAEMON_KEY, "pid", str(os.getpid()))
        self._set(_DAEMON_KEY, "uptime", str(uptime))
        self._set(_DAEMON_KEY, "advertising", "true" if has_server else "false")
        self._set(_DAEMON_KEY, "auth_mode", "enabled" if _users else "disabled")
        self._set(_DAEMON_KEY, "connected_clients", str(n_connected))

    def _publish_sessions(self, state: PeripheralState):
        from mobile_management.peripheral import _authenticated

        current_keys: set = set()
        for mac in state.registry._order:
            sessions = state.registry._sessions.get(mac, [])
            if not sessions:
                continue
            latest = sessions[-1]
            if not latest.is_connected:
                continue

            key = f"{_SESSION_PREFIX}{mac}"
            current_keys.add(key)

            self._set(key, "name", latest.name or "Unknown")
            self._set(key, "username", latest.username or "")
            self._set(key, "connected_at", latest.connected_at.isoformat(timespec='seconds'))
            self._set(key, "last_keepalive",
                      latest.last_keepalive.isoformat(timespec='seconds')
                      if latest.last_keepalive else "")
            self._set(key, "authenticated", "true" if _authenticated else "false")
            self._set(key, "command_count", str(len(latest.commands)))
            self._set(key, "duration", latest.duration_str)

        stale = self._prev_session_keys - current_keys
        for old_key in stale:
            self._delete(old_key)
        self._prev_session_keys = current_keys

    def _publish_telemetry(self, state: PeripheralState):
        snap = state.snapshot
        if snap is None:
            return

        self._set(_TELEMETRY_KEY, "cpu_percent", f"{snap.cpu_pct:.1f}")
        self._set(_TELEMETRY_KEY, "mem_percent", f"{snap.mem_pct:.1f}")

        temps = snap.temperatures
        if temps and temps.cpu_die is not None:
            self._set(_TELEMETRY_KEY, "cpu_temp", f"{temps.cpu_die:.1f}")

        ports_up = sum(1 for up in snap.port_up if up)
        self._set(_TELEMETRY_KEY, "ports_up", str(ports_up))
        self._set(_TELEMETRY_KEY, "ports_total", str(len(snap.port_up)))

        psu_ok = sum(1 for p in snap.psus if p.present and p.output_ok)
        self._set(_TELEMETRY_KEY, "psu_ok", str(psu_ok))
        self._set(_TELEMETRY_KEY, "psu_total", str(len(snap.psus)))

        fan_ok = sum(1 for f in snap.fans if f.present and f.ok)
        self._set(_TELEMETRY_KEY, "fan_ok", str(fan_ok))
        self._set(_TELEMETRY_KEY, "fan_total", str(len(snap.fans)))

        self._set(_TELEMETRY_KEY, "last_update", datetime.now().isoformat(timespec='seconds'))

    async def run(self, state: PeripheralState):
        """Main publish loop — call as an asyncio task."""
        if self._db is None:
            log.info("StatePublisher: no STATE_DB — publish loop disabled")
            return

        log.info(f"StatePublisher: publishing every {self._interval}s")
        try:
            while True:
                try:
                    self._publish_daemon_status(state)
                    self._publish_sessions(state)
                    self._publish_telemetry(state)
                except Exception as exc:
                    log.warning(f"StatePublisher: publish error: {exc}")
                await asyncio.sleep(self._interval)
        except asyncio.CancelledError:
            pass
        finally:
            self.cleanup()

    def cleanup(self):
        """Remove all STATE_DB entries on shutdown."""
        if self._db is None:
            return
        try:
            self._set(_DAEMON_KEY, "state", "stopped")
            self._set(_DAEMON_KEY, "connected_clients", "0")
            self._set(_DAEMON_KEY, "advertising", "false")
            for key in list(self._prev_session_keys):
                self._delete(key)
            self._prev_session_keys.clear()
            log.info("StatePublisher: cleaned up STATE_DB")
        except Exception as exc:
            log.warning(f"StatePublisher: cleanup error: {exc}")
