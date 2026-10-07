"""Post images — the ONE place a post's image is validated, stored and removed (issue #1030).

`posts.image_url` has carried an image on generated TEXT posts since the image-gen overhaul, but
every path into it was a background task: the author could neither attach their own artwork nor ask
for a render from the Content Studio. This module is that manual half, and it deliberately reuses
the SAME brief engine + gated render the scheduled path uses — a per-surface prompt helper is
exactly what `docs/image-stack.md` forbids, so `generate_image_for_post` is the only new engine
here and `run_content_plan._generate_text_post_image` now goes through it too.

Two ways an image lands on a post, and like newsletter covers they are NOT symmetric in ORIGIN
(the author's own file vs. a render) but they ARE symmetric in review: a post is already held in
the review queue until a human approves it, so neither needs a second gate on top.

Storage:
- attached  → ``images/posts/<post_id>/`` — the same dir the generated path writes, so
  ``purge_post_assets`` cleans it after publish.
- composing → ``images/post_previews/<user_id>/`` — there is no row yet, mirroring what
  ``/generate-carousel`` already does for slides it hands back before a post exists. Abandoned
  previews are left on disk for the same reason abandoned carousel previews are: pruning them
  would have to outlive a post scheduled 30 days out.

``posts.image_url`` stores the PUBLIC ``/api/assets?file_name=`` URL rather than a path, because
that is the value the publish step hands to LinkedIn.

A GENERATED image also gets a brief receipt beside it (``media_provenance``, issue #1377) — the
render prompt, its focal concept and the vision gate's verdict, keyed by that same stored URL. An
uploaded one gets none: there is no brief behind the author's own artwork, and writing an empty
receipt would make an unauthored image read as one that depicted nothing.
"""

import os
import secrets
import shutil
from dataclasses import dataclass
from typing import Optional
from urllib.parse import parse_qs, quote, urlparse

from cqc_lem import assets_dir
from cqc_lem.utilities.logger import log_debug, log_info, log_warning
from cqc_lem.utilities.media_provenance import write_brief_receipt

POST_IMAGE_SUBDIR = "images/posts"
POST_IMAGE_PREVIEW_SUBDIR = "images/post_previews"

# 4:5 portrait by default (`post_image_ratio`); the others are what the renderers support.
DEFAULT_POST_IMAGE_RATIO = "4:5"
POST_IMAGE_RATIOS = ("4:5", "1:1", "16:9", "9:16")
# Which render produced the stored image — recorded on the receipt (#2249 gauntlet).
RENDER_PATH_AVATAR = "avatar"
RENDER_PATH_BASE = "base"
RENDER_PATH_BASE_FALLBACK = "base_after_avatar"
# The staged judge's WHY, recorded on the receipt beside the stored render (issue #2241).
GATE_RECEIPT_KEYS = ("gate_rubric", "gate_failing", "gate_issues", "gate_blind_description")

MAX_POST_IMAGE_BYTES = 8 * 1024 * 1024
# Below this a LinkedIn image share renders as a blurry thumbnail rather than media.
MIN_POST_IMAGE_WIDTH = 400
MIN_POST_IMAGE_HEIGHT = 400

# LinkedIn's image upload takes PNG and JPEG; the extension is also what `determine_media_type`
# reads to pick the share category, so an unsupported one fails at publish, not here.
_EXT_BY_FORMAT = {"PNG": ".png", "JPEG": ".jpg"}
ALLOWED_POST_IMAGE_FORMATS = tuple(_EXT_BY_FORMAT)


class PostImageRejected(Exception):
    """The image failed the deterministic gate — carries the user-facing reason."""


