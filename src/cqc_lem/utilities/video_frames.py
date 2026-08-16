"""Representative KEYFRAMES of a stored video — the ONE place their timing, naming and rules live.

Rubric rows R1 (the first 2-3 seconds) and R8 (the closing frame) in
`docs/content-quality-audits/video.md` are graded on PIXELS, so they need an image of the clip. The
measurement receipt (`video_receipt.py`, #1517) carries numbers and cannot answer them: it records
what the file measured, not what it looked like.

Why the images have to be written at STORE time, like the receipt: `purge_post_assets` (#148)
deletes a post's MP4 the moment `post_to_linkedin` succeeds — LinkedIn re-hosts the media, so the
local copy is dead weight — and everything that grades a shipped post runs after that. The clip and
the grader are only in the same room once. Retaining three JPEGs per video post is the cost the
owner accepted (decision `2A` on #1363) against the megabytes #148 reclaims.

They survive the purge with no carve-out, for the same reason the `.probe.json` receipt does: the
purge removes the exact `.mp4` named by `posts.video_url`, and a sidecar sharing that stem is not
that path.

Two rules the readers depend on:

* **A frame is EXTRACTED, never inferred.** Nothing is reported unless ffmpeg actually wrote a
  non-empty file, and an unread duration yields the opening frame only — inventing a midpoint for a
  clip whose length was never measured is how an audit ends up citing a frame that does not depict
  what it claims.
* **Best-effort in every direction.** No ffmpeg, an unreadable clip, a non-zero exit or an
  unwritable directory contributes no frame and raises nothing: the video is already stored, and
  losing it (or losing a measured corpus) over a thumbnail is exactly backwards. The caller holds
  the post/user context, so the caller logs — this module stays stdlib-only.
"""

import os
import shutil
import subprocess
from typing import Callable, Optional

# Rubric row R1 grades the first 2-3 seconds, so the opening frame is sampled EARLY rather than at
# t=0: frame zero of a Runway render is routinely a near-black fade-in and says nothing about the
# hook. The closing frame backs off the end for the same reason (R8).
OPEN_FRAME_SECONDS = 0.5
CLOSE_FRAME_BACKOFF_SECONDS = 0.3
FRAME_TIMEOUT_SECONDS = 30
# The labels, in the order a reader looks at them. `open` is the only one a clip always has.
FRAME_LABELS: tuple = ("open", "mid", "close")
KEYFRAME_SUFFIX_TEMPLATE = ".frame-{label}.jpg"


def frame_timestamps(duration: Optional[float]) -> list:
    """`(label, seconds)` pairs to grab from a clip of `duration` seconds."""
    try:
        seconds = float(duration)
    except (TypeError, ValueError):
        return [("open", OPEN_FRAME_SECONDS)]
    if seconds <= OPEN_FRAME_SECONDS + CLOSE_FRAME_BACKOFF_SECONDS:
        return [("open", OPEN_FRAME_SECONDS)]
    return [("open", OPEN_FRAME_SECONDS),
            ("mid", round(seconds / 2, 2)),
            ("close", round(seconds - CLOSE_FRAME_BACKOFF_SECONDS, 2))]


def keyframe_path(video_path: Optional[str], label: str) -> Optional[str]:
    """Path of the retained `label` keyframe for the video stored at `video_path`.

    Pure: the file may or may not exist, and after publication the video it is named for never does.
    """
    file_path = str(video_path or "").strip()
    if not file_path:
        return None
    return os.path.splitext(file_path)[0] + KEYFRAME_SUFFIX_TEMPLATE.format(label=label)


def extract_frames(video_path: Optional[str], duration: Optional[float],
                   out_path_for: Callable[[str], Optional[str]]) -> list:
    """Write one JPEG per `frame_timestamps` entry and return the paths that actually landed.

    `out_path_for(label)` names each output, so the store path can write sidecars beside the MP4
    while the corpus sampler writes into an audit directory — one extraction engine, two
    destinations. A destination of None skips that frame.
    """
    path = str(video_path or "").strip()
    if not path or not os.path.exists(path):
        return []
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return []
    written: list = []
    for label, seconds in frame_timestamps(duration):
        out_path = out_path_for(label)
        if not out_path:
            continue
        try:
            # -ss BEFORE -i seeks by keyframe, which is what a 5-10s render wants: it is fast and
            # the frame it lands on is the one a scrolling viewer actually sees.
            subprocess.run([ffmpeg, "-y", "-ss", str(seconds), "-i", path,
                            "-frames:v", "1", "-q:v", "2", out_path],
                           capture_output=True, check=False, timeout=FRAME_TIMEOUT_SECONDS)
        except (OSError, subprocess.SubprocessError):
            continue
        if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
            written.append(out_path)
    return written


def retain_keyframes(video_path: Optional[str], duration: Optional[float]) -> list:
    """Write the keyframes for a just-stored video beside it; return the paths that landed.

    Call it at the same moment the measurement receipt is written — last, on the bytes that ship,
    after the caption burn and C2PA signing have rewritten the file. A frame taken before either
    would show a clip that never reached LinkedIn.
    """
    return extract_frames(video_path, duration, lambda label: keyframe_path(video_path, label))


def retained_keyframes(video_path: Optional[str]) -> list:
    """`(label, path)` for every keyframe retained for `video_path` that is still on disk.

    The reader half, in reading order: after publication the MP4 is gone and these sidecars are all
    a grader has, so this answers from the filesystem rather than from a record of what was written.
    """
    found: list = []
    for label in FRAME_LABELS:
        path = keyframe_path(video_path, label)
        if path and os.path.exists(path) and os.path.getsize(path) > 0:
            found.append((label, path))
    return found
