"""#2241 showcase C: the feed text beside headline-free images, quote cards, never bare, curated text.

Each class pins one engine rule round C broke, with the exact text that broke it.
"""

import dataclasses
from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip("PIL")

from PIL import Image  # noqa: E402

from cqc_lem.utilities.ai import (
    curated_commentary as ccm,  # noqa: E402
    image_brief as ib,  # noqa: E402
    image_gen,  # noqa: E402
)
from cqc_lem.utilities.ai.content_framework import LINKEDIN_FOLD_CHARS, feed_fold_text  # noqa: E402
from cqc_lem.utilities.curated_sources import escape_little_text  # noqa: E402
from tests.unit.utilities.test_post_rhythm import (  # noqa: E402
    _REPORT,
    _concept,
    _Env,
    _force_chain,
)

pytestmark = pytest.mark.unit

POST_130 = ("Imagine running your whole deployment on a cheap VPS and still pushing four solid "
            "releases a day—no downtime, no Kubernetes.  \n\nOn July 26, 2026 I switched my "
            "production stack to a blue/green rollout with Docker Compose on a low-cost VPS.")


class TestFeedFoldText:
    def test_two_lines_within_the_fold(self):
        assert feed_fold_text("Hook line.\n\nSecond line.\n\nThird.") == "Hook line. Second line."

    def test_a_long_opening_is_cut_at_a_word_and_marked(self):
        fold = feed_fold_text("word " * 100)
        assert fold.endswith("…") and len(fold) <= LINKEDIN_FOLD_CHARS + 1

    def test_empty(self):
        assert feed_fold_text("") == "" and feed_fold_text(None) == ""


def _two_calls(rubric_json: str):
    blind = MagicMock(choices=[MagicMock(message=MagicMock(content="A snail with letters."))])
    targeted = MagicMock(choices=[MagicMock(message=MagicMock(content=rubric_json))])
    return [blind, targeted]


_RUBRIC = ('{"rubric": {"specificity": 3, "no_cliche": 5, "thumbnail_read": 4, "craft": 4, '
           '"scroll_stop": 4, "brand_fit": 4}}')


class TestFeedContextReachesOnlyTheTargetedJudge:
    def _inspect(self, tmp_path, feed):
        path = str(tmp_path / "r.png")
        Image.new("RGB", (32, 32)).save(path)
        with patch.object(image_gen.client.chat.completions, "create",
                          side_effect=_two_calls(_RUBRIC)) as create:
            verdict = image_gen.inspect_render_quality(path, "x", "post_image",
                                                       concept=_concept(_REPORT),
                                                       feed_context=feed)
        blind_text = create.call_args_list[0].kwargs["messages"][0]["content"][0]["text"]
        targeted_text = create.call_args_list[1].kwargs["messages"][0]["content"][0]["text"]
        return verdict, blind_text, targeted_text

    def test_the_feed_text_reshapes_specificity_and_never_reaches_the_blind_look(self, tmp_path):
        feed = "Our snail-mail support queue cost us weekends."
        verdict, blind, targeted = self._inspect(tmp_path, feed)
        assert feed not in blind
        assert feed in targeted
        assert "A reader sees this text first" in targeted
        assert "the pair reads as one idea within 2 seconds" in targeted
        assert verdict.acceptable  # #2270's floors still hold: specificity 3, scroll_stop 4

    def test_without_feed_text_the_headline_rule_stands(self, tmp_path):
        _verdict, _blind, targeted = self._inspect(tmp_path, None)
        assert "A reader sees this text first" not in targeted
        assert "with its headline, a scroller would correctly guess" in targeted

    def test_the_base_gated_renderer_threads_it(self, tmp_path):
        raw = str(tmp_path / "raw.png")
        Image.new("RGB", (1024, 1024)).save(raw)
        with patch.object(image_gen, "_render_with_backend", return_value=(raw, "gpt-image")), \
                patch.object(image_gen, "inspect_render_quality",
                             return_value=image_gen.QualityVerdict(acceptable=True)) as judge, \
                patch("cqc_lem.utilities.observability.track_image_gate_verdict"):
            image_gen.render_image_gated("p", surface="video", concept=_concept(_REPORT),
                                         feed_context="FOLD", enforce=True)
        assert judge.call_args.kwargs["feed_context"] == "FOLD"

    @pytest.mark.parametrize("avatar", [None, {"id": 1}])
    def test_generate_post_image_threads_it_on_both_branches(self, avatar):
        from cqc_lem.utilities.ai import ai_helper

        with patch("cqc_lem.utilities.avatar.guardrails.resolve_avatar_for", return_value=avatar), \
                patch.object(image_gen, "render_avatar_image_gated", return_value="a") as av, \
                patch.object(image_gen, "render_image_gated", return_value="b") as base:
            ai_helper.generate_post_image("p", 1, surface="video", feed_context="FOLD")
        used = av if avatar else base
        assert used.call_args.kwargs["feed_context"] == "FOLD"

    def test_a_photo_only_post_is_judged_with_its_fold_and_a_card_with_its_headline(self,
                                                                                  tmp_path):
        calls = []
        env = _Env(tmp_path)
        real = image_gen.QualityVerdict(acceptable=True)

        def judge(*args, **kwargs):
            calls.append(kwargs.get("feed_context"))
            return real

        env.post(_REPORT, **_force_chain("photo_only"),
                 **{"cqc_lem.utilities.ai.image_gen.inspect_render_quality":
                    {"side_effect": judge}})
        env.post(_REPORT, **_force_chain("typeset_card"),
                 **{"cqc_lem.utilities.ai.image_gen.inspect_render_quality":
                    {"side_effect": judge}})
        assert calls[0] == feed_fold_text(_REPORT)
        assert calls[-1] is None


