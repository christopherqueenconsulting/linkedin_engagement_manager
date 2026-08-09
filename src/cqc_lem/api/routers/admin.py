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
from typing import List, Optional

from celery import states as celery_states
from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, model_validator

from cqc_lem.api.models import ResponseModel
from cqc_lem.app.engagement.feed import automate_commenting, consolidate_duplicate_comments_for_user
from cqc_lem.app.engagement.outreach import automate_appreciation_dms_for_user, send_private_dm
from cqc_lem.app.engagement.posting import automate_reply_commenting
from cqc_lem.utilities.db import (
    FeedbackStatus,
    PostType,
    get_feedback_by_id,
    get_feedback_list,
    get_post_buyer_stage,
    get_post_type,
    get_post_user_id,
    get_user_geo,
    is_user_admin,
    record_feedback_review,
    replace_video_url_base,
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
                           x_admin_secret: Optional[str] = Header(default=None)) -> ResponseModel:
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
def admin_resume_automation(x_admin_secret: Optional[str] = Header(default=None)) -> ResponseModel:
    """Lift a manual automation pause immediately."""
    _require_admin(x_admin_secret)
    from cqc_lem.utilities.linkedin.rate_limit import resume_automation
    return ResponseModel(status_code=200, detail={"resumed": resume_automation()})


@router.get("/automation-status", responses={200: {"description": "Automation status"},
                                                   403: {"description": "Forbidden"}})
def admin_automation_status(x_admin_secret: Optional[str] = Header(default=None)) -> ResponseModel:
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
) -> ResponseModel:
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
) -> ResponseModel:
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
) -> ResponseModel:
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

def _queued(result, task: str, **detail) -> ResponseModel:
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
) -> ResponseModel:
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
) -> ResponseModel:
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
) -> ResponseModel:
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
) -> ResponseModel:
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
) -> ResponseModel:
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
) -> ResponseModel:
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
) -> ResponseModel:
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
) -> ResponseModel:
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
def admin_youtube_status(session_token: str, live: bool = False) -> ResponseModel:
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
                            x_admin_secret: Optional[str] = Header(default=None)) -> ResponseModel:
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


from cqc_lem.api import main as _main  # noqa: E402  — last; see the module docstring
