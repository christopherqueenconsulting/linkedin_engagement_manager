"""The FastAPI application: every `/api` route, the edge credential gate, and session resolution.

The invariant this file exists to hold (issue #914): **every `/api` route resolves its caller
through `get_session_user_id()`** — `require_session_user_id()` is that plus a 401. An `email`,
`user_id` or `post_id` arriving in a request is a TARGET to authorise (403 +
`foreign_target_denied`), never the actor. `tests/unit/api/test_api_route_identity.py` walks the
route table and fails the build on a gated route that resolves nobody, so the rule is checked
rather than remembered.

The bearer gate in `api_token_middleware` is an edge filter and NOT authorisation — see the long
comment above `_API_ACCESS_TOKEN_SET`. Authorisation happens in the handler, which fails closed.
"""

import io
import json
import os
import re
import time
import zipfile
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from enum import IntEnum
from typing import Any, Awaitable, Callable, Dict, Iterator, List, NoReturn, Optional, Union
from urllib.parse import urlparse

import requests
from celery import chain as celery_chain
from fastapi import (
    APIRouter,
    FastAPI,
    HTTPException,
    Query,
    Request,
    Response,
    status,
)
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from linkedin_api.clients.auth.client import AuthClient
from linkedin_api.clients.restli.client import RestliClient
from linkedin_api.common.errors import ResponseFormattingError
from pydantic import BaseModel, Field, field_validator

from cqc_lem import assets_dir

# Re-exported, not merely imported: 363 annotations in this file say `ResponseModel`, and every test
# and router that reaches `cqc_lem.api.main.ResponseModel` keeps resolving. It lives in `api.models`
# now because a router module cannot import a NAME out of this one without a cycle (#1154).
from cqc_lem.api.models import (
    _LEN_DM_TEMPLATE,
    ResponseModel,
    SessionTokenField,
    error_responses,
)
from cqc_lem.api.response_schemas import (
    ActivityEntry,
    DashboardStats,
    PlannedTasksDetail,
    PostsPage,
)
from cqc_lem.api.spa_assets import (
    NO_STORE_CACHE_CONTROL,
    VITE_BASE_URL_PLACEHOLDER,
    ArchivedStaticFiles,
    public_base_url,
    register_spa_public_routes,
    render_base_url,
    spa_index_headers,
    sync_build_to_archive,
)
from cqc_lem.app.aws_test_celery_task import test_get_my_profile
from cqc_lem.app.engagement.invites import automate_invites_to_company_page_for_user
from cqc_lem.app.engagement.outreach import send_lead_response
from cqc_lem.app.engagement.posting import automate_reply_commenting, sweep_reply_comments
from cqc_lem.app.run_content_plan import auto_create_weekly_content, plan_content_for_user
from cqc_lem.utilities.auth_factors import (
    enrollment_required,
)
from cqc_lem.utilities.content_generation_status import clear_generation_status, get_generation_status, mark_queued
from cqc_lem.utilities.db import (
    SESSION_SCOPE_AGENT,
    SESSION_SCOPE_ENROLL,
    SESSION_SCOPE_EXTENSION,
    SESSION_SCOPE_FULL,
    SESSION_SCOPE_RECOVERY,
    AuthAuditEvent,
    CatchupTouchStatus,
    ConnectionRequestStatus,
    FeedbackSource,
    LeadSignalStatus,
    LeadStage,
    OwnershipUnprovable,
    PostStatus,
    PostType,
    ScheduledDmStatus,
    add_user_with_access_token,
    bulk_update_posts,
    count_hot_leads,
    count_new_lead_signals,
    extend_trial_for_user,
    get_catchup_touch,
    get_catchup_touch_user_id,
    get_catchup_touches,
    get_connection_request,
    get_connection_request_user_id,
    get_connection_requests,
    get_dashboard_counts,
    get_engagement_preferences,
    get_latest_post_stats,
    get_latest_review_feedback_id,
    get_lead,
    get_lead_signal,
    get_lead_signals,
    get_leads,
    get_passkey_by_credential_id,
    get_planned_tasks,
    get_post_type,
    get_post_url_from_log_for_user,
    get_post_user_id,
    get_posted_posts,
    get_posts,
    get_recent_logs,
    get_scheduled_dm_user_id,
    get_scheduled_dms,
    get_session_user_id as _db_get_session_user_id,
    get_user_email,
    get_user_subscription_info,
    get_video_credit_balance,
    insert_connection_request,
    insert_feedback,
    insert_post,
    insert_scheduled_dm,
    record_auth_event,
    release_enrollment_scope,
    resolve_session as _db_resolve_session,
    soft_delete_posts,
    update_catchup_touch,
    update_catchup_touch_status,
    update_connection_request,
    update_connection_request_status,
    update_db_post,
    update_db_post_rejection_reason,
    update_factor_counter,
    update_lead,
    update_lead_signal,
    update_post_use_avatar,
    update_post_video_quality,
    update_scheduled_dm,
    update_scheduled_dm_status,
    update_user_linkedin_token,
    user_owns_posts,
)
from cqc_lem.utilities.env_constants import (
    API_ACCESS_TOKENS,
    AUTH_CHALLENGE_TTL_SECONDS,
    LI_CLIENT_ID,
    LI_CLIENT_SECRET,
    LI_REDIRECT_URL,
    LI_STATE_SALT,
    SESSION_COOKIE_NAME,
)
from cqc_lem.utilities.linkedin.verification_pin import (
    extract_pin_from_text,
    extract_token_from_address,
    submit_pin_by_token,
)
from cqc_lem.utilities.logger import log_critical, log_debug, log_error, log_info, log_warning
from cqc_lem.utilities.mime_type_helper import get_file_mime_type
from cqc_lem.utilities.observability import (
    capture_exception,
    track_api_call,
)
from cqc_lem.utilities.post_image import (
    owns_post_image_url,
)
from cqc_lem.utilities.quality_gates import (
    parse_gate_findings,
)
from cqc_lem.utilities.utils import get_file_extension_from_filepath
from cqc_lem.utilities.webauthn_util import (
    RelyingParty,
    WebAuthnUnavailable,
    credential_id_from_response,
    relying_party as webauthn_relying_party,
    verify_assertion as verify_passkey_assertion,
)

# The docs surface lives UNDER /api (issue #1020). At the FastAPI defaults it sits at /docs,
# /redoc and /openapi.json — outside /api, so the credential gate below (which only inspects paths
# starting with "/api/") never saw it and all three were served to anyone. Moving them under /api
# puts them on the same side of that boundary as everything else they describe; they are then
# re-opened deliberately via _PUBLIC_API_PREFIXES rather than by accident of routing.
#
# swagger_ui_oauth2_redirect_url must be set explicitly: FastAPI defaults it to the fixed literal
# "/docs/oauth2-redirect" and does NOT derive it from docs_url, so moving docs_url alone strands
# the helper outside /api and breaks Swagger's Authorize flow silently.
app = FastAPI(
    docs_url="/api/docs",
    redoc_url="/api/redoc",
    openapi_url="/api/openapi.json",
    swagger_ui_oauth2_redirect_url="/api/docs/oauth2-redirect",
)

# All API routes live under /api so the React client's baseURL: '/api' works
router = APIRouter(prefix="/api")


@app.middleware("http")
async def observability_middleware(request: Request, call_next):
    """Time and count every request, and file an `$exception` for the ones nothing else caught.

    Only UNHANDLED exceptions reach the `except` here: FastAPI turns a route's own `HTTPException`
    into a response before it ever unwinds this far, which is what keeps a 4xx from filing an
    error-tracking issue (`docs/error-tracking.md`). `track_api_call` runs in `finally` so a
    request that blew up is still counted — as the 500 it was.
    """
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


# The ONE `/api` path that is meant to be cached. `get_assets` is public by design (LinkedIn
# fetches these URLs unauthenticated when publishing) and every stored name carries a random
# token, so the bytes behind one URL never change — the opposite of the payloads below.
#
# Matched on a path-segment boundary for the same reason `_is_public_api_path` below is: a bare
# `startswith` would hand a future `/api/assets-admin` the exemption too, and an exemption is
# exactly the thing that must not spread by accident.
_CACHEABLE_API_PREFIX = "/api/assets"


def _is_cacheable_api_path(path: str) -> bool:
    return path == _CACHEABLE_API_PREFIX or path.startswith(_CACHEABLE_API_PREFIX + "/")


async def api_cache_control_middleware(request: Request, call_next):
    """Mark every `/api` payload uncacheable, the way the HTML shell already is (issue #1527).

    FastAPI sends no `Cache-Control` of its own, and this app is served through a Cloudflare tunnel
    that caches a GET without one: measured on prod, a second identical `GET /api/app-info` comes
    back `cf-cache-status: HIT`. That makes a write invisible. The reporter skipped a group post,
    pressed "Put back in the queue" and generated an image for it — the `PUT`s answered 200, the
    SPA re-fetched `/api/user/group-post-draft`, and the refetch was served from the edge copy
    written before either write, so the draft still read SKIPPED with no image. A full page reload
    showed the same thing, which is the tell: the request never reached the origin at all.

    It is also what stops one account being served another's data. The SPA sends the same query
    string for every caller (`session_token=cookie` — the session rides in an httpOnly cookie since
    #745), so a shared cache keyed on the URL has one entry for a per-user payload.

    Set on the response rather than per route: a route that forgets is exactly the case that goes
    unnoticed, and the header is only meaningful in front of a cache nothing here can see.
    """
    response = await call_next(request)
    path = request.url.path
    if path.startswith("/api/") and not _is_cacheable_api_path(path):
        response.headers["Cache-Control"] = NO_STORE_CACHE_CONTROL
        response.headers["Pragma"] = "no-cache"
    return response


# Credential gate for /api routes. Active only when API_ACCESS_TOKENS is set, so local/dev (and
# existing tests) run open. Login and the Stripe webhook stay public; everything else under /api
# must present ONE of two credentials. Routes served outside /api (SPA, /health,
# /auth/linkedin/*) are never gated here.
#
# Since issue #950 the bearer token is a NON-BROWSER credential and nothing else. It used to be
# baked into the SPA bundle at build time (`VITE_API_TOKEN`), which made it a secret every visitor
# held — worth nothing as a gate, unrotatable without a rebuild + redeploy, and counted as a layer
# in threat models it was not one in. The SPA authenticates on its httpOnly session cookie, which
# is what every /api handler already resolves the caller from since #914.
#
# So the gate asks only "did this caller bring A credential": a valid bearer (scripts, Postman, the
# admin tooling in `scripts/`) or a session credential the route itself will judge. It is an edge
# filter, and a weak one on purpose — one arbitrary cookie byte clears it, so what it actually keeps
# off the handlers is unauthenticated traffic that never tries, not anything automated. It is NOT
# authorisation. Authorisation is `require_session_user_id()` in the handler, which fails closed on a
# cookie that does not resolve; presenting a junk cookie buys a 401 from the route instead of a 401
# from here. That the handler ALWAYS does so is checked, not assumed —
# `tests/unit/api/test_api_route_identity.py` walks the route table and fails the build on a gated
# /api route that resolves no caller.
_API_ACCESS_TOKEN_SET = {t.strip() for t in API_ACCESS_TOKENS.split(",") if t.strip()}
# /api/assets is public: it serves generated post media (images/videos) that
# LinkedIn fetches over an unauthenticated public URL when publishing. The
# handler (get_assets) is GET-only and path-traversal safe (_find_asset_file
# rejects .. / separators and only returns real files under assets_dir).
# /api/extension is public: it serves the browser-extension zip as a plain <a href>
# download from the account page, which carries no credential. The bundle is
# non-sensitive public code (destined for the Chrome Web Store); the route is GET-only.
# /api/user/linkedin-cookie is public because the browser extension POSTs to it cross-origin from
# linkedin.com, so it carries neither a bearer token nor the LEM cookie. It is self-authenticating:
# the handler validates the user's own LEM session_token in the body and 401s if it's invalid — same
# model as the /api/auth/ endpoints. This exact leaf path only; the rest of /api/user/* stays gated.
# /api/faq is public: it serves the published front-page FAQ (issue #506) to logged-out visitors on
# the landing page. GET-only, no user data — same shape as /api/app-info.
# /api/flags is public for the SAME reason (issue #651): the landing page bootstraps its feature
# flags from it while logged out, so it carries no credential at all. Gating it would 401 the flags
# query, and the SPA's axios interceptor treats ANY 401 as a dead session — it clears lem_session
# and redirects, so a signed-in visitor hitting the landing page would be silently logged out.
# GET-only; it returns the registry's own toggle values, and the optional session_token is
# self-authenticating (an invalid one resolves the "system" identity rather than erroring) — same
# model as /api/user/linkedin-cookie.
# /api/brand-showcase is public: it serves the LEM brand account's own published posts and stored
# engagement counts to the front-page showcase (issue #1299). Read-only, no user data, same shape as
# /api/faq. It is also gated by a feature flag inside the handler, so the default-off posture keeps
# the route harmless until the owner enables it.
# /api/docs, /api/redoc and /api/openapi.json are the docs surface moved in from the FastAPI
# defaults (issue #1020). They are LEAF entries, not "/api/docs/" subtrees: the non-slash branch
# below already matches path-segment children, so "/api/docs/oauth2-redirect" is covered while a
# future "/api/docs-admin" is not. They stay public because they were public before the move and
# gating them would leave the SPA's own API undocumented for every non-bearer caller — what the
# move buys is that the admin surface is no longer IN the schema (see _hide_admin_routes_from_schema).
_PUBLIC_API_PREFIXES = ("/api/auth/", "/api/billing/webhook", "/api/assets",
                        "/api/linkedin/verification-pin", "/api/linkedin/comment-notification",
                        "/api/app-info", "/api/faq", "/api/flags", "/api/brand-showcase",
                        "/api/extension/", "/api/user/linkedin-cookie",
                        "/api/docs", "/api/redoc", "/api/openapi.json")


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


def _has_session_credential(request: Request) -> bool:
    """Did the caller bring something the SESSION resolver can judge?

    Presence only — whether it resolves is the handler's call, and it fails closed there. The
    middleware runs before routing and has no database, so validating here would mean a second
    session lookup on every request that answers nothing the route does not already answer.

    ONE shape: the `lem_session` cookie. Normally the httpOnly one the login response set; in the
    SPA's cookie-less fallback (#1611) it is the same token written from script, because a browser
    that refused a `Secure` cookie still authenticates at the route on the `session_token` field and
    would otherwise be refused HERE, before the resolver it would have satisfied ever ran. Either
    way this reads a cookie and judges nothing — see `docs/identity-and-sessions.md`.

    `X-Session-Token` used to count here too and was
    removed in #1357, because it was the one credential in this check that could never become a
    user: `get_session_user_id` resolves an explicit token from the `session_token` FIELD and has
    never read that header, so a caller carrying only the header cleared this gate and was then
    401'd by the route — the failure reading as "my token is wrong" rather than "that header is not
    wired up". A presence check for something no resolver reads is weaker than it looks, not a
    fallback. The non-browser credential is `API_ACCESS_TOKENS` (#950), checked by the caller below.
    """
    return bool((request.cookies.get(SESSION_COOKIE_NAME) or "").strip())


@app.middleware("http")
async def api_token_middleware(request: Request,
                               call_next: Callable[[Request], Awaitable[Response]]) -> Response:
    """The edge credential filter described above: did the caller bring A credential at all?

    Inactive when `API_ACCESS_TOKENS` is unset (local/dev and the unit suite run open) and on every
    path outside `/api/` or inside `_PUBLIC_API_PREFIXES`. It is deliberately weak — one arbitrary
    cookie byte clears it — because it is NOT authorisation; `require_session_user_id()` in the
    handler is, and it fails closed.
    """
    if _api_token_required(request.url.path):
        token = _bearer_token(request.headers.get("Authorization"))
        if token not in _API_ACCESS_TOKEN_SET and not _has_session_credential(request):
            return JSONResponse(status_code=401, content={"status_code": 401, "detail": "Unauthorized"})
    return await call_next(request)


# ---------------------------------------------------------------------------
# Session resolution (issue #745, phase 2b)
#
# The session token now lives in an httpOnly cookie, so a script on the page cannot read it. Every
# handler still calls get_session_user_id(...) with whatever the caller sent, and THIS wrapper
# decides: an explicit token that resolves wins (non-browser callers, the LinkedIn OAuth state
# round trip, the tutorial capture harness), otherwise the request's cookie is used. The SPA sends
# the non-secret sentinel COOKIE_SESSION_SENTINEL in the field so ~150 existing call sites keep
# their shape while holding no secret at all.
#
# The cookie is read off a ContextVar rather than threaded through every signature — a handler
# that never took a Request object still gets cookie auth.
# ---------------------------------------------------------------------------

COOKIE_SESSION_SENTINEL = "cookie"

