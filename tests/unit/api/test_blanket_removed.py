"""Acceptance test for issue #1212: the blanket autouse patch is gone.

A test that does not state an account state must fail hard, not silently pass on a
default fake session. This module is intentionally separate from conftest.py so it
cannot accidentally inherit any future autouse helper.
"""
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.unit


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
        from cqc_lem.api.main import app
        with TestClient(app, raise_server_exceptions=False) as tc:
            yield tc
    finally:
        for p in patches:
            p.stop()


class TestNoDefaultAccountState:
    def test_authenticated_endpoint_fails_without_stated_state(self, client):
        """A test that states nothing must hit the real session resolver and fail."""
        resp = client.get("/api/user/account-readiness?session_token=anything")
        assert resp.status_code == 500
