"""The read-only engagement-mode surfaces of the API (issue #2367).

The mode on `GET /user/settings` and the Suggested list behind `GET /user/engagement-suggestions`.
"""

from datetime import datetime
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit

_MODE = "cqc_lem.utilities.engagement_mode.get_user_engagement_mode"
_URL = "https://www.linkedin.com/feed/update/urn:li:ugcPost:7479519458164695040/"

_MAIN = "cqc_lem.api.main"
_USER = "cqc_lem.api.routers.user"


class TestApi:
    def test_settings_reports_the_mode_read_only(self, api_client, signed_in):
        with patch(f"{_MAIN}.get_session_user_id", return_value=5), \
             patch(f"{_USER}.get_user_subscription_info", return_value=None), \
             patch(f"{_USER}.get_user_preferences", return_value=None), \
             patch(f"{_USER}.get_user_blog_url", return_value=None), \
             patch(f"{_USER}.get_user_sitemap_url", return_value=None), \
             patch(f"{_USER}.get_company_linked_in_url_for_user", return_value=None), \
             patch(_MODE, return_value=None):
            resp = api_client.get("/api/user/settings", params={"session_token": "t"})
        assert resp.status_code == 200
        # Unreadable is reported the way the lanes act on it.
        assert resp.json()["detail"]["engagement_mode"] == "suggest"

    def test_suggestions_list(self, api_client, signed_in):
        when = datetime(2026, 10, 10, 12, 0)
        rows = [{"id": 2, "kind": "comment", "source": "auto_seed_comment_on_post",
                 "target_url": _URL, "body": "Q?", "created_at": when}]
        with patch(f"{_MAIN}.get_session_user_id", return_value=5), \
             patch(f"{_USER}.get_engagement_suggestions", return_value=rows) as read, \
             patch(_MODE, return_value="suggest"):
            resp = api_client.get("/api/user/engagement-suggestions", params={"session_token": "t"})
        assert resp.status_code == 200
        detail = resp.json()["detail"]
        assert detail["engagement_mode"] == "suggest"
        assert detail["suggestions"] == [{**rows[0], "created_at": when.isoformat()}]
        read.assert_called_once_with(5, limit=50)

    def test_suggestions_read_failure_is_503(self, api_client, signed_in):
        with patch(f"{_MAIN}.get_session_user_id", return_value=5), \
             patch(f"{_USER}.get_engagement_suggestions", return_value=None):
            resp = api_client.get("/api/user/engagement-suggestions", params={"session_token": "t"})
        assert resp.status_code == 503

    def test_suggestions_need_a_session(self, api_client):
        with patch(f"{_MAIN}.get_session_user_id", return_value=None):
            resp = api_client.get("/api/user/engagement-suggestions", params={"session_token": "x"})
        assert resp.status_code == 401
