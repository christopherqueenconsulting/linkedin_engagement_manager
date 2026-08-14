"""Rendered line-structure tests for carousel slides (issue #1510).

Findings F2 and F3 of the #1139 audit are both about the SHAPE of a slide's text, not
its length (#1375 owns that): ``_wrap_text`` split on newlines as if they were spaces,
so a checklist rendered as one run-on paragraph, and ``_step_content`` drew its ``->``
marker once per WRAPPED line, so one sentence became three mid-clause bullets.

Like ``test_carousel_text_fit``, these tests do not eyeball a PNG: they wrap
``PIL.ImageDraw.text`` to record every string the renderer PAINTS and where it painted
it, then assert the drawn line set — the reader's view — rather than that files exist.
"""

import os
import tempfile
from contextlib import contextmanager
from unittest.mock import patch

import pytest

pytest.importorskip("PIL")

from PIL import Image, ImageDraw  # noqa: E402

from cqc_lem.utilities.carousel_creator import (  # noqa: E402
    EducationalContentCarousel,
    EducationalContentSlide,
    create_carousel_slide_images,
)

# The checklist body from the audit, verbatim — the shape `reference_slide_directive`
# asks the writer for and `slide_artifacts` rewards.
CHECKLIST_BODY = (
    "Release gate checklist:\n"
    "- Coverage floor >= 80%\n"
    "- Migration version is a timestamp\n"
    "- One reviewer signs the diff"
)

# `_step_content`'s own geometry: the arrow sits at PAD, its point's lines at the indent.
STEP_PAD = 62
STEP_BULLET_INDENT = 52


@contextmanager
def _recording_renderer(band_path=None):
    """Record `(x, text)` for every painted string, grouped per slide.

    Every layout opens its slide with a 1080x1080 ``Image.new``, so that call is the
    slide boundary. `band_path` is what ``select_slide_image`` returns — None keeps the
    slide text-only, which is the looser of the two line caps and the one a multi-point
    body needs.
    """
    slides: list[list[tuple[int, str]]] = []
    original_text = ImageDraw.ImageDraw.text
    original_new = Image.new

    def _record(self, xy, text, *args, **kwargs):
        if slides:
            slides[-1].append((int(xy[0]), text))
        return original_text(self, xy, text, *args, **kwargs)

    def _new_slide(mode, size, *args, **kwargs):
        if size == (1080, 1080):
            slides.append([])
        return original_new(mode, size, *args, **kwargs)

    with patch.object(ImageDraw.ImageDraw, "text", _record), \
            patch.object(Image, "new", _new_slide), \
            patch("cqc_lem.utilities.carousel_creator.select_slide_image",
                  return_value=band_path):
        yield slides


def _render_body(body: str, template: str, tmp_path) -> list[tuple[int, str]]:
    """Render a 3-slide deck carrying `body` on its ONE content slide.

    Cover and CTA carry short text of their own so their tighter line caps never turn a
    structure question into a length one. Returns the content slide's painted
    `(x, text)` pairs in paint order.
    """
    carousel = EducationalContentCarousel(
        cover=EducationalContentSlide(title="Ship faster", content="How we hold the gate."),
        contents=[EducationalContentSlide(title="The release gate", content=body)],
        call_to_action=EducationalContentSlide(title="Save this", content="For your next retro."),
    )
    out_dir = tempfile.mkdtemp(dir=str(tmp_path))
    with _recording_renderer() as slides:
        create_carousel_slide_images(carousel, post_id=1510, output_dir=out_dir,
                                     template=template)
    assert len(slides) == 3
    return slides[1]


def _body_lines(painted: list[tuple[int, str]], source_words: set[str]) -> list[str]:
    """The painted strings that are body text, not chrome.

    Slide chrome (counters, step numbers, the `->` marker) shares the canvas with the
    body, so a line counts only when it carries a word the author actually wrote.
    """
    return [text for _x, text in painted
            if any(word in text for word in source_words)]


