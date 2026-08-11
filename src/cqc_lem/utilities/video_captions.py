"""The ONE place a video post gets its muted-autoplay caption (issue #1278, decision 1A).

LinkedIn's feed player starts muted and the post's caption sits below the fold on mobile, so the
first 2-3 seconds of the video have to communicate on their own. This module burns the post's own
opening line into the rendered MP4 with ffmpeg's `subtitles` filter, and always writes the `.srt`
sidecar it burned so the author can attach captions manually on LinkedIn if they want the native
toggle too.

Three things this deliberately does NOT do:

- **It authors nothing.** The caption is the post's first 1-2 lines, wrapped deterministically.
  No LLM call, no per-content-type prompt helper (`content_framework.py` already owns the words
  the post opens with, and the caption must say exactly what the reader is about to read).
- **It never re-renders the video.** The burn is a single ffmpeg pass over an MP4 that already
  exists; the only new spend is local CPU, attributed through `track_media_cost` at
  `VIDEO_CAPTION_RENDER_COST_PER_MINUTE`.
- **It never covers a face it was not invited to cover.** An avatar-led video (the LoRA rendered
  the source frame, `posts.avatar_media`) is skipped unless the user turned on
  `users.avatar_caption_overlay` — the sidecar is still written, so opting out costs the burn-in,
  not the captions.

It fails OPEN in every direction: no ffmpeg, an unreadable video, a caption that comes out empty,
a non-zero exit, an output the caller's probe rejects — the post keeps the video it already had. A
caption is an enhancement, and the gate that decides whether a video post may ship
(`_post_missing_required_asset`) is deliberately somewhere else.
"""
import os
import shutil
import subprocess
import textwrap
from dataclasses import dataclass
from typing import Callable, Optional

from cqc_lem.utilities.env_constants import (
    VIDEO_CAPTION_HOLD_SECONDS,
    VIDEO_CAPTION_MAX_CHARS_PER_LINE,
    VIDEO_CAPTION_MAX_LINES,
    VIDEO_CAPTION_RENDER_COST_PER_MINUTE,
)
from cqc_lem.utilities.linkedin_formatter import strip_non_bmp
from cqc_lem.utilities.logger import log_debug, log_info, log_warning

TASK_NAME = "apply_video_captions"

# Default video length used for cost attribution when ffprobe cannot read the real one. Matches
# video_models.DEFAULT_VIDEO_DURATION; duplicated as a literal so this module stays importable
# without pulling the Runway model registry in.
_FALLBACK_DURATION_SECONDS = 5.0

# libass style for the burned card: large, bold, white on a semi-opaque black box, bottom-centred
# with a generous margin. Bottom-centred is the safe default for a 9:16 talking-head frame — the
# face sits in the upper two thirds — but it is NOT why avatar videos are gated; that is the
# user's own opt-in, because "safe" is not a promise this module gets to make about someone's
# likeness. Colours are libass BGR, not RGB.
CAPTION_STYLE = (
    "FontName=DejaVu Sans,Fontsize=28,Bold=1,PrimaryColour=&H00FFFFFF,"
    "BorderStyle=3,Outline=2,Shadow=0,BackColour=&H80000000,Alignment=2,MarginV=60"
)

# Lines that are not the hook: a hashtag block, a bare link, or punctuation-only decoration.
_SKIP_PREFIXES = ("#", "http://", "https://")


@dataclass(frozen=True)
class CaptionResult:
    """What `apply_captions_to_video` did, for the caller to persist and log.

    `video_path` is ALWAYS a usable video — the input path when nothing was burned — so a caller
    can assign it unconditionally. `burned` is the only field that says the pixels changed;
    `srt_path`/`caption_text` are set whenever a caption could be derived at all, including on the
    avatar opt-out path where the sidecar is written but the frame is untouched.
    """
    video_path: str
    burned: bool = False
    srt_path: Optional[str] = None
    caption_text: Optional[str] = None
    skipped_reason: Optional[str] = None