# The ContextVars below are MODULE state, and that is exactly why the module may only ever be
# imported under this one name (issue #1354). `/app` and `/app/src` are both importable, so
# `src.cqc_lem.api.main` loads this same file a SECOND time as a distinct module object with its own
# ContextVars. Serve the app from that copy — `uvicorn src.cqc_lem.api.main:app`, which is what the
# start script used to say — and `session_cookie_middleware` publishes the cookie on one copy's
# ContextVar while every `api/routers/*.py` handler reads the other's. Cookie auth then returns None
# for every router-served route while the routes still defined here keep working, so sign-in
# succeeds and everything after it 401s. Nothing raises; the only signal is this tripwire.
_CANONICAL_MODULE = "cqc_lem.api.main"


def _guard_canonical_module() -> bool:
    """CRITICAL if this module was imported under any name but `cqc_lem.api.main` (#1354).

    Returns True when the import is canonical, so the check is testable without reading logs.
    """
    if __name__ in (_CANONICAL_MODULE, "__main__"):
        return True
    log_critical(
        "api.main imported under a non-canonical module name — cookie auth will not resolve",
        module_name=__name__, canonical=_CANONICAL_MODULE)
    return False


_request_session_cookie: ContextVar[Optional[str]] = ContextVar("lem_session_cookie", default=None)
_request_path: ContextVar[Optional[str]] = ContextVar("lem_request_path", default=None)
_request_object: ContextVar[Optional[Request]] = ContextVar("lem_request", default=None)
# The scope this request actually resolved on, stamped by the resolver. `/auth/session` reports the
# enrolment hold off THIS, so the browser is never told something the server would then contradict.
_request_session_scope: ContextVar[Optional[str]] = ContextVar("lem_session_scope", default=None)

_guard_canonical_module()


@app.middleware("http")
async def session_cookie_middleware(request: Request, call_next):
    """Publish the session cookie, the path and the Request on ContextVars for this request only.

    This is what lets ~150 handlers that never took a `Request` still authenticate on the httpOnly
    cookie. Every var is reset in `finally` — leaking one into the next request on a reused worker
    would hand that caller someone else's session.
    """
    cookie_reset = _request_session_cookie.set(request.cookies.get(SESSION_COOKIE_NAME))
    # The request rides alongside for the SAME reason the cookie does: the scope check below belongs
    # to the one resolver every handler already calls, and most handlers never took a Request. The
    # path decides the verdict; the client is what makes the audit row worth reading.
    path_reset = _request_path.set(request.url.path)
    request_reset = _request_object.set(request)
    scope_reset = _request_session_scope.set(None)
    try:
        return await call_next(request)
    finally:
        _request_session_cookie.reset(cookie_reset)
        _request_path.reset(path_reset)
        _request_object.reset(request_reset)
        _request_session_scope.reset(scope_reset)


# Registered LAST on purpose, which makes it the OUTERMOST middleware: Starlette runs the most
# recently added one first, and `api_token_middleware` answers a credential-less /api request with
# its own 401 without ever calling `call_next`. Registered where it is defined, that refusal — the
# response every request gets in production before the caller signs in — would leave the origin
# with no `Cache-Control` at all, which is the one thing this middleware exists to prevent.
app.middleware("http")(api_cache_control_middleware)


# ---------------------------------------------------------------------------
# Session SCOPES (issue #745, phase 2c.1 — issue #905)
#
# `full` and `recovery` sessions are the browser's and reach everything. The other two are held to
# a named surface, and both holds are enforced HERE, in the one resolver, rather than at each call
# site — a narrowing that has to be remembered at ~150 handlers is a narrowing that leaks.
#
#   extension — minted by POST /user/extension-token behind a step-up. Until 2c.1 it was an
#               ordinary full session that merely ALSO satisfied the step-up gate at the cookie
#               endpoint, so a stolen extension token could read every post, every DM template and
#               every setting the SPA can. It now reaches exactly the one path the extension calls.
#   enroll    — a PIN login that landed past REQUIRE_STRONG_FACTOR_AFTER on an account with no
#               strong factor (design §7 Stage 2). Signed in, so nobody is locked out, but it may
#               only add a factor and save recovery codes until it does — at which point
#               `release_enrollment_scope` promotes it to `full`.
#
# The set of UNRESTRICTED scopes is named explicitly (`_UNRESTRICTED_SCOPES`) rather than inferred
# from "has no surface entry". Those are the same thing today and stop being the same thing the
# first time someone adds a scope and forgets the surface — and this whole file argues that a rule
# you have to remember somewhere else is a rule that leaks. A legacy row carrying NULL is the 2b
# session and reads as `full`; anything else unrecognised is refused, not waved through.
# ---------------------------------------------------------------------------

# Exactly one path: `browser_extension/popup.js` POSTs li_at and nothing else. Adding an entry here
# hands every extension token — including a stolen one — whatever that endpoint can do.
_EXTENSION_SESSION_SURFACE = frozenset({"/user/linkedin-cookie"})

# Everything the forced-enrolment screen needs and nothing else: who am I, sign me out, the boot
# payloads the SPA fetches before any page renders, and the enrolment ceremonies themselves.
# `/user/security` and `/user/step-up/*` are deliberately absent — there is nothing to prove yet.
_ENROLL_SESSION_SURFACE = frozenset({
    "/auth/session", "/auth/logout", "/app-info", "/flags",
    "/user/auth-factors",
    "/user/passkeys/register/begin", "/user/passkeys/register/complete",
    "/user/totp/enroll/begin", "/user/totp/enroll/confirm",
    "/user/recovery-codes/regenerate",
})

# Everything a headless agent needs to QUEUE work and read its own results — and nothing else
# (issue #1026). It reads the four review queues plus the settings that decide whether queueing is
# even safe, and creates pending items. Absent by design: every credential path
# (`/user/linkedin-cookie`, `/user/linkedin-password`, `/user/passkeys/*`, `/user/totp/*`,
# `/user/recovery-codes/*`), the account-mover (`/user/email/change/*`), `/user/sessions/revoke`,
# and `/user/extension-token` + `/user/agent-token` themselves — a stolen agent token must not be
# able to mint its successor or lock the owner out.
_AGENT_SESSION_SURFACE = frozenset({
    # read the queues
    "/connection_requests", "/outreach/targets", "/dms", "/lead_signals", "/leads",
    "/catchup/touches",
    # read the state that decides whether loading is safe at all
    "/user/engagement-preferences", "/user/automation-status", "/dashboard/stats",
    "/user/linkedin-profile-skills",
    # create pending work + save drafts for a human to approve
    "/connection_request", "/outreach/target", "/schedule_dm", "/lead_signal", "/lead",
})

_SCOPE_SURFACES: Dict[str, frozenset[str]] = {
    SESSION_SCOPE_EXTENSION: _EXTENSION_SESSION_SURFACE,
    SESSION_SCOPE_ENROLL: _ENROLL_SESSION_SURFACE,
    SESSION_SCOPE_AGENT: _AGENT_SESSION_SURFACE,
}

# The browser's own two sessions, and the ONLY scopes that reach everything. A NULL scope — every
# row written before 2c — resolves to `full` before this is consulted, so 2b sessions are untouched.
_UNRESTRICTED_SCOPES = frozenset({SESSION_SCOPE_FULL, SESSION_SCOPE_RECOVERY})

_SCOPE_REFUSAL_CODE = {
    SESSION_SCOPE_EXTENSION: "session_scope_forbidden",
    SESSION_SCOPE_AGENT: "session_scope_forbidden",
    # The SPA reads this one and renders the enrolment gate instead of a dead page.
    SESSION_SCOPE_ENROLL: "enrollment_required",
}


def _scope_path(path: Optional[str]) -> Optional[str]:
    """The surface key for a request path.

    The router is mounted under `/api`, and a handful of routes (the LinkedIn OAuth redirect
    targets, `/health`, the SPA catch-all) also sit at the root — so the `/api` prefix is stripped
    and both spellings map to one entry. Matching is exact set membership, never a prefix, so
    `/user/auth-factors` cannot open a future `/user/auth-factors-admin`.
    """
    if not path:
        return None
    if path == "/api":
        return "/"
    if path.startswith("/api/"):
        path = path[len("/api"):]
    return path.rstrip("/") or "/"


def _scope_allows(scope: Optional[str], path: Optional[str]) -> bool:
    """Pure path check. Whether an `enroll` hold is still WARRANTED is a separate question with a
    different input (the account, not the path) and is answered in `_scope_checked`.

    Both unknowns fail CLOSED, and for the same reason. An unknown PATH means a restricted token
    reached a handler by a route this middleware never saw. An unknown SCOPE means a value nobody
    taught this table about — a typo, a hand-edited row, or a scope some later phase added and
    only half wired up — and granting it everything by omission would make the table itself the
    opt-in thing this design exists to avoid.
    """
    effective = scope or SESSION_SCOPE_FULL
    if effective in _UNRESTRICTED_SCOPES:
        return True
    surface = _SCOPE_SURFACES.get(effective)
    if surface is None:
        log_warning("Session carries an unrecognised scope — refusing", scope=effective)
        return False
    return _scope_path(path) in surface


def _scope_refusal(scope: str) -> HTTPException:
    """403, never 401: the SPA's axios interceptor reads any 401 as a dead session and signs the
    user out, which would turn "finish enrolling" into "you have been logged out".
    """
    return HTTPException(status_code=403, detail={
        "code": _SCOPE_REFUSAL_CODE.get(scope, "session_scope_forbidden"),
        "message": ("Finish setting up two-factor sign-in to use the rest of LEM."
                    if scope == SESSION_SCOPE_ENROLL
                    else "This session is not allowed to use that."),
    })


# ---------------------------------------------------------------------------
# CSRF — the custom-header layer (issue #957)
#
# `X-LEM-Client` IS NOT A SECRET. Its value is a constant in a public bundle and it is meant to be:
# the mechanism is that a cross-origin HTML form cannot set a request header AT ALL, whatever the
# value would have been, and setting one from `fetch()` triggers a preflight that has nothing to
# succeed against (no CORS middleware is installed). So presence is the entire check — comparing the
# value would buy nothing and would invite the next reader to treat it as a token, rotate it, and
# put it in `.env`.
#
# It replaces a layer the SPA lost. Until #950 the bundle shipped a bearer token, which was worthless
# as ACCESS control (every visitor held it) but real as a CSRF layer for exactly this reason. Four
# mutating routes take query parameters and no body — `/create_weekly_content/`,
# `/invite_to_li_company_page/`, `/aws_test_get_my_profile/`, `/automate_reply_commenting` — so the
# "a JSON body needs a preflight" layer never covered them, and `SameSite=Lax` was left holding them
# alone. `Lax` holds; one layer is still one layer, and a new query-parameter route inherits it.
#
# Query parameters are not the only shape that layer misses, which is why this gate is scoped to
# EVERY state-changing cookie-authenticated request and not to those four. A cross-origin caller can
# also produce `multipart/form-data` with no preflight, and `/user/newsletter-draft/cover` and
# `/avatar/training` (the latter spends an avatar credit) take exactly that. Narrowing this back to a
# route list would uncover them.
#
# Two deliberate narrowings:
#
#   * **State-changing methods only.** CSRF is a forged WRITE — with no CORS the attacker cannot read
#     a response, so a forged GET buys nothing. Requiring it on reads would also break the browser's
#     own credentialed GETs that carry no headers at all: a plain `<a href>` download or an <img> src.
#   * **A bearer-authenticated caller is exempt.** Scripts, Postman and the admin tooling are not
#     browsers and have no ambient credential to forge with. It also makes the rollout breakage-free:
#     an SPA bundle cached from before #950 still sends a bearer and no header. This exemption is for
#     NON-BROWSER callers and outlives the stale-bundle rollout it also happens to cover; it is not a
#     temporary shim to remove once the caches turn over.
#
# Enforced in the ONE resolver, on the cookie branch, for the same reason the scope narrowing is
# (#905): the credential this defends against is the one the BROWSER attaches by itself, so the check
# belongs where that credential is read, not at ~150 call sites where it would be forgotten once.
#
# The layer depends on the SPA being same-origin with the API, and it is by construction, not by
# deployment: the axios client's `baseURL` is the RELATIVE `/api`, so every request it makes is same
# origin whatever the host — dev server, docker-compose, the prod nginx edge — and a custom header
# on a same-origin request is never preflighted. `ui/src/api/client.test.ts`'s "the baseURL is
# relative" pins that, and `test_no_cors_middleware_is_installed` pins the other
# half: CORS with credentials would let a real cross-origin caller ask permission for this header and
# reinstate the hole the layer closes.
# ---------------------------------------------------------------------------

CLIENT_HEADER_NAME = "X-LEM-Client"

_CSRF_UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def _bearer_authenticated(request: Optional[Request]) -> bool:
    """Did this caller present a configured non-browser bearer token?

    False when no tokens are configured at all: nothing is a valid bearer then, so nothing is
    exempt — a deployment that runs the gate open must not also opt out of the CSRF layer.
    """
    if request is None or not _API_ACCESS_TOKEN_SET:
        return False
    return _bearer_token(request.headers.get("Authorization")) in _API_ACCESS_TOKEN_SET


def _csrf_refusal() -> HTTPException:
    """403, never 401 — same reason as `_scope_refusal`: the SPA's axios interceptor reads any 401
    as a dead session and signs the user out, so a stale bundle would log people out rather than
    tell them to reload.
    """
    return HTTPException(status_code=403, detail={
        "code": "client_header_required",
        "message": f"This request must be sent by the LEM app ({CLIENT_HEADER_NAME} header missing).",
    })


def _require_client_header() -> None:
    """Refuse a state-changing request that authenticated on the session COOKIE and did not come
    from the SPA. Read-only requests and non-browser bearer callers pass straight through.
    """
    request = _request_object.get()
    if request is None:
        # `session_cookie_middleware` sets this ContextVar and the cookie one in the SAME block, so a
        # live HTTP request can never carry the cookie without the request (pinned by
        # `test_the_request_and_cookie_contextvars_are_set_together`). Reaching here therefore means a
        # caller that resolved a session outside HTTP — a Celery beat, a direct call, a test — and
        # there is no cross-site forgery without a cross-site request. DEBUG rather than a warning
        # precisely because that is the EXPECTED shape for every non-HTTP caller.
        log_debug("Client-header check skipped — no HTTP request in scope",
                  path=_scope_path(_request_path.get()))
        return
    if request.method.upper() not in _CSRF_UNSAFE_METHODS:
        return
    if request.headers.get(CLIENT_HEADER_NAME):
        return
    if _bearer_authenticated(request):
        return
    # A warning, and the recurrence escalation (utilities/CLAUDE.md) is wanted here: a cookie-
    # authenticated write that no LEM client sent is either a forged cross-site request or a bundle
    # stale enough to be broken, and both are worth a look. The message is a stable template so the
    # grouping key holds; the path rides as context.
    log_warning("Refused a cookie-authenticated write with no client header",
                path=_scope_path(_request_path.get()), method=request.method.upper())
    raise _csrf_refusal()


def _explicit_token(session_token: Optional[str]) -> Optional[str]:
    """The caller-supplied token, or None when they presented the cookie sentinel instead."""
    if not session_token or session_token == COOKIE_SESSION_SENTINEL:
        return None
    return session_token


def current_session_token(session_token: Optional[str] = None) -> Optional[str]:
    """The token this request is actually authenticated by — resolved in the SAME order as
    get_session_user_id, so the two can never name different sessions for one request.

    An explicit token only wins when it RESOLVES. get_session_user_id falls through a stale explicit
    token to the cookie, so returning that stale token here would mean logout deletes nothing (the
    live session row outlives the logout) and "sign out all other devices" revokes the caller's OWN
    live session, having failed to match it as the one to keep. The resolve is skipped when there is
    no cookie to fall through to — a non-browser caller costs no extra query.
    """
    explicit = _explicit_token(session_token)
    cookie_token = _request_session_cookie.get()
    if explicit and (not cookie_token or _db_get_session_user_id(explicit)):
        return explicit
    return cookie_token or explicit


def get_session_user_id(session_token: Optional[str] = None) -> Optional[int]:
    """Resolve the caller's user id from the explicit token or the httpOnly session cookie.

    Wraps `db.resolve_session`, which is the only thing that touches the sessions table. An
    explicit token that does NOT resolve falls through to the cookie rather than 401ing: a browser
    holding a stale token from before the cutover is still the signed-in person on that cookie.

    A session that resolves but is SCOPED away from this path raises 403 rather than falling
    through (2c.1): falling through would let a request that carried both a restricted token and a
    full cookie be served on the cookie, which is the narrowing quietly not happening.

    A session that resolves off the COOKIE on a state-changing request must also carry the SPA's
    client header (#957) — the cookie is the one credential a browser attaches by itself, so it is
    the only one a cross-site form can forge with. An explicit token is not: the attacker would have
    to know it, and it is httpOnly.
    """
    explicit = _explicit_token(session_token)
    if explicit:
        resolved = _db_resolve_session(explicit)
        if resolved:
            return _scope_checked(resolved, explicit)
    cookie_token = _request_session_cookie.get()
    if cookie_token and cookie_token != explicit:
        resolved = _db_resolve_session(cookie_token)
        if resolved:
            # Before the scope check, and before anything that writes: a forged request should reach
            # no audit row, no enrolment promotion and no handler.
            _require_client_header()
            return _scope_checked(resolved, cookie_token)
    return None


