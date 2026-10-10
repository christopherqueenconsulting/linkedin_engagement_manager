"""Curated-post publishing through the versioned ``/rest/posts`` endpoint — beside ``poster.py``.

Three shapes, one per curated treatment (``docs/curated-sources.md``):

- a **reshare with commentary** — ``reshareContext.parent`` names the original post, and LinkedIn
  renders it under our commentary with its author's name, face and link, so the author is credited
  by the platform itself;
- a **re-charted data card** — one image we drew from the source's verified figures;
- a **link post** — an article card whose thumbnail is the publisher's own ``og:image``, uploaded
  unmodified (the Posts API does not scrape a URL for you; the partner sets the card fields).

Same OAuth token and ``w_member_social`` scope as every other post. Every commentary goes through
``escape_little_text`` first, because ``/rest/posts`` parses commentary as LinkedIn's "little"
format and an unescaped ``(`` or ``@`` can truncate or reject it.

UNTESTED ASSUMPTION, documented rather than hidden: LinkedIn's reshare example uses an
ORGANIZATION author. A member author is the same body with ``urn:li:person:{sub}``, and the docs
list no separate scope, but nobody has run it live yet — ``docs/curated-sources.md`` carries the
one-shot test the owner runs after deploy. Until then a reshare that LinkedIn answers with a 4xx is
treated as REFUSED (never retried), the same as an author who disabled resharing.
"""

import os
import tempfile
from typing import Optional

import requests

from cqc_lem.utilities.curated_sources import escape_little_text, is_reshareable_urn
from cqc_lem.utilities.db import get_user_access_token, get_user_linked_sub_id
from cqc_lem.utilities.demo_mode import guard_linkedin
from cqc_lem.utilities.env_constants import LI_API_VERSION
from cqc_lem.utilities.linkedin.poster import (
    REGISTER_UPLOAD_TIMEOUT,
    _create_image_post_versioned,
    upload_image_versioned,
)
from cqc_lem.utilities.logger import log_debug, log_error, log_info, log_warning

POSTS_URL = "https://api.linkedin.com/rest/posts"
# The publisher's og:image is fetched from THEIR server, so it is bounded: https only, an image
# content type, and no bigger than LinkedIn would take as a thumbnail anyway.
THUMBNAIL_MAX_BYTES = 5 * 1024 * 1024
ARTICLE_TITLE_MAX = 200
ARTICLE_DESCRIPTION_MAX = 300


class ReshareRefused(Exception):
    """LinkedIn answered a reshare with a 4xx, so the reshare is refused for good.

    The cause is the author disabling resharing, a parent we may not reshare, or a member token
    that cannot reshare at all. Never retried: the source is marked blocked and the owner is told
    why.
    """


def _distribution() -> dict:
    return {"feedDistribution": "MAIN_FEED", "targetEntities": [],
            "thirdPartyDistributionChannels": []}


def build_reshare_body(author_urn: str, commentary: str, parent_urn: str) -> dict:
    """The ``/rest/posts`` body for a reshare with commentary.

    Args:
        author_urn: ``urn:li:person:{sub}``.
        commentary: Our text, as written — escaped here.
        parent_urn: The original post's ``urn:li:share:N`` or ``urn:li:ugcPost:N``.

    Returns:
        The request body.

    Raises:
        ValueError: ``parent_urn`` is not a share/ugcPost URN. An ``urn:li:activity`` is what the
            feed shows, and LinkedIn refuses it as a parent (INVALID_URN_TYPE).
    """
    if not is_reshareable_urn(parent_urn):
        raise ValueError(f"{parent_urn!r} is not a reshareable share/ugcPost URN")
    return {
        "author": author_urn,
        "commentary": escape_little_text(commentary),
        "visibility": "PUBLIC",
        "distribution": _distribution(),
        "lifecycleState": "PUBLISHED",
        "isReshareDisabledByAuthor": False,
        "reshareContext": {"parent": parent_urn.strip()},
    }


def build_article_body(author_urn: str, commentary: str, url: str, title: str,
                       description: str = "", thumbnail_urn: Optional[str] = None) -> dict:
    """The ``/rest/posts`` body for a link post with an article card.

    Args:
        author_urn: ``urn:li:person:{sub}``.
        commentary: Our text, as written — escaped here.
        url: The article URL the card links to.
        title: The card title (the publisher's own title).
        description: The card description.
        thumbnail_urn: The uploaded og:image, or None for a card without one.

    Returns:
        The request body.
    """
    article = {"source": url, "title": (title or url)[:ARTICLE_TITLE_MAX]}
    if description:
        article["description"] = description[:ARTICLE_DESCRIPTION_MAX]
    if thumbnail_urn:
        article["thumbnail"] = thumbnail_urn
    return {
        "author": author_urn,
        "commentary": escape_little_text(commentary),
        "visibility": "PUBLIC",
        "distribution": _distribution(),
        "content": {"article": article},
        "lifecycleState": "PUBLISHED",
        "isReshareDisabledByAuthor": False,
    }


def _create_post(access_token: str, body: dict) -> Optional[str]:
    """POST one body to ``/rest/posts``; the new post's URN, or None on a 2xx with no id."""
    response = requests.post(
        POSTS_URL,
        headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json",
                 "X-Restli-Protocol-Version": "2.0.0", "LinkedIn-Version": LI_API_VERSION},
        json=body, timeout=30)
    response.raise_for_status()
    urn = response.headers.get("x-restli-id")
    if not urn:
        try:
            urn = response.json().get("id")
        except ValueError:
            urn = None
    return urn


