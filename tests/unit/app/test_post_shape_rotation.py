"""Unit tests for post SHAPE rotation in create_text_post: one blueprint per post, rotated away
from the user's recent post shapes (V51 history) via the shared framework core, persisted so the
NEXT post rotates too."""

import pytest
from unittest.mock import MagicMock, patch

pytestmark = pytest.mark.unit

_RCP = "cqc_lem.app.run_content_plan"


_DISABLED_LM = {"enabled": False, "keyword": None, "message": None}


def _run(post_id=77, blueprint=None, history=None, lead_magnet=None, stories=None):
    from cqc_lem.app import run_content_plan as rcp
    captured = {}

    def gen(user_profile, stage, prefs=None, profile_synthesis=None, blueprint=None,
            lead_magnet_cta=None, post_id=None, history_directive=None, story_directive=None,
            content_mix=None):
        captured["blueprint"] = blueprint
        captured["lead_magnet_cta"] = lead_magnet_cta
        captured["post_id"] = post_id
        captured["history_directive"] = history_directive
        captured["story_directive"] = story_directive
        return "generated post"

    with patch(f"{_RCP}.get_engagement_preferences", return_value={}), \
         patch(f"{_RCP}.get_or_create_profile_synthesis", return_value="voice"), \
         patch(f"{_RCP}.get_lead_magnet_settings", return_value=lead_magnet or _DISABLED_LM), \
         patch(f"{_RCP}.get_recent_post_texts", return_value=[]), \
         patch(f"{_RCP}.get_recent_post_shape_history", return_value=history or []) as hist, \
         patch(f"{_RCP}.get_story_bank_entries", return_value=stories or []), \
         patch(f"{_RCP}.record_story_bank_use") as story_use, \
         patch(f"{_RCP}.update_db_post_shape") as save, \
         patch(f"{_RCP}.get_thought_leadership_post_from_ai", side_effect=gen):
        out = rcp.create_text_post(1, "awareness", post_type="thought_leadership",
                                   user_profile=MagicMock(), refine_final_post=False,
                                   blueprint=blueprint, post_id=post_id)
    return out, captured, hist, save, story_use


class TestPostShapeRotation:
    def test_selects_rotated_shape_and_persists(self):
        history = [{"archetype": "personal_lesson", "hook_style": "question"},
                   {"archetype": "contrarian_take", "hook_style": "surprising_stat"}]
        out, captured, hist, save, _ = _run(history=history)
        assert out == "generated post"
        bp = captured["blueprint"]
        assert bp["format"] not in ("personal_lesson", "contrarian_take")
        assert bp["hook_style"] not in ("question", "surprising_stat")
        hist.assert_called_once()
        save.assert_called_once_with(77, bp["format"], bp["hook_style"], topic=bp.get("subject"))

    def test_no_persistence_without_post_id(self):
        out, captured, _, save, _ = _run(post_id=None)
        assert out == "generated post"
        assert captured["blueprint"] is not None  # shape still assigned for the prompt
        save.assert_not_called()

    def test_explicit_blueprint_skips_history_lookup(self):
        bp_in = {"format": "tactical_list", "hook_style": "bold_claim", "cta_style": "save_worthy"}
        out, captured, hist, save, _ = _run(blueprint=bp_in)
        # The assigned shape rides through untouched; the writer's copy additionally carries the
        # post's verified-fact allow-list (#619), and the caller's dict is left alone.
        assert captured["blueprint"].items() >= bp_in.items()
        assert "fact_anchors" not in bp_in
        hist.assert_not_called()
        save.assert_called_once_with(77, "tactical_list", "bold_claim", topic=None)

    def test_lead_magnet_cta_woven_when_enabled_and_selected(self):
        lm = {"enabled": True, "keyword": "AUDIT", "message": "Free profile audit checklist."}
        # post_id=3 is a multiple of the default 1-in-3 cadence → selected.
        _, captured, _, _, _ = _run(post_id=3, lead_magnet=lm)
        cta = captured["lead_magnet_cta"]
        assert cta and "AUDIT" in cta and "SANCTIONED" in cta

    def test_lead_magnet_cta_absent_when_not_selected(self):
        lm = {"enabled": True, "keyword": "AUDIT", "message": "Free profile audit checklist."}
        # post_id=77 (77 % 3 != 0) is NOT selected → no CTA on this post.
        _, captured, _, _, _ = _run(post_id=77, lead_magnet=lm)
        assert captured["lead_magnet_cta"] == ""

    def test_lead_magnet_cta_absent_when_disabled(self):
        lm = {"enabled": False, "keyword": "AUDIT", "message": "Free profile audit checklist."}
        _, captured, _, _, _ = _run(post_id=3, lead_magnet=lm)
        assert captured["lead_magnet_cta"] == ""

    def test_shape_history_failure_never_blocks_generation(self):
        from cqc_lem.app import run_content_plan as rcp
        with patch(f"{_RCP}.get_engagement_preferences", return_value={}), \
             patch(f"{_RCP}.get_or_create_profile_synthesis", return_value="voice"), \
             patch(f"{_RCP}.get_lead_magnet_settings", return_value={"enabled": False, "keyword": None, "message": None}), \
             patch(f"{_RCP}.get_recent_post_texts", side_effect=RuntimeError("db down")), \
             patch(f"{_RCP}.get_recent_post_shape_history", side_effect=RuntimeError("db down")), \
             patch(f"{_RCP}.update_db_post_shape"), \
             patch(f"{_RCP}.get_thought_leadership_post_from_ai", return_value="post"):
            out = rcp.create_text_post(1, "awareness", post_type="thought_leadership",
                                       user_profile=MagicMock(), refine_final_post=False)
        assert out == "post"
