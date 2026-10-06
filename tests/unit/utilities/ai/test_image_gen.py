"""The ONE renderer facade: backend policy, bounded Replicate runs, and the vision gate."""
import json
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest

from cqc_lem.utilities.ai import image_gen
from cqc_lem.utilities.ai.image_gen import (
    QualityVerdict,
    render_image_from_prompt,
    render_image_gated,
    run_replicate_bounded,
    size_for_ratio,
)

pytestmark = pytest.mark.unit


class TestSizeForRatio:
    @pytest.mark.parametrize("ratio,size", [
        ("1:1", "1024x1024"), ("16:9", "1536x1024"), ("9:16", "1024x1536"),
        ("4:5", "1024x1536"),  # rendered portrait, then centre-cropped by conform_to_ratio
        ("3:2", "1024x1024"),  # unknown ratios fall back to square
    ])
    def test_mapping(self, ratio, size):
        assert size_for_ratio(ratio) == size


class TestBackendPolicy:
    def test_auto_prefers_gpt_image(self):
        with patch.object(image_gen, "_render_via_gpt_image", return_value="/tmp/g.png") as gpt, \
             patch.object(image_gen, "_render_via_flux") as flux:
            assert render_image_from_prompt("p", ratio="16:9", user_id=3) == "/tmp/g.png"
        gpt.assert_called_once()
        flux.assert_not_called()

    def test_auto_falls_back_to_flux_on_gpt_failure(self):
        with patch.object(image_gen, "_render_via_gpt_image",
                          side_effect=RuntimeError("403 org not verified")), \
             patch.object(image_gen, "_render_via_flux", return_value="/tmp/f.webp") as flux:
            assert render_image_from_prompt("p", user_id=3) == "/tmp/f.webp"
        flux.assert_called_once()

    def test_forced_gpt_image_does_not_fall_back(self):
        with patch.object(image_gen, "IMAGE_BACKEND", "gpt-image"), \
             patch.object(image_gen, "_render_via_gpt_image", side_effect=RuntimeError("down")), \
             patch.object(image_gen, "_render_via_flux") as flux:
            with pytest.raises(RuntimeError):
                render_image_from_prompt("p")
        flux.assert_not_called()

    def test_forced_flux_never_touches_gpt_image(self):
        with patch.object(image_gen, "IMAGE_BACKEND", "flux"), \
             patch.object(image_gen, "_render_via_gpt_image") as gpt, \
             patch.object(image_gen, "_render_via_flux", return_value="/tmp/f.webp"):
            assert render_image_from_prompt("p") == "/tmp/f.webp"
        gpt.assert_not_called()


class TestRunReplicateBounded:
    def test_returns_on_first_success(self):
        with patch("replicate.run", return_value=["url"]) as run:
            assert run_replicate_bounded("m/ref", {"prompt": "p"}) == ["url"]
        run.assert_called_once()

    def test_retries_once_then_raises(self):
        with patch("replicate.run", side_effect=RuntimeError("boom")) as run, \
             patch("time.sleep"):
            with pytest.raises(RuntimeError, match="boom"):
                run_replicate_bounded("m/ref", {"prompt": "p"}, attempts=2)
        assert run.call_count == 2

    def test_waits_by_polling_not_on_the_create_request(self):
        with patch("replicate.run", return_value=["url"]) as run:
            run_replicate_bounded("m/ref", {"prompt": "p"})
        assert run.call_args.kwargs["wait"] is False

    def test_create_read_timeout_is_retried(self):
        with patch("replicate.run", side_effect=[httpx.ReadTimeout("create"), ["url"]]) as run, \
             patch("time.sleep"):
            assert run_replicate_bounded("m/ref", {"prompt": "p"}, attempts=2) == ["url"]
        assert run.call_count == 2

    def test_iterator_output_is_drained_inside_the_bound(self):
        with patch("replicate.run", return_value=(u for u in ["a", "b"])):
            assert run_replicate_bounded("m/ref", {"prompt": "p"}) == ["a", "b"]

    def test_sdk_create_request_is_not_held_open(self):
        """Against the real SDK: create carries no ``Prefer: wait``, and a GET poll resolves it."""
        import replicate

        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            body = {"id": "p1", "model": "m/ref", "version": "v", "input": {},
                    "status": "starting", "urls": {"get": "https://api.replicate.com/v1/predictions/p1"}}
            if request.method == "GET":
                body.update(status="succeeded", output=["https://x/folder/img.webp"])
            return httpx.Response(201 if request.method == "POST" else 200, json=body)

        sdk = replicate.Client(api_token="t", transport=httpx.MockTransport(handler))
        sdk.poll_interval = 0
        with patch("replicate.run", sdk.run):
            out = run_replicate_bounded("m/ref", {"prompt": "p"})
        assert str(out[0]) == "https://x/folder/img.webp"
        create = requests[0]
        assert create.method == "POST" and "prefer" not in create.headers
        assert [r.method for r in requests[1:]] == ["GET"]


