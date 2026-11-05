"""Suggest-only engagement: the ONE place a user's engagement mode is decided (issue #2367).

Trial accounts get engagement as SUGGESTIONS: no Selenium lane runs on their LinkedIn account, and
a lane that would have drafted a comment or a DM stores the draft (`engagement_suggestions`) for the
user to copy and post by hand.

Three rules hold everywhere this module is used:

- **Fail closed.** A browser is allowed only when the stored mode reads `automate` AND the account
  is not on a trial (`subscription_status = 'trial'` or `subscription_tier = 'free_trial'`). A trial
  account is suggest-only whatever its stored mode says — which is what covers the trial accounts
  that existed before the column did and so took its `automate` default. A missing row, a DB fault,
  or any value this module does not recognise reads as `suggest`: the cost of a wrong `suggest` is a
  skipped run, while the cost of a wrong `automate` is a browser on an account that never agreed to
  one.
- **Two layers.** Every Selenium lane task asks `skip_browser_lane` before it does anything (INFO
  once per account per day, DEBUG otherwise). `get_docker_driver` asks `require_browser_automation`
  as well, so a lane that forgot the first check still cannot open a session for a suggest user —
  it raises `SuggestOnlyEngagement` instead.
- **Not a feature flag.** This is a per-account safety control stored in the database. It never
  falls back to an environment variable and is never read from PostHog (`utilities/flags.py`).

A **sandbox** account (`users.account_kind = 'sandbox'`, set only by the owner with
`scripts/set_account_kind.py`) is held to the same gate and then some: it never acts on LinkedIn, by
any route. Its mode reads `suggest` whatever is stored, so every browser lane skips and
`get_docker_driver` refuses it; `guard_send` refuses it at the API publish entry points; and the
queue-fed send lanes (`send_scheduled_dm`, `send_connection_request`, `send_catchup_touch`,
`send_lead_response`, `post_to_linkedin`) ask `sandbox_dry_run` first, which writes the row's
terminal state WITHOUT any LinkedIn call, so an end-to-end QA run completes. Only `standard` and
`internal` may send: an unreadable kind, a missing `account_kind`, and any value this module does
not recognise all read as "do not send". `tests/unit/test_sandbox_send_invariants.py` fails the build
when a send path is added without the gate. Posture: `docs/agent-roles.md` § Sandbox tenant.

API publishing (`post_to_linkedin`, the OAuth `w_member_social` path) is not a browser, so the
suggest-only mode does not gate it for a trial account; the sandbox gate does (`guard_send`). Full
posture: `docs/engagement-automation.md` § Engagement mode.
"""

import hashlib
from typing import Optional

from cqc_lem.utilities.db import (
    EngagementMode,
    EngagementSuggestionKind,
    get_user_engagement_state,
    has_engagement_suggestion,
    insert_engagement_suggestion,
)
from cqc_lem.utilities.logger import log_debug, log_info, log_warning

#: What a skipped lane task returns, so a Celery result reads the same on every lane.
SUGGEST_ONLY_SKIP_MESSAGE = "Skipped — suggest-only engagement mode"
#: What a send task returns when it stored its draft as a suggestion instead of sending it.
SUGGESTION_SAVED_MESSAGE = "Saved as a suggestion — suggest-only engagement mode"
#: What a draft-storing lane returns when it could not read the mode, and so did nothing.
ENGAGEMENT_MODE_UNREADABLE_MESSAGE = "Deferred — engagement mode unreadable"
#: What a queue-fed send lane returns when it completed a row as a dry run on a sandbox account.
SANDBOX_DRY_RUN_MESSAGE = "Dry run — sandbox account, nothing was sent"

#: `users.account_kind` values. Only the first two may send; `sandbox` never does.
ACCOUNT_KIND_STANDARD = "standard"
ACCOUNT_KIND_INTERNAL = "internal"
ACCOUNT_KIND_SANDBOX = "sandbox"
#: The kinds that may act on LinkedIn. Matched EXACTLY, so `'Sandbox'`, `'sandbox '` and any typo are
#: not in it and read as "do not send". NOTE the direction: `get_user_account_kind` (the permissions
#: reader) reads an unknown value as `standard`, the safe direction for APPROVALS; for SENDS the safe
#: direction is the opposite, so this set is an allow-list rather than "not sandbox".
SENDING_ACCOUNT_KINDS = frozenset({ACCOUNT_KIND_STANDARD, ACCOUNT_KIND_INTERNAL})


class SandboxAccountRefused(RuntimeError):
    """An API publish was attempted for an account that may not send (sandbox, or kind unreadable).

    Raised by `guard_send`, the entry check of every LinkedIn API publish helper. Reaching it from a
    lane means the lane did not ask `sandbox_dry_run` first, which is a defect in the lane.
    """


