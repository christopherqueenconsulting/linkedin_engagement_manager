"""Unit tests for the unified DM/lead-magnet placeholder engine and post regeneration."""

from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit

_OUT = "cqc_lem.app.engagement.outreach"  # lgtm[py/unused-global-variable]
_RCP = "cqc_lem.app.run_content_plan"  # lgtm[py/unused-global-variable]


class TestRenderDmPlaceholders:
    def test_fills_all_known_tokens(self):
        from cqc_lem.utilities.dm_templates import render_dm_placeholders
        out = render_dm_placeholders("Hi {first_name}, re {headline} — see {blog_url}",
                                     first_name="Jane", headline="AI Ops", blog_url="https://x.co/b")
        assert out == "Hi Jane, re AI Ops — see https://x.co/b"

    def test_first_name_only_leaves_link_literal(self):
        # Lead magnet supports {first_name} + a literal link in the message body.
        from cqc_lem.utilities.dm_templates import render_dm_placeholders
        out = render_dm_placeholders("Here you go, {first_name} — https://x.co/audit", first_name="Chris")
        assert out == "Here you go, Chris — https://x.co/audit"

    def test_empty_first_name_defaults_to_there(self):
        from cqc_lem.utilities.dm_templates import render_dm_placeholders
        assert render_dm_placeholders("Hi {first_name}", first_name="") == "Hi there"

    def test_unknown_token_left_literal_not_crash(self):
        from cqc_lem.utilities.dm_templates import render_dm_placeholders
        assert render_dm_placeholders("Hi {first_name} {frst_nme}", first_name="Jane") == "Hi Jane {frst_nme}"

    def test_malformed_brace_degrades_to_known_replacement(self):
        from cqc_lem.utilities.dm_templates import render_dm_placeholders
        # A stray "{" would make str.format_map raise — we fall back to replacing known tokens.
        out = render_dm_placeholders("50% off { for {first_name}", first_name="Jane")
        assert "Jane" in out

    def test_empty_and_none(self):
        from cqc_lem.utilities.dm_templates import render_dm_placeholders
        assert render_dm_placeholders("") == ""
        assert render_dm_placeholders(None) == ""


class TestBuildDmRoutesThroughEngine:
    def test_template_still_fills_first_name(self):
        from cqc_lem.app.engagement.outreach import build_dm_from_template
        prof = MagicMock(); prof.job_title = "AI Engineer"
        with patch(f"{_OUT}.get_dm_template",
                   return_value={"template_text": "Hi {first_name}, re {headline}", "step": 0}), \
             patch(f"{_OUT}.get_ai_message_refinement", side_effect=lambda m, character_limit=300: m):
            msg = build_dm_from_template(1, "connection_accepted", "Jane", prof)
        assert msg == "Hi Jane, re AI Engineer"


class TestApplyPostGuidance:
    def test_empty_guidance_returns_input(self):
        from cqc_lem.utilities.ai.ai_helper import apply_post_guidance
        assert apply_post_guidance("original post", "   ") == "original post"

    def test_applies_guidance_via_llm(self):
        from cqc_lem.utilities.ai import ai_helper
        resp = MagicMock()
        resp.choices = [MagicMock()]
        resp.choices[0].message.content = "revised post per guidance"
        with patch.object(ai_helper, "_call_llm", return_value=resp) as call:
            out = ai_helper.apply_post_guidance("original", "make it punchier", prefs={"comment_style": "x"})
        assert out == "revised post per guidance"
        assert call.called

    def test_profile_synthesis_included_as_voice_reference(self):
        from cqc_lem.utilities.ai import ai_helper
        resp = MagicMock()
        resp.choices = [MagicMock()]
        resp.choices[0].message.content = "revised"
        with patch.object(ai_helper, "_call_llm", return_value=resp) as call:
            ai_helper.apply_post_guidance("original", "tweak", profile_synthesis="Warm, direct, ops-heavy voice.")
        prompt = call.call_args.kwargs["messages"][1]["content"][0]["text"]
        assert "Warm, direct, ops-heavy voice." in prompt
        assert "voice reference" in prompt

    def test_blank_profile_synthesis_omitted(self):
        from cqc_lem.utilities.ai import ai_helper
        resp = MagicMock()
        resp.choices = [MagicMock()]
        resp.choices[0].message.content = "revised"
        with patch.object(ai_helper, "_call_llm", return_value=resp) as call:
            ai_helper.apply_post_guidance("original", "tweak", profile_synthesis="   ")
        prompt = call.call_args.kwargs["messages"][1]["content"][0]["text"]
        assert "voice reference" not in prompt

    def test_blank_llm_output_falls_back_to_input(self):
        from cqc_lem.utilities.ai import ai_helper
        resp = MagicMock()
        resp.choices = [MagicMock()]
        resp.choices[0].message.content = "   "
        with patch.object(ai_helper, "_call_llm", return_value=resp):
            assert ai_helper.apply_post_guidance("original", "tweak it") == "original"


