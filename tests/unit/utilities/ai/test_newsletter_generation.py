"""Unit tests for newsletter edition generation."""

import json
from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit

_AI = "cqc_lem.utilities.ai.ai_helper"


def _resp(text):
    r = MagicMock(); r.choices = [MagicMock(message=MagicMock(content=text))]
    return r


class TestGenerateNewsletterEdition:
    def test_parses_json_with_subtitle(self):
        from cqc_lem.utilities.ai import ai_helper
        prof = MagicMock(); prof.model_dump_json.return_value = "{}"
        payload = json.dumps({
            "title": "5 Levers for Reach",
            "subtitle": "The reach levers most creators ignore — and how to pull them this week.",
            "body": "Hook line\n\nSECTION ONE\n\nA developed paragraph with a real example.",
        })
        with patch(f"{_AI}._call_llm", return_value=_resp(payload)):
            out = ai_helper.generate_newsletter_edition(prof, topic="reach")
        assert out["title"] == "5 Levers for Reach"
        assert out["subtitle"] and len(out["subtitle"]) <= 150
        assert "SECTION ONE" in out["body"]

    def test_subtitle_falls_back_to_topic_when_missing(self):
        from cqc_lem.utilities.ai import ai_helper
        prof = MagicMock(); prof.model_dump_json.return_value = "{}"
        payload = json.dumps({"title": "T", "body": "Some body text here."})
        with patch(f"{_AI}._call_llm", return_value=_resp(payload)):
            out = ai_helper.generate_newsletter_edition(prof, topic="delegation")
        assert out["subtitle"] == "delegation"

    def test_markdown_stripped_from_body(self):
        from cqc_lem.utilities.ai import ai_helper
        prof = MagicMock(); prof.model_dump_json.return_value = "{}"
        body = "# Big Header\n\nHere is **bold** text and a *takeaway*."
        payload = json.dumps({"title": "T", "subtitle": "S", "body": body})
        with patch(f"{_AI}._call_llm", return_value=_resp(payload)):
            out = ai_helper.generate_newsletter_edition(prof)
        assert "#" not in out["body"]
        assert "**" not in out["body"]
        assert "Big Header" in out["body"]
        assert "bold" in out["body"] and "takeaway" in out["body"]

    def test_body_keeps_section_spacing(self):
        from cqc_lem.utilities.ai import ai_helper
        prof = MagicMock(); prof.model_dump_json.return_value = "{}"
        body = "Hook\n\nSECTION ONE\n\nParagraph one.\n\nSECTION TWO\n\nParagraph two."
        payload = json.dumps({"title": "T", "subtitle": "S", "body": body})
        with patch(f"{_AI}._call_llm", return_value=_resp(payload)):
            out = ai_helper.generate_newsletter_edition(prof)
        assert "\n\n" in out["body"]  # blank lines between sections preserved

    def test_fallback_first_line_is_title(self):
        from cqc_lem.utilities.ai import ai_helper
        prof = MagicMock(); prof.model_dump_json.return_value = "{}"
        with patch(f"{_AI}._call_llm", return_value=_resp("My Title\nBody line one\nBody line two")):
            out = ai_helper.generate_newsletter_edition(prof, topic="growth")
        assert out["title"] == "My Title"
        assert out["body"].startswith("Body line one")
        assert out["subtitle"] == "growth"

    def test_none_on_empty(self):
        from cqc_lem.utilities.ai import ai_helper
        prof = MagicMock(); prof.model_dump_json.return_value = "{}"
        with patch(f"{_AI}._call_llm", return_value=_resp(None)):
            assert ai_helper.generate_newsletter_edition(prof) is None


class TestGenerationUsesSynthesisAndSubject:
    def test_synthesis_replaces_raw_json_and_subject_in_prompt(self):
        from cqc_lem.utilities.ai import ai_helper
        prof = MagicMock(); prof.model_dump_json.return_value = "RAW_PROFILE_JSON_SHOULD_NOT_APPEAR"
        payload = json.dumps({"title": "T", "subtitle": "S", "subject": "Planned Subject X", "body": "Body."})
        with patch(f"{_AI}._call_llm", return_value=_resp(payload)) as call:
            out = ai_helper.generate_newsletter_edition(
                prof, topic="desc", subject="Planned Subject X", avoid_subjects=["Avoid Me"],
                profile_synthesis="THE VOICE BRIEF")
        prompt = call.call_args.kwargs["messages"][1]["content"]
        assert "THE VOICE BRIEF" in prompt
        assert "RAW_PROFILE_JSON_SHOULD_NOT_APPEAR" not in prompt  # raw dump NOT used when synthesis given
        assert "Planned Subject X" in prompt
        assert "Avoid Me" in prompt
        assert out["subject"] == "Planned Subject X"

    def test_falls_back_to_raw_json_without_synthesis(self):
        from cqc_lem.utilities.ai import ai_helper
        prof = MagicMock(); prof.model_dump_json.return_value = "RAW_JSON_HERE"
        payload = json.dumps({"title": "T", "subtitle": "S", "body": "Body."})
        with patch(f"{_AI}._call_llm", return_value=_resp(payload)) as call:
            ai_helper.generate_newsletter_edition(prof, topic="desc")
        prompt = call.call_args.kwargs["messages"][1]["content"]
        assert "RAW_JSON_HERE" in prompt

    def test_guidance_included_in_prompt(self):
        from cqc_lem.utilities.ai import ai_helper
        prof = MagicMock(); prof.model_dump_json.return_value = "{}"
        payload = json.dumps({"title": "T", "subtitle": "S", "subject": "S", "body": "B."})
        with patch(f"{_AI}._call_llm", return_value=_resp(payload)) as call:
            ai_helper.generate_newsletter_edition(prof, profile_synthesis="v", guidance="Make it about hiring")
        prompt = call.call_args.kwargs["messages"][1]["content"]
        assert "Make it about hiring" in prompt

    def test_subject_defaults_from_passed_subject_when_model_omits(self):
        from cqc_lem.utilities.ai import ai_helper
        prof = MagicMock(); prof.model_dump_json.return_value = "{}"
        payload = json.dumps({"title": "T", "subtitle": "S", "body": "B."})  # no subject key
        with patch(f"{_AI}._call_llm", return_value=_resp(payload)):
            out = ai_helper.generate_newsletter_edition(prof, subject="Fallback Subject", profile_synthesis="v")
        assert out["subject"] == "Fallback Subject"


