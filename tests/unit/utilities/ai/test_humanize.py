"""Unit tests for the humanization / anti-AI-tell rewrite pass (issue #416 — A5): the deterministic
audit + transform helpers (em-dash cap, contraction insertion, tell-word detection), the master +
per-type toggle, and the humanize_text pass itself (READER-mode rewrite via the LLM judge, its
fail-open guarantees, no-fabrication contract, and length budget).
"""

from unittest.mock import MagicMock, patch

import pytest

from cqc_lem.utilities.ai import content_alignment as ca

pytestmark = pytest.mark.unit


def _llm_reply(text: str) -> MagicMock:
    resp = MagicMock()
    resp.choices = [MagicMock(message=MagicMock(content=text))]
    return resp


class TestEmDashCap:
    def test_count_counts_both_variants(self):
        assert ca.count_em_dashes("a — b -- c — d") == 3
        assert ca.count_em_dashes("no dashes here") == 0
        assert ca.count_em_dashes("") == 0

    def test_cap_reduces_to_at_most_one(self):
        text = "First point — second point — third point — fourth point"
        out = ca.cap_em_dashes(text, 1)
        assert ca.count_em_dashes(out) <= 1
        # The first dash is kept; the rest become commas.
        assert out.startswith("First point — second point")
        assert "third point, fourth point" in out

    def test_cap_handles_double_hyphen(self):
        out = ca.cap_em_dashes("a -- b -- c", 0)
        assert ca.count_em_dashes(out) == 0
        assert out == "a, b, c"

    def test_cap_noop_on_empty(self):
        assert ca.cap_em_dashes("", 1) == ""


class TestContractions:
    @pytest.mark.parametrize("expanded,contracted", [
        ("it is great", "it's great"),
        ("we do not know", "we don't know"),
        ("they are here", "they're here"),
        ("I cannot do it", "I can't do it"),
        ("this will not work", "this won't work"),
        ("you have seen it", "you've seen it"),
    ])
    def test_common_forms_are_contracted(self, expanded, contracted):
        assert ca.apply_contractions(expanded) == contracted

    def test_leading_capital_is_preserved(self):
        assert ca.apply_contractions("It is done.") == "It's done."
        assert ca.apply_contractions("Do not stop.") == "Don't stop."

    def test_already_contracted_text_is_untouched(self):
        text = "it's fine, we're good, don't worry"
        assert ca.apply_contractions(text) == text

    def test_noop_on_empty(self):
        assert ca.apply_contractions("") == ""


class TestFindAiTellWords:
    def test_detects_tier1_clichés(self):
        hits = ca.find_ai_tell_words(
            "We must delve into this rich tapestry and leverage the ecosystem.")
        assert set(hits) >= {"delve", "tapestry", "leverage", "ecosystem"}

    def test_case_insensitive_and_deduped(self):
        hits = ca.find_ai_tell_words("Leverage. leverage. LEVERAGE.")
        assert hits == ["leverage"]

    def test_clean_text_has_no_hits(self):
        assert ca.find_ai_tell_words("We use the tool to grow the team.") == []

    def test_empty_input(self):
        assert ca.find_ai_tell_words("") == []
        assert ca.find_ai_tell_words(None) == []


class TestHumanizeEnabled:
    def test_default_on(self, monkeypatch):
        monkeypatch.delenv("HUMANIZE_ENABLED", raising=False)
        assert ca.humanize_enabled() is True
        assert ca.humanize_enabled("comment") is True

    @pytest.mark.parametrize("val", ["0", "false", "no", "off"])
    def test_master_off_disables_all(self, monkeypatch, val):
        monkeypatch.setenv("HUMANIZE_ENABLED", val)
        assert ca.humanize_enabled() is False
        assert ca.humanize_enabled("post") is False

    def test_per_type_override_disables_one_type(self, monkeypatch):
        monkeypatch.setenv("HUMANIZE_ENABLED", "on")
        monkeypatch.setenv("HUMANIZE_COMMENT_ENABLED", "off")
        assert ca.humanize_enabled("comment") is False
        # Other types unaffected.
        assert ca.humanize_enabled("post") is True
        assert ca.humanize_enabled("newsletter") is True


