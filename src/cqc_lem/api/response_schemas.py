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

from datetime import date, datetime
from typing import Any, Dict, List, Literal, Optional, Tuple, Type

from pydantic import BaseModel, ConfigDict, create_model

__all__ = [
    "ActivityEntry",
    "ArtifactCtaAttribution",
    "CatchupPerContactCapBounds",
    "CatchupContactIntervalBounds",
    "DashboardStats",
    "DmTemplate",
    "EngagementTarget",
    "EngagementTargetSuggestion",
    "EngagementTargetsDetail",
    "FeedReach",
    "GateDefaults",
    "GateFinding",
    "GmailForwardConfirmation",
    "GroupPostDraftDetail",
    "LeadMagnetDetail",
    "NewsletterDraftDetail",
    "NewsletterEdition",
    "NewsletterSettingsDetail",
    "NewsletterSubscriberStat",
    "NewsletterSubscribersDetail",
    "PlannedTask",
    "PlannedTasksDetail",
    "PostSummary",
    "PostsPage",
    "StoryBankDetail",
    "StoryEntry",
    "SubscriptionSummary",
    "UserGroup",
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


class EngagementTarget(BaseModel):
    """One account on the curated engagement roster (#616), as `GET /user/engagement-targets` reads it.

    The field list is the SELECT the reader runs (`db._ENGAGEMENT_TARGET_COLS`), so `week_start` —
    the rolling counter's anchor, which the SPA does not render — is documented rather than dropped:
    it is on the wire either way, and a payload that documents less than it sends is what this
    module exists to stop.

    Everything from `comment_blocked_streak` down is automation-owned (#962, #979): the roster PUT
    writes only the editable fields, so a save can never reset a streak or a follow state.
    """

    id: int
    profile_url: str
    name: Optional[str]
    category: Literal["peer", "icp", "creator"]
    max_comments_per_week: int
    active: bool
    last_engaged_at: Optional[datetime]
    comments_this_week: int
    week_start: Optional[date]
    source: Literal["user", "suggested"]
    comment_blocked_streak: int
    last_blocked_at: Optional[datetime]
    follow_status: Literal["unknown", "not_following", "following", "follow_failed"]
    followed_at: Optional[datetime]
    follow_attempts: int
    connect_status: Literal["unknown", "needs_connection", "requested", "connected", "failed"]
    connect_requested_at: Optional[datetime]


class EngagementTargetSuggestion(BaseModel):
    """A seed candidate for an EMPTY roster — deliberately a narrower shape than a saved row.

    `suggest_engagement_targets` builds these from `post_engagers` and they have never been stored,
    so they carry no `id` and none of the automation-owned counters. Documenting them as full
    targets would generate a type whose `id` the SPA would read as a number and get `undefined`.
    """

    profile_url: str
    name: Optional[str]
    category: Literal["peer", "icp", "creator"]
    max_comments_per_week: int
    active: bool
    source: Literal["user", "suggested"]


class EngagementTargetsDetail(BaseModel):
    """`detail` of `GET /user/engagement-targets` — the saved roster plus seeds for an empty one."""

    targets: List[EngagementTarget]
    suggestions: List[EngagementTargetSuggestion]


class StoryEntry(BaseModel):
    """One piece of the user's own raw material (#620), as the story bank reads it.

    `used_count` / `last_used_at` are the rotation counters generation writes; they are read-only
    here, and the SELECT (`db._STORY_BANK_COLS`) is what this list is taken from.
    """

    id: int
    kind: Literal["anecdote", "number", "opinion", "client_win", "mistake", "artifact"]
    title: Optional[str]
    body: str
    happened_at: Optional[date]
    used_count: int
    last_used_at: Optional[datetime]
    active: bool


class StoryBankDetail(BaseModel):
    """`detail` of `GET /user/story-bank` — the entries plus what a seeded bank means.

    `kinds` is `db.STORY_BANK_KINDS` verbatim, so the SPA's picker cannot offer a kind the writer
    would fall back to `anecdote` on.
    """

    entries: List[StoryEntry]
    kinds: List[str]
    target_entries: int


class DmTemplate(BaseModel):
    """One rung of a DM template ladder (event type + step), as `GET /user/dm-templates` reads it.

    Every field is a stored column and the PUT replaces the whole set, so none of them is optional:
    a key the generated type let a caller drop is a column a save would blank.
    """

    event_type: str
    step: int
    delay_hours: int
    template_text: str
    is_active: bool


class NewsletterSettingsDetail(BaseModel):
    """`detail` of `GET /user/newsletter-settings` — the whole row, defaults filled in.

    Derived from `_NEWSLETTER_DEFAULTS`: a user with no row gets that dict back verbatim, so the two
    shapes are the same answer and the defaults are the honest field list. `newsletter_url` and
    `last_published_at` are written by the publish run, not by the settings PUT.

    `cadence` stays a plain string: the SPA's `<select>` hands back `string`, and a Literal here
    would document a closed vocabulary that `update_newsletter_settings` does not enforce.
    """

    enabled: bool
    title: Optional[str]
    topic: Optional[str]
    cadence: str
    align_with_blog: bool
    newsletter_url: Optional[str]
    last_published_at: Optional[datetime]
    publish_day: int
    publish_hour: int
    generate_lead_days: int
    max_queued_drafts: int
    invite_connections_enabled: bool
    max_invites_per_run: int
    cover_image_auto: bool
    auto_publish_newsletters: bool


class NewsletterSubscriberStat(BaseModel):
    """One subscriber-growth snapshot (#400).

    `subscriber_count` is NULL when the page could not be read on that run — a different fact from
    zero subscribers, which is why it is nullable rather than defaulted.
    """

    subscriber_count: Optional[int]
    invites_sent: int
    captured_at: datetime


class ArtifactCtaAttribution(BaseModel):
    """Owned-asset CTA deliveries in the window (#624) — what subscriber growth is read against.

    `newsletter_links` is None, not 0, when no subscribe URL is configured: there was nothing to
    carry, which is a different fact from "carried nothing".
    """

    window_days: int
    lead_magnet_dms: int
    newsletter_links: Optional[int]


class NewsletterSubscribersDetail(BaseModel):
    """`detail` of `GET /user/newsletter-subscribers` — the growth series and its attribution."""

    latest: Optional[int]
    history: List[NewsletterSubscriberStat]
    attribution: ArtifactCtaAttribution


class NewsletterEdition(BaseModel):
    """One queued edition in the review queue.

    The stored `cover_image_path` is popped by the handler and replaced with `cover_image_url`
    (#893): the SPA renders the cover from a URL and must never be handed a server path.
    """

    id: int
    title: Optional[str]
    subtitle: Optional[str]
    subject: Optional[str]
    format: Optional[str]
    hook_style: Optional[str]
    body: Optional[str]
    status: str
    scheduled_for: Optional[str]
    cover_image_url: Optional[str]
    cover_image_source: Optional[Literal["upload", "ai"]]
    cover_image_status: Optional[Literal["pending_review", "approved"]]


class NewsletterDraftDetail(BaseModel):
    """`detail` of `GET /user/newsletter-draft`.

    `next_publish` is the slot AFTER the last edition already queued — when a NEW draft would go
    out, not when the next send is.

    `auto_publish_newsletters` rides along from the user's settings so the queue can say what
    actually happens at a draft's slot (publish, or wait for approval) instead of asserting one
    universal truth — it is read here, never written.
    """

    editions: List[NewsletterEdition]
    next_publish: Optional[str]
    max_queued_drafts: int
    generate_lead_days: int
    auto_publish_newsletters: bool


class UserGroup(BaseModel):
    """One of the user's LinkedIn groups and its per-group toggles.

    `enabled` (commenting) and `post_enabled` (publishing) are independent on purpose — being in a
    group is not permission to publish into it. `is_next_post` is marked ON the row rather than
    returned beside the list, so an SPA bundle open from before a deploy still reads `detail` as the
    plain array it expects (#743).
    """

    group_id: str
    group_name: Optional[str]
    enabled: bool
    post_enabled: bool
    last_posted_at: Optional[str]
    is_next_post: bool


class GroupPostDraftDetail(BaseModel):
    """The weekly group post waiting to be published (#932, #1224, #1415).

    `detail` is null in full when nothing is queued, so every field here is required: the handler
    reads one row and then always adds the three derived keys.

    `media_url` is null on a text-only draft and `media_type` says which kind of media is attached
    when it is not (#1443). `can_undo_skip` is whether "Skip this week" can still be reversed, and
    `undo_deadline` is the publish slot that window closes at.
    """

    id: int
    user_id: int
    group_id: str
    group_name: Optional[str]
    content: str
    media_url: Optional[str]
    media_type: Optional[Literal["image", "video"]]
    status: str
    created_at: Optional[str]
    updated_at: Optional[str]
    published_at: Optional[str]
    best_practices: List[str]
    undo_deadline: Optional[str]
    can_undo_skip: bool


class LeadMagnetDetail(BaseModel):
    """`detail` of `GET /user/lead-magnet` — the comment keyword and the DM it pays out (#624)."""

    enabled: bool
    keyword: Optional[str]
    message: Optional[str]


class DashboardStats(BaseModel):
    """`detail` of `GET /dashboard/stats/` — the three headline counters, SQL aggregates over all posts."""

    scheduled_this_week: int
    pending_review: int
    posted_total: int


class PlannedTask(BaseModel):
    """One upcoming item on the dashboard's forward half, from whichever queue it came out of.

    `scheduled_time` is an explicit-UTC ISO string (`_utc_iso`) so the browser localizes it instead
    of reading a naive value as local.
    """

    kind: Literal["Post", "DM", "Newsletter"]
    id: int
    title: str
    status: str
    scheduled_time: Optional[str]


class PlannedTasksDetail(BaseModel):
    """`detail` of `GET /dashboard/planned-tasks/`."""

    tasks: List[PlannedTask]


class ActivityEntry(BaseModel):
    """One row of the activity feed — what LEM already did.

    `post_url` goes through `_public_post_url`, so a home-feed comment (logged under a synthetic
    `feedpost://` key, with no LinkedIn permalink) reports null rather than leaking that string.
    """

    id: int
    action_type: str
    result: str
    post_id: Optional[int]
    post_url: Optional[str]
    message: Optional[str]
    created_at: Optional[str]


class GateFinding(BaseModel):
    """One quality-gate result in the shape the review UI renders (#421).

    Every key is written by `quality_gates.build_finding`, which is the only writer of a stored
    `posts.gate_reason` entry — so this is the whole finding, not a subset of it. `demoted` marks
    the findings that actually held the post at PENDING; the rest are advisory notes beside them.
    """

    gate: str
    label: str
    score: Optional[float]
    threshold: Optional[float]
    demoted: bool
    explanation: str
    remediation: str
    details: List[str]


class PostSummary(BaseModel):
    """One row of the Content Studio's paged post list.

    Not the stored row: the handler selects these keys out of it and renames `id` to `post_id`.
    `carousel_slides` is null on anything that is not a deck, and `gate_reason` is the parsed
    findings list (empty, never null, when a draft is being held for nothing).
    """

    post_id: int
    content: str
    video_url: Optional[str]
    image_url: Optional[str]
    scheduled_time: Optional[str]
    post_type: str
    status: str
    carousel_slides: Optional[List[str]]
    archetype: Optional[str]
    authenticity_score: Optional[int]
    gate_reason: List[GateFinding]
    rejection_reason: Optional[str]
    manual_publish: bool


class PostsPage(BaseModel):
    """`detail` of `GET /posts/` — one page of the caller's own posts, plus the paging context."""

    posts: List[PostSummary]
    total: int
    page: int
    page_size: int


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
