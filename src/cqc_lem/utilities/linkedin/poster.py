"""LinkedIn publishing through the official API — the non-Selenium half of posting.

Everything here authenticates with the user's stored OAuth token (`get_user_access_token`) rather
than a browser session, so none of it is subject to the navigation 429 breaker that throttles the
Selenium engagement lanes. Four shapes live here: a text / single-media / article-link `ugcPost`
(`share_on_linkedin`), a multi-image carousel (`share_carousel_on_linkedin`), a NATIVE document
deck via the versioned Documents API (`share_document_on_linkedin`), and comments and replies on
the member's OWN posts via socialActions.

Two rules run through all of it. **Missing credentials are not an exception** — every entry point
logs and returns None so the caller can flag the post instead of failing a Celery task. And **a
deck is all-or-nothing**: a carousel or document whose slides do not ALL resolve to a real image is
not posted at all, because a placeholder deck on the feed is worse than a post the caller marks
`error` for manual fix.
"""

import json
import os
import re
import shutil
import tempfile
import uuid
from typing import Annotated, Dict, List, Optional

import requests
from linkedin_api.clients.restli.client import RestliClient
from pydantic import BaseModel, Field
from pydantic.types import StringConstraints

from cqc_lem.utilities.db import get_user_access_token, get_user_linked_sub_id
from cqc_lem.utilities.env_constants import LI_API_VERSION
from cqc_lem.utilities.logger import log_debug, log_error, log_info, log_warning
from cqc_lem.utilities.mime_type_helper import get_file_mime_type
from cqc_lem.utilities.utils import get_file_extension_from_filepath

# (connect, read) seconds for the two-step media upload. `requests` waits forever by default and no
# task in this tree sets a Celery time limit, so an unanswered LinkedIn socket parks a Selenium-lane
# worker slot until the process is recycled. The byte upload gets the longer read budget of the two.
REGISTER_UPLOAD_TIMEOUT = (5, 30)
MEDIA_UPLOAD_TIMEOUT = (5, 120)

# Define annotations for constrained strings
ReadyStatus = Annotated[str, StringConstraints(pattern=r'^(READY)$')]
ShareMediaCategory = Annotated[str, StringConstraints(pattern=r'^(NONE|ARTICLE|IMAGE|VIDEO|DOCUMENT)$')]
NonEmptyString = Annotated[str, StringConstraints(min_length=1)]


class ShareMedia(BaseModel):
    """One media entry inside a legacy `ugcPost` share.

    `media` carries the asset URN returned by `upload_media` for an uploaded image / video /
    document; `originalUrl` is what an ARTICLE share uses instead, since a link has nothing
    uploaded to point at. The two are never both meaningful on the same entry.
    """

    status: ReadyStatus = "READY"
    description: Optional[Dict[str, str]] = Field(default_factory=lambda: {"text": ""})
    media: Optional[str] = None  # DigitalMediaAsset URN
    originalUrl: Optional[str] = Field(default_factory=lambda: "")
    title: Optional[Dict[str, str]] = Field(default_factory=lambda: {"text": ""})


class ShareContent(BaseModel):
    """The `com.linkedin.ugc.ShareContent` half of a legacy `ugcPost` entity.

    `shareMediaCategory` is constrained to the categories LinkedIn accepts and has to agree with
    what `media` holds — `NONE` for a text-only share, `IMAGE` / `VIDEO` / `ARTICLE` / `DOCUMENT`
    once entries are present. The commentary is a non-empty string, so a body-less share fails at
    construction here rather than as an opaque API rejection.
    """

    shareCommentary: Dict[str, NonEmptyString]
    shareMediaCategory: ShareMediaCategory
    media: Optional[List[ShareMedia]] = None