class TestGenerationWritesToBlueprint:
    _BLUEPRINT = {"subject": "S", "angle": "a", "format": "contrarian",
                  "hook_style": "bold_claim", "cta_style": "debate"}

    def _gen(self, **kwargs):
        from cqc_lem.utilities.ai import ai_helper
        prof = MagicMock(); prof.model_dump_json.return_value = "{}"
        payload = json.dumps({"title": "T", "subtitle": "S", "subject": "S",
                              "body": "The opener line.\n\nSECTION\n\nBody text."})
        with patch(f"{_AI}._call_llm", return_value=_resp(payload)) as call:
            out = ai_helper.generate_newsletter_edition(prof, profile_synthesis="THE VOICE BRIEF",
                                                        **kwargs)
        system = call.call_args.kwargs["messages"][0]["content"]
        user = call.call_args.kwargs["messages"][1]["content"]
        return out, system, user

    def test_blueprint_structure_hook_cta_injected(self):
        from cqc_lem.utilities.ai.content_framework import CTA_STYLES, HOOK_STYLES, NEWSLETTER_FORMATS as FORMATS
        out, system, user = self._gen(blueprint=self._BLUEPRINT)
        assert "ASSIGNED BLUEPRINT" in system
        assert FORMATS["contrarian"]["structure"][1] in system  # skeleton sections present
        assert HOOK_STYLES["bold_claim"]["guidance"] in system
        assert CTA_STYLES["debate"]["guidance"] in system
        # Blueprint shape echoed back for persistence.
        assert out["format"] == "contrarian" and out["hook_style"] == "bold_claim"
        assert out["cta_style"] == "debate"
        assert out["opening_line"] == "The opener line."

    def test_newsletter_writing_directive_injected(self):
        _, system, _ = self._gen(blueprint=self._BLUEPRINT)
        assert "LinkedIn newsletter craft rules" in system
        assert "inbox subject line" in system.lower()
        assert "cover-image brief" in system
        assert "NOTIFICATION-DRIVEN reader" in system

    def test_system_prompt_carries_no_canned_scaffold(self):
        from cqc_lem.utilities.ai.slop_lint import find_canned_scaffolds
        _, system, _ = self._gen(blueprint=self._BLUEPRINT)
        # The directive itself quotes the banned list, so remove it before grading.
        directive_only = system.split("LinkedIn newsletter craft rules")[0]
        assert find_canned_scaffolds(directive_only) == [], (
            "newsletter system prompt supplies a template the lint flags in the output")

    def test_user_prompt_blog_fidelity_signal(self):
        _, _, user = self._gen(blog_content="The central framework is X.")
        assert "SOURCE MATERIAL" in user
        assert "TRACK its central claim" in user
        assert "The central framework is X." in user

    def test_avoid_openers_injected(self):
        _, _, user = self._gen(avoid_openers=["Most founders treat X like Y.", "It was a Tuesday."])
        assert "Most founders treat X like Y." in user
        assert "It was a Tuesday." in user
        assert "must NOT reuse or resemble" in user

    def test_research_findings_injected_as_source_material(self):
        _, _, user = self._gen(research={"findings": "In Q1 2026, 57% of SMBs adopted...",
                                         "sources": [{"url": "https://src"}]})
        assert "SOURCE MATERIAL" in user
        assert "In Q1 2026, 57% of SMBs adopted..." in user
        assert "Do NOT invent statistics" in user
        assert "Do NOT paste URLs" in user
        assert "https://src" not in user  # source URLs never enter the writing prompt/body

    def test_no_research_block_when_findings_empty(self):
        _, _, user = self._gen(research={"findings": "", "sources": []})
        assert "SOURCE MATERIAL" not in user

    def test_blueprint_plus_synthesis_coexist(self):
        prof_marker = "RAW_PROFILE_JSON_SHOULD_NOT_APPEAR"
        from cqc_lem.utilities.ai import ai_helper
        prof = MagicMock(); prof.model_dump_json.return_value = prof_marker
        payload = json.dumps({"title": "T", "subtitle": "S", "subject": "S", "body": "B."})
        with patch(f"{_AI}._call_llm", return_value=_resp(payload)) as call:
            ai_helper.generate_newsletter_edition(prof, profile_synthesis="THE VOICE BRIEF",
                                                  blueprint=self._BLUEPRINT,
                                                  research={"findings": "A fact.", "sources": []})
        user = call.call_args.kwargs["messages"][1]["content"]
        assert "THE VOICE BRIEF" in user
        assert prof_marker not in user

    def test_no_blueprint_keeps_default_behavior(self):
        out, system, _ = self._gen()
        assert "ASSIGNED BLUEPRINT" not in system
        assert out["format"] is None and out["hook_style"] is None
        assert out["opening_line"] == "The opener line."

    def test_fallback_plain_text_still_carries_shape(self):
        from cqc_lem.utilities.ai import ai_helper
        prof = MagicMock(); prof.model_dump_json.return_value = "{}"
        with patch(f"{_AI}._call_llm", return_value=_resp("My Title\nFirst body line\nMore")):
            out = ai_helper.generate_newsletter_edition(prof, topic="growth",
                                                        blueprint=self._BLUEPRINT)
        assert out["format"] == "contrarian" and out["hook_style"] == "bold_claim"
        assert out["opening_line"] == "First body line"


class TestNewsletterBlogAlignmentWiring:
    def _edition(self, body):
        return _resp(json.dumps({"title": "A specific title", "subtitle": "why to read it",
                                  "subject": "the subject", "body": body}))

    def test_blog_alignment_triggers_regeneration_when_promoted(self, monkeypatch):
        from cqc_lem.utilities.ai import ai_helper
        monkeypatch.setenv("SLOP_LINT_BLOG_ALIGNMENT_MIN", "0.30")
        monkeypatch.setenv("SLOP_LINT_SEVERITY_BLOG_ALIGNMENT", "hard")
        blog = ("We rebuilt the billing importer from the audit log after a Friday deploy took down "
                "checkout for forty minutes.")
        bad_body = "Generic leadership advice with no shared terms."
        clean_body = ("The Friday billing deploy that took down checkout for forty minutes taught us "
                      "one rule: no pricing change ships after noon on a Friday. We rebuilt the "
                      "billing importer from the audit log, one row at a time.")
        prof = MagicMock()
        prof.model_dump_json.return_value = "{}"
        with patch(f"{_AI}._call_llm",
                   side_effect=[self._edition(bad_body), self._edition(clean_body)]) as m:
            out = ai_helper.generate_newsletter_edition(prof, topic="ops", blog_content=blog)
        assert m.call_count == 2
        assert out["body"] == clean_body

    def test_blog_content_reaches_slop_lint(self, monkeypatch):
        from cqc_lem.utilities.ai import ai_helper, slop_lint as _slop
        monkeypatch.setenv("SLOP_LINT_BLOG_ALIGNMENT_MIN", "0.30")
        blog = "Specific facts about billing importers."
        body = "Generic leadership advice with no shared terms."
        prof = MagicMock()
        prof.model_dump_json.return_value = "{}"
        with patch(f"{_AI}._call_llm", return_value=self._edition(body)), \
             patch.object(_slop, "lint_report", wraps=_slop.lint_report) as lint:
            ai_helper.generate_newsletter_edition(prof, topic="ops", blog_content=blog)
        assert any(call.kwargs.get("blog_content") == blog for call in lint.call_args_list)


