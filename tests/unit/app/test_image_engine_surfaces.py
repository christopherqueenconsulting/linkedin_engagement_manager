"""The staged image engine reaches every surface with a user (issue #2241).

PR #2248 wired Stage 1 + the blind judge into newsletter covers only. These pin the rest: post
images, carousel slides, the video source frame + motion prompt, and the admin variant tool —
each runs Stage 1 ONCE per artifact, briefs with the user's brand clause, and hands the same
concept to the judge. Every LLM, render and DB call is mocked.
"""

import os
from unittest.mock import MagicMock, patch

import pytest

from cqc_lem.utilities.ai.image_brief import ImageBrief
from cqc_lem.utilities.ai.image_concept import ImageConcept

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _no_clip_check(monkeypatch):
    """Off unless a test turns it on: the clip check downloads the clip (#2249)."""
    monkeypatch.setenv("VIDEO_CLIP_CHECK_ENABLED", "false")

_RCP = "cqc_lem.app.run_content_plan"
_BRAND = "Brand palette: light gold (#e9d437) against charcoal (#1f1f1f)."


def _concept(**overrides) -> ImageConcept:
    fields = dict(thesis="Late invoices quietly starve a small agency's cash flow.",
                  audience="agency owners",
                  specific_entities=("unpaid invoices", "agency owner", "bank balance"),
                  emotional_beat="quiet dread turning to resolve", hook_phrase="Paid in 90 days",
                  treatment="concrete_scene", treatment_rationale="a tangible situation")
    fields.update(overrides)
    return ImageConcept(**fields)


def _author_concept(**overrides) -> ImageConcept:
    """A people_scene about the author's OWN decision — the one case the likeness belongs."""
    fields = dict(treatment="people_scene", audience="founders like the author",
                  emotional_beat="the author's relief after finally saying no")
    fields.update(overrides)
    return _concept(**fields)


def _brief(concept=None, hook_text=None) -> ImageBrief:
    return ImageBrief(prompt="a rendered prompt", ratio="4:5", surface="post_image",
                      style_preset="post_image", focal_concept="the focal idea",
                      concept=concept, hook_text=hook_text)


# ── Post images ───────────────────────────────────────────────────────────────

