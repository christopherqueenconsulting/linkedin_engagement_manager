import os
import shutil
from datetime import timedelta, datetime, timezone

from celery_once import QueueOnce

from cqc_lem import assets_dir
from cqc_lem.app.my_celery import app as shared_task
from cqc_lem.app.run_automation import automate_commenting, automate_profile_viewer_engagement, \
    automate_appreciation_dms_for_user, clean_stale_invites, update_stale_profile, post_to_linkedin, \
    automate_invites_to_company_page_for_user, auto_seed_comment_on_post, send_scheduled_dm, \
    sweep_reply_comments
from cqc_lem.utilities.db import (
    get_ready_to_post_posts, get_orphaned_scheduled_posts, update_db_post_status,
    get_active_user_ids, PostStatus, has_linkedin_session, has_scheduled_post_today,
    get_company_linked_in_url_for_user,
    get_users_with_stripe_subscriptions, update_subscription_from_stripe,
    get_due_scheduled_dms, get_orphaned_scheduled_dms, update_scheduled_dm_status, ScheduledDmStatus,
    get_users_with_reply_mode, get_engagement_preferences,
)
from cqc_lem.utilities.env_constants import SELENIUM_KEEP_VIDEOS_X_DAYS, CQC_LEM_POST_TIME_DELTA_MINUTES
from cqc_lem.utilities.logger import myprint, log_info, log_debug, log_warning
from cqc_lem.utilities.notifications import notify_linkedin_session



@shared_task.task(bind=True, base=QueueOnce, once={'graceful': True, }, reject_on_worker_lost=True)
def auto_check_scheduled_posts(self):
    """Checks if there are any posts to publish."""

    # Get post that should have run between yesterday and in the next 20 minutes
    posts = get_ready_to_post_posts(post_time_delta_minutes=CQC_LEM_POST_TIME_DELTA_MINUTES)
    # Fetch active users only when there are posts (avoids a DB round-trip when idle).
    active_user_ids = set(get_active_user_ids()) if posts else set()

    for post in posts:
        post_id, scheduled_time, user_id = post

        if scheduled_time.tzinfo is None:
            scheduled_time = scheduled_time.replace(tzinfo=timezone.utc)

        log_info(f"Post ready to schedule", post_id=post_id, user_id=user_id, task_name="auto_check_scheduled_posts")

        # Update the DB with post status = scheduled so it won't get processed again
        update_db_post_status(post_id, PostStatus.SCHEDULED)
        log_info(f"Post {post_id} queued for {scheduled_time}", post_id=post_id, user_id=user_id)

        # Schedule the post to be posted (REST API — no Selenium required)
        post_kwargs = {'user_id': user_id, 'post_id': post_id}
        post_to_linkedin.apply_async(kwargs=post_kwargs, eta=scheduled_time)

        # Only dispatch Selenium pre-post tasks for users with an active LinkedIn
        # connection and subscription. Inactive/disconnected users' sessions fail
        # immediately and waste a Chrome slot that active users need.
        if user_id in active_user_ids:
            base_kwargs = {'user_id': user_id, 'loop_for_duration': 60 * 15}

            # Start the pre-post commenting task 15 minutes before scheduled post (loop for 15 minutes)
            automate_commenting.apply_async(kwargs=base_kwargs, eta=scheduled_time - timedelta(minutes=15))

            # Seed a pinned first comment ~3 min after the post publishes (its golden hour) so the
            # post has a value-adding comment thread started from the author.
            auto_seed_comment_on_post.apply_async(kwargs={'user_id': user_id, 'post_id': post_id},
                                                  eta=scheduled_time + timedelta(minutes=3))

            # Schedule the pre-post profile viewer dm task 10 minutes before scheduled post (loop for 10 minutes)
            base_kwargs['loop_for_duration'] = 60 * 10
            automate_profile_viewer_engagement.apply_async(kwargs=base_kwargs, eta=scheduled_time - timedelta(minutes=10))
        else:
            log_warning(
                "Skipping pre-post Selenium tasks — user not active/connected",
                user_id=user_id, post_id=post_id, task_name="auto_check_scheduled_posts",
            )

    # Re-queue any posts that got stuck in 'scheduled' (task was lost, e.g. on container restart)
    # but never transitioned to 'posted'. The 2-hour gap ensures we don't race with a task
    # that is still in-flight.
    orphaned = get_orphaned_scheduled_posts(lookback_hours=2)
    for post_id, scheduled_time, user_id in orphaned:
        if scheduled_time.tzinfo is None:
            scheduled_time = scheduled_time.replace(tzinfo=timezone.utc)
        log_warning(
            f"Re-queueing orphaned scheduled post",
            post_id=post_id, user_id=user_id, task_name="auto_check_scheduled_posts",
        )
        post_to_linkedin.apply_async(kwargs={'user_id': user_id, 'post_id': post_id})

    if len(posts) == 0 and len(orphaned) == 0:
        return f"No Post to Schedule"
    else:
        return f"Started Process for {len(posts)} post(s); re-queued {len(orphaned)} orphaned post(s)"


