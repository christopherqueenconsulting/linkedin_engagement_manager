"""Representative KEYFRAMES of a stored carousel deck — the ONE place their naming and rules live.

Mirrors `video_frames.py` (#1595) for the same reason: `purge_post_assets` (#148) clears a deck's
whole slide directory the moment the post publishes, and everything that grades a SHIPPED deck runs
after that. The render receipt (`deck_render.py`, #1513) survives the purge and carries per-slide
text-fit numbers, but it is not an image — rubric rows R2 (per-slide fit at render size) and R8
(does the slide's photo depict the slide's idea) are graded on PIXELS, and the receipt cannot answer
either. The slide PNGs and the grader are only ever in the same room at render time.

Two slides are retained per deck, not every slide: the COVER (what earns the swipe, R1/R2) and the
first BODY slide (the photo band + text-fit case R2/R8 actually need to see). A CTA slide is not
retained — R7 is already covered by the receipt's `chars_dropped` for that slide, and a third image
per deck was judged not worth the extra storage against what it would add (issue #1704).

Two rules the readers depend on, same as `video_frames.py`:

* **A keyframe is COPIED from what was actually rendered, never regenerated.** It is a JPEG copy of
  the exact PNG the deck shipped with — never a fresh render, which could depict a different draft
  than what LinkedIn received.
* **Best-effort in every direction.** A missing source slide, an unwritable directory or a Pillow
  failure contributes no keyframe and raises nothing: the deck is already stored, and losing it (or
  losing a measured corpus) over a thumbnail is exactly backwards. The caller holds the post/user
  context, so the caller logs — this module stays import-light like its video counterpart.
"""

import os
from typing import Optional

KEYFRAME_SUFFIX = ".keyframe.jpg"
# The roles retained, in the order a reader looks at them. `cover` is the only one every deck has.
KEYFRAME_ROLES: tuple = ("cover", "body")


def keyframe_path(slide_path: Optional[str]) -> Optional[str]:
    """Path of the retained keyframe for the slide stored at `slide_path`.

    Pure: the file may or may not exist, and after publication the slide it is named for never
    does. Namespaced under the same stem as the slide (`slide_01.png` -> `slide_01.keyframe.jpg`)
    so `purge_post_assets`'s keep-set can recognise it by suffix alone.
    """
    path = str(slide_path or "").strip()
    if not path:
        return None
    return os.path.splitext(path)[0] + KEYFRAME_SUFFIX


def is_carousel_keyframe(path: Optional[str]) -> bool:
    """True for a path this module wrote — the predicate `purge_post_assets` keeps by."""
    return str(path or "").endswith(KEYFRAME_SUFFIX)


def retain_carousel_keyframes(image_paths: list, slide_receipts: list) -> list:
    """Write JPEG keyframes for the cover and the first body slide; return the paths that landed.

    Call it right after `write_deck_render_receipt`, on the same `image_paths` /
    `slide_receipts` the render loop just produced — the bytes on disk at that moment are exactly
    what ships, before `purge_post_assets` ever runs.

    Args:
        image_paths: The rendered slide PNG paths, in slide order (1-indexed by position).
        slide_receipts: The per-slide receipt dicts from the same render, each carrying `role` at
            the matching position.

    Returns:
        The keyframe paths actually written, `cover` first when both land.
    """
    from cqc_lem.utilities.logger import log_debug

    if not image_paths or not slide_receipts:
        return []
    try:
        from PIL import Image
    except ImportError:
        return []

    written: list = []
    wanted = {"cover": None, "body": None}
    for idx, receipt in enumerate(slide_receipts):
        role = receipt.get("role")
        if role == "cover" and wanted["cover"] is None and idx < len(image_paths):
            wanted["cover"] = image_paths[idx]
        elif role == "body" and wanted["body"] is None and idx < len(image_paths):
            wanted["body"] = image_paths[idx]

    for role in KEYFRAME_ROLES:
        source = wanted.get(role)
        if not source or not os.path.exists(source):
            continue
        out_path = keyframe_path(source)
        if not out_path:
            continue
        try:
            with Image.open(source) as img:
                img.convert("RGB").save(out_path, "JPEG", quality=85)
        except Exception as e:  # noqa: BLE001 - a thumbnail is never worth losing the deck over
            log_debug(f"Could not write carousel keyframe for role={role}: {e}")
            continue
        if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
            written.append(out_path)
    return written


def retained_carousel_keyframes(output_dir: Optional[str]) -> list:
    """`(role, path)` for every keyframe retained in `output_dir` that is still on disk.

    The reader half: after publication the slide PNGs are gone and these sidecars are all a grader
    has, so this answers from the filesystem — by the `.keyframe.jpg` suffix, not by re-deriving
    which slide index held which role — rather than from a record of what was written.
    """
    directory = str(output_dir or "").strip()
    if not directory or not os.path.isdir(directory):
        return []
    found: list = []
    for entry in sorted(os.listdir(directory)):
        if not is_carousel_keyframe(entry):
            continue
        path = os.path.join(directory, entry)
        if os.path.getsize(path) <= 0:
            continue
        # The stem before `.keyframe.jpg` still carries the original slide filename
        # (e.g. `slide_01`); role isn't encoded in the name, so pair by write order instead —
        # `retain_carousel_keyframes` always writes cover before body, and there are at most two.
        found.append(path)
    roles = KEYFRAME_ROLES[: len(found)]
    return list(zip(roles, found))
