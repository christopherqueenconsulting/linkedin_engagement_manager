#!/usr/bin/env python3
r"""Render N posts in sequence through ``generate_image_for_post`` and contact-sheet the rhythm.

The gauntlet harness for the post treatment rotation (#2241, anti-monotony round). Each post goes
through the REAL ``post_image.generate_image_for_post`` into ``--assets-dir``, so every render's
brief receipt lands there and the next post's rotation reads it — treatment, panel variant,
layout, shot and photo grade all act exactly as they do in production. The result is one PNG of
every image in FEED order (newest first), each labelled with what the rotation chose.

Two modes:

- **paid** (default): nothing is mocked. Stage 1, the brief author, the renderer and the blind
  judge all run, so this needs the live stack — run it in a prod-image sidecar
  (``docs/image-stack.md``). It reads the user's real brand kit and profile.
- ``--offline``: $0 and no network. Stage 1 is a deterministic parse of each post (first sentence
  as thesis, the first grounded $/% figure as the thesis stat, first person as a human moment)
  that still runs the real archetype selection and layout/cast/shot rotation; the brief is a
  template; every AI scene is a labelled PLACEHOLDER tinted by its grade; the judge reports
  ``unchecked``. The code-drawn cards (``data_card``, ``quote_card``, charts) and the typeset
  panel variants are the real renderers.

Usage::

    PYTHONPATH=src python scripts/simulate_post_rhythm.py posts.json --user-id 1 \\
        --assets-dir /tmp/rhythm --offline [--card-share 0.4] [--byline "Jane Doe"]

``posts.json`` is a JSON list of post texts (or ``{"text": ...}`` objects).
"""

import argparse
import json
import os
import re
import sys
from contextlib import ExitStack
from typing import Any, Optional
from unittest.mock import patch

# Operator harness, not the app: a paid run in a prod-image sidecar carries the production
# POSTHOG_API_KEY and reads the brand kit through the DB, so a schema this branch is ahead of
# (`brand_kit` before its migration deployed) filed a production error-tracking issue (#2266,
# see `logger.telemetry_muted`). Set BEFORE cqc_lem is imported — the Logs handler is built then.
os.environ.setdefault("LEM_TELEMETRY_MUTED", "1")

if os.path.isdir(os.path.join(os.path.dirname(__file__), "..", "src")):
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

THUMB_WIDTH = 360
THUMB_HEIGHT = 450
LABEL_HEIGHT = 64
COLUMNS = 3
GUTTER = 16
# Placeholder tints per photo grade, so the contact sheet shows the grade rotating.
_GRADE_TINTS = {"daylight": (226, 222, 210), "cool_interior": (176, 190, 204),
                "warm_dusk": (232, 186, 130)}


def load_posts(path: str) -> list[str]:
    """The post texts in ``path``: a JSON list of strings or of ``{"text": ...}`` objects.

    Args:
        path: The JSON file.

    Returns:
        The non-empty texts, in order.

    Raises:
        ValueError: The file is not a list of posts.
    """
    with open(path, "r", encoding="utf-8") as handle:
        raw = json.load(handle)
    if not isinstance(raw, list):
        raise ValueError("posts file must be a JSON list")
    texts = [item.get("text") if isinstance(item, dict) else item for item in raw]
    return [t for t in texts if isinstance(t, str) and t.strip()]


# --- Offline stand-ins ------------------------------------------------------------------------

_STAT_IN_SENTENCE = re.compile(r"(\$)?(\d[\d,]*(?:\.\d+)?)\s?(%)?")
_FIRST_PERSON = re.compile(r"\b(?:I|I'm|I've|we|our|my)\b")


def _offline_stat(text: str) -> Optional[dict]:
    """The first $ or % figure the deterministic validator accepts, as a raw thesis_stat."""
    from cqc_lem.utilities.ai.image_concept import source_sentences

    for sentence in source_sentences(text):
        for match in _STAT_IN_SENTENCE.finditer(sentence):
            unit = match.group(1) or match.group(3) or ""
            if not unit:
                continue
            words = [w.strip(".,;:!?") for w in sentence.split()
                     if not any(ch.isdigit() for ch in w)]
            label = " ".join(w for w in words if w)[:60].rsplit(" ", 1)[0]
            label = " ".join(label.split()[:6])
            return {"label": label, "value": match.group(2), "unit": unit,
                    "source_sentence": sentence}
    return None