def _enrollment_held() -> bool:
    """Is THIS request's session the held, enrolment-only kind (2c.1)?

    Read off the ContextVar the resolver already stamped, NOT a fresh lookup. What the SPA is told
    and what the server enforces have to be the same verdict from the same read: deciding it again
    here would let a DB hiccup answer "not held" to the browser while every request it then made
    was refused — the app rendering over a wall of 403s, which is the exact page the gate exists to
    prevent.

    Takes no token on purpose. An earlier draft accepted one and discarded it "for call-site
    shape", which in an auth module is a signature that lies: it would answer about the CURRENT
    request no matter whose token you passed.
    """
    return _request_session_scope.get() == SESSION_SCOPE_ENROLL


def _release_hold(token: str) -> bool:
    """Best effort. The hold is re-decided from the ACCOUNT on every request, so a promotion that
    does not land costs one extra query next time, never access — which is why this is caught at
    all. It is still a write that should have worked, so it warns rather than whispers.
    """
    try:
        return release_enrollment_scope(token)
    except Exception as e:
        log_warning(f"Could not release enrollment scope: {e}")
        return False


def _scope_checked(resolved: Dict[str, Any], token: Optional[str]) -> int:
    scope = resolved.get("scope") or SESSION_SCOPE_FULL
    path = _request_path.get()
    user_id = resolved["user_id"]

    if scope == SESSION_SCOPE_ENROLL and not enrollment_required(user_id):
        # **The hold belongs to the ACCOUNT, not to this session row.** Deciding it from the row
        # alone strands every OTHER device: enrol on the laptop and the phone still holds a row
        # that says `enroll`, while the account now HAS a factor — so enrolling again is step-up
        # gated and the step-up ceremony is outside the enrolment surface. That is a dead end with
        # no way out but signing out. Re-asking the account also covers the rollout being pulled
        # back (cleared date, date moved forward, `STRONG_AUTH_ENABLED=false`), and a promotion
        # that failed to write. Promote the row so this costs one extra query once, not forever.
        if token:
            _release_hold(token)
        _request_session_scope.set(SESSION_SCOPE_FULL)
        return user_id

    if _scope_allows(scope, path):
        _request_session_scope.set(scope)
        return user_id
    if scope == SESSION_SCOPE_EXTENSION:
        # Audited AND warned, for the extension scope only. The extension calls exactly one path
        # (`browser_extension/popup.js` POSTs li_at and nothing else), so this cannot happen by
        # accident — it means someone else is holding that token, which is the one thing here worth
        # waking someone for. A held ENROLMENT session, by contrast, produces these constantly and
        # harmlessly while the SPA settles, so it stays DEBUG and unaudited: warning on an expected
        # no-op would file a defect for working behaviour and bury this row when it matters.
        log_warning("Extension session refused outside its scope", user_id=user_id,
                    path=_scope_path(path))
        http_request = _request_object.get()
        _safe_auth_event(AuthAuditEvent.SESSION_SCOPE_DENIED, user_id=user_id, success=False,
                         ip=_client_ip(http_request), user_agent=_user_agent(http_request),
                         details={"scope": scope, "path": _scope_path(path)})
    else:
        log_debug("Session refused outside its scope", user_id=user_id, scope=scope)
    raise _scope_refusal(scope)


def _safe_auth_event(event: "AuthAuditEvent", **kwargs) -> None:
    """The audit row must never be the reason a refusal turns into a 500 — the refusal IS the
    control, the row is the record of it. Losing the record still matters: this row is the only
    signal that someone else may be holding an extension token, so a failed write warns.
    """
    try:
        record_auth_event(event, **kwargs)
    except Exception as e:
        log_warning(f"Could not record auth event: {e}")


def require_session_user_id(session_token: Optional[str] = None) -> int:
    """The acting user, or 401 — the ONE way an `/api` handler learns who is calling (issue #914).

    Until this landed, a set of routes read the acting user out of an `email` / `user_id` REQUEST
    PARAMETER, behind nothing but the shared bearer token the SPA ships in its build. That token is
    known to anyone who loads the page, so those routes were "name an account, act on it". A
    parameter now only ever names a TARGET, and a target has to be authorised against this.
    """
    user_id = get_session_user_id(session_token)
    if not user_id:
        # DEBUG, not WARNING: sessions expire in the ordinary course of things and the SPA polls,
        # so warning here would escalate working behaviour into a filed defect
        # (utilities/CLAUDE.md). A denied TARGET below is the opposite — see _deny().
        log_debug("Rejected an /api call with no resolvable session")
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    return user_id


def _deny(reason: str, user_id: int, **context: Any) -> NoReturn:
    """403 on an authorised-caller-wrong-target, logged AND audited as the anomaly it is.

    A caller who resolves a session and then names ANOTHER account is either a broken client or
    somebody working the hole #914 closed, and neither is an expected no-op — so this warns, and
    the recurrence escalation (utilities/CLAUDE.md) turning a repeat into a filed defect is the
    behaviour we want. The message stays a stable template so the dedup key groups.

    It also writes an `auth_audit_log` row, for the same reason `_scope_checked` does: a log line is
    greppable, an audit row is queryable per account, and "who has been naming other people's
    accounts" is a question you ask about ONE user after the fact. Only the KIND of identifier and
    the path go in — never the caller-supplied value, which is somebody else's address.
    """
    log_warning(f"Rejected a foreign target on /api: {reason}", user_id=user_id, **context)
    http_request = _request_object.get()
    _safe_auth_event(AuthAuditEvent.FOREIGN_TARGET_DENIED, user_id=user_id, success=False,
                     ip=_client_ip(http_request), user_agent=_user_agent(http_request),
                     details={"target": reason, "path": _scope_path(_request_path.get())})
    raise HTTPException(status_code=403, detail="Forbidden")


def _reject_foreign_email(user_id: int, email: Optional[str]) -> None:
    """A caller may name their OWN address as the target and nobody else's.

    The parameter is redundant now — the session already decided whose data this is — but the SPA
    and the legacy clients still send it, and answering a mismatch with the CALLER's data would be a
    silent substitution. 403 says what happened.
    """
    named = (email or "").strip().lower()
    if not named:
        return
    if named != (get_user_email(user_id) or "").strip().lower():
        # The address itself is never logged — it is the caller-supplied half and the audit log is
        # not the place to accumulate other people's addresses.
        _deny("email", user_id)


def _reject_foreign_user_id(user_id: int, target_user_id: Optional[int]) -> None:
    """Same rule as `_reject_foreign_email`, for the routes that name the target by id."""
    if target_user_id is None:
        return
    try:
        named = int(target_user_id)
    except (TypeError, ValueError):
        # A non-numeric target is unauthorisable, so it fails closed rather than 500ing. FastAPI
        # coerces today's query parameters; the helper must hold on its own for a body model.
        _deny("user_id (unparseable)", user_id)
    if named != user_id:
        _deny("user_id", user_id)


def _require_own_posts(user_id: int, post_ids: List[int]) -> None:
    """403 unless every named post belongs to the caller.

    Deliberately NOT a 404 per id: telling an attacker which ids exist is the enumeration this
    endpoint used to hand out for free.

    A database fault answers 503, not 403. The action is refused either way — that is the
    fail-closed half — but "you may not touch these posts" and "we could not find out" are different
    facts and a security-shaped 403 for an infrastructure outage sends the user and on-call in the
    wrong direction, on top of filing a defect through `_deny`'s recurrence escalation.
    """
    try:
        owns = user_owns_posts(user_id, post_ids)
    except OwnershipUnprovable:
        # `db.user_owns_posts` already logged the fault with the exception attached.
        raise HTTPException(status_code=503, detail="Could not verify post ownership — try again")
    if not owns:
        _deny("post_ids", user_id)


def _client_ip(request: Optional[Request]) -> Optional[str]:
    """The caller's address behind the Cloudflare tunnel + nginx edge.

    CF-Connecting-IP first, and this ordering is the security-relevant part: Cloudflare sets that
    header on every request it proxies and OVERWRITES whatever the client sent, so it is the one
    value here an attacker cannot choose. X-Forwarded-For cannot be trusted the same way — a proxy
    APPENDS to the chain the client supplied, so its first entry is attacker-controlled, and reading
    it as the client would let a single host reset its own per-IP auth limit with one header and
    write a forged ip_hash into the audit log. It stays only as the fallback for a deployment with
    no Cloudflare in front, where nothing else knows the original address.
    """
    if request is None:
        return None
    cf_ip = (request.headers.get("CF-Connecting-IP") or "").strip()
    if cf_ip:
        return cf_ip
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        first = forwarded.split(",")[0].strip()
        if first:
            return first
    return request.client.host if request.client else None


def _user_agent(request: Optional[Request]) -> Optional[str]:
    return request.headers.get("User-Agent") if request is not None else None