class TestHumanizeText:
    def test_disabled_returns_byte_identical(self, monkeypatch):
        monkeypatch.setenv("HUMANIZE_ENABLED", "off")
        original = "We must delve into this — it is a rich tapestry."
        with patch("cqc_lem.utilities.ai.ai_helper._call_llm") as m:
            assert ca.humanize_text(original, "post") == original
            m.assert_not_called()

    def test_empty_input_is_returned_unchanged(self, monkeypatch):
        monkeypatch.setenv("HUMANIZE_ENABLED", "on")
        with patch("cqc_lem.utilities.ai.ai_helper._call_llm") as m:
            assert ca.humanize_text("", "post") == ""
            assert ca.humanize_text("   ", "post") == "   "
            m.assert_not_called()

    def test_rewrite_is_returned_with_deterministic_guards_applied(self, monkeypatch):
        monkeypatch.setenv("HUMANIZE_ENABLED", "on")
        # A rewrite the model returns that still carries 3 em-dashes and an expanded form — the
        # deterministic guards must still cap the dashes and add the contraction.
        rewrite = "I shipped it in March — it is live now — the team helped — we are proud."
        with patch("cqc_lem.utilities.ai.ai_helper._call_llm", return_value=_llm_reply(rewrite)):
            out = ca.humanize_text("original AI slop text here", "post")
        assert ca.count_em_dashes(out) <= 1
        assert "it's live now" in out
        assert "we're proud" in out

    def test_fails_open_on_llm_error(self, monkeypatch):
        monkeypatch.setenv("HUMANIZE_ENABLED", "on")
        original = "This is the original draft that must survive a proxy outage."
        with patch("cqc_lem.utilities.ai.ai_helper._call_llm",
                   side_effect=RuntimeError("proxy down")):
            assert ca.humanize_text(original, "post") == original

    def test_fails_open_on_empty_rewrite(self, monkeypatch):
        monkeypatch.setenv("HUMANIZE_ENABLED", "on")
        original = "Original content that should be kept when the model returns nothing."
        with patch("cqc_lem.utilities.ai.ai_helper._call_llm", return_value=_llm_reply("")):
            assert ca.humanize_text(original, "post") == original

    def test_fails_open_on_truncated_rewrite(self, monkeypatch):
        monkeypatch.setenv("HUMANIZE_ENABLED", "on")
        original = "A" * 400  # a long draft
        with patch("cqc_lem.utilities.ai.ai_helper._call_llm", return_value=_llm_reply("oops")):
            # A collapse to a tiny fragment means the model refused/was cut off — keep the original.
            assert ca.humanize_text(original, "post") == original

    def test_max_chars_budget_keeps_original_when_exceeded(self, monkeypatch):
        monkeypatch.setenv("HUMANIZE_ENABLED", "on")
        original = "A short DM well within the limit."
        long_rewrite = "x" * 500
        with patch("cqc_lem.utilities.ai.ai_helper._call_llm",
                   return_value=_llm_reply(long_rewrite)):
            assert ca.humanize_text(original, "dm", max_chars=300) == original

    def test_max_chars_budget_accepts_within_limit(self, monkeypatch):
        monkeypatch.setenv("HUMANIZE_ENABLED", "on")
        original = "The original rendered DM that reads a bit stiff and formal today."
        rewrite = "Hey, thanks for connecting. What are you working on this week?"
        with patch("cqc_lem.utilities.ai.ai_helper._call_llm", return_value=_llm_reply(rewrite)):
            out = ca.humanize_text(original, "dm", max_chars=300)
        assert out == rewrite
        assert len(out) <= 300

    def test_prompt_forbids_fabrication_and_lists_detected_tells(self, monkeypatch):
        monkeypatch.setenv("HUMANIZE_ENABLED", "on")
        captured = {}

        def _fake(**kwargs):
            captured["messages"] = kwargs["messages"]
            return _llm_reply("A clean, humanized version of the draft that stands on its own.")

        draft = "We leverage a rich tapestry to foster synergy."
        with patch("cqc_lem.utilities.ai.ai_helper._call_llm", side_effect=_fake):
            ca.humanize_text(draft, "post", profile_synthesis="Ran a 12-person agency for 8 years.")
        system = captured["messages"][0]["content"]
        # No-fabrication contract is present (the core no-fabrication guarantee for auto-published text).
        assert "NEVER invent" in system
        # Real specifics are sourced ONLY from the author background, never invented.
        assert "Ran a 12-person agency" in system
        # Detected tells are surfaced to the rewrite prompt.
        assert "leverage" in system and "tapestry" in system


