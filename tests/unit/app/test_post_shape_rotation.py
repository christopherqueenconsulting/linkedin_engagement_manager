"""Unit tests for post SHAPE rotation in create_text_post: one blueprint per post, rotated away
from the user's recent post shapes (V51 history) via the shared framework core, persisted so the
NEXT post rotates too."""

import pytest
from unittest.mock import MagicMock, patch

pytestmark = pytest.mark.unit

_RCP = "cqc_lem.app.run_content_plan"


def _run(post_id=77, blueprint=None, history=None):
    from cqc_lem.app import run_content_plan as rcp
    captured = {}

    def gen(user_profile, stage, prefs=None, profile_synthesis=None, blueprint=None):
        captured["blueprint"] = blueprint
        return "generated post"

    with patch(f"{_RCP}.get_engagement_preferences", return_value={}), \
         patch(f"{_RCP}.get_or_create_profile_synthesis", return_value="voice"), \
         patch(f"{_RCP}.get_recent_post_shape_history", return_value=history or []) as hist, \
         patch(f"{_RCP}.update_db_post_shape") as save, \
         patch(f"{_RCP}.get_thought_leadership_post_from_ai", side_effect=gen):
        out = rcp.create_text_post(1, "awareness", post_type="thought_leadership",
                                   user_profile=MagicMock(), refine_final_post=False,
                                   blueprint=blueprint, post_id=post_id)
    return out, captured, hist, save


class TestPostShapeRotation:
    def test_selects_rotated_shape_and_persists(self):
        history = [{"archetype": "personal_lesson", "hook_style": "question"},
                   {"archetype": "contrarian_take", "hook_style": "surprising_stat"}]
        out, captured, hist, save = _run(history=history)
        assert out == "generated post"
        bp = captured["blueprint"]
        assert bp["format"] not in ("personal_lesson", "contrarian_take")
        assert bp["hook_style"] not in ("question", "surprising_stat")
        hist.assert_called_once()
        save.assert_called_once_with(77, bp["format"], bp["hook_style"])

    def test_no_persistence_without_post_id(self):
        out, captured, _, save = _run(post_id=None)
        assert out == "generated post"
        assert captured["blueprint"] is not None  # shape still assigned for the prompt
        save.assert_not_called()

    def test_explicit_blueprint_skips_history_lookup(self):
        bp_in = {"format": "tactical_list", "hook_style": "bold_claim", "cta_style": "save_worthy"}
        out, captured, hist, save = _run(blueprint=bp_in)
        assert captured["blueprint"] is bp_in
        hist.assert_not_called()
        save.assert_called_once_with(77, "tactical_list", "bold_claim")

    def test_shape_history_failure_never_blocks_generation(self):
        from cqc_lem.app import run_content_plan as rcp
        with patch(f"{_RCP}.get_engagement_preferences", return_value={}), \
             patch(f"{_RCP}.get_or_create_profile_synthesis", return_value="voice"), \
             patch(f"{_RCP}.get_recent_post_shape_history", side_effect=RuntimeError("db down")), \
             patch(f"{_RCP}.update_db_post_shape"), \
             patch(f"{_RCP}.get_thought_leadership_post_from_ai", return_value="post"):
            out = rcp.create_text_post(1, "awareness", post_type="thought_leadership",
                                       user_profile=MagicMock(), refine_final_post=False)
        assert out == "post"
