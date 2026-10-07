"""The $0 branded motion title card — the DEFAULT video when the AI source frame is refused.

Showcase round 4: a video post whose source frame the judge rejected fell straight to Pexels stock,
and stock matched the post's WORDS rather than its idea — "model retirement" came back as an
elderly couple in a snowy doorway, and a VARCHAR story got two hands reaching for each other. Both
read as automated. The fallback is now this card, drawn by code:

- the brand ground (charcoal, or off-white), the post's hook in Montserrat — gold on charcoal —
  revealed word by word, the kicker above it, a gold rule that draws in under it, and the byline;
- a slow drift of one large brand slab behind the type, so the clip moves for its whole length;
- 6-8 seconds (``VIDEO_TITLE_CARD_SECONDS``) at the tier's ratio (1:1 standard, 9:16 premium),
  piped frame by frame from Pillow into ffmpeg's libx264 — no drawtext, libass or system font;
- stored and captioned exactly like any other video (the caller's store path), named
  ``title_card_*`` so telemetry reads it as its own tier, never as Runway or stock.

The hook is the post's own words, never authored here: Stage 1's hook when the post has one,
else a COMPLETE first sentence or clause of the post (``video_captions.caption_candidates``).
The lower band of the frame is left clear for the burned caption, so the two never collide.

Pexels survives only behind ``VIDEO_PEXELS_FALLBACK_ENABLED`` (default off), and then only with a
query built from Stage 1's concrete visual anchors — never the post's raw words, and never an
anchor carrying a known stock homonym (``STOCK_HOMONYMS``). With no safe query there is no stock.
"""

import hashlib
import os
import secrets
import subprocess
from dataclasses import dataclass
from typing import Any, Optional

from cqc_lem.utilities.env_constants import isTrue
from cqc_lem.utilities.logger import log_debug, log_info, log_warning

TASK_NAME = "video_title_card"

TITLE_CARD_PREFIX = "title_card_"
TITLE_CARD_FPS = 24
TITLE_CARD_MIN_SECONDS, TITLE_CARD_MAX_SECONDS = 6.0, 8.0
TITLE_CARD_SIZES = {"1:1": (1080, 1080), "9:16": (1080, 1920)}
GROUND_CHARCOAL, GROUND_OFF_WHITE = "charcoal", "off_white"
GROUNDS = (GROUND_CHARCOAL, GROUND_OFF_WHITE)
# The longest post-derived hook the card sets (a complete clause; never cut to fit).
TITLE_HOOK_MAX_CHARS = 110
# The lower share of the frame the card keeps clear for the burned caption band.
CAPTION_CLEARANCE = 0.30

# Words whose stock-search sense is NOT the business sense the post means. A stock query carrying
# one returns the literal pun: "retirement" is pensioners, "model" a fashion shoot.
STOCK_HOMONYMS = frozenset({
    "retirement", "retire", "retired", "model", "models", "pipeline", "pipelines", "cloud",
    "clouds", "bug", "bugs", "token", "tokens", "agent", "agents", "spam", "stack", "stacks",
    "crash", "crashes", "deploy", "deployment", "release", "releases", "branch", "branches",
    "tree", "trees", "string", "strings", "hands", "hand", "reach", "ladder", "funnel", "lead",
    "leads", "burn", "cold", "warm", "hot", "fire", "firewall", "window", "windows", "shell",
    "python", "java", "rust", "container", "containers", "queue", "queues", "cookie", "cookies",
    "memory", "drift", "gateway", "bridge", "key", "keys", "lock", "golden", "hour", "zero",
})


def pexels_fallback_enabled() -> bool:
    """``VIDEO_PEXELS_FALLBACK_ENABLED``, read at the call site so the toggle needs no restart."""
    return isTrue(os.environ.get("VIDEO_PEXELS_FALLBACK_ENABLED", "False"))


def title_card_seconds() -> float:
    """``VIDEO_TITLE_CARD_SECONDS`` clamped to the 6-8 s the card is designed for."""
    try:
        seconds = float(os.environ.get("VIDEO_TITLE_CARD_SECONDS", "7.0"))
    except ValueError:
        seconds = 7.0
    return min(TITLE_CARD_MAX_SECONDS, max(TITLE_CARD_MIN_SECONDS, seconds))


