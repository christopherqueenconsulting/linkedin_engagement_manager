"""Newsletter cover images — the ONE place a cover is validated, stored, and generated (issue #893).

Two ways a cover gets onto an edition, and they are deliberately NOT symmetric:

- **Upload** — the author's own artwork. It is theirs, so it lands ``approved`` and is complete on
  its own: a user who never touches generation still has a working cover path.
- **AI generation** — opt-in (per newsletter via ``cover_image_auto``, or per edition from the
  review queue). A cover is a PUBLIC brand asset, so a generated one lands ``pending_review`` and
  the publish flow attaches nothing until the author approves it.

Both halves pass the same deterministic gate first (``inspect_cover_bytes``): decodable image, an
allowed format, under the byte cap, big enough to read as a LinkedIn article cover, and not
portrait. That gate is what stops a 40 KB portrait screenshot — or a generation that came back
truncated — from ever reaching the publish step; approval is the human half on top of it.

Paths are stored RELATIVE to ``assets_dir`` so they map straight onto ``/api/assets?file_name=``.
"""

import json
import os
import re
import secrets
import shutil
from dataclasses import dataclass
from typing import Any, Optional

from cqc_lem import assets_dir
from cqc_lem.utilities.logger import log_debug, log_info, log_warning

# How many prior covers steer the next brief's variety (issue #1992). Mirrors `enforce_variety`'s
# window for posts; reads what is already on disk rather than a new DB column.
_VARIETY_WINDOW = 5

# The staged judge's findings, copied from the render gate onto the cover's brief receipt.
_GATE_RECEIPT_KEYS = ("gate_rubric", "gate_failing", "gate_issues", "gate_blind_description",
                      "gate_pop", "archetype_rendered", "archetype_fallback_reason",
                      "graphic_facts", "cover_composed", "cover_layout", "typeset_layout")

COVER_SOURCE_UPLOAD = "upload"
COVER_SOURCE_AI = "ai"
COVER_STATUS_PENDING = "pending_review"
COVER_STATUS_APPROVED = "approved"

# Where covers live under assets_dir. Kept as posix-style so the stored value is also the
# /api/assets?file_name= value on every platform.
COVER_SUBDIR = "images/newsletter_covers"

MAX_COVER_BYTES = 8 * 1024 * 1024
# LinkedIn renders an article cover at 1.91:1; these are the floors below which the cover reads as
# a broken thumbnail rather than a brand asset.
MIN_COVER_WIDTH = 640
MIN_COVER_HEIGHT = 336
# Square is still usable (LinkedIn letterboxes it); portrait never is.
MIN_COVER_RATIO = 1.0
MAX_COVER_RATIO = 3.0
# Flux's closest supported ratio to LinkedIn's 1.91:1 cover.
COVER_IMAGE_RATIO = "16:9"

_EXT_BY_FORMAT = {"PNG": ".png", "JPEG": ".jpg", "WEBP": ".webp"}
ALLOWED_COVER_FORMATS = tuple(_EXT_BY_FORMAT)


class CoverRejected(Exception):
    """The image failed the deterministic cover gate — carries the user-facing reason."""


@dataclass
class CoverVerdict:
    """Outcome of the deterministic gate. ``reason`` is user-facing when ``ok`` is False."""
    ok: bool
    reason: Optional[str] = None
    width: Optional[int] = None
    height: Optional[int] = None
    image_format: Optional[str] = None

    @property
    def extension(self) -> str:
        """Suffix for the stored file, taken from the format Pillow actually DECODED.

        Never from whatever the uploader named the file — a mislabelled ``.png`` that is really a
        JPEG would otherwise be served under a type it is not. ``.png`` when the format is unknown,
        which only happens on a verdict that already failed the gate and is never written.
        """
        return _EXT_BY_FORMAT.get(self.image_format or "", ".png")


def inspect_cover_bytes(data: bytes) -> CoverVerdict:
    """Grade raw image bytes against the cover contract. Never raises."""
    if not data:
        return CoverVerdict(False, "No image data received")
    if len(data) > MAX_COVER_BYTES:
        return CoverVerdict(False, f"Image is larger than {MAX_COVER_BYTES // (1024 * 1024)} MB")
    try:
        import io

        from PIL import Image

        with Image.open(io.BytesIO(data)) as img:
            img_format = (img.format or "").upper()
            width, height = img.size
    except Exception as e:
        log_debug("Cover image could not be decoded", error=str(e), action_type="newsletter_cover")
        return CoverVerdict(False, "That file is not a readable image")

    if img_format not in _EXT_BY_FORMAT:
        return CoverVerdict(False, "Use a PNG, JPG, or WEBP image", width, height, img_format)
    if width < MIN_COVER_WIDTH or height < MIN_COVER_HEIGHT:
        return CoverVerdict(False,
                            f"Image is too small — at least {MIN_COVER_WIDTH}x{MIN_COVER_HEIGHT} px",
                            width, height, img_format)
    ratio = width / height if height else 0
    if ratio < MIN_COVER_RATIO or ratio > MAX_COVER_RATIO:
        return CoverVerdict(False, "Use a landscape image (roughly 1.91:1 works best)",
                            width, height, img_format)
    return CoverVerdict(True, None, width, height, img_format)


def inspect_cover_file(abs_path: str) -> CoverVerdict:
    """Grade an image already on disk (the generation path). Never raises."""
    try:
        with open(abs_path, "rb") as fh:
            return inspect_cover_bytes(fh.read())
    except OSError as e:
        log_debug("Cover image file unreadable", error=str(e), action_type="newsletter_cover")
        return CoverVerdict(False, "The generated image could not be read")


def cover_abs_path(relative_path: Optional[str]) -> Optional[str]:
    """Absolute path for a stored cover, or None when it escapes assets_dir / no longer exists.

    The value comes from our own DB, but resolving it through ``realpath`` and re-checking
    containment keeps a hand-edited row from handing the publish flow an arbitrary file to upload.
    """
    if not relative_path:
        return None
    root = os.path.realpath(assets_dir)
    candidate = os.path.realpath(os.path.join(root, relative_path))
    if candidate != root and not candidate.startswith(root + os.sep):
        log_warning("Newsletter cover path escapes the assets dir", action_type="newsletter_cover")
        return None
    return candidate if os.path.isfile(candidate) else None


def cover_public_url(relative_path: Optional[str]) -> Optional[str]:
    """The /api/assets URL the SPA renders the cover from, or None when there is no cover."""
    if not relative_path:
        return None
    from urllib.parse import quote

    from cqc_lem.utilities.env_constants import API_URL_FINAL
    return f"{API_URL_FINAL}/api/assets?file_name={quote(relative_path)}"


def _cover_dir(user_id: int) -> str:
    return os.path.join(assets_dir, *COVER_SUBDIR.split("/"), str(int(user_id)))


