"""The ONE place a video post gets its muted-autoplay caption (issue #1278, decision 1A).

LinkedIn's feed player starts muted and the post's caption sits below the fold on mobile, so the
first 2-3 seconds of the video have to communicate on their own. This module burns the post's own
opening line into the rendered MP4, and always writes the `.srt` sidecar it burned so the author
can attach captions manually on LinkedIn if they want the native toggle too.

The card is ON BRAND (showcase round 4): Montserrat in off-white on a translucent charcoal band
with a gold rule, inside safe margins, drawn by Pillow and laid over the video with ffmpeg's
`overlay` filter — so the burn needs no libass or system font. It carries the FULL first sentence
or a complete clause of it, wrapped over at most `VIDEO_CAPTION_MAX_LINES` lines at the largest
size that fits; it is never cut with an ellipsis mid-thought ("…turn into a hidden…" was the
critic's exhibit). A post whose first clause cannot fit is not captioned at all.

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
  not the captions. `avatar_led` is the CALLER's answer and the caller owes it fail-CLOSED: an
  unreadable `posts.avatar_media` has to arrive here as True.

It fails OPEN in every direction: no ffmpeg, an unreadable video, a caption that comes out empty,
a non-zero exit, an output the caller's probe rejects — the post keeps the video it already had. A
caption is an enhancement, and the gate that decides whether a video post may ship
(`_post_missing_required_asset`) is deliberately somewhere else.
"""
import os
import re
import shutil
import subprocess
import textwrap
from dataclasses import dataclass
from typing import Any, Callable, Optional

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

# The burned band's geometry, as fractions of the frame (showcase round 4). Bottom-centred is the
# safe default for a 9:16 talking-head frame — the face sits in the upper two thirds — but it is
# NOT why avatar videos are gated; that is the user's own opt-in, because "safe" is not a promise
# this module gets to make about someone's likeness. The bottom margin clears LinkedIn's player
# controls; the side margins keep the type off the crop edge of every feed surface.
CAPTION_SIDE_MARGIN = 0.07
CAPTION_BOTTOM_MARGIN = 0.09
CAPTION_FONT_MAX = 0.056
CAPTION_FONT_MIN = 0.030
# The band is charcoal at this opacity (0-255): the frame still reads through it.
CAPTION_BAND_ALPHA = 205

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


_SENTENCE_END = re.compile(r"(?<=[.!?])[\"'”’)]*\s+")
_CLAUSE_BREAK = re.compile(r"\s*(?:[,;:]|\s[-–—]\s|[—–])\s*")
# A clause ending on one of these is a fragment ("…pushing four releases with"), not a thought.
_DANGLING_END = frozenset({"a", "an", "the", "and", "or", "but", "to", "of", "for", "with", "in",
                           "on", "at", "by", "from", "into", "than", "that", "which", "who",
                           "is", "are", "was", "were", "my", "our", "your", "their", "its"})


# A list marker opening a line ("- Automate ticket routing…", "• ", "2) "): showcase round 8's
# slot_141 card printed the literal "- " above its caption. Never part of the words a frame sets.
_LIST_MARKER = re.compile(r"^(?:[-–—•▪●◦‣]|\d{1,2}[.)](?=\s+[A-Za-z]))\s+")


def strip_list_marker(text: Optional[str]) -> str:
    """``text`` without a leading list marker ("- ", "• ", "2) ") — what a headline never prints."""
    return _LIST_MARKER.sub("", (text or "").lstrip(), count=1)


def _hook_sentences(content: Optional[str]) -> list:
    """The opening's sentences in order — each source line ends one, punctuated or not."""
    text = strip_non_bmp(content or "").replace("*", "").replace("_", " ")
    sentences: list = []
    for raw in text.splitlines():
        line = strip_list_marker(" ".join(raw.split()))
        if not line or line.startswith(_SKIP_PREFIXES) or not any(c.isalnum() for c in line):
            continue
        sentences += [part.strip() for part in _SENTENCE_END.split(line) if part.strip()]
        # Two source lines is the cap the audit asked for; the clause rule below decides how much
        # of them actually fits.
        if len(sentences) >= 2:
            break
    return sentences


def _clause_prefixes(sentence: str) -> list:
    """Every complete leading run of ``sentence``'s clauses, longest first, never the whole."""
    breaks = list(_CLAUSE_BREAK.finditer(sentence))
    out: list = []
    for match in reversed(breaks):
        clause = sentence[:match.start()].rstrip(" ,;:-–—")
        words = clause.split()
        if len(words) >= 3 and words[-1].lower().strip(".,!?") not in _DANGLING_END:
            out.append(clause)
    return out