def stock_query_from_concept(concept: Any) -> Optional[str]:
    """A Pexels query built ONLY from Stage 1's concrete visual anchors, or None.

    The literal-pun guard: the post's raw words are never the query, and an anchor carrying a
    ``STOCK_HOMONYMS`` word is dropped whole ("model retirement cron" is not three objects, it is
    a pun waiting to happen). At most two anchors, so the search stays specific.

    Args:
        concept: Stage 1's ``ImageConcept``, or None.

    Returns:
        The query, or None when no safe anchor survives (no stock at all).
    """
    if concept is None:
        return None
    from cqc_lem.utilities.ai.image_brief import person_word, usable_anchors

    safe = []
    for anchor in usable_anchors(concept):
        words = {w.strip(".,;:!?'\"").lower() for w in str(anchor).split()}
        if not words or words & STOCK_HOMONYMS or person_word(anchor):
            continue
        safe.append(str(anchor).strip())
        if len(safe) == 2:
            break
    return " ".join(safe) or None


def title_card_hook(text: Optional[str], concept: Any = None) -> Optional[str]:
    """The words the card sets — the post's own, never authored. None when there are none.

    Stage 1's hook (already gated for grammar and truth) when the post has one; else the post's
    complete first sentence or leading clause of at most ``TITLE_HOOK_MAX_CHARS``.

    Args:
        text: The post.
        concept: Stage 1's concept, or None.

    Returns:
        The hook, or None.
    """
    hook = str(getattr(concept, "hook_phrase", "") or "").strip()
    if hook:
        return hook
    from cqc_lem.utilities.video_captions import caption_candidates

    candidates = caption_candidates(text, TITLE_HOOK_MAX_CHARS)
    return candidates[0] if candidates else None


def pick_ground(seed: str) -> str:
    """Charcoal or off-white, stable per piece so a re-render draws the same card."""
    digest = hashlib.sha256((seed or "").encode("utf-8")).digest()
    return GROUNDS[digest[0] % len(GROUNDS)]


@dataclass(frozen=True)
class TitleCardPalette:
    """The card's colours as RGB tuples."""

    ground: tuple
    slab: tuple
    hook: tuple
    kicker: tuple
    rule: tuple
    byline: tuple


def title_card_palette(brand: Any = None, ground: str = GROUND_CHARCOAL) -> TitleCardPalette:
    """The card's colours in the author's brand.

    On charcoal the hook is the brand gold. Gold type cannot reach 4.5:1 on off-white, so there the
    hook is charcoal and the gold carries the rule and the slab instead; the kicker keeps the dark
    gold only where it still reads at 3:1.

    Args:
        brand: An ``image_compose.BrandStyle``; the reference brand by default.
        ground: ``GROUND_CHARCOAL`` or ``GROUND_OFF_WHITE``.

    Returns:
        The palette.
    """
    from cqc_lem.utilities.ai.image_compose import BrandStyle, _hex, contrast_ratio

    brand = brand or BrandStyle()
    dark, light = _hex(brand.neutral_dark), _hex(brand.neutral_light)
    gold, accent = _hex(brand.primary), _hex(brand.accent)

    def mix(a: tuple, b: tuple, t: float) -> tuple:
        return tuple(round(x + (y - x) * t) for x, y in zip(a, b))

    if ground == GROUND_OFF_WHITE:
        kicker = (accent if contrast_ratio(brand.accent, brand.neutral_light) >= 3.0 else dark)
        return TitleCardPalette(ground=light, slab=mix(light, gold, 0.22), hook=dark,
                                kicker=kicker, rule=gold, byline=mix(dark, light, 0.25))
    return TitleCardPalette(ground=dark, slab=mix(dark, accent, 0.16), hook=gold, kicker=accent
                            if contrast_ratio(brand.accent, brand.neutral_dark) >= 3.0 else gold,
                            rule=gold, byline=mix(light, dark, 0.15))


