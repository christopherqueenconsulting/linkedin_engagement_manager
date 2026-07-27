import io
import json
import math
import os
import time
import zipfile
from datetime import datetime, timedelta, timezone
from enum import IntEnum
from typing import Dict, List, Union
from typing import Optional, Any
from urllib.parse import urlparse, urlunparse

from cqc_lem import assets_dir
from cqc_lem.app.aws_test_celery_task import test_get_my_profile
from cqc_lem.app.run_automation import (
    automate_invites_to_company_page_for_user, automate_reply_commenting,
    automate_commenting, automate_appreciation_dms_for_user,
    send_private_dm, consolidate_duplicate_comments_for_user, sweep_reply_comments,
    send_lead_response,
)
from celery import chain as celery_chain
from cqc_lem.app.run_content_plan import auto_create_weekly_content, plan_content_for_user
from cqc_lem.utilities.db import (
    insert_post, get_post_by_email, get_user_id, update_db_post, get_post_user_id,
    add_user_with_access_token, update_user, PostType, PostStatus, get_posts, get_dashboard_counts,
    get_planned_tasks,
    get_recent_logs, bulk_update_posts, soft_delete_posts,
    insert_scheduled_dm, get_scheduled_dms, get_scheduled_dm, get_scheduled_dm_user_id,
    update_scheduled_dm, update_scheduled_dm_status, ScheduledDmStatus,
    insert_connection_request, get_connection_requests, get_connection_request_user_id,
    update_connection_request, update_connection_request_status, ConnectionRequestStatus,
    insert_outreach_target, get_outreach_targets, get_outreach_target_user_id,
    get_outreach_target_by_url, update_outreach_target, update_outreach_target_status,
    OutreachStatus,
    get_lead_signals, get_lead_signal, update_lead_signal,
    count_new_lead_signals, LeadSignalStatus,
    get_leads, get_lead, update_lead, count_hot_leads, LeadStage,
    get_catchup_touches, get_catchup_touch, get_catchup_touch_user_id, update_catchup_touch,
    update_catchup_touch_status, CatchupTouchStatus, CatchupEventType,
    DEFAULT_CATCHUP_EVENT_TYPES, VALID_CATCHUP_TOUCH_MODES, VALID_CATCHUP_MESSAGE_SOURCES,
    CATCHUP_TOUCHES_MIN, CATCHUP_TOUCHES_MAX, CATCHUP_TOUCHES_MAX_STANDARD,
    max_catchup_touches_allowed,
    create_pin_for_email, verify_pin_for_email, delete_pin_for_email,
    create_session, get_session_user_id, delete_session,
    add_user_by_email, get_user_email, get_user_analytics_profile, get_user_token_info,
    store_linkedin_li_at,
    has_linkedin_session, get_user_password_pair_by_id,
    get_company_linked_in_url_for_user, update_company_linked_in_url_for_user,
    get_user_subscription_info, get_user_preferences, update_user_preferences,
    DEFAULT_CONTENT_BUFFER_DAYS, DEFAULT_CONTENT_BUFFER_MAX_POSTS,
    MAX_CONTENT_BUFFER_DAYS, MAX_CONTENT_BUFFER_POSTS,
    get_engagement_preferences, has_engagement_preferences, update_engagement_preferences,
    DEFAULT_POSTS_PER_WEEK, POSTS_PER_WEEK_MIN, POSTS_PER_WEEK_MAX,
    DEFAULT_POSTING_DAYS, normalize_posting_days,
    get_or_create_reply_inbound_token,
    get_newsletter_settings, update_newsletter_settings,
    get_pending_newsletter_editions,
    get_latest_edition_scheduled_for, update_newsletter_edition, get_newsletter_edition,
    get_user_timezone,
    get_user_groups, set_groups_enabled, get_post_engagement_rows, get_post_performance_rows,
    get_content_mix_counts, get_comment_outcomes,
    get_follower_stats, get_daily_action_counts,
    get_lead_magnet_settings, update_lead_magnet_settings,
    get_dm_templates, upsert_dm_templates,
    get_engagement_targets, upsert_engagement_targets, delete_engagement_target,
    suggest_engagement_targets, ENGAGEMENT_TARGET_CATEGORIES, ENGAGEMENT_TARGET_SOURCES,
    ENGAGEMENT_TARGET_WEEKLY_DEFAULT, ENGAGEMENT_TARGET_WEEKLY_MAX,
    get_story_bank_entries, upsert_story_bank_entries, delete_story_bank_entry,
    STORY_BANK_KINDS, STORY_BANK_TARGET_ENTRIES,
    update_subscription_from_stripe, update_user_linkedin_token,
    get_users_with_stripe_subscriptions,
    update_user_linkedin_password,
    get_user_linkedin_display_name, update_user_linkedin_display_name,
    get_user_blog_url, get_user_sitemap_url, get_linkedin_profile_url_by_user_id,
    get_user_by_stripe_customer_id, get_avatar_credit_ledger_entry_by_session,
    get_avatar_credit_balance, add_avatar_credits,
    get_video_credit_balance, add_video_credits,
    get_video_credit_ledger_entry_by_session, update_post_video_quality,
    deduct_avatar_credit, insert_avatar_training,
    update_avatar_training_status, set_active_avatar,
    get_avatar_trainings, get_active_avatar,
    get_user_timezone, update_user_timezone,
    get_user_geo, update_user_location, get_user_content_language,
    replace_video_url_base, get_post_type, get_post_buyer_stage, get_post_status,
    update_db_post_carousel_slides,
    get_post_url_from_log_for_user,
    insert_feedback, FeedbackSource,
    get_latest_review_feedback_id, get_early_adopter_grant, extend_trial_for_user,
)
from cqc_lem.utilities.content_generation_status import mark_queued, get_generation_status, \
    clear_generation_status
from cqc_lem.utilities.email import generate_pin, hash_pin, send_pin_email
from cqc_lem.utilities.linkedin.verification_pin import (
    extract_pin_from_text, extract_token_from_address, submit_pin_by_token)
from cqc_lem.utilities.geocoding import geocode_city, GeocodeError
from cqc_lem.utilities.linkedin.token_refresh import (
    get_token_expiry, is_token_expired, is_token_expiring_soon, attempt_token_refresh,
)
from cqc_lem.utilities.env_constants import LI_CLIENT_ID, LI_CLIENT_SECRET, LI_REDIRECT_URL, LI_STATE_SALT, ADMIN_SECRET, API_ACCESS_TOKENS, \
    DEFAULT_IMAGE_MODEL, DEFAULT_VIDEO_MODEL, DEFAULT_VIDEO_RATIO
import requests
from cqc_lem.utilities.logger import myprint, log_warning, log_info, log_error
from cqc_lem.utilities.mime_type_helper import get_file_mime_type
from cqc_lem.utilities.quality_gates import (parse_gate_findings, clamp_threshold,
                                             AUTHENTICITY_SCORE_MIN_BOUNDS,
                                             SIMILARITY_MAX_PCT_BOUNDS)
from cqc_lem.utilities.observability import (
    capture_exception, track_api_call, track_funnel_event, anonymous_distinct_id,
    FUNNEL_SIGNUP_STARTED, FUNNEL_SIGNUP_COMPLETED, FUNNEL_TRIAL_STARTED,
    FUNNEL_SUBSCRIPTION_STARTED, FUNNEL_CHURNED,
)
from cqc_lem.utilities.utils import get_file_extension_from_filepath
from fastapi import FastAPI, HTTPException, Request, status, APIRouter, Header, Depends
from fastapi import File, Form, Query, UploadFile
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials, APIKeyHeader
from fastapi.responses import FileResponse
from fastapi.responses import JSONResponse
from fastapi.responses import HTMLResponse
from fastapi.responses import RedirectResponse
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from linkedin_api.clients.auth.client import AuthClient
from linkedin_api.clients.restli.client import RestliClient
from linkedin_api.common.errors import ResponseFormattingError
from pydantic import BaseModel, field_validator, model_validator, Field

app = FastAPI()

# All API routes live under /api so the React client's baseURL: '/api' works
router = APIRouter(prefix="/api")


@app.middleware("http")
async def observability_middleware(request: Request, call_next):
    start = time.time()
    status_code = 500
    try:
        response = await call_next(request)
        status_code = response.status_code
        return response
    except Exception as e:
        # Only UNHANDLED exceptions reach here — a route's own HTTPException is turned into a
        # response by FastAPI's handler further in, so 4xx never files an error-tracking issue.
        capture_exception(e, route=request.url.path, method=request.method,
                          source="fastapi.middleware")
        raise
    finally:
        track_api_call(
            route=request.url.path,
            method=request.method,
            status_code=status_code,
            latency_ms=int((time.time() - start) * 1000),
        )


# Bearer-token gate for /api routes. Active only when API_ACCESS_TOKENS is set,
# so local/dev (and existing tests) run open. Login and the Stripe webhook stay
# public; everything else under /api requires a valid bearer token. Routes served
# outside /api (SPA, /health, /docs, /auth/linkedin/*) are never gated here.
_API_ACCESS_TOKEN_SET = {t.strip() for t in API_ACCESS_TOKENS.split(",") if t.strip()}
# /api/assets is public: it serves generated post media (images/videos) that
# LinkedIn fetches over an unauthenticated public URL when publishing. The
# handler (get_assets) is GET-only and path-traversal safe (_find_asset_file
# rejects .. / separators and only returns real files under assets_dir).
# /api/extension is public: it serves the browser-extension zip as a plain <a href>
# download from the account page, which carries no bearer token. The bundle is
# non-sensitive public code (destined for the Chrome Web Store); the route is GET-only.
# /api/user/linkedin-cookie is public because the browser extension POSTs to it WITHOUT the
# SPA's bearer token (the extension can't hold the rotating API token). It is
# self-authenticating: the handler validates the user's own LEM session_token and 401s if
# it's invalid — same model as the /api/auth/ endpoints. This exact leaf path only; the rest
# of /api/user/* stays gated.
# /api/faq is public: it serves the published front-page FAQ (issue #506) to logged-out visitors on
# the landing page. GET-only, no user data — same shape as /api/app-info.
# /api/flags is public for the SAME reason (issue #651): the landing page bootstraps its feature
# flags from it and carries no bearer token. Gating it would 401 the flags query, and the SPA's
# axios interceptor treats ANY 401 as a dead session — it clears lem_session and redirects, so a
# signed-in visitor hitting the landing page would be silently logged out. GET-only; it returns the
# registry's own toggle values, and the optional session_token is self-authenticating (an invalid
# one resolves the "system" identity rather than erroring) — same model as /api/user/linkedin-cookie.
_PUBLIC_API_PREFIXES = ("/api/auth/", "/api/billing/webhook", "/api/assets",
                        "/api/linkedin/verification-pin", "/api/linkedin/comment-notification",
                        "/api/app-info", "/api/faq", "/api/flags",
                        "/api/extension/", "/api/user/linkedin-cookie")


def _is_public_api_path(path: str) -> bool:
    # An entry ending in "/" opens that whole subtree; every other entry matches only itself or a
    # path segment beneath it. Without the boundary, "/api/faq" would also unlock a future
    # "/api/faq-admin".
    for prefix in _PUBLIC_API_PREFIXES:
        if prefix.endswith("/"):
            if path.startswith(prefix):
                return True
        elif path == prefix or path.startswith(prefix + "/"):
            return True
    return False


def _api_token_required(path: str) -> bool:
    if not _API_ACCESS_TOKEN_SET or not path.startswith("/api/"):
        return False
    return not _is_public_api_path(path)


def _bearer_token(authorization: Optional[str]) -> Optional[str]:
    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    return token.strip()


@app.middleware("http")
async def api_token_middleware(request: Request, call_next):
    if _api_token_required(request.url.path):
        token = _bearer_token(request.headers.get("Authorization"))
        if token not in _API_ACCESS_TOKEN_SET:
            return JSONResponse(status_code=401, content={"status_code": 401, "detail": "Unauthorized"})
    return await call_next(request)


_ui_dist = os.path.join(os.path.dirname(__file__), "..", "ui", "dist")

error_responses = {
    400: {"description": "Bad Request"},
    401: {"description": "Unauthorized"},
    403: {"description": "Forbidden"},
    404: {"description": "Not Found"},
    405: {"description": "Method Not Allowed"},
    422: {"description": "Unprocessable Entity"}
}


class ResponseModel(BaseModel):
    status_code: int
    detail: Any


def _parse_slides(raw) -> Optional[List[str]]:
    if not raw:
        return None
    if isinstance(raw, list):
        return raw
    try:
        result = json.loads(raw)
        return result if isinstance(result, list) else None
    except (json.JSONDecodeError, TypeError):
        return None


def _utc_iso(dt) -> Optional[str]:
    """Serialize a datetime as an explicit-UTC ISO string (trailing 'Z') so clients localize it
    correctly. Stored datetimes are UTC but historically naive — assume naive means UTC. Without the
    offset the browser parses the string as local time and every displayed value is off by its
    UTC offset."""
    if dt is None:
        return None
    if isinstance(dt, str):
        try:
            dt = datetime.fromisoformat(dt.replace("Z", "+00:00"))
        except ValueError:
            return dt
    dt = dt.replace(tzinfo=timezone.utc) if getattr(dt, "tzinfo", None) is None else dt.astimezone(timezone.utc)
    return dt.isoformat().replace("+00:00", "Z")


def _public_post_url(value) -> Optional[str]:
    """Only surface real http(s) permalinks. Home-feed comments have no LinkedIn permalink and are
    logged under a synthetic 'feedpost://<hash>' dedup key — never expose that raw string to the UI."""
    if isinstance(value, str) and value.lower().startswith(("http://", "https://")):
        return value
    return None


class PostRequest(BaseModel):
    content: str
    video_url: Optional[str] = None
    post_type: Optional[PostType] = PostType.TEXT
    scheduled_datetime: datetime
    email: Optional[str] = None
    status: Optional[PostStatus] = PostStatus.PENDING
    carousel_slides: Optional[List[str]] = None
    use_avatar: Optional[bool] = False
    video_quality: Optional[str] = "standard"  # standard | premium | premium_top


class AvatarCreditCheckoutRequest(BaseModel):
    session_token: str
    package: str
    success_url: str
    cancel_url: str


class VideoCreditCheckoutRequest(BaseModel):
    session_token: str
    package: str        # "small" | "medium" | "large" | "max"
    success_url: str
    cancel_url: str


class UpgradeVideoRequest(BaseModel):
    session_token: str
    post_id: int
    tier: str = "premium"  # "premium" (1 credit) or "premium_top" (3 credits)


class AvatarActivateRequest(BaseModel):
    session_token: str


class BulkUpdateRequest(BaseModel):
    post_ids: List[int]
    status: Optional[PostStatus] = None
    scheduled_datetime: Optional[datetime] = None


class BulkDeleteRequest(BaseModel):
    post_ids: List[int]


class UserSettingsRequest(BaseModel):
    email: str
    new_email: Optional[str] = None
    blog_url: Optional[str] = None
    sitemap_url: Optional[str] = None


class FunnelAttribution(BaseModel):
    """Where a visitor came from, captured client-side on first landing (issue #503). Every field is
    optional — a direct visit sends none of them."""
    utm_source: Optional[str] = None
    utm_medium: Optional[str] = None
    utm_campaign: Optional[str] = None
    utm_content: Optional[str] = None
    utm_term: Optional[str] = None
    referrer: Optional[str] = None
    landing_page: Optional[str] = None
    channel: Optional[str] = None
    # The referral link's referrer id (issue #658, `?ref=<user id>`). Allow-listed like the rest —
    # `normalize_attribution` drops anything not in its key list.
    ref: Optional[str] = None


class AuthInitRequest(BaseModel):
    email: str
    attribution: Optional[FunnelAttribution] = None


class AuthVerifyRequest(BaseModel):
    email: str
    pin: str
    attribution: Optional[FunnelAttribution] = None


class LogoutRequest(BaseModel):
    session_token: str


class CheckoutSessionRequest(BaseModel):
    session_token: str
    tier: str
    success_url: str
    cancel_url: str


class PortalSessionRequest(BaseModel):
    session_token: str
    return_url: str


class TrialExtendRequest(BaseModel):
    """Claim the early-adopter extended trial (issue #499)."""
    session_token: str


class UserPreferencesRequest(BaseModel):
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
_LEN_DM_TEMPLATE = 2000   # dm_templates.template_text (TEXT; app cap)
_LEN_TARGET_PROFILE_URL = 512  # engagement_targets.profile_url VARCHAR(512)
_LEN_TARGET_NAME = 255         # engagement_targets.name VARCHAR(255)
_LEN_STORY_TITLE = 255         # story_bank.title VARCHAR(255)
_LEN_STORY_BODY = 5000         # story_bank.body (TEXT; app cap)
_LEN_NL_TITLE = 255       # newsletter_settings.title VARCHAR(255)
_LEN_NL_TOPIC = 512       # newsletter_settings.topic VARCHAR(512)
_LEN_DM_RECIPIENT_URL = 512   # scheduled_dms.recipient_profile_url VARCHAR(512)
_LEN_DM_RECIPIENT_NAME = 255  # scheduled_dms.recipient_name VARCHAR(255)
_LEN_CONNECT_NOTE = 300       # LinkedIn caps a connection-request note at 300 chars
_LEN_FEEDBACK_BODY = 5000     # feedback.body (TEXT; app cap)
_LEN_FEEDBACK_TYPE_HINT = 32  # feedback.type_hint VARCHAR(32)
# Screenshots ride along inside feedback.context_json as a data URL. Capped so one report can't
# blow past max_allowed_packet; the widget downsizes/rejects before it gets here.
_LEN_FEEDBACK_SCREENSHOT = 2_000_000
_LEN_FEEDBACK_CONTEXT = 8000  # serialized auto-attached context, screenshot excluded


class NewsletterSettingsRequest(BaseModel):
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
    session_token: str
    edition_id: int
    title: Optional[str] = None
    subtitle: Optional[str] = None
    body: Optional[str] = None
    scheduled_datetime: Optional[datetime] = None
    action: str = "save"


class NewsletterRegenerateRequest(BaseModel):
    session_token: str
    edition_id: int
    guidance: Optional[str] = None  # free-text "Added Guidance"; empty => AI decides a fresh take


class PostRegenerateRequest(BaseModel):
    session_token: str
    post_id: int
    guidance: Optional[str] = None  # free-text "Added Guidance"; empty => fresh take honoring settings


class PostRescoreRequest(BaseModel):
    session_token: str
    post_id: int


class ScheduleDmRequest(BaseModel):
    session_token: str
    recipient_profile_url: str = Field(max_length=_LEN_DM_RECIPIENT_URL)
    recipient_name: Optional[str] = Field(default=None, max_length=_LEN_DM_RECIPIENT_NAME)
    message: str = Field(max_length=_LEN_DM_TEMPLATE)
    scheduled_datetime: datetime
    status: str = "pending"  # 'pending' (draft) or 'approved' (queue for send)


class UpdateDmRequest(BaseModel):
    session_token: str
    dm_id: int
    recipient_profile_url: Optional[str] = Field(default=None, max_length=_LEN_DM_RECIPIENT_URL)
    recipient_name: Optional[str] = Field(default=None, max_length=_LEN_DM_RECIPIENT_NAME)
    message: Optional[str] = Field(default=None, max_length=_LEN_DM_TEMPLATE)
    scheduled_datetime: Optional[datetime] = None
    action: Optional[str] = None  # 'approve' | 'cancel' | None (save fields only)


class DmDeleteRequest(BaseModel):
    session_token: str
    dm_id: int


class ConnectionRequestCreate(BaseModel):
    session_token: str
    recipient_profile_url: str = Field(max_length=_LEN_DM_RECIPIENT_URL)
    recipient_name: Optional[str] = Field(default=None, max_length=_LEN_DM_RECIPIENT_NAME)
    message: Optional[str] = Field(default=None, max_length=_LEN_CONNECT_NOTE)  # optional connect note
    # None → follow the user's connection_request_mode (auto_approve queues it, pre_review holds it as a
    # draft). An explicit 'pending' or 'approved' overrides that; any other value is rejected (422).
    status: Optional[str] = None


class ConnectionRequestUpdate(BaseModel):
    session_token: str
    request_id: int
    recipient_profile_url: Optional[str] = Field(default=None, max_length=_LEN_DM_RECIPIENT_URL)
    recipient_name: Optional[str] = Field(default=None, max_length=_LEN_DM_RECIPIENT_NAME)
    message: Optional[str] = Field(default=None, max_length=_LEN_CONNECT_NOTE)
    action: Optional[str] = None  # 'approve' | 'cancel' | None (save fields only)


class ConnectionRequestDelete(BaseModel):
    session_token: str
    request_id: int


