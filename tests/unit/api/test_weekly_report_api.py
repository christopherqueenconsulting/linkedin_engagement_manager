"""GET /api/user/weekly-report (issue #2632)."""

from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit

_M = "cqc_lem.api.main"
_F = "cqc_lem.utilities.flags.flag_enabled"
_L = "cqc_lem.utilities.weekly_report.load_weekly_report"


def test_flag_off_is_404(api_client):
    with patch(f"{_M}.get_session_user_id", return_value=1), patch(_F, return_value=False):
        resp = api_client.get("/api/user/weekly-report", params={"session_token": "t"})
    assert resp.status_code == 404


def test_bad_session_is_401(api_client):
    with patch(f"{_M}.get_session_user_id", return_value=None):
        resp = api_client.get("/api/user/weekly-report", params={"session_token": "t"})
    assert resp.status_code == 401


def test_flag_on_returns_payload_for_the_caller_only(api_client):
    with patch(f"{_M}.get_session_user_id", return_value=7), patch(_F, return_value=True), \
         patch(_L, return_value={"numbers": {}}) as load:
        resp = api_client.get("/api/user/weekly-report", params={"session_token": "t"})
    assert resp.status_code == 200
    assert resp.json()["detail"] == {"numbers": {}}
    load.assert_called_once_with(7)