class TestRegeneratePost:
    def _patches(self, post_type="text"):
        return {
            "get_post_user_id": patch("cqc_lem.utilities.db.get_post_user_id", return_value=1),
            "get_post_buyer_stage": patch("cqc_lem.utilities.db.get_post_buyer_stage", return_value="awareness"),
            "get_post_type": patch("cqc_lem.utilities.db.get_post_type", return_value=post_type),
            "load_profile": patch(f"{_RCP}.load_profile_for_user", return_value=MagicMock()),
            "create_text_post": patch(f"{_RCP}.create_text_post", return_value="fresh post body"),
            "prefs": patch(f"{_RCP}.get_engagement_preferences", return_value={}),
            "synth": patch(f"{_RCP}.get_or_create_profile_synthesis", return_value="voice"),
            "update_content": patch(f"{_RCP}.update_db_post_content"),
            "update_status": patch(f"{_RCP}.update_db_post_status"),
        }

    def test_regenerates_and_resets_to_pending(self):
        from cqc_lem.app import run_content_plan as rcp
        from cqc_lem.utilities.db import PostStatus
        p = self._patches()
        with p["get_post_user_id"], p["get_post_buyer_stage"], p["get_post_type"], p["load_profile"], \
             p["create_text_post"], p["prefs"], p["synth"], \
             p["update_content"] as uc, p["update_status"] as us:
            out = rcp.regenerate_post(42)
        assert out == "fresh post body"
        uc.assert_called_once_with(42, "fresh post body")
        us.assert_called_once_with(42, PostStatus.PENDING)

    def test_guidance_pass_applied(self):
        from cqc_lem.app import run_content_plan as rcp
        p = self._patches()
        with p["get_post_user_id"], p["get_post_buyer_stage"], p["get_post_type"], p["load_profile"], \
             p["create_text_post"], p["prefs"], p["synth"], p["update_content"] as uc, p["update_status"], \
             patch(f"{_RCP}.apply_post_guidance", return_value="guided post") as apg, \
             patch(f"{_RCP}.sanitize_for_linkedin", side_effect=lambda s: s):
            rcp.regenerate_post(42, guidance="add a stat")
        apg.assert_called_once()
        uc.assert_called_once_with(42, "guided post")

    def test_carousel_post_regenerates(self):
        from cqc_lem.app import run_content_plan as rcp
        from cqc_lem.utilities.db import PostStatus
        p = self._patches(post_type="carousel")
        with p["get_post_user_id"], p["get_post_buyer_stage"], p["get_post_type"], p["load_profile"], \
             patch(f"{_RCP}.create_carousel_content", return_value="fresh carousel body") as ccp, \
             patch(f"{_RCP}._score_and_persist_dwell"), \
             patch(f"{_RCP}._persist_gate_findings"), \
             patch(f"{_RCP}._post_used_avatar_media", return_value=False), \
             patch(f"{_RCP}._post_is_flagged_error", return_value=False), \
             p["update_content"] as uc, p["update_status"] as us:
            out = rcp.regenerate_post(42, guidance="drop the pricing slide")
        assert out == "fresh carousel body"
        # The guidance steers the DECK, not a caption-only rewrite after it (issue #794).
        ccp.assert_called_once_with(1, "awareness", 42, guidance="drop the pricing slide")
        uc.assert_called_once_with(42, "fresh carousel body")
        us.assert_called_once_with(42, PostStatus.PENDING)

    def test_carousel_slide_failure_keeps_error_status(self):
        """create_carousel_content flags a slide-render failure 'error'; the regenerate close-out
        must not clear it back to PENDING — the deck still carries the old draft's slides.
        """
        from cqc_lem.app import run_content_plan as rcp
        p = self._patches(post_type="carousel")
        with p["get_post_user_id"], p["get_post_buyer_stage"], p["get_post_type"], p["load_profile"], \
             patch(f"{_RCP}.create_carousel_content", return_value="fresh carousel body"), \
             patch(f"{_RCP}._score_and_persist_dwell"), \
             patch(f"{_RCP}._persist_gate_findings"), \
             patch(f"{_RCP}._post_used_avatar_media", return_value=False), \
             patch(f"{_RCP}._post_is_flagged_error", return_value=True), \
             p["update_content"] as uc, p["update_status"] as us:
            out = rcp.regenerate_post(42)
        assert out == "fresh carousel body"
        uc.assert_called_once_with(42, "fresh carousel body")
        us.assert_not_called()

    def test_no_user_returns_none(self):
        from cqc_lem.app import run_content_plan as rcp
        with patch("cqc_lem.utilities.db.get_post_user_id", return_value=None):
            assert rcp.regenerate_post(42) is None
