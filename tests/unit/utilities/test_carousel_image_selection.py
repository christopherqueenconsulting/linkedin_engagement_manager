"""Unit tests for the structured, deterministic carousel content-slide image
selection + layout routing (cqc_lem.utilities.carousel_creator)."""

import tempfile

import pytest
from unittest.mock import MagicMock, patch

from cqc_lem.utilities import carousel_creator as cc
from cqc_lem.utilities.carousel_creator import (
    EducationalContentCarousel,
    choose_content_layout,
    create_carousel_slide_images,
    create_one_column_text_layout_slide,
    create_one_column_text_1_layout_slide,
    create_title_and_body_layout_slide,
    create_title_and_body_1_layout_slide,
    derive_image_query,
    select_slide_image,
    _carousel_content_type,
    _fit_and_crop_image,
    _heuristic_image_query,
    _seeded_unit,
    _should_include_slide_image,
    _should_generate_with_replicate,
    _user_has_active_avatar,
    _generate_avatar_slide_image,
)

ENV = "cqc_lem.utilities.env_constants"

IMAGE_LAYOUTS = {create_one_column_text_layout_slide, create_one_column_text_1_layout_slide}
TEXT_LAYOUTS = {create_title_and_body_layout_slide, create_title_and_body_1_layout_slide}


@pytest.mark.unit
class TestLayoutRouting:
    def test_image_slide_routes_to_image_capable_layout(self):
        # Every content slide that has an image must land on a picture-placeholder layout.
        for slide_index in range(2, 12):
            fn = choose_content_layout("/tmp/pic.jpg", post_id=42, slide_index=slide_index)
            assert fn in IMAGE_LAYOUTS

    def test_textonly_slide_routes_to_text_layout(self):
        for slide_index in range(2, 12):
            fn = choose_content_layout(None, post_id=42, slide_index=slide_index)
            assert fn in TEXT_LAYOUTS

    def test_layout_choice_is_deterministic(self):
        a = choose_content_layout("/tmp/pic.jpg", post_id=7, slide_index=3)
        b = choose_content_layout("/tmp/pic.jpg", post_id=7, slide_index=3)
        assert a is b

    def test_custom_text_pool_used_when_no_image(self):
        sentinel = MagicMock(name="big_number")
        fn = choose_content_layout(None, post_id=1, slide_index=2, text_layouts=[sentinel])
        assert fn is sentinel


@pytest.mark.unit
class TestSeedDeterminism:
    def test_seeded_unit_is_stable_and_in_range(self):
        v1 = _seeded_unit(101, 3, "include")
        v2 = _seeded_unit(101, 3, "include")
        assert v1 == v2
        assert 0.0 <= v1 < 1.0

    def test_different_purpose_differs(self):
        assert _seeded_unit(101, 3, "include") != _seeded_unit(101, 3, "source")

    def test_none_post_id_is_stable(self):
        assert _seeded_unit(None, 5, "include") == _seeded_unit(None, 5, "include")


@pytest.mark.unit
class TestShouldIncludeImage:
    def test_disabled_returns_false(self):
        with patch(f"{ENV}.CAROUSEL_IMAGES_ENABLED", False), \
             patch(f"{ENV}.CAROUSEL_IMAGE_RATE", 1.0):
            assert _should_include_slide_image(1, 2) is False

    def test_rate_one_always_true(self):
        with patch(f"{ENV}.CAROUSEL_IMAGES_ENABLED", True), \
             patch(f"{ENV}.CAROUSEL_IMAGE_RATE", 1.0):
            assert _should_include_slide_image(1, 2) is True

    def test_rate_zero_always_false(self):
        with patch(f"{ENV}.CAROUSEL_IMAGES_ENABLED", True), \
             patch(f"{ENV}.CAROUSEL_IMAGE_RATE", 0.0):
            assert _should_include_slide_image(1, 2) is False

    def test_partial_rate_is_deterministic(self):
        with patch(f"{ENV}.CAROUSEL_IMAGES_ENABLED", True), \
             patch(f"{ENV}.CAROUSEL_IMAGE_RATE", 0.5):
            first = _should_include_slide_image(9, 4)
            assert first == _should_include_slide_image(9, 4)


