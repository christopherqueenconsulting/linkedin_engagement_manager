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
import secrets
import shutil
from dataclasses import dataclass
from typing import Optional

from cqc_lem import assets_dir
from cqc_lem.utilities.logger import log_debug, log_info, log_warning

# How many prior covers steer the next brief's variety (issue #1992). Mirrors `enforce_variety`'s
# window for posts; reads what is already on disk rather than a new DB column.
_VARIETY_WINDOW = 5

# The staged judge's findings, copied from the render gate onto the cover's brief receipt.
_GATE_RECEIPT_KEYS = ("gate_rubric", "gate_failing", "gate_issues", "gate_blind_description")

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
                          subtitle: Optional[str], body: Optional[str]) -> Optional[dict]:
    """Which avatar (if any) this cover renders with.

    ``use_avatar`` is the per-edition override: False never renders it, True skips only the
    per-surface opt-in and the relevance classifier. ``None`` (Auto) needs BOTH the guardrails
    (``avatar_use_newsletter`` opt-in + approval) AND the classifier to agree — the owner's ask
    was "some newsletters, when it's relevant to the article", and this is that conjunction.
    """
    if use_avatar is False:
        return None
    if use_avatar is True:
        return _avatar_for_explicit_choice(user_id)

    from cqc_lem.utilities.avatar.guardrails import AVATAR_SURFACE_NEWSLETTER, resolve_avatar_for

    avatar = resolve_avatar_for(user_id, surface=AVATAR_SURFACE_NEWSLETTER)
    if not avatar:
        return None
    return avatar if classify_avatar_relevance(title, subtitle, body) else None


def generate_cover_for_edition(user_id: int, edition_id: int, title: Optional[str],
                               subtitle: Optional[str], body: Optional[str],
                               profile=None,
                               use_avatar: Optional[bool] = None,
                               guidance: Optional[str] = None,
                               edition_format: Optional[str] = None,
                               hook_style: Optional[str] = None,
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
    from cqc_lem.utilities.media_provenance import write_brief_receipt

    avatar = _resolve_cover_avatar(user_id, use_avatar, title, subtitle, body)
    shape = (f"format={edition_format or 'unspecified'}, hook_style={hook_style or 'unspecified'}"
            if edition_format or hook_style else None)

    try:
        from cqc_lem.utilities.ai.image_concept import GRAPHIC_WINDOW, ROTATION_WINDOW
        concept = analyze_content_for_image(
            _edition_full_text(subtitle, body), title=title, surface="newsletter",
            user_id=user_id,
            recent_treatments=_recent_cover_treatments(user_id, GRAPHIC_WINDOW),
            recent_layouts=_recent_concept_field(user_id, "layout", ROTATION_WINDOW),
            recent_casts=_recent_concept_field(user_id, "cast", ROTATION_WINDOW),
            recent_hook_shapes=_recent_concept_field(user_id, "hook_shape", ROTATION_WINDOW))
        # The avatar is resolved BEFORE the brief is authored: its declared subject clause is what
        # stops the prompt LLM inventing a different person for the LoRA to contradict (#744).
        brief = build_image_brief("\n\n".join(p for p in (title, subtitle, body) if p),
                                  surface="newsletter", ratio=COVER_IMAGE_RATIO, profile=profile,
                                  avatar=avatar, extra_direction=guidance, content_shape=shape,
                                  avoid_terms=_recent_cover_signals(user_id), concept=concept)
    except Exception as e:
        log_warning("Newsletter cover prompt failed", exc=e, user_id=user_id,
                    action_type="newsletter_cover")
        return None, "Could not write a cover prompt"

    render_info: dict = {}
    try:
        if avatar:
            generated_path = render_avatar_image_gated(
                brief.prompt, avatar=avatar, user_id=user_id, surface="newsletter",
                ratio=COVER_IMAGE_RATIO, focal_concept=brief.focal_concept,
                render_info=render_info, concept=brief.concept, hook_text=brief.hook_text,
                layout=getattr(brief.concept, "layout", None))
        else:
            generated_path = render_image_gated(brief.prompt, surface="newsletter",
                                                ratio=COVER_IMAGE_RATIO,
                                                focal_concept=brief.focal_concept,
                                                user_id=user_id, render_info=render_info,
                                                concept=brief.concept,
                                                hook_text=brief.hook_text,
                                                layout=getattr(brief.concept, "layout", None))
    except Exception as e:
        log_warning("Newsletter cover generation failed", exc=e, user_id=user_id,
                    action_type="newsletter_cover")
        return None, "Image generation failed"

    if not generated_path or not os.path.isfile(generated_path):
        log_warning("Newsletter cover generation returned no image", user_id=user_id,
                    action_type="newsletter_cover")
        return None, "Image generation returned nothing"

    verdict = inspect_cover_file(generated_path)
    if not verdict.ok:
        log_warning("Generated newsletter cover failed the cover gate", user_id=user_id,
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
                               "hook_style": hook_style, **gate_detail})
    log_info("Generated newsletter cover", user_id=user_id, action_type="newsletter_cover")
    return relative, None