def save_cover_bytes(user_id: int, edition_id: int, data: bytes) -> str:
    """Gate then persist cover bytes; returns the assets-relative path.

    Raises ``CoverRejected`` with a user-facing reason when the gate fails — the caller turns that
    into a 400 rather than storing an unusable cover.
    """
    verdict = inspect_cover_bytes(data)
    if not verdict.ok:
        raise CoverRejected(verdict.reason or "Image rejected")
    directory = _cover_dir(user_id)
    os.makedirs(directory, exist_ok=True)
    # Random suffix: /api/assets is public, so a predictable name would let anyone enumerate
    # another user's unpublished cover.
    name = f"ed{int(edition_id)}_{secrets.token_hex(6)}{verdict.extension}"
    with open(os.path.join(directory, name), "wb") as fh:
        fh.write(data)
    return f"{COVER_SUBDIR}/{int(user_id)}/{name}"


def remove_cover_file(relative_path: Optional[str]) -> bool:
    """Best-effort delete of a stored cover file AND its `.brief.json` receipt (issue #2010).

    Never raises — the DB row is the source of truth. The receipt lives and dies with the cover it
    describes: `_recent_focal_concepts` only ever reads receipts for covers still on disk, so a
    superseded regeneration stops steering variety the moment its file is gone, instead of an
    orphan surviving in `images/newsletter_covers/<user_id>/` forever with nothing left to
    reference it.
    """
    from cqc_lem.utilities.media_provenance import BRIEF_RECEIPT_SUFFIX

    abs_path = cover_abs_path(relative_path)
    if not abs_path:
        return False
    try:
        os.remove(abs_path)
    except OSError as e:
        log_debug("Could not delete newsletter cover file", error=str(e),
                  action_type="newsletter_cover")
        return False
    receipt_path = os.path.splitext(abs_path)[0] + BRIEF_RECEIPT_SUFFIX
    if os.path.isfile(receipt_path):
        try:
            os.remove(receipt_path)
        except OSError as e:
            log_debug("Could not delete newsletter cover brief receipt", error=str(e),
                      action_type="newsletter_cover")
    return True


def _edition_text(title: Optional[str], subtitle: Optional[str], body: Optional[str]) -> str:
    return "\n\n".join(p for p in (title, subtitle, (body or "")[:1500]) if p)


def _edition_full_text(subtitle: Optional[str], body: Optional[str]) -> str:
    """The WHOLE edition below its title — Stage 1 reads the payoff, not just the hook."""
    return "\n\n".join(p for p in (subtitle, body) if p and p.strip())


def _recent_cover_receipts(user_id: int, limit: int = _VARIETY_WINDOW,
                           include_fallback: bool = True) -> list[dict]:
    """The last `limit` cover brief receipts, most-recent first (issue #1992).

    Reads them off the assets volume rather than a new DB column — the brief receipt
    (`media_provenance.write_brief_receipt`) is already the durable record. Never raises: a
    directory that doesn't exist yet, or a receipt that won't parse, just contributes nothing.

    ``include_fallback=False`` skips receipts the deterministic template wrote: they describe what
    the template assembled, not a choice the author made, so they are noise as variety steering.
    """
    directory = _cover_dir(user_id)
    try:
        names = [n for n in os.listdir(directory) if n.endswith(".brief.json")]
    except OSError:
        return []
    names.sort(key=lambda n: os.path.getmtime(os.path.join(directory, n)), reverse=True)
    receipts = []
    for name in names[:max(0, int(limit))]:
        try:
            with open(os.path.join(directory, name), "r", encoding="utf-8") as fh:
                payload = json.load(fh)
        except (OSError, ValueError):
            continue
        if not isinstance(payload, dict) or (not include_fallback and payload.get("fallback")):
            continue
        if str(payload.get("focal_concept") or "").strip():
            receipts.append(payload)
    return receipts


def _recent_focal_concepts(user_id: int, limit: int = _VARIETY_WINDOW,
                           include_fallback: bool = True) -> list[str]:
    """The last `limit` cover receipts' `focal_concept`, most-recent first (issue #1992)."""
    return [str(r["focal_concept"]).strip()
            for r in _recent_cover_receipts(user_id, limit, include_fallback)]


def _recent_cover_treatments(user_id: int, limit: int = 3) -> list[str]:
    """The treatments of the last `limit` covers, most-recent first (gauntlet round 1 of #2241).

    Fallback receipts count too — a fallback cover still went out looking like its treatment. Fed
    to Stage 1 so it prefers a different treatment, and to the deterministic graphic cap.
    """
    return [str(r.get("treatment")).strip() for r in _recent_cover_receipts(user_id, limit)
            if str(r.get("treatment") or "").strip()]


def _recent_concept_field(user_id: int, field: str, limit: int) -> list:
    """One Stage 1 concept field off the last `limit` cover receipts, most-recent first.

    Feeds the deterministic rotations (round 6 of #2241): layout, cast and hook shape are each
    picked least-recently-used against what the series already showed.
    """
    values = []
    for receipt in _recent_cover_receipts(user_id, limit):
        concept = receipt.get("concept")
        value = concept.get(field) if isinstance(concept, dict) else None
        if value:
            values.append(value)
    return values


def _cover_family(archetype: Optional[str]) -> str:
    """``drawn`` for a code-drawn cover, ``render`` for an AI render — what a reader tells apart."""
    from cqc_lem.utilities.ai.image_concept import CODE_DRAWN_ARCHETYPES

    return "drawn" if archetype in CODE_DRAWN_ARCHETYPES else "render"


def recent_cover_pairs(user_id: int, limit: int) -> list:
    """``(layout, family)`` of the last ``limit`` covers, most recent first (unknowns skipped)."""
    pairs = []
    for receipt in _recent_cover_receipts(user_id, limit):
        concept = receipt.get("concept") if isinstance(receipt.get("concept"), dict) else {}
        # The layout that SHIPPED: a recompose (round 9) may have moved it off the concept's.
        layout = receipt.get("cover_layout") or concept.get("layout")
        archetype = receipt.get("archetype_rendered") or concept.get("archetype")
        if layout and archetype:
            pairs.append((layout, _cover_family(archetype)))
    return pairs


def _families_for(concept: Any) -> set:
    """Every family the cover can SHIP as: its planned archetype's, plus its chain's render fallback.

    Round 9: cover_19 planned a code-drawn checklist (``drawn``), the judge refused it, and the
    chain fell to its people_scene (``render``) on the layout cover_17 had just used as a render.
    """
    families = {_cover_family(getattr(concept, "archetype", None))}
    chain = tuple(getattr(concept, "archetype_ranking", ()) or ())
    if chain:
        families.add(_cover_family(chain[-1]))
    return families


