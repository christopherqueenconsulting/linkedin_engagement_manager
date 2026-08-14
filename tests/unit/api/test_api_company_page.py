"""Unit tests for PUT /api/user/company-page."""

from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit

_M = "cqc_lem.api.main"
_USER = "cqc_lem.api.routers.user"


_VALID = "https://www.linkedin.com/company/acme/"


class TestUpdateCompanyPage:
    def test_saves_valid_url(self, api_client):
        with patch(f"{_M}.get_session_user_id", return_value=42), \
             patch(f"{_USER}.update_company_linked_in_url_for_user", return_value=True) as upd:
            resp = api_client.put("/api/user/company-page",
                              json={"session_token": "t", "company_linked_in_url": _VALID})
        assert resp.status_code == 200
        upd.assert_called_once_with(42, _VALID)

    def test_clears_when_empty(self, api_client):
        with patch(f"{_M}.get_session_user_id", return_value=42), \
             patch(f"{_USER}.update_company_linked_in_url_for_user", return_value=True) as upd:
            resp = api_client.put("/api/user/company-page",
                              json={"session_token": "t", "company_linked_in_url": ""})
        assert resp.status_code == 200
        upd.assert_called_once_with(42, None)

    def test_rejects_non_linkedin_url(self, api_client):
        with patch(f"{_M}.get_session_user_id", return_value=42), \
             patch(f"{_USER}.update_company_linked_in_url_for_user", return_value=True) as upd:
            resp = api_client.put("/api/user/company-page",
                              json={"session_token": "t", "company_linked_in_url": "https://example.com/x"})
        assert resp.status_code == 422
        upd.assert_not_called()

    def test_401_invalid_session(self, api_client):
        with patch(f"{_M}.get_session_user_id", return_value=None):
            resp = api_client.put("/api/user/company-page",
                              json={"session_token": "bad", "company_linked_in_url": _VALID})
        assert resp.status_code == 401