@dataclass
class PostImageVerdict:
    """Outcome of the deterministic gate. ``reason`` is user-facing when ``ok`` is False."""
    ok: bool
    reason: Optional[str] = None
    width: Optional[int] = None
    height: Optional[int] = None
    image_format: Optional[str] = None

    @property
    def extension(self) -> str:
        """Extension to store this image under, taken from the DECODED format, never the upload's name.

        An uploaded filename is untrusted — a `.png` that is really a JPEG is an ordinary browser
        upload — and `determine_media_type` reads the STORED extension at publish to pick LinkedIn's
        share category, so getting it from the name would fail there rather than here. The `.png`
        fallback is only reachable on a rejected verdict: every `ok` verdict carries a format that
        was matched against `_EXT_BY_FORMAT`.
        """
        return _EXT_BY_FORMAT.get(self.image_format or "", ".png")


def inspect_post_image_bytes(data: bytes) -> PostImageVerdict:
    """Grade raw image bytes against the post-image contract. Never raises."""
    if not data:
        return PostImageVerdict(False, "No image data received")
    if len(data) > MAX_POST_IMAGE_BYTES:
        return PostImageVerdict(False,
                                f"Image is larger than {MAX_POST_IMAGE_BYTES // (1024 * 1024)} MB")
    try:
        import io

        from PIL import Image

        with Image.open(io.BytesIO(data)) as img:
            img_format = (img.format or "").upper()
            width, height = img.size
    except Exception as e:
        log_debug("Post image could not be decoded", error=str(e), action_type="post_image")
        return PostImageVerdict(False, "That file is not a readable image")

    if img_format not in _EXT_BY_FORMAT:
        return PostImageVerdict(False, "Use a PNG or JPG image", width, height, img_format)
    if width < MIN_POST_IMAGE_WIDTH or height < MIN_POST_IMAGE_HEIGHT:
        return PostImageVerdict(
            False,
            f"Image is too small — at least {MIN_POST_IMAGE_WIDTH}x{MIN_POST_IMAGE_HEIGHT} px",
            width, height, img_format)
    return PostImageVerdict(True, None, width, height, img_format)


def post_image_relative_dir(user_id: int, post_id: Optional[int]) -> str:
    """Assets-relative dir this image belongs in — per POST once there is one, per USER while the
    author is still composing (a preview has no row to be scoped by).
    """
    if post_id:
        return f"{POST_IMAGE_SUBDIR}/{int(post_id)}"
    return f"{POST_IMAGE_PREVIEW_SUBDIR}/{int(user_id)}"


def post_image_public_url(relative_path: str) -> str:
    """The /api/assets URL stored on the row and rendered by the SPA."""
    from cqc_lem.utilities.env_constants import API_URL_FINAL
    return f"{API_URL_FINAL}/api/assets?file_name={quote(relative_path)}"


def post_image_relative_path(image_url: Optional[str]) -> Optional[str]:
    """The assets-relative path inside a stored post-image URL, or None when it isn't one.

    Only OUR own `/api/assets?file_name=` shape resolves: an image_url that points somewhere else
    (a hand-edited row) has no file of ours behind it, so there is nothing to read or delete.
    """
    if not image_url or not isinstance(image_url, str):
        return None
    try:
        query = parse_qs(urlparse(image_url).query)
    except ValueError:
        return None
    values = query.get("file_name") or []
    relative = (values[0] if values else "").replace("\\", "/").strip("/")
    if not relative:
        return None
    prefixes = (f"{POST_IMAGE_SUBDIR}/", f"{POST_IMAGE_PREVIEW_SUBDIR}/")
    return relative if relative.startswith(prefixes) else None


def post_image_abs_path(image_url: Optional[str]) -> Optional[str]:
    """Absolute path of a stored post image, or None when it escapes assets_dir / is gone.

    The value comes from our own DB, but resolving through ``realpath`` and re-checking containment
    keeps a hand-edited row from handing an arbitrary file to a delete (or to LinkedIn).
    """
    relative = post_image_relative_path(image_url)
    if not relative:
        return None
    root = os.path.realpath(assets_dir)
    candidate = os.path.realpath(os.path.join(root, relative))
    if candidate != root and not candidate.startswith(root + os.sep):
        log_warning("Post image path escapes the assets dir", action_type="post_image")
        return None
    return candidate if os.path.isfile(candidate) else None


