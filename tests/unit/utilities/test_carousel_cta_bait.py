"""The closing slide never PAINTS engagement bait (issue #1511).

`save_worthy_directive` forbids the writer an engagement-bait close, but
``_listicle_cta`` drew a hardcoded **"Leave a comment below"** pill above whatever CTA
the model wrote — on `DEFAULT_TEMPLATE`, i.e. the most common closing slide LEM ships.
Unlike a caption it cannot be repaired downstream: `strip_engagement_bait` and the
`bait_closer` slop check operate on TEXT and this is pixels.

These tests do not eyeball the render. They wrap ``PIL.ImageDraw.text`` to record every
string the renderer actually PAINTS (the harness `test_carousel_text_fit.py` uses), run
the unmodified ``create_carousel_slide_images`` on every template, and assert no slide
carries a bait imperative — judged by `contains_engagement_bait`, the ONE bait detector,
plus the literal imperatives named in the issue.
"""

import os
import tempfile
from contextlib import contextmanager
from unittest.mock import patch

import pytest

pytest.importorskip("PIL")

from PIL import Image, ImageDraw  # noqa: E402

from cqc_lem.utilities.ai.content_framework import (  # noqa: E402
    SAVE_ASK_PHRASE,
    SAVE_ASK_PILL,
    SAVE_ASK_STEM,
    save_worthy_directive,
)
from cqc_lem.utilities.carousel_creator import (  # noqa: E402
    CAROUSEL_TEMPLATES,
    DEFAULT_TEMPLATE,
    EducationalContentCarousel,
    EducationalContentSlide,
    create_carousel_slide_images,
)
from cqc_lem.utilities.linkedin_formatter import contains_engagement_bait  # noqa: E402

# Reflex-action asks a slide must never carry. The detector catches most of them; the
# ones it does not ("leave a comment" with no trigger word after it — exactly the pill
# this issue is about) are listed so a re-introduction is caught by name.
BAIT_IMPERATIVES = (
    "comment below",
    "leave a comment",
    "drop a comment",
    "follow for more",
    "tag a friend",
    "like if you",
    "repost if you",
    "double tap",
    "smash the like",
)

# Deck copy with nothing bait-like in it, so anything the assertions catch is CHROME the
# layout drew of its own accord rather than something the test fed in.
CLEAN_TITLE = "The 5 checks I run before every release"
CLEAN_BODY = ("Tag the build, migrate the schema, run the gate, flip the color, keep the "
              "rollback tag. Skipping the fourth is how the last outage happened.")
CLEAN_CTA_TITLE = "Save this for your next release review"
CLEAN_CTA_BODY = "Which of the five does your team skip when the ship date slips?"


@contextmanager
def _recording_renderer(tmp_path):
    """Render recording painted strings PER slide (`Image.new` is the slide boundary)."""
    slides: list[list[str]] = []
    original_text = ImageDraw.ImageDraw.text
    original_new = Image.new

    def _record(self, xy, text, *args, **kwargs):
        if slides:
            slides[-1].append(text)
        return original_text(self, xy, text, *args, **kwargs)

    def _new_slide(mode, size, *args, **kwargs):
        if size == (1080, 1080):
            slides.append([])
        return original_new(mode, size, *args, **kwargs)

    band = os.path.join(str(tmp_path), "band.jpg")
    Image.new("RGB", (1200, 800), (40, 40, 40)).save(band)

    with patch.object(ImageDraw.ImageDraw, "text", _record), \
            patch.object(Image, "new", _new_slide), \
            patch("cqc_lem.utilities.carousel_creator.select_slide_image", return_value=band):
        yield slides


def _render(template: str, tmp_path) -> list[str]:
    """Render a 3-slide deck on `template`; one joined painted string per slide."""
    carousel = EducationalContentCarousel(
        cover=EducationalContentSlide(title=CLEAN_TITLE, content=CLEAN_BODY),
        contents=[EducationalContentSlide(title="The gate that holds", content=CLEAN_BODY)],
        call_to_action=EducationalContentSlide(title=CLEAN_CTA_TITLE, content=CLEAN_CTA_BODY),
    )
    out_dir = tempfile.mkdtemp(dir=str(tmp_path))
    with _recording_renderer(tmp_path) as slides:
        create_carousel_slide_images(carousel, post_id=1511, output_dir=out_dir, template=template)
    return [" ".join(painted) for painted in slides]


@pytest.mark.unit
class TestNoLayoutPaintsEngagementBait:
    """No `*_cta` layout — and no other slide role — draws a bait imperative."""

    @pytest.mark.parametrize("template", sorted(CAROUSEL_TEMPLATES))
    def test_closing_slide_carries_no_bait_imperative(self, template, tmp_path):
        cta = _render(template, tmp_path)[-1]

        assert not contains_engagement_bait(cta), f"{template}: bait on the CTA slide -> {cta!r}"
        for imperative in BAIT_IMPERATIVES:
            assert imperative not in cta.lower(), f"{template}: {imperative!r} in {cta!r}"

    @pytest.mark.parametrize("template", sorted(CAROUSEL_TEMPLATES))
    def test_no_slide_of_the_deck_carries_a_bait_imperative(self, template, tmp_path):
        # A cover or body layout could grow the same hardcoded pill; the ask is that NO
        # carousel layout renders one, not just the closing three.
        for idx, rendered in enumerate(_render(template, tmp_path), start=1):
            assert not contains_engagement_bait(rendered), \
                f"{template} slide {idx}: bait -> {rendered!r}"
            for imperative in BAIT_IMPERATIVES:
                assert imperative not in rendered.lower(), \
                    f"{template} slide {idx}: {imperative!r} in {rendered!r}"


@pytest.mark.unit
class TestThePillStatesTheSaveAsk:
    """The pill the bait replaced states the directive's own ask, from ONE constant."""

    def test_default_template_paints_the_save_ask_pill(self, tmp_path):
        cta = _render(DEFAULT_TEMPLATE, tmp_path)[-1]
        assert SAVE_ASK_PILL in cta

    def test_the_pill_and_the_writer_directive_share_one_source(self):
        # The render side cannot disagree with the writer side again: both strings are
        # built from SAVE_ASK_STEM, and the directive is where that stem is stated.
        directive = save_worthy_directive("carousel").lower()
        assert SAVE_ASK_STEM in directive
        assert SAVE_ASK_PHRASE in directive
        assert SAVE_ASK_PILL.lower().startswith(SAVE_ASK_STEM)
        assert not contains_engagement_bait(SAVE_ASK_PILL)