class TestPlanNewsletterTopics:
    def test_returns_distinct_subjects(self):
        from cqc_lem.utilities.ai import ai_helper
        payload = json.dumps({"editions": [
            {"subject": "Content frameworks that scale", "angle": "foundational"},
            {"subject": "Engagement tactics that compound", "angle": "tactical"},
            {"subject": "Personal-brand positioning", "angle": "advanced"},
        ]})
        with patch(f"{_AI}._call_llm", return_value=_resp(payload)):
            out = ai_helper.plan_newsletter_topics("voice", "desc", {}, [], 3)
        assert len(out) == 3
        assert {o["subject"] for o in out} == {
            "Content frameworks that scale", "Engagement tactics that compound",
            "Personal-brand positioning"}
        assert out[0]["angle"] == "foundational"

    def test_dedups_within_response(self):
        from cqc_lem.utilities.ai import ai_helper
        payload = json.dumps({"editions": [
            {"subject": "Same Topic", "angle": "a"},
            {"subject": "same topic", "angle": "b"},  # case-insensitive duplicate dropped
            {"subject": "Different", "angle": "c"},
        ]})
        with patch(f"{_AI}._call_llm", return_value=_resp(payload)):
            out = ai_helper.plan_newsletter_topics("v", "d", None, [], 3)
        assert [o["subject"] for o in out] == ["Same Topic", "Different"]

    def test_passes_prior_subjects_into_prompt(self):
        from cqc_lem.utilities.ai import ai_helper
        payload = json.dumps({"editions": [{"subject": "Fresh", "angle": "x"}]})
        with patch(f"{_AI}._call_llm", return_value=_resp(payload)) as call:
            ai_helper.plan_newsletter_topics("v", "d", None, ["Old Subject A", "Old Subject B"], 1)
        prompt = call.call_args.kwargs["messages"][1]["content"]
        assert "Old Subject A" in prompt and "Old Subject B" in prompt
        assert "AVOID" in prompt

    def test_respects_count_cap(self):
        from cqc_lem.utilities.ai import ai_helper
        payload = json.dumps({"editions": [
            {"subject": "A", "angle": "1"}, {"subject": "B", "angle": "2"},
            {"subject": "C", "angle": "3"}]})
        with patch(f"{_AI}._call_llm", return_value=_resp(payload)):
            out = ai_helper.plan_newsletter_topics("v", "d", None, [], 2)
        assert len(out) == 2

    def test_tolerates_json_fences(self):
        from cqc_lem.utilities.ai import ai_helper
        payload = "```json\n" + json.dumps({"editions": [{"subject": "S", "angle": "a"}]}) + "\n```"
        with patch(f"{_AI}._call_llm", return_value=_resp(payload)):
            out = ai_helper.plan_newsletter_topics("v", "d", None, [], 1)
        assert out and out[0]["subject"] == "S"

    def test_extracts_json_from_surrounding_prose(self):
        from cqc_lem.utilities.ai import ai_helper
        payload = "Here is the plan:\n" + json.dumps({"editions": [{"subject": "S", "angle": "a"}]})
        with patch(f"{_AI}._call_llm", return_value=_resp(payload)):
            out = ai_helper.plan_newsletter_topics("v", "d", None, [], 1)
        assert out and out[0]["subject"] == "S"

    def test_bad_json_falls_back_empty(self):
        from cqc_lem.utilities.ai import ai_helper
        with patch(f"{_AI}._call_llm", return_value=_resp("not json at all")):
            assert ai_helper.plan_newsletter_topics("v", "d", None, [], 3) == []

    def test_llm_exception_falls_back_empty(self):
        from cqc_lem.utilities.ai import ai_helper
        with patch(f"{_AI}._call_llm", side_effect=Exception("boom")):
            assert ai_helper.plan_newsletter_topics("v", "d", None, [], 3) == []


class TestPlannerEmitsBlueprints:
    def test_full_blueprints_with_valid_keys_and_structure(self):
        from cqc_lem.utilities.ai import ai_helper
        from cqc_lem.utilities.ai.content_framework import CTA_STYLES, HOOK_STYLES, NEWSLETTER_FORMATS as FORMATS
        payload = json.dumps({"editions": [
            {"subject": "A", "angle": "1", "format": "case_study", "hook_style": "micro_story",
             "cta_style": "reply_question"},
            {"subject": "B", "angle": "2", "format": "listicle", "hook_style": "bold_claim",
             "cta_style": "challenge"},
        ]})
        with patch(f"{_AI}._call_llm", return_value=_resp(payload)):
            out = ai_helper.plan_newsletter_topics("v", "d", None, [], 2)
        assert [o["format"] for o in out] == ["case_study", "listicle"]
        assert [o["hook_style"] for o in out] == ["micro_story", "bold_claim"]
        for o in out:
            assert o["format"] in FORMATS and o["hook_style"] in HOOK_STYLES
            assert o["cta_style"] in CTA_STYLES
            assert o["structure"] == FORMATS[o["format"]]["structure"]

    def test_consecutive_repeats_from_model_are_fixed_in_code(self):
        from cqc_lem.utilities.ai import ai_helper
        payload = json.dumps({"editions": [
            {"subject": "A", "angle": "1", "format": "deep_dive", "hook_style": "question"},
            {"subject": "B", "angle": "2", "format": "deep_dive", "hook_style": "question"},
            {"subject": "C", "angle": "3", "format": "deep_dive", "hook_style": "question"},
        ]})
        with patch(f"{_AI}._call_llm", return_value=_resp(payload)):
            out = ai_helper.plan_newsletter_topics("v", "d", None, [], 3)
        for a, b in zip(out, out[1:]):
            assert a["format"] != b["format"]
            assert a["hook_style"] != b["hook_style"]

    def test_recent_shapes_in_prompt_and_avoided(self):
        from cqc_lem.utilities.ai import ai_helper
        payload = json.dumps({"editions": [{"subject": "A", "angle": "1", "format": "deep_dive",
                                            "hook_style": "question"}]})
        with patch(f"{_AI}._call_llm", return_value=_resp(payload)) as call:
            out = ai_helper.plan_newsletter_topics(
                "v", "d", None, [], 1, recent_formats=["deep_dive", "framework"],
                recent_hook_styles=["question"])
        prompt = call.call_args.kwargs["messages"][1]["content"]
        assert "deep_dive" in prompt and "framework" in prompt and "question" in prompt
        # Model repeated a recent shape; code rotated it away.
        assert out[0]["format"] not in ("deep_dive", "framework")
        assert out[0]["hook_style"] != "question"

    def test_format_menu_present_in_system_prompt(self):
        from cqc_lem.utilities.ai import ai_helper
        from cqc_lem.utilities.ai.content_framework import NEWSLETTER_FORMATS as FORMATS
        payload = json.dumps({"editions": [{"subject": "A", "angle": "1"}]})
        with patch(f"{_AI}._call_llm", return_value=_resp(payload)) as call:
            ai_helper.plan_newsletter_topics("v", "d", None, [], 1)
        system = call.call_args.kwargs["messages"][0]["content"]
        for key in FORMATS:
            assert key in system

    def test_planner_prompt_targets_inbox_and_cta(self):
        from cqc_lem.utilities.ai import ai_helper
        payload = json.dumps({"editions": [{"subject": "A", "angle": "1"}]})
        with patch(f"{_AI}._call_llm", return_value=_resp(payload)) as call:
            ai_helper.plan_newsletter_topics("v", "d", None, [], 1)
        system = call.call_args.kwargs["messages"][0]["content"]
        assert "inbox-worthy title + subtitle" in system
        assert "reply-driving question" in system
        assert "blog alignment has something concrete to track" in system


