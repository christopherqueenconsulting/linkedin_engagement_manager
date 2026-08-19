"""`/api/admin/*` — the operator and triage surface, split out of `api/main.py` (#1154).

Unlike every other slice, the admin **gates move with the routes**. Six handlers take
`_require_api_and_admin` as a `Depends()` DEFAULT ARGUMENT, which binds when the module is imported
— and `_main` is imported LAST here, below the routes, so it does not exist yet at that moment.
Reaching the gate as `_main._require_api_and_admin` would therefore raise at import, not at request
time. All five gates (`_require_admin`, `_require_api_and_admin`, `_require_user_admin`,
`_admin_secret_scheme`, `_bearer_scheme`) are read by `/api/admin/*` and nothing else, so they have
exactly one home and this is it.

Two consequences worth knowing before editing:

* `patch("cqc_lem.api.main.ADMIN_SECRET", ...)` no longer binds anything — `ADMIN_SECRET` is read
  here now. `tests/unit/api/test_router_patch_seam.py` names the tests that have to follow.
* `tests/unit/api/test_api_route_identity.py` derives its gate closure over `main` AND every router
  module, so `_ADMIN_SEEDS` keeps resolving. Deleting that derivation's router half would make the
  eighteen admin routes read as ungated — silently.

What stays in `main` is the auth kernel plus `_utc_iso` (which non-admin handlers still read),
reached as `_main.<name>`, an attribute resolved at REQUEST time. So
`patch("cqc_lem.api.main.get_session_user_id")` still binds what these handlers read.

`from cqc_lem.api import main as _main` sits at the BOTTOM; `routers/__init__.py` explains why.
"""

from enum import StrEnum
from typing import Any, List, Optional

from celery import states as celery_states
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field, model_validator

from cqc_lem.api.models import ResponseModel
from cqc_lem.app.engagement.feed import automate_commenting, consolidate_duplicate_comments_for_user
from cqc_lem.app.engagement.outreach import automate_appreciation_dms_for_user, send_private_dm
from cqc_lem.app.engagement.posting import automate_reply_commenting
from cqc_lem.utilities.db import (
    AuthAuditEvent,
    FeedbackStatus,
    PostType,
    admin_email_allowlist,
    count_admin_users,
    count_auth_audit_log_for_admin,
    count_users_for_admin,
    get_feedback_by_id,
    get_feedback_list,
    get_post_buyer_stage,
    get_post_type,
    get_post_user_id,
    get_user_for_admin,
    get_user_geo,
    grant_subscription_extension,
    is_user_admin,
    list_auth_audit_log_for_admin,
    list_users_for_admin,
    record_auth_event,
    record_feedback_review,
    replace_video_url_base,
    set_user_admin,
    set_user_disabled,
    update_user_location,
)
from cqc_lem.utilities.env_constants import (
    ADMIN_SECRET,
    DEFAULT_IMAGE_MODEL,
    DEFAULT_VIDEO_MODEL,
    DEFAULT_VIDEO_RATIO,
)
from cqc_lem.utilities.geocoding import GeocodeError, geocode_city
from cqc_lem.utilities.logger import log_info

router = APIRouter(prefix="/api/admin")


class AdminLocationByCityRequest(BaseModel):
    """The admin twin of `LocationByCityRequest`.

    `user_id` is the account being edited, so this body carries no session at all — the admin
    credential is what authorises the route.
    """

    user_id: int
    city: str
    state: Optional[str] = None
    country: Optional[str] = None


class FeedbackReviewAction(StrEnum):
    """The two verdicts an admin can hand a feedback row (issue #793).

    `approve` runs the classifier/filer with a human watching; `dismiss` settles the row and files nothing.
    """

    APPROVE = 'approve'
    DISMISS = 'dismiss'


class FeedbackReviewRequest(BaseModel):
    """Admin triage decision for a single feedback row (issue #793)."""
    session_token: str
    action: FeedbackReviewAction


class AdminFixVideoUrlsRequest(BaseModel):
    """Body of `POST /admin/fix-video-urls` — rewrite a stale asset host across stored video URLs.

    Admin-authorised by the `X-Admin-Secret` header, so no session rides in the body. `user_id`
    absent means EVERY user's rows are rewritten.
    """

    old_base: str
    new_base: str
    user_id: Optional[int] = None


class AdminRegenerateCarouselRequest(BaseModel):
    """Body of `POST /admin/regenerate-carousel`.

    `user_id` is whose voice/profile the new slides are written in — it is not an ownership check, so it need not
    match the post's author.
    """

    post_id: int
    user_id: int
    template: Optional[str] = None  # e.g. "bold_listicle", "minimal_dark"; None = auto-pick by stage


class AdminRegenerateVideoRequest(BaseModel):
    """Body of `POST /admin/regenerate-video` — re-render ONLY the video asset, keeping the text."""

    post_id: int
    user_id: int


class VariantCombo(BaseModel):
    """One point in the media-variant comparison matrix.

    `seed` is what makes a run repeatable; `include_video=False` renders the still only, which is the cheap half of
    a comparison.
    """

    image_model: str = DEFAULT_IMAGE_MODEL
    video_model: str = DEFAULT_VIDEO_MODEL
    ratio: str = DEFAULT_VIDEO_RATIO
    duration: int = 5
    seed: Optional[int] = None
    include_video: bool = True


class GenerateMediaVariantsRequest(BaseModel):
    """Body of `POST /admin/generate-media-variants` — render candidates for review, mutating no post.

    `_require_source` is the guard that matters: with neither a `post_id` nor `text`/`topic` there
    is nothing to render, and this endpoint SPENDS money per variant, so an empty request is a 422
    rather than a bill.
    """

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


# ---------------------------------------------------------------------------
# Admin endpoints — require X-Admin-Secret header matching ADMIN_SECRET env var
# ---------------------------------------------------------------------------

def _require_admin(x_admin_secret: Optional[str] = Header(default=None)) -> None:
    if not ADMIN_SECRET or x_admin_secret != ADMIN_SECRET:
        raise HTTPException(status_code=403, detail="Forbidden")


