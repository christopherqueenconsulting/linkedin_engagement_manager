"""Unit tests for the /api/user/engagement-preferences endpoints."""

from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture(scope="module")
def client():
    patches = [
        patch("cqc_lem.utilities.observability.track_api_call"),
        patch("cqc_lem.app.run_automation.automate_invites_to_company_page_for_user"),
        patch("cqc_lem.app.run_automation.automate_reply_commenting"),
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


_SESSION = "tok"
_USER = 5


class TestGetEngagementPreferences:
    def test_returns_prefs(self, client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_USER), \
             patch("cqc_lem.api.main.has_engagement_preferences", return_value=True), \
             patch("cqc_lem.api.main.get_engagement_preferences",
                   return_value={"tone": "warm", "comment_length": "short"}):
            resp = client.get(f"/api/user/engagement-preferences?session_token={_SESSION}")
        assert resp.status_code == 200
        assert resp.json()["detail"]["tone"] == "warm"
        assert resp.json()["detail"]["has_saved_preferences"] is True

    def test_flags_a_never_configured_account(self, client):
        """The Settings hub starts these — and only these — on the Balanced preset (#558)."""
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_USER), \
             patch("cqc_lem.api.main.has_engagement_preferences", return_value=False), \
             patch("cqc_lem.api.main.get_engagement_preferences",
                   return_value={"tone": None, "comment_length": "medium"}):
            resp = client.get(f"/api/user/engagement-preferences?session_token={_SESSION}")
        assert resp.status_code == 200
        assert resp.json()["detail"]["has_saved_preferences"] is False

    def test_401(self, client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=None):
            resp = client.get("/api/user/engagement-preferences?session_token=bad")
        assert resp.status_code == 401


class TestUpdateEngagementPreferences:
    def test_updates_and_excludes_session_token(self, client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_USER), \
             patch("cqc_lem.api.main.update_engagement_preferences", return_value=True) as upd:
            resp = client.put("/api/user/engagement-preferences",
                              json={"session_token": _SESSION, "tone": "bold", "include_topics": ["AI"]})
        assert resp.status_code == 200
        prefs_arg = upd.call_args[0][1]
        assert "session_token" not in prefs_arg
        assert prefs_arg["tone"] == "bold" and prefs_arg["include_topics"] == ["AI"]

    def test_accepts_focus_and_goals(self, client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_USER), \
             patch("cqc_lem.api.main.update_engagement_preferences", return_value=True) as upd:
            resp = client.put("/api/user/engagement-preferences",
                              json={"session_token": _SESSION, "focus_topics": ["B2B sales"],
                                    "business_goals": "book calls", "personal_goals": "grow authority"})
        assert resp.status_code == 200
        prefs_arg = upd.call_args[0][1]
        assert prefs_arg["focus_topics"] == ["B2B sales"]
        assert prefs_arg["business_goals"] == "book calls"
        assert prefs_arg["personal_goals"] == "grow authority"

    def test_default_video_quality_passthrough(self, client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_USER), \
             patch("cqc_lem.api.main.update_engagement_preferences", return_value=True) as upd:
            resp = client.put("/api/user/engagement-preferences",
                              json={"session_token": _SESSION, "default_video_quality": "premium"})
        assert resp.status_code == 200
        assert upd.call_args[0][1]["default_video_quality"] == "premium"

    def test_default_video_quality_defaults_standard_when_omitted(self, client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_USER), \
             patch("cqc_lem.api.main.update_engagement_preferences", return_value=True) as upd:
            resp = client.put("/api/user/engagement-preferences", json={"session_token": _SESSION})
        assert resp.status_code == 200
        assert upd.call_args[0][1]["default_video_quality"] == "standard"

    def test_invalid_video_quality_coerced_to_standard(self, client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_USER), \
             patch("cqc_lem.api.main.update_engagement_preferences", return_value=True) as upd:
            resp = client.put("/api/user/engagement-preferences",
                              json={"session_token": _SESSION, "default_video_quality": "bogus"})
        assert resp.status_code == 200
        assert upd.call_args[0][1]["default_video_quality"] == "standard"

    def test_comment_length_passthrough(self, client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_USER), \
             patch("cqc_lem.api.main.update_engagement_preferences", return_value=True) as upd:
            resp = client.put("/api/user/engagement-preferences",
                              json={"session_token": _SESSION, "comment_length": "long"})
        assert resp.status_code == 200
        assert upd.call_args[0][1]["comment_length"] == "long"

    def test_comment_length_defaults_medium_when_omitted(self, client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_USER), \
             patch("cqc_lem.api.main.update_engagement_preferences", return_value=True) as upd:
            resp = client.put("/api/user/engagement-preferences", json={"session_token": _SESSION})
        assert resp.status_code == 200
        assert upd.call_args[0][1]["comment_length"] == "medium"

    def test_invalid_comment_length_coerced_to_medium(self, client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_USER), \
             patch("cqc_lem.api.main.update_engagement_preferences", return_value=True) as upd:
            resp = client.put("/api/user/engagement-preferences",
                              json={"session_token": _SESSION, "comment_length": "bogus"})
        assert resp.status_code == 200
        assert upd.call_args[0][1]["comment_length"] == "medium"

    def test_500_on_failure(self, client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_USER), \
             patch("cqc_lem.api.main.update_engagement_preferences", return_value=False):
            resp = client.put("/api/user/engagement-preferences", json={"session_token": _SESSION})
        assert resp.status_code == 500

    def test_401(self, client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=None):
            resp = client.put("/api/user/engagement-preferences", json={"session_token": "bad"})
        assert resp.status_code == 401

    def test_long_tone_passes_through(self, client):
        # Regression: a realistic multi-word tone (>64 chars) must not be dropped or truncated by
        # the app layer. The DB column length itself is guarded by migration V52 (see below).
        long_tone = "direct, warm, credible, plainspoken - a practitioner, not a pitch, and then some"
        assert len(long_tone) > 64
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_USER), \
             patch("cqc_lem.api.main.update_engagement_preferences", return_value=True) as upd:
            resp = client.put("/api/user/engagement-preferences",
                              json={"session_token": _SESSION, "tone": long_tone})
        assert resp.status_code == 200
        assert upd.call_args[0][1]["tone"] == long_tone