class TestPostImageStagedEngine:
    def _generate(self, tmp_path, monkeypatch, *, avatar=None, concept="default", env_ratio=None,
                  render_info_out=None, lora_effect=None):
        from cqc_lem.utilities.post_image import generate_image_for_post

        # card_share 1.0 with no history: this post's treatment is the typeset card (the
        # composite these tests pin) — the rotation itself is tests/unit/utilities/ai/
        # test_post_treatment.py's.

        if env_ratio is None:
            monkeypatch.delenv("POST_IMAGE_RATIO", raising=False)
        else:
            monkeypatch.setenv("POST_IMAGE_RATIO", env_ratio)
        concept = _concept() if concept == "default" else concept
        rendered = str(tmp_path / "render.png")
        with open(rendered, "wb") as fh:
            fh.write(b"png")
        assets = tmp_path / "assets"
        assets.mkdir(exist_ok=True)

        def _render(*_args, render_info=None, **_kwargs):
            render_info.update(render_info_out or {"gate_verdict": "accepted"})
            return rendered

        with patch("cqc_lem.utilities.post_image.assets_dir", str(assets)), \
             patch("cqc_lem.assets_dir", str(assets)), \
             patch("cqc_lem.utilities.linkedin.helper.load_profile_for_user", return_value=None), \
             patch("cqc_lem.utilities.avatar.guardrails.resolve_avatar_for", return_value=avatar), \
             patch("cqc_lem.utilities.ai.image_concept.analyze_content_for_image",
                   return_value=concept) as stage1, \
             patch("cqc_lem.utilities.brand_kit.brand_clause_for_user",
                   return_value=_BRAND) as brand, \
             patch("cqc_lem.utilities.brand_kit.card_share_for_user", return_value=1.0), \
             patch("cqc_lem.utilities.ai.image_brief.build_image_brief",
                   return_value=_brief(concept, "Paid in 90 days")) as build, \
             patch("cqc_lem.utilities.ai.image_gen.render_image_gated",
                   side_effect=_render) as render, \
             patch("cqc_lem.utilities.ai.image_gen.render_avatar_image_gated",
                   side_effect=lora_effect or _render) as lora:
            result = generate_image_for_post(9, "Post text about invoices", post_id=42)
        return result, stage1, brand, build, render, lora

    def test_stage_one_runs_once_and_its_concept_reaches_brief_and_judge(self, tmp_path,
                                                                         monkeypatch):
        concept = _concept()
        (url, reason), stage1, brand, build, render, _ = self._generate(
            tmp_path, monkeypatch, concept=concept)
        assert reason is None and url
        stage1.assert_called_once()
        assert stage1.call_args[0][0] == "Post text about invoices"
        assert build.call_args[1]["concept"] is concept
        brand.assert_called_once_with(9)
        assert build.call_args[1]["brand_kit"] == _BRAND
        assert render.call_args[1]["concept"] is concept
        assert render.call_args[1]["hook_text"] == "Paid in 90 days"

    def test_a_failed_stage_one_is_passed_on_as_none_never_rerun(self, tmp_path, monkeypatch):
        (url, _), stage1, _, build, _, _ = self._generate(tmp_path, monkeypatch, concept=None)
        # The brief got the caller's answer EXPLICITLY, so it never runs Stage 1 a second time.
        assert "concept" in build.call_args[1] and build.call_args[1]["concept"] is None
        stage1.assert_called_once()
        assert url

    def test_the_avatar_path_is_graded_against_the_concept_too(self, tmp_path, monkeypatch):
        concept = _author_concept()
        avatar = {"model_ref": "owner/lora:v1", "trigger_word": "TOK"}
        (url, _), _, _, _, render, lora = self._generate(tmp_path, monkeypatch, avatar=avatar,
                                                         concept=concept)
        assert url
        render.assert_not_called()
        assert lora.call_args[1]["concept"] is concept
        assert lora.call_args[1]["hook_text"] == "Paid in 90 days"
        assert lora.call_args[1]["ratio"] == "4:5"

    @pytest.mark.parametrize("env_ratio,expected", [(None, "4:5"), ("1:1", "1:1"),
                                                     ("  16:9 ", "16:9"), ("7:3", "4:5")])
    def test_the_ratio_is_read_at_call_time(self, tmp_path, monkeypatch, env_ratio, expected):
        _, _, _, build, render, _ = self._generate(tmp_path, monkeypatch, env_ratio=env_ratio)
        assert build.call_args[1]["ratio"] == expected
        assert render.call_args[1]["ratio"] == expected

    def test_a_post_not_about_the_author_renders_without_the_likeness(self, tmp_path,
                                                                      monkeypatch):
        """#2249 gauntlet: six LoRA renders of posts about invoices and servers, six rejections."""
        avatar = {"model_ref": "owner/lora:v1", "trigger_word": "TOK"}
        (url, _), _, _, build, render, lora = self._generate(tmp_path, monkeypatch, avatar=avatar,
                                                             concept=_concept())
        assert url
        lora.assert_not_called()
        render.assert_called_once()
        assert build.call_args[1]["avatar"] is None

    @pytest.mark.parametrize("failure", ["rejected", "raises", "nothing", "flux_fallback"])
    def test_an_unusable_avatar_render_gets_one_gpt_image_attempt(self, tmp_path, monkeypatch,
                                                                  failure):
        from cqc_lem.utilities.media_provenance import read_brief_receipt
        avatar = {"model_ref": "owner/lora:v1", "trigger_word": "TOK"}
        bad = str(tmp_path / "lora.png")
        with open(bad, "wb") as fh:
            fh.write(b"png")

        def _lora(*_args, render_info=None, **_kwargs):
            if failure == "raises":
                raise RuntimeError("Replicate 500")
            if failure == "nothing":
                return None
            if failure == "flux_fallback":
                render_info.update({"used_avatar": False, "gate_verdict": "unchecked"})
            else:
                render_info.update({"used_avatar": True, "gate_verdict": "rejected"})
            return bad

        with patch("cqc_lem.utilities.post_image.log_info") as info:
            (url, reason), _, _, build, render, lora = self._generate(
                tmp_path, monkeypatch, avatar=avatar, concept=_author_concept(), lora_effect=_lora)
        assert reason is None and url
        lora.assert_called_once()
        render.assert_called_once()
        concept = build.call_args_list[0][1]["concept"]
        # Re-briefed WITHOUT the likeness, on the SAME concept — Stage 1 is not re-run.
        assert build.call_count == 2
        assert build.call_args_list[0][1]["avatar"] == avatar
        assert build.call_args_list[1][1]["avatar"] is None
        assert build.call_args_list[1][1]["concept"] is concept
        assert render.call_args[1]["concept"] is concept
        assert any("one gpt-image attempt" in c.args[0] for c in info.call_args_list)
        with patch("cqc_lem.assets_dir", str(tmp_path / "assets")):
            receipt = read_brief_receipt(url)
        assert receipt["render_path"] == "base_after_avatar"
        assert receipt["avatar_fallback_reason"]

    def test_a_usable_avatar_render_records_its_path(self, tmp_path, monkeypatch):
        from cqc_lem.utilities.media_provenance import read_brief_receipt
        avatar = {"model_ref": "owner/lora:v1", "trigger_word": "TOK"}
        (url, _), _, _, build, render, _ = self._generate(tmp_path, monkeypatch, avatar=avatar,
                                                          concept=_author_concept())
        render.assert_not_called()
        assert build.call_count == 1
        with patch("cqc_lem.assets_dir", str(tmp_path / "assets")):
            receipt = read_brief_receipt(url)
        assert receipt["render_path"] == "avatar" and "avatar_fallback_reason" not in receipt

    def test_the_fallback_that_is_also_rejected_ships_bare(self, tmp_path, monkeypatch):
        from cqc_lem.utilities.post_image import GATE_REJECTED_REASON
        avatar = {"model_ref": "owner/lora:v1", "trigger_word": "TOK"}
        (url, reason), _, _, _, render, _ = self._generate(
            tmp_path, monkeypatch, avatar=avatar, concept=_author_concept(),
            render_info_out={"gate_verdict": "rejected"})
        assert url is None and reason == GATE_REJECTED_REASON
        render.assert_called_once()

    def test_a_failed_fallback_brief_is_reported(self, tmp_path, monkeypatch):
        from cqc_lem.utilities.post_image import generate_image_for_post
        monkeypatch.delenv("POST_IMAGE_RATIO", raising=False)
        avatar = {"model_ref": "owner/lora:v1", "trigger_word": "TOK"}
        with patch("cqc_lem.utilities.linkedin.helper.load_profile_for_user", return_value=None), \
             patch("cqc_lem.utilities.avatar.guardrails.resolve_avatar_for", return_value=avatar), \
             patch("cqc_lem.utilities.ai.image_concept.analyze_content_for_image",
                   return_value=_author_concept()), \
             patch("cqc_lem.utilities.ai.image_brief.build_image_brief",
                   side_effect=[_brief(), RuntimeError("author down")]), \
             patch("cqc_lem.utilities.ai.image_gen.render_avatar_image_gated", return_value=None):
            assert generate_image_for_post(9, "text", post_id=42) == (
                None, "Could not write an image prompt")

    def test_a_raising_fit_check_renders_without_the_likeness(self, tmp_path, monkeypatch):
        with patch("cqc_lem.utilities.avatar.guardrails.resolve_avatar_for_concept",
                   side_effect=RuntimeError("db down")):
            (url, _), _, _, build, render, lora = self._generate(tmp_path, monkeypatch)
        assert url and build.call_args[1]["avatar"] is None
        lora.assert_not_called()

    def test_the_judges_rubric_lands_on_the_receipt(self, tmp_path, monkeypatch):
        from cqc_lem.utilities.media_provenance import read_brief_receipt
        out = {"gate_verdict": "accepted", "gate_rubric": {"relevance": 5},
               "gate_failing": [], "gate_issues": [], "gate_blind_description": "a desk"}
        (url, _), *_ = self._generate(tmp_path, monkeypatch, render_info_out=out)
        with patch("cqc_lem.assets_dir", str(tmp_path / "assets")):
            receipt = read_brief_receipt(url)
        assert receipt["gate_rubric"] == {"relevance": 5}
        assert receipt["gate_blind_description"] == "a desk"
        assert receipt["concept"]["thesis"].startswith("Late invoices")