class TestVisionGate:
    def _verdict_response(self, payload: dict) -> SimpleNamespace:
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=json.dumps(payload)))])

    def test_gate_reads_the_image_with_lem_vision(self, tmp_path):
        img = tmp_path / "a.png"
        img.write_bytes(b"png")
        with patch.object(image_gen, "client") as mock_client:
            mock_client.chat.completions.create.return_value = self._verdict_response(
                {"acceptable": False, "relevance": 1, "issues": ["garbled text"]})
            verdict = image_gen.inspect_render_quality(str(img), "a focal concept")
        assert verdict.checked and not verdict.acceptable
        assert verdict.issues == ["garbled text"]
        assert mock_client.chat.completions.create.call_args[1]["model"] == "lem-vision"

    def test_gate_fails_open_on_vision_outage(self, tmp_path):
        img = tmp_path / "a.png"
        img.write_bytes(b"png")
        with patch.object(image_gen, "client") as mock_client:
            mock_client.chat.completions.create.side_effect = RuntimeError("proxy down")
            verdict = image_gen.inspect_render_quality(str(img), "focal")
        assert verdict.acceptable and not verdict.checked

    def test_gate_fails_open_on_vision_outage_for_newsletter_too(self, tmp_path):
        img = tmp_path / "a.png"
        img.write_bytes(b"png")
        with patch.object(image_gen, "client") as mock_client:
            mock_client.chat.completions.create.side_effect = RuntimeError("proxy down")
            verdict = image_gen.inspect_render_quality(str(img), "focal", surface="newsletter")
        assert verdict.acceptable and not verdict.checked

    def test_newsletter_surface_adds_the_stock_office_cliche_rule(self, tmp_path):
        img = tmp_path / "a.png"
        img.write_bytes(b"png")
        with patch.object(image_gen, "client") as mock_client:
            mock_client.chat.completions.create.return_value = self._verdict_response(
                {"acceptable": True, "relevance": 5, "issues": []})
            image_gen.inspect_render_quality(str(img), "a leak", surface="newsletter")
        prompt_text = mock_client.chat.completions.create.call_args[1]["messages"][0]["content"][0]["text"]
        assert "person at laptop" in prompt_text

    def test_other_surfaces_never_get_the_newsletter_rule(self, tmp_path):
        img = tmp_path / "a.png"
        img.write_bytes(b"png")
        with patch.object(image_gen, "client") as mock_client:
            mock_client.chat.completions.create.return_value = self._verdict_response(
                {"acceptable": True, "relevance": 5, "issues": []})
            image_gen.inspect_render_quality(str(img), "a leak", surface="post_image")
        prompt_text = mock_client.chat.completions.create.call_args[1]["messages"][0]["content"][0]["text"]
        assert "person at laptop" not in prompt_text

    def test_newsletter_rejects_a_low_relevance_stock_scene_even_when_the_model_says_acceptable(
            self, tmp_path):
        # A laptop-on-desk cover for an AI-cost edition is "in the same domain" — a lenient vision
        # call answers acceptable=true at relevance 3 every time. The floor overrides it.
        img = tmp_path / "a.png"
        img.write_bytes(b"png")
        with patch.object(image_gen, "client") as mock_client:
            mock_client.chat.completions.create.return_value = self._verdict_response(
                {"acceptable": True, "relevance": 3,
                 "issues": ["person at laptop with notebook and coffee mug"]})
            verdict = image_gen.inspect_render_quality(str(img), "a leak", surface="newsletter")
        assert not verdict.acceptable

    def test_newsletter_synthesizes_an_issue_when_the_model_gave_none(self, tmp_path):
        # The model can say acceptable=true, relevance=2, issues=[] all at once — the override
        # must still leave something readable in the verdict, not an empty issues list.
        img = tmp_path / "a.png"
        img.write_bytes(b"png")
        with patch.object(image_gen, "client") as mock_client:
            mock_client.chat.completions.create.return_value = self._verdict_response(
                {"acceptable": True, "relevance": 2, "issues": []})
            verdict = image_gen.inspect_render_quality(str(img), "a leak", surface="newsletter")
        assert not verdict.acceptable
        assert verdict.issues

    def test_newsletter_accepts_an_object_first_scene_with_relevance_at_least_4(self, tmp_path):
        img = tmp_path / "a.png"
        img.write_bytes(b"png")
        with patch.object(image_gen, "client") as mock_client:
            mock_client.chat.completions.create.return_value = self._verdict_response(
                {"acceptable": True, "relevance": 4, "issues": []})
            verdict = image_gen.inspect_render_quality(str(img), "a leak", surface="newsletter")
        assert verdict.acceptable

    def test_non_strict_surface_keeps_the_bare_relevance_floor(self, tmp_path):
        img = tmp_path / "a.png"
        img.write_bytes(b"png")
        with patch.object(image_gen, "client") as mock_client:
            mock_client.chat.completions.create.return_value = self._verdict_response(
                {"acceptable": True, "relevance": 3, "issues": []})
            verdict = image_gen.inspect_render_quality(str(img), "a leak", surface="carousel")
        assert verdict.acceptable

    def test_post_image_rejects_a_low_relevance_scene_even_when_the_model_says_acceptable(
            self, tmp_path):
        # Issue #2015: text-post images hit the same "relates but doesn't depict" failure the
        # newsletter floor (#1992) already fixed — a bare relevance-3 "it relates" is not enough.
        img = tmp_path / "a.png"
        img.write_bytes(b"png")
        with patch.object(image_gen, "client") as mock_client:
            mock_client.chat.completions.create.return_value = self._verdict_response(
                {"acceptable": True, "relevance": 3, "issues": []})
            verdict = image_gen.inspect_render_quality(str(img), "a leak", surface="post_image")
        assert not verdict.acceptable
        assert verdict.issues

    def test_post_image_accepts_a_scene_with_relevance_at_least_4(self, tmp_path):
        img = tmp_path / "a.png"
        img.write_bytes(b"png")
        with patch.object(image_gen, "client") as mock_client:
            mock_client.chat.completions.create.return_value = self._verdict_response(
                {"acceptable": True, "relevance": 4, "issues": []})
            verdict = image_gen.inspect_render_quality(str(img), "a leak", surface="post_image")
        assert verdict.acceptable

    def test_enforced_surface_regenerates_with_the_issues_appended(self):
        verdicts = [QualityVerdict(acceptable=False, issues=["distorted face"]),
                    QualityVerdict(acceptable=True)]
        with patch.object(image_gen, "_render_with_backend",
                          side_effect=[("/tmp/1.png", "gpt-image"),
                                       ("/tmp/2.png", "gpt-image")]) as render, \
             patch.object(image_gen, "inspect_render_quality", side_effect=verdicts):
            path = render_image_gated("base prompt", surface="newsletter")
        assert path == "/tmp/2.png"
        assert render.call_count == 2
        assert "distorted face" in render.call_args_list[1][0][0]

    def test_attempts_are_bounded_and_the_last_render_ships(self):
        bad = QualityVerdict(acceptable=False, issues=["filler"])
        with patch.object(image_gen, "_render_with_backend",
                          return_value=("/tmp/x.png", "gpt-image")) as render, \
             patch.object(image_gen, "inspect_render_quality", return_value=bad):
            path = render_image_gated("p", surface="newsletter")
        assert path == "/tmp/x.png"
        assert render.call_count == image_gen.IMAGE_GATE_MAX_ATTEMPTS

    def test_unenforced_surface_is_advisory_only(self):
        bad = QualityVerdict(acceptable=False, issues=["filler"])
        with patch.object(image_gen, "IMAGE_QUALITY_GATE_SURFACES", ("newsletter",)), \
             patch.object(image_gen, "_render_with_backend",
                          return_value=("/tmp/x.png", "gpt-image")) as render, \
             patch.object(image_gen, "inspect_render_quality", return_value=bad):
            assert render_image_gated("p", surface="carousel") == "/tmp/x.png"
        render.assert_called_once()


