"""The stored-video MEASUREMENT RECEIPT — the ONE place its shape, filename and rules live (#1517).

`run_content_plan` writes it when a rendered video is stored; `content_quality.score_video_asset`
reads it back. They are separate modules for the same reason the deck receipt (#1513) is: the reader
runs inside the nightly telemetry beat, which must not grow a rendering import graph, and the writer
lives in the generation path.

Why a receipt exists at all: `purge_post_assets` (#148) deletes a post's stored MP4 the moment
`post_to_linkedin` succeeds — LinkedIn re-hosts the media, so the local copy is dead weight. The
nightly quality beat scores content that has ALREADY shipped, so it re-probes a file that no longer
exists and records `NULL / NULL / missing` for every video post. The measurement and the file are
only in hand at the same moment ONCE: at store time. This is that moment written down.

It survives the purge for free, without touching it: the purge removes the exact `.mp4` it resolved
from `posts.video_url`, so a sidecar sharing the stem stays — the same way the caption `.srt`
(#1278) does, and for the same reason (it is read AFTER the post publishes).

Two rules the readers depend on:

* **A receipt is a MEASUREMENT, never a default.** Nothing writes one for a probe that did not read
  the file, and a dimension the probe left unread is stored as null. A video recorded as
  "0 seconds, ok" is indistinguishable from one that was never measured (#630).
* **A receipt that will not parse is no receipt.** `read_video_receipt` returns None for absent and
  for broken alike, so the caller falls back to a live probe instead of scoring a guess.
"""

import json
import os
from typing import Any, Mapping, Optional

VIDEO_RECEIPT_SUFFIX = ".probe.json"

# The measures a receipt carries — the same keys `content_quality.probe_video_asset` produces, so
# a recorded reading and a live probe are interchangeable at the call site.
VIDEO_RECEIPT_MEASURES: tuple = ("duration_seconds", "aspect_ratio", "asset_probe",
                                 "has_video_stream")


def video_receipt_path(video_path: Optional[str]) -> Optional[str]:
    """Path of the measurement receipt for the video stored at `video_path`.

    Pure: the file may or may not exist, and after publication the video it names never does.
    """
    file_path = str(video_path or "").strip()
    if not file_path:
        return None
    return os.path.splitext(file_path)[0] + VIDEO_RECEIPT_SUFFIX


def write_video_receipt(video_path: Optional[str], post_id: Optional[int],
                        measures: Mapping[str, Any]) -> Optional[str]:
    """Write ONE receipt beside a stored video; return its path, or None if it could not be written.

    Best-effort by design — telemetry never costs a user their video, which is already on disk by
    the time this runs. A failed write is the caller's to log: it holds the post/user context, and
    this module stays stdlib-only.
    """
    path = video_receipt_path(video_path)
    if not path:
        return None
    payload = {"post_id": post_id,
               "measures": {key: measures.get(key) for key in VIDEO_RECEIPT_MEASURES}}
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle)
    return path


def read_video_receipt(receipt_path: Optional[str]) -> Optional[dict]:
    """The measures recorded for a stored video, or None when there is no usable receipt.

    Never raises: the nightly pass walks every video a user shipped, and one truncated JSON file
    must not take the run down. Absent and broken both answer None — a fabricated reading would be
    worse than the live probe the caller falls back to.

    `asset_probe` is what proves the payload is a reading rather than a shape that happens to
    parse, so a receipt without one is rejected. Everything else is returned exactly as recorded,
    null included: an unread duration stays unread.
    """
    file_path = str(receipt_path or "").strip()
    if not file_path or not os.path.exists(file_path):
        return None
    try:
        with open(file_path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    measures = payload.get("measures")
    if not isinstance(measures, dict):
        return None
    if not str(measures.get("asset_probe") or "").strip():
        return None
    return {key: measures.get(key) for key in VIDEO_RECEIPT_MEASURES}