def download_media(media_path: str) -> str:
    """Fetch a remote media URL into a uniquely named /tmp file so it can be uploaded as bytes.

    The caller owns the returned path: `upload_media` deletes what it downloaded, and
    `share_document_on_linkedin` tracks them in `tmp_paths` and clears them in its `finally`.

    Raises:
        requests.HTTPError: a non-2xx download is deliberately not swallowed — media we cannot
            fetch must fail the post, never publish it silently without the media.
    """
    response = requests.get(media_path, timeout=30)
    response.raise_for_status()
    content = response.content
    extension = get_file_extension_from_filepath(media_path)
    tmp_path = f"/tmp/{uuid.uuid4()}{extension}"
    with open(tmp_path, "wb") as f:
        f.write(content)
    log_info(f"Downloaded media to: {tmp_path}")
    return tmp_path


def upload_media(access_token, owner_sub_id: str, media_path, media_type: str = "image"):
    """Register and upload one media file, returning the asset URN a `ShareMedia` entry points at.

    Two hops: `assets?action=registerUpload` reserves the asset and hands back a one-shot upload
    URL, then the bytes go there. `media_path` may be an http(s) URL — it is downloaded first and
    the temp copy removed once the upload has been attempted.

    `media_type` names the feedshare recipe (`image` / `video` / `document`, case-insensitive),
    which is how LinkedIn decides to process the asset.

    Raises:
        Exception: the byte upload did not answer 201. Nothing is published at this point, so a
            failed upload costs the post, never leaves a half-published one.
    """
    API_URL = 'https://api.linkedin.com/v2'

    is_tmp_path = False

    # If media_path is a URL, download the content
    if media_path.startswith('http'):
        media_path = download_media(media_path)
        is_tmp_path = True

    with open(media_path, 'rb') as media_file:
        media_content = media_file.read()

    headers = {
        'Authorization': f'Bearer {access_token}',
        'Content-Type': 'application/octet-stream'
    }
    upload_response = requests.post(
        f'{API_URL}/assets?action=registerUpload',
        headers=headers,
        timeout=REGISTER_UPLOAD_TIMEOUT,
        data=json.dumps({
            "registerUploadRequest": {
                "recipes": [f"urn:li:digitalmediaRecipe:feedshare-{str(media_type).lower()}"],
                "owner": f"urn:li:person:{owner_sub_id}",
                "serviceRelationships": [
                    {
                        "relationshipType": "OWNER",
                        "identifier": "urn:li:userGeneratedContent"
                    }
                ]
            }
        })
    )

    #print(f"Upload Response: {upload_response.json()}")

    upload_url = upload_response.json()['value']['uploadMechanism'][
        'com.linkedin.digitalmedia.uploading.MediaUploadHttpRequest']['uploadUrl']
    returned_headers = upload_response.json()['value']['uploadMechanism'][
        'com.linkedin.digitalmedia.uploading.MediaUploadHttpRequest']['headers']
    asset = upload_response.json()['value']['asset']
    mediaArtifact = upload_response.json()['value']['mediaArtifact']
    log_info(f"Asset: {asset} | Upload URL: {upload_url} | Headers: {returned_headers} | Media Artifact: {mediaArtifact}")

    # Upload the media file. A longer read budget than the register call: this is the one that
    # carries the bytes, and a video can be tens of MB over a slow uplink.
    upload_response = requests.put(upload_url, headers=headers, data=media_content,
                                   timeout=MEDIA_UPLOAD_TIMEOUT)

    # Delete the temp file
    if is_tmp_path:
        os.remove(media_path)

    if upload_response.status_code == 201:
        return asset
    else:
        raise Exception("Media upload failed")


def determine_media_type(media_path: str) -> str:
    """Classify a media file by extension into the `shareMediaCategory` LinkedIn needs for it.

    Returns:
        `IMAGE` or `VIDEO` — never a guess.

    Raises:
        ValueError: the extension is neither. The answer picks the feedshare recipe the bytes are
            registered under, so defaulting would upload the file as the wrong kind of asset;
            stopping the post is the safe end.
    """
    _, ext = os.path.splitext(os.path.basename(media_path))
    mime = get_file_mime_type(ext)
    if "image" in mime:
        return "IMAGE"
    elif "video" in mime:
        return "VIDEO"
    else:
        raise ValueError(f"Unsupported media type for extension '{ext}': {mime}")