def _layout_penalty(layout: str, families: set, recent_pairs: list) -> int:
    """How badly ``layout`` repeats the recent covers: 0 is fresh.

    The previous cover's layout (any family) weighs 2 — two consecutive covers never share one;
    each (layout, family) the cover could ship as that one of the last ``COVER_LAYOUT_WINDOW``
    covers used weighs 1; a third split in a row weighs 1 (``split_run_exceeded``).
    """
    from cqc_lem.utilities.ai.image_concept import split_run_exceeded

    window = list(recent_pairs or [])[:COVER_LAYOUT_WINDOW]
    penalty = 2 if window and window[0][0] == layout else 0
    penalty += sum(1 for family in families if (layout, family) in window)
    if split_run_exceeded(layout, [p[0] for p in window]):
        penalty += 1
    return penalty


# Round 9: covers 17 and 19 shared a split three covers apart. The (layout, family) window is the
# last three covers, and the previous cover's layout never repeats whatever its family.
COVER_LAYOUT_WINDOW = 3


def fresh_cover_layout(concept: Any, recent_pairs: list,
                       recent_layouts: Optional[list] = None) -> Any:
    """The concept on the layout that repeats the recent covers least (showcase rounds 8 and 9).

    cover_17 and cover_20 were both AI renders on ``split_left`` — the same charcoal-left
    composition three covers apart; in round 9 cover_19 matched cover_17 because it was checked as
    the code-drawn cover it PLANNED, then shipped as the render its chain fell back to. Every family
    the cover can ship as is checked now (``_families_for``), against the last
    ``COVER_LAYOUT_WINDOW`` covers, and the previous cover's layout is never reused. Ties go to the
    layout used longest ago, then to the concept's own. A concept off the cover layouts is returned
    as is.

    #2316: with ``COVER_SPLIT_MAX`` splits already in the last ``COVER_SPLIT_WINDOW`` - 1 covers
    (``recent_layouts``, what SHIPPED — a typeset card counts as non-split), every split option
    weighs past any repeat, so the cover goes full-bleed.

    Args:
        concept: Stage 1's concept (frozen), or None.
        recent_pairs: ``recent_cover_pairs``, most recent first.
        recent_layouts: ``recent_cover_layouts``, most recent first; None skips the split budget.

    Returns:
        The concept, its layout possibly changed.
    """
    import dataclasses

    from cqc_lem.utilities.ai.image_concept import COVER_BREAK_LAYOUT, COVER_LAYOUTS

    layout = getattr(concept, "layout", None)
    options = (*COVER_LAYOUTS, COVER_BREAK_LAYOUT)
    if concept is None or layout not in options:
        return concept
    families = _families_for(concept)
    recent = [p[0] for p in list(recent_pairs or [])]
    over_budget = split_budget_spent(recent_layouts)

    def rank(option: str) -> tuple:
        last_seen = recent.index(option) if option in recent else len(recent) + 1
        budget = 10 if over_budget and is_split_layout(option) else 0
        return (budget + _layout_penalty(option, families, recent_pairs), -last_seen,
                option != layout)

    pick = min(options, key=rank)
    if pick == layout:
        return concept
    try:
        return dataclasses.replace(concept, layout=pick)
    except (TypeError, ValueError):
        return concept


def _recent_cover_archetypes(user_id: int, limit: int) -> list[str]:
    """The archetypes the last `limit` covers actually SHIPPED as, most-recent first.

    ``archetype_rendered`` wins over the concept's pick: a stat card that fell back to an
    editorial render went out as the editorial render, and that is what the series showed.
    """
    values = []
    for receipt in _recent_cover_receipts(user_id, limit):
        concept = receipt.get("concept") if isinstance(receipt.get("concept"), dict) else {}
        value = receipt.get("archetype_rendered") or concept.get("archetype")
        if value:
            values.append(str(value))
    return values


def _recent_cover_signals(user_id: int, limit: int = _VARIETY_WINDOW) -> list[str]:
    """What the last few covers looked like — ``"<treatment>: <focal concept>"`` — for the brief.

    A SOFT steer to the author only (issue #2241): it asks for a visibly different setting and
    composition, and never changes the treatment Stage 1 chose or bans the edition's own
    entities. The old object/family avoid list could only push the author from one metaphor to
    another, and with every concrete subject banned around it, it pushed toward metaphor.
    """
    signals = []
    for receipt in _recent_cover_receipts(user_id, limit, include_fallback=False):
        treatment = str(receipt.get("treatment") or "").strip()
        focal = " ".join(str(receipt["focal_concept"]).split())[:120]
        signals.append(f"{treatment}: {focal}" if treatment else focal)
    return signals


def classify_avatar_relevance(title: Optional[str], subtitle: Optional[str],
                              body: Optional[str]) -> bool:
    """Is this edition a piece where the author's likeness belongs on the cover?

    True only for first-person / personal-story / author-announcement editions — a market-analysis
    edition with the author's face on it reads as vanity, not relevance. Fails CLOSED: any error
    means "no avatar", matching the guardrails' posture.
    """
    import json as _json

    from cqc_lem.utilities.ai.client import client

    excerpt = _edition_text(title, subtitle, body)[:1200]
    if not excerpt:
        return False
    try:
        response = client.chat.completions.create(
            model="lem-simple",
            messages=[{"role": "user", "content": (
                "Is this newsletter edition a first-person / personal-story / "
                "author-announcement piece where the AUTHOR'S OWN likeness belongs on the cover "
                "image? Answer with ONLY a JSON object {\"avatar_relevant\": true|false}.\n\n"
                f"<edition>{excerpt}</edition>")}],
            response_format={"type": "json_object"},
            temperature=0,
            max_tokens=30,
        )
        return bool(_json.loads(response.choices[0].message.content).get("avatar_relevant"))
    except Exception as e:
        log_debug("Avatar relevance classifier unavailable — defaulting to no avatar",
                  error=str(e), action_type="newsletter_cover")
        return False


def _avatar_for_explicit_choice(user_id: int) -> Optional[dict]:
    """The avatar for a per-edition 'With me' click.

    The explicit choice beats the per-surface opt-in (same precedence a post's compose-time toggle
    has), but NEVER beats ``avatar_disabled`` or the preview/approval gate. Fails closed.
    """
    try:
        from cqc_lem.utilities.avatar.guardrails import avatar_is_usable
        from cqc_lem.utilities.db import get_active_avatar, get_avatar_preferences

        if get_avatar_preferences(user_id).get("avatar_disabled"):
            return None
        avatar = get_active_avatar(user_id)
        return avatar if avatar_is_usable(avatar) else None
    except Exception as e:
        log_warning("Avatar check failed for explicit cover choice — rendering without", exc=e,
                    user_id=user_id, action_type="newsletter_cover")
        return None


