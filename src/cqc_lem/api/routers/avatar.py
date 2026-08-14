"""`/api/avatar/*` — LoRA avatar training, samples, approval and preferences, split from `main.py`.

Second slice of the router split (#1154); the mechanic is the one #1178 established.

The auth kernel stays in `main`: `get_session_user_id` is reached as `_main.get_session_user_id`,
an ATTRIBUTE resolved at REQUEST time. That is what keeps `patch("cqc_lem.api.main.
get_session_user_id")` binding what these handlers read, without moving a 33-symbol closure that
carries every documented auth invariant. The avatar db functions DID move, so a patch aimed at
`main` for one of those fails loudly and has to be re-pointed here.

`from cqc_lem.api import main as _main` sits at the BOTTOM so both import orders see a complete
router; `routers/__init__.py` has the prefix rule and the reasoning.
"""

from typing import Any, Optional

from fastapi import (
    APIRouter,  # noqa: E402  — grouped with the router declaration it serves
    File,
    Form,
    HTTPException,
    UploadFile,
)
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

from cqc_lem.api.models import ResponseModel, error_responses
from cqc_lem.utilities.db import (
    AVATAR_APPROVAL_APPROVED,
    AVATAR_APPROVAL_REJECTED,
    claim_avatar_sample_render,
    deduct_avatar_credit,
    get_active_avatar,
    get_avatar_credit_balance,
    get_avatar_preferences,
    get_avatar_training,
    get_avatar_trainings,
    get_user_subscription_info,
    insert_avatar_training,
    release_avatar_sample_render,
    set_active_avatar,
    set_avatar_approval,
    update_avatar_attributes,
    update_avatar_preferences,
    update_avatar_training_status,
)
from cqc_lem.utilities.logger import log_error

router = APIRouter(prefix="/api/avatar")


class AvatarCreditCheckoutRequest(BaseModel):
    """Body of `POST /avatar/credits/checkout`.

    `package` is validated against `stripe_util.AVATAR_CREDIT_PACKAGES` in the handler (400), not here.
    """

    session_token: str
    package: str
    success_url: str
    cancel_url: str


class AvatarActivateRequest(BaseModel):
    """The session-only body shared by every per-avatar action.

    Regenerate samples, approve, reject, activate — the avatar itself is named by the path, and
    authorised there.
    """

    session_token: str


class AvatarAttributesRequest(BaseModel):
    """Self-declared likeness attributes (issue #744, decision 3A). Both fields are optional and
    a null clears the declaration — an undeclared attribute renders no subject clause at all.
    """
    session_token: str
    gender_presentation: Optional[str] = None
    age_band: Optional[str] = None


class AvatarPreferencesRequest(BaseModel):
    """Per-user avatar guardrails. Every flag is optional so the SPA can PATCH one toggle."""
    session_token: str
    avatar_disabled: Optional[bool] = None
    avatar_use_post_image: Optional[bool] = None
    avatar_use_carousel: Optional[bool] = None
    avatar_use_video: Optional[bool] = None
    avatar_use_newsletter: Optional[bool] = None
    # Not a render surface: permission for burned captions to sit on an avatar-led video frame
    # (issue #1278).
    avatar_caption_overlay: Optional[bool] = None


