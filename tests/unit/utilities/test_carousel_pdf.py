"""Unit tests for create_carousel_pdf — slide PNGs bundled into a native document."""

import os

import pytest


def _write_png(path: str, color=(10, 20, 30)) -> str:
    from PIL import Image
    Image.new("RGB", (64, 64), color=color).save(path)
    return path


@pytest.mark.unit
class TestCreateCarouselPdf:

    def test_builds_multi_page_pdf_from_slides(self, tmp_path):
        from cqc_lem.utilities.carousel_creator import create_carousel_pdf

        slides = [_write_png(str(tmp_path / f"slide_{i}.png")) for i in range(3)]
        out_dir = tmp_path / "out"

        pdf_path = create_carousel_pdf(slides, post_id=42, output_dir=str(out_dir))

        assert pdf_path == os.path.join(str(out_dir), "document_42.pdf")
        assert os.path.isfile(pdf_path)
        with open(pdf_path, "rb") as f:
            assert f.read(5) == b"%PDF-"

    def test_defaults_to_post_assets_dir(self, tmp_path, monkeypatch):
        """The default path is derived from the module's __file__ — redirect that so the
        assets tree is created under tmp_path instead of in the repo working copy."""
        from cqc_lem.utilities import carousel_creator
        from cqc_lem.utilities.carousel_creator import create_carousel_pdf

        fake_pkg = tmp_path / "pkg" / "utilities"
        fake_pkg.mkdir(parents=True)
        monkeypatch.setattr(carousel_creator, "__file__", str(fake_pkg / "carousel_creator.py"))

        slides = [_write_png(str(tmp_path / "slide_1.png"))]

        pdf_path = create_carousel_pdf(slides, post_id=7)

        assert pdf_path.endswith(os.path.join("carousel", "7", "document_7.pdf"))
        assert pdf_path.startswith(os.path.realpath(str(tmp_path)))
        assert os.path.isfile(pdf_path)

    def test_returns_none_without_slides(self, tmp_path):
        from cqc_lem.utilities.carousel_creator import create_carousel_pdf

        assert create_carousel_pdf([], post_id=1, output_dir=str(tmp_path)) is None
        assert create_carousel_pdf(None, post_id=1, output_dir=str(tmp_path)) is None

    def test_returns_none_when_a_slide_is_unreadable(self, tmp_path):
        """Never publish a partial deck — one bad slide fails the whole document."""
        from cqc_lem.utilities.carousel_creator import create_carousel_pdf

        good = _write_png(str(tmp_path / "slide_1.png"))
        bad = str(tmp_path / "slide_2.png")
        with open(bad, "wb") as f:
            f.write(b"not really a png")

        assert create_carousel_pdf([good, bad], post_id=1, output_dir=str(tmp_path)) is None

    def test_returns_none_when_a_slide_is_missing(self, tmp_path):
        from cqc_lem.utilities.carousel_creator import create_carousel_pdf

        good = _write_png(str(tmp_path / "slide_1.png"))
        missing = str(tmp_path / "nope.png")

        assert create_carousel_pdf([good, missing], post_id=1, output_dir=str(tmp_path)) is None
