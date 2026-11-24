"""GET /api/user/icp-audience-report (issue #2667)."""

from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit

_M = "cqc_lem.api.main"
_F = "cqc_lem.utilities.flags.flag_enabled"
_L = "cqc_lem.utilities.icp_audience_report.load_icp_audience_report"
_URL = "/api/user/icp-audience-report"


def test_flag_off_is_404(api_client):
    with patch(f"{_M}.get_session_user_id", return_value=1), patch(_F, return_value=False):
        assert api_client.get(_URL, params={"session_token": "t"}).status_code == 404


def test_bad_session_is_401(api_client):
    with patch(f"{_M}.get_session_user_id", return_value=None):
        assert api_client.get(_URL, params={"session_token": "t"}).status_code == 401


def test_returns_payload_for_the_caller_only(api_client):
    with patch(f"{_M}.get_session_user_id", return_value=7), patch(_F, return_value=True), \
         patch(_L, return_value={"readable": True}) as load:
        resp = api_client.get(_URL, params={"session_token": "t"})
    assert resp.status_code == 200 and resp.json()["detail"] == {"readable": True}
    load.assert_called_once_with(7)