@router.get("/credits", responses={
    200: {"description": "Credit balance and active avatar returned"},
    **{k: v for k, v in error_responses.items() if k in [401]}
})
def get_avatar_credits_endpoint(session_token: str) -> ResponseModel[dict[str, Any]]:
    """Avatar credit balance plus the currently active avatar.

    The SPA needs both together to decide whether to offer training or a render.
    """
    user_id = _main.get_session_user_id(session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    balance = get_avatar_credit_balance(user_id)
    active = get_active_avatar(user_id)
    return ResponseModel(status_code=200, detail={"balance": balance, "active_avatar": active})


@router.post("/credits/checkout", responses={
    200: {"description": "Stripe checkout URL returned"},
    **{k: v for k, v in error_responses.items() if k in [400, 401]}
})
def avatar_credits_checkout(request: AvatarCreditCheckoutRequest) -> ResponseModel[dict[str, Any]]:
    """Stripe hand-off for an avatar-credit package.

    The credits are NOT granted here — the `checkout.session.completed` webhook does that, idempotently on the
    session id.
    """
    user_id = _main.get_session_user_id(request.session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")

    subscription = get_user_subscription_info(user_id)
    stripe_customer_id = subscription.get("stripe_customer_id") if subscription else None
    if not stripe_customer_id:
        raise HTTPException(status_code=400, detail="No Stripe customer record — contact support")

    from cqc_lem.utilities.stripe_util import AVATAR_CREDIT_PACKAGES, create_avatar_credits_checkout
    if request.package not in AVATAR_CREDIT_PACKAGES:
        raise HTTPException(status_code=400, detail=f"Unknown package '{request.package}'")

    url = create_avatar_credits_checkout(
        stripe_customer_id, request.package, request.success_url, request.cancel_url
    )
    if not url:
        raise HTTPException(status_code=500, detail="Could not create Stripe checkout session")
    return ResponseModel(status_code=200, detail={"checkout_url": url})


@router.post("/training", responses={
    200: {"description": "Training started"},
    **{k: v for k, v in error_responses.items() if k in [400, 401, 402]}
})
async def start_avatar_training_endpoint(
    session_token: str = Form(...),
    trigger_word: str = Form(...),
    photos: UploadFile = File(...),
) -> ResponseModel[dict[str, Any]]:
    """Train a LoRA avatar from an uploaded photo ZIP. Costs one avatar credit.

    Both size limits guard a zip bomb, which is why the UNCOMPRESSED total is checked as well as
    the upload: 50 MB compressed can expand to gigabytes. The credit is deducted only AFTER
    Replicate accepted the job, so a failed start never charges.
    """
    user_id = _main.get_session_user_id(session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")

    balance = get_avatar_credit_balance(user_id)
    if balance < 1:
        raise HTTPException(status_code=402, detail="Insufficient avatar credits. Purchase credits to train a new avatar.")

    zip_bytes = await photos.read()
    if not zip_bytes:
        raise HTTPException(status_code=400, detail="No file data received")

    # The zip scan and the multi-MB Replicate upload are both blocking, and the upload is the
    # slowest thing any route in this file does. Off the event loop — a plain `def` endpoint would
    # have got this threadpool for free, and only the `await photos.read()` above forces `async`.
    return await run_in_threadpool(_start_avatar_training, user_id, zip_bytes, trigger_word)


def _start_avatar_training(user_id: int, zip_bytes: bytes, trigger_word: str) -> ResponseModel[dict[str, Any]]:
    """Validate the upload, start the Replicate job, then charge for it (see the route above)."""
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


@router.get("/trainings", responses={
    200: {"description": "Avatar trainings listed"},
    **{k: v for k, v in error_responses.items() if k in [401]}
})
def list_avatar_trainings(session_token: str) -> ResponseModel[list[dict[str, Any]]]:
    """Every avatar training this user has started, in whatever state it reached."""
    user_id = _main.get_session_user_id(session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    trainings = get_avatar_trainings(user_id)
    return ResponseModel(status_code=200, detail=trainings)


@router.get("/training/{avatar_db_id}/status", responses={
    200: {"description": "Training status synced"},
    **{k: v for k, v in error_responses.items() if k in [401, 404]}
})
def sync_avatar_training_status(avatar_db_id: int, session_token: str) -> ResponseModel[dict[str, Any]]:
    """Poll Replicate for a training's state and write it back — the SPA's progress call.

    A training already in a terminal state is NOT re-polled, and the sample renders it may trigger
    on first reaching `succeeded` are claimed, so repeated polling cannot spend inference money
    over and over. The row is looked up within the caller's own trainings, so a foreign id is a 404.
    """
    user_id = _main.get_session_user_id(session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")

    trainings = get_avatar_trainings(user_id)
    match = next((t for t in trainings if t["id"] == avatar_db_id), None)
    if not match:
        raise HTTPException(status_code=404, detail="Training not found")

    if match["status"] in ("succeeded", "failed", "canceled"):
        _queue_avatar_samples_if_due(match, user_id)
        return ResponseModel(status_code=200, detail=match)

    from cqc_lem.utilities.avatar.replicate_avatar import poll_training_status
    new_status, model_ref = poll_training_status(match["training_id"])
    update_avatar_training_status(match["training_id"], new_status, model_ref)
    match["status"] = new_status
    if model_ref:
        match["model_ref"] = model_ref
    _queue_avatar_samples_if_due(match, user_id)
    return ResponseModel(status_code=200, detail=match)


def _queue_avatar_samples_if_due(avatar: dict, user_id: int) -> None:
    """Kick off the preview renders the moment a training reaches 'succeeded' (issue #744).

    The claim is what makes this idempotent: a repeated (or double-clicked) status poll arriving
    while the first render is still running loses the claim and queues nothing, so polling cannot
    spend inference money over and over. Best-effort: a broker hiccup must not fail the status
    read — the claim is handed back so the next poll can try again.
    """
    if avatar.get("status") != "succeeded" or not avatar.get("model_ref"):
        return
    if avatar.get("sample_paths"):
        return
    if not claim_avatar_sample_render(user_id, avatar["id"]):
        return
    try:
        from cqc_lem.app.run_avatar import render_avatar_samples_task
        # retry=False: this is a side-effect of a status poll the SPA makes every 20s. A broker
        # outage must fail it in one attempt, not hold the HTTP response open through a retry
        # ladder — the next poll (or the explicit Regenerate button) queues it again.
        render_avatar_samples_task.apply_async(
            kwargs={"avatar_id": avatar["id"], "user_id": user_id}, retry=False)
    except Exception as e:
        release_avatar_sample_render(user_id, avatar["id"])
        log_error("Could not queue avatar sample rendering", exc=e, user_id=user_id)


def _require_own_avatar(user_id: int, avatar_db_id: int) -> dict:
    avatar = get_avatar_training(user_id, avatar_db_id)
    if not avatar:
        raise HTTPException(status_code=404, detail="Training not found")
    return avatar


@router.get("/training/{avatar_db_id}/samples", responses={
    200: {"description": "Avatar samples returned"},
    **{k: v for k, v in error_responses.items() if k in [401, 404]}
})
def get_avatar_samples(avatar_db_id: int, session_token: str) -> ResponseModel[dict[str, Any]]:
    """The rendered preview set plus everything the approval UI needs to decide."""
    user_id = _main.get_session_user_id(session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")

    avatar = _require_own_avatar(user_id, avatar_db_id)
    from cqc_lem.utilities.avatar.samples import sample_payload
    from cqc_lem.utilities.env_constants import AVATAR_SAMPLE_REGEN_MAX
    return ResponseModel(status_code=200, detail={
        "avatar_id": avatar["id"],
        "status": avatar["status"],
        "approval_status": avatar["approval_status"],
        "samples": sample_payload(avatar),
        "samples_generated_at": avatar["samples_generated_at"],
        "sample_regen_count": avatar["sample_regen_count"],
        "sample_regen_remaining": max(0, AVATAR_SAMPLE_REGEN_MAX - avatar["sample_regen_count"]),
        "gender_presentation": avatar["gender_presentation"],
        "age_band": avatar["age_band"],
        "attributes_confirmed_at": avatar["attributes_confirmed_at"],
    })


@router.post("/training/{avatar_db_id}/samples", responses={
    200: {"description": "Sample regeneration queued"},
    **{k: v for k, v in error_responses.items() if k in [400, 401, 404, 429, 500]}
})
def regenerate_avatar_samples(avatar_db_id: int, request: AvatarActivateRequest) -> ResponseModel[str]:
    """Re-roll the preview set. Capped by AVATAR_SAMPLE_REGEN_MAX on top of the credit ledger —
    samples cost inference money but no training credit, so without a cap this is unbounded.
    """
    user_id = _main.get_session_user_id(request.session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")

    avatar = _require_own_avatar(user_id, avatar_db_id)
    if avatar["status"] != "succeeded" or not avatar["model_ref"]:
        raise HTTPException(status_code=400, detail="Only a succeeded training can render samples")

    from cqc_lem.utilities.env_constants import AVATAR_SAMPLE_REGEN_MAX
    # Reserve the re-roll in the same statement that checks the cap. Reading the counter and
    # queueing separately let a double-click (the counter only moves when a render FINISHES)
    # queue two full three-image renders against one reading — an unbounded spend is exactly
    # what the cap exists to stop. The task hands the reservation back if it renders nothing.
    if not claim_avatar_sample_render(user_id, avatar_db_id, regeneration=True,
                                      max_regenerations=AVATAR_SAMPLE_REGEN_MAX):
        raise HTTPException(
            status_code=429,
            detail=f"Sample regeneration limit reached ({AVATAR_SAMPLE_REGEN_MAX}). "
                   f"Train a new avatar with better photos instead.")

    try:
        from cqc_lem.app.run_avatar import render_avatar_samples_task
        render_avatar_samples_task.apply_async(
            kwargs={"avatar_id": avatar_db_id, "user_id": user_id, "count_regeneration": True})
    except Exception as e:
        release_avatar_sample_render(user_id, avatar_db_id, regeneration=True)
        log_error("Could not queue avatar sample regeneration", exc=e, user_id=user_id)
        raise HTTPException(status_code=500, detail="Could not queue sample regeneration")
    return ResponseModel(status_code=200, detail="Sample regeneration queued")


@router.put("/training/{avatar_db_id}/attributes", responses={
    200: {"description": "Attributes saved"},
    **{k: v for k, v in error_responses.items() if k in [401, 404, 500]}
})
def update_avatar_attributes_endpoint(avatar_db_id: int,
                                      request: AvatarAttributesRequest) -> ResponseModel[Optional[dict[str, Any]]]:
    """Store the user's SELF-DECLARED likeness attributes (issue #744, decision 3A).

    Nothing here inspects the user's photos — an unrecognized value is stored as NULL, which
    renders an empty subject clause rather than a guess.
    """
    user_id = _main.get_session_user_id(request.session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")

    _require_own_avatar(user_id, avatar_db_id)
    if not update_avatar_attributes(user_id, avatar_db_id,
                                    request.gender_presentation, request.age_band):
        raise HTTPException(status_code=500, detail="Could not save avatar attributes")
    return ResponseModel(status_code=200, detail=get_avatar_training(user_id, avatar_db_id))


@router.post("/training/{avatar_db_id}/approve", responses={
    200: {"description": "Avatar approved"},
    **{k: v for k, v in error_responses.items() if k in [400, 401, 404, 500]}
})
def approve_avatar(avatar_db_id: int, request: AvatarActivateRequest) -> ResponseModel[str]:
    """Approve an avatar for use. Requires samples to exist — approving an avatar nobody has
    seen is the exact blind activation this gate was added to remove.
    """
    user_id = _main.get_session_user_id(request.session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")

    avatar = _require_own_avatar(user_id, avatar_db_id)
    if avatar["status"] != "succeeded":
        raise HTTPException(status_code=400, detail="Only succeeded trainings can be approved")
    if not avatar["sample_paths"]:
        raise HTTPException(status_code=400,
                            detail="Render preview samples before approving this avatar")

    if not set_avatar_approval(user_id, avatar_db_id, AVATAR_APPROVAL_APPROVED):
        raise HTTPException(status_code=500, detail="Could not approve avatar")
    return ResponseModel(status_code=200, detail="Avatar approved")


@router.post("/training/{avatar_db_id}/reject", responses={
    200: {"description": "Avatar rejected"},
    **{k: v for k, v in error_responses.items() if k in [401, 404, 500]}
})
def reject_avatar(avatar_db_id: int, request: AvatarActivateRequest) -> ResponseModel[str]:
    """Reject an avatar. Also deactivates it — leaving a rejected likeness active would keep
    publishing exactly the media the user just rejected.
    """
    user_id = _main.get_session_user_id(request.session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")

    _require_own_avatar(user_id, avatar_db_id)
    if not set_avatar_approval(user_id, avatar_db_id, AVATAR_APPROVAL_REJECTED):
        raise HTTPException(status_code=500, detail="Could not reject avatar")
    return ResponseModel(status_code=200, detail="Avatar rejected")


@router.get("/preferences", responses={
    200: {"description": "Avatar guardrail preferences returned"},
    **{k: v for k, v in error_responses.items() if k in [401]}
})
def get_avatar_preferences_endpoint(session_token: str) -> ResponseModel[dict[str, Any]]:
    """The per-user avatar guardrails.

    The opt-ins `resolve_avatar_for` reads before any likeness renders, plus the master
    `avatar_disabled` switch.
    """
    user_id = _main.get_session_user_id(session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    return ResponseModel(status_code=200, detail=get_avatar_preferences(user_id))


@router.put("/preferences", responses={
    200: {"description": "Avatar guardrail preferences updated"},
    **{k: v for k, v in error_responses.items() if k in [400, 401, 500]}
})
def update_avatar_preferences_endpoint(request: AvatarPreferencesRequest) -> ResponseModel[dict[str, Any]]:
    """PATCH one or more avatar guardrails.

    `exclude_none` is what makes it a patch, so the SPA can send a single toggle without resetting
    the rest. An all-None body is a 400, not a no-op 200.
    """
    user_id = _main.get_session_user_id(request.session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")

    prefs = request.model_dump(exclude={"session_token"}, exclude_none=True)
    if not prefs:
        raise HTTPException(status_code=400, detail="No preferences supplied")
    if not update_avatar_preferences(user_id, prefs):
        raise HTTPException(status_code=500, detail="Could not update avatar preferences")
    return ResponseModel(status_code=200, detail=get_avatar_preferences(user_id))


@router.put("/training/{avatar_db_id}/activate", responses={
    200: {"description": "Avatar activated"},
    **{k: v for k, v in error_responses.items() if k in [400, 401, 404]}
})
def activate_avatar(avatar_db_id: int, request: AvatarActivateRequest) -> ResponseModel[str]:
    """Make a trained avatar the one that renders.

    Both gates are hard: the training must have SUCCEEDED, and the user must have reviewed and APPROVED its preview
    samples first — activating an unreviewed likeness is how a bad one reaches LinkedIn as the author's face.
    """
    user_id = _main.get_session_user_id(request.session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")

    match = _require_own_avatar(user_id, avatar_db_id)
    if match["status"] != "succeeded":
        raise HTTPException(status_code=400, detail="Only succeeded trainings can be activated")
    if match["approval_status"] != AVATAR_APPROVAL_APPROVED:
        raise HTTPException(status_code=400,
                            detail="Review the preview samples and approve this avatar first")

    if set_active_avatar(user_id, avatar_db_id):
        return ResponseModel(status_code=200, detail="Avatar activated")
    raise HTTPException(status_code=500, detail="Could not activate avatar")


from cqc_lem.api import main as _main  # noqa: E402  — last; see the module docstring
