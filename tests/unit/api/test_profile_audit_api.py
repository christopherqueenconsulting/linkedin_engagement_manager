"""GET /api/user/profile-audit and POST /api/user/profile-audit/finding (issue #2753)."""

from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit

_M = "cqc_lem.api.main"
_F = "cqc_lem.utilities.flags.flag_enabled"
_V = "cqc_lem.utilities.profile_audit_view.load_audit_view"
_DB = "cqc_lem.utilities.db."
_URL = "/api/user/profile-audit"
_BODY = {"session_token": "t", "check_id": "custom_url", "action": "ignored"}


def test_get_bad_session_401_and_flag_off_404(api_client):
    with patch(f"{_M}.get_session_user_id", return_value=None):
        assert api_client.get(_URL, params={"session_token": "t"}).status_code == 401
    with patch(f"{_M}.get_session_user_id", return_value=1), patch(_F, return_value=False):
        assert api_client.get(_URL, params={"session_token": "t"}).status_code == 404


def test_get_returns_view_for_caller(api_client):
    with patch(f"{_M}.get_session_user_id", return_value=7), patch(_F, return_value=True), \
            patch(_V, return_value={"latest": None, "trend": [], "correlation": {}}) as load:
        resp = api_client.get(_URL, params={"session_token": "t"})
    assert resp.status_code == 200
    assert resp.json()["detail"]["latest"] is None
    load.assert_called_once_with(7)


def test_get_failed_read_is_503_not_empty(api_client):
    with patch(f"{_M}.get_session_user_id", return_value=7), patch(_F, return_value=True), \
            patch(_V, return_value=None):
        assert api_client.get(_URL, params={"session_token": "t"}).status_code == 503


def test_post_records_action_against_latest_audit(api_client):
    with patch(f"{_M}.get_session_user_id", return_value=7), patch(_F, return_value=True), \
            patch(_DB + "get_latest_profile_audit", return_value={"id": 9}), \
            patch(_DB + "set_profile_finding_action", return_value=True) as store:
        assert api_client.post(f"{_URL}/finding", json=_BODY).status_code == 200
    store.assert_called_once_with(7, "custom_url", "ignored", 9)


def test_post_reopen_clears(api_client):
    with patch(f"{_M}.get_session_user_id", return_value=7), patch(_F, return_value=True), \
            patch(_DB + "get_latest_profile_audit", return_value={"id": 9}), \
            patch(_DB + "set_profile_finding_action", return_value=True) as store:
        assert api_client.post(f"{_URL}/finding", json={**_BODY, "action": "reopen"}).status_code == 200
    store.assert_called_once_with(7, "custom_url", None, 9)


def test_post_rejections(api_client):
    with patch(f"{_M}.get_session_user_id", return_value=None):
        assert api_client.post(f"{_URL}/finding", json=_BODY).status_code == 401
    with patch(f"{_M}.get_session_user_id", return_value=7), patch(_F, return_value=False):
        assert api_client.post(f"{_URL}/finding", json=_BODY).status_code == 404
    with patch(f"{_M}.get_session_user_id", return_value=7), patch(_F, return_value=True):
        assert api_client.post(f"{_URL}/finding", json={**_BODY, "check_id": "nope"}).status_code == 404
        assert api_client.post(f"{_URL}/finding", json={**_BODY, "action": "delete"}).status_code == 422
        with patch(_DB + "get_latest_profile_audit", return_value=None):
            assert api_client.post(f"{_URL}/finding", json=_BODY).status_code == 404


def test_post_unsaved_is_503(api_client):
    with patch(f"{_M}.get_session_user_id", return_value=7), patch(_F, return_value=True), \
            patch(_DB + "get_latest_profile_audit", return_value={"id": 9}), \
            patch(_DB + "set_profile_finding_action", return_value=False):
        assert api_client.post(f"{_URL}/finding", json=_BODY).status_code == 503