# Declared security schemes so an admin tool sees BOTH required credentials: the bearer API token
# AND the admin secret. Both are needed.
#
# The descriptions name the CREDENTIAL, never where it is stored (issue #1020). Naming the env var
# hands an attacker the exact string to hunt for in a leaked build, a stack trace or a misconfigured
# container — free reconnaissance in a document these routes no longer even appear in.
_bearer_scheme = HTTPBearer(
    auto_error=False,
    description="Non-browser API access token, sent as 'Authorization: Bearer <token>'. Issued "
                "server-side and never shipped in the SPA bundle (issue #950); browsers "
                "authenticate on the session cookie instead.",
)
_admin_secret_scheme = APIKeyHeader(
    name="X-Admin-Secret", auto_error=False,
    description="Administrator secret, sent as the 'X-Admin-Secret' header. Server-side "
                "configuration only — it is never issued to a browser.",
)


def _require_api_and_admin(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer_scheme),
    x_admin_secret: Optional[str] = Depends(_admin_secret_scheme),
) -> None:
    """Require BOTH the bearer API token and the admin secret. Used by the
    /admin/test/* endpoints so the docs page presents and enforces both.
    """
    token = credentials.credentials if credentials else None
    if _main._API_ACCESS_TOKEN_SET and token not in _main._API_ACCESS_TOKEN_SET:
        raise HTTPException(status_code=401, detail="Unauthorized — missing or invalid bearer token")
    if not ADMIN_SECRET or x_admin_secret != ADMIN_SECRET:
        raise HTTPException(status_code=403, detail="Forbidden — missing or invalid X-Admin-Secret")


@router.post("/automation-pause", responses={200: {"description": "Automation paused"},
                                                    403: {"description": "Forbidden"}})
def admin_pause_automation(hours: float = 24,
                           x_admin_secret: Optional[str] = Header(default=None)) -> ResponseModel[dict[str, Any]]:
    """Kill-switch: pause ALL Selenium automation (comments/replies/DMs/stats/invites) for `hours` so
    a 429-rate-limited account/IP can recover. Posting (API) is unaffected. Default 24h.
    """
    _require_admin(x_admin_secret)
    from cqc_lem.utilities.linkedin.rate_limit import pause_automation
    seconds = int(max(0.0, hours) * 3600) or 3600
    ok = pause_automation(seconds, reason="admin")
    return ResponseModel(status_code=200, detail={"paused": ok, "seconds": seconds})


@router.post("/automation-resume", responses={200: {"description": "Automation resumed"},
                                                     403: {"description": "Forbidden"}})
def admin_resume_automation(x_admin_secret: Optional[str] = Header(default=None)) -> ResponseModel[dict[str, Any]]:
    """Lift a manual automation pause immediately."""
    _require_admin(x_admin_secret)
    from cqc_lem.utilities.linkedin.rate_limit import resume_automation
    return ResponseModel(status_code=200, detail={"resumed": resume_automation()})


@router.get("/automation-status", responses={200: {"description": "Automation status"},
                                                   403: {"description": "Forbidden"}})
def admin_automation_status(x_admin_secret: Optional[str] = Header(default=None)) -> ResponseModel[dict[str, Any]]:
    """Current pause + 429-breaker state (seconds remaining on each)."""
    _require_admin(x_admin_secret)
    from cqc_lem.utilities.linkedin.rate_limit import automation_pause_remaining, rate_limit_cooldown_remaining
    pause_s = automation_pause_remaining()
    return ResponseModel(status_code=200, detail={
        "paused": pause_s > 0,
        "pause_remaining_s": pause_s,
        "breaker_remaining_s": rate_limit_cooldown_remaining(),
    })


@router.post("/fix-video-urls", responses={
    200: {"description": "Video URLs updated"},
    403: {"description": "Forbidden"},
})
def admin_fix_video_urls(
    request: AdminFixVideoUrlsRequest,
    x_admin_secret: Optional[str] = Header(default=None),
) -> ResponseModel[dict[str, Any]]:
    """Rewrite a stale asset host across stored video URLs — the repair for an asset base that moved.

    Blind string replacement across rows, so `user_id` is the blast-radius control: omitting it
    rewrites EVERY user's videos.
    """
    _require_admin(x_admin_secret)
    updated = replace_video_url_base(request.old_base, request.new_base, request.user_id)
    log_info(f"admin/fix-video-urls: replaced {updated} row(s) — {request.old_base!r} → {request.new_base!r}")
    return ResponseModel(status_code=200, detail={"updated_rows": updated})


@router.post("/user/location", responses={
    200: {"description": "User login location updated"},
    403: {"description": "Forbidden"},
    404: {"description": "User not found"},
    422: {"description": "Could not geocode the city"},
})
def admin_set_user_location(
    request: AdminLocationByCityRequest,
    x_admin_secret: Optional[str] = Header(default=None),
) -> ResponseModel[dict[str, Any]]:
    """Align a user's login location to their purchased proxy's city (admin-only). Geocodes the
    city/state and persists it so the automation browser's geo matches the proxy IP.
    """
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
    log_info(f"admin/user/location: set user {request.user_id} -> {geo['city']}, {geo.get('country')} "
            f"({geo['latitude']},{geo['longitude']} {geo.get('timezone')})")
    return ResponseModel(status_code=200, detail=geo)


@router.post("/regenerate-carousel", responses={
    200: {"description": "Carousel regenerated"},
    403: {"description": "Forbidden"},
    404: {"description": "Post not found or not a carousel"},
})
def admin_regenerate_carousel(
    request: AdminRegenerateCarouselRequest,
    x_admin_secret: Optional[str] = Header(default=None),
) -> ResponseModel[dict[str, Any]]:
    """Re-render a carousel's slides and overwrite the post's content.

    DOCUMENT posts are accepted too — a document post is a carousel published as a native PDF, so
    it runs the same slide-regeneration path. Anything else is a 404.
    """
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
        log_info(f"admin/regenerate-carousel: failed for post_id={request.post_id} — {exc}")
        raise HTTPException(status_code=500, detail=str(exc))

    log_info(f"admin/regenerate-carousel: regenerated post_id={request.post_id}")
    return ResponseModel(status_code=200, detail={"post_id": request.post_id, "content_preview": new_content[:120]})


