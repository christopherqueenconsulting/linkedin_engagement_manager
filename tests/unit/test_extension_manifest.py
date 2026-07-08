"""Validate the browser extension manifest + icons are Chrome Web Store-ready."""

import json
import os
import struct

import pytest

pytestmark = pytest.mark.unit

_EXT_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "src", "cqc_lem", "browser_extension"
)


def _png_size(path: str):
    with open(path, "rb") as f:
        head = f.read(24)
    assert head[:8] == b"\x89PNG\r\n\x1a\n", f"{path} is not a PNG"
    return struct.unpack(">II", head[16:24])  # width, height


def _manifest():
    with open(os.path.join(_EXT_DIR, "manifest.json"), encoding="utf-8") as f:
        return json.load(f)


class TestManifest:
    def test_is_mv3_with_required_fields(self):
        m = _manifest()
        assert m["manifest_version"] == 3
        assert m["name"] and m["version"] and m["description"]
        assert "cookies" in m["permissions"]

    def test_icons_declared_at_all_store_sizes(self):
        icons = _manifest()["icons"]
        for size in ("16", "48", "128"):
            assert size in icons, f"missing {size}px icon in manifest"

    def test_icon_files_exist_and_match_declared_size(self):
        for size, rel in _manifest()["icons"].items():
            path = os.path.join(_EXT_DIR, rel)
            assert os.path.isfile(path), f"icon file missing: {rel}"
            w, h = _png_size(path)
            assert (w, h) == (int(size), int(size)), f"{rel} is {w}x{h}, expected {size}x{size}"
