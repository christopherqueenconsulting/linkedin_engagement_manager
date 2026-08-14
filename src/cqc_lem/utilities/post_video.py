"""Post videos — the video half of the compose-time media surface (issue #1443).

`post_image.py` is still the ONE place a post's IMAGE is validated, stored and removed; this module
is its counterpart for the checks a video needs and an image does not — container, codec, duration
and frame size — plus the three `*_media_*` resolvers a caller that accepts EITHER kind uses (today
that is the weekly group post, whose draft carries `media_url` + `media_type`).

Why a second module rather than a wider image one: `owns_post_image_url` is the gate
`/schedule_post/` puts on `posts.image_url`, and widening it so a video preview resolved there would
let a caller point a post's IMAGE field at an MP4. The two halves therefore keep separate preview
dirs (`videos/post_previews/<user_id>/` beside `images/post_previews/<user_id>/`) and separate
ownership functions, and only the group-post path — which genuinely takes both — reads the union.

The gate is deterministic where it can be: size and the ISO base-media `ftyp` brand are read
straight out of the bytes, so an unreadable or wrong-container file is a 400 at upload rather than
an empty media frame on a published group post. Duration, dimensions and codec need ffprobe, which
is not installed in every container that serves this API, so that half **fails open** exactly as
`_probe_video_file` does (issue #1280): a probe that cannot run accepts what the head check already
proved, and a probe that CAN run and reports a violation refuses.
"""

import json
import os
import secrets
import shutil
import subprocess
from dataclasses import dataclass
from typing import Optional
from urllib.parse import parse_qs, urlparse

from cqc_lem import assets_dir
from cqc_lem.utilities.logger import log_debug, log_info, log_warning
from cqc_lem.utilities.post_image import (
    owns_post_image_url,
    post_image_abs_path,
    post_image_public_url,
    remove_post_image_file,
)

POST_VIDEO_PREVIEW_SUBDIR = "videos/post_previews"

# 200 MB is well under LinkedIn's own 5 GB ceiling: this file is streamed through the API and then
# handed to a Chrome node by the publish run, so the practical limit is ours, not theirs.
MAX_POST_VIDEO_BYTES = 200 * 1024 * 1024
# LinkedIn's documented floor — anything under it is a truncated upload, not a video.
MIN_POST_VIDEO_BYTES = 75 * 1024
MIN_POST_VIDEO_SECONDS = 3.0
MAX_POST_VIDEO_SECONDS = 15 * 60
MIN_POST_VIDEO_WIDTH = 256
MIN_POST_VIDEO_HEIGHT = 144
ALLOWED_POST_VIDEO_CODECS = ("h264", "hevc")

# ISO base-media brands we store as MP4, and QuickTime's. The brand is read from the file's own
# `ftyp` box rather than the upload's name for the same reason `PostImageVerdict.extension` uses the
# DECODED format: the extension on disk is what `determine_media_type` reads at publish to pick
# LinkedIn's share category, so a `.mp4` that is really something else has to fail here.
_MP4_BRANDS = frozenset({"isom", "iso2", "iso4", "iso5", "iso6", "iso8", "avc1", "mp41", "mp42",
                         "mmp4", "m4v", "m4v ", "dash", "3gp4", "3gp5"})
_QUICKTIME_BRANDS = frozenset({"qt", "qt  "})

_EXT_BY_CONTAINER = {"mp4": ".mp4", "mov": ".mov"}

_FFPROBE_TIMEOUT_SECONDS = 20


class PostVideoRejected(Exception):
    """The video failed the deterministic gate — carries the user-facing reason."""


@dataclass
class PostVideoVerdict:
    """Outcome of the video gate. ``reason`` is user-facing when ``ok`` is False.

    ``duration_seconds`` / ``width`` / ``height`` / ``codec`` are None when ffprobe was unavailable —
    absence is "not measured", never "measured as zero".
    """

    ok: bool
    reason: Optional[str] = None
    container: Optional[str] = None
    size_bytes: Optional[int] = None
    duration_seconds: Optional[float] = None
    width: Optional[int] = None
    height: Optional[int] = None
    codec: Optional[str] = None

    @property
    def extension(self) -> str:
        """Extension to store this video under, taken from the DECODED container, never the name."""
        return _EXT_BY_CONTAINER.get(self.container or "", ".mp4")


