"""The carousel RENDER RECEIPT — the ONE place its shape, filename and states are named (#1513).

`carousel_creator` writes it; the nightly content-quality beat reads it back. They live in separate
modules because the writer pulls in Pillow, python-pptx and pydantic, and the nightly beat scores
three text surfaces that never touch a slide — so the reader stays stdlib-only and the beat's import
graph does not grow a rendering stack.

Why a receipt exists at all: a layout can still lose text. Issue #1375 made the loss VISIBLE —
`_fit` shrinks the type first and marks what it finally cuts — but visible is not measured, and the
only moment where the string the writer wrote and the lines the layout drew are BOTH in hand is the
render itself. Nothing downstream can re-derive the drop — not the stored PNG, not
`posts.carousel_slides`. The receipt is written next to the slides it describes, exactly as a video
post's telemetry reads the stored MP4 (issue #1281).
"""

import json
import os
from typing import Optional

DECK_RENDER_FILENAME = "deck_render.json"

SLIDE_ROLE_COVER = "cover"
SLIDE_ROLE_BODY = "body"
SLIDE_ROLE_CTA = "cta"

# The three readings a receipt can produce. `missing` is a deck with no receipt on disk — every deck
# rendered before #1513 shipped, and any whose assets were pruned; `unreadable` is a receipt that is
# there and will not parse, which is a fault rather than an absence. Neither ever becomes a zero: a
# deck recorded as "0 characters dropped" is indistinguishable from a clean render.
DECK_PROBE_OK = "ok"
DECK_PROBE_MISSING = "missing"
DECK_PROBE_UNREADABLE = "unreadable"


def deck_render_receipt_path(output_dir: str) -> str:
    """Path of the render receipt for a deck rendered into `output_dir`."""
    return os.path.join(output_dir, DECK_RENDER_FILENAME)


def write_deck_render_receipt(output_dir: str, post_id: Optional[int], template: str,
                              slides: list) -> Optional[str]:
    """Write ONE render receipt for a deck; return its path, or None if the write failed.

    Best-effort by design: telemetry never costs a user their carousel, so a failed write is logged
    and the slides still ship. It is a WARNING rather than an expected no-op because the render just
    succeeded — an unwritable output directory here is a real fault, not a quiet skip.
    """
    from cqc_lem.utilities.logger import log_warning

    path = deck_render_receipt_path(output_dir)
    try:
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"post_id": post_id, "template": template, "slides": list(slides or [])},
                      handle)
        return path
    except OSError as e:
        log_warning("Could not write the carousel render receipt", exc=e, post_id=post_id)
        return None


def read_deck_render_receipt(path: Optional[str]) -> tuple:
    """Read one receipt as `(receipt, probe_state)`.

    Never raises: the nightly pass walks every deck a user shipped and one truncated JSON file must
    not take the run down.
    """
    file_path = str(path or "").strip()
    if not file_path or not os.path.exists(file_path):
        return None, DECK_PROBE_MISSING
    try:
        with open(file_path, "r", encoding="utf-8") as handle:
            receipt = json.load(handle)
    except (OSError, ValueError):
        return None, DECK_PROBE_UNREADABLE
    if not isinstance(receipt, dict) or not isinstance(receipt.get("slides"), list):
        return None, DECK_PROBE_UNREADABLE
    return receipt, DECK_PROBE_OK
