"""The post image's REAL render → crop → judge path (#2249 gauntlet).

Post 100 shipped `gate_verdict: unchecked` with no rubric and no log line between the render and
the store. These drive `generate_image_for_post` through the real `render_image_gated`,
`_render_with_backend`, `conform_to_ratio` and staged judge — only the gpt-image call, the vision
calls, Stage 1 and the brief author are mocked — so a judge that never sees the image, or fails
silently, fails here.
"""

import base64
import io
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from cqc_lem.utilities.ai import image_gen
from cqc_lem.utilities.ai.image_brief import ImageBrief
from cqc_lem.utilities.ai.image_concept import ImageConcept

pytestmark = pytest.mark.unit

_CONCEPT = ImageConcept(thesis="Late invoices quietly starve a small agency's cash flow.",
                        audience="agency owners",
                        specific_entities=("unpaid invoices", "bank balance"),
                        emotional_beat="quiet dread", hook_phrase="",
                        treatment="concrete_scene", treatment_rationale="a tangible situation",
                        visual_anchors=("unpaid invoices", "bank statement"))
_ACCEPT = {"rubric": {name: 5 for name in image_gen.RUBRIC_CRITERIA}, "text_seen": "",
           "entities_depicted": {"unpaid invoices": True}, "thesis_inferable": True,
           "reusable_elsewhere": False, "issues": []}
_REJECT = {**_ACCEPT, "rubric": {**_ACCEPT["rubric"], "specificity": 2, "craft": 2},
           "issues": ["garbled text on a pinned paper"]}


def _resp(content):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content),
                                                    finish_reason="stop")])


def _write_portrait(path):
    from PIL import Image
    Image.new("RGB", (1024, 1536), (30, 60, 90)).save(path, format="PNG")
    return str(path)


def _size_of(part) -> tuple:
    from PIL import Image
    data = part["image_url"]["url"].split(",", 1)[1]
    with Image.open(io.BytesIO(base64.b64decode(data))) as img:
        return img.size


class _Vision:
    """The two lem-vision calls per look: blind description, then the targeted rubric."""

    def __init__(self, targeted_replies):
        self.targeted = list(targeted_replies)
        self.sizes: list = []
        self.calls = 0

    def __call__(self, **kwargs):
        self.calls += 1
        content = kwargs["messages"][0]["content"]
        self.sizes.append(_size_of(content[1]))
        if "response_format" not in kwargs:
            return _resp("Unpaid invoices on a kitchen table. There is no visible text.")
        reply = self.targeted.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return _resp(reply if isinstance(reply, str) else json.dumps(reply))


def _generate(tmp_path, monkeypatch, vision):
    from cqc_lem.utilities.post_image import generate_image_for_post

    monkeypatch.delenv("POST_IMAGE_RATIO", raising=False)
    assets = tmp_path / "assets"
    assets.mkdir(exist_ok=True)
    brief = ImageBrief(prompt="Unpaid invoices on a kitchen table at night, a bank statement.",
                       ratio="4:5", surface="post_image", style_preset="post_image",
                       focal_concept="unpaid invoices", concept=_CONCEPT)
    counter = {"n": 0}

    def _gpt(prompt, *, ratio, **_kwargs):
        counter["n"] += 1
        assert ratio == "4:5"
        return _write_portrait(tmp_path / f"render{counter['n']}.png")

    with patch("cqc_lem.utilities.post_image.assets_dir", str(assets)), \
         patch("cqc_lem.assets_dir", str(assets)), \
         patch("cqc_lem.utilities.linkedin.helper.load_profile_for_user", return_value=None), \
         patch("cqc_lem.utilities.avatar.guardrails.resolve_avatar_for", return_value=None), \
         patch("cqc_lem.utilities.ai.image_concept.analyze_content_for_image",
               return_value=_CONCEPT), \
         patch("cqc_lem.utilities.brand_kit.brand_clause_for_user", return_value=""), \
         patch("cqc_lem.utilities.ai.image_brief.build_image_brief", return_value=brief), \
         patch.object(image_gen, "IMAGE_BACKEND", "gpt-image"), \
         patch.object(image_gen, "IMAGE_QUALITY_GATE_SURFACES", ("newsletter", "post_image")), \
         patch.object(image_gen, "IMAGE_GATE_MAX_ATTEMPTS", 1), \
         patch.object(image_gen, "_render_via_gpt_image", side_effect=_gpt), \
         patch.object(image_gen.client.chat.completions, "create", side_effect=vision), \
         patch("cqc_lem.utilities.post_image.log_info") as info, \
         patch.object(image_gen, "log_warning") as warn, \
         patch.object(image_gen, "log_info") as gate_info:
        result = generate_image_for_post(9, "A post about unpaid invoices.", post_id=42)
    return result, assets, info, warn, gate_info