def _resolve_cover_avatar(user_id: int, use_avatar: Optional[bool], title: Optional[str],
                          subtitle: Optional[str], body: Optional[str],
                          concept: Any = None, names: Optional[list] = None) -> Optional[dict]:
    """Which avatar (if any) this cover renders with.

    ``use_avatar`` is the per-edition override: False never renders it, True skips only the
    per-surface opt-in and the relevance classifier. ``None`` (Auto) with no Stage 1 concept needs
    BOTH the guardrails (``avatar_use_newsletter`` opt-in + approval) AND the classifier to agree.

    #2316 (the owner's rule, which replaced round 8's concept fit rule on covers): with a concept,
    Auto renders the avatar exactly when the cover is a people scene that REFERENCES the author
    (``guardrails.author_reference`` — the concept speaks of them, the edition names them, or it
    is first-person about them) and the guardrails allow it. A people scene about someone else
    never carries the author's likeness.
    """
    if use_avatar is False:
        return None
    if use_avatar is True:
        return _avatar_for_explicit_choice(user_id)

    from cqc_lem.utilities.avatar.guardrails import (
        AVATAR_SURFACE_NEWSLETTER,
        author_reference,
        resolve_avatar_for,
    )

    if concept is not None:
        text = "\n\n".join(p for p in (title, subtitle, body) if p)
        if not _is_people_concept(concept) or not author_reference(concept, text, names):
            return None
        return resolve_avatar_for(user_id, surface=AVATAR_SURFACE_NEWSLETTER)
    avatar = resolve_avatar_for(user_id, surface=AVATAR_SURFACE_NEWSLETTER)
    if not avatar:
        return None
    return avatar if classify_avatar_relevance(title, subtitle, body) else None


# #2316 — the owner's people-on-a-cover rule, as the receipt records it.
PEOPLE_POLICY_AVATAR = "avatar"                      # about the author: their avatar renders
PEOPLE_POLICY_STOCK = "third_party"                  # about someone else: an article-anchored scene
PEOPLE_POLICY_NO_AVATAR = "non_people:no_avatar"     # about the author, no usable avatar
PEOPLE_POLICY_OPTED_OUT = "non_people:opted_out"     # about the author, the author said no
PEOPLE_POLICY_NONE = ""                              # not a people scene at all


def _is_people_concept(concept: Any) -> bool:
    from cqc_lem.utilities.ai.image_concept import ARCHETYPE_PEOPLE, TREATMENT_PEOPLE

    return concept is not None and (
        getattr(concept, "treatment", None) == TREATMENT_PEOPLE
        or getattr(concept, "archetype", None) == ARCHETYPE_PEOPLE
        or ARCHETYPE_PEOPLE in tuple(getattr(concept, "archetype_ranking", ()) or ()))


def _avatar_opted_out(user_id: int, use_avatar: Optional[bool]) -> bool:
    """The author said no: this edition's "Without me", or ``avatar_disabled``. Fails CLOSED."""
    if use_avatar is False:
        return True
    try:
        from cqc_lem.utilities.db import get_avatar_preferences

        return bool(get_avatar_preferences(user_id).get("avatar_disabled"))
    except Exception as e:
        log_debug("Avatar preferences unreadable — treating the author as opted out",
                  error=str(e), user_id=user_id, action_type="newsletter_cover")
        return True


def resolve_people_cover(user_id: int, use_avatar: Optional[bool], concept: Any,
                         title: Optional[str], subtitle: Optional[str], body: Optional[str],
                         profile: Any = None) -> "tuple[Any, Optional[dict], str]":
    """Who a people_scene cover may show — the owner's rule, decided BEFORE the brief (#2316).

    "Stock people when it makes sense, but if it references the user (or me, myself, or the user's
    name) then it needs to use their avatar." So a people_scene cover is:

    - about the AUTHOR (``guardrails.author_reference``: the concept speaks of them, the edition
      names them, or it is first-person about them) — their avatar (``_resolve_cover_avatar``:
      ``avatar_use_newsletter`` + approval; a per-edition "With me" skips only the opt-in). No
      usable avatar, or the author opted out (this edition's "Without me", ``avatar_disabled``),
      and the cover moves to a NON-people treatment (``image_concept.without_people``) — never a
      stock stand-in for the author;
    - about someone else — no avatar; a stock scene must still show the article's own entity
      (round 9's ``article_scene_failure``).

    Args:
        user_id: The author.
        use_avatar: The per-edition choice (True "With me", False "Without me", None Auto).
        concept: Stage 1's concept, or None.
        title: The edition title.
        subtitle: The edition subtitle.
        body: The edition body.
        profile: The author's profile, for their name.

    Returns:
        ``(concept, avatar, policy)`` — the concept possibly moved off people, the avatar or None,
        and one of the ``PEOPLE_POLICY_*`` values.
    """
    from cqc_lem.utilities.avatar.guardrails import author_names, author_reference

    names = author_names(profile)
    avatar = _resolve_cover_avatar(user_id, use_avatar, title, subtitle, body, concept=concept,
                                   names=names)
    if concept is None or not _is_people_concept(concept):
        return concept, avatar, PEOPLE_POLICY_NONE
    if avatar:
        return concept, avatar, PEOPLE_POLICY_AVATAR
    about_author = author_reference(concept, "\n\n".join(p for p in (title, subtitle, body) if p),
                                    names)
    if not about_author:
        return concept, None, PEOPLE_POLICY_STOCK
    policy = (PEOPLE_POLICY_OPTED_OUT if _avatar_opted_out(user_id, use_avatar)
              else PEOPLE_POLICY_NO_AVATAR)
    from cqc_lem.utilities.ai.image_concept import (
        ARCHETYPE_WINDOW,
        ROTATION_WINDOW,
        assign_art_style,
        without_people,
    )

    moved = without_people(concept, "newsletter", _recent_cover_archetypes(user_id,
                                                                           ARCHETYPE_WINDOW))
    moved = assign_art_style(moved, _recent_concept_field(user_id, "art_style", ROTATION_WINDOW))
    log_info("Cover is about the author and no avatar may render — no person on it",
             user_id=user_id, action_type="newsletter_cover", reason=about_author, policy=policy)
    return moved, None, policy


def cover_headline(brief_hook: Optional[str], concept: Any, title: Optional[str],
                   subtitle: Optional[str]) -> Optional[str]:
    """The headline every cover carries: Stage 1's gated hook, else the edition's own title.

    Showcase round 4: a cover is never ``photo_only``. The title is the author's headline, so it
    is the honest fallback; a long one is cut to a complete clause, never mid-thought
    (``post_image.last_resort_hook``). The subtitle stands in only for an untitled edition.

    Args:
        brief_hook: The brief's hook (Stage 1's, after the final hook gate), or None.
        concept: Stage 1's concept, or None.
        title: The edition title.
        subtitle: The edition subtitle.

    Returns:
        The headline, or None for an edition with no words at all.
    """
    if (brief_hook or "").strip():
        return brief_hook
    from cqc_lem.utilities.ai.fact_consistency import prints_any
    from cqc_lem.utilities.post_image import last_resort_hook

    # Showcase round 7: a figure the edition body never sources (cover_18's 53.7%) is never the
    # fallback headline either — the first numberless candidate wins, else the title without it.
    unsourced = tuple(getattr(concept, "unsourced_figures", ()) or ())
    first = None
    for text in (title, subtitle):
        hook = last_resort_hook(None, text or "")
        if hook and not prints_any(hook, unsourced):
            return hook
        first = first or hook
    if unsourced and concept is not None:
        from cqc_lem.utilities.ai.image_concept import clause_hook

        clause = clause_hook(" ".join(w for w in (getattr(concept, "thesis", "") or "").split()
                                      if not any(ch.isdigit() for ch in w)))
        if clause:
            return clause
    if first:
        return _without_figures(first, unsourced)
    return last_resort_hook(concept, "") if concept is not None else None


