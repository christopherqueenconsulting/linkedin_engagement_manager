"""Animated loop posts — the ONE place a post's GIF / short-MP4 loop is made (docs/animated-posts.md).

Some LinkedIn creators post GIFs, cinemagraphs and short loops. There is no quantitative evidence
that a loop out-performs a still, so this is an OPT-IN experiment surface behind
`ANIMATED_POST_ENABLED` (`flags.ANIMATED_POST`, default OFF): with the flag off nothing here runs
and no post changes.

What it deliberately does NOT do:

- **It adds no generation path.** The source clip is the existing Runway image-to-video render
  (`video_models.create_runway_video`), animated off the post's OWN already-gated still — the frame
  `post_image.generate_image_for_post` stored through the staged engine. The motion prompt is the
  existing concept-driven author (`get_runway_ml_video_prompt_from_ai`) fed the brief receipt that
  render left behind, so no second Stage 1 call is paid. The only addition is
  `LOOP_MOTION_DIRECTIVE`, which asks for a cinemagraph: camera locked, ONE element moving.
- **It never blocks a post.** Every function returns None on failure and never raises; the post
  keeps its static image, and the publisher (`poster.share_animated_image_on_linkedin`) falls back
  to that image on any failure before the share exists.

LinkedIn limits (cited in docs/animated-posts.md): a GIF goes through the Images API, which caps it
at 250 frames and 36,152,320 pixels. The 5 MB budget here is ours, not LinkedIn's — a feed GIF that
size already loads slowly on mobile, and it keeps the shared assets volume bounded.
"""
import math
import os
import subprocess
import tempfile
import uuid
from types import SimpleNamespace
from typing import Optional

from cqc_lem.utilities.logger import log_debug, log_info, log_warning
from cqc_lem.utilities.video_captions import _discard, _ffmpeg_bin

TASK_NAME = "animated_loop"

# LinkedIn Images API: "GIF format supports up to 250 frames" and "less than 36,152,320 pixels".
GIF_MAX_FRAMES = 250
GIF_MAX_PIXELS = 36_152_320
# Our own budget, not LinkedIn's (the UI accepts 100 MB). See the module docstring.
GIF_MAX_BYTES = 5 * 1024 * 1024
# Width/fps step-down tries before giving up — bounded so a clip that will not fit costs at most
# three local encodes.
LOOP_MAX_ATTEMPTS = 3
# LinkedIn autoplays and loops a short MP4 (a weak, third-party source — see the doc); 15 s is the
# ceiling this module will produce, and the Videos API floor is 3 s.
LOOP_MP4_MAX_SECONDS = 15.0
LOOP_MP4_MIN_SECONDS = 3.0
# Runway's minimum clip on the standard tier (gen4_turbo accepts 5 or 10).
LOOP_SOURCE_DURATION = 5
# Runway prompt_text is capped at 1000 chars; leave headroom.
LOOP_PROMPT_MAX_CHARS = 900

# Prepended so a motion author that wrote a camera move is overridden, not obeyed.
LOOP_MOTION_DIRECTIVE = (
    "Cinemagraph loop: camera locked off on a tripod, no pan, no zoom, no cut. Exactly ONE element "
    "moves, gently and continuously (steam, light, fabric, water, a screen glow); everything else "
    "stays perfectly still. Subtle, slow motion that can repeat seamlessly."
)

# The still's ratio -> a Runway ratio gen4_turbo renders. 4:5 has no gen4 resolution, so it rides
# the nearest portrait one (3:4) and Runway crops the edges.
_RUNWAY_RATIO_FOR_IMAGE = {"4:5": "3:4", "1:1": "1:1", "16:9": "16:9", "9:16": "9:16"}

_FFMPEG_TIMEOUT = 180


def animated_post_enabled(user_id: Optional[int]) -> bool:
    """`ANIMATED_POST_ENABLED`, read at the CALL SITE. Unreadable reads as OFF."""
    try:
        from cqc_lem.utilities.flags import ANIMATED_POST, flag_enabled
        return bool(flag_enabled(ANIMATED_POST, user_id=user_id))
    except Exception as e:
        log_debug(f"Animated-post flag unreadable — treating as off ({type(e).__name__})",
                  user_id=user_id, task_name=TASK_NAME)
        return False