def share_on_linkedin(user_id: int, content: str,
                      media_path: Optional[str] = None,
                      article_url: Optional[str] = None
                      ):
    """Publish a text, single-media, or article-link post as the user.

    `media_path` may be a local path or an http(s) URL; its kind is read off the extension and the
    bytes are uploaded BEFORE the share is created, so an upload failure raises with nothing
    published. `article_url` is the link-share alternative — when both are supplied the article
    replaces the media entry.

    Returns:
        The created post's URN, or None when the user has no stored LinkedIn credentials. That
        None is an account state for the caller to surface, not a failure to retry — no token
        means no amount of retrying will post.
    """
    restli_client = RestliClient()
    restli_client.session.hooks["response"].append(lambda r: r.raise_for_status())

    linked_sub_id = get_user_linked_sub_id(user_id)
    access_token = get_user_access_token(user_id)

    if not linked_sub_id or not access_token:
        # DEBUG: the docstring is explicit that this None is an ACCOUNT STATE for the caller to
        # surface, not a failure here — an account that never connected LinkedIn would otherwise
        # file a defect on every scheduled post.
        log_debug(f"No LinkedIn credentials found for user {user_id} — cannot post")
        return None

    media_objects = []
    share_media_category = "NONE"

    if media_path:
        media_type = determine_media_type(media_path)
        log_info(f"Detected Media Type: {media_type}")
        media_urn = upload_media(access_token,
                                 linked_sub_id,
                                 media_path,
                                 media_type)  # Assuming upload_media returns the URN of the uploaded media
        log_info(f"Media Uploaded to LinkedIn: {media_urn}")
        if media_type.lower() in ['image' ]:
            share_media_category = 'IMAGE'
        elif media_type.lower() in ['video' ]:
            share_media_category = 'VIDEO'
        elif media_type.lower() == 'article':
            share_media_category = 'ARTICLE'
        else:
            share_media_category = 'NONE'
        log_info(f"Set share_media_category: {share_media_category}")

        media_objects = [
            ShareMedia(
                status="READY",
                media=media_urn
            ).model_dump()
        ]
    if article_url:
        share_media_category = 'ARTICLE'
        media_objects = [
            ShareMedia(
                status="READY",
                originalUrl=article_url,
            ).model_dump()
        ]

    share_content = ShareContent(
        shareCommentary={"text": content},
        shareMediaCategory=share_media_category,
        media=media_objects
    ).model_dump()

    posts_create_response = restli_client.create(
        # resource_path="/posts",
        resource_path="/ugcPosts",
        entity={
            "author": f"urn:li:person:{linked_sub_id}",
            "lifecycleState": "PUBLISHED",
            # "visibility": "PUBLIC",
            # "distribution": {
            #    "feedDistribution": "MAIN_FEED",
            #    "targetEntities": [],
            #    "thirdPartyDistributionChannels": [],
            # },
            "specificContent": {
                "com.linkedin.ugc.ShareContent": {
                    **share_content
                }
            },
            "visibility": {
                "com.linkedin.ugc.MemberNetworkVisibility": "PUBLIC"
            }

        },
        # version_string=os.getenv("LI_API_VERSION"),
        access_token=access_token,
    )

    # For each key in posts_create_response print it out
    #myprint(f"Post Create Response:")
    #for key, value in posts_create_response.entity.items():
    #    myprint(f"{key}: {value}")

    urn = posts_create_response.entity_id
    log_info(f"Shared on LinkedIn via API call: https://www.linkedin.com/feed/update/{urn}")

    return urn


