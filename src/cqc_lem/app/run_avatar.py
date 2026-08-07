"""Avatar background work — sample rendering for the preview/approval gate (issue #744).

Rendering three LoRA images takes tens of seconds, far too long for the HTTP request that
notices the training finished, so it runs here. The task is idempotent: it re-renders the fixed
scene set and overwrites the stored paths.
"""
from typing import Optional

from cqc_lem.app.my_celery import app as shared_task
from cqc_lem.utilities.db import (
    AVATAR_APPROVAL_PENDING,
    get_avatar_training,
    release_avatar_sample_render,
    set_avatar_approval,
    update_avatar_samples,
)
from cqc_lem.utilities.logger import log_info, log_warning


@shared_task.task
def render_avatar_samples_task(avatar_id: int, user_id: int,
                               count_regeneration: bool = False) -> Optional[int]:
    """Render + persist the sample set for one avatar. Returns how many samples were stored.

    The caller reserved this render before queueing (``claim_avatar_sample_render``); every path
    that ships no images hands the reservation back, so a failed render never costs the user one
    of their re-rolls and never wedges the automatic first render.
    """
    avatar = get_avatar_training(user_id, avatar_id)
    if not avatar:
        release_avatar_sample_render(user_id, avatar_id, regeneration=count_regeneration)
        log_warning("Avatar sample render skipped: avatar not found", user_id=user_id,
                    task_name="render_avatar_samples")
        return None
    if avatar.get("status") != "succeeded" or not avatar.get("model_ref"):
        release_avatar_sample_render(user_id, avatar_id, regeneration=count_regeneration)
        log_warning("Avatar sample render skipped: training has not succeeded", user_id=user_id,
                    task_name="render_avatar_samples")
        return None

    from cqc_lem.utilities.avatar.samples import render_avatar_samples
    samples = render_avatar_samples(avatar)
    if not samples:
        release_avatar_sample_render(user_id, avatar_id, regeneration=count_regeneration)
        log_warning("Avatar sample render produced nothing — leaving the avatar un-approvable",
                    user_id=user_id, task_name="render_avatar_samples")
        return 0

    update_avatar_samples(avatar_id, samples)
    # New samples invalidate an earlier verdict: the user approved the images they SAW, and these
    # are different images. Re-approving is one click; publishing an unreviewed likeness is not.
    if avatar.get("approval_status") != AVATAR_APPROVAL_PENDING:
        set_avatar_approval(user_id, avatar_id, AVATAR_APPROVAL_PENDING)
    log_info(f"Stored {len(samples)} avatar samples", user_id=user_id,
             task_name="render_avatar_samples")
    return len(samples)