def gif_frame_count(gif_path: str) -> Optional[int]:
    """Frames in a GIF, or None when it cannot be read as one. Never raises."""
    try:
        from PIL import Image
        with Image.open(gif_path) as img:
            if (img.format or "").upper() != "GIF":
                return None
            return int(getattr(img, "n_frames", 1))
    except Exception as e:
        log_debug(f"GIF could not be read ({type(e).__name__})", task_name=TASK_NAME)
        return None


def gif_within_limits(gif_path: Optional[str], max_bytes: int = GIF_MAX_BYTES) -> bool:
    """Is this file a GIF LinkedIn's Images API will take, inside our byte budget?

    Checked by the producer after every encode AND by the publisher right before upload, because
    the file on disk is the only thing either can trust.
    """
    if not gif_path or not os.path.isfile(gif_path):
        return False
    try:
        size = os.path.getsize(gif_path)
    except OSError:
        return False
    if size <= 0 or size > max_bytes:
        return False
    frames = gif_frame_count(gif_path)
    if frames is None or frames < 1 or frames > GIF_MAX_FRAMES:
        return False
    try:
        from PIL import Image
        with Image.open(gif_path) as img:
            width, height = img.size
    except Exception:
        return False
    return width * height < GIF_MAX_PIXELS


def _attempt_plan(width: int, fps: int) -> list:
    """The (width, fps) tries, smallest last: at most `LOOP_MAX_ATTEMPTS`, each strictly smaller."""
    plan = [(int(width), int(fps)),
            (int(width * 0.75), max(int(fps) - 2, 6)),
            (int(width * 0.55), max(int(fps) - 4, 6))]
    return plan[:LOOP_MAX_ATTEMPTS]


def _max_frames_for(seconds: float, fps: int) -> int:
    return min(GIF_MAX_FRAMES, max(1, math.ceil(seconds * fps)))


def gif_filter(seconds: float, fps: int, width: int) -> str:
    """The palettegen/paletteuse filtergraph, ping-ponged so the loop has no visible seam.

    The clip's first `seconds/2` plays forward then in reverse, so the last frame IS the first —
    a Runway clip is not authored to loop, and a hard cut back to frame 0 reads as a glitch.
    """
    half = max(seconds / 2.0, 0.5)
    return (f"trim=duration={half:.3f},setpts=PTS-STARTPTS,fps={int(fps)},"
            f"scale={int(width)}:-1:flags=lanczos,split[f][r];[r]reverse[rv];"
            f"[f][rv]concat=n=2:v=1:a=0,split[a][b];[a]palettegen=stats_mode=diff[p];"
            f"[b][p]paletteuse=dither=bayer:bayer_scale=5:diff_mode=rectangle")


def _run_ffmpeg(args: list) -> bool:
    """Run one ffmpeg command; True only on exit 0. Never raises."""
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=_FFMPEG_TIMEOUT)
    except (OSError, subprocess.SubprocessError) as e:
        log_debug(f"ffmpeg could not run ({type(e).__name__})", task_name=TASK_NAME)
        return False
    if result.returncode != 0:
        log_debug(f"ffmpeg loop encode stderr: {(result.stderr or '')[-300:]}", task_name=TASK_NAME)
        return False
    return True


def _temp_out(suffix: str) -> str:
    return os.path.join(tempfile.gettempdir(), f"loop_{uuid.uuid4().hex}{suffix}")