def owns_post_image_url(user_id: int, image_url: Optional[str]) -> bool:
    """Is this URL a compose-time preview WE issued to THIS user?

    The compose form has no post row to attach to, so it hands the URL back at schedule time. That
    makes `image_url` caller-supplied input on a field the publish step later fetches, so only a
    preview under the caller's own dir is accepted — never an arbitrary URL, and never another
    account's preview.
    """
    relative = post_image_relative_path(image_url)
    if not relative:
        return False
    return relative.startswith(f"{POST_IMAGE_PREVIEW_SUBDIR}/{int(user_id)}/")


def _write_into(relative_dir: str, name: str) -> str:
    directory = os.path.join(assets_dir, *relative_dir.split("/"))
    os.makedirs(directory, exist_ok=True)
    return os.path.join(directory, name)


def save_post_image_bytes(user_id: int, data: bytes, post_id: Optional[int] = None) -> str:
    """Gate then persist uploaded image bytes; returns the public URL.

    Raises ``PostImageRejected`` with a user-facing reason when the gate fails — the caller turns
    that into a 400 rather than storing an image that would break the share.
    """
    verdict = inspect_post_image_bytes(data)
    if not verdict.ok:
        raise PostImageRejected(verdict.reason or "Image rejected")
    relative_dir = post_image_relative_dir(user_id, post_id)
    # Random suffix: /api/assets is public, so a predictable name would let anyone read another
    # user's unpublished image.
    name = f"img_{secrets.token_hex(6)}{verdict.extension}"
    with open(_write_into(relative_dir, name), "wb") as fh:
        fh.write(data)
    log_info("Stored uploaded post image", user_id=user_id, post_id=post_id,
             action_type="post_image")
    return post_image_public_url(f"{relative_dir}/{name}")


def store_rendered_post_image(user_id: int, source_path: str,
                              post_id: Optional[int] = None) -> Optional[str]:
    """Copy a rendered image into the post's (or the author's preview) dir. None on failure."""
    if not source_path or not os.path.isfile(source_path):
        return None
    relative_dir = post_image_relative_dir(user_id, post_id)
    extension = os.path.splitext(source_path)[1] or ".png"
    name = f"img_{secrets.token_hex(6)}{extension}"
    try:
        shutil.copyfile(source_path, _write_into(relative_dir, name))
    except OSError as e:
        log_warning("Could not store the generated post image", exc=e, user_id=user_id,
                    post_id=post_id, action_type="post_image")
        return None
    return post_image_public_url(f"{relative_dir}/{name}")


# An animated loop (docs/animated-posts.md) is stored BESIDE the still it was animated from, named
# off that still's own file name. No column records it: the name IS the binding, so replacing or
# removing the still can never leave the publisher holding a loop of a different picture.
POST_LOOP_SUFFIX = ".loop.gif"


def post_loop_relative_path(image_url: Optional[str]) -> Optional[str]:
    """Assets-relative path the loop for this stored still lives at (whether or not it exists)."""
    relative = post_image_relative_path(image_url)
    if not relative:
        return None
    return f"{os.path.splitext(relative)[0]}{POST_LOOP_SUFFIX}"


def post_loop_abs_path(image_url: Optional[str]) -> Optional[str]:
    """Absolute path of the stored loop for this still, or None when there is none.

    Resolved the same contained way as the still itself — a hand-edited row cannot point this at a
    file outside `assets_dir`.
    """
    relative = post_loop_relative_path(image_url)
    if not relative:
        return None
    root = os.path.realpath(assets_dir)
    candidate = os.path.realpath(os.path.join(root, relative))
    if not candidate.startswith(root + os.sep):
        return None
    return candidate if os.path.isfile(candidate) else None