def srt_timestamp(seconds: float) -> str:
    """`HH:MM:SS,mmm` — the SRT cue format, clamped at zero so a negative offset can't emit junk."""
    total_ms = int(round(max(0.0, seconds) * 1000))
    hours, rem = divmod(total_ms, 3600000)
    minutes, rem = divmod(rem, 60000)
    secs, ms = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{ms:03d}"


def caption_lines(content: Optional[str], *, max_lines: int = VIDEO_CAPTION_MAX_LINES,
                  max_chars: int = VIDEO_CAPTION_MAX_CHARS_PER_LINE) -> list:
    """The post's opening, wrapped into at most `max_lines` burnable lines.

    The hook is whatever the post already opens with, minus the things that read as noise on a
    video frame: hashtag blocks, bare links, markdown emphasis, and emoji (non-BMP characters,
    which libass renders as tofu even where `send_keys` would accept them). An over-long hook is
    truncated with an ellipsis rather than dropped — a partial hook still communicates, a blank
    frame does not.

    Returns:
        `[]` when the post has no usable prose at all, which is the caller's signal to skip.
    """
    text = strip_non_bmp(content or "").replace("*", "").replace("_", " ")
    hook_parts: list = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(_SKIP_PREFIXES) or not any(c.isalnum() for c in line):
            continue
        hook_parts.append(line)
        # Two source lines is the cap the audit asked for; the wrap below decides how much of
        # them actually fits.
        if len(hook_parts) >= 2:
            break

    hook = " ".join(hook_parts).strip()
    if not hook:
        return []
    return textwrap.wrap(hook, width=max(8, int(max_chars)), max_lines=max(1, int(max_lines)),
                         placeholder="...")


def build_caption_srt(lines: list, out_path: str,
                      hold_seconds: float = VIDEO_CAPTION_HOLD_SECONDS) -> Optional[str]:
    """Write the one-cue SRT that holds the hook over the muted-autoplay window.

    ONE cue, not a transcript: LEM's generated videos have no narration to transcribe (premium
    Veo audio is steered to ambience), so what a viewer needs is the hook on screen while the
    player is still muted.

    Returns:
        The path written, or None when there is nothing to write.
    """
    if not lines:
        return None
    body = "\n".join(str(line) for line in lines)
    block = f"1\n{srt_timestamp(0.0)} --> {srt_timestamp(max(0.5, float(hold_seconds)))}\n{body}\n"
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(block)
    return out_path


def _ffmpeg_bin(name: str = "ffmpeg") -> Optional[str]:
    return shutil.which(name)


def _escape_filter_path(path: str) -> str:
    """Escape a path for ffmpeg's filtergraph parser.

    Backslash, colon and single quote all terminate or re-open a filter argument, so an unescaped
    Windows-style or timestamped path silently becomes a different filter — the failure looks like
    "no such file" on a file that exists.
    """
    return path.replace("\\", "\\\\").replace(":", r"\:").replace("'", r"\'")


def video_duration_seconds(video_path: str) -> float:
    """Best-effort duration for cost attribution. Falls back to a nominal length, never raises."""
    ffprobe = _ffmpeg_bin("ffprobe")
    if not ffprobe:
        return _FALLBACK_DURATION_SECONDS
    try:
        out = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", video_path],
            capture_output=True, text=True, timeout=15)
        duration = float((out.stdout or "").strip())
        return duration if duration > 0 else _FALLBACK_DURATION_SECONDS
    except (ValueError, OSError, subprocess.SubprocessError):
        return _FALLBACK_DURATION_SECONDS


