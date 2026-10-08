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

``resolve_avatar_for_concept`` adds ONE more rule on top for surfaces that have a Stage 1 concept
(post images, the video source frame, carousel slides): the policy may ALLOW the avatar, but the
author is only put in frame when the piece is about them — a ``people_scene`` whose concept reads
as the author's own story, decision or reaction. Everything else renders without the likeness
through gpt-image, which follows a brief far better than the FLUX LoRA (post-image gauntlet on
#2249: six avatar renders of posts about other things, six rejections). An explicit compose-time
``posts.use_avatar = true`` still wins — the author asked for themselves on this post.
"""
import re
from typing import Any, Optional

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
    # NOT a surface — it does not decide whether the avatar renders, it decides whether burned
    # caption text may sit on a frame the avatar already rendered (issue #1278). Default OFF for
    # the same reason as the surfaces: nothing goes over a real person's likeness unasked.
    "avatar_caption_overlay": False,
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


# The concept fields that say who the piece is about. A first-person marker or "the author" in any
# of them means the piece is the author's own story, decision or reaction.
_AUTHOR_SIGNAL = re.compile(
    r"\b(?:the\s+author|author's|the\s+writer|writer's|i|i'm|i've|i'd|my|me|myself|"
    r"personal\s+(?:story|lesson|experience|journey|decision|reflection)|first[-\s]person|"
    r"(?:their|his|her)\s+own\s+(?:story|decision|mistake|journey|experience|reaction|lesson))\b",
    re.IGNORECASE)
# First-person singular in the post itself: "I", "I'm", "my", "me". Case-sensitive "I" on purpose —
# a lower-case "i" is almost always noise.
_FIRST_PERSON = re.compile(r"\b(?:I|I'm|I've|I'd|I'll|[Mm]y|[Mm]e|[Mm]yself)\b")
_FIRST_PERSON_MIN_HITS = 3
_FIRST_PERSON_MIN_DENSITY = 0.02
_PEOPLE_SCENE = "people_scene"


def avatar_fits_concept(concept: Any, source_text: Optional[str] = None) -> bool:
    """Is this piece about the author, so their likeness belongs in its image?

    True only for a ``people_scene`` whose concept (audience, emotional beat, thesis, rationale,
    chosen idea) names the author or speaks in the first person — or, failing that, whose source
    text is a first-person narrative. No concept is False: without an analysis there is no way to
    tell, and an unneeded likeness is the costlier mistake.

    Args:
        concept: Stage 1's ``ImageConcept`` (duck-typed, so this module never imports the AI stack).
        source_text: The post / carousel / video text the concept was read from.

    Returns:
        Whether the avatar should be in frame.
    """
    if concept is None or getattr(concept, "treatment", None) != _PEOPLE_SCENE:
        return False
    fields = " ".join(str(getattr(concept, name, "") or "") for name in (
        "audience", "emotional_beat", "thesis", "treatment_rationale", "chosen_idea"))
    if _AUTHOR_SIGNAL.search(fields):
        return True
    words = (source_text or "").split()
    hits = len(_FIRST_PERSON.findall(source_text or ""))
    return hits >= _FIRST_PERSON_MIN_HITS and hits / max(len(words), 1) >= _FIRST_PERSON_MIN_DENSITY


# #2316, the owner's rule for people on a cover: "if it references the user (or me, myself, or the
# user's name) then it needs to use their avatar". First-person SINGULAR, and "we/our/us" as the
# author's own firm — the piece is the author's story either way.
_SELF_REFERENCE = re.compile(r"\b(?:I|I'm|I've|I'd|I'll|[Mm]e|[Mm]y|[Mm]yself|[Mm]ine|[Ww]e|"
                             r"[Ww]e're|[Ww]e've|[Ww]e'd|[Oo]ur|[Oo]urs|[Oo]urselves|[Uu]s)\b")
SELF_REFERENCE_CONCEPT = "concept"
SELF_REFERENCE_FIRST_PERSON = "first_person"
SELF_REFERENCE_NAMED = "named"
# A first name shorter than this is too likely to be a common word ("Al", "Ed") to read as a name.
_MIN_NAME_CHARS = 3


def author_names(profile: Any) -> list:
    """The author's full and first name off a profile (duck-typed), most specific first."""
    full = " ".join(str(getattr(profile, "full_name", "") or "").split())
    first = str(getattr(profile, "first_name", "") or "").strip() or (full.split()[0] if full
                                                                       else "")
    return [n for n in dict.fromkeys((full, first)) if len(n) >= _MIN_NAME_CHARS]


def names_author(text: Optional[str], names: Optional[list]) -> bool:
    """Does ``text`` name the author — any of ``names`` as a whole, capitalised word run?"""
    return any(re.search(r"(?<![\w'])" + re.escape(name) + r"(?![\w])", text or "")
               for name in (names or []) if name)


def author_reference(concept: Any, source_text: Optional[str],
                     names: Optional[list] = None) -> str:
    """Does this piece's image subject reference the AUTHOR? Deterministic (#2316).

    Three ways, in order — the reason names which:

    - ``concept``: the concept's subject (thesis, chosen or people idea, audience, beat,
      rationale) speaks of the author ("the author", "I", "my") or names them;
    - ``named``: the text names the author (first or full name from the profile);
    - ``first_person``: the text is first-person about the author — at least
      ``_FIRST_PERSON_MIN_HITS`` self-references ("I", "me", "my", "myself", and "we/our/us" as the
      author's firm) at a density of ``_FIRST_PERSON_MIN_DENSITY``.

    Args:
        concept: Stage 1's concept (duck-typed), or None.
        source_text: The title, subtitle and body the concept was read from.
        names: ``author_names(profile)``.

    Returns:
        The reason, or '' when the piece is not about the author.
    """
    fields = " ".join(str(getattr(concept, name, "") or "") for name in (
        "thesis", "chosen_idea", "people_idea", "audience", "emotional_beat",
        "treatment_rationale")) if concept is not None else ""
    if _AUTHOR_SIGNAL.search(fields) or names_author(fields, names):
        return SELF_REFERENCE_CONCEPT
    if names_author(source_text, names):
        return SELF_REFERENCE_NAMED
    words = (source_text or "").split()
    hits = len(_SELF_REFERENCE.findall(source_text or ""))
    if hits >= _FIRST_PERSON_MIN_HITS and hits / max(len(words), 1) >= _FIRST_PERSON_MIN_DENSITY:
        return SELF_REFERENCE_FIRST_PERSON
    return ""


def _post_explicitly_wants_avatar(post_id: int) -> bool:
    """Did the author tick "use my avatar" on THIS post at compose time? Fails closed (False)."""
    try:
        from cqc_lem.utilities.db import get_post_use_avatar
        return get_post_use_avatar(post_id) is True
    except Exception as e:
        log_debug("Compose-time avatar choice unreadable — applying the fit rule", error=str(e),
                  post_id=post_id, action_type="avatar_guardrail")
        return False


def resolve_avatar_for_concept(user_id: Optional[int], *, surface: str, concept: Any,
                               source_text: Optional[str] = None,
                               post_id: Optional[int] = None) -> Optional[dict]:
    """The avatar when policy allows it AND the piece is about the author; else None.

    The ONE decision for surfaces that have a Stage 1 concept. Policy (``resolve_avatar_for``)
    still decides first; an explicit compose-time opt-in for THIS post skips the fit check.

    Args:
        user_id: The author.
        surface: One of ``AVATAR_SURFACES``.
        concept: Stage 1's analysis of the piece, or None.
        source_text: The text the concept was read from, for the first-person check.
        post_id: The post, for its compose-time choice.

    Returns:
        The avatar dict, or None to render without the likeness.
    """
    avatar = resolve_avatar_for(user_id, surface=surface, post_id=post_id)
    if avatar is None or avatar_fits_concept(concept, source_text):
        return avatar
    if post_id and _post_explicitly_wants_avatar(post_id):
        return avatar
    log_debug("Avatar not used: the piece is not about the author", user_id=user_id,
              post_id=post_id, surface=surface, action_type="avatar_guardrail",
              treatment=getattr(concept, "treatment", None))
    return None