def _post_sentences(content: Optional[str]) -> list:
    """Every sentence of the post's prose, in order — the same cleaning as `_hook_sentences`."""
    text = strip_non_bmp(content or "").replace("*", "").replace("_", " ")
    out: list = []
    for raw in text.splitlines():
        line = strip_list_marker(" ".join(raw.split()))
        if not line or line.startswith(_SKIP_PREFIXES) or not any(c.isalnum() for c in line):
            continue
        out += [part.strip() for part in _SENTENCE_END.split(line) if part.strip()]
    return out


_WORDS = re.compile(r"[a-z0-9]+")
# Token overlap (of the shorter side) at which a caption reads as the headline said again.
SAME_THOUGHT_OVERLAP = 0.6


def same_thought(a: Optional[str], b: Optional[str]) -> bool:
    """Whether two lines say the same thing — one contains the other, or their words overlap."""
    ta, tb = set(_WORDS.findall((a or "").lower())), set(_WORDS.findall((b or "").lower()))
    if not ta or not tb:
        return False
    return len(ta & tb) / min(len(ta), len(tb)) >= SAME_THOUGHT_OVERLAP


def _candidates_from(sentences: list, budget: int) -> list:
    if not sentences:
        return []
    first = sentences[0]
    out: list = []
    if len(sentences) > 1 and len(first) < 40:
        both = f"{first} {sentences[1]}"
        if len(both) <= budget:
            out.append(both)
    if len(first) <= budget:
        out.append(first)
    out += [clause for clause in _clause_prefixes(first) if len(clause) <= budget]
    return out


def payoff_candidates(content: Optional[str], budget: int, headline: Optional[str]) -> list:
    """Caption candidates that do NOT repeat ``headline`` — the post's next sentence, its payoff.

    Showcase round 6: a title card set the post's first sentence as its headline and the burned
    caption set the same sentence again beneath it. The caption now starts AFTER the sentence the
    headline came from, and no candidate may say the same thing as the headline.

    Args:
        content: The post.
        budget: The most characters the caption may hold.
        headline: The words the frame already shows.

    Returns:
        Candidates, best first; ``[]`` when nothing but the headline fits.
    """
    sentences = _post_sentences(content)
    if headline:
        matched = [i for i, sentence in enumerate(sentences) if same_thought(sentence, headline)]
        if matched:
            # The payoff follows the headline; a headline that closes the post leaves the rest.
            sentences = (sentences[matched[0] + 1:]
                         or [s for s in sentences if not same_thought(s, headline)])
    return [c for c in _candidates_from(sentences, budget) if not same_thought(c, headline)]


def caption_candidates(content: Optional[str], budget: int) -> list:
    """The texts a caption may carry, preferred first — each a COMPLETE thought within ``budget``.

    The first sentence whole (with the next one too when the first is short); then the longest
    leading runs of the first sentence's clauses that fit. Never a truncation: a first clause
    longer than ``budget`` yields no candidate at all.

    Args:
        content: The post.
        budget: The most characters the caption may hold.

    Returns:
        Candidate captions, best first; ``[]`` when no complete thought fits.
    """
    return _candidates_from(_hook_sentences(content), budget)


def caption_lines(content: Optional[str], *, max_lines: int = VIDEO_CAPTION_MAX_LINES,
                  max_chars: int = VIDEO_CAPTION_MAX_CHARS_PER_LINE,
                  avoid: Optional[str] = None) -> list:
    """The post's opening, as at most ``max_lines`` lines that carry a COMPLETE thought.

    The hook is whatever the post already opens with, minus the things that read as noise on a
    video frame: hashtag blocks, bare links, markdown emphasis, and emoji (non-BMP characters,
    which the burn would render as tofu). It is the FULL first sentence when that fits, else the
    longest complete leading clause of it — never a cut with an ellipsis (showcase round 4:
    "…turn into a hidden…"). A post whose first clause cannot fit is not captioned.

    ``avoid`` is the headline the frame already shows (a title card's): the caption then carries
    the post's NEXT sentence instead (`payoff_candidates`), never the headline again.

    Returns:
        `[]` when the post has no usable prose, or no complete thought that fits — the caller's
        signal to skip.
    """
    width = max(8, int(max_chars))
    lines_cap = max(1, int(max_lines))
    candidates = (payoff_candidates(content, width * lines_cap, avoid) if avoid
                  else caption_candidates(content, width * lines_cap))
    for candidate in candidates:
        # Never split "cost-saving" at its hyphen: the burn re-joins lines with a space.
        lines = textwrap.wrap(candidate, width=width, break_on_hyphens=False)
        if lines and len(lines) <= lines_cap:
            return lines
    return []


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


