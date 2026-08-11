"""Unit tests for issue #1264 — the authenticity score must describe the draft that ships.

Not the one the review gate threw away.

`_review_generated_post` regenerates the post once on a similarity / A2-proof / fabrication / slop
failure, and its retry re-enters `create_text_post` with `similarity_check=False` — the flag that
also guards the authenticity scoring call. Scoring therefore has to run AFTER the review gate, and
still exactly once per shipped post.
"""

from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit

_RCP = "cqc_lem.app.run_content_plan"

_DISABLED_LM = {"enabled": False, "keyword": None, "message": None}

_RECENT = ("Why routing LLM calls by complexity cuts your cost per successful call.\n\n"
           "Most teams overpay for AI because every request hits the frontier model, even the "
           "trivial ones. Route the simple calls to cheap models and reserve the expensive ones "
           "for genuinely hard prompts.")

# Rewording of _RECENT — trips the similarity gate, so the review gate regenerates.
_NEAR_DUP = ("Most teams overpay for AI: every request hits the frontier model, even trivial "
             "ones. Routing LLM calls by complexity cuts the cost per successful call — cheap "
             "models for simple calls, expensive models reserved for the genuinely hard prompts.")

_FRESH = ("AI reliability starts with observability.\n\nWe traced a silent failure spike in "
          "production to one retry loop nobody owned. Dashboards did not catch it; distributed "
          "tracing did. Instrument before you scale.")

# No fact-anchor contract on this archetype, so only the gates under test can fire.
_NEUTRAL_BLUEPRINT = {"subject": None, "angle": "", "format": "personal_lesson",
                      "structure": [], "hook_style": "micro_story", "cta_style": "reply_question"}


def _run(outputs, recent=None, post_id=77):
    """Drive create_text_post with a generator returning `outputs` in order.

    The authenticity judge and its DB write are captured rather than mocked away.

    Returns (content, generator_mock, score_authenticity_mock, update_score_mock).
    """
    from cqc_lem.app import run_content_plan as rcp
    gen = MagicMock(side_effect=list(outputs))
    scorer = MagicMock(return_value={"score": 88, "reasons": [], "flagged": False})
    upd = MagicMock()
    patches = [
        patch(f"{_RCP}.get_engagement_preferences", return_value={}),
        patch(f"{_RCP}.get_or_create_profile_synthesis", return_value="voice"),
        patch(f"{_RCP}.get_lead_magnet_settings", return_value=_DISABLED_LM),
        patch(f"{_RCP}.get_recent_post_texts", return_value=list(recent or [])),
        patch(f"{_RCP}.get_recent_post_shape_history", return_value=[]),
        patch(f"{_RCP}._select_post_blueprint", return_value=dict(_NEUTRAL_BLUEPRINT)),
        patch(f"{_RCP}.update_db_post_shape"),
        patch(f"{_RCP}.get_thought_leadership_post_from_ai", gen),
        # Identity refinement passes so the gates see the generator output verbatim.
        patch(f"{_RCP}.get_ai_linked_post_refinement", side_effect=lambda c, **kw: c),
        patch(f"{_RCP}.optimize_post_hook", side_effect=lambda c, **kw: c),
        patch(f"{_RCP}.sanitize_for_linkedin", side_effect=lambda c, **kw: c),
        patch(f"{_RCP}.strip_engagement_bait", side_effect=lambda c, **kw: c),
        patch(f"{_RCP}.humanize_text", side_effect=lambda c, **kw: c),
        patch(f"{_RCP}.authenticity_gate_enabled", return_value=True),
        patch(f"{_RCP}.score_authenticity", scorer),
        patch(f"{_RCP}.update_db_post_authenticity_score", upd),
    ]
    for p in patches:
        p.start()
    try:
        out = rcp.create_text_post(1, "awareness", post_type="thought_leadership",
                                   user_profile=MagicMock(), post_id=post_id)
    finally:
        for p in patches:
            p.stop()
    return out, gen, scorer, upd


class TestAuthenticityScoresTheShippedDraft:
    def test_regenerated_post_is_the_one_scored(self):
        """The review gate regenerated: the judge must see the SECOND draft, not the discarded one."""
        out, gen, scorer, upd = _run([_NEAR_DUP, _FRESH], recent=[_RECENT])
        assert out == _FRESH
        assert gen.call_count == 2  # the gate did regenerate
        scorer.assert_called_once()
        assert scorer.call_args.args[0] == _FRESH
        assert _NEAR_DUP not in [c.args[0] for c in scorer.call_args_list]
        upd.assert_called_once_with(77, 88)

    def test_kept_first_draft_is_the_one_scored(self):
        """The retry failed and the gate kept the first draft — that draft is what gets scored."""
        out, gen, scorer, upd = _run([_NEAR_DUP, RuntimeError("llm down")], recent=[_RECENT])
        assert out == _NEAR_DUP
        scorer.assert_called_once()
        assert scorer.call_args.args[0] == _NEAR_DUP
        upd.assert_called_once_with(77, 88)

    def test_no_regeneration_scores_exactly_once(self):
        """Common path: the gate did not regenerate, so there is no extra judge spend."""
        out, gen, scorer, upd = _run([_FRESH], recent=[_RECENT])
        assert out == _FRESH
        assert gen.call_count == 1
        scorer.assert_called_once()
        assert scorer.call_args.args[0] == _FRESH
        upd.assert_called_once_with(77, 88)

    def test_regeneration_still_scores_exactly_once(self):
        """The retry re-enters create_text_post — it must NOT bring a second judge call with it."""
        _, gen, scorer, _ = _run([_NEAR_DUP, _FRESH], recent=[_RECENT])
        assert gen.call_count == 2
        assert scorer.call_count == 1