def _is_local_image_path(value: str) -> bool:
    """Return True if value looks like a local file path rather than a Pexels search query."""
    return bool(value) and (value.startswith("/") or os.path.isfile(value))


def _is_image_url(value: str) -> bool:
    """Return True if value is an HTTP/HTTPS URL (download handled by upload_media)."""
    return bool(value) and value.startswith("http")


def share_carousel_on_linkedin(user_id: int, content: str, slide_texts: list[str]) -> Optional[str]:
    """Post a multi-image carousel to LinkedIn.

    slide_texts entries can be:
    - An absolute file path to a pre-generated PNG slide image (created by create_carousel_slide_images)
    - A plain text search query (legacy) — a Pexels image is fetched for it

    All images are uploaded individually and included as a multi-image ugcPost.
    """
    import os

    from cqc_lem.utilities.carousel_creator import get_pexels_image_path

    restli_client = RestliClient()
    restli_client.session.hooks["response"].append(lambda r: r.raise_for_status())

    linked_sub_id = get_user_linked_sub_id(user_id)
    access_token = get_user_access_token(user_id)

    if not linked_sub_id or not access_token:
        # DEBUG: same account state as share_on_linkedin.
        log_debug(f"No LinkedIn credentials found for user {user_id} — cannot post carousel")
        return None

    # NO placeholder/default image. Every slide must resolve to a REAL image — a provided
    # path/URL (the normal case: branded slide PNGs from create_carousel_slide_images) or a
    # successful Pexels lookup. If ANY slide can't, we do NOT post a placeholder carousel
    # (which previously repeated a single default image on every slide); the caller flags
    # the post 'error' for manual/dev fix instead.
    media_urns = []
    missing = 0
    for slide in slide_texts:
        if _is_image_url(slide):
            image_path = slide  # upload_media downloads URLs
        elif _is_local_image_path(slide) and os.path.isfile(slide):
            image_path = slide
        else:
            image_path = get_pexels_image_path(slide)  # text query; None if Pexels unavailable

        if not image_path or (not _is_image_url(image_path) and not os.path.isfile(image_path)):
            # DEBUG: the per-slide detail. ONE condition gets ONE record, and the aggregate below
            # is the one that says the carousel did not ship.
            log_debug(f"Carousel slide has no real image (slide={slide!r}) — refusing to use a placeholder")
            missing += 1
            continue
        urn = upload_media(access_token, linked_sub_id, image_path, "IMAGE")
        if urn:
            media_urns.append(urn)
        else:
            missing += 1

    # Don't post a partial/placeholder carousel — require a real image for every slide.
    if missing > 0 or not media_urns:
        # ERROR: the post does not publish and the caller flags it 'error' for a manual fix. That
        # is a failure the user experiences, not a degraded path we recovered from.
        log_error("Carousel not posted — slide(s) without a real image",
                  user_id=user_id, missing_slides=missing)
        return None

    media_objects = [ShareMedia(status="READY", media=urn).model_dump() for urn in media_urns]

    share_content = ShareContent(
        shareCommentary={"text": content},
        shareMediaCategory="IMAGE",
        media=media_objects,
    ).model_dump()

    posts_create_response = restli_client.create(
        resource_path="/ugcPosts",
        entity={
            "author": f"urn:li:person:{linked_sub_id}",
            "lifecycleState": "PUBLISHED",
            "specificContent": {
                "com.linkedin.ugc.ShareContent": {
                    **share_content
                }
            },
            "visibility": {
                "com.linkedin.ugc.MemberNetworkVisibility": "PUBLIC"
            },
        },
        access_token=access_token,
    )

    urn = posts_create_response.entity_id
    log_info(f"Carousel shared on LinkedIn: https://www.linkedin.com/feed/update/{urn}")
    return urn