# ── Video source frame + motion ───────────────────────────────────────────────

class TestVideoStagedEngine:
    def _run(self, *, avatar=None, quality="standard", concept=None, caption=None):
        concept = concept or _concept()

        def _prompt(*_args, brief_info=None, **_kwargs):
            brief_info["brief"] = _brief(concept, None)
            return "scene"

        with patch("cqc_lem.utilities.db.get_post_video_quality", return_value=quality), \
             patch("cqc_lem.utilities.db.get_default_video_quality", return_value=quality), \
             patch("cqc_lem.utilities.db.get_video_credit_balance", return_value=0), \
             patch("cqc_lem.utilities.avatar.guardrails.resolve_avatar_for", return_value=avatar), \
             patch(f"{_RCP}._check_avatar_likeness"), \
             patch(f"{_RCP}._persist_video_model"), \
             patch("cqc_lem.utilities.ai.image_concept.analyze_content_for_image",
                   return_value=concept) as stage1, \
             patch("cqc_lem.utilities.brand_kit.brand_clause_for_user", return_value=_BRAND), \
             patch("cqc_lem.utilities.video_captions.burned_caption_text",
                   return_value=caption) as captions, \
             patch(f"{_RCP}.get_flux_image_prompt_from_ai", side_effect=_prompt) as prompt, \
             patch(f"{_RCP}.get_runway_ml_video_prompt_from_ai", return_value="motion") as motion, \
             patch("cqc_lem.utilities.ai.ai_helper.generate_post_image",
                   return_value="/tmp/avatar.png") as gpi, \
             patch("cqc_lem.utilities.ai.image_gen.render_image_gated",
                   return_value="/tmp/frame.png") as gated, \
             patch("cqc_lem.utilities.ai.image_gen.render_image_from_prompt") as ungated, \
             patch(f"{_RCP}.create_runway_video", return_value="https://x.mp4") as runway:
            from cqc_lem.app.run_content_plan import _generate_video_src
            src = _generate_video_src(7, "The post about invoices", None, post_id=9)
        self.captions = captions
        return src, concept, stage1, prompt, motion, gpi, gated, ungated, runway

    def test_the_frame_judge_reads_the_burned_caption_as_its_headline(self):
        caption = "Agencies wait 90 days to get paid. Here is the fix."
        _, _, _, prompt, _, _, gated, _, _ = self._run(caption=caption)
        assert gated.call_args[1]["hook_text"] == caption
        assert gated.call_args[1]["surface"] == "video"
        self.captions.assert_called_once_with("The post about invoices", user_id=7,
                                              avatar_led=False)
        # Judge-only: the brief author never sees it.
        assert caption not in str(prompt.call_args)

    def test_the_avatar_frame_judge_reads_it_too_with_the_avatar_gate(self):
        avatar = {"model_ref": "owner/lora:v1", "trigger_word": "TOK"}
        _, _, _, _, _, gpi, _, _, _ = self._run(avatar=avatar, concept=_author_concept(),
                                                caption="My hardest no.")
        assert gpi.call_args[1]["hook_text"] == "My hardest no."
        assert self.captions.call_args.kwargs["avatar_led"] is True

    def test_no_burned_caption_means_no_headline(self):
        _, _, _, _, _, _, gated, _, _ = self._run(caption=None)
        assert gated.call_args[1]["hook_text"] is None

    def test_the_standard_no_avatar_frame_is_now_gated_on_the_video_surface(self):
        src, concept, stage1, _, _, gpi, gated, ungated, runway = self._run()
        assert src == "https://x.mp4"
        ungated.assert_not_called()
        gpi.assert_not_called()
        gated.assert_called_once()
        assert gated.call_args[1]["surface"] == "video"
        assert gated.call_args[1]["concept"] is concept
        assert gated.call_args[1]["focal_concept"] == "the focal idea"
        assert runway.call_args[0][0] == "/tmp/frame.png"

    def test_one_concept_feeds_the_frame_brief_the_brand_and_the_motion(self):
        _, concept, stage1, prompt, motion, *_ = self._run()
        stage1.assert_called_once()
        assert stage1.call_args[0][0] == "The post about invoices"
        assert prompt.call_args[1]["concept"] is concept
        assert prompt.call_args[1]["brand_kit"] == _BRAND
        assert prompt.call_args[1]["surface"] == "video"
        assert motion.call_args[1]["concept"] is concept

    def test_the_avatar_frame_is_graded_against_the_same_concept(self):
        avatar = {"model_ref": "owner/lora:v1", "trigger_word": "TOK"}
        _, concept, _, _, _, gpi, gated, _, _ = self._run(avatar=avatar, concept=_author_concept())
        gated.assert_not_called()
        assert gpi.call_args[1]["concept"] is concept
        assert gpi.call_args[1]["focal_concept"] == "the focal idea"


    def test_a_video_not_about_the_author_frames_without_the_likeness(self):
        avatar = {"model_ref": "owner/lora:v1", "trigger_word": "TOK"}
        _, _, _, prompt, _, gpi, gated, _, _ = self._run(avatar=avatar)
        gpi.assert_not_called()
        gated.assert_called_once()
        assert prompt.call_args[1]["avatar"] is None