def _receipt(url, assets):
    from cqc_lem.utilities.media_provenance import read_brief_receipt
    with patch("cqc_lem.assets_dir", str(assets)):
        return read_brief_receipt(url)


def test_the_judge_sees_the_cropped_4_5_file_and_its_verdict_is_recorded(tmp_path, monkeypatch):
    vision = _Vision([_ACCEPT])
    (url, reason), assets, info, warn, _ = _generate(tmp_path, monkeypatch, vision)
    assert reason is None and url
    assert vision.sizes == [(1024, 1280), (1024, 1280)], "both looks graded the CROPPED render"
    receipt = _receipt(url, assets)
    assert receipt["gate_verdict"] == "accepted"
    assert receipt["gate_rubric"]["specificity"] == 5
    assert receipt["render_path"] == "base"
    warn.assert_not_called()
    stored = info.call_args_list[-1]
    assert stored.args[0] == "Generated post image" and stored.kwargs["gate_verdict"] == "accepted"


def test_a_fenced_judge_reply_is_still_graded(tmp_path, monkeypatch):
    vision = _Vision([f"```json\n{json.dumps(_ACCEPT)}\n```"])
    (url, _), assets, _, warn, _ = _generate(tmp_path, monkeypatch, vision)
    assert _receipt(url, assets)["gate_verdict"] == "accepted"
    warn.assert_not_called()


def test_a_judge_blip_is_retried_once_and_then_graded(tmp_path, monkeypatch):
    vision = _Vision(["", _ACCEPT])
    (url, _), assets, _, warn, _ = _generate(tmp_path, monkeypatch, vision)
    assert vision.calls == 4, "one render, two looks of two calls each"
    assert _receipt(url, assets)["gate_verdict"] == "accepted"
    assert warn.call_args.kwargs["exc"] is not None, "the failed look is a WARNING with exc="


def test_twice_unchecked_ships_but_says_so_everywhere(tmp_path, monkeypatch):
    vision = _Vision([RuntimeError("vision 502"), RuntimeError("vision 502")])
    (url, reason), assets, info, warn, gate_info = _generate(tmp_path, monkeypatch, vision)
    assert reason is None and url, "availability: an unreachable judge still fails open"
    assert warn.call_count == 2
    assert all(c.kwargs["exc"] is not None for c in warn.call_args_list)
    receipt = _receipt(url, assets)
    assert receipt["gate_verdict"] == "unchecked"
    assert "vision 502" in "; ".join(receipt["gate_issues"])
    assert any("UNCHECKED" in c.args[0] for c in gate_info.call_args_list)
    assert "vision 502" in info.call_args_list[-1].kwargs["gate_issues"]


def test_a_rejected_render_logs_the_judges_whole_why(tmp_path, monkeypatch):
    from cqc_lem.utilities.post_image import GATE_REJECTED_REASON
    vision = _Vision([_REJECT])
    (url, reason), assets, info, _, _ = _generate(tmp_path, monkeypatch, vision)
    assert url is None and reason == GATE_REJECTED_REASON
    record = info.call_args_list[-1]
    assert "rejected by the quality gate" in record.args[0]
    assert "specificity=2" in record.kwargs["gate_rubric"]
    assert "craft" in record.kwargs["gate_failing"]
    assert "garbled text on a pinned paper" in record.kwargs["gate_issues"]
    assert "no visible text" in record.kwargs["gate_blind_description"]
    assert record.kwargs["render_path"] == "base"


def test_a_non_enforced_surface_is_not_retried():
    vision = _Vision([RuntimeError("down")])
    with patch.object(image_gen.client.chat.completions, "create", side_effect=vision), \
         patch.object(image_gen, "log_warning"), \
         patch.object(image_gen, "_render_with_backend", return_value=("/tmp/x.png", "gpt-image")), \
         patch.object(image_gen, "_image_part", return_value={"image_url": {"url": "x"}}):
        info: dict = {}
        image_gen.render_image_gated("p", surface="video", concept=_CONCEPT, render_info=info)
    assert vision.calls == 1
    assert info["gate_verdict"] == "unchecked"


def test_the_legacy_judge_fails_loudly_too():
    with patch.object(image_gen.client.chat.completions, "create", return_value=_resp("")), \
         patch.object(image_gen, "_image_part", return_value={"image_url": {"url": "x"}}), \
         patch.object(image_gen, "log_warning") as warn:
        verdict = image_gen.inspect_render_quality("/tmp/x.png", "focal", surface="post_image")
    assert verdict.checked is False and verdict.acceptable is True
    assert "legacy judge reply was not a JSON object" in verdict.issues[0]
    assert warn.call_args.kwargs["exc"] is not None
