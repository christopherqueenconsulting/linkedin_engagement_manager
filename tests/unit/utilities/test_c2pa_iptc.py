"""IPTC digital-source-type writer and the final-write signing helper (#2634)."""

from unittest.mock import patch

import pytest
from PIL import Image

from cqc_lem.utilities import c2pa_helper
from cqc_lem.utilities.c2pa_helper import (
    read_ai_source_type,
    sign_ai_image,
    write_ai_source_metadata,
)

pytestmark = pytest.mark.unit

EXPECTED = "http://cv.iptc.org/newscodes/digitalsourcetype/trainedAlgorithmicMedia"


@pytest.mark.parametrize("ext,fmt", [(".png", "PNG"), (".jpg", "JPEG"), (".webp", "WEBP")])
def test_source_type_survives_a_reread(tmp_path, ext, fmt):
    path = tmp_path / f"img{ext}"
    Image.new("RGB", (32, 24), (10, 120, 200)).save(path, fmt)
    assert read_ai_source_type(str(path)) is None

    assert write_ai_source_metadata(str(path)) is True

    assert read_ai_source_type(str(path)) == EXPECTED
    with Image.open(path) as img:  # still a decodable image of the same size
        img.load()
        assert img.size == (32, 24)


def test_unsupported_and_missing_files_are_noops(tmp_path):
    txt = tmp_path / "a.txt"
    txt.write_text("x")
    assert write_ai_source_metadata(str(txt)) is False
    assert write_ai_source_metadata(str(tmp_path / "gone.png")) is False
    assert read_ai_source_type(str(tmp_path / "gone.png")) is None


def test_corrupt_png_returns_false_and_leaves_file(tmp_path):
    path = tmp_path / "bad.png"
    path.write_bytes(b"not a png")
    assert write_ai_source_metadata(str(path)) is False
    assert path.read_bytes() == b"not a png"
    assert not (tmp_path / "bad.png.iptc.tmp").exists()


def test_sign_writes_iptc_before_c2pa_and_persists_flag(tmp_path):
    path = tmp_path / "a.png"
    Image.new("RGB", (8, 8)).save(path, "PNG")
    order = []

    def _c2pa(p):
        order.append(("c2pa", read_ai_source_type(p)))
        return True

    with patch.object(c2pa_helper, "C2PA_ENABLED", True), \
         patch.object(c2pa_helper, "add_ai_content_credentials", side_effect=_c2pa), \
         patch("cqc_lem.utilities.db.set_post_c2pa_signed") as persist:
        assert sign_ai_image(str(path), post_id=7) is True

    assert order == [("c2pa", EXPECTED)]  # the manifest sees the metadata already in place
    persist.assert_called_once_with(7, True)


def test_failed_signing_persists_false_and_returns_false(tmp_path):
    path = tmp_path / "a.png"
    Image.new("RGB", (8, 8)).save(path, "PNG")
    with patch.object(c2pa_helper, "C2PA_ENABLED", True), \
         patch.object(c2pa_helper, "add_ai_content_credentials", return_value=False), \
         patch("cqc_lem.utilities.db.set_post_c2pa_signed") as persist:
        assert sign_ai_image(str(path), post_id=7) is False
    persist.assert_called_once_with(7, False)


def test_c2pa_off_writes_iptc_only_and_persists_nothing(tmp_path):
    path = tmp_path / "a.png"
    Image.new("RGB", (8, 8)).save(path, "PNG")
    with patch.object(c2pa_helper, "C2PA_ENABLED", False), \
         patch("cqc_lem.utilities.db.set_post_c2pa_signed") as persist:
        assert sign_ai_image(str(path), post_id=7) is False
    assert read_ai_source_type(str(path)) == EXPECTED
    persist.assert_not_called()


def test_sign_never_raises(tmp_path):
    with patch.object(c2pa_helper, "write_ai_source_metadata", side_effect=RuntimeError("boom")):
        assert sign_ai_image(str(tmp_path / "x.png")) is False


def test_surface_call_sites_sign(tmp_path):
    """image_gen, newsletter cover and variants route through the final-write signer."""
    from cqc_lem.utilities.ai import image_gen

    with patch("cqc_lem.utilities.c2pa_helper.sign_ai_image") as sign:
        assert image_gen._sign_final("/tmp/a.png", 3) == "/tmp/a.png"
        assert image_gen._sign_final(None, 3) is None
    sign.assert_called_once_with("/tmp/a.png", post_id=3)

    from cqc_lem.app import generate_variants
    with patch("cqc_lem.utilities.c2pa_helper.sign_ai_image") as sign, \
         patch("cqc_lem.utilities.c2pa_helper.add_ai_content_credentials") as video:
        generate_variants._sign_best_effort("/tmp/v.webp", image=True)
        generate_variants._sign_best_effort("/tmp/v.mp4")
    sign.assert_called_once_with("/tmp/v.webp")
    video.assert_called_once_with("/tmp/v.mp4")