class TestVideoFrameGateAndClipCheck:
    """#2249 video gauntlet: the frame gate is enforced before Runway; the clip is checked after."""

    def _run(self, *, verdict="accepted", runway=("https://r/1.mp4",), checks=(), enabled=False,
             monkeypatch=None, brief_info=None):
        from cqc_lem.utilities.video_clip_check import ClipVerdict
        if enabled:
            monkeypatch.setenv("VIDEO_CLIP_CHECK_ENABLED", "true")

        def _frame(*_args, render_info=None, **_kwargs):
            render_info.update({"gate_verdict": verdict, "gate_issues": ["craft 2/5"]})
            return "/tmp/frame.png"

        with patch("cqc_lem.utilities.db.get_post_video_quality", return_value="standard"), \
             patch("cqc_lem.utilities.db.get_default_video_quality", return_value="standard"), \
             patch("cqc_lem.utilities.avatar.guardrails.resolve_avatar_for", return_value=None), \
             patch(f"{_RCP}._persist_video_model"), \
             patch(f"{_RCP}.create_folder_if_not_exists"), \
             patch("cqc_lem.utilities.ai.image_concept.analyze_content_for_image",
                   return_value=_concept()), \
             patch("cqc_lem.utilities.brand_kit.brand_clause_for_user", return_value=""), \
             patch(f"{_RCP}.get_flux_image_prompt_from_ai", return_value="scene"), \
             patch(f"{_RCP}.get_runway_ml_video_prompt_from_ai", return_value="m" * 500), \
             patch("cqc_lem.utilities.ai.image_gen.render_image_gated",
                   side_effect=_frame) as gated, \
             patch(f"{_RCP}.create_runway_video", side_effect=list(runway)) as rw, \
             patch("cqc_lem.utilities.video_clip_check.check_clip_url",
                   side_effect=[ClipVerdict(**c) for c in checks]) as check, \
             patch("cqc_lem.utilities.video_title_card.create_title_card_video",
                   return_value="/tmp/title_card_9.mp4") as card, \
             patch(f"{_RCP}.log_info") as info:
            from cqc_lem.app.run_content_plan import _generate_video_src
            src = _generate_video_src(7, "The post", None, post_id=9, brief_info=brief_info)
        return src, gated, rw, check, card, info

    def test_the_frame_gate_is_enforced_whatever_the_env_says(self):
        _, gated, *_ = self._run()
        assert gated.call_args.kwargs["enforce"] is True

    def test_a_rejected_frame_is_never_animated(self):
        brief_info: dict = {}
        src, _, runway, _, card, info = self._run(verdict="rejected", brief_info=brief_info)
        # Showcase round 4: the refused frame falls to the $0 branded title card, not stock.
        assert src == "/tmp/title_card_9.mp4"
        runway.assert_not_called()
        card.assert_called_once()
        assert any("not animating it" in c.args[0] for c in info.call_args_list)
        assert "brief" not in brief_info and "gate_verdict" not in brief_info

    def test_the_check_is_off_when_disabled(self):
        src, _, runway, check, _, _ = self._run()
        assert src == "https://r/1.mp4"
        check.assert_not_called()
        runway.assert_called_once()

    def test_a_clean_clip_ships_after_one_check(self, monkeypatch):
        brief_info: dict = {}
        src, _, runway, check, _, _ = self._run(
            enabled=True, monkeypatch=monkeypatch, brief_info=brief_info,
            checks=[{"checked": True}])
        assert src == "https://r/1.mp4"
        runway.assert_called_once()
        check.assert_called_once_with("https://r/1.mp4", user_id=7, post_id=9)
        assert brief_info["clip_check"] == {"first": {"checked": True, "defects": [],
                                                      "details": "", "reason": ""}}

    def test_a_defect_buys_one_re_render_with_a_repair_clause(self, monkeypatch):
        brief_info: dict = {}
        src, _, runway, check, _, _ = self._run(
            enabled=True, monkeypatch=monkeypatch, brief_info=brief_info,
            runway=("https://r/1.mp4", "https://r/2.mp4"),
            checks=[{"checked": True, "defects": ["an object or person appeared"],
                     "details": "a mug pops in"}, {"checked": True}])
        assert src == "https://r/2.mp4"
        assert runway.call_count == 2
        retry_motion = runway.call_args_list[1].args[1]
        assert len(retry_motion) <= 512
        assert "a mug pops in" in retry_motion and retry_motion.startswith("m")
        assert brief_info["clip_check"]["shipped"] == "retry"
        assert brief_info["clip_check"]["retry"]["checked"] is True
        assert check.call_count == 2

    def test_a_re_render_that_returns_nothing_ships_the_first_clip(self, monkeypatch):
        brief_info: dict = {}
        src, *_ = self._run(enabled=True, monkeypatch=monkeypatch, brief_info=brief_info,
                            runway=("https://r/1.mp4", None),
                            checks=[{"checked": True, "defects": ["a limb or face distorted"]}])
        assert src == "https://r/1.mp4"
        assert brief_info["clip_check"]["shipped"] == "first"

    def test_an_unchecked_clip_ships_without_a_re_render(self, monkeypatch):
        src, _, runway, *_ = self._run(enabled=True, monkeypatch=monkeypatch,
                                       checks=[{"checked": False, "reason": "no ffmpeg"}])
        assert src == "https://r/1.mp4"
        runway.assert_called_once()

    def test_the_clip_record_reaches_the_stored_receipt(self, tmp_path, monkeypatch):
        import cqc_lem.app.run_content_plan as rcp
        from cqc_lem.utilities.media_provenance import read_brief_receipt
        monkeypatch.setattr(rcp, "assets_dir", str(tmp_path))
        monkeypatch.setattr("cqc_lem.assets_dir", str(tmp_path))

        def _save(url, directory):
            path = os.path.join(directory, "clip.mp4")
            with open(path, "wb") as fh:
                fh.write(b"\x00\x00\x00 ftypisom" + b"\x00" * 56)
            return path

        record = {"first": {"checked": True, "defects": [], "details": "", "reason": ""}}
        with patch(f"{_RCP}.save_video_url_to_dir", side_effect=_save), \
             patch(f"{_RCP}._accept_probed_video", return_value=True), \
             patch(f"{_RCP}._caption_video_asset"), \
             patch(f"{_RCP}._record_video_asset_measures"), \
             patch("cqc_lem.utilities.c2pa_helper.add_ai_content_credentials"), \
             patch(f"{_RCP}.update_db_post_video_url"):
            url = rcp._store_video_asset(7, "https://r/1.mp4", user_id=3, brief=_brief(),
                                         gate_verdict="accepted", clip_check=record)
        assert read_brief_receipt(url)["clip_check"] == record

    def test_no_clip_record_means_no_extra(self):
        from cqc_lem.app.run_content_plan import _clip_check_extra
        assert _clip_check_extra(None) is None and _clip_check_extra({}) is None
        assert _clip_check_extra({"clip_check": {"a": 1}}) == {"clip_check": {"a": 1}}


