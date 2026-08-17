"""`/api/user/*` — the account itself, split from `main.py` (#1154).

Settings, engagement preferences, strong-auth enrolment, newsletter and post authoring, groups,
affiliate and analytics: the biggest of the four slices at 79 routes. The mechanic is the one #1178
established.

The auth kernel stays in `main` and is reached as `_main.get_session_user_id` — an ATTRIBUTE
resolved at REQUEST time, which is what keeps the ~596 patches aimed at `cqc_lem.api.main` binding
what these handlers actually read. Three groups of symbol are reached that way rather than moved:

  * the kernel itself, and the scope/CSRF machinery around it (#914 / #950 / #957 / #905 / #1026);
  * six symbols the `auth` routes also read — `current_session_token`, `_challenge_expiry`,
    `_enrollment_held`, `_passkeys_or_503`, `_verify_assertion_for_user`, `_utc_iso` — which move
    with `auth`, not here, or that slice would be carved out of two modules at once;
  * six more that handlers left behind in `main` still call: `_deny`, `_reject_foreign_email`,
    `_warn_if_naive_schedule`, `_agent_scoped`, `_GMAIL_CONFIRM_KEY` and
    `get_gmail_forward_confirmation`. A symbol with a caller on both sides must have exactly one
    home, and `main` is where its remaining callers already look.

`SessionTokenField` and `_LEN_DM_TEMPLATE` could not be reached either way: both bind inside a
pydantic CLASS BODY, at import time, on BOTH sides of the split, and by then `_main` does not exist
yet. They live in `api/models.py`, which `main` re-exports from.

`from cqc_lem.api import main as _main` sits at the BOTTOM on purpose; `routers/__init__.py` has the
prefix rule and the reasoning.
"""

import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import requests
from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field, field_validator

from cqc_lem.api.models import (
    _LEN_DM_TEMPLATE,
    ResponseModel,
    SessionTokenField,
    error_responses,
)
from cqc_lem.api.response_schemas import (
    CatchupContactIntervalBounds,
    CatchupPerContactCapBounds,
    DmTemplate,
    EngagementTargetsDetail,
    FeedReach,
    GateDefaults,
    GmailForwardConfirmation,
    GroupPostDraftDetail,
    LeadMagnetDetail,
    NewsletterDraftDetail,
    NewsletterSettingsDetail,
    NewsletterSubscribersDetail,
    StoryBankDetail,
    UserGroup,
    UserSettingsDetail,
    detail_model_from,
)
from cqc_lem.app.engagement.posting import update_stale_profile
from cqc_lem.utilities.ai.content_alignment import profile_niche_anchors
from cqc_lem.utilities.ai.content_framework import GROUP_POST_BEST_PRACTICES
from cqc_lem.utilities.auth_factors import (
    METHOD_PASSKEY,
    METHOD_TOTP,
    available_methods,
    begin_totp_enrollment,
    confirm_totp_enrollment,
    enrollment_allowed,
    enrollment_hold_active,
    factor_summary,
    generate_recovery_codes,
    has_confirmed_totp,
    has_strong_factor,
    record_step_up,
    session_signed_in_with_recovery_code,
    step_up_satisfied,
    strong_factor_deadline,
    verify_totp_code,
)
from cqc_lem.utilities.auth_rate_limit import check_auth_init, check_auth_verify
from cqc_lem.utilities.db import (
    CATCHUP_MAX_PER_CONTACT_DAYS_DEFAULT,
    CATCHUP_MAX_PER_CONTACT_DAYS_MAX,
    CATCHUP_MAX_PER_CONTACT_DAYS_MIN,
    CATCHUP_MIN_CONTACT_INTERVAL_DAYS_DEFAULT,
    CATCHUP_MIN_CONTACT_INTERVAL_DAYS_MAX,
    CATCHUP_MIN_CONTACT_INTERVAL_DAYS_MIN,
    CATCHUP_TOUCHES_MAX,
    CATCHUP_TOUCHES_MAX_STANDARD,
    CATCHUP_TOUCHES_MIN,
    COMPANY_PAGE_INVITES_PER_DAY_DEFAULT,
    COMPANY_PAGE_INVITES_PER_DAY_MAX,
    COMPANY_PAGE_INVITES_PER_DAY_MIN,
    DEFAULT_CATCHUP_EVENT_TYPES,
    DEFAULT_CONTENT_BUFFER_DAYS,
    DEFAULT_CONTENT_BUFFER_MAX_POSTS,
    DEFAULT_POSTING_DAYS,
    DEFAULT_POSTS_PER_WEEK,
    ENGAGEMENT_TARGET_CATEGORIES,
    ENGAGEMENT_TARGET_SOURCES,
    ENGAGEMENT_TARGET_WEEKLY_DEFAULT,
    ENGAGEMENT_TARGET_WEEKLY_MAX,
    MAX_CONTENT_BUFFER_DAYS,
    MAX_CONTENT_BUFFER_POSTS,
    POSTS_PER_WEEK_MAX,
    POSTS_PER_WEEK_MIN,
    ROSTER_FOLLOWS_PER_DAY_DEFAULT,
    ROSTER_FOLLOWS_PER_DAY_MAX,
    ROSTER_FOLLOWS_PER_DAY_MIN,
    SESSION_SCOPE_AGENT,
    SESSION_SCOPE_EXTENSION,
    STORY_BANK_KINDS,
    STORY_BANK_TARGET_ENTRIES,
    VALID_CATCHUP_MESSAGE_SOURCES,
    VALID_CATCHUP_TOUCH_MODES,
    AuthAuditEvent,
    CatchupEventType,
    GroupPostDraftStatus,
    GroupPostMediaType,
    PostStatus,
    add_passkey_factor,
    bulk_update_posts,
    change_user_email,
    clear_user_linkedin_password,
    consume_auth_challenge,
    count_recovery_codes,
    create_auth_challenge,
    create_pin_for_email,
    create_session,
    delete_auth_factor,
    delete_engagement_target,
    delete_pin_for_email,
    delete_story_bank_entry,
    get_auth_audit_events,
    get_comment_outcomes,
    get_company_linked_in_url_for_user,
    get_content_mix_counts,
    get_current_group_post_draft,
    get_daily_action_counts,
    get_dm_templates,
    get_engagement_preferences,
    get_engagement_targets,
    get_follower_stats,
    get_latest_edition_scheduled_for,
    get_lead_magnet_settings,
    get_linkedin_profile_url_by_user_id,
    get_newsletter_edition,
    get_newsletter_settings,
    get_next_group_for_post,
    get_open_group_post_draft,
    get_or_create_reply_inbound_token,
    get_pending_newsletter_editions,
    get_pin_lockout,
    get_post_content,
    get_post_coverage_counts,
    get_post_enabled_group_ids,
    get_post_engagement_rows,
    get_post_image_url,
    get_post_manual_publish,
    get_post_performance_rows,
    get_post_status,
    get_post_user_id,
    get_session_id,
    get_story_bank_entries,
    get_user_blog_url,
    get_user_content_language,
    get_user_email,
    get_user_geo,
    get_user_groups,
    get_user_id,
    get_user_linkedin_display_name,
    get_user_passkey_credential_ids,
    get_user_preferences,
    get_user_public_uid,
    get_user_sitemap_url,
    get_user_subscription_info,
    get_user_timezone,
    get_user_token_info,
    has_engagement_preferences,
    has_linkedin_password,
    has_linkedin_session,
    insert_occasion_post,
    list_user_sessions,
    max_catchup_touches_allowed,
    normalize_posting_days,
    record_auth_event,
    revoke_other_sessions,
    revoke_session,
    set_groups_enabled,
    store_linkedin_li_at,
    suggest_engagement_targets,
    update_company_linked_in_url_for_user,
    update_db_post_image_url,
    update_engagement_preferences,
    update_group_post_draft,
    update_lead_magnet_settings,
    update_newsletter_edition,
    update_newsletter_settings,
    update_user,
    update_user_linkedin_display_name,
    update_user_linkedin_password,
    update_user_location,
    update_user_preferences,
    update_user_timezone,
    upsert_dm_templates,
    upsert_engagement_targets,
    upsert_story_bank_entries,
    verify_pin_for_email,
)
from cqc_lem.utilities.email import generate_pin, hash_pin, send_pin_email
from cqc_lem.utilities.geocoding import GeocodeError, geocode_city
from cqc_lem.utilities.group_post_slot import group_skip_undo_open, skip_undo_deadline
from cqc_lem.utilities.linkedin.helper import load_profile_for_user
from cqc_lem.utilities.linkedin.login_status import get_login_status
from cqc_lem.utilities.linkedin.token_refresh import resolve_token_status
from cqc_lem.utilities.logger import log_debug, log_error, log_info, log_warning
from cqc_lem.utilities.post_image import (
    PostImageRejected,
    claim_manual_generation,
    generate_image_for_post,
    remove_post_image_file,
    save_post_image_bytes,
)
from cqc_lem.utilities.post_video import (
    MAX_POST_VIDEO_BYTES,
    PostVideoRejected,
    owns_post_media_url,
    post_media_abs_path,
    remove_post_media_file,
    save_post_video_file,
)
from cqc_lem.utilities.profile_refresh import claim_profile_refresh, refresh_claimed_seconds
from cqc_lem.utilities.quality_gates import (
    AUTHENTICITY_SCORE_MIN_BOUNDS,
    SIMILARITY_MAX_PCT_BOUNDS,
    clamp_threshold,
)
from cqc_lem.utilities.webauthn_util import (
    build_authentication_options,
    build_registration_options,
    verify_registration as verify_passkey_registration,
)

# The FULL prefix, declared here rather than passed to `include_router`: `route.path` is what
# `_scope_path`, `_hide_admin_routes_from_schema` and the session-scope guards all read.
router = APIRouter(prefix="/api/user")


def _release_enrollment_hold(user_id: int, session_token: Optional[str]) -> None:
    """A factor just landed, so the hold that forced this session to the enrolment screen is over.

    Conditional on the scope inside the UPDATE, so this is a no-op for the ordinary case — a full,
    recovery or extension session enrolling a factor is not widened by it. Short-circuited on the
    rollout being live for the same reason the login path is: a deployment with no deadline cannot
    hold a session, so it should not pay a write to find that out.
    """
    if not enrollment_hold_active():
        return
    token = _main.current_session_token(session_token)
    if token and _main._release_hold(token):
        log_info("Mandatory enrolment satisfied — session released", user_id=user_id)


class UserSettingsRequest(BaseModel):
    """Body of `PUT /user/` — the blog/sitemap URLs, and nothing account-critical."""

    session_token: SessionTokenField = None
    # The caller's OWN address, and only ever as a target to check (issue #914).
    email: Optional[str] = None
    # `new_email` no longer moves the account: that is POST /user/email/change/init|verify — PIN to
    # the NEW address, step-up gated, every other session revoked. This endpoint used to do it on
    # the strength of knowing the CURRENT address, which is the whole account for one parameter.
    # It stays DECLARED so a client still sending it gets told (400). Dropping the field would let
    # Pydantic discard it and answer 200, and a silent success on an email change is how somebody
    # believes their address moved when it did not.
    new_email: Optional[str] = None
    blog_url: Optional[str] = None
    sitemap_url: Optional[str] = None


class RevokeSessionRequest(BaseModel):
    """Per-device revocation (issue #745, 2b). `session_id` revokes one device; `all_others`
    revokes every device except the one making the call.
    """
    session_token: SessionTokenField = None
    session_id: Optional[int] = None
    all_others: bool = False


class ExtensionTokenRequest(BaseModel):
    """Body of `POST /user/extension-token`.

    Carries only the CURRENT session, because the token it mints is a narrow `extension`-scoped one and the ceremony
    authorising it happens in the SPA.
    """

    session_token: SessionTokenField = None


class AgentTokenRequest(BaseModel):
    """Mint a token for a headless automation (issue #1026). `label` is what the operator will
    read on the Security card when deciding which machine to revoke.
    """
    session_token: SessionTokenField = None
    label: Optional[str] = Field(default=None, max_length=120)
    ttl_days: int = Field(default=90, ge=1, le=365)


class EmailChangeInitRequest(BaseModel):
    """Body of `POST /user/email/change/init`.

    The PIN goes to `new_email`, never to the current address — proving control of the destination is the whole
    point of the flow.
    """

    session_token: SessionTokenField = None
    new_email: str


class EmailChangeVerifyRequest(BaseModel):
    """Body of `POST /user/email/change/verify`.

    `new_email` is repeated because the PIN was hashed against that address; it has to match the one `/init` mailed
    for the code to verify at all.
    """

    session_token: SessionTokenField = None
    new_email: str
    pin: str


class SessionOnlyRequest(BaseModel):
    """The bare body shared by the ceremonies that need nothing but "who is asking".

    Passkey register/begin, TOTP enroll/begin, recovery-code regeneration and step-up/begin.
    """

    session_token: SessionTokenField = None


class PasskeyRegisterCompleteRequest(BaseModel):
    """Body of `POST /user/passkeys/register/complete`.

    `handle` names the pending challenge, which is claimed exactly once — a replayed registration response finds
    nothing to verify against.
    """

    session_token: SessionTokenField = None
    handle: str
    credential: Dict[str, Any]
    label: Optional[str] = None


class TotpConfirmRequest(BaseModel):
    """Body of `POST /user/totp/enroll/confirm`.

    The six digits that turn a pending authenticator secret into a confirmed factor.
    """

    session_token: SessionTokenField = None
    code: str


class AuthFactorDeleteRequest(BaseModel):
    """Body of `POST /user/auth-factors/delete`.

    `factor_id` is a target: the delete is scoped to the session's own user, so another account's factor id reads as
    "not found", never a removal.
    """

    session_token: SessionTokenField = None
    factor_id: int


class StepUpVerifyRequest(BaseModel):
    """Body of `POST /user/step-up/verify`.

    `method` decides which of the optional fields is read: `code` for TOTP, `handle` + `credential` for a passkey. A
    recovery code is not an accepted method here (design §6.8) — it restores access, it does not unlock the LinkedIn
    credentials.
    """

    session_token: SessionTokenField = None
    method: str
    code: Optional[str] = None
    handle: Optional[str] = None
    credential: Optional[Dict[str, Any]] = None


class UserPreferencesRequest(BaseModel):
    """Body of `PUT /user/settings` — the account-level knobs, none of them credentials.

    The `Optional[...] = None` fields follow one rule: omitted means UNCHANGED, so a client that
    predates a knob can never reset it by not sending it.
    """

    session_token: str
    last_login_inactivate_delay: Optional[int] = 90
    auto_schedule_posts: bool = False
    # Rolling forward buffer of ready posts (issue #544). Omitted → left as-is, so a client that
    # doesn't know about these knobs can't reset them. Bounded: they cap forward generation spend.
    content_buffer_days: Optional[int] = Field(default=None, ge=1, le=MAX_CONTENT_BUFFER_DAYS)
    content_buffer_max_posts: Optional[int] = Field(default=None, ge=1, le=MAX_CONTENT_BUFFER_POSTS)
    # BCP-47 tag the user's generated content (incl. premium-video audio) must be in — issue #548.
    # Omitted → unchanged; "" → cleared back to the Login Location default. Width matches
    # users.content_language VARCHAR(16).
    content_language: Optional[str] = Field(default=None, max_length=16)


# Input length limits — kept in lockstep with the DB column widths (see migrations) so an
# over-long value returns a clean 422 here instead of a MySQL 1406 that silently rolls back the
# whole upsert (the bug fixed by V52). The SPA mirrors these in ui/.../account/fieldLimits.ts.
_LEN_TONE = 255           # engagement_preferences.tone (V52: VARCHAR(255))
_LEN_COMMENT_STYLE = 255  # engagement_preferences.comment_style VARCHAR(255)
_LEN_GOALS = 2000         # engagement_preferences.business_goals/personal_goals (TEXT; app cap)
_LEN_BUYER_STAGE = 32     # engagement_preferences.default_buyer_stage VARCHAR(32)
_VALID_VIDEO_QUALITIES = ("standard", "premium", "premium_top")  # engagement_preferences.default_video_quality
_LEN_LM_KEYWORD = 128     # lead_magnet_settings.keyword VARCHAR(128)
_LEN_LM_MESSAGE = 2000    # lead_magnet_settings.message (TEXT; app cap)
# _LEN_DM_TEMPLATE belongs to this block and lives in api/models.py: this module and `main` both
# read it in a pydantic class body, which binds at import time.
_LEN_TARGET_PROFILE_URL = 512  # engagement_targets.profile_url VARCHAR(512)
_LEN_TARGET_NAME = 255         # engagement_targets.name VARCHAR(255)
_LEN_STORY_TITLE = 255         # story_bank.title VARCHAR(255)
_LEN_STORY_BODY = 5000         # story_bank.body (TEXT; app cap)
_LEN_GROUP_POST = 3000    # LinkedIn caps a post at 3000 chars (group_post_drafts.content is TEXT)
# Source text for an image prompt, NOT a post-length cap. LinkedIn's 3000 is enforced on the
# compose form only — nothing truncates a generated draft, and the Review & Edit textarea has no
# maxLength — so bounding this at 3000 would answer 422 for a long draft, and FastAPI's validation
# `detail` is a LIST, which the SPA cannot show: the author would get "Image generation failed"
# with no way to act on it. Bounded generously instead; it is still what reaches the brief LLM.
_LEN_IMAGE_PROMPT_SOURCE = 10000
_LEN_NL_TITLE = 255       # newsletter_settings.title VARCHAR(255)
_LEN_NL_TOPIC = 512       # newsletter_settings.topic VARCHAR(512)


class NewsletterSettingsRequest(BaseModel):
    """Body of `PUT /user/newsletter-settings` — the whole settings row, not a patch.

    Every field is dumped straight into `update_newsletter_settings`, so an omitted field takes its
    DEFAULT here rather than keeping the stored value. The `_clamp_*` validators bound the three
    fields that decide spend (queue depth, lead time, invites per run) so a bad client cannot ask
    for an unbounded amount of generation.
    """

    session_token: str
    enabled: bool = False
    title: Optional[str] = Field(default=None, max_length=_LEN_NL_TITLE)
    topic: Optional[str] = Field(default=None, max_length=_LEN_NL_TOPIC)
    cadence: str = "weekly"
    align_with_blog: bool = True
    publish_day: int = 1
    publish_hour: int = 9
    generate_lead_days: int = 3
    max_queued_drafts: int = 1
    invite_connections_enabled: bool = False
    max_invites_per_run: int = 50
    # Opt-in AI cover generation for each new draft (issue #893) — off by default because
    # generation costs money per edition.
    cover_image_auto: bool = False

    @field_validator("max_queued_drafts")
    @classmethod
    def _clamp_max_queued(cls, v: int) -> int:
        return max(1, min(10, v))

    @field_validator("generate_lead_days")
    @classmethod
    def _clamp_lead_days(cls, v: int) -> int:
        return max(0, min(60, v))

    @field_validator("max_invites_per_run")
    @classmethod
    def _clamp_max_invites(cls, v: int) -> int:
        return max(0, min(500, v))


class NewsletterDraftRequest(BaseModel):
    """Body of `PUT /user/newsletter-draft`.

    `action` is `save` (fields only), `approve` or `skip`; anything else maps to no status change, so an unknown
    action saves rather than publishing.
    """

    session_token: str
    edition_id: int
    title: Optional[str] = None
    subtitle: Optional[str] = None
    body: Optional[str] = None
    scheduled_datetime: Optional[datetime] = None
    action: str = "save"


class NewsletterRegenerateRequest(BaseModel):
    """Body of `POST /user/newsletter-draft/regenerate` — rewrite one queued edition."""

    session_token: str
    edition_id: int
    guidance: Optional[str] = None  # free-text "Added Guidance"; empty => AI decides a fresh take


