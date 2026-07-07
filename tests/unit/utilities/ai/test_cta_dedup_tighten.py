"""Unit tests for the tightened lead-magnet CTA flow: strengthened prompt contract, rewrite-pass
preservation context, and soft-paraphrase dedup at repair time."""

import pytest
from unittest.mock import MagicMock, patch

pytestmark = pytest.mark.unit

_LM = {"enabled": True, "keyword": "AUDIT",
       "message": "Here you go, {first_name} - the AI Ops Readiness Checklist."}


class TestStrengthenedDirective:
    def test_directive_demands_verbatim_mechanic(self):
        from cqc_lem.utilities.ai.content_alignment import lead_magnet_cta_directive
        d = lead_magnet_cta_directive(_LM, True)
        assert "REQUIRED" in d
        assert "Do NOT paraphrase" in d
        assert "'reach out'" in d
        assert "exactly ONE ask" in d
        assert "AUDIT" in d

    def test_preserve_note_carries_keyword(self):
        from cqc_lem.utilities.ai.content_alignment import lead_magnet_preserve_note
        note = lead_magnet_preserve_note("AUDIT")
        assert "PRESERVE" in note and "AUDIT" in note and "comment" in note.lower()

    def test_preserve_note_empty_without_keyword(self):
        from cqc_lem.utilities.ai.content_alignment import lead_magnet_preserve_note
        assert lead_magnet_preserve_note(None) == ""
        assert lead_magnet_preserve_note("  ") == ""


class TestSoftOfferDedup:
    """post_id=30 is on the default 1-in-3 rotation."""

    def test_soft_paraphrase_stripped_when_repair_fires(self):
        # The live post-27 failure mode: model wove a soft offer, mechanic missing.
        from cqc_lem.utilities.ai.content_alignment import (ensure_lead_magnet_cta,
                                                            has_lead_magnet_cta_mechanic)
        content = ("Strong insight about AI costs.\n\n"
                   "Feel free to reach out if you'd like a checklist to help you balance both. "
                   "It only takes 5 minutes to run.")
        out = ensure_lead_magnet_cta(content, _LM, post_id=30)
        assert has_lead_magnet_cta_mechanic(out, "AUDIT")
        assert "reach out" not in out.lower()
        assert "Strong insight about AI costs." in out  # real content untouched

    def test_soft_duplicate_stripped_even_when_mechanic_survived(self):
        from cqc_lem.utilities.ai.content_alignment import ensure_lead_magnet_cta
        content = ("Real point here.\n\n"
                   "Happy to share my checklist if that helps.\n\n"
                   "Comment AUDIT and I'll DM it to you.")
        out = ensure_lead_magnet_cta(content, _LM, post_id=30)
        assert "Happy to share" not in out
        assert out.count("AUDIT") == 1  # exactly one ask, no second append

    def test_benign_reach_out_sentence_survives(self):
        # Offer verb WITHOUT a resource noun must never be stripped.
        from cqc_lem.utilities.ai.content_alignment import ensure_lead_magnet_cta
        content = "Reach out to your vendor and ask for pricing.\n\nComment AUDIT and I'll DM it to you."
        out = ensure_lead_magnet_cta(content, _LM, post_id=30)
        assert "Reach out to your vendor" in out

    def test_clean_selected_content_is_byte_identical(self):
        from cqc_lem.utilities.ai.content_alignment import ensure_lead_magnet_cta
        content = "Point.\n\nComment AUDIT and I'll DM you the checklist."
        assert ensure_lead_magnet_cta(content, _LM, post_id=30) == content

    def test_unselected_post_untouched_even_with_soft_offer(self):
        from cqc_lem.utilities.ai.content_alignment import ensure_lead_magnet_cta
        content = "Post.\n\nFeel free to reach out if you'd like the checklist."
        assert ensure_lead_magnet_cta(content, _LM, post_id=31) == content  # 31 % 3 != 0

    def test_mechanic_sentence_never_stripped_by_dedup(self):
        # A mechanic line that ALSO contains offer-ish words must be kept.
        from cqc_lem.utilities.ai.content_alignment import ensure_lead_magnet_cta
        content = "Point.\n\nComment AUDIT and I'll send you the checklist by DM."
        out = ensure_lead_magnet_cta(content, _LM, post_id=30)
        assert "Comment AUDIT" in out