_ui_dist = os.path.join(os.path.dirname(__file__), "..", "ui", "dist")


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
    UTC offset.
    """
    if dt is None:
        return None
    if isinstance(dt, str):
        try:
            dt = datetime.fromisoformat(dt.replace("Z", "+00:00"))
        except ValueError:
            return dt
    dt = dt.replace(tzinfo=timezone.utc) if getattr(dt, "tzinfo", None) is None else dt.astimezone(timezone.utc)
    return dt.isoformat().replace("+00:00", "Z")


def _warn_if_naive_schedule(dt, endpoint: str, **context) -> None:
    """A scheduling write whose datetime carries NO offset is a client bug, and a silent one: the
    storage layer assumes naive means UTC (db.to_naive_utc), so a wall clock the user picked in
    their own zone is stored verbatim and the post fires offset-hours away from the time they saw
    (issue #774 — a 9am post published at 5am). We keep interpreting it as UTC (the contract, and
    what every legacy caller relies on) but leave a breadcrumb, because today the only evidence is
    the wrong publish time itself.
    """
    if dt is not None and getattr(dt, "tzinfo", None) is None:
        log_warning(
            f"Naive scheduled_datetime received by {endpoint} — assuming UTC "
            f"(clients must send an explicit-UTC ISO string; see docs/timezone-contract.md)",
            **context,
        )


def _public_post_url(value) -> Optional[str]:
    """Only surface real http(s) permalinks. Home-feed comments have no LinkedIn permalink and are
    logged under a synthetic 'feedpost://<hash>' dedup key — never expose that raw string to the UI.
    """
    if isinstance(value, str) and value.lower().startswith(("http://", "https://")):
        return value
    return None


class PostRequest(BaseModel):
    """Body of both `POST /schedule_post/` and `PUT /update_post/{post_id}` — compose and edit.

    `email` is a TARGET the handler authorises against the session, never the caller's identity
    (issue #914).
    """

    session_token: SessionTokenField = None
    content: str
    video_url: Optional[str] = None
    post_type: Optional[PostType] = PostType.TEXT
    scheduled_datetime: datetime
    email: Optional[str] = None
    status: Optional[PostStatus] = PostStatus.PENDING
    carousel_slides: Optional[List[str]] = None
    # None = no compose-time choice, so the user's per-content-type avatar opt-ins decide
    # (issue #744). True/False is an explicit override for this post.
    use_avatar: Optional[bool] = None
    video_quality: Optional[str] = "standard"  # standard | premium | premium_top
    rejection_reason: Optional[str] = Field(default=None, max_length=1000)
    # A compose-time image the author uploaded or generated BEFORE the row existed (issue #1030).
    # Only a preview URL we issued to this caller is accepted — see `owns_post_image_url`.
    image_url: Optional[str] = Field(default=None, max_length=1000)


class VideoCreditCheckoutRequest(BaseModel):
    """Body of `POST /video/credits/checkout`.

    `package` is validated against `stripe_util.VIDEO_CREDIT_PACKAGES` in the handler (400), not here.
    """

    session_token: str
    package: str        # "small" | "medium" | "large" | "max"
    success_url: str
    cancel_url: str


class UpgradeVideoRequest(BaseModel):
    """Body of `POST /video/upgrade`.

    `post_id` is a target the handler authorises (403 if it is not the session user's post) and the tier decides how
    many video credits the render will cost.
    """

    session_token: str
    post_id: int
    tier: str = "premium"  # "premium" (1 credit) or "premium_top" (3 credits)


class BulkUpdateRequest(BaseModel):
    """Body of `POST /posts/bulk_update/`.

    Every id in `post_ids` is a target the handler proves the session owns (`_require_own_posts`, fails closed)
    before any of them is touched.
    """

    session_token: SessionTokenField = None
    post_ids: List[int]
    status: Optional[PostStatus] = None
    scheduled_datetime: Optional[datetime] = None


class BulkDeleteRequest(BaseModel):
    """Body of `DELETE /posts/` — a SOFT delete.

    `rejection_reason` is the author's own words on why the draft was no good, and is fed back in when the post is
    later regenerated (issue #713).
    """

    session_token: SessionTokenField = None
    post_ids: List[int]
    rejection_reason: Optional[str] = Field(default=None, max_length=1000)


class TrialExtendRequest(BaseModel):
    """Claim the early-adopter extended trial (issue #499)."""
    session_token: str


# Input length limits — kept in lockstep with the DB column widths (see migrations) so an over-long
# value returns a clean 422 here instead of a MySQL 1406 that silently rolls back the whole upsert.
# The rest of this block went to the /api/user router with the models that read it.
_LEN_DM_RECIPIENT_URL = 512   # scheduled_dms.recipient_profile_url VARCHAR(512)
_LEN_DM_RECIPIENT_NAME = 255  # scheduled_dms.recipient_name VARCHAR(255)
_LEN_CONNECT_NOTE = 300       # LinkedIn caps a connection-request note at 300 chars
_LEN_RECIPIENT_EMAIL = 255    # connection_requests.recipient_email VARCHAR(255)
_LEN_FEEDBACK_BODY = 5000     # feedback.body (TEXT; app cap)
_LEN_FEEDBACK_TYPE_HINT = 32  # feedback.type_hint VARCHAR(32)
# Screenshots ride along inside feedback.context_json as a data URL. Capped so one report can't
# blow past max_allowed_packet; the widget downsizes/rejects before it gets here.
_LEN_FEEDBACK_SCREENSHOT = 2_000_000
_LEN_FEEDBACK_CONTEXT = 8000  # serialized auto-attached context, screenshot excluded
# Shape check only — RFC 5322 is not the job here, just catching a typo'd address before it is
# typed into LinkedIn's own Connect dialog (issue #1836). No `email-validator` dependency: it is
# only a `pydantic[email]` extra, not installed in this project.
_RECIPIENT_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _validate_recipient_email_shape(v: Optional[str]) -> Optional[str]:
    """Shared by `ConnectionRequestCreate`/`Update` — reject a MALFORMED address as a 422.

    An empty or whitespace-only string is an ABSENT address, not a malformed one, and is normalised
    to None so it behaves exactly like an omitted key (issue #1836). A caller that has no address
    for this row must be able to say so without being punished with a 422 — and on the PUT, None
    means "leave this column alone", so `""` can never blank an address a human already saved.
    """
    if v is None:
        return None
    v = v.strip()
    if not v:
        return None
    if not _RECIPIENT_EMAIL_RE.match(v):
        raise ValueError(f"'{v}' does not look like an email address")
    return v


class ScheduleDmRequest(BaseModel):
    """Body of `POST /schedule_dm`.

    `status` is the approval gate: `pending` is a draft a human still has to release, `approved` queues it for
    `auto_check_scheduled_dms` to actually send — and an `agent`-scoped session is refused the `approved` value
    outright.
    """

    session_token: str
    recipient_profile_url: str = Field(max_length=_LEN_DM_RECIPIENT_URL)
    recipient_name: Optional[str] = Field(default=None, max_length=_LEN_DM_RECIPIENT_NAME)
    message: str = Field(max_length=_LEN_DM_TEMPLATE)
    scheduled_datetime: datetime
    status: str = "pending"  # 'pending' (draft) or 'approved' (queue for send)


class UpdateDmRequest(BaseModel):
    """Body of `PUT /dm`.

    `action` is separate from the editable fields on purpose: `approve` is what releases the DM to be sent, and it
    is refused for an `agent`-scoped session.
    """

    session_token: str
    dm_id: int
    recipient_profile_url: Optional[str] = Field(default=None, max_length=_LEN_DM_RECIPIENT_URL)
    recipient_name: Optional[str] = Field(default=None, max_length=_LEN_DM_RECIPIENT_NAME)
    message: Optional[str] = Field(default=None, max_length=_LEN_DM_TEMPLATE)
    scheduled_datetime: Optional[datetime] = None
    action: Optional[str] = None  # 'approve' | 'cancel' | None (save fields only)


class DmDeleteRequest(BaseModel):
    """Body of `DELETE /dm`.

    The delete is SOFT — the row moves to `canceled` so it is never sent, and the history of what was drafted
    survives.
    """

    session_token: str
    dm_id: int


class ConnectionRequestCreate(BaseModel):
    """Body of `POST /connection_request` (issue #398) — add ONE proactive connect target.

    The invite itself rides the existing rate-limited drip and the shared daily invite cap; this
    only queues a row. Volume prospecting is not what this is for.
    """

    session_token: str
    recipient_profile_url: str = Field(max_length=_LEN_DM_RECIPIENT_URL)
    recipient_name: Optional[str] = Field(default=None, max_length=_LEN_DM_RECIPIENT_NAME)
    message: Optional[str] = Field(default=None, max_length=_LEN_CONNECT_NOTE)  # optional connect note
    # None → follow the user's connection_request_mode (auto_approve queues it, pre_review holds it as a
    # draft). An explicit 'pending' or 'approved' overrides that; any other value is rejected (422).
    status: Optional[str] = None
    # Known ONLY when LinkedIn's Connect dialog turns out to be the email-verification variant
    # (issue #1836) — most targets never carry one. Never echoed back by GET /connection_requests.
    recipient_email: Optional[str] = Field(default=None, max_length=_LEN_RECIPIENT_EMAIL)

    @field_validator("recipient_email")
    @classmethod
    def _recipient_email_shape(cls, v: Optional[str]) -> Optional[str]:
        return _validate_recipient_email_shape(v)


class ConnectionRequestUpdate(BaseModel):
    """Body of `PUT /connection_request`.

    `approve` is the release-to-send action and is refused for an `agent`-scoped session; an unrecognised action is
    a 422 rather than a silent field-only save. `retry` (issue #1735) re-queues a `failed` request the same way
    `approve` does, and is refused on any other status.
    """

    session_token: str
    request_id: int
    recipient_profile_url: Optional[str] = Field(default=None, max_length=_LEN_DM_RECIPIENT_URL)
    recipient_name: Optional[str] = Field(default=None, max_length=_LEN_DM_RECIPIENT_NAME)
    message: Optional[str] = Field(default=None, max_length=_LEN_CONNECT_NOTE)
    action: Optional[str] = None  # 'approve' | 'cancel' | 'retry' | None (save fields only)
    # Lets a human supply the email a 'failed' Class C row is missing, ahead of an action='retry'
    # (issue #1836).
    recipient_email: Optional[str] = Field(default=None, max_length=_LEN_RECIPIENT_EMAIL)

    @field_validator("recipient_email")
    @classmethod
    def _recipient_email_shape(cls, v: Optional[str]) -> Optional[str]:
        return _validate_recipient_email_shape(v)


class ConnectionRequestDelete(BaseModel):
    """Body of `DELETE /connection_request` — a SOFT cancel to `canceled`.

    Nothing is sent, and the record of what was queued survives.
    """

    session_token: str
    request_id: int


# LinkedIn Catch-up touches (issue #482) — approval-gated milestone congratulations
_LEN_CATCHUP_NAME = 255     # catchup_touches.person_name VARCHAR(255)
_LEN_CATCHUP_MESSAGE = 1000  # catchup_touches.message (TEXT; app cap — a DM is refined to ≤300)


class UpdateCatchupTouchRequest(BaseModel):
    """Body of `PUT /catchup/touch` (issue #482).

    `approve` queues the congratulations for the daily-capped drip, and the handler refuses to approve a touch with
    no message — an empty one would be turned into a permanent SKIPPED by the sender.
    """

    session_token: str
    touch_id: int
    person_name: Optional[str] = Field(default=None, max_length=_LEN_CATCHUP_NAME)
    message: Optional[str] = Field(default=None, max_length=_LEN_CATCHUP_MESSAGE)
    action: Optional[str] = None  # 'approve' | 'cancel' | None (save fields only)


class CatchupTouchDeleteRequest(BaseModel):
    """Body of `DELETE /catchup/touch`.

    Soft: the row stays as the dedup tombstone for that milestone, so cancelling a touch does not invite it to be
    re-drafted tomorrow.
    """

    session_token: str
    touch_id: int


class GenerateCarouselPreviewRequest(BaseModel):
    """Body of `POST /generate-carousel` — render slide images for a caller to attach to a post.

    A preview only: the slides come back as URLs for `carousel_slides`, and nothing is scheduled
    or stored against a post here.
    """

    session_token: str
    stage: str = "awareness"  # awareness | consideration | decision | personal
    template: Optional[str] = None  # None = auto-pick by stage


class FeedbackRequest(BaseModel):
    """In-app feedback / bug report (issue #496). session_token is optional: the widget is offered
    to logged-out visitors too, and those land with a NULL user_id.
    """
    body: str = Field(min_length=1, max_length=_LEN_FEEDBACK_BODY)
    session_token: SessionTokenField = None
    source: str = str(FeedbackSource.WIDGET)
    type_hint: Optional[str] = Field(default=None, max_length=_LEN_FEEDBACK_TYPE_HINT)
    context: Optional[Dict[str, Any]] = None
    screenshot: Optional[str] = Field(default=None, max_length=_LEN_FEEDBACK_SCREENSHOT)

    @field_validator("body")
    @classmethod
    def body_not_blank(cls, v: str) -> str:
        """Reject whitespace-only feedback.

        `min_length=1` alone lets a lone space through, and a blank row costs a human a triage decision that has
        nothing in it to decide.
        """
        if not v.strip():
            raise ValueError("Feedback body cannot be empty")
        return v.strip()

    @field_validator("source")
    @classmethod
    def known_source(cls, v: str) -> str:
        """Reject an unknown source rather than coercing it.

        `feedback.source` is a MySQL ENUM, so a value invented by a caller would be a write failure deeper in —
        and `/api/feedback` is unauthenticated, which makes this field an untrusted string.
        """
        valid = {str(s) for s in FeedbackSource}
        if v not in valid:
            raise ValueError(f"Unknown source '{v}' — expected one of {sorted(valid)}")
        return v


class NpsSurveyRequest(BaseModel):
    """An NPS answer (issue #501): the 0-10 score plus the free-text 'why'. Bounds mirror
    `utilities.surveys.NPS_MIN/NPS_MAX`.
    """
    session_token: str
    score: int = Field(ge=0, le=10)
    why: Optional[str] = Field(default=None, max_length=_LEN_FEEDBACK_BODY)
    survey_key: Optional[str] = Field(default=None, max_length=_LEN_FEEDBACK_TYPE_HINT)
    context: Optional[Dict[str, Any]] = None


class ReviewSurveyRequest(BaseModel):
    """A review (issue #501): a 1-5 rating, 'what would make this a 10?', an optional public
    testimonial and the consent flag that says we may quote it. Submitting one satisfies the
    extended-trial gate (issue #499).
    """
    session_token: str
    rating: int = Field(ge=1, le=5)
    improvement: Optional[str] = Field(default=None, max_length=_LEN_FEEDBACK_BODY)
    testimonial: Optional[str] = Field(default=None, max_length=_LEN_FEEDBACK_BODY)
    consent_testimonial: bool = False
    survey_key: Optional[str] = Field(default=None, max_length=_LEN_FEEDBACK_TYPE_HINT)
    context: Optional[Dict[str, Any]] = None


class SurveyDismissRequest(BaseModel):
    """Body of `POST /survey/dismiss` — the user closed the modal without answering.

    Recording the ask is what stops BOTH the modal and the email bringing it back (issue #501); a
    dismissal is a real answer to "should we ask again", just not to the survey.
    """

    session_token: str
    survey_key: str = Field(min_length=1, max_length=_LEN_FEEDBACK_TYPE_HINT)


class PostHogSurveyRequest(BaseModel):
    """A PostHog Surveys answer relayed by the SPA (issue #653). `kind` says which of the two LEM
    surveys answered — the score bounds are the KIND's, checked in the handler, because 0-10 and 1-5
    can't both be a field constraint. `survey_id`/`survey_name` are PostHog's own, kept so a
    `feedback` row can be lined up against the `survey sent` event the browser already emitted.
    """
    session_token: str
    kind: str = Field(min_length=1, max_length=_LEN_FEEDBACK_TYPE_HINT)
    score: int = Field(ge=0, le=10)
    comment: Optional[str] = Field(default=None, max_length=_LEN_FEEDBACK_BODY)
    survey_id: Optional[str] = Field(default=None, max_length=64)
    survey_name: Optional[str] = Field(default=None, max_length=128)
    context: Optional[Dict[str, Any]] = None


class ShippedNoticeAckRequest(BaseModel):
    """Acknowledging a "you asked, we shipped" notice (issue #502). `resolved` is the micro-CSAT:
    True/False answers "did this fix it?", None means the user just dismissed the notice.
    """
    session_token: str
    notice_id: int = Field(gt=0)
    resolved: Optional[bool] = None
    comment: Optional[str] = Field(default=None, max_length=_LEN_FEEDBACK_BODY)


class FutureForwardValues(IntEnum):
    """An INDEX into `automate_reply_commenting`'s backoff ladder, not a number of seconds.

    The task maps 0-5 onto `[0, 5m, 10m, 15m, 30m, 60m]` and steps the index up itself on each
    re-queue, so a caller passing 5 starts the sweep at the widest spacing. Constrained to an enum
    because an out-of-range value would index past that list.
    """

    Zero = 0
    ONE = 1
    TWO = 2
    THREE = 3
    FOUR = 4
    FIVE = 5


@app.get("/health")
def health_check():
    """Liveness for the blue/green flip and the Cloudflare tunnel. Deliberately trivial: it gates
    every deploy, so it must never depend on Redis, MySQL or Celery being reachable.
    """
    return {"status": "healthy"}


@app.get("/health/deep")
def health_check_deep():
    """Readiness of the things `/health` deliberately ignores — for external monitors.

    `/health` returning 200 while the entire Celery tier was in `Created` is exactly how the
    v0.118.0 outage stayed invisible for four hours: the API was genuinely fine, and nothing an
    uptime monitor could reach knew that automation was dead. This endpoint answers the question a
    monitor actually wants to ask.

    Never raises and never 503s on a partial: an external monitor should read `status`, and a
    scrape that can't tell must report `unknown` rather than a confident wrong answer.

    A worker being PRESENT is not the same as a worker WORKING. `maint begin` cancels every queue
    consumer, so a stuck maintenance mode leaves the whole tier registered, answering, and
    consuming nothing — observed live during the v0.120.0 deploy as `healthy` with empty lane
    lists. Registration was never the question a monitor is asking; consumption is. So `healthy`
    requires at least one worker actually subscribed to at least one queue.

    `maintenance` is reported alongside so a degraded reading is legible rather than mysterious:
    during a deploy it is the expected cause, and outside one it is the thing to go clear. A
    DECLARED window is not an outage, so it does not degrade the status either — see below.
    """
    lanes: dict = {}
    status = "healthy"
    consuming = 0
    maintenance = None
    try:
        from cqc_lem.utilities.maintenance import _inspect
        # active_queues() reaches every worker over the broker's control channel, so a lane whose
        # container was never started simply isn't in the reply — which is the signal we want.
        replies = _inspect().active_queues() or {}
        for worker, queues in replies.items():
            lanes[worker] = sorted(q.get("name", "?") for q in (queues or []))
        consuming = sum(1 for names in lanes.values() if names)
        if consuming == 0:
            # Covers BOTH "no workers answered" and "workers answered but consume nothing".
            # They are the same outage from a monitor's point of view: no task will run.
            status = "degraded"
    except Exception as e:
        log_warning("Deep health check could not reach the Celery control channel", exc=e)
        status = "unknown"             # unmeasured is never "healthy"

    # Best-effort and deliberately outside the block above: Redis being unreadable must not
    # downgrade a control-channel answer we already trust. None = "could not tell", never False.
    try:
        from cqc_lem.utilities.maintenance import is_maintenance_mode
        maintenance = bool(is_maintenance_mode())
    except Exception:
        maintenance = None

    # A DECLARED maintenance window is not an outage. `maint begin` cancels EVERY lane's consumer
    # at once (not one at a time, so the "one live consumer" tolerance above does not cover it),
    # and scripts/deploy.sh runs it on every release — four windows a day. Without this, the
    # monitor documented in docs/stack-watchdog.md would fire on every successful deploy, and an
    # alert that cries wolf on every deploy is one that gets muted. The suppression is bounded by
    # the flag's own TTL (deploy sets 1800s; `maint end` deletes it), so a maintenance mode that
    # never lifts still goes `degraded` once the flag expires — that IS the state worth waking
    # someone for. Only `degraded` is suppressed: an unreadable control channel stays `unknown`,
    # and unreadable Redis (None, never False) never suppresses anything.
    if status == "degraded" and maintenance is True:
        status = "healthy"

    # `lanes` is computed but NOT returned (issue #1020): this endpoint is unauthenticated by
    # design — an external dead-man's switch cannot carry a credential — and the per-worker map was
    # the one field with disclosure value, naming container IDs and the internal queue topology.
    # `workers` and `consuming` are bare counts derived from it and say nothing about the topology,
    # and `consuming` is what DECIDES `status`, so a degraded reading stays explicable without it.
    #
    # Key order matters as much as the key set: `status` is first and FastAPI preserves dict
    # insertion order, so dropping the last key leaves the literal `"status":"healthy"` that
    # docs/stack-watchdog.md pins as a monitor contract byte-identical.
    return {"status": status, "workers": len(lanes), "consuming": consuming,
            "maintenance": maintenance}


@router.get("/app-info")
def get_app_info() -> ResponseModel[dict[str, Any]]:
    """Public: the SPA footer reads the running release version + whether to display it."""
    from cqc_lem.utilities.env_constants import SHOW_VERSION_FOOTER, get_app_version
    return ResponseModel(status_code=200, detail={
        "version": get_app_version(),
        "show_version": SHOW_VERSION_FOOTER,
    })


def _bounded_context(context: Optional[Dict[str, Any]]) -> Optional[dict]:
    """Client-supplied context for a feedback/survey row, dropped when a caller tries to write an
    unbounded JSON blob — the widget and the survey modal only send a handful of fields.
    """
    payload: Dict[str, Any] = dict(context or {})
    if len(json.dumps(payload, default=str)) > _LEN_FEEDBACK_CONTEXT:
        payload = {"truncated": True}
    return payload or None


@router.post("/feedback")
def submit_feedback_endpoint(request: FeedbackRequest) -> ResponseModel[dict[str, Any]]:
    """Capture in-app feedback / a bug report (issue #496) — the first capture point of the
    feedback->auto-work loop. A valid session_token attributes the row to that user; without one
    (logged-out visitor) the row is kept anonymously with a NULL user_id.
    """
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
def submit_nps_endpoint(request: NpsSurveyRequest) -> ResponseModel[dict[str, Any]]:
    """Capture an NPS response (issue #501) as a `feedback` row with source='nps'. Promoters get
    invited to turn that score into a review, which is what unlocks the extended trial (#499).
    """
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
def submit_review_endpoint(request: ReviewSurveyRequest) -> ResponseModel[dict[str, Any]]:
    """Capture a review (issue #501) as a `feedback` row with source='review'. That row IS the gate
    the extended trial checks (issue #499), so the response reports the unlock.
    """
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
def dismiss_survey_endpoint(request: SurveyDismissRequest) -> ResponseModel[dict[str, Any]]:
    """User closed the survey modal without answering — record the ask so neither the modal nor the
    email brings it back (issue #501).
    """
    user_id = get_session_user_id(request.session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")

    from cqc_lem.utilities.surveys import dismiss_survey
    return ResponseModel(status_code=200,
                         detail={"dismissed": dismiss_survey(user_id, request.survey_key)})


@router.post("/survey/posthog")
def submit_posthog_survey_endpoint(request: PostHogSurveyRequest) -> ResponseModel[dict[str, Any]]:
    """Capture a PostHog Surveys answer (issue #653) as a `feedback` row so it reaches the
    feedback->auto-work loop. The browser has already emitted PostHog's own `survey sent`; this
    handler deliberately does NOT emit the homegrown `survey_response` event, so one answer is
    counted once.
    """
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


@router.get("/flags")
def get_feature_flags(session_token: Optional[str] = None) -> ResponseModel[dict[str, Any]]:
    """Server-evaluated feature flags for the SPA (issue #651, docs/feature-flags.md).

    This is the SPA's flag BOOTSTRAP: values are resolved server-side with PostHog local evaluation
    (or the env fallback) and shipped in one payload, so the browser renders the right thing on the
    FIRST paint instead of flickering while a client-side flag request lands — and so the SPA, the
    API and the Celery workers can never disagree about a flag's value.

    An invalid or absent session resolves the SAME flags for the `"system"` identity rather than
    401ing: the landing page is logged out and still needs to know what to render.
    """
    from cqc_lem.utilities.flags import bootstrap_payload
    user_id = get_session_user_id(session_token) if session_token else None
    return ResponseModel(status_code=200, detail=bootstrap_payload(user_id))


@router.get("/faq")
def faq_endpoint() -> ResponseModel[dict[str, Any]]:
    """Public: the front-page FAQ (issue #506). Serves only the published entries, in display
    order — the SPA falls back to its built-in copy if this is empty or unreachable.
    """
    from cqc_lem.utilities.db import get_published_faq_entries
    return ResponseModel(status_code=200, detail={
        "entries": [{"id": e.get("id"), "question": e.get("question"), "answer": e.get("answer"),
                     "updated_at": _utc_iso(e.get("updated_at"))}
                    for e in get_published_faq_entries()],
    })


_BRAND_SHOWCASE_LIMIT = 6
_BRAND_SHOWCASE_WINDOW_SECONDS = 60
_BRAND_SHOWCASE_CACHE_TTL_SECONDS = 300


def _brand_showcase_rate_limit_key(ip: Optional[str]) -> str:
    return f"brand-showcase:rate:{ip or 'unknown'}"


def _brand_showcase_cache_key() -> str:
    return "brand-showcase:posts"


def _brand_showcase_posts(brand_user_id: int) -> List[dict]:
    """Build the curated list of brand posts with their stored stats.

    Filters to `status='posted'` via `get_posted_posts`, so drafts or errored rows can never
    reach the public endpoint. Numbers are read straight from `post_stats` and passed through
    unchanged; the UI displays them exactly as received.
    """
    raw_posts = get_posted_posts(brand_user_id)
    if raw_posts is None:
        raise RuntimeError("Could not read brand posts")

    posts: List[dict] = []
    for row in reversed(raw_posts[-_BRAND_SHOWCASE_LIMIT:]):
        post_id = row.get("id")
        if not isinstance(post_id, int):
            continue
        stats = get_latest_post_stats(brand_user_id, post_id)
        posts.append({
            "id": post_id,
            "content": row.get("content") or "",
            "post_type": row.get("post_type") or PostType.TEXT.value,
            "published_at": _utc_iso(row.get("scheduled_time")),
            "post_url": get_post_url_from_log_for_user(brand_user_id, post_id) or None,
            "reactions": stats.get("reactions") if stats else None,
            "comments": stats.get("comments") if stats else None,
            "reposts": stats.get("reposts") if stats else None,
            "impressions": stats.get("impressions") if stats else None,
            "saves": stats.get("saves") if stats else None,
        })
    return posts


@router.get("/brand-showcase", responses={
    200: {"description": "Brand posts returned"},
    503: {"description": "Database unavailable"},
})
def brand_showcase_endpoint(request: Request) -> ResponseModel[dict[str, Any]]:
    """Public: real posts and stored engagement counts from the LEM brand account (issue #1299).

    Returns only posts already published by the brand user and only stats already recorded in
    `post_stats`. A feature flag gates the endpoint so it stays harmless by default; when disabled
    or when there is nothing to show it returns a 200 with an empty list. Database faults return
    503, matching the identity-and-sessions posture.
    """
    import mysql.connector

    from cqc_lem.utilities.brand_account import brand_user_id
    from cqc_lem.utilities.flags import BRAND_SHOWCASE, flag_enabled
    from cqc_lem.utilities.linkedin.rate_limit import shared_redis_client

    if not flag_enabled(BRAND_SHOWCASE):
        return ResponseModel(status_code=200, detail={"posts": []})

    client = shared_redis_client()
    ip = _client_ip(request)

    if client is not None:
        try:
            count = int(client.get(_brand_showcase_rate_limit_key(ip)) or 0)
            if count >= 30:
                return ResponseModel(status_code=200, detail={"posts": []})
            pipe = client.pipeline()
            rate_key = _brand_showcase_rate_limit_key(ip)
            pipe.incr(rate_key)
            pipe.expire(rate_key, _BRAND_SHOWCASE_WINDOW_SECONDS)
            pipe.execute()
        except Exception as exc:
            log_debug("Brand showcase rate-limit check skipped", exc=exc)

    if client is not None:
        try:
            cached = client.get(_brand_showcase_cache_key())
            if cached:
                posts = json.loads(cached)
                return ResponseModel(status_code=200, detail={"posts": posts})
        except Exception as exc:
            log_debug("Brand showcase cache read skipped", exc=exc)

    try:
        posts = _brand_showcase_posts(brand_user_id())
    except mysql.connector.Error as exc:
        log_error("Brand showcase could not read brand posts", exc=exc)
        raise HTTPException(status_code=503, detail="Database unavailable")
    except Exception as exc:
        log_error("Brand showcase failed", exc=exc)
        raise HTTPException(status_code=503, detail="Could not build brand showcase")

    if client is not None:
        try:
            client.setex(_brand_showcase_cache_key(), _BRAND_SHOWCASE_CACHE_TTL_SECONDS,
                         json.dumps(posts, default=str))
        except Exception as exc:
            log_debug("Brand showcase cache write skipped", exc=exc)

    return ResponseModel(status_code=200, detail={"posts": posts})


@router.post("/shipped/ack")
def ack_shipped_notice_endpoint(request: ShippedNoticeAckRequest) -> ResponseModel[dict[str, Any]]:
    """Acknowledge a shipped-fix notice and, when the user answered "did this fix it?", record the
    micro-CSAT (issue #502). A "not fixed" answer lands as a `feedback` row at status `new`, so it
    re-enters the auto-work loop instead of stopping at a metric.
    """
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
    200: {"description": "Dashboard stats returned", "model": ResponseModel[DashboardStats]},
    **{k: v for k, v in error_responses.items() if k in [401, 403]}
})
def get_dashboard_stats(session_token: Optional[str] = None,
                        email: Optional[str] = None) -> ResponseModel[dict[str, Any]]:
    """The dashboard's headline counters.

    `email` is a target checked against the session, not an identity — passing someone else's is a 403.
    """
    user_id = require_session_user_id(session_token)
    _reject_foreign_email(user_id, email)

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
    200: {"description": "Planned tasks returned", "model": ResponseModel[PlannedTasksDetail]},
    **{k: v for k, v in error_responses.items() if k in [401, 403]}
})
def get_planned_tasks_endpoint(session_token: Optional[str] = None,
                               email: Optional[str] = None,
                               limit: int = Query(default=10, ge=1, le=50)) -> ResponseModel[dict[str, Any]]:
    """What LEM is about to do next for this user — the forward half of the dashboard.

    Spans every queue, with `kind` saying which. Times go out as explicit-UTC ISO so the browser
    localizes them instead of reading a naive value as local.
    """
    user_id = require_session_user_id(session_token)
    _reject_foreign_email(user_id, email)

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
    200: {"description": "Recent activity log returned",
          "model": ResponseModel[List[ActivityEntry]]},
    **{k: v for k, v in error_responses.items() if k in [401, 403]}
})
def get_activity(session_token: Optional[str] = None, email: Optional[str] = None,
                 limit: int = 20) -> ResponseModel[list[dict[str, Any]]]:
    """The activity feed — what LEM already did, newest first.

    `post_url` goes through `_public_post_url`, so a home-feed comment (which has no LinkedIn
    permalink and is logged under a synthetic `feedpost://` dedup key) reports None rather than
    leaking that internal string into the UI.
    """
    user_id = require_session_user_id(session_token)
    _reject_foreign_email(user_id, email)

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
async def linkedin_verification_pin_inbound(request: Request) -> ResponseModel[str]:
    """SendGrid Inbound Parse webhook: the user's email reply carrying their LinkedIn
    6-digit code. The tokenized Reply-To (pin+<token>@parse-domain) attributes it to the
    paused login; we extract the code and hand it to the waiting task. Always 200 so
    SendGrid doesn't retry-storm on a malformed/unrelated message.
    """
    try:
        form = await request.form()
    except Exception:
        return ResponseModel(status_code=200, detail="ignored")
    # Everything past the form parse is blocking — MySQL and Redis on every path, and on the
    # Gmail-confirmation branch two 15s `requests.get` plus an SMTP send. This handler has to stay
    # `async def` to await the form, so the blocking span goes to the threadpool that a plain `def`
    # endpoint would have got for free, instead of stalling the event loop for up to 30s.
    return await run_in_threadpool(_handle_inbound_parse, form)