class NewsletterCoverRequest(BaseModel):
    """Body of the cover-generation route (issue #893).

    A generated cover always lands `pending_review` — it is a public brand asset — so this request never publishes
    anything.
    """

    session_token: str
    edition_id: int
    # Per-edition avatar override: None = Auto (guardrails opt-in + relevance classifier both
    # required), True = With me (skips only the opt-in/classifier — never avatar_disabled or the
    # approval gate), False = Without me.
    use_avatar: Optional[bool] = None


class NewsletterCoverDecisionRequest(NewsletterCoverRequest):
    """The human review verdict on a generated cover — the ONE thing that lets one reach LinkedIn."""

    action: str = "approve"  # 'approve' publishes it with the edition; 'remove' drops it entirely


class PostRegenerateRequest(BaseModel):
    """Body of the post-regeneration route. `post_id` is a target the handler authorises."""

    session_token: str
    post_id: int
    guidance: Optional[str] = None  # free-text "Added Guidance"; empty => fresh take honoring settings


class OccasionPostRequest(BaseModel):
    """Body of `POST /user/post/occasion` — seed one occasion/milestone draft (issue #1074).

    `occasion` is the author's own account of the real event, and it is the ONLY specific the writer
    is allowed to state about it, so it is required rather than defaulted. `archetype` is validated
    against the framework's occasion family in the handler (400), not here, so the menu stays in one
    place.
    """

    session_token: SessionTokenField = None
    archetype: str
    occasion: str = Field(min_length=10, max_length=2000)
    scheduled_datetime: Optional[datetime] = None


class PostMarkPostedRequest(BaseModel):
    """Body of `POST /user/post/mark-posted` — the author says they published this one by hand."""

    session_token: SessionTokenField = None
    post_id: int


class PostRescoreRequest(BaseModel):
    """Body of `POST /user/post/rescore` — re-judge an edited draft without regenerating it.

    The edit must already be SAVED (issue #421); this request carries no text of its own.
    """

    session_token: str
    post_id: int


class PostImageGenerateRequest(BaseModel):
    """Render an image for a post (issue #1030). `post_id` is absent while the author is still
    composing — there is no row yet — in which case `content` is the only text there is.
    """
    session_token: SessionTokenField = None
    post_id: Optional[int] = None
    content: Optional[str] = Field(default=None, max_length=_LEN_IMAGE_PROMPT_SOURCE)


class PostImageRemoveRequest(BaseModel):
    """Body of `POST /user/post/image/remove` — take the image off a post AND delete the file."""

    session_token: SessionTokenField = None
    post_id: int


class EngagementPreferencesRequest(BaseModel):
    """Body of `PUT /user/engagement-preferences` — targeting, voice and the per-day caps.

    Two rules run through the whole model. The `max_length=` bounds are kept in lockstep with the
    DB column widths so an over-long value is a clean 422 instead of a MySQL 1406 that silently
    rolls back the whole upsert. And the `_coerce_*` validators never reject an unknown enum value,
    they fall back to the safe default — an SPA build that predates a mode must not 422 a settings
    save. `Optional[...] = None` on a cap (e.g. `max_follows_per_day`) means "leave what is
    stored", NOT the code default, so an older client cannot resurrect a lane the user switched off.
    """

    session_token: str
    tone: Optional[str] = Field(default=None, max_length=_LEN_TONE)
    comment_length: str = "medium"
    comment_style: Optional[str] = Field(default=None, max_length=_LEN_COMMENT_STYLE)
    use_emojis: bool = True
    use_hashtags: bool = False
    include_topics: List[str] = []
    exclude_topics: List[str] = []
    include_keywords: List[str] = []
    exclude_keywords: List[str] = []
    include_authors: List[str] = []
    exclude_authors: List[str] = []
    post_types: List[str] = []
    focus_topics: List[str] = []
    business_goals: Optional[str] = Field(default=None, max_length=_LEN_GOALS)
    personal_goals: Optional[str] = Field(default=None, max_length=_LEN_GOALS)
    # Quality-gate sensitivity (issue #421). None = keep the deploy default.
    authenticity_score_min: Optional[int] = None
    post_similarity_max_pct: Optional[int] = None
    min_reactions: Optional[int] = None
    max_post_age_hours: Optional[int] = 24
    reply_to_own_comments: bool = True
    max_comments_per_day: int = 20
    max_dms_per_day: int = 20
    max_invites_per_day: int = 10
    # Company-page invites (issue #732). Effective ceiling is min(this, max_invites_per_day).
    max_company_page_invites_per_day: int = COMPANY_PAGE_INVITES_PER_DAY_DEFAULT
    connection_request_mode: str = "auto_approve"  # 'auto_approve' (default) | 'pre_review'
    # Smart connection targeting (issue #486): 'off' | 'suggest' (default) | 'auto_queue'
    connection_targeting_mode: str = "suggest"
    connection_target_authors: List[str] = []
    min_connection_icp_score: int = 55
    default_buyer_stage: Optional[str] = Field(default=None, max_length=_LEN_BUYER_STAGE)
    default_video_quality: str = "standard"
    reply_check_mode: str = "event"
    reply_sweeps_per_day: int = 2
    reply_max_post_age_days: int = 2
    feed_fallback_when_empty: bool = True
    link_in_first_comment: bool = True
    # Publishing cadence — how many day-type slots a week the content plan fills (issue #621).
    posts_per_week: int = DEFAULT_POSTS_PER_WEEK
    # Which weekdays those slots may land on, Mon=0 … Sun=6 (issue #581). Default Mon-Fri; all
    # seven remain selectable.
    posting_days: List[int] = list(DEFAULT_POSTING_DAYS)
    # AI image on generated TEXT posts (image-generation overhaul). The review queue stays the
    # human gate on every image; this only controls whether one is generated at all.
    text_post_images: bool = True
    # Opt-in auto-follow of roster targets (issue #962), OFF by default. The cap is its own small
    # per-day budget — a follow never spends the comment lane's.
    roster_auto_follow: bool = False
    # None (omitted by the client) means "leave what is stored alone" — NOT the code default. An
    # older SPA build that never learned this field must not resurrect 3 follows/day over a 0 the
    # user set to switch an outbound lane off.
    max_follows_per_day: Optional[int] = None
    # Opt-in auto-connect for roster targets following did not unlock (issue #979), OFF by default
    # and independent of roster_auto_follow. No cap of its own — roster invites take at most a
    # minority share of whatever max_invites_per_day has left.
    roster_auto_connect: bool = False
    # Catch-up congratulations (issue #482)
    max_catchup_touches_per_day: int = CATCHUP_TOUCHES_MAX_STANDARD
    catchup_touch_mode: str = "pre_review"  # 'pre_review' (default) | 'auto_approve'
    catchup_event_types: List[str] = list(DEFAULT_CATCHUP_EVENT_TYPES)
    catchup_message_source: str = "linkedin"  # 'linkedin' (LinkedIn's own draft) | 'ai'
    # Per-contact catch-up frequency guard (issue #1078). 0 disables the guard.
    min_catchup_contact_interval_days: int = CATCHUP_MIN_CONTACT_INTERVAL_DAYS_DEFAULT
    max_catchup_touches_per_contact_days: int = CATCHUP_MAX_PER_CONTACT_DAYS_DEFAULT

    @field_validator("comment_length")
    @classmethod
    def _coerce_comment_length(cls, v: str) -> str:
        return v if v in ("short", "medium", "long") else "medium"

    @field_validator("default_video_quality")
    @classmethod
    def _coerce_video_quality(cls, v: str) -> str:
        return v if v in _VALID_VIDEO_QUALITIES else "standard"

    @field_validator("reply_check_mode")
    @classmethod
    def _coerce_reply_mode(cls, v: str) -> str:
        return v if v in ("event", "scheduled", "off") else "event"

    @field_validator("connection_request_mode")
    @classmethod
    def _coerce_connection_mode(cls, v: str) -> str:
        return v if v in ("auto_approve", "pre_review") else "auto_approve"

    @field_validator("connection_targeting_mode")
    @classmethod
    def _coerce_targeting_mode(cls, v: str) -> str:
        return v if v in ("off", "suggest", "auto_queue") else "suggest"

    @field_validator("min_connection_icp_score")
    @classmethod
    def _clamp_min_icp(cls, v: int) -> int:
        try:
            return min(100, max(0, int(v)))
        except (TypeError, ValueError):
            return 55

    @field_validator("reply_sweeps_per_day")
    @classmethod
    def _clamp_sweeps(cls, v: int) -> int:
        try:
            return min(12, max(2, int(v)))
        except (TypeError, ValueError):
            return 2

    @field_validator("reply_max_post_age_days")
    @classmethod
    def _clamp_age_days(cls, v: int) -> int:
        try:
            return min(14, max(1, int(v)))
        except (TypeError, ValueError):
            return 2

    @field_validator("max_company_page_invites_per_day")
    @classmethod
    def _clamp_company_page_invites(cls, v: int) -> int:
        try:
            return min(COMPANY_PAGE_INVITES_PER_DAY_MAX,
                       max(COMPANY_PAGE_INVITES_PER_DAY_MIN, int(v)))
        except (TypeError, ValueError):
            return COMPANY_PAGE_INVITES_PER_DAY_DEFAULT

    @field_validator("max_follows_per_day")
    @classmethod
    def _clamp_follows(cls, v: Optional[int]) -> Optional[int]:
        # An explicit 0 is preserved (MIN is 0): it is how the user switches the follow lane off
        # without touching the toggle, so clamping it up would re-enable an outbound action.
        if v is None:
            return None
        try:
            return min(ROSTER_FOLLOWS_PER_DAY_MAX, max(ROSTER_FOLLOWS_PER_DAY_MIN, int(v)))
        except (TypeError, ValueError):
            return ROSTER_FOLLOWS_PER_DAY_DEFAULT

    @field_validator("posts_per_week")
    @classmethod
    def _clamp_posts_per_week(cls, v: int) -> int:
        try:
            return min(POSTS_PER_WEEK_MAX, max(POSTS_PER_WEEK_MIN, int(v)))
        except (TypeError, ValueError):
            return DEFAULT_POSTS_PER_WEEK

    # mode="before": a malformed day list must fall back to Mon-Fri, not 422 the whole settings
    # save — the SPA writes every engagement field in one request.
    @field_validator("posting_days", mode="before")
    @classmethod
    def _clean_posting_days(cls, v) -> List[int]:
        return normalize_posting_days(v)

    @field_validator("authenticity_score_min")
    @classmethod
    def _clamp_authenticity_min(cls, v: Optional[int]) -> Optional[int]:
        return clamp_threshold(v, *AUTHENTICITY_SCORE_MIN_BOUNDS)

    @field_validator("post_similarity_max_pct")
    @classmethod
    def _clamp_similarity_max(cls, v: Optional[int]) -> Optional[int]:
        return clamp_threshold(v, *SIMILARITY_MAX_PCT_BOUNDS)

    @field_validator("catchup_touch_mode")
    @classmethod
    def _coerce_catchup_mode(cls, v: str) -> str:
        return v if v in VALID_CATCHUP_TOUCH_MODES else "pre_review"

    @field_validator("catchup_message_source")
    @classmethod
    def _coerce_catchup_message_source(cls, v: str) -> str:
        return v if v in VALID_CATCHUP_MESSAGE_SOURCES else "linkedin"

    @field_validator("max_catchup_touches_per_day")
    @classmethod
    def _clamp_catchup_cap(cls, v: int) -> int:
        # Absolute ceiling only — the per-plan allowance (10/day premium, 5/day otherwise) is applied
        # in update_engagement_preferences, which knows the user.
        try:
            return min(CATCHUP_TOUCHES_MAX, max(CATCHUP_TOUCHES_MIN, int(v)))
        except (TypeError, ValueError):
            return CATCHUP_TOUCHES_MAX_STANDARD

    @field_validator("catchup_event_types")
    @classmethod
    def _clean_catchup_event_types(cls, v: List[str]) -> List[str]:
        # Drop unknown milestone types at the boundary — the ledger column is a MySQL ENUM.
        return [t for t in (v or []) if t in tuple(CatchupEventType)]

    @field_validator("min_catchup_contact_interval_days")
    @classmethod
    def _clamp_catchup_contact_interval(cls, v: int) -> int:
        try:
            return min(CATCHUP_MIN_CONTACT_INTERVAL_DAYS_MAX,
                       max(CATCHUP_MIN_CONTACT_INTERVAL_DAYS_MIN, int(v)))
        except (TypeError, ValueError):
            return CATCHUP_MIN_CONTACT_INTERVAL_DAYS_DEFAULT

    @field_validator("max_catchup_touches_per_contact_days")
    @classmethod
    def _clamp_catchup_per_contact_cap(cls, v: int) -> int:
        try:
            return min(CATCHUP_MAX_PER_CONTACT_DAYS_MAX,
                       max(CATCHUP_MAX_PER_CONTACT_DAYS_MIN, int(v)))
        except (TypeError, ValueError):
            return CATCHUP_MAX_PER_CONTACT_DAYS_DEFAULT


# What `GET /user/engagement-preferences` DOCUMENTS (#1446) — never what it serializes. The saved
# row is exactly what the PUT body carries, so the field list is taken from the request model
# rather than restated: a restatement is a second list to keep in step with the 45 columns, and
# `test_response_schemas.py` ties both of them to `db._ENGAGEMENT_DEFAULTS`. The extras below are
# the read-only context the handler adds on top of the row — every one of them REQUIRED and
# nullable, because the handler assigns the key on both branches of its own try/except: `= None`
# would document it as possibly ABSENT, which the generated TypeScript spells `key?:`.
EngagementPreferencesDetail = detail_model_from(
    "EngagementPreferencesDetail", EngagementPreferencesRequest,
    drop=("session_token",),
    extras={
        "has_saved_preferences": (bool, ...),
        "reply_inbound_address": (Optional[str], ...),
        "gmail_forward_confirmation": (Optional[GmailForwardConfirmation], ...),
        "max_catchup_touches_allowed": (int, ...),
        "catchup_contact_interval_bounds": (CatchupContactIntervalBounds, ...),
        "catchup_per_contact_cap_bounds": (CatchupPerContactCapBounds, ...),
        "gate_defaults": (GateDefaults, ...),
        "feed_reach": (Optional[FeedReach], ...),
    },
    doc="The saved engagement preferences, plus the read-only context the Settings hub renders.\n\n"
        "Everything above `has_saved_preferences` is a stored column and can be PUT back; "
        "everything from it down is derived per request and is ignored on a save.",
)


class DmTemplateItem(BaseModel):
    """One row of a DM template ladder.

    `step` 0 is the first touch and higher steps are the follow-ups, each fired `delay_hours` after the one before
    it.
    """

    event_type: str
    step: int = 0
    delay_hours: int = 0
    template_text: str = Field(max_length=_LEN_DM_TEMPLATE)
    is_active: bool = True


class DmTemplatesRequest(BaseModel):
    """Body of `PUT /user/dm-templates` — the user's WHOLE template set, replaced wholesale."""

    session_token: str
    templates: List[DmTemplateItem] = []


class EngagementTargetItem(BaseModel):
    """One person on the engagement roster.

    Every validator here coerces rather than rejects: a roster save is one PUT of the whole list, so one bad row
    must not 422 away the entire edit.
    """

    profile_url: str = Field(max_length=_LEN_TARGET_PROFILE_URL)
    name: Optional[str] = Field(default=None, max_length=_LEN_TARGET_NAME)
    category: str = "peer"
    max_comments_per_week: int = ENGAGEMENT_TARGET_WEEKLY_DEFAULT
    active: bool = True
    source: str = "user"

    @field_validator("category")
    @classmethod
    def _valid_category(cls, v: str) -> str:
        return v if v in ENGAGEMENT_TARGET_CATEGORIES else "peer"

    @field_validator("source")
    @classmethod
    def _valid_source(cls, v: str) -> str:
        return v if v in ENGAGEMENT_TARGET_SOURCES else "user"

    @field_validator("max_comments_per_week")
    @classmethod
    def _clamp_weekly_cap(cls, v: int) -> int:
        # Clamped, never rejected: the per-author cap is a safety rail, so an out-of-range slider
        # must not 422 away the operator's whole roster edit.
        return max(0, min(ENGAGEMENT_TARGET_WEEKLY_MAX, int(v)))


class EngagementTargetsRequest(BaseModel):
    """Body of `PUT /user/engagement-targets` — the WHOLE roster, replaced in one write."""

    session_token: str
    targets: List[EngagementTargetItem] = []


class EngagementTargetDeleteRequest(BaseModel):
    """Body of `DELETE /user/engagement-targets`.

    The target is named by profile URL, not by id — that is the roster's natural key, and it is what the SPA is
    holding.
    """

    session_token: str
    profile_url: str = Field(max_length=_LEN_TARGET_PROFILE_URL)


class StoryBankItem(BaseModel):
    """One piece of the user's own raw material (issue #620). `body` is the only required field —
    quick capture is a textarea, not a form wizard, so the title defaults from the body.
    """
    id: Optional[int] = None
    kind: str = "anecdote"
    title: Optional[str] = Field(default=None, max_length=_LEN_STORY_TITLE)
    body: str = Field(max_length=_LEN_STORY_BODY)
    happened_at: Optional[str] = None
    active: bool = True

    @field_validator("kind")
    @classmethod
    def _valid_kind(cls, v: str) -> str:
        return v if v in STORY_BANK_KINDS else "anecdote"


class StoryBankRequest(BaseModel):
    """Body of `PUT /user/story-bank` (issue #620) — upsert a batch of the user's own raw material.

    An entry carrying an `id` updates that row; one without adds a new entry.
    """

    session_token: str
    entries: List[StoryBankItem] = []


class StoryBankDeleteRequest(BaseModel):
    """Body of `DELETE /user/story-bank`."""

    session_token: str
    entry_id: int


class LinkedInPasswordRequest(BaseModel):
    """Body of the store-my-LinkedIn-password route.

    The value is written as an AES-256-GCM envelope by `db.py` and is never readable back through the API — see
    `docs/secrets-at-rest.md`.
    """

    session_token: str
    linkedin_password: str


class LinkedInDisplayNameRequest(BaseModel):
    """The user's own name as LinkedIn renders it.

    It is a SETTING and never scraped (issue #731), because it is how `ThreadState` decides which messages in a
    thread are ours.
    """

    session_token: str
    linkedin_display_name: str


class TimezoneRequest(BaseModel):
    """Body of `PUT /user/timezone` — an IANA zone name; it decides when scheduled posts fire."""

    session_token: str
    timezone: str


class LocationRequest(BaseModel):
    """Body of `PUT /user/location` — the Login Location the Selenium session emulates.

    Stored verbatim, so this is the MANUAL path: coordinates are required (and range-checked),
    everything else is optional labelling. `/user/location/autocapture` and `/user/location/by-city`
    are the two routes that derive these values instead of being handed them.
    """

    session_token: str
    latitude: float
    longitude: float
    city: Optional[str] = None
    country: Optional[str] = None
    locale: Optional[str] = None
    timezone: Optional[str] = None


class LocationAutocaptureRequest(BaseModel):
    """Body of `POST /user/location/autocapture` — nothing but the session.

    The location is derived from the caller's real IP, read off the Cloudflare tunnel headers
    rather than the immediate peer.
    """

    session_token: str


class LocationByCityRequest(BaseModel):
    """Body of `POST /user/location/by-city` — geocode a city the user picked, rather than guessing from their IP.

    `country` is an ISO-3166 alpha-2 code.
    """

    session_token: str
    city: str
    state: Optional[str] = None
    country: Optional[str] = None


class LinkedInCookieRequest(BaseModel):
    """Body of `POST /user/linkedin-cookie` — the crown jewel (design §2, T1).

    Storing a `li_at` IS handing over a live LinkedIn session, so the route is step-up gated and is
    the ONE place an `extension`-scoped token may reach.
    """

    session_token: str
    li_at: str
    jsessionid: Optional[str] = None
    # Cookie-only migration (issue #745, design §5.4): set by the SPA prompt shown to accounts that
    # still hold a LinkedIn password. Defaults to False so the browser extension — which posts the
    # same body on every reconnect — never silently removes a user's only working login.
    drop_password: Optional[bool] = False