# Comment-first outreach funnel (issue #399) — approval-gated comment->connect->DM
_LEN_OUTREACH_URL = 512    # outreach_funnel_targets.target_profile_url / context_url VARCHAR(512)
_LEN_OUTREACH_NAME = 255   # outreach_funnel_targets.target_name VARCHAR(255)
_LEN_OUTREACH_DRAFT = 3000  # outreach_funnel_targets.draft_text (TEXT; app cap)


class OutreachTargetRequest(BaseModel):
    session_token: str
    target_profile_url: str = Field(max_length=_LEN_OUTREACH_URL)
    target_name: Optional[str] = Field(default=None, max_length=_LEN_OUTREACH_NAME)
    context_url: Optional[str] = Field(default=None, max_length=_LEN_OUTREACH_URL)
    draft_text: Optional[str] = Field(default=None, max_length=_LEN_OUTREACH_DRAFT)
    status: str = "pending"  # 'pending' (draft) or 'approved' (let the processor fire this stage)


class UpdateOutreachTargetRequest(BaseModel):
    session_token: str
    target_id: int
    target_name: Optional[str] = Field(default=None, max_length=_LEN_OUTREACH_NAME)
    context_url: Optional[str] = Field(default=None, max_length=_LEN_OUTREACH_URL)
    draft_text: Optional[str] = Field(default=None, max_length=_LEN_OUTREACH_DRAFT)
    action: Optional[str] = None  # 'approve' | 'cancel' | None (save fields only)


class OutreachTargetDeleteRequest(BaseModel):
    session_token: str
    target_id: int


# LinkedIn Catch-up touches (issue #482) — approval-gated milestone congratulations
_LEN_CATCHUP_NAME = 255     # catchup_touches.person_name VARCHAR(255)
_LEN_CATCHUP_MESSAGE = 1000  # catchup_touches.message (TEXT; app cap — a DM is refined to ≤300)


class UpdateCatchupTouchRequest(BaseModel):
    session_token: str
    touch_id: int
    person_name: Optional[str] = Field(default=None, max_length=_LEN_CATCHUP_NAME)
    message: Optional[str] = Field(default=None, max_length=_LEN_CATCHUP_MESSAGE)
    action: Optional[str] = None  # 'approve' | 'cancel' | None (save fields only)


class CatchupTouchDeleteRequest(BaseModel):
    session_token: str
    touch_id: int


class EngagementPreferencesRequest(BaseModel):
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
    # Catch-up congratulations (issue #482)
    max_catchup_touches_per_day: int = CATCHUP_TOUCHES_MAX_STANDARD
    catchup_touch_mode: str = "pre_review"  # 'pre_review' (default) | 'auto_approve'
    catchup_event_types: List[str] = list(DEFAULT_CATCHUP_EVENT_TYPES)
    catchup_message_source: str = "linkedin"  # 'linkedin' (LinkedIn's own draft) | 'ai'

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


class DmTemplateItem(BaseModel):
    event_type: str
    step: int = 0
    delay_hours: int = 0
    template_text: str = Field(max_length=_LEN_DM_TEMPLATE)
    is_active: bool = True


class DmTemplatesRequest(BaseModel):
    session_token: str
    templates: List[DmTemplateItem] = []


class EngagementTargetItem(BaseModel):
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
    session_token: str
    targets: List[EngagementTargetItem] = []


class EngagementTargetDeleteRequest(BaseModel):
    session_token: str
    profile_url: str = Field(max_length=_LEN_TARGET_PROFILE_URL)


class StoryBankItem(BaseModel):
    """One piece of the user's own raw material (issue #620). `body` is the only required field —
    quick capture is a textarea, not a form wizard, so the title defaults from the body."""
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
    session_token: str
    entries: List[StoryBankItem] = []


class StoryBankDeleteRequest(BaseModel):
    session_token: str
    entry_id: int


class LinkedInPasswordRequest(BaseModel):
    session_token: str
    linkedin_password: str


class LinkedInDisplayNameRequest(BaseModel):
    session_token: str
    linkedin_display_name: str


class TimezoneRequest(BaseModel):
    session_token: str
    timezone: str


class LocationRequest(BaseModel):
    session_token: str
    latitude: float
    longitude: float
    city: Optional[str] = None
    country: Optional[str] = None
    locale: Optional[str] = None
    timezone: Optional[str] = None


class LocationAutocaptureRequest(BaseModel):
    session_token: str


class LocationByCityRequest(BaseModel):
    session_token: str
    city: str
    state: Optional[str] = None
    country: Optional[str] = None


class AdminLocationByCityRequest(BaseModel):
    user_id: int
    city: str
    state: Optional[str] = None
    country: Optional[str] = None


class LinkedInCookieRequest(BaseModel):
    session_token: str
    li_at: str
    jsessionid: Optional[str] = None


class LinkedInCompanyPageRequest(BaseModel):
    session_token: str
    company_linked_in_url: Optional[str] = None


class AdminFixVideoUrlsRequest(BaseModel):
    old_base: str
    new_base: str
    user_id: Optional[int] = None


class AdminRegenerateCarouselRequest(BaseModel):
    post_id: int
    user_id: int
    template: Optional[str] = None  # e.g. "bold_listicle", "minimal_dark"; None = auto-pick by stage


class AdminRegenerateVideoRequest(BaseModel):
    post_id: int
    user_id: int


class VariantCombo(BaseModel):
    image_model: str = DEFAULT_IMAGE_MODEL
    video_model: str = DEFAULT_VIDEO_MODEL
    ratio: str = DEFAULT_VIDEO_RATIO
    duration: int = 5
    seed: Optional[int] = None
    include_video: bool = True


class GenerateMediaVariantsRequest(BaseModel):
    post_id: Optional[int] = None
    text: Optional[str] = None
    topic: Optional[str] = None
    user_id: Optional[int] = None
    combos: Optional[List[VariantCombo]] = None  # None = default 3-variant matrix

    @model_validator(mode="after")
    def _require_source(self):
        if self.post_id is None and not (self.text or self.topic):
            raise ValueError("Provide post_id or text/topic")
        return self


class GenerateCarouselPreviewRequest(BaseModel):
    session_token: str
    stage: str = "awareness"  # awareness | consideration | decision | personal
    template: Optional[str] = None  # None = auto-pick by stage


class FeedbackRequest(BaseModel):
    """In-app feedback / bug report (issue #496). session_token is optional: the widget is offered
    to logged-out visitors too, and those land with a NULL user_id."""
    body: str = Field(min_length=1, max_length=_LEN_FEEDBACK_BODY)
    session_token: Optional[str] = None
    source: str = str(FeedbackSource.WIDGET)
    type_hint: Optional[str] = Field(default=None, max_length=_LEN_FEEDBACK_TYPE_HINT)
    context: Optional[Dict[str, Any]] = None
    screenshot: Optional[str] = Field(default=None, max_length=_LEN_FEEDBACK_SCREENSHOT)

    @field_validator("body")
    @classmethod
    def body_not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("Feedback body cannot be empty")
        return v.strip()

    @field_validator("source")
    @classmethod
    def known_source(cls, v: str) -> str:
        valid = {str(s) for s in FeedbackSource}
        if v not in valid:
            raise ValueError(f"Unknown source '{v}' — expected one of {sorted(valid)}")
        return v


class NpsSurveyRequest(BaseModel):
    """An NPS answer (issue #501): the 0-10 score plus the free-text 'why'. Bounds mirror
    `utilities.surveys.NPS_MIN/NPS_MAX`."""
    session_token: str
    score: int = Field(ge=0, le=10)
    why: Optional[str] = Field(default=None, max_length=_LEN_FEEDBACK_BODY)
    survey_key: Optional[str] = Field(default=None, max_length=_LEN_FEEDBACK_TYPE_HINT)
    context: Optional[Dict[str, Any]] = None


class ReviewSurveyRequest(BaseModel):
    """A review (issue #501): a 1-5 rating, 'what would make this a 10?', an optional public
    testimonial and the consent flag that says we may quote it. Submitting one satisfies the
    extended-trial gate (issue #499)."""
    session_token: str
    rating: int = Field(ge=1, le=5)
    improvement: Optional[str] = Field(default=None, max_length=_LEN_FEEDBACK_BODY)
    testimonial: Optional[str] = Field(default=None, max_length=_LEN_FEEDBACK_BODY)
    consent_testimonial: bool = False
    survey_key: Optional[str] = Field(default=None, max_length=_LEN_FEEDBACK_TYPE_HINT)
    context: Optional[Dict[str, Any]] = None


class SurveyDismissRequest(BaseModel):
    session_token: str
    survey_key: str = Field(min_length=1, max_length=_LEN_FEEDBACK_TYPE_HINT)


class PostHogSurveyRequest(BaseModel):
    """A PostHog Surveys answer relayed by the SPA (issue #653). `kind` says which of the two LEM
    surveys answered — the score bounds are the KIND's, checked in the handler, because 0-10 and 1-5
    can't both be a field constraint. `survey_id`/`survey_name` are PostHog's own, kept so a
    `feedback` row can be lined up against the `survey sent` event the browser already emitted."""
    session_token: str
    kind: str = Field(min_length=1, max_length=_LEN_FEEDBACK_TYPE_HINT)
    score: int = Field(ge=0, le=10)
    comment: Optional[str] = Field(default=None, max_length=_LEN_FEEDBACK_BODY)
    survey_id: Optional[str] = Field(default=None, max_length=64)
    survey_name: Optional[str] = Field(default=None, max_length=128)
    context: Optional[Dict[str, Any]] = None


class ShippedNoticeAckRequest(BaseModel):
    """Acknowledging a "you asked, we shipped" notice (issue #502). `resolved` is the micro-CSAT:
    True/False answers "did this fix it?", None means the user just dismissed the notice."""
    session_token: str
    notice_id: int = Field(gt=0)
    resolved: Optional[bool] = None
    comment: Optional[str] = Field(default=None, max_length=_LEN_FEEDBACK_BODY)


class FutureForwardValues(IntEnum):
    Zero = 0
    ONE = 1
    TWO = 2
    THREE = 3
    FOUR = 4
    FIVE = 5


@app.get("/health")
def health_check():
    return {"status": "healthy"}


@router.get("/app-info")
def get_app_info() -> ResponseModel:
    """Public: the SPA footer reads the running release version + whether to display it."""
    from cqc_lem.utilities.env_constants import get_app_version, SHOW_VERSION_FOOTER
    return ResponseModel(status_code=200, detail={
        "version": get_app_version(),
        "show_version": SHOW_VERSION_FOOTER,
    })


def _bounded_context(context: Optional[Dict[str, Any]]) -> Optional[dict]:
    """Client-supplied context for a feedback/survey row, dropped when a caller tries to write an
    unbounded JSON blob — the widget and the survey modal only send a handful of fields."""
    payload: Dict[str, Any] = dict(context or {})
    if len(json.dumps(payload, default=str)) > _LEN_FEEDBACK_CONTEXT:
        payload = {"truncated": True}
    return payload or None


@router.post("/feedback")
def submit_feedback_endpoint(request: FeedbackRequest) -> ResponseModel:
    """Capture in-app feedback / a bug report (issue #496) — the first capture point of the
    feedback->auto-work loop. A valid session_token attributes the row to that user; without one
    (logged-out visitor) the row is kept anonymously with a NULL user_id."""
    user_id = get_session_user_id(request.session_token) if request.session_token else None
    context: Dict[str, Any] = _bounded_context(request.context) or {}
    if request.screenshot:
        context["screenshot"] = request.screenshot
    feedback_id = insert_feedback(request.body, user_id=user_id,
                                  source=FeedbackSource(request.source),
                                  type_hint=request.type_hint, context=context or None)
    if not feedback_id:
        raise HTTPException(status_code=500, detail="Could not save feedback")
    log_info("Feedback captured", user_id=user_id)
    return ResponseModel(status_code=200, detail={"feedback_id": feedback_id})