@pytest.mark.unit
class TestQueryDerivation:
    def test_heuristic_strips_stopwords_and_limits(self):
        q = _heuristic_image_query("How to Boost Your Productivity", "the daily focus habit", "educational")
        assert "the" not in q.split()
        assert "your" not in q.split()
        assert len(q.split()) <= 3

    def test_heuristic_falls_back_to_content_type(self):
        q = _heuristic_image_query("a to of", "", "product_demo")
        assert q == "product demo"

    def test_llm_disabled_uses_heuristic(self):
        with patch(f"{ENV}.CAROUSEL_IMAGE_QUERY_LLM", False):
            q = derive_image_query("Remote Team Collaboration", "async standups", "educational")
        assert "remote" in q.lower()

    def test_llm_path_returns_cleaned_keywords(self):
        mock_resp = MagicMock()
        mock_resp.choices = [MagicMock(message=MagicMock(content=" team collaboration, laptop \n"))]
        with patch(f"{ENV}.CAROUSEL_IMAGE_QUERY_LLM", True), \
             patch("cqc_lem.utilities.ai.client.client") as mock_client:
            mock_client.chat.completions.create.return_value = mock_resp
            q = derive_image_query("Collaboration", "work together", "educational")
        assert q == "team collaboration laptop"

    def test_llm_failure_falls_back_to_heuristic(self):
        with patch(f"{ENV}.CAROUSEL_IMAGE_QUERY_LLM", True), \
             patch("cqc_lem.utilities.ai.client.client") as mock_client:
            mock_client.chat.completions.create.side_effect = RuntimeError("boom")
            q = derive_image_query("Data Pipelines", "streaming events", "industry_insights")
        assert "data" in q.lower() or "pipelines" in q.lower() or "streaming" in q.lower()

    def test_llm_empty_response_falls_back(self):
        mock_resp = MagicMock()
        mock_resp.choices = [MagicMock(message=MagicMock(content="   "))]
        with patch(f"{ENV}.CAROUSEL_IMAGE_QUERY_LLM", True), \
             patch("cqc_lem.utilities.ai.client.client") as mock_client:
            mock_client.chat.completions.create.return_value = mock_resp
            q = derive_image_query("Cloud Security", "zero trust", "industry_insights")
        assert q  # non-empty heuristic