class SuggestOnlyEngagement(RuntimeError):
    """A browser session was requested for a user whose engagement mode does not allow one.

    Raised by `get_docker_driver` — the backstop behind every lane's own `skip_browser_lane`
    check. Reaching it means a lane skipped that check, which is a defect in the lane, not in the
    account.
    """


#: The subscription markers of a trial account. Either one makes the account suggest-only.
TRIAL_SUBSCRIPTION_STATUS = "trial"
TRIAL_SUBSCRIPTION_TIER = "free_trial"
#: How long the once-a-day INFO skip claim lives — a little over a day, so it never lapses early.
_SKIP_LOG_CLAIM_TTL_SECONDS = 26 * 60 * 60


def _norm(value: object) -> str:
    return str(value).strip().lower() if value is not None else ""


def _is_trial(state: dict) -> bool:
    return (_norm(state.get("subscription_status")) == TRIAL_SUBSCRIPTION_STATUS
            or _norm(state.get("subscription_tier")) == TRIAL_SUBSCRIPTION_TIER)


def _read_state(user_id: Optional[int]) -> Optional[dict]:
    """The engagement state row, or None for no account, a read fault or a malformed reading."""
    if user_id is None:
        return None
    try:
        state = get_user_engagement_state(user_id)
    except Exception as e:
        # The reader catches MySQL errors itself; anything reaching here (an unset DB port's
        # TypeError, say) is a fault the reader could not see.
        log_warning("Engagement mode unreadable — treating the account as suggest-only", exc=e,
                    user_id=user_id)
        return None
    if not isinstance(state, dict) or not {"engagement_mode", "subscription_status",
                                           "subscription_tier", "account_kind"} <= state.keys():
        # A reading that cannot say whether the account is on a trial, or what KIND of account it
        # is, is not a reading at all.
        return None
    return state


def read_account_kind(user_id: Optional[int]) -> Optional[str]:
    """The account's `account_kind` exactly as stored, or None when it could not be READ.

    Three-valued on purpose. `get_user_account_kind` collapses "unreadable" into `standard` because
    that is the safe answer for a PERMISSION; for a SEND it is the unsafe one, so this reader keeps
    the difference and every send-side caller treats None as "do not send".
    """
    state = _read_state(user_id)
    return None if state is None else state.get("account_kind")


def may_send(user_id: Optional[int]) -> bool:
    """True only when the account's kind is readable and is `standard` or `internal`."""
    return read_account_kind(user_id) in SENDING_ACCOUNT_KINDS


def is_sandbox_account(user_id: Optional[int]) -> bool:
    """True only when the account's kind is READABLE and is exactly `sandbox`.

    Deliberately not `not may_send(...)`: an unreadable kind is not a sandbox account and must not be
    completed as a dry run (that would write a terminal state for a row that may be a real send);
    it falls through to the gates, which refuse it.
    """
    return read_account_kind(user_id) == ACCOUNT_KIND_SANDBOX


def sandbox_dry_run(user_id: Optional[int], task_name: str) -> bool:
    """True when `task_name` for this account is a dry run, and logs it.

    The queue-fed send lanes call this BEFORE `skip_browser_lane`. True means: the caller writes the
    row's terminal state and returns `SANDBOX_DRY_RUN_MESSAGE` without touching LinkedIn. False
    means: carry on to the normal gates, which stop an unreadable or unrecognised kind.

    Args:
        user_id: the account the send would go out from.
        task_name: the lane, for the log line.
    """
    if not is_sandbox_account(user_id):
        return False
    log_info(f"{task_name}: sandbox account, completed as a dry run; nothing was sent",
             user_id=user_id, task_name=task_name, action_type="sandbox_dry_run")
    return True


def guard_send(user_id: Optional[int], surface: str) -> None:
    """Raise `SandboxAccountRefused` unless this account may act on LinkedIn.

    The entry check of every LinkedIn API publish helper (`poster.py`, `reshare.py`): the API path is
    not a browser, so `get_docker_driver`'s backstop never sees it. Fails closed — an unreadable kind,
    a missing account and an unrecognised value all refuse.

    Args:
        user_id: the account the publish would go out as.
        surface: the helper, for the error and the log line.

    Raises:
        SandboxAccountRefused: the account is `sandbox`, or its kind cannot be read.
    """
    if may_send(user_id):
        return
    log_info(f"{surface}: refused — account may not act on LinkedIn", user_id=user_id,
             task_name=surface, action_type="sandbox_refused")
    raise SandboxAccountRefused(f"{surface} refused — the account may not act on LinkedIn")