class TestReplyCheckConfig:
    def test_get_includes_reply_inbound_address(self, client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_USER), \
             patch("cqc_lem.api.main.get_engagement_preferences",
                   return_value={"reply_check_mode": "event"}), \
             patch("cqc_lem.api.main.get_or_create_reply_inbound_token", return_value="tok9"), \
             patch.dict("os.environ", {"LINKEDIN_PARSE_DOMAIN": "parse.example.com"}):
            resp = client.get(f"/api/user/engagement-preferences?session_token={_SESSION}")
        assert resp.status_code == 200
        assert resp.json()["detail"]["reply_inbound_address"] == "reply+tok9@parse.example.com"

    def test_reply_mode_passthrough_and_clamps(self, client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_USER), \
             patch("cqc_lem.api.main.update_engagement_preferences", return_value=True) as upd:
            resp = client.put("/api/user/engagement-preferences",
                              json={"session_token": _SESSION, "reply_check_mode": "scheduled",
                                    "reply_sweeps_per_day": 99, "reply_max_post_age_days": 0})
        assert resp.status_code == 200
        arg = upd.call_args[0][1]
        assert arg["reply_check_mode"] == "scheduled"
        assert arg["reply_sweeps_per_day"] == 12   # clamped to max
        assert arg["reply_max_post_age_days"] == 1  # clamped to min

    def test_bad_reply_mode_coerced_to_event(self, client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_USER), \
             patch("cqc_lem.api.main.update_engagement_preferences", return_value=True) as upd:
            resp = client.put("/api/user/engagement-preferences",
                              json={"session_token": _SESSION, "reply_check_mode": "bogus"})
        assert resp.status_code == 200
        assert upd.call_args[0][1]["reply_check_mode"] == "event"


class TestPostsPerWeek:
    """Publishing cadence (issue #621)."""

    def test_defaults_to_three_when_omitted(self, client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_USER), \
             patch("cqc_lem.api.main.update_engagement_preferences", return_value=True) as upd:
            resp = client.put("/api/user/engagement-preferences",
                              json={"session_token": _SESSION})
        assert resp.status_code == 200
        assert upd.call_args[0][1]["posts_per_week"] == 3

    def test_passthrough_and_clamps(self, client):
        for given, expected in ((2, 2), (7, 7), (0, 2), (99, 7)):
            with patch("cqc_lem.api.main.get_session_user_id", return_value=_USER), \
                 patch("cqc_lem.api.main.update_engagement_preferences", return_value=True) as upd:
                resp = client.put("/api/user/engagement-preferences",
                                  json={"session_token": _SESSION, "posts_per_week": given})
            assert resp.status_code == 200
            assert upd.call_args[0][1]["posts_per_week"] == expected