@pytest.mark.unit
class TestAvatarGating:
    def test_no_user_id_no_avatar(self):
        assert _user_has_active_avatar(None) is False

    def test_active_avatar_true(self):
        avatar = {"status": "succeeded", "model_ref": "user/model:v1",
                  "approval_status": "approved"}
        with patch("cqc_lem.utilities.db.get_avatar_preferences",
                   return_value={"avatar_use_carousel": True}), \
             patch("cqc_lem.utilities.db.get_post_use_avatar", return_value=None), \
             patch("cqc_lem.utilities.db.get_active_avatar", return_value=avatar):
            assert _user_has_active_avatar(5) is True

    def test_incomplete_avatar_false(self):
        with patch("cqc_lem.utilities.db.get_avatar_preferences",
                   return_value={"avatar_use_carousel": True}), \
             patch("cqc_lem.utilities.db.get_post_use_avatar", return_value=None), \
             patch("cqc_lem.utilities.db.get_active_avatar", return_value={"status": "training"}):
            assert _user_has_active_avatar(5) is False

    def test_avatar_lookup_error_false(self):
        with patch("cqc_lem.utilities.db.get_avatar_preferences",
                   return_value={"avatar_use_carousel": True}), \
             patch("cqc_lem.utilities.db.get_post_use_avatar", return_value=None), \
             patch("cqc_lem.utilities.db.get_active_avatar", side_effect=Exception("db down")):
            assert _user_has_active_avatar(5) is False

    def test_carousel_opt_in_off_false(self):
        """Guardrail (issue #744): an approved avatar is still not used until the user opts
        carousels in."""
        avatar = {"status": "succeeded", "model_ref": "user/model:v1",
                  "approval_status": "approved"}
        with patch("cqc_lem.utilities.db.get_avatar_preferences",
                   return_value={"avatar_use_carousel": False}), \
             patch("cqc_lem.utilities.db.get_post_use_avatar", return_value=None), \
             patch("cqc_lem.utilities.db.get_active_avatar", return_value=avatar):
            assert _user_has_active_avatar(5) is False

    def test_replicate_disabled_false(self):
        with patch(f"{ENV}.CAROUSEL_REPLICATE_ENABLED", False):
            assert _should_generate_with_replicate(1, 2, 5, "personal_story") is False

    def test_replicate_irrelevant_content_type_false(self):
        with patch(f"{ENV}.CAROUSEL_REPLICATE_ENABLED", True), \
             patch(f"{ENV}.CAROUSEL_REPLICATE_RATE", 1.0):
            assert _should_generate_with_replicate(1, 2, 5, "educational") is False

    def test_replicate_all_gates_pass_true(self):
        avatar = {"status": "succeeded", "model_ref": "user/model:v1",
                  "approval_status": "approved"}
        with patch(f"{ENV}.CAROUSEL_REPLICATE_ENABLED", True), \
             patch(f"{ENV}.CAROUSEL_REPLICATE_RATE", 1.0), \
             patch("cqc_lem.utilities.db.get_avatar_preferences",
                   return_value={"avatar_use_carousel": True}), \
             patch("cqc_lem.utilities.db.get_post_use_avatar", return_value=None), \
             patch("cqc_lem.utilities.db.get_active_avatar", return_value=avatar):
            assert _should_generate_with_replicate(1, 2, 5, "personal_story") is True

    def test_replicate_rate_zero_false(self):
        with patch(f"{ENV}.CAROUSEL_REPLICATE_ENABLED", True), \
             patch(f"{ENV}.CAROUSEL_REPLICATE_RATE", 0.0):
            assert _should_generate_with_replicate(1, 2, 5, "personal_story") is False


@pytest.mark.unit
class TestGenerateAvatarSlideImage:
    def test_returns_path(self):
        with patch("cqc_lem.utilities.ai.ai_helper.generate_post_image", return_value="/tmp/gen.png"):
            assert _generate_avatar_slide_image("team", 5) == "/tmp/gen.png"

    def test_failure_returns_none(self):
        with patch("cqc_lem.utilities.ai.ai_helper.generate_post_image", side_effect=Exception("api")):
            assert _generate_avatar_slide_image("team", 5) is None

    def test_routes_through_the_carousel_surface_with_the_post_id(self):
        with patch("cqc_lem.utilities.ai.ai_helper.generate_post_image",
                   return_value="/tmp/gen.png") as gen:
            _generate_avatar_slide_image("team retro", 5, 9, "personal_story")
        assert gen.call_args.kwargs["surface"] == "carousel"
        assert gen.call_args.kwargs["post_id"] == 9
        assert gen.call_args.kwargs["depicts_person"] is True

    def test_object_query_is_not_routed_to_the_lora(self):
        """Prepending the trigger word to 'quarterly dashboard metrics' asked the LoRA to insert
        the user's face into a scene never written to contain a person (issue #744)."""
        with patch("cqc_lem.utilities.ai.ai_helper.generate_post_image",
                   return_value="/tmp/gen.png") as gen:
            _generate_avatar_slide_image("quarterly dashboard metrics", 5, 9, "educational")
        assert gen.call_args.kwargs["depicts_person"] is False


@pytest.mark.unit
class TestQueryDepictsPerson:
    def test_personal_story_always_depicts_the_author(self):
        assert cc._query_depicts_person("quarterly dashboard", "personal_story") is True

    @pytest.mark.parametrize("query", ["a team retro", "client meeting", "founder portrait"])
    def test_person_terms_detected(self, query):
        assert cc._query_depicts_person(query, "educational") is True

    @pytest.mark.parametrize("query", ["dashboard metrics", "server rack", "", None])
    def test_object_queries_rejected(self, query):
        assert cc._query_depicts_person(query, "educational") is False

    def test_punctuation_does_not_hide_a_person_term(self):
        assert cc._query_depicts_person("(team), retro", "educational") is True