# --- native document (PDF) posts -----------------------------------------------
# LinkedIn's document format is a DIFFERENT feed object than a multi-image share: it
# renders as a swipeable, downloadable deck and is the highest-reach 2026 format. It is
# published through the VERSIONED Documents API (/rest/documents + /rest/posts), not the
# legacy multi-image ugcPost path used by share_carousel_on_linkedin. The legacy
# assets/ugcPost DOCUMENT path is kept as a fallback for tokens whose app is not
# provisioned for the versioned API.

DOCUMENT_TITLE_MAX = 100


def _document_title(content: str, fallback: str = "Document") -> str:
    """Derive the in-feed deck title from the post body's first non-empty line."""
    for line in (content or "").splitlines():
        line = line.strip()
        if line:
            return line[:DOCUMENT_TITLE_MAX]
    return fallback


def upload_document(access_token: str, owner_sub_id: str, document_path: str) -> Optional[str]:
    """Upload a PDF via the versioned Documents API. Returns the urn:li:document:... URN."""
    init_response = requests.post(
        "https://api.linkedin.com/rest/documents?action=initializeUpload",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
            "LinkedIn-Version": LI_API_VERSION,
            "X-Restli-Protocol-Version": "2.0.0",
        },
        json={"initializeUploadRequest": {"owner": f"urn:li:person:{owner_sub_id}"}},
        timeout=30,
    )
    # A retired LinkedIn-Version answers 426 NONEXISTENT_VERSION on every versioned call, which
    # would otherwise look like "app not provisioned" and silently demote every document post to
    # the legacy path. Name the real cause so it's a config fix, not a debugging session.
    if init_response.status_code == 426:
        log_error(f"LinkedIn API version {LI_API_VERSION} is retired — set LI_API_VERSION to a "
                  f"current version to publish native documents",
                  api_provider="linkedin", http_status=426)
    init_response.raise_for_status()

    value = init_response.json().get("value", {})
    upload_url = value.get("uploadUrl")
    document_urn = value.get("document")
    if not upload_url or not document_urn:
        raise ValueError(f"Documents API returned no upload URL/URN: {value}")

    with open(document_path, "rb") as document_file:
        document_bytes = document_file.read()

    upload_response = requests.put(
        upload_url,
        headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/octet-stream"},
        data=document_bytes,
        timeout=120,
    )
    if upload_response.status_code not in (200, 201):
        raise RuntimeError(f"Document upload failed with status {upload_response.status_code}")

    log_info("Document uploaded to LinkedIn", api_provider="linkedin")
    return document_urn


def _create_document_post_versioned(access_token: str, author: str, content: str,
                                    document_urn: str, title: str) -> Optional[str]:
    """Publish the uploaded document through the versioned /rest/posts endpoint."""
    response = requests.post(
        "https://api.linkedin.com/rest/posts",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
            "LinkedIn-Version": LI_API_VERSION,
            "X-Restli-Protocol-Version": "2.0.0",
        },
        json={
            "author": author,
            "commentary": content,
            "visibility": "PUBLIC",
            "distribution": {
                "feedDistribution": "MAIN_FEED",
                "targetEntities": [],
                "thirdPartyDistributionChannels": [],
            },
            "content": {"media": {"title": title, "id": document_urn}},
            "lifecycleState": "PUBLISHED",
            "isReshareDisabledByAuthor": False,
        },
        timeout=30,
    )
    response.raise_for_status()

    # The versioned API returns the new post's URN in the x-restli-id header; some
    # responses echo it in the body instead.
    urn = response.headers.get("x-restli-id")
    if not urn:
        try:
            urn = response.json().get("id")
        except ValueError:
            urn = None
    return urn