@dataclass(frozen=True)
class _Word:
    text: str
    x: int
    y: int
    start: float


@dataclass(frozen=True)
class TitleCardLayout:
    """Everything a frame needs, decided once: sizes, positions and each word's reveal time."""

    size: tuple
    palette: TitleCardPalette
    hook_font: Any
    hook_size: int
    words: tuple
    reveal_end: float
    rule_box: tuple
    kicker: str
    kicker_font: Any
    kicker_xy: tuple
    byline: str
    byline_font: Any
    byline_xy: tuple
    lines: tuple
    seconds: float
    slab_top: int = 0


def _medium_font(size: int) -> Any:
    from PIL import ImageFont

    from cqc_lem.utilities.ai.image_compose import FONT_DIR, load_font

    path = os.path.join(str(FONT_DIR), "Montserrat-Medium.ttf")
    try:
        return ImageFont.truetype(path, size)
    except OSError:
        return load_font(size)


def plan_title_card(hook: str, *, size: tuple, palette: TitleCardPalette, kicker: str = "",
                    byline: str = "", seconds: float = 7.0) -> TitleCardLayout:
    """Fit the hook and place every element. Raises ``ValueError`` when the hook cannot be set.

    Args:
        hook: The words to set.
        size: ``(width, height)``.
        palette: The colours.
        kicker: The uppercase topic tag ('' for none).
        byline: The author's name ('' for none).
        seconds: The clip length.

    Returns:
        The layout.
    """
    from PIL import Image, ImageDraw

    from cqc_lem.utilities.ai.image_compose import _line_height, _wrap, load_font

    if not (hook or "").strip():
        raise ValueError("a title card needs a hook")
    width, height = size
    margin = round(width * 0.09)
    draw = ImageDraw.Draw(Image.new("RGB", (8, 8)))
    usable_w = width - 2 * margin
    top_zone = round(height * 0.17)
    usable_h = round(height * (1 - CAPTION_CLEARANCE)) - top_zone - round(height * 0.08)
    fitted = None
    for size_px in range(round(width * 0.105), round(width * 0.05) - 1, -2):
        font = load_font(size_px)
        lines = _wrap(draw, hook.split(), font, usable_w)
        line_h = _line_height(font, size_px)
        if lines and len(lines) <= 5 and line_h * len(lines) <= usable_h:
            fitted = (font, size_px, lines, line_h)
            break
    if fitted is None:
        raise ValueError("the hook does not fit the title card legibly")
    font, size_px, lines, line_h = fitted
    block_h = line_h * len(lines)
    y0 = top_zone + max(0, (usable_h - block_h) // 2)
    total_words = sum(len(line.split()) for line in lines)
    step = min(0.2, 1.7 / max(1, total_words))
    words: list[_Word] = []
    index = 0
    space = draw.textlength(" ", font=font)
    for row, line in enumerate(lines):
        x = margin - draw.textbbox((0, 0), line, font=font)[0]
        for word in line.split():
            words.append(_Word(word, round(x), y0 + row * line_h, 0.35 + index * step))
            x += draw.textlength(word, font=font) + space
            index += 1
    reveal_end = (words[-1].start + 0.35) if words else 0.6
    rule_y = y0 + block_h + round(line_h * 0.35)
    rule_h = max(6, round(width * 0.008))
    rule_box = (margin, rule_y, margin + round(width * 0.22), rule_y + rule_h)
    kicker_font = load_font(max(18, round(width * 0.03)))
    byline_font = _medium_font(max(18, round(width * 0.034)))
    byline_xy = (margin, rule_y + rule_h + round(width * 0.035))
    # The slab lives BELOW the type block, so it never sits under a word.
    slab_top = min(round(height * 0.80), max(round(height * 0.62),
                                             byline_xy[1] + round(height * 0.08)))
    return TitleCardLayout(size=(width, height), palette=palette, hook_font=font,
                           hook_size=size_px, words=tuple(words), reveal_end=reveal_end,
                           rule_box=rule_box, kicker=(kicker or "").upper().strip(),
                           kicker_font=kicker_font, kicker_xy=(margin, round(height * 0.09)),
                           byline=(byline or "").strip(), byline_font=byline_font,
                           byline_xy=byline_xy, lines=tuple(lines), seconds=seconds,
                           slab_top=slab_top)


def _ease(p: float) -> float:
    p = min(1.0, max(0.0, p))
    return 1 - (1 - p) ** 3


def _mix(a: tuple, b: tuple, t: float) -> tuple:
    return tuple(round(x + (y - x) * t) for x, y in zip(a, b))


def render_title_card_frame(layout: TitleCardLayout, t: float) -> Any:
    """One frame at ``t`` seconds, as a PIL RGB image.

    Args:
        layout: ``plan_title_card`` output.
        t: Seconds from the start.

    Returns:
        The frame.
    """
    from PIL import Image, ImageDraw

    pal = layout.palette
    width, height = layout.size
    image = Image.new("RGB", (width, height), pal.ground)
    draw = ImageDraw.Draw(image)
    # The slab drifts left across the whole clip: the card moves even after the reveal ends. It
    # sits below the type block with a gold edge, under where the caption band will burn.
    drift = t / max(0.1, layout.seconds)
    sx = round(width * (0.46 - 0.12 * drift))
    sy = (layout.slab_top or round(height * 0.66)) + round(height * 0.02 * (1 - drift))
    edge = max(6, round(width * 0.008))
    draw.rectangle((sx, sy, width, height), fill=pal.slab)
    draw.rectangle((sx, sy, width, sy + edge - 1), fill=pal.rule)
    if layout.kicker:
        p = _ease(t / 0.4)
        draw.text(layout.kicker_xy, layout.kicker, font=layout.kicker_font,
                  fill=_mix(pal.ground, pal.kicker, p))
    lift = layout.hook_size * 0.35
    for word in layout.words:
        p = _ease((t - word.start) / 0.32)
        if p <= 0:
            continue
        draw.text((word.x, word.y + round((1 - p) * lift)), word.text, font=layout.hook_font,
                  fill=_mix(pal.ground, pal.hook, p))
    grow = _ease((t - layout.reveal_end) / 0.5)
    if grow > 0:
        left, top, right, bottom = layout.rule_box
        draw.rectangle((left, top, left + max(1, round((right - left) * grow)), bottom),
                       fill=pal.rule)
    if layout.byline:
        p = _ease((t - layout.reveal_end - 0.3) / 0.5)
        if p > 0:
            draw.text(layout.byline_xy, layout.byline, font=layout.byline_font,
                      fill=_mix(pal.ground, pal.byline, p))
    return image


def _ffmpeg() -> Optional[str]:
    import shutil

    return shutil.which("ffmpeg")


def write_title_card_video(layout: TitleCardLayout, out_path: str, *,
                           fps: int = TITLE_CARD_FPS, timeout: int = 300) -> bool:
    """Encode the card to ``out_path`` (H.264 MP4, silent). True only when the file is real.

    Frames are piped as raw RGB into one ffmpeg process. A missing ffmpeg, a broken pipe, a
    non-zero exit or an empty output all return False and leave no partial file behind.

    Args:
        layout: ``plan_title_card`` output.
        out_path: The MP4 to write.
        fps: Frames per second.
        timeout: Seconds ffmpeg may take to finish after the last frame.

    Returns:
        Whether a usable MP4 was written.
    """
    ffmpeg = _ffmpeg()
    if not ffmpeg:
        log_debug("ffmpeg is not installed — no title card", task_name=TASK_NAME)
        return False
    width, height = layout.size
    command = [ffmpeg, "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24",
               "-s", f"{width}x{height}", "-r", str(fps), "-i", "-", "-an", "-c:v", "libx264",
               "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
               "-movflags", "+faststart", out_path]
    frames = max(1, round(layout.seconds * fps))
    try:
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.PIPE)
        try:
            for index in range(frames):
                process.stdin.write(render_title_card_frame(layout, index / fps).tobytes())
            # communicate() flushes and closes stdin itself, then waits for the encoder.
            _out, err = process.communicate(timeout=timeout)
        except (OSError, ValueError, subprocess.SubprocessError):
            process.kill()
            _out, err = process.communicate()
    except (OSError, subprocess.SubprocessError) as e:
        log_warning("Title card encode could not run — no title card", exc=e, task_name=TASK_NAME)
        _remove(out_path)
        return False
    if process.returncode != 0:
        # A fixed template, the stderr tail at DEBUG: a per-file tail would never repeat a key.
        log_warning("Title card encode failed — no title card", task_name=TASK_NAME)
        log_debug(f"ffmpeg title card stderr: {(err or b'')[-300:]!r}", task_name=TASK_NAME)
        _remove(out_path)
        return False
    try:
        if os.path.getsize(out_path) <= 0:
            raise OSError("empty output")
    except OSError as e:
        log_warning("Title card encode produced no usable file — no title card", exc=e,
                    task_name=TASK_NAME)
        _remove(out_path)
        return False
    return True


