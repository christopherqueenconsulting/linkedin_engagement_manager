"""Rendered-text regression tests for carousel slides (issue #1375).

Four shipped slides — including a closing CTA — ended mid-sentence because
``_draw_block`` iterated ``lines[:max_lines]`` and silently dropped the rest. These
tests do not eyeball the render: they wrap ``PIL.ImageDraw.text`` to record every
string the renderer actually PAINTS, run the unmodified
``create_carousel_slide_images``, and compare the painted text against the input.

Every body slide in production carries a photo band (``CAROUSEL_IMAGE_RATE`` defaults
to 1.0), and the band branch is the tighter of the two line caps — so the band is
present in every test here.
"""

import os
import tempfile
from contextlib import contextmanager
from unittest.mock import patch

import pytest

pytest.importorskip("PIL")

from PIL import Image, ImageDraw  # noqa: E402

from cqc_lem.utilities.carousel_creator import (  # noqa: E402
    CAROUSEL_SLIDE_BODY_MAX_CHARS,
    CAROUSEL_TEMPLATES,
    CAROUSEL_TRUNCATION_MARKER,
    EducationalContentCarousel,
    EducationalContentSlide,
    create_carousel_slide_images,
)

# The four slides issue #1375 recorded shipping clipped, verbatim from the issue.
SHIPPED_CLIPPED_BODIES = {
    "post87_slide3": (
        "Case study: 160 tagged releases in 32 days (June 25 to July 27, 2026). Result: "
        "Shipping became a non-event due to automated release trains."
    ),
    "post87_slide4": (
        "I lean toward high-frequency automation. If a pipeline can handle 5 releases a day "
        "without manual intervention, the risk per release drops."
    ),
    "post87_slide5_cta": (
        "Do you prefer low-volume certainty or high-frequency automation? Save this for your "
        "next sprint retrospective to spark the debate."
    ),
    "post86_slide3": (
        "1. Autonomous agent pipeline - writes code and opens PRs. 2. CI gate with test suite - "
        "blocks failures. 3. Coverage floor >=80% - ensures quality."
    ),
}


# Bodies of EXACTLY CAROUSEL_SLIDE_BODY_MAX_CHARS characters, in the word shapes that
# set widest. The budget is a character count the prompt states, but the renderer bounds
# PIXELS — so it only holds if it holds for the widest shapes, not just for average
# prose. `caps` and `wide` are what caught the three blocks whose line caps were too
# tight to honour the budget at all.
BUDGET_BODIES = {
    "prose": (
        "Release gate rule: a coverage floor at 80 percent, timestamped migrations, "
        "one named reviewer per diff, and a rollback tag recorded before any deploy."
    ),
    "caps": (
        "WE SHIPPED 160 RELEASES IN 32 DAYS WITH ZERO ROLLBACKS AND NO MANUAL STEPS AT "
        "ALL, WHICH CHANGED HOW THE WHOLE TEAM PLANS EVERY SINGLE SPRINT AT WORK."
    ),
    "wide": (
        "Implementing infrastructure observability requires distributed instrumentation, "
        "comprehensive documentation, plus uncompromising reliability tracking."
    ),
    "narrow": (
        "It is a fact: if it is in the list it is in the test, and if it is in the test "
        "it is in the log, so it is in the plan and it is on the ship list here."
    ),
}


@contextmanager
def _recording_renderer(tmp_path):
    """Render with a photo band present, recording painted strings PER slide.

    Every layout opens its slide with ``Image.new``, so that call is the slide
    boundary: the yielded list holds one list of painted strings per rendered slide.
    """
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


def _render(body: str, template: str, tmp_path) -> list[str]:
    """Render a 3-slide deck whose cover, body and CTA all carry `body`.

    Returns one string per slide — everything that slide painted, joined — so a caller
    asserts on what a reader would actually see rather than on the model that went in.
    Wrapping splits on whitespace and these strings are re-joined on it, so an intact
    body reappears verbatim inside its slide's text.
    """
    carousel = EducationalContentCarousel(
        cover=EducationalContentSlide(title="Ship faster without breaking prod", content=body),
        contents=[EducationalContentSlide(title="The release gate that holds", content=body)],
        call_to_action=EducationalContentSlide(title="Save this for your next retro", content=body),
    )
    out_dir = tempfile.mkdtemp(dir=str(tmp_path))
    with _recording_renderer(tmp_path) as slides:
        create_carousel_slide_images(carousel, post_id=1375, output_dir=out_dir, template=template)
    # `step_framework` paints its own "->" marker between wrapped lines (F3 of the
    # #1139 audit, tracked separately as #1510). Dropping the marker keeps these tests
    # about the LENGTH contract and nothing else.
    return [" ".join(painted).replace("-> ", "") for painted in slides]