def caption_hold_seconds(video_path: str, title_card_headline: Optional[str]) -> float:
    """How long the caption cue holds: the muted-autoplay window, or the WHOLE title card.

    #2316: a title card keeps its lower band clear for the caption (``video_title_card``), so a cue
    that ended at ``VIDEO_CAPTION_HOLD_SECONDS`` left that band empty for the last 40% of the card.
    On a title card the cue runs to the end of the video.

    Args:
        video_path: The MP4.
        title_card_headline: ``video_title_card.card_headline`` — None for any other video.

    Returns:
        The cue's length in seconds.
    """
    if not title_card_headline:
        return VIDEO_CAPTION_HOLD_SECONDS
    return max(VIDEO_CAPTION_HOLD_SECONDS, video_duration_seconds(video_path))


def _ffmpeg_bin(name: str = "ffmpeg") -> Optional[str]:
    return shutil.which(name)


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


def video_dimensions(video_path: str) -> Optional[tuple]:
    """``(width, height)`` of the video's first stream via ffprobe, or None. Never raises."""
    ffprobe = _ffmpeg_bin("ffprobe")
    if not ffprobe:
        return None
    try:
        out = subprocess.run(
            [ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries",
             "stream=width,height", "-of", "csv=p=0:s=x", video_path],
            capture_output=True, text=True, timeout=15)
        width, height = (int(v) for v in str(out.stdout or "").strip().split("x")[:2])
        return (width, height) if width > 0 and height > 0 else None
    except (ValueError, TypeError, OSError, subprocess.SubprocessError):
        return None


def read_srt_cue(srt_path: str) -> tuple:
    """The ONE cue ``build_caption_srt`` wrote, as ``(lines, hold_seconds)``; ``([], 0.0)`` if none.

    The sidecar is the single record of what is burned, so the burn reads it back rather than
    being handed the text a second way that could drift from it.
    """
    try:
        with open(srt_path, "r", encoding="utf-8") as handle:
            rows = [row.rstrip("\n") for row in handle]
    except OSError:
        return [], 0.0
    for index, row in enumerate(rows):
        match = _CUE_TIMING.match(row)
        if match:
            h, m, sec, ms = (int(v) for v in match.groups())
            lines = []
            for text in rows[index + 1:]:
                if not text.strip():
                    break
                lines.append(text.strip())
            return lines, h * 3600 + m * 60 + sec + ms / 1000.0
    return [], 0.0


_CUE_TIMING = re.compile(r"\s*\d+:\d+:\d+,\d+\s*-->\s*(\d+):(\d+):(\d+),(\d+)")


def fit_caption(draw: Any, text: str, max_width: int, max_lines: int, high: int,
                low: int) -> Optional[tuple]:
    """The largest Montserrat size in ``[low, high]`` that sets ``text`` in ``max_lines`` lines.

    Returns:
        ``(font, size, lines, line_height)``, or None when no size fits.
    """
    from cqc_lem.utilities.ai.image_compose import _line_height, _wrap, load_font

    words = text.split()
    for size in range(max(low, high), low - 1, -2):
        font = load_font(size)
        lines = _wrap(draw, words, font, max_width)
        if lines and len(lines) <= max_lines:
            return font, size, lines, _line_height(font, size)
    return None


def caption_band_top(size: tuple, max_lines: Optional[int] = None) -> int:
    """The HIGHEST y the burned caption band can reach on a frame of ``size``.

    The band's worst case — the largest font at the most lines — so anything a frame draws above
    it can never sit under a caption (showcase round 6: the title card's byline did).

    Args:
        size: The video's ``(width, height)``.
        max_lines: The most lines; ``VIDEO_CAPTION_MAX_LINES`` by default.

    Returns:
        The band's top edge in pixels.
    """
    from cqc_lem.utilities.ai.image_compose import _line_height, load_font

    width, height = int(size[0]), int(size[1])
    basis = min(width, height)
    size_px = round(basis * CAPTION_FONT_MAX)
    line_h = _line_height(load_font(size_px), size_px)
    lines = int(max_lines or VIDEO_CAPTION_MAX_LINES)
    bottom = height - round(height * CAPTION_BOTTOM_MARGIN)
    return bottom - line_h * lines - 2 * round(basis * 0.025)


