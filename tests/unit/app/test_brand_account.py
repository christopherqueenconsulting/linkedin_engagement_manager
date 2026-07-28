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


def _enabled(brand_user_id="", signup_url="", phase="P0"):
    """Patch the module-level env constants brand_account read at import time. Issue #736: the brand
    user resolves with NO env at all, so the default here is the empty override."""
    return [
        patch(f"{_BA}.BRAND_USER_ID", brand_user_id),
        patch(f"{_ATTR}.BRAND_SIGNUP_URL", signup_url),
        patch(f"{_ATTR}.PUBLIC_BASE_URL", ""),
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


class TestBrandUserId:
    """Issue #736: the brand account is user 1 by convention. No env var may be REQUIRED for the
    self-marketing engine to run — the old enabled/email pair kept it dormant for months."""

    def test_resolves_user_one_with_no_configuration_at_all(self):
        from cqc_lem.utilities.brand_account import brand_user_id
        with _Patched(_enabled()), patch(f"{_DB}.get_user_id") as lookup:
            assert brand_user_id() == 1
        lookup.assert_not_called()

    def test_explicit_override_is_honored(self):
        from cqc_lem.utilities.brand_account import brand_user_id
        with _Patched(_enabled(brand_user_id=" 7 ")):
            assert brand_user_id() == 7

    @pytest.mark.parametrize("bad", ["nope", "0", "-3", "1.5", None])
    def test_an_unusable_override_falls_back_to_the_convention(self, bad):
        """Falling back — never returning None. A typo must not switch self-marketing off silently."""
        from cqc_lem.utilities.brand_account import brand_user_id
        with _Patched(_enabled(brand_user_id=bad)):
            assert brand_user_id() == 1

    def test_is_brand_user(self):
        from cqc_lem.utilities.brand_account import is_brand_user
        with _Patched(_enabled()):
            assert is_brand_user(1) is True
            assert is_brand_user("1") is True
            assert is_brand_user(8) is False
            assert is_brand_user(None) is False
            assert is_brand_user("not-an-id") is False

    def test_is_brand_user_follows_the_override(self):
        from cqc_lem.utilities.brand_account import is_brand_user
        with _Patched(_enabled(brand_user_id="7")):
            assert is_brand_user(7) is True
            assert is_brand_user(1) is False


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

    def test_a_missing_cap_takes_the_phase_value(self):
        from cqc_lem.utilities.brand_account import brand_preference_overrides
        overrides = brand_preference_overrides({}, "P1")
        assert overrides["max_comments_per_day"] == 15
        assert overrides["max_dms_per_day"] == 10
        assert overrides["max_invites_per_day"] == 8

    def test_an_unreadable_cap_takes_the_phase_value(self):
        from cqc_lem.utilities.brand_account import brand_preference_overrides
        overrides = brand_preference_overrides({"max_dms_per_day": "many"}, "P1")
        assert overrides["max_dms_per_day"] == 10


class TestOwnerTunedSettingsSurvive:
    """Issue #736 — the brand user is ALSO the owner's ordinary account, so the phase policy is a
    CEILING, not an assignment: it only ever tightens."""

    def test_a_hand_tuned_lower_cap_is_kept(self):
        from cqc_lem.utilities.brand_account import brand_preference_overrides
        overrides = brand_preference_overrides({"max_comments_per_day": 3}, "P1")
        assert overrides["max_comments_per_day"] == 3       # his stricter number survives
        assert overrides["max_dms_per_day"] == 10           # the ones he never set take the phase

    def test_a_cap_of_zero_is_a_real_choice_not_an_unset_value(self):
        from cqc_lem.utilities.brand_account import brand_preference_overrides
        assert brand_preference_overrides({"max_invites_per_day": 0}, "P2")["max_invites_per_day"] == 0

    def test_a_stricter_connect_posture_is_kept_but_a_looser_one_is_not(self):
        from cqc_lem.utilities.brand_account import brand_preference_overrides
        strict = brand_preference_overrides(
            {"connection_request_mode": "pre_review", "connection_targeting_mode": "off"}, "P2")
        assert strict["connection_request_mode"] == "pre_review"
        assert strict["connection_targeting_mode"] == "off"
        loose = brand_preference_overrides(
            {"connection_request_mode": "auto_approve", "connection_targeting_mode": "auto_queue"},
            "P0")
        assert loose["connection_request_mode"] == "pre_review"
        assert loose["connection_targeting_mode"] == "suggest"

    def test_an_unknown_saved_mode_falls_back_to_the_phase(self):
        from cqc_lem.utilities.brand_account import brand_preference_overrides
        overrides = brand_preference_overrides({"connection_targeting_mode": "bogus"}, "P1")
        assert overrides["connection_targeting_mode"] == "auto_queue"

    def test_the_ceiling_can_never_be_raised_above_the_phase(self):
        from cqc_lem.utilities.brand_account import (CAP_FIELDS, LAUNCH_PHASES,
                                                     brand_outbound_policy,
                                                     brand_preference_overrides)
        hand_tuned = {cap: 999 for cap in CAP_FIELDS}
        for phase in LAUNCH_PHASES:
            policy = brand_outbound_policy(phase)
            overrides = brand_preference_overrides(hand_tuned, phase)
            for cap in CAP_FIELDS:
                assert overrides[cap] == policy[cap]


class TestPreferenceChanges:
    def test_reports_only_what_actually_changes(self):
        from cqc_lem.utilities.brand_account import preference_changes
        existing = {"max_comments_per_day": 8, "max_dms_per_day": 20}
        changes = preference_changes(existing, {"max_comments_per_day": 8, "max_dms_per_day": 5})
        assert changes == {"max_dms_per_day": (20, 5)}

    def test_a_string_column_matching_an_int_policy_is_not_a_change(self):
        """The DB can hand a cap back as a string — reporting that as an edit to the owner's own
        settings every single night would make the log useless."""
        from cqc_lem.utilities.brand_account import preference_changes
        assert preference_changes({"max_dms_per_day": "5"}, {"max_dms_per_day": 5}) == {}

    def test_focus_topic_lists_compare_by_value(self):
        from cqc_lem.utilities.brand_account import preference_changes
        assert preference_changes({"focus_topics": ["a", "b"]}, {"focus_topics": ["a", "b"]}) == {}
        assert preference_changes({"focus_topics": []}, {"focus_topics": ["a"]}) == {
            "focus_topics": ([], ["a"])}

    def test_an_unset_field_is_a_change(self):
        from cqc_lem.utilities.brand_account import preference_changes
        assert preference_changes({}, {"connection_request_mode": "pre_review"}) == {
            "connection_request_mode": (None, "pre_review")}


class TestSyncBrandPreferences:
    def test_syncs_user_one_with_no_configuration(self):
        from cqc_lem.utilities.brand_account import sync_brand_preferences
        with _Patched(_enabled(phase="P0")), \
             patch(f"{_DB}.get_engagement_preferences", return_value={}), \
             patch(f"{_DB}.update_engagement_preferences", return_value=True) as upsert:
            assert sync_brand_preferences() is not None
        assert upsert.call_args.args[0] == 1

    def test_sends_only_the_policy_fields(self):
        """Issue #639: the upsert merges over the SAVED row, so this task must send its policy
        fields ONLY — re-sending the read-back prefs would rewrite every column from a dict that
        is code defaults whenever the read failed."""
        from cqc_lem.utilities.brand_account import sync_brand_preferences
        existing = {"tone": "warm", "max_comments_per_day": 20, "focus_topics": ["agency growth"]}
        with _Patched(_enabled(phase="P1")), \
             patch(f"{_DB}.get_engagement_preferences", return_value=existing), \
             patch(f"{_DB}.update_engagement_preferences", return_value=True) as upsert:
            applied = sync_brand_preferences()
        saved = upsert.call_args.args[1]
        assert upsert.call_args.args[0] == 1
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
             patch(f"{_DB}.get_engagement_preferences", return_value={}), \
             patch(f"{_DB}.update_engagement_preferences", return_value=True) as upsert:
            sync_brand_preferences("p2")
        assert upsert.call_args.args[1]["max_comments_per_day"] == 20

    def test_returns_none_when_the_upsert_fails(self):
        from cqc_lem.utilities.brand_account import sync_brand_preferences
        with _Patched(_enabled()), \
             patch(f"{_DB}.get_engagement_preferences", return_value={}), \
             patch(f"{_DB}.update_engagement_preferences", return_value=False):
            assert sync_brand_preferences() is None

    def test_does_not_clobber_the_owners_stricter_caps(self):
        """The collision this convention introduces (#736): user 1 is the owner's own account."""
        from cqc_lem.utilities.brand_account import sync_brand_preferences
        existing = {"max_comments_per_day": 2, "max_dms_per_day": 1, "max_invites_per_day": 1,
                    "connection_request_mode": "pre_review", "connection_targeting_mode": "off",
                    "tone": "warm", "focus_topics": ["agency growth"]}
        with _Patched(_enabled(phase="P2")), \
             patch(f"{_DB}.get_engagement_preferences", return_value=existing), \
             patch(f"{_DB}.update_engagement_preferences", return_value=True) as upsert:
            sync_brand_preferences()
        saved = upsert.call_args.args[1]
        assert saved["max_comments_per_day"] == 2
        assert saved["max_dms_per_day"] == 1
        assert saved["max_invites_per_day"] == 1
        assert saved["connection_request_mode"] == "pre_review"
        assert saved["connection_targeting_mode"] == "off"

    def test_logs_every_field_it_changes(self):
        from cqc_lem.utilities.brand_account import sync_brand_preferences
        with _Patched(_enabled(phase="P0")), \
             patch(f"{_DB}.get_engagement_preferences", return_value={"max_dms_per_day": 20}), \
             patch(f"{_DB}.update_engagement_preferences", return_value=True), \
             patch(f"{_BA}.log_info") as info:
            sync_brand_preferences()
        message = info.call_args.args[0]
        assert "max_dms_per_day: 20 -> 5" in message
        assert info.call_args.kwargs["user_id"] == 1

    def test_an_unchanged_account_logs_no_edit(self):
        """A nightly INFO line that fires whether or not anything moved is not a change log."""
        from cqc_lem.utilities.brand_account import brand_preference_overrides, \
            sync_brand_preferences
        settled = dict(brand_preference_overrides({}, "P0"))
        with _Patched(_enabled(phase="P0")), \
             patch(f"{_DB}.get_engagement_preferences", return_value=settled), \
             patch(f"{_DB}.update_engagement_preferences", return_value=True), \
             patch(f"{_BA}.log_info") as info:
            sync_brand_preferences()
        info.assert_not_called()


class TestAutoSyncBrandAccount:
    def test_runs_with_no_env_configuration_at_all(self):
        """Issue #736's whole point: the beat used to return 'not configured' every night in prod."""
        from cqc_lem.app.run_scheduler import auto_sync_brand_account
        applied = {"max_comments_per_day": 8, "max_dms_per_day": 5, "max_invites_per_day": 5}
        with _Patched(_enabled(phase="P0")), \
             patch(f"{_BA}.sync_brand_preferences", return_value=applied) as sync, \
             patch(f"{_RS}.get_active_user_ids", return_value=[1]):
            result = auto_sync_brand_account()
        sync.assert_called_once_with("P0")
        assert result.startswith("Brand account 1 synced to phase P0")

    def test_syncs_and_reports_the_applied_caps(self):
        from cqc_lem.app.run_scheduler import auto_sync_brand_account
        applied = {"max_comments_per_day": 8, "max_dms_per_day": 5, "max_invites_per_day": 5}
        with patch(f"{_BA}.brand_user_id", return_value=7), \
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
        with patch(f"{_BA}.brand_user_id", return_value=7), \
             patch(f"{_BA}.current_launch_phase", return_value="P0"), \
             patch(f"{_BA}.sync_brand_preferences", return_value=applied), \
             patch(f"{_RS}.get_active_user_ids", return_value=[1, 2]), \
             patch(f"{_RS}.log_warning") as warn:
            auto_sync_brand_account()
        assert warn.call_count == 1

    def test_reports_a_failed_sync_without_touching_the_user_list(self):
        from cqc_lem.app.run_scheduler import auto_sync_brand_account
        with patch(f"{_BA}.brand_user_id", return_value=7), \
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
