"""A regenerated carousel must publish new asset URLs so cached slide bytes cannot stick."""

from pathlib import Path
from unittest.mock import patch

import pytest

pytest.importorskip("PIL")

from cqc_lem.utilities.carousel_creator import (  # noqa: E402
    EducationalContentCarousel,
    EducationalContentSlide,
    create_carousel_slide_images,
)


@pytest.mark.unit
def test_each_render_uses_distinct_slide_asset_names(tmp_path) -> None:
    deck = EducationalContentCarousel(
        cover=EducationalContentSlide(title="Updated plan", content="What changed"),
        contents=[EducationalContentSlide(title="New approach", content="Ship the fix")],
        call_to_action=EducationalContentSlide(title="Next step", content="Try it today"),
    )
    with patch("cqc_lem.utilities.carousel_creator.select_slide_image", return_value=None), \
            patch("cqc_lem.utilities.carousel_creator.secrets.token_hex",
                  side_effect=["first-render", "second-render"]):
        first = create_carousel_slide_images(deck, post_id=42, output_dir=str(tmp_path),
                                             template="bold_listicle")
        second = create_carousel_slide_images(deck, post_id=42, output_dir=str(tmp_path),
                                              template="bold_listicle")

    assert len(first) == len(second) == 3
    assert all("first-render" in path for path in first)
    assert all("second-render" in path for path in second)
    assert set(first).isdisjoint(second)
    assert all((tmp_path / Path(path).name).is_file()
               for path in [*first, *second])
