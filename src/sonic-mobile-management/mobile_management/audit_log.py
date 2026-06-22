"""
audit_log.py — Write BLE control command audit entries to STATE_DB.

Maintains a ring buffer of the last MAX_ENTRIES commands in STATE_DB keys:
    MOBILE_MANAGEMENT_AUDIT|<sequence_number>

Each entry stores: timestamp, username, command, detail, result.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime

log = logging.getLogger(__name__)

MAX_ENTRIES = 100
_KEY_PREFIX = "MOBILE_MANAGEMENT_AUDIT|"


class AuditLog:

    def __init__(self):
        self._db = None
        self._state_db = None
        self._seq = 0
        try:
            from swsscommon.swsscommon import SonicV2Connector
            self._db = SonicV2Connector()
            self._state_db = self._db.STATE_DB
            self._db.connect(self._state_db)
            self._seq = self._recover_sequence()
            log.info(f"AuditLog: connected to STATE_DB (next seq={self._seq})")
        except Exception as exc:
            log.warning(f"AuditLog: STATE_DB connect failed: {exc}")
            self._db = None

    def _recover_sequence(self) -> int:
        """Find the highest existing sequence number to resume from."""
        if self._db is None:
            return 0
        try:
            keys = self._db.keys(self._state_db, f"{_KEY_PREFIX}*")
            if not keys:
                return 0
            seqs = []
            for k in keys:
                try:
                    seqs.append(int(k.split("|")[1]))
                except (IndexError, ValueError):
                    pass
            return max(seqs) + 1 if seqs else 0
        except Exception:
            return 0

    def record(self, username: str, command: str, detail: str, result: str):
        if self._db is None:
            return
        try:
            key = f"{_KEY_PREFIX}{self._seq}"
            self._db.set(self._state_db, key, "timestamp",
                         datetime.now().isoformat(timespec='seconds'))
            self._db.set(self._state_db, key, "username", username or "anonymous")
            self._db.set(self._state_db, key, "command", command)
            self._db.set(self._state_db, key, "detail", detail)
            self._db.set(self._state_db, key, "result", result)

            old_seq = self._seq - MAX_ENTRIES
            if old_seq >= 0:
                old_key = f"{_KEY_PREFIX}{old_seq}"
                self._db.delete(self._state_db, old_key)

            self._seq += 1
        except Exception as exc:
            log.warning(f"AuditLog: write failed: {exc}")

    def cleanup(self):
        """Remove all audit entries from STATE_DB."""
        if self._db is None:
            return
        try:
            keys = self._db.keys(self._state_db, f"{_KEY_PREFIX}*")
            for k in (keys or []):
                self._db.delete(self._state_db, k)
            log.info("AuditLog: cleaned up STATE_DB")
        except Exception as exc:
            log.warning(f"AuditLog: cleanup failed: {exc}")