def render_caption_overlay(lines: list, size: tuple, out_path: str, brand: Any = None,
                           max_lines: Optional[int] = None) -> bool:
    """Draw the on-brand caption band as a full-frame transparent PNG. True when it was drawn.

    Off-white Montserrat on a translucent charcoal band, a gold rule on its leading edge, inside
    the safe margins. The text is the caption's own lines re-wrapped by PIXEL width at the largest
    size that fits — the character wrap that chose them is only a budget.

    Args:
        lines: ``caption_lines`` output.
        size: The video's ``(width, height)``.
        out_path: Where to write the PNG.
        brand: An ``image_compose.BrandStyle``; the reference brand by default.
        max_lines: The most lines; ``VIDEO_CAPTION_MAX_LINES`` by default.

    Returns:
        False when there is no text or it cannot be set legibly — the caller ships uncaptioned.
    """
    from PIL import Image, ImageDraw

    from cqc_lem.utilities.ai.image_compose import BrandStyle, _hex

    text = " ".join(str(line) for line in lines or []).strip()
    if not text:
        return False
    brand = brand or BrandStyle()
    width, height = int(size[0]), int(size[1])
    basis = min(width, height)
    side = round(width * CAPTION_SIDE_MARGIN)
    pad_x, pad_y = round(basis * 0.035), round(basis * 0.025)
    rule = max(4, round(basis * 0.008))
    image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    fitted = fit_caption(draw, text, width - 2 * side - 2 * pad_x - rule,
                         int(max_lines or VIDEO_CAPTION_MAX_LINES),
                         round(basis * CAPTION_FONT_MAX), round(basis * CAPTION_FONT_MIN))
    if fitted is None:
        return False
    font, _size, wrapped, line_h = fitted
    bottom = height - round(height * CAPTION_BOTTOM_MARGIN)
    top = bottom - line_h * len(wrapped) - 2 * pad_y
    radius = round(basis * 0.018)
    draw.rounded_rectangle((side, top, width - side, bottom), radius=radius,
                           fill=(*_hex(brand.neutral_dark), CAPTION_BAND_ALPHA))
    # The gold rule sits inside the band's rounded corners, never outside them.
    draw.rectangle((side + radius // 3, top + radius, side + radius // 3 + rule - 1,
                    bottom - radius), fill=(*_hex(brand.primary), 255))
    y = top + pad_y
    ink = (*_hex(brand.neutral_light), 255)
    for line in wrapped:
        left = draw.textbbox((0, 0), line, font=font)[0]
        draw.text((side + rule + pad_x - left, y), line, font=font, fill=ink)
        y += line_h
    image.save(out_path, "PNG")
    return True


def burn_captions(video_path: str, srt_path: str, out_path: str, timeout: int = 600,
                  brand: Any = None) -> bool:
    """Burn `srt_path`'s cue into `video_path`, writing `out_path`. True only when the output is real.

    The cue is drawn by `render_caption_overlay` and laid over the video for the cue's window by
    ffmpeg's `overlay` filter. Video is re-encoded (libx264) because the burn paints pixels; audio
    is stream-copied, so a premium Veo soundtrack survives untouched and a silent render costs
    nothing extra. A missing ffmpeg, an unreadable cue or video size, a caption that cannot be set
    legibly, a non-zero exit or a zero-byte output all return False — the caller then keeps the
    uncaptioned video.
    """
    ffmpeg = _ffmpeg_bin()
    if not ffmpeg:
        log_debug("ffmpeg is not installed — skipping video caption burn-in", task_name=TASK_NAME)
        return False
    lines, hold = read_srt_cue(srt_path)
    size = video_dimensions(video_path)
    if not lines or size is None:
        log_warning("Caption burn-in could not read the cue or the video size — shipping the "
                    "video uncaptioned", task_name=TASK_NAME)
        return False
    overlay_path = f"{out_path}.band.png"
    try:
        if not render_caption_overlay(lines, size, overlay_path, brand=brand):
            log_warning("The caption does not fit the frame legibly — shipping the video "
                        "uncaptioned", task_name=TASK_NAME)
            return False
        graph = (f"[0:v][1:v]overlay=0:0:enable='between(t,0,{max(0.5, hold):.3f})'"
                 ",format=yuv420p[v]")
        try:
            result = subprocess.run(
                [ffmpeg, "-y", "-i", video_path, "-i", overlay_path, "-filter_complex", graph,
                 "-map", "[v]", "-map", "0:a?", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                 "-c:a", "copy", "-movflags", "+faststart", out_path],
                capture_output=True, text=True, timeout=timeout)
        except (OSError, subprocess.SubprocessError) as e:
            log_warning("Caption burn-in could not run — shipping the video uncaptioned", exc=e,
                        task_name=TASK_NAME)
            return False
    finally:
        _discard(overlay_path)

    if result.returncode != 0:
        # The message stays a fixed template and the ffmpeg tail goes to DEBUG on purpose: the
        # recurrence key is built from the interpolated string, and a stderr tail (frame counts,
        # per-file paths) is unbounded in ways the masks do not catch — so interpolating it here
        # would mean a systematically broken burn never repeats a key, never escalates, and
        # silently ships every video post uncaptioned forever.
        log_warning("Caption burn-in failed — shipping the video uncaptioned", task_name=TASK_NAME)
        log_debug(f"ffmpeg caption burn stderr: {(result.stderr or '')[-300:]}",
                  task_name=TASK_NAME)
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