@pytest.mark.unit
class TestSelectSlideImage:
    def test_disabled_returns_default(self):
        with patch.object(cc, "_should_include_slide_image", return_value=False):
            assert select_slide_image(
                title="T", content="C", content_type="educational",
                post_id=1, slide_index=2, default_path="/def.png",
            ) == "/def.png"

    def test_pexels_first_path(self):
        with patch.object(cc, "_should_include_slide_image", return_value=True), \
             patch.object(cc, "derive_image_query", return_value="team laptop"), \
             patch.object(cc, "_should_generate_with_replicate", return_value=False), \
             patch(f"{ENV}.CAROUSEL_PEXELS_ENABLED", True), \
             patch.object(cc, "get_pexels_image_path", return_value="/tmp/pexels.jpg") as mock_pex:
            result = select_slide_image(
                title="T", content="C", content_type="educational",
                post_id=1, slide_index=2, default_path="/def.png",
            )
        assert result == "/tmp/pexels.jpg"
        mock_pex.assert_called_once()

    def test_replicate_used_when_gated_in(self):
        with patch.object(cc, "_should_include_slide_image", return_value=True), \
             patch.object(cc, "derive_image_query", return_value="founder story"), \
             patch.object(cc, "_should_generate_with_replicate", return_value=True), \
             patch.object(cc, "_generate_avatar_slide_image", return_value="/tmp/avatar.png") as mock_gen, \
             patch.object(cc, "get_pexels_image_path") as mock_pex:
            result = select_slide_image(
                title="T", content="C", content_type="personal_story",
                post_id=1, slide_index=2, user_id=5, default_path="/def.png",
            )
        assert result == "/tmp/avatar.png"
        mock_gen.assert_called_once()
        mock_pex.assert_not_called()

    def test_replicate_failure_falls_back_to_pexels(self):
        with patch.object(cc, "_should_include_slide_image", return_value=True), \
             patch.object(cc, "derive_image_query", return_value="founder story"), \
             patch.object(cc, "_should_generate_with_replicate", return_value=True), \
             patch.object(cc, "_generate_avatar_slide_image", return_value=None), \
             patch(f"{ENV}.CAROUSEL_PEXELS_ENABLED", True), \
             patch.object(cc, "get_pexels_image_path", return_value="/tmp/pexels.jpg"):
            result = select_slide_image(
                title="T", content="C", content_type="personal_story",
                post_id=1, slide_index=2, user_id=5, default_path="/def.png",
            )
        assert result == "/tmp/pexels.jpg"

    def test_all_sources_fail_returns_default(self):
        with patch.object(cc, "_should_include_slide_image", return_value=True), \
             patch.object(cc, "derive_image_query", return_value="x"), \
             patch.object(cc, "_should_generate_with_replicate", return_value=False), \
             patch(f"{ENV}.CAROUSEL_PEXELS_ENABLED", True), \
             patch.object(cc, "get_pexels_image_path", return_value=None):
            result = select_slide_image(
                title="T", content="C", content_type="educational",
                post_id=1, slide_index=2, default_path="/def.png",
            )
        assert result == "/def.png"

    def test_pexels_disabled_returns_default(self):
        with patch.object(cc, "_should_include_slide_image", return_value=True), \
             patch.object(cc, "derive_image_query", return_value="x"), \
             patch.object(cc, "_should_generate_with_replicate", return_value=False), \
             patch(f"{ENV}.CAROUSEL_PEXELS_ENABLED", False):
            result = select_slide_image(
                title="T", content="C", content_type="educational",
                post_id=1, slide_index=2, default_path=None,
            )
        assert result is None