@shared_task.task(bind=True, base=QueueOnce, once={'graceful': True, }, reject_on_worker_lost=True)
def auto_check_scheduled_dms(self):
    """Scan for approved scheduled DMs that are due and dispatch the send task at their eta
    (issue #306, mirrors auto_check_scheduled_posts). Only dispatches for active/connected users;
    the per-day DM cap is enforced at send time in send_scheduled_dm."""
    dms = get_due_scheduled_dms(post_time_delta_minutes=CQC_LEM_POST_TIME_DELTA_MINUTES)
    active_user_ids = set(get_active_user_ids()) if dms else set()

    dispatched = 0
    for dm_id, scheduled_time, user_id in dms:
        if scheduled_time.tzinfo is None:
            scheduled_time = scheduled_time.replace(tzinfo=timezone.utc)
        if user_id not in active_user_ids:
            log_warning("Skipping scheduled DM — user not active/connected",
                        user_id=user_id, task_name="auto_check_scheduled_dms")
            continue
        # Mark 'scheduled' so it isn't re-dispatched on the next scan, then send at its eta.
        update_scheduled_dm_status(dm_id, ScheduledDmStatus.SCHEDULED)
        send_scheduled_dm.apply_async(kwargs={'dm_id': dm_id}, eta=scheduled_time)
        log_info(f"Scheduled DM {dm_id} queued for {scheduled_time}",
                 user_id=user_id, task_name="auto_check_scheduled_dms")
        dispatched += 1

    # Re-queue DMs stuck in 'scheduled' whose send task was lost (e.g. on container restart) —
    # mirrors the orphaned-post recovery above. The 2-hour gap avoids racing an in-flight task.
    orphaned = get_orphaned_scheduled_dms(lookback_hours=2)
    for dm_id, scheduled_time, user_id in orphaned:
        log_warning(
            f"Re-queueing orphaned scheduled DM {dm_id}",
            user_id=user_id, task_name="auto_check_scheduled_dms",
        )
        send_scheduled_dm.apply_async(kwargs={'dm_id': dm_id})

    if dispatched == 0 and len(orphaned) == 0:
        return "No DMs to Schedule"
    return f"Scheduled {dispatched} DM(s); re-queued {len(orphaned)} orphaned DM(s)"


@shared_task.task
def auto_appreciate_dms():
    # For each user schedule appreciate DMS
    users = get_active_user_ids()

    for user_id in users:
        # Send appreciation DM for 5 minutes
        kwargs = {
            'user_id': user_id,
            'loop_for_duration': 60 * 5
        }

        # No need to worry as this task is rate limited to 2 per minute
        automate_appreciation_dms_for_user.apply_async(kwargs=kwargs, retry=True,
                                                       retry_policy={
                                                           'max_retries': 3,
                                                           'interval_start': 60,
                                                           'interval_step': 30
                                                       })
    if len(users) == 0:
        return f"No Active Users"
    else:
        return f"Started Appreciate DM Process for {len(users)} user(s)"


@shared_task.task
def auto_daily_engagement():
    """Daily golden-hour feed-commenting run — fires EVERY day at a peak engagement window, on
    top of the pre-post commenting that already runs around each scheduled post. This gives a
    consistent daily reciprocity burst even when a post is (or isn't) scheduled. Volume stays safe
    because both this run and the pre-post runs share the per-day comment cap (enforced in
    comment_on_feed_inline), and QueueOnce (keys=['user_id']) prevents overlapping double-runs for
    the same user."""
    users = get_active_user_ids()
    dispatched = 0
    for user_id in users:
        if not has_linkedin_session(user_id):
            continue  # no session → the Selenium task would just fail and waste a Chrome slot
        automate_commenting.apply_async(kwargs={'user_id': user_id, 'loop_for_duration': 60 * 15})
        dispatched += 1
    return f"Golden-hour engagement dispatched for {dispatched}/{len(users)} active user(s)"


