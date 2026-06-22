"""
test_audit_log.py — Unit tests for AuditLog with mocked STATE_DB.

Run:  pytest tests/test_audit_log.py -v
"""
import pytest


class TestAuditLogRecord:

    def test_record_writes_all_fields(self, mock_swsscommon):
        fake_sv2, _ = mock_swsscommon

        from mobile_management.audit_log import AuditLog
        al = AuditLog()

        al.record("admin", "Port admin up", "port=1 admin=up", "success")

        entry = fake_sv2._data["STATE_DB"]["MOBILE_MANAGEMENT_AUDIT|0"]
        assert entry["username"] == "admin"
        assert entry["command"] == "Port admin up"
        assert entry["detail"] == "port=1 admin=up"
        assert entry["result"] == "success"
        assert "timestamp" in entry

    def test_sequence_increments(self, mock_swsscommon):
        fake_sv2, _ = mock_swsscommon

        from mobile_management.audit_log import AuditLog
        al = AuditLog()

        al.record("user1", "cmd1", "d1", "success")
        al.record("user2", "cmd2", "d2", "success")
        al.record("user3", "cmd3", "d3", "failed")

        assert "MOBILE_MANAGEMENT_AUDIT|0" in fake_sv2._data["STATE_DB"]
        assert "MOBILE_MANAGEMENT_AUDIT|1" in fake_sv2._data["STATE_DB"]
        assert "MOBILE_MANAGEMENT_AUDIT|2" in fake_sv2._data["STATE_DB"]
        assert fake_sv2._data["STATE_DB"]["MOBILE_MANAGEMENT_AUDIT|2"]["username"] == "user3"

    def test_anonymous_when_no_username(self, mock_swsscommon):
        fake_sv2, _ = mock_swsscommon

        from mobile_management.audit_log import AuditLog
        al = AuditLog()

        al.record("", "cmd", "detail", "success")
        assert fake_sv2._data["STATE_DB"]["MOBILE_MANAGEMENT_AUDIT|0"]["username"] == "anonymous"


class TestRingBuffer:

    def test_evicts_old_entries(self, mock_swsscommon, monkeypatch):
        fake_sv2, _ = mock_swsscommon

        import mobile_management.audit_log as audit_mod
        monkeypatch.setattr(audit_mod, "MAX_ENTRIES", 3)

        from mobile_management.audit_log import AuditLog
        al = AuditLog()

        for i in range(5):
            al.record(f"user{i}", f"cmd{i}", f"d{i}", "success")

        keys = [k for k in fake_sv2._data["STATE_DB"]
                if k.startswith("MOBILE_MANAGEMENT_AUDIT|")]
        assert len(keys) == 3
        assert "MOBILE_MANAGEMENT_AUDIT|0" not in fake_sv2._data["STATE_DB"]
        assert "MOBILE_MANAGEMENT_AUDIT|1" not in fake_sv2._data["STATE_DB"]
        assert "MOBILE_MANAGEMENT_AUDIT|2" in fake_sv2._data["STATE_DB"]
        assert "MOBILE_MANAGEMENT_AUDIT|4" in fake_sv2._data["STATE_DB"]


class TestRecoverSequence:

    def test_resumes_from_existing_entries(self, mock_swsscommon):
        fake_sv2, _ = mock_swsscommon

        fake_sv2._data["STATE_DB"]["MOBILE_MANAGEMENT_AUDIT|5"] = {"command": "old1"}
        fake_sv2._data["STATE_DB"]["MOBILE_MANAGEMENT_AUDIT|7"] = {"command": "old2"}
        fake_sv2._data["STATE_DB"]["MOBILE_MANAGEMENT_AUDIT|10"] = {"command": "old3"}

        from mobile_management.audit_log import AuditLog
        al = AuditLog()

        assert al._seq == 11

        al.record("test", "new_cmd", "detail", "success")
        assert "MOBILE_MANAGEMENT_AUDIT|11" in fake_sv2._data["STATE_DB"]

    def test_starts_at_zero_when_empty(self, mock_swsscommon):
        from mobile_management.audit_log import AuditLog
        al = AuditLog()
        assert al._seq == 0


class TestCleanup:

    def test_cleanup_removes_all_entries(self, mock_swsscommon):
        fake_sv2, _ = mock_swsscommon

        from mobile_management.audit_log import AuditLog
        al = AuditLog()

        for i in range(5):
            al.record(f"user{i}", f"cmd{i}", f"d{i}", "success")

        al.cleanup()

        keys = [k for k in fake_sv2._data["STATE_DB"]
                if k.startswith("MOBILE_MANAGEMENT_AUDIT|")]
        assert len(keys) == 0


class TestGracefulDegradation:

    def test_no_crash_when_db_unavailable(self, monkeypatch):
        """AuditLog should be a no-op when swsscommon is not available."""
        import sys
        monkeypatch.delitem(sys.modules, "swsscommon", raising=False)
        monkeypatch.delitem(sys.modules, "swsscommon.swsscommon", raising=False)

        from mobile_management.audit_log import AuditLog
        al = AuditLog()

        assert al._db is None
        al.record("user", "cmd", "detail", "success")  # should not crash
        al.cleanup()  # should not crash
