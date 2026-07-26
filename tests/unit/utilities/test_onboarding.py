"""Unit tests for the activation checklist + stalled-user nudges (issue #500):
step derivation, one-shot state persistence + PostHog emission, and the pure nudge selector."""

from datetime import datetime, timedelta

import pytest
from unittest.mock import patch

pytestmark = pytest.mark.unit

_ON = "cqc_lem.utilities.onboarding"

NOW = datetime(2026, 7, 25, 12, 0, 0)


def _steps(**done):
    """Checklist dict keyed like the module does, defaulting every step to incomplete."""
    from cqc_lem.utilities.db import ONBOARDING_STEPS
    steps = {step: False for step in ONBOARDING_STEPS}
    for key, value in done.items():
        steps[key] = value
    return steps


class TestSelectNudge:
    def test_no_nudge_before_the_grace_period(self):
        from cqc_lem.utilities.onboarding import select_nudge
        assert select_nudge(_steps(), started_at=NOW - timedelta(hours=23), now=NOW) is None

    def test_connect_nudge_after_24h_without_a_session(self):
        from cqc_lem.utilities.onboarding import select_nudge, NUDGE_CONNECT
        nudge = select_nudge(_steps(), started_at=NOW - timedelta(hours=25), now=NOW)
        assert nudge["key"] == NUDGE_CONNECT
        assert nudge["cta_path"] == "/account"

    def test_voice_nudge_after_48h_once_connected(self):
        from cqc_lem.utilities.onboarding import select_nudge, NUDGE_VOICE
        steps = _steps(linkedin_connected=True)
        assert select_nudge(steps, started_at=NOW - timedelta(hours=47), now=NOW) is None
        nudge = select_nudge(steps, started_at=NOW - timedelta(hours=49), now=NOW)
        assert nudge["key"] == NUDGE_VOICE

    def test_first_post_nudge_after_72h(self):
        from cqc_lem.utilities.onboarding import select_nudge, NUDGE_FIRST_POST
        steps = _steps(linkedin_connected=True, voice_set=True)
        assert select_nudge(steps, started_at=NOW - timedelta(hours=71), now=NOW) is None
        nudge = select_nudge(steps, started_at=NOW - timedelta(hours=73), now=NOW)
        assert nudge["key"] == NUDGE_FIRST_POST

    def test_earliest_incomplete_step_wins(self):
        """A user who set their voice but never connected still gets the connect nudge."""
        from cqc_lem.utilities.onboarding import select_nudge, NUDGE_CONNECT
        steps = _steps(voice_set=True, first_post_approved=True)
        nudge = select_nudge(steps, started_at=NOW - timedelta(days=5), now=NOW)
        assert nudge["key"] == NUDGE_CONNECT

    def test_activated_user_is_never_nudged(self):
        from cqc_lem.utilities.onboarding import select_nudge
        steps = _steps(activated=True)
        assert select_nudge(steps, started_at=NOW - timedelta(days=10),
                            trial_ends_at=NOW + timedelta(days=1), now=NOW) is None

    def test_each_nudge_sends_only_once(self):
        from cqc_lem.utilities.onboarding import select_nudge, NUDGE_CONNECT
        assert select_nudge(_steps(), started_at=NOW - timedelta(days=3),
                            sent_keys={NUDGE_CONNECT}, now=NOW) is None

    def test_cooldown_blocks_a_second_nudge_the_same_day(self):
        from cqc_lem.utilities.onboarding import select_nudge
        assert select_nudge(_steps(), started_at=NOW - timedelta(days=3),
                            last_sent_at=NOW - timedelta(hours=2), now=NOW) is None

    def test_trial_ending_nudge_once_the_step_nudge_was_sent(self):
        from cqc_lem.utilities.onboarding import select_nudge, NUDGE_CONNECT, NUDGE_TRIAL_ENDING
        nudge = select_nudge(_steps(), started_at=NOW - timedelta(days=11),
                             trial_ends_at=NOW + timedelta(days=2),
                             sent_keys={NUDGE_CONNECT}, now=NOW)
        assert nudge["key"] == NUDGE_TRIAL_ENDING

    def test_no_trial_nudge_when_the_trial_is_far_out_or_over(self):
        from cqc_lem.utilities.onboarding import select_nudge, NUDGE_CONNECT
        sent = {NUDGE_CONNECT}
        started = NOW - timedelta(days=11)
        assert select_nudge(_steps(), started_at=started, trial_ends_at=NOW + timedelta(days=5),
                            sent_keys=sent, now=NOW) is None
        assert select_nudge(_steps(), started_at=started, trial_ends_at=NOW - timedelta(hours=1),
                            sent_keys=sent, now=NOW) is None

    def test_missing_started_at_never_nudges_a_step(self):
        from cqc_lem.utilities.onboarding import select_nudge
        assert select_nudge(_steps(), started_at=None, now=NOW) is None