class TestNewsletterMechanicalEdit:
    """Issue #1079: the final mechanical editor pass.

    Opt-in via the newsletter-editor flag, runs after humanization and before the slop lint,
    and fails open.
    """

    @pytest.fixture(autouse=True)
    def _no_structure_retry(self, monkeypatch):
        # These assert exact LLM call counts. The #1435 structural checking side would spend a
        # regeneration on every two-word stub body here, which is a different test's subject.
        monkeypatch.setenv("NEWSLETTER_STRUCTURE_ENABLED", "off")

    def _profile(self):
        prof = MagicMock()
        prof.model_dump_json.return_value = "{}"
        return prof

    def test_mechanical_edit_skipped_when_flag_disabled(self, monkeypatch):
        monkeypatch.setenv("HUMANIZE_ENABLED", "off")
        from cqc_lem.utilities.ai import ai_helper
        edition_json = json.dumps({"title": "T", "subtitle": "S", "body": "raw body."})
        with patch(f"{_AI}._call_llm", return_value=_resp(edition_json)) as call, \
             patch(f"{_AI}.flag_enabled", return_value=False) as flag:
            edition = ai_helper.generate_newsletter_edition(self._profile(), topic="x")
        assert edition is not None
        assert edition["body"] == "raw body."
        flag.assert_called_with(ai_helper.NEWSLETTER_EDITOR, user_id=None)
        # Exactly one LLM call: the initial draft generation.
        assert call.call_count == 1

    def test_mechanical_edit_runs_when_flag_enabled(self, monkeypatch):
        monkeypatch.setenv("HUMANIZE_ENABLED", "off")
        from cqc_lem.utilities.ai import ai_helper
        edition_json = json.dumps({"title": "T", "subtitle": "S", "body": "raw body."})
        edited = "Raw body."
        with patch(f"{_AI}._call_llm", side_effect=[_resp(edition_json), _resp(edited)]) as call, \
             patch(f"{_AI}.flag_enabled", return_value=True):
            edition = ai_helper.generate_newsletter_edition(self._profile(), topic="x")
        assert edition is not None
        assert edition["body"] == edited
        # Two LLM calls: draft generation + mechanical edit.
        assert call.call_count == 2
        assert call.call_args.kwargs.get("model") == "lem-medium"

    def test_mechanical_edit_runs_before_slop_lint(self, monkeypatch):
        monkeypatch.setenv("HUMANIZE_ENABLED", "off")
        from cqc_lem.utilities.ai import ai_helper
        edition_json = json.dumps({"title": "T", "subtitle": "S", "body": "raw body."})
        edited = "Edited body."
        seen = []
        with patch(f"{_AI}._call_llm", side_effect=[_resp(edition_json), _resp(edited)]) as call, \
             patch(f"{_AI}.flag_enabled", return_value=True), \
             patch(f"{_AI}._slop.lint_report") as lint:
            ai_helper.generate_newsletter_edition(self._profile(), topic="x")
        # The mechanical-edit call is the second _call_llm invocation.
        assert call.call_count == 2
        mechanical_body = call.call_args_list[1].kwargs["messages"][1]["content"]
        seen.append(mechanical_body)
        # The slop lint was called at least once with the edited body.
        lint.assert_called()
        linted_body = lint.call_args[0][0]
        assert linted_body == edited

    def test_mechanical_edit_fails_open_and_uses_original_body(self, monkeypatch):
        monkeypatch.setenv("HUMANIZE_ENABLED", "off")
        from cqc_lem.utilities.ai import ai_helper
        edition_json = json.dumps({"title": "T", "subtitle": "S", "body": "raw body."})
        with patch(f"{_AI}._call_llm", side_effect=[_resp(edition_json), Exception("boom")]), \
             patch(f"{_AI}.flag_enabled", return_value=True):
            edition = ai_helper.generate_newsletter_edition(self._profile(), topic="x")
        assert edition is not None
        assert edition["body"] == "raw body."


