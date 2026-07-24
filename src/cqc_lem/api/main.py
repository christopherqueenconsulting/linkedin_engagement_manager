import io
import json
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
    create_pin_for_email, verify_pin_for_email, delete_pin_for_email,
    create_session, get_session_user_id, delete_session,
    add_user_by_email, get_user_email, get_user_token_info, store_linkedin_li_at,
    has_linkedin_session, get_user_password_pair_by_id,
    get_company_linked_in_url_for_user, update_company_linked_in_url_for_user,
    get_user_subscription_info, get_user_preferences, update_user_preferences,
    get_engagement_preferences, update_engagement_preferences, get_or_create_reply_inbound_token,
    get_newsletter_settings, update_newsletter_settings,
    get_pending_newsletter_editions,
    get_latest_edition_scheduled_for, update_newsletter_edition, get_newsletter_edition,
    get_user_timezone,
    get_user_groups, set_groups_enabled, get_post_engagement_rows,
    get_lead_magnet_settings, update_lead_magnet_settings,
    get_dm_templates, upsert_dm_templates,
    update_subscription_from_stripe, update_user_linkedin_token,
    get_users_with_stripe_subscriptions,
    update_user_linkedin_password,
    get_user_blog_url, get_user_sitemap_url, get_linkedin_profile_url_by_user_id,
    get_user_by_stripe_customer_id, get_avatar_credit_ledger_entry_by_session,
    get_avatar_credit_balance, add_avatar_credits,
    get_video_credit_balance, add_video_credits,
    get_video_credit_ledger_entry_by_session, update_post_video_quality,
    deduct_avatar_credit, insert_avatar_training,
    update_avatar_training_status, set_active_avatar,
    get_avatar_trainings, get_active_avatar,
    get_user_timezone, update_user_timezone,
    get_user_geo, update_user_location,
    replace_video_url_base, get_post_type, get_post_buyer_stage, get_post_status,
    update_db_post_carousel_slides,
    get_post_url_from_log_for_user,
)
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
from cqc_lem.utilities.logger import myprint, log_warning, log_info
from cqc_lem.utilities.mime_type_helper import get_file_mime_type
from cqc_lem.utilities.observability import track_api_call
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
_PUBLIC_API_PREFIXES = ("/api/auth/", "/api/billing/webhook", "/api/assets",
                        "/api/linkedin/verification-pin", "/api/linkedin/comment-notification",
                        "/api/app-info",
                        "/api/extension/", "/api/user/linkedin-cookie")


def _api_token_required(path: str) -> bool:
    if not _API_ACCESS_TOKEN_SET or not path.startswith("/api/"):
        return False
    return not any(path.startswith(p) for p in _PUBLIC_API_PREFIXES)


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


class AuthInitRequest(BaseModel):
    email: str


class AuthVerifyRequest(BaseModel):
    email: str
    pin: str


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


class UserPreferencesRequest(BaseModel):
    session_token: str
    last_login_inactivate_delay: Optional[int] = 90
    auto_schedule_posts: bool = False


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
_LEN_NL_TITLE = 255       # newsletter_settings.title VARCHAR(255)
_LEN_NL_TOPIC = 512       # newsletter_settings.topic VARCHAR(512)
_LEN_DM_RECIPIENT_URL = 512   # scheduled_dms.recipient_profile_url VARCHAR(512)
_LEN_DM_RECIPIENT_NAME = 255  # scheduled_dms.recipient_name VARCHAR(255)


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

    @field_validator("max_queued_drafts")
    @classmethod
    def _clamp_max_queued(cls, v: int) -> int:
        return max(1, min(10, v))

    @field_validator("generate_lead_days")
    @classmethod
    def _clamp_lead_days(cls, v: int) -> int:
        return max(0, min(60, v))


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


class EngagementPreferencesRequest(BaseModel):
    session_token: str
    tone: Optional[str] = Field(default=None, max_length=_LEN_TONE)
    comment_length: str = "short"
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
    min_reactions: Optional[int] = None
    max_post_age_hours: Optional[int] = 24
    reply_to_own_comments: bool = True
    max_comments_per_day: int = 20
    max_dms_per_day: int = 20
    default_buyer_stage: Optional[str] = Field(default=None, max_length=_LEN_BUYER_STAGE)
    default_video_quality: str = "standard"
    reply_check_mode: str = "event"
    reply_sweeps_per_day: int = 2
    reply_max_post_age_days: int = 2
    feed_fallback_when_empty: bool = True
    link_in_first_comment: bool = True

    @field_validator("default_video_quality")
    @classmethod
    def _coerce_video_quality(cls, v: str) -> str:
        return v if v in _VALID_VIDEO_QUALITIES else "standard"

    @field_validator("reply_check_mode")
    @classmethod
    def _coerce_reply_mode(cls, v: str) -> str:
        return v if v in ("event", "scheduled", "off") else "event"

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


