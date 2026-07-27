"""Unit tests for the LEM brand (dogfooding) account policy + scheduling glue (issue #504)."""

import pytest
from unittest.mock import patch

pytestmark = pytest.mark.unit

_BA = "cqc_lem.utilities.brand_account"
# BRAND_SIGNUP_URL moved behind utilities/marketing/attribution.signup_url (issue #658), so the
# signup CTA's env constant is patched on THAT module now.
_ATTR = "cqc_lem.utilities.marketing.attribution"
_DB = "cqc_lem.utilities.db"
_RS = "cqc_lem.app.run_scheduler"


def _enabled(email="brand@lem.test", signup_url="", phase="P0"):
    """Patch the module-level env constants brand_account read at import time."""
    return [
        patch(f"{_BA}.BRAND_ACCOUNT_ENABLED", True),
        patch(f"{_BA}.BRAND_ACCOUNT_EMAIL", email),
        patch(f"{_ATTR}.BRAND_SIGNUP_URL", signup_url),
        patch(f"{_BA}.LAUNCH_PHASE", phase),
    ]


class _Patched:
    """Apply a list of patchers as one context manager."""

    def __init__(self, patchers):
        self._patchers = patchers

    def __enter__(self):
        for p in self._patchers:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in reversed(self._patchers):
            p.stop()
        return False


class TestCurrentLaunchPhase:
    def test_reads_the_configured_phase(self):
        from cqc_lem.utilities.brand_account import current_launch_phase
        with patch(f"{_BA}.LAUNCH_PHASE", "p1"):
            assert current_launch_phase() == "P1"

    def test_unknown_phase_falls_back_to_the_most_conservative(self):
        from cqc_lem.utilities.brand_account import current_launch_phase
        with patch(f"{_BA}.LAUNCH_PHASE", "GA"):
            assert current_launch_phase() == "P0"

    def test_empty_phase_falls_back_without_warning(self):
        from cqc_lem.utilities.brand_account import current_launch_phase
        with patch(f"{_BA}.LAUNCH_PHASE", ""), patch(f"{_BA}.log_warning") as warn:
            assert current_launch_phase() == "P0"
        warn.assert_not_called()


class TestBrandOutboundPolicy:
    def test_volume_ramps_with_the_phase(self):
        from cqc_lem.utilities.brand_account import brand_outbound_policy
        p0, p1, p2 = (brand_outbound_policy(p) for p in ("P0", "P1", "P2"))
        for cap in ("max_comments_per_day", "max_dms_per_day", "max_invites_per_day"):
            assert p0[cap] < p1[cap] <= p2[cap]

    def test_p0_keeps_a_human_on_every_connect(self):
        from cqc_lem.utilities.brand_account import brand_outbound_policy
        policy = brand_outbound_policy("P0")
        assert policy["connection_request_mode"] == "pre_review"
        assert policy["connection_targeting_mode"] == "suggest"

    def test_no_phase_exceeds_the_per_user_ceilings(self):
        from cqc_lem.utilities.brand_account import (BRAND_CAP_CEILINGS, LAUNCH_PHASES,
                                                     brand_outbound_policy)
        for phase in LAUNCH_PHASES:
            policy = brand_outbound_policy(phase)
            for cap, ceiling in BRAND_CAP_CEILINGS.items():
                assert policy[cap] <= ceiling

    def test_an_over_ceiling_phase_entry_is_clamped(self):
        from cqc_lem.utilities.brand_account import brand_outbound_policy
        hot = {"P2": {"max_comments_per_day": 500, "max_dms_per_day": 500,
                      "max_invites_per_day": 500, "connection_request_mode": "auto_approve",
                      "connection_targeting_mode": "auto_queue"}}
        with patch(f"{_BA}.PHASE_OUTBOUND_POLICY", hot):
            assert brand_outbound_policy("P2") == {
                "max_comments_per_day": 20, "max_dms_per_day": 20, "max_invites_per_day": 10,
                "connection_request_mode": "auto_approve",
                "connection_targeting_mode": "auto_queue"}

    def test_defaults_to_the_active_phase(self):
        from cqc_lem.utilities.brand_account import brand_outbound_policy
        with patch(f"{_BA}.LAUNCH_PHASE", "P1"):
            assert brand_outbound_policy() == brand_outbound_policy("P1")