@shared_task.task
def dispatch_scheduled_reply_sweeps():
    """Beat: for users on reply_check_mode='scheduled', run a recent-posts reply sweep at their
    configured cadence (reply_sweeps_per_day, 2–12). A per-user Redis key with TTL = the cadence
    interval gates it: while the key exists we're within the interval and skip, so running this beat
    every ~30 min naturally yields ~reply_sweeps_per_day sweeps. Fails open on Redis outage (dispatch
    anyway) — sweep_reply_comments itself is QueueOnce + 429-safe, so an extra run is harmless."""
    from cqc_lem.utilities.linkedin.rate_limit import _redis_client
    users = get_users_with_reply_mode("scheduled")
    if not users:
        return "No scheduled-mode users"
    client = _redis_client()
    dispatched = 0
    for user_id in users:
        if not has_linkedin_session(user_id):
            continue
        sweeps = int(get_engagement_preferences(user_id).get("reply_sweeps_per_day") or 2)
        interval_s = max(2 * 60 * 60, (24 * 60 * 60) // max(1, sweeps))  # floor 2h between sweeps
        due = True
        if client is not None:
            try:
                # nx=True sets the key only if absent (i.e. we're past the interval) → this run is due.
                due = bool(client.set(f"linkedin:last_reply_sweep:{user_id}", "1", nx=True, ex=interval_s))
            except Exception:
                due = True
        if due:
            sweep_reply_comments.apply_async(kwargs={'user_id': user_id})
            dispatched += 1
    return f"Scheduled reply sweeps dispatched for {dispatched}/{len(users)} user(s)"


def _max_dt(*dts):
    """Max of the given datetimes, ignoring None (returns None if all are None)."""
    present = [d for d in dts if d is not None]
    return max(present) if present else None


def _topup_newsletter_drafts_for_user(user_id: int, now: datetime,
                                      allow_bootstrap: bool = True) -> int:
    """Top a single user's review queue up to their max_queued_drafts and return how many drafts were
    generated. Each draft covers the next uncovered cadence slot.

    `allow_bootstrap` gates the first-ever draft (empty queue): the daily beat passes True so the very
    first draft still waits for the generate_lead_days window; an explicit user action (e.g. raising
    the count) passes False to fill the queue ahead immediately."""
    import pytz
    from cqc_lem.utilities.db import (get_newsletter_settings, get_user_timezone,
                                      count_pending_newsletter_editions,
                                      get_latest_edition_scheduled_for, create_newsletter_edition,
                                      get_pending_newsletter_editions, get_recent_newsletter_subjects,
                                      get_recent_newsletter_blueprint_history,
                                      get_engagement_preferences)
    from cqc_lem.utilities.linkedin.helper import load_profile_for_user
    from cqc_lem.utilities.ai.ai_helper import (generate_newsletter_edition, plan_newsletter_topics,
                                                get_or_create_profile_synthesis)
    from cqc_lem.utilities.ai.content_framework import compact_blueprint
    from cqc_lem.utilities.ai.content_research import research_topic
    from cqc_lem.utilities.newsletter import upcoming_publish_slots, should_generate_now
    from cqc_lem.utilities.notifications import notify_newsletter_draft_ready

    settings = get_newsletter_settings(user_id)
    cap = settings.get("max_queued_drafts", 1)
    lead = settings.get("generate_lead_days", 3)
    pending = count_pending_newsletter_editions(user_id)
    remaining = cap - pending
    if remaining <= 0:
        return 0  # queue already full

    tz = pytz.timezone(get_user_timezone(user_id))
    # Anchor on the latest slot ANY edition already covers (incl. published/skipped) so a freed cap
    # slot fills the next future slot, never re-covering a skipped one.
    anchor = _max_dt(settings.get("last_published_at"),
                     get_latest_edition_scheduled_for(user_id))
    slots = upcoming_publish_slots(
        settings.get("publish_day", 1), settings.get("publish_hour", 9),
        settings.get("cadence", "weekly"), anchor, tz, now, remaining)
    # Bootstrap gate: only the first-ever draft waits for the lead window, and only when triggered by
    # the daily beat. Once the queue is rolling (pending > 0), or when the user explicitly asked for
    # more queued drafts, keep it topped up regardless of how far out the slots land.
    if pending == 0 and allow_bootstrap and not should_generate_now(slots[0], now, lead_days=lead):
        return 0

    profile = load_profile_for_user(user_id)
    synthesis = get_or_create_profile_synthesis(user_id, profile)
    prefs = get_engagement_preferences(user_id)
    description = settings.get("topic")

    # Dedup history: subjects of already-queued editions + recently published/skipped ones. Feeding
    # this into the planner (and the per-edition generator) is what keeps every edition unique and
    # fresh instead of near-duplicate rehashes of the newsletter's single description.
    queued_subjects = [e.get("subject") for e in get_pending_newsletter_editions(user_id)
                       if e.get("subject")]
    prior_subjects = list(dict.fromkeys(
        [s for s in queued_subjects if s] + get_recent_newsletter_subjects(user_id, limit=20)))

    # SHAPE history (formats/hook styles/actual opening lines of recent editions, queued included) —
    # fed to the planner so new editions rotate away from recently used shapes, and to the writer so
    # no two editions open with the same line or rhetorical template.
    shape_history = get_recent_newsletter_blueprint_history(user_id, limit=12)
    recent_formats = [h.get("format") for h in shape_history if h.get("format")]
    recent_hooks = [h.get("hook_style") for h in shape_history if h.get("hook_style")]
    recent_openers = [h.get("opening_line") for h in shape_history if h.get("opening_line")]

    # PLAN a coherent, distinct sequence of edition BLUEPRINTS up front (one shot), THEN write one
    # edition per blueprint. Falls back to single-topic generation when planning yields nothing.
    planned = plan_newsletter_topics(synthesis or "", description or "", prefs, prior_subjects,
                                     len(slots), recent_formats=recent_formats,
                                     recent_hook_styles=recent_hooks)

    generated = 0
    for i, slot in enumerate(slots):
        plan = planned[i] if i < len(planned) else None
        subject_ctx = None
        if plan:
            subject_ctx = plan["subject"]
            if plan.get("angle"):
                subject_ctx = f"{plan['subject']} — angle: {plan['angle']}"
        # Even the fallback path stays distinct: avoid every other planned subject + all prior ones.
        avoid = prior_subjects + [p["subject"] for j, p in enumerate(planned)
                                  if j != i and p.get("subject")]
        # ONE research call per edition: current stats/examples for THIS subject, woven into the
        # body as source material. Degrades to empty findings (write from expertise) on any failure.
        research = research_topic(
            (plan.get("subject") if plan else None) or description or "",
            content_type="newsletter", blueprint=plan, context_description=description, prefs=prefs)
        edition = generate_newsletter_edition(profile, topic=description, prefs=prefs,
                                              subject=subject_ctx, avoid_subjects=avoid,
                                              profile_synthesis=synthesis, blueprint=plan,
                                              avoid_openers=recent_openers, research=research)
        if not edition:
            break
        edition_subject = edition.get("subject") or (plan["subject"] if plan else None)
        if not create_newsletter_edition(user_id, edition["title"], edition.get("subtitle"),
                                          edition["body"], slot, subject=edition_subject,
                                          edition_format=edition.get("format"),
                                          hook_style=edition.get("hook_style"),
                                          opening_line=edition.get("opening_line"),
                                          blueprint=compact_blueprint(plan) if plan else None):
            break  # duplicate slot / db error → stop this user's run
        if edition_subject:
            prior_subjects.append(edition_subject)  # subsequent iterations avoid it too
        if edition.get("opening_line"):
            recent_openers.insert(0, edition["opening_line"])  # later slots avoid this opener too
        notify_newsletter_draft_ready(user_id, edition["title"], slot)
        generated += 1
    return generated


@shared_task.task
def auto_generate_newsletter_drafts():
    """Keep each enabled user's review queue topped up to their max_queued_drafts, so they can plan
    ahead. The first-ever draft waits for the generate_lead_days window; once the queue is rolling it
    refills to the cap as editions publish/skip."""
    from cqc_lem.utilities.db import get_enabled_newsletter_user_ids

    # Naive UTC on purpose — compared against naive DB datetimes downstream.
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    generated = 0
    for user_id in get_enabled_newsletter_user_ids():
        try:
            generated += _topup_newsletter_drafts_for_user(user_id, now)
        except Exception as e:
            log_warning("Failed to generate newsletter draft", exc=e, user_id=user_id,
                        task_name="auto_generate_newsletter_drafts")
    return f"Generated {generated} newsletter draft(s)"


@shared_task.task
def generate_newsletter_drafts_for_user(user_id: int):
    """Top up a single user's newsletter review queue on demand (e.g. right after they raise their
    max_queued_drafts in settings), so new slots don't wait for the daily beat. Skips the bootstrap
    lead-window gate: an explicit settings change should fill the queue ahead immediately."""
    from cqc_lem.utilities.db import get_newsletter_settings

    if not get_newsletter_settings(user_id).get("enabled"):
        return "Newsletter disabled"
    try:
        generated = _topup_newsletter_drafts_for_user(user_id,
                                                       datetime.now(timezone.utc).replace(tzinfo=None),
                                                       allow_bootstrap=False)
    except Exception as e:
        log_warning("Failed to generate newsletter draft", exc=e, user_id=user_id,
                    task_name="generate_newsletter_drafts_for_user")
        return "Generated 0 newsletter draft(s)"
    return f"Generated {generated} newsletter draft(s)"


@shared_task.task
def regenerate_newsletter_edition(edition_id: int, guidance: str = None):
    """Regenerate ONE queued newsletter edition in place. Generation is a slow lem-complex call, so
    it runs as a task (not inline in the API request). Honors free-text `guidance` when provided
    (edit the same subject OR take a completely different direction); with no guidance the AI decides
    a fresh, distinct take. Grounded in the author's voice synthesis + the newsletter description, and
    steered AWAY from the OTHER queued editions' subjects (and recent history) so regeneration never
    reintroduces a duplicate. Updates the row (title/subtitle/subject/body) and resets status to
    'draft'."""
    from cqc_lem.utilities.db import (get_newsletter_edition, get_newsletter_settings,
                                      get_pending_newsletter_editions, get_recent_newsletter_subjects,
                                      get_recent_newsletter_blueprint_history,
                                      get_engagement_preferences, update_newsletter_edition)
    from cqc_lem.utilities.linkedin.helper import load_profile_for_user
    from cqc_lem.utilities.ai.ai_helper import (generate_newsletter_edition,
                                                get_or_create_profile_synthesis)
    from cqc_lem.utilities.ai.content_framework import compact_blueprint, select_blueprint
    from cqc_lem.utilities.ai.content_research import research_topic

    edition = get_newsletter_edition(edition_id)
    if not edition or edition.get("status") not in ("draft", "approved"):
        return f"Edition {edition_id} not regenerable"
    user_id = edition["user_id"]
    settings = get_newsletter_settings(user_id)
    prefs = get_engagement_preferences(user_id)
    profile = load_profile_for_user(user_id)
    synthesis = get_or_create_profile_synthesis(user_id, profile)

    # Avoid the OTHER queued editions' subjects + recent history so the rewrite stays unique.
    others = [e.get("subject") for e in get_pending_newsletter_editions(user_id)
              if e.get("id") != edition_id and e.get("subject")]
    avoid = list(dict.fromkeys([s for s in others if s] + get_recent_newsletter_subjects(user_id, limit=20)))

    # Rotate the rewrite's SHAPE too: pick a fresh format/hook/CTA away from the recent history —
    # including this edition's own previous shape — so regeneration changes form, not just words.
    # Free-text guidance may name a format (e.g. "make it a case study"); the builder honors it.
    shape_history = get_recent_newsletter_blueprint_history(user_id, limit=12)
    recent_formats = [h.get("format") for h in shape_history if h.get("format")]
    recent_hooks = [h.get("hook_style") for h in shape_history if h.get("hook_style")]
    recent_openers = [h.get("opening_line") for h in shape_history if h.get("opening_line")]

    # With guidance we keep the current subject as the starting point (the guidance may edit it or
    # redirect entirely). With NO guidance the AI decides a fresh, distinct subject (subject=None).
    subject = edition.get("subject") if (guidance and guidance.strip()) else None
    blueprint = select_blueprint("newsletter", subject=subject, recent_formats=recent_formats,
                                 recent_hook_styles=recent_hooks, guidance=guidance)
    # ONE research call per regenerate — grounds the rewrite in current facts; empty on failure.
    research = research_topic(subject or settings.get("topic") or "", content_type="newsletter",
                              blueprint=blueprint,
                              context_description=settings.get("topic"), prefs=prefs)
    try:
        new_ed = generate_newsletter_edition(profile, topic=settings.get("topic"), prefs=prefs,
                                             subject=subject, avoid_subjects=avoid,
                                             profile_synthesis=synthesis, guidance=guidance,
                                             blueprint=blueprint, avoid_openers=recent_openers,
                                             research=research)
    except Exception as e:
        log_warning("Newsletter regeneration failed", exc=e, user_id=user_id,
                    task_name="regenerate_newsletter_edition")
        return f"Regeneration failed for edition {edition_id}"
    if not new_ed:
        return f"Regeneration produced nothing for edition {edition_id}"
    update_newsletter_edition(edition_id, user_id, title=new_ed["title"],
                              subtitle=new_ed.get("subtitle"), body=new_ed["body"],
                              subject=new_ed.get("subject") or subject, status="draft",
                              edition_format=new_ed.get("format"),
                              hook_style=new_ed.get("hook_style"),
                              opening_line=new_ed.get("opening_line"),
                              blueprint=compact_blueprint(
                                  {**blueprint, "subject": new_ed.get("subject") or subject}))
    log_info("Regenerated newsletter edition", user_id=user_id,
             task_name="regenerate_newsletter_edition")
    return f"Regenerated newsletter edition {edition_id}"


@shared_task.task
def auto_publish_scheduled_editions():
    """Publish any newsletter edition whose scheduled slot has arrived (approved or untouched draft)."""
    from cqc_lem.app.run_automation import auto_publish_edition
    from cqc_lem.utilities.db import get_editions_due_to_publish
    due = get_editions_due_to_publish(datetime.now(timezone.utc).replace(tzinfo=None))
    for e in due:
        auto_publish_edition.apply_async(kwargs={'edition_id': e['id']})
    return f"Dispatched {len(due)} newsletter edition(s)"


@shared_task.task
def auto_refresh_profile_syntheses():
    """Weekly: (re)generate the cached, DURABLE voice synthesis for each active user whose synthesis is
    missing or stale (>7 days). The synthesis replaces the bloated full profile JSON as the voice
    source in every comment/post prompt; refreshing it on a slow cadence keeps the voice stable while
    still tracking real profile changes. No Selenium — works off each user's cached profile JSON."""
    from cqc_lem.utilities.ai.ai_helper import synthesize_profile
    from cqc_lem.utilities.db import get_user_ids_needing_profile_synthesis, set_profile_synthesis
    from cqc_lem.utilities.linkedin.helper import load_profile_for_user
    active = set(get_active_user_ids())
    stale = [uid for uid in get_user_ids_needing_profile_synthesis(stale_days=7) if uid in active]
    refreshed = 0
    for uid in stale:
        profile = load_profile_for_user(uid)
        if profile is None:
            continue
        try:
            synthesis = synthesize_profile(profile)
        except Exception as e:
            log_warning("Weekly profile synthesis failed", exc=e, user_id=uid,
                        task_name="auto_refresh_profile_syntheses")
            continue
        if synthesis and set_profile_synthesis(uid, synthesis):
            refreshed += 1
    log_info(f"Refreshed {refreshed}/{len(stale)} stale profile synthesis(es)",
             task_name="auto_refresh_profile_syntheses")
    return f"Refreshed {refreshed}/{len(stale)} profile synthesis(es)"


@shared_task.task
def auto_sync_groups():
    """Refresh each active user's joined-groups list (new groups default to enabled)."""
    from cqc_lem.app.run_automation import auto_sync_user_groups
    users = get_active_user_ids()
    n = 0
    for uid in users:
        if has_linkedin_session(uid):
            auto_sync_user_groups.apply_async(kwargs={'user_id': uid})
            n += 1
    return f"Group sync dispatched for {n}/{len(users)} user(s)"


@shared_task.task
def auto_group_engagement():
    """Daily value-add commenting in each active user's ENABLED groups (shares the per-day cap)."""
    from cqc_lem.app.run_automation import auto_comment_in_groups
    users = get_active_user_ids()
    n = 0
    for uid in users:
        if has_linkedin_session(uid):
            auto_comment_in_groups.apply_async(kwargs={'user_id': uid})
            n += 1
    return f"Group commenting dispatched for {n}/{len(users)} user(s)"


@shared_task.task
def auto_group_posts():
    """Weekly: publish one value-add post into an enabled group per active user."""
    from cqc_lem.app.run_automation import auto_post_to_group
    from cqc_lem.utilities.db import get_enabled_group_ids
    users = get_active_user_ids()
    n = 0
    for uid in users:
        if not has_linkedin_session(uid):
            continue
        groups = get_enabled_group_ids(uid)
        if groups:
            auto_post_to_group.apply_async(kwargs={'user_id': uid, 'group_id': groups[0]})
            n += 1
    return f"Group posts dispatched for {n}/{len(users)} user(s)"


@shared_task.task
def auto_scrape_stats():
    """Daily: capture engagement stats on each active user's recent posts (powers post-time recs)."""
    from cqc_lem.app.run_automation import auto_scrape_post_stats
    users = get_active_user_ids()
    n = 0
    for uid in users:
        if has_linkedin_session(uid):
            auto_scrape_post_stats.apply_async(kwargs={'user_id': uid})
            n += 1
    return f"Stats scrape dispatched for {n}/{len(users)} user(s)"


@shared_task.task
def auto_send_due_followups():
    """Dispatch a per-user Selenium task to send due DM follow-ups (each gated by reply-detection)."""
    from cqc_lem.app.run_automation import process_user_followups
    from cqc_lem.utilities.db import get_due_followups
    # due_at is stored naive-UTC; compare against naive-UTC now (not container-local time).
    due = get_due_followups(datetime.now(timezone.utc).replace(tzinfo=None))
    user_ids = sorted({f["user_id"] for f in due})
    for uid in user_ids:
        process_user_followups.apply_async(kwargs={"user_id": uid})
    return f"Dispatched follow-ups for {len(user_ids)} user(s)"


@shared_task.task
def auto_notify_missing_linkedin_session():
    """Email active users who have no validated LinkedIn session cookie, prompting them
    to connect — automation can't run without one. Throttled per-user inside
    notify_linkedin_session, so this can run daily without spamming."""
    users = get_active_user_ids()
    notified = 0
    for user_id in users:
        try:
            if not has_linkedin_session(user_id):
                if notify_linkedin_session(user_id, revalidation=False):
                    notified += 1
        except Exception as e:
            log_warning("Failed to notify missing LinkedIn session", exc=e, user_id=user_id)
    return f"Notified {notified} of {len(users)} active user(s) missing a LinkedIn session"


@shared_task.task
def auto_backfill_missing_assets():
    """Safety net: regenerate missing media for unposted video/carousel posts before they
    publish, so a post never reaches its scheduled time without its asset (e.g. when the
    original generation failed)."""
    from cqc_lem.utilities.db import get_unposted_posts_missing_assets
    from cqc_lem.app.run_content_plan import regenerate_post_video_task, regenerate_post_carousel_task

    posts = get_unposted_posts_missing_assets()
    queued = 0
    for post_id, user_id, post_type, buyer_stage, scheduled_time in posts:
        pt = str(post_type).lower()
        if pt == 'video':
            regenerate_post_video_task.apply_async(kwargs={'post_id': post_id})
            queued += 1
        elif pt == 'carousel':
            regenerate_post_carousel_task.apply_async(kwargs={'post_id': post_id})
            queued += 1
        log_warning("Backfilling missing media asset for unposted post",
                    post_id=post_id, user_id=user_id, task_name="auto_backfill_missing_assets")
    log_info(f"Asset backfill: queued {queued} regeneration(s) across {len(posts)} post(s)",
             task_name="auto_backfill_missing_assets")
    return f"Queued {queued} asset regeneration(s)"


@shared_task.task
def auto_clean_stale_invites():
    """Cleans up stale invites for each active user"""

    # Get all active users and loop through them
    users = get_active_user_ids()

    for user_id in users:
        # Clean up stale invites for this user
        kwargs = {'user_id': user_id}
        clean_stale_invites.apply_async(kwargs=kwargs, retry=True,
                                        retry_policy={
                                            'max_retries': 3,
                                            'interval_start': 60,
                                            'interval_step': 30
                                        })
    if len(users) == 0:
        return f"No Active Users"
    else:
        return f"Started Process for {len(users)} user(s)"


@shared_task.task
def auto_clean_stale_profiles():
    """Cleans up stale profiles for each active user"""

    # Get all active users and loop through them
    users = get_active_user_ids()

    for user_id in users:
        log_info(f"Cleaning stale profiles", user_id=user_id, task_name="auto_clean_stale_profiles")

        # Clean up stale profiles for this user
        # update_stale_profile(user_id)
        update_stale_profile.apply_async(kwargs={'user_id': user_id},
                                         retry=True,
                                         retry_policy={
                                             'max_retries': 3,
                                             'interval_start': 60,
                                             'interval_step': 30
                                         })

    if len(users) == 0:
        return f"No Active Users"
    else:
        return f"Started Process for {len(users)} user(s)"


@shared_task.task
def auto_invite_to_company_pages():
    """Start invite process for each active user who has a linked in company page"""

    # Get all active users and loop through them
    users = get_active_user_ids()

    started = 0
    for user_id in users:
        # Only invite for users who have actually set a company page — otherwise the
        # inviter would build "<None>?invite=true" and fail. Invite credits are limited
        # and reset monthly, which is why this runs on the 1st.
        if not get_company_linked_in_url_for_user(user_id):
            log_debug("Skipping company page invites — no company page set",
                      user_id=user_id, task_name="auto_invite_to_company_pages")
            continue

        log_info(f"Starting company page invites", user_id=user_id, task_name="auto_invite_to_company_pages")
        automate_invites_to_company_page_for_user.apply_async(kwargs={'user_id': user_id},
                                         retry=True,
                                         retry_policy={
                                             'max_retries': 3,
                                             'interval_start': 60,
                                             'interval_step': 30
                                         })
        started += 1

    if started == 0:
        return f"No active users with a company page"
    else:
        return f"Started company-page invites for {started} user(s)"



@shared_task.task
def auto_clean_old_videos():
    """Cleans up old videos in the selenium folder"""

    days_to_keep = SELENIUM_KEEP_VIDEOS_X_DAYS
    log_info(f"Cleaning old videos older than {days_to_keep} days", task_name="auto_clean_old_videos")
    expiration_date = datetime.now() - timedelta(days=days_to_keep)
    selenium_folder = os.path.join(assets_dir, 'selenium')
    delete_count = 0
    # Get all the folders in the selenium folder
    for folder in os.listdir(selenium_folder):
        folder_path = os.path.join(selenium_folder, folder)
        if os.path.isdir(folder_path) and datetime.fromtimestamp(os.path.getmtime(folder_path)) < expiration_date:
            log_debug(f"Deleting expired video folder: {folder_path}")
            shutil.rmtree(folder_path)
            delete_count += 1

    # Organize the videos by name and timestamp
    moved_videos = organize_videos_by_name_and_timestamp()

    return f"Deleted {delete_count} folders | Moved {moved_videos} videos"


def organize_videos_by_name_and_timestamp():
    selenium_folder = os.path.join(assets_dir, 'selenium')

    # Keep track of videos moved
    moved_videos = 0

    # Create a map to store unique names
    unique_name_map = {}

    # Iterate through each folder in the selenium folder
    for folder in os.listdir(selenium_folder):
        # Skip Folders that start with "CQC_LEM"
        if folder.startswith("CQC_LEM"):
            continue

        folder_path = os.path.join(selenium_folder, folder)
        if os.path.isdir(folder_path):
            # Iterate through each file in the folder
            for file in os.listdir(folder_path):
                if file.endswith('.mp4'):
                    file_path = os.path.join(folder_path, file)
                    file_name = os.path.splitext(file)[0]
                    file_timestamp = datetime.fromtimestamp(os.path.getmtime(file_path)).strftime('%Y_%m_%d_%H_%M_%S')

                    # Create a unique name map entry if it doesn't exist
                    if file_name not in unique_name_map:
                        unique_name_map[file_name] = []

                    # Add the file path and timestamp to the unique name map
                    unique_name_map[file_name].append((file_path, file_timestamp))

    # Create folders for each unique name and move the files
    for name, files in unique_name_map.items():
        name_folder = os.path.join(selenium_folder, name)
        os.makedirs(name_folder, exist_ok=True)

        for file_path, file_timestamp in files:
            new_file_name = f"{file_timestamp}.mp4"
            new_file_path = os.path.join(name_folder, new_file_name)
            shutil.move(file_path, new_file_path)
            log_debug(f"Moved video to organized location: {new_file_path}")
            # Delete the folder belonging to the file_path
            parent_folder = os.path.dirname(file_path)
            shutil.rmtree(parent_folder)
            log_debug(f"Deleted video source folder: {parent_folder}")
            moved_videos += 1

    return moved_videos


@shared_task.task(bind=True, base=QueueOnce, once={'graceful': True})
def sync_stripe_subscriptions(self):
    """Daily safety-net: fetch every active/past_due subscription from Stripe and
    reconcile against our DB. Catches any webhook events that were missed due to
    downtime, URL mismatches, or signature errors.
    """
    from cqc_lem.utilities.stripe_util import (
        fetch_subscription, get_subscription_tier_from_price, stripe_status_to_db,
    )

    rows = get_users_with_stripe_subscriptions()
    log_info(f"Stripe subscription sync: checking {len(rows)} subscriber(s)", task_name="sync_stripe_subscriptions")

    for row in rows:
        sub_id = row.get("stripe_subscription_id")
        customer_id = row.get("stripe_customer_id")
        if not sub_id:
            continue

        sub = fetch_subscription(sub_id)
        if not sub:
            log_warning(f"Could not fetch Stripe subscription {sub_id}, skipping", api_provider="stripe")
            continue

        stripe_status = sub.get("status", "")
        db_status = stripe_status_to_db(stripe_status)

        price_id = None
        items = sub.get("items", {}).get("data", [])
        if items:
            price_id = items[0].get("price", {}).get("id")
        tier = get_subscription_tier_from_price(price_id) if price_id else None

        period_end_ts = sub.get("current_period_end")
        period_end = datetime.fromtimestamp(period_end_ts, tz=timezone.utc) if period_end_ts else None

        current_db_status = row.get("subscription_status")
        if current_db_status != db_status or (tier and tier != row.get("subscription_tier")):
            log_info(
                f"Syncing subscription: DB={current_db_status}/{row.get('subscription_tier')} → Stripe={db_status}/{tier}",
                user_id=row["id"], api_provider="stripe",
            )
            update_subscription_from_stripe(customer_id, db_status, tier, sub_id, period_end)
        else:
            log_debug(f"Subscription up-to-date ({db_status}/{tier})", user_id=row["id"])


if __name__ == "__main__":
    print("Process finished")
