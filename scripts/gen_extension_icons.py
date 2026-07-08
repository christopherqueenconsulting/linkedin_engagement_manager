#!/usr/bin/env python3
"""Generate the LEM LinkedIn Connect extension icons (Chrome Web Store requires 16/48/128).

A white two-link "chain" mark on a LinkedIn-blue rounded square — conveys "connect a
session" without any trademarked LinkedIn glyph. Re-run after changing the design:

    poetry run python scripts/gen_extension_icons.py
"""
import os

from PIL import Image, ImageDraw

BLUE = (10, 102, 194, 255)  # #0A66C2
WHITE = (255, 255, 255, 255)
OUT_DIR = os.path.join(os.path.dirname(__file__), "..", "src", "cqc_lem", "browser_extension", "icons")


def _rounded_square(size: int) -> Image.Image:
    # Supersample 4x for crisp edges, then downscale.
    ss = size * 4
    img = Image.new("RGBA", (ss, ss), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    radius = int(ss * 0.22)
    d.rounded_rectangle([0, 0, ss - 1, ss - 1], radius=radius, fill=BLUE)

    # Two interlocking rings (a chain link) drawn as thick white ellipse outlines.
    stroke = max(2, int(ss * 0.08))
    r = int(ss * 0.18)          # ring radius
    cy = ss // 2
    overlap = int(r * 0.5)
    cx_left = ss // 2 - overlap
    cx_right = ss // 2 + overlap
    for cx in (cx_left, cx_right):
        d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=WHITE, width=stroke)

    return img.resize((size, size), Image.LANCZOS)


def main() -> None:
    os.makedirs(OUT_DIR, exist_ok=True)
    for size in (16, 48, 128):
        path = os.path.join(OUT_DIR, f"icon{size}.png")
        _rounded_square(size).save(path)
        print(f"wrote {os.path.relpath(path)}")


if __name__ == "__main__":
    main()