class TestFindTitleSlopWords:
    def test_detects_hype_and_tier1_words(self):
        hits = ca.find_title_slop_words(
            "7 Game-Changing AI Tactics for Explosive LinkedIn Growth")
        assert set(hits) >= {"game-changing", "explosive"}
        assert "unlock" in ca.find_title_slop_words("Unlock the Ultimate Secret")
        assert "ultimate" in ca.find_title_slop_words("Unlock the Ultimate Secret")

    def test_case_insensitive_and_deduped(self):
        assert ca.find_title_slop_words("Explosive. EXPLOSIVE growth") == ["explosive"]

    def test_detects_hype_tokens_that_start_with_a_digit(self):
        assert ca.find_title_slop_words("10x Your LinkedIn Reach") == ["10x"]
        assert ca.find_title_slop_words("How to 10X reach") == ["10x"]

    def test_clean_headline_has_no_hits(self):
        assert ca.find_title_slop_words("What our best buyers actually read") == []

    def test_empty_input(self):
        assert ca.find_title_slop_words("") == []
        assert ca.find_title_slop_words(None) == []


class TestHumanizeTitle:
    def test_sloppy_title_in_dehyped_title_out(self, monkeypatch):
        monkeypatch.setenv("HUMANIZE_ENABLED", "on")
        slop = "7 Game-Changing AI Tactics for Explosive LinkedIn Growth"
        clean = "The AI tactics that actually moved our LinkedIn numbers"
        with patch("cqc_lem.utilities.ai.ai_helper._call_llm", return_value=_llm_reply(clean)):
            out = ca.humanize_title(slop, "newsletter")
        assert out == clean
        assert ca.find_title_slop_words(out) == []

    def test_disabled_returns_byte_identical(self, monkeypatch):
        monkeypatch.setenv("HUMANIZE_ENABLED", "off")
        slop = "Unlock Explosive Growth With These Game-Changing Tactics"
        with patch("cqc_lem.utilities.ai.ai_helper._call_llm") as m:
            assert ca.humanize_title(slop, "newsletter") == slop
            m.assert_not_called()

    def test_per_type_toggle_disables_newsletter_titles(self, monkeypatch):
        monkeypatch.setenv("HUMANIZE_ENABLED", "on")
        monkeypatch.setenv("HUMANIZE_NEWSLETTER_ENABLED", "off")
        slop = "Unlock Explosive Growth With These Game-Changing Tactics"
        with patch("cqc_lem.utilities.ai.ai_helper._call_llm") as m:
            assert ca.humanize_title(slop, "newsletter") == slop
            m.assert_not_called()

    def test_empty_input_is_returned_unchanged(self, monkeypatch):
        monkeypatch.setenv("HUMANIZE_ENABLED", "on")
        with patch("cqc_lem.utilities.ai.ai_helper._call_llm") as m:
            assert ca.humanize_title("", "newsletter") == ""
            assert ca.humanize_title(None, "newsletter") is None
            m.assert_not_called()

    def test_reply_is_coerced_back_into_one_headline(self, monkeypatch):
        monkeypatch.setenv("HUMANIZE_ENABLED", "on")
        messy = 'Title: "The AI tactics that actually moved our numbers."\n\n(kept the hook)'
        with patch("cqc_lem.utilities.ai.ai_helper._call_llm", return_value=_llm_reply(messy)):
            out = ca.humanize_title("Unlock Explosive Growth Today With AI", "newsletter")
        assert out == "The AI tactics that actually moved our numbers"

    def test_em_dashes_are_stripped_from_the_headline(self, monkeypatch):
        monkeypatch.setenv("HUMANIZE_ENABLED", "on")
        with patch("cqc_lem.utilities.ai.ai_helper._call_llm",
                   return_value=_llm_reply("Buyers ignored our best work — here is what changed")):
            out = ca.humanize_title("Why Impressive AI Content Repels Your Best Buyers", "newsletter")
        assert ca.count_em_dashes(out) == 0
        assert out == "Buyers ignored our best work, here is what changed"

    def test_fails_open_on_llm_error(self, monkeypatch):
        monkeypatch.setenv("HUMANIZE_ENABLED", "on")
        original = "Why Impressive AI Content Repels Your Best Buyers"
        with patch("cqc_lem.utilities.ai.ai_helper._call_llm", side_effect=RuntimeError("proxy down")):
            assert ca.humanize_title(original, "newsletter") == original

    def test_fails_open_on_empty_or_fragment_reply(self, monkeypatch):
        monkeypatch.setenv("HUMANIZE_ENABLED", "on")
        original = "Why Impressive AI Content Repels Your Best Buyers"
        for reply in ("", "   ", "AI"):
            with patch("cqc_lem.utilities.ai.ai_helper._call_llm", return_value=_llm_reply(reply)):
                assert ca.humanize_title(original, "newsletter") == original

    def test_fails_open_when_reply_is_prose_not_a_headline(self, monkeypatch):
        monkeypatch.setenv("HUMANIZE_ENABLED", "on")
        original = "Why Impressive AI Content Repels Your Best Buyers"
        prose = ("Here is a rewritten headline for you, and I kept the hook while removing the hype "
                 "words you asked me to remove from the original draft headline.")
        with patch("cqc_lem.utilities.ai.ai_helper._call_llm", return_value=_llm_reply(prose)):
            assert ca.humanize_title(original, "newsletter") == original

    def test_fails_open_when_rewrite_is_slopier(self, monkeypatch):
        monkeypatch.setenv("HUMANIZE_ENABLED", "on")
        original = "How we rebuilt our newsletter open rates"
        with patch("cqc_lem.utilities.ai.ai_helper._call_llm",
                   return_value=_llm_reply("Unlock Explosive Open Rates")):
            assert ca.humanize_title(original, "newsletter") == original

    def test_max_chars_budget_keeps_original_when_exceeded(self, monkeypatch):
        monkeypatch.setenv("HUMANIZE_ENABLED", "on")
        original = "A" * 120
        with patch("cqc_lem.utilities.ai.ai_helper._call_llm", return_value=_llm_reply("B" * 60)):
            assert ca.humanize_title(original, "newsletter", max_chars=40) == original

    def test_short_draft_may_grow_up_to_the_90_char_headline_floor(self, monkeypatch):
        # De-hyping a short hype headline needs a little room; budget = min(max_chars, max(90, len)).
        monkeypatch.setenv("HUMANIZE_ENABLED", "on")
        clean = "The reach numbers we actually got after six months of posting"  # 61 chars
        with patch("cqc_lem.utilities.ai.ai_helper._call_llm", return_value=_llm_reply(clean)):
            assert ca.humanize_title("10x Your Reach", "newsletter") == clean
        with patch("cqc_lem.utilities.ai.ai_helper._call_llm", return_value=_llm_reply("B" * 91)):
            assert ca.humanize_title("10x Your Reach", "newsletter") == "10x Your Reach"

    def test_long_draft_may_not_grow_past_its_own_length(self, monkeypatch):
        monkeypatch.setenv("HUMANIZE_ENABLED", "on")
        original = "A" * 100
        with patch("cqc_lem.utilities.ai.ai_helper._call_llm", return_value=_llm_reply("B" * 101)):
            assert ca.humanize_title(original, "newsletter") == original

    def test_prompt_forbids_fabrication_and_lists_detected_tells(self, monkeypatch):
        monkeypatch.setenv("HUMANIZE_ENABLED", "on")
        captured = {}

        def _fake(**kwargs):
            captured["messages"] = kwargs["messages"]
            return _llm_reply("The AI tactics that actually moved our numbers")

        with patch("cqc_lem.utilities.ai.ai_helper._call_llm", side_effect=_fake):
            ca.humanize_title("7 Game-Changing Tactics to Unlock Explosive Growth", "newsletter",
                              profile_synthesis="Ran a 12-person agency for 8 years.")
        system = captured["messages"][0]["content"]
        assert "NEVER invent facts" in system
        assert "Ran a 12-person agency" in system
        for tell in ("game-changing", "unlock", "explosive"):
            assert tell in system
        assert "at most 255 characters" in system