class TestStructuralLabelsStrippedFromBody:
    """#1284. The blueprint hands the writer section names; three of the five PUBLISHED editions in
    the real corpus shipped a bare "CTA" line above their closing ask, visible to subscribers.
    """

    def _body(self, body):
        from cqc_lem.utilities.ai import ai_helper
        prof = MagicMock(); prof.model_dump_json.return_value = "{}"
        payload = json.dumps({"title": "T", "subtitle": "S", "subject": "S", "body": body})
        with patch(f"{_AI}._call_llm", return_value=_resp(payload)):
            return ai_helper.generate_newsletter_edition(prof)["body"]

    def test_bare_cta_label_line_is_dropped(self):
        out = self._body("The opener.\n\nA developed paragraph.\n\nCTA\n\nWhat did you try?")
        assert "\nCTA" not in out and not out.startswith("CTA")
        assert "What did you try?" in out
        assert "A developed paragraph." in out

    def test_decorated_and_numbered_labels_are_dropped(self):
        out = self._body("Opener.\n\n- HOOK:\n\nReal line.\n\nSection 2\n\nMore.\n\nConclusion\n\nEnd.")
        lines = [line.strip().lower() for line in out.split("\n") if line.strip()]
        assert "hook:" not in lines and "section 2" not in lines and "conclusion" not in lines
        assert "real line." in lines and "end." in lines

    def test_reader_facing_heading_is_kept(self):
        out = self._body("Opener.\n\nKEY TAKEAWAYS\n\nOne thing worth keeping.")
        assert "KEY TAKEAWAYS" in out

    def test_a_label_word_inside_a_sentence_survives(self):
        out = self._body("Opener.\n\nEvery CTA should earn its place in the body.")
        assert "Every CTA should earn its place in the body." in out

    def test_a_one_word_sentence_is_prose_not_a_label(self):
        # A terminal period says a reader reads this line. Stripping it first turned "Intro." into
        # the label "intro" and deleted the line from the published edition.
        out = self._body("Intro.\n\nA developed paragraph.\n\nClose.\n\nWhat did you try?")
        assert "Intro." in out and "Close." in out

    def test_removing_a_label_does_not_leave_an_empty_paragraph(self):
        # The article editor renders the extra newline, so the gap the label left is visible.
        out = self._body("The opener.\n\nA developed paragraph.\n\nCTA\n\nWhat did you try?")
        assert "\n\n\n" not in out


class TestNewsletterStructuralFloor:
    """#1284. The measured gaps in the real corpus: 7/10 editions opened past the fold, 9/10 carried
    a wall-of-text paragraph, 6/10 had no list block, 7/10 undershot the 800-word floor, and 8/10
    closed by routing the reader to a comments box.
    """

    def test_directive_states_the_measured_floors(self):
        from cqc_lem.utilities.ai.content_framework import (
            DWELL_PARAGRAPH_MAX_CHARS,
            LINKEDIN_FOLD_CHARS,
            NEWSLETTER_WORD_CEILING,
            NEWSLETTER_WORD_FLOOR,
            newsletter_writing_directive,
        )
        directive = newsletter_writing_directive()
        assert str(LINKEDIN_FOLD_CHARS) in directive
        assert str(DWELL_PARAGRAPH_MAX_CHARS) in directive
        assert f"{NEWSLETTER_WORD_FLOOR}-{NEWSLETTER_WORD_CEILING}" in directive
        assert "numbered or bulleted block" in directive

    def test_directive_bans_the_comments_box_in_any_wording(self):
        from cqc_lem.utilities.ai.content_framework import newsletter_writing_directive
        directive = newsletter_writing_directive().lower()
        assert "reply" in directive
        assert "in the comments" in directive and "never route the reader to a comments box" in directive

    def test_directive_names_the_same_labels_the_cleaner_strips(self):
        from cqc_lem.utilities.ai.content_framework import (
            NEWSLETTER_STRUCTURAL_LABELS,
            newsletter_writing_directive,
        )
        directive = newsletter_writing_directive()
        # The writer side and the cleaning side must name one list, the POST_BANNED_SCAFFOLDS rule.
        for label in ("cta", "hook", "intro", "body", "conclusion"):
            assert label in NEWSLETTER_STRUCTURAL_LABELS
            assert label.upper() in directive
        assert "key takeaways" not in NEWSLETTER_STRUCTURAL_LABELS


def _clean_edition_body(words: int = 900) -> str:
    """A body that clears every structural floor.

    A short opening line, short paragraphs, a list block, and a word count inside the 800-1200 band.
    """
    paragraph = "We shipped the change and the number moved. " * 5   # ~35 words, ~220 chars
    filler = []
    total = len(paragraph.split()) + 20
    while total < words:
        filler.append(paragraph.strip())
        total += len(paragraph.split())
    return ("A short opening line that stands alone.\n\n"
            + "\n\n".join(filler)
            + "\n\nKEY TAKEAWAYS\n\n1. Measure the thing.\n2. Then change it.\n3. Measure again.\n\n"
              "What did you change last week?")


