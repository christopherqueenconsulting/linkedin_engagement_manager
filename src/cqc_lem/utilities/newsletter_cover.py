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

# How many prior covers' focal concepts a new brief is steered away from (issue #1992). Mirrors
# `enforce_variety`'s window for posts; reads what is already on disk rather than a new DB column.
_VARIETY_WINDOW = 5

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
    """Best-effort delete of a stored cover file. Never raises — the DB row is the source of truth."""
    abs_path = cover_abs_path(relative_path)
    if not abs_path:
        return False
    try:
        os.remove(abs_path)
        return True
    except OSError as e:
        log_debug("Could not delete newsletter cover file", error=str(e),
                  action_type="newsletter_cover")
        return False


def _edition_text(title: Optional[str], subtitle: Optional[str], body: Optional[str]) -> str:
    return "\n\n".join(p for p in (title, subtitle, (body or "")[:1500]) if p)


def _extract_cover_concept(title: Optional[str], subtitle: Optional[str],
                           body: Optional[str]) -> dict:
    """Cheap `lem-simple` read of the edition's PAYOFF concept, from the FULL body.

    `_edition_text` sends the first 1500 chars of body to the brief author, which is the HOOK —
    for an edition about a routing switch, a silent failure, or a budget leak, the payoff concept
    those actually name sits past that boundary, so the brief author fell to the only concrete
    nouns in the hook (laptop, screen, LinkedIn) every time (issue #1992). Returns `{}` on any
    failure or an unusable response — the caller still has title/subtitle/body to fall back to, so
    a dead LLM degrades to weaker steering, never to no image at all.
    """
    text = "\n\n".join(p for p in (title, subtitle, body) if p)[:8000]
    if not text.strip():
        return {}
    from cqc_lem.utilities.ai.client import client

    try:
        response = client.chat.completions.create(
            model="lem-simple",
            messages=[{"role": "user", "content": (
                "Read this newsletter edition and name its core MECHANISM — not its topic, the "
                "topic's PHYSICAL METAPHOR. Never a person, laptop, screen, or office scene. "
                "Respond with ONLY a JSON object: {\"core_mechanism\": \"<the idea reduced to a "
                "physical mechanism, one sentence — a switch, a leak, a scale, a chain, a "
                "valve...>\", \"tangible_metaphor_candidates\": [\"<object>\", \"<object>\", "
                "\"<object>\"], \"avoid\": [\"<generic noun this edition's hook keeps repeating, "
                "e.g. laptop, screen, desk>\"]}\n\n"
                f"<edition>{text}</edition>")}],
            response_format={"type": "json_object"},
            temperature=0.4,
            max_tokens=300,
        )
        parsed = json.loads(response.choices[0].message.content or "{}")
    except Exception as e:
        log_debug("Cover concept extraction unavailable — brief falls back to raw edition text",
                  error=str(e), action_type="newsletter_cover")
        return {}
    mechanism = str(parsed.get("core_mechanism") or "").strip()
    candidates = [str(c).strip() for c in (parsed.get("tangible_metaphor_candidates") or [])
                 if str(c).strip()][:3]
    avoid = [str(a).strip() for a in (parsed.get("avoid") or []) if str(a).strip()]
    if not mechanism and not candidates:
        return {}
    return {"core_mechanism": mechanism, "tangible_metaphor_candidates": candidates,
           "avoid": avoid}


def _cover_concept_text(title: Optional[str], subtitle: Optional[str], body: Optional[str],
                        variety_avoid: Optional[list] = None) -> str:
    """The content string a cover's brief is authored from — the edition's PAYOFF, not its hook.

    Falls back to the old excerpt (title/subtitle + first 1500 chars of body) when concept
    extraction returns nothing, so the brief author always has SOMETHING concrete to draw from.
    """
    concept = _extract_cover_concept(title, subtitle, body)
    parts = [p for p in (title, subtitle) if p]
    if concept.get("core_mechanism") or concept.get("tangible_metaphor_candidates"):
        if concept.get("core_mechanism"):
            parts.append(f"Core mechanism to depict: {concept['core_mechanism']}")
        if concept.get("tangible_metaphor_candidates"):
            parts.append("Candidate physical metaphors: "
                         + "; ".join(concept["tangible_metaphor_candidates"]))
    else:
        parts.append((body or "")[:1500])
    avoid_all = list(concept.get("avoid") or []) + list(variety_avoid or [])
    if avoid_all:
        parts.append("Already used by recent covers — pick something visually distinct: "
                     + "; ".join(avoid_all))
    return "\n\n".join(p for p in parts if p)