@pytest.mark.unit
class TestCarouselIntegrationRouting:
    """End-to-end: an image-bearing content slide never lands on a no-picture layout."""

    def _make_prs(self):
        # Minimal fake presentation capturing which layouts were used.
        from cqc_lem.utilities.carousel_creator import EducationalContentCarousel
        carousel = EducationalContentCarousel(**{
            "cover": {"title": "Cover", "content": "Sub"},
            "contents": [
                {"title": "Tip One", "content": "Body one"},
                {"title": "Tip Two", "content": "Body two"},
            ],
            "call_to_action": {"title": "CTA", "content": "Act now"},
        })
        return carousel

    def test_image_slides_use_picture_layouts(self):
        carousel = self._make_prs()
        prs = MagicMock()
        used = []

        def fake_choose(image_path, post_id, slide_index, *a, **k):
            def _layout(**kwargs):
                used.append((slide_index, "image" if kwargs.get("image_path") else "text"))
                return MagicMock()
            return _layout

        with patch.object(cc, "select_slide_image", return_value="/tmp/pic.jpg"), \
             patch.object(cc, "choose_content_layout", side_effect=fake_choose), \
             patch.object(cc, "get_default_image_path", return_value="/def.png"), \
             patch.object(cc, "get_pexels_image_path", return_value="/tmp/cta.jpg"), \
             patch.object(cc, "create_title_layout_slide", return_value=MagicMock()), \
             patch.object(cc, "create_title_only_layout_slide", return_value=MagicMock()), \
             patch.object(cc, "create_title_and_body_layout_slide", return_value=MagicMock()), \
             patch.object(cc, "create_section_title_and_description_layout_slide", return_value=MagicMock()), \
             patch.object(cc, "create_custom_3_1_layout_slide", return_value=MagicMock()), \
             patch.object(cc, "create_caption_only_layout_slide", return_value=MagicMock()):
            cc.create_ppt_educational_content_carousel(prs, carousel, post_id=99, user_id=None)

        # Both content slides (index 2 and 3) got an image.
        assert used == [(2, "image"), (3, "image")]


# ── Pillow renderer: the actually-posted PNG carousel ─────────────────────────

def _write_solid_image(color, size=(800, 600), suffix=".jpg"):
    from PIL import Image
    path = tempfile.NamedTemporaryFile(delete=False, suffix=suffix).name
    Image.new("RGB", size, color).save(path)
    return path


def _edu_carousel():
    return EducationalContentCarousel(**{
        "cover": {"title": "5 Productivity Tips", "content": "Get more done"},
        "contents": [
            {"title": "Time Blocking", "content": "Reserve focused blocks for deep work."},
            {"title": "Batch Tasks", "content": "Group similar tasks to cut context switching."},
        ],
        "call_to_action": {"title": "Your turn", "content": "Which tip will you try?"},
    })


@pytest.mark.unit
class TestFitAndCropImage:
    def test_exact_size_and_rgb(self):
        src = _write_solid_image((10, 20, 30), size=(400, 200), suffix=".png")
        panel = _fit_and_crop_image(src, 1080, 360)
        assert panel.size == (1080, 360)
        assert panel.mode == "RGB"

    def test_flattens_transparency(self):
        from PIL import Image
        src = tempfile.NamedTemporaryFile(delete=False, suffix=".png").name
        Image.new("RGBA", (300, 300), (0, 128, 255, 128)).save(src)
        panel = _fit_and_crop_image(src, 200, 200)
        assert panel.mode == "RGB"
        assert panel.size == (200, 200)

    def test_bad_image_raises(self):
        bad = tempfile.NamedTemporaryFile(delete=False, suffix=".jpg").name
        with open(bad, "w") as fh:
            fh.write("not an image")
        with pytest.raises(Exception):
            _fit_and_crop_image(bad, 100, 100)


@pytest.mark.unit
class TestContentTypeMapping:
    def test_maps_known_type(self):
        assert _carousel_content_type(_edu_carousel()) == "educational"

    def test_unknown_defaults_professional(self):
        assert _carousel_content_type(object()) == "professional"