def store_post_loop(image_url: Optional[str], gif_path: str) -> Optional[str]:
    """Copy a produced loop GIF in beside its still. Returns the stored path, or None.

    Only a still that is OURS (a resolvable stored image) can carry a loop, and only a `.gif` is
    accepted — the publisher routes this file through LinkedIn's image upload, so a non-GIF here
    would publish as the wrong thing.
    """
    if not gif_path or not gif_path.lower().endswith(".gif") or not os.path.isfile(gif_path):
        return None
    if not post_image_abs_path(image_url):
        return None
    relative = post_loop_relative_path(image_url)
    if not relative:
        return None
    target = os.path.join(assets_dir, *relative.split("/"))
    try:
        shutil.copyfile(gif_path, target)
    except OSError as e:
        log_warning("Could not store the animated loop beside the post image", exc=e,
                    action_type="post_image")
        return None
    return target


def remove_post_image_file(image_url: Optional[str]) -> bool:
    """Best-effort delete of a stored post image file. Never raises — the row is the truth.

    Its animated loop goes with it: a loop of a picture the post no longer carries must never be
    what the publisher finds.
    """
    loop_path = post_loop_abs_path(image_url)
    if loop_path:
        try:
            os.remove(loop_path)
        except OSError as e:
            log_debug("Could not delete post loop file", error=str(e), action_type="post_image")
    abs_path = post_image_abs_path(image_url)
    if not abs_path:
        return False
    try:
        os.remove(abs_path)
        return True
    except OSError as e:
        log_debug("Could not delete post image file", error=str(e), action_type="post_image")
        return False


_GENERATE_WINDOW_SECONDS = 3600
_GENERATE_KEY_PREFIX = "lem:post_image_gen"
# Returned by `generate_image_for_post` when the gate LOOKED and said no — the one failure that is
# a verdict on the render rather than an upstream that did not answer, so callers can tell them apart.
GATE_REJECTED_REASON = "The generated image did not pass the quality check — try again"


def claim_manual_generation(user_id: int) -> bool:
    """Claim one manual "Generate image" click; False once the hourly cap is spent.

    Claimed BEFORE the render, not counted after it: the button can be held down and every press is
    real inference spend, which is the same reason `regenerate_avatar_samples` reserves its re-roll
    in the statement that checks the cap. Fails OPEN when Redis is unavailable — a broker restart
    must not take the feature down, and the cost ledger still records whatever is spent.
    """
    from cqc_lem.utilities.env_constants import POST_IMAGE_GENERATE_MAX_PER_HOUR
    from cqc_lem.utilities.linkedin.rate_limit import shared_redis_client

    redis_client = shared_redis_client()
    if redis_client is None:
        return True
    key = f"{_GENERATE_KEY_PREFIX}:{int(user_id)}"
    try:
        count = int(redis_client.incr(key))
        if count == 1:
            # TTL only on the first increment, so the window is fixed rather than sliding.
            redis_client.expire(key, _GENERATE_WINDOW_SECONDS)
        return count <= POST_IMAGE_GENERATE_MAX_PER_HOUR
    except Exception as e:
        log_warning("Post image generation limiter unavailable — failing open", exc=e,
                    user_id=user_id, action_type="post_image")
        return True


def _gate_log_fields(render_info: dict) -> dict:
    """The judge's verdict as log fields: rubric, failing criteria, issues and blind description."""
    fields = {key: render_info[key] for key in GATE_RECEIPT_KEYS if render_info.get(key)}
    if "gate_rubric" in fields:
        fields["gate_rubric"] = ", ".join(f"{k}={v}" for k, v in fields["gate_rubric"].items())
    for key in ("gate_failing", "gate_issues"):
        if key in fields:
            fields[key] = "; ".join(str(i) for i in fields[key])
    return fields