def _credentials(user_id: int) -> tuple[Optional[str], Optional[str]]:
    sub, token = get_user_linked_sub_id(user_id), get_user_access_token(user_id)
    if not sub or not token:
        # DEBUG: same contract as share_on_linkedin — no token is an ACCOUNT state the caller
        # surfaces on the post, not a failure to file.
        log_debug("No LinkedIn credentials — cannot publish a curated post", user_id=user_id)
        return None, None
    return sub, token


def share_reshare_on_linkedin(user_id: int, commentary: str, parent_urn: str) -> Optional[str]:
    """Reshare ``parent_urn`` with our commentary as the user.

    Returns:
        The new post's URN, or None (no credentials, a read timeout that may have published, or a
        2xx with no id — the caller holds the post for a human in every case).

    Raises:
        ReshareRefused: LinkedIn answered 4xx. Not retried.
        ValueError: ``parent_urn`` is an activity URN (refused before any request).
    """
    guard_linkedin("reshare.share_reshare_on_linkedin")
    sub, token = _credentials(user_id)
    if not sub:
        return None
    body = build_reshare_body(f"urn:li:person:{sub}", commentary, parent_urn)
    try:
        urn = _create_post(token, body)
    except requests.exceptions.ReadTimeout as e:
        log_error("Reshare timed out — not retrying, it may already be live", exc=e,
                  user_id=user_id, api_provider="linkedin")
        return None
    except requests.exceptions.HTTPError as e:
        status = getattr(e.response, "status_code", 0) or 0
        if 400 <= status < 500:
            raise ReshareRefused(f"LinkedIn refused the reshare ({status})") from e
        raise
    log_info(f"Curated reshare published: https://www.linkedin.com/feed/update/{urn}",
             user_id=user_id, api_provider="linkedin")
    return urn


def share_curated_image_on_linkedin(user_id: int, commentary: str,
                                    image_path: str) -> Optional[str]:
    """Publish a re-charted data card (one image we drew) with escaped commentary.

    Returns:
        The post URN, or None (no credentials, or an unconfirmed publish).
    """
    guard_linkedin("reshare.share_curated_image_on_linkedin")
    sub, token = _credentials(user_id)
    if not sub:
        return None
    image_urn = upload_image_versioned(token, sub, image_path)
    try:
        # The builder escapes the commentary itself (#2261) — escaping here too would double it.
        return _create_image_post_versioned(token, f"urn:li:person:{sub}", commentary, image_urn)
    except requests.exceptions.ReadTimeout as e:
        log_error("Re-chart publish timed out — not retrying, it may already be live", exc=e,
                  user_id=user_id, api_provider="linkedin")
        return None


def fetch_thumbnail(url: Optional[str]) -> Optional[str]:
    """Download the publisher's og:image to a temp file, byte-for-byte. None when not usable.

    Only an https URL answering with an ``image/*`` type under ``THUMBNAIL_MAX_BYTES`` is kept.
    The bytes are never edited — the card shows exactly the preview the publisher chose.
    """
    if not url or not url.startswith("https://"):
        return None
    try:
        response = requests.get(url, timeout=REGISTER_UPLOAD_TIMEOUT, stream=True,
                                headers={"User-Agent": "LEM link preview"})
        response.raise_for_status()
        kind = (response.headers.get("content-type") or "").split(";")[0].strip().lower()
        if not kind.startswith("image/"):
            return None
        data = b""
        for chunk in response.iter_content(64 * 1024):
            data += chunk
            if len(data) > THUMBNAIL_MAX_BYTES:
                return None
    except requests.RequestException as e:
        log_warning("Could not fetch the publisher's preview image — posting the card without it",
                    exc=e, api_provider="linkedin")
        return None
    handle, path = tempfile.mkstemp(suffix="." + (kind.split("/")[-1] or "img")[:5],
                                    prefix="lem_og_")
    with os.fdopen(handle, "wb") as fh:
        fh.write(data)
    return path


def share_article_on_linkedin(user_id: int, commentary: str, url: str, title: str,
                              description: str = "",
                              thumbnail_url: Optional[str] = None) -> Optional[str]:
    """Publish a link post: our commentary over an article card crediting the publisher.

    The thumbnail is the publisher's og:image, uploaded unmodified. A thumbnail that cannot be
    fetched or uploaded costs the image, never the post: the card publishes without one.

    Returns:
        The post URN, or None (no credentials, or an unconfirmed publish).
    """
    guard_linkedin("reshare.share_article_on_linkedin")
    sub, token = _credentials(user_id)
    if not sub:
        return None
    thumbnail_urn = None
    path = fetch_thumbnail(thumbnail_url)
    if path:
        try:
            thumbnail_urn = upload_image_versioned(token, sub, path)
        except Exception as e:
            log_warning("Thumbnail upload failed — posting the card without it", exc=e,
                        user_id=user_id, api_provider="linkedin")
        finally:
            try:
                os.remove(path)
            except OSError as e:
                log_debug("Temp thumbnail not removed", error=str(e))
    body = build_article_body(f"urn:li:person:{sub}", commentary, url, title, description,
                              thumbnail_urn)
    try:
        urn = _create_post(token, body)
    except requests.exceptions.ReadTimeout as e:
        log_error("Link post timed out — not retrying, it may already be live", exc=e,
                  user_id=user_id, api_provider="linkedin")
        return None
    log_info(f"Curated link post published: https://www.linkedin.com/feed/update/{urn}",
             user_id=user_id, api_provider="linkedin")
    return urn