@pytest.mark.unit
class TestSlideBodyBudgetRendersIntact:
    """The ONE budget the prompt states is a number the renderer honours."""

    @pytest.mark.parametrize("template", sorted(CAROUSEL_TEMPLATES))
    @pytest.mark.parametrize("shape", sorted(BUDGET_BODIES))
    def test_body_at_budget_is_never_clipped(self, shape, template, tmp_path):
        # A character budget is not a pixel budget: the same 150 characters are ~30%
        # wider set in caps or in long words. One lucky string proves nothing, so the
        # budget is asserted across the word shapes a writer actually produces.
        body = BUDGET_BODIES[shape]
        assert len(body) == CAROUSEL_SLIDE_BODY_MAX_CHARS, (
            f"The {shape} body is the budget under test — keep it exactly "
            f"{CAROUSEL_SLIDE_BODY_MAX_CHARS} characters"
        )

        slides = _render(body, template, tmp_path)

        # Cover, body and CTA slide all carry it; every one must be whole.
        assert len(slides) == 3
        for rendered in slides:
            assert CAROUSEL_TRUNCATION_MARKER not in rendered
            assert body in rendered

    @pytest.mark.parametrize("name,body", sorted(SHIPPED_CLIPPED_BODIES.items()))
    def test_the_four_shipped_slides_render_intact(self, name, body, tmp_path):
        # bold_listicle is DEFAULT_TEMPLATE and the one posts 86/87 shipped on.
        slides = _render(body, "bold_listicle", tmp_path)

        assert len(slides) == 3
        for rendered in slides:
            assert body in rendered, f"{name} still clipped: {rendered!r}"

    def test_cta_slide_specifically_survives_on_every_template(self, tmp_path):
        cta = SHIPPED_CLIPPED_BODIES["post87_slide5_cta"]
        for template in sorted(CAROUSEL_TEMPLATES):
            slides = _render(cta, template, tmp_path)
            # The CTA is the LAST slide of the deck.
            assert cta in slides[-1], f"{template}: CTA clipped -> {slides[-1]!r}"


@pytest.mark.unit
class TestOverflowDegradesVisibly:
    """Past the budget the text may shrink or be marked — never vanish silently."""

    def test_absurdly_long_body_is_marked_not_silently_cut(self, tmp_path):
        body = ("Every release gate rule we run, stated in full and then restated at length "
                "so that no layout anywhere can hold it: ") * 4
        slides = _render(body, "bold_listicle", tmp_path)

        for rendered in slides:
            # Whatever was dropped is announced by the marker, so a reader never sees
            # a sentence simply stop.
            assert body not in rendered
            assert CAROUSEL_TRUNCATION_MARKER in rendered

    def test_truncation_is_logged_so_the_defect_is_visible(self, tmp_path):
        body = "A single unbroken statement of the rule, repeated until nothing can hold it. " * 6
        with patch("cqc_lem.utilities.logger.log_warning") as mock_warn:
            _render(body, "bold_listicle", tmp_path)
        assert mock_warn.called
        assert "truncated" in mock_warn.call_args_list[0].args[0]


@pytest.mark.unit
class TestFitTextBlock:
    """The fit engine itself — pure, so it is tested without rendering a deck."""

    def _draw(self):
        return ImageDraw.Draw(Image.new("RGB", (1080, 1080)))

    def _font(self, size=38):
        from cqc_lem.utilities.carousel_creator import load_slide_font
        font = load_slide_font(size, bold=False)
        if not hasattr(font, "font_variant"):
            pytest.skip("Pillow bitmap fallback cannot be resized — shrink path unavailable")
        return font

    def test_short_text_is_untouched(self):
        from cqc_lem.utilities.carousel_creator import fit_text_block
        draw, font = self._draw(), self._font()
        lines, fitted, truncated = fit_text_block("Two short words", font, 900, 3, 18, draw)
        assert lines == ["Two short words"]
        assert fitted is font
        assert truncated is False

    def test_overflow_shrinks_the_font_before_it_cuts(self):
        from cqc_lem.utilities.carousel_creator import _wrap_text, fit_text_block
        draw, font = self._draw(), self._font()
        text = ("Coverage floor at 80 percent, timestamped migrations, one named reviewer per "
                "diff, a rollback tag recorded before deploy, and a green CI run on the tag.")
        # 4 lines' worth of text into a 3-line block: shrink covers it, nothing is cut.
        assert len(_wrap_text(text, font, 900, draw)) == 4
        lines, fitted, truncated = fit_text_block(text, font, 900, 3, 18, draw)

        assert truncated is False
        assert fitted.size < font.size
        assert " ".join(lines) == text

    def test_marker_fits_the_line_it_is_appended_to(self):
        from cqc_lem.utilities.carousel_creator import fit_text_block
        draw, font = self._draw(), self._font()
        text = "Rule: " + ("coverage floor at eighty percent for every merged pull request. " * 12)
        lines, fitted, truncated = fit_text_block(text, font, 900, 2, 18, draw)

        assert truncated is True
        assert lines[-1].endswith(CAROUSEL_TRUNCATION_MARKER)
        assert draw.textlength(lines[-1], font=fitted) <= 900