class TestRewritePassPreservation:
    def _resp(self, text="rewritten"):
        r = MagicMock(); r.choices = [MagicMock()]; r.choices[0].message.content = text
        return r

    def test_refinement_prompt_includes_preserve_note_when_keyword(self):
        from cqc_lem.utilities.ai import ai_helper
        with patch.object(ai_helper, "_call_llm", return_value=self._resp()) as call:
            ai_helper.get_ai_linked_post_refinement("draft", preserve_cta_keyword="AUDIT")
        sent = str(call.call_args.kwargs["messages"])
        assert "PRESERVE" in sent and "AUDIT" in sent

    def test_refinement_prompt_unchanged_without_keyword(self):
        from cqc_lem.utilities.ai import ai_helper
        with patch.object(ai_helper, "_call_llm", return_value=self._resp()) as call:
            ai_helper.get_ai_linked_post_refinement("draft")
        assert "PRESERVE" not in str(call.call_args.kwargs["messages"])

    def test_hook_pass_carves_keyword_out_of_its_antibait_rule(self):
        from cqc_lem.utilities.ai import ai_helper
        with patch.object(ai_helper, "_call_llm", return_value=self._resp()) as call:
            ai_helper.optimize_post_hook("draft", preserve_cta_keyword="AUDIT")
        system = call.call_args.kwargs["messages"][0]["content"]
        assert "PRESERVE" in system and "AUDIT" in system

    def test_hook_pass_unchanged_without_keyword(self):
        from cqc_lem.utilities.ai import ai_helper
        with patch.object(ai_helper, "_call_llm", return_value=self._resp()) as call:
            ai_helper.optimize_post_hook("draft")
        assert "PRESERVE" not in call.call_args.kwargs["messages"][0]["content"]


class TestCreateTextPostThreadsKeyword:
    def test_rewrite_passes_receive_keyword_on_selected_post(self):
        from cqc_lem.app import run_content_plan as rcp
        _RCP = "cqc_lem.app.run_content_plan"

        def gen(*a, **kw):
            return "generated body"

        with patch(f"{_RCP}.get_engagement_preferences", return_value={}), \
             patch(f"{_RCP}.get_or_create_profile_synthesis", return_value="voice"), \
             patch(f"{_RCP}.get_lead_magnet_settings", return_value=_LM), \
             patch(f"{_RCP}.get_recent_post_texts", return_value=[]), \
             patch(f"{_RCP}.get_recent_post_shape_history", return_value=[]), \
             patch(f"{_RCP}.update_db_post_shape"), \
             patch(f"{_RCP}.get_thought_leadership_post_from_ai", side_effect=gen), \
             patch(f"{_RCP}.get_ai_linked_post_refinement",
                   side_effect=lambda c, **kw: c) as refine, \
             patch(f"{_RCP}.optimize_post_hook", side_effect=lambda c, **kw: c) as hook:
            rcp.create_text_post(1, "awareness", post_type="thought_leadership",
                                 user_profile=MagicMock(), refine_final_post=True, post_id=30)
        assert refine.call_args.kwargs["preserve_cta_keyword"] == "AUDIT"
        assert hook.call_args.kwargs["preserve_cta_keyword"] == "AUDIT"

    def test_rewrite_passes_get_none_on_unselected_post(self):
        from cqc_lem.app import run_content_plan as rcp
        _RCP = "cqc_lem.app.run_content_plan"

        def gen(*a, **kw):
            return "generated body"

        with patch(f"{_RCP}.get_engagement_preferences", return_value={}), \
             patch(f"{_RCP}.get_or_create_profile_synthesis", return_value="voice"), \
             patch(f"{_RCP}.get_lead_magnet_settings", return_value=_LM), \
             patch(f"{_RCP}.get_recent_post_texts", return_value=[]), \
             patch(f"{_RCP}.get_recent_post_shape_history", return_value=[]), \
             patch(f"{_RCP}.update_db_post_shape"), \
             patch(f"{_RCP}.get_thought_leadership_post_from_ai", side_effect=gen), \
             patch(f"{_RCP}.get_ai_linked_post_refinement",
                   side_effect=lambda c, **kw: c) as refine, \
             patch(f"{_RCP}.optimize_post_hook", side_effect=lambda c, **kw: c) as hook:
            rcp.create_text_post(1, "awareness", post_type="thought_leadership",
                                 user_profile=MagicMock(), refine_final_post=True, post_id=31)
        assert refine.call_args.kwargs["preserve_cta_keyword"] is None
        assert hook.call_args.kwargs["preserve_cta_keyword"] is None