class TestAvatarRenderIsGatedToo:
    """Regression: the avatar branch was the ONE render path with no quality check, so a post
    about LLM routing costs came back as a plain headshot and nothing questioned it.
    """

    _AVATAR = {"model_ref": "owner/lora:v1", "trigger_word": "TOK",
               "gender_presentation": "man", "age_band": "40s"}

    def _render(self, verdicts, surface="post_image"):
        with patch("cqc_lem.utilities.avatar.replicate_avatar.generate_image_with_avatar",
                   side_effect=[("/tmp/1.png", True), ("/tmp/2.png", True)]) as lora, \
             patch("cqc_lem.utilities.ai.ai_helper._record_avatar_media") as record, \
             patch.object(image_gen, "inspect_render_quality", side_effect=verdicts):
            path = image_gen.render_avatar_image_gated(
                "base prompt", avatar=self._AVATAR, user_id=3, surface=surface,
                focal_concept="the idea", post_id=9)
        return path, lora, record

    def test_rejected_avatar_render_is_retried_with_a_positive_directive(self):
        path, lora, record = self._render([QualityVerdict(acceptable=False,
                                                          issues=["six fingers on the left hand"]),
                                           QualityVerdict(acceptable=True)])
        assert path == "/tmp/2.png"
        assert lora.call_count == 2
        # This path is always FLUX, which renders what a prompt NAMES — so the retry states what
        # the image must SHOW, never the defect it was rejected for (issue #1141).
        retry = lora.call_args[0][0]
        assert "hands relaxed and out of frame" in retry
        assert "fingers" not in retry
        # The trigger word + declared clause must survive the retry prompt.
        assert retry.startswith("TOK, a man in his 40s")

    def test_accepted_render_returns_immediately_and_signs_provenance(self):
        path, lora, record = self._render([QualityVerdict(acceptable=True)])
        assert path == "/tmp/1.png"
        lora.assert_called_once()
        record.assert_called_once_with("/tmp/1.png", 9, 3)

    def test_render_info_reports_the_attempt_that_was_returned(self):
        """The likeness probe needs THIS frame's provenance, not the post's (issue #1430).

        Attempt 1 renders on the LoRA and gets rejected, attempt 2 falls back to base Flux, and
        the fallback is what ships — so `used_avatar` must read False.
        """
        info: dict = {}
        with patch("cqc_lem.utilities.avatar.replicate_avatar.generate_image_with_avatar",
                   side_effect=[("/tmp/1.png", True), ("/tmp/2.png", False)]), \
             patch("cqc_lem.utilities.ai.ai_helper._record_avatar_media"), \
             patch.object(image_gen, "inspect_render_quality",
                          side_effect=[QualityVerdict(acceptable=False, issues=["blurry"]),
                                       QualityVerdict(acceptable=True)]):
            path = image_gen.render_avatar_image_gated(
                "base prompt", avatar=self._AVATAR, user_id=3, surface="post_image",
                focal_concept="the idea", post_id=9, render_info=info)
        assert path == "/tmp/2.png"
        assert info["used_avatar"] is False

    def test_render_info_is_reported_for_a_lora_render(self):
        info: dict = {}
        with patch("cqc_lem.utilities.avatar.replicate_avatar.generate_image_with_avatar",
                   return_value=("/tmp/1.png", True)), \
             patch("cqc_lem.utilities.ai.ai_helper._record_avatar_media"), \
             patch.object(image_gen, "inspect_render_quality",
                          return_value=QualityVerdict(acceptable=True)):
            image_gen.render_avatar_image_gated(
                "base prompt", avatar=self._AVATAR, user_id=3, surface="post_image",
                post_id=9, render_info=info)
        assert info["used_avatar"] is True

    def test_unenforced_surface_does_not_retry(self):
        with patch.object(image_gen, "IMAGE_QUALITY_GATE_SURFACES", ("newsletter",)):
            path, lora, _ = self._render([QualityVerdict(acceptable=False, issues=["x"])],
                                         surface="carousel")
        assert path == "/tmp/1.png"
        lora.assert_called_once()