class TestNewsletterStructureReport:
    """#1435. The CHECKING half of the #1284 floor.

    The same four measurements, read off the existing `dwell_report()`, never a parallel grader.
    """

    def test_clean_body_passes_with_no_failures(self):
        from cqc_lem.utilities.ai.content_framework import newsletter_structure_report
        report = newsletter_structure_report(_clean_edition_body())
        assert report["checked"] is True
        assert report["passes"] is True, report["failures"]
        assert report["failures"] == []

    def test_long_opening_line_is_named_with_its_measurement(self):
        from cqc_lem.utilities.ai.content_framework import (
            LINKEDIN_FOLD_CHARS,
            NEWSLETTER_STRUCTURE_CHECK_FOLD,
            newsletter_structure_report,
        )
        body = ("Opening. " * 40).strip() + "\n\n" + _clean_edition_body()
        report = newsletter_structure_report(body)
        failure = next(f for f in report["failures"] if f["check"] == NEWSLETTER_STRUCTURE_CHECK_FOLD)
        assert str(LINKEDIN_FOLD_CHARS) in failure["detail"]
        assert "-character line" in failure["detail"]

    def test_wall_of_text_paragraph_is_named_with_its_length(self):
        from cqc_lem.utilities.ai.content_framework import (
            DWELL_PARAGRAPH_MAX_CHARS,
            NEWSLETTER_STRUCTURE_CHECK_WALL,
            newsletter_structure_report,
        )
        wall = "This paragraph keeps going and going without a break. " * 12
        report = newsletter_structure_report(_clean_edition_body() + "\n\n" + wall)
        failure = next(f for f in report["failures"] if f["check"] == NEWSLETTER_STRUCTURE_CHECK_WALL)
        assert str(len(wall.strip())) in failure["detail"]
        assert str(DWELL_PARAGRAPH_MAX_CHARS) in failure["detail"]

    def test_missing_list_block_is_a_failure(self):
        from cqc_lem.utilities.ai.content_framework import (
            NEWSLETTER_STRUCTURE_CHECK_LIST,
            newsletter_structure_report,
        )
        body = _clean_edition_body().replace("1. Measure the thing.", "Measure the thing.") \
                                   .replace("2. Then change it.", "Then change it.") \
                                   .replace("3. Measure again.", "Measure again.")
        checks = [f["check"] for f in newsletter_structure_report(body)["failures"]]
        assert NEWSLETTER_STRUCTURE_CHECK_LIST in checks

    def test_word_band_is_the_newsletter_band_not_the_dwell_target(self):
        from cqc_lem.utilities.ai.content_framework import (
            DWELL_TARGET_WORDS_MAX,
            NEWSLETTER_STRUCTURE_CHECK_WORDS,
            NEWSLETTER_WORD_FLOOR,
            newsletter_structure_report,
        )
        # A body inside the dwell grader's own 180-400 word post target is still SHORT for an
        # edition — the one threshold this report may not inherit.
        short = _clean_edition_body(words=DWELL_TARGET_WORDS_MAX)
        failure = next(f for f in newsletter_structure_report(short)["failures"]
                       if f["check"] == NEWSLETTER_STRUCTURE_CHECK_WORDS)
        assert str(NEWSLETTER_WORD_FLOOR) in failure["detail"]
        long_body = _clean_edition_body(words=1500)
        assert any(f["check"] == NEWSLETTER_STRUCTURE_CHECK_WORDS
                   for f in newsletter_structure_report(long_body)["failures"])

    def test_metrics_come_from_the_existing_dwell_report(self):
        from cqc_lem.utilities.ai import content_framework as fw
        body = _clean_edition_body()
        report = fw.newsletter_structure_report(body)
        assert report["metrics"] == fw.dwell_report(body)["metrics"]
        assert report["dwell_score"] == fw.dwell_report(body)["score"]

    def test_empty_body_is_checked_false_and_passing(self):
        from cqc_lem.utilities.ai.content_framework import newsletter_structure_report
        for empty in (None, "", "   "):
            report = newsletter_structure_report(empty)
            assert report["checked"] is False and report["passes"] is True

    def test_disabled_by_env_fails_open(self, monkeypatch):
        from cqc_lem.utilities.ai.content_framework import newsletter_structure_report
        monkeypatch.setenv("NEWSLETTER_STRUCTURE_ENABLED", "off")
        report = newsletter_structure_report("One wall. " * 60)
        assert report["checked"] is False and report["passes"] is True and report["failures"] == []

    def test_the_arrow_list_the_writer_prompt_asks_for_counts_as_a_list_block(self):
        """The grader must see the marker the WRITER contract mandates, or it burns a shared draft.

        `generate_newsletter_edition`'s format rules ask for list items "beginning with a literal
        '-> ' or a bullet character", and `sanitize_for_linkedin` only rewrites `- `/`* ` into a
        bullet — so an arrow list survives to the grader verbatim.
        """
        from cqc_lem.utilities.ai.content_framework import (
            NEWSLETTER_STRUCTURE_CHECK_LIST,
            newsletter_structure_report,
        )
        from cqc_lem.utilities.linkedin_formatter import sanitize_for_linkedin
        body = _clean_edition_body().replace(
            "1. Measure the thing.\n2. Then change it.\n3. Measure again.",
            "-> Measure the thing.\n-> Then change it.\n-> Measure again.")
        assert "-> Measure the thing." in sanitize_for_linkedin(body)
        checks = [f["check"] for f in newsletter_structure_report(body)["failures"]]
        assert NEWSLETTER_STRUCTURE_CHECK_LIST not in checks


class TestNewsletterWallRepairIsDeterministic:
    """#1435. The one failure a reflow can fix is never spent on a generation.

    Asking a retry to split its paragraphs AND hold 800-1200 words traded the second for the first
    on this change's own A/B (mean 768 -> 565 words), so the paragraphs are reflowed in code.
    """

    def test_a_wall_paragraph_is_split_below_the_ceiling(self):
        from cqc_lem.utilities.ai.content_framework import (
            DWELL_PARAGRAPH_MAX_CHARS,
            dwell_metrics,
            newsletter_shape_body,
        )
        wall = "The deploy took eleven minutes and nobody could say why. " * 12
        shaped = newsletter_shape_body(wall)
        assert dwell_metrics(shaped)["longest_paragraph_chars"] <= DWELL_PARAGRAPH_MAX_CHARS

    def test_nothing_is_trimmed_from_a_long_edition(self):
        from cqc_lem.utilities.ai.content_framework import newsletter_shape_body
        # `shape_for_dwell` caps a POST at LinkedIn's 3000-char post limit. An edition is far longer
        # than that and must come back whole — the trim branch has to be unreachable here.
        wall = "We measured it, then we changed it, then we measured it again. " * 90
        shaped = newsletter_shape_body(wall)
        assert len(shaped.split()) == len(wall.split())
        assert shaped.split()[-1] == wall.split()[-1]

    def test_a_scannable_body_is_returned_unchanged(self):
        from cqc_lem.utilities.ai.content_framework import newsletter_shape_body
        body = _clean_edition_body()
        assert newsletter_shape_body(body) == body

    def test_a_list_block_is_left_alone(self):
        from cqc_lem.utilities.ai.content_framework import newsletter_shape_body
        wall = "One long paragraph that runs on and on without any break at all. " * 6
        body = wall + "\n\n1. First step.\n2. Second step.\n3. Third step."
        shaped = newsletter_shape_body(body)
        assert "1. First step.\n2. Second step.\n3. Third step." in shaped

    def test_falsy_input_is_returned_unchanged(self):
        from cqc_lem.utilities.ai.content_framework import newsletter_shape_body
        assert newsletter_shape_body("") == ""
        assert newsletter_shape_body(None) is None

    def test_a_generated_wall_is_reflowed_and_the_opening_line_re_derived(self, monkeypatch):
        monkeypatch.setenv("HUMANIZE_ENABLED", "off")
        from cqc_lem.utilities.ai import ai_helper
        from cqc_lem.utilities.ai.content_framework import DWELL_PARAGRAPH_MAX_CHARS, dwell_metrics
        wall = "The deploy took eleven minutes and nobody could say why. " * 12
        payload = json.dumps({"title": "T", "subtitle": "S", "subject": "S", "body": wall})
        prof = MagicMock()
        prof.model_dump_json.return_value = "{}"
        with patch(f"{_AI}._call_llm", return_value=_resp(payload)), \
             patch(f"{_AI}.flag_enabled", return_value=False):
            edition = ai_helper.generate_newsletter_edition(prof, topic="x")
        assert dwell_metrics(edition["body"])["longest_paragraph_chars"] <= DWELL_PARAGRAPH_MAX_CHARS
        assert edition["opening_line"] == edition["body"].splitlines()[0]

    def test_the_reflow_is_off_when_the_checking_side_is(self, monkeypatch):
        monkeypatch.setenv("HUMANIZE_ENABLED", "off")
        monkeypatch.setenv("NEWSLETTER_STRUCTURE_ENABLED", "off")
        from cqc_lem.utilities.ai import ai_helper
        wall = "The deploy took eleven minutes and nobody could say why. " * 12
        payload = json.dumps({"title": "T", "subtitle": "S", "subject": "S", "body": wall.strip()})
        prof = MagicMock()
        prof.model_dump_json.return_value = "{}"
        with patch(f"{_AI}._call_llm", return_value=_resp(payload)), \
             patch(f"{_AI}.flag_enabled", return_value=False):
            edition = ai_helper.generate_newsletter_edition(prof, topic="x")
        assert edition["body"] == wall.strip()