@router.post("/survey/nps")
def submit_nps_endpoint(request: NpsSurveyRequest) -> ResponseModel:
    """Capture an NPS response (issue #501) as a `feedback` row with source='nps'. Promoters get
    invited to turn that score into a review, which is what unlocks the extended trial (#499)."""
    user_id = get_session_user_id(request.session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")

    from cqc_lem.utilities.db import has_review_feedback
    from cqc_lem.utilities.surveys import PROMOTER_SCORE, nps_bucket, record_nps_response
    feedback_id = record_nps_response(user_id, request.score, why=request.why,
                                      context=_bounded_context(request.context),
                                      survey_key=request.survey_key)
    if not feedback_id:
        raise HTTPException(status_code=500, detail="Could not save your score")
    log_info("NPS response captured", user_id=user_id)
    return ResponseModel(status_code=200, detail={
        "feedback_id": feedback_id,
        "bucket": nps_bucket(request.score),
        "review_invite": request.score >= PROMOTER_SCORE and not has_review_feedback(user_id),
    })


@router.post("/survey/review")
def submit_review_endpoint(request: ReviewSurveyRequest) -> ResponseModel:
    """Capture a review (issue #501) as a `feedback` row with source='review'. That row IS the gate
    the extended trial checks (issue #499), so the response reports the unlock."""
    user_id = get_session_user_id(request.session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")

    from cqc_lem.utilities.surveys import record_review_response
    feedback_id = record_review_response(
        user_id, request.rating, improvement=request.improvement,
        testimonial=request.testimonial, consent_testimonial=request.consent_testimonial,
        context=_bounded_context(request.context), survey_key=request.survey_key)
    if not feedback_id:
        raise HTTPException(status_code=500, detail="Could not save your review")
    log_info("Review captured", user_id=user_id)
    return ResponseModel(status_code=200, detail={
        "feedback_id": feedback_id,
        "trial_extension_unlocked": True,
    })


@router.post("/survey/dismiss")
def dismiss_survey_endpoint(request: SurveyDismissRequest) -> ResponseModel:
    """User closed the survey modal without answering — record the ask so neither the modal nor the
    email brings it back (issue #501)."""
    user_id = get_session_user_id(request.session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")

    from cqc_lem.utilities.surveys import dismiss_survey
    return ResponseModel(status_code=200,
                         detail={"dismissed": dismiss_survey(user_id, request.survey_key)})


@router.post("/survey/posthog")
def submit_posthog_survey_endpoint(request: PostHogSurveyRequest) -> ResponseModel:
    """Capture a PostHog Surveys answer (issue #653) as a `feedback` row so it reaches the
    feedback->auto-work loop. The browser has already emitted PostHog's own `survey sent`; this
    handler deliberately does NOT emit the homegrown `survey_response` event, so one answer is
    counted once."""
    user_id = get_session_user_id(request.session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")

    from cqc_lem.utilities.surveys import posthog_survey_kinds, record_posthog_survey_response
    spec = posthog_survey_kinds().get(request.kind)
    if spec is None:
        raise HTTPException(status_code=422, detail=f"Unknown survey kind '{request.kind}'")
    if not spec["min"] <= request.score <= spec["max"]:
        raise HTTPException(
            status_code=422,
            detail=f"Score {request.score} is outside {spec['min']}-{spec['max']} for "
                   f"'{request.kind}'")

    result = record_posthog_survey_response(
        user_id, request.kind, request.score, comment=request.comment,
        survey_id=request.survey_id, survey_name=request.survey_name,
        context=_bounded_context(request.context))
    if not result:
        raise HTTPException(status_code=500, detail="Could not save your response")
    log_info("PostHog survey response captured", user_id=user_id)
    return ResponseModel(status_code=200, detail=result)


@router.get("/user/survey")
def survey_endpoint(session_token: str) -> ResponseModel:
    """The survey to show in-app right now (day-3 NPS, trial T-3d NPS, or the review that unlocks
    the extended trial), or none (issue #501). With PostHog Surveys on (issue #653) the NPS asks are
    retired from this snapshot — PostHog is asking them."""
    user_id = get_session_user_id(session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")

    from cqc_lem.utilities.surveys import survey_snapshot
    return ResponseModel(status_code=200, detail=survey_snapshot(user_id))


@router.get("/user/shipped")
def shipped_notices_endpoint(session_token: str) -> ResponseModel:
    """The "you asked, we shipped" notices waiting for this user, plus the recent changelog (issue
    #502). A notice only appears once the reporter has had the fix for FEEDBACK_FIX_CSAT_DELAY_HOURS
    — that delay is what schedules the micro-CSAT it carries."""
    user_id = get_session_user_id(session_token)
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


@router.get("/flags")
def get_feature_flags(session_token: Optional[str] = None) -> ResponseModel:
    """Server-evaluated feature flags for the SPA (issue #651, docs/feature-flags.md).

    This is the SPA's flag BOOTSTRAP: values are resolved server-side with PostHog local evaluation
    (or the env fallback) and shipped in one payload, so the browser renders the right thing on the
    FIRST paint instead of flickering while a client-side flag request lands — and so the SPA, the
    API and the Celery workers can never disagree about a flag's value.

    An invalid or absent session resolves the SAME flags for the `"system"` identity rather than
    401ing: the landing page is logged out and still needs to know what to render."""
    from cqc_lem.utilities.flags import bootstrap_payload
    user_id = get_session_user_id(session_token) if session_token else None
    return ResponseModel(status_code=200, detail=bootstrap_payload(user_id))


@router.get("/faq")
def faq_endpoint() -> ResponseModel:
    """Public: the front-page FAQ (issue #506). Serves only the published entries, in display
    order — the SPA falls back to its built-in copy if this is empty or unreachable."""
    from cqc_lem.utilities.db import get_published_faq_entries
    return ResponseModel(status_code=200, detail={
        "entries": [{"id": e.get("id"), "question": e.get("question"), "answer": e.get("answer"),
                     "updated_at": _utc_iso(e.get("updated_at"))}
                    for e in get_published_faq_entries()],
    })


@router.post("/shipped/ack")
def ack_shipped_notice_endpoint(request: ShippedNoticeAckRequest) -> ResponseModel:
    """Acknowledge a shipped-fix notice and, when the user answered "did this fix it?", record the
    micro-CSAT (issue #502). A "not fixed" answer lands as a `feedback` row at status `new`, so it
    re-enters the auto-work loop instead of stopping at a metric."""
    user_id = get_session_user_id(request.session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")

    from cqc_lem.utilities.db import get_unseen_shipped_notices, mark_shipped_notice_seen
    from cqc_lem.utilities.feedback.shipped import fix_csat_delay_hours
    from cqc_lem.utilities.surveys import record_fix_csat_response
    # Ownership check: a notice is only ackable by a reporter it was actually addressed to, and only
    # once it is actually surfacable — same delay gate as GET /api/user/shipped, so an early ack
    # can't burn a notice (and its micro-CSAT) before the user has been shown it.
    notice = next((n for n in get_unseen_shipped_notices(
                       user_id, delay_hours=fix_csat_delay_hours(), limit=50)
                   if n.get("id") == request.notice_id), None)
    if not notice:
        raise HTTPException(status_code=404, detail="Notice not found or already acknowledged")

    mark_shipped_notice_seen(request.notice_id, user_id)
    feedback_id = None
    if request.resolved is not None:
        feedback_id = record_fix_csat_response(
            user_id, notice.get("github_issue_number"), bool(request.resolved),
            comment=request.comment)
        log_info("Fix CSAT captured", user_id=user_id)
    return ResponseModel(status_code=200, detail={"acknowledged": True,
                                                  "feedback_id": feedback_id})


@router.get("/dashboard/stats/", responses={
    200: {"description": "Dashboard stats returned"},
    **{k: v for k, v in error_responses.items() if k in [400, 403]}
})
def get_dashboard_stats(email: str) -> ResponseModel:
    if not email:
        raise HTTPException(status_code=400, detail="Email is required")

    user_id = get_user_id(email)
    if not user_id:
        raise HTTPException(status_code=403, detail="User not found")

    now = datetime.now(timezone.utc)
    week_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    # Back up to Monday. Use timedelta, not replace(day=...): naive day subtraction
    # goes out of range in the first days of a month (e.g. Wed the 1st → day=-1).
    week_start = week_start - timedelta(days=week_start.weekday())

    # SQL aggregates over ALL posts — the old code counted in Python over get_posts()'s 10-oldest
    # slice, so 'posted' capped near 10 and 'scheduled this week' read ~0 (and a naive/aware
    # datetime compare could 500 the endpoint → all-zeros fallback in the UI).
    stats: Dict[str, int] = get_dashboard_counts(user_id, week_start)
    return ResponseModel(status_code=200, detail=stats)


@router.get("/dashboard/planned-tasks/", responses={
    200: {"description": "Planned tasks returned"},
    **{k: v for k, v in error_responses.items() if k in [400, 403]}
})
def get_planned_tasks_endpoint(email: str, limit: int = Query(default=10, ge=1, le=50)) -> ResponseModel:
    if not email:
        raise HTTPException(status_code=400, detail="Email is required")

    user_id = get_user_id(email)
    if not user_id:
        raise HTTPException(status_code=403, detail="User not found")

    tasks = [
        {
            "kind": t["kind"],
            "id": t["id"],
            "title": t["title"],
            "status": t["status"],
            "scheduled_time": _utc_iso(t["scheduled_time"]),
        }
        for t in get_planned_tasks(user_id, limit=limit)
    ]
    return ResponseModel(status_code=200, detail={"tasks": tasks})


@router.get("/activity/", responses={
    200: {"description": "Recent activity log returned"},
    **{k: v for k, v in error_responses.items() if k in [400, 403]}
})
def get_activity(email: str, limit: int = 20) -> ResponseModel:
    if not email:
        raise HTTPException(status_code=400, detail="Email is required")

    user_id = get_user_id(email)
    if not user_id:
        raise HTTPException(status_code=403, detail="User not found")

    logs = get_recent_logs(user_id, limit=limit)
    serialized = [
        {
            "id": row["id"],
            "action_type": row["action_type"],
            "result": row["result"],
            "post_id": row["post_id"],
            "post_url": _public_post_url(row["post_url"]),
            "message": row["message"],
            "created_at": _utc_iso(row.get("created_at")),
        }
        for row in logs
    ]
    return ResponseModel(status_code=200, detail=serialized)


@router.post("/linkedin/verification-pin/inbound")
async def linkedin_verification_pin_inbound(request: Request) -> ResponseModel:
    """SendGrid Inbound Parse webhook: the user's email reply carrying their LinkedIn
    6-digit code. The tokenized Reply-To (pin+<token>@parse-domain) attributes it to the
    paused login; we extract the code and hand it to the waiting task. Always 200 so
    SendGrid doesn't retry-storm on a malformed/unrelated message."""
    try:
        form = await request.form()
    except Exception:
        return ResponseModel(status_code=200, detail="ignored")
    to_field = str(form.get("to") or "")
    envelope = str(form.get("envelope") or "")
    # SendGrid Inbound Parse routes ALL mail for the parse host to this ONE URL, so this endpoint
    # must also handle the reply+<token> traffic (Gmail forwarding confirmations + comment
    # notifications), not only pin+<token> PIN replies. Dispatch by the address prefix.
    from cqc_lem.utilities.linkedin.notification_email import extract_reply_token_from_address
    if extract_reply_token_from_address(to_field) or extract_reply_token_from_address(envelope):
        return _process_reply_inbound(form)
    text = str(form.get("text") or form.get("html") or "")
    subject = str(form.get("subject") or "")
    token = extract_token_from_address(to_field) or extract_token_from_address(envelope)
    pin = extract_pin_from_text(text) or extract_pin_from_text(subject)
    if not token or not pin:
        return ResponseModel(status_code=200, detail="ignored")
    user_id = submit_pin_by_token(token, pin)
    if user_id:
        log_info("Received LinkedIn verification PIN via email reply", user_id=user_id)
    return ResponseModel(status_code=200, detail="accepted" if user_id else "ignored")


_GMAIL_CONFIRM_KEY = "linkedin:gmail_forward_confirm:{user_id}"


def _handle_gmail_forwarding_confirmation(user_id: int, subject: str, text: str, html: str) -> ResponseModel:
    """Auto-confirm the user's Gmail forwarding to our address: click the verify link server-side
    and stash the numeric code + status so the UI can show it as a fallback if the auto-click didn't
    take. Always 200."""
    from cqc_lem.utilities.linkedin.notification_email import (
        extract_gmail_confirmation_url, extract_gmail_confirmation_code)
    # Prefer the HTML href — the plain-text part wraps the long verify URL across lines and breaks it.
    url = extract_gmail_confirmation_url(html) or extract_gmail_confirmation_url(text)
    code = (extract_gmail_confirmation_code(subject) or extract_gmail_confirmation_code(text)
            or extract_gmail_confirmation_code(html))
    # Log the shape so we can see Gmail's exact format when extraction misses.
    log_info(f"Gmail forwarding confirmation received: url={'yes' if url else 'no'} code={code or 'none'} "
             f"subject={subject[:120]!r} text_head={(text or '')[:500]!r}", user_id=user_id)
    confirmed = False
    if url:
        try:
            resp = requests.get(url, timeout=15)
            confirmed = resp.status_code < 400
            # Gmail may return an interstitial confirm page; follow a nested vf-/uf- link if present.
            page = getattr(resp, "text", "") or ""
            nested = extract_gmail_confirmation_url(page)
            if nested and nested != url:
                try:
                    confirmed = requests.get(nested, timeout=15).status_code < 400 or confirmed
                except Exception:
                    pass
        except Exception as e:
            log_warning("Gmail forwarding auto-confirm click failed", exc=e, user_id=user_id)
    # Server-side clicking may not complete Gmail's flow (interstitial / datacenter IP). Forward the
    # verify link + code to the user's real inbox so they can finish it from their own signed-in
    # browser — the reliable path. The confirmation went to our reply+ address, so they never saw it.
    forwarded = False
    if url or code:
        try:
            from cqc_lem.utilities.db import get_user_email
            from cqc_lem.utilities.email import send_reply_forward_confirmation_email
            user_email = get_user_email(user_id)
            if user_email:
                forwarded = send_reply_forward_confirmation_email(user_email, url, code)
        except Exception as e:
            log_warning("Could not forward Gmail confirmation to user", exc=e, user_id=user_id)
    try:
        from cqc_lem.utilities.linkedin.rate_limit import _redis_client
        client = _redis_client()
        if client is not None:
            client.set(_GMAIL_CONFIRM_KEY.format(user_id=user_id),
                       json.dumps({"code": code, "confirmed": confirmed, "url_found": bool(url),
                                   "forwarded_to_user": forwarded}),
                       ex=7 * 24 * 60 * 60)
    except Exception:
        pass
    log_info(f"Gmail forwarding confirmation: url_found={bool(url)} confirmed={confirmed} "
             f"code={'yes' if code else 'no'} forwarded_to_user={forwarded}", user_id=user_id)
    detail = "confirmed" if confirmed else ("forwarded" if forwarded else ("code_stored" if code else "ignored"))
    return ResponseModel(status_code=200, detail=detail)


def get_gmail_forward_confirmation(user_id: int) -> "dict | None":
    """Last Gmail-forwarding confirmation result for a user (auto-confirmed? code fallback?)."""
    try:
        from cqc_lem.utilities.linkedin.rate_limit import _redis_client
        client = _redis_client()
        if client is None:
            return None
        raw = client.get(_GMAIL_CONFIRM_KEY.format(user_id=user_id))
        if not raw:
            return None
        return json.loads(raw.decode() if isinstance(raw, (bytes, bytearray)) else raw)
    except Exception:
        return None


def _reply_sweep_debounced(user_id: int, window_s: int = 120) -> bool:
    """True the first time we see this user within window_s — collapses a burst of comment
    notifications into ONE reply sweep. Fails OPEN (returns True) if Redis is unavailable so a
    notification is never silently dropped."""
    try:
        from cqc_lem.utilities.linkedin.rate_limit import _redis_client
        client = _redis_client()
        if client is None:
            return True
        return bool(client.set(f"linkedin:reply_sweep_debounce:{user_id}", "1", nx=True, ex=window_s))
    except Exception:
        return True


def _process_reply_inbound(form) -> ResponseModel:
    """Handle inbound mail sent to a reply+<token>@parse-domain address: a Gmail forwarding
    confirmation (auto-click the verify link) or a forwarded LinkedIn comment notification (trigger a
    debounced recent-posts reply sweep). Reactions/unknown tokens are ignored. Always 200. Called
    from BOTH inbound endpoints because SendGrid Inbound Parse posts all parse-host mail to one URL."""
    from cqc_lem.utilities.linkedin.notification_email import (
        extract_reply_token_from_address, is_comment_notification, is_gmail_forwarding_confirmation)
    from cqc_lem.utilities.db import get_user_id_by_reply_token
    to_field = str(form.get("to") or "")
    envelope = str(form.get("envelope") or "")
    from_field = str(form.get("from") or "")
    subject = str(form.get("subject") or "")
    text = str(form.get("text") or "")
    html = str(form.get("html") or "")
    token = extract_reply_token_from_address(to_field) or extract_reply_token_from_address(envelope)
    if not token:
        return ResponseModel(status_code=200, detail="ignored")
    user_id = get_user_id_by_reply_token(token)
    if not user_id:
        return ResponseModel(status_code=200, detail="ignored")
    # Gmail forwarding confirmation: the address is ours + token-gated, so auto-click the verify link.
    if is_gmail_forwarding_confirmation(from_field, subject, text or html):
        return _handle_gmail_forwarding_confirmation(user_id, subject, text, html)
    if not is_comment_notification(subject, text or html):
        return ResponseModel(status_code=200, detail="ignored")
    if not _reply_sweep_debounced(user_id):
        return ResponseModel(status_code=200, detail="debounced")
    sweep_reply_comments.apply_async(kwargs={"user_id": user_id}, countdown=120)
    log_info("Triggered reply sweep from comment notification", user_id=user_id)
    return ResponseModel(status_code=200, detail="accepted")


@router.post("/linkedin/comment-notification/inbound")
async def linkedin_comment_notification_inbound(request: Request) -> ResponseModel:
    """SendGrid Inbound Parse webhook for reply+<token> mail (kept as an explicit path; SendGrid
    actually delivers to the shared parse URL, which also routes here via _process_reply_inbound)."""
    try:
        form = await request.form()
    except Exception:
        return ResponseModel(status_code=200, detail="ignored")
    return _process_reply_inbound(form)


@router.put("/user/", responses={
    200: {"description": "User settings updated"},
    **{k: v for k, v in error_responses.items() if k in [400, 403, 404]}
})
def update_user_endpoint(settings: UserSettingsRequest) -> ResponseModel:
    if not settings.email:
        raise HTTPException(status_code=400, detail="Email is required")

    user_id = get_user_id(settings.email)
    if not user_id:
        raise HTTPException(status_code=403, detail="User not found")

    if not any([settings.new_email, settings.blog_url, settings.sitemap_url]):
        return ResponseModel(status_code=200, detail="User settings unchanged")

    updated = update_user(user_id, email=settings.new_email, blog_url=settings.blog_url, sitemap_url=settings.sitemap_url)
    if not updated:
        raise HTTPException(status_code=404, detail="Update failed")
    return ResponseModel(status_code=200, detail="User updated successfully")


@router.post("/automate_reply_commenting", responses={
    200: {"description": "Post reply automation scheduled successfully"},
    **{k: v for k, v in error_responses.items() if k in [403, 404]}
})
def automate_reply_commenting_for_post_id(post_id: int, loop_for_duration: int = 60 * 60,
                                          future_forward: FutureForwardValues = Query(
                                              default=0,
                                              description="Forward index (0-5) to use for future calls",
                                              examples=[0, 1, 2, 3, 4, 5]
                                          )):
    user_id = get_post_user_id(post_id)
    if not user_id:
        raise HTTPException(status_code=403, detail="User Id for Post not found")

    try:
        base_kwargs = {
            'user_id': user_id,
            'post_id': post_id,
            'loop_for_duration': loop_for_duration,
            'future_forward': future_forward
        }
        automate_reply_commenting.apply_async(kwargs=base_kwargs)
        return ResponseModel(status_code=200, detail="Post automation reply successfully scheduled")
    except Exception as e:
        raise HTTPException(status_code=404, detail=f"Could not schedule automation for post. Error: {e}")


@router.post("/schedule_post/", responses={
    200: {"description": "Post scheduled successfully"},
    **{k: v for k, v in error_responses.items() if k in [403, 404]}
})
def schedule_post(post: PostRequest) -> ResponseModel:
    user_id = get_user_id(post.email)
    if not user_id:
        raise HTTPException(status_code=403, detail="User not found")

    # SPA-created posts carry an explicit status: "Approve & Schedule" → approved,
    # "Save Draft" → pending. Auto-generated content sets its own status elsewhere.
    if insert_post(post.email, post.content, post.scheduled_datetime, post.post_type,
                   video_url=post.video_url, carousel_slides=post.carousel_slides,
                   video_quality=post.video_quality or "standard",
                   status=post.status or PostStatus.PENDING):
        return ResponseModel(status_code=200, detail="Post scheduled successfully")
    else:
        raise HTTPException(status_code=404, detail="Could not schedule post")


@router.post("/create_weekly_content/", responses={
    200: {"description": "Weekly content created successfully"},
    500: {"description": "Could not queue content generation"},
    **{k: v for k, v in error_responses.items() if k in [400]}
})
def create_weekly_content(user_id: int) -> ResponseModel:
    if not user_id:
        raise HTTPException(status_code=400, detail="User ID is required")

    # Generation runs for minutes in the background, so publish a 'queued' progress record now —
    # the SPA polls /content_generation_status/ and would otherwise show nothing (issue #545).
    mark_queued(user_id)

    # Chain: plan posts for the rest of the month first, then fill content for this week.
    # This ensures the user always has PLANNING rows before content generation runs.
    try:
        celery_chain(
            plan_content_for_user.si(user_id=user_id),
            auto_create_weekly_content.si(user_id=user_id),
        ).apply_async()
    except Exception as e:
        # Nothing will ever run, so drop the 'queued' record rather than leaving the SPA polling
        # a run that never starts (it would otherwise sit there until the TTL expires).
        clear_generation_status(user_id)
        log_error("Could not dispatch weekly content generation", exc=e, user_id=user_id)
        raise HTTPException(status_code=500, detail="Could not queue content generation")

    return ResponseModel(status_code=200, detail="Weekly content created successfully")


@router.get("/content_generation_status/", responses={
    200: {"description": "Content generation progress"},
    **{k: v for k, v in error_responses.items() if k in [401]}
})
def get_content_generation_status_endpoint(session_token: str) -> ResponseModel:
    """Progress of the caller's weekly content-generation run — queued → in_progress (X of N) →
    done/failed. `detail` is None when no run is being tracked (nothing started, or it aged out).
    Scoped by session rather than a user_id query param so one user can't poll another's run."""
    user_id = get_session_user_id(session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    return ResponseModel(status_code=200, detail=get_generation_status(user_id))


@router.post("/invite_to_li_company_page/", responses={
    200: {"description": "Invite Users to LinkedIn Company Page"},
    **{k: v for k, v in error_responses.items() if k in [400]}
})
def invite_to_li_company_page(user_id: int) -> ResponseModel:
    if not user_id:
        raise HTTPException(status_code=400, detail="User ID is required")

    automate_invites_to_company_page_for_user.apply_async(
        kwargs={'user_id': user_id}, retry=True,
        retry_policy={'max_retries': 3, 'interval_start': 60, 'interval_step': 30}
    )
    return ResponseModel(status_code=200, detail="Process to invite to LinkedIn Company Page Started")


@router.post("/aws_test_get_my_profile/", responses={
    200: {"description": "Test Get My Profile on AWS"},
    **{k: v for k, v in error_responses.items() if k in [400]}
})
def aws_test_get_my_profile(user_id: int) -> ResponseModel:
    if not user_id:
        raise HTTPException(status_code=400, detail="User ID is required")

    test_get_my_profile.apply_async(kwargs={'user_id': user_id}, retry=True,
                                    retry_policy={'max_retries': 1})
    return ResponseModel(status_code=200, detail="Test Get My Profile on AWS Message Sent to Celery Queue")


@router.get('/user_id/', responses={
    200: {"description": "User ID retrieved successfully"},
    **{k: v for k, v in error_responses.items() if k in [400, 403]}
})
def get_user_id_from_email(email: str) -> ResponseModel:
    if not email:
        raise HTTPException(status_code=400, detail="Email is required")

    user_id = get_user_id(email)
    if not user_id:
        raise HTTPException(status_code=403, detail="User not found")

    return ResponseModel(status_code=200, detail=user_id)


@router.get("/posts/", responses={
    200: {"description": "Posts retrieved successfully"},
    **{k: v for k, v in error_responses.items() if k in [400, 404]}
})
def get_posts_for_email(
    email: str,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=10, ge=1, le=200),
    sort_order: str = Query(default='asc', pattern='^(asc|desc)$'),
    sort_by: str = Query(default='scheduled_time', pattern='^(scheduled_time|status|post_type|id)$'),
    status_filter: Optional[str] = Query(default=None),
    post_type_filter: Optional[str] = Query(default=None, pattern='^(text|video|carousel|document)$'),
    search: Optional[str] = Query(default=None, max_length=500),
) -> ResponseModel:
    if not email:
        raise HTTPException(status_code=400, detail="Email is required")

    offset = (page - 1) * page_size
    posts, total = get_post_by_email(
        email, limit=page_size, offset=offset,
        sort_order=sort_order, status_filter=status_filter,
        post_type_filter=post_type_filter, search=search, sort_by=sort_by,
    )

    posts_list = [
        {
            "post_id": post["id"],
            "content": post["content"],
            "video_url": post["video_url"],
            "scheduled_time": _utc_iso(post["scheduled_time"]),
            "post_type": post["post_type"],
            "status": post["status"],
            "carousel_slides": _parse_slides(post.get("carousel_slides")),
            # The SHAPE this draft was written to (V51 posts.archetype) — the review UI reports it
            # on post_approved / post_rejected so approval rate can be broken down by archetype.
            "archetype": post.get("archetype"),
            # Why a draft is being held, and what to do about it (issue #421).
            "authenticity_score": post.get("authenticity_score"),
            "gate_reason": parse_gate_findings(post.get("gate_reason")),
        }
        for post in posts
    ]
    return ResponseModel(status_code=200, detail={
        "posts": posts_list,
        "total": total,
        "page": page,
        "page_size": page_size,
    })


@router.post("/posts/bulk_update/", responses={
    200: {"description": "Posts updated successfully"},
    **{k: v for k, v in error_responses.items() if k in [400, 405]}
})
def bulk_update_posts_endpoint(request: BulkUpdateRequest) -> ResponseModel:
    if not request.post_ids:
        raise HTTPException(status_code=400, detail="post_ids is required")

    if bulk_update_posts(request.post_ids, status=request.status, scheduled_time=request.scheduled_datetime):
        return ResponseModel(status_code=200, detail="Posts updated successfully")
    else:
        raise HTTPException(status_code=405, detail="Posts could not be updated")


@router.delete("/posts/", responses={
    200: {"description": "Posts deleted (soft) successfully"},
    **{k: v for k, v in error_responses.items() if k in [400, 405]}
})
def delete_posts_endpoint(request: BulkDeleteRequest) -> ResponseModel:
    if not request.post_ids:
        raise HTTPException(status_code=400, detail="post_ids is required")

    if soft_delete_posts(request.post_ids):
        return ResponseModel(status_code=200, detail="Posts deleted successfully")
    else:
        raise HTTPException(status_code=405, detail="Posts could not be deleted")


@router.get("/post_url/", responses={
    200: {"description": "LinkedIn post URL returned"},
    **{k: v for k, v in error_responses.items() if k in [400, 403]}
})
def get_post_url(post_id: int, email: str) -> ResponseModel:
    if not email:
        raise HTTPException(status_code=400, detail="Email is required")
    user_id = get_user_id(email)
    if not user_id:
        raise HTTPException(status_code=403, detail="User not found")
    post_url = get_post_url_from_log_for_user(user_id, post_id)
    return ResponseModel(status_code=200, detail={"post_url": post_url})


@router.post("/update_post/", responses={
    200: {"description": "Post updated successfully"},
    **{k: v for k, v in error_responses.items() if k in [405]}
})
def update_post(post_id: int, post: PostRequest) -> ResponseModel:
    myprint(f"Received Post Request: {post}")

    if update_db_post(post.content, post.video_url, post.scheduled_datetime, post.post_type, post_id, post.status):
        return ResponseModel(status_code=200, detail="Post updated successful")
    else:
        raise HTTPException(status_code=405, detail="Post could not be updated")


def _find_asset_file(root: str, rel_path: str) -> str | None:
    """Resolve rel_path within root using OS directory entries.

    Each returned path comes from os.scandir() (server-controlled), so it is
    never derived from user-supplied input.  Traversal components (.. / .) and
    separator characters are rejected before any filesystem access.
    """
    parts = [p for p in rel_path.replace("\\", "/").split("/") if p]
    if not parts:
        return None
    for part in parts:
        if part in (".", "..") or os.sep in part:
            return None
    current = root
    for i, part in enumerate(parts):
        try:
            matched = next((e for e in os.scandir(current) if e.name == part), None)
        except OSError:
            return None
        if matched is None:
            return None
        is_last = i == len(parts) - 1
        if is_last:
            return matched.path if matched.is_file() else None
        if not matched.is_dir():
            return None
        current = matched.path   # descend into subdirectory
    return None


@router.get("/assets", response_model=None, responses={
    200: {"description": "Asset returned successfully"},
    206: {"description": "Asset returned successfully via stream"},
    **{k: v for k, v in error_responses.items() if k in [400, 404]}
})
def get_assets(file_name: str, content_type: Optional[str] = None,
               request: Optional[Any] = None) -> Union[ResponseModel, FileResponse, StreamingResponse]:
    if not file_name:
        raise HTTPException(status_code=400, detail="A File Name is required")

    # Resolve the file via a filesystem scan of the trusted assets_dir (CWE-22).
    # _find_asset_file returns OS-provided paths, never paths constructed from
    # user input, so the taint chain from file_name is broken entirely.
    file_path = _find_asset_file(assets_dir, file_name)
    if file_path is None:
        raise HTTPException(status_code=404, detail="File not found")

    myprint(f"File Path: {file_path}")
    myprint(f"Content Type: {content_type}")

    file_extension = get_file_extension_from_filepath(file_path)
    mim_type = get_file_mime_type(file_extension)

    if request:
        return range_requests_response(request, file_path=file_path, content_type=mim_type)
    else:
        return FileResponse(status_code=200, path=file_path, media_type=mim_type, content_disposition_type=content_type)


def _attribution_dict(attribution: Optional[FunnelAttribution]) -> dict:
    return attribution.model_dump(exclude_none=True) if attribution else {}


def _track_signup_funnel(user_id: int, email: str, attribution: dict, pin_bypassed: bool) -> None:
    """`signup_completed` + `trial_started` for an account that was just created. Both are aliased
    onto the anonymous id `signup_started` used, so the funnel joins end to end in PostHog; the trial
    starts at the same instant because `add_user_by_email` opens the free trial on insert."""
    from cqc_lem.utilities.env_constants import FREE_TRIAL_DAYS
    anon_id = anonymous_distinct_id(email)
    track_funnel_event(FUNNEL_SIGNUP_COMPLETED, user_id=user_id, attribution=attribution,
                       alias_from=anon_id, method="email_pin", pin_bypassed=pin_bypassed)
    track_funnel_event(FUNNEL_TRIAL_STARTED, user_id=user_id, attribution=attribution,
                       trial_days=FREE_TRIAL_DAYS, tier="free_trial")


# LinkedIn OAuth initiation — builds the authorization URL and redirects user to LinkedIn
@router.post("/auth/email/init")
def auth_email_init(request: AuthInitRequest) -> ResponseModel:
    email = request.email.strip().lower()
    if not email:
        raise HTTPException(status_code=400, detail="Email is required")

    user_exists = bool(get_user_id(email))
    attribution = _attribution_dict(request.attribution)
    if not user_exists:
        # A known email re-authenticating is a login, not a signup — only new emails enter the funnel.
        track_funnel_event(FUNNEL_SIGNUP_STARTED, distinct_id=anonymous_distinct_id(email),
                           attribution=attribution, method="email_pin")
    pin = generate_pin()
    pin_hash = hash_pin(pin, email)

    # Check bypass first so we skip DB + email entirely when no provider is configured
    _, bypassed = send_pin_email(email, pin, is_new_user=not user_exists, probe_only=True)
    if bypassed:
        # No email provider configured — create user + session immediately, skip PIN step
        user_id = get_user_id(email)
        is_new_user = user_id is None
        if is_new_user:
            user_id = add_user_by_email(email)
            if not user_id:
                raise HTTPException(status_code=500, detail="Could not create user record")
        session_token = create_session(user_id)
        if not session_token:
            raise HTTPException(status_code=500, detail="Could not create session")
        if is_new_user:
            _track_signup_funnel(user_id, email, attribution, pin_bypassed=True)
        return ResponseModel(status_code=200, detail={
            "bypass": True,
            "session_token": session_token,
            "email": email,
            "is_new_user": is_new_user,
        })

    # Write PIN to DB BEFORE sending email — if send fails we can delete the row;
    # the reverse order would leave users with an unverifiable PIN in their inbox.
    if not create_pin_for_email(email, pin_hash):
        raise HTTPException(status_code=500, detail="Could not create PIN")

    sent, _ = send_pin_email(email, pin, is_new_user=not user_exists)
    if not sent:
        # Clean up the PIN row so the user can retry without waiting for expiry
        delete_pin_for_email(email)
        raise HTTPException(status_code=500, detail="Could not send PIN email — check email provider settings")

    return ResponseModel(status_code=200, detail={"bypass": False, "user_exists": user_exists, "message": "PIN sent to email"})


@router.post("/auth/email/verify")
def auth_email_verify(request: AuthVerifyRequest) -> ResponseModel:
    email = request.email.strip().lower()
    pin = request.pin.strip()
    if not email or not pin:
        raise HTTPException(status_code=400, detail="Email and PIN are required")

    pin_hash = hash_pin(pin, email)
    if not verify_pin_for_email(email, pin_hash):
        raise HTTPException(status_code=401, detail="Invalid or expired PIN")

    user_id = get_user_id(email)
    is_new_user = user_id is None
    if is_new_user:
        user_id = add_user_by_email(email)
        if not user_id:
            raise HTTPException(status_code=500, detail="Could not create user record")

    session_token = create_session(user_id)
    if not session_token:
        raise HTTPException(status_code=500, detail="Could not create session")

    if is_new_user:
        _track_signup_funnel(user_id, email, _attribution_dict(request.attribution),
                             pin_bypassed=False)

    return ResponseModel(
        status_code=200,
        detail={"session_token": session_token, "email": email, "is_new_user": is_new_user},
    )


@router.post("/auth/logout")
def auth_logout(request: LogoutRequest) -> ResponseModel:
    delete_session(request.session_token)
    return ResponseModel(status_code=200, detail="Logged out")


@router.get("/auth/session")
def auth_check_session(session_token: str) -> ResponseModel:
    user_id = get_session_user_id(session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    email = get_user_email(user_id)
    # Person facts the SPA sets on the PostHog person at $identify (issue #646). Plan/timezone/
    # signup only — never credentials — and the session check is the one call every authenticated
    # page already makes, so identify needs no extra round trip.
    profile = get_user_analytics_profile(user_id)
    return ResponseModel(status_code=200, detail={
        "user_id": user_id,
        "email": email,
        "plan": profile.get("subscription_tier"),
        "plan_status": profile.get("subscription_status"),
        "timezone": profile.get("timezone"),
        "created_at": _utc_iso(profile.get("created_at")),
        # The two facts PostHog Surveys target on (issue #653). They ride on the session check
        # because that is the call every authenticated page already makes — a survey that needed its
        # own round trip would be a survey that never fired on the first page view.
        "onboarding_completed_at": _utc_iso(profile.get("onboarding_completed_at")),
        "posts_approved": int(profile.get("posts_approved") or 0),
    })


@router.get("/user/token_status")
def get_user_token_status(session_token: str) -> ResponseModel:
    user_id = get_session_user_id(session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")

    token_info = get_user_token_info(user_id)
    if not token_info or not token_info.get('access_token'):
        return ResponseModel(status_code=200, detail={
            "token_expiry_date": None,
            "is_expiring_soon": True,
            "is_expired": True,
            "refresh_attempted": False,
            "refresh_succeeded": False,
        })

    expiring_soon = is_token_expiring_soon(token_info)
    expired = is_token_expired(token_info)
    refresh_attempted = False
    refresh_succeeded = False

    if expiring_soon and token_info.get('refresh_token'):
        refresh_attempted = True
        refresh_succeeded, _ = attempt_token_refresh(user_id)
        if refresh_succeeded:
            token_info = get_user_token_info(user_id)
            expiring_soon = is_token_expiring_soon(token_info)
            expired = is_token_expired(token_info)

    expiry = get_token_expiry(token_info)
    return ResponseModel(status_code=200, detail={
        "token_expiry_date": expiry.isoformat() if expiry else None,
        "is_expiring_soon": expiring_soon,
        "is_expired": expired,
        "refresh_attempted": refresh_attempted,
        "refresh_succeeded": refresh_succeeded,
    })


@app.get("/auth/linkedin/", response_model=None, include_in_schema=False)
@router.get("/auth/linkedin/", response_model=None, include_in_schema=False)
def linkedin_auth_init(email: str = None, session_token: str = None) -> RedirectResponse:
    client = AuthClient(LI_CLIENT_ID, LI_CLIENT_SECRET, LI_REDIRECT_URL)
    # Embed the session_token in state so the callback can find the right user,
    # even when the LinkedIn account email differs from the login email.
    state = f"{LI_STATE_SALT}:{session_token}" if session_token else LI_STATE_SALT
    auth_url = client.generate_member_auth_url(
        state=state,
        scopes=["openid", "profile", "email", "w_member_social"]
    )
    return RedirectResponse(url=auth_url)


# LinkedIn OAuth callback lives outside /api since LinkedIn redirects here
@app.get("/auth/linkedin/callback", response_model=None)
def linkedin_callback(code: str, state: str = None) -> Union[ResponseModel, RedirectResponse]:
    from urllib.parse import urlencode

    def _account_redirect(params: dict) -> RedirectResponse:
        parsed_url = urlparse(LI_REDIRECT_URL)
        base_host = f"{parsed_url.scheme}://{parsed_url.netloc.split(':')[0]}"
        if os.environ.get('NGROK_CUSTOM_DOMAIN'):
            base_host = "https://" + os.environ.get('NGROK_CUSTOM_DOMAIN')
        return RedirectResponse(url=f"{base_host}/account?{urlencode(params)}")

    # State format: "{LI_STATE_SALT}:{session_token}" or just "{LI_STATE_SALT}"
    session_token_from_state: Optional[str] = None
    if state is not None:
        if ':' in state:
            salt_part, session_token_from_state = state.split(':', 1)
            if salt_part != LI_STATE_SALT:
                raise HTTPException(status_code=400, detail="Invalid state parameter")
        elif state != LI_STATE_SALT:
            raise HTTPException(status_code=400, detail="Invalid state parameter")

    client = AuthClient(LI_CLIENT_ID, LI_CLIENT_SECRET, LI_REDIRECT_URL)
    try:
        access_token_response = client.exchange_auth_code_for_access_token(code)
    except (ResponseFormattingError, Exception) as exc:
        myprint(f"LinkedIn token exchange failed: {exc}")
        return _account_redirect({'li_error': 'token_exchange_failed'})

    myprint("Access token Response from api call")
    for key, value in access_token_response.__dict__.items():
        myprint(f"{key}: {value}")

    if not access_token_response.access_token:
        myprint("LinkedIn token exchange returned no access_token")
        return _account_redirect({'li_error': 'no_access_token'})

    try:
        restli_client = RestliClient()
        response = restli_client.get(
            resource_path='/userinfo',
            access_token=access_token_response.access_token,
        )
        myprint("Response from /userinfo api call:")
        for key, value in response.__dict__.items():
            myprint(f"{key}: {value}")
    except Exception as exc:
        myprint(f"LinkedIn /userinfo call failed: {exc}")
        return _account_redirect({'li_error': 'userinfo_failed'})

    user_email = response.entity.get('email', '')
    linked_sub_id = response.entity.get('sub', '')

    # Prefer updating the logged-in user's record directly (handles the case where
    # the LinkedIn account email differs from the app login email).
    user_id = get_session_user_id(session_token_from_state) if session_token_from_state else None
    if user_id:
        myprint(f"Updating LinkedIn token for session user_id={user_id}")
        update_user_linkedin_token(
            user_id,
            linked_sub_id,
            access_token_response.access_token,
            access_token_response.expires_in,
            access_token_response.refresh_token,
            access_token_response.refresh_token_expires_in,
            linkedin_email=user_email or None,
        )
    else:
        if not user_email:
            myprint("LinkedIn /userinfo returned no email and no valid session")
            return _account_redirect({'li_error': 'no_email'})
        myprint(f"No session in state — upserting by LinkedIn email {user_email}")
        add_user_with_access_token(
            user_email,
            linked_sub_id,
            access_token_response.access_token,
            access_token_response.expires_in,
            access_token_response.refresh_token,
            access_token_response.refresh_token_expires_in,
        )

    return _account_redirect({'email': user_email, 'li_connected': '1'})


@router.get("/user/settings")
def get_user_settings(session_token: str) -> ResponseModel:
    user_id = get_session_user_id(session_token)
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


@router.put("/user/settings")
def update_user_settings_endpoint(request: UserPreferencesRequest) -> ResponseModel:
    user_id = get_session_user_id(request.session_token)
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


@router.get("/user/engagement-preferences")
def get_engagement_preferences_endpoint(session_token: str) -> ResponseModel:
    user_id = get_session_user_id(session_token)
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
        from cqc_lem.utilities.linkedin.notification_email import reply_inbound_address
        token = get_or_create_reply_inbound_token(user_id)
        prefs["reply_inbound_address"] = reply_inbound_address(token) if token else None
    except Exception:
        prefs["reply_inbound_address"] = None
    # Gmail forwarding auto-confirmation status (so the UI can surface the code if auto-confirm failed).
    prefs["gmail_forward_confirmation"] = get_gmail_forward_confirmation(user_id)
    # Read-only: the highest catch-up cap this plan allows, so the UI can bound the input and show
    # what upgrading unlocks (10/day is premium-only).
    prefs["max_catchup_touches_allowed"] = max_catchup_touches_allowed(user_id)
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
        from cqc_lem.app.run_automation import get_feed_funnel
        prefs["feed_reach"] = get_feed_funnel(user_id)
    except Exception:
        prefs["feed_reach"] = None
    return ResponseModel(status_code=200, detail=prefs)


@router.put("/user/engagement-preferences")
def update_engagement_preferences_endpoint(request: EngagementPreferencesRequest) -> ResponseModel:
    user_id = get_session_user_id(request.session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    prefs = request.model_dump(exclude={"session_token"})
    if not update_engagement_preferences(user_id, prefs):
        raise HTTPException(status_code=500, detail="Could not update engagement preferences")
    return ResponseModel(status_code=200, detail="Engagement preferences updated")


@router.get("/user/newsletter-settings")
def get_newsletter_settings_endpoint(session_token: str) -> ResponseModel:
    user_id = get_session_user_id(session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    return ResponseModel(status_code=200, detail=get_newsletter_settings(user_id))


@router.get("/user/newsletter-subscribers")
def get_newsletter_subscribers_endpoint(session_token: str) -> ResponseModel:
    """Subscriber-growth time-series for the current user (issue #400): the recorded snapshots plus
    the latest known subscriber count, for charting growth over time.

    `attribution` (issue #624) is what that growth can be read against: the owned-asset CTAs that
    actually delivered something in the same window — approval-gated lead-magnet DMs, and posts that
    carried the subscribe link into their first comment."""
    user_id = get_session_user_id(session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    from cqc_lem.utilities.db import (get_newsletter_subscriber_stats,
                                      get_latest_newsletter_subscriber_count,
                                      count_artifact_cta_deliveries)
    newsletter_url = (get_newsletter_settings(user_id) or {}).get("newsletter_url")
    return ResponseModel(status_code=200, detail={
        "latest": get_latest_newsletter_subscriber_count(user_id),
        "history": get_newsletter_subscriber_stats(user_id),
        "attribution": count_artifact_cta_deliveries(user_id, newsletter_url=newsletter_url),
    })


@router.put("/user/newsletter-settings")
def update_newsletter_settings_endpoint(request: NewsletterSettingsRequest) -> ResponseModel:
    user_id = get_session_user_id(request.session_token)
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
    user's last_published_at is used, giving the soonest upcoming slot."""
    import pytz
    from datetime import datetime as _dt, timezone as _tz
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


@router.get("/user/newsletter-draft")
def get_newsletter_draft_endpoint(session_token: str) -> ResponseModel:
    user_id = get_session_user_id(session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    editions = get_pending_newsletter_editions(user_id)
    for e in editions:
        if e.get("scheduled_for") is not None:
            e["scheduled_for"] = _utc_iso(e["scheduled_for"])
    # next_publish is the slot AFTER the last edition already queued, so the UI can show what's next.
    anchor = get_latest_edition_scheduled_for(user_id)
    next_pub = _compute_next_publish(user_id, anchor=anchor)
    settings = get_newsletter_settings(user_id)
    return ResponseModel(status_code=200, detail={
        "editions": editions,
        "next_publish": _utc_iso(next_pub),
        "max_queued_drafts": settings.get("max_queued_drafts", 1),
        "generate_lead_days": settings.get("generate_lead_days", 3),
    })


@router.put("/user/newsletter-draft")
def update_newsletter_draft_endpoint(request: NewsletterDraftRequest) -> ResponseModel:
    user_id = get_session_user_id(request.session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    existing = get_newsletter_edition(request.edition_id)
    if not existing or existing.get("user_id") != user_id:
        raise HTTPException(status_code=404, detail="Edition not found")
    status = {"approve": "approved", "skip": "skipped"}.get(request.action)  # None for 'save'
    if not update_newsletter_edition(request.edition_id, user_id, title=request.title,
                                     subtitle=request.subtitle, body=request.body, status=status,
                                     scheduled_for=request.scheduled_datetime):
        raise HTTPException(status_code=500, detail="Could not update newsletter draft")
    return ResponseModel(status_code=200, detail="Newsletter draft updated")


@router.post("/user/newsletter-draft/regenerate")
def regenerate_newsletter_draft_endpoint(request: NewsletterRegenerateRequest) -> ResponseModel:
    """Regenerate a single queued edition. Generation is a slow lem-complex call, so dispatch it to a
    Celery task and let the UI refetch the queue once it lands. Optional free-text `guidance` steers
    the rewrite; empty guidance lets the AI decide a fresh, distinct take."""
    user_id = get_session_user_id(request.session_token)
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


@router.post("/user/post/regenerate")
def regenerate_post_endpoint(request: PostRegenerateRequest) -> ResponseModel:
    """Regenerate a single pending/approved post. Generation is a slow lem-complex call, so dispatch
    it to a Celery task; the post resets to 'pending' for re-review. Optional free-text `guidance`
    steers the rewrite while the base regeneration honors the user's saved engagement settings."""
    user_id = get_session_user_id(request.session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    if get_post_user_id(request.post_id) != user_id:
        raise HTTPException(status_code=404, detail="Post not found")
    # Regeneration resets the post to PENDING — only sensible from the review states. Regenerating
    # a scheduled/posted/rejected post would silently yank it out of (or back into) the pipeline.
    post_status = get_post_status(request.post_id)
    if post_status not in (PostStatus.PENDING.value, PostStatus.APPROVED.value):
        raise HTTPException(
            status_code=409,
            detail=f"Post is '{post_status}' — only pending or approved posts can be regenerated")
    from cqc_lem.app.run_content_plan import regenerate_post_task
    guidance = (request.guidance or "").strip() or None
    regenerate_post_task.apply_async(kwargs={"post_id": request.post_id, "guidance": guidance})
    return ResponseModel(status_code=200, detail="Regeneration started")


@router.post("/user/post/rescore")
def rescore_post_endpoint(request: PostRescoreRequest) -> ResponseModel:
    """Re-run the quality gates on a pending/approved post's CURRENT content (issue #421) — the
    'edit & re-score' half of the review flow. Save the edit first, then call this: a draft that now
    clears every gate is promoted PENDING -> APPROVED without a full regenerate, and one that still
    fails comes back with a fresh reason + remediation. Runs inline (one judge call) so the UI can
    show the verdict immediately."""
    user_id = get_session_user_id(request.session_token)
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


# --- Scheduled 1:1 DMs (issue #306) — mirrors the post scheduler endpoints ---

@router.post("/schedule_dm")
def schedule_dm_endpoint(request: ScheduleDmRequest) -> ResponseModel:
    """Create a scheduled 1:1 DM (draft or approved). The beat scanner (auto_check_scheduled_dms)
    sends approved DMs at their scheduled_time via send_scheduled_dm, honoring per-day DM caps."""
    user_id = get_session_user_id(request.session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    status = ScheduledDmStatus.APPROVED if request.status == "approved" else ScheduledDmStatus.PENDING
    dm_id = insert_scheduled_dm(user_id, request.recipient_profile_url, request.message,
                                request.scheduled_datetime, recipient_name=request.recipient_name,
                                status=status)
    if not dm_id:
        raise HTTPException(status_code=500, detail="Could not schedule DM")
    return ResponseModel(status_code=200, detail={"dm_id": dm_id})


@router.get("/dms")
def list_scheduled_dms_endpoint(session_token: str, status_filter: Optional[str] = None,
                                page: int = 1, page_size: int = 25,
                                sort_order: str = "asc") -> ResponseModel:
    user_id = get_session_user_id(session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    result = get_scheduled_dms(user_id, status_filter=status_filter, page=page,
                               page_size=page_size, sort_order=sort_order)
    for dm in result.get("dms", []):
        for key in ("scheduled_time", "created_at", "updated_at"):
            if dm.get(key) is not None:
                dm[key] = _utc_iso(dm[key])
    return ResponseModel(status_code=200, detail=result)


@router.put("/dm")
def update_scheduled_dm_endpoint(request: UpdateDmRequest) -> ResponseModel:
    user_id = get_session_user_id(request.session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    if get_scheduled_dm_user_id(request.dm_id) != user_id:
        raise HTTPException(status_code=404, detail="Scheduled DM not found")
    action_map = {"approve": ScheduledDmStatus.APPROVED, "cancel": ScheduledDmStatus.CANCELED}
    if request.action is not None and request.action not in action_map:
        raise HTTPException(status_code=422,
                            detail=f"Unknown action '{request.action}' — expected 'approve' or 'cancel'")
    status = action_map.get(request.action)
    if status is None and all(v is None for v in (request.recipient_profile_url, request.recipient_name,
                                                  request.message, request.scheduled_datetime)):
        raise HTTPException(status_code=422, detail="Nothing to update — provide at least one field or an action")
    if not update_scheduled_dm(request.dm_id, recipient_profile_url=request.recipient_profile_url,
                               recipient_name=request.recipient_name, message=request.message,
                               scheduled_time=request.scheduled_datetime, status=status):
        raise HTTPException(status_code=500, detail="Could not update scheduled DM")
    return ResponseModel(status_code=200, detail="Scheduled DM updated")


@router.delete("/dm")
def delete_scheduled_dm_endpoint(request: DmDeleteRequest) -> ResponseModel:
    """Cancel a scheduled DM (soft — sets status 'canceled' so it won't be sent)."""
    user_id = get_session_user_id(request.session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    if get_scheduled_dm_user_id(request.dm_id) != user_id:
        raise HTTPException(status_code=404, detail="Scheduled DM not found")
    if not update_scheduled_dm_status(request.dm_id, ScheduledDmStatus.CANCELED):
        raise HTTPException(status_code=500, detail="Could not cancel scheduled DM")
    return ResponseModel(status_code=200, detail="Scheduled DM canceled")


@router.post("/connection_request")
def create_connection_request_endpoint(request: ConnectionRequestCreate) -> ResponseModel:
    """Add a proactive connection-request target (issue #398). If no status is supplied the user's
    connection_request_mode governs it — 'auto_approve' (default) queues the target for the daily-capped
    drip immediately, 'pre_review' holds it as a draft awaiting explicit approval. An explicit status
    ('pending'|'approved') overrides that; anything else is rejected. The drip reuses invite_to_connect
    and honors the rate-limit / kill-switch and the combined daily invite cap. NO volume prospecting."""
    user_id = get_session_user_id(request.session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    if request.status is None:
        mode = get_engagement_preferences(user_id).get("connection_request_mode", "auto_approve")
        status = (ConnectionRequestStatus.APPROVED if mode == "auto_approve"
                  else ConnectionRequestStatus.PENDING)
    elif request.status in ("pending", "approved"):
        status = (ConnectionRequestStatus.APPROVED if request.status == "approved"
                  else ConnectionRequestStatus.PENDING)
    else:
        raise HTTPException(status_code=422,
                            detail=f"Invalid status '{request.status}' — expected 'pending' or 'approved'")
    request_id = insert_connection_request(user_id, request.recipient_profile_url,
                                           message=request.message,
                                           recipient_name=request.recipient_name, status=status)
    if not request_id:
        raise HTTPException(status_code=500, detail="Could not create connection request")
    return ResponseModel(status_code=200, detail={"request_id": request_id})


@router.get("/connection_requests")
def list_connection_requests_endpoint(session_token: str, status_filter: Optional[str] = None,
                                      page: int = 1, page_size: int = 25,
                                      sort_order: str = "desc") -> ResponseModel:
    user_id = get_session_user_id(session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    return ResponseModel(status_code=200,
                         detail=get_connection_requests(user_id, status_filter=status_filter,
                                                        page=page, page_size=page_size,
                                                        sort_order=sort_order))


@router.put("/connection_request")
def update_connection_request_endpoint(request: ConnectionRequestUpdate) -> ResponseModel:
    user_id = get_session_user_id(request.session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    if get_connection_request_user_id(request.request_id) != user_id:
        raise HTTPException(status_code=404, detail="Connection request not found")
    action_map = {"approve": ConnectionRequestStatus.APPROVED, "cancel": ConnectionRequestStatus.CANCELED}
    if request.action is not None and request.action not in action_map:
        raise HTTPException(status_code=422,
                            detail=f"Unknown action '{request.action}' — expected 'approve' or 'cancel'")
    status = action_map.get(request.action)
    if status is None and all(v is None for v in (request.recipient_profile_url,
                                                  request.recipient_name, request.message)):
        raise HTTPException(status_code=422, detail="Nothing to update — provide at least one field or an action")
    if not update_connection_request(request.request_id,
                                     recipient_profile_url=request.recipient_profile_url,
                                     recipient_name=request.recipient_name, message=request.message,
                                     status=status):
        raise HTTPException(status_code=500, detail="Could not update connection request")
    return ResponseModel(status_code=200, detail="Connection request updated")


@router.delete("/connection_request")
def delete_connection_request_endpoint(request: ConnectionRequestDelete) -> ResponseModel:
    """Cancel a connection request (soft — sets status 'canceled' so it won't be sent)."""
    user_id = get_session_user_id(request.session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    if get_connection_request_user_id(request.request_id) != user_id:
        raise HTTPException(status_code=404, detail="Connection request not found")
    if not update_connection_request_status(request.request_id, ConnectionRequestStatus.CANCELED):
        raise HTTPException(status_code=500, detail="Could not cancel connection request")
    return ResponseModel(status_code=200, detail="Connection request canceled")


@router.post("/outreach/target")
def create_outreach_target_endpoint(request: OutreachTargetRequest) -> ResponseModel:
    """Add a prospect to the comment-first outreach funnel (issue #399). The target starts at the
    'comment' stage; every stage is approval-gated — the funnel processor only acts on APPROVED
    stages and re-drops each fired stage to 'pending', so no step auto-fires at volume."""
    user_id = get_session_user_id(request.session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    # Normalize at the boundary so whitespace variants ("…/in/jane" vs "…/in/jane ") can't slip past
    # the duplicate check and the unique constraint as distinct rows.
    target_profile_url = request.target_profile_url.strip()
    target_name = request.target_name.strip() if request.target_name else None
    context_url = request.context_url.strip() if request.context_url else None
    if not target_profile_url:
        raise HTTPException(status_code=422, detail="target_profile_url is required")
    if get_outreach_target_by_url(user_id, target_profile_url):
        raise HTTPException(status_code=409, detail="Target is already in the outreach funnel")
    status = OutreachStatus.APPROVED if request.status == "approved" else OutreachStatus.PENDING
    target_id = insert_outreach_target(user_id, target_profile_url,
                                       target_name=target_name, context_url=context_url,
                                       draft_text=request.draft_text, status=status)
    if not target_id:
        raise HTTPException(status_code=500, detail="Could not create outreach target")
    return ResponseModel(status_code=200, detail={"target_id": target_id})


@router.get("/outreach/targets")
def list_outreach_targets_endpoint(session_token: str, status_filter: Optional[str] = None,
                                   stage_filter: Optional[str] = None, page: int = 1,
                                   page_size: int = 25, sort_order: str = "asc") -> ResponseModel:
    user_id = get_session_user_id(session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    return ResponseModel(status_code=200, detail=get_outreach_targets(
        user_id, status_filter=status_filter, stage_filter=stage_filter, page=page,
        page_size=page_size, sort_order=sort_order))


@router.put("/outreach/target")
def update_outreach_target_endpoint(request: UpdateOutreachTargetRequest) -> ResponseModel:
    """Edit a funnel target's current-stage draft, or approve/cancel it. 'approve' gates the current
    stage for the processor; 'cancel' aborts the whole funnel for this target."""
    user_id = get_session_user_id(request.session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    if get_outreach_target_user_id(request.target_id) != user_id:
        raise HTTPException(status_code=404, detail="Outreach target not found")
    action_map = {"approve": OutreachStatus.APPROVED, "cancel": OutreachStatus.CANCELED}
    if request.action is not None and request.action not in action_map:
        raise HTTPException(status_code=422,
                            detail=f"Unknown action '{request.action}' — expected 'approve' or 'cancel'")
    status = action_map.get(request.action)
    if status is None and all(v is None for v in (request.target_name, request.context_url,
                                                  request.draft_text)):
        raise HTTPException(status_code=422, detail="Nothing to update — provide at least one field or an action")
    if not update_outreach_target(request.target_id, target_name=request.target_name,
                                  context_url=request.context_url, draft_text=request.draft_text,
                                  status=status):
        raise HTTPException(status_code=500, detail="Could not update outreach target")
    return ResponseModel(status_code=200, detail="Outreach target updated")


@router.delete("/outreach/target")
def delete_outreach_target_endpoint(request: OutreachTargetDeleteRequest) -> ResponseModel:
    """Cancel a funnel target (soft — sets status 'canceled' so no further stage fires)."""
    user_id = get_session_user_id(request.session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    if get_outreach_target_user_id(request.target_id) != user_id:
        raise HTTPException(status_code=404, detail="Outreach target not found")
    if not update_outreach_target_status(request.target_id, OutreachStatus.CANCELED):
        raise HTTPException(status_code=500, detail="Could not cancel outreach target")
    return ResponseModel(status_code=200, detail="Outreach target canceled")


# Inbound hot leads (issue #483) — the leads inbox. Signals are detected on read paths that already
# run; the operator approves (or edits then approves) the drafted response before anything is sent.
_LEN_LEAD_DRAFT = 3000  # lead_signals.draft_response (TEXT; app cap)


class LeadSignalUpdate(BaseModel):
    session_token: str
    signal_id: int
    draft_response: Optional[str] = Field(default=None, max_length=_LEN_LEAD_DRAFT)
    action: Optional[str] = None  # 'approve' | 'dismiss' | None (save the draft only)


@router.get("/lead_signals")
def list_lead_signals_endpoint(session_token: str, status_filter: Optional[str] = None,
                               page: int = 1, page_size: int = 25,
                               sort_order: str = "desc") -> ResponseModel:
    """The leads inbox: detected buying signals with their approval-gated draft responses."""
    user_id = get_session_user_id(session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    result = get_lead_signals(user_id, status_filter=status_filter, page=page,
                              page_size=page_size, sort_order=sort_order)
    result["new_count"] = count_new_lead_signals(user_id)
    return ResponseModel(status_code=200, detail=result)


@router.put("/lead_signal")
def update_lead_signal_endpoint(request: LeadSignalUpdate) -> ResponseModel:
    """Edit a lead's draft, dismiss the signal, or APPROVE it — approval is the only thing that
    dispatches a response, and it sends exactly the text the operator sees."""
    user_id = get_session_user_id(request.session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    signal = get_lead_signal(request.signal_id)
    if not signal or signal.get("user_id") != user_id:
        raise HTTPException(status_code=404, detail="Lead signal not found")
    action_map = {"approve": LeadSignalStatus.APPROVED, "dismiss": LeadSignalStatus.DISMISSED}
    if request.action is not None and request.action not in action_map:
        raise HTTPException(status_code=422,
                            detail=f"Unknown action '{request.action}' — expected 'approve' or 'dismiss'")
    status = action_map.get(request.action)
    if status is None and request.draft_response is None:
        raise HTTPException(status_code=422, detail="Nothing to update — provide a draft or an action")
    draft = request.draft_response if request.draft_response is not None else signal.get("draft_response")
    if status == LeadSignalStatus.APPROVED and not (draft or "").strip():
        raise HTTPException(status_code=422, detail="Cannot approve a lead with an empty response")
    if not update_lead_signal(request.signal_id, draft_response=request.draft_response, status=status):
        raise HTTPException(status_code=500, detail="Could not update lead signal")
    if status == LeadSignalStatus.APPROVED:
        send_lead_response.apply_async(kwargs={"signal_id": request.signal_id})
    return ResponseModel(status_code=200, detail="Lead signal updated")


# Lead scoring & CRM-lite pipeline (issue #484) — the scored board over everyone who engages with
# the user. Scores are rebuilt nightly from existing engagement data; these endpoints read it and
# let the operator correct a stage, keep a note, or drop someone from the board.
_LEN_LEAD_NOTES = 512  # leads.notes VARCHAR(512)
_LEAD_STAGES = tuple(str(s) for s in LeadStage)


class LeadUpdate(BaseModel):
    session_token: str
    lead_id: int
    notes: Optional[str] = Field(default=None, max_length=_LEN_LEAD_NOTES)
    stage: Optional[str] = None   # manual stage override; '' clears it back to the computed stage
    action: Optional[str] = None  # 'dismiss' | 'restore' | None


class LeadRefreshRequest(BaseModel):
    session_token: str


@router.get("/leads")
def list_leads_endpoint(session_token: str, stage_filter: Optional[str] = None,
                        include_dismissed: bool = False, page: int = 1,
                        page_size: int = 100) -> ResponseModel:
    """The pipeline board: scored leads hottest first, each with why it scored and what to do next."""
    user_id = get_session_user_id(session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    if stage_filter and stage_filter not in _LEAD_STAGES:
        raise HTTPException(status_code=422, detail=f"Unknown stage '{stage_filter}'")
    result = get_leads(user_id, stage_filter=stage_filter, include_dismissed=include_dismissed,
                       page=page, page_size=page_size)
    result["hot_count"] = count_hot_leads(user_id)
    return ResponseModel(status_code=200, detail=result)


@router.put("/lead")
def update_lead_endpoint(request: LeadUpdate) -> ResponseModel:
    """Operator edits: move a lead's stage by hand, keep a note, or dismiss/restore it. The nightly
    re-score never overwrites any of these."""
    user_id = get_session_user_id(request.session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    lead = get_lead(request.lead_id)
    if not lead or lead.get("user_id") != user_id:
        raise HTTPException(status_code=404, detail="Lead not found")
    if request.action is not None and request.action not in ("dismiss", "restore"):
        raise HTTPException(status_code=422,
                            detail=f"Unknown action '{request.action}' — expected 'dismiss' or 'restore'")
    if request.stage and request.stage not in _LEAD_STAGES:
        raise HTTPException(status_code=422, detail=f"Unknown stage '{request.stage}'")
    if request.notes is None and request.stage is None and request.action is None:
        raise HTTPException(status_code=422, detail="Nothing to update")
    dismissed = {"dismiss": True, "restore": False}.get(request.action)
    if not update_lead(request.lead_id, notes=request.notes, manual_stage=request.stage,
                       dismissed=dismissed):
        raise HTTPException(status_code=500, detail="Could not update lead")
    return ResponseModel(status_code=200, detail="Lead updated")


@router.post("/leads/refresh")
def refresh_leads_endpoint(request: LeadRefreshRequest) -> ResponseModel:
    """Re-score this user's pipeline now instead of waiting for tonight's rebuild."""
    user_id = get_session_user_id(request.session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    from cqc_lem.app.run_scheduler import rebuild_leads_for_user
    rebuild_leads_for_user.apply_async(kwargs={"user_id": user_id})
    return ResponseModel(status_code=200, detail="Re-scoring your leads — refresh in a moment")


@router.get("/catchup/touches")
def list_catchup_touches_endpoint(session_token: str, status_filter: Optional[str] = None,
                                  event_type_filter: Optional[str] = None, page: int = 1,
                                  page_size: int = 25, sort_order: str = "desc") -> ResponseModel:
    """Drafted LinkedIn Catch-up congratulations awaiting review (issue #482), highest-scoring first."""
    user_id = get_session_user_id(session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    return ResponseModel(status_code=200, detail=get_catchup_touches(
        user_id, status_filter=status_filter, event_type_filter=event_type_filter,
        page=page, page_size=page_size, sort_order=sort_order))


@router.put("/catchup/touch")
def update_catchup_touch_endpoint(request: UpdateCatchupTouchRequest) -> ResponseModel:
    """Edit a drafted congratulations, or approve/cancel it. Approving queues it for the daily-capped
    send drip; nothing is sent until a human approves (unless the account opted into auto-approve)."""
    user_id = get_session_user_id(request.session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    touch = get_catchup_touch(request.touch_id)
    if not touch or touch.get("user_id") != user_id:
        raise HTTPException(status_code=404, detail="Catch-up touch not found")
    action_map = {"approve": CatchupTouchStatus.APPROVED, "cancel": CatchupTouchStatus.CANCELED}
    if request.action is not None and request.action not in action_map:
        raise HTTPException(status_code=422,
                            detail=f"Unknown action '{request.action}' — expected 'approve' or 'cancel'")
    status = action_map.get(request.action)
    if status is None and all(v is None for v in (request.message, request.person_name)):
        raise HTTPException(status_code=422, detail="Nothing to update — provide at least one field or an action")
    if request.message is not None and not request.message.strip():
        raise HTTPException(status_code=422, detail="Message cannot be empty")
    # Approving is what queues the send, and send_catchup_touch turns a blank message into a permanent
    # SKIPPED — so an approval must always land on a real message, saved now or already stored.
    if status == CatchupTouchStatus.APPROVED and not (request.message or touch.get("message") or "").strip():
        raise HTTPException(status_code=422, detail="Add a message before approving this catch-up touch")
    if not update_catchup_touch(request.touch_id, message=request.message,
                                person_name=request.person_name, status=status):
        raise HTTPException(status_code=500, detail="Could not update catch-up touch")
    return ResponseModel(status_code=200, detail="Catch-up touch updated")


@router.delete("/catchup/touch")
def delete_catchup_touch_endpoint(request: CatchupTouchDeleteRequest) -> ResponseModel:
    """Cancel a drafted catch-up touch (soft — sets status 'canceled' so it won't be sent, and the
    row stays as the dedup tombstone for that milestone)."""
    user_id = get_session_user_id(request.session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    if get_catchup_touch_user_id(request.touch_id) != user_id:
        raise HTTPException(status_code=404, detail="Catch-up touch not found")
    if not update_catchup_touch_status(request.touch_id, CatchupTouchStatus.CANCELED):
        raise HTTPException(status_code=500, detail="Could not cancel catch-up touch")
    return ResponseModel(status_code=200, detail="Catch-up touch canceled")


class GroupTogglesRequest(BaseModel):
    session_token: str
    groups: dict = {}  # {group_id: enabled}


@router.get("/user/groups")
def get_user_groups_endpoint(session_token: str) -> ResponseModel:
    user_id = get_session_user_id(session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    return ResponseModel(status_code=200, detail=get_user_groups(user_id))


@router.put("/user/groups")
def update_user_groups_endpoint(request: GroupTogglesRequest) -> ResponseModel:
    user_id = get_session_user_id(request.session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    if not set_groups_enabled(user_id, request.groups):
        raise HTTPException(status_code=500, detail="Could not update group settings")
    return ResponseModel(status_code=200, detail="Group settings updated")


@router.get("/user/post-stats")
def get_post_stats_endpoint(session_token: str) -> ResponseModel:
    """Personalized best-times-to-post recommendations plus a which-hooks/formats/topics-win
    ranking, both derived from the user's own post stats."""
    from cqc_lem.utilities.post_stats import rank_content_attributes, recommend_post_times
    user_id = get_session_user_id(session_token)
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


@router.get("/user/engagement-analytics")
def get_engagement_analytics_endpoint(session_token: str, days: int = 90) -> ResponseModel:
    """Per-post performance table + a daily engagement-rate / impression trend for the analytics
    dashboard (issue #395), derived from the user's captured post_stats, plus the 70/20/10
    content-mix compliance ratio for the same window (issue #618), the comment-outcome quality
    score (issue #628) and the content-quality rollup (issue #630). The hook/format leaderboard is
    served by /user/post-stats (rankings)."""
    from cqc_lem.utilities.ai.content_alignment import content_mix_compliance
    from cqc_lem.utilities.comment_outcomes import comment_quality_report
    from cqc_lem.utilities.content_quality import quality_rollup, rollup_days
    from cqc_lem.utilities.db import get_content_quality_scores
    from cqc_lem.utilities.linkedin.rate_limit import commenting_hold_reason, commenting_hold_remaining
    from cqc_lem.utilities.post_stats import build_engagement_trend, build_performance_table
    user_id = get_session_user_id(session_token)
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
    return ResponseModel(status_code=200, detail={
        "per_post": build_performance_table(rows),
        "trend": build_engagement_trend(rows),
        "sample_size": len(rows),
        "days": days,
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


@router.get("/user/posthog-stats")
def get_posthog_stats_endpoint(session_token: str) -> ResponseModel:
    """The in-SPA 'your stats' panel (issue #654), backed by PostHog HogQL Endpoints
    (scripts/posthog_provision.py) instead of a bespoke MySQL reporting layer. A thin server-side
    proxy: the personal API key lives here and never reaches the browser, and every read is scoped
    to THIS user's own distinct_id — PostHog is one project shared by every LEM account. Degrades
    per-panel (`available: false`) rather than failing the whole response when a key is unset, an
    endpoint isn't provisioned yet, or PostHog is unreachable."""
    from cqc_lem.utilities.posthog_endpoints import get_user_stats_panel
    user_id = get_session_user_id(session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    return ResponseModel(status_code=200, detail=get_user_stats_panel(user_id))


def _suppression_status(user_id: int) -> dict:
    """Current suppression-tripwire picture for one user (issue #629): the standing trip (if any)
    plus a FRESH evaluation of the same signals. Both are returned on purpose — the trip is what
    paused engagement and never self-clears, while the live verdict is how the user can see their
    reach has recovered and decide to re-enable."""
    from cqc_lem.utilities.comment_outcomes import comment_quality_report
    from cqc_lem.utilities.linkedin.rate_limit import (
        automation_pause_reason, automation_pause_remaining, is_suppression_pause,
        rate_limit_cooldown_remaining, suppression_trip_state)
    from cqc_lem.utilities.post_stats import build_engagement_trend
    from cqc_lem.utilities.suppression import (comment_history_days, evaluate_suppression,
                                               history_days, tripwire_enabled)

    window = history_days()
    comment_window = comment_history_days()
    trend = build_engagement_trend(get_post_performance_rows(user_id, days=window))
    quality = comment_quality_report(get_comment_outcomes(user_id, days=comment_window),
                                     days=comment_window)
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


@router.get("/user/automation-status")
def get_automation_status_endpoint(session_token: str) -> ResponseModel:
    """Suppression-tripwire + automation-pause state for the Account banner (issue #629)."""
    user_id = get_session_user_id(session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    return ResponseModel(status_code=200, detail=_suppression_status(user_id))


class AutomationResumeRequest(BaseModel):
    session_token: str


@router.post("/user/automation-resume")
def resume_automation_endpoint(request: AutomationResumeRequest) -> ResponseModel:
    """The manual re-enable path for a suppression trip (issue #629). The tripwire NEVER resumes on
    its own, so this endpoint is the only way back: it clears the stored trip and lifts the pause —
    but only when the pause is the tripwire's own trip for THIS user, so re-enabling here can never
    stomp a 429 cooldown, a maintenance window, an admin kill-switch — or another user's standing
    trip, since `pause_automation` is one global breaker shared by the whole fleet."""
    from cqc_lem.utilities.linkedin.rate_limit import (
        automation_pause_reason, automation_pause_remaining, clear_suppression_trip,
        resume_automation, suppression_pause_reason)
    user_id = get_session_user_id(request.session_token)
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


@router.get("/user/audience-growth")
def get_audience_growth_endpoint(session_token: str, days: int = 90) -> ResponseModel:
    """Follower/audience telemetry for the analytics dashboard's growth panel (issue #627): the
    daily follower series with 7/30-day deltas, the latest profile-view and search-appearance
    readings, and the user's daily posting/commenting activity to overlay on the same window.
    Audience growth is the system's primary outcome — post engagement is the leading indicator."""
    from cqc_lem.utilities.audience_stats import (GROWTH_WINDOWS, build_activity_series,
                                                  follower_growth)
    user_id = get_session_user_id(session_token)
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
    session_token: str
    enabled: bool = False
    keyword: Optional[str] = Field(default=None, max_length=_LEN_LM_KEYWORD)
    message: Optional[str] = Field(default=None, max_length=_LEN_LM_MESSAGE)


@router.get("/user/lead-magnet")
def get_lead_magnet_endpoint(session_token: str) -> ResponseModel:
    user_id = get_session_user_id(session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    return ResponseModel(status_code=200, detail=get_lead_magnet_settings(user_id))


@router.put("/user/lead-magnet")
def update_lead_magnet_endpoint(request: LeadMagnetRequest) -> ResponseModel:
    user_id = get_session_user_id(request.session_token)
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


@router.get("/user/dm-templates")
def get_dm_templates_endpoint(session_token: str) -> ResponseModel:
    user_id = get_session_user_id(session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    return ResponseModel(status_code=200, detail=get_dm_templates(user_id))


@router.put("/user/dm-templates")
def update_dm_templates_endpoint(request: DmTemplatesRequest) -> ResponseModel:
    user_id = get_session_user_id(request.session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    if not upsert_dm_templates(user_id, [t.model_dump() for t in request.templates]):
        raise HTTPException(status_code=500, detail="Could not update DM templates")
    return ResponseModel(status_code=200, detail="DM templates updated")


@router.get("/user/engagement-targets")
def get_engagement_targets_endpoint(session_token: str) -> ResponseModel:
    """The user's engagement roster plus seed suggestions for an empty one (issue #616)."""
    user_id = get_session_user_id(session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    return ResponseModel(status_code=200, detail={
        "targets": get_engagement_targets(user_id),
        "suggestions": suggest_engagement_targets(user_id),
    })


@router.put("/user/engagement-targets")
def update_engagement_targets_endpoint(request: EngagementTargetsRequest) -> ResponseModel:
    user_id = get_session_user_id(request.session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    if not upsert_engagement_targets(user_id, [t.model_dump() for t in request.targets]):
        raise HTTPException(status_code=500, detail="Could not update engagement roster")
    return ResponseModel(status_code=200, detail="Engagement roster updated")


@router.delete("/user/engagement-targets")
def delete_engagement_target_endpoint(request: EngagementTargetDeleteRequest) -> ResponseModel:
    user_id = get_session_user_id(request.session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    if not delete_engagement_target(user_id, request.profile_url):
        raise HTTPException(status_code=500, detail="Could not remove roster target")
    return ResponseModel(status_code=200, detail="Roster target removed")


@router.get("/user/story-bank")
def get_story_bank_endpoint(session_token: str) -> ResponseModel:
    """The user's story bank plus how many entries a usable bank needs (issue #620)."""
    user_id = get_session_user_id(session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    entries = get_story_bank_entries(user_id)
    return ResponseModel(status_code=200, detail={
        "entries": entries,
        "kinds": list(STORY_BANK_KINDS),
        "target_entries": STORY_BANK_TARGET_ENTRIES,
    })


@router.put("/user/story-bank")
def update_story_bank_endpoint(request: StoryBankRequest) -> ResponseModel:
    user_id = get_session_user_id(request.session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    if not upsert_story_bank_entries(user_id, [e.model_dump() for e in request.entries]):
        raise HTTPException(status_code=500, detail="Could not update story bank")
    return ResponseModel(status_code=200, detail="Story bank updated")


@router.delete("/user/story-bank")
def delete_story_bank_endpoint(request: StoryBankDeleteRequest) -> ResponseModel:
    user_id = get_session_user_id(request.session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    if not delete_story_bank_entry(user_id, request.entry_id):
        raise HTTPException(status_code=500, detail="Could not remove story bank entry")
    return ResponseModel(status_code=200, detail="Story bank entry removed")


@router.put("/user/linkedin-password")
def update_linkedin_password(request: LinkedInPasswordRequest) -> ResponseModel:
    """Store the user's LinkedIn password for Selenium-driven automation tasks.
    The value is stored as-is because Selenium must type it into the browser.
    It is never returned in any response payload.
    """
    user_id = get_session_user_id(request.session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    if not request.linkedin_password:
        raise HTTPException(status_code=400, detail="Password cannot be empty")
    saved = update_user_linkedin_password(user_id, request.linkedin_password)
    if not saved:
        raise HTTPException(status_code=500, detail="Could not save LinkedIn password")
    return ResponseModel(status_code=200, detail="LinkedIn password saved")


def _scraped_profile_name(user_id: int) -> Optional[str]:
    """The full name on the profile LEM last scraped for this user, at any age — used ONLY to
    pre-fill/suggest the display-name field. Never a silent substitute for the saved value: the
    reply comparison must run on what the user confirmed, not on a scrape that may be a placeholder."""
    try:
        from cqc_lem.utilities.db import get_linked_in_profile_by_user_id
        raw = get_linked_in_profile_by_user_id(user_id, updated_less_than_days_ago=3650)
        if not raw:
            return None
        data = json.loads(raw[0] if isinstance(raw, (tuple, list)) else raw)
        return ((data or {}).get("full_name") or "").strip() or None
    except Exception:
        return None


@router.get("/user/linkedin-display-name")
def get_linkedin_display_name_endpoint(session_token: str) -> ResponseModel:
    """The user's LinkedIn display name (issue #731) plus the name LEM scraped from their profile.

    Reply detection compares the last sender in a DM thread against this exact string, so the UI
    shows the scraped name as a suggestion and the user confirms what LinkedIn actually renders."""
    user_id = get_session_user_id(session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    return ResponseModel(status_code=200, detail={
        "linkedin_display_name": get_user_linkedin_display_name(user_id),
        "profile_full_name": _scraped_profile_name(user_id),
    })


@router.put("/user/linkedin-display-name")
def update_linkedin_display_name_endpoint(request: LinkedInDisplayNameRequest) -> ResponseModel:
    """Save the user's LinkedIn display name. Required, and rejected empty: without it every DM
    reply check is UNKNOWN and the follow-up sequencer skips the person entirely (issue #731)."""
    user_id = get_session_user_id(request.session_token)
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


@router.get("/user/timezone")
def get_user_timezone_endpoint(session_token: str) -> ResponseModel:
    user_id = get_session_user_id(session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    return ResponseModel(status_code=200, detail={"timezone": get_user_timezone(user_id)})


@router.get("/user/linkedin-profile")
def get_user_linkedin_profile_endpoint(session_token: str) -> ResponseModel:
    user_id = get_session_user_id(session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    return ResponseModel(status_code=200, detail={
        "linkedin_profile_url": get_linkedin_profile_url_by_user_id(user_id),
    })


@router.put("/user/timezone")
def update_user_timezone_endpoint(request: TimezoneRequest) -> ResponseModel:
    from zoneinfo import available_timezones, ZoneInfoNotFoundError
    user_id = get_session_user_id(request.session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    if request.timezone not in available_timezones():
        raise HTTPException(status_code=422, detail=f"Unknown timezone: {request.timezone!r}")
    saved = update_user_timezone(user_id, request.timezone)
    if not saved:
        raise HTTPException(status_code=500, detail="Could not update timezone")
    return ResponseModel(status_code=200, detail="Timezone updated")


@router.get("/user/location")
def get_user_location_endpoint(session_token: str) -> ResponseModel:
    user_id = get_session_user_id(session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    return ResponseModel(status_code=200, detail=get_user_geo(user_id) or {})


@router.put("/user/location")
def update_user_location_endpoint(request: LocationRequest) -> ResponseModel:
    user_id = get_session_user_id(request.session_token)
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


@router.post("/user/location/autocapture")
def autocapture_user_location_endpoint(request: LocationAutocaptureRequest, http_request: Request) -> ResponseModel:
    """Geolocate the caller's real IP and persist it as their login location.
    The app sits behind a Cloudflare tunnel, so the client IP arrives in
    CF-Connecting-IP / X-Forwarded-For — never trust the immediate peer."""
    user_id = get_session_user_id(request.session_token)
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


@router.post("/user/location/by-city")
def set_user_location_by_city_endpoint(request: LocationByCityRequest) -> ResponseModel:
    """Geocode a user-selected city/state (free OSM Nominatim) and persist it as their login
    location, so the automation browser's emulated geo/timezone matches where they intend to
    appear. Complementary to /autocapture (IP-based)."""
    user_id = get_session_user_id(request.session_token)
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


@router.post("/user/linkedin-cookie")
def store_linkedin_cookie_endpoint(request: LinkedInCookieRequest) -> ResponseModel:
    """Store the user's existing LinkedIn session cookie (li_at) so automation resumes
    an already-trusted session instead of doing a fresh password login — which is what
    triggers LinkedIn's "Check your app" new-device challenge. The user captures li_at
    once (one-click extension or paste); see docs/LINKEDIN_COOKIE.md."""
    user_id = get_session_user_id(request.session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")

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
    return ResponseModel(
        status_code=200,
        detail="LinkedIn session saved. Automation will reuse it and skip the password login.",
    )


_EXTENSION_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "browser_extension"))


@router.get("/extension/linkedin-connect.zip")
def download_linkedin_extension() -> StreamingResponse:
    """Package the 'LEM LinkedIn Connect' browser extension as a zip the user can load
    unpacked in Chrome/Edge (chrome://extensions → Developer mode → Load unpacked). This is
    the one-click session-reconnect path referenced by the account page and reconnect email;
    until it's on the Chrome Web Store, users side-load this bundle. See docs/LINKEDIN_COOKIE.md."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, _dirs, files in os.walk(_EXTENSION_DIR):
            for name in sorted(files):
                fp = os.path.join(root, name)
                zf.write(fp, arcname=os.path.relpath(fp, _EXTENSION_DIR))
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="lem-linkedin-connect.zip"'},
    )


@router.get("/user/account-readiness")
def account_readiness_endpoint(session_token: str) -> ResponseModel:
    """Report whether the account has everything the automation needs (LinkedIn OAuth for
    posting, a session cookie or password for engagement, an active plan; location is
    recommended). The UI uses this to mark required fields and gate automation pages."""
    user_id = get_session_user_id(session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")

    token_info = get_user_token_info(user_id)
    has_oauth = bool(token_info and token_info.get("access_token"))

    has_session_cookie = has_linkedin_session(user_id)
    _, li_password = get_user_password_pair_by_id(user_id)
    has_engagement_login = has_session_cookie or bool(li_password)

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
         "hint": "Connect your LinkedIn session (cookie) or save your LinkedIn password."},
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
    return ResponseModel(status_code=200, detail={"ready": ready, "items": items})


@router.get("/user/onboarding")
def onboarding_endpoint(session_token: str) -> ResponseModel:
    """The activation checklist (issue #500): each step, when it completed, and the next-best nudge
    to show in-app. Reading it also advances the persisted state, so the PostHog activation funnel
    records a step the moment the user finishes it — not a day later when the beat task runs."""
    user_id = get_session_user_id(session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")

    from cqc_lem.utilities.onboarding import onboarding_snapshot
    return ResponseModel(status_code=200, detail=onboarding_snapshot(user_id))


@router.put("/user/company-page")
def update_company_page_endpoint(request: LinkedInCompanyPageRequest) -> ResponseModel:
    """Save (or clear) the user's LinkedIn company page URL. The monthly invite
    automation (1st of each month) sends connection invites to this page for active
    users; users without one are skipped."""
    user_id = get_session_user_id(request.session_token)
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


@router.post("/trial/extend", responses={
    200: {"description": "Extension result (granted or the reason it wasn't)"},
    **{k: v for k, v in error_responses.items() if k in [401, 404]},
})
def trial_extend_endpoint(request: TrialExtendRequest) -> ResponseModel:
    """Claim the early-adopter extended trial (issue #499): EARLY_ADOPTER_TRIAL_DAYS instead of the
    standard FREE_TRIAL_DAYS, in exchange for a public review.

    Not-granted outcomes are 200s with a `reason`, not errors — the SPA renders them as a prompt
    ("submit a quick review to unlock N days"), and an exhausted cohort is a normal state, not a
    failure: the user simply keeps their standard trial.
    """
    from cqc_lem.utilities.env_constants import (
        EARLY_ADOPTER_TRIAL_ENABLED, EARLY_ADOPTER_TRIAL_DAYS, FREE_TRIAL_DAYS,
    )
    if not EARLY_ADOPTER_TRIAL_ENABLED:
        raise HTTPException(status_code=404, detail="Early-adopter extended trial is not available")

    user_id = get_session_user_id(request.session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")

    review_id = get_latest_review_feedback_id(user_id)
    if not review_id:
        return ResponseModel(status_code=200, detail={
            "granted": False,
            "reason": "review_required",
            "trial_days": FREE_TRIAL_DAYS,
            "message": f"Submit a quick review to unlock {EARLY_ADOPTER_TRIAL_DAYS} days.",
        })

    result = extend_trial_for_user(user_id, feedback_id=review_id)
    trial_ends_at = result.get("trial_ends_at")
    return ResponseModel(status_code=200, detail={
        "granted": result.get("granted", False),
        "reason": result.get("reason"),
        "cohort": result.get("cohort"),
        "trial_days": result.get("trial_days", FREE_TRIAL_DAYS),
        "trial_ends_at": trial_ends_at.isoformat() if trial_ends_at else None,
    })


def _early_adopter_checkout_extras(user_id: int) -> tuple[Optional[int], Optional[List[dict]]]:
    """Mirror an unfinished early-adopter trial into Stripe on conversion (issue #499): the days
    still left on the grant become the Checkout trial, and the optional launch coupon rides along.
    Only grant holders are affected — a standard trial converts exactly as it does today.

    Best-effort by design: this is a perk lookup, so any failure degrades to a normal checkout
    rather than blocking the user from paying us."""
    from cqc_lem.utilities.env_constants import EARLY_ADOPTER_COUPON_ID
    try:
        grant = get_early_adopter_grant(user_id)
    except Exception as e:
        log_warning("Could not read early-adopter grant for checkout", exc=e, user_id=user_id)
        return None, None
    if not grant:
        return None, None
    ends_at = grant.get("trial_ends_at")
    trial_period_days = None
    if ends_at:
        if ends_at.tzinfo is None:
            ends_at = ends_at.replace(tzinfo=timezone.utc)
        remaining = math.ceil((ends_at - datetime.now(timezone.utc)).total_seconds() / 86400)
        if remaining >= 1:
            trial_period_days = int(remaining)
    # The coupon rides with the unfinished trial, so an expired/exhausted grant carries neither —
    # otherwise a long-lapsed grant would keep discounting every future checkout.
    if trial_period_days is None:
        return None, None
    discounts = [{"coupon": EARLY_ADOPTER_COUPON_ID}] if EARLY_ADOPTER_COUPON_ID else None
    return trial_period_days, discounts


@router.post("/billing/create-checkout-session")
def billing_create_checkout_session(request: CheckoutSessionRequest) -> ResponseModel:
    user_id = get_session_user_id(request.session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")

    subscription = get_user_subscription_info(user_id)
    stripe_customer_id = subscription.get("stripe_customer_id") if subscription else None
    if not stripe_customer_id:
        raise HTTPException(status_code=400, detail="No Stripe customer record — contact support")

    from cqc_lem.utilities.stripe_util import create_checkout_session, upgrade_subscription

    # If the user already has an active subscription, modify it in-place rather than
    # creating a new Checkout session — which would register a second subscription.
    existing_sub_id = subscription.get("stripe_subscription_id") if subscription else None
    existing_status = subscription.get("subscription_status") if subscription else None
    if existing_sub_id and existing_status in ("active", "trial"):
        upgraded = upgrade_subscription(existing_sub_id, request.tier)
        if upgraded:
            # No redirect needed — Stripe webhook will fire subscription.updated and sync DB
            return ResponseModel(status_code=200, detail={"checkout_url": None, "upgraded": True})
        myprint(
            f"In-place upgrade failed for sub={existing_sub_id}; falling back to checkout session"
        )

    trial_period_days, discounts = _early_adopter_checkout_extras(user_id)
    url = create_checkout_session(
        stripe_customer_id,
        request.tier,
        request.success_url,
        request.cancel_url,
        trial_period_days=trial_period_days,
        discounts=discounts,
    )
    if not url:
        raise HTTPException(status_code=500, detail="Could not create Stripe checkout session")
    return ResponseModel(status_code=200, detail={"checkout_url": url, "upgraded": False})


@router.post("/billing/create-portal-session")
def billing_create_portal_session(request: PortalSessionRequest) -> ResponseModel:
    user_id = get_session_user_id(request.session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")

    subscription = get_user_subscription_info(user_id)
    stripe_customer_id = subscription.get("stripe_customer_id") if subscription else None
    if not stripe_customer_id:
        raise HTTPException(status_code=400, detail="No Stripe customer record — contact support")

    from cqc_lem.utilities.stripe_util import create_portal_session
    url = create_portal_session(stripe_customer_id, request.return_url)
    if not url:
        raise HTTPException(status_code=500, detail="Could not create Stripe portal session")
    return ResponseModel(status_code=200, detail={"portal_url": url})


def _track_billing_funnel(event: str, stripe_customer_id: str, **props) -> None:
    """Funnel event for a Stripe lifecycle webhook. The webhook carries no UTMs — PostHog holds them
    on the person from the `$set_once` written at signup — so only the plan facts ride along here.
    The customer→user lookup is guarded: analytics must never fail a billing webhook, because Stripe
    would retry it and the subscription state is already committed."""
    try:
        user = get_user_by_stripe_customer_id(stripe_customer_id) or {}
        track_funnel_event(event, user_id=user.get("id"),
                           distinct_id=f"stripe_{stripe_customer_id}",
                           stripe_customer_id=stripe_customer_id, **props)
    except Exception as e:
        log_warning(f"Could not track billing funnel event '{event}'", exc=e)


@router.post("/billing/webhook")
async def billing_webhook(request: Request) -> dict:
    payload = await request.body()
    sig_header = request.headers.get("Stripe-Signature", "")

    from cqc_lem.utilities.stripe_util import (
        validate_webhook, get_subscription_tier_from_price, stripe_status_to_db,
    )
    event = validate_webhook(payload, sig_header)
    if event is None:
        raise HTTPException(status_code=400, detail="Invalid Stripe webhook signature")

    event_type = event.get("type", "")
    data = event.get("data", {}).get("object", {})
    myprint(f"Stripe webhook received: {event_type}")

    # --- Subscription lifecycle events ---
    if event_type in (
        "customer.subscription.created",
        "customer.subscription.updated",
    ):
        stripe_customer_id = data.get("customer")
        stripe_subscription_id = data.get("id")
        if not stripe_customer_id:
            myprint(f"Webhook {event_type} missing customer field — skipping")
            return {"received": True}
        sub_status = data.get("status", "")
        db_status = stripe_status_to_db(sub_status)

        # Determine tier from the first line item's price
        price_id = None
        items = data.get("items", {}).get("data", [])
        if items:
            price_id = items[0].get("price", {}).get("id")
        tier = get_subscription_tier_from_price(price_id) if price_id else None

        # Period end (Unix timestamp → datetime)
        period_end_ts = data.get("current_period_end")
        period_end = (
            datetime.fromtimestamp(period_end_ts, tz=timezone.utc) if period_end_ts else None
        )

        myprint(
            f"Subscription {stripe_subscription_id}: stripe_status={sub_status} "
            f"→ db_status={db_status}, tier={tier}, period_end={period_end}"
        )
        update_subscription_from_stripe(
            stripe_customer_id, db_status, tier, stripe_subscription_id, period_end
        )
        # Only `created` — `updated` fires on every plan/status change and would double-count.
        if event_type == "customer.subscription.created":
            _track_billing_funnel(
                FUNNEL_SUBSCRIPTION_STARTED, stripe_customer_id, tier=tier, status=db_status,
                stripe_subscription_id=stripe_subscription_id,
            )

    elif event_type == "customer.subscription.deleted":
        stripe_customer_id = data.get("customer")
        stripe_subscription_id = data.get("id")
        if not stripe_customer_id:
            myprint(f"Webhook {event_type} missing customer field — skipping")
            return {"received": True}
        myprint(f"Subscription {stripe_subscription_id} deleted for customer {stripe_customer_id}")
        # tier=None preserves the historical tier in the DB
        update_subscription_from_stripe(
            stripe_customer_id, "cancelled", None, stripe_subscription_id
        )
        _track_billing_funnel(FUNNEL_CHURNED, stripe_customer_id, reason="subscription_deleted",
                              stripe_subscription_id=stripe_subscription_id)

    # --- Invoice / payment events (fired on every billing cycle renewal) ---
    elif event_type == "invoice.payment_succeeded":
        stripe_customer_id = data.get("customer")
        stripe_subscription_id = data.get("subscription")
        if not stripe_customer_id:
            myprint(f"Webhook {event_type} missing customer field — skipping")
            return {"received": True}
        if stripe_subscription_id:
            myprint(
                f"Invoice payment succeeded for customer={stripe_customer_id}, "
                f"subscription={stripe_subscription_id} — marking active"
            )
            # Re-fetch the subscription to get the current tier and period end
            from cqc_lem.utilities.stripe_util import fetch_subscription
            sub = fetch_subscription(stripe_subscription_id)
            if sub:
                price_id = None
                items = sub.get("items", {}).get("data", [])
                if items:
                    price_id = items[0].get("price", {}).get("id")
                tier = get_subscription_tier_from_price(price_id) if price_id else None
                period_end_ts = sub.get("current_period_end")
                period_end = datetime.fromtimestamp(period_end_ts, tz=timezone.utc) if period_end_ts else None
                update_subscription_from_stripe(
                    stripe_customer_id, "active", tier, stripe_subscription_id, period_end
                )

    elif event_type == "invoice.payment_failed":
        stripe_customer_id = data.get("customer")
        stripe_subscription_id = data.get("subscription")
        if not stripe_customer_id:
            myprint(f"Webhook {event_type} missing customer field — skipping")
            return {"received": True}
        if stripe_subscription_id:
            myprint(
                f"Invoice payment FAILED for customer={stripe_customer_id}, "
                f"subscription={stripe_subscription_id} — marking past_due"
            )
            # tier=None preserves existing tier; status → past_due
            update_subscription_from_stripe(
                stripe_customer_id, "past_due", None, stripe_subscription_id
            )

    elif event_type == "checkout.session.completed":
        meta = data.get("metadata", {})
        if meta.get("type") == "avatar_credits":
            # Only credit on confirmed card payment — async methods (e.g. bank transfer)
            # may fire this event before funds clear.
            if data.get("payment_status") != "paid":
                myprint(
                    f"checkout.session.completed: payment_status={data.get('payment_status')} "
                    f"— not yet paid, skipping credit grant"
                )
                return {"received": True}

            stripe_customer_id = data.get("customer")
            stripe_session_id = data.get("id")
            credits = int(meta.get("credits", 0))
            package = meta.get("package", "unknown")

            if not stripe_customer_id or credits <= 0:
                myprint(f"Avatar credits webhook: missing customer or zero credits — skipping")
                return {"received": True}

            # Idempotency: Stripe may retry — skip if credits already granted for this session.
            if get_avatar_credit_ledger_entry_by_session(stripe_session_id):
                myprint(f"Avatar credits already granted for session={stripe_session_id} — skipping duplicate")
                return {"received": True}

            user_row = get_user_by_stripe_customer_id(stripe_customer_id)
            if user_row:
                add_avatar_credits(
                    user_row["id"],
                    credits,
                    f"purchase_{package}",
                    stripe_session_id,
                )
                myprint(
                    f"Added {credits} avatar credit(s) for user_id={user_row['id']} "
                    f"via session={stripe_session_id}"
                )
            else:
                myprint(f"Avatar credits webhook: no user found for customer={stripe_customer_id}")

        elif meta.get("type") == "video_credits":
            if data.get("payment_status") != "paid":
                myprint(f"video_credits checkout: payment_status={data.get('payment_status')} — skipping")
                return {"received": True}
            stripe_customer_id = data.get("customer")
            stripe_session_id = data.get("id")
            credits = int(meta.get("credits", 0))
            package = meta.get("package", "unknown")
            if not stripe_customer_id or credits <= 0:
                myprint("Video credits webhook: missing customer or zero credits — skipping")
                return {"received": True}
            if get_video_credit_ledger_entry_by_session(stripe_session_id):
                myprint(f"Video credits already granted for session={stripe_session_id} — skipping duplicate")
                return {"received": True}
            user_row = get_user_by_stripe_customer_id(stripe_customer_id)
            if user_row:
                add_video_credits(user_row["id"], credits, f"purchase_{package}", stripe_session_id)
                myprint(f"Added {credits} video credit(s) for user_id={user_row['id']} via session={stripe_session_id}")
            else:
                myprint(f"Video credits webhook: no user found for customer={stripe_customer_id}")

    elif event_type == "charge.refunded":
        payment_intent_id = data.get("payment_intent")
        stripe_customer_id = data.get("customer")
        amount = data.get("amount", 0)
        amount_refunded = data.get("amount_refunded", 0)

        if not payment_intent_id or not stripe_customer_id:
            myprint("charge.refunded: missing payment_intent or customer — skipping")
            return {"received": True}

        # Only deduct credits for a full refund — partial refunds don't map cleanly to credits.
        if amount_refunded < amount:
            myprint(
                f"charge.refunded: partial refund ({amount_refunded}/{amount} cents) "
                f"for customer={stripe_customer_id} — no credit adjustment"
            )
            return {"received": True}

        # Find the checkout session that generated this charge to check its metadata.
        from cqc_lem.utilities.stripe_util import get_checkout_session_by_payment_intent
        session = get_checkout_session_by_payment_intent(payment_intent_id)
        if not session:
            myprint(f"charge.refunded: no checkout session found for payment_intent={payment_intent_id}")
            return {"received": True}

        session_meta = session.get("metadata", {})
        credit_type = session_meta.get("type")
        if credit_type not in ("avatar_credits", "video_credits"):
            myprint(f"charge.refunded: not a credits charge — ignoring")
            return {"received": True}

        # Route to the right ledger based on what was purchased.
        if credit_type == "avatar_credits":
            entry_fn, add_fn, label = get_avatar_credit_ledger_entry_by_session, add_avatar_credits, "avatar"
        else:
            entry_fn, add_fn, label = get_video_credit_ledger_entry_by_session, add_video_credits, "video"

        stripe_session_id = session.get("id")
        original_entry = entry_fn(stripe_session_id)
        if not original_entry:
            myprint(f"charge.refunded: no {label} credit ledger entry for session={stripe_session_id} — nothing to deduct")
            return {"received": True}

        user_row = get_user_by_stripe_customer_id(stripe_customer_id)
        if not user_row:
            myprint(f"charge.refunded: no user found for customer={stripe_customer_id}")
            return {"received": True}

        credits_to_deduct = original_entry["delta"]
        add_fn(user_row["id"], -credits_to_deduct, f"refund_{stripe_session_id}", stripe_session_id=None)
        myprint(
            f"Deducted {credits_to_deduct} {label} credit(s) for user_id={user_row['id']} "
            f"due to full refund of session={stripe_session_id}"
        )

    else:
        myprint(f"Stripe webhook event ignored: {event_type}")

    return {"received": True}


# ---------------------------------------------------------------------------
# Avatar endpoints
# ---------------------------------------------------------------------------

@router.get("/avatar/credits", responses={
    200: {"description": "Credit balance and active avatar returned"},
    **{k: v for k, v in error_responses.items() if k in [401]}
})
def get_avatar_credits_endpoint(session_token: str) -> ResponseModel:
    user_id = get_session_user_id(session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    balance = get_avatar_credit_balance(user_id)
    active = get_active_avatar(user_id)
    return ResponseModel(status_code=200, detail={"balance": balance, "active_avatar": active})


@router.post("/avatar/credits/checkout", responses={
    200: {"description": "Stripe checkout URL returned"},
    **{k: v for k, v in error_responses.items() if k in [400, 401]}
})
def avatar_credits_checkout(request: AvatarCreditCheckoutRequest) -> ResponseModel:
    user_id = get_session_user_id(request.session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")

    subscription = get_user_subscription_info(user_id)
    stripe_customer_id = subscription.get("stripe_customer_id") if subscription else None
    if not stripe_customer_id:
        raise HTTPException(status_code=400, detail="No Stripe customer record — contact support")

    from cqc_lem.utilities.stripe_util import create_avatar_credits_checkout, AVATAR_CREDIT_PACKAGES
    if request.package not in AVATAR_CREDIT_PACKAGES:
        raise HTTPException(status_code=400, detail=f"Unknown package '{request.package}'")

    url = create_avatar_credits_checkout(
        stripe_customer_id, request.package, request.success_url, request.cancel_url
    )
    if not url:
        raise HTTPException(status_code=500, detail="Could not create Stripe checkout session")
    return ResponseModel(status_code=200, detail={"checkout_url": url})


@router.get("/video/credits", responses={
    200: {"description": "Video credit balance returned"},
    **{k: v for k, v in error_responses.items() if k in [401]}
})
def get_video_credits_endpoint(session_token: str) -> ResponseModel:
    user_id = get_session_user_id(session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    return ResponseModel(status_code=200, detail={"balance": get_video_credit_balance(user_id)})


@router.post("/video/credits/checkout", responses={
    200: {"description": "Stripe checkout URL returned"},
    **{k: v for k, v in error_responses.items() if k in [400, 401]}
})
def video_credits_checkout(request: VideoCreditCheckoutRequest) -> ResponseModel:
    user_id = get_session_user_id(request.session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    subscription = get_user_subscription_info(user_id)
    stripe_customer_id = subscription.get("stripe_customer_id") if subscription else None
    if not stripe_customer_id:
        raise HTTPException(status_code=400, detail="No Stripe customer record — contact support")
    from cqc_lem.utilities.stripe_util import create_video_credits_checkout, VIDEO_CREDIT_PACKAGES
    if request.package not in VIDEO_CREDIT_PACKAGES:
        raise HTTPException(status_code=400, detail=f"Unknown package '{request.package}'")
    url = create_video_credits_checkout(
        stripe_customer_id, request.package, request.success_url, request.cancel_url)
    if not url:
        raise HTTPException(status_code=500, detail="Could not create Stripe checkout session")
    return ResponseModel(status_code=200, detail={"checkout_url": url})


@router.post("/video/upgrade", responses={
    200: {"description": "Premium video regeneration queued"},
    **{k: v for k, v in error_responses.items() if k in [400, 401, 403, 404]},
    402: {"description": "Insufficient video credits"},
})
def upgrade_video(request: UpgradeVideoRequest) -> ResponseModel:
    """Upgrade a video post to a premium tier — regenerates the video at premium
    quality (Veo + audio), charging credits at render time (refunded on failure)."""
    user_id = get_session_user_id(request.session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    if get_post_user_id(request.post_id) != user_id:
        raise HTTPException(status_code=403, detail="Not your post")
    if get_post_type(request.post_id) != PostType.VIDEO:
        raise HTTPException(status_code=404, detail="Post not found or not a video post")

    from cqc_lem.utilities.env_constants import PREMIUM_VIDEO_CREDITS, PREMIUM_TOP_VIDEO_CREDITS
    tier_credits = {"premium": PREMIUM_VIDEO_CREDITS, "premium_top": PREMIUM_TOP_VIDEO_CREDITS}
    if request.tier not in tier_credits:
        raise HTTPException(status_code=400, detail=f"Unknown tier '{request.tier}'")
    needed = tier_credits[request.tier]
    if get_video_credit_balance(user_id) < needed:
        raise HTTPException(status_code=402,
                            detail=f"Insufficient video credits (need {needed}). Buy credits to use premium video.")

    update_post_video_quality(request.post_id, request.tier)
    from cqc_lem.app.run_content_plan import regenerate_post_video_task
    regenerate_post_video_task.apply_async(kwargs={"post_id": request.post_id})
    myprint(f"video/upgrade: queued post_id={request.post_id} tier={request.tier} for user_id={user_id}")
    return ResponseModel(status_code=200, detail={
        "post_id": request.post_id, "tier": request.tier,
        "credits_required": needed, "status": "queued",
    })


@router.post("/avatar/training", responses={
    200: {"description": "Training started"},
    **{k: v for k, v in error_responses.items() if k in [400, 401, 402]}
})
async def start_avatar_training_endpoint(
    session_token: str = Form(...),
    trigger_word: str = Form(...),
    photos: UploadFile = File(...),
) -> ResponseModel:
    user_id = get_session_user_id(session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")

    balance = get_avatar_credit_balance(user_id)
    if balance < 1:
        raise HTTPException(status_code=402, detail="Insufficient avatar credits. Purchase credits to train a new avatar.")

    zip_bytes = await photos.read()
    if not zip_bytes:
        raise HTTPException(status_code=400, detail="No file data received")

    _MAX_ZIP_BYTES = 50 * 1024 * 1024  # 50 MB compressed
    _MAX_UNCOMPRESSED_BYTES = 200 * 1024 * 1024  # 200 MB uncompressed guard
    if len(zip_bytes) > _MAX_ZIP_BYTES:
        raise HTTPException(status_code=413, detail="ZIP file too large (max 50 MB)")
    import io
    import zipfile as _zipfile
    try:
        with _zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            total_uncompressed = sum(entry.file_size for entry in zf.infolist())
        if total_uncompressed > _MAX_UNCOMPRESSED_BYTES:
            raise HTTPException(status_code=413, detail="ZIP contents too large (max 200 MB uncompressed)")
    except _zipfile.BadZipFile:
        raise HTTPException(status_code=400, detail="Uploaded file is not a valid ZIP")

    from cqc_lem.utilities.avatar.replicate_avatar import start_avatar_training
    try:
        training_id = start_avatar_training(user_id, zip_bytes, trigger_word)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Could not start training: {exc}")

    deduct_avatar_credit(user_id, training_id)
    db_id = insert_avatar_training(user_id, training_id, trigger_word)
    return ResponseModel(status_code=200, detail={"training_id": training_id, "db_id": db_id})


@router.get("/avatar/trainings", responses={
    200: {"description": "Avatar trainings listed"},
    **{k: v for k, v in error_responses.items() if k in [401]}
})
def list_avatar_trainings(session_token: str) -> ResponseModel:
    user_id = get_session_user_id(session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    trainings = get_avatar_trainings(user_id)
    return ResponseModel(status_code=200, detail=trainings)


@router.get("/avatar/training/{avatar_db_id}/status", responses={
    200: {"description": "Training status synced"},
    **{k: v for k, v in error_responses.items() if k in [401, 404]}
})
def sync_avatar_training_status(avatar_db_id: int, session_token: str) -> ResponseModel:
    user_id = get_session_user_id(session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")

    trainings = get_avatar_trainings(user_id)
    match = next((t for t in trainings if t["id"] == avatar_db_id), None)
    if not match:
        raise HTTPException(status_code=404, detail="Training not found")

    if match["status"] in ("succeeded", "failed", "canceled"):
        return ResponseModel(status_code=200, detail=match)

    from cqc_lem.utilities.avatar.replicate_avatar import poll_training_status
    new_status, model_ref = poll_training_status(match["training_id"])
    update_avatar_training_status(match["training_id"], new_status, model_ref)
    match["status"] = new_status
    if model_ref:
        match["model_ref"] = model_ref
    return ResponseModel(status_code=200, detail=match)


@router.put("/avatar/training/{avatar_db_id}/activate", responses={
    200: {"description": "Avatar activated"},
    **{k: v for k, v in error_responses.items() if k in [400, 401, 404]}
})
def activate_avatar(avatar_db_id: int, request: AvatarActivateRequest) -> ResponseModel:
    user_id = get_session_user_id(request.session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")

    trainings = get_avatar_trainings(user_id)
    match = next((t for t in trainings if t["id"] == avatar_db_id), None)
    if not match:
        raise HTTPException(status_code=404, detail="Training not found")
    if match["status"] != "succeeded":
        raise HTTPException(status_code=400, detail="Only succeeded trainings can be activated")

    if set_active_avatar(user_id, avatar_db_id):
        return ResponseModel(status_code=200, detail="Avatar activated")
    raise HTTPException(status_code=500, detail="Could not activate avatar")


# ---------------------------------------------------------------------------
# Admin endpoints — require X-Admin-Secret header matching ADMIN_SECRET env var
# ---------------------------------------------------------------------------

def _require_admin(x_admin_secret: Optional[str] = Header(default=None)) -> None:
    if not ADMIN_SECRET or x_admin_secret != ADMIN_SECRET:
        raise HTTPException(status_code=403, detail="Forbidden")


# Declared security schemes so Swagger (/docs) shows BOTH required credentials in
# the Authorize dialog: the bearer API token AND the admin secret. Both are needed.
_bearer_scheme = HTTPBearer(
    auto_error=False,
    description="API access token — one of API_ACCESS_TOKENS. Sent as 'Authorization: Bearer <token>'.",
)
_admin_secret_scheme = APIKeyHeader(
    name="X-Admin-Secret", auto_error=False,
    description="Admin secret — the ADMIN_SECRET env var.",
)


def _require_api_and_admin(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer_scheme),
    x_admin_secret: Optional[str] = Depends(_admin_secret_scheme),
) -> None:
    """Require BOTH the bearer API token and the admin secret. Used by the
    /admin/test/* endpoints so the docs page presents and enforces both."""
    token = credentials.credentials if credentials else None
    if _API_ACCESS_TOKEN_SET and token not in _API_ACCESS_TOKEN_SET:
        raise HTTPException(status_code=401, detail="Unauthorized — missing or invalid bearer token")
    if not ADMIN_SECRET or x_admin_secret != ADMIN_SECRET:
        raise HTTPException(status_code=403, detail="Forbidden — missing or invalid X-Admin-Secret")


@router.post("/admin/automation-pause", responses={200: {"description": "Automation paused"},
                                                    403: {"description": "Forbidden"}})
def admin_pause_automation(hours: float = 24,
                           x_admin_secret: Optional[str] = Header(default=None)) -> ResponseModel:
    """Kill-switch: pause ALL Selenium automation (comments/replies/DMs/stats/invites) for `hours` so
    a 429-rate-limited account/IP can recover. Posting (API) is unaffected. Default 24h."""
    _require_admin(x_admin_secret)
    from cqc_lem.utilities.linkedin.rate_limit import pause_automation
    seconds = int(max(0.0, hours) * 3600) or 3600
    ok = pause_automation(seconds, reason="admin")
    return ResponseModel(status_code=200, detail={"paused": ok, "seconds": seconds})


@router.post("/admin/automation-resume", responses={200: {"description": "Automation resumed"},
                                                     403: {"description": "Forbidden"}})
def admin_resume_automation(x_admin_secret: Optional[str] = Header(default=None)) -> ResponseModel:
    """Lift a manual automation pause immediately."""
    _require_admin(x_admin_secret)
    from cqc_lem.utilities.linkedin.rate_limit import resume_automation
    return ResponseModel(status_code=200, detail={"resumed": resume_automation()})


@router.get("/admin/automation-status", responses={200: {"description": "Automation status"},
                                                   403: {"description": "Forbidden"}})
def admin_automation_status(x_admin_secret: Optional[str] = Header(default=None)) -> ResponseModel:
    """Current pause + 429-breaker state (seconds remaining on each)."""
    _require_admin(x_admin_secret)
    from cqc_lem.utilities.linkedin.rate_limit import (
        automation_pause_remaining, rate_limit_cooldown_remaining)
    pause_s = automation_pause_remaining()
    return ResponseModel(status_code=200, detail={
        "paused": pause_s > 0,
        "pause_remaining_s": pause_s,
        "breaker_remaining_s": rate_limit_cooldown_remaining(),
    })


@router.post("/admin/fix-video-urls", responses={
    200: {"description": "Video URLs updated"},
    403: {"description": "Forbidden"},
})
def admin_fix_video_urls(
    request: AdminFixVideoUrlsRequest,
    x_admin_secret: Optional[str] = Header(default=None),
) -> ResponseModel:
    _require_admin(x_admin_secret)
    updated = replace_video_url_base(request.old_base, request.new_base, request.user_id)
    myprint(f"admin/fix-video-urls: replaced {updated} row(s) — {request.old_base!r} → {request.new_base!r}")
    return ResponseModel(status_code=200, detail={"updated_rows": updated})


@router.post("/admin/user/location", responses={
    200: {"description": "User login location updated"},
    403: {"description": "Forbidden"},
    404: {"description": "User not found"},
    422: {"description": "Could not geocode the city"},
})
def admin_set_user_location(
    request: AdminLocationByCityRequest,
    x_admin_secret: Optional[str] = Header(default=None),
) -> ResponseModel:
    """Align a user's login location to their purchased proxy's city (admin-only). Geocodes the
    city/state and persists it so the automation browser's geo matches the proxy IP."""
    _require_admin(x_admin_secret)
    if get_user_geo(request.user_id) is None:
        raise HTTPException(status_code=404, detail="User not found")
    try:
        geo = geocode_city(request.city, request.state, request.country)
    except GeocodeError as e:
        raise HTTPException(status_code=422, detail=str(e))
    saved = update_user_location(
        request.user_id, geo["latitude"], geo["longitude"],
        city=geo["city"], country=geo["country"], locale=geo["locale"],
        timezone=geo["timezone"], source="manual")
    if not saved:
        raise HTTPException(status_code=500, detail="Could not save location")
    myprint(f"admin/user/location: set user {request.user_id} -> {geo['city']}, {geo.get('country')} "
            f"({geo['latitude']},{geo['longitude']} {geo.get('timezone')})")
    return ResponseModel(status_code=200, detail=geo)


@router.post("/admin/regenerate-carousel", responses={
    200: {"description": "Carousel regenerated"},
    403: {"description": "Forbidden"},
    404: {"description": "Post not found or not a carousel"},
})
def admin_regenerate_carousel(
    request: AdminRegenerateCarouselRequest,
    x_admin_secret: Optional[str] = Header(default=None),
) -> ResponseModel:
    _require_admin(x_admin_secret)

    post_type = get_post_type(request.post_id)
    # Document posts are carousels published as a native PDF — same slide regeneration path.
    if post_type not in (PostType.CAROUSEL, PostType.DOCUMENT):
        raise HTTPException(status_code=404, detail="Post not found or not a carousel post")

    stage = get_post_buyer_stage(request.post_id) or "awareness"
    from cqc_lem.app.run_content_plan import create_carousel_content
    try:
        new_content = create_carousel_content(
            request.user_id, stage=stage, post_id=request.post_id,
            template=request.template,
        )
        from cqc_lem.utilities.db import update_db_post_content
        update_db_post_content(request.post_id, new_content)
    except Exception as exc:
        myprint(f"admin/regenerate-carousel: failed for post_id={request.post_id} — {exc}")
        raise HTTPException(status_code=500, detail=str(exc))

    myprint(f"admin/regenerate-carousel: regenerated post_id={request.post_id}")
    return ResponseModel(status_code=200, detail={"post_id": request.post_id, "content_preview": new_content[:120]})


@router.post("/admin/regenerate-video", responses={
    200: {"description": "Video regenerated"},
    403: {"description": "Forbidden"},
    404: {"description": "Post not found or not a video"},
    500: {"description": "Regeneration failed"},
})
def admin_regenerate_video(
    request: AdminRegenerateVideoRequest,
    x_admin_secret: Optional[str] = Header(default=None),
) -> ResponseModel:
    """Regenerate ONLY the video asset for an existing video post (keeps content)."""
    _require_admin(x_admin_secret)

    post_type = get_post_type(request.post_id)
    if post_type != PostType.VIDEO:
        raise HTTPException(status_code=404, detail="Post not found or not a video post")

    from cqc_lem.app.run_content_plan import regenerate_video_for_post
    try:
        new_url = regenerate_video_for_post(request.post_id)
    except Exception as exc:
        myprint(f"admin/regenerate-video: failed for post_id={request.post_id} — {exc}")
        raise HTTPException(status_code=500, detail=str(exc))
    if not new_url:
        raise HTTPException(status_code=500, detail="Video regeneration failed (no asset produced)")

    myprint(f"admin/regenerate-video: regenerated post_id={request.post_id} -> {new_url}")
    return ResponseModel(status_code=200, detail={"post_id": request.post_id, "video_url": new_url})


@router.post("/admin/generate-media-variants", responses={
    200: {"description": "Variants generated"},
    403: {"description": "Forbidden"},
    422: {"description": "Provide post_id or text/topic"},
    500: {"description": "Generation failed"},
})
def admin_generate_media_variants(
    request: GenerateMediaVariantsRequest,
    x_admin_secret: Optional[str] = Header(default=None),
) -> ResponseModel:
    """Generate image/video variants for review WITHOUT mutating any post.

    Returns public /api/assets URLs + a cost estimate. Defaults to a 3-variant
    Gen-4 Turbo matrix; pass `combos` to compare other models/ratios/seeds.
    """
    _require_admin(x_admin_secret)

    from cqc_lem.app.generate_variants import generate_media_variants
    combos = [c.model_dump() for c in request.combos] if request.combos else None
    try:
        payload = generate_media_variants(
            post_id=request.post_id, text=request.text, topic=request.topic,
            user_id=request.user_id, combos=combos,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except Exception as exc:
        myprint(f"admin/generate-media-variants: failed — {exc}")
        raise HTTPException(status_code=500, detail=str(exc))

    myprint(f"admin/generate-media-variants: batch={payload['batch_id']} "
            f"variants={len(payload['variants'])} est=${payload['total_estimated_cost_usd']}")
    return ResponseModel(status_code=200, detail=payload)


# ---------------------------------------------------------------------------
# Admin test-run endpoints — kick a single engagement task on the selenium
# worker so you can watch it live in the Chrome VNC. Inputs are typed query
# params (so /docs renders individual fields, not a raw JSON body). BOTH the
# bearer API token AND X-Admin-Secret are required (see _require_api_and_admin).
# ---------------------------------------------------------------------------

@router.post("/admin/test/comment", responses={
    200: {"description": "Commenting test run queued"},
    401: {"description": "Missing/invalid bearer token"},
    403: {"description": "Missing/invalid admin secret"},
})
def admin_test_comment(
    user_id: int = Query(..., description="LinkedIn account user id", examples=[1]),
    loop_for_duration: int = Query(300, ge=10, le=3600,
                                   description="Seconds before the run self-terminates"),
    _: None = Depends(_require_api_and_admin),
) -> ResponseModel:
    """Run the feed-commenting automation for a user (comments on posts in their feed)."""
    result = automate_commenting.apply_async(kwargs={
        "user_id": user_id, "loop_for_duration": loop_for_duration,
    }, queue="se_engage")
    myprint(f"admin/test/comment: queued task={result.id} user_id={user_id}")
    return ResponseModel(status_code=200, detail={
        "task_id": result.id, "task": "automate_commenting", "user_id": user_id,
    })


@router.post("/admin/test/reply", responses={
    200: {"description": "Reply test run queued"},
    401: {"description": "Missing/invalid bearer token"},
    403: {"description": "Missing/invalid admin secret"},
    404: {"description": "User for post not found"},
})
def admin_test_reply(
    post_id: int = Query(..., description="Id of an already-posted post to reply on", examples=[42]),
    loop_for_duration: int = Query(300, ge=10, le=3600,
                                   description="Seconds before the run self-terminates"),
    future_forward: int = Query(0, ge=0, le=5, description="Forward index (0-5)"),
    _: None = Depends(_require_api_and_admin),
) -> ResponseModel:
    """Run the reply-to-comments automation for a specific (already-posted) post."""
    user_id = get_post_user_id(post_id)
    if not user_id:
        raise HTTPException(status_code=404, detail="User for post not found")
    result = automate_reply_commenting.apply_async(kwargs={
        "user_id": user_id, "post_id": post_id,
        "loop_for_duration": loop_for_duration, "future_forward": future_forward,
    }, queue="se_engage")
    myprint(f"admin/test/reply: queued task={result.id} post_id={post_id}")
    return ResponseModel(status_code=200, detail={
        "task_id": result.id, "task": "automate_reply_commenting",
        "post_id": post_id, "user_id": user_id,
    })


@router.post("/admin/consolidate-duplicate-comments", responses={
    200: {"description": "Consolidation run queued"},
    401: {"description": "Missing/invalid bearer token"},
    403: {"description": "Missing/invalid admin secret"},
})
def admin_consolidate_duplicate_comments(
    user_id: int = Query(..., description="LinkedIn account user id", examples=[1]),
    dry_run: bool = Query(True, description="Report-only when true; set false to actually delete extras"),
    hours: int = Query(168, ge=1, le=2160,
                       description="Look back this many hours for duplicate-commented posts (default 7 days)"),
    _: None = Depends(_require_api_and_admin),
) -> ResponseModel:
    """Keep one comment per post and delete the extras for posts this user commented on more than once.
    Defaults to a DRY RUN (report only) — pass dry_run=false to actually delete."""
    result = consolidate_duplicate_comments_for_user.apply_async(kwargs={
        "user_id": user_id, "dry_run": dry_run, "hours": hours,
    }, queue="se_engage")
    myprint(f"admin/consolidate-duplicate-comments: queued task={result.id} user_id={user_id} dry_run={dry_run}")
    return ResponseModel(status_code=200, detail={
        "task_id": result.id, "task": "consolidate_duplicate_comments_for_user",
        "user_id": user_id, "dry_run": dry_run, "hours": hours,
    })


@router.post("/admin/test/dm", responses={
    200: {"description": "DM test run queued"},
    401: {"description": "Missing/invalid bearer token"},
    403: {"description": "Missing/invalid admin secret"},
})
def admin_test_dm(
    user_id: int = Query(..., description="LinkedIn account user id", examples=[1]),
    loop_for_duration: int = Query(300, ge=10, le=3600,
                                   description="Seconds before the run self-terminates"),
    _: None = Depends(_require_api_and_admin),
) -> ResponseModel:
    """Run the appreciation-DM automation (DMs people who recently viewed the profile)."""
    result = automate_appreciation_dms_for_user.apply_async(kwargs={
        "user_id": user_id, "loop_for_duration": loop_for_duration,
    }, queue="se_outreach")
    myprint(f"admin/test/dm: queued task={result.id} user_id={user_id}")
    return ResponseModel(status_code=200, detail={
        "task_id": result.id, "task": "automate_appreciation_dms_for_user",
        "user_id": user_id,
    })


@router.post("/admin/test/dm-direct", responses={
    200: {"description": "Direct DM queued"},
    401: {"description": "Missing/invalid bearer token"},
    403: {"description": "Missing/invalid admin secret"},
})
def admin_test_dm_direct(
    user_id: int = Query(..., description="LinkedIn account user id", examples=[1]),
    profile_url: str = Query(..., description="LinkedIn profile URL to message",
                             examples=["https://www.linkedin.com/in/some-person/"]),
    message: str = Query(..., description="Message body to send", examples=["Hi — testing, please ignore."]),
    _: None = Depends(_require_api_and_admin),
) -> ResponseModel:
    """Send ONE direct DM to a specific profile URL — the most deterministic way to
    watch the messaging flow end-to-end in the VNC."""
    result = send_private_dm.apply_async(kwargs={
        "user_id": user_id, "profile_url": profile_url, "message": message,
    }, queue="se_outreach")
    myprint(f"admin/test/dm-direct: queued task={result.id} user_id={user_id} -> {profile_url}")
    return ResponseModel(status_code=200, detail={
        "task_id": result.id, "task": "send_private_dm",
        "user_id": user_id, "profile_url": profile_url,
    })


@router.get("/admin/task-status/{task_id}", responses={
    200: {"description": "Task status"},
    401: {"description": "Missing/invalid bearer token"},
    403: {"description": "Missing/invalid admin secret"},
})
def admin_task_status(
    task_id: str,
    _: None = Depends(_require_api_and_admin),
) -> ResponseModel:
    """Poll a queued test task's state (PENDING/STARTED/SUCCESS/FAILURE)."""
    from cqc_lem.app.my_celery import app as celery_app
    res = celery_app.AsyncResult(task_id)
    detail = {"task_id": task_id, "state": res.state}
    if res.ready():
        detail["result"] = str(res.result)[:500]
    return ResponseModel(status_code=200, detail=detail)


@router.get("/carousel-templates", responses={200: {"description": "Available carousel templates"}})
def list_carousel_templates() -> ResponseModel:
    """Return all available carousel visual templates for the UI picker."""
    from cqc_lem.utilities.carousel_creator import CAROUSEL_TEMPLATES
    templates = [
        {"key": k, "label": v["label"], "description": v["description"]}
        for k, v in CAROUSEL_TEMPLATES.items()
    ]
    return ResponseModel(status_code=200, detail={"templates": templates})


@router.post("/generate-carousel", responses={
    200: {"description": "Carousel slides generated"},
    403: {"description": "Forbidden"},
    500: {"description": "Generation failed"},
})
def generate_carousel_preview(request: GenerateCarouselPreviewRequest) -> ResponseModel:
    """Generate carousel slide images from AI content + chosen template.
    Returns slide_urls (publicly accessible) and a suggested caption.
    The caller can pass these as carousel_slides when scheduling the post.
    """
    import time as _time
    from cqc_lem.utilities.db import get_session_user_id
    from cqc_lem.utilities.env_constants import API_URL_FINAL
    from cqc_lem.utilities.carousel_creator import (
        create_carousel_slide_images, CAROUSEL_TEMPLATES, DEFAULT_TEMPLATE,
        EducationalContentCarousel, CaseStudyCarousel, PersonalStoryCarousel,
        IndustryInsightsCarousel, ProductDemoCarousel,
    )
    from cqc_lem.utilities.ai.ai_helper import generate_carousel_content

    user_id = get_session_user_id(request.session_token)
    if not user_id:
        raise HTTPException(status_code=403, detail="Invalid session token")
    stage = request.stage or "awareness"
    stage_lower = stage.lower()

    _template_by_stage = {
        "awareness": "bold_listicle",
        "consideration": "step_framework",
        "decision": "stat_reveal",
        "personal": "story_arc",
        "story": "story_arc",
    }
    carousel_template = request.template or next(
        (v for k, v in _template_by_stage.items() if k in stage_lower),
        DEFAULT_TEMPLATE,
    )

    if carousel_template not in CAROUSEL_TEMPLATES:
        carousel_template = DEFAULT_TEMPLATE

    # Generate a unique preview directory so slides don't collide across users
    preview_id = f"preview_{user_id}_{int(_time.time())}"
    current_dir = os.path.dirname(os.path.abspath(__file__))
    assets_root = os.path.join(current_dir, "..", "assets", "images", "carousel", preview_id)
    output_dir = os.path.realpath(assets_root)

    try:
        post_text, carousel_dict = generate_carousel_content(user_id, stage)

        _model_map = {
            "awareness": EducationalContentCarousel,
            "consideration": CaseStudyCarousel,
            "decision": ProductDemoCarousel,
            "personal": PersonalStoryCarousel,
            "story": PersonalStoryCarousel,
        }
        model_cls = next(
            (v for k, v in _model_map.items() if k in stage_lower),
            IndustryInsightsCarousel,
        )

        carousel_obj = model_cls(**carousel_dict)
        image_paths = create_carousel_slide_images(
            carousel_obj, post_id=0, output_dir=output_dir, template=carousel_template
        )
        slide_urls = [
            f"{API_URL_FINAL}/api/assets?file_name=images/carousel/{preview_id}/{os.path.basename(p)}"
            for p in image_paths
        ]
    except Exception as exc:
        myprint(f"generate-carousel: failed for user_id={user_id} — {exc}")
        raise HTTPException(status_code=500, detail=str(exc))

    return ResponseModel(status_code=200, detail={
        "slide_urls": slide_urls,
        "caption": post_text,
        "template": carousel_template,
    })


# Register the /api router
app.include_router(router)

# Backward-compat redirect: /assets?file_name=... → /api/assets?file_name=...
# Must be registered before the SPA StaticFiles mount so it takes priority.
@app.get("/assets", include_in_schema=False)
async def assets_compat_redirect(request: Request, file_name: Optional[str] = None):
    if file_name:
        return RedirectResponse(url=f"/api/assets?{request.url.query}", status_code=301)
    raise HTTPException(status_code=404)

# Serve the React SPA for all non-API routes (must come after include_router)
if os.path.isdir(_ui_dist):
    _spa_index = os.path.join(_ui_dist, "index.html")

    class _ImmutableStaticFiles(StaticFiles):
        # Vite emits content-hashed filenames, so assets can be cached forever.
        # (CDN edge cache is also purged on each deploy via build-and-push.yml.)
        async def get_response(self, path, scope):
            response = await super().get_response(path, scope)
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
            return response

    # Serve static assets (JS/CSS/icons) from the dist root
    app.mount("/assets", _ImmutableStaticFiles(directory=os.path.join(_ui_dist, "assets")), name="spa-assets")

    @app.get("/{full_path:path}", response_class=HTMLResponse, include_in_schema=False)
    def serve_spa(full_path: str):
        with open(_spa_index) as fh:
            # The HTML shell references hashed asset filenames, so it must NEVER be
            # cached by browsers or the CDN — otherwise a stale shell points at an
            # old bundle after every deploy. (Cloudflare must respect this; if a
            # "Cache Everything" rule overrides it, the rule needs an HTML bypass.)
            return HTMLResponse(content=fh.read(), headers={
                "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
                "Pragma": "no-cache",
            })


def send_bytes_range_requests(
        file_path: str, start: int, end: int, chunk_size: int = 10_000
):
    with open(file_path, "rb") as f:
        f.seek(start)
        while (pos := f.tell()) <= end:
            read_size = min(chunk_size, end + 1 - pos)
            yield f.read(read_size)


def _get_range_header(range_header: str, file_size: int) -> tuple[int, int]:
    def _invalid_range():
        return HTTPException(
            status.HTTP_416_REQUESTED_RANGE_NOT_SATISFIABLE,
            detail=f"Invalid request range (Range:{range_header!r})",
        )

    try:
        h = range_header.replace("bytes=", "").split("-")
        start = int(h[0]) if h[0] != "" else 0
        end = int(h[1]) if h[1] != "" else file_size - 1
    except ValueError:
        raise _invalid_range()

    if start > end or start < 0 or end > file_size - 1:
        raise _invalid_range()
    return start, end


def range_requests_response(
        request: Request, file_path: str, content_type: str
) -> StreamingResponse:
    file_size = os.stat(file_path).st_size
    range_header = request.headers.get("range")

    headers = {
        "content-type": content_type,
        "accept-ranges": "bytes",
        "content-encoding": "identity",
        "content-length": str(file_size),
        "access-control-expose-headers": (
            "content-type, accept-ranges, content-length, "
            "content-range, content-encoding"
        ),
    }
    start = 0
    end = file_size - 1
    status_code = status.HTTP_200_OK

    if range_header is not None:
        start, end = _get_range_header(range_header, file_size)
        size = end - start + 1
        headers["content-length"] = str(size)
        headers["content-range"] = f"bytes {start}-{end}/{file_size}"
        status_code = status.HTTP_206_PARTIAL_CONTENT

    return StreamingResponse(
        send_bytes_range_requests(file_path, start, end),
        headers=headers,
        status_code=status_code,
    )