class TestPostingDays:
    """The publishing day allow-list (issue #581)."""

    def test_defaults_to_monday_to_friday_when_omitted(self, client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_USER), \
             patch("cqc_lem.api.main.update_engagement_preferences", return_value=True) as upd:
            resp = client.put("/api/user/engagement-preferences",
                              json={"session_token": _SESSION})
        assert resp.status_code == 200
        assert upd.call_args[0][1]["posting_days"] == [0, 1, 2, 3, 4]

    def test_all_seven_days_are_accepted(self, client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_USER), \
             patch("cqc_lem.api.main.update_engagement_preferences", return_value=True) as upd:
            resp = client.put("/api/user/engagement-preferences",
                              json={"session_token": _SESSION, "posting_days": [6, 5, 4, 3, 2, 1, 0]})
        assert resp.status_code == 200
        assert upd.call_args[0][1]["posting_days"] == [0, 1, 2, 3, 4, 5, 6]

    def test_bad_values_fall_back_rather_than_422_the_whole_save(self, client):
        # The SPA saves every engagement field in one request — a malformed day list must not take
        # the user's tone, caps and targeting down with it.
        for given in ([], None, "nonsense", [9, -1], ["mon"]):
            with patch("cqc_lem.api.main.get_session_user_id", return_value=_USER), \
                 patch("cqc_lem.api.main.update_engagement_preferences", return_value=True) as upd:
                resp = client.put("/api/user/engagement-preferences",
                                  json={"session_token": _SESSION, "posting_days": given})
            assert resp.status_code == 200, given
            assert upd.call_args[0][1]["posting_days"] == [0, 1, 2, 3, 4], given


class TestEngagementPersistenceRegression:
    """Guards for the class of bug that silently dropped engagement settings."""

    def test_every_request_field_has_a_db_column(self):
        # If a settings field exists on the API model but not in the DB column set, saves that
        # include it would fail/no-op — exactly the failure mode we fixed. Keep them in lockstep.
        from cqc_lem.api.main import EngagementPreferencesRequest
        from cqc_lem.utilities.db import _ENGAGEMENT_DEFAULTS
        fields = set(EngagementPreferencesRequest.model_fields) - {"session_token"}
        missing = fields - set(_ENGAGEMENT_DEFAULTS)
        assert not missing, f"model fields not persisted by db: {missing}"

    def test_over_limit_tone_rejected_with_422(self, client):
        # Both-sides alignment: a value longer than the DB column returns a clean 422 (Pydantic
        # max_length) and never reaches the DB, instead of a 500 MySQL 1406.
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_USER), \
             patch("cqc_lem.api.main.update_engagement_preferences", return_value=True) as upd:
            resp = client.put("/api/user/engagement-preferences",
                              json={"session_token": _SESSION, "tone": "x" * 256})
        assert resp.status_code == 422
        upd.assert_not_called()

    def test_v52_migration_widens_tone(self):
        # The root cause was tone VARCHAR(64) overflowing (MySQL 1406) and rolling back the whole
        # engagement upsert. Assert the migration widening is present so it can't silently regress.
        from pathlib import Path
        p = (Path(__file__).resolve().parents[3]
             / "compose/local/database/migrations/V52__widen_engagement_tone.sql")
        sql = p.read_text().lower()
        assert "tone" in sql and "varchar(255)" in sql


class TestFeedFallbackAndReach:
    def test_feed_fallback_passthrough(self, client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_USER), \
             patch("cqc_lem.api.main.update_engagement_preferences", return_value=True) as upd:
            resp = client.put("/api/user/engagement-preferences",
                              json={"session_token": _SESSION, "feed_fallback_when_empty": False})
        assert resp.status_code == 200
        assert upd.call_args[0][1]["feed_fallback_when_empty"] is False

    def test_link_in_first_comment_passthrough(self, client):
        """The #392 opt-out reaches the DB layer, and defaults ON when the client omits it."""
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_USER), \
             patch("cqc_lem.api.main.update_engagement_preferences", return_value=True) as upd:
            off = client.put("/api/user/engagement-preferences",
                             json={"session_token": _SESSION, "link_in_first_comment": False})
            default = client.put("/api/user/engagement-preferences",
                                 json={"session_token": _SESSION})
        assert off.status_code == 200 and default.status_code == 200
        assert upd.call_args_list[0][0][1]["link_in_first_comment"] is False
        assert upd.call_args_list[1][0][1]["link_in_first_comment"] is True

    def test_get_includes_feed_reach(self, client):
        funnel = {"examined": 40, "passed_filters": 12, "matched_topics": 0,
                  "commented": 3, "fallback_used": True}
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_USER), \
             patch("cqc_lem.api.main.get_engagement_preferences", return_value={}), \
             patch("cqc_lem.api.main.get_or_create_reply_inbound_token", return_value=None), \
             patch("cqc_lem.app.run_automation.get_feed_funnel", return_value=funnel):
            resp = client.get(f"/api/user/engagement-preferences?session_token={_SESSION}")
        assert resp.status_code == 200
        assert resp.json()["detail"]["feed_reach"] == funnel


