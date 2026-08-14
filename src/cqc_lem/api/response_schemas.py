"""What a narrowed route's `detail` actually contains — for the PUBLISHED SCHEMA ONLY (#1446).

`ResponseModel[T]` (#1219) is parametrized with CONTAINER types because FastAPI serializes the
response THROUGH the annotation: a model with named fields there would silently drop every key it
does not declare. So `/api/openapi.json` documents most payloads as a bare object, and the SPA's
generated types bottom out at `Record<string, unknown>`.

The models here narrow that WITHOUT touching the wire. They are handed to FastAPI as
``responses={200: {"model": ResponseModel[X]}}``, which sets what the operation DOCUMENTS and is
never used to serialize anything — the handler keeps returning its own dict, key for key,
including keys no model here declares. `tests/unit/api/test_response_schemas.py` pins both halves:
the bytes are unchanged, and every field declared here is one the handler really returns.

Three rules for anything added below:

* **Never invent a field.** A documented key the handler does not return is worse than an
  undocumented one — the SPA generates a type from it and reads `undefined`.
* **A key the handler ALWAYS returns is REQUIRED, even when its value is None.** `= None` on a
  field does not mean "nullable", it means "may be absent", and the generated TypeScript spells
  that `key?:` — which lets a caller building the whole-row PUT body drop the key entirely. That
  is the shape of the bug this file exists downstream of (#1446's `post_types`). Nullable and
  always-present is `Optional[X]` with NO default.
* **A Redis-backed record allows extras** (`extra="allow"`). Those payloads are written elsewhere
  and grow keys without this module noticing; `additionalProperties: true` is what keeps the
  generated TypeScript honest about that instead of pretending the list is closed. Their fields
  are the ones genuinely allowed to be absent, so they keep their `= None`.
"""

from typing import Any, Dict, Literal, Optional, Tuple, Type

from pydantic import BaseModel, ConfigDict, create_model

__all__ = [
    "CatchupPerContactCapBounds",
    "CatchupContactIntervalBounds",
    "FeedReach",
    "GateDefaults",
    "GmailForwardConfirmation",
    "SubscriptionSummary",
    "UserPreferencesDetail",
    "UserSettingsDetail",
    "detail_model_from",
]


class GateDefaults(BaseModel):
    """The deploy-wide quality-gate thresholds a user who has not overridden them gets (#421)."""

    authenticity_score_min: int
    post_similarity_max_pct: int


class CatchupContactIntervalBounds(BaseModel):
    """Bounds the SPA clamps `min_catchup_contact_interval_days` to (#1078)."""

    min_days: int
    max_days: int


class CatchupPerContactCapBounds(BaseModel):
    """Bounds the SPA clamps `max_catchup_touches_per_contact_days` to (#1078)."""

    min: int
    max: int


class GmailForwardConfirmation(BaseModel):
    """Where the user got to confirming Gmail forwarding — a Redis record, so extras are allowed.

    `source` says how it was proven: `auto_click` (we clicked Gmail's verify link) or
    `forwarded_email` (a LinkedIn notification actually reached the forwarding address). `code` is
    dropped from the record once `code_expires_at` passes, because Gmail stops accepting it.
    """

    model_config = ConfigDict(extra="allow")

    code: Optional[str] = None
    code_expires_at: Optional[int] = None
    confirmed: Optional[bool] = None
    url_found: Optional[bool] = None
    forwarded_to_user: Optional[bool] = None
    source: Optional[str] = None


class FeedReach(BaseModel):
    """The last feed scan's reach funnel — examined → passed filters → matched → commented.

    Written by the feed walk into Redis (`set_feed_funnel`), which records considerably more than
    the Settings hub reads; the fields below are the ones a client is documented to rely on, and
    `extra="allow"` says the rest are there.

    `feed_sort` is the run's sort state (#817): only `recent` means LEM's recency-first scoring saw
    a recency-ordered feed, and `n/a` is a surface that never had a sort control at all.
    """

    model_config = ConfigDict(extra="allow")

    examined: int
    passed_filters: int
    matched_topics: int
    commented: int
    fallback_used: bool
    roster_commented: Optional[int] = None
    feed_commented: Optional[int] = None
    roster_targets_visited: Optional[int] = None
    roster_examined: Optional[int] = None
    off_topic_skipped: Optional[int] = None
    max_post_age_hours: Optional[int] = None
    min_reactions: Optional[int] = None
    feed_sort: Optional[Literal["recent", "top", "missing", "unknown", "n/a"]] = None
    at: Optional[str] = None


class SubscriptionSummary(BaseModel):
    """The Account page's subscription block — null in full when the user has no subscription row.

    Every key is written unconditionally by the handler's literal, so every one is required and
    nullable rather than optional: absent and null are different answers to the SPA.
    """

    status: Optional[str]
    tier: Optional[str]
    trial_started_at: Optional[str]
    trial_ends_at: Optional[str]
    stripe_customer_id: Optional[str]


class UserPreferencesDetail(BaseModel):
    """Account-level preferences — the five columns the Account page edits, plus the derived one.

    `content_language` and `effective_content_language` are both returned on purpose (#548): the
    explicit setting (None = follow Login Location) and what generation will actually use.

    All six are required: `PUT /user/settings` writes the whole object, so a key the SPA is
    allowed to omit is a column a partial save resets.
    """

    last_login_inactivate_delay: Optional[int]
    auto_schedule_posts: bool
    content_buffer_days: int
    content_buffer_max_posts: int
    content_language: Optional[str]
    effective_content_language: Optional[str]


class UserSettingsDetail(BaseModel):
    """`detail` of `GET /user/settings` — subscription, preferences, blog/sitemap and company page.

    The handler returns one literal with all five keys on every path; `subscription` and
    `preferences` are null for a user who has no such row, which is not the same as missing.
    """

    subscription: Optional[SubscriptionSummary]
    preferences: Optional[UserPreferencesDetail]
    blog_url: Optional[str]
    sitemap_url: Optional[str]
    company_linked_in_url: Optional[str]


def detail_model_from(name: str, source: Type[BaseModel], *, drop: Tuple[str, ...] = (),
                      extras: Optional[Dict[str, Any]] = None, doc: str = "") -> Type[BaseModel]:
    """Build a response model from a REQUEST model's fields, so the two cannot drift.

    A route whose payload is "the row you can PUT back, plus some read-only context" is documented
    by reusing the request model's field names and annotations rather than restating 45 of them —
    a restatement is a second list to keep in step, and the one that goes stale is always the one
    nobody serializes through.

    Only the annotations are carried over. The request model's `max_length=` bounds are input
    validation kept in lockstep with the DB column widths; documenting them on a RESPONSE would
    claim the server truncates, which it does not.

    Args:
        name: The generated model's name — it becomes the component name in the schema, and the
            TypeScript type name the SPA imports.
        source: The request model to take field names and annotations from.
        drop: Field names to leave out (`session_token` is a credential, never a response field).
        extras: Extra `field_name -> (annotation, default)` pairs for the read-only context the
            handler adds on top of the row.
        doc: Docstring for the generated model — it becomes the schema's `description`.

    Returns:
        A pydantic model with one required field per carried-over annotation.
    """
    fields: Dict[str, Any] = {
        field_name: (field.annotation, ...)
        for field_name, field in source.model_fields.items()
        if field_name not in drop
    }
    fields.update(extras or {})
    model = create_model(name, **fields)
    model.__doc__ = doc or f"Response payload derived from {source.__name__}."
    return model
