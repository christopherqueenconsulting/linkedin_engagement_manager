"""The ONE place a stored media URL is walked back to what is behind it (issue #1377).

Two readings of the same gap, which is why they share a module: **is the file still there**, and
**which brief rendered it**. Before this, `posts.image_url` / `posts.video_url` were strings nothing
ever re-read — a dangling value looked exactly like a live one, and the `ImageBrief.focal_concept`
the render was graded against was thrown away the moment the render finished.

*Integrity.* `scan_post_media` grades a stored URL against the assets volume and is **read-only,
reporting**: it never deletes a file and never clears a row. That is deliberate — a `posted` post
with no local file is the CORRECT end state, because `purge_post_assets` (#148) removes the MP4 and
the `images/posts/<post_id>/` directory the moment LinkedIn re-hosts the media. So the reading that
matters is not "missing", it is **missing on a row that has not published yet**
(`MEDIA_MISSING` with `expected=False`): that post is still going to be served, and its media is
already gone. The two are never summed.

*Brief receipt.* `write_brief_receipt` records the brief beside the render as
``<stored file stem>.brief.json``, keyed by the stored URL — the same sidecar shape the video
measurement receipt (#1517) and the caption `.srt` (#1278) use, and it survives publication for the
same reason they do: the audit that needs it reads content that has already SHIPPED. A video's
sidecar survives for free (the purge removes only the exact `.mp4` named by `video_url`); an image's
needs the carve-out `purge_post_assets` already makes for the deck receipt, because it clears the
post's whole image directory.

Two rules the readers depend on, taken from `video_receipt.py` because they are the same rules:

* **A receipt is a RECORD, never a default.** Nothing writes one for a render with no authored
  brief — a Pexels stock fallback did not come from the brief that was written for the render it
  replaced, and filing one under it would be a fabricated provenance claim.
* **A receipt that will not parse is no receipt.** `read_brief_receipt` answers None for absent and
  for broken alike, so a caller falls back to "unknown" rather than to a guess.
"""

import json
import os
from dataclasses import dataclass
from typing import Any, Mapping, Optional
from urllib.parse import parse_qs, urlparse

from cqc_lem.platform.db.enums import PostStatus
from cqc_lem.utilities.logger import log_warning

BRIEF_RECEIPT_SUFFIX = ".brief.json"

# What one stored media URL grades as. `unresolvable` is a value that is not one of our own
# `/api/assets?file_name=` URLs at all (a hand-edited row, or a remote URL) — there is no file of
# ours to look for, so it is never counted as missing.
MEDIA_PRESENT = "present"
MEDIA_MISSING = "missing"
MEDIA_UNRESOLVABLE = "unresolvable"

# The statuses whose local media is SUPPOSED to be gone. Only POSTED qualifies: `purge_post_assets`
# runs on a successful publish and on nothing else, so a missing file under any other status is an
# asset that went away while the post still needed it. The enum, never the literal — `platform/db/
# enums.py` is pure values and safe to import anywhere, and this comparison decides whether a row is
# a defect or a no-op, so it must not drift from the column's vocabulary.
_PURGED_AT_PUBLISH_STATUSES = (PostStatus.POSTED.value,)

# The brief fields a receipt carries. `focal_concept` is the one row 6 of the image rubric is graded
# on ("the render depicts the brief's stated idea"); the rest is what makes a bad render diagnosable
# — the preset that framed it, the surface it was for, and whether the vision gate had an opinion.
_BRIEF_FIELDS = ("focal_concept", "prompt", "surface", "style_preset", "ratio")


def _assets_root() -> str:
    """The assets volume root, read at CALL time.

    Not a module-level `from cqc_lem import assets_dir` binding, because both readers here are
    reached through other modules (`post_image`, `run_content_plan`, `purge_post_assets`) that each
    hold their own binding — one place resolving it late is what keeps those from disagreeing about
    where the volume is.
    """
    import cqc_lem

    return os.path.realpath(cqc_lem.assets_dir)


def asset_relative_path(media_url: Optional[str]) -> Optional[str]:
    """The assets-relative path inside one of our own `/api/assets?file_name=` URLs.

    None for anything else — a remote URL, a hand-edited row, or a `file_name` that climbs out of
    the volume with `..`. Unlike `post_image_relative_path` this accepts ANY subdirectory: the
    generated video path (`videos/runwayml/`) is not a compose-time preview, and a report that
    silently skipped it would answer "nothing dangling" for the column P3 was filed about.
    """
    if not media_url or not isinstance(media_url, str):
        return None
    try:
        query = parse_qs(urlparse(media_url).query)
    except ValueError:
        return None
    values = query.get("file_name") or []
    relative = (values[0] if values else "").replace("\\", "/").strip("/")
    if not relative:
        return None
    parts = [part for part in relative.split("/") if part and part != "."]
    if not parts or any(part == ".." for part in parts):
        return None
    return "/".join(parts)


def stored_asset_path(media_url: Optional[str]) -> Optional[str]:
    """Absolute path the URL names, whether or not a file is there.

    Existence-agnostic on purpose, which is what separates it from `post_image_abs_path`: both
    callers here need the path of something that may well be gone — the integrity scan is asking
    exactly that question, and a brief receipt outlives the render it describes.
    """
    relative = asset_relative_path(media_url)
    if not relative:
        return None
    root = _assets_root()
    candidate = os.path.realpath(os.path.join(root, *relative.split("/")))
    if candidate != root and not candidate.startswith(root + os.sep):
        return None
    return candidate


