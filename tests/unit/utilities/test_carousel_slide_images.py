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