class TestVideoCaptionIsJudgeOnly:
    """The caption reaches the video frame's judge, never the render (PR #2249)."""

    def test_video_never_composites_and_the_caption_never_reaches_the_renderer(self):
        from cqc_lem.utilities.ai import image_gen
        caption = "Agencies wait 90 days to get paid."
        with patch.object(image_gen, "_render_with_backend",
                          return_value=("/tmp/frame.png", "gpt-image")) as render, \
             patch("cqc_lem.utilities.ai.image_compose.compose_headline") as compose, \
             patch.object(image_gen, "inspect_render_quality",
                          return_value=image_gen.QualityVerdict(acceptable=True)) as judge:
            path = image_gen.render_image_gated("a scene", surface="video", ratio="9:16",
                                                concept=_concept(), hook_text=caption,
                                                enforce=True)
        assert path == "/tmp/frame.png", "the raw frame ships — no composite"
        compose.assert_not_called()
        assert render.call_args.kwargs["ratio"] == "9:16", "no square scene for a split layout"
        assert caption not in render.call_args.args[0]
        assert judge.call_args.kwargs["hook_text"] == caption
        assert judge.call_args.kwargs["composite_path"] is None
        assert "video" not in image_gen.COMPOSE_SURFACES


class TestBurnedCaptionText:
    _POST = "Agencies wait 90 days to get paid.\nHere is the fix.\n\nMore detail.\n#cashflow"

    def _caption(self, *, flag=True, avatar_led=False, opt_in=False):
        from cqc_lem.utilities import video_captions as vc
        with patch("cqc_lem.utilities.flags.flag_enabled", return_value=flag), \
             patch.object(vc, "captions_allowed_on_avatar_video", return_value=opt_in):
            return vc.burned_caption_text(self._POST, user_id=7, avatar_led=avatar_led)

    def test_it_is_the_same_text_the_burn_uses(self):
        from cqc_lem.utilities.video_captions import caption_lines
        assert self._caption() == " ".join(caption_lines(self._POST))
        assert self._caption().startswith("Agencies wait 90 days")

    def test_flag_off_is_none(self):
        assert self._caption(flag=False) is None

    def test_an_avatar_frame_without_the_overlay_opt_in_is_none(self):
        assert self._caption(avatar_led=True) is None
        assert self._caption(avatar_led=True, opt_in=True)

    def test_no_prose_is_none(self):
        from cqc_lem.utilities import video_captions as vc
        with patch("cqc_lem.utilities.flags.flag_enabled", return_value=True):
            assert vc.burned_caption_text("#a #b", user_id=7) is None

    def test_a_raising_flag_read_is_none(self):
        from cqc_lem.utilities import video_captions as vc
        with patch("cqc_lem.utilities.flags.flag_enabled", side_effect=RuntimeError("x")):
            assert vc.burned_caption_text(self._POST, user_id=7) is None