def _caption_brand(user_id: Optional[int]) -> Any:
    """The author's exact brand colours for the band; the reference brand when unreadable."""
    from cqc_lem.utilities.ai.image_compose import brand_style

    try:
        from cqc_lem.utilities.brand_kit import brand_clause_for_user
        return brand_style(brand_clause_for_user(user_id) if user_id else None)
    except Exception as e:
        # The reference brand is a correct, on-brand fallback — never a reason to skip the burn.
        log_debug("Brand kit unreadable for the caption band — reference brand", error=str(e),
                  user_id=user_id, task_name=TASK_NAME)
        return brand_style(None)


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


def burned_caption_text(content: Optional[str], *, user_id: Optional[int] = None,
                        avatar_led: bool = False) -> Optional[str]:
    """The text `apply_captions_to_video` WILL burn onto this post's video, or None. Never raises.

    The same `caption_lines` and the same two gates (the `VIDEO_CAPTIONS` flag; the avatar overlay
    opt-in on an avatar-led frame), asked BEFORE the render — so the source frame's judge can read
    the frame the way a viewer will, with its caption as the headline (PR #2249). None whenever no
    caption would ship, because a headline the viewer never sees must not grade the frame.

    Args:
        content: The post's caption text.
        user_id: The owner, for the flag and the opt-in.
        avatar_led: The frame renders off the user's avatar.

    Returns:
        The caption as one line, or None.
    """
    try:
        from cqc_lem.utilities.flags import VIDEO_CAPTIONS, flag_enabled

        if not flag_enabled(VIDEO_CAPTIONS, user_id=user_id):
            return None
        if avatar_led and not captions_allowed_on_avatar_video(user_id):
            return None
        return " ".join(caption_lines(content)) or None
    except Exception as e:
        log_debug("Caption text unavailable for the frame judge", error=str(e), user_id=user_id,
                  task_name=TASK_NAME)
        return None


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
        # A title card already shows a headline; the caption must not say it again.
        from cqc_lem.utilities.video_title_card import card_headline

        headline = card_headline(video_path, content)
        lines = caption_lines(content, avoid=headline)
        if not lines:
            log_debug("No usable caption text on this post — nothing to burn", post_id=post_id,
                      user_id=user_id, task_name=TASK_NAME)
            return CaptionResult(video_path=video_path, skipped_reason="no_caption_text")

        from cqc_lem.utilities.utils import create_folder_if_not_exists
        srt_dir = caption_srt_dir()
        create_folder_if_not_exists(srt_dir)
        stem = os.path.splitext(os.path.basename(video_path))[0]
        srt_path = build_caption_srt(lines, os.path.join(srt_dir, f"{stem}.srt"),
                                     hold_seconds=caption_hold_seconds(video_path, headline))
        caption_text = "\n".join(lines)

        if avatar_led and not captions_allowed_on_avatar_video(user_id):
            # The sidecar still ships: the author can attach it on LinkedIn themselves, and
            # nothing was painted over their likeness to earn it.
            log_debug("Avatar-led video without the caption overlay opt-in — sidecar only",
                      post_id=post_id, user_id=user_id, task_name=TASK_NAME)
            return CaptionResult(video_path=video_path, srt_path=srt_path,
                                 caption_text=caption_text, skipped_reason="avatar_opt_out")

        burned_path = f"{video_path}.captioned.mp4"
        if not burn_captions(video_path, srt_path, burned_path, brand=_caption_brand(user_id)):
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
