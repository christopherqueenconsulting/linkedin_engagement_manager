"""Curated outside sources: the collector beat, the slot drafter, and the publish gate.

``docs/curated-sources.md`` is the posture; ``utilities/curated_sources.py`` owns every rule this
module applies. Three entry points:

- ``collect_curated_sources`` (beat, daily) — reads the RSS allowlist and the government-data
  watchlist ONCE and records candidates for every active user with ``CURATED_SOURCES_ENABLED``.
  LinkedIn feed/roster candidates arrive from the feed lane's own hook
  (``curated_collectors.record_linkedin_candidate``), not from here.
- ``curated_content_for_slot`` — called by the content plan for a TEXT slot. It CLAIMS the slot
  (the affiliate-promo precedent) only when the flag is on, the slot is ``value``/``authority``,
  and no curated post sits within two posts either side (at most one in three). Otherwise it
  returns None and the ordinary post is written.
- ``curated_publish_refusal`` / ``publish_curated_post`` — the publish path. A curated post
  publishes ONLY when its approval was a human's (``approved_by = 'user:<id>'``): the automatic
  approvers (``PostApprover``) are refused, and the post goes back to PENDING for its owner.

Curated drafts are never auto-approved: ``run_content_plan._may_auto_approve`` holds every one of
them PENDING.
"""

import os
from typing import Callable, Optional

from cqc_lem.app.my_celery import app as shared_task
from cqc_lem.utilities.ai.curated_commentary import (
    extract_chart_facts,
    generate_curated_commentary,
    validated_chart,
)
from cqc_lem.utilities.curated_collectors import collect_gov_data, collect_rss, curated_enabled
from cqc_lem.utilities.curated_sources import (
    BLOCK_NO_PROVENANCE,
    CEILING_WINDOW,
    STATUS_BLOCKED,
    STATUS_DRAFTED,
    STATUS_PUBLISHED,
    TREATMENT_LINK,
    TREATMENT_RECHART,
    TREATMENT_RESHARE,
    TREATMENTS,
    ceiling_allows,
    load_feed_allowlist,
    pick_treatment,
    political_reason,
    provenance_reason,
    screen_candidate,
    slot_allows,
    treatment_allowed,
    with_credit,
)
from cqc_lem.utilities.db import (
    PostStatus,
    attach_curated_source_to_post,
    get_active_user_ids,
    get_curated_neighbors,
    get_draftable_curated_sources,
    update_curated_source_status,
    update_db_post_image_url,
)
from cqc_lem.utilities.logger import log_debug, log_info, log_warning
from cqc_lem.utilities.observability import track_curated_source

# Each candidate that passes the gates costs a commentary call, so a slot tries a few, not all.
DRAFT_CANDIDATES = 3
# The post statuses a publish may start from: the scheduler moves APPROVED to SCHEDULED before
# dispatch, so both are "approved" for this check.
_PUBLISHABLE = (PostStatus.APPROVED.value, PostStatus.SCHEDULED.value)
_HUMAN_APPROVER_PREFIX = "user:"


# --- Collect -------------------------------------------------------------------------------------

@shared_task.task(name="cqc_lem.app.run_curated_sources.collect_curated_sources")
def collect_curated_sources() -> str:
    """Daily: the RSS allowlist + gov-data watchlist into ``curated_sources`` for enabled users."""
    users = [uid for uid in (get_active_user_ids() or []) if curated_enabled(uid)]
    if not users:
        # DEBUG: the flag is OFF by default, so an empty run is the normal state, not a fault.
        log_debug("Curated sources: no enabled users — nothing collected",
                  task_name="collect_curated_sources")
        return "No users with curated sources enabled"
    allowlist = load_feed_allowlist()
    rss = collect_rss(users, allowlist)
    gov = collect_gov_data(users, allowlist)
    new, blocked = rss["new"] + gov["new"], rss["blocked"] + gov["blocked"]
    status = "failed" if rss["failed_feeds"] and not rss["feeds"] and not gov["series"] else "ok"
    track_curated_source("collect", status, new=new, blocked=blocked)
    log_info(f"Curated sources collected: {new} new, {blocked} blocked, "
             f"{rss['failed_feeds']} feed(s) failed", task_name="collect_curated_sources")
    return f"Collected {new} new / {blocked} blocked for {len(users)} user(s)"


# --- Draft ---------------------------------------------------------------------------------------

def _block(source: dict, reason: str, user_id: int) -> None:
    update_curated_source_status(source["id"], STATUS_BLOCKED, block_reason=reason)
    track_curated_source("draft", "blocked", user_id=user_id, platform=source.get("platform") or "",
                         reason=reason.split(":")[0])
    log_info("Curated source blocked at draft time", user_id=user_id, source_id=source.get("id"),
             reason=reason, task_name="curated_draft")


