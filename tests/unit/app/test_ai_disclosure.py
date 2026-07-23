"""Unit tests for the A4 per-user AI-assistance disclosure (issue #385): the idempotent append
helper and its wiring into create_text_post (opt-in, appended LAST after refinement)."""

import pytest
from unittest.mock import MagicMock, patch

pytestmark = pytest.mark.unit

_RCP = "cqc_lem.app.run_content_plan"
_DISABLED_LM = {"enabled": False, "keyword": None, "message": None}
_POST = "AI reliability starts with observability.\n\nInstrument before you scale."


class TestApplyAiAssistDisclosure:
    def test_noop_when_disabled(self):
        from cqc_lem.app.run_content_plan import _apply_ai_assist_disclosure
        assert _apply_ai_assist_disclosure(_POST, {"ai_disclosure_enabled": False}) == _POST

    def test_noop_when_prefs_missing(self):
        from cqc_lem.app.run_content_plan import _apply_ai_assist_disclosure
        assert _apply_ai_assist_disclosure(_POST, None) == _POST
        assert _apply_ai_assist_disclosure(_POST, {}) == _POST

    def test_noop_when_content_empty(self):
        from cqc_lem.app.run_content_plan import _apply_ai_assist_disclosure
        assert _apply_ai_assist_disclosure("", {"ai_disclosure_enabled": True}) == ""

    def test_appends_default_when_enabled_and_no_text(self):
        from cqc_lem.app.run_content_plan import (_apply_ai_assist_disclosure,
                                                  DEFAULT_AI_ASSIST_DISCLOSURE)
        out = _apply_ai_assist_disclosure(_POST, {"ai_disclosure_enabled": True})
        assert out == _POST + "\n\n" + DEFAULT_AI_ASSIST_DISCLOSURE
        assert out.endswith(DEFAULT_AI_ASSIST_DISCLOSURE)

    def test_appends_custom_text(self):
        from cqc_lem.app.run_content_plan import _apply_ai_assist_disclosure
        out = _apply_ai_assist_disclosure(
            _POST, {"ai_disclosure_enabled": True, "ai_disclosure_text": "Written with AI help."})
        assert out.endswith("\n\nWritten with AI help.")

    def test_idempotent_when_already_present(self):
        from cqc_lem.app.run_content_plan import _apply_ai_assist_disclosure
        prefs = {"ai_disclosure_enabled": True, "ai_disclosure_text": "AI-assisted."}
        once = _apply_ai_assist_disclosure(_POST, prefs)
        twice = _apply_ai_assist_disclosure(once, prefs)
        assert once == twice
        assert twice.count("AI-assisted.") == 1


def _run(prefs, post="generated post"):
    """Drive create_text_post through the full refine+review path with the given prefs."""
    from cqc_lem.app import run_content_plan as rcp
    patches = [
        patch(f"{_RCP}.get_engagement_preferences", return_value=prefs),
        patch(f"{_RCP}.get_or_create_profile_synthesis", return_value="voice"),
        patch(f"{_RCP}.get_lead_magnet_settings", return_value=_DISABLED_LM),
        patch(f"{_RCP}.get_recent_post_texts", return_value=[]),
        patch(f"{_RCP}.get_recent_post_shape_history", return_value=[]),
        patch(f"{_RCP}.update_db_post_shape"),
        patch(f"{_RCP}.get_thought_leadership_post_from_ai", return_value=post),
        patch(f"{_RCP}.get_ai_linked_post_refinement", side_effect=lambda c, **kw: c),
        patch(f"{_RCP}.optimize_post_hook", side_effect=lambda c, **kw: c),
        patch(f"{_RCP}.sanitize_for_linkedin", side_effect=lambda c, **kw: c),
        patch(f"{_RCP}.strip_engagement_bait", side_effect=lambda c, **kw: c),
    ]
    for p in patches:
        p.start()
    try:
        return rcp.create_text_post(1, "awareness", post_type="thought_leadership",
                                    user_profile=MagicMock(), post_id=77)
    finally:
        for p in patches:
            p.stop()


class TestCreateTextPostDisclosureWiring:
    def test_disclosure_appended_when_enabled(self):
        from cqc_lem.app.run_content_plan import DEFAULT_AI_ASSIST_DISCLOSURE
        out = _run({"ai_disclosure_enabled": True})
        assert out.endswith(DEFAULT_AI_ASSIST_DISCLOSURE)

    def test_custom_disclosure_appended(self):
        out = _run({"ai_disclosure_enabled": True, "ai_disclosure_text": "Made with AI."})
        assert out.endswith("\n\nMade with AI.")

    def test_no_disclosure_when_disabled(self):
        from cqc_lem.app.run_content_plan import DEFAULT_AI_ASSIST_DISCLOSURE
        out = _run({"ai_disclosure_enabled": False})
        assert DEFAULT_AI_ASSIST_DISCLOSURE not in out
        assert out == "generated post"