class TestStoryBankNudge:
    """The story-bank seeding nudge (issue #620) — fires once the voice is set and the bank is
    still under the target, behind the blocking setup steps but ahead of the trial warning."""

    _DONE = dict(linkedin_connected=True, voice_set=True, first_post_approved=True,
                 caps_enabled=True)

    def test_fires_for_an_empty_bank_after_the_grace_period(self):
        from cqc_lem.utilities.onboarding import select_nudge, NUDGE_STORY_BANK
        nudge = select_nudge(_steps(**self._DONE), started_at=NOW - timedelta(hours=49),
                             story_bank_count=0, now=NOW)
        assert nudge["key"] == NUDGE_STORY_BANK
        assert nudge["cta_path"] == "/account?section=content"

    def test_waits_out_the_grace_period(self):
        from cqc_lem.utilities.onboarding import select_nudge
        assert select_nudge(_steps(**self._DONE), started_at=NOW - timedelta(hours=47),
                            story_bank_count=0, now=NOW) is None

    def test_a_seeded_bank_is_not_nudged(self):
        from cqc_lem.utilities.onboarding import select_nudge, STORY_BANK_TARGET_ENTRIES
        assert select_nudge(_steps(**self._DONE), started_at=NOW - timedelta(days=5),
                            story_bank_count=STORY_BANK_TARGET_ENTRIES, now=NOW) is None

    def test_unknown_count_never_nudges(self):
        from cqc_lem.utilities.onboarding import select_nudge
        assert select_nudge(_steps(**self._DONE), started_at=NOW - timedelta(days=5),
                            story_bank_count=None, now=NOW) is None

    def test_waits_until_the_voice_is_set(self):
        from cqc_lem.utilities.onboarding import select_nudge, NUDGE_CONNECT, NUDGE_VOICE
        # Voice still unset: the voice STEP nudge wins, and once that has been sent the story bank
        # still waits rather than jumping the queue.
        steps = _steps(linkedin_connected=True)
        assert select_nudge(steps, started_at=NOW - timedelta(days=5), story_bank_count=0,
                            now=NOW)["key"] == NUDGE_VOICE
        assert select_nudge(steps, started_at=NOW - timedelta(days=5), story_bank_count=0,
                            sent_keys={NUDGE_CONNECT, NUDGE_VOICE}, now=NOW) is None

    def test_sends_only_once(self):
        from cqc_lem.utilities.onboarding import select_nudge, NUDGE_STORY_BANK
        assert select_nudge(_steps(**self._DONE), started_at=NOW - timedelta(days=5),
                            story_bank_count=0, sent_keys={NUDGE_STORY_BANK}, now=NOW) is None

    def test_ranks_ahead_of_the_trial_warning(self):
        from cqc_lem.utilities.onboarding import select_nudge, NUDGE_STORY_BANK
        nudge = select_nudge(_steps(**self._DONE), started_at=NOW - timedelta(days=5),
                             trial_ends_at=NOW + timedelta(days=1), story_bank_count=0, now=NOW)
        assert nudge["key"] == NUDGE_STORY_BANK

    def test_activated_user_is_still_never_nudged(self):
        from cqc_lem.utilities.onboarding import select_nudge
        assert select_nudge(_steps(activated=True, voice_set=True),
                            started_at=NOW - timedelta(days=5), story_bank_count=0, now=NOW) is None


def _eval_patches(*, session=True, prefs=None, configured=True, approved=True, posted=True,
                  engaged=True):
    base_prefs = {"tone": "friendly", "comment_style": None, "focus_topics": [],
                  "include_topics": [], "max_comments_per_day": 20, "max_dms_per_day": 20}
    base_prefs.update(prefs or {})

    def _has_post(_user_id, statuses):
        from cqc_lem.utilities.db import PostStatus
        return posted if tuple(statuses) == (PostStatus.POSTED,) else approved

    return [
        patch(f"{_ON}.has_linkedin_session", return_value=session),
        patch(f"{_ON}.get_engagement_preferences", return_value=base_prefs),
        patch(f"{_ON}.has_engagement_preferences", return_value=configured),
        patch(f"{_ON}.has_post_with_status", side_effect=_has_post),
        patch(f"{_ON}.has_automated_engagement", return_value=engaged),
    ]