@pytest.mark.unit
class TestAuthoredLineBreaksSurvive:
    """F2 — a newline the writer typed is structure, never a space."""

    def test_checklist_renders_as_separate_lines(self, tmp_path):
        painted = _render_body(CHECKLIST_BODY, "bold_listicle", tmp_path)
        drawn = [text for _x, text in painted]

        for point in CHECKLIST_BODY.split("\n"):
            assert point in drawn, f"{point!r} was not drawn as its own line: {drawn!r}"

    def test_two_points_are_never_concatenated_into_one_line(self, tmp_path):
        painted = _render_body(CHECKLIST_BODY, "bold_listicle", tmp_path)

        # The measured before-state was 'Release gate checklist: - Coverage floor >= 80% -',
        # i.e. a line carrying the head of one point and the start of the next.
        for _x, text in painted:
            assert not ("checklist:" in text and "Coverage floor" in text), (
                f"points were run together on one line: {text!r}"
            )

    @pytest.mark.parametrize("template",
                             ["bold_listicle", "minimal_dark", "stat_reveal",
                              "step_framework", "story_arc"])
    def test_every_template_honours_the_line_breaks(self, template, tmp_path):
        body = "First point stands alone.\nSecond point stands alone."
        painted = _render_body(body, template, tmp_path)
        drawn = [text for _x, text in painted]

        assert "First point stands alone." in drawn
        assert "Second point stands alone." in drawn

    def test_blank_lines_do_not_spend_a_line_of_the_cap(self, tmp_path):
        body = "First point stands alone.\n\n\nSecond point stands alone."
        painted = _render_body(body, "bold_listicle", tmp_path)
        drawn = [text for _x, text in painted]

        assert "" not in drawn
        assert drawn.count("First point stands alone.") == 1

    def test_a_point_too_wide_for_the_slide_still_wraps(self, tmp_path):
        long_point = ("Coverage floor at eighty percent, timestamped migrations, one named "
                      "reviewer per diff, and a rollback tag recorded before any deploy.")
        painted = _render_body(f"Release gate:\n{long_point}", "bold_listicle", tmp_path)
        drawn = [text for _x, text in painted]

        assert "Release gate:" in drawn
        assert long_point not in drawn  # wrapped, not drawn as one over-wide line
        wrapped = _body_lines(painted, {"Coverage", "reviewer", "rollback"})
        assert len(wrapped) > 1
        assert " ".join(wrapped) == long_point


@pytest.mark.unit
class TestStepFrameworkBulletsPoints:
    """F3 — `step_framework` draws its marker per POINT, never per wrapped line."""

    def _arrows(self, painted) -> list[tuple[int, str]]:
        return [(x, text) for x, text in painted if text == "->"]

    def test_one_arrow_per_authored_point(self, tmp_path):
        body = ("Set the coverage floor.\n"
                "Timestamp every migration.\n"
                "Name one reviewer per diff.")
        painted = _render_body(body, "step_framework", tmp_path)

        assert len(self._arrows(painted)) == 3

    def test_a_wrapped_sentence_is_one_bullet_not_three(self, tmp_path):
        # The measured before-state: one sentence drawn as three '->' bullets, each
        # starting mid-clause.
        body = ("We rebuilt the release gate: coverage floor at 80 percent, timestamped "
                "migrations, and one named reviewer per diff.")
        painted = _render_body(body, "step_framework", tmp_path)

        assert len(self._arrows(painted)) == 1
        lines = _body_lines(painted, {"rebuilt", "migrations", "reviewer"})
        assert len(lines) > 1, "the sentence should still WRAP across lines"
        assert " ".join(lines) == body

    def test_continuation_lines_sit_under_their_own_bullet(self, tmp_path):
        body = ("We rebuilt the release gate: coverage floor at 80 percent, timestamped "
                "migrations, and one named reviewer per diff.\nThen we shipped it.")
        painted = _render_body(body, "step_framework", tmp_path)

        arrows = self._arrows(painted)
        assert len(arrows) == 2
        assert {x for x, _text in arrows} == {STEP_PAD}
        # Every body line — first line of a point or a continuation — is indented past
        # the marker column, so a continuation never reads as its own bullet.
        words = {"rebuilt", "migrations", "reviewer", "shipped"}
        body_x = {x for x, text in painted if any(word in text for word in words)}
        assert body_x == {STEP_PAD + STEP_BULLET_INDENT}

    def test_the_authors_own_bullet_glyph_is_not_drawn_twice(self, tmp_path):
        painted = _render_body(CHECKLIST_BODY, "step_framework", tmp_path)
        drawn = [text for _x, text in painted]

        assert len(self._arrows(painted)) == 4
        assert "- Coverage floor >= 80%" not in drawn
        assert "Coverage floor >= 80%" in drawn

    def test_a_leading_hyphen_that_is_not_a_bullet_is_kept(self, tmp_path):
        # '-5% churn' opens with a hyphen but no separator, so it is content, not a marker.
        painted = _render_body("-5% churn after the change", "step_framework", tmp_path)
        drawn = [text for _x, text in painted]

        assert "-5% churn after the change" in drawn