def make_loop_from_video(mp4_path: str, *, seconds: float = 4.0, fps: int = 12,
                         width: int = 720) -> Optional[str]:
    """Turn a short MP4 into a seamless looping GIF inside LinkedIn's limits and our byte budget.

    Hard-enforces ≤`GIF_MAX_FRAMES` frames (an `-frames:v` cap AND a read-back of the file) and
    ≤`GIF_MAX_BYTES`, stepping width and fps down for at most `LOOP_MAX_ATTEMPTS` encodes.

    Returns:
        The path of a temp GIF the caller owns, or None — no ffmpeg, an unreadable source, or a
        clip that will not fit in three tries. Never raises.
    """
    if not mp4_path or not os.path.isfile(mp4_path):
        return None
    ffmpeg = _ffmpeg_bin()
    if not ffmpeg:
        log_debug("ffmpeg is not installed — no animated loop", task_name=TASK_NAME)
        return None

    for attempt, (try_width, try_fps) in enumerate(_attempt_plan(width, fps), start=1):
        out_path = _temp_out(".gif")
        args = [ffmpeg, "-y", "-i", mp4_path, "-an",
                "-filter_complex", gif_filter(seconds, try_fps, try_width),
                "-frames:v", str(_max_frames_for(seconds, try_fps)),
                "-loop", "0", out_path]
        if _run_ffmpeg(args) and gif_within_limits(out_path):
            log_info("Made animated loop GIF", task_name=TASK_NAME, attempt=attempt,
                     width=try_width, fps=try_fps)
            return out_path
        _discard(out_path)
        log_debug(f"Loop GIF attempt {attempt} over limits or failed — stepping down",
                  task_name=TASK_NAME)
    log_info("Could not fit the animated loop inside the GIF limits — keeping the still",
             task_name=TASK_NAME)
    return None


def make_loop_mp4(mp4_path: str, seconds: float = 6) -> Optional[str]:
    """A trimmed MP4 of ≤`LOOP_MP4_MAX_SECONDS` that loops seamlessly (forward then reversed).

    The alternative output to the GIF: same ping-pong seam handling, H.264 + yuv420p so every
    player takes it, silent (a loop with a soundtrack that reverses is worse than none).

    Returns:
        A temp MP4 path the caller owns, or None. Never raises.
    """
    if not mp4_path or not os.path.isfile(mp4_path):
        return None
    ffmpeg = _ffmpeg_bin()
    if not ffmpeg:
        log_debug("ffmpeg is not installed — no loop MP4", task_name=TASK_NAME)
        return None
    total = min(max(float(seconds), LOOP_MP4_MIN_SECONDS), LOOP_MP4_MAX_SECONDS)
    half = total / 2.0
    out_path = _temp_out(".mp4")
    video_filter = (f"trim=duration={half:.3f},setpts=PTS-STARTPTS,split[f][r];[r]reverse[rv];"
                    f"[f][rv]concat=n=2:v=1:a=0,format=yuv420p")
    args = [ffmpeg, "-y", "-i", mp4_path, "-an", "-filter_complex", video_filter,
            "-t", f"{total:.3f}", "-c:v", "libx264", "-movflags", "+faststart", out_path]
    if not _run_ffmpeg(args):
        _discard(out_path)
        return None
    try:
        if os.path.getsize(out_path) <= 0:
            _discard(out_path)
            return None
    except OSError:
        return None
    return out_path


def loop_motion_prompt(motion: str) -> str:
    """The cinemagraph directive FIRST, then the concept-driven motion, inside Runway's cap."""
    combined = f"{LOOP_MOTION_DIRECTIVE} {(motion or '').strip()}".strip()
    return combined[:LOOP_PROMPT_MAX_CHARS]


def _receipt_concept(receipt: Optional[dict]):
    """The Stage 1 concept from a brief receipt, shaped for the motion author's getattr reads."""
    concept = (receipt or {}).get("concept")
    if isinstance(concept, dict) and concept.get("thesis"):
        return SimpleNamespace(**concept)
    return None


def _code_drawn(receipt: Optional[dict]) -> bool:
    """Did the stored still ship as a code-drawn data graphic (its receipt says so)?"""
    from cqc_lem.utilities.ai.image_concept import CODE_DRAWN_ARCHETYPES

    return str((receipt or {}).get("archetype_rendered") or "") in CODE_DRAWN_ARCHETYPES