def _evaluate(**kw):
    ctxs = _eval_patches(**kw)
    for c in ctxs:
        c.start()
    try:
        from cqc_lem.utilities.onboarding import evaluate_steps
        return evaluate_steps(1)
    finally:
        for c in ctxs:
            c.stop()


class TestEvaluateSteps:
    def test_all_steps_complete(self):
        from cqc_lem.utilities.db import OnboardingStep
        steps = _evaluate()
        assert all(steps[s] for s in OnboardingStep)

    def test_voice_and_caps_need_a_saved_preferences_row(self):
        """Code-level defaults apply to everyone, so an unsaved row must not count as configured."""
        from cqc_lem.utilities.db import OnboardingStep
        steps = _evaluate(configured=False)
        assert steps[OnboardingStep.VOICE_SET] is False
        assert steps[OnboardingStep.CAPS_ENABLED] is False

    def test_voice_counts_when_only_topics_are_set(self):
        from cqc_lem.utilities.db import OnboardingStep
        steps = _evaluate(prefs={"tone": None, "focus_topics": ["ai"]})
        assert steps[OnboardingStep.VOICE_SET] is True

    def test_caps_off_when_both_are_zero(self):
        from cqc_lem.utilities.db import OnboardingStep
        steps = _evaluate(prefs={"max_comments_per_day": 0, "max_dms_per_day": 0})
        assert steps[OnboardingStep.CAPS_ENABLED] is False

    def test_activation_needs_both_a_published_post_and_engagement(self):
        from cqc_lem.utilities.db import OnboardingStep
        assert _evaluate(engaged=False)[OnboardingStep.ACTIVATED] is False
        assert _evaluate(posted=False)[OnboardingStep.ACTIVATED] is False


class TestSyncOnboardingState:
    def test_marks_new_steps_once_and_tracks_them(self):
        from cqc_lem.utilities.db import OnboardingStep
        state = {"started_at": NOW - timedelta(hours=5),
                 "linkedin_connected_at": NOW - timedelta(hours=4)}
        with patch(f"{_ON}.ensure_onboarding_state", return_value=True), \
             patch(f"{_ON}.get_onboarding_state", return_value=state), \
             patch(f"{_ON}.evaluate_steps", return_value={
                 OnboardingStep.LINKEDIN_CONNECTED: True, OnboardingStep.VOICE_SET: True,
                 OnboardingStep.FIRST_POST_APPROVED: False, OnboardingStep.CAPS_ENABLED: True,
                 OnboardingStep.ACTIVATED: False}), \
             patch(f"{_ON}.mark_onboarding_step", return_value=True) as mark, \
             patch("cqc_lem.utilities.observability.track_onboarding_step") as track:
            from cqc_lem.utilities.onboarding import sync_onboarding_state
            sync_onboarding_state(7)

        marked = [c.args[1] for c in mark.call_args_list]
        # Already-stamped and incomplete steps are skipped.
        assert marked == [OnboardingStep.VOICE_SET, OnboardingStep.CAPS_ENABLED]
        assert track.call_count == 2

    def test_each_step_also_emits_the_funnel_event_and_activation(self):
        from cqc_lem.utilities.db import OnboardingStep
        state = {"started_at": NOW - timedelta(hours=5)}
        with patch(f"{_ON}.ensure_onboarding_state", return_value=True), \
             patch(f"{_ON}.get_onboarding_state", return_value=state), \
             patch(f"{_ON}.evaluate_steps", return_value={
                 OnboardingStep.LINKEDIN_CONNECTED: True, OnboardingStep.ACTIVATED: True}), \
             patch(f"{_ON}.mark_onboarding_step", return_value=True), \
             patch("cqc_lem.utilities.observability.track_onboarding_step"), \
             patch("cqc_lem.utilities.observability.track_funnel_event") as funnel:
            from cqc_lem.utilities.onboarding import sync_onboarding_state
            sync_onboarding_state(7)

        events = [c.args[0] for c in funnel.call_args_list]
        # One step event per completed step, plus the dedicated `activated` milestone.
        assert events == ["onboarding_step_completed", "onboarding_step_completed", "activated"]
        assert funnel.call_args_list[0].kwargs["step"] == str(OnboardingStep.LINKEDIN_CONNECTED)
        assert funnel.call_args_list[-1].kwargs["user_id"] == 7

    def test_tracking_failure_does_not_break_the_sync(self):
        from cqc_lem.utilities.db import OnboardingStep
        with patch(f"{_ON}.ensure_onboarding_state", return_value=True), \
             patch(f"{_ON}.get_onboarding_state", return_value={"started_at": NOW}), \
             patch(f"{_ON}.evaluate_steps",
                   return_value={OnboardingStep.LINKEDIN_CONNECTED: True}), \
             patch(f"{_ON}.mark_onboarding_step", return_value=True), \
             patch("cqc_lem.utilities.observability.track_onboarding_step",
                   side_effect=RuntimeError("posthog down")):
            from cqc_lem.utilities.onboarding import sync_onboarding_state
            assert sync_onboarding_state(7) == {"started_at": NOW}