def _handle_inbound_parse(form) -> ResponseModel[str]:
    """The synchronous body of the SendGrid Inbound Parse webhook (see the route above)."""
    to_field = str(form.get("to") or "")
    envelope = str(form.get("envelope") or "")
    # SendGrid Inbound Parse routes ALL mail for the parse host to this ONE URL, so this endpoint
    # must also handle the reply+<token> traffic (Gmail forwarding confirmations + comment
    # notifications), not only pin+<token> PIN replies. Dispatch by the address prefix.
    from cqc_lem.integrations.linkedin.notification_email import extract_reply_token_from_address
    if extract_reply_token_from_address(to_field) or extract_reply_token_from_address(envelope):
        return _process_reply_inbound(form)
    text = str(form.get("text") or form.get("html") or "")
    subject = str(form.get("subject") or "")
    token = extract_token_from_address(to_field) or extract_token_from_address(envelope)
    pin = extract_pin_from_text(text) or extract_pin_from_text(subject)
    if not token or not pin:
        # Mail addressed to neither pin+ nor reply+ lands here (catch-all spam, misdirected
        # forwards) — the one bucket that used to vanish without a trace.
        _log_inbound_verdict("no_pin_token" if not token else "no_pin_in_text", form)
        return ResponseModel(status_code=200, detail="ignored")
    user_id = submit_pin_by_token(token, pin)
    _log_inbound_verdict("pin_accepted" if user_id else "pin_ignored", form, user_id=user_id)
    if user_id:
        log_info("Received LinkedIn verification PIN via email reply", user_id=user_id)
    return ResponseModel(status_code=200, detail="accepted" if user_id else "ignored")


_GMAIL_CONFIRM_KEY = "linkedin:gmail_forward_confirm:{user_id}"
# What goes stale is the CODE fallback, not the fact that Gmail asked us to verify the address. The
# record itself is what the UI reads to tell "never started" apart from "waiting on the first
# forwarded email", and neither the Gmail filter nor the user's progress through it expires — an
# expiring record put a fine setup back on "Forwarding not confirmed yet" (issue #813). So no record
# carries a TTL; only the code is dropped once it is too old to be worth offering.
_GMAIL_CODE_FRESH_SECONDS = 7 * 24 * 60 * 60


def _store_gmail_forward_confirmation(user_id: int, record: "dict", quiet: bool = False) -> bool:
    """Persist forwarding status; True when something was written. Confirmed records are never
    downgraded by a later pending one — evidence that the chain worked does not stop being evidence.
    Pass quiet=True on per-email paths so one Redis outage can't turn into a warning flood.
    """
    try:
        from cqc_lem.utilities.linkedin.rate_limit import _redis_client
        client = _redis_client()
        if client is None:
            return False
        if not record.get("confirmed"):
            if (get_gmail_forward_confirmation(user_id) or {}).get("confirmed"):
                return False
            if record.get("code"):
                record = {**record, "code_expires_at": int(time.time()) + _GMAIL_CODE_FRESH_SECONDS}
        client.set(_GMAIL_CONFIRM_KEY.format(user_id=user_id), json.dumps(record))
        return True
    except Exception as e:
        # A repeated log_warning is re-emitted at ERROR and files a defect (utilities/CLAUDE.md), so
        # the path that runs on every inbound email must not warn per email.
        (log_debug if quiet else log_warning)(
            "Could not store Gmail forwarding confirmation result", exc=e, user_id=user_id)
        return False


def _record_forwarding_confirmed_by_delivery(user_id: int) -> None:
    """A LinkedIn notification arriving at the user's private reply+<token> address is end-to-end
    proof the forwarding chain works — stronger proof than our server-side click of Gmail's verify
    link, which routinely fails from a datacenter IP. Without this, a user who finished Gmail's
    confirmation by hand (the path we explicitly ask them to take when the auto-click fails) stayed
    at confirmed=False forever and kept being told replies would never fire (issue #813).
    """
    if (get_gmail_forward_confirmation(user_id) or {}).get("confirmed"):
        return
    # Only announce a real state change — with Redis down the store is a no-op, and logging per
    # inbound email would turn one unavailable dependency into a flood.
    if _store_gmail_forward_confirmation(user_id, {"confirmed": True, "source": "forwarded_email"},
                                         quiet=True):
        log_info("Gmail forwarding confirmed by an arriving LinkedIn notification", user_id=user_id)


def _handle_gmail_forwarding_confirmation(user_id: int, subject: str, text: str, html: str) -> ResponseModel[str]:
    """Auto-confirm the user's Gmail forwarding to our address: click the verify link server-side
    and stash the numeric code + status so the UI can show it as a fallback if the auto-click didn't
    take. Always 200.
    """
    from cqc_lem.integrations.linkedin.notification_email import (
        extract_gmail_confirmation_code,
        extract_gmail_confirmation_url,
    )
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
                except Exception as e:
                    log_debug("Nested Gmail confirmation check failed", exc=e, user_id=user_id)
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
    _store_gmail_forward_confirmation(user_id, {
        "code": code, "confirmed": confirmed, "url_found": bool(url),
        "forwarded_to_user": forwarded, **({"source": "auto_click"} if confirmed else {}),
    })
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
        record = json.loads(raw.decode() if isinstance(raw, (bytes, bytearray)) else raw)
        # The record outlives the code on purpose: keep reporting where the user got to, but stop
        # offering a code Gmail no longer accepts.
        expires_at = record.get("code_expires_at") if isinstance(record, dict) else None
        if expires_at and time.time() > expires_at:
            record.pop("code", None)
        return record
    except Exception:
        return None


def _reply_sweep_debounced(user_id: int, window_s: int = 120) -> bool:
    """True the first time we see this user within window_s — collapses a burst of comment
    notifications into ONE reply sweep. Fails OPEN (returns True) if Redis is unavailable so a
    notification is never silently dropped.
    """
    try:
        from cqc_lem.utilities.linkedin.rate_limit import _redis_client
        client = _redis_client()
        if client is None:
            return True
        return bool(client.set(f"linkedin:reply_sweep_debounce:{user_id}", "1", nx=True, ex=window_s))
    except Exception:
        return True


def _log_inbound_verdict(verdict: str, form, user_id: "Optional[int]" = None) -> None:
    """One log line + one PostHog event per inbound parse email saying what we did with it. The
    webhook ignores most mail BY DESIGN, which made a broken forwarding chain indistinguishable from
    no mail at all — weeks of 100%-silently-dropped traffic looked like "feature never used". The
    raw (truncated) address fields are included so a token/format mismatch is visible from the log.
    """
    to_field = str(form.get("to") or "")[:160]
    envelope = str(form.get("envelope") or "")[:240]
    from_field = str(form.get("from") or "")[:120]
    subject = str(form.get("subject") or "")[:120]
    log_info(f"Inbound parse email verdict={verdict} to={to_field!r} envelope={envelope!r} "
             f"from={from_field!r} subject={subject!r}", user_id=user_id)
    try:
        from cqc_lem.utilities.observability import track_inbound_email
        track_inbound_email(verdict, user_id=user_id)
    except Exception as e:
        log_debug("Could not track inbound email verdict", exc=e, user_id=user_id)


def _process_reply_inbound(form) -> ResponseModel[str]:
    """Handle inbound mail sent to a reply+<token>@parse-domain address: a Gmail forwarding
    confirmation (auto-click the verify link) or a forwarded LinkedIn comment notification (trigger a
    debounced recent-posts reply sweep). Reactions/unknown tokens are ignored. Always 200. Called
    from BOTH inbound endpoints because SendGrid Inbound Parse posts all parse-host mail to one URL.
    """
    from cqc_lem.integrations.linkedin.notification_email import (
        extract_reply_token_from_address,
        is_comment_notification,
        is_gmail_forwarding_confirmation,
        is_linkedin_notification,
    )
    from cqc_lem.utilities.db import get_user_id_by_reply_token
    to_field = str(form.get("to") or "")
    envelope = str(form.get("envelope") or "")
    from_field = str(form.get("from") or "")
    subject = str(form.get("subject") or "")
    text = str(form.get("text") or "")
    html = str(form.get("html") or "")
    token = extract_reply_token_from_address(to_field) or extract_reply_token_from_address(envelope)
    if not token:
        _log_inbound_verdict("no_reply_token", form)
        return ResponseModel(status_code=200, detail="ignored")
    user_id = get_user_id_by_reply_token(token)
    if not user_id:
        _log_inbound_verdict("unknown_reply_token", form)
        return ResponseModel(status_code=200, detail="ignored")
    # Gmail forwarding confirmation: the address is ours + token-gated, so auto-click the verify link.
    if is_gmail_forwarding_confirmation(from_field, subject, text or html):
        _log_inbound_verdict("gmail_confirmation", form, user_id=user_id)
        return _handle_gmail_forwarding_confirmation(user_id, subject, text, html)
    comment = is_comment_notification(subject, text or html)
    # Record the proof BEFORE the comment/reaction split — a forwarded reaction email shows the
    # forwarding rule is live just as well as a comment one does, and the status chip is about the
    # chain working, not about this particular email being actionable (issue #813).
    if comment or is_linkedin_notification(from_field, subject, text or html):
        _record_forwarding_confirmed_by_delivery(user_id)
    if not comment:
        _log_inbound_verdict("linkedin_not_comment" if is_linkedin_notification(
            from_field, subject, text or html) else "unrelated", form, user_id=user_id)
        return ResponseModel(status_code=200, detail="ignored")
    if not _reply_sweep_debounced(user_id):
        _log_inbound_verdict("debounced", form, user_id=user_id)
        return ResponseModel(status_code=200, detail="debounced")
    # slot 0 is the single-shot trigger (the golden-hour amplifier owns the other slots). Omitting it
    # once raised KeyError at enqueue and 500'd every forwarded notification; cqc_lem.app.queue_once
    # now fills the default into the dedup key, so this stays explicit for meaning, not for safety.
    sweep_reply_comments.apply_async(kwargs={"user_id": user_id, "sweep_slot": 0}, countdown=120)
    _log_inbound_verdict("comment_accepted", form, user_id=user_id)
    log_info("Triggered reply sweep from comment notification", user_id=user_id)
    return ResponseModel(status_code=200, detail="accepted")