@router.post("/regenerate-video", responses={
    200: {"description": "Video regenerated"},
    403: {"description": "Forbidden"},
    404: {"description": "Post not found or not a video"},
    500: {"description": "Regeneration failed"},
})
def admin_regenerate_video(
    request: AdminRegenerateVideoRequest,
    x_admin_secret: Optional[str] = Header(default=None),
) -> ResponseModel[dict[str, Any]]:
    """Regenerate ONLY the video asset for an existing video post (keeps content)."""
    _require_admin(x_admin_secret)

    post_type = get_post_type(request.post_id)
    if post_type != PostType.VIDEO:
        raise HTTPException(status_code=404, detail="Post not found or not a video post")

    from cqc_lem.app.run_content_plan import regenerate_video_for_post
    try:
        new_url = regenerate_video_for_post(request.post_id)
    except Exception as exc:
        log_info(f"admin/regenerate-video: failed for post_id={request.post_id} — {exc}")
        raise HTTPException(status_code=500, detail=str(exc))
    if not new_url:
        raise HTTPException(status_code=500, detail="Video regeneration failed (no asset produced)")

    log_info(f"admin/regenerate-video: regenerated post_id={request.post_id} -> {new_url}")
    return ResponseModel(status_code=200, detail={"post_id": request.post_id, "video_url": new_url})


@router.post("/generate-media-variants", responses={
    200: {"description": "Variants generated"},
    403: {"description": "Forbidden"},
    422: {"description": "Provide post_id or text/topic"},
    500: {"description": "Generation failed"},
})
def admin_generate_media_variants(
    request: GenerateMediaVariantsRequest,
    x_admin_secret: Optional[str] = Header(default=None),
) -> ResponseModel[dict[str, Any]]:
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
        log_info(f"admin/generate-media-variants: failed — {exc}")
        raise HTTPException(status_code=500, detail=str(exc))

    log_info(f"admin/generate-media-variants: batch={payload['batch_id']} "
            f"variants={len(payload['variants'])} est=${payload['total_estimated_cost_usd']}")
    return ResponseModel(status_code=200, detail=payload)


# ---------------------------------------------------------------------------
# Admin test-run endpoints — kick a single engagement task on the selenium
# worker so you can watch it live in the Chrome VNC. Inputs are typed query
# params (so /docs renders individual fields, not a raw JSON body). BOTH the
# bearer API token AND X-Admin-Secret are required (see _require_api_and_admin).
# ---------------------------------------------------------------------------

def _queued(result, task: str, **detail) -> ResponseModel[dict[str, Any]]:
    """Report a QueueOnce dispatch honestly — 409 when nothing was actually queued.

    Every task behind these endpoints is a QueueOnce task with `once={'graceful': True}`, and a
    graceful rejection does not raise: celery-once answers `EagerResult(None, None, REJECTED)`
    (celery_once/tasks.py:104) because a run for the same key is already in flight. Reporting that
    as a 200 with `"task_id": null` told the operator a run had started and handed them an id that
    `/admin/task-status/{task_id}` can never resolve — the one thing these endpoints exist to let
    you watch.
    """
    if result.state == celery_states.REJECTED or result.id is None:
        raise HTTPException(
            status_code=409,
            detail=f"A {task} run is already queued or in progress for this target")
    return ResponseModel(status_code=200, detail={"task_id": result.id, "task": task, **detail})

@router.post("/test/comment", responses={
    200: {"description": "Commenting test run queued"},
    401: {"description": "Missing/invalid bearer token"},
    403: {"description": "Missing/invalid admin secret"},
    409: {"description": "A run is already queued or in progress for this target"},
})
def admin_test_comment(
    user_id: int = Query(..., description="LinkedIn account user id", examples=[1]),
    loop_for_duration: int = Query(300, ge=10, le=3600,
                                   description="Seconds before the run self-terminates"),
    _: None = Depends(_require_api_and_admin),
) -> ResponseModel[dict[str, Any]]:
    """Run the feed-commenting automation for a user (comments on posts in their feed)."""
    result = automate_commenting.apply_async(kwargs={
        "user_id": user_id, "loop_for_duration": loop_for_duration,
    }, queue="se_engage")
    log_info(f"admin/test/comment: queued task={result.id} user_id={user_id}")
    return _queued(result, "automate_commenting", user_id=user_id)


@router.post("/test/reply", responses={
    200: {"description": "Reply test run queued"},
    401: {"description": "Missing/invalid bearer token"},
    403: {"description": "Missing/invalid admin secret"},
    404: {"description": "User for post not found"},
    409: {"description": "A run is already queued or in progress for this target"},
})
def admin_test_reply(
    post_id: int = Query(..., description="Id of an already-posted post to reply on", examples=[42]),
    loop_for_duration: int = Query(300, ge=10, le=3600,
                                   description="Seconds before the run self-terminates"),
    future_forward: int = Query(0, ge=0, le=5, description="Forward index (0-5)"),
    _: None = Depends(_require_api_and_admin),
) -> ResponseModel[dict[str, Any]]:
    """Run the reply-to-comments automation for a specific (already-posted) post."""
    user_id = get_post_user_id(post_id)
    if not user_id:
        raise HTTPException(status_code=404, detail="User for post not found")
    result = automate_reply_commenting.apply_async(kwargs={
        "user_id": user_id, "post_id": post_id,
        "loop_for_duration": loop_for_duration, "future_forward": future_forward,
    }, queue="se_engage")
    log_info(f"admin/test/reply: queued task={result.id} post_id={post_id}")
    return _queued(result, "automate_reply_commenting", post_id=post_id, user_id=user_id)