class TestNextNudgeForUser:
    def test_reads_persisted_steps_sent_ledger_and_trial(self):
        from cqc_lem.utilities.onboarding import next_nudge_for_user, NUDGE_CONNECT
        state = {"started_at": datetime.now() - timedelta(days=2)}
        with patch(f"{_ON}.get_onboarding_nudges_sent", return_value={}), \
             patch(f"{_ON}.get_user_subscription_info", return_value={"trial_ends_at": None}):
            nudge = next_nudge_for_user(3, state)
        assert nudge["key"] == NUDGE_CONNECT

    def test_respects_the_cooldown_from_the_last_sent_nudge(self):
        from cqc_lem.utilities.onboarding import next_nudge_for_user
        state = {"started_at": datetime.now() - timedelta(days=4)}
        with patch(f"{_ON}.get_onboarding_nudges_sent",
                   return_value={"set_voice": datetime.now() - timedelta(hours=1)}), \
             patch(f"{_ON}.get_user_subscription_info", return_value={}):
            assert next_nudge_for_user(3, state) is None


class TestSnapshotAndSend:
    def test_snapshot_shape(self):
        from cqc_lem.utilities.db import OnboardingStep
        state = {"started_at": NOW, f"{OnboardingStep.LINKEDIN_CONNECTED.value}_at": NOW}
        with patch(f"{_ON}.sync_onboarding_state", return_value=state), \
             patch(f"{_ON}.next_nudge_for_user", return_value=None):
            from cqc_lem.utilities.onboarding import onboarding_snapshot
            snap = onboarding_snapshot(5)
        assert snap["activated"] is False
        assert snap["nudge"] is None
        assert [s["key"] for s in snap["steps"]] == [str(s) for s in OnboardingStep]
        assert snap["steps"][0]["ok"] is True and snap["steps"][1]["ok"] is False

    def test_send_uses_llm_copy_and_records_the_nudge(self):
        from cqc_lem.utilities.onboarding import send_onboarding_nudge, _STEP_NUDGES
        nudge = dict(_STEP_NUDGES[0])
        with patch("cqc_lem.utilities.ai.ai_helper.generate_onboarding_nudge_copy",
                   return_value="Fresh copy."), \
             patch("cqc_lem.utilities.notifications.notify_onboarding_nudge",
                   return_value=True) as notify, \
             patch(f"{_ON}.record_onboarding_nudge", return_value=True) as record:
            assert send_onboarding_nudge(9, nudge) is True
        assert notify.call_args.args[1]["body"] == "Fresh copy."
        record.assert_called_once_with(9, nudge["key"])

    def test_send_falls_back_to_default_copy_when_the_llm_fails(self):
        from cqc_lem.utilities.onboarding import send_onboarding_nudge, _STEP_NUDGES
        nudge = dict(_STEP_NUDGES[0])
        with patch("cqc_lem.utilities.ai.ai_helper.generate_onboarding_nudge_copy",
                   side_effect=RuntimeError("llm down")), \
             patch("cqc_lem.utilities.notifications.notify_onboarding_nudge",
                   return_value=True) as notify, \
             patch(f"{_ON}.record_onboarding_nudge", return_value=True):
            assert send_onboarding_nudge(9, nudge) is True
        assert notify.call_args.args[1]["body"] == nudge["body"]

    def test_nothing_is_recorded_when_the_email_fails(self):
        from cqc_lem.utilities.onboarding import send_onboarding_nudge, _STEP_NUDGES
        with patch("cqc_lem.utilities.ai.ai_helper.generate_onboarding_nudge_copy",
                   return_value="Fresh copy."), \
             patch("cqc_lem.utilities.notifications.notify_onboarding_nudge", return_value=False), \
             patch(f"{_ON}.record_onboarding_nudge") as record:
            assert send_onboarding_nudge(9, dict(_STEP_NUDGES[0])) is False
        record.assert_not_called()