class TestGmailForwardConfirmationInPrefs:
    def test_get_includes_gmail_forward_confirmation(self, client):
        conf = {"code": "12345678", "confirmed": False, "url_found": True}
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_USER), \
             patch("cqc_lem.api.main.get_engagement_preferences", return_value={}), \
             patch("cqc_lem.api.main.get_or_create_reply_inbound_token", return_value=None), \
             patch("cqc_lem.api.main.get_gmail_forward_confirmation", return_value=conf):
            resp = client.get(f"/api/user/engagement-preferences?session_token={_SESSION}")
        assert resp.status_code == 200
        assert resp.json()["detail"]["gmail_forward_confirmation"] == conf


class TestConnectionTargetingPrefs:
    """Smart connection targeting configuration (issue #486)."""

    def test_targeting_fields_passthrough(self, client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_USER), \
             patch("cqc_lem.api.main.update_engagement_preferences", return_value=True) as upd:
            resp = client.put("/api/user/engagement-preferences",
                              json={"session_token": _SESSION,
                                    "connection_targeting_mode": "auto_queue",
                                    "connection_target_authors": ["https://x/in/guru"],
                                    "min_connection_icp_score": 70})
        assert resp.status_code == 200
        prefs_arg = upd.call_args[0][1]
        assert prefs_arg["connection_targeting_mode"] == "auto_queue"
        assert prefs_arg["connection_target_authors"] == ["https://x/in/guru"]
        assert prefs_arg["min_connection_icp_score"] == 70

    def test_defaults_to_suggest_when_omitted(self, client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_USER), \
             patch("cqc_lem.api.main.update_engagement_preferences", return_value=True) as upd:
            resp = client.put("/api/user/engagement-preferences",
                              json={"session_token": _SESSION})
        assert resp.status_code == 200
        # Default posture never sends on its own: candidates are filed as drafts.
        assert upd.call_args[0][1]["connection_targeting_mode"] == "suggest"
        assert upd.call_args[0][1]["min_connection_icp_score"] == 55

    def test_bad_mode_coerced_and_icp_clamped(self, client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_USER), \
             patch("cqc_lem.api.main.update_engagement_preferences", return_value=True) as upd:
            resp = client.put("/api/user/engagement-preferences",
                              json={"session_token": _SESSION,
                                    "connection_targeting_mode": "blast_everyone",
                                    "min_connection_icp_score": 500})
        assert resp.status_code == 200
        assert upd.call_args[0][1]["connection_targeting_mode"] == "suggest"
        assert upd.call_args[0][1]["min_connection_icp_score"] == 100


class TestRosterAutoFollow:
    """Opt-in roster auto-follow (issue #962)."""

    def test_the_toggle_defaults_off_and_an_omitted_cap_is_left_alone(self, client):
        # The toggle defaults OFF (a client that never learned the field cannot switch an outbound
        # lane on), but the CAP is not written at all when omitted — writing the code default would
        # overwrite a deliberate 0 and restart the lane at 3/day.
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_USER), \
             patch("cqc_lem.api.main.update_engagement_preferences", return_value=True) as upd:
            resp = client.put("/api/user/engagement-preferences",
                              json={"session_token": _SESSION})
        assert resp.status_code == 200
        assert upd.call_args[0][1]["roster_auto_follow"] is False
        assert "max_follows_per_day" not in upd.call_args[0][1]

    def test_the_toggle_round_trips(self, client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_USER), \
             patch("cqc_lem.api.main.update_engagement_preferences", return_value=True) as upd:
            resp = client.put("/api/user/engagement-preferences",
                              json={"session_token": _SESSION, "roster_auto_follow": True})
        assert resp.status_code == 200
        assert upd.call_args[0][1]["roster_auto_follow"] is True

    def test_the_cap_clamps_instead_of_422ing_the_whole_save(self, client):
        # The SPA writes every engagement field in one request, so one bad number must never take
        # the other 40 settings down with it (the V52 lesson).
        from cqc_lem.utilities.db import ROSTER_FOLLOWS_PER_DAY_MAX
        for given, expected in ((0, 0), (3, 3), (-5, 0), (999, ROSTER_FOLLOWS_PER_DAY_MAX)):
            with patch("cqc_lem.api.main.get_session_user_id", return_value=_USER), \
                 patch("cqc_lem.api.main.update_engagement_preferences", return_value=True) as upd:
                resp = client.put("/api/user/engagement-preferences",
                                  json={"session_token": _SESSION, "max_follows_per_day": given})
            assert resp.status_code == 200
            assert upd.call_args[0][1]["max_follows_per_day"] == expected