def _create_document_post_legacy(access_token: str, owner_sub_id: str, content: str,
                                 document_path: str, title: str) -> Optional[str]:
    """Fallback: legacy assets upload + ugcPost with shareMediaCategory=DOCUMENT."""
    asset_urn = upload_media(access_token, owner_sub_id, document_path, "document")

    share_content = ShareContent(
        shareCommentary={"text": content},
        shareMediaCategory="DOCUMENT",
        media=[ShareMedia(status="READY", media=asset_urn, title={"text": title}).model_dump()],
    ).model_dump()

    restli_client = _restli()
    posts_create_response = restli_client.create(
        resource_path="/ugcPosts",
        entity={
            "author": f"urn:li:person:{owner_sub_id}",
            "lifecycleState": "PUBLISHED",
            "specificContent": {
                "com.linkedin.ugc.ShareContent": {
                    **share_content
                }
            },
            "visibility": {
                "com.linkedin.ugc.MemberNetworkVisibility": "PUBLIC"
            },
        },
        access_token=access_token,
    )
    return posts_create_response.entity_id


def share_document_on_linkedin(user_id: int, content: str, slides: list[str],
                               post_id: Optional[int] = None,
                               title: Optional[str] = None) -> Optional[str]:
    """Publish a NATIVE document post: the rendered carousel slides bundled into one PDF.

    ``slides`` holds the same entries as a carousel post — absolute paths to slide PNGs or
    asset URLs. Every slide must resolve to a real image; a partial deck is never posted
    (the caller flags the post 'error' instead), matching share_carousel_on_linkedin.
    """
    from cqc_lem.utilities.carousel_creator import create_carousel_pdf

    linked_sub_id = get_user_linked_sub_id(user_id)
    access_token = get_user_access_token(user_id)

    if not linked_sub_id or not access_token:
        log_warning("No LinkedIn credentials found — cannot post document", user_id=user_id)
        return None

    local_paths: List[str] = []
    tmp_paths: List[str] = []
    # Without a post_id there is no per-post assets dir to write the PDF into, so it goes to
    # a temp dir that we own and delete once the upload is done (or has failed).
    tmp_pdf_dir: Optional[str] = None
    try:
        for slide in slides or []:
            if _is_image_url(slide):
                path = download_media(slide)
                tmp_paths.append(path)
            elif _is_local_image_path(slide) and os.path.isfile(slide):
                path = slide
            else:
                log_warning("Document slide is not a real image — not posting",
                            user_id=user_id, post_id=post_id)
                return None
            local_paths.append(path)

        if not local_paths:
            log_warning("Document post has no slides — not posting", user_id=user_id, post_id=post_id)
            return None

        if post_id is None:
            tmp_pdf_dir = tempfile.mkdtemp()
        pdf_path = create_carousel_pdf(
            local_paths,
            post_id if post_id is not None else user_id,
            output_dir=tmp_pdf_dir,
        )

        if not pdf_path:
            log_warning("Could not build document PDF from slides", user_id=user_id, post_id=post_id)
            return None

        deck_title = title or _document_title(content)

        try:
            document_urn = upload_document(access_token, linked_sub_id, pdf_path)
            urn = _create_document_post_versioned(access_token, f"urn:li:person:{linked_sub_id}",
                                                  content, document_urn, deck_title)
        except Exception as e:
            # Nothing is published until /rest/posts succeeds, so retrying on the legacy path
            # cannot duplicate the post.
            log_warning("Versioned Documents API failed — falling back to legacy ugcPost document",
                        exc=e, user_id=user_id, post_id=post_id, api_provider="linkedin")
            try:
                urn = _create_document_post_legacy(access_token, linked_sub_id, content, pdf_path, deck_title)
            except Exception as legacy_error:
                log_error("Document post failed on both the versioned and legacy paths",
                          exc=legacy_error, user_id=user_id, post_id=post_id, api_provider="linkedin")
                return None

        if not urn:
            log_error("Document post returned no URN", user_id=user_id, post_id=post_id, api_provider="linkedin")
            return None

        log_info(f"Document shared on LinkedIn: https://www.linkedin.com/feed/update/{urn}",
                 user_id=user_id, post_id=post_id, api_provider="linkedin")
        return urn
    finally:
        # Best-effort scratch cleanup: the post is already published (or already failed) at this
        # point, so a leftover temp file must never change the outcome — log it and move on.
        for path in tmp_paths:
            try:
                os.remove(path)
            except OSError as e:
                log_warning("Could not remove temporary document slide", exc=e,
                            user_id=user_id, post_id=post_id)
        if tmp_pdf_dir:
            shutil.rmtree(tmp_pdf_dir, ignore_errors=True)