@router.post("/consolidate-duplicate-comments", responses={
    200: {"description": "Consolidation run queued"},
    401: {"description": "Missing/invalid bearer token"},
    403: {"description": "Missing/invalid admin secret"},
    409: {"description": "A run is already queued or in progress for this target"},
})
def admin_consolidate_duplicate_comments(
    user_id: int = Query(..., description="LinkedIn account user id", examples=[1]),
    dry_run: bool = Query(True, description="Report-only when true; set false to actually delete extras"),
    hours: int = Query(168, ge=1, le=2160,
                       description="Look back this many hours for duplicate-commented posts (default 7 days)"),
    _: None = Depends(_require_api_and_admin),
) -> ResponseModel[dict[str, Any]]:
    """Keep one comment per post and delete the extras for posts this user commented on more than once.
    Defaults to a DRY RUN (report only) — pass dry_run=false to actually delete.
    """
    result = consolidate_duplicate_comments_for_user.apply_async(kwargs={
        "user_id": user_id, "dry_run": dry_run, "hours": hours,
    }, queue="se_engage")
    log_info(f"admin/consolidate-duplicate-comments: queued task={result.id} user_id={user_id} dry_run={dry_run}")
    return _queued(result, "consolidate_duplicate_comments_for_user",
                   user_id=user_id, dry_run=dry_run, hours=hours)


@router.post("/test/dm", responses={
    200: {"description": "DM test run queued"},
    401: {"description": "Missing/invalid bearer token"},
    403: {"description": "Missing/invalid admin secret"},
    409: {"description": "A run is already queued or in progress for this target"},
})
def admin_test_dm(
    user_id: int = Query(..., description="LinkedIn account user id", examples=[1]),
    loop_for_duration: int = Query(300, ge=10, le=3600,
                                   description="Seconds before the run self-terminates"),
    _: None = Depends(_require_api_and_admin),
) -> ResponseModel[dict[str, Any]]:
    """Run the appreciation-DM automation (DMs people who recently viewed the profile)."""
    result = automate_appreciation_dms_for_user.apply_async(kwargs={
        "user_id": user_id, "loop_for_duration": loop_for_duration,
    }, queue="se_outreach")
    log_info(f"admin/test/dm: queued task={result.id} user_id={user_id}")
    return _queued(result, "automate_appreciation_dms_for_user", user_id=user_id)


@router.post("/test/dm-direct", responses={
    200: {"description": "Direct DM queued"},
    401: {"description": "Missing/invalid bearer token"},
    403: {"description": "Missing/invalid admin secret"},
    409: {"description": "A run is already queued or in progress for this target"},
})
def admin_test_dm_direct(
    user_id: int = Query(..., description="LinkedIn account user id", examples=[1]),
    profile_url: str = Query(..., description="LinkedIn profile URL to message",
                             examples=["https://www.linkedin.com/in/some-person/"]),
    message: str = Query(..., description="Message body to send", examples=["Hi — testing, please ignore."]),
    _: None = Depends(_require_api_and_admin),
) -> ResponseModel[dict[str, Any]]:
    """Send ONE direct DM to a specific profile URL — the most deterministic way to
    watch the messaging flow end-to-end in the VNC.
    """
    result = send_private_dm.apply_async(kwargs={
        "user_id": user_id, "profile_url": profile_url, "message": message,
    }, queue="se_outreach")
    log_info(f"admin/test/dm-direct: queued task={result.id} user_id={user_id} -> {profile_url}")
    return _queued(result, "send_private_dm", user_id=user_id, profile_url=profile_url)


@router.get("/task-status/{task_id}", responses={
    200: {"description": "Task status"},
    401: {"description": "Missing/invalid bearer token"},
    403: {"description": "Missing/invalid admin secret"},
})
def admin_task_status(
    task_id: str,
    _: None = Depends(_require_api_and_admin),
) -> ResponseModel[dict[str, Any]]:
    """Poll a queued test task's state (PENDING/STARTED/SUCCESS/FAILURE)."""
    from cqc_lem.app.my_celery import app as celery_app
    res = celery_app.AsyncResult(task_id)
    detail = {"task_id": task_id, "state": res.state}
    if res.ready():
        detail["result"] = str(res.result)[:500]
    return ResponseModel(status_code=200, detail=detail)


# ---------------------------------------------------------------------------
# Feedback admin triage panel (issue #793) — user-level admin role, not the
# operational X-Admin-Secret endpoints above.
# ---------------------------------------------------------------------------

# A row that already reached GitHub must never be re-reviewed. `file_feedback_issue` re-classifies
# (LLM spend) and dedups against the OPEN clusters — and a filed row IS its own open cluster, so a
# second approve matches it to itself at similarity 1.0 and posts a false "+1 another report" on the
# very issue it created. Dismissing one would mark it not-actionable while its issue stays open.
_REVIEW_SETTLED_STATUSES = (FeedbackStatus.CLUSTERED, FeedbackStatus.ISSUE_CREATED,
                            FeedbackStatus.RESOLVED)