def cited_byline(byline: Optional[str], headline: Optional[str], concept: Any) -> Optional[str]:
    """The cover's byline, carrying the edition's own source when the headline prints a figure.

    Showcase round 7: cover_18 printed 53.7% with no source. A stat card draws its source line
    itself; any other cover whose headline carries a figure the body cites by name (the concept's
    ``graphic.source_line``, set by ``image_concept.apply_figure_provenance``) prints it beside
    the byline.

    Args:
        byline: The byline the cover would draw.
        headline: The headline it sets.
        concept: The brief's concept, or None.

    Returns:
        The byline, possibly with " · Source: …" appended.
    """
    source_line = str(((getattr(concept, "graphic", None) or {}).get("source_line")) or "").strip()
    if (not source_line or not any(ch.isdigit() for ch in headline or "")
            or getattr(concept, "archetype", "") == "stat_card"):
        return byline
    return f"{byline} · {source_line}" if byline else source_line


def _without_figures(text: str, figures: tuple) -> str:
    """``text`` with each figure (and an "of" that hung on it) removed, re-capitalised."""
    out = text
    for figure in figures:
        out = re.sub(re.escape(figure) + r"\s*(?:of\s+)?", "", out, flags=re.IGNORECASE)
    out = " ".join(out.split()).strip(" -–—:,")
    return out[:1].upper() + out[1:] if out else text


def ensure_composed_cover(path: str, hook: Optional[str], render_info: dict, *, concept: Any,
                          brand: str, byline: Optional[str],
                          user_id: Optional[int] = None) -> Optional[str]:
    """``path`` if it already carries the type panel; else the panel composed onto it now.

    The gated renderer composites the headline onto every candidate and records the raw render as
    ``raw_render_path``; a code-drawn graphic is drawn WITH its panel. Anything else — a headline
    the compositor refused, a render that came back raw — is composed here, and if even that fails
    the cover becomes a code-drawn typographic card of the same headline. A cover never ships as a
    bare photograph.

    Args:
        path: The render the gate returned.
        hook: The cover headline.
        render_info: The gate's out-param; ``cover_composed`` is recorded on it.
        concept: Stage 1's concept, for the layout and kicker.
        brand: The brand clause.
        byline: The cover byline.
        user_id: For log context.

    Returns:
        The composed cover's path, or None when there is no headline to compose.
    """
    from cqc_lem.utilities.ai.image_compose import brand_style, compose_headline
    from cqc_lem.utilities.ai.image_concept import CODE_DRAWN_ARCHETYPES
    from cqc_lem.utilities.ai.image_gen import _kicker_for
    from cqc_lem.utilities.ai.image_graphics import QUOTE_CARD, TYPESET_CARD, render_typeset_card

    drawn = render_info.get("archetype_rendered") in (*CODE_DRAWN_ARCHETYPES, QUOTE_CARD,
                                                      TYPESET_CARD)
    if render_info.get("raw_render_path") or drawn:
        return path
    if not hook:
        log_warning("A newsletter cover has no headline to compose — not shipping it bare",
                    user_id=user_id, action_type="newsletter_cover")
        return None
    from cqc_lem.utilities.ai.image_concept import derive_kicker

    style = brand_style(brand)
    # Every cover carries a kicker: Stage 1's, else one derived from the headline's own words.
    kicker = (_kicker_for(concept) if concept is not None else "") or derive_kicker(hook)
    try:
        report: dict = {}
        composed = compose_headline(path, hook, layout=getattr(concept, "layout", None),
                                    brand=style, surface="newsletter", kicker=kicker or None,
                                    signature=byline, report=report)
        render_info["cover_composed"] = "late_compose"
        if report.get("layout"):
            render_info["cover_layout"] = report["layout"]
        return composed
    except Exception as e:
        log_info("Cover headline would not compose onto the render — drawing a typeset cover",
                 user_id=user_id, action_type="newsletter_cover", error=str(e))
    try:
        card = render_typeset_card(hook, surface="newsletter", kicker=kicker,
                                   signature=byline or "", brand=style)
        render_info["cover_composed"] = "typeset_card"
        return card.path
    except Exception as e:
        log_warning("A newsletter cover could not be composed — not shipping it bare", exc=e,
                    user_id=user_id, action_type="newsletter_cover")
        return None


# Showcase round 8: cover_18 shipped SQUARE (960x960 in the export, 1024x1024 on disk). The avatar
# LoRA renders 1:1, and a full-bleed layout composes onto the render itself, so nothing restored the
# 16:9 a cover slot needs. Every cover leaves here 16:9.
COVER_SIZE = (1920, 1080)
_COVER_RATIO_TOLERANCE = 0.02


def is_cover_ratio(path: Optional[str]) -> bool:
    """Is the image at ``path`` 16:9 (within ``_COVER_RATIO_TOLERANCE``)? False when unreadable."""
    try:
        from PIL import Image

        with Image.open(path) as img:
            width, height = img.size
    except Exception:
        return False
    return height > 0 and abs(width / height - COVER_SIZE[0] / COVER_SIZE[1]) <= _COVER_RATIO_TOLERANCE


def _to_cover_canvas(path: str) -> Optional[str]:
    """``path`` scaled to cover 1920x1080 and centre-cropped to it, as a new PNG. Never raises."""
    try:
        from PIL import Image

        from cqc_lem.utilities.ai.image_compose import Box, cover_fit

        with Image.open(path) as img:
            fitted = cover_fit(img.convert("RGB"), Box(0, 0, *COVER_SIZE))
        out = f"{os.path.splitext(path)[0]}_16x9.png"
        fitted.save(out, "PNG")
        return out
    except Exception as e:
        log_debug("Cover could not be fitted to 16:9", error=str(e), action_type="newsletter_cover")
        return None


# A render this wide or wider loses little to a 16:9 crop (gpt-image's 3:2 "16:9" loses 16% of its
# height); a square one (the avatar LoRA) would lose a face, so it is recomposed on a split.
_FULL_BLEED_MIN_ASPECT = 1.4


def _landscape(path: str) -> bool:
    """Is the render at ``path`` landscape enough to set full-bleed on a 16:9 crop?"""
    try:
        from PIL import Image

        with Image.open(path) as img:
            width, height = img.size
    except Exception:
        return False
    return height > 0 and width / height >= _FULL_BLEED_MIN_ASPECT