class TestGeneratorsRouteThroughHumanize:
    """Acceptance: all four content types run through the humanization pass before review. Each test
    turns the pass ON, spies on the wired humanize_text, and asserts the generator's output is the
    humanized result tagged with the right content_type.
    """

    def _profile(self):
        prof = MagicMock()
        prof.model_dump_json.return_value = "{}"
        return prof

    def test_comment_generator_routes_through(self, monkeypatch):
        monkeypatch.setenv("HUMANIZE_ENABLED", "on")
        from cqc_lem.utilities.ai import ai_helper
        seen = {}

        # The humanized text is what the comment quality gate grades (issue #617), so the spy
        # returns a contract-clearing comment rather than a bare marker string.
        humanized = ("Your post on the target number is the part I'd push on. We tried that last "
                     "month and it slowed us down.")

        def _spy(text, content_type="post", **kw):
            seen["content_type"] = content_type
            return humanized

        with patch("cqc_lem.utilities.ai.ai_helper._call_llm", return_value=_llm_reply("raw comment")), \
             patch("cqc_lem.utilities.ai.ai_helper.research_topic", return_value={}), \
             patch("cqc_lem.utilities.ai.ai_helper._humanize_text", side_effect=_spy):
            out = ai_helper.generate_ai_response("a target post", self._profile())
        assert out == humanized
        assert seen["content_type"] == "comment"

    def test_seed_comment_routes_through(self, monkeypatch):
        monkeypatch.setenv("HUMANIZE_ENABLED", "on")
        from cqc_lem.utilities.ai import ai_helper
        with patch("cqc_lem.utilities.ai.ai_helper._call_llm", return_value=_llm_reply("raw seed")), \
             patch("cqc_lem.utilities.ai.ai_helper._humanize_text",
                   side_effect=lambda t, content_type="post", **kw: f"H:{content_type}") as h:
            out = ai_helper.generate_seed_comment("my post", self._profile())
        assert out == "H:comment"
        h.assert_called_once()

    def test_thread_reply_routes_through(self, monkeypatch):
        monkeypatch.setenv("HUMANIZE_ENABLED", "on")
        from cqc_lem.utilities.ai import ai_helper
        with patch("cqc_lem.utilities.ai.ai_helper._call_llm", return_value=_llm_reply("raw reply")), \
             patch("cqc_lem.utilities.ai.ai_helper._humanize_text",
                   side_effect=lambda t, content_type="post", **kw: f"H:{content_type}"):
            out = ai_helper.generate_thread_reply("my post", "their comment", self._profile())
        assert out == "H:comment"

    def test_newsletter_body_routes_through(self, monkeypatch):
        monkeypatch.setenv("HUMANIZE_ENABLED", "on")
        from cqc_lem.utilities.ai import ai_helper
        edition_json = '{"title": "T", "subtitle": "S", "body": "raw newsletter body"}'
        with patch("cqc_lem.utilities.ai.ai_helper._call_llm", return_value=_llm_reply(edition_json)), \
             patch("cqc_lem.utilities.ai.ai_helper._humanize_text",
                   side_effect=lambda t, content_type="post", **kw: f"HUMANIZED[{content_type}]") as h:
            edition = ai_helper.generate_newsletter_edition(self._profile(), topic="x")
        assert edition is not None
        assert "HUMANIZED[newsletter]" in edition["body"]
        h.assert_called_once()

    def test_newsletter_title_routes_through_title_dehype(self, monkeypatch):
        monkeypatch.setenv("HUMANIZE_ENABLED", "on")
        from cqc_lem.utilities.ai import ai_helper
        seen = {}

        def _spy(title, content_type="post", **kw):
            seen["content_type"] = content_type
            seen["title"] = title
            return "The AI tactics that actually moved our numbers"

        edition_json = ('{"title": "7 Game-Changing AI Tactics for Explosive Growth", '
                        '"subtitle": "S", "body": "raw newsletter body"}')
        with patch("cqc_lem.utilities.ai.ai_helper._call_llm", return_value=_llm_reply(edition_json)), \
             patch("cqc_lem.utilities.ai.ai_helper._humanize_text", side_effect=lambda t, **kw: t), \
             patch("cqc_lem.utilities.ai.ai_helper._humanize_title", side_effect=_spy):
            edition = ai_helper.generate_newsletter_edition(self._profile(), topic="x")
        assert edition is not None
        assert edition["title"] == "The AI tactics that actually moved our numbers"
        assert seen["content_type"] == "newsletter"
        assert seen["title"] == "7 Game-Changing AI Tactics for Explosive Growth"

    def test_newsletter_fallback_title_routes_through_title_dehype(self, monkeypatch):
        monkeypatch.setenv("HUMANIZE_ENABLED", "on")
        from cqc_lem.utilities.ai import ai_helper
        # Non-JSON reply -> the fallback path (first line = title, remainder = body).
        raw = "Unlock Explosive Growth With AI\n\nThe body of the edition goes here."
        with patch("cqc_lem.utilities.ai.ai_helper._call_llm", return_value=_llm_reply(raw)), \
             patch("cqc_lem.utilities.ai.ai_helper._humanize_text", side_effect=lambda t, **kw: t), \
             patch("cqc_lem.utilities.ai.ai_helper._humanize_title",
                   side_effect=lambda t, content_type="post", **kw: f"TITLE[{content_type}]"):
            edition = ai_helper.generate_newsletter_edition(self._profile(), topic="x")
        assert edition is not None
        assert edition["title"] == "TITLE[newsletter]"

    def test_newsletter_title_unchanged_when_humanize_disabled(self, monkeypatch):
        monkeypatch.setenv("HUMANIZE_ENABLED", "off")
        from cqc_lem.utilities.ai import ai_helper
        raw_title = "7 Game-Changing AI Tactics for Explosive Growth"
        edition_json = ('{"title": "' + raw_title + '", "subtitle": "S", '
                        '"body": "raw newsletter body"}')
        with patch("cqc_lem.utilities.ai.ai_helper._call_llm", return_value=_llm_reply(edition_json)):
            edition = ai_helper.generate_newsletter_edition(self._profile(), topic="x")
        assert edition is not None
        assert edition["title"] == raw_title
        assert edition["body"] == "raw newsletter body"

    def test_dm_builder_routes_through(self, monkeypatch):
        monkeypatch.setenv("HUMANIZE_ENABLED", "on")
        from cqc_lem.app.engagement import outreach
        with patch("cqc_lem.app.engagement.outreach.get_dm_template",
                   return_value={"template_text": "Hi {first_name}"}), \
             patch("cqc_lem.app.engagement.outreach.get_ai_message_refinement", return_value="Hi Sam, welcome."), \
             patch("cqc_lem.app.engagement.outreach.humanize_text",
                   side_effect=lambda t, content_type="post", **kw: f"DM[{content_type},{kw.get('max_chars')}]") as h:
            out = outreach.build_dm_from_template(1, "connection", "Sam", MagicMock())
        assert out == "DM[dm,300]"
        h.assert_called_once()

    def test_post_path_imports_the_real_pass(self):
        # create_text_post humanizes just before the A1 gate (verified in the module); assert the post
        # path binds the canonical humanize_text, not a stub, so the wiring can't silently no-op.
        from cqc_lem.app import run_content_plan as rcp
        assert rcp.humanize_text is ca.humanize_text
