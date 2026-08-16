"""Unit tests for lead-magnet CTA survival through the post refinement pipeline: the prompt-level
directive weaves the CTA in, but the downstream LLM rewrites (refinement + hook passes) were
observed in prod rewording the 'comment KEYWORD' mechanic away (post 30) or dropping it (post 27),
so the auto-DM keyword listener never fired. create_text_post now runs a deterministic
verify-and-repair (ensure_lead_magnet_cta) after the pipeline.
"""

from unittest.mock import MagicMock, patch

import pytest

from cqc_lem.utilities.ai.content_alignment import has_lead_magnet_cta_mechanic

pytestmark = pytest.mark.unit

_RCP = "cqc_lem.app.run_content_plan"

_LM_ON = {"enabled": True, "keyword": "AUDIT", "message": "A free 12-point LinkedIn profile audit PDF."}
_LM_OFF = {"enabled": False, "keyword": None, "message": None}

_GENERATED = ("Six months of testing taught us where acquisition cost hides.\n\n"
              "Comment AUDIT and I'll DM you the 12-point checklist we used.")

# Prod post-30 failure mode: the refinement rewords the mechanic into a generic ask.
_REWORDED = ("Six months of testing taught us where acquisition cost hides.\n\n"
             "Reach out for my AI Ops Readiness Checklist.")

# Prod post-27 failure mode: the CTA disappears entirely.
_DROPPED = "Six months of testing taught us where acquisition cost hides."


def _run_pipeline(post_id, refined_output, lead_magnet=_LM_ON, prefs=None):
    """Drive create_text_post with the FULL refinement pipeline on, simulating the LLM passes via
    `refined_output` (what the rewrites turn the generated post into).
    """
    from cqc_lem.app import run_content_plan as rcp

    # **kwargs: the generators keep growing aligned kwargs (history_directive, sequence keys, …) —
    # this stub only cares that content comes back.
    def gen(user_profile, stage, prefs=None, profile_synthesis=None, blueprint=None,
            lead_magnet_cta=None, **kwargs):
        return _GENERATED

    with patch(f"{_RCP}.get_engagement_preferences", return_value=prefs or {}), \
         patch(f"{_RCP}.get_or_create_profile_synthesis", return_value="voice"), \
         patch(f"{_RCP}.get_lead_magnet_settings", return_value=lead_magnet), \
         patch(f"{_RCP}.get_recent_post_texts", return_value=[]), \
         patch(f"{_RCP}.get_recent_post_shape_history", return_value=[]), \
         patch(f"{_RCP}.update_db_post_shape"), \
         patch(f"{_RCP}.get_thought_leadership_post_from_ai", side_effect=gen), \
         patch(f"{_RCP}.get_ai_linked_post_refinement", return_value=refined_output), \
         patch(f"{_RCP}._score_and_persist_authenticity"), \
         patch(f"{_RCP}.optimize_post_hook", side_effect=lambda t, **kw: t):
        return rcp.create_text_post(1, "awareness", post_type="thought_leadership",
                                    user_profile=MagicMock(), refine_final_post=True,
                                    post_id=post_id)


class TestCreateTextPostCtaSurvival:
    def test_reworded_cta_is_repaired(self):
        # post_id=30 is on the 1-in-3 rotation; the rewrite lost the comment-keyword mechanic.
        out = _run_pipeline(post_id=30, refined_output=_REWORDED)
        assert has_lead_magnet_cta_mechanic(out, "AUDIT")
        assert "AUDIT" in out

    def test_dropped_cta_is_repaired(self):
        out = _run_pipeline(post_id=27, refined_output=_DROPPED)
        assert has_lead_magnet_cta_mechanic(out, "AUDIT")
        assert out.startswith(_DROPPED)

    def test_surviving_cta_is_left_untouched(self):
        # Refinement kept a working mechanic — no repair line, content byte-identical to pipeline out.
        out = _run_pipeline(post_id=30, refined_output=_GENERATED)
        assert out == _GENERATED
        assert out.count("AUDIT") == 1

    def test_unselected_post_is_not_repaired(self):
        # post_id=28 (28 % 3 != 0) never gets the CTA, even though the lead magnet is on.
        out = _run_pipeline(post_id=28, refined_output=_DROPPED)
        assert out == _DROPPED
        assert "AUDIT" not in out

    def test_disabled_lead_magnet_is_never_repaired(self):
        out = _run_pipeline(post_id=30, refined_output=_DROPPED, lead_magnet=_LM_OFF)
        assert out == _DROPPED

    def test_repair_honors_use_emojis_preference(self):
        plain = _run_pipeline(post_id=30, refined_output=_DROPPED, prefs={"use_emojis": False})
        assert plain.isascii()
        with_emoji = _run_pipeline(post_id=30, refined_output=_DROPPED, prefs={"use_emojis": True})
        assert not with_emoji.isascii()
        assert has_lead_magnet_cta_mechanic(with_emoji, "AUDIT")

    def test_repair_is_deterministic_per_post(self):
        assert (_run_pipeline(post_id=30, refined_output=_DROPPED)
                == _run_pipeline(post_id=30, refined_output=_DROPPED))

    def test_colliding_keyword_cta_survives_bait_filter(self):
        # A trigger word colliding with the bait regex ('YES') is exempted via the threaded
        # keyword — the surviving CTA line stays byte-identical instead of strip-then-repair.
        lm = {"enabled": True, "keyword": "YES", "message": "A checklist."}
        kept = ("Six months of testing taught us where acquisition cost hides.\n\n"
                "Comment YES and I'll DM you the checklist we used.")
        out = _run_pipeline(post_id=30, refined_output=kept, lead_magnet=lm)
        assert out == kept

    def test_different_posts_get_different_repair_phrasings(self):
        outs = {_run_pipeline(post_id=pid, refined_output=_DROPPED)[len(_DROPPED):]
                for pid in (27, 30, 33, 36, 39, 42)}
        assert len(outs) == 6  # 6-variant menu keyed by post_id % 6