class TestNewsletterStructureDirective:
    """The retry steer names the measured number and the concrete repair, never a full rewrite."""

    def test_directive_names_each_failure_and_its_repair(self):
        from cqc_lem.utilities.ai.content_framework import (
            DWELL_PARAGRAPH_MAX_CHARS,
            newsletter_structure_directive,
            newsletter_structure_report,
        )
        wall = "This paragraph keeps going and going without a break. " * 12
        report = newsletter_structure_report(_clean_edition_body() + "\n\n" + wall)
        directive = newsletter_structure_directive(report["failures"])
        assert str(len(wall.strip())) in directive
        assert f"{DWELL_PARAGRAPH_MAX_CHARS} characters" in directive
        assert "Split every long one" in directive
        assert "Keep the same subject, argument and facts" in directive

    def test_no_failures_is_the_empty_string(self):
        from cqc_lem.utilities.ai.content_framework import newsletter_structure_directive
        assert newsletter_structure_directive([]) == ""
        assert newsletter_structure_directive(None) == ""

    def test_unknown_or_malformed_failures_are_ignored(self):
        from cqc_lem.utilities.ai.content_framework import newsletter_structure_directive
        assert newsletter_structure_directive([{"check": "made_up", "detail": "x"}, "junk", {}]) == ""

    def test_reasons_read_like_the_slop_lint_reasons(self):
        from cqc_lem.utilities.ai.content_framework import (
            NEWSLETTER_STRUCTURE_CHECK_LIST,
            newsletter_structure_reasons,
        )
        reasons = newsletter_structure_reasons(
            [{"check": NEWSLETTER_STRUCTURE_CHECK_LIST, "detail": "carries no block"}, {"check": "x"}])
        assert reasons == [f"{NEWSLETTER_STRUCTURE_CHECK_LIST}: carries no block"]


class TestStructureFailuresFeedTheBoundedRegeneration:
    """#1435 acceptance: the failures ride the SAME bounded regeneration the slop lint uses.

    Nothing here may hold or pause an edition.
    """

    def _profile(self):
        prof = MagicMock()
        prof.model_dump_json.return_value = "{}"
        return prof

    def _payload(self, body):
        return json.dumps({"title": "T", "subtitle": "S", "subject": "S", "body": body})

    def test_a_structurally_short_edition_is_regenerated_once(self, monkeypatch):
        monkeypatch.setenv("HUMANIZE_ENABLED", "off")
        from cqc_lem.utilities.ai import ai_helper
        good = _clean_edition_body()
        with patch(f"{_AI}._call_llm",
                   side_effect=[_resp(self._payload("Too short.")),
                                _resp(self._payload(good))]) as call, \
             patch(f"{_AI}.flag_enabled", return_value=False):
            edition = ai_helper.generate_newsletter_edition(self._profile(), topic="x")
        assert call.call_count == 2
        assert edition["body"].startswith("A short opening line")

    def test_the_retry_directive_carries_the_structural_failures(self, monkeypatch):
        monkeypatch.setenv("HUMANIZE_ENABLED", "off")
        from cqc_lem.utilities.ai import ai_helper
        with patch(f"{_AI}._call_llm",
                   side_effect=[_resp(self._payload("Too short.")),
                                _resp(self._payload(_clean_edition_body()))]) as call, \
             patch(f"{_AI}.flag_enabled", return_value=False):
            ai_helper.generate_newsletter_edition(self._profile(), topic="x")
        retry_system = call.call_args_list[1].kwargs["messages"][0]["content"]
        assert "MISSED THE STRUCTURAL FLOOR" in retry_system
        assert "outside the 800-1200 band" in retry_system

    def test_a_clean_edition_spends_no_extra_draft(self, monkeypatch):
        monkeypatch.setenv("HUMANIZE_ENABLED", "off")
        from cqc_lem.utilities.ai import ai_helper
        with patch(f"{_AI}._call_llm",
                   return_value=_resp(self._payload(_clean_edition_body()))) as call, \
             patch(f"{_AI}.flag_enabled", return_value=False):
            edition = ai_helper.generate_newsletter_edition(self._profile(), topic="x")
        assert call.call_count == 1
        assert edition is not None

    def test_a_still_failing_edition_is_returned_not_held(self, monkeypatch, caplog):
        monkeypatch.setenv("HUMANIZE_ENABLED", "off")
        from cqc_lem.utilities.ai import ai_helper
        with patch(f"{_AI}._call_llm", return_value=_resp(self._payload("Still too short."))), \
             patch(f"{_AI}.flag_enabled", return_value=False):
            edition = ai_helper.generate_newsletter_edition(self._profile(), topic="x")
        assert edition is not None and edition["body"] == "Still too short."
        assert "structural floor" in caplog.text
        assert "newsletter_word_band" in caplog.text

    def test_attempts_are_capped_by_the_shared_slop_budget(self, monkeypatch):
        monkeypatch.setenv("HUMANIZE_ENABLED", "off")
        monkeypatch.setenv("SLOP_LINT_MAX_ATTEMPTS", "3")
        from cqc_lem.utilities.ai import ai_helper
        with patch(f"{_AI}._call_llm", return_value=_resp(self._payload("Still too short."))) as call, \
             patch(f"{_AI}.flag_enabled", return_value=False):
            ai_helper.generate_newsletter_edition(self._profile(), topic="x")
        # Initial draft + at most (attempts - 1) regenerations — one budget for both graders.
        assert call.call_count == 3

    def test_disabled_checking_side_restores_the_prior_behaviour(self, monkeypatch):
        monkeypatch.setenv("HUMANIZE_ENABLED", "off")
        monkeypatch.setenv("NEWSLETTER_STRUCTURE_ENABLED", "off")
        from cqc_lem.utilities.ai import ai_helper
        with patch(f"{_AI}._call_llm", return_value=_resp(self._payload("Too short."))) as call, \
             patch(f"{_AI}.flag_enabled", return_value=False):
            edition = ai_helper.generate_newsletter_edition(self._profile(), topic="x")
        assert call.call_count == 1 and edition["body"] == "Too short."