def _render_avatar_post_image(brief, avatar: dict, user_id: int, post_id: Optional[int],
                              ratio: str, render_info: dict) -> "tuple[Optional[str], Optional[str]]":
    """The LoRA render of a post image, and why it is unusable (None when it is usable).

    Unusable means: the render raised (a Replicate 5xx), produced nothing, was REJECTED by the
    gate, or came back from the base-FLUX fallback inside the avatar renderer without the gate
    accepting it — the likeness never rendered, and FLUX is the weaker brief-follower.

    Returns:
        ``(path, fallback_reason)``.
    """
    from cqc_lem.utilities.ai.image_gen import render_avatar_image_gated
    try:
        path = render_avatar_image_gated(
            brief.prompt, avatar=avatar, user_id=user_id, surface="post_image",
            ratio=ratio, focal_concept=brief.focal_concept, post_id=post_id,
            render_info=render_info, concept=brief.concept, hook_text=brief.hook_text)
    except Exception as e:
        return None, f"avatar render raised {type(e).__name__}: {e}"[:200]
    if not path or not os.path.isfile(path):
        return None, "avatar render returned nothing"
    verdict = render_info.get("gate_verdict")
    if verdict == "rejected":
        return path, "avatar render rejected by the quality gate"
    if render_info.get("used_avatar") is False and verdict != "accepted":
        return path, "avatar inference failed and the base-FLUX fallback was not accepted"
    return path, None


def post_image_ratio() -> str:
    """The ratio a post image renders at — ``POST_IMAGE_RATIO``, read at call time, default 4:5.

    4:5 portrait takes more of the LinkedIn feed than a square does. An unsupported value falls
    back to the default rather than reaching a renderer that would quietly square it.

    Returns:
        One of ``POST_IMAGE_RATIOS``.
    """
    ratio = (os.getenv("POST_IMAGE_RATIO") or DEFAULT_POST_IMAGE_RATIO).strip()
    return ratio if ratio in POST_IMAGE_RATIOS else DEFAULT_POST_IMAGE_RATIO