@router.post("/linkedin/comment-notification/inbound")
async def linkedin_comment_notification_inbound(request: Request) -> ResponseModel[str]:
    """SendGrid Inbound Parse webhook for reply+<token> mail (kept as an explicit path; SendGrid
    actually delivers to the shared parse URL, which also routes here via _process_reply_inbound).
    """
    try:
        form = await request.form()
    except Exception:
        return ResponseModel(status_code=200, detail="ignored")
    # Same reasoning as the shared parse route: _process_reply_inbound is blocking.
    return await run_in_threadpool(_process_reply_inbound, form)


@router.post("/automate_reply_commenting", responses={
    200: {"description": "Post reply automation scheduled successfully"},
    **{k: v for k, v in error_responses.items() if k in [401, 403, 404]}
})
def automate_reply_commenting_for_post_id(post_id: int, session_token: Optional[str] = None,
                                          loop_for_duration: int = 60 * 60,
                                          future_forward: FutureForwardValues = Query(
                                              default=0,
                                              description="Forward index (0-5) to use for future calls",
                                              examples=[0, 1, 2, 3, 4, 5]
                                          )) -> ResponseModel[str]:
    """Queue a reply-commenting sweep over one of the caller's OWN posts.

    `post_id` is a target, not an identity: it used to name the account the Selenium session ran as,
    which let any bearer holder point one at somebody else's post (issue #914).
    """
    # The post used to name the account this ran as, so any bearer holder could point a Selenium
    # session at somebody else's post (issue #914). It names a target now.
    user_id = require_session_user_id(session_token)
    _require_own_posts(user_id, [post_id])

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
    500: {"description": "Session resolved to an account with no address"},
    **{k: v for k, v in error_responses.items() if k in [401, 403, 404]}
})
def schedule_post(post: PostRequest) -> ResponseModel[str]:
    """Create a post row from the composer — draft (`pending`) or queued (`approved`).

    Two things the body does NOT get to decide: the row is written against the SESSION's address
    read back from the account, and a compose-time `image_url` is kept only when it is a preview WE
    issued to this account (issue #1030) — anything else is dropped, never stored.
    """
    user_id = require_session_user_id(post.session_token)
    _reject_foreign_email(user_id, post.email)
    # The row is written against the SESSION's address, never the body's — a target that passed the
    # check is by definition the same string, and reading it back from the account is what keeps
    # that true if the check ever moves (issue #914).
    email = get_user_email(user_id)
    if not email:
        # 500, not 403. The session RESOLVED, so this caller is who they say they are; an account
        # with a live session and no address is our inconsistency, and calling it "Forbidden" tells
        # the user they lack permission to their own account while hiding a data fault.
        log_error("Session resolved to a user with no email address", user_id=user_id)
        raise HTTPException(status_code=500, detail="Could not read the account address")

    _warn_if_naive_schedule(post.scheduled_datetime, "/schedule_post/", user_id=user_id)

    # A compose-time image is a URL the CALLER hands us for a field the publish step later fetches,
    # so it is accepted only when it is a preview WE issued to this account (issue #1030) — never
    # an arbitrary URL, and never another user's preview. Anything else is dropped, not stored.
    image_url = post.image_url if owns_post_image_url(user_id, post.image_url) else None
    if post.image_url and not image_url:
        log_warning("Refused a post image URL that is not this account's preview",
                    user_id=user_id, action_type="post_image")

    # SPA-created posts carry an explicit status: "Approve & Schedule" → approved,
    # "Save Draft" → pending. Auto-generated content sets its own status elsewhere.
    if insert_post(email, post.content, post.scheduled_datetime, post.post_type,
                   video_url=post.video_url, carousel_slides=post.carousel_slides,
                   video_quality=post.video_quality or "standard",
                   status=post.status or PostStatus.PENDING,
                   use_avatar=post.use_avatar, image_url=image_url):
        return ResponseModel(status_code=200, detail="Post scheduled successfully")
    else:
        raise HTTPException(status_code=404, detail="Could not schedule post")


@router.post("/create_weekly_content/", responses={
    200: {"description": "Weekly content created successfully"},
    500: {"description": "Could not queue content generation"},
    **{k: v for k, v in error_responses.items() if k in [401, 403]}
})
def create_weekly_content(session_token: Optional[str] = None,
                          user_id: Optional[int] = None) -> ResponseModel[str]:
    """Kick off a plan-then-generate chain for the CALLER.

    A `queued` progress record is published up front so the SPA has something to poll immediately.

    `user_id` is a target to authorise, never the account the work runs as: it used to BE the
    authorisation, so a bearer holder could spend somebody else's LLM budget (issue #914). If
    dispatch fails the `queued` record is cleared, because a SPA polling a run that will never
    start looks identical to one that is merely slow.
    """
    # `user_id` used to BE the authorisation — a bearer holder could spend another account's LLM
    # budget and fill their calendar with drafts (issue #914). It is a target now, and the work runs
    # as `caller_id`. Two names for one account is how this endpoint got here, so the parameter is
    # NOT reassigned: below this line `user_id` is only ever the thing that was authorised, and
    # `caller_id` is the only thing that reaches a task.
    caller_id = require_session_user_id(session_token)
    _reject_foreign_user_id(caller_id, user_id)

    # Generation runs for minutes in the background, so publish a 'queued' progress record now —
    # the SPA polls /content_generation_status/ and would otherwise show nothing (issue #545).
    mark_queued(caller_id)

    # Chain: plan posts for the rest of the month first, then fill content for this week.
    # This ensures the user always has PLANNING rows before content generation runs.
    try:
        celery_chain(
            plan_content_for_user.si(user_id=caller_id),
            auto_create_weekly_content.si(user_id=caller_id),
        ).apply_async()
    except Exception as e:
        # Nothing will ever run, so drop the 'queued' record rather than leaving the SPA polling
        # a run that never starts (it would otherwise sit there until the TTL expires).
        clear_generation_status(caller_id)
        log_error("Could not dispatch weekly content generation", exc=e, user_id=caller_id)
        raise HTTPException(status_code=500, detail="Could not queue content generation")

    return ResponseModel(status_code=200, detail="Weekly content created successfully")


@router.get("/content_generation_status/", responses={
    200: {"description": "Content generation progress"},
    **{k: v for k, v in error_responses.items() if k in [401]}
})
def get_content_generation_status_endpoint(session_token: str) -> ResponseModel[Optional[dict[str, Any]]]:
    """Progress of the caller's weekly content-generation run — queued → in_progress (X of N) →
    done/failed. `detail` is None when no run is being tracked (nothing started, or it aged out).
    Scoped by session rather than a user_id query param so one user can't poll another's run.
    """
    user_id = get_session_user_id(session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    return ResponseModel(status_code=200, detail=get_generation_status(user_id))


@router.post("/invite_to_li_company_page/", responses={
    200: {"description": "Invite Users to LinkedIn Company Page"},
    **{k: v for k, v in error_responses.items() if k in [401, 403]}
})
def invite_to_li_company_page(session_token: Optional[str] = None,
                              user_id: Optional[int] = None) -> ResponseModel[str]:
    """Run the company-page invite drip for the CALLER now instead of waiting for the beat.

    The endpoint only dispatches — `plan_daily_invites` still decides the allowance from the
    smallest of the three ceilings before Chrome opens, so calling this repeatedly cannot exceed
    the day's budget.
    """
    caller_id = require_session_user_id(session_token)
    _reject_foreign_user_id(caller_id, user_id)

    automate_invites_to_company_page_for_user.apply_async(
        kwargs={'user_id': caller_id}, retry=True,
        retry_policy={'max_retries': 3, 'interval_start': 60, 'interval_step': 30}
    )
    return ResponseModel(status_code=200, detail="Process to invite to LinkedIn Company Page Started")


@router.post("/aws_test_get_my_profile/", responses={
    200: {"description": "Test Get My Profile on AWS"},
    **{k: v for k, v in error_responses.items() if k in [401, 403]}
})
def aws_test_get_my_profile(session_token: Optional[str] = None,
                            user_id: Optional[int] = None) -> ResponseModel[str]:
    """Smoke-test the AWS Celery path end to end by fetching the caller's own LinkedIn profile.

    Diagnostic only — it proves a task reached a worker and came back. `user_id` is a target to
    authorise; the task always runs as the caller.
    """
    caller_id = require_session_user_id(session_token)
    _reject_foreign_user_id(caller_id, user_id)

    test_get_my_profile.apply_async(kwargs={'user_id': caller_id}, retry=True,
                                    retry_policy={'max_retries': 1})
    return ResponseModel(status_code=200, detail="Test Get My Profile on AWS Message Sent to Celery Queue")


@router.get('/user_id/', responses={
    200: {"description": "User ID retrieved successfully"},
    **{k: v for k, v in error_responses.items() if k in [401, 403]}
})
def get_user_id_from_email(session_token: Optional[str] = None,
                           email: Optional[str] = None) -> ResponseModel[int]:
    """The CALLER's own user id. It was an email→id oracle for any bearer holder (issue #914) —
    which is how an attacker turned a known address into the id the automation routes wanted.
    """
    user_id = require_session_user_id(session_token)
    _reject_foreign_email(user_id, email)
    return ResponseModel(status_code=200, detail=user_id)


@router.get("/posts/", responses={
    200: {"description": "Posts retrieved successfully", "model": ResponseModel[PostsPage]},
    **{k: v for k, v in error_responses.items() if k in [401, 403]}
})
def get_posts_for_email(
    session_token: Optional[str] = None,
    email: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=10, ge=1, le=200),
    sort_order: str = Query(default='asc', pattern='^(asc|desc)$'),
    sort_by: str = Query(default='scheduled_time', pattern='^(scheduled_time|status|post_type|id)$'),
    status_filter: Optional[str] = Query(default=None),
    post_type_filter: Optional[str] = Query(default=None, pattern='^(text|video|carousel|document)$'),
    search: Optional[str] = Query(default=None, max_length=500),
    start_date: Optional[datetime] = Query(default=None),
    end_date: Optional[datetime] = Query(default=None),
) -> ResponseModel[dict[str, Any]]:
    """The Content Studio's paged post list, always scoped to the session's own rows.

    `email` is a target checked against the session, never the account queried — the query itself
    is scoped by `user_id`, so forgetting the check would still not return a stranger's drafts.
    """
    user_id = require_session_user_id(session_token)
    _reject_foreign_email(user_id, email)

    offset = (page - 1) * page_size
    posts, total = get_posts(
        user_id, limit=page_size, offset=offset,
        sort_order=sort_order, status_filter=status_filter,
        post_type_filter=post_type_filter, search=search, sort_by=sort_by,
        start_date=start_date, end_date=end_date,
    )

    posts_list = [
        {
            "post_id": post["id"],
            "content": post["content"],
            "video_url": post["video_url"],
            "image_url": post.get("image_url"),
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
            # Why the user rejected/deleted this draft (issue #713) — surfaced when regenerating.
            "rejection_reason": post.get("rejection_reason"),
            # An occasion draft the author publishes by hand (issue #1074) — the Studio renders a
            # copy-and-mark-as-posted state for it instead of a schedule.
            "manual_publish": bool(post.get("manual_publish")),
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
    **{k: v for k, v in error_responses.items() if k in [400, 401, 403, 405]}
})
def bulk_update_posts_endpoint(request: BulkUpdateRequest) -> ResponseModel[str]:
    """Restatus and/or reschedule a batch of the caller's posts, all-or-nothing.

    Ownership is proved for EVERY id before anything is written, and the update is additionally
    scoped by `user_id=` — the check is the gate, the scope is what makes forgetting it harmless.
    """
    user_id = require_session_user_id(request.session_token)
    if not request.post_ids:
        raise HTTPException(status_code=400, detail="post_ids is required")
    _require_own_posts(user_id, request.post_ids)

    _warn_if_naive_schedule(request.scheduled_datetime, "/posts/bulk_update/", user_id=user_id)

    # `user_id=` scopes the WHERE clause as well as the check in front of it — the check is the
    # gate, the scope is what makes forgetting it harmless (issue #914).
    if bulk_update_posts(request.post_ids, status=request.status,
                         scheduled_time=request.scheduled_datetime, user_id=user_id):
        return ResponseModel(status_code=200, detail="Posts updated successfully")
    else:
        raise HTTPException(status_code=405, detail="Posts could not be updated")


@router.delete("/posts/", responses={
    200: {"description": "Posts deleted (soft) successfully"},
    **{k: v for k, v in error_responses.items() if k in [400, 401, 403, 405]}
})
def delete_posts_endpoint(request: BulkDeleteRequest) -> ResponseModel[str]:
    """Soft-delete a batch of the caller's posts, keeping the author's stated reason.

    The reason is not bookkeeping: `regenerate_post_endpoint` feeds it back to the model as
    guidance, so a rejected draft's replacement knows what was wrong with it (issue #713).
    """
    user_id = require_session_user_id(request.session_token)
    if not request.post_ids:
        raise HTTPException(status_code=400, detail="post_ids is required")
    _require_own_posts(user_id, request.post_ids)

    reason = (request.rejection_reason or "").strip() or None
    if soft_delete_posts(request.post_ids, rejection_reason=reason, user_id=user_id):
        return ResponseModel(status_code=200, detail="Posts deleted successfully")
    else:
        raise HTTPException(status_code=405, detail="Posts could not be deleted")


@router.get("/post_url/", responses={
    200: {"description": "LinkedIn post URL returned"},
    **{k: v for k, v in error_responses.items() if k in [401, 403]}
})
def get_post_url(post_id: int, session_token: Optional[str] = None,
                 email: Optional[str] = None) -> ResponseModel[dict[str, Any]]:
    """The published LinkedIn permalink for one of the caller's posts, read off the POST log.

    `post_url` is None when the post has not been published yet AND when the id belongs to somebody
    else — the lookup is scoped to the user, so an unknown id can never answer with a stranger's
    permalink.
    """
    user_id = require_session_user_id(session_token)
    _reject_foreign_email(user_id, email)
    # The lookup is already scoped to the user, so a foreign post_id reads as "no URL" rather than
    # another account's permalink.
    post_url = get_post_url_from_log_for_user(user_id, post_id)
    return ResponseModel(status_code=200, detail={"post_url": post_url})


@router.post("/update_post/", responses={
    200: {"description": "Post updated successfully"},
    **{k: v for k, v in error_responses.items() if k in [401, 403, 405]}
})
def update_post(post_id: int, post: PostRequest) -> ResponseModel[str]:
    """Edit one of the caller's posts in place.

    `use_avatar` is three-valued (issue #744) and is written ONLY when the body states it: omitting
    the field means "leave my choice alone", never "clear it".
    """
    user_id = require_session_user_id(post.session_token)
    _reject_foreign_email(user_id, post.email)
    _require_own_posts(user_id, [post_id])

    _warn_if_naive_schedule(post.scheduled_datetime, "/update_post/", post_id=post_id,
                            user_id=user_id)

    if update_db_post(post.content, post.video_url, post.scheduled_datetime, post.post_type, post_id,
                      post.status, user_id=user_id):
        reason = (post.rejection_reason or "").strip() or None
        if reason:
            update_db_post_rejection_reason(post_id, reason, user_id=user_id)
        # Only on an explicit value: omitting the field means "leave my choice alone", not
        # "clear it" (issue #744 — use_avatar is three-valued).
        if post.use_avatar is not None:
            update_post_use_avatar(post_id, post.use_avatar)
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
    """Serve generated post media.

    PUBLIC by design — LinkedIn fetches these URLs unauthenticated when publishing — which is why it is GET-only and
    why `_find_asset_file` resolves the name against real directory entries instead of building a path out of caller
    input (CWE-22).

    A `request` turns the reply into a range-capable stream, which is what lets a browser scrub a
    generated video; without one the whole file is sent.
    """
    if not file_name:
        raise HTTPException(status_code=400, detail="A File Name is required")

    # Resolve the file via a filesystem scan of the trusted assets_dir (CWE-22).
    # _find_asset_file returns OS-provided paths, never paths constructed from
    # user input, so the taint chain from file_name is broken entirely.
    file_path = _find_asset_file(assets_dir, file_name)
    if file_path is None:
        raise HTTPException(status_code=404, detail="File not found")

    log_info(f"File Path: {file_path}")
    log_info(f"Content Type: {content_type}")

    file_extension = get_file_extension_from_filepath(file_path)
    mim_type = get_file_mime_type(file_extension)

    if request:
        return range_requests_response(request, file_path=file_path, content_type=mim_type)
    else:
        return FileResponse(status_code=200, path=file_path, media_type=mim_type, content_disposition_type=content_type)