def _chart_hook(source: dict) -> str:
    words = ((source.get("title") or source.get("publisher") or "").split())[:8]
    return " ".join(words) or "The numbers"


def _render_rechart(user_id: int, post_id: int, source: dict, graphic: dict) -> Optional[str]:
    """Draw the re-chart and store it as the post's image. None when it cannot be drawn.

    ``render_graphic`` re-checks every drawn figure against its source sentence immediately before
    drawing (``assert_traceable``) and raises rather than draw an untraceable one.
    """
    from cqc_lem.utilities.ai.image_graphics import (
        HIGHLIGHT_CHART,
        STAT_CARD,
        GraphicError,
        available_archetypes,
        render_graphic,
    )
    from cqc_lem.utilities.post_image import store_rendered_post_image

    archetype = HIGHLIGHT_CHART if HIGHLIGHT_CHART in available_archetypes(graphic) else STAT_CARD
    kicker = " ".join((source.get("publisher") or "").split()[:3])
    try:
        render = render_graphic(archetype, graphic, surface="post_image", hook=_chart_hook(source),
                                kicker=kicker)
    except GraphicError as e:
        log_info("Re-chart refused before render", user_id=user_id, post_id=post_id,
                 reason=str(e), task_name="curated_draft")
        return None
    try:
        return store_rendered_post_image(user_id, render.path, post_id)
    finally:
        try:
            os.remove(render.path)
        except OSError as e:
            log_debug("Temp re-chart not removed", error=str(e))


def draft_curated_post(user_id: int, post_id: int, source: dict, content_mix: Optional[str],
                       prefs: Optional[dict] = None, profile_synthesis: Optional[str] = None,
                       profile=None) -> Optional[str]:
    """Draft ``post_id`` from ``source``. Returns the commentary, or None (source not used).

    The block gate runs AGAIN here — a source collected before a rule changed must not slip
    through — and provenance is required. Our OWN commentary is screened by the political filter
    too: the filter is absolute, whatever the model wrote.
    """
    verdict = screen_candidate(source)
    if not verdict.ok:
        _block(source, verdict.reason, user_id)
        return None
    missing = provenance_reason(source)
    if missing:
        _block(source, missing, user_id)
        return None

    graphic = None
    if not treatment_allowed(source, TREATMENT_RESHARE).ok and \
            treatment_allowed(source, TREATMENT_RECHART).ok:
        raw = extract_chart_facts(source)
        graphic = validated_chart(source, raw) if raw else None
    treatment = pick_treatment(source, bool(graphic))
    if treatment is None:
        _block(source, f"{BLOCK_NO_PROVENANCE}:no_treatment", user_id)
        return None

    image_url = None
    if treatment == TREATMENT_RECHART:
        image_url = _render_rechart(user_id, post_id, source, graphic)
        if not image_url:
            treatment = TREATMENT_LINK if treatment_allowed(source, TREATMENT_LINK).ok else None
            if treatment is None:
                _block(source, "rechart_failed", user_id)
                return None

    commentary = generate_curated_commentary(user_id, source, treatment, profile=profile,
                                             profile_synthesis=profile_synthesis, prefs=prefs,
                                             content_mix=content_mix)
    if not commentary:
        _block(source, "commentary_refused", user_id)
        return None
    political = political_reason(commentary)
    if political:
        _block(source, f"commentary_{political}", user_id)
        return None

    if not attach_curated_source_to_post(post_id, source["id"], treatment):
        return None
    if image_url:
        update_db_post_image_url(post_id, image_url)
    update_curated_source_status(source["id"], STATUS_DRAFTED, post_id=post_id)
    track_curated_source("draft", "drafted", user_id=user_id,
                         platform=source.get("platform") or "", treatment=treatment)
    log_info(f"Curated draft written as a {treatment}", user_id=user_id, post_id=post_id,
             source_id=source["id"], task_name="curated_draft")
    return commentary