class TestNoBrandMarksConstraint:
    """Regression: gpt-image-2 inserted a LinkedIn logo into business scenes on its own, and
    both generated covers reached review carrying someone else's trademark.
    """

    def test_every_render_prompt_carries_the_constraint(self):
        with patch.object(image_gen, "_render_via_gpt_image", return_value="/tmp/g.png") as gpt:
            image_gen.render_image_from_prompt("a founder at a desk", user_id=3)
        sent = gpt.call_args[0][0]
        assert "logos" in sent and "brand marks" in sent

    def test_flux_fallback_gets_the_positive_phrasing(self):
        """FLUX ignores negation — naming 'logos' can summon one — so the FLUX path carries
        the constraint phrased positively, never as a prohibition.
        """
        with patch.object(image_gen, "_render_via_gpt_image", side_effect=RuntimeError("down")), \
             patch.object(image_gen, "_render_via_flux", return_value="/tmp/f.webp") as flux:
            image_gen.render_image_from_prompt("a founder at a desk", user_id=3)
        sent = flux.call_args[0][0]
        assert "plain and unbranded" in sent
        assert "logos" not in sent and "brand marks" not in sent

    def test_avatar_render_carries_the_flux_phrasing_too(self):
        avatar = {"model_ref": "owner/lora:v1", "trigger_word": "TOK",
                  "gender_presentation": "man", "age_band": "40s"}
        with patch("cqc_lem.utilities.avatar.replicate_avatar.generate_image_with_avatar",
                   return_value=("/tmp/a.png", True)) as lora, \
             patch("cqc_lem.utilities.ai.ai_helper._record_avatar_media"), \
             patch.object(image_gen, "inspect_render_quality",
                          return_value=QualityVerdict(acceptable=True)):
            image_gen.render_avatar_image_gated("a founder at a desk", avatar=avatar, user_id=3,
                                                surface="post_image")
        assert "plain and unbranded" in lora.call_args[0][0]
        # The fallback prompt must carry it as well — that render is still published.
        assert "plain and unbranded" in lora.call_args[1]["fallback_prompt"]

    def test_constraint_is_never_doubled_and_never_mixed(self):
        once_gpt = image_gen.with_no_marks("scene", "gpt-image")
        assert image_gen.with_no_marks(once_gpt, "gpt-image") == once_gpt
        # A prompt already carrying one variant never gains the other.
        assert image_gen.with_no_marks(once_gpt, "flux") == once_gpt
        once_flux = image_gen.with_no_marks("scene", "flux")
        assert image_gen.with_no_marks(once_flux, "gpt-image") == once_flux