class LinkedInCompanyPageRequest(BaseModel):
    """Body of `PUT /user/company-page`.

    An empty/absent URL CLEARS the page, which is how a user opts out of the company-page invite drip — an account
    with no page is simply skipped.
    """

    session_token: str
    company_linked_in_url: Optional[str] = None


@router.get("/survey")
def survey_endpoint(session_token: str) -> ResponseModel[dict[str, Any]]:
    """The survey to show in-app right now (day-3 NPS, trial T-3d NPS, or the review that unlocks
    the extended trial), or none (issue #501). With PostHog Surveys on (issue #653) the NPS asks are
    retired from this snapshot — PostHog is asking them.
    """
    user_id = _main.get_session_user_id(session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")

    from cqc_lem.utilities.surveys import survey_snapshot
    return ResponseModel(status_code=200, detail=survey_snapshot(user_id))


@router.get("/shipped")
def shipped_notices_endpoint(session_token: str) -> ResponseModel[dict[str, Any]]:
    """The "you asked, we shipped" notices waiting for this user, plus the recent changelog (issue
    #502). A notice only appears once the reporter has had the fix for FEEDBACK_FIX_CSAT_DELAY_HOURS
    — that delay is what schedules the micro-CSAT it carries.
    """
    user_id = _main.get_session_user_id(session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")

    from cqc_lem.utilities.db import get_recent_shipped_notices, get_unseen_shipped_notices
    from cqc_lem.utilities.feedback.shipped import fix_csat_delay_hours
    notices = get_unseen_shipped_notices(user_id, delay_hours=fix_csat_delay_hours())
    return ResponseModel(status_code=200, detail={
        "notices": [{"id": n.get("id"), "issue_number": n.get("github_issue_number"),
                     "changelog_line": n.get("changelog_line"),
                     "shipped_at": n.get("shipped_at")} for n in notices],
        "changelog": [{"issue_number": n.get("github_issue_number"),
                       "changelog_line": n.get("changelog_line"),
                       "shipped_at": n.get("shipped_at")} for n in get_recent_shipped_notices()],
    })


@router.put("/", responses={
    200: {"description": "User settings updated"},
    **{k: v for k, v in error_responses.items() if k in [400, 401, 403, 404]}
})
def update_user_endpoint(settings: UserSettingsRequest) -> ResponseModel[str]:
    """Save the blog/sitemap URLs.

    A URL the client SENT is written even when it is empty — that is how one gets removed. The
    fields it did not send are the ones left alone, which is what `model_fields_set` reads: testing
    the values for truth instead meant clearing a blog URL answered 200 while storing nothing, and
    the Account page reported it saved (issue #1574).

    Sending `new_email` is a 400, LOUDLY: this endpoint used to move the account address on the strength of knowing
    the current one, and a silent 200 is how somebody believes their address changed when it did not (issue #914).
    """
    user_id = _main.require_session_user_id(settings.session_token)
    _main._reject_foreign_email(user_id, settings.email)

    if settings.new_email:
        # Never silently: a client that still expects this endpoint to move the address has to hear
        # that it did not, or the account keeps answering to the old one while the user believes
        # otherwise (issue #914).
        raise HTTPException(
            status_code=400,
            detail="Changing the account email moved to POST /user/email/change/init "
                   "and POST /user/email/change/verify",
        )

    written = {name: getattr(settings, name)
               for name in ("blog_url", "sitemap_url") if name in settings.model_fields_set}
    if not written:
        return ResponseModel(status_code=200, detail="User settings unchanged")

    updated = update_user(user_id, **written)
    if not updated:
        raise HTTPException(status_code=404, detail="Update failed")
    return ResponseModel(status_code=200, detail="User updated successfully")


@router.get("/token_status")
def get_user_token_status(session_token: str) -> ResponseModel[dict[str, Any]]:
    """The LinkedIn OAuth token's state for the SPA's reconnect countdown.

    `resolve_token_status` is the ONE decision core, shared with the daily renewal beat (issue
    #600), so what the user sees and what triggers the reconnect email can never disagree.
    `days_remaining` is None — never 0 — when it cannot be read.
    """
    user_id = _main.get_session_user_id(session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")

    # One decision core shared with the daily renewal beat (issue #600), so the countdown the SPA
    # renders and the one that triggers the reconnect email can never disagree.
    return ResponseModel(status_code=200, detail=resolve_token_status(user_id))


@router.get("/linkedin-signin-status")
def get_linkedin_signin_status(session_token: str) -> ResponseModel[dict[str, Any]]:
    """The last LinkedIn sign-in the automation made, and whether it is waiting on the user's
    device approval (issue #933).

    LinkedIn's "Did you just try to sign in?" challenge is approved on LinkedIn, not here, so the
    approval email was the only place it was ever visible — a user who had already tapped Yes had
    no way to confirm LEM received it. `unknown` means nothing is recorded (no sign-in since the
    record expired, or Redis is down); it is NOT a failure, so the SPA says so rather than
    implying the connection is broken.
    """
    user_id = _main.get_session_user_id(session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")

    status = get_login_status(user_id) or {}
    return ResponseModel(status_code=200, detail={
        "state": status.get("state") or "unknown",
        "signed_in_at": status.get("signed_in_at"),
        "approval_requested_at": status.get("approval_requested_at"),
        "approval_cleared_at": status.get("approval_cleared_at"),
    })


@router.get("/security")
def get_user_security(session_token: Optional[str] = None) -> ResponseModel[dict[str, Any]]:
    """Everything the account page's Security card shows (issue #745, 2b): the devices signed in,
    the recent auth history, and the state of the email attribute. Never returns a token, a token
    hash or an IP hash.
    """
    user_id = _main.get_session_user_id(session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")

    token = _main.current_session_token(session_token)
    sessions = [{
        "id": s["id"],
        "label": s["label"],
        "created_at": _main._utc_iso(s.get("created_at")),
        "last_seen_at": _main._utc_iso(s.get("last_seen_at")),
        "expires_at": _main._utc_iso(s.get("expires_at")),
        "is_current": s["is_current"],
    } for s in list_user_sessions(user_id, current_token=token)]
    events = [{
        "event": e.get("event"),
        "success": bool(e.get("success")),
        "user_agent": e.get("user_agent"),
        "created_at": _main._utc_iso(e.get("created_at")),
    } for e in get_auth_audit_events(user_id)]

    return ResponseModel(status_code=200, detail={
        "public_uid": get_user_public_uid(user_id),
        "email": get_user_email(user_id),
        "sessions": sessions,
        "recent_events": events,
    })


@router.post("/sessions/revoke")
def revoke_user_session(request: RevokeSessionRequest, http_request: Request = None) -> ResponseModel[dict[str, Any]]:
    """Sign a device out. Revoking the CURRENT session is allowed — it is the same thing as logging
    out — and the caller's next request simply resolves to no user.
    """
    user_id = _main.get_session_user_id(request.session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    # Step-up gated (2c): signing every other device out is how an attacker with one stolen session
    # locks the real owner out of their own account.
    _require_step_up(user_id, request.session_token, "revoke_session",
                     http_request=http_request)

    ip = _main._client_ip(http_request)
    user_agent = _main._user_agent(http_request)
    if request.all_others:
        revoked = revoke_other_sessions(user_id, keep_token=_main.current_session_token(request.session_token))
        record_auth_event(AuthAuditEvent.SESSIONS_REVOKED_ALL, user_id=user_id, ip=ip,
                          user_agent=user_agent, details={"revoked": revoked})
        return ResponseModel(status_code=200, detail={"revoked": revoked})

    if not request.session_id:
        raise HTTPException(status_code=400, detail="session_id or all_others is required")
    # revoke_session is scoped by user_id, so an id belonging to another account is a 404, never a
    # cross-account revoke.
    if not revoke_session(user_id, request.session_id):
        raise HTTPException(status_code=404, detail="Session not found")
    record_auth_event(AuthAuditEvent.SESSION_REVOKED, user_id=user_id, ip=ip,
                      user_agent=user_agent, details={"session_id": request.session_id})
    return ResponseModel(status_code=200, detail={"revoked": 1})


@router.post("/extension-token")
def mint_extension_token(request: ExtensionTokenRequest, http_request: Request = None) -> ResponseModel[dict[str, Any]]:
    """Mint a session token for the browser extension (issue #745, 2b).

    The extension needs a token it can hold; the SPA no longer has one to give it, because the
    browser's own session is an httpOnly cookie. So it gets its OWN session row — labelled, listed
    beside every other device on the Security card, and revocable on its own without signing the
    person out of the app.

    Since 2c.1 that row is also NARROW: `scope='extension'` reaches `/user/linkedin-cookie` and
    nothing else (`_EXTENSION_SESSION_SURFACE`). The token lives in extension storage on a machine
    we do not control, so the blast radius of losing one is now "someone can overwrite my li_at",
    not "someone can read my whole account".
    """
    user_id = _main.get_session_user_id(request.session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    # This is where the extension's step-up happens (2c) — ONCE, here in the SPA, where a passkey
    # ceremony is possible. The minted token is `extension`-scoped, and that scope is what later
    # lets it POST a cookie without a ceremony it could never run (design §6.5).
    _require_step_up(user_id, request.session_token, "mint_extension_token",
                     http_request=http_request)

    token = create_session(user_id, user_agent=_main._user_agent(http_request),
                           ip=_main._client_ip(http_request), label="LinkedIn Connect extension",
                           scope=SESSION_SCOPE_EXTENSION)
    if not token:
        raise HTTPException(status_code=500, detail="Could not create session")
    return ResponseModel(status_code=200, detail={"session_token": token})


@router.post("/agent-token")
def mint_agent_token(request: AgentTokenRequest, http_request: Request = None) -> ResponseModel[dict[str, Any]]:
    """Mint a long-lived, narrow session for a headless automation (issue #1026).

    Same shape as the extension token and for the same reason: a non-browser client needs a
    credential it can hold, and the ceremony that makes minting safe can only happen HERE, in the
    SPA, with a human present. The agent never runs one.

    What makes this different from the extension is the blast radius in the other direction. The
    extension token can write ONE credential; this one can write no credentials at all. It reaches
    the queueing surface (`_AGENT_SESSION_SURFACE`) — read the review queues, create pending items —
    and every approval stays with the human, enforced server-side in `_refuse_agent_approval`.

    The TTL is explicit rather than idle-driven: an agent that runs weekly would find a 24h session
    expired every run. The row is still listed and revocable per-device, so revoking it stops the
    automation and signs nobody out.
    """
    user_id = _main.get_session_user_id(request.session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    # The agent's step-up happens ONCE, here, where a passkey ceremony is possible — exactly the
    # extension's bargain. `extension_scope_ok` is NOT passed: an agent token must never be able to
    # mint its own successor without a fresh human ceremony.
    _require_step_up(user_id, request.session_token, "mint_agent_token",
                     http_request=http_request)

    token = create_session(user_id, user_agent=_main._user_agent(http_request),
                           ip=_main._client_ip(http_request),
                           label=(request.label or "Headless agent"),
                           scope=SESSION_SCOPE_AGENT,
                           ttl_hours=request.ttl_days * 24)
    if not token:
        raise HTTPException(status_code=500, detail="Could not create session")
    record_auth_event(AuthAuditEvent.AGENT_TOKEN_MINTED, user_id=user_id,
                      ip=_main._client_ip(http_request), user_agent=_main._user_agent(http_request),
                      details={"scope": SESSION_SCOPE_AGENT, "ttl_days": request.ttl_days,
                               "label": request.label or "Headless agent"})
    return ResponseModel(status_code=200,
                         detail={"session_token": token, "expires_in_days": request.ttl_days})


@router.post("/email/change/init")
def user_email_change_init(request: EmailChangeInitRequest,
                           http_request: Request = None) -> ResponseModel[dict[str, Any]]:
    """Start an email change: PIN goes to the NEW address, so control of it has to be proven before
    the account moves. The address is an attribute of the account — the identity is `public_uid`.
    """
    user_id = _main.get_session_user_id(request.session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")

    # Step-up gated on INIT, which is the real control: the confirmation PIN only ever goes to the
    # new address, so a change that cannot be started cannot be finished. Gating /verify too would
    # put the 5-minute freshness window around "go read your email", which is a lockout waiting to
    # happen for no extra security.
    _require_step_up(user_id, request.session_token, "email_change",
                     http_request=http_request)

    new_email = request.new_email.strip().lower()
    if not new_email or "@" not in new_email:
        raise HTTPException(status_code=400, detail="A valid email address is required")
    if new_email == (get_user_email(user_id) or "").lower():
        raise HTTPException(status_code=400, detail="That is already your email address")

    ip = _main._client_ip(http_request)
    verdict = check_auth_init(new_email, ip)
    if not verdict.allowed:
        raise HTTPException(status_code=429, detail="Too many requests — try again later",
                            headers={"Retry-After": str(verdict.retry_after_seconds)})

    existing_owner = get_user_id(new_email)
    if existing_owner and existing_owner != user_id:
        # Deliberately the same 400 the SPA shows for any rejected address: a distinct "already
        # registered" reply would turn this endpoint into an account-existence oracle.
        raise HTTPException(status_code=400, detail="That address cannot be used")

    # Unlike login, this flow has no bypass: the whole point is proving control of the NEW address,
    # so with no mail provider configured the change is unavailable rather than unconfirmed.
    _, bypassed = send_pin_email(new_email, "", probe_only=True)
    if bypassed:
        raise HTTPException(status_code=503,
                            detail="Email delivery is not configured — email change is unavailable")

    pin = generate_pin()
    if not create_pin_for_email(new_email, hash_pin(pin, new_email)):
        raise HTTPException(status_code=500, detail="Could not create PIN")
    sent, _ = send_pin_email(new_email, pin, is_new_user=False)
    if not sent:
        delete_pin_for_email(new_email)
        raise HTTPException(status_code=500, detail="Could not send confirmation email")

    record_auth_event(AuthAuditEvent.EMAIL_CHANGE_REQUESTED, user_id=user_id, email=new_email,
                      ip=ip, user_agent=_main._user_agent(http_request))
    return ResponseModel(status_code=200, detail={"message": "Confirmation PIN sent"})


@router.post("/email/change/verify")
def user_email_change_verify(request: EmailChangeVerifyRequest,
                             http_request: Request = None) -> ResponseModel[dict[str, Any]]:
    """Confirm the new address with the PIN sent to it, then move the account. Every OTHER device is
    revoked: an email change is exactly what an attacker does after stealing a session, and the real
    owner has to be able to end those sessions by taking their address back.
    """
    user_id = _main.get_session_user_id(request.session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")

    new_email = request.new_email.strip().lower()
    pin = request.pin.strip()
    if not new_email or not pin:
        raise HTTPException(status_code=400, detail="Email and PIN are required")

    ip = _main._client_ip(http_request)
    user_agent = _main._user_agent(http_request)
    verdict = check_auth_verify(new_email, ip)
    if not verdict.allowed:
        raise HTTPException(status_code=429, detail="Too many attempts — try again later",
                            headers={"Retry-After": str(verdict.retry_after_seconds)})
    if get_pin_lockout(new_email):
        raise HTTPException(status_code=429, detail="Too many incorrect PINs — start over shortly")

    if not verify_pin_for_email(new_email, hash_pin(pin, new_email)):
        record_auth_event(AuthAuditEvent.EMAIL_CHANGED, user_id=user_id, email=new_email, ip=ip,
                          user_agent=user_agent, success=False, details={"reason": "bad_pin"})
        raise HTTPException(status_code=401, detail="Invalid or expired PIN")

    token = _main.current_session_token(request.session_token)
    old_email = get_user_email(user_id)
    if not change_user_email(user_id, new_email, changed_by_session_id=get_session_id(token) if token else None):
        raise HTTPException(status_code=400, detail="That address cannot be used")

    revoked = revoke_other_sessions(user_id, keep_token=token)
    record_auth_event(AuthAuditEvent.EMAIL_CHANGED, user_id=user_id, email=new_email, ip=ip,
                      user_agent=user_agent, details={"old_email": old_email,
                                                      "sessions_revoked": revoked})
    log_info("User email changed", user_id=user_id)
    return ResponseModel(status_code=200, detail={"email": new_email, "sessions_revoked": revoked})


CHALLENGE_REGISTER = "webauthn_register"


CHALLENGE_STEP_UP = "webauthn_step_up"


def _step_up_error(user_id: int) -> HTTPException:
    """A step-up refusal is **403, never 401**. The SPA's axios interceptor treats any 401 as a dead
    session — it clears the cookie sentinel and redirects to the landing page — so answering "prove
    it's you" with a 401 would log the user out instead of asking them anything.
    """
    return HTTPException(status_code=403, detail={
        "code": "step_up_required",
        "message": "Confirm it's you to change this.",
        "methods": available_methods(user_id),
    })


def _require_step_up(user_id: int, session_token: Optional[str], action: str,
                     extension_scope_ok: bool = False,
                     http_request: Optional[Request] = None) -> None:
    """Gate a credential-touching write on a recently proved factor.

    `extension_scope_ok` is opt-in per call site and only ONE passes it — the cookie endpoint the
    browser extension actually calls. An extension token is otherwise an ordinary session, so a
    blanket exemption would let a stolen one change the email address and revoke every device.

    The denial carries ip/user_agent because STEP_UP_DENIED is the audit row that most often means
    "someone else is holding this session" — without the client on it there is nothing to chase.
    """
    if step_up_satisfied(user_id, _main.current_session_token(session_token),
                         extension_scope_ok=extension_scope_ok):
        return
    record_auth_event(AuthAuditEvent.STEP_UP_DENIED, user_id=user_id, success=False,
                      ip=_main._client_ip(http_request), user_agent=_main._user_agent(http_request),
                      details={"action": action})
    raise _step_up_error(user_id)


def _require_enrollment_allowed(user_id: int, session_token: Optional[str], action: str,
                                http_request: Optional[Request] = None) -> None:
    """Gate ADDING a factor once the account already holds one.

    Enrolling stamps the session as verified, so an ungated enrolment is a step-up the caller never
    had to prove: a stolen session would add its own passkey and walk into the LinkedIn credentials
    with it. The first factor stays ungated (nothing to prove with) and a recovery-code session
    stays allowed (its owner is the one who legitimately cannot prove one) — see
    `auth_factors.enrollment_allowed`.
    """
    if enrollment_allowed(user_id, _main.current_session_token(session_token)):
        return
    record_auth_event(AuthAuditEvent.STEP_UP_DENIED, user_id=user_id, success=False,
                      ip=_main._client_ip(http_request), user_agent=_main._user_agent(http_request),
                      details={"action": action})
    raise _step_up_error(user_id)


def _stamp_enrollment(user_id: int, session_token: Optional[str], kind: str) -> None:
    """The ceremony IS a fresh proof of possession, so the session it ran on is now stepped up —
    otherwise a user who just touched their sensor would be asked to touch it again to save
    recovery codes.

    Except on a recovery-code session: that one enrolled WITHOUT proving anything, and handing it
    step-up for free is precisely how a found sheet of codes would become a LinkedIn session. It
    runs the ordinary step-up ceremony with the factor it just enrolled — one extra touch, and an
    audited one.

    Best-effort otherwise: the factor is already stored, so a missed stamp costs one extra prompt,
    not the enrolment. Only the step-up endpoint itself treats a failed stamp as fatal.
    """
    token = _main.current_session_token(session_token)
    if session_signed_in_with_recovery_code(token):
        log_debug("Recovery-code session enrolled a factor — not stamping step-up",
                  user_id=user_id)
        return
    if not record_step_up(token):
        log_warning(f"{kind} enrolled but session step-up stamp failed", user_id=user_id)


@router.get("/auth-factors")
def get_user_auth_factors(session_token: Optional[str] = None) -> ResponseModel[dict[str, Any]]:
    """What the Security card renders: enrolled factors, recovery-code counts, whether this
    deployment can do passkeys at all, and whether the email PIN has been demoted to a bootstrap.
    """
    user_id = _main.get_session_user_id(session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")

    summary = factor_summary(user_id)
    token = _main.current_session_token(session_token)
    return ResponseModel(status_code=200, detail={
        "factors": [{
            "id": f["id"],
            "kind": f["kind"],
            "label": f.get("label"),
            "created_at": _main._utc_iso(f.get("created_at")),
            "last_used_at": _main._utc_iso(f.get("last_used_at")),
        } for f in summary.factors],
        "recovery_codes_unused": summary.recovery_unused,
        "recovery_codes_total": summary.recovery_total,
        "passkeys_supported": summary.passkeys_supported,
        "has_strong_factor": summary.has_strong_factor,
        "pin_is_bootstrap_only": summary.pin_is_bootstrap_only,
        "step_up_satisfied": step_up_satisfied(user_id, token),
        # 2c.1: the card is also what the forced-enrolment gate renders, and it is the one place a
        # user can see the deadline they are being held against.
        "strong_factor_deadline": _main._utc_iso(strong_factor_deadline()),
        "enrollment_required": _main._enrollment_held(),
    })


@router.post("/passkeys/register/begin")
def passkey_register_begin(request: SessionOnlyRequest,
                           http_request: Request = None) -> ResponseModel[dict[str, Any]]:
    """Options for `navigator.credentials.create`. The FIRST factor needs only a session; adding
    another one to an account that already has one is step-up gated, because enrolling stamps the
    session as verified. The lockout case §6.8 worries about — someone who lost the factor they
    had — comes back in on a recovery code, which `enrollment_allowed` lets through by name.
    """
    user_id = _main.get_session_user_id(request.session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    _require_enrollment_allowed(user_id, request.session_token, "enroll_passkey",
                                http_request=http_request)
    _main._passkeys_or_503()

    email = get_user_email(user_id) or f"user-{user_id}"
    options, challenge = build_registration_options(
        user_id=user_id, user_name=email, user_display_name=email,
        existing_credential_ids=get_user_passkey_credential_ids(user_id))
    handle = create_auth_challenge(CHALLENGE_REGISTER, _main._challenge_expiry(),
                                   user_id=user_id, challenge=challenge)
    if not handle:
        raise HTTPException(status_code=500, detail="Could not start passkey registration")
    return ResponseModel(status_code=200, detail={"handle": handle, "options": options})


@router.post("/passkeys/register/complete")
def passkey_register_complete(request: PasskeyRegisterCompleteRequest,
                              http_request: Request = None) -> ResponseModel[dict[str, Any]]:
    """Verify and store a new passkey. The challenge is claimed exactly once, so a replayed
    registration response finds nothing to verify against.
    """
    user_id = _main.get_session_user_id(request.session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    # Gated on BOTH halves of the ceremony: begin is where the options come from, but complete is
    # where the credential actually lands, and a handle obtained before a factor existed must not
    # still be spendable after one does.
    _require_enrollment_allowed(user_id, request.session_token, "enroll_passkey",
                                http_request=http_request)
    _main._passkeys_or_503()

    pending = consume_auth_challenge(request.handle, CHALLENGE_REGISTER)
    if not pending or pending.get("user_id") != user_id:
        raise HTTPException(status_code=400, detail="That registration expired — try again")

    result = verify_passkey_registration(request.credential, pending["challenge"])
    if not result:
        raise HTTPException(status_code=400, detail="That passkey could not be verified")

    factor_id = add_passkey_factor(user_id, result.credential_id, result.public_key,
                                   sign_count=result.sign_count, label=request.label)
    if not factor_id:
        # Deliberately the SAME message as a failed verification. Credential ids are globally
        # unique, so "already registered" would tell a caller that a passkey they hold is enrolled
        # on some OTHER account. db.add_passkey_factor logs the real reason server-side.
        raise HTTPException(status_code=400, detail="That passkey could not be verified")

    _stamp_enrollment(user_id, request.session_token, "Passkey")
    _release_enrollment_hold(user_id, request.session_token)
    record_auth_event(AuthAuditEvent.FACTOR_ADDED, user_id=user_id, ip=_main._client_ip(http_request),
                      user_agent=_main._user_agent(http_request), details={"kind": "passkey"})
    log_info("Passkey enrolled", user_id=user_id)
    return ResponseModel(status_code=200, detail={
        "factor_id": factor_id,
        # First strong factor: from here on an email PIN alone will not sign this account in, and
        # the user needs to be told that BEFORE they close the page without saving recovery codes.
        "recovery_codes_needed": count_recovery_codes(user_id)[0] == 0,
    })


@router.post("/totp/enroll/begin")
def totp_enroll_begin(request: SessionOnlyRequest, http_request: Request = None) -> ResponseModel[dict[str, Any]]:
    """Mint an authenticator-app secret. Returned in the clear exactly once — the row stores it as
    a `lemv1:` envelope bound to this user, so it can never be read back out.
    """
    user_id = _main.get_session_user_id(request.session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    _require_enrollment_allowed(user_id, request.session_token, "enroll_totp",
                                http_request=http_request)

    # One authenticator per account. A second confirmed row would count towards has_strong_factor
    # and show on the Security card while only the newer one's codes are ever checked — and
    # replacing the old seed silently would be a way to take a working factor off the account.
    if has_confirmed_totp(user_id):
        raise HTTPException(status_code=400, detail="An authenticator app is already set up — "
                                                    "remove it before adding another")

    email = get_user_email(user_id) or f"user-{user_id}"
    started = begin_totp_enrollment(user_id, email)
    if not started:
        raise HTTPException(status_code=500, detail="Could not start authenticator setup")
    _factor_id, secret, uri = started
    return ResponseModel(status_code=200, detail={"secret": secret, "otpauth_uri": uri})


@router.post("/totp/enroll/confirm")
def totp_enroll_confirm(request: TotpConfirmRequest, http_request: Request = None) -> ResponseModel[dict[str, Any]]:
    """Turn a pending authenticator secret into a confirmed factor by proving one code.

    Gated on BOTH halves of the ceremony like the passkey path: a secret minted before a factor
    existed must not still be confirmable once one does. Confirming stamps this session as verified
    and releases an enrolment hold, and `recovery_codes_needed` tells the SPA to make the user save
    a sheet BEFORE they close the page — from here an email PIN alone will not sign them in.
    """
    user_id = _main.get_session_user_id(request.session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    _require_enrollment_allowed(user_id, request.session_token, "enroll_totp",
                                http_request=http_request)

    if not confirm_totp_enrollment(user_id, request.code):
        raise HTTPException(status_code=400, detail="That code did not match — check the time on "
                                                    "your phone and try the next one")
    _stamp_enrollment(user_id, request.session_token, "TOTP")
    _release_enrollment_hold(user_id, request.session_token)
    record_auth_event(AuthAuditEvent.FACTOR_ADDED, user_id=user_id, ip=_main._client_ip(http_request),
                      user_agent=_main._user_agent(http_request), details={"kind": "totp"})
    return ResponseModel(status_code=200, detail={
        "recovery_codes_needed": count_recovery_codes(user_id)[0] == 0,
    })


@router.post("/auth-factors/delete")
def delete_user_auth_factor(request: AuthFactorDeleteRequest,
                            http_request: Request = None) -> ResponseModel[dict[str, Any]]:
    """Remove a factor — step-up gated, because removing the thing that protects the account is
    exactly what an attacker holding a stolen session would do first.
    """
    user_id = _main.get_session_user_id(request.session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    _require_step_up(user_id, request.session_token, "delete_auth_factor",
                     http_request=http_request)

    # Read the kind BEFORE the delete — after it the row is gone and the audit row would only be
    # able to say "a factor", which is the one detail that matters when reading these back.
    removed_kind = next((f.get("kind") for f in factor_summary(user_id).factors
                         if f.get("id") == request.factor_id), None)
    if not delete_auth_factor(user_id, request.factor_id):
        raise HTTPException(status_code=404, detail="Factor not found")
    still_strong = has_strong_factor(user_id)
    record_auth_event(AuthAuditEvent.FACTOR_REMOVED, user_id=user_id, ip=_main._client_ip(http_request),
                      user_agent=_main._user_agent(http_request),
                      details={"factor_id": request.factor_id, "kind": removed_kind,
                               "has_strong_factor": still_strong})
    return ResponseModel(status_code=200, detail={"removed": 1,
                                                  "has_strong_factor": still_strong})


@router.post("/recovery-codes/regenerate")
def regenerate_recovery_codes(request: SessionOnlyRequest,
                              http_request: Request = None) -> ResponseModel[dict[str, Any]]:
    """A fresh sheet of single-use codes, shown ONCE. Step-up gated because a new sheet silently
    invalidates the old one — an attacker could otherwise lock the real owner out of their own
    recovery path without ever touching a factor.
    """
    user_id = _main.get_session_user_id(request.session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    _require_step_up(user_id, request.session_token, "regenerate_recovery_codes",
                     http_request=http_request)

    codes = generate_recovery_codes(user_id)
    if not codes:
        raise HTTPException(status_code=500, detail="Could not generate recovery codes")
    record_auth_event(AuthAuditEvent.RECOVERY_CODES_GENERATED, user_id=user_id,
                      ip=_main._client_ip(http_request), user_agent=_main._user_agent(http_request),
                      details={"count": len(codes)})
    return ResponseModel(status_code=200, detail={"codes": codes})


@router.post("/step-up/begin")
def step_up_begin(request: SessionOnlyRequest) -> ResponseModel[dict[str, Any]]:
    """Start a step-up passkey ceremony for the signed-in account. Scoped to the user's OWN
    credentials — a step-up is "prove you are still you", not "log someone in".
    """
    user_id = _main.get_session_user_id(request.session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    _main._passkeys_or_503()

    credential_ids = get_user_passkey_credential_ids(user_id)
    if not credential_ids:
        raise HTTPException(status_code=400, detail="No passkey is enrolled on this account")
    options, challenge = build_authentication_options(credential_ids)
    handle = create_auth_challenge(CHALLENGE_STEP_UP, _main._challenge_expiry(),
                                   user_id=user_id, challenge=challenge)
    if not handle:
        raise HTTPException(status_code=500, detail="Could not start verification")
    return ResponseModel(status_code=200, detail={"handle": handle, "options": options})


@router.post("/step-up/verify")
def step_up_verify(request: StepUpVerifyRequest, http_request: Request = None) -> ResponseModel[dict[str, Any]]:
    """Prove a factor and stamp THIS session as freshly verified.

    A recovery code is deliberately NOT accepted here (design §6.8): it gets you back INTO the
    account and lets you enrol a factor, but it must not by itself unlock the LinkedIn credentials —
    otherwise a stolen recovery sheet is a stolen LinkedIn session.
    """
    user_id = _main.get_session_user_id(request.session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")

    ip = _main._client_ip(http_request)
    user_agent = _main._user_agent(http_request)
    # Keyed per ACCOUNT, never on an empty string: `_check` skips a blank identity, so an account
    # with no email row would otherwise get no per-identity limit at all here.
    email = get_user_email(user_id) or f"user-{user_id}"
    verdict = check_auth_verify(email, ip)
    if not verdict.allowed:
        raise HTTPException(status_code=429, detail="Too many attempts — try again later",
                            headers={"Retry-After": str(verdict.retry_after_seconds)})

    verified = False
    if request.method == METHOD_PASSKEY:
        _main._passkeys_or_503()
        pending = consume_auth_challenge(request.handle or "", CHALLENGE_STEP_UP)
        if pending and pending.get("user_id") == user_id:
            verified = _main._verify_assertion_for_user(request.credential or {}, pending["challenge"],
                                                  expected_user_id=user_id) is not None
    elif request.method == METHOD_TOTP:
        verified = verify_totp_code(user_id, request.code or "")

    if not verified:
        record_auth_event(AuthAuditEvent.SECOND_FACTOR_FAILED, user_id=user_id, ip=ip,
                          user_agent=user_agent, success=False,
                          details={"method": request.method, "stage": "step_up"})
        raise HTTPException(status_code=400, detail="That did not verify — try again")

    if not record_step_up(_main.current_session_token(request.session_token)):
        # The factor verified but the stamp did not land, so the very next write would ask again —
        # an invisible loop. Fail loudly instead of returning a 200 that changed nothing.
        log_error("Step-up verified but session stamp failed", user_id=user_id)
        raise HTTPException(status_code=500, detail="Could not record verification")
    record_auth_event(AuthAuditEvent.STEP_UP_VERIFIED, user_id=user_id, ip=ip,
                      user_agent=user_agent, details={"method": request.method})
    return ResponseModel(status_code=200, detail={"verified": True})


@router.get("/settings", responses={200: {"model": ResponseModel[UserSettingsDetail]}})
def get_user_settings(session_token: str) -> ResponseModel[dict[str, Any]]:
    """The Account page's snapshot: subscription, preferences, blog/sitemap and company page.

    `content_language` and `effective_content_language` are BOTH returned on purpose (issue #548) —
    the explicit setting (None = follow Login Location) and what generation will actually use — so
    the UI can show the inherited default without re-implementing the precedence rules.
    """
    user_id = _main.get_session_user_id(session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")

    subscription = get_user_subscription_info(user_id)
    preferences = get_user_preferences(user_id)
    blog_url = get_user_blog_url(user_id)
    sitemap_url = get_user_sitemap_url(user_id)
    company_linked_in_url = get_company_linked_in_url_for_user(user_id)

    def _iso(dt):
        return dt.isoformat() if dt else None

    return ResponseModel(status_code=200, detail={
        "subscription": {
            "status": subscription.get("subscription_status") if subscription else None,
            "tier": subscription.get("subscription_tier") if subscription else None,
            "trial_started_at": _iso(subscription.get("trial_started_at")) if subscription else None,
            "trial_ends_at": _iso(subscription.get("trial_ends_at")) if subscription else None,
            "stripe_customer_id": subscription.get("stripe_customer_id") if subscription else None,
        } if subscription else None,
        "preferences": {
            "last_login_inactivate_delay": preferences.get("last_login_inactivate_delay") if preferences else 90,
            "auto_schedule_posts": bool(preferences.get("auto_schedule_posts")) if preferences else False,
            "content_buffer_days": preferences.get("content_buffer_days") or DEFAULT_CONTENT_BUFFER_DAYS,
            "content_buffer_max_posts": (preferences.get("content_buffer_max_posts")
                                         or DEFAULT_CONTENT_BUFFER_MAX_POSTS),
            # The explicit setting (None = follow Login Location) plus what generation will
            # actually use, so the UI can show the inherited default without duplicating the
            # precedence rules — issue #548.
            "content_language": preferences.get("content_language"),
            "effective_content_language": get_user_content_language(user_id),
        } if preferences else None,
        "blog_url": blog_url,
        "sitemap_url": sitemap_url,
        "company_linked_in_url": company_linked_in_url,
    })


@router.put("/settings")
def update_user_settings_endpoint(request: UserPreferencesRequest) -> ResponseModel[str]:
    """Save the account-level preferences.

    Only the fields on `UserPreferencesRequest` are touched — engagement targeting/voice/caps are a separate row and
    a separate endpoint.
    """
    user_id = _main.get_session_user_id(request.session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")

    updated = update_user_preferences(
        user_id,
        inactivate_delay=request.last_login_inactivate_delay,
        auto_schedule_posts=request.auto_schedule_posts,
        content_buffer_days=request.content_buffer_days,
        content_buffer_max_posts=request.content_buffer_max_posts,
        content_language=request.content_language,
    )
    if not updated:
        raise HTTPException(status_code=500, detail="Could not update preferences")
    return ResponseModel(status_code=200, detail="Preferences updated")


@router.get("/linkedin-profile-skills")
def get_linkedin_profile_skills_endpoint(session_token: str) -> ResponseModel[dict[str, Any]]:
    """Return the cached profile's top-5 skills and their overlap with declared focus topics.

    Read-only and best-effort: a missing or unparseable profile returns an empty list, never an
    error, so the Settings page always renders.
    """
    user_id = _main.get_session_user_id(session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    try:
        profile = load_profile_for_user(user_id)
    except Exception:
        profile = None
    skills = profile_niche_anchors(profile) if profile else []
    prefs = get_engagement_preferences(user_id)
    focus = [t.strip().lower() for t in (prefs.get("focus_topics") or []) if str(t).strip()]
    adopted = [s for s in skills if s.strip().lower() in focus]
    return ResponseModel(status_code=200, detail={
        "skills": skills,
        "adopted": adopted,
        "focus_topics": prefs.get("focus_topics") or [],
    })


@router.get("/engagement-preferences",
            responses={200: {"model": ResponseModel[EngagementPreferencesDetail]}})
def get_engagement_preferences_endpoint(session_token: str) -> ResponseModel[dict[str, Any]]:
    """The saved engagement preferences, plus the read-only context the Settings hub renders.

    That context is the gate defaults, the plan's catch-up ceiling, the forwarding address, and the
    last feed scan's reach funnel.

    Every one of those extras is wrapped so it cannot fail the page. `has_saved_preferences`
    defaults to True when unreadable in particular: it decides whether a brand-new account is
    started on the Balanced preset, and a hiccup must never make a returning user look brand new
    and get their saved values overwritten (issue #558).
    """
    user_id = _main.get_session_user_id(session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    prefs = get_engagement_preferences(user_id)
    # Read-only: has this user ever saved settings? The Settings hub starts a brand-new account on
    # the Balanced preset and leaves every existing account's saved values alone (issue #558).
    # Unreadable → report "configured", so a hiccup can never make a returning user look brand new.
    try:
        prefs["has_saved_preferences"] = has_engagement_preferences(user_id)
    except Exception:
        prefs["has_saved_preferences"] = True
    # Read-only: the address the user forwards LinkedIn comment-notification emails to (event mode).
    try:
        from cqc_lem.integrations.linkedin.notification_email import reply_inbound_address
        token = get_or_create_reply_inbound_token(user_id)
        prefs["reply_inbound_address"] = reply_inbound_address(token) if token else None
    except Exception:
        prefs["reply_inbound_address"] = None
    # Gmail forwarding auto-confirmation status (so the UI can surface the code if auto-confirm failed).
    prefs["gmail_forward_confirmation"] = _main.get_gmail_forward_confirmation(user_id)
    # Read-only: the highest catch-up cap this plan allows, so the UI can bound the input and show
    # what upgrading unlocks (10/day is premium-only).
    prefs["max_catchup_touches_allowed"] = max_catchup_touches_allowed(user_id)
    # Read-only: bounds for the per-contact catch-up frequency guard (issue #1078).
    prefs["catchup_contact_interval_bounds"] = {
        "min_days": CATCHUP_MIN_CONTACT_INTERVAL_DAYS_MIN,
        "max_days": CATCHUP_MIN_CONTACT_INTERVAL_DAYS_MAX,
    }
    prefs["catchup_per_contact_cap_bounds"] = {
        "min": CATCHUP_MAX_PER_CONTACT_DAYS_MIN,
        "max": CATCHUP_MAX_PER_CONTACT_DAYS_MAX,
    }
    # Read-only: the deploy-wide gate thresholds, so the UI can show what "default" actually means
    # for a user who hasn't overridden them (issue #421).
    from cqc_lem.utilities.ai.content_alignment import authenticity_score_min
    from cqc_lem.utilities.ai.content_framework import post_similarity_max
    prefs["gate_defaults"] = {
        "authenticity_score_min": authenticity_score_min(),
        "post_similarity_max_pct": round(post_similarity_max() * 100),
    }
    # Read-only: the last feed scan's reach funnel so the user can see when their targeting is too
    # strict (posts examined -> matched their filters -> commented).
    try:
        from cqc_lem.app.engagement.feed import get_feed_funnel
        prefs["feed_reach"] = get_feed_funnel(user_id)
    except Exception:
        prefs["feed_reach"] = None
    return ResponseModel(status_code=200, detail=prefs)


@router.put("/engagement-preferences")
def update_engagement_preferences_endpoint(request: EngagementPreferencesRequest) -> ResponseModel[str]:
    """Save targeting, voice and the per-day caps — and refuse an `agent`-scoped token outright.

    Scope surfaces match on PATH, so granting the agent the read granted this write with it. This
    is the one write that re-opens everything else: it sets `connection_request_mode` /
    `catchup_touch_mode` (either one flipped to `auto_approve` restores the passive approval
    bypass) and the caps bounding every outbound lane. A token that cannot approve ONE item must
    not be able to configure the account into approving all of them (issue #1026).
    """
    user_id = _main.get_session_user_id(request.session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    # READ-ONLY for an agent (issue #1026). The scope surface matches on PATH, so the entry added so
    # the agent could read whether automation is safe to queue for granted this write along with it
    # — and this is the one write that re-opens everything else: it sets connection_request_mode /
    # catchup_touch_mode (flipping either to auto_approve restores the passive approval bypass) and
    # the per-day caps that bound every outbound lane. A token that cannot approve one item must not
    # be able to configure the account into approving all of them.
    if _main._agent_scoped():
        raise HTTPException(status_code=403, detail={
            "code": "agent_may_not_configure",
            "message": "This token can read engagement preferences but cannot change them.",
        })
    prefs = request.model_dump(exclude={"session_token"})
    # NULL is "never chosen" for the follow cap (issue #962), and the upsert writes every column
    # from this dict — so an omitted field is dropped here rather than merged as the code default,
    # which would overwrite a deliberate 0 and restart an outbound lane the user had switched off.
    if prefs.get("max_follows_per_day") is None:
        prefs.pop("max_follows_per_day", None)
    if not update_engagement_preferences(user_id, prefs):
        raise HTTPException(status_code=500, detail="Could not update engagement preferences")
    return ResponseModel(status_code=200, detail="Engagement preferences updated")


@router.get("/newsletter-settings",
            responses={200: {"model": ResponseModel[NewsletterSettingsDetail]}})
def get_newsletter_settings_endpoint(session_token: str) -> ResponseModel[dict[str, Any]]:
    """The caller's newsletter settings row, defaults filled in by `get_newsletter_settings`."""
    user_id = _main.get_session_user_id(session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    return ResponseModel(status_code=200, detail=get_newsletter_settings(user_id))


@router.get("/newsletter-subscribers",
            responses={200: {"model": ResponseModel[NewsletterSubscribersDetail]}})
def get_newsletter_subscribers_endpoint(session_token: str) -> ResponseModel[dict[str, Any]]:
    """Subscriber-growth time-series for the current user (issue #400): the recorded snapshots plus
    the latest known subscriber count, for charting growth over time.

    `attribution` (issue #624) is what that growth can be read against: the owned-asset CTAs that
    actually delivered something in the same window — approval-gated lead-magnet DMs, and posts that
    carried the subscribe link into their first comment.
    """
    user_id = _main.get_session_user_id(session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    from cqc_lem.utilities.db import (
        count_artifact_cta_deliveries,
        get_latest_newsletter_subscriber_count,
        get_newsletter_subscriber_stats,
    )
    newsletter_url = (get_newsletter_settings(user_id) or {}).get("newsletter_url")
    return ResponseModel(status_code=200, detail={
        "latest": get_latest_newsletter_subscriber_count(user_id),
        "history": get_newsletter_subscriber_stats(user_id),
        "attribution": count_artifact_cta_deliveries(user_id, newsletter_url=newsletter_url),
    })


@router.put("/newsletter-settings")
def update_newsletter_settings_endpoint(request: NewsletterSettingsRequest) -> ResponseModel[str]:
    """Save the newsletter settings and, when the feature is on, top the draft queue up NOW.

    The top-up is what makes a raised `max_queued_drafts` visible immediately instead of at the
    next daily beat. Idempotent — an already-full queue generates nothing — so a repeated save
    cannot run up generation spend.
    """
    user_id = _main.get_session_user_id(request.session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    if not update_newsletter_settings(user_id, request.model_dump(exclude={"session_token"})):
        raise HTTPException(status_code=500, detail="Could not update newsletter settings")
    # Top up the review queue now so a raised max_queued_drafts adds drafts immediately instead of
    # waiting for the daily beat. Idempotent: a full queue generates nothing.
    if request.enabled:
        from cqc_lem.app.run_scheduler import generate_newsletter_drafts_for_user
        generate_newsletter_drafts_for_user.apply_async(kwargs={"user_id": user_id})
    return ResponseModel(status_code=200, detail="Newsletter settings updated")


def _compute_next_publish(user_id: int, anchor=None):
    """Next scheduled publish datetime (naive UTC) after `anchor`, or None. When `anchor` is None the
    user's last_published_at is used, giving the soonest upcoming slot.
    """
    from datetime import datetime as _dt, timezone as _tz

    import pytz

    from cqc_lem.utilities.newsletter import next_publish_datetime
    settings = get_newsletter_settings(user_id)
    try:
        tz = pytz.timezone(get_user_timezone(user_id))
    except Exception:
        tz = pytz.utc
    if anchor is None:
        anchor = settings.get("last_published_at")
    return next_publish_datetime(
        settings.get("publish_day", 1), settings.get("publish_hour", 9),
        settings.get("cadence", "weekly"), anchor, tz,
        _dt.now(_tz.utc).replace(tzinfo=None))  # naive UTC — compared to naive DB datetimes


@router.get("/newsletter-draft", responses={200: {"model": ResponseModel[NewsletterDraftDetail]}})
def get_newsletter_draft_endpoint(session_token: str) -> ResponseModel[dict[str, Any]]:
    """The newsletter review queue.

    `next_publish` is the slot AFTER the last edition already queued, so it answers "when would a NEW draft go out",
    not "when is the next send".

    A cover leaves here as a URL and its filesystem path is popped — the SPA must never be handed
    a server path for an asset it only ever renders.
    """
    user_id = _main.get_session_user_id(session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    from cqc_lem.utilities.newsletter_cover import cover_public_url
    editions = get_pending_newsletter_editions(user_id)
    for e in editions:
        if e.get("scheduled_for") is not None:
            e["scheduled_for"] = _main._utc_iso(e["scheduled_for"])
        # The SPA renders the cover from a URL, never a filesystem path.
        e["cover_image_url"] = cover_public_url(e.get("cover_image_path"))
        e.pop("cover_image_path", None)
    # next_publish is the slot AFTER the last edition already queued, so the UI can show what's next.
    anchor = get_latest_edition_scheduled_for(user_id)
    next_pub = _compute_next_publish(user_id, anchor=anchor)
    settings = get_newsletter_settings(user_id)
    return ResponseModel(status_code=200, detail={
        "editions": editions,
        "next_publish": _main._utc_iso(next_pub),
        "max_queued_drafts": settings.get("max_queued_drafts", 1),
        "generate_lead_days": settings.get("generate_lead_days", 3),
    })


def _should_rebrief_cover(existing: dict, request: "NewsletterDraftRequest") -> bool:
    """True when the edited fields change the opening text of an AI-generated cover.

    Only title or subtitle edits trigger a re-brief: they are the text the cover brief reads
    alongside the body, and the cover is generated before final edits. Body-only edits are left
    to the author to decide — they may be deep in the newsletter and not change the visual idea.
    Uploads are never re-briefed: the author chose that artwork themselves.
    """
    from cqc_lem.utilities.newsletter_cover import COVER_SOURCE_AI

    if existing.get("cover_image_source") != COVER_SOURCE_AI:
        return False
    if existing.get("cover_image_status") is None:
        return False
    title = request.title
    subtitle = request.subtitle
    if title is None and subtitle is None:
        return False
    existing_title = (existing.get("title") or "").strip()
    existing_subtitle = (existing.get("subtitle") or "").strip()
    new_title = (title if title is not None else existing.get("title") or "").strip()
    new_subtitle = (subtitle if subtitle is not None else existing.get("subtitle") or "").strip()
    return new_title != existing_title or new_subtitle != existing_subtitle


@router.put("/newsletter-draft")
def update_newsletter_draft_endpoint(request: NewsletterDraftRequest) -> ResponseModel[str]:
    """Edit a queued edition, and optionally approve or skip it.

    An unrecognised `action` maps to no status change, so it saves the fields rather than
    publishing on a typo. Ownership is checked against the edition's own `user_id` — a foreign
    edition id is a 404, never an edit.

    When a title/subtitle edit lands on an AI-generated cover, the cover is re-briefed from the
    updated opening text (issue #1287). The old AI cover file is removed, a new generation is
    queued, and the result still lands `pending_review` — the hard-approval gate is unchanged.
    """
    user_id = _main.get_session_user_id(request.session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    existing = get_newsletter_edition(request.edition_id)
    if not existing or existing.get("user_id") != user_id:
        raise HTTPException(status_code=404, detail="Edition not found")
    status = {"approve": "approved", "skip": "skipped"}.get(request.action)  # None for 'save'
    rebrief = _should_rebrief_cover(existing, request)
    if not update_newsletter_edition(request.edition_id, user_id, title=request.title,
                                     subtitle=request.subtitle, body=request.body, status=status,
                                     scheduled_for=request.scheduled_datetime):
        raise HTTPException(status_code=500, detail="Could not update newsletter draft")
    if rebrief:
        from cqc_lem.app.run_scheduler import generate_newsletter_cover
        from cqc_lem.utilities.db import clear_edition_cover_image
        from cqc_lem.utilities.newsletter_cover import remove_cover_file

        previous_path = existing.get("cover_image_path")
        if clear_edition_cover_image(request.edition_id, user_id):
            if previous_path:
                remove_cover_file(previous_path)
        else:
            log_warning("Could not clear stale AI cover before re-brief",
                        user_id=user_id, edition_id=request.edition_id,
                        action_type="newsletter_cover")
        generate_newsletter_cover.apply_async(
            kwargs={"edition_id": request.edition_id, "use_avatar": None})
        log_info("Re-briefing newsletter cover after title/subtitle edit",
                 user_id=user_id, edition_id=request.edition_id,
                 action_type="newsletter_cover")
    return ResponseModel(status_code=200, detail="Newsletter draft updated")


@router.post("/newsletter-draft/regenerate")
def regenerate_newsletter_draft_endpoint(request: NewsletterRegenerateRequest) -> ResponseModel[str]:
    """Regenerate a single queued edition. Generation is a slow lem-complex call, so dispatch it to a
    Celery task and let the UI refetch the queue once it lands. Optional free-text `guidance` steers
    the rewrite; empty guidance lets the AI decide a fresh, distinct take.
    """
    user_id = _main.get_session_user_id(request.session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    existing = get_newsletter_edition(request.edition_id)
    if not existing or existing.get("user_id") != user_id:
        raise HTTPException(status_code=404, detail="Edition not found")
    from cqc_lem.app.run_scheduler import regenerate_newsletter_edition
    guidance = (request.guidance or "").strip() or None
    regenerate_newsletter_edition.apply_async(
        kwargs={"edition_id": request.edition_id, "guidance": guidance})
    return ResponseModel(status_code=200, detail="Regeneration started")


def _owned_edition(session_token: str, edition_id: int) -> "tuple[int, dict]":
    """Resolve the session and the edition it may touch, or raise. A foreign edition is a 404 (not
    a 403) so the endpoint never confirms that another user's edition id exists.
    """
    user_id = _main.get_session_user_id(session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    edition = get_newsletter_edition(edition_id)
    if not edition or edition.get("user_id") != user_id:
        raise HTTPException(status_code=404, detail="Edition not found")
    return user_id, edition


def _cover_detail(edition_id: int) -> dict:
    """The cover state the SPA re-renders from after any cover action."""
    from cqc_lem.utilities.newsletter_cover import cover_public_url
    edition = get_newsletter_edition(edition_id) or {}
    return {
        "edition_id": edition_id,
        "cover_image_url": cover_public_url(edition.get("cover_image_path")),
        "cover_image_source": edition.get("cover_image_source"),
        "cover_image_status": edition.get("cover_image_status"),
    }


@router.post("/newsletter-draft/cover", responses={
    200: {"description": "Cover uploaded"},
    **{k: v for k, v in error_responses.items() if k in [400, 401, 404]},
    500: {"description": "Server error"}
})
async def upload_newsletter_cover_endpoint(
    session_token: str = Form(...),
    edition_id: int = Form(...),
    file: UploadFile = File(...),
) -> ResponseModel[dict[str, Any]]:
    """Attach the author's OWN cover artwork to a queued edition (issue #893).

    Their artwork needs no review, so it lands 'approved' and publishes with the edition — this is
    the half of the feature that works standalone for a user who never touches AI generation. It
    still passes the deterministic cover gate, so an unreadable or portrait file is a 400 here
    rather than a broken cover at publish time.
    """
    from cqc_lem.utilities.db import set_edition_cover_image
    from cqc_lem.utilities.newsletter_cover import (
        COVER_SOURCE_UPLOAD,
        COVER_STATUS_APPROVED,
        CoverRejected,
        remove_cover_file,
        save_cover_bytes,
    )
    user_id, edition = _owned_edition(session_token, edition_id)
    data = await file.read()
    try:
        relative = save_cover_bytes(user_id, edition_id, data)
    except CoverRejected as e:
        raise HTTPException(status_code=400, detail=str(e))
    except OSError as e:
        log_info(f"newsletter cover upload failed for edition {edition_id} — {e}")
        raise HTTPException(status_code=500, detail="Could not store the cover image")
    previous = edition.get("cover_image_path")
    if not set_edition_cover_image(edition_id, user_id, relative, COVER_SOURCE_UPLOAD,
                                   COVER_STATUS_APPROVED):
        remove_cover_file(relative)  # don't leave an orphan file behind a failed write
        raise HTTPException(status_code=500, detail="Could not save the cover image")
    if previous and previous != relative:
        remove_cover_file(previous)
    return ResponseModel(status_code=200, detail=_cover_detail(edition_id))


@router.post("/newsletter-draft/cover/generate", responses={
    200: {"description": "Cover generation started"},
    **{k: v for k, v in error_responses.items() if k in [401, 404]}
})
def generate_newsletter_cover_endpoint(request: NewsletterCoverRequest) -> ResponseModel[str]:
    """Generate a cover for ONE edition. Image generation is slow and costs money, so it runs as a
    Celery task and the result lands 'pending_review' for the author to approve.
    """
    user_id, _edition = _owned_edition(request.session_token, request.edition_id)
    from cqc_lem.app.run_scheduler import generate_newsletter_cover
    generate_newsletter_cover.apply_async(kwargs={"edition_id": request.edition_id,
                                                  "use_avatar": request.use_avatar})
    log_info(f"newsletter cover generation queued for edition {request.edition_id} (user {user_id})")
    return ResponseModel(status_code=200, detail="Cover generation started")


@router.post("/newsletter-draft/cover/decision", responses={
    200: {"description": "Cover decision applied"},
    **{k: v for k, v in error_responses.items() if k in [400, 401, 404]},
    500: {"description": "Server error"}
})
def decide_newsletter_cover_endpoint(request: NewsletterCoverDecisionRequest) -> ResponseModel[dict[str, Any]]:
    """The human half of the cover gate: 'approve' clears a generated cover for publish, 'remove'
    drops it (file included) so the edition publishes with no cover at all.
    """
    from cqc_lem.utilities.db import clear_edition_cover_image, set_edition_cover_status
    from cqc_lem.utilities.newsletter_cover import COVER_STATUS_APPROVED, remove_cover_file
    user_id, edition = _owned_edition(request.session_token, request.edition_id)
    if not edition.get("cover_image_path"):
        raise HTTPException(status_code=404, detail="This edition has no cover image")
    if request.action == "remove":
        if not clear_edition_cover_image(request.edition_id, user_id):
            raise HTTPException(status_code=500, detail="Could not remove the cover image")
        remove_cover_file(edition.get("cover_image_path"))
    elif request.action == "approve":
        if not set_edition_cover_status(request.edition_id, user_id, COVER_STATUS_APPROVED):
            raise HTTPException(status_code=500, detail="Could not approve the cover image")
    else:
        raise HTTPException(status_code=400, detail="Unknown cover action")
    return ResponseModel(status_code=200, detail=_cover_detail(request.edition_id))


@router.post("/post/regenerate")
def regenerate_post_endpoint(request: PostRegenerateRequest) -> ResponseModel[str]:
    """Regenerate a single pending/approved/rejected post. Generation is a slow lem-complex call,
    so dispatch it to a Celery task; the post resets to 'pending' for re-review. Optional free-text
    `guidance` steers the rewrite while the base regeneration honors the user's saved engagement
    settings. Works for text, carousel, document, and video posts (issue #794).
    """
    user_id = _main.get_session_user_id(request.session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    if get_post_user_id(request.post_id) != user_id:
        raise HTTPException(status_code=404, detail="Post not found")
    # Regeneration resets the post to PENDING — sensible from the review states and from rejected,
    # where the stored rejection reason becomes the default guidance so the same issue is avoided.
    post_status = get_post_status(request.post_id)
    if post_status not in (PostStatus.PENDING.value, PostStatus.APPROVED.value, PostStatus.REJECTED.value):
        raise HTTPException(
            status_code=409,
            detail=f"Post is '{post_status}' — only pending, approved, or rejected posts can be regenerated")
    from cqc_lem.app.run_content_plan import regenerate_post_task
    from cqc_lem.utilities.db import get_post_rejection_reason
    guidance = (request.guidance or "").strip() or None
    # A rejected post with no explicit guidance inherits its stored rejection reason.
    if guidance is None and post_status == PostStatus.REJECTED.value:
        guidance = get_post_rejection_reason(request.post_id)
    regenerate_post_task.apply_async(kwargs={"post_id": request.post_id, "guidance": guidance})
    return ResponseModel(status_code=200, detail="Regeneration started")


# How far out an occasion draft is scheduled when the author names no date: far enough to be
# reviewed and edited, near enough that a launch announcement is still news.
OCCASION_DEFAULT_LEAD_HOURS = 24


@router.post("/post/occasion", responses={
    200: {"description": "Occasion draft queued"},
    **{k: v for k, v in error_responses.items() if k in [400, 401, 403]},
    500: {"description": "Could not create the draft"},
})
def create_occasion_post_endpoint(request: OccasionPostRequest) -> ResponseModel[dict[str, Any]]:
    """Seed ONE occasion/milestone draft for the caller, written to the named archetype.

    The row is created up front (so the Content Studio shows it immediately) and marked
    `manual_publish`, which is what permanently keeps the scheduler and `post_to_linkedin` off it —
    the author publishes it through LinkedIn's native occasion composer, which is the only place the
    occasion entity exists. Drafting itself is a slow LLM call, so it runs in Celery.
    """
    from cqc_lem.utilities.ai.content_framework import occasion_formats, occasion_stage

    user_id = _main.require_session_user_id(request.session_token)

    archetype = (request.archetype or "").strip().lower()
    if archetype not in occasion_formats("post"):
        raise HTTPException(
            status_code=400,
            detail=f"Unknown occasion type '{request.archetype}' "
                   f"(known: {', '.join(occasion_formats('post'))})")
    occasion = request.occasion.strip()
    if len(occasion) < 10:
        raise HTTPException(status_code=400,
                            detail="Describe the occasion — it is the only fact the draft may state")

    scheduled = request.scheduled_datetime
    if scheduled is None:
        scheduled = datetime.now(timezone.utc) + timedelta(hours=OCCASION_DEFAULT_LEAD_HOURS)
    else:
        _main._warn_if_naive_schedule(scheduled, "/user/post/occasion", user_id=user_id)

    post_id = insert_occasion_post(user_id, scheduled, occasion_stage("post", archetype))
    if not post_id:
        log_error("Could not create the occasion post row", user_id=user_id)
        raise HTTPException(status_code=500, detail="Could not create the draft")

    from cqc_lem.app.run_content_plan import draft_occasion_post_task
    draft_occasion_post_task.apply_async(
        kwargs={"post_id": post_id, "archetype": archetype, "occasion": occasion})
    return ResponseModel(status_code=200, detail={"post_id": post_id, "archetype": archetype,
                                                 "manual_publish": True})


@router.post("/post/mark-posted", responses={
    200: {"description": "Post marked as published"},
    **{k: v for k, v in error_responses.items() if k in [401, 403, 404]},
    409: {"description": "Post does not publish natively, or is already published"},
    500: {"description": "Could not update the post"},
})
def mark_post_posted_endpoint(request: PostMarkPostedRequest) -> ResponseModel[str]:
    """Record that the caller published a `manual_publish` draft by hand.

    Deliberately narrow: only a native-publish draft can be marked this way, because for every
    other post 'posted' is written by the publish task that has the LinkedIn URN to prove it. A
    post already marked is a 409, not a silent no-op — the author needs to know which of two clicks
    landed.
    """
    user_id = _main.require_session_user_id(request.session_token)
    if get_post_user_id(request.post_id) != user_id:
        raise HTTPException(status_code=404, detail="Post not found")
    if not get_post_manual_publish(request.post_id):
        raise HTTPException(status_code=409,
                            detail="This post publishes automatically — LEM marks it posted itself")
    if get_post_status(request.post_id) == PostStatus.POSTED.value:
        raise HTTPException(status_code=409, detail="This post is already marked as published")
    if not bulk_update_posts([request.post_id], status=PostStatus.POSTED, user_id=user_id):
        log_error("Could not mark the occasion post as published",
                  user_id=user_id, post_id=request.post_id)
        raise HTTPException(status_code=500, detail="Could not update the post")
    return ResponseModel(status_code=200, detail="Post marked as published")


@router.post("/post/rescore")
def rescore_post_endpoint(request: PostRescoreRequest) -> ResponseModel[dict[str, Any]]:
    """Re-run the quality gates on a pending/approved post's CURRENT content (issue #421) — the
    'edit & re-score' half of the review flow. Save the edit first, then call this: a draft that now
    clears every gate is promoted PENDING -> APPROVED without a full regenerate, and one that still
    fails comes back with a fresh reason + remediation. Runs inline (one judge call) so the UI can
    show the verdict immediately.
    """
    user_id = _main.get_session_user_id(request.session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    if get_post_user_id(request.post_id) != user_id:
        raise HTTPException(status_code=404, detail="Post not found")
    post_status = get_post_status(request.post_id)
    if post_status not in (PostStatus.PENDING.value, PostStatus.APPROVED.value):
        raise HTTPException(
            status_code=409,
            detail=f"Post is '{post_status}' — only pending or approved posts can be re-scored")
    from cqc_lem.app.run_content_plan import rescore_post
    try:
        result = rescore_post(request.post_id)
    except Exception as e:
        log_error("Could not re-score post", exc=e, user_id=user_id, post_id=request.post_id)
        raise HTTPException(status_code=500, detail="Could not re-score this post")
    return ResponseModel(status_code=200, detail=result)


def _post_open_to_image_edits(session_token: Optional[str], post_id: int) -> int:
    """The caller's user_id for a post they own and can still change the image on.

    A published post's image is already on LinkedIn — changing the row would only make the queue
    disagree with what shipped, so that is a 409 rather than a silently useless write.
    """
    user_id = _main.require_session_user_id(session_token)
    if get_post_user_id(post_id) != user_id:
        raise HTTPException(status_code=404, detail="Post not found")
    if get_post_status(post_id) == PostStatus.POSTED.value:
        raise HTTPException(status_code=409,
                            detail="This post is already published — its image can't be changed")
    return user_id


def _attach_post_image(user_id: int, post_id: int, image_url: str) -> None:
    """Point the row at a newly stored image and drop the file it replaced."""
    previous = get_post_image_url(post_id)
    if not update_db_post_image_url(post_id, image_url):
        remove_post_image_file(image_url)  # don't leave an orphan behind a failed write
        log_error("Could not store the post image URL", user_id=user_id, post_id=post_id)
        raise HTTPException(status_code=500, detail="Could not save the image")
    if previous and previous != image_url:
        remove_post_image_file(previous)


@router.post("/post/image", responses={
    200: {"description": "Image attached"},
    **{k: v for k, v in error_responses.items() if k in [400, 401, 403, 404]},
    409: {"description": "Post is already published"},
    500: {"description": "Server error"},
})
async def upload_post_image_endpoint(
    session_token: str = Form(...),
    post_id: Optional[int] = Form(default=None),
    file: UploadFile = File(...),
) -> ResponseModel[dict[str, Any]]:
    """Attach the author's OWN image to a post — or, with no `post_id`, to a draft still being
    composed, which hands back a preview URL to pass to `/schedule_post/`.

    The bytes pass the deterministic gate first, so an unreadable or tiny file is a 400 here rather
    than a share LinkedIn refuses at publish time.
    """
    if post_id:
        user_id = _post_open_to_image_edits(session_token, post_id)
    else:
        user_id = _main.require_session_user_id(session_token)

    data = await file.read()
    try:
        image_url = save_post_image_bytes(user_id, data, post_id=post_id)
    except PostImageRejected as e:
        raise HTTPException(status_code=400, detail=str(e))
    except OSError as e:
        log_error("Could not store an uploaded post image", exc=e, user_id=user_id,
                  post_id=post_id)
        raise HTTPException(status_code=500, detail="Could not store the image")

    if post_id:
        _attach_post_image(user_id, post_id, image_url)
    return ResponseModel(status_code=200, detail={"post_id": post_id, "image_url": image_url})


@router.post("/post/image/generate", responses={
    200: {"description": "Image generated"},
    **{k: v for k, v in error_responses.items() if k in [400, 401, 403, 404]},
    409: {"description": "Post is already published"},
    429: {"description": "Hourly generation limit reached"},
    502: {"description": "Generation failed upstream"},
})
def generate_post_image_endpoint(request: PostImageGenerateRequest) -> ResponseModel[dict[str, Any]]:
    """Render an image for a post from its text, through the SAME brief + gated renderer the
    scheduled path uses. Runs inline (one render) so the studio can show the result immediately.

    `content` wins over the stored row: the author is usually looking at an edit they have not
    saved yet, and drawing the image from stale text is the one way this feature is confusing.
    """
    if request.post_id:
        user_id = _post_open_to_image_edits(request.session_token, request.post_id)
        text = (request.content or "").strip() or (get_post_content(request.post_id) or "")
    else:
        user_id = _main.require_session_user_id(request.session_token)
        text = (request.content or "").strip()

    if not text.strip():
        raise HTTPException(status_code=400,
                            detail="Write the post content first — the image is drawn from it")
    if not claim_manual_generation(user_id):
        raise HTTPException(
            status_code=429,
            detail="You've generated a lot of images in the last hour — try again shortly.")

    image_url, reason = generate_image_for_post(user_id, text, post_id=request.post_id)
    if not image_url:
        # 502, not 500: the render is an upstream call, and telling the user "the image service
        # didn't answer" is both true and actionable (try again) where "server error" is neither.
        raise HTTPException(status_code=502, detail=reason or "Could not generate an image")

    if request.post_id:
        _attach_post_image(user_id, request.post_id, image_url)
    return ResponseModel(status_code=200,
                         detail={"post_id": request.post_id, "image_url": image_url})


_VIDEO_UPLOAD_CHUNK_BYTES = 1024 * 1024


async def _spool_upload(file: UploadFile, limit: int) -> tuple[str, int]:
    """Stream an upload into a temp file, stopping one byte past `limit`. Returns `(path, bytes)`.

    Read in chunks rather than `await file.read()`: a post video is two orders of magnitude larger
    than an image, so holding the whole upload in memory to find out it is over the cap is the one
    way this endpoint takes the API container down. The caller owns the temp file from here.
    """
    fd, path = tempfile.mkstemp(prefix="post_video_")
    total = 0
    try:
        with os.fdopen(fd, "wb") as out:
            while chunk := await file.read(_VIDEO_UPLOAD_CHUNK_BYTES):
                total += len(chunk)
                if total > limit:
                    break
                out.write(chunk)
    except Exception:
        _drop_temp_upload(path)
        raise
    return path, total


def _drop_temp_upload(path: Optional[str]) -> None:
    """Delete a spooled upload. Best-effort — a leftover temp file is not worth a 500."""
    if not path:
        return
    try:
        os.remove(path)
    except OSError as e:
        log_debug("Could not delete a spooled upload", error=str(e), action_type="post_video")


@router.post("/post/video", responses={
    200: {"description": "Video stored"},
    **{k: v for k, v in error_responses.items() if k in [400, 401]},
    500: {"description": "Server error"},
})
async def upload_post_video_endpoint(
    session_token: str = Form(...),
    file: UploadFile = File(...),
) -> ResponseModel[dict[str, Any]]:
    """Store the author's own video as a compose-time preview and hand back its URL.

    The URL is what `PUT /user/group-post-draft` takes to attach a video to the weekly group post
    (issue #1443) — the same shape `POST /user/post/image` hands back for an image, and gated the
    same way when it comes back in. The file passes the video contract (container, size, and —
    where ffprobe is installed — duration, frame size and codec) BEFORE it is stored, so a file the
    group composer would refuse is a 400 here rather than an empty media frame on a published post.
    """
    user_id = _main.require_session_user_id(session_token)
    spooled = None
    try:
        spooled, size = await _spool_upload(file, MAX_POST_VIDEO_BYTES)
        if size > MAX_POST_VIDEO_BYTES:
            raise HTTPException(
                status_code=400,
                detail=f"Video is larger than {MAX_POST_VIDEO_BYTES // (1024 * 1024)} MB")
        video_url = save_post_video_file(user_id, spooled)
        spooled = None  # save_post_video_file moved it
    except PostVideoRejected as e:
        raise HTTPException(status_code=400, detail=str(e))
    except OSError as e:
        log_error("Could not store an uploaded post video", exc=e, user_id=user_id)
        raise HTTPException(status_code=500, detail="Could not store the video")
    finally:
        _drop_temp_upload(spooled)
    return ResponseModel(status_code=200, detail={"video_url": video_url})


@router.post("/post/image/remove", responses={
    200: {"description": "Image removed"},
    **{k: v for k, v in error_responses.items() if k in [401, 403, 404]},
    409: {"description": "Post is already published"},
    500: {"description": "Server error"},
})
def remove_post_image_endpoint(request: PostImageRemoveRequest) -> ResponseModel[dict[str, Any]]:
    """Take the image off a post so it publishes as plain text. The file goes with it."""
    user_id = _post_open_to_image_edits(request.session_token, request.post_id)
    previous = get_post_image_url(request.post_id)
    if not update_db_post_image_url(request.post_id, None):
        log_error("Could not clear the post image", user_id=user_id, post_id=request.post_id)
        raise HTTPException(status_code=500, detail="Could not remove the image")
    remove_post_image_file(previous)
    return ResponseModel(status_code=200, detail={"post_id": request.post_id, "image_url": None})


class GroupTogglesRequest(BaseModel):
    """Body of `PUT /user/groups`.

    `groups` accepts BOTH shapes for compatibility (issue #769) — a bare bool is the old commenting toggle; the dict
    form also carries `post_enabled`.
    """

    session_token: str
    # {group_id: enabled} or {group_id: {"enabled": bool, "post_enabled": bool}} (issue #769)
    groups: dict = {}


@router.get("/groups", responses={200: {"model": ResponseModel[List[UserGroup]]}})
def get_user_groups_endpoint(session_token: str) -> ResponseModel[list[dict[str, Any]]]:
    """The user's LinkedIn groups and their per-group toggles.

    Which group the next weekly group post lands in is marked ON the row (`is_next_post`) rather
    than returned beside the list, so an SPA bundle still open from before a deploy keeps reading
    `detail` as the plain array it expects (issue #743).
    """
    user_id = _main.get_session_user_id(session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    groups = get_user_groups(user_id)
    # Which group the next weekly group post lands in — marked on the row rather than returned
    # beside the list, so an older SPA bundle still reads `detail` as a plain array (issue #743).
    nxt = get_next_group_for_post(user_id)
    next_gid = nxt.get("group_id") if nxt else None
    for g in groups:
        g["is_next_post"] = g.get("group_id") == next_gid
    return ResponseModel(status_code=200, detail=groups)


@router.put("/groups")
def update_user_groups_endpoint(request: GroupTogglesRequest) -> ResponseModel[str]:
    """Save the per-group commenting/posting toggles."""
    user_id = _main.get_session_user_id(request.session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    if not set_groups_enabled(user_id, request.groups):
        raise HTTPException(status_code=500, detail="Could not update group settings")
    return ResponseModel(status_code=200, detail="Group settings updated")


class GroupPostDraftUpdateRequest(BaseModel):
    """Body of `PUT /user/group-post-draft` (issues #932, #1224).

    It names no draft id: the handler always edits the caller's OWN current draft, so there is no id a client could
    point somewhere else.
    """

    session_token: str
    # The user's revision of the drafted text. None = leave it as it is.
    content: Optional[str] = Field(default=None, max_length=_LEN_GROUP_POST)
    # 'ready' or 'skipped' — the two ends of the user's own call. 'published'/'failed' are the
    # publish run's record of what happened and are refused here (GroupPostDraftStatus.user_settable).
    status: Optional[str] = None
    # A media URL WE issued this user from the post-image surface, attaching an image (or video) to
    # the group post. Its kind is derived from the stored file, never from this field.
    media_url: Optional[str] = Field(default=None, max_length=1024)
    # Detach whatever is attached, so the post goes out as text. Wins over `media_url`.
    remove_media: bool = False


def _resolve_group_media(user_id: int, media_url: str) -> "GroupPostMediaType":
    """Grade a media URL the SPA wants attached to the group post, or raise the user-facing refusal.

    `owns_post_media_url` is the same gate `/schedule_post/` puts on a compose-time image, widened
    to the video half (issue #1443) because this is the one surface that takes both: the value is
    caller input on a field the publish run later hands to LinkedIn, so only a preview issued to
    THIS user resolves — an image preview from `POST /user/post/image` or a video one from
    `POST /user/post/video`. The kind comes off the stored file's extension because that is what the
    group composer's uploader is actually given.
    """
    if not owns_post_media_url(user_id, media_url):
        raise HTTPException(status_code=400,
                            detail="Attach media you uploaded or generated here first")
    stored_path = post_media_abs_path(media_url)
    if not stored_path:
        raise HTTPException(status_code=400, detail="That media is no longer available")
    from cqc_lem.utilities.linkedin.poster import determine_media_type
    try:
        # The FILE on disk, never the URL: the extension is what the uploader is judged on, and the
        # stored name already comes from the decoded format rather than what was uploaded.
        kind = determine_media_type(stored_path)
    except ValueError:
        raise HTTPException(status_code=400, detail="Use an image or a video")
    return GroupPostMediaType.VIDEO if kind == "VIDEO" else GroupPostMediaType.IMAGE


@router.get("/group-post-draft",
            responses={200: {"model": ResponseModel[Optional[GroupPostDraftDetail]]}})
def get_group_post_draft_endpoint(session_token: str) -> ResponseModel[Optional[dict[str, Any]]]:
    """The group post waiting to be published, so the user can read it before it ships (issue #932).

    `detail` is None when nothing is queued — the SPA hides the card rather than inventing one.

    A draft the user SKIPPED is still returned (issue #1224): skipping is reversible until the slot
    passes, and a draft the studio cannot show is one the user cannot restore.

    `can_undo_skip` says whether that undo is still live (issue #1415) and `undo_deadline` is the
    publish slot it closes at, so the SPA offers the control only while the PUT would honour it
    rather than showing one that silently does nothing.

    The payload carries `best_practices` — the SAME list the drafting prompt is held to — so the
    guidance the author edits against cannot drift from the guidance the model wrote against.
    """
    user_id = _main.get_session_user_id(session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    draft = get_current_group_post_draft(user_id)
    if draft:
        draft["best_practices"] = list(GROUP_POST_BEST_PRACTICES)
        deadline = skip_undo_deadline(draft)
        draft["undo_deadline"] = deadline.isoformat() if deadline else None
        draft["can_undo_skip"] = (draft.get("status") == str(GroupPostDraftStatus.SKIPPED)
                                  and group_skip_undo_open(draft))
    return ResponseModel(status_code=200, detail=draft)


@router.put("/group-post-draft", responses={
    **{k: v for k, v in error_responses.items() if k in [400, 401, 404]},
    409: {"description": "Another group post is already queued, its group no longer takes posts, "
                         "or this week's publish slot has passed"},
    422: {"description": "Unsupported status or empty text"},
    500: {"description": "Server error"},
})
def update_group_post_draft_endpoint(request: GroupPostDraftUpdateRequest) -> ResponseModel[str]:
    """Edit the queued group post.

    Revise the text, attach or drop its media, skip it, or undo a skip and put the draft back in the
    queue. The undo restores the SAME row rather than drafting a second one — one open draft per user
    is the lane's invariant (issue #932) — and is refused once this week's publish slot has passed
    (issue #1415). Scoped to the caller's OWN current draft — the id is never taken from the request.
    """
    user_id = _main.get_session_user_id(request.session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    draft = get_current_group_post_draft(user_id)
    if not draft:
        raise HTTPException(status_code=404, detail="No group post is queued")
    status = None
    if request.status is not None:
        settable = {str(s): s for s in GroupPostDraftStatus.user_settable()}
        if request.status not in settable:
            raise HTTPException(status_code=422, detail="Unsupported group post draft status")
        status = settable[request.status]
        # An undo on a week that was never skipped changes nothing — an expected no-op, so it is a
        # DEBUG line and none of the restore gates below apply to it (issue #1415).
        undoing_skip = status == GroupPostDraftStatus.READY and draft.get("status") != str(status)
        if status == GroupPostDraftStatus.READY and not undoing_skip:
            log_debug("Group post undo skip is a no-op — this week was not skipped",
                      user_id=user_id, task_name="update_group_post_draft_endpoint")
        if undoing_skip:
            # Undoing a skip is bounded by the slot the draft is waiting on: once the publish beat
            # for that week has run the week is spent, and restoring the row would ship a post
            # written for a week that has passed. The SPA hides the control by then, so reaching
            # here means a stale tab.
            if not group_skip_undo_open(draft):
                raise HTTPException(
                    status_code=409,
                    detail="This week's group post slot has passed — the skip is final. "
                           "The next post is drafted Sunday.")
            # One OPEN draft per user is what stops the weekly beat replacing a post the user is
            # still editing, so a restore that would make a second one is refused rather than
            # quietly leaving two rows the publish run could pick between.
            open_draft = get_open_group_post_draft(user_id)
            if open_draft and open_draft.get("id") != draft["id"]:
                raise HTTPException(status_code=409,
                                    detail="A newer group post is already queued")
            # The publish beat SKIPS a draft whose group has since been switched off for posting
            # (run_scheduler.auto_group_posts), so not every skipped draft is one the user skipped.
            # Restoring one of those would report success and then be dropped again at the next
            # slot, silently, every week — so it is refused with the reason the user can act on.
            # None means the switches were unreadable, which is not evidence of an opt-out: the
            # publish beat holds the draft on that read too.
            post_enabled = get_post_enabled_group_ids(user_id)
            if post_enabled is not None and draft.get("group_id") not in post_enabled:
                raise HTTPException(
                    status_code=409,
                    detail="That group no longer takes posts — turn posting back on for it first")
    content = request.content
    if content is not None and not content.strip():
        # An empty draft would publish nothing and read as a bug — skipping is the way to cancel.
        raise HTTPException(status_code=422, detail="Group post text cannot be empty")

    media_fields = {}
    if request.remove_media:
        media_fields = {"media_url": None, "media_type": None}
    elif request.media_url:
        media_fields = {"media_url": request.media_url,
                        "media_type": _resolve_group_media(user_id, request.media_url)}
    if content is None and status is None and not media_fields:
        raise HTTPException(status_code=422, detail="Nothing to update")
    if not update_group_post_draft(draft["id"], content=content, status=status, **media_fields):
        raise HTTPException(status_code=500, detail="Could not update the group post")
    if media_fields and draft.get("media_url") and draft["media_url"] != media_fields["media_url"]:
        # The row no longer points at it, so the file it replaced is an orphan under the user's
        # preview dir — same clean-up `_attach_post_image` does for a post. Either kind: what is
        # being replaced is whatever the author attached last, image or video.
        remove_post_media_file(draft["media_url"])
    if status == GroupPostDraftStatus.SKIPPED:
        return ResponseModel(status_code=200, detail="Group post skipped")
    if status == GroupPostDraftStatus.READY:
        return ResponseModel(status_code=200, detail="Group post restored")
    return ResponseModel(status_code=200, detail="Group post updated")


@router.get("/post-stats")
def get_post_stats_endpoint(session_token: str) -> ResponseModel[dict[str, Any]]:
    """Personalized best-times-to-post recommendations plus a which-hooks/formats/topics-win
    ranking, both derived from the user's own post stats.
    """
    from cqc_lem.utilities.post_stats import rank_content_attributes, recommend_post_times
    user_id = _main.get_session_user_id(session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    rows = get_post_engagement_rows(user_id)
    # Recommendations are shown as "post on Wednesday at 4pm" — that hour has to be the user's own
    # wall clock, not the UTC the stats are stored in.
    user_tz = get_user_timezone(user_id)
    return ResponseModel(status_code=200, detail={
        "recommendations": recommend_post_times(rows, tz=user_tz),
        "rankings": rank_content_attributes(rows, top_n=5),
        "sample_size": len(rows),
        "timezone": user_tz,
    })


@router.get("/engagement-analytics")
def get_engagement_analytics_endpoint(session_token: str, days: int = 90) -> ResponseModel[dict[str, Any]]:
    """Per-post performance table + a daily engagement-rate / impression trend for the analytics
    dashboard (issue #395), derived from the user's captured post_stats, plus the 70/20/10
    content-mix compliance ratio for the same window (issue #618), the comment-outcome quality
    score (issue #628) and the content-quality rollup (issue #630). The hook/format leaderboard is
    served by /user/post-stats (rankings).
    """
    from cqc_lem.utilities.ai.content_alignment import content_mix_compliance
    from cqc_lem.utilities.comment_outcomes import comment_quality_report
    from cqc_lem.utilities.content_quality import quality_rollup, rollup_days
    from cqc_lem.utilities.db import get_content_quality_scores
    from cqc_lem.utilities.linkedin.rate_limit import commenting_hold_reason, commenting_hold_remaining
    from cqc_lem.utilities.post_stats import build_engagement_trend, build_performance_table
    user_id = _main.get_session_user_id(session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    days = max(1, min(int(days), 365))
    rows = get_post_performance_rows(user_id, days=days)
    # Comment outcomes are a per-COMMENT signal on a much shorter cadence than post stats, so they
    # are scored over the analytics window but reported with their own sample size — a user with no
    # readings yet sees an empty score, not a fabricated 0% reply rate.
    comment_quality = comment_quality_report(get_comment_outcomes(user_id, days=days), days=days)
    hold_remaining = commenting_hold_remaining(user_id)
    comment_quality["hold"] = {"active": hold_remaining > 0,
                               "reason": commenting_hold_reason(user_id) if hold_remaining else None,
                               "seconds_remaining": hold_remaining}
    # Why the panel is measuring a SUBSET (issue #809). Only posts with a captured post_stats row can
    # be measured, so without these the dashboard shows a number that reconciles with nothing else on
    # the screen. `measured` is taken from the rows we actually read, never re-counted in SQL, so the
    # coverage line can't contradict `sample_size`.
    posted_counts = get_post_coverage_counts(user_id, days=days)
    coverage = {**posted_counts, "measured": len(rows),
                "awaiting_capture": max(0, int(posted_counts.get("posted_in_window") or 0) - len(rows))}
    return ResponseModel(status_code=200, detail={
        "per_post": build_performance_table(rows),
        "trend": build_engagement_trend(rows),
        "sample_size": len(rows),
        "days": days,
        "coverage": coverage,
        # Mix compliance is a property of the PLAN, not of captured stats — it reports even when no
        # post has engagement data yet.
        "content_mix": content_mix_compliance(get_content_mix_counts(user_id, days=days)),
        "comment_quality": comment_quality,
        # Content quality reads its OWN period (the rollup's week), not the analytics window: the
        # panel's whole job is this-period-vs-last-period, and a 90-day "current" period would have
        # nothing to compare against.
        "content_quality": quality_rollup(
            get_content_quality_scores(user_id, days=rollup_days() * 2)),
    })


@router.get("/posthog-stats")
def get_posthog_stats_endpoint(session_token: str) -> ResponseModel[dict[str, Any]]:
    """The in-SPA 'your stats' panel (issue #654), backed by PostHog HogQL Endpoints
    (scripts/posthog_provision.py) instead of a bespoke MySQL reporting layer. A thin server-side
    proxy: the personal API key lives here and never reaches the browser, and every read is scoped
    to THIS user's own distinct_id — PostHog is one project shared by every LEM account. Degrades
    per-panel (`available: false`) rather than failing the whole response when a key is unset, an
    endpoint isn't provisioned yet, or PostHog is unreachable.
    """
    from cqc_lem.utilities.posthog_endpoints import get_user_stats_panel
    user_id = _main.get_session_user_id(session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    return ResponseModel(status_code=200, detail=get_user_stats_panel(user_id))


def _suppression_status(user_id: int) -> dict:
    """Current suppression-tripwire picture for one user (issue #629): the standing trip (if any)
    plus a FRESH evaluation of the same signals. Both are returned on purpose — the trip is what
    paused engagement and never self-clears, while the live verdict is how the user can see their
    reach has recovered and decide to re-enable.
    """
    from cqc_lem.utilities.comment_outcomes import comment_quality_report
    from cqc_lem.utilities.linkedin.rate_limit import (
        automation_pause_reason,
        automation_pause_remaining,
        is_suppression_pause,
        rate_limit_cooldown_remaining,
        suppression_trip_state,
    )
    from cqc_lem.utilities.post_stats import build_engagement_trend
    from cqc_lem.utilities.suppression import (
        comment_history_days,
        comment_min_sample,
        evaluate_suppression,
        history_days,
        tripwire_enabled,
    )

    window = history_days()
    comment_window = comment_history_days()
    trend = build_engagement_trend(get_post_performance_rows(user_id, days=window))
    # Same window AND same floor as the beat: this verdict is what the user reads to decide their
    # reach has recovered, so it must be scored on the condition that paused them, not a stricter one.
    quality = comment_quality_report(get_comment_outcomes(user_id, days=comment_window),
                                     days=comment_window, min_sample=comment_min_sample())
    verdict = evaluate_suppression(trend, comment_quality=quality)
    trip = suppression_trip_state(user_id)
    pause_remaining = automation_pause_remaining()
    pause_reason = automation_pause_reason() if pause_remaining else None
    return {
        "enabled": tripwire_enabled(),
        "tripped": trip is not None,
        "trip": trip,
        "current": verdict,
        "recovered": trip is not None and not verdict.get("tripped"),
        "engagement_paused": pause_remaining > 0,
        "pause_reason": pause_reason,
        "pause_by_tripwire": is_suppression_pause(pause_reason),
        "pause_remaining_s": pause_remaining,
        "breaker_remaining_s": rate_limit_cooldown_remaining(),
    }


@router.get("/automation-status")
def get_automation_status_endpoint(session_token: str) -> ResponseModel[dict[str, Any]]:
    """Suppression-tripwire + automation-pause state for the Account banner (issue #629)."""
    user_id = _main.get_session_user_id(session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    return ResponseModel(status_code=200, detail=_suppression_status(user_id))


class AutomationResumeRequest(BaseModel):
    """Body of `POST /user/automation-resume`.

    Session only — recovery from a suppression trip is deliberately a HUMAN act on their own account (issue #629),
    so there is nobody else to name.
    """

    session_token: str


@router.post("/automation-resume")
def resume_automation_endpoint(request: AutomationResumeRequest) -> ResponseModel[dict[str, Any]]:
    """The manual re-enable path for a suppression trip (issue #629). The tripwire NEVER resumes on
    its own, so this endpoint is the only way back: it clears the stored trip and lifts the pause —
    but only when the pause is the tripwire's own trip for THIS user, so re-enabling here can never
    stomp a 429 cooldown, a maintenance window, an admin kill-switch — or another user's standing
    trip, since `pause_automation` is one global breaker shared by the whole fleet.
    """
    from cqc_lem.utilities.linkedin.rate_limit import (
        automation_pause_reason,
        automation_pause_remaining,
        clear_suppression_trip,
        resume_automation,
        suppression_pause_reason,
    )
    user_id = _main.get_session_user_id(request.session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    cleared = clear_suppression_trip(user_id)
    reason = automation_pause_reason() if automation_pause_remaining() else None
    resumed = resume_automation() if reason == suppression_pause_reason(user_id) else False
    log_info("User re-enabled engagement after a suppression trip", user_id=user_id,
             action_type="rate_limit")
    return ResponseModel(status_code=200, detail={
        "cleared": cleared, "resumed": resumed,
        **_suppression_status(user_id),
    })


class AffiliateStatusRequest(BaseModel):
    """Body of the affiliate enrol/opt-out toggle (issue #737, program A)."""

    session_token: str
    # (A) affiliate status. One field, because opting out has to be ONE click — a confirm-then-submit
    # dance is the dark pattern the issue explicitly rules out.
    enrolled: bool


class AffiliatePromoConsentRequest(BaseModel):
    """Body of the promo-consent toggle (issue #737, program B).

    It governs publishing LEM promotion from the user's OWN LinkedIn account, which is why
    enabling needs evidenced consent and disabling does not.
    """

    session_token: str
    # (B) — publishing LEM promotion from the user's OWN LinkedIn account.
    enabled: bool
    # Enabling REQUIRES the user to have seen and accepted the consent copy. The API refuses an
    # enable without it rather than trusting the SPA to have shown the screen: consent that the
    # server never verified is consent we cannot evidence.
    consent_acknowledged: bool = False


class AffiliateNoticeRequest(BaseModel):
    """Body of `POST /user/affiliate/notice` — session only.

    The timestamp it records is the EVIDENCE that the enrolment notice was delivered, which is what makes default-on
    enrolment fair.
    """

    session_token: str


def _affiliate_detail(user_id: int, **extra) -> dict:
    from cqc_lem.utilities.marketing.affiliate import affiliate_state
    state = affiliate_state(user_id)
    for key in ("notice_seen_at", "promo_consent_at"):
        state[key] = _main._utc_iso(state.get(key))
    return {**state, **extra}


@router.get("/affiliate", responses={
    200: {"description": "Affiliate program state for the signed-in user"},
    **{k: v for k, v in error_responses.items() if k in [401]},
})
def get_affiliate_endpoint(session_token: str) -> ResponseModel[dict[str, Any]]:
    """The Account > Affiliate section's whole picture (issue #737): status, referral link, referrals
    driven, trial days earned against the cap, and BOTH toggles with their consent record.

    Reading this page is also what enrols a user who predates the program — enrollment is
    default-on, and `enroll_user` is idempotent, so an existing opted-out row is never revived.
    """
    user_id = _main.get_session_user_id(session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    try:
        from cqc_lem.utilities.marketing.affiliate import enroll_user
        enroll_user(user_id)
    except Exception as e:
        log_warning("Could not backfill affiliate enrollment", exc=e, user_id=user_id)
    return ResponseModel(status_code=200, detail=_affiliate_detail(user_id))


@router.post("/affiliate/status", responses={
    200: {"description": "Updated affiliate state"},
    **{k: v for k, v in error_responses.items() if k in [401]},
})
def set_affiliate_status_endpoint(request: AffiliateStatusRequest) -> ResponseModel[dict[str, Any]]:
    """Join or leave (A). Takes effect immediately, and the response carries the resulting trial end
    date so the SPA can tell the user their new trial length in the same breath as the change —
    opting out returns them to the standard trial, it does not take away days they EARNED.

    The date is read back off the user when the flip moved no reward, which is the ORDINARY case now
    that the join bonus is 0: "opt-out is immediate and the user is notified of the resulting trial
    length" cannot depend on a grant having happened.
    """
    from cqc_lem.utilities.db import TRIAL_EXTENDABLE_STATUSES, get_user_subscription_info
    from cqc_lem.utilities.marketing.affiliate import set_status
    user_id = _main.get_session_user_id(request.session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    result = set_status(user_id, bool(request.enrolled))
    reward = result.get("reward") or {}
    trial_ends_at = reward.get("trial_ends_at")
    if not trial_ends_at:
        # Only for a user who still HAS a trial — `users.trial_ends_at` outlives the trial, so a paid
        # or cancelled account would otherwise be told "your trial still ends <a date in the past>".
        info = get_user_subscription_info(user_id) or {}
        if str(info.get("subscription_status") or "") in TRIAL_EXTENDABLE_STATUSES:
            trial_ends_at = info.get("trial_ends_at")
    log_info(f"Affiliate status set to {'enrolled' if request.enrolled else 'opted_out'}",
             user_id=user_id)
    return ResponseModel(status_code=200, detail=_affiliate_detail(
        user_id,
        reward_days=int(reward.get("days") or 0),
        trial_ends_at=_main._utc_iso(trial_ends_at) if trial_ends_at else None,
    ))


@router.post("/affiliate/promo-consent", responses={
    200: {"description": "Updated affiliate state"},
    **{k: v for k, v in error_responses.items() if k in [401, 422]},
})
def set_affiliate_promo_consent_endpoint(request: AffiliatePromoConsentRequest) -> ResponseModel[dict[str, Any]]:
    """(B) — the separate, explicit opt-IN for LEM publishing promotional content about LEM from the
    user's OWN LinkedIn account. Default OFF, and it can only be turned on by this call, with
    `consent_acknowledged`, which is what makes the stored timestamp mean something.

    Turning it off needs no acknowledgement: withdrawing consent is never gated.
    """
    from cqc_lem.utilities.marketing.affiliate import set_promo_consent
    user_id = _main.get_session_user_id(request.session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    if request.enabled and not request.consent_acknowledged:
        raise HTTPException(status_code=422,
                            detail="Explicit consent is required to publish promotional content "
                                   "from your LinkedIn account")
    result = set_promo_consent(user_id, bool(request.enabled))
    if not result.get("ok"):
        raise HTTPException(status_code=422,
                            detail="Join the affiliate program before enabling promotional posts")
    log_info(f"Affiliate promo consent {'granted' if request.enabled else 'withdrawn'}",
             user_id=user_id)
    return ResponseModel(status_code=200, detail=_affiliate_detail(user_id))


@router.post("/affiliate/notice", responses={
    200: {"description": "Enrollment notice acknowledged"},
    **{k: v for k, v in error_responses.items() if k in [401]},
})
def acknowledge_affiliate_notice_endpoint(request: AffiliateNoticeRequest) -> ResponseModel[dict[str, Any]]:
    """Record that the user has SEEN the enrollment notice. Default enrollment is only fair if the
    notice was actually delivered, so this timestamp is the evidence it was.
    """
    from cqc_lem.utilities.db import mark_affiliate_notice_seen
    user_id = _main.get_session_user_id(request.session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    mark_affiliate_notice_seen(user_id)
    return ResponseModel(status_code=200, detail=_affiliate_detail(user_id))


@router.get("/audience-growth")
def get_audience_growth_endpoint(session_token: str, days: int = 90) -> ResponseModel[dict[str, Any]]:
    """Follower/audience telemetry for the analytics dashboard's growth panel (issue #627): the
    daily follower series with 7/30-day deltas, the latest profile-view and search-appearance
    readings, and the user's daily posting/commenting activity to overlay on the same window.
    Audience growth is the system's primary outcome — post engagement is the leading indicator.
    """
    from cqc_lem.utilities.audience_stats import GROWTH_WINDOWS, build_activity_series, follower_growth
    user_id = _main.get_session_user_id(session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    days = max(1, min(int(days), 365))
    # The 7/30-day deltas need a baseline that predates the window being charted, so read enough
    # history to cover the longest growth window on top of it.
    history_days = days + max(GROWTH_WINDOWS)
    growth = follower_growth(get_follower_stats(user_id, days=history_days))
    growth["series"] = [p for p in growth["series"] if p["date"] >= _window_start(days)]
    return ResponseModel(status_code=200, detail={
        **growth,
        "activity": build_activity_series(get_daily_action_counts(user_id, days=days)),
        "days": days,
    })


def _window_start(days: int) -> str:
    return (datetime.now(timezone.utc).date() - timedelta(days=int(days))).isoformat()


class LeadMagnetRequest(BaseModel):
    """Body of `PUT /user/lead-magnet` — the comment-keyword mechanic that pays out a DM (#624).

    `keyword` is the trigger word a reader comments to receive `message`, so the handler refuses
    one that collides with the engagement-bait filter — the filter would strip it from generated
    posts and the mechanic would silently never fire.
    """

    session_token: str
    enabled: bool = False
    keyword: Optional[str] = Field(default=None, max_length=_LEN_LM_KEYWORD)
    message: Optional[str] = Field(default=None, max_length=_LEN_LM_MESSAGE)


@router.get("/lead-magnet", responses={200: {"model": ResponseModel[LeadMagnetDetail]}})
def get_lead_magnet_endpoint(session_token: str) -> ResponseModel[dict[str, Any]]:
    """The caller's lead-magnet settings: the trigger keyword and the DM it pays out."""
    user_id = _main.get_session_user_id(session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    return ResponseModel(status_code=200, detail=get_lead_magnet_settings(user_id))


@router.put("/lead-magnet")
def update_lead_magnet_endpoint(request: LeadMagnetRequest) -> ResponseModel[str]:
    """Save the lead-magnet settings, refusing a bait-colliding keyword.

    The 422 names a workable alternative, because a mechanic that silently never fires is worse
    than one that would not save.
    """
    user_id = _main.get_session_user_id(request.session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    # A trigger word that itself reads as engagement bait (YES/AGREE/BELOW/AMEN/ME/👇) would be
    # stripped from generated posts by the bait filter — reject it up front.
    from cqc_lem.utilities.linkedin_formatter import is_bait_keyword
    if request.keyword and is_bait_keyword(request.keyword):
        raise HTTPException(
            status_code=422,
            detail=(f"Keyword '{request.keyword.strip()}' collides with the engagement-bait filter "
                    "(words like YES, AGREE, BELOW, AMEN, ME). Choose a distinctive trigger word "
                    "such as AUDIT or GUIDE."))
    if not update_lead_magnet_settings(user_id, request.model_dump(exclude={"session_token"})):
        raise HTTPException(status_code=500, detail="Could not update lead magnet")
    return ResponseModel(status_code=200, detail="Lead magnet updated")


@router.get("/dm-templates", responses={200: {"model": ResponseModel[List[DmTemplate]]}})
def get_dm_templates_endpoint(session_token: str) -> ResponseModel[list[dict[str, Any]]]:
    """The caller's DM template ladders — every event type, every follow-up step."""
    user_id = _main.get_session_user_id(session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    # A DB fault is a 503, not an empty set (issue #1575). The editor posts back the WHOLE set and
    # the server deletes what the payload omits, so answering an unreadable table with `[]` would
    # turn the user's next save into a wipe of every ladder they have.
    templates = get_dm_templates(user_id)
    if templates is None:
        raise HTTPException(status_code=503, detail="Could not read DM templates")
    return ResponseModel(status_code=200, detail=templates)


@router.put("/dm-templates")
def update_dm_templates_endpoint(request: DmTemplatesRequest) -> ResponseModel[str]:
    """Replace the caller's DM templates with the posted set — a whole-set upsert, not a patch."""
    user_id = _main.get_session_user_id(request.session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    if not upsert_dm_templates(user_id, [t.model_dump() for t in request.templates]):
        raise HTTPException(status_code=500, detail="Could not update DM templates")
    return ResponseModel(status_code=200, detail="DM templates updated")


@router.get("/engagement-targets",
            responses={200: {"model": ResponseModel[EngagementTargetsDetail]}})
def get_engagement_targets_endpoint(session_token: str) -> ResponseModel[dict[str, Any]]:
    """The user's engagement roster plus seed suggestions for an empty one (issue #616)."""
    user_id = _main.get_session_user_id(session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    return ResponseModel(status_code=200, detail={
        "targets": get_engagement_targets(user_id),
        "suggestions": suggest_engagement_targets(user_id),
    })


@router.put("/engagement-targets")
def update_engagement_targets_endpoint(request: EngagementTargetsRequest) -> ResponseModel[str]:
    """Replace the caller's engagement roster with the posted list."""
    user_id = _main.get_session_user_id(request.session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    if not upsert_engagement_targets(user_id, [t.model_dump() for t in request.targets]):
        raise HTTPException(status_code=500, detail="Could not update engagement roster")
    return ResponseModel(status_code=200, detail="Engagement roster updated")


@router.delete("/engagement-targets")
def delete_engagement_target_endpoint(request: EngagementTargetDeleteRequest) -> ResponseModel[str]:
    """Drop one person off the caller's roster, matched by profile URL and scoped to their own rows."""
    user_id = _main.get_session_user_id(request.session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    if not delete_engagement_target(user_id, request.profile_url):
        raise HTTPException(status_code=500, detail="Could not remove roster target")
    return ResponseModel(status_code=200, detail="Roster target removed")


@router.get("/story-bank", responses={200: {"model": ResponseModel[StoryBankDetail]}})
def get_story_bank_endpoint(session_token: str) -> ResponseModel[dict[str, Any]]:
    """The user's story bank plus how many entries a usable bank needs (issue #620)."""
    user_id = _main.get_session_user_id(session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    entries = get_story_bank_entries(user_id)
    return ResponseModel(status_code=200, detail={
        "entries": entries,
        "kinds": list(STORY_BANK_KINDS),
        "target_entries": STORY_BANK_TARGET_ENTRIES,
    })


@router.put("/story-bank")
def update_story_bank_endpoint(request: StoryBankRequest) -> ResponseModel[str]:
    """Upsert story-bank entries — the FACT half the content core draws on (issue #620)."""
    user_id = _main.get_session_user_id(request.session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    if not upsert_story_bank_entries(user_id, [e.model_dump() for e in request.entries]):
        raise HTTPException(status_code=500, detail="Could not update story bank")
    return ResponseModel(status_code=200, detail="Story bank updated")


@router.delete("/story-bank")
def delete_story_bank_endpoint(request: StoryBankDeleteRequest) -> ResponseModel[str]:
    """Remove one story-bank entry, scoped to the caller's own rows."""
    user_id = _main.get_session_user_id(request.session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    if not delete_story_bank_entry(user_id, request.entry_id):
        raise HTTPException(status_code=500, detail="Could not remove story bank entry")
    return ResponseModel(status_code=200, detail="Story bank entry removed")


@router.put("/linkedin-password", deprecated=True)
def update_linkedin_password(request: LinkedInPasswordRequest,
                             http_request: Request = None) -> ResponseModel[str]:
    """DEPRECATED (issue #745, design decision 2A) — use POST /user/linkedin-cookie instead.

    Store the user's LinkedIn password for Selenium-driven automation tasks. The value is
    encrypted at rest but must stay *reversible* because Selenium types it into the browser, so a
    stored password is strictly worse than a stored `li_at`: the cookie is revocable from
    LinkedIn's own "Sign out of all sessions" and is not a credential people reuse elsewhere.
    Kept for the deprecation window so accounts that only have a password keep working.
    It is never returned in any response payload.
    """
    user_id = _main.get_session_user_id(request.session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    _require_step_up(user_id, request.session_token, "store_linkedin_password",
                     http_request=http_request)
    if not request.linkedin_password:
        raise HTTPException(status_code=400, detail="Password cannot be empty")
    saved = update_user_linkedin_password(user_id, request.linkedin_password)
    if not saved:
        raise HTTPException(status_code=500, detail="Could not save LinkedIn password")
    return ResponseModel(status_code=200, detail="LinkedIn password saved")


def _scraped_profile_name(user_id: int) -> Optional[str]:
    """The full name on the profile LEM last scraped for this user, at any age — used ONLY to
    pre-fill/suggest the display-name field. Never a silent substitute for the saved value: the
    reply comparison must run on what the user confirmed, not on a scrape that may be a placeholder.
    """
    try:
        from cqc_lem.utilities.db import get_linked_in_profile_by_user_id
        raw = get_linked_in_profile_by_user_id(user_id, updated_less_than_days_ago=3650)
        if not raw:
            return None
        data = json.loads(raw[0] if isinstance(raw, (tuple, list)) else raw)
        return ((data or {}).get("full_name") or "").strip() or None
    except Exception:
        return None


@router.get("/linkedin-display-name")
def get_linkedin_display_name_endpoint(session_token: str) -> ResponseModel[dict[str, Any]]:
    """The user's LinkedIn display name (issue #731) plus the name LEM scraped from their profile.

    Reply detection compares the last sender in a DM thread against this exact string, so the UI
    shows the scraped name as a suggestion and the user confirms what LinkedIn actually renders.
    """
    user_id = _main.get_session_user_id(session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    return ResponseModel(status_code=200, detail={
        "linkedin_display_name": get_user_linkedin_display_name(user_id),
        "profile_full_name": _scraped_profile_name(user_id),
    })


@router.put("/linkedin-display-name")
def update_linkedin_display_name_endpoint(request: LinkedInDisplayNameRequest) -> ResponseModel[str]:
    """Save the user's LinkedIn display name. Required, and rejected empty: without it every DM
    reply check is UNKNOWN and the follow-up sequencer skips the person entirely (issue #731).
    """
    user_id = _main.get_session_user_id(request.session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    name = " ".join((request.linkedin_display_name or "").split())
    if not name:
        raise HTTPException(status_code=400,
                            detail="Enter your name exactly as it appears on your LinkedIn profile")
    if len(name) > 255:
        raise HTTPException(status_code=400, detail="Name is too long (max 255 characters)")
    if not update_user_linkedin_display_name(user_id, name):
        raise HTTPException(status_code=500, detail="Could not save your LinkedIn display name")
    return ResponseModel(status_code=200, detail="LinkedIn display name saved")


@router.get("/timezone")
def get_user_timezone_endpoint(session_token: str) -> ResponseModel[dict[str, Any]]:
    """The caller's IANA timezone — the one scheduling reads, so the SPA shows the same clock."""
    user_id = _main.get_session_user_id(session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    return ResponseModel(status_code=200, detail={"timezone": get_user_timezone(user_id)})


@router.get("/linkedin-profile")
def get_user_linkedin_profile_endpoint(session_token: str) -> ResponseModel[dict[str, Any]]:
    """The caller's connected LinkedIn profile URL. None when no LinkedIn account is attached yet.

    `refresh_available_in_seconds` reports the on-demand re-scrape window (issue #1076) so the SPA
    renders the same disabled state after a reload that it showed right after the press — a peek,
    never a claim.
    """
    user_id = _main.get_session_user_id(session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    return ResponseModel(status_code=200, detail={
        "linkedin_profile_url": get_linkedin_profile_url_by_user_id(user_id),
        "refresh_available_in_seconds": refresh_claimed_seconds(user_id),
    })


@router.post("/linkedin-profile/refresh", status_code=202, responses={
    202: {"description": "Refresh queued, or already claimed for today"},
    **{k: v for k, v in error_responses.items() if k in [401, 403]}
})
def refresh_user_linkedin_profile_endpoint(request: SessionOnlyRequest) -> ResponseModel[dict[str, Any]]:
    """Re-scrape the caller's OWN LinkedIn profile now and regenerate the voice synthesis from it.

    Without this, a profile edit reaches LEM's writing only when the weekly staleness beat
    (`run_scheduler.auto_refresh_profile_syntheses`) catches up — up to 7 days of content generated
    from the old headline, old skills, old experience.

    Always 202, never 429: pressing the button a second time in the same day is a person pressing a
    button twice, not an error, so the answer says `queued: false` and names the window instead of
    reading as a failure. The claim is taken BEFORE dispatch — a double-click must cost one Chrome
    session, not two — and the task's own `QueueOnce` lock is the second line against a duplicate
    that slips past a failed-open limiter.

    An `agent`-scoped session never reaches here: this path is absent from
    `_AGENT_SESSION_SURFACE`, so `_scope_allows` refuses it before the handler runs. Spending a
    Selenium slot is exactly the kind of capacity a headless token must not be able to draw on
    (same posture as `agent_may_not_configure`).
    """
    user_id = _main.require_session_user_id(request.session_token)
    claim = claim_profile_refresh(user_id)
    if claim.queued:
        update_stale_profile.apply_async(kwargs={"user_id": user_id, "force_refresh": True},
                                         retry=True, retry_policy={"max_retries": 1})
        log_info("Queued an on-demand LinkedIn profile refresh", user_id=user_id,
                 task_name="update_stale_profile")
    return ResponseModel(status_code=202, detail={
        "queued": claim.queued,
        "reason": claim.reason,
        "retry_after_seconds": claim.retry_after_seconds,
    })


@router.put("/timezone")
def update_user_timezone_endpoint(request: TimezoneRequest) -> ResponseModel[str]:
    """Save the caller's timezone, validated against the live tz database.

    Rejected rather than coerced: this zone is what turns a scheduled wall-clock time into a real
    publish moment, so an unrecognised name silently falling back to UTC would post at the wrong
    hour with nothing to show for it.
    """
    from zoneinfo import available_timezones
    user_id = _main.get_session_user_id(request.session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    if request.timezone not in available_timezones():
        raise HTTPException(status_code=422, detail=f"Unknown timezone: {request.timezone!r}")
    saved = update_user_timezone(user_id, request.timezone)
    if not saved:
        raise HTTPException(status_code=500, detail="Could not update timezone")
    return ResponseModel(status_code=200, detail="Timezone updated")


@router.get("/location")
def get_user_location_endpoint(session_token: str) -> ResponseModel[dict[str, Any]]:
    """The caller's stored Login Location. An empty dict means none is set — not an error."""
    user_id = _main.get_session_user_id(session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    return ResponseModel(status_code=200, detail=get_user_geo(user_id) or {})


@router.put("/location")
def update_user_location_endpoint(request: LocationRequest) -> ResponseModel[str]:
    """Save a manually chosen Login Location (`source='manual'`), stored as given.

    The two bounds checks are the only validation: coordinates outside the real ranges and a
    `country` that is not an ISO-3166 alpha-2 code would both reach the automation browser's geo
    emulation as nonsense.
    """
    user_id = _main.get_session_user_id(request.session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    if not (-90 <= request.latitude <= 90) or not (-180 <= request.longitude <= 180):
        raise HTTPException(status_code=422, detail="Invalid latitude/longitude")
    if request.country and len(request.country) != 2:
        raise HTTPException(status_code=422, detail="country must be an ISO-3166 alpha-2 code")
    saved = update_user_location(
        user_id, request.latitude, request.longitude,
        city=request.city, country=request.country, locale=request.locale,
        timezone=request.timezone, source="manual",
    )
    if not saved:
        raise HTTPException(status_code=500, detail="Could not update location")
    return ResponseModel(status_code=200, detail="Location updated")


@router.post("/location/autocapture")
def autocapture_user_location_endpoint(request: LocationAutocaptureRequest,
                                       http_request: Request) -> ResponseModel[dict[str, Any]]:
    """Geolocate the caller's real IP and persist it as their login location.
    The app sits behind a Cloudflare tunnel, so the client IP arrives in
    CF-Connecting-IP / X-Forwarded-For — never trust the immediate peer.
    """
    user_id = _main.get_session_user_id(request.session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")

    client_ip = (
        http_request.headers.get("cf-connecting-ip")
        or (http_request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
        or (http_request.client.host if http_request.client else None)
    )
    if not client_ip:
        raise HTTPException(status_code=400, detail="Could not determine client IP")

    try:
        resp = requests.get(f"https://ipapi.co/{client_ip}/json/", timeout=8)
        resp.raise_for_status()
        data = resp.json()
        if data.get("error"):
            raise ValueError(data.get("reason", "ip geolocation failed"))
        lat, lng = float(data["latitude"]), float(data["longitude"])
    except Exception as e:
        log_warning("IP geolocation failed", exc=e, user_id=user_id)
        raise HTTPException(status_code=502, detail="IP geolocation service unavailable")

    locale = None
    languages = data.get("languages")  # e.g. "en-US,es"
    if languages:
        locale = languages.split(",")[0]

    saved = update_user_location(
        user_id, lat, lng,
        city=data.get("city"), country=data.get("country_code") or data.get("country"),
        locale=locale, timezone=data.get("timezone"), source="ip_autocapture",
    )
    if not saved:
        raise HTTPException(status_code=500, detail="Could not save captured location")
    return ResponseModel(status_code=200, detail={
        "latitude": lat, "longitude": lng,
        "city": data.get("city"), "country": data.get("country_code") or data.get("country"),
        "timezone": data.get("timezone"), "locale": locale,
    })


@router.post("/location/by-city")
def set_user_location_by_city_endpoint(request: LocationByCityRequest) -> ResponseModel[dict[str, Any]]:
    """Geocode a user-selected city/state (free OSM Nominatim) and persist it as their login
    location, so the automation browser's emulated geo/timezone matches where they intend to
    appear. Complementary to /autocapture (IP-based).
    """
    user_id = _main.get_session_user_id(request.session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    try:
        geo = geocode_city(request.city, request.state, request.country)
    except GeocodeError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except Exception as e:
        log_warning("Geocoding failed", exc=e, user_id=user_id)
        raise HTTPException(status_code=502, detail="Geocoding service unavailable")
    saved = update_user_location(
        user_id, geo["latitude"], geo["longitude"],
        city=geo["city"], country=geo["country"], locale=geo["locale"],
        timezone=geo["timezone"], source="manual")
    if not saved:
        raise HTTPException(status_code=500, detail="Could not save location")
    return ResponseModel(status_code=200, detail=geo)


@router.post("/linkedin-cookie")
def store_linkedin_cookie_endpoint(request: LinkedInCookieRequest,
                                   http_request: Request = None) -> ResponseModel[str]:
    """Store the user's existing LinkedIn session cookie (li_at) so automation resumes
    an already-trusted session instead of doing a fresh password login — which is what
    triggers LinkedIn's "Check your app" new-device challenge. The user captures li_at
    once (one-click extension or paste); see docs/LINKEDIN_COOKIE.md.
    """
    user_id = _main.get_session_user_id(request.session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    # The crown jewel (design §2, T1): storing a li_at IS handing over a LinkedIn session, so it is
    # step-up gated like every other credential write. This is the ONE call site that accepts the
    # extension scope — its token can never run a passkey ceremony, and its step-up already happened
    # in the SPA when the token was minted (design §6.5).
    _require_step_up(user_id, request.session_token, "store_linkedin_cookie",
                     extension_scope_ok=True, http_request=http_request)

    # A cookie value cannot contain whitespace or ';'. Strip optional surrounding quotes.
    li_at = (request.li_at or "").strip().strip('"')
    if len(li_at) < 20 or any(c.isspace() for c in li_at) or ";" in li_at:
        raise HTTPException(
            status_code=422,
            detail="Invalid li_at value — paste the full LinkedIn 'li_at' cookie value.",
        )
    jsessionid = (request.jsessionid or "").strip() or None

    if not store_linkedin_li_at(user_id, li_at, jsessionid=jsessionid):
        raise HTTPException(status_code=500, detail="Could not store LinkedIn session")

    # The stored LinkedIn password is a decryptable password even after #745 encrypts it, so the
    # approved end state is to stop holding one (design §5.4). Only drop it once the cookie that
    # replaces it is safely stored — and only when the user asked for it.
    password_dropped = False
    if request.drop_password:
        password_dropped = clear_user_linkedin_password(user_id)

    return ResponseModel(
        status_code=200,
        detail=("LinkedIn session saved. Automation will reuse it and skip the password login."
                + (" Your stored LinkedIn password has been deleted." if password_dropped else "")),
    )


@router.get("/account-readiness")
def account_readiness_endpoint(session_token: str) -> ResponseModel[dict[str, Any]]:
    """Report whether the account has everything the automation needs (LinkedIn OAuth for
    posting, a session cookie or password for engagement, an active plan; location is
    recommended). The UI uses this to mark required fields and gate automation pages.
    """
    user_id = _main.get_session_user_id(session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")

    token_info = get_user_token_info(user_id)
    has_oauth = bool(token_info and token_info.get("access_token"))

    has_session_cookie = has_linkedin_session(user_id)
    # Presence check, not a read: get_user_password_pair_by_id would decrypt the password just to
    # see whether one exists, and an undecryptable row would then read as "no password" and quietly
    # flip this required item to not-ready (issue #745).
    has_password = has_linkedin_password(user_id)
    has_engagement_login = has_session_cookie or has_password
    # Design §5.4: accounts whose ONLY engagement login is a stored password get a one-time prompt
    # to paste a session cookie instead, after which the password is deleted rather than kept.
    cookie_migration_needed = has_password and not has_session_cookie

    sub = get_user_subscription_info(user_id)
    sub_status = (sub or {}).get("subscription_status")
    sub_active = sub_status in ("active", "trial")

    geo = get_user_geo(user_id)
    has_location = bool(geo and geo.get("latitude") is not None)

    # Required (issue #731): reply detection compares a thread's last sender against this name, so
    # without it every DM follow-up is skipped as unreadable — a silently dead sequencer.
    has_display_name = bool(get_user_linkedin_display_name(user_id))

    items = [
        {"key": "email", "label": "Verified email", "ok": True, "required": True,
         "hint": None},
        {"key": "linkedin_oauth", "label": "LinkedIn connected (posting)", "ok": has_oauth,
         "required": True, "hint": "Connect LinkedIn in your account."},
        {"key": "linkedin_session", "label": "LinkedIn session (engagement)",
         "ok": has_engagement_login, "required": True,
         "hint": "Connect your LinkedIn session (cookie) — the one-click extension is easiest."},
        {"key": "linkedin_display_name", "label": "Your LinkedIn display name",
         "ok": has_display_name, "required": True,
         "hint": "Enter your name exactly as it appears on your LinkedIn profile — LEM needs it to "
                 "tell your own messages apart from replies."},
        {"key": "subscription", "label": "Active plan", "ok": sub_active, "required": True,
         "hint": "Start a plan or trial under Subscription."},
        {"key": "location", "label": "Login location set", "ok": has_location,
         "required": False, "hint": "Set your login location to reduce LinkedIn challenges."},
    ]
    ready = all(i["ok"] for i in items if i["required"])
    return ResponseModel(status_code=200, detail={
        "ready": ready, "items": items, "cookie_migration_needed": cookie_migration_needed})


@router.get("/onboarding")
def onboarding_endpoint(session_token: str) -> ResponseModel[dict[str, Any]]:
    """The activation checklist (issue #500): each step, when it completed, and the next-best nudge
    to show in-app. Reading it also advances the persisted state, so the PostHog activation funnel
    records a step the moment the user finishes it — not a day later when the beat task runs.
    """
    user_id = _main.get_session_user_id(session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")

    from cqc_lem.utilities.onboarding import onboarding_snapshot
    return ResponseModel(status_code=200, detail=onboarding_snapshot(user_id))


@router.put("/company-page")
def update_company_page_endpoint(request: LinkedInCompanyPageRequest) -> ResponseModel[str]:
    """Save (or clear) the user's LinkedIn company page URL. The monthly invite
    automation (1st of each month) sends connection invites to this page for active
    users; users without one are skipped.
    """
    user_id = _main.get_session_user_id(request.session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")

    url = (request.company_linked_in_url or "").strip() or None
    if url is not None:
        if not (url.startswith("https://www.linkedin.com/") or url.startswith("https://linkedin.com/")):
            raise HTTPException(
                status_code=422,
                detail="Enter a full LinkedIn company page URL (https://www.linkedin.com/company/...).",
            )

    if not update_company_linked_in_url_for_user(user_id, url):
        raise HTTPException(status_code=500, detail="Could not save company page")
    return ResponseModel(status_code=200, detail="Company page saved" if url else "Company page cleared")


from cqc_lem.api import main as _main  # noqa: E402  — last; see the module docstring