def _read_container(path: str, size_bytes: int) -> Optional[str]:
    """The container this file really is (`mp4` / `mov`), or None when it is neither.

    ISO base-media files (MP4 and QuickTime alike) open with an `ftyp` box whose payload starts with
    the major brand, so the first 12 bytes answer this without decoding anything.
    """
    try:
        with open(path, "rb") as fh:
            head = fh.read(12)
    except OSError:
        return None
    if len(head) < 12 or head[4:8] != b"ftyp":
        return None
    box_size = int.from_bytes(head[0:4], "big")
    if box_size < 8 or box_size > size_bytes:
        return None
    brand = head[8:12].decode("ascii", errors="ignore").lower()
    if brand in _MP4_BRANDS:
        return "mp4"
    if brand in _QUICKTIME_BRANDS:
        return "mov"
    return None


def _ffprobe_stream(path: str) -> Optional[dict]:
    """Duration, frame size and video codec via ffprobe, or None when it could not be measured.

    None is every not-measured case — ffprobe absent, non-zero exit, unparseable output. The caller
    accepts on None because the head check is deterministic and this half is fail-open (issue #1280);
    that is also why an absent binary is DEBUG rather than a warning, since a container without
    ffmpeg installed is an expected environment, not a defect.
    """
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        log_debug("ffprobe is not installed — accepting the video on its container signature",
                  action_type="post_video")
        return None
    try:
        out = subprocess.run(
            [ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries",
             "stream=codec_name,width,height:format=duration", "-of", "json", path],
            capture_output=True, text=True, timeout=_FFPROBE_TIMEOUT_SECONDS)
        if out.returncode != 0:
            return None
        parsed = json.loads(out.stdout or "{}")
    except (OSError, ValueError, subprocess.SubprocessError) as e:
        log_debug("ffprobe could not read the uploaded video", error=str(e),
                  action_type="post_video")
        return None
    streams = parsed.get("streams") or []
    stream = streams[0] if streams else {}
    raw_duration = (parsed.get("format") or {}).get("duration")
    try:
        duration = float(raw_duration)
    except (TypeError, ValueError):
        duration = None
    return {
        "duration": duration,
        "width": stream.get("width"),
        "height": stream.get("height"),
        "codec": (stream.get("codec_name") or "").lower() or None,
    }


def inspect_post_video_file(path: str) -> PostVideoVerdict:
    """Grade a stored video file against the post-video contract. Never raises."""
    try:
        size_bytes = os.path.getsize(path)
    except OSError as e:
        log_debug("Uploaded video could not be read", error=str(e), action_type="post_video")
        return PostVideoVerdict(False, "That file is not a readable video")

    if size_bytes > MAX_POST_VIDEO_BYTES:
        return PostVideoVerdict(
            False, f"Video is larger than {MAX_POST_VIDEO_BYTES // (1024 * 1024)} MB",
            size_bytes=size_bytes)
    if size_bytes < MIN_POST_VIDEO_BYTES:
        return PostVideoVerdict(False, "That file is too small to be a video", size_bytes=size_bytes)

    container = _read_container(path, size_bytes)
    if container is None:
        return PostVideoVerdict(False, "Use an MP4 or MOV video", size_bytes=size_bytes)

    measured = _ffprobe_stream(path)
    if measured is None:
        return PostVideoVerdict(True, None, container, size_bytes)

    duration, width = measured["duration"], measured["width"]
    height, codec = measured["height"], measured["codec"]
    verdict = PostVideoVerdict(True, None, container, size_bytes, duration, width, height, codec)

    if duration is None or duration <= 0:
        return PostVideoVerdict(False, "That file is not a readable video", container, size_bytes,
                                duration, width, height, codec)
    if duration < MIN_POST_VIDEO_SECONDS:
        verdict.ok, verdict.reason = False, (
            f"Video must be at least {int(MIN_POST_VIDEO_SECONDS)} seconds long")
    elif duration > MAX_POST_VIDEO_SECONDS:
        verdict.ok, verdict.reason = False, (
            f"Video must be shorter than {MAX_POST_VIDEO_SECONDS // 60} minutes")
    elif not width or not height:
        verdict.ok, verdict.reason = False, "That file has no readable video track"
    elif width < MIN_POST_VIDEO_WIDTH or height < MIN_POST_VIDEO_HEIGHT:
        verdict.ok, verdict.reason = False, (
            f"Video is too small — at least {MIN_POST_VIDEO_WIDTH}x{MIN_POST_VIDEO_HEIGHT} px")
    elif codec not in ALLOWED_POST_VIDEO_CODECS:
        verdict.ok, verdict.reason = False, "Use a video encoded with H.264 or HEVC"
    return verdict