def _recent_focal_concepts(user_id: int, limit: int = _VARIETY_WINDOW) -> list:
    """The last `limit` cover receipts' `focal_concept`, most-recent first (issue #1992).

    Mirrors `enforce_variety`'s cross-item memory for posts, but reads it off the assets volume
    rather than a new DB column — the brief receipt (`media_provenance.write_brief_receipt`) is
    already the durable record. Never raises: a directory that doesn't exist yet, or a receipt
    that won't parse, just contributes nothing to the variety context.
    """
    directory = _cover_dir(user_id)
    try:
        names = [n for n in os.listdir(directory) if n.endswith(".brief.json")]
    except OSError:
        return []
    names.sort(key=lambda n: os.path.getmtime(os.path.join(directory, n)), reverse=True)
    concepts = []
    for name in names[:max(0, int(limit))]:
        try:
            with open(os.path.join(directory, name), "r", encoding="utf-8") as fh:
                payload = json.load(fh)
        except (OSError, ValueError):
            continue
        concept = str(payload.get("focal_concept") or "").strip()
        if concept:
            concepts.append(concept)
    return concepts


def build_cover_prompt(title: Optional[str], subtitle: Optional[str], body: Optional[str],
                       profile=None) -> str:
    """The image prompt for an edition's cover, via the ONE brief engine.

    Deliberately not a parallel per-content-type prompt helper: ``build_image_brief`` owns the
    engagement fundamentals a cover needs — ONE focal subject drawn from the edition's actual
    content, strong foreground separation, and NO text/logos/charts (generators render those as
    garbled artifacts, which is exactly what makes a cover look machine-made). The ``newsletter``
    preset adds the cover-specific art direction.
    """
    from cqc_lem.utilities.ai.image_brief import build_image_brief

    return build_image_brief(_edition_text(title, subtitle, body), surface="newsletter",
                             ratio=COVER_IMAGE_RATIO, profile=profile).prompt


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
    """The avatar for a per-edition 'With me' click. The explicit choice beats the per-surface
    opt-in (same precedence a post's compose-time toggle has), but NEVER beats ``avatar_disabled``
    or the preview/approval gate. Fails closed.
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
    personal-story one; folded into the brief's ``content_shape``. The content itself is drawn
    from ``_cover_concept_text`` (the edition's PAYOFF, steered away from the last
    ``_VARIETY_WINDOW`` covers' focal concepts) rather than the edition's raw hook text — a real
    brief AND the deterministic fallback both get a ``.brief.json`` receipt beside the stored
    cover (``media_provenance.write_brief_receipt``), so every generation is auditable.
    """
    from cqc_lem.utilities.ai.image_brief import build_image_brief
    from cqc_lem.utilities.ai.image_gen import render_avatar_image_gated, render_image_gated
    from cqc_lem.utilities.media_provenance import write_brief_receipt

    avatar = _resolve_cover_avatar(user_id, use_avatar, title, subtitle, body)
    content = _cover_concept_text(title, subtitle, body,
                                  variety_avoid=_recent_focal_concepts(user_id))
    shape = (f"format={edition_format or 'unspecified'}, hook_style={hook_style or 'unspecified'}"
            if edition_format or hook_style else None)

    try:
        # The avatar is resolved BEFORE the brief is authored: its declared subject clause is what
        # stops the prompt LLM inventing a different person for the LoRA to contradict (#744).
        brief = build_image_brief(content, surface="newsletter",
                                  ratio=COVER_IMAGE_RATIO, profile=profile, avatar=avatar,
                                  extra_direction=guidance, content_shape=shape)
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
                render_info=render_info)
        else:
            generated_path = render_image_gated(brief.prompt, surface="newsletter",
                                                ratio=COVER_IMAGE_RATIO,
                                                focal_concept=brief.focal_concept,
                                                user_id=user_id, render_info=render_info)
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
    write_brief_receipt(cover_public_url(relative), brief, user_id=user_id,
                        gate_verdict=render_info.get("gate_verdict"),
                        extra={"edition_id": edition_id, "edition_format": edition_format,
                               "hook_style": hook_style})
    log_info("Generated newsletter cover", user_id=user_id, action_type="newsletter_cover")
    return relative, None