def curated_content_for_slot(user_id: int, post_id: Optional[int], content_mix: Optional[str],
                             load_voice: Optional[Callable[[], tuple]] = None) -> Optional[str]:
    """The curated commentary that CLAIMS this text slot, or None to write the ordinary post.

    Args:
        user_id: The author.
        post_id: The planned post being filled.
        content_mix: The slot's 70/20/10 class.
        load_voice: Returns ``(prefs, profile_synthesis)``; called only once a candidate exists,
            so the common "not this slot" answer costs no voice read.

    Returns:
        The commentary with its credit line, or None.

    Never raises: a curated-sources fault must cost the author a curated post, never the post.
    """
    try:
        if post_id is None or not curated_enabled(user_id) or not slot_allows(content_mix):
            return None
        neighbors = get_curated_neighbors(user_id, post_id, CEILING_WINDOW - 1)
        if neighbors is None or not ceiling_allows(*neighbors):
            # DEBUG: the ceiling holding is the feature working (two of every three posts).
            log_debug("Curated ceiling holds this slot for an original post", user_id=user_id,
                      post_id=post_id, task_name="curated_draft")
            return None
        candidates = get_draftable_curated_sources(user_id, limit=DRAFT_CANDIDATES)
        if not candidates:
            return None
        prefs, profile_synthesis = load_voice() if load_voice else (None, None)
        profile = None
        for source in candidates:
            content = draft_curated_post(user_id, post_id, source, content_mix, prefs=prefs,
                                         profile_synthesis=profile_synthesis, profile=profile)
            if content:
                return content
        return None
    except Exception as e:
        log_warning("Curated drafting failed — writing the ordinary post instead", exc=e,
                    user_id=user_id, post_id=post_id, task_name="curated_draft")
        return None


# --- Publish -------------------------------------------------------------------------------------

def curated_publish_refusal(context: Optional[dict]) -> Optional[tuple[str, PostStatus]]:
    """Why a curated post may NOT publish, and the status it goes back to — or None to publish.

    Fails CLOSED: an approval no human made (``system:*``, or none recorded) is refused and the post
    returns to PENDING for its owner. A source the guardrails now block, or one that is gone, holds
    the post at ERROR for a human.
    """
    if not context:
        return None
    if context.get("post_status") not in _PUBLISHABLE:
        return "not_approved", PostStatus.PENDING
    if not str(context.get("approved_by") or "").startswith(_HUMAN_APPROVER_PREFIX):
        return "not_owner_approved", PostStatus.PENDING
    source = context.get("source")
    if not source:
        return "source_missing", PostStatus.ERROR
    if context.get("source_treatment") not in TREATMENTS:
        return "no_treatment", PostStatus.ERROR
    verdict = screen_candidate(source)
    if not verdict.ok:
        return f"blocked:{verdict.reason}", PostStatus.ERROR
    if not treatment_allowed(source, context["source_treatment"]).ok:
        return "treatment_not_allowed", PostStatus.ERROR
    return None


def publish_curated_post(user_id: int, post_id: int, content: str,
                         context: dict) -> tuple[str, Optional[str]]:
    """Publish an owner-approved curated post by its treatment. Returns ``(body, urn)``.

    The ``Source:`` credit is re-applied here, so an owner edit that dropped it cannot publish a
    curated post without its credit. ``urn`` is None on any unconfirmed publish (the caller holds
    the post at ERROR); a reshare LinkedIn REFUSED also blocks the source, never retried.
    """
    from cqc_lem.utilities.linkedin.reshare import (
        ReshareRefused,
        share_article_on_linkedin,
        share_curated_image_on_linkedin,
        share_reshare_on_linkedin,
    )
    from cqc_lem.utilities.post_image import post_image_abs_path

    source = context["source"]
    treatment = context["source_treatment"]
    body = with_credit(content, source, treatment)
    urn = None
    try:
        if treatment == TREATMENT_RESHARE:
            urn = share_reshare_on_linkedin(user_id, body, source["canonical_id"])
        elif treatment == TREATMENT_RECHART:
            path = post_image_abs_path(context.get("image_url"))
            if path:
                urn = share_curated_image_on_linkedin(user_id, body, path)
            else:
                log_warning("Re-chart post has no stored image — not published", user_id=user_id,
                            post_id=post_id, task_name="post_to_linkedin")
        else:
            urn = share_article_on_linkedin(user_id, body, source["url"],
                                            source.get("title") or source.get("publisher") or "",
                                            (source.get("excerpt") or "")[:300],
                                            source.get("og_image_url"))
    except ReshareRefused as e:
        update_curated_source_status(source["id"], STATUS_BLOCKED, block_reason="reshare_refused")
        log_warning("LinkedIn refused the reshare — source blocked, not retried", exc=e,
                    user_id=user_id, post_id=post_id, task_name="post_to_linkedin")
        track_curated_source("publish", "refused", user_id=user_id, platform=source.get("platform")
                             or "", treatment=treatment, reason="reshare_refused")
        return body, None
    if urn:
        update_curated_source_status(source["id"], STATUS_PUBLISHED)
        track_curated_source("publish", "published", user_id=user_id,
                             platform=source.get("platform") or "", treatment=treatment)
    return body, urn
