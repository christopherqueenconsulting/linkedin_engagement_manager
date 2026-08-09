"""Unit tests for PUT /api/user/company-page."""

from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit

_M = "cqc_lem.api.main"
_USER = "cqc_lem.api.routers.user"


@pytest.fixture(scope="module")
def client():
    patches = [
        patch("cqc_lem.utilities.observability.track_api_call"),
        patch("cqc_lem.app.engagement.invites.automate_invites_to_company_page_for_user"),
        patch("cqc_lem.app.engagement.posting.automate_reply_commenting"),
        patch("cqc_lem.app.run_content_plan.auto_create_weekly_content"),
        patch("cqc_lem.app.aws_test_celery_task.test_get_my_profile"),
    ]
    for p in patches:
        p.start()
    try:
        from fastapi.testclient import TestClient

        from cqc_lem.api.main import app
        with TestClient(app, raise_server_exceptions=False) as tc:
            yield tc
    finally:
        for p in patches:
            p.stop()


_VALID = "https://www.linkedin.com/company/acme/"


class TestUpdateCompanyPage:
    def test_saves_valid_url(self, client):
        with patch(f"{_M}.get_session_user_id", return_value=42), \
             patch(f"{_USER}.update_company_linked_in_url_for_user", return_value=True) as upd:
            resp = client.put("/api/user/company-page",
                              json={"session_token": "t", "company_linked_in_url": _VALID})
        assert resp.status_code == 200
        upd.assert_called_once_with(42, _VALID)

    def test_clears_when_empty(self, client):
        with patch(f"{_M}.get_session_user_id", return_value=42), \
             patch(f"{_USER}.update_company_linked_in_url_for_user", return_value=True) as upd:
            resp = client.put("/api/user/company-page",
                              json={"session_token": "t", "company_linked_in_url": ""})
        assert resp.status_code == 200
        upd.assert_called_once_with(42, None)

    def test_rejects_non_linkedin_url(self, client):
        with patch(f"{_M}.get_session_user_id", return_value=42), \
             patch(f"{_USER}.update_company_linked_in_url_for_user", return_value=True) as upd:
            resp = client.put("/api/user/company-page",
                              json={"session_token": "t", "company_linked_in_url": "https://example.com/x"})
        assert resp.status_code == 422
        upd.assert_not_called()

    def test_401_invalid_session(self, client):
        with patch(f"{_M}.get_session_user_id", return_value=None):
            resp = client.put("/api/user/company-page",
                              json={"session_token": "bad", "company_linked_in_url": _VALID})
        assert resp.status_code == 401
