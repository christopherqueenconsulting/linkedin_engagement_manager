"""The ONE gate on whether a user's avatar may be used for a piece of generated media.

Issue #744 (Phase 2 of #548), decision 4A. Before this, "does the user have an active avatar"
was the entire policy: a never-previewed LoRA rendered a synthetic likeness of a real person
straight onto a published post, and the compose-time `use_avatar` toggle was accepted by the API
and dropped. Every caller that used to ask ``get_active_avatar()`` asks ``resolve_avatar_for()``
instead, and `None` always means "render with base Flux / Pexels".

Precedence, strongest first:
  1. ``users.avatar_disabled`` — the explicit "don't use my avatar" switch. Nothing overrides it.
  2. ``posts.use_avatar`` — the compose-time choice for THIS post (NULL = no choice made).
  3. ``users.avatar_use_<surface>`` — the per-content-type opt-in, default OFF.
  4. The avatar itself must be succeeded, have a model_ref, AND be ``approved``.

It fails CLOSED: any error resolving the policy returns None. A missed avatar render is a
slightly less personal image; a wrong one is a synthetic likeness of a real person, published.
"""
from typing import Optional

from cqc_lem.utilities.logger import log_debug, log_warning

AVATAR_SURFACE_POST_IMAGE = "post_image"
AVATAR_SURFACE_CAROUSEL = "carousel"
AVATAR_SURFACE_VIDEO = "video"
AVATAR_SURFACE_NEWSLETTER = "newsletter"

AVATAR_SURFACES: tuple[str, ...] = (
    AVATAR_SURFACE_POST_IMAGE,
    AVATAR_SURFACE_CAROUSEL,
    AVATAR_SURFACE_VIDEO,
    AVATAR_SURFACE_NEWSLETTER,
)

_PREF_BY_SURFACE: dict[str, str] = {
    AVATAR_SURFACE_POST_IMAGE: "avatar_use_post_image",
    AVATAR_SURFACE_CAROUSEL: "avatar_use_carousel",
    AVATAR_SURFACE_VIDEO: "avatar_use_video",
    AVATAR_SURFACE_NEWSLETTER: "avatar_use_newsletter",
}

DEFAULT_AVATAR_PREFERENCES: dict[str, bool] = {
    "avatar_disabled": False,
    "avatar_use_post_image": False,
    "avatar_use_carousel": False,
    "avatar_use_video": False,
    "avatar_use_newsletter": False,
}


def avatar_is_usable(avatar: Optional[dict]) -> bool:
    """A trained avatar the user has actually previewed and approved."""
    if not avatar:
        return False
    return (
        avatar.get("status") == "succeeded"
        and bool(avatar.get("model_ref"))
        and avatar.get("approval_status") == "approved"
    )


def resolve_avatar_for(user_id: Optional[int], *, surface: str,
                       post_id: Optional[int] = None) -> Optional[dict]:
    """The user's avatar when policy allows it on this surface, else None (use base Flux/Pexels)."""
    if not user_id:
        return None
    if surface not in _PREF_BY_SURFACE:
        raise ValueError(f"Unknown avatar surface: {surface}")

    try:
        from cqc_lem.utilities.db import get_active_avatar, get_avatar_preferences, get_post_use_avatar

        prefs = get_avatar_preferences(user_id)
        if prefs.get("avatar_disabled"):
            log_debug("Avatar use declined: user opted out entirely", user_id=user_id,
                      action_type="avatar_guardrail")
            return None

        post_choice = get_post_use_avatar(post_id) if post_id else None
        if post_choice is False:
            log_debug("Avatar use declined: post opted out at compose time", user_id=user_id,
                      post_id=post_id, action_type="avatar_guardrail")
            return None
        if post_choice is not True and not prefs.get(_PREF_BY_SURFACE[surface]):
            log_debug(f"Avatar use declined: {surface} opt-in is off", user_id=user_id,
                      action_type="avatar_guardrail")
            return None

        avatar = get_active_avatar(user_id)
        if not avatar_is_usable(avatar):
            log_debug("Avatar use declined: no approved active avatar", user_id=user_id,
                      action_type="avatar_guardrail")
            return None
        return avatar
    except Exception as e:
        # Fail closed — see the module docstring.
        log_warning("Avatar guardrail check failed, falling back to the base model", exc=e,
                    user_id=user_id, action_type="avatar_guardrail")
        return None


def avatar_allowed_for(user_id: Optional[int], *, surface: str,
                       post_id: Optional[int] = None) -> bool:
    """Boolean form of :func:`resolve_avatar_for` for callers that only branch on it."""
    return resolve_avatar_for(user_id, surface=surface, post_id=post_id) is not None