@pytest.mark.unit
class TestWrapTextHonoursNewlines:
    """The pure wrapper — the ONE place the flattening happened."""

    def _draw(self):
        return ImageDraw.Draw(Image.new("RGB", (1080, 1080)))

    def _font(self, size=38):
        from cqc_lem.utilities.carousel_creator import load_slide_font
        return load_slide_font(size, bold=False)

    def test_each_source_line_wraps_on_its_own(self):
        from cqc_lem.utilities.carousel_creator import _wrap_text
        lines = _wrap_text("Alpha\nBeta\nGamma", self._font(), 900, self._draw())
        assert lines == ["Alpha", "Beta", "Gamma"]

    def test_blank_and_padded_lines_are_dropped_and_stripped(self):
        from cqc_lem.utilities.carousel_creator import _wrap_text
        lines = _wrap_text("  Alpha  \n\n   \nBeta", self._font(), 900, self._draw())
        assert lines == ["Alpha", "Beta"]

    def test_points_group_by_source_line(self):
        from cqc_lem.utilities.carousel_creator import _wrap_points
        groups = _wrap_points("Alpha\nBeta", self._font(), 900, self._draw())
        assert groups == [["Alpha"], ["Beta"]]

    def test_fitted_lines_regroup_onto_their_points(self):
        from cqc_lem.utilities.carousel_creator import _group_fitted_lines
        font, draw = self._font(), self._draw()
        text = "Alpha one\nBeta two"
        groups = _group_fitted_lines(text, ["Alpha one", "Beta two"], font, 900, draw)
        assert groups == [["Alpha one"], ["Beta two"]]

    def test_a_truncated_tail_rides_the_last_point_it_belongs_to(self):
        from cqc_lem.utilities.carousel_creator import _group_fitted_lines
        font, draw = self._font(), self._draw()
        # Fewer fitted lines than points: the cut tail simply is not grouped.
        groups = _group_fitted_lines("Alpha\nBeta\nGamma", ["Alpha", "Beta..."], font, 900, draw)
        assert groups == [["Alpha"], ["Beta..."]]

    def test_marker_words_are_stripped_only_when_they_are_markers(self):
        from cqc_lem.utilities.carousel_creator import _strip_point_marker
        assert _strip_point_marker("- Coverage floor") == "Coverage floor"
        assert _strip_point_marker("* Coverage floor") == "Coverage floor"
        assert _strip_point_marker("-> Coverage floor") == "Coverage floor"
        assert _strip_point_marker("-5% churn") == "-5% churn"
        assert _strip_point_marker("1. Coverage floor") == "1. Coverage floor"


@pytest.mark.unit
class TestSlideImagesStillRender:
    """The structure fix must not change the file contract the deck path depends on."""

    def test_a_multi_line_body_still_produces_one_png_per_slide(self, tmp_path):
        carousel = EducationalContentCarousel(
            cover=EducationalContentSlide(title="Cover", content="Intro"),
            contents=[EducationalContentSlide(title="One", content=CHECKLIST_BODY)],
            call_to_action=EducationalContentSlide(title="CTA", content="Save this."),
        )
        with patch("cqc_lem.utilities.carousel_creator.select_slide_image", return_value=None):
            paths = create_carousel_slide_images(carousel, post_id=1510,
                                                 output_dir=str(tmp_path),
                                                 template="step_framework")
        assert len(paths) == 3
        for path in paths:
            assert os.path.exists(path)