def burn_captions(video_path: str, srt_path: str, out_path: str, timeout: int = 600) -> bool:
    """Burn `srt_path` into `video_path`, writing `out_path`. True only when the output is real.

    Video is re-encoded (libx264) because the burn paints pixels; audio is stream-copied, so a
    premium Veo soundtrack survives untouched and a silent render costs nothing extra. A non-zero
    exit, a missing ffmpeg, or a zero-byte output all return False — the caller then keeps the
    uncaptioned video.
    """
    ffmpeg = _ffmpeg_bin()
    if not ffmpeg:
        log_debug("ffmpeg is not installed — skipping video caption burn-in", task_name=TASK_NAME)
        return False

    video_filter = f"subtitles='{_escape_filter_path(srt_path)}':force_style='{CAPTION_STYLE}'"
    try:
        result = subprocess.run(
            [ffmpeg, "-y", "-i", video_path, "-vf", video_filter,
             "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "copy", out_path],
            capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as e:
        log_warning("Caption burn-in could not run — shipping the video uncaptioned", exc=e,
                    task_name=TASK_NAME)
        return False

    if result.returncode != 0:
        log_warning("Caption burn-in failed — shipping the video uncaptioned "
                    f"(ffmpeg: {(result.stderr or '')[-300:]})", task_name=TASK_NAME)
        return False
    try:
        if os.path.getsize(out_path) <= 0:
            log_warning("Caption burn-in produced an empty file — shipping the video uncaptioned",
                        task_name=TASK_NAME)
            return False
    except OSError as e:
        log_warning("Caption burn-in produced no readable output — shipping the video uncaptioned",
                    exc=e, task_name=TASK_NAME)
        return False
    return True


def _discard(path: str) -> None:
    """Remove a temp render, ignoring a file that was never written.

    Every burn failure leaves whatever ffmpeg had flushed before it gave up. Nothing purges it —
    `purge_post_assets` only knows the post's own video name — so an unremoved temp accumulates on
    the shared assets volume once per failed render, forever.
    """
    try:
        os.remove(path)
    except OSError:
        # Nothing to clean up (ffmpeg never created the file, or another sweep took it). The
        # caller is already on a failure path shipping the uncaptioned video — a temp we could
        # not delete must not turn that into a second failure.
        pass


def _track_burn_cost(video_path: str, user_id: Optional[int], post_id: Optional[int]) -> None:
    """Attribute the local render minutes the burn spent, mirroring the tutorial renderer."""
    try:
        from cqc_lem.utilities.observability import track_media_cost
        duration = video_duration_seconds(video_path)
        usd = round(VIDEO_CAPTION_RENDER_COST_PER_MINUTE * duration / 60.0, 6)
        track_media_cost("video", "local-ffmpeg", usd, user_id=user_id, post_id=post_id,
                         qty=duration, model="ffmpeg-libx264",
                         meta={"kind": "caption_burn"})
    except Exception as e:
        # Cost tracking must never cost us the caption that was already burned.
        log_debug(f"Caption burn cost not attributed ({type(e).__name__}: {e})")


def captions_allowed_on_avatar_video(user_id: Optional[int]) -> bool:
    """Whether burned text may cover an avatar-led frame — `users.avatar_caption_overlay`.

    Fails CLOSED, exactly like `avatar.guardrails`: an unreadable preference means the user's
    likeness keeps the frame to itself.
    """
    if not user_id:
        return False
    try:
        from cqc_lem.utilities.db import get_avatar_preferences
        return bool(get_avatar_preferences(user_id).get("avatar_caption_overlay"))
    except Exception as e:
        log_warning("Could not read the avatar caption opt-in — leaving the avatar frame alone",
                    exc=e, user_id=user_id, task_name=TASK_NAME)
        return False


def caption_srt_dir() -> str:
    """Where caption sidecars live inside the shared assets volume."""
    from cqc_lem import assets_dir
    return os.path.join(assets_dir, "videos", "captions")


def caption_srt_asset_url(srt_path: str) -> str:
    """Public `/api/assets` URL for a written sidecar — the same shape `posts.video_url` uses."""
    from cqc_lem.utilities.env_constants import API_URL_FINAL
    return f"{API_URL_FINAL}/api/assets?file_name=videos/captions/{os.path.basename(srt_path)}"


def apply_captions_to_video(video_path: str, content: Optional[str], *,
                            post_id: Optional[int] = None, user_id: Optional[int] = None,
                            avatar_led: bool = False,
                            validate: Optional[Callable[[str], bool]] = None) -> CaptionResult:
    """Caption a just-stored video in place: write the sidecar, then burn it in when allowed.

    The burn writes a temp file and only replaces `video_path` once ffmpeg produced something
    real, so a failed render leaves the original exactly where the caller put it — the stored
    `posts.video_url` stays valid whatever happens here.

    Args:
        video_path: the stored MP4, rewritten in place when the burn succeeds.
        content: the post's caption text — the source of the hook, never re-authored here.
        post_id: the post being captioned, for logging and cost attribution.
        user_id: the owner, who resolves the feature flag and the avatar opt-in.
        avatar_led: this video was rendered off the user's avatar (`posts.avatar_media`), so the
            burn needs `users.avatar_caption_overlay` on top of the feature flag.
        validate: the caller's asset probe, run on the BURNED file before it replaces the original.
            The input was probed (issue #1280); the re-encode makes the shipped file a different
            one, so without this the bytes that actually reach LinkedIn are the only bytes nothing
            ever checked. A False verdict keeps the original — ffmpeg exiting 0 is not proof the
            output is playable.

    Returns:
        A `CaptionResult` whose `video_path` is always usable. Callers persist `caption_text` /
        `caption_srt_url` only when the sidecar was written.
    """
    from cqc_lem.utilities.flags import VIDEO_CAPTIONS, flag_enabled

    if not flag_enabled(VIDEO_CAPTIONS, user_id=user_id):
        # DEBUG, not WARNING: an off flag is the documented default state, not a degraded path.
        log_debug("Video captions are off — leaving the render alone", post_id=post_id,
                  user_id=user_id, task_name=TASK_NAME)
        return CaptionResult(video_path=video_path, skipped_reason="flag_off")

    try:
        lines = caption_lines(content)
        if not lines:
            log_debug("No usable caption text on this post — nothing to burn", post_id=post_id,
                      user_id=user_id, task_name=TASK_NAME)
            return CaptionResult(video_path=video_path, skipped_reason="no_caption_text")

        from cqc_lem.utilities.utils import create_folder_if_not_exists
        srt_dir = caption_srt_dir()
        create_folder_if_not_exists(srt_dir)
        stem = os.path.splitext(os.path.basename(video_path))[0]
        srt_path = build_caption_srt(lines, os.path.join(srt_dir, f"{stem}.srt"))
        caption_text = "\n".join(lines)

        if avatar_led and not captions_allowed_on_avatar_video(user_id):
            # The sidecar still ships: the author can attach it on LinkedIn themselves, and
            # nothing was painted over their likeness to earn it.
            log_debug("Avatar-led video without the caption overlay opt-in — sidecar only",
                      post_id=post_id, user_id=user_id, task_name=TASK_NAME)
            return CaptionResult(video_path=video_path, srt_path=srt_path,
                                 caption_text=caption_text, skipped_reason="avatar_opt_out")

        burned_path = f"{video_path}.captioned.mp4"
        if not burn_captions(video_path, srt_path, burned_path):
            # No warning here — burn_captions logged the reason where it detected it.
            _discard(burned_path)
            return CaptionResult(video_path=video_path, srt_path=srt_path,
                                 caption_text=caption_text, skipped_reason="burn_failed")

        if validate is not None and not validate(burned_path):
            log_warning("The captioned video did not pass the asset probe — shipping the original",
                        post_id=post_id, user_id=user_id, task_name=TASK_NAME)
            _discard(burned_path)
            return CaptionResult(video_path=video_path, srt_path=srt_path,
                                 caption_text=caption_text, skipped_reason="burn_rejected")

        _track_burn_cost(video_path, user_id, post_id)
        # Replace in place so the stored asset URL (and any C2PA signing that follows) applies to
        # the video that actually ships.
        os.replace(burned_path, video_path)
        log_info("Burned muted-autoplay captions into the video", post_id=post_id, user_id=user_id,
                 task_name=TASK_NAME)
        return CaptionResult(video_path=video_path, burned=True, srt_path=srt_path,
                             caption_text=caption_text)
    except Exception as e:
        # Fail open: a caption is an enhancement, and the video already passed its probe.
        log_warning("Video captioning failed — shipping the video uncaptioned", exc=e,
                    post_id=post_id, user_id=user_id, task_name=TASK_NAME)
        return CaptionResult(video_path=video_path, skipped_reason="error")