def offline_concept(text: str, *, recent_archetypes=None, recent_layouts=None,
                    recent_casts=None, recent_shots=None, **_kwargs) -> Any:
    """A deterministic Stage 1 for ``text`` that runs the REAL archetype and layout rotation.

    Args:
        text: The post.
        recent_archetypes: As ``analyze_content_for_image`` receives them.
        recent_layouts: As above.
        recent_casts: As above.
        recent_shots: As above.

    Returns:
        An ``ImageConcept``.
    """
    from cqc_lem.utilities.ai.image_concept import (
        ImageConcept,
        assign_art_style,
        assign_layout_and_cast,
        derive_kicker,
        select_archetype,
        source_sentences,
    )
    from cqc_lem.utilities.ai.image_graphics import validate_graphic_facts
    from cqc_lem.utilities.ai.post_treatment import _STANCE

    sentences = source_sentences(text) or [text.strip()]
    thesis = " ".join(sentences[0].split()[:20])
    stat = _offline_stat(text)
    graphic = validate_graphic_facts({"thesis_stat": stat} if stat else {}, text)
    hook_words = [w.strip(".,;:!?") for w in thesis.split()][:5]
    stance = bool(_STANCE.search(text.replace("’", "'")))
    concept = ImageConcept(
        thesis=thesis, audience="small-business owners", specific_entities=(),
        emotional_beat="quiet resolve", hook_phrase=" ".join(hook_words),
        treatment="editorial_concept", treatment_rationale="offline parse",
        visual_anchors=("a small-business owner",), valence="mixed" if stance else "positive",
        hook_shape="plain_claim", kicker=derive_kicker(text), graphic=graphic,
        human_moment=bool(_FIRST_PERSON.search(text)),
        chosen_idea=f"a small-business owner living out: {thesis}")
    concept = select_archetype(concept, "post_image", recent_archetypes)
    concept = assign_art_style(concept)
    return assign_layout_and_cast(concept, "post_image", recent_layouts, recent_casts,
                                  recent_shots)


def offline_brief(text: str, *, surface: str, ratio: str, concept: Any = None, **_kwargs) -> Any:
    """A template brief — the renderer is a placeholder, so no prompt is authored.

    Args:
        text: The post.
        surface: The surface.
        ratio: The caller's ratio.
        concept: The concept the caller passes.

    Returns:
        An ``ImageBrief`` shaped as the real author's would be.
    """
    from cqc_lem.utilities.ai.image_brief import ImageBrief
    from cqc_lem.utilities.ai.image_concept import fit_hook

    hook = fit_hook(concept.hook_phrase, concept.thesis) if concept and concept.hook_phrase \
        else None
    idea = getattr(concept, "chosen_idea", "") or text[:120]
    return ImageBrief(prompt=f"A candid documentary photograph: {idea}.",
                      ratio="1:1" if hook else ratio, surface=surface, style_preset=surface,
                      focal_concept=getattr(concept, "thesis", "") or text[:80],
                      concept=concept, treatment=getattr(concept, "treatment", None),
                      hook_text=hook, prompt_check="offline template")