def _png(path, size, fmt="PNG"):
    from PIL import Image
    Image.new("RGB", size, (40, 90, 160)).save(path, format=fmt)
    return str(path)


class TestConformToRatio:
    """4:5 portrait (issue #2241): render 1024x1536, centre-crop to 1024x1280 before the judge.

    gpt-image has no 4:5 size; the crop is deterministic.
    """

    def test_a_portrait_gpt_image_render_crops_to_exactly_1024x1280(self, tmp_path):
        from PIL import Image
        path = _png(tmp_path / "r.png", (1024, 1536))
        assert image_gen.conform_to_ratio(path, "4:5") == path
        with Image.open(path) as img:
            assert img.size == (1024, 1280) and img.format == "PNG"

    def test_the_crop_is_centred(self, tmp_path):
        from PIL import Image
        img = Image.new("RGB", (1024, 1536), (0, 0, 0))
        img.paste((255, 0, 0), (0, 0, 1024, 128))       # top band: cropped away
        img.paste((0, 255, 0), (0, 128, 1024, 1408))    # the kept middle
        img.paste((0, 0, 255), (0, 1408, 1024, 1536))   # bottom band: cropped away
        path = str(tmp_path / "c.png")
        img.save(path)
        image_gen.conform_to_ratio(path, "4:5")
        with Image.open(path) as out:
            assert out.getpixel((10, 0)) == (0, 255, 0)
            assert out.getpixel((10, 1279)) == (0, 255, 0)

    def test_a_too_wide_render_crops_its_width(self, tmp_path):
        from PIL import Image
        path = _png(tmp_path / "w.png", (1024, 1024))
        image_gen.conform_to_ratio(path, "4:5")
        with Image.open(path) as img:
            assert img.size == (819, 1024)

    def test_a_native_4_5_render_is_left_alone(self, tmp_path):
        path = _png(tmp_path / "f.webp", (896, 1120), fmt="WEBP")
        before = (tmp_path / "f.webp").read_bytes()
        image_gen.conform_to_ratio(path, "4:5")
        assert (tmp_path / "f.webp").read_bytes() == before

    @pytest.mark.parametrize("ratio", ["1:1", "16:9", "9:16"])
    def test_native_ratios_are_never_touched(self, tmp_path, ratio):
        path = _png(tmp_path / "n.png", (1024, 1536))
        before = (tmp_path / "n.png").read_bytes()
        image_gen.conform_to_ratio(path, ratio)
        assert (tmp_path / "n.png").read_bytes() == before

    def test_nothing_rendered_is_passed_through(self):
        assert image_gen.conform_to_ratio(None, "4:5") is None

    def test_an_unreadable_file_is_kept_never_raised(self, tmp_path):
        path = tmp_path / "bad.png"
        path.write_bytes(b"not an image")
        assert image_gen.conform_to_ratio(str(path), "4:5") == str(path)
        assert path.read_bytes() == b"not an image"

    def test_the_gpt_image_path_crops_before_returning(self, tmp_path):
        from PIL import Image
        path = _png(tmp_path / "g.png", (1024, 1536))
        with patch.object(image_gen, "IMAGE_BACKEND", "gpt-image"), \
             patch.object(image_gen, "_render_via_gpt_image", return_value=path) as gpt:
            out, backend = image_gen._render_with_backend("p", ratio="4:5")
        assert backend == "gpt-image" and gpt.call_args[1]["ratio"] == "4:5"
        with Image.open(out) as img:
            assert img.size == (1024, 1280)

    def test_the_flux_path_asks_for_4_5_and_conforms_whatever_comes_back(self, tmp_path):
        from PIL import Image
        path = _png(tmp_path / "f.png", (1024, 1024))
        with patch.object(image_gen, "IMAGE_BACKEND", "flux"), \
             patch.object(image_gen, "_render_via_flux", return_value=path) as flux:
            out, backend = image_gen._render_with_backend("p", ratio="4:5")
        assert backend == "flux" and flux.call_args[1]["ratio"] == "4:5"
        with Image.open(out) as img:
            assert img.size == (819, 1024)

    def test_the_avatar_path_crops_before_the_judge_looks(self, tmp_path):
        from PIL import Image
        path = _png(tmp_path / "a.png", (1024, 1536))
        seen = {}

        def _judge(image_path, *_a, **_k):
            with Image.open(image_path) as img:
                seen["size"] = img.size
            return QualityVerdict(acceptable=True)

        with patch("cqc_lem.utilities.avatar.replicate_avatar.generate_image_with_avatar",
                   return_value=(path, False)) as lora, \
             patch.object(image_gen, "inspect_render_quality", side_effect=_judge):
            image_gen.render_avatar_image_gated(
                "p", avatar={"model_ref": "o/l:v", "trigger_word": "TOK"}, user_id=3,
                surface="post_image", ratio="4:5")
        assert lora.call_args[1]["ratio"] == "4:5"
        assert seen["size"] == (1024, 1280)