def generate_image_for_post(user_id: int, text: str, post_id: Optional[int] = None
                            ) -> "tuple[Optional[str], Optional[str]]":
    """Render the image a text post publishes with.

    Returns ``(public_url, None)`` or ``(None, reason)``.

    Never raises: an image is enhancement, and a failed render must never take a post — or the
    author's edit — down with it. The avatar rides the EXISTING ``post_image`` surface and is
    resolved BEFORE the brief is authored, so the declared subject clause leads the prompt (#744).
    A final ``rejected`` gate verdict is never stored (#2105); an ``unchecked`` one fails open.

    The staged engine (issue #2241), as for newsletter covers: Stage 1 reads the post ONCE, the
    brief is authored from that concept plus the user's brand clause, and the render is graded by
    the blind judge against the same concept on both the base and avatar paths. The judge's rubric
    rides into the brief receipt.

    The likeness renders only when the piece is about the author
    (``guardrails.resolve_avatar_for_concept``). When it does and the LoRA render is unusable —
    rejected, empty, or a Replicate error — ONE gpt-image attempt without the likeness runs before
    the post ships bare; the receipt's ``render_path`` says which won (#2249 gauntlet).
    """
    if not text or not text.strip():
        return None, "Write the post content first — the image is drawn from it"

    from cqc_lem.utilities.ai.image_brief import build_image_brief
    from cqc_lem.utilities.ai.image_concept import analyze_content_for_image
    from cqc_lem.utilities.avatar.guardrails import (
        AVATAR_SURFACE_POST_IMAGE,
        resolve_avatar_for_concept,
    )
    from cqc_lem.utilities.brand_kit import brand_clause_for_user

    ratio = post_image_ratio()
    profile = None
    try:
        from cqc_lem.utilities.linkedin.helper import load_profile_for_user
        profile = load_profile_for_user(user_id)
    except Exception as e:
        log_debug("Profile load skipped for post image", error=str(e), user_id=user_id,
                  action_type="post_image")

    concept = analyze_content_for_image(text, surface="post_image", user_id=user_id)
    try:
        # Stage 1 first, because whether the author belongs in frame is a question about the piece.
        avatar = resolve_avatar_for_concept(user_id, surface=AVATAR_SURFACE_POST_IMAGE,
                                            concept=concept, source_text=text, post_id=post_id)
    except Exception as e:
        log_warning("Avatar check failed for post image — rendering without", exc=e,
                    user_id=user_id, post_id=post_id, action_type="post_image")
        avatar = None

    brand = brand_clause_for_user(user_id)
    try:
        brief = build_image_brief(text, surface="post_image", ratio=ratio,
                                  profile=profile, avatar=avatar, concept=concept,
                                  brand_kit=brand)
    except Exception as e:
        log_warning("Post image prompt failed", exc=e, user_id=user_id, post_id=post_id,
                    action_type="post_image")
        return None, "Could not write an image prompt"

    render_info: dict = {}
    render_path = RENDER_PATH_BASE
    fallback_reason: Optional[str] = None
    rendered: Optional[str] = None
    if avatar:
        rendered, fallback_reason = _render_avatar_post_image(brief, avatar, user_id, post_id,
                                                              ratio, render_info)
        render_path = RENDER_PATH_AVATAR
        if fallback_reason:
            # ONE non-avatar attempt before giving up: the stricter gate must not silently strip
            # images off posts because the LoRA could not follow a brief (#2249 gauntlet).
            log_info("Avatar post image unusable — one gpt-image attempt without the likeness",
                     user_id=user_id, post_id=post_id, action_type="post_image",
                     reason=fallback_reason, **_gate_log_fields(render_info))
            render_path = RENDER_PATH_BASE_FALLBACK
            render_info = {}
            try:
                brief = build_image_brief(text, surface="post_image", ratio=ratio,
                                          profile=profile, avatar=None, concept=concept,
                                          brand_kit=brand)
            except Exception as e:
                log_warning("Post image prompt failed", exc=e, user_id=user_id, post_id=post_id,
                            action_type="post_image")
                return None, "Could not write an image prompt"
    if render_path != RENDER_PATH_AVATAR:
        try:
            from cqc_lem.utilities.ai.image_gen import render_image_gated
            rendered = render_image_gated(brief.prompt, surface="post_image",
                                          ratio=ratio,
                                          focal_concept=brief.focal_concept,
                                          user_id=user_id, post_id=post_id,
                                          render_info=render_info, concept=brief.concept,
                                          hook_text=brief.hook_text)
        except Exception as e:
            log_warning("Post image generation failed", exc=e, user_id=user_id, post_id=post_id,
                        action_type="post_image")
            return None, "Image generation failed"

    if not rendered or not os.path.isfile(rendered):
        log_warning("Post image generation returned no image", user_id=user_id, post_id=post_id,
                    action_type="post_image")
        return None, "Image generation returned nothing"

    if render_info.get("gate_verdict") == "rejected":
        # The gate LOOKED and said no on the final candidate (issue #2105). Failing open is for a
        # gate that could not run (`unchecked`); a rejection means the post ships with no image.
        # Nothing is stored, so nothing carries a receipt — the log IS the rejection record, and
        # it carries the judge's whole WHY so a bare post is diagnosable (#2249 gauntlet).
        log_info("Post image rejected by the quality gate — the post ships without one",
                 user_id=user_id, post_id=post_id, action_type="post_image",
                 render_path=render_path, avatar_fallback_reason=fallback_reason,
                 **_gate_log_fields(render_info))
        return None, GATE_REJECTED_REASON

    stored = store_rendered_post_image(user_id, rendered, post_id=post_id)
    if not stored:
        return None, "Could not store the generated image"
    # Recorded against the STORED url, not the temp render: the receipt is keyed by the value that
    # lands on `posts.image_url`, which is the only handle a later audit has (issue #1377).
    gate_detail = {key: render_info[key] for key in GATE_RECEIPT_KEYS if key in render_info}
    gate_detail["render_path"] = render_path
    if fallback_reason:
        gate_detail["avatar_fallback_reason"] = fallback_reason
    write_brief_receipt(stored, brief, post_id=post_id, user_id=user_id,
                        gate_verdict=render_info.get("gate_verdict"), extra=gate_detail)
    # The verdict is ON the log line: an `unchecked` image (judge unreachable twice) ships, as it
    # always has, but is never indistinguishable from a graded one again (#2249 gauntlet).
    log_info("Generated post image", user_id=user_id, post_id=post_id, action_type="post_image",
             gate_verdict=render_info.get("gate_verdict"), render_path=render_path,
             gate_issues="; ".join(str(i) for i in render_info.get("gate_issues") or []))
    return stored, None