def post_video_relative_dir(user_id: int) -> str:
    """Assets-relative dir a compose-time video preview belongs in — per USER.

    There is no per-POST branch on this side: a post's video is generated by the content plan and
    stored by that path, and the only manual video today is the group post's, which has no `posts`
    row at all.
    """
    return f"{POST_VIDEO_PREVIEW_SUBDIR}/{int(user_id)}"


def post_video_relative_path(video_url: Optional[str]) -> Optional[str]:
    """The assets-relative path inside a stored post-video URL, or None when it isn't one."""
    if not video_url or not isinstance(video_url, str):
        return None
    try:
        query = parse_qs(urlparse(video_url).query)
    except ValueError:
        return None
    values = query.get("file_name") or []
    relative = (values[0] if values else "").replace("\\", "/").strip("/")
    if not relative:
        return None
    return relative if relative.startswith(f"{POST_VIDEO_PREVIEW_SUBDIR}/") else None


def post_video_abs_path(video_url: Optional[str]) -> Optional[str]:
    """Absolute path of a stored post video, or None when it escapes assets_dir / is gone."""
    relative = post_video_relative_path(video_url)
    if not relative:
        return None
    root = os.path.realpath(assets_dir)
    candidate = os.path.realpath(os.path.join(root, relative))
    if candidate != root and not candidate.startswith(root + os.sep):
        log_warning("Post video path escapes the assets dir", action_type="post_video")
        return None
    return candidate if os.path.isfile(candidate) else None


def owns_post_video_url(user_id: int, video_url: Optional[str]) -> bool:
    """Is this URL a compose-time video preview WE issued to THIS user?

    Same argument as `owns_post_image_url`: the value is caller input on a field the publish run
    later hands to LinkedIn, so only a preview under the caller's own dir resolves.
    """
    relative = post_video_relative_path(video_url)
    if not relative:
        return False
    return relative.startswith(f"{POST_VIDEO_PREVIEW_SUBDIR}/{int(user_id)}/")


def save_post_video_file(user_id: int, source_path: str) -> str:
    """Gate then persist an uploaded video; returns the public URL and CONSUMES `source_path`.

    Raises ``PostVideoRejected`` with a user-facing reason when the gate fails — the caller turns
    that into a 400 rather than storing a file the group composer would refuse at publish time. The
    source is moved, not copied: it is the caller's spooled upload and there is no reason to hold
    two copies of a 200 MB file.
    """
    verdict = inspect_post_video_file(source_path)
    if not verdict.ok:
        raise PostVideoRejected(verdict.reason or "Video rejected")
    relative_dir = post_video_relative_dir(user_id)
    directory = os.path.join(assets_dir, *relative_dir.split("/"))
    os.makedirs(directory, exist_ok=True)
    # Random suffix: /api/assets is public, so a predictable name would let anyone read another
    # user's unpublished video.
    name = f"vid_{secrets.token_hex(6)}{verdict.extension}"
    shutil.move(source_path, os.path.join(directory, name))
    log_info("Stored uploaded post video", user_id=user_id, action_type="post_video")
    return post_image_public_url(f"{relative_dir}/{name}")


def remove_post_video_file(video_url: Optional[str]) -> bool:
    """Best-effort delete of a stored post video file. Never raises — the row is the truth."""
    abs_path = post_video_abs_path(video_url)
    if not abs_path:
        return False
    try:
        os.remove(abs_path)
        return True
    except OSError as e:
        log_debug("Could not delete post video file", error=str(e), action_type="post_video")
        return False


# --- Either-kind resolvers -------------------------------------------------------------------
# For a surface that accepts an image OR a video (the group post). Each one asks the image half
# first and only then this one, so a caller that only ever handles images keeps reading the narrower
# function directly and nothing widens `/schedule_post/`'s gate.


def owns_post_media_url(user_id: int, media_url: Optional[str]) -> bool:
    """Is this URL a compose-time image OR video preview we issued to THIS user?"""
    return owns_post_image_url(user_id, media_url) or owns_post_video_url(user_id, media_url)


def post_media_abs_path(media_url: Optional[str]) -> Optional[str]:
    """Absolute path of a stored image or video, or None when it escapes assets_dir / is gone."""
    return post_image_abs_path(media_url) or post_video_abs_path(media_url)


def remove_post_media_file(media_url: Optional[str]) -> bool:
    """Best-effort delete of a stored image or video file. Never raises."""
    return remove_post_image_file(media_url) or remove_post_video_file(media_url)