def solid_cover_layout(layout: Optional[str]) -> str:
    """``layout`` when it sets the headline on a solid split panel, else the default split.

    Args:
        layout: The concept's cover layout.

    Returns:
        A split layout (``image_compose.SPLIT_LAYOUTS``).
    """
    from cqc_lem.utilities.ai.image_compose import OVERLAY_FALLBACK_LAYOUT, SPLIT_LAYOUTS

    return layout if layout in SPLIT_LAYOUTS else OVERLAY_FALLBACK_LAYOUT


def ensure_cover_ratio(path: str, hook: Optional[str], render_info: dict, *, concept: Any,
                       brand: str, byline: Optional[str],
                       user_id: Optional[int] = None) -> Optional[str]:
    """``path`` when it is 16:9; else the cover re-composed on a 16:9 canvas. Never raises.

    The RAW render (``render_info["raw_render_path"]``) is composed again on a 1920x1080 canvas,
    on a SOLID split panel (``solid_cover_layout``, round 9), so no headline is cropped and none
    sits on a photograph; with no raw render the composed cover itself is fitted. None only when
    neither could be made.

    Args:
        path: The composed cover.
        hook: Its headline.
        render_info: The gate's out-param.
        concept: Stage 1's concept, for the layout and kicker.
        brand: The brand clause.
        byline: The cover byline.
        user_id: For log context.

    Returns:
        A 16:9 cover's path, or None.
    """
    if is_cover_ratio(path):
        return path
    raw = render_info.get("raw_render_path")
    if raw and os.path.isfile(raw) and hook:
        try:
            from cqc_lem.utilities.ai.image_compose import brand_style, compose_headline
            from cqc_lem.utilities.ai.image_concept import derive_kicker
            from cqc_lem.utilities.ai.image_gen import _kicker_for

            kicker = (_kicker_for(concept) if concept is not None else "") or derive_kicker(hook)
            planned = getattr(concept, "layout", None)
            if planned and not is_split_layout(planned) and _landscape(raw):
                # #2316: a full-bleed cover renders at gpt-image's 3:2 "16:9" size; it is fitted to
                # 16:9 and set full-bleed again. An overlay below the contrast bar still moves to
                # a solid split inside the compositor (round 9), and says so in ``report``.
                canvas = _to_cover_canvas(raw)
                report: dict = {}
                composed = compose_headline(canvas, hook, layout=planned,
                                            brand=brand_style(brand), surface="newsletter",
                                            kicker=kicker or None, signature=byline,
                                            report=report) if canvas else None
                if composed and is_cover_ratio(composed):
                    render_info["cover_composed"] = "ratio_fit_full_bleed"
                    render_info["cover_layout"] = report.get("layout") or planned
                    return composed
            # Round 9: cover_18's recompose kept its full-bleed layout and set gold type on a
            # translucent strip over a light render, the byline lost in the photo. A recomposed
            # cover is otherwise set on a SOLID split panel, the raw render cropped into its scene.
            layout = solid_cover_layout(planned)
            composed = compose_headline(raw, hook, layout=layout, brand=brand_style(brand),
                                        surface="newsletter", kicker=kicker or None,
                                        signature=byline, canvas=COVER_SIZE)
            if composed and is_cover_ratio(composed):
                log_info("Cover re-composed at 16:9 from a non-16:9 render", user_id=user_id,
                         action_type="newsletter_cover")
                render_info["cover_composed"] = "ratio_recompose"
                render_info["cover_layout"] = layout
                return composed
        except Exception as e:
            log_debug("Cover could not be re-composed at 16:9 — fitting the composite",
                      error=str(e), user_id=user_id, action_type="newsletter_cover")
    fitted = _to_cover_canvas(path)
    if fitted and is_cover_ratio(fitted):
        render_info["cover_composed"] = "ratio_fit"
        return fitted
    log_warning("A newsletter cover could not be made 16:9 — not shipping it", user_id=user_id,
                action_type="newsletter_cover")
    return None


# #2316: all five covers of the round-10 batch were left/right splits. No more than COVER_SPLIT_MAX
# splits in any COVER_SPLIT_WINDOW covers; the rest are full-bleed renders or full-canvas typeset
# cards (the number-led card is the stat hero).
COVER_SPLIT_WINDOW = 5
COVER_SPLIT_MAX = 2
COVER_LAYOUT_TYPESET = "typeset_card"


def is_split_layout(layout: Optional[str]) -> bool:
    """Is ``layout`` a type-panel-beside-scene split (every code-drawn graphic is one)?"""
    from cqc_lem.utilities.ai.image_compose import SPLIT_LAYOUTS

    return layout in SPLIT_LAYOUTS


def recent_cover_layouts(user_id: int, limit: int = COVER_SPLIT_WINDOW - 1) -> list:
    """The layouts the last ``limit`` covers SHIPPED on, most recent first (unknowns skipped)."""
    from cqc_lem.utilities.ai.image_graphics import TYPESET_CARD

    out = []
    for receipt in _recent_cover_receipts(user_id, limit):
        concept = receipt.get("concept") if isinstance(receipt.get("concept"), dict) else {}
        layout = receipt.get("cover_layout") or (
            COVER_LAYOUT_TYPESET if receipt.get("archetype_rendered") == TYPESET_CARD
            else concept.get("layout"))
        if layout:
            out.append(str(layout))
    return out


def split_budget_spent(recent_layouts: Optional[list]) -> bool:
    """Would one more split break "no more than ``COVER_SPLIT_MAX`` in any ``COVER_SPLIT_WINDOW``"?"""
    window = list(recent_layouts or [])[:COVER_SPLIT_WINDOW - 1]
    return sum(1 for layout in window if is_split_layout(layout)) >= COVER_SPLIT_MAX


def shipped_cover_layout(render_info: dict, concept: Any) -> Optional[str]:
    """The layout the cover actually shipped on: the compositor's report, else the concept's."""
    from cqc_lem.utilities.ai.image_concept import CODE_DRAWN_ARCHETYPES
    from cqc_lem.utilities.ai.image_graphics import QUOTE_CARD, TYPESET_CARD

    if render_info.get("archetype_rendered") == TYPESET_CARD:
        return COVER_LAYOUT_TYPESET
    if render_info.get("cover_layout"):
        return render_info["cover_layout"]
    layout = getattr(concept, "layout", None)
    if render_info.get("archetype_rendered") in (*CODE_DRAWN_ARCHETYPES, QUOTE_CARD):
        # A code-drawn graphic is always set beside its panel (``image_graphics.render_graphic``).
        return layout if is_split_layout(layout) else "split_left"
    return layout


# What the judge says when a people render is stock: round 9's capped issue, or its own words.
_GENERIC_ISSUE_RE = re.compile(r"\b(?:generic|stock|unrelated|not\s+related|could\s+be\s+any|"
                               r"nothing\s+from\s+the\s+article|off[-\s]topic)\b", re.IGNORECASE)