class TestMotionPromptCarriesTheConcept:
    def _draft(self, concept):
        from cqc_lem.utilities.ai import ai_helper
        resp = MagicMock()
        resp.choices = [MagicMock()]
        resp.choices[0].message.content = "Slow push-in."
        with patch.object(ai_helper, "_call_llm", return_value=resp) as llm:
            out = ai_helper._draft_motion_prompt("post", "scene", audio_note="", concept=concept)
        return out, llm.call_args[1]["messages"][1]["content"][0]["text"]

    def test_thesis_and_beat_reach_the_motion_author(self):
        out, user_text = self._draft(_concept())
        assert out == "Slow push-in."
        assert "<thesis>Late invoices quietly starve" in user_text
        assert "<emotional_beat>quiet dread turning to resolve</emotional_beat>" in user_text

    def test_without_a_concept_only_the_discipline_rides(self):
        _, user_text = self._draft(None)
        assert "<thesis>" not in user_text and "emotional_beat" not in user_text
        assert "ONE subtle, continuous action that expresses the post's point" in user_text

    def test_the_motion_discipline_rides_on_every_prompt(self):
        _, user_text = self._draft(_concept())
        for rule in ("ONE subtle, continuous action that expresses quiet dread turning to resolve",
                     "locked off or does a single slow push-in",
                     "no new objects or people enter the frame",
                     "Hands and limbs move naturally", "Screens stay dark and unchanged"):
            assert rule in user_text, rule

    def test_the_public_entry_point_threads_the_concept_through(self):
        from cqc_lem.utilities.ai import ai_helper
        concept = _concept()
        with patch.object(ai_helper, "_draft_motion_prompt", return_value="Slow push-in.") as draft:
            ai_helper.get_runway_ml_video_prompt_from_ai("post", "scene", model="gen4_turbo",
                                                         concept=concept)
        assert draft.call_args[1]["concept"] is concept

    def test_a_beat_alone_still_steers(self):
        from cqc_lem.utilities.ai.ai_helper import _motion_concept_block
        block = _motion_concept_block(_concept(thesis=""))
        assert "<thesis>" not in block and "emotional_beat" in block


class TestFluxPromptWrapper:
    def test_an_explicit_none_concept_is_forwarded_so_stage_one_never_reruns(self):
        from cqc_lem.utilities.ai.ai_helper import get_flux_image_prompt_from_ai
        with patch("cqc_lem.utilities.ai.image_brief.build_image_brief",
                   return_value=_brief()) as build:
            get_flux_image_prompt_from_ai("post", concept=None, brand_kit=_BRAND)
        assert "concept" in build.call_args[1] and build.call_args[1]["concept"] is None
        assert build.call_args[1]["brand_kit"] == _BRAND

    def test_an_omitted_concept_leaves_stage_one_to_the_brief_engine(self):
        from cqc_lem.utilities.ai.ai_helper import get_flux_image_prompt_from_ai
        with patch("cqc_lem.utilities.ai.image_brief.build_image_brief",
                   return_value=_brief()) as build:
            get_flux_image_prompt_from_ai("post")
        assert "concept" not in build.call_args[1]


class TestGeneratePostImagePassesTheConcept:
    @pytest.mark.parametrize("avatar", [None, {"model_ref": "o/l:v", "trigger_word": "TOK"}])
    def test_both_gated_branches_get_concept_and_hook(self, avatar):
        from cqc_lem.utilities.ai.ai_helper import generate_post_image
        concept = _concept()
        with patch("cqc_lem.utilities.avatar.guardrails.resolve_avatar_for", return_value=avatar), \
             patch("cqc_lem.utilities.ai.image_gen.render_image_gated",
                   return_value="/tmp/a.png") as gated, \
             patch("cqc_lem.utilities.ai.image_gen.render_avatar_image_gated",
                   return_value="/tmp/a.png") as lora:
            generate_post_image("p", 7, concept=concept, hook_text="Paid in 90 days")
        called = lora if avatar else gated
        assert called.call_args[1]["concept"] is concept
        assert called.call_args[1]["hook_text"] == "Paid in 90 days"


# ── Carousel ──────────────────────────────────────────────────────────────────

