import json
import os
import re
import uuid
from typing import List, Optional, Annotated, Dict

import requests
from linkedin_api.clients.restli.client import RestliClient
from pydantic import BaseModel, Field
from pydantic.types import StringConstraints

from cqc_lem.utilities.db import get_user_linked_sub_id, get_user_access_token
from cqc_lem.utilities.logger import myprint
from cqc_lem.utilities.mime_type_helper import get_file_mime_type
from cqc_lem.utilities.utils import get_file_extension_from_filepath

# Define annotations for constrained strings
ReadyStatus = Annotated[str, StringConstraints(pattern=r'^(READY)$')]
ShareMediaCategory = Annotated[str, StringConstraints(pattern=r'^(NONE|ARTICLE|IMAGE|VIDEO)$')]
NonEmptyString = Annotated[str, StringConstraints(min_length=1)]


class ShareMedia(BaseModel):
    status: ReadyStatus = "READY"
    description: Optional[Dict[str, str]] = Field(default_factory=lambda: {"text": ""})
    media: Optional[str] = None  # DigitalMediaAsset URN
    originalUrl: Optional[str] = Field(default_factory=lambda: "")
    title: Optional[Dict[str, str]] = Field(default_factory=lambda: {"text": ""})


class ShareContent(BaseModel):
    shareCommentary: Dict[str, NonEmptyString]
    shareMediaCategory: ShareMediaCategory
    media: Optional[List[ShareMedia]] = None


def download_media(media_path: str) -> str:
    response = requests.get(media_path, timeout=30)
    response.raise_for_status()
    content = response.content
    extension = get_file_extension_from_filepath(media_path)
    tmp_path = f"/tmp/{uuid.uuid4()}{extension}"
    with open(tmp_path, "wb") as f:
        f.write(content)
    myprint(f"Downloaded media to: {tmp_path}")
    return tmp_path


def upload_media(access_token, owner_sub_id: str, media_path, media_type: str = "image"):
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
    myprint(f"Asset: {asset} | Upload URL: {upload_url} | Headers: {returned_headers} | Media Artifact: {mediaArtifact}")

    # Upload the media file
    upload_response = requests.put(upload_url, headers=headers, data=media_content)

    # Delete the temp file
    if is_tmp_path:
        os.remove(media_path)

    if upload_response.status_code == 201:
        return asset
    else:
        raise Exception("Media upload failed")


def determine_media_type(media_path: str) -> str:
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
    restli_client = RestliClient()
    restli_client.session.hooks["response"].append(lambda r: r.raise_for_status())

    linked_sub_id = get_user_linked_sub_id(user_id)
    access_token = get_user_access_token(user_id)

    if not linked_sub_id or not access_token:
        myprint(f"No LinkedIn credentials found for user {user_id} — cannot post")
        return None

    media_objects = []
    share_media_category = "NONE"

    if media_path:
        media_type = determine_media_type(media_path)
        myprint(f"Detected Media Type: {media_type}")
        media_urn = upload_media(access_token,
                                 linked_sub_id,
                                 media_path,
                                 media_type)  # Assuming upload_media returns the URN of the uploaded media
        myprint(f"Media Uploaded to LinkedIn: {media_urn}")
        if media_type.lower() in ['image' ]:
            share_media_category = 'IMAGE'
        elif media_type.lower() in ['video' ]:
            share_media_category = 'VIDEO'
        elif media_type.lower() == 'article':
            share_media_category = 'ARTICLE'
        else:
            share_media_category = 'NONE'
        myprint(f"Set share_media_category: {share_media_category}")

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
    myprint(f"Shared on LinkedIn via API call: https://www.linkedin.com/feed/update/{urn}")

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
        myprint(f"No LinkedIn credentials found for user {user_id} — cannot post carousel")
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
            myprint(f"Carousel slide has no real image (slide={slide!r}) — refusing to use a placeholder")
            missing += 1
            continue
        urn = upload_media(access_token, linked_sub_id, image_path, "IMAGE")
        if urn:
            media_urns.append(urn)
        else:
            missing += 1

    # Don't post a partial/placeholder carousel — require a real image for every slide.
    if missing > 0 or not media_urns:
        myprint(f"Carousel for user {user_id}: {missing} slide(s) without a real image — not posting (flag error).")
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
    myprint(f"Carousel shared on LinkedIn: https://www.linkedin.com/feed/update/{urn}")
    return urn



# --- socialActions comments API -------------------------------------------------
# Comments on the MEMBER'S OWN posts go through LinkedIn's official socialActions API
# (w_member_social scope — the same token that publishes posts), NOT Selenium. This is
# immune to the browser-navigation 429 rate limit that throttles feed automation, and
# needs no login. Only own-post comments/replies are API-available; pinning, feed
# comments on others' posts, and DMs remain Selenium-only (LinkedIn exposes no API).
_URN_RE = re.compile(r"urn:li:(?:share|ugcPost|activity):[0-9]+")


def object_urn_from_post_url(post_url: str) -> Optional[str]:
    """Pull the share/ugcPost/activity URN out of a feed permalink like
    https://www.linkedin.com/feed/update/urn:li:share:123/ — the socialActions path key."""
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
    socialActions API. Returns the created comment URN, or None on missing creds/failure."""
    sub_id = get_user_linked_sub_id(user_id)
    access_token = get_user_access_token(user_id)
    if not sub_id or not access_token:
        myprint(f"No LinkedIn credentials for user {user_id} — cannot comment via API")
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
    myprint(f"Commented on {object_urn} via API: {comment_urn}")
    return comment_urn


def get_my_comment_urns_on_object(user_id: int, object_urn: str) -> List[str]:
    """URNs of comments authored by THIS user on object_urn (used to find comments to delete)."""
    sub_id = get_user_linked_sub_id(user_id)
    access_token = get_user_access_token(user_id)
    if not sub_id or not access_token or not object_urn:
        return []
    actor = f"urn:li:person:{sub_id}"
    resp = _restli().get_all(
        resource_path="/socialActions/{urn}/comments",
        path_keys={"urn": object_urn},
        access_token=access_token,
    )
    mine = []
    for el in (resp.elements or []):
        if el.get("actor") == actor:
            urn = el.get("$URN") or el.get("urn") or el.get("commentUrn")
            if urn:
                mine.append(urn)
    return mine


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
    myprint(f"Deleted comment {comment_urn} on {object_urn} via API")
    return True