def _require_user_admin(session_token: str) -> int:
    """Validate the session and ensure the user is designated as an admin.

    Returns the user_id on success; raises HTTPException 401/403 otherwise.
    """
    user_id = _main.get_session_user_id(session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    if not is_user_admin(user_id):
        raise HTTPException(status_code=403, detail="Admin access required")
    return user_id


@router.get("/feedback", responses={
    200: {"description": "Feedback list returned"},
    401: {"description": "Invalid or expired session"},
    403: {"description": "Admin access required"},
})
def admin_feedback_list(
    session_token: str,
    status: Optional[str] = None,
    source: Optional[str] = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> ResponseModel[dict[str, Any]]:
    """List feedback submissions for the admin triage panel."""
    _require_user_admin(session_token)
    rows = get_feedback_list(status=status, source=source, limit=limit, offset=offset)
    return ResponseModel(status_code=200, detail={
        "items": [
            {
                "id": r.get("id"),
                "user_id": r.get("user_id"),
                "email": r.get("email"),
                "is_admin_reporter": bool(r.get("is_admin")),
                "source": r.get("source"),
                "type_hint": r.get("type_hint"),
                "body": r.get("body"),
                "context_json": r.get("context_json"),
                "status": r.get("status"),
                "cluster_id": r.get("cluster_id"),
                "github_issue_number": r.get("github_issue_number"),
                "reviewed_by": r.get("reviewed_by"),
                "reviewed_at": _main._utc_iso(r.get("reviewed_at")),
                "created_at": _main._utc_iso(r.get("created_at")),
            }
            for r in rows
        ],
        "limit": limit,
        "offset": offset,
    })


@router.post("/feedback/{feedback_id}/review", responses={
    200: {"description": "Review recorded"},
    401: {"description": "Invalid or expired session"},
    403: {"description": "Admin access required"},
    404: {"description": "Feedback row not found"},
    409: {"description": "Feedback already triaged"},
    422: {"description": "Invalid action"},
})
def admin_feedback_review(
    feedback_id: int,
    request: FeedbackReviewRequest,
) -> ResponseModel[dict[str, Any]]:
    """Approve a feedback row for auto-triage or dismiss it."""
    reviewer_user_id = _require_user_admin(request.session_token)
    from cqc_lem.utilities.feedback.issue_service import IssueAction, file_feedback_issue
    row = get_feedback_by_id(feedback_id)
    if not row:
        raise HTTPException(status_code=404, detail="Feedback not found")

    # The panel only offers the buttons on `new` rows, but its list is cached and two admins (or the
    # auto-filer beat) can settle a row between render and click.
    row_status = str(row.get("status") or "")
    if row_status in _REVIEW_SETTLED_STATUSES or row.get("github_issue_number"):
        raise HTTPException(status_code=409,
                            detail=f"Feedback already triaged (status {row_status or 'unknown'})")

    if request.action == FeedbackReviewAction.DISMISS:
        if not record_feedback_review(feedback_id, reviewer_user_id,
                                      status=FeedbackStatus.DISMISSED):
            raise HTTPException(status_code=500, detail="Could not dismiss feedback")
        log_info("Feedback dismissed by admin", feedback_id=feedback_id,
                 user_id=reviewer_user_id)
        return ResponseModel(status_code=200, detail={"reviewed": True, "action": "dismissed"})

    # approve: run the classifier/filer path now, told that a human is watching (#1036) — the
    # unattended-run holds re-applied to an explicit approve are what left the row in `new`.
    result = file_feedback_issue(row, admin_approved=True)
    # Stamp the reviewer even when the filer itself dropped/FAQ'd/human-routed the row.
    record_feedback_review(feedback_id, reviewer_user_id)
    log_info("Feedback approved by admin", feedback_id=feedback_id,
             user_id=reviewer_user_id, action=result.get("action"))
    return ResponseModel(status_code=200, detail={
        "reviewed": True,
        "action": "approved",
        # The panel cannot infer this from the 200: an approve that GitHub refused records the
        # review and changes NOTHING else, which is indistinguishable from a filed one unless the
        # answer says so (#1036).
        "filed": result.get("action") in (IssueAction.FILED, IssueAction.DEDUPED),
        "filing_result": result,
    })


class YouTubeTokenRequest(BaseModel):
    """Body of `POST /admin/youtube-token` — install a Google refresh token WITHOUT a deploy (#742).

    Write-only: the token lands in `app_credentials` and no route ever returns it. Admin-authorised
    by the route, so this body carries only the secret it is delivering.
    """

    refresh_token: str


@router.get("/youtube-status", responses={
    200: {"description": "YouTube publishing status"},
    401: {"description": "Invalid or expired session"},
    403: {"description": "Admin access required"},
})
def admin_youtube_status(session_token: str, live: bool = False) -> ResponseModel[dict[str, Any]]:
    """'YouTube publishing: connected / needs re-auth (reason)' for the settings surface (#742).

    Reads the last recorded weekly probe by default so opening Settings never spends a round trip on
    Google; `live=true` re-probes on demand. State only — the refresh token itself is never returned.
    """
    _require_user_admin(session_token)
    from cqc_lem.utilities.marketing.youtube_auth import status_report
    return ResponseModel(status_code=200, detail=status_report(live=live))


@router.post("/youtube-token", responses={
    200: {"description": "Refresh token stored"},
    403: {"description": "Forbidden"},
    422: {"description": "Empty refresh token"},
})
def admin_set_youtube_token(request: YouTubeTokenRequest,
                            x_admin_secret: Optional[str] = Header(default=None)) -> ResponseModel[dict[str, Any]]:
    """Install a re-minted YouTube refresh token WITHOUT a deploy (issue #742): it lands in
    `app_credentials` and takes precedence over `YOUTUBE_REFRESH_TOKEN` in `.env`. Admin-secret
    gated rather than session gated — this request body carries a live credential. The stored token
    is probed immediately, so the response says whether the new value actually works.
    """
    _require_admin(x_admin_secret)
    from cqc_lem.utilities.marketing.youtube_auth import probe, store_refresh_token
    token = (request.refresh_token or "").strip()
    if not token:
        raise HTTPException(status_code=422, detail="refresh_token is required")
    if not store_refresh_token(token, note="installed via /admin/youtube-token"):
        raise HTTPException(status_code=500, detail="Could not store the refresh token")
    state = probe()
    log_info("YouTube refresh token installed via admin endpoint", task_name="admin_youtube_token")
    return ResponseModel(status_code=200, detail={"stored": True, "status": state.get("status"),
                                                  "reason": state.get("reason")})


# ---------------------------------------------------------------------------
# User management (issue #1450, Phase 2 in #1603) — the admin's view of other accounts, and its
# writes: role, per-user disable, a one-time subscription comp, and an audit-log viewer.
#
# Session gated (`_require_user_admin`), never `X-Admin-Secret`: a human clicks these. Absent from
# every entry in `_SCOPE_SURFACES` by design — surfaces match on PATH not method, so listing users
# under the `agent` scope would hand a headless token the role write on the same path prefix.
# `tests/unit/api/test_admin_users_api.py` pins that absence.
#
# Design, the schema→screen mapping and the adversarial review: `docs/admin-user-management.md`.
# ---------------------------------------------------------------------------

class SubscriptionStatusFilter(StrEnum):
    """The `users.subscription_status` MySQL ENUM.

    Declared so an unknown filter is a 422 and not a SQL parameter nobody validated.
    """

    ACTIVE = 'active'
    INACTIVE = 'inactive'
    TRIAL = 'trial'
    CANCELLED = 'cancelled'
    PAST_DUE = 'past_due'


class LinkedInConnectionFilter(StrEnum):
    """The `users.linkedin_connection_status` MySQL ENUM."""

    CONNECTED = 'connected'
    EXPIRED = 'expired'
    DISCONNECTED = 'disconnected'


class AdminUserRoleRequest(BaseModel):
    """Body of `POST /admin/users/{user_id}/admin` — grant (`true`) or revoke (`false`).

    `user_id` is the TARGET and rides in the path; the ACTOR is whoever `_require_user_admin`
    resolves from this session, never a parameter.
    """

    session_token: str
    is_admin: bool


class AdminUserDisabledRequest(BaseModel):
    """Body of `POST /admin/users/{user_id}/disable` (issue #1603, Phase 2 of #1450).

    Same shape as `AdminUserRoleRequest`: `user_id` in the path is the TARGET, never a parameter.
    """

    session_token: str
    disabled: bool


class AdminSubscriptionGrantRequest(BaseModel):
    """Body of `POST /admin/users/{user_id}/subscription-grant` (issue #1603).

    A ONE-TIME, time-boxed comp — `days` bumps `subscription_current_period_end` once, at grant
    time. It is NOT a standing override: the next Stripe webhook write is expected and correct, and
    there is no precedence rule here because nothing here is meant to outlast that write. Capped at
    a year so a typo cannot mint a decade of free service.
    """

    session_token: str
    days: int = Field(..., ge=1, le=365)


def _admin_flags(row: dict) -> tuple:
    """`(effective, via_column, via_allowlist)` for one row.

    The SAME question `is_user_admin` answers, off data already fetched rather than a query per row.
    """
    via_column = bool(row.get("is_admin"))
    via_allowlist = (row.get("email") or "").strip().lower() in admin_email_allowlist()
    return (via_column or via_allowlist), via_column, via_allowlist


def _admin_user_summary(row: dict) -> dict[str, Any]:
    """The list row.

    Built field by field: every credential column is excluded by not being named, and there is no
    `has_password`-shaped boolean either — a present/absent answer over a credential is still an
    oracle (`docs/admin-user-management.md` §2).
    """
    effective, via_column, via_allowlist = _admin_flags(row)
    return {
        "id": row.get("id"),
        "email": row.get("email"),
        "linkedin_email": row.get("linkedin_email"),
        "is_admin": effective,
        "admin_via_column": via_column,
        "admin_via_allowlist": via_allowlist,
        "subscription_status": row.get("subscription_status"),
        "subscription_tier": row.get("subscription_tier"),
        "trial_ends_at": _main._utc_iso(row.get("trial_ends_at")),
        "linkedin_connection_status": row.get("linkedin_connection_status"),
        "last_login": _main._utc_iso(row.get("last_login")),
        "signed_up_at": _main._utc_iso(row.get("signed_up_at")),
        "activated_at": _main._utc_iso(row.get("activated_at")),
        "disabled": row.get("disabled_at") is not None,
        "disabled_at": _main._utc_iso(row.get("disabled_at")),
    }


def _admin_user_detail(row: dict) -> dict[str, Any]:
    """The drawer.

    The long tail an admin opens a row to read — never latitude/longitude (someone's home at 7
    decimal places), never `proxy_url` (it embeds user:pass) or `reply_inbound_token`.
    """
    detail = _admin_user_summary(row)
    detail.update({
        "public_uid": row.get("public_uid"),
        "linkedin_display_name": row.get("linkedin_display_name"),
        "email_verified_at": _main._utc_iso(row.get("email_verified_at")),
        "trial_started_at": _main._utc_iso(row.get("trial_started_at")),
        "subscription_current_period_end": _main._utc_iso(row.get("subscription_current_period_end")),
        "timezone": row.get("timezone"),
        "city": row.get("city"),
        "country": row.get("country"),
        "locale": row.get("locale"),
        "content_language": row.get("content_language"),
        "location_source": row.get("location_source"),
        "blog_url": row.get("blog_url"),
        "sitemap_url": row.get("sitemap_url"),
        "company_linked_in_url": row.get("company_linked_in_url"),
        "auto_schedule_posts": bool(row.get("auto_schedule_posts")),
        "content_buffer_days": row.get("content_buffer_days"),
        "content_buffer_max_posts": row.get("content_buffer_max_posts"),
        "last_login_inactivate_delay": row.get("last_login_inactivate_delay"),
        "avatar_disabled": bool(row.get("avatar_disabled")),
        "avatar_use_post_image": bool(row.get("avatar_use_post_image")),
        "avatar_use_carousel": bool(row.get("avatar_use_carousel")),
        "avatar_use_video": bool(row.get("avatar_use_video")),
        "avatar_use_newsletter": bool(row.get("avatar_use_newsletter")),
        "avatar_caption_overlay": bool(row.get("avatar_caption_overlay")),
        "updated_at": _main._utc_iso(row.get("updated_at")),
        "linkedin_connected_at": _main._utc_iso(row.get("linkedin_connected_at")),
        "voice_set_at": _main._utc_iso(row.get("voice_set_at")),
        "first_post_approved_at": _main._utc_iso(row.get("first_post_approved_at")),
        "caps_enabled_at": _main._utc_iso(row.get("caps_enabled_at")),
        # Read-only, and deliberately so: an admin editing another person's caps or voice changes
        # what LinkedIn sees as that person's writing, with no consent trail.
        "max_comments_per_day": row.get("max_comments_per_day"),
        "max_dms_per_day": row.get("max_dms_per_day"),
        "posts_per_week": row.get("posts_per_week"),
        "comment_length": row.get("comment_length"),
    })
    return detail


@router.get("/users", responses={
    200: {"description": "User list returned"},
    401: {"description": "Invalid or expired session"},
    403: {"description": "Admin access required"},
    503: {"description": "The user list could not be read"},
})
def admin_user_list(
    session_token: str,
    q: Optional[str] = None,
    subscription_status: Optional[SubscriptionStatusFilter] = None,
    linkedin_connection_status: Optional[LinkedInConnectionFilter] = None,
    is_admin: Optional[bool] = None,
    limit: int = Query(25, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> ResponseModel[dict[str, Any]]:
    """One page of accounts for the admin User Management screen (issue #1450).

    Two queries and nothing per row: the page itself (one LEFT JOIN to `onboarding_state`) and its
    COUNT under the same filters. `is_admin` filters on the EFFECTIVE answer — the column OR the
    `ADMIN_USER_EMAILS` allowlist — so the filter and each row's badge can never disagree.

    Either query failing is **503**, never an empty page: "no account matches that filter" is an
    answer an operator acts on, and a DB fault must not be able to say it.
    """
    _require_user_admin(session_token)
    filters = {
        "search": q,
        "subscription_status": subscription_status.value if subscription_status else None,
        "linkedin_connection_status": (linkedin_connection_status.value
                                       if linkedin_connection_status else None),
        "is_admin": is_admin,
    }
    rows = list_users_for_admin(limit=limit, offset=offset, **filters)
    total = count_users_for_admin(**filters)
    if rows is None or total is None:
        raise HTTPException(status_code=503, detail="Could not read the user list. Try again.")
    return ResponseModel(status_code=200, detail={
        "items": [_admin_user_summary(r) for r in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
    })


@router.get("/users/{user_id}", responses={
    200: {"description": "User detail returned"},
    401: {"description": "Invalid or expired session"},
    403: {"description": "Admin access required"},
    404: {"description": "No such user"},
})
def admin_user_detail(user_id: int, session_token: str) -> ResponseModel[dict[str, Any]]:
    """Everything behind a row — the 26 columns a table cannot carry, minus every credential."""
    _require_user_admin(session_token)
    row = get_user_for_admin(user_id)
    if not row:
        raise HTTPException(status_code=404, detail="User not found")
    return ResponseModel(status_code=200, detail=_admin_user_detail(row))


@router.post("/users/{user_id}/admin", responses={
    200: {"description": "Role change applied (or already in that state)"},
    401: {"description": "Invalid or expired session"},
    403: {"description": "Admin access required, or step-up required"},
    404: {"description": "No such user"},
    409: {"description": "Refused: self-revoke, the last admin, or an allowlist admin"},
    503: {"description": "The admin count could not be read"},
})
def admin_set_user_role(user_id: int, request: AdminUserRoleRequest,
                        http_request: Request = None) -> ResponseModel[dict[str, Any]]:
    """Grant or remove another account's admin role — the ONE write on this surface (issue #1450).

    Four refusals, each for a failure that has a name:

    * **Self-revoke** is refused outright. The last-admin count would catch the lockout, but a
      second admin demoting themselves mid-incident is an ordinary mistake with no legitimate
      version — the deliberate one is asking the other admin to do it.
    * **The last effective admin** may not be demoted. "Effective" is the column OR the
      `ADMIN_USER_EMAILS` allowlist, i.e. exactly what `is_user_admin` decides with; a guard that
      counted the column alone would refuse a safe revoke on an allowlist-run deployment and, worse,
      allow the last column-admin to go on one that is not.
    * **An allowlist admin** cannot be revoked here at all: the column is already 0, so the write
      would change nothing and report success while the person stays an admin. `ADMIN_USER_EMAILS`
      is env, and env is where that is undone.
    * **An unreadable admin count** is 503, never a guess. 0 admins is the one answer that must not
      be inferred from a DB fault.

    Step-up gated: granting admin is not touching a credential, it is minting the authority to
    touch everyone's.
    """
    actor_id = _require_user_admin(request.session_token)
    # Deferred: `routers.user` and `routers.admin` are both imported by `main`, so a module-level
    # import here would run one router's body during the other's. The ONE step-up refusal shape
    # (403 `step_up_required` + the methods available) is worth reaching for rather than restating.
    from cqc_lem.api.routers.user import _require_step_up
    _require_step_up(actor_id, request.session_token, "admin_role_change",
                     http_request=http_request)

    target = get_user_for_admin(user_id)
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    effective, via_column, via_allowlist = _admin_flags(target)

    if not request.is_admin and int(user_id) == int(actor_id):
        raise HTTPException(status_code=409,
                            detail="You cannot remove your own admin access. Ask another admin.")
    if not request.is_admin and via_allowlist:
        raise HTTPException(
            status_code=409,
            detail="This account is an admin through the ADMIN_USER_EMAILS allowlist. Remove the "
                   "address there — clearing the flag here would change nothing.")
    if request.is_admin == effective and via_column == request.is_admin:
        # Already in the requested state. Said explicitly, because MySQL reports zero changed rows
        # for a no-op UPDATE and that is indistinguishable from "no such user".
        return ResponseModel(status_code=200, detail={
            "user_id": user_id, "is_admin": effective, "changed": False})

    if not request.is_admin:
        admin_count = count_admin_users()
        if admin_count is None:
            raise HTTPException(status_code=503,
                                detail="Could not verify how many admins remain. Try again.")
        if admin_count <= 1:
            raise HTTPException(
                status_code=409,
                detail="This is the last admin account. Grant admin to someone else first.")

    if not set_user_admin(user_id, request.is_admin):
        raise HTTPException(status_code=500, detail="Could not update the admin role")

    event = AuthAuditEvent.ADMIN_GRANTED if request.is_admin else AuthAuditEvent.ADMIN_REVOKED
    # Keyed on the TARGET, actor in `details` — the reading `user_id` already has on every other
    # row in that table, and it is what puts the change in the affected user's own Security card.
    record_auth_event(event, user_id=user_id, success=True,
                      ip=_main._client_ip(http_request), user_agent=_main._user_agent(http_request),
                      details={"actor_user_id": actor_id})
    log_info("Admin role changed", user_id=actor_id, task_name="admin_set_user_role",
             target_user_id=user_id, admin_action=str(event))
    return ResponseModel(status_code=200, detail={
        "user_id": user_id, "is_admin": request.is_admin, "changed": True})


@router.post("/users/{user_id}/disable", responses={
    200: {"description": "Disabled state applied (or already in that state)"},
    401: {"description": "Invalid or expired session"},
    403: {"description": "Admin access required, or step-up required"},
    404: {"description": "No such user"},
    409: {"description": "Refused: cannot disable your own account"},
})
def admin_set_user_disabled(user_id: int, request: AdminUserDisabledRequest,
                            http_request: Request = None) -> ResponseModel[dict[str, Any]]:
    """Disable or re-enable another account (issue #1603, Phase 2 of #1450).

    Honoured by `get_active_user_ids()` — the ONE per-user gate every automation lane reads — so a
    disabled account stops there, not behind a badge nobody checks. Self-disable is refused
    outright (409), the same shape as the self-revoke guard on the admin-role write: there is no
    legitimate version of locking out the account you are using right now.
    """
    actor_id = _require_user_admin(request.session_token)
    from cqc_lem.api.routers.user import _require_step_up
    _require_step_up(actor_id, request.session_token, "admin_user_disable",
                     http_request=http_request)

    if int(user_id) == int(actor_id):
        raise HTTPException(status_code=409,
                            detail="You cannot disable your own account. Ask another admin.")

    target = get_user_for_admin(user_id)
    if not target:
        raise HTTPException(status_code=404, detail="User not found")

    already_disabled = target.get("disabled_at") is not None
    if request.disabled == already_disabled:
        return ResponseModel(status_code=200, detail={
            "user_id": user_id, "disabled": already_disabled, "changed": False})

    if not set_user_disabled(user_id, request.disabled):
        raise HTTPException(status_code=500, detail="Could not update the disabled state")

    event = AuthAuditEvent.ADMIN_USER_DISABLED if request.disabled else AuthAuditEvent.ADMIN_USER_ENABLED
    record_auth_event(event, user_id=user_id, success=True,
                      ip=_main._client_ip(http_request), user_agent=_main._user_agent(http_request),
                      details={"actor_user_id": actor_id})
    log_info("Admin user disabled state changed", user_id=actor_id,
             task_name="admin_set_user_disabled", target_user_id=user_id,
             admin_action=str(event))
    return ResponseModel(status_code=200, detail={
        "user_id": user_id, "disabled": request.disabled, "changed": True})


@router.post("/users/{user_id}/subscription-grant", responses={
    200: {"description": "Subscription extended"},
    401: {"description": "Invalid or expired session"},
    403: {"description": "Admin access required, or step-up required"},
    404: {"description": "No such user"},
    500: {"description": "Could not apply the grant"},
})
def admin_grant_subscription(user_id: int, request: AdminSubscriptionGrantRequest,
                             http_request: Request = None) -> ResponseModel[dict[str, Any]]:
    """Grant a one-time, time-boxed subscription extension (issue #1603).

    A direct bump to `subscription_current_period_end`, applied once at grant time — NOT a
    standing override. `subscription_status`/`subscription_tier`/`subscription_current_period_end`
    stay Stripe-webhook-owned; the next real webhook write is expected and correct after this, and
    there is nothing standing here for it to silently revert.
    """
    actor_id = _require_user_admin(request.session_token)
    from cqc_lem.api.routers.user import _require_step_up
    _require_step_up(actor_id, request.session_token, "admin_subscription_grant",
                     http_request=http_request)

    target = get_user_for_admin(user_id)
    if not target:
        raise HTTPException(status_code=404, detail="User not found")

    if not grant_subscription_extension(user_id, request.days):
        raise HTTPException(status_code=500, detail="Could not apply the subscription grant")

    record_auth_event(AuthAuditEvent.ADMIN_SUBSCRIPTION_GRANTED, user_id=user_id, success=True,
                      ip=_main._client_ip(http_request), user_agent=_main._user_agent(http_request),
                      details={"actor_user_id": actor_id, "days_granted": request.days})
    log_info("Admin granted a subscription extension", user_id=actor_id,
             task_name="admin_grant_subscription", target_user_id=user_id, days=request.days)
    refreshed = get_user_for_admin(user_id) or {}
    return ResponseModel(status_code=200, detail={
        "user_id": user_id, "days_granted": request.days,
        "subscription_current_period_end": _main._utc_iso(
            refreshed.get("subscription_current_period_end"))})


@router.get("/audit-log", responses={
    200: {"description": "Audit log page returned"},
    401: {"description": "Invalid or expired session"},
    403: {"description": "Admin access required"},
    503: {"description": "The audit log could not be read"},
})
def admin_audit_log(
    session_token: str,
    user_id: Optional[int] = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> ResponseModel[dict[str, Any]]:
    """The admin-facing view over `auth_audit_log` (issue #1603): who changed whose role/state.

    Who, when, from where. Never returns `ip_hash`: it is stored for forensics, not for a screen.
    Either query failing is 503, never an empty page, for the same reason as the user list: a fault
    must not be able to say "nothing happened" when it means "could not tell".
    """
    _require_user_admin(session_token)
    rows = list_auth_audit_log_for_admin(user_id=user_id, limit=limit, offset=offset)
    total = count_auth_audit_log_for_admin(user_id=user_id)
    if rows is None or total is None:
        raise HTTPException(status_code=503, detail="Could not read the audit log. Try again.")
    return ResponseModel(status_code=200, detail={
        "items": [
            {
                "id": r.get("id"),
                "user_id": r.get("user_id"),
                "email": r.get("email"),
                "event": r.get("event"),
                "success": bool(r.get("success")),
                "user_agent": r.get("user_agent"),
                "session_id": r.get("session_id"),
                "details": r.get("details"),
                "created_at": _main._utc_iso(r.get("created_at")),
            }
            for r in rows
        ],
        "total": total,
        "limit": limit,
        "offset": offset,
    })


from cqc_lem.api import main as _main  # noqa: E402  — last; see the module docstring