# ---------------------------------------------------------------------------
# Strong authentication — passkeys, TOTP, recovery codes, step-up (issue #745, 2c)
#
# The policy lives in utilities/auth_factors.py and the ceremonies in utilities/webauthn_util.py;
# what is here is the HTTP seam, the audit rows, and the one decision those two cannot make — how
# a refusal is shaped so the SPA can react to it.
# ---------------------------------------------------------------------------


def _challenge_expiry() -> datetime:
    return datetime.now(timezone.utc) + timedelta(seconds=AUTH_CHALLENGE_TTL_SECONDS)


def _agent_scoped() -> bool:
    """Is THIS request being served on an agent token? Read off the ContextVar the resolver stamped,
    never a fresh lookup — same argument as `_enrollment_held`.
    """
    return _request_session_scope.get() == SESSION_SCOPE_AGENT


def _agent_approval_refusal() -> HTTPException:
    return HTTPException(status_code=403, detail={
        "code": "agent_may_not_approve",
        "message": "This token can queue work for review but cannot approve it.",
    })


def _refuse_agent_approval(action: Optional[str]) -> None:
    """An `agent`-scoped caller may queue work; only a human may authorise it (issue #1026).

    The queueing surface has to include the PUT routes — that is how a draft gets saved for review —
    so "the agent cannot approve" cannot be expressed as a path list. It is expressed here, on the
    one field that turns a draft into a send. Enforcing it server-side means the guarantee survives
    a prompt change, a rewritten skill, or a stolen token; a convention in an agent's instructions
    survives none of those.

    This is only HALF the guarantee. `action` is how the five PUT handlers name approval; the create
    handlers reach the identical state through `status` — see `_refuse_agent_approved_status`. A
    connection request's `retry` action (issue #1735) reaches the same APPROVED state a `failed` row
    never had a human sign off on the second time, so it is refused identically to `approve`.
    """
    if action not in ("approve", "retry"):
        return
    if not _agent_scoped():
        return
    raise _agent_approval_refusal()


def _refuse_agent_approved_status(status: Optional[str]) -> None:
    """The other way a row reaches APPROVED, and the one an `action` guard cannot see.

    Every create endpoint on the queueing surface takes a `status` and inserts APPROVED when it
    reads "approved". Guarding only the PUT `action` left that wide open: a POST /schedule_dm
    carrying status="approved" lands a row `auto_check_scheduled_dms` then SENDS, with no human in
    the loop and no `action` field for the other guard to inspect. An agent asking for an approved
    row IS an agent approving, so it is refused rather than quietly downgraded — a silent downgrade
    would let the caller believe it had dispatched something it had not.
    """
    if status != "approved":
        return
    if not _agent_scoped():
        return
    raise _agent_approval_refusal()


def _passkeys_or_503() -> RelyingParty:
    try:
        return webauthn_relying_party()
    except WebAuthnUnavailable as e:
        # 503 and not 500: nothing is broken, this deployment simply has no secure public origin to
        # bind a credential to. The SPA hides the passkey option rather than showing a dead button.
        raise HTTPException(status_code=503, detail=str(e))


def _verify_assertion_for_user(credential: Dict[str, Any], challenge: str,
                               expected_user_id: Optional[int] = None) -> Optional[Dict[str, Any]]:
    """Verify a passkey assertion and return the stored factor row it proved, or None.

    The credential id only SELECTS the row — nothing is trusted until the signature verifies
    against the public key stored for it. `expected_user_id` is what keeps a step-up honest: an
    assertion from a different account's passkey is a valid assertion, just not for this session.
    """
    credential_id = credential_id_from_response(credential)
    if not credential_id:
        return None
    stored = get_passkey_by_credential_id(credential_id)
    if not stored:
        return None
    if expected_user_id is not None and stored["user_id"] != expected_user_id:
        return None
    new_count = verify_passkey_assertion(credential, challenge, stored["public_key"],
                                         stored.get("sign_count") or 0)
    if new_count is None:
        return None
    update_factor_counter(stored["id"], new_count)
    return stored


@app.get("/auth/linkedin/", response_model=None, include_in_schema=False)
@router.get("/auth/linkedin/", response_model=None, include_in_schema=False)
def linkedin_auth_init(session_token: str = None) -> RedirectResponse:
    """The `email` parameter is gone (issue #914). It was already dead — the user comes from the
    `session_token` carried in the OAuth `state` — but it left the last handler in this file whose
    signature named an account it did not use, and it put the address in a URL (browser history,
    `Referer`) for nothing. An unused actor parameter is the next author's invitation to use it.
    """
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
    """LinkedIn's OAuth redirect target. Lives OUTSIDE `/api` because LinkedIn sends the browser here.

    `state` is `"<salt>:<session_token>"`, and the session it carries is what identifies the user —
    the LinkedIn account's email is often not the LEM login email, so matching on address would
    attach the token to the wrong row. A mismatched salt is a 400; with no session at all the token
    is upserted by the LinkedIn email, and no email means there is nothing to attach it to.

    Every failure path REDIRECTS to /account with an `li_error` rather than rendering an error: the
    caller is a browser mid-flow, and an API error page strands the user outside the app.
    """
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
        log_info(f"LinkedIn token exchange failed: {exc}")
        return _account_redirect({'li_error': 'token_exchange_failed'})

    log_info("Access token Response from api call")
    for key, value in access_token_response.__dict__.items():
        log_info(f"{key}: {value}")

    if not access_token_response.access_token:
        log_info("LinkedIn token exchange returned no access_token")
        return _account_redirect({'li_error': 'no_access_token'})

    try:
        restli_client = RestliClient()
        response = restli_client.get(
            resource_path='/userinfo',
            access_token=access_token_response.access_token,
        )
        log_info("Response from /userinfo api call:")
        for key, value in response.__dict__.items():
            log_info(f"{key}: {value}")
    except Exception as exc:
        log_info(f"LinkedIn /userinfo call failed: {exc}")
        return _account_redirect({'li_error': 'userinfo_failed'})

    user_email = response.entity.get('email', '')
    linked_sub_id = response.entity.get('sub', '')

    # Prefer updating the logged-in user's record directly (handles the case where
    # the LinkedIn account email differs from the app login email).
    user_id = get_session_user_id(session_token_from_state) if session_token_from_state else None
    if user_id:
        log_info(f"Updating LinkedIn token for session user_id={user_id}")
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
            log_info("LinkedIn /userinfo returned no email and no valid session")
            return _account_redirect({'li_error': 'no_email'})
        log_info(f"No session in state — upserting by LinkedIn email {user_email}")
        add_user_with_access_token(
            user_email,
            linked_sub_id,
            access_token_response.access_token,
            access_token_response.expires_in,
            access_token_response.refresh_token,
            access_token_response.refresh_token_expires_in,
        )

    return _account_redirect({'email': user_email, 'li_connected': '1'})


# --- Occasion / milestone posts (issue #1074) --------------------------------------------------
# LinkedIn's "Celebrate an occasion" composer creates an entity no API call can, so these two
# endpoints are the whole loop: seed a draft LEM writes, then record that the author published it
# by hand. Nothing here ever publishes — that is the point of the feature.


# --- Post images (issue #1030) --------------------------------------------------------------
# An image was only ever attached to a post by a background task. These three endpoints are the
# author's half: upload their own artwork, ask for a render, or take one off again — from the
# compose form (no row yet, so the image is a preview URL handed back at schedule time) and from
# the Review & Edit tab (the row exists, so the write lands on it immediately).


# --- Scheduled 1:1 DMs (issue #306) — mirrors the post scheduler endpoints ---