def _edu_carousel():
    from cqc_lem.utilities.carousel_creator import EducationalContentCarousel
    return EducationalContentCarousel(**{
        "cover": {"title": "Get paid faster", "content": "Invoices that clear"},
        "contents": [
            {"title": "Unpaid invoices", "content": "Chase every unpaid invoice weekly."},
            {"title": "Bank balance", "content": "Watch the bank balance, not revenue."},
            {"title": "Owner habits", "content": "The agency owner sets the tone."},
        ],
        "call_to_action": {"title": "Your turn", "content": "Which will you try?"},
    })


class TestCarouselStagedEngine:
    def test_one_concept_per_carousel_however_many_slides(self, tmp_path):
        from cqc_lem.utilities import carousel_creator as cc
        concept = _concept()
        with patch("cqc_lem.utilities.env_constants.CAROUSEL_IMAGES_ENABLED", True), \
             patch("cqc_lem.utilities.env_constants.CAROUSEL_IMAGE_RATE", 1.0), \
             patch("cqc_lem.utilities.env_constants.CAROUSEL_IMAGE_QUERY_LLM", True), \
             patch("cqc_lem.utilities.env_constants.CAROUSEL_PEXELS_ENABLED", True), \
             patch.object(cc, "_should_generate_with_replicate", return_value=False), \
             patch.object(cc, "get_pexels_image_path", return_value=None) as pexels, \
             patch("cqc_lem.utilities.ai.image_concept.analyze_content_for_image",
                   return_value=concept) as stage1, \
             patch("cqc_lem.utilities.ai.client.client") as llm, \
             patch.object(cc, "write_deck_render_receipt"), \
             patch.object(cc, "retain_carousel_keyframes"):
            cc.create_carousel_slide_images(_edu_carousel(), post_id=5, output_dir=str(tmp_path),
                                            user_id=7)
        stage1.assert_called_once()
        assert "Chase every unpaid invoice" in stage1.call_args[0][0]
        assert "Watch the bank balance" in stage1.call_args[0][0]
        assert stage1.call_args[1]["surface"] == "carousel"
        # The concept's entities ARE the query — no per-slide keyword call.
        llm.chat.completions.create.assert_not_called()
        queries = [c.args[0] for c in pexels.call_args_list]
        assert len(queries) == 3
        # Anchors this slide names lead: slide 2 searches the bank balance, slide 3 the owner.
        assert queries[1].startswith("bank balance")
        assert queries[2].startswith("agency owner")
        # "unpaid invoices" is a paper prop, which the engine stopped offering as an anchor in
        # round 7, so it is never a stock search either — slide 1 takes the deck's anchors.
        assert queries[0] == "agency owner bank balance"
        assert not any("invoice" in q for q in queries)

    def test_no_slide_wanting_an_image_costs_no_concept(self, tmp_path):
        from cqc_lem.utilities import carousel_creator as cc
        with patch("cqc_lem.utilities.env_constants.CAROUSEL_IMAGES_ENABLED", False), \
             patch("cqc_lem.utilities.ai.image_concept.analyze_content_for_image") as stage1, \
             patch.object(cc, "write_deck_render_receipt"), \
             patch.object(cc, "retain_carousel_keyframes"):
            cc.create_carousel_slide_images(_edu_carousel(), post_id=5, output_dir=str(tmp_path),
                                            user_id=7)
        stage1.assert_not_called()

    def test_the_pptx_path_shares_one_concept_and_brand_across_avatar_slides(self):
        from cqc_lem.utilities import carousel_creator as cc
        concept = _concept()
        brief = _brief(concept, None)
        with patch("cqc_lem.utilities.env_constants.CAROUSEL_IMAGES_ENABLED", True), \
             patch("cqc_lem.utilities.env_constants.CAROUSEL_IMAGE_RATE", 1.0), \
             patch("cqc_lem.utilities.env_constants.CAROUSEL_IMAGE_QUERY_LLM", False), \
             patch.object(cc, "_should_generate_with_replicate", return_value=True), \
             patch("cqc_lem.utilities.ai.image_concept.analyze_content_for_image",
                   return_value=concept) as stage1, \
             patch("cqc_lem.utilities.brand_kit.brand_clause_for_user",
                   return_value=_BRAND) as brand, \
             patch("cqc_lem.utilities.ai.image_brief.build_image_brief",
                   return_value=brief) as build, \
             patch("cqc_lem.utilities.ai.ai_helper.generate_post_image",
                   return_value="/tmp/slide.png") as gpi, \
             patch.object(cc, "Presentation"), \
             patch.object(cc, "convert_ppt_theme_colors"), \
             patch.object(cc, "create_ppt_educational_content_carousel",
                          side_effect=lambda prs, carousel, **kw: [
                              cc.select_slide_image(title=c.title, content=c.content,
                                                    content_type="educational",
                                                    post_id=kw["post_id"], slide_index=i,
                                                    user_id=kw["user_id"])
                              for i, c in enumerate(carousel.contents, start=2)] and prs):
            cc.create_ppt("deck_test", _edu_carousel(), post_id=5, user_id=7)
        stage1.assert_called_once()
        brand.assert_called_once_with(7)
        assert build.call_count == 3
        for call in build.call_args_list:
            assert call.kwargs["concept"] is concept
            assert call.kwargs["brand_kit"] == _BRAND
            assert call.kwargs["surface"] == "carousel"
        assert gpi.call_args[1]["concept"] is concept

    def test_outside_a_scope_the_slide_brief_runs_stage_one_itself(self):
        from cqc_lem.utilities import carousel_creator as cc
        with patch("cqc_lem.utilities.brand_kit.brand_clause_for_user", return_value=_BRAND), \
             patch("cqc_lem.utilities.ai.image_brief.build_image_brief",
                   return_value=_brief()) as build, \
             patch("cqc_lem.utilities.ai.ai_helper.generate_post_image", return_value="/tmp/s.png"):
            assert cc._generate_avatar_slide_image("team retro", 5, 9, "personal_story") == "/tmp/s.png"
        assert "concept" not in build.call_args.kwargs
        assert build.call_args.kwargs["brand_kit"] == _BRAND

    def test_a_concept_with_no_usable_entities_falls_back_to_the_keyword_path(self):
        from cqc_lem.utilities.carousel_creator import derive_image_query
        bare = _concept(specific_entities=(), weak=True)
        with patch("cqc_lem.utilities.env_constants.CAROUSEL_IMAGE_QUERY_LLM", False):
            assert derive_image_query("Remote Team Collaboration", "async standups",
                                      "educational", concept=bare) == \
                derive_image_query("Remote Team Collaboration", "async standups", "educational")

    def test_the_full_text_walk_skips_paths_and_urls(self):
        from cqc_lem.utilities.carousel_creator import _carousel_full_text
        text = _carousel_full_text({"title": "T", "image_path": "/tmp/x.png",
                                    "items": [{"content": "C"}, "https://example.com"]})
        assert text == "T\nC"

    def test_a_model_that_cannot_dump_costs_only_the_concept(self):
        from cqc_lem.utilities.carousel_creator import _carousel_full_text
        broken = MagicMock()
        broken.model_dump.side_effect = RuntimeError("bad model")
        assert _carousel_full_text(broken) == ""


