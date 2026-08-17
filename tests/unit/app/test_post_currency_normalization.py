"""Issue #1529 — the currency safety net has to be the LAST word on a generated post.

`_refine_draft` runs `sanitize_for_linkedin` (and so `normalize_currency_symbols`) BEFORE the
humanization rewrite and before the review gate's regeneration. Both of those are LLM passes, and
`linkedin_formatter`'s whole posture is that a model ignoring the directive is the normal case — so
a net that stops one pass short of the end is not a net. The humanizer already re-runs
`normalize_public_text` on its own output for exactly that reason.

Two other generated post texts never reach `sanitize_for_linkedin` at all: a carousel CAPTION (JSON
mode, so it gets `normalize_public_text` + `enforce_post_readability` only) and the weekly group post
draft (the user owns ready/skipped on it, never its text).
"""

from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit

_RCP = "cqc_lem.app.run_content_plan"
_FEED = "cqc_lem.app.engagement.feed"

_DISABLED_LM = {"enabled": False, "keyword": None, "message": None}
_NEUTRAL_BLUEPRINT = {"subject": None, "angle": "", "format": "personal_lesson",
                      "structure": [], "hook_style": "micro_story", "cta_style": "reply_question"}

_CLEAN = ("Telephony markups are the quietest line on the bill.\n\nWe audited one client's carrier "
          "invoice and found the per-seat markup buried under three bundles. Read the invoice, not "
          "the quote.")
# What the humanizer hands back: the same draft, repriced in rupees by the LAST LLM pass.
_REPRICED = _CLEAN.replace("markup buried", "markup of ₹1,200 buried")


def _create_text_post(humanized):
    """Drive `create_text_post` with the humanization pass returning `humanized`."""
    from cqc_lem.app import run_content_plan as rcp

    patches = [
        patch(f"{_RCP}.get_engagement_preferences", return_value={}),
        patch(f"{_RCP}.get_or_create_profile_synthesis", return_value="voice"),
        patch(f"{_RCP}.get_lead_magnet_settings", return_value=dict(_DISABLED_LM)),
        patch(f"{_RCP}.get_recent_post_texts", return_value=[]),
        patch(f"{_RCP}.get_recent_post_shape_history", return_value=[]),
        patch(f"{_RCP}._select_post_blueprint", return_value=dict(_NEUTRAL_BLUEPRINT)),
        patch(f"{_RCP}.update_db_post_shape"),
        patch(f"{_RCP}.get_thought_leadership_post_from_ai", MagicMock(return_value=_CLEAN)),
        # Identity refinement passes, so the only text change under test is the humanizer's.
        patch(f"{_RCP}.get_ai_linked_post_refinement", side_effect=lambda c, **kw: c),
        patch(f"{_RCP}.optimize_post_hook", side_effect=lambda c, **kw: c),
        patch(f"{_RCP}.strip_engagement_bait", side_effect=lambda c, **kw: c),
        patch(f"{_RCP}.humanize_text", side_effect=lambda c, **kw: humanized),
        patch(f"{_RCP}.authenticity_gate_enabled", return_value=False),
    ]
    for p in patches:
        p.start()
    try:
        return rcp.create_text_post(1, "awareness", post_type="thought_leadership",
                                    user_profile=MagicMock(), post_id=77)
    finally:
        for p in patches:
            p.stop()


class TestTheTextPostPipelineHasTheLastWord:
    def test_a_rupee_the_humanizer_introduced_is_still_repriced(self):
        out = _create_text_post(_REPRICED)
        assert "₹" not in out
        assert "$1,200" in out

    def test_a_clean_draft_is_returned_verbatim(self):
        assert _create_text_post(_CLEAN) == _CLEAN


def _create_carousel(caption):
    """Drive `create_carousel_content` with every seam mocked; returns (result, stored_caption)."""
    from cqc_lem.app import run_content_plan as rcp

    deck = {"cover": {"title": "Deploy time", "content": "We cut it from 41 minutes to 9."},
            "insights": [{"title": "Build cache", "content": "One shared cache saved 14 minutes."}]}
    with patch(f"{_RCP}.get_engagement_preferences", return_value={}), \
         patch(f"{_RCP}.get_or_create_profile_synthesis", return_value="brief"), \
         patch(f"{_RCP}.get_recent_post_shape_history", return_value=[]), \
         patch(f"{_RCP}.get_shape_performance", return_value=None), \
         patch(f"{_RCP}.get_story_bank_entries", return_value=[]), \
         patch(f"{_RCP}.record_story_bank_use"), \
         patch(f"{_RCP}.update_db_post_shape"), \
         patch(f"{_RCP}.update_db_post_status"), \
         patch(f"{_RCP}._report_carousel_fact_grounding"), \
         patch(f"{_RCP}._report_carousel_slide_slop"), \
         patch(f"{_RCP}.load_profile_for_user", return_value=MagicMock()), \
         patch(f"{_RCP}._score_and_persist_authenticity") as judge, \
         patch("cqc_lem.utilities.ai.ai_helper.generate_carousel_content",
               return_value=(caption, deck)):
        result = rcp.create_carousel_content(1, "awareness", 7)
    return result, judge.call_args[0][2]


class TestTheCarouselCaptionIsNormalizedToo:
    def test_a_deck_caption_priced_in_rupees_is_repriced(self):
        result, _ = _create_carousel("Markups run ₹1,200 per seat.")
        assert result == "Markups run $1,200 per seat."

    def test_the_judge_reads_the_caption_that_ships(self):
        # The authenticity score has to describe the shipped text, not the pre-normalized one.
        _, scored = _create_carousel("Markups run ₹1,200 per seat.")
        assert scored == "Markups run $1,200 per seat."

    def test_a_deliberate_currency_survives_the_deck(self):
        result, _ = _create_carousel("We closed €2M in European ARR.")
        assert result == "We closed €2M in European ARR."


def _draft_group_post(generated):
    """Drive `auto_draft_group_post`; returns the text handed to `create_group_post_draft`."""
    from cqc_lem.app.engagement import feed

    with patch(f"{_FEED}.get_open_group_post_draft", return_value=None), \
         patch(f"{_FEED}.load_profile_for_user", return_value=MagicMock()), \
         patch(f"{_FEED}.get_engagement_preferences", return_value={}), \
         patch(f"{_FEED}.get_or_create_profile_synthesis", return_value="voice"), \
         patch(f"{_FEED}.generate_group_post", return_value=generated), \
         patch(f"{_FEED}.create_group_post_draft", return_value=5) as store:
        feed.auto_draft_group_post.run(user_id=1, group_id="g1", group_name="Ops Leaders")
    return store.call_args[0][2]


class TestTheGroupPostDraftIsNormalizedToo:
    def test_a_group_draft_priced_in_rupees_is_repriced(self):
        assert _draft_group_post("Markups run ₹1,200 per seat.") == \
            "Markups run $1,200 per seat."

    def test_a_deliberate_currency_survives_the_group_draft(self):
        text = "We closed €2M in European ARR."
        assert _draft_group_post(text) == text
