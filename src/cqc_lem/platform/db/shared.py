"""What more than one side of the db.py split needs.

`utilities/db.py` re-exports every name here, and a repository module imports it directly. That is
the whole point: a name read by BOTH a repository and something still in db.py cannot live in
either, or the facade would have to import the repository and the repository the facade.

Nothing here does I/O. The two types are load-bearing beyond their shape:

* `OwnershipUnprovable` is raised when the ownership query did not RUN (issue #914). Callers catch it
  by identity, and a re-export is the same object, so `except db.OwnershipUnprovable` keeps working.
* `BlockedVisit` is what one recorded blocked visit returns.

Some entries are transitional. `_FEEDBACK_COLUMNS` is private to the feedback aggregate and sits here
only because a function that has not moved yet still reads it; when the split finishes, a name read
by exactly one aggregate belongs in that aggregate's module, not here. Issue #1154.
"""

from typing import NamedTuple

from cqc_lem.platform.db.enums import ConnectStatus, FollowStatus, OnboardingStep

# Ordered checklist + the onboarding_state column that timestamps each step's first completion.

ONBOARDING_STEPS: tuple = tuple(OnboardingStep)


class OwnershipUnprovable(Exception):
    """The ownership query did not run, so nothing was proved either way (issue #914).

    Distinct from `user_owns_posts` answering False, which means the query DID run and disproved
    ownership. Both refuse the action — that is the fail-closed half and it is not negotiable — but
    they are not the same fact and must not be reported as the same one: "Forbidden" tells a user
    they lack permission to their own drafts, and sends on-call hunting an authorisation bug while
    the database is the thing that is down.
    """
# Rolling forward buffer of ready posts (issue #544). Generation is bounded on purpose: a user
# always has a few days of approve-able content ahead, but we never generate a large forward
# batch, so a cancelling user leaves at most ~content_buffer_max_posts of wasted LLM/video spend.

DEFAULT_CONTENT_BUFFER_DAYS = 5

DEFAULT_CONTENT_BUFFER_MAX_POSTS = 5
# Hard ceilings on the per-user knobs — the planning horizon is 30 days, and the ceiling is what
# actually caps forward generation spend, so it is not user-raisable past this.

MAX_CONTENT_BUFFER_DAYS = 30
# What a session token is allowed to do (issue #745, 2c). A `full` session is the browser's; an
# `extension` session belongs to the LinkedIn Connect extension, which can never run a passkey
# ceremony — its step-up happened once, in the SPA, when the token was minted.
#
# A `recovery` session signed in with a recovery code. It is an ordinary session in every way except
# one: it may enrol a factor without first proving one, because its owner is by definition the
# person who no longer has a factor to prove.
#
# An `enroll` session (2c.1, issue #905) is a PIN login that landed after REQUIRE_STRONG_FACTOR_AFTER
# on an account holding no strong factor. It is signed in — the PIN is still a valid bootstrap, so
# nobody is locked out — but it may reach only the enrolment surface until it adds a factor, at
# which point `release_enrollment_scope` promotes it to `full`.

SESSION_SCOPE_FULL = "full"
SESSION_SCOPE_EXTENSION = "extension"
SESSION_SCOPE_RECOVERY = "recovery"

AUTH_FACTOR_PASSKEY = "passkey"

VALID_VIDEO_QUALITIES = ("standard", "premium", "premium_top")

ENGAGEMENT_TARGET_WEEKLY_DEFAULT = 2

ENGAGEMENT_TARGET_CONNECT_STATUSES = frozenset(ConnectStatus)

ENGAGEMENT_TARGET_FOLLOW_STATUSES = frozenset(FollowStatus)


class BlockedVisit(NamedTuple):
    """What one recorded blocked visit left behind: the new streak, and the target's connect state
    AFTER the escalation check ran. Both come out of the same statement, so a caller can never
    report a streak the escalation disagrees with.
    """
    streak: int
    connect_status: str
# avatar_trainings.approval_status — the preview/approval gate (issue #744). A freshly trained
# avatar is 'pending' until the user has seen its sample renders and said yes.

AVATAR_APPROVAL_PENDING = "pending"
# Clustering convention (issue #498): a cluster is identified by the `feedback.id` of its SEED row —
# the first report that got an issue filed. The seed carries `cluster_id = id`; every later duplicate
# copies that cluster_id and the seed's github_issue_number. No extra table, so "one recurring
# problem = one issue" is a self-join instead of a schema change.

_FEEDBACK_COLUMNS = ("id, user_id, source, type_hint, body, context_json, embedding, cluster_id, "
                     "github_issue_number, status, sentiment, reviewed_by, reviewed_at, created_at")
# Everything here exists to be imported elsewhere, so nothing in this module reads it and
# CodeQL's py/unused-global-variable fires on the definitions. `__all__` is the one declaration
# ruff (F401) and CodeQL both understand — the same fix #1159 applied to db.py, which took that
# file from 21 open alerts to 0. A per-line ruff directive would be invisible to CodeQL.

__all__ = [
    "AUTH_FACTOR_PASSKEY",
    "AVATAR_APPROVAL_PENDING",
    "BlockedVisit",
    "DEFAULT_CONTENT_BUFFER_DAYS",
    "DEFAULT_CONTENT_BUFFER_MAX_POSTS",
    "ENGAGEMENT_TARGET_CONNECT_STATUSES",
    "ENGAGEMENT_TARGET_FOLLOW_STATUSES",
    "ENGAGEMENT_TARGET_WEEKLY_DEFAULT",
    "MAX_CONTENT_BUFFER_DAYS",
    "ONBOARDING_STEPS",
    "OwnershipUnprovable",
    "SESSION_SCOPE_EXTENSION",
    "SESSION_SCOPE_FULL",
    "SESSION_SCOPE_RECOVERY",
    "VALID_VIDEO_QUALITIES",
    "_FEEDBACK_COLUMNS",
]