def placeholder_render(prompt: str, *, ratio: str = "1:1", user_id=None, **_kwargs):
    """A labelled stand-in for an AI scene, tinted by the prompt's photo grade.

    Args:
        prompt: The render prompt (its "Photo grade:" clause picks the tint).
        ratio: ``1:1`` beside a type panel, else the post ratio.
        user_id: The author, for the output dir.

    Returns:
        ``(path, "offline")`` as ``_render_with_backend`` returns.
    """
    import secrets

    from PIL import Image, ImageDraw

    from cqc_lem.utilities.ai import image_gen
    from cqc_lem.utilities.ai.image_brief import PHOTO_GRADES
    from cqc_lem.utilities.ai.image_compose import load_font

    grade = next((g for g, text in PHOTO_GRADES.items() if text in prompt), "")
    size = (1024, 1024) if ratio == "1:1" else (1024, 1280)
    image = Image.new("RGB", size, _GRADE_TINTS.get(grade, (200, 200, 200)))
    draw = ImageDraw.Draw(image)
    for i in range(0, size[1], 64):
        draw.line((0, i, size[0], i + 200), fill=(255, 255, 255), width=2)
    font = load_font(54)
    draw.text((60, size[1] // 2 - 90), "AI SCENE", font=font, fill=(40, 40, 40))
    draw.text((60, size[1] // 2), (grade or "no grade").replace("_", " "), font=load_font(40),
              fill=(40, 40, 40))
    out_dir = os.path.join(image_gen.assets_dir, "images", "generated", str(user_id or "system"))
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"placeholder_{secrets.token_hex(6)}.png")
    image.save(path, "PNG")
    return path, "offline"


class _OfflineProfile:
    def __init__(self, full_name: str):
        self.full_name = full_name


def _offline_patches(stack: ExitStack, *, byline: str, card_share: float) -> None:
    from cqc_lem.utilities.ai import image_gen
    from cqc_lem.utilities.ai.image_gen import QualityVerdict

    unchecked = QualityVerdict(acceptable=True, checked=False, issues=["offline: not judged"])
    stack.enter_context(patch("cqc_lem.utilities.ai.image_concept.analyze_content_for_image",
                              side_effect=offline_concept))
    stack.enter_context(patch("cqc_lem.utilities.ai.image_brief.build_image_brief",
                              side_effect=offline_brief))
    stack.enter_context(patch("cqc_lem.utilities.avatar.guardrails.resolve_avatar_for_concept",
                              return_value=None))
    stack.enter_context(patch("cqc_lem.utilities.linkedin.helper.load_profile_for_user",
                              return_value=_OfflineProfile(byline)))
    stack.enter_context(patch("cqc_lem.utilities.brand_kit.brand_clause_for_user",
                              return_value=""))
    stack.enter_context(patch("cqc_lem.utilities.brand_kit.card_share_for_user",
                              return_value=card_share))
    stack.enter_context(patch("cqc_lem.utilities.ai.post_treatment.pick_quote",
                              side_effect=lambda c, t="", user_id=None: (c[0], "offline")))
    stack.enter_context(patch.object(image_gen, "_render_with_backend",
                                     side_effect=placeholder_render))
    stack.enter_context(patch.object(image_gen, "inspect_render_quality",
                                     return_value=unchecked))
    stack.enter_context(patch("cqc_lem.utilities.observability.track_image_gate_verdict"))


# --- The run and the sheet ----------------------------------------------------------------------

def _assets_patches(stack: ExitStack, assets: str) -> None:
    from cqc_lem.utilities import post_image
    from cqc_lem.utilities.ai import image_gen

    stack.enter_context(patch("cqc_lem.assets_dir", assets))
    stack.enter_context(patch.object(post_image, "assets_dir", assets))
    stack.enter_context(patch.object(image_gen, "assets_dir", assets))


def run(posts: list[str], *, user_id: int, assets: str, offline: bool, byline: str = "",
        card_share: float = 0.4) -> list[dict]:
    """Render every post in order; return one row per post with its receipt's rhythm.

    Args:
        posts: The post texts, oldest first.
        user_id: The author whose receipts accumulate.
        assets: The assets dir the renders and receipts go into.
        offline: The $0 mode.
        byline: The offline profile's name (the quote card's attribution).
        card_share: The offline brand kit's card share.

    Returns:
        ``{"index", "url", "path", "reason", "rhythm"}`` per post.
    """
    from cqc_lem.utilities.media_provenance import read_brief_receipt
    from cqc_lem.utilities.post_image import generate_image_for_post, post_image_abs_path

    os.makedirs(assets, exist_ok=True)
    rows = []
    with ExitStack() as stack:
        _assets_patches(stack, assets)
        if offline:
            _offline_patches(stack, byline=byline, card_share=card_share)
        for index, text in enumerate(posts, 1):
            url, reason = generate_image_for_post(user_id, text)
            receipt = read_brief_receipt(url) if url else None
            rows.append({"index": index, "url": url, "path": post_image_abs_path(url),
                         "reason": reason, "rhythm": (receipt or {}).get("rhythm") or {}})
    return rows


def label_for(row: dict) -> str:
    """One contact-sheet caption: ``#n treatment · panel · grade``.

    Args:
        row: A ``run`` row.

    Returns:
        The caption.
    """
    rhythm = row.get("rhythm") or {}
    parts = [rhythm.get("treatment") or "no image"]
    parts += [str(rhythm[k]).replace("_", " ") for k in ("panel", "grade", "layout")
              if rhythm.get(k)]
    return f"#{row['index']}  " + " · ".join(parts)


def contact_sheet(rows: list[dict], out_path: str) -> str:
    """Tile the images newest-first (feed order), each captioned with its rhythm.

    Args:
        rows: ``run`` rows, oldest first.
        out_path: Where to write the PNG.

    Returns:
        ``out_path``.
    """
    from PIL import Image, ImageDraw, ImageOps

    from cqc_lem.utilities.ai.image_compose import load_font

    feed = list(reversed(rows))
    rows_n = max(1, -(-len(feed) // COLUMNS))
    width = COLUMNS * THUMB_WIDTH + (COLUMNS + 1) * GUTTER
    height = rows_n * (THUMB_HEIGHT + LABEL_HEIGHT) + (rows_n + 1) * GUTTER
    sheet = Image.new("RGB", (width, height), (255, 255, 255))
    draw = ImageDraw.Draw(sheet)
    font = load_font(15)
    for n, row in enumerate(feed):
        x = GUTTER + (n % COLUMNS) * (THUMB_WIDTH + GUTTER)
        y = GUTTER + (n // COLUMNS) * (THUMB_HEIGHT + LABEL_HEIGHT + GUTTER)
        if row.get("path") and os.path.isfile(row["path"]):
            with Image.open(row["path"]) as img:
                thumb = ImageOps.contain(img.convert("RGB"), (THUMB_WIDTH, THUMB_HEIGHT))
            sheet.paste(thumb, (x + (THUMB_WIDTH - thumb.width) // 2, y))
        else:
            draw.rectangle((x, y, x + THUMB_WIDTH, y + THUMB_HEIGHT), outline=(180, 180, 180))
            draw.text((x + 12, y + 12), (row.get("reason") or "no image")[:40], font=font,
                      fill=(120, 0, 0))
        words, lines = label_for(row).split(" "), [""]
        for word in words:
            trial = f"{lines[-1]} {word}".strip()
            if draw.textlength(trial, font=font) <= THUMB_WIDTH or not lines[-1]:
                lines[-1] = trial
            else:
                lines.append(word)
        for k, line in enumerate(lines[:2]):
            draw.text((x, y + THUMB_HEIGHT + 6 + k * 22), line, font=font, fill=(30, 30, 30))
    sheet.save(out_path, "PNG")
    return out_path


def main(argv: Optional[list[str]] = None) -> int:
    """CLI entry point.

    Args:
        argv: Arguments; ``sys.argv[1:]`` by default.

    Returns:
        0 when every post produced an image, 1 otherwise.
    """
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("posts", help="JSON list of post texts")
    parser.add_argument("--user-id", type=int, required=True)
    parser.add_argument("--assets-dir", required=True)
    parser.add_argument("--out", help="contact sheet PNG (default: <assets-dir>/contact_sheet.png)")
    parser.add_argument("--offline", action="store_true", help="$0: code-drawn + placeholders")
    parser.add_argument("--byline", default="Christopher Queen",
                        help="offline: the profile name a quote card is attributed to")
    parser.add_argument("--card-share", type=float, default=0.4,
                        help="offline: the brand kit's typeset-card share")
    args = parser.parse_args(argv)
    if args.offline:
        # The client module needs a key to import; offline nothing reaches it.
        os.environ.setdefault("OPENAI_API_KEY", "offline-no-network")
    assets = os.path.abspath(args.assets_dir)
    rows = run(load_posts(args.posts), user_id=args.user_id, assets=assets,
               offline=args.offline, byline=args.byline, card_share=args.card_share)
    out = contact_sheet(rows, args.out or os.path.join(assets, "contact_sheet.png"))
    for row in rows:
        sys.stdout.write(f"{label_for(row)} | {row.get('reason') or row.get('url')}\n")
    sys.stdout.write(f"contact sheet: {out}\n")
    return 0 if all(row.get("url") for row in rows) else 1


if __name__ == "__main__":
    sys.exit(main())