class DmTemplateItem(BaseModel):
    event_type: str
    step: int = 0
    delay_hours: int = 0
    template_text: str = Field(max_length=_LEN_DM_TEMPLATE)
    is_active: bool = True


class DmTemplatesRequest(BaseModel):
    session_token: str
    templates: List[DmTemplateItem] = []


class LinkedInPasswordRequest(BaseModel):
    session_token: str
    linkedin_password: str


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
    **{k: v for k, v in error_responses.items() if k in [400]}
})
def create_weekly_content(user_id: int) -> ResponseModel:
    if not user_id:
        raise HTTPException(status_code=400, detail="User ID is required")

    # Chain: plan posts for the rest of the month first, then fill content for this week.
    # This ensures the user always has PLANNING rows before content generation runs.
    celery_chain(
        plan_content_for_user.si(user_id=user_id),
        auto_create_weekly_content.si(user_id=user_id),
    ).apply_async()
    return ResponseModel(status_code=200, detail="Weekly content created successfully")


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
    post_type_filter: Optional[str] = Query(default=None, pattern='^(text|video|carousel)$'),
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


# LinkedIn OAuth initiation — builds the authorization URL and redirects user to LinkedIn
@router.post("/auth/email/init")
def auth_email_init(request: AuthInitRequest) -> ResponseModel:
    email = request.email.strip().lower()
    if not email:
        raise HTTPException(status_code=400, detail="Email is required")

    user_exists = bool(get_user_id(email))
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
    return ResponseModel(status_code=200, detail={"user_id": user_id, "email": email})


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
    # Read-only: the address the user forwards LinkedIn comment-notification emails to (event mode).
    try:
        from cqc_lem.utilities.linkedin.notification_email import reply_inbound_address
        token = get_or_create_reply_inbound_token(user_id)
        prefs["reply_inbound_address"] = reply_inbound_address(token) if token else None
    except Exception:
        prefs["reply_inbound_address"] = None
    # Gmail forwarding auto-confirmation status (so the UI can surface the code if auto-confirm failed).
    prefs["gmail_forward_confirmation"] = get_gmail_forward_confirmation(user_id)
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
        sched = e.get("scheduled_for")
        if sched is not None:
            e["scheduled_for"] = sched.isoformat() if hasattr(sched, "isoformat") else str(sched)
    # next_publish is the slot AFTER the last edition already queued, so the UI can show what's next.
    anchor = get_latest_edition_scheduled_for(user_id)
    next_pub = _compute_next_publish(user_id, anchor=anchor)
    settings = get_newsletter_settings(user_id)
    return ResponseModel(status_code=200, detail={
        "editions": editions,
        "next_publish": next_pub.isoformat() if next_pub is not None else None,
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
    return ResponseModel(status_code=200,
                         detail=get_scheduled_dms(user_id, status_filter=status_filter, page=page,
                                                  page_size=page_size, sort_order=sort_order))


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
    return ResponseModel(status_code=200, detail={
        "recommendations": recommend_post_times(rows),
        "rankings": rank_content_attributes(rows, top_n=5),
        "sample_size": len(rows),
    })


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

    items = [
        {"key": "email", "label": "Verified email", "ok": True, "required": True,
         "hint": None},
        {"key": "linkedin_oauth", "label": "LinkedIn connected (posting)", "ok": has_oauth,
         "required": True, "hint": "Connect LinkedIn in your account."},
        {"key": "linkedin_session", "label": "LinkedIn session (engagement)",
         "ok": has_engagement_login, "required": True,
         "hint": "Connect your LinkedIn session (cookie) or save your LinkedIn password."},
        {"key": "subscription", "label": "Active plan", "ok": sub_active, "required": True,
         "hint": "Start a plan or trial under Subscription."},
        {"key": "location", "label": "Login location set", "ok": has_location,
         "required": False, "hint": "Set your login location to reduce LinkedIn challenges."},
    ]
    ready = all(i["ok"] for i in items if i["required"])
    return ResponseModel(status_code=200, detail={"ready": ready, "items": items})


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

    url = create_checkout_session(
        stripe_customer_id,
        request.tier,
        request.success_url,
        request.cancel_url,
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
    if post_type != PostType.CAROUSEL:
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