def _remove(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        # Nothing was written, or it is already gone: the caller is on a failure path either way.
        pass


def title_card_dir() -> str:
    """Where title cards are rendered — inside the assets volume, so the store path may move them."""
    from cqc_lem import assets_dir

    return os.path.join(assets_dir, "videos", "title_card")


def _brand_for(user_id: Optional[int]) -> Any:
    from cqc_lem.utilities.ai.image_compose import brand_style

    try:
        from cqc_lem.utilities.brand_kit import brand_clause_for_user
        return brand_style(brand_clause_for_user(user_id) if user_id else None)
    except Exception as e:
        # The reference brand is still the brand: no reason to lose the card.
        log_debug("Brand kit unreadable for the title card — reference brand", error=str(e),
                  user_id=user_id, task_name=TASK_NAME)
        return brand_style(None)


def create_title_card_video(text: Optional[str], *, user_id: Optional[int] = None,
                            post_id: Optional[int] = None, concept: Any = None,
                            ratio: str = "1:1", byline: Optional[str] = None) -> Optional[str]:
    """Render the post's branded title card and return the local MP4 path, or None. Never raises.

    Args:
        text: The post.
        user_id: The author — their brand colours.
        post_id: For the file name and log context.
        concept: Stage 1's concept (its hook and kicker), or None.
        ratio: ``"1:1"`` or ``"9:16"``, matching the tier the clip replaces.
        byline: The author's name.

    Returns:
        A path inside the assets volume named ``title_card_*.mp4``, or None.
    """
    try:
        hook = title_card_hook(text, concept)
        if not hook:
            log_info("No complete hook to set on a title card", user_id=user_id, post_id=post_id,
                     task_name=TASK_NAME)
            return None
        from cqc_lem.utilities.ai.image_concept import concept_kicker
        from cqc_lem.utilities.utils import create_folder_if_not_exists

        palette = title_card_palette(_brand_for(user_id), pick_ground(f"{post_id}:{hook}"))
        layout = plan_title_card(hook, size=TITLE_CARD_SIZES.get(ratio, TITLE_CARD_SIZES["1:1"]),
                                 palette=palette, kicker=concept_kicker(concept),
                                 byline=byline or "", seconds=title_card_seconds())
        directory = title_card_dir()
        create_folder_if_not_exists(directory)
        out_path = os.path.join(directory,
                                f"{TITLE_CARD_PREFIX}{post_id or 0}_{secrets.token_hex(6)}.mp4")
        if not write_title_card_video(layout, out_path):
            return None
        log_info("Rendered the branded title card video", user_id=user_id, post_id=post_id,
                 task_name=TASK_NAME, ratio=ratio)
        return out_path
    except Exception as e:
        # The card is the fallback's fallback: a fault here is a defect worth an alert, and the
        # post then goes to the missing-asset gate exactly as a failed render always did.
        log_warning("Title card video raised — the video post has no asset", exc=e,
                    user_id=user_id, post_id=post_id, task_name=TASK_NAME)
        return None
