"""Suggest-only engagement: the ONE place a user's engagement mode is decided (issue #2367).

Trial accounts get engagement as SUGGESTIONS: no Selenium lane runs on their LinkedIn account, and
a lane that would have drafted a comment or a DM stores the draft (`engagement_suggestions`) for the
user to copy and post by hand. Existing accounts are `automate` and behave exactly as before.

Three rules hold everywhere this module is used:

- **Fail closed.** Only a stored, readable `automate` allows a browser. A missing row, a DB fault,
  or any value this module does not recognise reads as `suggest` — the cost of a wrong `suggest` is
  a skipped run, while the cost of a wrong `automate` is a browser on an account that never agreed
  to one.
- **Two layers.** Every Selenium lane task asks `skip_browser_lane` before it does anything, and
  logs the skip at DEBUG (it is an expected no-op). `get_docker_driver` asks
  `require_browser_automation` as well, so a lane that forgot the first check still cannot open a
  session for a suggest user — it raises `SuggestOnlyEngagement` instead.
- **Not a feature flag.** This is a per-account safety control stored in the database. It never
  falls back to an environment variable and is never read from PostHog (`utilities/flags.py`).

API publishing (`post_to_linkedin`, the OAuth `w_member_social` path) is not a browser and is not
gated here. Full posture: `docs/engagement-automation.md` § Engagement mode.
"""

import hashlib
from typing import Optional

from cqc_lem.utilities.db import (
    EngagementMode,
    EngagementSuggestionKind,
    get_user_engagement_mode,
    has_engagement_suggestion,
    insert_engagement_suggestion,
)
from cqc_lem.utilities.logger import log_debug, log_info, log_warning

#: What a skipped lane task returns, so a Celery result reads the same on every lane.
SUGGEST_ONLY_SKIP_MESSAGE = "Skipped — suggest-only engagement mode"
#: What a send task returns when it stored its draft as a suggestion instead of sending it.
SUGGESTION_SAVED_MESSAGE = "Saved as a suggestion — suggest-only engagement mode"


class SuggestOnlyEngagement(RuntimeError):
    """A browser session was requested for a user whose engagement mode does not allow one.

    Raised by `get_docker_driver` — the backstop behind every lane's own `skip_browser_lane`
    check. Reaching it means a lane skipped that check, which is a defect in the lane, not in the
    account.
    """


def resolve_engagement_mode(user_id: Optional[int]) -> EngagementMode:
    """The user's engagement mode, read fail-closed.

    Args:
        user_id: the account. None (no account at all) is `suggest`.

    Returns:
        `EngagementMode.AUTOMATE` only when the stored value is exactly `automate`; otherwise
        `EngagementMode.SUGGEST`.
    """
    if user_id is None:
        return EngagementMode.SUGGEST
    try:
        raw = get_user_engagement_mode(user_id)
    except Exception as e:
        # The reader catches MySQL errors itself; anything reaching here (an unset DB port's
        # TypeError, say) is a fault the reader could not see. Still fail closed.
        log_warning("Engagement mode unreadable — treating the account as suggest-only", exc=e,
                    user_id=user_id)
        return EngagementMode.SUGGEST
    if raw is not None and str(raw) == EngagementMode.AUTOMATE.value:
        return EngagementMode.AUTOMATE
    return EngagementMode.SUGGEST


def browser_automation_allowed(user_id: Optional[int]) -> bool:
    """True only when the user's engagement mode is a readable `automate`."""
    return resolve_engagement_mode(user_id) == EngagementMode.AUTOMATE


def is_suggest_only(user_id: Optional[int]) -> bool:
    """True when the account's engagement is suggestions only.

    The negation of `browser_automation_allowed`, named for the API-driven lanes that store a draft
    rather than skip (the seed and second-wave comments).
    """
    return not browser_automation_allowed(user_id)


def skip_browser_lane(user_id: Optional[int], task_name: str) -> bool:
    """True when a Selenium lane must not run for this user; logs the skip at DEBUG.

    Args:
        user_id: the account the lane would run as.
        task_name: the lane, for the log line.

    Returns:
        True for a suggest-mode (or unreadable) account — the caller returns without touching
        LinkedIn. False for an `automate` account.
    """
    if browser_automation_allowed(user_id):
        return False
    # DEBUG: a suggest-mode account skipping every browser lane is the mode working, not a fault.
    log_debug(f"{task_name} skipped — suggest-only engagement mode", user_id=user_id,
              task_name=task_name)
    return True


def require_browser_automation(user_id: Optional[int], session_name: str) -> None:
    """Raise `SuggestOnlyEngagement` unless the user may have a browser session.

    Args:
        user_id: the account the session would run as.
        session_name: the session label, for the error.

    Raises:
        SuggestOnlyEngagement: the account is suggest-only, or its mode cannot be read.
    """
    if not browser_automation_allowed(user_id):
        raise SuggestOnlyEngagement(
            f"Browser session '{session_name}' refused — the account is suggest-only")


def dm_suggestion_key(profile_url: str, message: str) -> str:
    """The dedup identity of a DM suggestion: one per recipient AND message text.

    A different message to the same person is a different draft; the same message re-dispatched is
    not. Hashed so a long body cannot overflow the key column.
    """
    digest = hashlib.sha256((message or "").strip().encode("utf-8")).hexdigest()[:16]
    return f"dm:{profile_url}:{digest}"


def suggestion_exists(user_id: int, dedup_key: str) -> bool:
    """Whether this draft is already stored — so a re-dispatched lane can skip its LLM call.

    An unreadable answer is True: the cost of a missed suggestion is one draft, while the cost of
    regenerating on every retry is an LLM call per retry for a row the INSERT would then ignore.
    """
    return has_engagement_suggestion(user_id, dedup_key) is not False


def save_suggestion(user_id: int, kind: EngagementSuggestionKind, source: str, body: str,
                    dedup_key: str, target_url: Optional[str] = None) -> Optional[bool]:
    """Store a draft the user can copy, in place of sending it.

    Args:
        user_id: whose draft it is.
        kind: comment, reply or dm.
        source: the lane that drafted it.
        body: the draft text.
        dedup_key: identity of the draft — the same key twice stores one row.
        target_url: the post or profile it is meant for, when there is one.

    Returns:
        True when stored, False when it was already stored, None when the write failed (the
        repository logs that).
    """
    if not (body or "").strip():
        log_debug("Empty draft — no suggestion stored", user_id=user_id, task_name=source)
        return False
    stored = insert_engagement_suggestion(user_id, kind, source, body, dedup_key,
                                          target_url=target_url)
    if stored:
        log_info(f"Engagement suggestion stored ({kind})", user_id=user_id, task_name=source)
    return stored