def people_render_unrelated(render_info: dict, concept: Any) -> bool:
    """Did a STOCK people render come back judged generic or unrelated after its repair (#2316)?

    cover_17 (a worried warehouse worker) passed the rubric while the judge's own issues said
    "scene is generic, not AI-specific". A people_scene that rendered without the avatar ships
    only when the judge neither failed its specificity/cliché floor nor called it generic.

    Args:
        render_info: The gate's out-param.
        concept: The brief's concept.

    Returns:
        True when the render must not ship as a people cover.
    """
    from cqc_lem.utilities.ai.image_concept import ARCHETYPE_PEOPLE

    shipped = render_info.get("archetype_rendered") or getattr(concept, "archetype", None)
    if shipped != ARCHETYPE_PEOPLE and not (shipped is None and _is_people_concept(concept)):
        return False
    if render_info.get("used_avatar"):
        return False
    failing = set(render_info.get("gate_failing") or [])
    if render_info.get("gate_verdict") == "rejected" and failing & {"specificity", "no_cliche"}:
        return True
    return any(_GENERIC_ISSUE_RE.search(str(i)) for i in render_info.get("gate_issues") or [])


def typeset_cover(hook: Optional[str], concept: Any, brand: str, byline: Optional[str],
                  render_info: dict, reason: str, user_id: Optional[int] = None) -> Optional[str]:
    """The cover as a full-canvas typeset card — non-people, non-split, $0 (#2316). Never raises.

    A headline that leads with a figure takes the number-led card (the stat hero); any other the
    poster. ``render_info`` records the card as what shipped.

    Args:
        hook: The cover headline.
        concept: The brief's concept, for the kicker.
        brand: The brand clause.
        byline: The cover byline.
        render_info: The gate's out-param, updated in place.
        reason: Why the cover was re-set (``cover_composed``).
        user_id: For the output directory and log context.

    Returns:
        The card's path, or None when the headline cannot be set.
    """
    if not (hook or "").strip():
        return None
    try:
        from cqc_lem.utilities.ai.image_compose import brand_style
        from cqc_lem.utilities.ai.image_concept import derive_kicker
        from cqc_lem.utilities.ai.image_gen import _kicker_for
        from cqc_lem.utilities.ai.image_graphics import (
            CARD_NUMBER_LED,
            CARD_POSTER,
            TYPESET_CARD,
            render_typeset_card,
            typeset_layouts_for,
        )

        layout = CARD_NUMBER_LED if CARD_NUMBER_LED in typeset_layouts_for(hook) else CARD_POSTER
        kicker = (_kicker_for(concept) if concept is not None else "") or derive_kicker(hook)
        out_dir = os.path.join(assets_dir, "images", "generated", str(user_id or "system"))
        os.makedirs(out_dir, exist_ok=True)
        card = render_typeset_card(hook, surface="newsletter", kicker=kicker,
                                   signature=byline or "", brand=brand_style(brand),
                                   card_layout=layout,
                                   out_path=os.path.join(out_dir,
                                                         f"img_{secrets.token_hex(8)}.png"))
    except Exception as e:
        log_warning("Typeset cover could not be drawn", exc=e, user_id=user_id,
                    action_type="newsletter_cover", reason=reason)
        return None
    render_info.update({"archetype_rendered": TYPESET_CARD, "cover_composed": reason,
                        "cover_layout": COVER_LAYOUT_TYPESET, "typeset_layout": layout,
                        "gate_verdict": "code_drawn"})
    render_info.pop("raw_render_path", None)
    log_info("Newsletter cover re-set as a typeset card", user_id=user_id,
             action_type="newsletter_cover", reason=reason, card_layout=layout)
    return card.path