class TestNewsletterSlopRetry:
    """The bounded regeneration's own behaviour (issue #1434).

    The retry is a fresh full draft, not an edit, so it can come back carrying MORE than the draft
    it replaced — and the stored edition shows only what was still firing at the end, which is why
    the outcome of each regeneration is recorded rather than inferred later.

    The structural floor (#1435) shares this budget and is graded off the same body, so it is turned
    OFF here: these drafts are short by design (one sentence carrying one violation), and leaving the
    other grader on would fail every one of them for word count and stop the slop lint being what
    decided anything. `TestStructureFailuresFeedTheBoundedRegeneration` covers the shared budget.
    """

    @pytest.fixture(autouse=True)
    def _structure_off(self, monkeypatch):
        monkeypatch.setenv("NEWSLETTER_STRUCTURE_ENABLED", "off")

    # One HARD check (contrastive_frame).
    _ONE_HARD = ("It is not just tooling, it is a mindset. We shipped the importer on a Tuesday "
                 "and it held.")
    # The same frame PLUS a lexicon pileup — strictly worse than the draft it would replace.
    _TWO_HARD = ("It is not just tooling, it is a mindset. We leverage a robust ecosystem to "
                 "unlock value.")
    _CLEAN = "We shipped the importer on a Tuesday morning and it held through the quarter close."

    def _edition(self, body):
        return _resp(json.dumps({"title": "A specific title", "subtitle": "why to read it",
                                 "subject": "the subject", "body": body}))

    def _run(self, bodies, **kwargs):
        from cqc_lem.utilities.ai import ai_helper
        prof = MagicMock()
        prof.model_dump_json.return_value = "{}"
        with patch(f"{_AI}._call_llm", side_effect=[self._edition(b) for b in bodies]) as call, \
             patch(f"{_AI}.track_slop_retry") as track:
            out = ai_helper.generate_newsletter_edition(prof, topic="ops", **kwargs)
        return out, call, track

    def test_a_retry_that_came_back_worse_is_not_kept(self):
        out, call, _ = self._run([self._ONE_HARD, self._TWO_HARD])
        assert call.call_count == 2
        assert out["body"] == self._ONE_HARD, (
            "the newer draft carried both violations — keeping it ships the worse of the two")

    def test_a_retry_that_cleared_the_check_is_kept(self):
        out, call, _ = self._run([self._ONE_HARD, self._CLEAN])
        assert call.call_count == 2 and out["body"] == self._CLEAN

    def test_a_clean_first_draft_costs_one_call_and_reports_no_retry(self):
        out, call, track = self._run([self._CLEAN])
        assert call.call_count == 1 and out["body"] == self._CLEAN
        track.assert_not_called()

    def test_the_outcome_of_each_regeneration_is_recorded(self):
        from cqc_lem.utilities.ai import slop_lint as _slop
        _, _, track = self._run([self._ONE_HARD, self._CLEAN])
        assert track.call_args.args[0] == "newsletter"
        assert track.call_args.args[1] == _slop.RETRY_CLEARED
        assert track.call_args.kwargs["attempt"] == 2
        assert track.call_args.kwargs["max_attempts"] == 2

    def test_a_rewrite_that_adds_a_violation_is_recorded_as_worsened(self):
        from cqc_lem.utilities.ai import slop_lint as _slop
        _, _, track = self._run([self._ONE_HARD, self._TWO_HARD])
        assert track.call_args.args[1] == _slop.RETRY_WORSENED

    def test_whether_the_retry_survived_travels_with_the_outcome(self):
        # The event grades the regeneration; `kept` is what says whether the call bought anything,
        # and a discarded draft's outcome would otherwise read as the edition that shipped.
        _, _, kept_track = self._run([self._ONE_HARD, self._CLEAN])
        assert kept_track.call_args.kwargs["kept"] is True
        _, _, dropped_track = self._run([self._ONE_HARD, self._TWO_HARD])
        assert dropped_track.call_args.kwargs["kept"] is False

    def test_an_empty_regeneration_is_never_recorded_as_kept(self):
        from cqc_lem.utilities.ai import ai_helper
        prof = MagicMock()
        prof.model_dump_json.return_value = "{}"
        with patch(f"{_AI}._call_llm", side_effect=[self._edition(self._ONE_HARD), _resp("")]), \
             patch(f"{_AI}.track_slop_retry") as track:
            ai_helper.generate_newsletter_edition(prof, topic="ops")
        assert track.call_args.kwargs["kept"] is False

    def test_an_empty_regeneration_is_recorded_and_keeps_the_first_draft(self):
        from cqc_lem.utilities.ai import ai_helper, slop_lint as _slop
        prof = MagicMock()
        prof.model_dump_json.return_value = "{}"
        empty = _resp("")
        with patch(f"{_AI}._call_llm", side_effect=[self._edition(self._ONE_HARD), empty]), \
             patch(f"{_AI}.track_slop_retry") as track:
            out = ai_helper.generate_newsletter_edition(prof, topic="ops")
        assert out["body"] == self._ONE_HARD
        assert track.call_args.args[1] == _slop.RETRY_LOST

    def test_a_still_failing_edition_is_returned_with_the_patterns_named(self):
        from cqc_lem.utilities.ai import ai_helper
        prof = MagicMock()
        prof.model_dump_json.return_value = "{}"
        with patch(f"{_AI}._call_llm",
                   side_effect=[self._edition(self._ONE_HARD)] * 2), \
             patch(f"{_AI}.track_slop_retry"), patch(f"{_AI}.log_warning") as warn:
            out = ai_helper.generate_newsletter_edition(prof, topic="ops")
        assert out["body"] == self._ONE_HARD
        assert "contrastive_frame" in warn.call_args.args[0]

    def test_the_newsletter_attempt_budget_is_its_own(self, monkeypatch):
        monkeypatch.setenv("SLOP_LINT_MAX_ATTEMPTS_NEWSLETTER", "3")
        _, call, track = self._run([self._ONE_HARD] * 3)
        assert call.call_count == 3
        assert [c.kwargs["max_attempts"] for c in track.call_args_list] == [3, 3]
        assert [c.kwargs["attempt"] for c in track.call_args_list] == [2, 3]