# ── Admin variant tool ────────────────────────────────────────────────────────

class TestVariantsShareOneConcept:
    def test_concept_and_brand_are_resolved_once_per_batch(self, tmp_path):
        concept = _concept()
        img = tmp_path / "base.webp"
        img.write_bytes(b"x")
        combos = [{"image_model": "black-forest-labs/flux-dev", "include_video": False},
                  {"image_model": "black-forest-labs/flux-1.1-pro", "include_video": False}]
        with patch("cqc_lem.app.generate_variants.assets_dir", str(tmp_path)), \
             patch("cqc_lem.app.generate_variants.get_post_content", return_value="source text"), \
             patch("cqc_lem.app.generate_variants.load_profile_for_user", return_value=None), \
             patch("cqc_lem.utilities.avatar.guardrails.resolve_avatar_for", return_value=None), \
             patch("cqc_lem.utilities.ai.image_concept.analyze_content_for_image",
                   return_value=concept) as stage1, \
             patch("cqc_lem.utilities.brand_kit.brand_clause_for_user",
                   return_value=_BRAND) as brand, \
             patch("cqc_lem.app.generate_variants.get_flux_image_prompt_from_ai",
                   return_value="p") as prompt, \
             patch("cqc_lem.app.generate_variants.generate_post_image",
                   return_value=str(img)) as gpi:
            from cqc_lem.app.generate_variants import generate_media_variants
            generate_media_variants(post_id=3, user_id=7, combos=combos)
        stage1.assert_called_once()
        brand.assert_called_once_with(7)
        assert prompt.call_count == 2
        for call in prompt.call_args_list:
            assert call.kwargs["concept"] is concept and call.kwargs["brand_kit"] == _BRAND
        for call in gpi.call_args_list:
            assert call.kwargs["concept"] is concept


# ── Newsletter covers ─────────────────────────────────────────────────────────

class TestCoverCarriesTheBrand:
    def test_the_brand_clause_reaches_the_cover_brief(self, tmp_path):
        from cqc_lem.utilities import newsletter_cover as nc
        assets = tmp_path / "assets"
        assets.mkdir()
        with patch.object(nc, "assets_dir", str(assets)), \
             patch.object(nc, "_resolve_cover_avatar", return_value=None), \
             patch("cqc_lem.utilities.ai.image_concept.analyze_content_for_image",
                   return_value=None), \
             patch("cqc_lem.utilities.brand_kit.brand_clause_for_user",
                   return_value=_BRAND) as brand, \
             patch("cqc_lem.utilities.ai.image_brief.build_image_brief",
                   side_effect=RuntimeError("stop here")) as build:
            nc.generate_cover_for_edition(3, 9, "T", "S", "B")
        brand.assert_called_once_with(3)
        assert build.call_args.kwargs["brand_kit"] == _BRAND
        # Stage 1 ran in the cover and came back None: passed on, so the brief never re-runs it.
        assert "concept" in build.call_args.kwargs and build.call_args.kwargs["concept"] is None


@pytest.fixture(autouse=True)
def _no_last_resort_card():
    """Pin the treatment chain itself: the $0 last-resort typeset card is disabled here.

    Since #2241 showcase B a post whose every treatment fails ships the last-resort card instead
    of nothing; these tests assert the chain's own outcomes and the bare-ship path that remains
    when even that card cannot be drawn. The card has its own tests
    (``test_post_last_resort_card.py``).
    """
    import unittest.mock

    from cqc_lem.utilities import post_image as _post_image

    with unittest.mock.patch.object(
            _post_image, "_render_last_resort_card",
            return_value=_post_image._Rendered(reason="last resort disabled in this test")):
        yield