class TestNeverBare:
    def test_post_130_with_no_stage_1_hook_ships_the_card(self, tmp_path):
        # Round C: no headline survived Stage 1 and the opening is 19 words, so the old 12-word
        # precondition left the last resort with nothing to set — the post shipped bare.
        hookless = dataclasses.replace(_concept(POST_130), hook_phrase="", hook_options=None)
        rejected = image_gen.QualityVerdict(acceptable=False, rubric={"specificity": 2},
                                            failing=["specificity"], issues=["specificity 2/5"])
        env = _Env(tmp_path, verdict=rejected, analyze=lambda text, **_: hookless)
        url, reason, receipt = env.post(POST_130, **_force_chain("photo_only"))
        assert url and reason is None
        assert receipt["gate_verdict"] == "last_resort"
        assert receipt["hook_text"] == ("Imagine running your whole deployment on a cheap VPS "
                                        "and still pushing four solid releases a day")

    def test_a_crashing_card_still_never_takes_the_post_down(self, tmp_path):
        env = _Env(tmp_path)
        url, reason, _ = env.post(
            _REPORT, **_force_chain("photo_only"),
            **{"cqc_lem.utilities.ai.image_brief.build_image_brief": {
                "side_effect": RuntimeError("author down")},
               "cqc_lem.utilities.ai.image_graphics.render_typeset_card": {
                "side_effect": KeyError("boom")}})
        assert url is None and reason


class TestQuoteCardNotPopGated:
    def test_structural_only(self):
        assert "quote_card" in image_gen.STRUCTURAL_ONLY_ARCHETYPES


class TestVideoAccentReachesTheFinalPrompt:
    @pytest.mark.parametrize("avatar", [None, {"trigger_word": "TOK", "subject": "a man"}])
    def test_every_video_brief_names_a_gold_or_charcoal_accent(self, avatar):
        bare = ib.ImageBrief(prompt="A candid photo of an operations lead at a desk.", ratio="1:1",
                             surface="video", style_preset="video", focal_concept="x")
        with patch.object(ib, "_author_image_brief", return_value=bare) as author:
            brief = ib.build_image_brief("post", surface="video", avatar=avatar,
                                         brand_kit="light gold (#e9d437) against charcoal "
                                                   "(#1f1f1f) and off-white (#f7f5ef)")
        assert author.call_args.kwargs["avatar"] == avatar
        assert "gold accent object or wardrobe piece" in brief.prompt

    def test_a_prompt_that_already_names_one_is_untouched(self):
        prompt = "A lead in a charcoal blazer reviewing a plan."
        assert ib.video_accent_backstop(prompt) == prompt

    def test_other_surfaces_are_untouched(self):
        # The prompt names its backdrop, so round 8's palette backstop has nothing to add either.
        prompt = "A desk against a warm off-white wall."
        bare = ib.ImageBrief(prompt=prompt, ratio="1:1", surface="post_image",
                             style_preset="post_image", focal_concept="x")
        with patch.object(ib, "_author_image_brief", return_value=bare):
            assert ib.build_image_brief("post", surface="post_image").prompt == prompt

    def test_the_real_fallback_path_carries_it_for_an_avatar_frame(self):
        with patch("cqc_lem.utilities.ai.ai_helper._call_llm", side_effect=RuntimeError("down")):
            brief = ib.build_image_brief("We rebuilt our deploys.", surface="video",
                                         avatar={"trigger_word": "TOK"}, concept=None)
        assert brief.fallback and ib._names_a_color(brief.prompt, ib.VIDEO_ACCENT_COLORS)


MARKDOWN_DRAFT = (
    "OpenAI reports that Jump Trading is scaling research with longer AI workflows.\n\n"
    "## Here is the loop\n\n"
    "1. **Define a research question** - pick one.\n"
    "2. **Automate data collection** - pull the logs.\n"
    "3. *Review* the output with a human.\n\n"
    "That loop is the whole lesson for a small team, and it costs nothing to start this week.")


class TestCuratedPlainText:
    def test_markdown_is_stripped_before_escaping(self):
        escaped = escape_little_text(MARKDOWN_DRAFT)
        assert "\\*" not in escaped and "##" not in escaped
        assert "Define a research question" in escaped

    def test_finish_post_text_is_the_text_post_finish(self):
        out = ccm.finish_post_text(MARKDOWN_DRAFT)
        assert "**" not in out and "## " not in out and "*Review*" not in out
        assert "1. Define a research question" in out
        assert ccm.finish_post_text(None) == ""

    def test_a_generated_commentary_ships_plain(self):
        source = {"id": 1, "url": "https://openai.com/index/jump-trading", "publisher": "OpenAI",
                  "title": "How Jump Trading is scaling quant research with ChatGPT",
                  "excerpt": "Jump Trading uses OpenAI to expand quantitative research.",
                  "licence": "editorial"}
        with patch("cqc_lem.utilities.ai.ai_helper.lint_repaired",
                   side_effect=lambda text, *a, **k: text), \
                patch.object(ccm, "_complete", return_value=MARKDOWN_DRAFT):
            out = ccm.generate_curated_commentary(1, source, "link", profile_synthesis="voice")
        assert out and "**" not in out and "Define a research question" in out