def read_engagement_mode(user_id: Optional[int]) -> Optional[EngagementMode]:
    """The user's engagement mode, or None when it could not be READ.

    Lanes that store a draft instead of skipping (the seed and second-wave self-comments) need this
    three-valued answer: a read fault must defer them, never turn an `automate` account's live
    comment into a stored suggestion. Everything else wants `resolve_engagement_mode`.

    Args:
        user_id: the account. None (no account at all) reads as None.

    Returns:
        `AUTOMATE` only when the stored mode is exactly `automate`, the account kind is `standard` or
        `internal`, and the account is not on a trial; `SUGGEST` for any other readable state
        (a `sandbox` account included); None for no account, a missing row or a read fault.
    """
    state = _read_state(user_id)
    if state is None:
        return None
    # A sandbox account (or any kind this module does not recognise) never gets a browser, whatever
    # its stored mode says — exact match, fail closed.
    if state.get("account_kind") not in SENDING_ACCOUNT_KINDS:
        return EngagementMode.SUGGEST
    # Exact match on the mode (fail closed); the trial markers below are normalised instead,
    # because a looser trial match can only widen suggest.
    if state.get("engagement_mode") != EngagementMode.AUTOMATE.value:
        return EngagementMode.SUGGEST
    if _is_trial(state):
        # Trial = suggestions only, whatever the stored mode says (owner ruling, #2367).
        return EngagementMode.SUGGEST
    return EngagementMode.AUTOMATE


def resolve_engagement_mode(user_id: Optional[int]) -> EngagementMode:
    """The user's engagement mode, read fail-closed: an unreadable state is `SUGGEST`.

    Args:
        user_id: the account. None (no account at all) is `suggest`.

    Returns:
        `EngagementMode.AUTOMATE` only for a readable, non-trial `automate` account; otherwise
        `EngagementMode.SUGGEST`.
    """
    return read_engagement_mode(user_id) or EngagementMode.SUGGEST


def browser_automation_allowed(user_id: Optional[int]) -> bool:
    """True only when the user's engagement mode is a readable `automate`."""
    return resolve_engagement_mode(user_id) == EngagementMode.AUTOMATE


def is_suggest_only(user_id: Optional[int]) -> bool:
    """True when the account's engagement is suggestions only.

    The negation of `browser_automation_allowed`, named for the API-driven lanes that store a draft
    rather than skip (the seed and second-wave comments).
    """
    return not browser_automation_allowed(user_id)


def _claim_daily_skip_log(user_id: Optional[int]) -> bool:
    """True when this is the first lane skip logged for the account today (a Redis claim).

    Fails to False — DEBUG — when Redis is unavailable or the claim errors, so a Redis outage can
    never turn every skip into an INFO line.
    """
    if user_id is None:
        return False
    try:
        from datetime import datetime, timezone

        from cqc_lem.utilities.linkedin.rate_limit import shared_redis_client

        client = shared_redis_client()
        if client is None:
            return False
        day = datetime.now(timezone.utc).date().isoformat()
        return bool(client.set(f"engagement_mode:skip_logged:{user_id}:{day}", "1", nx=True,
                               ex=_SKIP_LOG_CLAIM_TTL_SECONDS))
    except Exception:
        return False


def skip_browser_lane(user_id: Optional[int], task_name: str) -> bool:
    """True when a Selenium lane must not run for this user, and logs the skip.

    The first skip per account per UTC day logs at INFO, so an account wrongly left in `suggest`
    leaves one trace in the production log (which runs at INFO); every other skip is DEBUG. Neither
    is a warning — a suggest-mode account skipping every browser lane is the mode working.

    Args:
        user_id: the account the lane would run as.
        task_name: the lane, for the log line.

    Returns:
        True for a suggest-mode (or unreadable) account — the caller returns without touching
        LinkedIn. False for an `automate` account.
    """
    if browser_automation_allowed(user_id):
        return False
    (log_info if _claim_daily_skip_log(user_id) else log_debug)(
        f"{task_name} skipped — suggest-only engagement mode", user_id=user_id, task_name=task_name)
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


def _digest(text: Optional[str], length: int) -> str:
    return hashlib.sha256((text or "").strip().encode("utf-8")).hexdigest()[:length]


def comment_suggestion_key(post_url: str) -> str:
    """The dedup identity of a comment suggestion: one per post.

    The URL is hashed, so the key is a fixed 41 characters and a long permalink can never be
    truncated at the column's 255 into a key that collides with another post's.
    """
    return f"comment:{_digest(post_url, 32)}"


def dm_suggestion_key(profile_url: str, message: str) -> str:
    """The dedup identity of a DM suggestion: one per recipient AND message text.

    A different message to the same person is a different draft; the same message re-dispatched is
    not. Both halves are hashed, so the key is a fixed length and truncation cannot drop either.
    """
    return f"dm:{_digest(profile_url, 32)}:{_digest(message, 16)}"


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
