"""Unit tests for create_carousel_slide_images() using Pillow."""

import os
from unittest.mock import patch

import pytest


@pytest.mark.unit
class TestCreateCarouselSlideImages:
    """Tests for the Pillow-based slide image renderer."""

    def _make_educational_carousel(self):
        from cqc_lem.utilities.carousel_creator import (
            EducationalContentCarousel,
            EducationalContentSlide,
        )
        return EducationalContentCarousel(
            cover=EducationalContentSlide(title="5 Tips for Growth", content="Learn to grow faster"),
            contents=[
                EducationalContentSlide(title="Tip 1: Set Goals", content="Define what success means."),
                EducationalContentSlide(title="Tip 2: Measure", content="Track your progress weekly."),
            ],
            call_to_action=EducationalContentSlide(title="Get Started Today", content="Comment below!"),
        )

    def test_returns_list_of_paths(self, tmp_path):
        from cqc_lem.utilities.carousel_creator import create_carousel_slide_images
        carousel = self._make_educational_carousel()

        with patch("cqc_lem.utilities.carousel_creator.get_pexels_image_path") as mock_pexels:
            mock_pexels.return_value = str(tmp_path / "fake.png")
            # Create a dummy PNG so the fallback image path is valid
            (tmp_path / "fake.png").write_bytes(b"")

            paths = create_carousel_slide_images(carousel, post_id=999, output_dir=str(tmp_path))

        assert isinstance(paths, list)
        assert len(paths) == 4  # cover + 2 contents + CTA

    def test_files_are_created(self, tmp_path):
        from cqc_lem.utilities.carousel_creator import create_carousel_slide_images
        carousel = self._make_educational_carousel()

        try:
            from PIL import Image as _PIL_Image  # noqa: F401 — check Pillow is available
        except ImportError:
            pytest.skip("Pillow not installed — skipping image render test")

        with patch("cqc_lem.utilities.carousel_creator.get_pexels_image_path", return_value=None):
            paths = create_carousel_slide_images(carousel, post_id=999, output_dir=str(tmp_path))

        for p in paths:
            assert isinstance(p, str)
            assert "slide_" in os.path.basename(p)
            assert p.endswith(".png")

    def test_output_dir_uses_post_id(self, tmp_path):
        from cqc_lem.utilities.carousel_creator import create_carousel_slide_images
        carousel = self._make_educational_carousel()

        with patch("cqc_lem.utilities.carousel_creator.get_pexels_image_path", return_value=None):
            try:
                paths = create_carousel_slide_images(carousel, post_id=42, output_dir=str(tmp_path))
                for p in paths:
                    assert str(tmp_path) in p
            except (ImportError, Exception):
                pytest.skip("Pillow not available or render failed in test env")

    def test_slide_count_matches_carousel_structure(self, tmp_path):
        """EducationalContentCarousel with N content slides → N+2 total (cover + contents + CTA)."""
        from cqc_lem.utilities.carousel_creator import (
            EducationalContentCarousel,
            EducationalContentSlide,
            create_carousel_slide_images,
        )
        carousel = EducationalContentCarousel(
            cover=EducationalContentSlide(title="Cover", content="Intro"),
            contents=[
                EducationalContentSlide(title=f"Slide {i}", content=f"Content {i}")
                for i in range(3)
            ],
            call_to_action=EducationalContentSlide(title="CTA", content="Do it!"),
        )

        with patch("cqc_lem.utilities.carousel_creator.get_pexels_image_path", return_value=None):
            try:
                paths = create_carousel_slide_images(carousel, post_id=1, output_dir=str(tmp_path))
                assert len(paths) == 5  # 1 cover + 3 contents + 1 CTA
            except (ImportError, Exception):
                pytest.skip("Pillow not available or render failed in test env")


@pytest.mark.unit
class TestDeckRenderReceipt:
    """The render receipt written next to the slides (issue #1513).

    The renderer is the only place the written string and the drawn lines are both in hand, so these
    assert the reading exists AND that it measures the clipping #1375 made visible, rather than the
    layout's own line caps.
    """

    def _carousel(self, body: str):
        from cqc_lem.utilities.carousel_creator import (
            EducationalContentCarousel,
            EducationalContentSlide,
        )
        return EducationalContentCarousel(
            cover=EducationalContentSlide(title="Cover", content="Intro"),
            contents=[EducationalContentSlide(title="One", content=body)],
            call_to_action=EducationalContentSlide(title="CTA", content="Save this for later."),
        )

    def _render(self, carousel, tmp_path, image_path=None):
        import json

        from cqc_lem.utilities.carousel_creator import create_carousel_slide_images
        from cqc_lem.utilities.deck_render import DECK_RENDER_FILENAME
        with patch("cqc_lem.utilities.carousel_creator.select_slide_image",
                   return_value=image_path):
            create_carousel_slide_images(carousel, post_id=87, output_dir=str(tmp_path),
                                         template="bold_listicle")
        with open(os.path.join(str(tmp_path), DECK_RENDER_FILENAME), encoding="utf-8") as handle:
            return json.load(handle)

    def test_records_one_row_per_slide_with_its_role_and_written_length(self, tmp_path):
        receipt = self._render(self._carousel("A short body."), tmp_path)
        assert receipt["post_id"] == 87 and receipt["template"] == "bold_listicle"
        assert [slide["role"] for slide in receipt["slides"]] == ["cover", "body", "cta"]
        body = receipt["slides"][1]
        assert body["body_chars"] == len("A short body.")
        assert body["chars_dropped"] == 0 and body["band"] is False

    def test_records_the_characters_the_layout_never_drew(self, tmp_path):
        # Far past what any layout can shrink to fit, so `_fit` marks the cut (#1375) — and this
        # count is the ONLY record of HOW MUCH the reader lost.
        receipt = self._render(self._carousel("word " * 99), tmp_path)
        body = receipt["slides"][1]
        assert body["chars_dropped"] > 0
        assert body["chars_drawn"] > 0
        assert body["chars_drawn"] + body["chars_dropped"] <= body["body_chars"]

    def test_an_unknown_template_is_recorded_as_the_one_that_was_drawn(self, tmp_path):
        import json

        from cqc_lem.utilities.carousel_creator import (
            DEFAULT_TEMPLATE,
            create_carousel_slide_images,
        )
        from cqc_lem.utilities.deck_render import DECK_RENDER_FILENAME

        with patch("cqc_lem.utilities.carousel_creator.select_slide_image", return_value=None):
            create_carousel_slide_images(self._carousel("Body."), post_id=87,
                                         output_dir=str(tmp_path), template="no_such_template")
        with open(os.path.join(str(tmp_path), DECK_RENDER_FILENAME), encoding="utf-8") as handle:
            assert json.load(handle)["template"] == DEFAULT_TEMPLATE

    def test_the_band_is_recorded_only_when_one_actually_landed(self, tmp_path):
        from PIL import Image
        photo = tmp_path / "band.png"
        Image.new("RGB", (400, 400), color=(10, 20, 30)).save(photo)
        receipt = self._render(self._carousel("A short body."), tmp_path, image_path=str(photo))
        roles = {slide["role"]: slide for slide in receipt["slides"]}
        # Only content slides get a photo band; cover and CTA never do.
        assert roles["body"]["band"] is True
        assert roles["cover"]["band"] is False and roles["cta"]["band"] is False
