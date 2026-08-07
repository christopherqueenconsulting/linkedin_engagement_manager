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
"""

import os
import secrets
import shutil
from dataclasses import dataclass
from typing import Optional
from urllib.parse import parse_qs, quote, urlparse

from cqc_lem import assets_dir
from cqc_lem.utilities.logger import log_debug, log_info, log_warning

POST_IMAGE_SUBDIR = "images/posts"
POST_IMAGE_PREVIEW_SUBDIR = "images/post_previews"

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


def remove_post_image_file(image_url: Optional[str]) -> bool:
    """Best-effort delete of a stored post image file. Never raises — the row is the truth."""
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


def generate_image_for_post(user_id: int, text: str, post_id: Optional[int] = None
                            ) -> "tuple[Optional[str], Optional[str]]":
    """Render the image a text post publishes with. Returns ``(public_url, None)`` or
    ``(None, reason)``.

    Never raises: an image is enhancement, and a failed render must never take a post — or the
    author's edit — down with it. The avatar rides the EXISTING ``post_image`` surface and is
    resolved BEFORE the brief is authored, so the declared subject clause leads the prompt (#744).
    """
    if not text or not text.strip():
        return None, "Write the post content first — the image is drawn from it"

    from cqc_lem.utilities.ai.image_brief import build_image_brief
    from cqc_lem.utilities.avatar.guardrails import AVATAR_SURFACE_POST_IMAGE, resolve_avatar_for
    from cqc_lem.utilities.env_constants import DEFAULT_IMAGE_RATIO

    profile = None
    try:
        from cqc_lem.utilities.linkedin.helper import load_profile_for_user
        profile = load_profile_for_user(user_id)
    except Exception as e:
        log_debug("Profile load skipped for post image", error=str(e), user_id=user_id,
                  action_type="post_image")

    try:
        avatar = resolve_avatar_for(user_id, surface=AVATAR_SURFACE_POST_IMAGE, post_id=post_id)
    except Exception as e:
        log_warning("Avatar check failed for post image — rendering without", exc=e,
                    user_id=user_id, post_id=post_id, action_type="post_image")
        avatar = None

    try:
        brief = build_image_brief(text, surface="post_image", ratio=DEFAULT_IMAGE_RATIO,
                                  profile=profile, avatar=avatar)
    except Exception as e:
        log_warning("Post image prompt failed", exc=e, user_id=user_id, post_id=post_id,
                    action_type="post_image")
        return None, "Could not write an image prompt"

    try:
        if avatar:
            from cqc_lem.utilities.ai.image_gen import render_avatar_image_gated
            rendered = render_avatar_image_gated(
                brief.prompt, avatar=avatar, user_id=user_id, surface="post_image",
                ratio=DEFAULT_IMAGE_RATIO, focal_concept=brief.focal_concept, post_id=post_id)
        else:
            from cqc_lem.utilities.ai.image_gen import render_image_gated
            rendered = render_image_gated(brief.prompt, surface="post_image",
                                          ratio=DEFAULT_IMAGE_RATIO,
                                          focal_concept=brief.focal_concept,
                                          user_id=user_id, post_id=post_id)
    except Exception as e:
        log_warning("Post image generation failed", exc=e, user_id=user_id, post_id=post_id,
                    action_type="post_image")
        return None, "Image generation failed"

    if not rendered or not os.path.isfile(rendered):
        log_warning("Post image generation returned no image", user_id=user_id, post_id=post_id,
                    action_type="post_image")
        return None, "Image generation returned nothing"

    stored = store_rendered_post_image(user_id, rendered, post_id=post_id)
    if not stored:
        return None, "Could not store the generated image"
    log_info("Generated post image", user_id=user_id, post_id=post_id, action_type="post_image")
    return stored, None