@pytest.mark.unit
class TestPillowComposition:
    """The posted PNGs: content slides composite a photo band; cover/CTA never do;
    a miss or decode failure falls back to text-only. RED marks the image region."""

    RED = (220, 30, 30)
    BAND_SAMPLE = (540, 860)  # deep inside the bottom photo band on content slides

    def _render(self, template, image_return, post_id=1):
        from PIL import Image
        outdir = tempfile.mkdtemp()
        with patch.object(cc, "select_slide_image", return_value=image_return):
            paths = create_carousel_slide_images(
                _edu_carousel(), post_id=post_id,
                output_dir=outdir, template=template,
            )
        return [Image.open(p).convert("RGB") for p in paths]

    @pytest.mark.parametrize("template", ["bold_listicle", "minimal_dark", "stat_reveal",
                                          "step_framework", "story_arc"])
    def test_content_slide_has_image_band(self, template):
        img_path = _write_solid_image(self.RED)
        slides = self._render(template, img_path)
        content = slides[1]  # slide_02 = first content slide
        assert content.size == (1080, 1080)
        px = content.getpixel(self.BAND_SAMPLE)
        assert px[0] > 150 and px[1] < 100 and px[2] < 100, f"{template} band not composited: {px}"

    def test_cover_and_cta_have_no_band(self):
        img_path = _write_solid_image(self.RED)
        slides = self._render("bold_listicle", img_path)
        cover_px = slides[0].getpixel(self.BAND_SAMPLE)
        cta_px = slides[-1].getpixel(self.BAND_SAMPLE)
        # Neither cover nor CTA should carry the red photo band.
        assert not (cover_px[0] > 150 and cover_px[1] < 100 and cover_px[2] < 100)
        assert not (cta_px[0] > 150 and cta_px[1] < 100 and cta_px[2] < 100)

    def test_no_image_falls_back_to_text_only(self):
        slides = self._render("bold_listicle", None, post_id=2)
        content = slides[1]
        assert content.size == (1080, 1080)
        px = content.getpixel(self.BAND_SAMPLE)
        assert not (px[0] > 150 and px[1] < 100 and px[2] < 100)  # no red band

    def test_decode_failure_falls_back_without_crashing(self):
        bad = tempfile.NamedTemporaryFile(delete=False, suffix=".jpg").name
        with open(bad, "w") as fh:
            fh.write("not an image")
        slides = self._render("minimal_dark", bad, post_id=3)
        content = slides[1]
        assert content.size == (1080, 1080)
        px = content.getpixel(self.BAND_SAMPLE)
        assert not (px[0] > 150 and px[1] < 100 and px[2] < 100)

    def test_selection_engine_called_per_content_slide_with_seed(self):
        outdir = tempfile.mkdtemp()
        with patch.object(cc, "select_slide_image", return_value=None) as mock_sel:
            create_carousel_slide_images(_edu_carousel(), post_id=77, output_dir=outdir,
                                         template="bold_listicle", user_id=5)
        # 2 content slides (cover + CTA excluded); seeded by post_id + slide_index.
        assert mock_sel.call_count == 2
        seen = {(c.kwargs["post_id"], c.kwargs["slide_index"], c.kwargs["user_id"])
                for c in mock_sel.call_args_list}
        assert seen == {(77, 2, 5), (77, 3, 5)}


@pytest.mark.unit
class TestWrapText:
    """Word-wrap must never produce a line wider than the budget — including long
    tokens (URLs) which are hard-broken — so slide text never bleeds past the margin."""

    def _draw_font(self):
        from PIL import Image, ImageDraw, ImageFont
        draw = ImageDraw.Draw(Image.new("RGB", (10, 10)))
        return draw, ImageFont.load_default()

    def test_empty_returns_empty(self):
        draw, font = self._draw_font()
        assert cc._wrap_text("", font, 100, draw) == []

    def test_wraps_within_max(self):
        draw, font = self._draw_font()
        text = "the quick brown fox jumps over the lazy dog " * 4
        max_px = 120
        lines = cc._wrap_text(text, font, max_px, draw)
        assert len(lines) > 1
        for ln in lines:
            assert draw.textlength(ln, font=font) <= max_px

    def test_long_token_is_hard_broken_within_max(self):
        draw, font = self._draw_font()
        long_word = "https://example.com/" + "a" * 400
        max_px = 90
        lines = cc._wrap_text(long_word, font, max_px, draw)
        assert len(lines) > 1
        for ln in lines:
            assert draw.textlength(ln, font=font) <= max_px
        # No context lost — every character survives the break.
        assert "".join(lines) == long_word