class TestGetBrandUserId:
    def test_none_when_disabled(self):
        from cqc_lem.utilities.brand_account import get_brand_user_id
        with patch(f"{_BA}.BRAND_ACCOUNT_ENABLED", False), \
             patch(f"{_DB}.get_user_id") as lookup:
            assert get_brand_user_id() is None
        lookup.assert_not_called()

    def test_none_when_email_missing(self):
        from cqc_lem.utilities.brand_account import get_brand_user_id
        with _Patched(_enabled(email="  ")), patch(f"{_DB}.get_user_id") as lookup:
            assert get_brand_user_id() is None
        lookup.assert_not_called()

    def test_none_when_no_user_matches(self):
        from cqc_lem.utilities.brand_account import get_brand_user_id
        with _Patched(_enabled()), patch(f"{_DB}.get_user_id", return_value=None):
            assert get_brand_user_id() is None

    def test_resolves_the_configured_email(self):
        from cqc_lem.utilities.brand_account import get_brand_user_id
        with _Patched(_enabled()), patch(f"{_DB}.get_user_id", return_value=7) as lookup:
            assert get_brand_user_id() == 7
        lookup.assert_called_once_with("brand@lem.test")

    def test_is_brand_user(self):
        from cqc_lem.utilities.brand_account import is_brand_user
        with _Patched(_enabled()), patch(f"{_DB}.get_user_id", return_value=7):
            assert is_brand_user(7) is True
            assert is_brand_user(8) is False


class TestBrandPreferenceOverrides:
    def test_seeds_icp_focus_topics_when_none_set(self):
        from cqc_lem.utilities.brand_account import BRAND_FOCUS_TOPICS, brand_preference_overrides
        overrides = brand_preference_overrides({"focus_topics": []}, "P0")
        assert overrides["focus_topics"] == list(BRAND_FOCUS_TOPICS)

    def test_keeps_owner_tuned_focus_topics(self):
        from cqc_lem.utilities.brand_account import brand_preference_overrides
        overrides = brand_preference_overrides({"focus_topics": ["agency growth"]}, "P0")
        assert "focus_topics" not in overrides

    def test_blank_focus_topics_count_as_unset(self):
        from cqc_lem.utilities.brand_account import BRAND_FOCUS_TOPICS, brand_preference_overrides
        overrides = brand_preference_overrides({"focus_topics": ["  "]}, "P0")
        assert overrides["focus_topics"] == list(BRAND_FOCUS_TOPICS)

    def test_seeds_the_signup_cta_utm_tagged(self):
        """Issue #658: the goal line is echoed by the brand's own posts/DMs, so the URL in it has to
        arrive tagged — an untagged one makes every signup it drives read as `direct`."""
        from cqc_lem.utilities.brand_account import brand_preference_overrides
        with patch(f"{_ATTR}.BRAND_SIGNUP_URL", "https://lem.test/trial"):
            overrides = brand_preference_overrides({}, "P0")
        goal = overrides["business_goals"]
        assert goal.startswith("Drive free-trial signups at https://lem.test/trial?")
        assert "utm_source=linkedin" in goal
        assert "utm_medium=profile" in goal
        assert "utm_campaign=brand-profile" in goal

    def test_no_cta_without_a_signup_url_and_never_over_existing_goals(self):
        from cqc_lem.utilities.brand_account import brand_preference_overrides
        with patch(f"{_ATTR}.BRAND_SIGNUP_URL", ""):
            assert "business_goals" not in brand_preference_overrides({}, "P0")
        with patch(f"{_ATTR}.BRAND_SIGNUP_URL", "https://lem.test/trial"):
            assert "business_goals" not in brand_preference_overrides({"business_goals": "mine"}, "P0")

    def test_caps_are_always_reasserted_over_a_manual_edit(self):
        from cqc_lem.utilities.brand_account import brand_preference_overrides
        overrides = brand_preference_overrides({"max_comments_per_day": 999}, "P0")
        assert overrides["max_comments_per_day"] == 8