@router.post("/schedule_dm")
def schedule_dm_endpoint(request: ScheduleDmRequest) -> ResponseModel[dict[str, Any]]:
    """Create a scheduled 1:1 DM (draft or approved). The beat scanner (auto_check_scheduled_dms)
    sends approved DMs at their scheduled_time via send_scheduled_dm, honoring per-day DM caps.
    """
    user_id = get_session_user_id(request.session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    _refuse_agent_approved_status(request.status)
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
                                sort_order: str = "asc") -> ResponseModel[dict[str, Any]]:
    """The scheduled-DM review queue, paged and scoped to the caller.

    Every datetime is rewritten as explicit-UTC ISO on the way out so the browser does not read a naive value as
    local time.
    """
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
def update_scheduled_dm_endpoint(request: UpdateDmRequest) -> ResponseModel[str]:
    """Edit a queued DM, or approve/cancel it.

    `approve` is what releases it to be sent, so it is refused for an `agent` session; an empty request (no fields,
    no action) is a 422 rather than a 200 that changed nothing.
    """
    user_id = get_session_user_id(request.session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    if get_scheduled_dm_user_id(request.dm_id) != user_id:
        raise HTTPException(status_code=404, detail="Scheduled DM not found")
    action_map = {"approve": ScheduledDmStatus.APPROVED, "cancel": ScheduledDmStatus.CANCELED}
    if request.action is not None and request.action not in action_map:
        raise HTTPException(status_code=422,
                            detail=f"Unknown action '{request.action}' — expected 'approve' or 'cancel'")
    _refuse_agent_approval(request.action)
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
def delete_scheduled_dm_endpoint(request: DmDeleteRequest) -> ResponseModel[str]:
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
def create_connection_request_endpoint(request: ConnectionRequestCreate) -> ResponseModel[dict[str, Any]]:
    """Add a proactive connection-request target (issue #398). If no status is supplied the user's
    connection_request_mode governs it — 'auto_approve' (default) queues the target for the daily-capped
    drip immediately, 'pre_review' holds it as a draft awaiting explicit approval. An explicit status
    ('pending'|'approved') overrides that; anything else is rejected. The drip reuses invite_to_connect
    and honors the rate-limit / kill-switch and the combined daily invite cap. NO volume prospecting.
    """
    user_id = get_session_user_id(request.session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    _refuse_agent_approved_status(request.status)
    if request.status is None:
        mode = get_engagement_preferences(user_id).get("connection_request_mode", "auto_approve")
        # The PASSIVE half of the same guarantee, and the one with no field to refuse on. This
        # account default is `auto_approve` out of the box, so an agent that named no status at all
        # would otherwise land an APPROVED row on nearly every account — the guarantee broken by
        # doing nothing. The default speaks for the human who left it set, not for the machine
        # holding a token, so an agent always queues for review here (issue #1026).
        status = (ConnectionRequestStatus.APPROVED
                  if mode == "auto_approve" and not _agent_scoped()
                  else ConnectionRequestStatus.PENDING)
    elif request.status in ("pending", "approved"):
        status = (ConnectionRequestStatus.APPROVED if request.status == "approved"
                  else ConnectionRequestStatus.PENDING)
    else:
        raise HTTPException(status_code=422,
                            detail=f"Invalid status '{request.status}' — expected 'pending' or 'approved'")
    request_id = insert_connection_request(user_id, request.recipient_profile_url,
                                           message=request.message,
                                           recipient_name=request.recipient_name, status=status,
                                           recipient_email=request.recipient_email)
    if not request_id:
        raise HTTPException(status_code=500, detail="Could not create connection request")
    return ResponseModel(status_code=200, detail={"request_id": request_id})


@router.get("/connection_requests")
def list_connection_requests_endpoint(session_token: str, status_filter: Optional[str] = None,
                                      page: int = 1, page_size: int = 25,
                                      sort_order: str = "desc") -> ResponseModel[dict[str, Any]]:
    """The connection-request queue, paged and scoped to the caller.

    `recipient_email` (issue #1836) is never echoed here — each row instead carries a
    `has_recipient_email` boolean, which is all a caller needs to decide whether to supply one.
    """
    user_id = get_session_user_id(session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    result = get_connection_requests(user_id, status_filter=status_filter, page=page,
                                     page_size=page_size, sort_order=sort_order)
    return ResponseModel(status_code=200, detail=result)


@router.put("/connection_request")
def update_connection_request_endpoint(request: ConnectionRequestUpdate) -> ResponseModel[str]:
    """Edit a queued connection request, or approve/cancel/retry it.

    `approve` releases it to the invite drip and is refused for an `agent` session; an unknown action is a 422,
    never a silent save. `retry` (issue #1735) is the ONLY way a `failed` request is ever sent again — LEM
    never auto-retries a failed invite (repeat-inviting a decliner risks the account), so a stuck `failed`
    row needs an explicit human decision every time. It is refused on anything but a `failed` row, and is
    gated the same as `approve` since it reaches the identical send-queue state.
    """
    user_id = get_session_user_id(request.session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    if get_connection_request_user_id(request.request_id) != user_id:
        raise HTTPException(status_code=404, detail="Connection request not found")
    action_map = {"approve": ConnectionRequestStatus.APPROVED, "cancel": ConnectionRequestStatus.CANCELED,
                 "retry": ConnectionRequestStatus.APPROVED}
    if request.action is not None and request.action not in action_map:
        raise HTTPException(status_code=422,
                            detail=f"Unknown action '{request.action}' — expected 'approve', 'cancel' or 'retry'")
    if request.action == "retry":
        current = get_connection_request(request.request_id)
        if not current or current["status"] != str(ConnectionRequestStatus.FAILED):
            raise HTTPException(status_code=422, detail="Only a 'failed' connection request can be retried")
    _refuse_agent_approval(request.action)
    status = action_map.get(request.action)
    if status is None and all(v is None for v in (request.recipient_profile_url,
                                                  request.recipient_name, request.message,
                                                  request.recipient_email)):
        raise HTTPException(status_code=422, detail="Nothing to update — provide at least one field or an action")
    if not update_connection_request(request.request_id,
                                     recipient_profile_url=request.recipient_profile_url,
                                     recipient_name=request.recipient_name, message=request.message,
                                     status=status, recipient_email=request.recipient_email):
        raise HTTPException(status_code=500, detail="Could not update connection request")
    return ResponseModel(status_code=200, detail="Connection request updated")


@router.delete("/connection_request")
def delete_connection_request_endpoint(request: ConnectionRequestDelete) -> ResponseModel[str]:
    """Cancel a connection request (soft — sets status 'canceled' so it won't be sent)."""
    user_id = get_session_user_id(request.session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    if get_connection_request_user_id(request.request_id) != user_id:
        raise HTTPException(status_code=404, detail="Connection request not found")
    if not update_connection_request_status(request.request_id, ConnectionRequestStatus.CANCELED):
        raise HTTPException(status_code=500, detail="Could not cancel connection request")
    return ResponseModel(status_code=200, detail="Connection request canceled")


# Inbound hot leads (issue #483) — the leads inbox. Signals are detected on read paths that already
# run; the operator approves (or edits then approves) the drafted response before anything is sent.
_LEN_LEAD_DRAFT = 3000  # lead_signals.draft_response (TEXT; app cap)


class LeadSignalUpdate(BaseModel):
    """Body of `PUT /lead_signal` (issue #483).

    `approve` is the ONLY thing that dispatches a response, and it sends exactly the text the operator sees — so an
    approval with an empty draft is refused rather than sending nothing to a real prospect.
    """

    session_token: str
    signal_id: int
    draft_response: Optional[str] = Field(default=None, max_length=_LEN_LEAD_DRAFT)
    action: Optional[str] = None  # 'approve' | 'dismiss' | None (save the draft only)


@router.get("/lead_signals")
def list_lead_signals_endpoint(session_token: str, status_filter: Optional[str] = None,
                               page: int = 1, page_size: int = 25,
                               sort_order: str = "desc") -> ResponseModel[dict[str, Any]]:
    """The leads inbox: detected buying signals with their approval-gated draft responses."""
    user_id = get_session_user_id(session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    result = get_lead_signals(user_id, status_filter=status_filter, page=page,
                              page_size=page_size, sort_order=sort_order)
    result["new_count"] = count_new_lead_signals(user_id)
    return ResponseModel(status_code=200, detail=result)


@router.put("/lead_signal")
def update_lead_signal_endpoint(request: LeadSignalUpdate) -> ResponseModel[str]:
    """Edit a lead's draft, dismiss the signal, or APPROVE it — approval is the only thing that
    dispatches a response, and it sends exactly the text the operator sees.
    """
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
    _refuse_agent_approval(request.action)
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
    """Body of `PUT /lead` — the operator's manual corrections to a scored lead (issue #484).

    Everything here SURVIVES the nightly re-score, which is the point: `stage` is a manual override
    (empty string clears it back to the computed stage) and `dismiss` keeps someone off the board
    without deleting the history the score is built from.
    """

    session_token: str
    lead_id: int
    notes: Optional[str] = Field(default=None, max_length=_LEN_LEAD_NOTES)
    stage: Optional[str] = None   # manual stage override; '' clears it back to the computed stage
    action: Optional[str] = None  # 'dismiss' | 'restore' | None


class LeadRefreshRequest(BaseModel):
    """Body of `POST /leads/refresh` — session only.

    The caller's own pipeline is the only thing that can be re-scored, so there is nothing else to name.
    """

    session_token: str


@router.get("/leads")
def list_leads_endpoint(session_token: str, stage_filter: Optional[str] = None,
                        include_dismissed: bool = False, page: int = 1,
                        page_size: int = 100) -> ResponseModel[dict[str, Any]]:
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
def update_lead_endpoint(request: LeadUpdate) -> ResponseModel[str]:
    """Operator edits: move a lead's stage by hand, keep a note, or dismiss/restore it. The nightly
    re-score never overwrites any of these.
    """
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
def refresh_leads_endpoint(request: LeadRefreshRequest) -> ResponseModel[str]:
    """Re-score this user's pipeline now instead of waiting for tonight's rebuild."""
    user_id = get_session_user_id(request.session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    from cqc_lem.app.run_scheduler import rebuild_leads_for_user
    rebuild_leads_for_user.apply_async(kwargs={"user_id": user_id})
    return ResponseModel(status_code=200, detail="Re-scoring your leads — refresh in a moment")


@router.get("/catchup/touches")
def list_catchup_touches_endpoint(
    session_token: str,
    status_filter: Optional[str] = None,
    event_type_filter: Optional[str] = None,
    page: int = 1,
    page_size: int = 25,
    sort_order: str = Query(default="desc", pattern="^(asc|desc)$"),
    sort_by: str = Query(default="score", pattern="^(score|date)$"),
    start_date: Optional[datetime] = Query(default=None),
    end_date: Optional[datetime] = Query(default=None),
) -> ResponseModel[dict[str, Any]]:
    """Drafted LinkedIn Catch-up congratulations awaiting review (issue #482).

    Highest-scoring first by default; `sort_by=date` orders by when the touch was drafted, and
    `start_date`/`end_date` bound that same date so the queue can be narrowed to a range (#1464).
    """
    user_id = get_session_user_id(session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    return ResponseModel(status_code=200, detail=get_catchup_touches(
        user_id, status_filter=status_filter, event_type_filter=event_type_filter,
        page=page, page_size=page_size, sort_order=sort_order, sort_by=sort_by,
        start_date=start_date, end_date=end_date))


@router.put("/catchup/touch")
def update_catchup_touch_endpoint(request: UpdateCatchupTouchRequest) -> ResponseModel[str]:
    """Edit a drafted congratulations, or approve/cancel it. Approving queues it for the daily-capped
    send drip; nothing is sent until a human approves (unless the account opted into auto-approve).
    """
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
    _refuse_agent_approval(request.action)
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
def delete_catchup_touch_endpoint(request: CatchupTouchDeleteRequest) -> ResponseModel[str]:
    """Cancel a drafted catch-up touch (soft — sets status 'canceled' so it won't be sent, and the
    row stays as the dedup tombstone for that milestone).
    """
    user_id = get_session_user_id(request.session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    if get_catchup_touch_user_id(request.touch_id) != user_id:
        raise HTTPException(status_code=404, detail="Catch-up touch not found")
    if not update_catchup_touch_status(request.touch_id, CatchupTouchStatus.CANCELED):
        raise HTTPException(status_code=500, detail="Could not cancel catch-up touch")
    return ResponseModel(status_code=200, detail="Catch-up touch canceled")


# --- Affiliate / ambassador program (issue #737) ---------------------------------------------------


_EXTENSION_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "browser_extension"))


@router.get("/extension/linkedin-connect.zip")
def download_linkedin_extension() -> StreamingResponse:
    """Package the 'LEM LinkedIn Connect' browser extension as a zip the user can load
    unpacked in Chrome/Edge (chrome://extensions → Developer mode → Load unpacked). This is
    the one-click session-reconnect path referenced by the account page and reconnect email;
    until it's on the Chrome Web Store, users side-load this bundle. See docs/LINKEDIN_COOKIE.md.
    """
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


@router.post("/trial/extend", responses={
    200: {"description": "Extension result (granted or the reason it wasn't)"},
    **{k: v for k, v in error_responses.items() if k in [401, 404]},
})
def trial_extend_endpoint(request: TrialExtendRequest) -> ResponseModel[dict[str, Any]]:
    """Claim the early-adopter extended trial (issue #499): EARLY_ADOPTER_TRIAL_DAYS instead of the
    standard FREE_TRIAL_DAYS, in exchange for a public review.

    Not-granted outcomes are 200s with a `reason`, not errors — the SPA renders them as a prompt
    ("submit a quick review to unlock N days"), and an exhausted cohort is a normal state, not a
    failure: the user simply keeps their standard trial.
    """
    from cqc_lem.utilities.env_constants import (
        EARLY_ADOPTER_TRIAL_DAYS,
        EARLY_ADOPTER_TRIAL_ENABLED,
        FREE_TRIAL_DAYS,
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


# ---------------------------------------------------------------------------
# Avatar endpoints
# ---------------------------------------------------------------------------


@router.get("/video/credits", responses={
    200: {"description": "Video credit balance returned"},
    **{k: v for k, v in error_responses.items() if k in [401]}
})
def get_video_credits_endpoint(session_token: str) -> ResponseModel[dict[str, Any]]:
    """The caller's video credit balance — what premium video renders are charged against."""
    user_id = get_session_user_id(session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    return ResponseModel(status_code=200, detail={"balance": get_video_credit_balance(user_id)})


@router.post("/video/credits/checkout", responses={
    200: {"description": "Stripe checkout URL returned"},
    **{k: v for k, v in error_responses.items() if k in [400, 401]}
})
def video_credits_checkout(request: VideoCreditCheckoutRequest) -> ResponseModel[dict[str, Any]]:
    """Stripe hand-off for a video-credit package.

    Like the avatar twin, the grant happens in the webhook, not here.
    """
    user_id = get_session_user_id(request.session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    subscription = get_user_subscription_info(user_id)
    stripe_customer_id = subscription.get("stripe_customer_id") if subscription else None
    if not stripe_customer_id:
        raise HTTPException(status_code=400, detail="No Stripe customer record — contact support")
    from cqc_lem.utilities.stripe_util import VIDEO_CREDIT_PACKAGES, create_video_credits_checkout
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
def upgrade_video(request: UpgradeVideoRequest) -> ResponseModel[dict[str, Any]]:
    """Upgrade a video post to a premium tier — regenerates the video at premium
    quality (Veo + audio), charging credits at render time (refunded on failure).
    """
    user_id = get_session_user_id(request.session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    if get_post_user_id(request.post_id) != user_id:
        raise HTTPException(status_code=403, detail="Not your post")
    if get_post_type(request.post_id) != PostType.VIDEO:
        raise HTTPException(status_code=404, detail="Post not found or not a video post")

    from cqc_lem.utilities.env_constants import PREMIUM_TOP_VIDEO_CREDITS, PREMIUM_VIDEO_CREDITS
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
    log_info(f"video/upgrade: queued post_id={request.post_id} tier={request.tier} for user_id={user_id}")
    return ResponseModel(status_code=200, detail={
        "post_id": request.post_id, "tier": request.tier,
        "credits_required": needed, "status": "queued",
    })


@router.get("/carousel-templates", responses={200: {"description": "Available carousel templates"}})
def list_carousel_templates() -> ResponseModel[dict[str, Any]]:
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
    502: {"description": "The generator returned a deck missing required slides"},
})
def generate_carousel_preview(request: GenerateCarouselPreviewRequest) -> ResponseModel[dict[str, Any]]:
    """Generate carousel slide images from AI content + chosen template.
    Returns slide_urls (publicly accessible) and a suggested caption.
    The caller can pass these as carousel_slides when scheduling the post.
    """
    import time as _time

    from cqc_lem.utilities.ai.ai_helper import generate_carousel_content
    from cqc_lem.utilities.carousel_creator import (
        CAROUSEL_TEMPLATES,
        DEFAULT_TEMPLATE,
        carousel_model_for_stage,
        create_carousel_slide_images,
        missing_carousel_fields,
    )
    from cqc_lem.utilities.env_constants import API_URL_FINAL

    # The module-level resolver, not db's: this route was importing the raw one, so the cookie
    # sentinel the SPA sends never resolved and no session scope reached it (issue #914).
    user_id = require_session_user_id(request.session_token)
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

        # The shared stage map (issue #1681): this route used to keep its own, which is how a
        # "personal" preview asked the generator for one deck shape and validated another.
        model_cls = carousel_model_for_stage(stage)

        # The generator's own shape gate already spent its repair call, so a deck still missing a
        # required slide is an upstream failure — say which slides, rather than handing the SPA a
        # raw pydantic ValidationError dump from the constructor (issue #1666).
        missing = missing_carousel_fields(model_cls, carousel_dict)
        if missing:
            raise HTTPException(
                status_code=502,
                detail="The carousel generator returned a deck missing required slide(s): "
                       + ", ".join(missing) + ". Try again.")

        carousel_obj = model_cls(**carousel_dict)
        image_paths = create_carousel_slide_images(
            carousel_obj, post_id=0, output_dir=output_dir, template=carousel_template
        )
        slide_urls = [
            f"{API_URL_FINAL}/api/assets?file_name=images/carousel/{preview_id}/{os.path.basename(p)}"
            for p in image_paths
        ]
    except HTTPException:
        # Already the answer we mean to send — re-wrapping it would bury a 502 inside a 500 whose
        # detail is the repr of this exception.
        raise
    except Exception as exc:
        log_info(f"generate-carousel: failed for user_id={user_id} — {exc}")
        raise HTTPException(status_code=500, detail=str(exc))

    return ResponseModel(status_code=200, detail={
        "slide_urls": slide_urls,
        "caption": post_text,
        "template": carousel_template,
    })


# Register the /api router
app.include_router(router)

# Per-area routers (#1154). Included with NO `prefix=` argument — each declares its full prefix on
# its own APIRouter, because `route.path` is what `_scope_path`, `_hide_admin_routes_from_schema`
# and the surface guards below all read, and an include-time prefix never appears there.
#
# Imported HERE rather than at the top of the file: these modules reach back into this one for the
# auth kernel, so the include has to happen after `get_session_user_id` and friends are bound.
from cqc_lem.api.routers import (  # noqa: E402
    admin as _admin_router,
    auth as _auth_router,
    avatar as _avatar_router,
    billing as _billing_router,
    outreach as _outreach_router,
    user as _user_router,
)

app.include_router(_admin_router.router)
app.include_router(_auth_router.router)
app.include_router(_avatar_router.router)
app.include_router(_billing_router.router)
app.include_router(_outreach_router.router)
app.include_router(_user_router.router)


def _walk_routes(routes) -> Iterator[object]:
    """Flatten the route table. FastAPI ≥0.139 keeps an included router as a single
    `_IncludedRouter` node holding the real routes rather than copying them onto `app.routes`, so a
    flat loop over `app.routes` sees the /api tree as ONE opaque entry and silently matches nothing.
    Descend through both shapes so this keeps working either way.
    """
    for route in routes:
        included = getattr(route, "original_router", None)
        children = getattr(included, "routes", None) if included is not None \
            else getattr(route, "routes", None)
        if children:
            yield from _walk_routes(children)
        if getattr(route, "endpoint", None) is not None:
            yield route


def _hide_admin_routes_from_schema() -> int:
    """Keep every `/api/admin/*` operation OUT of the generated OpenAPI schema (issue #1020).

    The published schema listed all eighteen admin operations by method and path — a targeting map
    for anyone probing the admin secret, on a document served without a credential. Their runtime
    auth is untouched: this changes what is DESCRIBED, not what is enforced.

    Derived from the route table rather than written as `include_in_schema=False` on eighteen
    decorators, because the failure mode is silent: a nineteenth admin route added later would
    publish itself and nothing would say so. `test_no_admin_route_appears_in_the_public_schema`
    checks the outcome, and this loop makes the outcome the default.

    Returns how many admin operations the walk MATCHED, not how many flags it flipped. `_walk_routes`
    descends into the ORIGINAL router objects, which outlive a re-import of this module — so counting
    flips reports 0 the second time around (a reloaded `app` re-including routers whose routes are
    already hidden) and reads exactly like the walk matching nothing, which is the one failure this
    number exists to expose.
    """
    hidden = 0
    for route in _walk_routes(app.routes):
        if getattr(route, "path", "").startswith("/api/admin") and hasattr(
                route, "include_in_schema"):
            route.include_in_schema = False
            hidden += 1
    return hidden


_ADMIN_ROUTES_HIDDEN = _hide_admin_routes_from_schema()
# Logged, not just returned: the number is how you tell "no admin routes to hide" from "the walk
# matched nothing", which is the failure this whole helper exists to make visible.
log_debug("Admin routes hidden from the OpenAPI schema", hidden=_ADMIN_ROUTES_HIDDEN)

# Backward-compat redirects for the docs surface moved under /api (issue #1020). Registered here so
# they precede the SPA catch-all below, which would otherwise answer them with index.html. 301
# (permanent) matches the /assets redirect below and keeps every bookmark, README link and Postman
# import working.
_DOCS_REDIRECTS = {"/docs": "/api/docs", "/redoc": "/api/redoc",
                   "/openapi.json": "/api/openapi.json"}


def _make_docs_redirect(target: str) -> Callable[[], Awaitable[RedirectResponse]]:
    async def _redirect() -> RedirectResponse:
        return RedirectResponse(url=target, status_code=301)
    return _redirect


for _legacy_path, _new_path in _DOCS_REDIRECTS.items():
    app.get(_legacy_path, include_in_schema=False)(_make_docs_redirect(_new_path))

# Backward-compat redirect: /assets?file_name=... → /api/assets?file_name=...
# Must be registered before the SPA StaticFiles mount so it takes priority.
@app.get("/assets", include_in_schema=False)
async def assets_compat_redirect(request: Request, file_name: Optional[str] = None):
    """301 the pre-`/api` asset URL to its current home, query string intact.

    Registered BEFORE the SPA StaticFiles mount so it wins the path; without a `file_name` this is
    the SPA's own `/assets` bundle directory, so it 404s here and lets the mount answer.
    """
    if file_name:
        return RedirectResponse(url=f"/api/assets?{request.url.query}", status_code=301)
    raise HTTPException(status_code=404)

# Serve the React SPA for all non-API routes (must come after include_router)
if os.path.isdir(_ui_dist):
    _spa_index = os.path.join(_ui_dist, "index.html")

    _spa_assets_dir = os.path.join(_ui_dist, "assets")

    # Retain this build's chunks so a tab opened before the deploy can still load the lazy ones it
    # was holding hashes for (issue #743). No-op unless SPA_ASSET_ARCHIVE_DIR is configured.
    sync_build_to_archive(_spa_assets_dir)

    # Vite emits content-hashed filenames, so assets can be cached forever, and a miss falls back to
    # a previously-deployed build. (CDN edge cache is also purged on each deploy via build-and-push.yml.)
    app.mount("/assets", ArchivedStaticFiles(directory=_spa_assets_dir), name="spa-assets")

    # robots.txt, sitemap.xml, favicon.ico and the Open Graph image (issue #1298). MUST come before
    # the catch-all below, which would otherwise hand a crawler index.html for every one of them.
    register_spa_public_routes(app, _ui_dist)

    @app.get("/{full_path:path}", response_class=HTMLResponse, include_in_schema=False)
    def serve_spa(full_path: str, request: Request):
        """The SPA catch-all — any path no route above claimed gets index.html.

        That is what makes client-side routing survive a hard refresh, and it is why this is
        registered LAST, after include_router.
        """
        with open(_spa_index) as fh:
            html = fh.read()
        # A build that never received VITE_PUBLIC_BASE_URL leaves the token in the shell (Vite only
        # substitutes a DEFINED env var), so fill it from the server's own canonical host. Without
        # this a crawler is handed a literal placeholder in og:image/og:url.
        html = render_base_url(html, public_base_url(str(request.base_url)), VITE_BASE_URL_PLACEHOLDER)
        # spa_index_headers() owns the no-store contract — see the note there.
        return HTMLResponse(content=html, headers=spa_index_headers())


def send_bytes_range_requests(
        file_path: str, start: int, end: int, chunk_size: int = 10_000
):
    """Yield `file_path` from byte `start` to `end` INCLUSIVE, in `chunk_size` pieces.

    Inclusive because that is what an HTTP `Range` header means; an exclusive read here would drop
    the last byte of every ranged response.
    """
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
    """Serve a file as a stream, honouring an HTTP `Range` header (206) or sending it whole (200).

    This is what makes a generated video scrubbable in a browser. An unsatisfiable range is a 416,
    not a clamped read — silently serving different bytes than were asked for corrupts the player's
    buffer. `content-encoding: identity` is stated so a proxy cannot compress a byte-range reply
    out of alignment with the offsets the client asked for.
    """
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