# --- socialActions comments API -------------------------------------------------
# Comments on the MEMBER'S OWN posts go through LinkedIn's official socialActions API
# (w_member_social scope — the same token that publishes posts), NOT Selenium. This is
# immune to the browser-navigation 429 rate limit that throttles feed automation, and
# needs no login. Only own-post comments/replies are API-available; pinning, feed
# comments on others' posts, and DMs remain Selenium-only (LinkedIn exposes no API).
_URN_RE = re.compile(r"urn:li:(?:share|ugcPost|activity):[0-9]+")


def object_urn_from_post_url(post_url: str) -> Optional[str]:
    """Pull the share/ugcPost/activity URN out of a feed permalink like
    https://www.linkedin.com/feed/update/urn:li:share:123/ — the socialActions path key.
    """
    if not post_url:
        return None
    m = _URN_RE.search(post_url)
    return m.group(0) if m else None


def _restli() -> RestliClient:
    client = RestliClient()
    client.session.hooks["response"].append(lambda r: r.raise_for_status())
    return client


def comment_on_linkedin_post(user_id: int, object_urn: str, text: str,
                             parent_comment_urn: Optional[str] = None) -> Optional[str]:
    """Create a comment (or reply, when parent_comment_urn is given) on object_urn via the
    socialActions API. Returns the created comment URN, or None on missing creds/failure.
    """
    sub_id = get_user_linked_sub_id(user_id)
    access_token = get_user_access_token(user_id)
    if not sub_id or not access_token:
        # DEBUG: same account state as share_on_linkedin.
        log_debug(f"No LinkedIn credentials for user {user_id} — cannot comment via API")
        return None
    if not object_urn or not (text or "").strip():
        return None
    entity = {"actor": f"urn:li:person:{sub_id}", "message": {"text": text}}
    if parent_comment_urn:
        entity["parentComment"] = parent_comment_urn
    resp = _restli().create(
        resource_path="/socialActions/{urn}/comments",
        path_keys={"urn": object_urn},
        entity=entity,
        access_token=access_token,
    )
    comment_urn = resp.entity_id or (resp.entity or {}).get("$URN")
    log_info(f"Commented on {object_urn} via API: {comment_urn}")
    return comment_urn


# NOTE: there is deliberately no "list my comments" helper — w_member_social grants comment
# WRITE but not READ, so socialActions get_all returns empty for this app. Old comments made via
# the UI/Selenium therefore can't be discovered through the API; they must be removed via Selenium.


def delete_linkedin_comment(user_id: int, object_urn: str, comment_urn: str) -> bool:
    """Delete one of the user's own comments on object_urn via the socialActions API."""
    sub_id = get_user_linked_sub_id(user_id)
    access_token = get_user_access_token(user_id)
    if not sub_id or not access_token or not object_urn or not comment_urn:
        return False
    # The comment path key is the numeric id at the tail of the comment URN
    # (urn:li:comment:(activity:123,456) -> 456), per the socialActions sub-resource.
    comment_id = comment_urn.rstrip(")").split(",")[-1] if "," in comment_urn else comment_urn.split(":")[-1]
    _restli().delete(
        resource_path="/socialActions/{urn}/comments/{commentId}",
        path_keys={"urn": object_urn, "commentId": comment_id},
        query_params={"actor": f"urn:li:person:{sub_id}"},
        access_token=access_token,
    )
    log_info(f"Deleted comment {comment_urn} on {object_urn} via API")
    return True