class TestSyncBrandPreferences:
    def test_no_brand_account_is_a_no_op(self):
        from cqc_lem.utilities.brand_account import sync_brand_preferences
        with patch(f"{_BA}.BRAND_ACCOUNT_ENABLED", False), \
             patch(f"{_DB}.update_engagement_preferences") as upsert:
            assert sync_brand_preferences() is None
        upsert.assert_not_called()

    def test_sends_only_the_policy_fields(self):
        """Issue #639: the upsert merges over the SAVED row, so this task must send its policy
        fields ONLY — re-sending the read-back prefs would rewrite every column from a dict that
        is code defaults whenever the read failed."""
        from cqc_lem.utilities.brand_account import sync_brand_preferences
        existing = {"tone": "warm", "max_comments_per_day": 20, "focus_topics": ["agency growth"]}
        with _Patched(_enabled(phase="P1")), \
             patch(f"{_DB}.get_user_id", return_value=7), \
             patch(f"{_DB}.get_engagement_preferences", return_value=existing), \
             patch(f"{_DB}.update_engagement_preferences", return_value=True) as upsert:
            applied = sync_brand_preferences()
        saved = upsert.call_args.args[1]
        assert upsert.call_args.args[0] == 7
        assert "tone" not in saved                            # voice the owner set is never rewritten
        assert "focus_topics" not in saved                    # nor their non-empty focus topics
        assert saved["max_comments_per_day"] == 15            # but the phase cap wins
        assert applied["max_dms_per_day"] == 10

    def test_unreadable_prefs_cannot_reset_the_whole_row(self):
        """A failed read makes `get_engagement_preferences` return code DEFAULTS. Those must never
        ride into the upsert — the db layer aborts on its own unreadable read, and it can only do
        that if this caller isn't handing it a full 39-column dict."""
        from cqc_lem.utilities.brand_account import sync_brand_preferences
        from cqc_lem.utilities.db import _ENGAGEMENT_DEFAULTS
        with _Patched(_enabled(phase="P0")), \
             patch(f"{_DB}.get_user_id", return_value=7), \
             patch(f"{_DB}.get_engagement_preferences", return_value=dict(_ENGAGEMENT_DEFAULTS)), \
             patch(f"{_DB}.update_engagement_preferences", return_value=True) as upsert:
            sync_brand_preferences()
        saved = upsert.call_args.args[1]
        assert set(saved) <= {"max_comments_per_day", "max_dms_per_day", "max_invites_per_day",
                              "connection_request_mode", "connection_targeting_mode",
                              "focus_topics", "business_goals"}
        assert "tone" not in saved and "reply_check_mode" not in saved

    def test_explicit_phase_argument_overrides_the_env(self):
        from cqc_lem.utilities.brand_account import sync_brand_preferences
        with _Patched(_enabled(phase="P0")), \
             patch(f"{_DB}.get_user_id", return_value=7), \
             patch(f"{_DB}.get_engagement_preferences", return_value={}), \
             patch(f"{_DB}.update_engagement_preferences", return_value=True) as upsert:
            sync_brand_preferences("p2")
        assert upsert.call_args.args[1]["max_comments_per_day"] == 20

    def test_returns_none_when_the_upsert_fails(self):
        from cqc_lem.utilities.brand_account import sync_brand_preferences
        with _Patched(_enabled()), \
             patch(f"{_DB}.get_user_id", return_value=7), \
             patch(f"{_DB}.get_engagement_preferences", return_value={}), \
             patch(f"{_DB}.update_engagement_preferences", return_value=False):
            assert sync_brand_preferences() is None


class TestAutoSyncBrandAccount:
    def test_no_op_when_not_configured(self):
        from cqc_lem.app.run_scheduler import auto_sync_brand_account
        with patch(f"{_BA}.get_brand_user_id", return_value=None), \
             patch(f"{_BA}.sync_brand_preferences") as sync:
            assert auto_sync_brand_account() == "Brand account not configured"
        sync.assert_not_called()

    def test_syncs_and_reports_the_applied_caps(self):
        from cqc_lem.app.run_scheduler import auto_sync_brand_account
        applied = {"max_comments_per_day": 8, "max_dms_per_day": 5, "max_invites_per_day": 5}
        with patch(f"{_BA}.get_brand_user_id", return_value=7), \
             patch(f"{_BA}.current_launch_phase", return_value="P0"), \
             patch(f"{_BA}.sync_brand_preferences", return_value=applied) as sync, \
             patch(f"{_RS}.get_active_user_ids", return_value=[7]), \
             patch(f"{_RS}.log_warning") as warn:
            result = auto_sync_brand_account()
        sync.assert_called_once_with("P0")
        warn.assert_not_called()
        assert "synced to phase P0" in result
        assert "comments/day 8" in result

    def test_warns_when_the_brand_account_is_not_active(self):
        from cqc_lem.app.run_scheduler import auto_sync_brand_account
        applied = {"max_comments_per_day": 8, "max_dms_per_day": 5, "max_invites_per_day": 5}
        with patch(f"{_BA}.get_brand_user_id", return_value=7), \
             patch(f"{_BA}.current_launch_phase", return_value="P0"), \
             patch(f"{_BA}.sync_brand_preferences", return_value=applied), \
             patch(f"{_RS}.get_active_user_ids", return_value=[1, 2]), \
             patch(f"{_RS}.log_warning") as warn:
            auto_sync_brand_account()
        assert warn.call_count == 1

    def test_reports_a_failed_sync_without_touching_the_user_list(self):
        from cqc_lem.app.run_scheduler import auto_sync_brand_account
        with patch(f"{_BA}.get_brand_user_id", return_value=7), \
             patch(f"{_BA}.current_launch_phase", return_value="P1"), \
             patch(f"{_BA}.sync_brand_preferences", return_value=None), \
             patch(f"{_RS}.get_active_user_ids") as actives:
            assert auto_sync_brand_account() == "Brand account sync failed (phase P1)"
        actives.assert_not_called()


class TestBeatSchedule:
    def test_task_is_registered_daily_before_the_content_plan(self):
        from cqc_lem.app.my_celery import app
        entry = app.conf.beat_schedule["sync-brand-account"]
        assert entry["task"] == "cqc_lem.app.run_scheduler.auto_sync_brand_account"
        assert entry["schedule"].hour == {0}
        assert entry["schedule"].minute == {45}