def brief_receipt_path(media_url: Optional[str]) -> Optional[str]:
    """Path of the brief receipt for the media stored at `media_url`, or None when it isn't ours.

    Pure: the receipt may or may not exist, and after publication the media it names never does.
    """
    path = stored_asset_path(media_url)
    if not path:
        return None
    return os.path.splitext(path)[0] + BRIEF_RECEIPT_SUFFIX


def is_brief_receipt(path: Optional[str]) -> bool:
    """Is this file one of our brief receipts? Read by `purge_post_assets` to spare it."""
    return bool(path) and str(path).endswith(BRIEF_RECEIPT_SUFFIX)


def write_brief_receipt(media_url: Optional[str], brief: Any, *,
                        post_id: Optional[int] = None, user_id: Optional[int] = None,
                        gate_verdict: Optional[str] = None) -> Optional[str]:
    """Record the brief beside its stored render; return the receipt path, or None.

    `brief` is duck-typed (anything carrying `focal_concept`, and optionally the rest of
    `ImageBrief`) so this module never imports the AI stack — the readers are an audit script and a
    reporting beat, and neither should pull in a rendering import graph.

    Never raises and never writes a placeholder: a brief with no `focal_concept` records nothing,
    because an empty receipt would read as "the render depicted nothing" rather than as "no brief
    was kept". A failed write is a WARNING — the render just succeeded, so an unwritable assets
    directory is a real fault, and it costs every render its provenance until someone looks.
    """
    focal = str(getattr(brief, "focal_concept", "") or "").strip()
    path = brief_receipt_path(media_url)
    if not focal or not path:
        return None
    payload: dict = {"post_id": post_id, "user_id": user_id,
                     "media": asset_relative_path(media_url),
                     "gate_verdict": gate_verdict}
    for field in _BRIEF_FIELDS:
        payload[field] = getattr(brief, field, None)
    payload["focal_concept"] = focal
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
        return path
    except (OSError, TypeError, ValueError) as e:
        # TypeError/ValueError as well as the write fault: `brief` is duck-typed, so a caller could
        # hand this something JSON cannot serialise — and this runs on the path that stores a
        # user's video. Provenance is never worth losing the asset.
        log_warning("Could not record the render's brief beside the stored media", exc=e,
                    post_id=post_id, user_id=user_id, action_type="media_provenance")
        return None


def read_brief_receipt(media_url: Optional[str]) -> Optional[dict]:
    """The brief recorded for a stored media URL, or None when there is no usable receipt.

    Never raises: an audit walks every media row a user has, and one truncated JSON file must not
    take the pass down. Absent and broken both answer None, and so does a payload with no
    `focal_concept` — that field is what proves the file is a recorded brief rather than a shape
    that happens to parse.
    """
    path = brief_receipt_path(media_url)
    if not path or not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    if not str(payload.get("focal_concept") or "").strip():
        return None
    return payload


@dataclass
class MediaIntegrityRow:
    """One graded media column on one post.

    `expected` only ever qualifies a MISSING file: True means the post published and
    `purge_post_assets` is why the file is gone. False on a missing file is the defect P3 named.
    """

    post_id: Optional[int]
    user_id: Optional[int]
    status: Optional[str]
    column: str
    url: Optional[str]
    state: str
    expected: bool
    has_brief: bool

    @property
    def dangling(self) -> bool:
        """Is this the reading that needs a human — media gone from a post that has not published?"""
        return self.state == MEDIA_MISSING and not self.expected


def _grade(post: Mapping[str, Any], column: str) -> Optional[MediaIntegrityRow]:
    url = post.get(column)
    if not url or not str(url).strip():
        return None
    status = str(post.get("status") or "") or None
    path = stored_asset_path(url)
    if not path:
        state = MEDIA_UNRESOLVABLE
    elif os.path.isfile(path):
        state = MEDIA_PRESENT
    else:
        state = MEDIA_MISSING
    return MediaIntegrityRow(
        post_id=post.get("id"), user_id=post.get("user_id"), status=status, column=column,
        url=str(url), state=state,
        expected=state == MEDIA_MISSING and (status or "") in _PURGED_AT_PUBLISH_STATUSES,
        has_brief=read_brief_receipt(url) is not None)


def scan_post_media(posts: Any) -> list:
    """Grade every media URL on the given post rows against the assets volume.

    Pure over the rows it is handed — the caller reads them — and read-only over the volume: it
    stats files and reads receipts, and changes neither. Returns one `MediaIntegrityRow` per
    NON-EMPTY media column, so a post carrying both an image and a video produces two.
    """
    rows: list = []
    for post in posts or []:
        for column in ("image_url", "video_url"):
            row = _grade(post, column)
            if row is not None:
                rows.append(row)
    return rows


def integrity_summary(rows: Any, dangling_sample: int = 20) -> dict:
    """Roll the graded rows up into the shape the reporting beat emits.

    `dangling` and `missing_expected` are separate counters and never added together: one is a
    defect, the other is `purge_post_assets` doing its job. `dangling_posts` is a bounded sample of
    the post ids behind the defect count — bounded because it rides a telemetry property, and the
    count above it is what says whether the sample is the whole story.
    """
    graded = list(rows or [])
    dangling = [row for row in graded if row.dangling]
    ids = sorted({row.post_id for row in dangling if row.post_id is not None})
    return {
        "checked": len(graded),
        "present": sum(1 for row in graded if row.state == MEDIA_PRESENT),
        "dangling": len(dangling),
        "missing_expected": sum(1 for row in graded
                                if row.state == MEDIA_MISSING and row.expected),
        "unresolvable": sum(1 for row in graded if row.state == MEDIA_UNRESOLVABLE),
        "with_brief": sum(1 for row in graded if row.has_brief),
        "dangling_posts": [str(post_id) for post_id in ids[:max(0, int(dangling_sample))]],
    }
