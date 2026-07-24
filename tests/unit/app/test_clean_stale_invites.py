"""Unit tests for the clean_stale_invites no-op stub task."""

import pytest
from unittest.mock import patch

pytestmark = pytest.mark.unit

_RA = "cqc_lem.app.run_automation"


class TestCleanStaleInvites:
    def test_returns_not_implemented_marker(self):
        from cqc_lem.app.run_automation import clean_stale_invites
        with patch(f"{_RA}.log_info") as mock_log:
            result = clean_stale_invites.run(user_id=42)
        assert result == {"status": "not_implemented", "cleaned": 0, "user_id": 42}

    def test_logs_explicit_stub_message(self):
        from cqc_lem.app.run_automation import clean_stale_invites
        with patch(f"{_RA}.log_info") as mock_log:
            clean_stale_invites.run(user_id=7)
        assert mock_log.call_count == 1
        _, kwargs = mock_log.call_args
        assert kwargs.get("user_id") == 7
        assert kwargs.get("task_name") == "clean_stale_invites"