def generate_cover_for_edition(user_id: int, edition_id: int, title: Optional[str],
                               subtitle: Optional[str], body: Optional[str],
                               profile=None,
                               use_avatar: Optional[bool] = None,
                               guidance: Optional[str] = None,
                               edition_format: Optional[str] = None,
                               hook_style: Optional[str] = None,
                               signature: Optional[str] = None,
                               ) -> "tuple[Optional[str], Optional[str]]":
    """Generate a cover for one edition. Returns ``(relative_path, None)`` or ``(None, reason)``.

    Never raises: a failed cover must not take an edition's draft down with it. The generated file
    is COPIED into the user's cover dir so removal/ownership stay scoped to the edition, and it
    passes the same deterministic gate an upload does before it is ever stored on the row.
    Whatever renders here still lands ``pending_review`` — the author stays the publish gate.

    ``guidance`` (issue #1890) is the author's free-text direction for THIS image — distinct from
    the edition's article-text "Added Guidance" — and is threaded straight into the brief's
    ``extra_direction`` rather than a new per-surface prompt helper (per CLAUDE.md's image stack).

    ``edition_format``/``hook_style`` (issue #1992) distinguish a listicle cover from a
    personal-story one; folded into the brief's ``content_shape``.

    The staged engine (issue #2241): Stage 1 reads title + subtitle + the FULL body and decides
    the treatment and the entities; the brief is authored and checked from that; and the render
    is graded by the blind judge against the same concept — on the avatar path too, which used to
    skip every newsletter-specific gate. A real brief AND the deterministic fallback both get a
    ``.brief.json`` receipt beside the stored cover (``media_provenance.write_brief_receipt``),
    carrying the concept, treatment, hook and the judge's rubric, so every generation — and every
    rejection the author then reviews — is auditable.
    """
    from cqc_lem.utilities.ai.image_brief import build_image_brief
    from cqc_lem.utilities.ai.image_concept import analyze_content_for_image
    from cqc_lem.utilities.ai.image_gen import render_avatar_image_gated, render_image_gated
    from cqc_lem.utilities.brand_kit import brand_clause_for_user
    from cqc_lem.utilities.media_provenance import write_brief_receipt
    from cqc_lem.utilities.post_image import story_facts_for

    shape = (f"format={edition_format or 'unspecified'}, hook_style={hook_style or 'unspecified'}"
            if edition_format or hook_style else None)

    brand = brand_clause_for_user(user_id)
    try:
        from cqc_lem.utilities.ai.image_concept import (
            ARCHETYPE_WINDOW,
            GRAPHIC_WINDOW,
            ROTATION_WINDOW,
        )
        concept = analyze_content_for_image(
            _edition_full_text(subtitle, body), title=title, surface="newsletter",
            user_id=user_id,
            recent_treatments=_recent_cover_treatments(user_id, GRAPHIC_WINDOW),
            recent_layouts=_recent_concept_field(user_id, "layout", ROTATION_WINDOW),
            recent_casts=_recent_concept_field(user_id, "cast", ROTATION_WINDOW),
            recent_hook_shapes=_recent_concept_field(user_id, "hook_shape", ROTATION_WINDOW),
            recent_shots=_recent_concept_field(user_id, "shot", ROTATION_WINDOW),
            recent_archetypes=_recent_cover_archetypes(user_id, ARCHETYPE_WINDOW),
            recent_art_styles=_recent_concept_field(user_id, "art_style", ROTATION_WINDOW),
            facts=story_facts_for(user_id))
        # #2316: who a people cover may show is decided first — it can move the concept off
        # people, which changes the families the layout rotation weighs.
        concept, avatar, people_policy = resolve_people_cover(user_id, use_avatar, concept, title,
                                                              subtitle, body, profile)
        recent_layouts = recent_cover_layouts(user_id)
        concept = fresh_cover_layout(concept, recent_cover_pairs(user_id, ROTATION_WINDOW),
                                     recent_layouts)
        # The avatar is resolved BEFORE the brief is authored: its declared subject clause is what
        # stops the prompt LLM inventing a different person for the LoRA to contradict (#744).
        brief = build_image_brief("\n\n".join(p for p in (title, subtitle, body) if p),
                                  surface="newsletter", ratio=COVER_IMAGE_RATIO, profile=profile,
                                  avatar=avatar, extra_direction=guidance, content_shape=shape,
                                  avoid_terms=_recent_cover_signals(user_id), concept=concept,
                                  brand_kit=brand)
    except Exception as e:
        log_warning("Newsletter cover prompt failed", exc=e, user_id=user_id,
                    action_type="newsletter_cover")
        return None, "Could not write a cover prompt"

    # The cover's byline (round 7): the newsletter's title when the caller has it, else the
    # author's name from the profile already in hand — no extra DB read.
    byline = signature or getattr(profile, "full_name", None) or None
    # Showcase round 4: every cover is COMPOSED — kicker, headline, byline. cover_19 shipped as a
    # bare people photo because no hook survived; the edition's own title is then the headline.
    hook = cover_headline(brief.hook_text, concept, title, subtitle)
    byline = cited_byline(byline, hook, brief.concept)
    render_info: dict = {}
    from cqc_lem.utilities.ai.image_brief import person_word

    try:
        if brief.concept is None and not avatar and person_word(brief.prompt):
            # #2316, cover_19: with no Stage 1 concept there is nothing to tell the author from a
            # stranger, nor an article entity to anchor a scene to — the fallback brief's "candid
            # hallway conversation" shipped as a gala couple. No person renders without one.
            generated_path = typeset_cover(hook, None, brand, byline, render_info,
                                           "people_without_concept", user_id)
        elif avatar:
            generated_path = render_avatar_image_gated(
                brief.prompt, avatar=avatar, user_id=user_id, surface="newsletter",
                ratio=COVER_IMAGE_RATIO, focal_concept=brief.focal_concept,
                render_info=render_info, concept=brief.concept, hook_text=hook,
                layout=getattr(brief.concept, "layout", None), signature=byline,
                brand_kit=brand)
        else:
            generated_path = render_image_gated(brief.prompt, surface="newsletter",
                                                ratio=COVER_IMAGE_RATIO,
                                                focal_concept=brief.focal_concept,
                                                user_id=user_id, render_info=render_info,
                                                concept=brief.concept,
                                                hook_text=hook,
                                                layout=getattr(brief.concept, "layout", None),
                                                signature=byline, brand_kit=brand)
    except Exception as e:
        log_warning("Newsletter cover generation failed", exc=e, user_id=user_id,
                    action_type="newsletter_cover")
        return None, "Image generation failed"

    if not generated_path or not os.path.isfile(generated_path):
        log_warning("Newsletter cover generation returned no image", user_id=user_id,
                    action_type="newsletter_cover")
        return None, "Image generation returned nothing"

    if people_render_unrelated(render_info, brief.concept):
        # #2316 (covers 17 and 19): a stock people render the judge called generic or unrelated,
        # after its repair, never ships as one — the cover becomes a typeset card instead.
        generated_path = typeset_cover(hook, brief.concept, brand, byline, render_info,
                                       "people_generic", user_id) or generated_path
        people_policy = people_policy or PEOPLE_POLICY_STOCK

    # The deterministic gate reads what the renderer produced FIRST — a truncated or undersized
    # render must not be rescued by being composed into a full-size canvas.
    verdict = inspect_cover_file(generated_path)
    if not verdict.ok:
        log_warning("Generated newsletter cover failed the cover gate", user_id=user_id,
                    action_type="newsletter_cover", reason=verdict.reason)
        return None, verdict.reason or "Generated image rejected"
    composed = ensure_composed_cover(generated_path, hook, render_info, concept=brief.concept,
                                     brand=brand, byline=byline, user_id=user_id)
    if not composed:
        return None, "The cover could not be composed with its headline"
    composed = ensure_cover_ratio(composed, hook, render_info, concept=brief.concept, brand=brand,
                                  byline=byline, user_id=user_id)
    if not composed:
        return None, "The cover could not be made 16:9"
    if (is_split_layout(shipped_cover_layout(render_info, brief.concept))
            and split_budget_spent(recent_layouts)):
        # #2316: the rotation steered this cover full-bleed, yet it shipped as a split (a
        # code-drawn graphic, a recompose, an overlay below the contrast bar) — and two of the
        # last four covers already are. It is re-set as a non-split typeset card.
        composed = typeset_cover(hook, brief.concept, brand, byline, render_info,
                                 "split_budget", user_id) or composed
    if composed != generated_path:
        generated_path = composed
        verdict = inspect_cover_file(generated_path)
        if not verdict.ok:
            log_warning("Composed newsletter cover failed the cover gate", user_id=user_id,
                        action_type="newsletter_cover", reason=verdict.reason)
            return None, verdict.reason or "Generated image rejected"

    directory = _cover_dir(user_id)
    try:
        os.makedirs(directory, exist_ok=True)
        name = f"ed{int(edition_id)}_{secrets.token_hex(6)}{verdict.extension}"
        shutil.copyfile(generated_path, os.path.join(directory, name))
    except OSError as e:
        log_warning("Could not store generated newsletter cover", exc=e, user_id=user_id,
                    action_type="newsletter_cover")
        return None, "Could not store the generated image"

    relative = f"{COVER_SUBDIR}/{int(user_id)}/{name}"
    # Keyed by the STORED public URL, so a later audit resolves it the same way it resolves the
    # row's cover_image_path. Written for the real brief AND the deterministic fallback alike —
    # `brief.fallback` is what tells the two apart (issue #1992).
    gate_detail = {key: render_info[key] for key in _GATE_RECEIPT_KEYS if key in render_info}
    if render_info.get("gate_verdict") == "rejected":
        # Expected, not a defect: the cover still lands pending_review and the author decides.
        # The receipt below carries the rubric so the review can see WHY.
        log_info("Newsletter cover failed the image judge — stored for review", user_id=user_id,
                 action_type="newsletter_cover",
                 issues="; ".join(str(i) for i in render_info.get("gate_issues") or []))
    write_brief_receipt(cover_public_url(relative), brief, user_id=user_id,
                        gate_verdict=render_info.get("gate_verdict"),
                        extra={"edition_id": edition_id, "edition_format": edition_format,
                               "hook_style": hook_style, "people_policy": people_policy,
                               **gate_detail})
    log_info("Generated newsletter cover", user_id=user_id, action_type="newsletter_cover")
    return relative, None