class TestReviewGateRetryCtaSurvival:
    def test_the_regeneration_is_composed_with_the_keyword_protected(self):
        """Issue #1217: the retry re-enters the compose core carrying the trigger word.

        The old recursive retry passed neither `preserve_cta_keyword` nor the bait exemption, so a
        regenerated post's mechanic could only be put back by the deterministic repair afterwards —
        and a trigger word colliding with the bait regex was stripped out of the retry before the
        repair ever saw it.
        """
        from cqc_lem.app import run_content_plan as rcp
        from cqc_lem.domain.models import PostDraftContext
        ctx = PostDraftContext(user_id=1, stage="awareness", post_type="thought_leadership",
                               user_profile=MagicMock(), prefs={}, profile_synthesis="",
                               blueprint={}, post_id=30, lead_magnet_cta="comment AUDIT")
        verdicts = [{"score": 0.9, "threshold": 0.5, "match": "an earlier post",
                     "too_similar": True, "measure": "embedding"},
                    {"score": 0.1, "threshold": 0.5, "match": None,
                     "too_similar": False, "measure": "embedding"}]
        with patch(f"{_RCP}.post_similarity_report", side_effect=verdicts), \
             patch(f"{_RCP}._record_post_similarity_finding"), \
             patch(f"{_RCP}._check_post_alignment", return_value=True), \
             patch(f"{_RCP}._compose_draft", return_value=(_GENERATED, ctx)) as compose:
            out = rcp._review_generated_post(ctx, _REWORDED, ["an earlier post"], story=None,
                                             cta_keyword="AUDIT")
        assert out == _GENERATED
        assert compose.call_args.kwargs == {"cta_keyword": "AUDIT", "bait_exempt_keyword": "AUDIT"}


class TestRegeneratePostCtaSurvival:
    def test_guidance_rewrite_repaired(self):
        # The guidance pass is one more LLM rewrite AFTER create_text_post's verify-and-repair —
        # it must be re-verified or the repaired CTA can be lost again.
        from cqc_lem.app import run_content_plan as rcp
        with patch("cqc_lem.utilities.db.get_post_user_id", return_value=1), \
             patch("cqc_lem.utilities.db.get_post_buyer_stage", return_value="awareness"), \
             patch("cqc_lem.utilities.db.get_post_type", return_value="text"), \
             patch(f"{_RCP}.load_profile_for_user", return_value=MagicMock()), \
             patch(f"{_RCP}.create_text_post", return_value=_GENERATED), \
             patch(f"{_RCP}.get_engagement_preferences", return_value={}), \
             patch(f"{_RCP}.get_or_create_profile_synthesis", return_value="voice"), \
             patch(f"{_RCP}.apply_post_guidance", return_value=_DROPPED), \
             patch(f"{_RCP}.get_lead_magnet_settings", return_value=_LM_ON), \
             patch(f"{_RCP}.update_db_post_content") as save, \
             patch(f"{_RCP}.update_db_post_status"):
            out = rcp.regenerate_post(30, guidance="make it shorter")
        assert has_lead_magnet_cta_mechanic(out, "AUDIT")
        save.assert_called_once_with(30, out)

    def test_guidance_rewrite_keeping_cta_untouched(self):
        from cqc_lem.app import run_content_plan as rcp
        with patch("cqc_lem.utilities.db.get_post_user_id", return_value=1), \
             patch("cqc_lem.utilities.db.get_post_buyer_stage", return_value="awareness"), \
             patch("cqc_lem.utilities.db.get_post_type", return_value="text"), \
             patch(f"{_RCP}.load_profile_for_user", return_value=MagicMock()), \
             patch(f"{_RCP}.create_text_post", return_value=_GENERATED), \
             patch(f"{_RCP}.get_engagement_preferences", return_value={}), \
             patch(f"{_RCP}.get_or_create_profile_synthesis", return_value="voice"), \
             patch(f"{_RCP}.apply_post_guidance", return_value=_GENERATED), \
             patch(f"{_RCP}.get_lead_magnet_settings", return_value=_LM_ON), \
             patch(f"{_RCP}.update_db_post_content"), \
             patch(f"{_RCP}.update_db_post_status"):
            out = rcp.regenerate_post(30, guidance="make it shorter")
        assert out == _GENERATED
        assert out.count("AUDIT") == 1
