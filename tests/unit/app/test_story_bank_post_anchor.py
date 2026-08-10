"""Unit tests for the story-bank wiring in the post pipeline (issue #620): the selected entry
reaches the writer prompt, an empty bank ships the no-fabrication fallback instead of an invented
anecdote, the entry's use is only counted when a post actually came out, and a draft that states a
specific we never supplied is regenerated once.
"""

from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit

_RCP = "cqc_lem.app.run_content_plan"

_DISABLED_LM = {"enabled": False, "keyword": None, "message": None}
_STORY = {"id": 5, "kind": "client_win", "title": "Onboarding rewrite",
          "body": "We cut a client's onboarding from 12 days to 3.", "happened_at": None,
          "used_count": 0, "last_used_at": None, "active": True}


def _run(stories=None, prefs=None, generated="generated post", post_id=77):
    from cqc_lem.app import run_content_plan as rcp
    captured = {}

    def gen(user_profile, stage, prefs=None, profile_synthesis=None, blueprint=None,
            lead_magnet_cta=None, post_id=None, history_directive=None, story_directive=None,
            content_mix=None, user_id=None):
        captured["story_directive"] = story_directive
        return generated

    with patch(f"{_RCP}.get_engagement_preferences", return_value=prefs or {}), \
         patch(f"{_RCP}.get_or_create_profile_synthesis", return_value="voice"), \
         patch(f"{_RCP}.get_lead_magnet_settings", return_value=_DISABLED_LM), \
         patch(f"{_RCP}.get_recent_post_texts", return_value=[]), \
         patch(f"{_RCP}.get_recent_post_shape_history", return_value=[]), \
         patch(f"{_RCP}.update_db_post_shape"), \
         patch(f"{_RCP}.get_story_bank_entries", return_value=stories or []) as fetch, \
         patch(f"{_RCP}.record_story_bank_use") as use, \
         patch(f"{_RCP}.get_thought_leadership_post_from_ai", side_effect=gen):
        out = rcp.create_text_post(1, "awareness", post_type="thought_leadership",
                                   user_profile=MagicMock(), refine_final_post=False,
                                   post_id=post_id)
    return out, captured, fetch, use


class TestStoryReachesThePrompt:
    def test_selected_entry_is_injected_as_the_factual_anchor(self):
        _, captured, _, _ = _run(stories=[_STORY])
        assert "12 days to 3" in captured["story_directive"]
        assert "ONLY personal specifics" in captured["story_directive"]

    def test_only_active_entries_are_requested(self):
        _, _, fetch, _ = _run(stories=[_STORY])
        assert fetch.call_args.kwargs.get("active_only") is True

    def test_empty_bank_ships_the_no_fabrication_fallback(self):
        from cqc_lem.utilities.ai import story_bank as sb
        _, captured, _, _ = _run(stories=[])
        assert captured["story_directive"] == sb.no_story_directive()

    def test_focus_topics_gate_relevance(self):
        from cqc_lem.utilities.ai import story_bank as sb
        _, captured, _, _ = _run(stories=[_STORY], prefs={"focus_topics": ["deep sea fishing"]})
        assert captured["story_directive"] == sb.no_story_directive()

    def test_db_failure_degrades_to_the_fallback(self):
        from cqc_lem.app import run_content_plan as rcp
        from cqc_lem.utilities.ai import story_bank as sb
        with patch(f"{_RCP}.get_story_bank_entries", side_effect=RuntimeError("no db")):
            assert rcp._select_story_for_post(1, {}, {}) is None
        assert sb.story_directive(None) == sb.no_story_directive()


class TestUseAccounting:
    def test_use_is_recorded_once_a_post_exists(self):
        _, _, _, use = _run(stories=[_STORY])
        use.assert_called_once_with(1, 5)

    def test_no_use_recorded_when_generation_produced_nothing(self):
        _, _, _, use = _run(stories=[_STORY], generated=None)
        use.assert_not_called()

    def test_no_use_recorded_when_the_bank_could_not_ground_the_post(self):
        _, _, _, use = _run(stories=[])
        use.assert_not_called()


def _review(content, story=_STORY, second="second draft", lead_magnet_cta="", **env):
    from cqc_lem.app import run_content_plan as rcp
    with patch(f"{_RCP}.create_text_post", return_value=second) as retry, \
         patch(f"{_RCP}._check_post_alignment", return_value=True), \
         patch.dict("os.environ", env, clear=False):
        out = rcp._review_generated_post(
            1, "awareness", "thought_leadership", MagicMock(), {}, 77, lead_magnet_cta, content, [],
            prefs={}, profile_synthesis="", story=story, story_directive="STORY DIRECTIVE")
    return out, retry


class TestFabricationGate:
    _SOURCED = "I cut a client's onboarding from 12 days to 3."
    _INVENTED = "I cut onboarding from 12 days to 3, and I grew their revenue 47%."

    def test_sourced_specifics_ship_without_a_retry(self):
        out, retry = _review(self._SOURCED)
        assert out == self._SOURCED
        retry.assert_not_called()

    def test_invented_specific_triggers_one_regeneration(self):
        out, retry = _review(self._INVENTED)
        assert out == "second draft"
        retry.assert_called_once()
        assert retry.call_args.kwargs["story_directive"] == "STORY DIRECTIVE"
        assert "47" in retry.call_args.kwargs["history_directive"]

    def test_lead_magnet_cta_numbers_are_not_flagged_as_fabricated(self):
        # The CTA directive is material WE handed the writer — a number in the user's configured
        # resource name ("my 5-step checklist") must not trigger a spurious regeneration that
        # would then be steered to strip the CTA mechanic.
        cta = "Lead magnet: comment AUDIT and I'll DM you my 5-step checklist."
        content = ("I cut a client's onboarding from 12 days to 3. "
                   "Comment AUDIT and I'll DM you my 5-step checklist.")
        out, retry = _review(content, lead_magnet_cta=cta)
        assert out == content
        retry.assert_not_called()

    def test_regeneration_can_be_switched_off(self):
        out, retry = _review(self._INVENTED, POST_FABRICATION_REGEN_ENABLED="off")
        assert out == self._INVENTED
        retry.assert_not_called()

    def test_no_story_means_no_allow_list_and_no_gate(self):
        # Without an entry every number would look fabricated — the check must not run at all.
        out, retry = _review(self._INVENTED, story=None)
        assert out == self._INVENTED
        retry.assert_not_called()

    def test_a_still_fabricating_retry_is_kept_not_looped(self):
        out, retry = _review(self._INVENTED, second="I still invented 91% growth.")
        assert out == "I still invented 91% growth."
        assert retry.call_count == 1

    def test_failed_retry_keeps_the_first_draft(self):
        from cqc_lem.app import run_content_plan as rcp
        with patch(f"{_RCP}.create_text_post", side_effect=RuntimeError("llm down")), \
             patch(f"{_RCP}._check_post_alignment", return_value=True):
            out = rcp._review_generated_post(
                1, "awareness", "thought_leadership", MagicMock(), {}, 77, "", self._INVENTED, [],
                prefs={}, profile_synthesis="", story=_STORY, story_directive="STORY DIRECTIVE")
        assert out == self._INVENTED
