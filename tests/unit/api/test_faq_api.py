"""Unit tests for the public FAQ endpoint (issue #506)."""

from datetime import datetime
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit

_M = "cqc_lem.api.main"
_DB = "cqc_lem.utilities.db"

_ENTRY = {"id": 3, "question": "Can I cancel anytime?", "answer": "Yes.", "cluster_id": 12,
          "updated_at": datetime(2026, 7, 25, 12, 0)}


class TestFaqEndpoint:
    def test_serves_the_published_entries(self, api_client):
        with patch(f"{_DB}.get_published_faq_entries", return_value=[_ENTRY]):
            resp = api_client.get("/api/faq")
        assert resp.status_code == 200
        # cluster_id is internal plumbing for the auto-FAQ pass — not public copy.
        assert resp.json()["detail"] == {"entries": [{
            "id": 3, "question": "Can I cancel anytime?", "answer": "Yes.",
            "updated_at": "2026-07-25T12:00:00Z"}]}

    def test_an_empty_faq_is_not_an_error(self, api_client):
        """The SPA falls back to its seeded copy on an empty list, so this must stay a 200."""
        with patch(f"{_DB}.get_published_faq_entries", return_value=[]):
            resp = api_client.get("/api/faq")
        assert resp.status_code == 200
        assert resp.json()["detail"] == {"entries": []}

    def test_is_reachable_by_a_logged_out_visitor(self):
        """The landing page carries no bearer token — /api/faq must stay outside the token gate."""
        from cqc_lem.api.main import _PUBLIC_API_PREFIXES, _api_token_required
        assert "/api/faq" in _PUBLIC_API_PREFIXES
        with patch(f"{_M}._API_ACCESS_TOKEN_SET", {"secret"}):
            assert _api_token_required("/api/faq") is False
            assert _api_token_required("/api/user/shipped") is True
