"""Acceptance test for issue #1212: the blanket autouse patch is gone.

A test that does not state an account state must fail hard, not silently pass on a
default fake session. The client comes from the shared `api_client` fixture (#1214),
which is exactly what makes this an honest check: that fixture patches NOTHING, so a
500 here can only mean the real session resolver ran.
"""

import pytest

pytestmark = pytest.mark.unit


class TestNoDefaultAccountState:
    def test_authenticated_endpoint_fails_without_stated_state(self, api_client):
        """A test that states nothing must hit the real session resolver and fail."""
        resp = api_client.get("/api/user/account-readiness?session_token=anything")
        assert resp.status_code == 500