def _download(url: str) -> Optional[str]:
    """Fetch the Runway output into a temp MP4. None on failure."""
    import requests
    out_path = _temp_out(".mp4")
    try:
        response = requests.get(url, timeout=60)
        response.raise_for_status()
        with open(out_path, "wb") as fh:
            fh.write(response.content)
        return out_path
    except Exception as e:
        log_debug(f"Loop source clip download failed ({type(e).__name__})", task_name=TASK_NAME)
        _discard(out_path)
        return None


def produce_post_loop(user_id: int, post_id: Optional[int], text: str,
                      image_url: Optional[str]) -> Optional[str]:
    """Animate a text post's stored still into a looping GIF stored beside it.

    Off unless `ANIMATED_POST_ENABLED`. Reuses the video pipeline end to end: Runway
    image-to-video on the STORED (already gated) still at `LOOP_SOURCE_DURATION`, with the
    concept-driven motion author fed that still's brief receipt, plus `LOOP_MOTION_DIRECTIVE`.

    Returns:
        The stored GIF's path, or None. Never raises — the still is untouched either way.
    """
    if not post_id or not image_url or not animated_post_enabled(user_id):
        return None
    clip_path = gif_path = None
    try:
        from cqc_lem.utilities.ai.ai_helper import get_runway_ml_video_prompt_from_ai
        from cqc_lem.utilities.ai.video_models import create_runway_video
        from cqc_lem.utilities.env_constants import STANDARD_VIDEO_MODEL
        from cqc_lem.utilities.media_provenance import read_brief_receipt
        from cqc_lem.utilities.post_image import (
            post_image_abs_path,
            post_image_ratio,
            store_post_loop,
        )

        still = post_image_abs_path(image_url)
        if not still:
            return None
        receipt = read_brief_receipt(image_url)
        if _code_drawn(receipt):
            # A code-drawn graphic's figures were verified against the article; an image-to-video
            # model would re-draw them (archetype round, #2241). Its loop is drawn by code
            # instead, at $0: the same verified facts in motion (docs/motion-design.md).
            from cqc_lem.utilities.motion_design import loop_from_receipt

            gif_path = loop_from_receipt(receipt, user_id=user_id, post_id=post_id,
                                         ratio=post_image_ratio())
            if not gif_path:
                log_debug("No code-drawn loop for this data graphic — the still ships",
                          user_id=user_id, post_id=post_id, task_name=TASK_NAME)
                return None
            return store_post_loop(image_url, gif_path)
        image_prompt = str((receipt or {}).get("prompt") or "")
        motion = get_runway_ml_video_prompt_from_ai(text, image_prompt, model=STANDARD_VIDEO_MODEL,
                                                    user_id=user_id, post_id=post_id,
                                                    concept=_receipt_concept(receipt))
        ratio = _RUNWAY_RATIO_FOR_IMAGE.get(post_image_ratio(), "1:1")
        src = create_runway_video(still, loop_motion_prompt(motion), model=STANDARD_VIDEO_MODEL,
                                  ratio=ratio, duration=LOOP_SOURCE_DURATION,
                                  user_id=user_id, post_id=post_id)
        if not src:
            return None
        clip_path = _download(src) if str(src).startswith("http") else None
        if not clip_path:
            return None
        gif_path = make_loop_from_video(clip_path)
        if not gif_path:
            return None
        stored = store_post_loop(image_url, gif_path)
        if stored:
            log_info("Stored animated loop beside the post image", user_id=user_id,
                     post_id=post_id, task_name=TASK_NAME)
        return stored
    except Exception as e:
        # WARNING, not DEBUG: with the flag ON this is the feature failing, and a systematic
        # failure (Runway auth, a broken motion author) must escalate rather than silently ship
        # every post static. The post itself is unaffected.
        log_warning("Animated loop failed — the post keeps its still image", exc=e,
                    user_id=user_id, post_id=post_id, task_name=TASK_NAME)
        return None
    finally:
        for path in (clip_path, gif_path):
            if path:
                _discard(path)
