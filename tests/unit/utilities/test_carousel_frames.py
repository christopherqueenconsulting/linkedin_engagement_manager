"""Pins `utilities/carousel_frames.py` — the retained keyframes rubric rows R2/R8 are graded on.

The failure mode that matters is a keyframe that lies: one reported without Pillow having written
it, the wrong slide role retained, or a sidecar named so that `purge_post_assets` takes it with the
rest of the directory — which would put the audit back where #1515 stopped.
"""

import pytest
from PIL import Image

from cqc_lem.utilities.carousel_frames import (
    KEYFRAME_SUFFIX,
    is_carousel_keyframe,
    keyframe_path,
    retain_carousel_keyframes,
    retained_carousel_keyframes,
)

pytestmark = pytest.mark.unit


def _make_png(path, color=(10, 20, 30)):
    Image.new("RGB", (8, 8), color).save(path, "PNG")
    return str(path)


class TestKeyframePath:
    def test_the_sidecar_shares_the_stem_but_is_never_the_png(self, tmp_path):
        slide = str(tmp_path / "slide_01.png")
        path = keyframe_path(slide)
        assert path.endswith(".keyframe.jpg")
        assert path != slide and not path.endswith(".png")

    def test_no_slide_is_no_path(self):
        assert keyframe_path(None) is None
        assert keyframe_path("   ") is None


class TestIsCarouselKeyframe:
    def test_matches_only_the_keyframe_suffix(self):
        assert is_carousel_keyframe(f"/x/slide_01{KEYFRAME_SUFFIX}") is True
        assert is_carousel_keyframe("/x/slide_01.png") is False
        assert is_carousel_keyframe("/x/deck_render.json") is False
        assert is_carousel_keyframe(None) is False


class TestRetainCarouselKeyframes:
    def test_writes_cover_and_first_body_slide_only(self, tmp_path):
        paths = [_make_png(tmp_path / f"slide_0{i}.png") for i in range(1, 6)]
        receipts = [
            {"index": 1, "role": "cover"},
            {"index": 2, "role": "body"},
            {"index": 3, "role": "body"},
            {"index": 4, "role": "body"},
            {"index": 5, "role": "cta"},
        ]
        written = retain_carousel_keyframes(paths, receipts)
        assert written == [keyframe_path(paths[0]), keyframe_path(paths[1])]
        for path in written:
            assert Image.open(path).format == "JPEG"
        # The second and third body slides, and the CTA, get nothing — one deck, two keyframes.
        assert not any(keyframe_path(p) and __import__("os").path.exists(keyframe_path(p))
                       for p in paths[2:])

    def test_a_two_slide_deck_only_yields_a_cover(self, tmp_path):
        paths = [_make_png(tmp_path / "slide_01.png"), _make_png(tmp_path / "slide_02.png")]
        receipts = [{"index": 1, "role": "cover"}, {"index": 2, "role": "cta"}]
        written = retain_carousel_keyframes(paths, receipts)
        assert written == [keyframe_path(paths[0])]

    def test_empty_input_writes_nothing(self):
        assert retain_carousel_keyframes([], []) == []
        assert retain_carousel_keyframes([], [{"index": 1, "role": "cover"}]) == []

    def test_a_missing_source_file_is_skipped_not_raised(self, tmp_path):
        missing = str(tmp_path / "slide_01.png")
        receipts = [{"index": 1, "role": "cover"}]
        assert retain_carousel_keyframes([missing], receipts) == []

    def test_pillow_failure_is_swallowed(self, tmp_path, monkeypatch):
        import cqc_lem.utilities.carousel_frames as cf

        bad = tmp_path / "slide_01.png"
        bad.write_bytes(b"not a real png")
        receipts = [{"index": 1, "role": "cover"}]
        assert cf.retain_carousel_keyframes([str(bad)], receipts) == []


class TestRetainedCarouselKeyframes:
    def test_reads_back_what_was_written_cover_first(self, tmp_path):
        paths = [_make_png(tmp_path / f"slide_0{i}.png") for i in range(1, 4)]
        receipts = [
            {"index": 1, "role": "cover"},
            {"index": 2, "role": "body"},
            {"index": 3, "role": "cta"},
        ]
        retain_carousel_keyframes(paths, receipts)
        found = retained_carousel_keyframes(str(tmp_path))
        assert [role for role, _ in found] == ["cover", "body"]
        assert found[0][1] == keyframe_path(paths[0])
        assert found[1][1] == keyframe_path(paths[1])

    def test_a_directory_with_no_keyframes_reads_empty(self, tmp_path):
        assert retained_carousel_keyframes(str(tmp_path)) == []

    def test_missing_or_unset_directory_reads_empty(self, tmp_path):
        assert retained_carousel_keyframes(str(tmp_path / "gone")) == []
        assert retained_carousel_keyframes(None) == []

    def test_an_empty_keyframe_file_is_not_reported(self, tmp_path):
        (tmp_path / "slide_01.keyframe.jpg").write_bytes(b"")
        assert retained_carousel_keyframes(str(tmp_path)) == []
