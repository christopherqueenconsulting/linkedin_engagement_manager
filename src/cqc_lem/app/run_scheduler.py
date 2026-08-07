"""Celery **beat** tasks — the fan-out layer between the schedule and the work itself.

Almost nothing here touches LinkedIn: each `auto_*` task decides WHO is due right now and dispatches
the real task (mostly `run_automation`) onto the right lane. Two rules run through all of them.

Every Selenium fan-out asks `_skip_if_throttled()` before dispatching, so a manual pause or an open
429 breaker stops work at the source instead of spending Chrome slots on sessions that would fail —
POSTING is API-driven and deliberately never gated. And the per-user beats tick every
STAGGER_TICK_MINUTES and dispatch only the users whose staggered slot has come up (`_stagger_due`,
issue #554), rather than handing one lane the whole fleet at a single crontab.
"""

import os
import shutil
from datetime import datetime, timedelta, timezone
from typing import Tuple

from cqc_lem import assets_dir
from cqc_lem.app.celeryconfig import SE_PREPOST_QUEUE
from cqc_lem.app.my_celery import app as shared_task
from cqc_lem.app.queue_once import QueueOnce
from cqc_lem.app.run_automation import (
    CATCHUP_PHASE_SCAN,
    CATCHUP_PHASE_SEND,
    CATCHUP_STATUS_AWAITING_APPROVAL,
    CATCHUP_STATUS_CAPPED,
    CATCHUP_STATUS_DISABLED,
    CATCHUP_STATUS_DISPATCHED,
    CATCHUP_STATUS_INACTIVE,
    CATCHUP_STATUS_NOTHING_TO_SEND,
    CATCHUP_STATUS_THROTTLED,
    automate_appreciation_dms_for_user,
    automate_commenting,
    automate_invites_to_company_page_for_user,
    automate_profile_viewer_engagement,
    clean_stale_invites,
    post_to_linkedin,
    report_catchup_run,
    send_catchup_touch,
    send_connection_request,
    send_scheduled_dm,
    sweep_comment_followups,
    sweep_comment_outcomes,
    sweep_reply_comments,
    update_stale_profile,
)
from cqc_lem.utilities.db import (
    CatchupTouchStatus,
    ConnectionRequestStatus,
    PostStatus,
    ScheduledDmStatus,
    count_catchup_touches_sent_today,
    count_invites_sent_today,
    count_pending_catchup_touches,
    get_active_user_ids,
    get_approved_catchup_touches,
    get_catchup_touch,
    get_approved_connection_requests,
    get_company_linked_in_url_for_user,
    get_due_scheduled_dms,
    get_engagement_preferences,
    get_orphaned_catchup_touches,
    get_orphaned_connection_requests,
    get_orphaned_scheduled_dms,
    get_orphaned_scheduled_posts,
    get_ready_to_post_posts,
    get_user_timezone,
    get_users_with_reply_mode,
    get_users_with_stripe_subscriptions,
    has_linkedin_session,
    max_catchup_touches_allowed,
    update_catchup_touch_status,
    update_connection_request_status,
    update_db_post_status,
    update_scheduled_dm_status,
    update_subscription_from_stripe,
)
from cqc_lem.utilities.engagement_window import (
    PRE_POST_COMMENT_LEAD_MINUTES,
    PRE_POST_SKIP_PAST_WINDOW,
    PRE_POST_SKIP_THROTTLED,
    PRE_POST_SKIP_USER_INACTIVE,
    PRE_POST_TASK_VIEWER,
    PRE_POST_VIEWER_LEAD_MINUTES,
    STAGGER_APPRECIATION_DM,
    STAGGER_COMPANY_INVITE,
    STAGGER_GOLDEN_HOUR,
    STAGGER_GROUP_ENGAGEMENT,
    claim_daily_slot,
    plan_daily_slot,
    plan_pre_post_window,
    record_pre_post_scheduled,
    record_pre_post_skipped,
    stagger_config,
)
from cqc_lem.utilities.env_constants import (
    COST_ROUTING_WINDOW_DAYS,
    CQC_LEM_POST_TIME_DELTA_MINUTES,
    SELENIUM_KEEP_VIDEOS_X_DAYS,
)
from cqc_lem.utilities.human_pacing import (
    ACTION_INVITE,
    PACE_RESPONSIVE,
    dispatch_jitter_seconds,
    engagement_caps_from_prefs,
    remaining_actions,
)
from cqc_lem.utilities.linkedin.rate_limit import (
    automation_pause_remaining,
    is_automation_paused,
    is_measurement_paused,
    rate_limit_cooldown_remaining,
)
from cqc_lem.utilities.logger import log_debug, log_info, log_warning
from cqc_lem.utilities.notifications import notify_linkedin_session
from cqc_lem.utilities.observability import FEATURE_CONTENT, FEATURE_NEWSLETTER, attribute_llm_cost, llm_attribution


def _skip_if_throttled(name: str, measurement_only: bool = False, **context) -> bool:
    """True when Selenium engagement should NOT fan out — either a manual automation pause OR an open
    429 circuit breaker cooldown is active. Beat dispatchers short-circuit on both so a rate-limited
    account isn't hammered: login_to_linkedin gates centrally too, but skipping at dispatch avoids
    spinning up Chrome sessions that would only fail AND — critically — stops tasks from probing the
    feed the instant the cooldown expires, which is what re-tripped the breaker into a doom loop.
    POSTING is API-driven and deliberately NOT gated.

    `measurement_only` lanes (read-only stat capture) skip for every pause EXCEPT the suppression
    tripwire's own, which must keep measuring or the collapse it detected can never be seen to
    recover (issue #629).
    """
    if is_measurement_paused() if measurement_only else is_automation_paused():
        log_info(f"{name} skipped — automation paused (~{automation_pause_remaining()}s left)", **context)
        return True
    cooldown = rate_limit_cooldown_remaining()
    if cooldown > 0:
        log_info(f"{name} skipped — LinkedIn 429 breaker open (~{cooldown}s left)", **context)
        return True
    return False


def _stagger_due(user_id: int, fanout: Tuple[str, int, int], task_name: str) -> bool:
    """True when THIS beat tick owns the user's staggered slot for `fanout` today (issue #554).

    The fan-out beats run every STAGGER_TICK_MINUTES and dispatch only the users whose slot has
    come up, so N users spread across the window instead of landing on one lane in one minute.
    Redis holds the claim, keyed by the slot's local date, which also buys a catch-up: a slot
    missed because the beat (or the 429 breaker) was down fires at the next tick instead of being
    lost for the day, and a claim written that late still can't reach tomorrow's slot. With no
    Redis the strict one-tick window keeps it to a single dispatch.
    """
    config = stagger_config(fanout)
    tz_name = get_user_timezone(user_id) if config.local else "UTC"
    slot = plan_daily_slot(user_id, config, tz_name)
    if not slot.reached:
        return False
    claimed = claim_daily_slot(user_id, config.name, slot.local_at.date().isoformat())
    due = slot.in_tick_window if claimed is None else claimed
    if due:
        log_debug(f"{task_name} slot due (local {slot.local_at:%H:%M}, +{slot.offset_minutes}m)",
                  user_id=user_id, task_name=task_name)
    return due


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

        log_info("Post ready to schedule", post_id=post_id, user_id=user_id, task_name="auto_check_scheduled_posts")

        # Update the DB with post status = scheduled so it won't get processed again
        update_db_post_status(post_id, PostStatus.SCHEDULED)
        log_info(f"Post {post_id} queued for {scheduled_time}", post_id=post_id, user_id=user_id)

        # Schedule the post to be posted (REST API — no Selenium required)
        post_kwargs = {'user_id': user_id, 'post_id': post_id}
        post_to_linkedin.apply_async(kwargs=post_kwargs, eta=scheduled_time)

        # Only dispatch Selenium pre-post tasks for users with an active LinkedIn
        # connection and subscription. Inactive/disconnected users' sessions fail
        # immediately and waste a Chrome slot that active users need. Also skip them
        # while throttled (pause / 429 breaker) so they don't probe the feed — the API
        # post above still goes out regardless. The two skip reasons are distinct, so
        # branch them separately to keep the logs honest (the throttle path already
        # logs its own reason inside _skip_if_throttled).
        if user_id not in active_user_ids:
            log_warning(
                "Skipping pre-post Selenium tasks — user not active/connected",
                user_id=user_id, post_id=post_id, task_name="auto_check_scheduled_posts",
            )
            record_pre_post_skipped(post_id, user_id, PRE_POST_SKIP_USER_INACTIVE)
        elif _skip_if_throttled("pre-post Selenium", user_id=user_id, post_id=post_id,
                                task_name="auto_check_scheduled_posts"):
            record_pre_post_skipped(post_id, user_id, PRE_POST_SKIP_THROTTLED)
        else:
            # Warm up the feed BEFORE the post goes live. A post picked up late (this scan looks back
            # to yesterday) has less than the full lead left — or none at all — so the window is
            # clamped rather than dispatched with an eta in the past, which Celery would run
            # immediately and turn a "pre"-post warm-up into a during/after-post one.
            comment_window = plan_pre_post_window(scheduled_time, PRE_POST_COMMENT_LEAD_MINUTES)
            if comment_window is None:
                record_pre_post_skipped(post_id, user_id, PRE_POST_SKIP_PAST_WINDOW)
            else:
                # Own lane (issue #553): on se_engage this eta-bound warm-up waits behind whatever
                # 15-minute golden-hour loop is already running, so with 3+ users posting in the
                # same window it starts after its own window closed. se_prepost has no long loops
                # on it, so the eta is honored. Golden-hour dispatch below stays on se_engage.
                automate_commenting.apply_async(
                    kwargs={'user_id': user_id, 'loop_for_duration': comment_window.duration_seconds,
                            'post_id': post_id},
                    eta=comment_window.eta,
                    queue=SE_PREPOST_QUEUE,
                )
                record_pre_post_scheduled(post_id, user_id, comment_window)

            # The seed first comment is dispatched by post_to_linkedin itself once the post is live —
            # it needs the published post's URL, and (being API-driven) it must not be gated on the
            # Selenium throttle/active checks that guard the browser tasks here.

            # Same clamp for the pre-post profile-viewer DM task (10-minute lead).
            viewer_window = plan_pre_post_window(scheduled_time, PRE_POST_VIEWER_LEAD_MINUTES)
            if viewer_window is None:
                record_pre_post_skipped(post_id, user_id, PRE_POST_SKIP_PAST_WINDOW,
                                        task_name=PRE_POST_TASK_VIEWER)
            else:
                automate_profile_viewer_engagement.apply_async(
                    kwargs={'user_id': user_id, 'loop_for_duration': viewer_window.duration_seconds},
                    eta=viewer_window.eta,
                )
                record_pre_post_scheduled(post_id, user_id, viewer_window,
                                          task_name=PRE_POST_TASK_VIEWER)

    # Re-queue any posts that got stuck in 'scheduled' (task was lost, e.g. on container restart)
    # but never transitioned to 'posted'. The 2-hour gap ensures we don't race with a task
    # that is still in-flight.
    orphaned = get_orphaned_scheduled_posts(lookback_hours=2)
    for post_id, scheduled_time, user_id in orphaned:
        if scheduled_time.tzinfo is None:
            scheduled_time = scheduled_time.replace(tzinfo=timezone.utc)
        log_warning(
            f"Re-queueing orphaned scheduled post (scheduled {scheduled_time.isoformat()})",
            post_id=post_id, user_id=user_id, task_name="auto_check_scheduled_posts",
        )
        post_to_linkedin.apply_async(kwargs={'user_id': user_id, 'post_id': post_id})

    if len(posts) == 0 and len(orphaned) == 0:
        return "No Post to Schedule"
    else:
        return f"Started Process for {len(posts)} post(s); re-queued {len(orphaned)} orphaned post(s)"


@shared_task.task(bind=True, base=QueueOnce, once={'graceful': True, }, reject_on_worker_lost=True)
def auto_check_scheduled_dms(self):
    """Scan for approved scheduled DMs that are due and dispatch the send task at their eta
    (issue #306, mirrors auto_check_scheduled_posts). Only dispatches for active/connected users;
    the per-day DM cap is enforced at send time in send_scheduled_dm.
    """
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


# How long a request may sit in 'sending' before the reaper below assumes its send task was lost.
_CONNECTION_ORPHAN_LOOKBACK_HOURS = 2
# ...which is also the hard ceiling on the pacing countdown a dispatch may carry (issue #626): a
# jitter longer than the orphan gap would let the reaper queue a SECOND send for a request whose
# first one is still legitimately pending, i.e. invite the same person twice. The 15-minute margin
# keeps a tuned-up PACING_JITTER_MAX_MINUTES from ever reaching that boundary.
_MAX_INVITE_DISPATCH_DELAY_SECONDS = _CONNECTION_ORPHAN_LOOKBACK_HOURS * 3600 - 15 * 60


@shared_task.task(bind=True, base=QueueOnce, once={'graceful': True, }, reject_on_worker_lost=True)
def auto_check_connection_requests(self):
    """Scan for approved proactive connection requests and dispatch the send task, enforcing the
    per-user daily invite cap AT DISPATCH (issue #398). Approval is required upstream (rows only reach
    'approved' via the API), and the whole scan short-circuits while the automation kill-switch / 429
    breaker is open, so a throttled account is never probed. The send task re-checks the cap and the
    throttle, deferring back to 'approved' if either trips between scan and send.
    """
    if _skip_if_throttled("auto_check_connection_requests"):
        return "Automation throttled"

    approved = get_approved_connection_requests()
    active_user_ids = set(get_active_user_ids()) if approved else set()

    dispatched = 0
    budgets: dict = {}  # user_id -> remaining invites allowed today
    for request_id, user_id in approved:
        if user_id not in active_user_ids:
            log_warning("Skipping connection request — user not active/connected",
                        user_id=user_id, task_name="auto_check_connection_requests")
            continue
        if user_id not in budgets:
            prefs = get_engagement_preferences(user_id)
            cap = int(prefs.get("max_invites_per_day") or 0)
            # Paced budget + account governor (issue #626) instead of the flat cap, so invite volume
            # varies day to day and shares one envelope with commenting and DMs.
            budgets[user_id] = remaining_actions(user_id, ACTION_INVITE, cap,
                                                 count_invites_sent_today(user_id),
                                                 caps=engagement_caps_from_prefs(prefs))
        if budgets[user_id] <= 0:
            continue  # daily budget already spent for this user — leave the rest 'approved' for tomorrow
        budgets[user_id] -= 1
        # Mark 'sending' so it isn't re-dispatched on the next scan, then send. The countdown spreads
        # the day's invites instead of firing the whole approved batch on one 5-minute beat tick.
        update_connection_request_status(request_id, ConnectionRequestStatus.SENDING)
        send_connection_request.apply_async(
            kwargs={'request_id': request_id},
            countdown=min(dispatch_jitter_seconds(), _MAX_INVITE_DISPATCH_DELAY_SECONDS))
        log_info(f"Connection request {request_id} dispatched",
                 user_id=user_id, task_name="auto_check_connection_requests")
        dispatched += 1

    # Re-queue requests stuck in 'sending' whose send task was lost (e.g. on container restart) —
    # mirrors the orphaned-DM recovery. The lookback gap avoids racing an in-flight task — which is
    # why the dispatch countdown above is clamped to stay inside it.
    orphaned = get_orphaned_connection_requests(lookback_hours=_CONNECTION_ORPHAN_LOOKBACK_HOURS)
    for request_id, user_id in orphaned:
        log_warning(f"Re-queueing orphaned connection request {request_id}",
                    user_id=user_id, task_name="auto_check_connection_requests")
        update_connection_request_status(request_id, ConnectionRequestStatus.SENDING)
        send_connection_request.apply_async(kwargs={'request_id': request_id})

    if dispatched == 0 and len(orphaned) == 0:
        return "No Connection Requests to Send"
    return f"Dispatched {dispatched} connection request(s); re-queued {len(orphaned)} orphaned"


@shared_task.task
def auto_appreciate_dms():
    """Beat: hand each due user a 5-minute appreciation-DM run on the `se_outreach` lane.

    The session check comes BEFORE the slot claim, so a user with no stored LinkedIn session doesn't
    spend their one slot for the day on a run that could only fail — connecting later still earns
    them a run at the next tick.

    Every dispatch carries a jittered countdown (#626), which is what keeps a stable per-user slot
    from starting at the same clock minute every day.
    """
    if _skip_if_throttled("auto_appreciate_dms"):
        return "Automation throttled"
    # For each user schedule appreciate DMS — at that user's staggered slot, so the single
    # se_outreach lane isn't handed the whole fleet in one minute (issue #554).
    users = get_active_user_ids()
    dispatched = 0

    for user_id in users:
        if not has_linkedin_session(user_id):
            continue  # no session → the Selenium DM run would fail, and it must not spend the slot
        if not _stagger_due(user_id, STAGGER_APPRECIATION_DM, "auto_appreciate_dms"):
            continue
        # Send appreciation DM for 5 minutes
        kwargs = {
            'user_id': user_id,
            'loop_for_duration': 60 * 5
        }

        # No need to worry as this task is rate limited to 2 per minute
        automate_appreciation_dms_for_user.apply_async(kwargs=kwargs, retry=True,
                                                       countdown=dispatch_jitter_seconds(),
                                                       retry_policy={
                                                           'max_retries': 3,
                                                           'interval_start': 60,
                                                           'interval_step': 30
                                                       })
        dispatched += 1
    if len(users) == 0:
        return "No Active Users"
    else:
        return f"Started Appreciate DM Process for {dispatched}/{len(users)} user(s)"


@shared_task.task
def auto_daily_engagement():
    """Daily golden-hour feed-commenting run — fires EVERY day inside each user's own peak
    engagement window, on top of the pre-post commenting that already runs around each scheduled
    post. This gives a consistent daily reciprocity burst even when a post is (or isn't) scheduled.

    The beat ticks every STAGGER_TICK_MINUTES and dispatches only the users whose staggered slot
    has come up (issue #554): `se_engage` drains ~8 of these 15-minute loops an hour, so fanning
    the whole fleet out at one crontab pushed later users hours past the window they were meant
    for. Volume stays safe because this run and the pre-post runs share the per-day comment cap
    (enforced in comment_on_feed_inline), and QueueOnce (keys=['user_id']) prevents overlapping
    double-runs for the same user.
    """
    if _skip_if_throttled("auto_daily_engagement"):
        return "Automation throttled"
    users = get_active_user_ids()
    dispatched = 0
    for user_id in users:
        if not has_linkedin_session(user_id):
            continue  # no session → the Selenium task would just fail and waste a Chrome slot
        # Session check first: the slot claim is consumed for the day, so a user who couldn't run
        # anyway must not burn theirs — connecting later in the day still earns a run.
        if not _stagger_due(user_id, STAGGER_GOLDEN_HOUR, "auto_daily_engagement"):
            continue  # not this user's slot yet (or already dispatched today)
        # Human pacing (issue #626): the staggered slot is stable per user, so without this the run
        # would start at the same clock minute every single day. A countdown of tens of minutes
        # moves the actual start; the worker is never blocked (this is eta scheduling, not a sleep).
        # The countdown rides the shim below rather than automate_commenting itself — see its
        # docstring for why hanging it on a QueueOnce task would silently drop the pre-post run.
        dispatch_golden_hour_engagement.apply_async(
            kwargs={'user_id': user_id, 'loop_for_duration': 60 * 15},
            countdown=dispatch_jitter_seconds())
        dispatched += 1
    return f"Golden-hour engagement dispatched for {dispatched}/{len(users)} active user(s)"


@shared_task.task
def dispatch_golden_hour_engagement(user_id: int, loop_for_duration: int = 60 * 15):
    """Lock-free carrier for the golden-hour run's pacing countdown (issue #626).

    `automate_commenting` is QueueOnce keyed on user_id, and celery-once takes that lock inside
    apply_async — not when the task finally runs. Hanging the 30–90 min pacing countdown directly
    on it would therefore hold the user's lock for the whole delay, and every pre-post warm-up
    dispatch for that user in the meantime (same key, `graceful: True`) would be swallowed with no
    error and no log — losing exactly the run that matters most. Carrying the countdown here keeps
    the jitter while the lock is taken only at the moment the run is really queued.
    """
    automate_commenting.apply_async(kwargs={'user_id': user_id,
                                            'loop_for_duration': loop_for_duration})
    return f"Golden-hour engagement queued for user {user_id}"


@shared_task.task
def dispatch_scheduled_reply_sweeps():
    """Beat: for users on reply_check_mode='scheduled', run a recent-posts reply sweep at their
    configured cadence (reply_sweeps_per_day, 2–12). A per-user Redis key with TTL = the cadence
    interval gates it: while the key exists we're within the interval and skip, so running this beat
    every ~30 min naturally yields ~reply_sweeps_per_day sweeps. Fails open on Redis outage (dispatch
    anyway) — sweep_reply_comments itself is QueueOnce + 429-safe, so an extra run is harmless.
    """
    if _skip_if_throttled("dispatch_scheduled_reply_sweeps"):
        return "Automation throttled"
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
            # Replying to comments on the user's OWN post is the one engagement a human really does
            # fast, so it keeps the RESPONSIVE profile (issue #626): the exact minute moves, the
            # response stays timely.
            # slot 0 is the single-shot scheduled trigger — the golden-hour amplifier owns the other
            # slots. Explicit here for that reason, not for safety: cqc_lem.app.queue_once fills the
            # default into the dedup key, so omitting it would build the identical lock (#989).
            sweep_reply_comments.apply_async(kwargs={'user_id': user_id, 'sweep_slot': 0},
                                             countdown=dispatch_jitter_seconds(PACE_RESPONSIVE))
            dispatched += 1
    return f"Scheduled reply sweeps dispatched for {dispatched}/{len(users)} user(s)"


@shared_task.task
def dispatch_comment_followups():
    """Beat: revisit posts each user automated a comment on recently and follow up on replies to our
    comment — react, and answer question-replies (issue #478). One sweep per active user with a
    LinkedIn session, per-user interval-gated to ~twice a day; sweep_comment_followups is QueueOnce +
    429-safe so an extra dispatch is harmless.
    """
    if _skip_if_throttled("dispatch_comment_followups"):
        return "Automation throttled"
    from cqc_lem.utilities.linkedin.rate_limit import _redis_client
    users = get_active_user_ids()
    if not users:
        return "No active users"
    client = _redis_client()
    dispatched = 0
    for user_id in users:
        if not has_linkedin_session(user_id):
            continue
        due = True
        if client is not None:
            try:
                due = bool(client.set(f"linkedin:last_comment_followup:{user_id}", "1",
                                      nx=True, ex=12 * 60 * 60))  # ~twice a day
            except Exception:
                due = True
        if due:
            sweep_comment_followups.apply_async(kwargs={'user_id': user_id},
                                                countdown=dispatch_jitter_seconds())
            dispatched += 1
    return f"Comment follow-up sweeps dispatched for {dispatched}/{len(users)} user(s)"


@shared_task.task
def dispatch_comment_outcome_sweeps():
    """Beat: read back what each user's ~24h-old comments actually earned — author replies, likes,
    and whether they survived in LinkedIn's default 'Most relevant' view (issue #628). One sweep per
    active user with a LinkedIn session, per-user interval-gated to once a day; the work list is
    already at-most-once per comment, so an extra dispatch just finds nothing to do.
    """
    if _skip_if_throttled("dispatch_comment_outcome_sweeps"):
        return "Automation throttled"
    from cqc_lem.utilities.linkedin.rate_limit import _redis_client
    users = get_active_user_ids()
    if not users:
        return "No active users"
    client = _redis_client()
    dispatched = 0
    for user_id in users:
        if not has_linkedin_session(user_id):
            continue
        due = True
        if client is not None:
            try:
                due = bool(client.set(f"linkedin:last_comment_outcome:{user_id}", "1",
                                      nx=True, ex=20 * 60 * 60))  # ~once a day
            except Exception:
                due = True
        if due:
            sweep_comment_outcomes.apply_async(kwargs={'user_id': user_id},
                                               countdown=dispatch_jitter_seconds())
            dispatched += 1
    return f"Comment outcome sweeps dispatched for {dispatched}/{len(users)} user(s)"


@shared_task.task(bind=True, base=QueueOnce, once={'graceful': True})
def auto_weekly_comment_quality(self, days: int = 7):
    """Weekly comment-quality scorecard per user (issue #628): author-reply rate, like rate and the
    'Most relevant' demotion rate over the last `days` of outcome readings → PostHog, and the G2
    feedback loop — a demotion rate over threshold on a real sample HOLDS that user's feed
    commenting and escalates, because continuing to spend the day's cap on comments nobody can see
    is the expensive failure this feature exists to catch.
    """
    from cqc_lem.utilities.comment_outcomes import VERDICT_HOLD, comment_quality_report, hold_seconds
    from cqc_lem.utilities.db import get_comment_outcomes
    from cqc_lem.utilities.linkedin.rate_limit import hold_commenting
    from cqc_lem.utilities.logger import log_critical
    from cqc_lem.utilities.observability import track_comment_quality

    users = get_active_user_ids()
    if not users:
        return "No active users"
    reported = held = 0
    for user_id in users:
        report = comment_quality_report(get_comment_outcomes(user_id, days=days), days=days)
        if not report.get("sample_size"):
            continue  # no readings this window — nothing to score or act on
        track_comment_quality(user_id, report)
        reported += 1
        verdict = report.get("verdict") or {}
        if verdict.get("status") == VERDICT_HOLD:
            hold_commenting(user_id, hold_seconds(), reason=verdict.get("reason") or "comment quality")
            # CRITICAL forwards to PostHog automatically — this is the needs-human flag: a human has
            # to fix the comments (or lift the hold), the system deliberately does not self-resume.
            log_critical(f"Feed commenting held for user {user_id} — {verdict.get('reason')}",
                         user_id=user_id, action_type="comment",
                         task_name="auto_weekly_comment_quality")
            held += 1
    return f"Comment quality reported for {reported}/{len(users)} user(s); {held} held"


@shared_task.task(bind=True, base=QueueOnce, once={'graceful': True})
def auto_suppression_tripwire(self):
    """Daily silent-suppression check per user (issue #629). LinkedIn never announces a penalty — a
    flagged account just sees its reach step-collapse and stays collapsed for 60-90 days. A day of
    automation is worth far less than a month of suppressed reach, so a sustained collapse against
    the user's OWN trailing median (or the D4 comment-demotion verdict) auto-pauses engagement,
    emails the user in plain language, and escalates as CRITICAL.

    Scheduled posts, stats capture and analytics are untouched: posting is API-driven and never
    gated, and the read-only capture lanes opt out of THIS pause (`is_measurement_paused`) so the
    trend keeps updating — otherwise the readings would freeze at the collapsed values and recovery
    could never be seen. The pause deliberately never self-resumes: while the trip stands this task
    re-arms it, so only a human clearing it in the UI restarts commenting/DMs.
    """
    from cqc_lem.utilities.comment_outcomes import comment_quality_report
    from cqc_lem.utilities.db import get_comment_outcomes, get_post_performance_rows
    from cqc_lem.utilities.linkedin.rate_limit import (
        automation_pause_reason,
        is_automation_paused,
        is_suppression_pause,
        pause_automation,
        record_suppression_trip,
        suppression_pause_reason,
        suppression_trip_state,
    )
    from cqc_lem.utilities.logger import log_critical
    from cqc_lem.utilities.notifications import notify_suppression_tripwire
    from cqc_lem.utilities.observability import track_suppression_check
    from cqc_lem.utilities.post_stats import build_engagement_trend
    from cqc_lem.utilities.suppression import (
        comment_history_days,
        evaluate_suppression,
        history_days,
        pause_seconds,
        tripwire_enabled,
    )

    if not tripwire_enabled():
        return "Suppression tripwire disabled"
    users = get_active_user_ids()
    if not users:
        return "No active users"
    window = history_days()
    comment_window = comment_history_days()
    checked = tripped = rearmed = 0
    for user_id in users:
        trend = build_engagement_trend(get_post_performance_rows(user_id, days=window))
        quality = comment_quality_report(get_comment_outcomes(user_id, days=comment_window),
                                         days=comment_window)
        verdict = evaluate_suppression(trend, comment_quality=quality)
        checked += 1
        already = suppression_trip_state(user_id)
        if not verdict.get("tripped"):
            # A recovered reading is REPORTED, never auto-cleared: the whole point is that only a
            # human decides the account is safe to automate again.
            track_suppression_check(user_id, verdict, paused=bool(already))
            continue
        seconds = pause_seconds()
        if already:
            # Still tripped and nobody has cleared it — refresh the pause so the TTL can't expire
            # engagement back on, but only when the standing pause is OURS (a maintenance or manual
            # pause has its own lifecycle and must not be extended by 90 days).
            if not is_automation_paused() or is_suppression_pause(automation_pause_reason()):
                pause_automation(seconds, reason=suppression_pause_reason(user_id))
                rearmed += 1
            track_suppression_check(user_id, verdict, paused=True, rearmed=True)
            continue
        pause_automation(seconds, reason=suppression_pause_reason(user_id))
        record_suppression_trip(user_id, verdict.get("reason") or "reach collapse", detail=verdict)
        notify_suppression_tripwire(user_id, verdict.get("reason") or "")
        track_suppression_check(user_id, verdict, paused=True)
        # CRITICAL forwards to PostHog automatically — this is the needs-human flag.
        log_critical(f"Suppression tripwire fired for user {user_id} — {verdict.get('reason')}",
                     user_id=user_id, action_type="rate_limit",
                     task_name="auto_suppression_tripwire")
        tripped += 1
    return (f"Suppression checked for {checked}/{len(users)} user(s); "
            f"{tripped} newly tripped, {rearmed} re-armed")


@shared_task.task(bind=True, base=QueueOnce, once={'graceful': True})
def auto_nightly_content_quality(self, days: int = None):
    """Nightly content-quality scoring pass (issue #630). Scores every piece the user SHIPPED in the
    window — posts, feed comments, newsletter editions — and emits one `content_quality` reading each:
    the D1 slop score, self-similarity against that surface's own recent history, #382's stored
    authenticity score, hook length against the 140-char mobile budget, and engagement per impression
    once the 23:00 stats scrape has captured it.

    Prompt and model changes regress quality SILENTLY (the weekly model-retirement cron swaps models
    under unchanged prompts), so the point of this task is the persisted trend, not any one reading —
    `auto_weekly_content_quality` is what turns the trend into an alert.

    Read-only over content that already shipped: it never edits, holds or re-generates anything, and a
    dimension it cannot measure is recorded as unmeasured rather than as a zero.
    """
    from cqc_lem.utilities.ai.content_framework import COMMENT_HISTORY_LIMIT
    from cqc_lem.utilities.content_quality import (
        SURFACE_COMMENT,
        SURFACE_POST,
        content_quality_enabled,
        detector_daily_max,
        detector_sampled,
        detector_score,
        max_items_per_run,
        score_item,
        similarity_reports,
        window_days,
    )
    from cqc_lem.utilities.db import (
        get_lead_magnet_settings,
        get_recent_comment_texts,
        get_recent_post_texts,
        get_shipped_content_for_quality,
        record_content_quality_score,
    )
    from cqc_lem.utilities.observability import track_content_quality
    from cqc_lem.utilities.post_stats import engagement_rate

    if not content_quality_enabled():
        return "Content quality telemetry disabled"
    users = get_active_user_ids()
    if not users:
        return "No active users"
    window = window_days() if days is None else max(1, int(days))
    cap = max_items_per_run()
    scored = dropped = 0
    for user_id in users:
        items = get_shipped_content_for_quality(user_id, days=window)
        if not items:
            continue
        if len(items) > cap:
            # A silent truncation would read as "a quiet week" in the rollup, so say what was dropped.
            log_warning(f"Content quality scoring capped at {cap} of {len(items)} pieces "
                        f"for user {user_id}", user_id=user_id,
                        task_name="auto_nightly_content_quality")
            dropped += len(items) - cap
            items = items[:cap]

        # Self-similarity is graded WITHIN a surface: a post compared against the user's comments would
        # score as unique no matter how templated it is. Newsletter editions have no body-history
        # reader, so their similarity reports as unmeasured rather than against the wrong scale.
        histories = {
            SURFACE_POST: get_recent_post_texts(user_id, limit=COMMENT_HISTORY_LIMIT),
            SURFACE_COMMENT: get_recent_comment_texts(user_id, limit=COMMENT_HISTORY_LIMIT),
        }
        similarity: dict = {}
        # similarity_reports is the ONLY LLM spend in this pass (one lem-embedding call per surface),
        # and this task loops over users rather than taking a user_id kwarg — so without an explicit
        # scope current_llm_attribution() has nobody to bill and every embedding lands on the "system"
        # sentinel instead of the account whose content it scored.
        with llm_attribution(user_id=user_id, feature=FEATURE_CONTENT):
            for surface, history in histories.items():
                group = [item for item in items if item.get("surface") == surface]
                if not group:
                    continue
                reports = similarity_reports([item.get("text") for item in group], history)
                for item, report in zip(group, reports):
                    similarity[(surface, item.get("ref_id"))] = report

        keyword = (get_lead_magnet_settings(user_id) or {}).get("keyword")
        detector_budget = detector_daily_max()
        for item in items:
            surface = str(item.get("surface") or "")
            detector = None
            if detector_budget > 0 and detector_sampled(surface, item.get("ref_id")):
                detector = detector_score(item.get("text"))
                detector_budget -= 1  # spent whether or not it answered — the CALL is the cost
            score = score_item(
                surface=surface, ref_id=item.get("ref_id"), text=item.get("text"),
                shipped_on=item.get("shipped_on"), format_key=item.get("format_key"),
                authenticity=item.get("authenticity_score"),
                similarity=similarity.get((surface, item.get("ref_id"))),
                engagement_rate=engagement_rate(item.get("reactions"), item.get("comments"),
                                                item.get("reposts"), item.get("impressions")),
                impressions=item.get("impressions"), exempt_keyword=keyword, detector=detector)
            record_content_quality_score(user_id, score)
            track_content_quality(user_id, score)
            scored += 1
    return f"Content quality scored {scored} piece(s) across {len(users)} user(s); {dropped} over cap"


@shared_task.task(bind=True, base=QueueOnce, once={'graceful': True})
def auto_weekly_content_quality(self, days: int = None):
    """Weekly content-quality rollup + regression alerts (issue #630).

    Reads back the nightly scores for TWO periods and compares them, so a regression is measured
    against the account's own prior week rather than an absolute target — the one exception being the
    engagement floor, which is the only threshold with a published benchmark behind it (B2B ER by
    impressions runs ~3.6%).

    Alerts ride the EXISTING pipeline: `log_error` forwards to PostHog at the default
    POSTHOG_LOG_LEVEL, so a regression becomes a grouped `$exception` issue without a second alerting
    path. Nothing is paused or held — quality drift is a "go look at the prompts" signal, not an
    account-safety one (that is #629's job).
    """
    from cqc_lem.utilities.content_quality import content_quality_enabled, quality_rollup, rollup_days
    from cqc_lem.utilities.db import get_content_quality_scores
    from cqc_lem.utilities.logger import log_error
    from cqc_lem.utilities.observability import track_content_quality_rollup

    if not content_quality_enabled():
        return "Content quality telemetry disabled"
    users = get_active_user_ids()
    if not users:
        return "No active users"
    window = rollup_days() if days is None else max(1, int(days))
    reported = alerted = 0
    for user_id in users:
        rows = get_content_quality_scores(user_id, days=window * 2)
        rollup = quality_rollup(rows, days=window)
        if not (rollup.get("current") or {}).get("items"):
            continue  # nothing shipped this period — no rollup to report and nothing to regress
        track_content_quality_rollup(user_id, rollup)
        reported += 1
        for alert in rollup.get("alerts") or []:
            log_error(f"Content quality regression ({alert.get('name')}) for user {user_id} — "
                      f"{alert.get('reason')}", user_id=user_id, action_type="post",
                      task_name="auto_weekly_content_quality")
            alerted += 1
    return (f"Content quality rollup reported for {reported}/{len(users)} user(s); "
            f"{alerted} regression alert(s)")


def _max_dt(*dts):
    """Max of the given datetimes, ignoring None (returns None if all are None)."""
    present = [d for d in dts if d is not None]
    return max(present) if present else None


@attribute_llm_cost(FEATURE_NEWSLETTER)
def _topup_newsletter_drafts_for_user(user_id: int, now: datetime,
                                      allow_bootstrap: bool = True) -> int:
    """Top a single user's review queue up to their max_queued_drafts and return how many drafts were
    generated. Each draft covers the next uncovered cadence slot.

    `allow_bootstrap` gates the first-ever draft (empty queue): the daily beat passes True so the very
    first draft still waits for the generate_lead_days window; an explicit user action (e.g. raising
    the count) passes False to fill the queue ahead immediately.
    """
    import pytz

    from cqc_lem.utilities.ai.ai_helper import (
        generate_newsletter_edition,
        get_or_create_profile_synthesis,
        plan_newsletter_topics,
    )
    from cqc_lem.utilities.ai.content_framework import compact_blueprint
    from cqc_lem.utilities.ai.content_research import research_topic
    from cqc_lem.utilities.blog_source import resolve_blog_source
    from cqc_lem.utilities.db import (
        count_pending_newsletter_editions,
        create_newsletter_edition,
        get_engagement_preferences,
        get_latest_edition_scheduled_for,
        get_newsletter_settings,
        get_pending_newsletter_editions,
        get_recent_newsletter_blueprint_history,
        get_recent_newsletter_subjects,
        get_user_timezone,
    )
    from cqc_lem.utilities.linkedin.helper import load_profile_for_user
    from cqc_lem.utilities.newsletter import should_generate_now, upcoming_publish_slots
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

    # Blog alignment (issue #967): resolved PER edition, so two editions queued in the same run
    # repurpose two different recent articles instead of rehashing one. The first empty resolve means
    # there is nothing readable (no blog set / unreachable), so stop paying for the fetch this run.
    blog_align = bool(settings.get("align_with_blog"))

    generated = 0
    for i, slot in enumerate(slots):
        blog_content = resolve_blog_source(user_id, settings) if blog_align else None
        blog_align = blog_align and blog_content is not None
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
                                              blog_content=blog_content,
                                              subject=subject_ctx, avoid_subjects=avoid,
                                              profile_synthesis=synthesis, blueprint=plan,
                                              avoid_openers=recent_openers, research=research)
        if not edition:
            break
        edition_subject = edition.get("subject") or (plan["subject"] if plan else None)
        new_edition_id = create_newsletter_edition(
            user_id, edition["title"], edition.get("subtitle"),
            edition["body"], slot, subject=edition_subject,
            edition_format=edition.get("format"),
            hook_style=edition.get("hook_style"),
            opening_line=edition.get("opening_line"),
            blueprint=compact_blueprint(plan) if plan else None)
        if not new_edition_id:
            break  # duplicate slot / db error → stop this user's run
        # Opt-in only (issue #893): generation costs money per edition, so a cover is queued for a
        # fresh draft only when the author turned it on for their newsletter. It still lands
        # pending_review — this never publishes an unreviewed image.
        if settings.get("cover_image_auto"):
            generate_newsletter_cover.apply_async(kwargs={"edition_id": new_edition_id})
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
    refills to the cap as editions publish/skip.
    """
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
def auto_track_newsletter_subscribers():
    """Fan out per-user newsletter subscriber-growth tracking for every enabled newsletter user
    (issue #400). Each per-user task reads the subscriber count into the growth time-series and,
    when opted in, invites connections within the per-run cap. Selenium-backed, so it respects the
    global throttle breaker.
    """
    if _skip_if_throttled("auto_track_newsletter_subscribers"):
        return "Skipped (throttled)"
    from cqc_lem.app.run_automation import track_newsletter_subscribers
    from cqc_lem.utilities.db import get_enabled_newsletter_user_ids

    dispatched = 0
    for user_id in get_enabled_newsletter_user_ids():
        track_newsletter_subscribers.apply_async(kwargs={"user_id": user_id})
        dispatched += 1
    return f"Dispatched newsletter subscriber tracking for {dispatched} user(s)"


@shared_task.task
def generate_newsletter_drafts_for_user(user_id: int):
    """Top up a single user's newsletter review queue on demand (e.g. right after they raise their
    max_queued_drafts in settings), so new slots don't wait for the daily beat. Skips the bootstrap
    lead-window gate: an explicit settings change should fill the queue ahead immediately.
    """
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
def generate_newsletter_cover(edition_id: int, use_avatar: bool = None):
    """Generate ONE edition's cover image (issue #893). Costs money per edition, so it only ever
    runs from an explicit opt-in: the author's per-edition "Generate cover" action, or their
    newsletter-level `cover_image_auto` setting.

    ``use_avatar`` is the per-edition choice from the review queue (None = Auto: the
    avatar_use_newsletter opt-in AND the relevance classifier must both agree). The auto
    (cover_image_auto) path always passes None.

    The result lands `pending_review` — never `approved`. A cover is a public brand asset, so the
    author is the gate between a generated image and a published edition; `_approved_cover_path`
    in the publish flow reads that status and nothing else.
    """
    from cqc_lem.utilities.db import get_newsletter_edition, set_edition_cover_image
    from cqc_lem.utilities.linkedin.helper import load_profile_for_user
    from cqc_lem.utilities.newsletter_cover import (
        COVER_SOURCE_AI,
        COVER_STATUS_PENDING,
        generate_cover_for_edition,
        remove_cover_file,
    )

    edition = get_newsletter_edition(edition_id)
    if not edition:
        return f"Edition {edition_id} not found"
    user_id = edition["user_id"]
    previous = edition.get("cover_image_path")
    try:
        profile = load_profile_for_user(user_id)
    except Exception as e:
        # The profile only sharpens the prompt's brand context — losing it is not losing the cover.
        log_debug("Could not load profile for newsletter cover", error=str(e), user_id=user_id,
                  task_name="generate_newsletter_cover")
        profile = None

    path, reason = generate_cover_for_edition(user_id, edition_id, edition.get("title"),
                                              edition.get("subtitle"), edition.get("body"),
                                              profile=profile, use_avatar=use_avatar)
    if not path:
        log_warning("Newsletter cover generation produced nothing", user_id=user_id,
                    task_name="generate_newsletter_cover", reason=reason)
        return f"No cover generated for edition {edition_id}"
    if not set_edition_cover_image(edition_id, user_id, path, COVER_SOURCE_AI,
                                   COVER_STATUS_PENDING):
        # The row is the source of truth: a render nothing points at is an orphan on the assets
        # volume, so it goes out with the failed write.
        remove_cover_file(path)
        log_warning("Could not store the generated newsletter cover", user_id=user_id,
                    task_name="generate_newsletter_cover")
        return f"Cover not stored for edition {edition_id}"
    # Re-generating replaces the row's path — the file it replaced would otherwise stay on disk
    # forever, unreferenced and unreachable.
    if previous and previous != path:
        remove_cover_file(previous)
    log_info("Newsletter cover awaiting review", user_id=user_id,
             task_name="generate_newsletter_cover")
    return f"Generated cover for edition {edition_id}"


@shared_task.task
def regenerate_newsletter_edition(edition_id: int, guidance: str = None):
    """Regenerate ONE queued newsletter edition in place. Generation is a slow lem-complex call, so
    it runs as a task (not inline in the API request). Honors free-text `guidance` when provided
    (edit the same subject OR take a completely different direction); with no guidance the AI decides
    a fresh, distinct take. Grounded in the author's voice synthesis + the newsletter description, and
    steered AWAY from the OTHER queued editions' subjects (and recent history) so regeneration never
    reintroduces a duplicate. Updates the row (title/subtitle/subject/body) and resets status to
    'draft'.
    """
    from cqc_lem.utilities.ai.ai_helper import generate_newsletter_edition, get_or_create_profile_synthesis
    from cqc_lem.utilities.ai.content_framework import compact_blueprint, select_blueprint
    from cqc_lem.utilities.ai.content_research import research_topic
    from cqc_lem.utilities.blog_source import resolve_blog_source
    from cqc_lem.utilities.db import (
        get_engagement_preferences,
        get_newsletter_edition,
        get_newsletter_settings,
        get_pending_newsletter_editions,
        get_recent_newsletter_blueprint_history,
        get_recent_newsletter_subjects,
        update_newsletter_edition,
    )
    from cqc_lem.utilities.linkedin.helper import load_profile_for_user

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
    # Blog alignment (issue #967) — None when the toggle is off or nothing readable came back.
    blog_content = resolve_blog_source(user_id, settings)
    try:
        with llm_attribution(user_id=user_id, feature=FEATURE_NEWSLETTER):
            new_ed = generate_newsletter_edition(profile, topic=settings.get("topic"), prefs=prefs,
                                                 blog_content=blog_content,
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


def _reschedule_pending_editions_forward(user_id: int, editions: list, now: datetime) -> int:
    """Re-slot the given pending editions (ordered by scheduled_for) onto the next consecutive cadence
    slots after `now`, preserving their order. Used when slots were missed so a backlog SHIFTS forward
    as an ordered sequence — the user's planned order is kept and nothing is dropped. Returns how many
    editions actually moved.
    """
    if not editions:
        return 0
    import pytz

    from cqc_lem.utilities.db import get_newsletter_settings, get_user_timezone, update_newsletter_edition
    from cqc_lem.utilities.newsletter import upcoming_publish_slots
    settings = get_newsletter_settings(user_id)
    tz = pytz.timezone(get_user_timezone(user_id))
    slots = upcoming_publish_slots(settings.get("publish_day", 1), settings.get("publish_hour", 9),
                                   settings.get("cadence", "weekly"), now, tz, now, len(editions))
    moved = 0
    for e, slot in zip(editions, slots):
        if e.get("scheduled_for") != slot and update_newsletter_edition(
                e["id"], user_id, scheduled_for=slot):
            moved += 1
    return moved


def _publish_next_due_edition_for_user(user_id: int, now: datetime, dispatch) -> int:
    """Publish at most ONE — the oldest — due edition for a user this run. When more than one is due
    (slots were missed and a backlog built up), publish the oldest and SHIFT the remaining pending
    editions forward onto future cadence slots (order preserved) so a subscriber never receives
    several editions at once. `dispatch(edition_id)` queues the actual Selenium publish. Returns 1 if
    an edition was dispatched, else 0.
    """
    from cqc_lem.utilities.db import get_pending_newsletter_editions
    pending = get_pending_newsletter_editions(user_id)  # ordered by scheduled_for ASC
    due = [e for e in pending
           if e.get("scheduled_for") is not None and e["scheduled_for"] <= now]
    if not due:
        return 0
    oldest = due[0]
    dispatch(oldest["id"])
    if len(due) > 1:
        remaining = [e for e in pending if e["id"] != oldest["id"]]
        moved = _reschedule_pending_editions_forward(user_id, remaining, now)
        log_info(f"Newsletter backlog: published oldest due edition {oldest['id']} and shifted "
                 f"{moved} later edition(s) forward to preserve planned order/cadence",
                 user_id=user_id, task_name="auto_publish_scheduled_editions")
    return 1


@shared_task.task
def auto_publish_scheduled_editions():
    """Publish due newsletter editions — at most ONE per user per run. When slots were missed and a
    backlog built up (e.g. after a throttle/pause), publish the oldest due edition and SHIFT the rest
    forward onto future cadence slots, so a subscriber never gets several editions at once and the
    user's planned order is preserved (nothing is dropped).
    """
    # Newsletter publishing is Selenium-driven, so gate it on the breaker like the other fan-outs.
    # This hourly task was the primary re-tripper of the 429 doom loop: it probed the feed the moment
    # the cooldown lapsed and re-escalated it back to the 6h cap.
    if _skip_if_throttled("auto_publish_scheduled_editions"):
        return "Automation throttled"
    from cqc_lem.app.run_automation import auto_publish_edition
    from cqc_lem.utilities.db import get_editions_due_to_publish
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    due = get_editions_due_to_publish(now)
    user_ids = sorted({e["user_id"] for e in due})
    dispatched = 0
    for uid in user_ids:
        dispatched += _publish_next_due_edition_for_user(
            uid, now, lambda eid: auto_publish_edition.apply_async(kwargs={"edition_id": eid}))
    return f"Dispatched {dispatched} newsletter edition(s) (<=1/user; backlog shifted forward)"


@shared_task.task
def auto_refresh_profile_syntheses():
    """Weekly: (re)generate the cached, DURABLE voice synthesis for each active user whose synthesis is
    missing or stale (>7 days). The synthesis replaces the bloated full profile JSON as the voice
    source in every comment/post prompt; refreshing it on a slow cadence keeps the voice stable while
    still tracking real profile changes. No Selenium — works off each user's cached profile JSON.
    """
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
    """Daily value-add commenting in each active user's ENABLED groups (shares the per-day cap),
    at that user's staggered slot so the single se_content lane drains evenly (issue #554).
    """
    if _skip_if_throttled("auto_group_engagement"):
        return "Automation throttled"
    from cqc_lem.app.run_automation import auto_comment_in_groups
    users = get_active_user_ids()
    n = 0
    for uid in users:
        if has_linkedin_session(uid) and _stagger_due(uid, STAGGER_GROUP_ENGAGEMENT,
                                                      "auto_group_engagement"):
            auto_comment_in_groups.apply_async(kwargs={'user_id': uid})
            n += 1
    return f"Group commenting dispatched for {n}/{len(users)} user(s)"


@shared_task.task
def auto_group_post_drafts():
    """Weekly, days AHEAD of the publish slot: write the coming week's group post into the review
    queue so the user can read and revise it before it ships (issue #932). Which group it is written
    for is the least-recently-tried post-enabled one (issue #769), so the weekly slot still rotates.
    A user who still has an unpublished draft is skipped by the task itself — carrying their edits
    forward beats replacing them with a generation they never asked for.
    """
    from cqc_lem.app.run_automation import auto_draft_group_post
    from cqc_lem.utilities.db import get_next_group_for_post
    users = get_active_user_ids()
    n = 0
    for uid in users:
        # Drafting opens no browser, but a user with no LinkedIn session has nothing to publish
        # into — writing them a post would only spend an LLM call on a draft that can't ship.
        if not has_linkedin_session(uid):
            continue
        group = get_next_group_for_post(uid)
        if group:
            auto_draft_group_post.apply_async(kwargs={'user_id': uid, 'group_id': group['group_id'],
                                                      'group_name': group.get('group_name')})
            n += 1
    return f"Group post drafts dispatched for {n}/{len(users)} user(s)"


@shared_task.task
def auto_group_posts():
    """Weekly: publish the group post the user has had since the draft beat to preview and edit
    (issue #932) into the group it was written for — never a copy or reshare of a scheduled feed
    post. Nothing is generated here: a user with no reviewed draft simply doesn't post this week,
    because an un-previewed group post is exactly what the draft replaced. A draft whose group has
    since been switched off for posting is dropped rather than published into a group the user
    opted out of.
    """
    from cqc_lem.app.run_automation import auto_post_to_group
    from cqc_lem.utilities.db import (
        GroupPostDraftStatus,
        get_open_group_post_draft,
        get_post_enabled_group_ids,
        update_group_post_draft,
    )
    users = get_active_user_ids()
    n = 0
    for uid in users:
        if not has_linkedin_session(uid):
            continue
        draft = get_open_group_post_draft(uid)
        if not draft:
            continue
        post_enabled = get_post_enabled_group_ids(uid)
        if post_enabled is None:
            # Could not read the switches (the read itself already logged the error). Cancelling a
            # post the user reviewed and approved is not something a transient DB fault gets to do,
            # so the draft is left open and publishes at the next weekly slot.
            log_debug("Group post draft held — post switches unreadable this run", user_id=uid,
                      task_name="auto_group_posts")
            continue
        if draft['group_id'] not in post_enabled:
            update_group_post_draft(draft['id'], status=GroupPostDraftStatus.SKIPPED)
            log_info("Group post draft dropped — its group no longer takes posts", user_id=uid,
                     task_name="auto_group_posts")
            continue
        auto_post_to_group.apply_async(kwargs={'user_id': uid, 'group_id': draft['group_id'],
                                               'group_name': draft.get('group_name'),
                                               'draft_id': draft['id']})
        n += 1
    return f"Group posts dispatched for {n}/{len(users)} user(s)"


@shared_task.task
def auto_scrape_stats():
    """Daily: capture engagement stats on each active user's recent posts (powers post-time recs)."""
    if _skip_if_throttled("auto_scrape_stats", measurement_only=True):
        return "Automation throttled"
    from cqc_lem.app.run_automation import auto_scrape_post_stats
    users = get_active_user_ids()
    n = 0
    for uid in users:
        if has_linkedin_session(uid):
            auto_scrape_post_stats.apply_async(kwargs={'user_id': uid})
            n += 1
    return f"Stats scrape dispatched for {n}/{len(users)} user(s)"


@shared_task.task
def auto_capture_follower_stats():
    """Daily: snapshot each active user's follower/connection counts and profile views (issue #627).
    Selenium-backed, so it respects the global throttle breaker, and it only dispatches for users
    who actually have a LinkedIn session to read the numbers with.
    """
    if _skip_if_throttled("auto_capture_follower_stats", measurement_only=True):
        return "Automation throttled"
    from cqc_lem.app.run_automation import capture_follower_stats
    users = get_active_user_ids()
    n = 0
    for uid in users:
        if has_linkedin_session(uid):
            capture_follower_stats.apply_async(kwargs={'user_id': uid})
            n += 1
    return f"Audience capture dispatched for {n}/{len(users)} user(s)"


@shared_task.task
def auto_send_due_followups():
    """Dispatch a per-user Selenium task to send due DM follow-ups (each gated by reply-detection)."""
    if _skip_if_throttled("auto_send_due_followups"):
        return "Automation throttled"
    from cqc_lem.app.run_automation import process_user_followups
    from cqc_lem.utilities.db import get_due_followups
    # due_at is stored naive-UTC; compare against naive-UTC now (not container-local time).
    due = get_due_followups(datetime.now(timezone.utc).replace(tzinfo=None))
    user_ids = sorted({f["user_id"] for f in due})
    for uid in user_ids:
        process_user_followups.apply_async(kwargs={"user_id": uid})
    return f"Dispatched follow-ups for {len(user_ids)} user(s)"


@shared_task.task
def auto_process_outreach_funnel():
    """Dispatch per-user processing of APPROVED comment->connect->DM funnel targets (issue #399).
    Approval-gated: only targets a human has approved advance, and each advance drops the target
    back to 'pending' for re-approval — nothing auto-fires at volume.
    """
    if _skip_if_throttled("auto_process_outreach_funnel"):
        return "Automation throttled"
    from cqc_lem.app.run_automation import process_outreach_funnel
    from cqc_lem.utilities.db import get_users_with_approved_outreach
    user_ids = get_users_with_approved_outreach()
    for uid in user_ids:
        process_outreach_funnel.apply_async(kwargs={"user_id": uid})
    return f"Dispatched outreach funnel for {len(user_ids)} user(s)"


@shared_task.task
def auto_scan_connection_candidates():
    """Dispatch per-user sourcing of ICP-fit connection targets from content engagers (issue #486).
    Sourcing may scrape adjacent authors' posts, so it IS gated on the 429 breaker / manual pause.
    The scan only FILES targets into the #398 connection_requests queue — the approval gate and the
    combined daily invite cap still decide what actually gets sent.
    """
    if _skip_if_throttled("auto_scan_connection_candidates"):
        return "Automation throttled"
    from cqc_lem.app.run_automation import scan_connection_candidates
    user_ids = get_active_user_ids()
    for uid in user_ids:
        scan_connection_candidates.apply_async(kwargs={"user_id": uid})
    return f"Dispatched connection targeting for {len(user_ids)} user(s)"


@shared_task.task
def auto_scan_outreach_funnel_targets():
    """Dispatch per-user sourcing for the comment-first outreach funnel (issue #623). The funnel
    processor (#399) has always existed; nothing ever fed it, so outreach_funnel_targets had zero
    rows in production. Sourcing scrapes the roster's recent posts, so it IS gated on the 429
    breaker / manual pause. It only FILES drafts — approval and the daily caps still gate sends.
    """
    if _skip_if_throttled("auto_scan_outreach_funnel_targets"):
        return "Automation throttled"
    from cqc_lem.app.run_automation import scan_outreach_funnel_targets
    user_ids = get_active_user_ids()
    for uid in user_ids:
        scan_outreach_funnel_targets.apply_async(kwargs={"user_id": uid})
    return f"Dispatched outreach funnel sourcing for {len(user_ids)} user(s)"


# Lead scoring & CRM-lite pipeline (issue #484). Nothing here touches LinkedIn — it re-reads
# engagement we already stored — so it is deliberately NOT gated on the 429 breaker / pause: a
# rate-limited account still deserves an accurate hot-leads list.
LEAD_ACTIVITY_WINDOW_DAYS = 90


def _lead_target_terms(prefs: dict) -> list:
    """What "good fit" means for this user, straight from their engagement preferences."""
    terms: list = []
    for key in ("focus_topics", "include_topics", "include_keywords"):
        terms.extend(prefs.get(key) or [])
    return [str(t).strip() for t in terms if str(t or "").strip()]


def _rebuild_leads_for_user(user_id: int, days: int = LEAD_ACTIVITY_WINDOW_DAYS) -> int:
    """Re-score one user's whole pipeline from existing engagement data. The reset runs first (and
    even when there's no activity at all) so people who went quiet decay out of 'hot'.
    """
    from cqc_lem.utilities.db import get_lead_activity, get_profile_facts, reset_lead_scores, upsert_lead
    from cqc_lem.utilities.lead_scoring import score_leads

    reset_lead_scores(user_id)
    rows = get_lead_activity(user_id, days=days)
    if not rows:
        return 0
    prefs = get_engagement_preferences(user_id) or {}
    urls = sorted({r.get("person_profile_url") for r in rows if r.get("person_profile_url")})
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    leads = score_leads(rows, now, facts_by_url=get_profile_facts(urls),
                        target_terms=_lead_target_terms(prefs))
    saved = 0
    for lead in leads:
        if upsert_lead(user_id, lead.person_key, person_name=lead.person_name,
                       person_profile_url=lead.person_profile_url, score=lead.score,
                       icp_score=lead.icp_score, engagement_score=lead.engagement_score,
                       stage=lead.stage, signals=",".join(lead.signals)[:255],
                       signal_count=lead.signal_count, reasons=lead.reasons,
                       next_action=lead.next_action, first_signal_at=lead.first_signal_at,
                       last_signal_at=lead.last_signal_at):
            saved += 1
    log_info(f"Rebuilt {saved} lead(s) for user {user_id}", user_id=user_id,
             task_name="rebuild_leads_for_user")
    return saved


@shared_task.task
def rebuild_leads_for_user(user_id: int):
    """Re-score a single user's lead pipeline (nightly, or on demand from the pipeline board)."""
    return f"Scored {_rebuild_leads_for_user(user_id)} lead(s) for user {user_id}"


@shared_task.task
def rebuild_lead_scores():
    """Nightly: re-score every active user's leads so the hot-leads list is fresh each morning."""
    users = get_active_user_ids()
    for uid in users:
        rebuild_leads_for_user.apply_async(kwargs={"user_id": uid})
    return f"Lead re-scoring dispatched for {len(users)} user(s)"


@shared_task.task
def auto_scan_catchup_moments():
    """Dispatch the daily per-user LinkedIn Catch-up scan (issue #482). Drafting only — the scan writes
    approval-gated rows to catchup_touches and sends nothing. Users who disabled every milestone type
    are skipped so we never open a Chrome session for them.
    """
    task_name = "auto_scan_catchup_moments"
    # The dispatcher reports too (issue #792): a lane that never dispatches because the 429 breaker
    # is open looks exactly like a lane whose per-user scans found nothing, and the difference is the
    # whole answer to "catch-up isn't sending anything".
    if _skip_if_throttled(task_name):
        report_catchup_run(None, {"phase": CATCHUP_PHASE_SCAN,
                                  "status": CATCHUP_STATUS_THROTTLED}, task_name)
        return "Automation throttled"
    from cqc_lem.app.run_automation import automate_catchup_touches
    dispatched = 0
    for user_id in get_active_user_ids():
        if not (get_engagement_preferences(user_id).get("catchup_event_types") or []):
            continue
        automate_catchup_touches.apply_async(kwargs={'user_id': user_id})
        dispatched += 1
    report_catchup_run(None, {"phase": CATCHUP_PHASE_SCAN, "dispatched": dispatched,
                              "status": CATCHUP_STATUS_DISPATCHED if dispatched
                              else CATCHUP_STATUS_DISABLED}, task_name)
    return f"Dispatched catch-up scan for {dispatched} user(s)"


@shared_task.task
def auto_check_catchup_touches():
    """Send APPROVED catch-up congratulations on a slow, per-user-capped drip (issue #482). Mirrors
    auto_check_connection_requests: approval happens upstream (rows only reach 'approved' via the API
    or the user's auto_approve mode), the whole scan short-circuits while the kill-switch / 429 breaker
    is open, and the send task re-checks both caps and the throttle.
    """
    task_name = "auto_check_catchup_touches"
    if _skip_if_throttled(task_name):
        report_catchup_run(None, {"phase": CATCHUP_PHASE_SEND,
                                  "status": CATCHUP_STATUS_THROTTLED}, task_name)
        return "Automation throttled"

    approved = get_approved_catchup_touches()
    active_user_ids = set(get_active_user_ids()) if approved else set()

    dispatched = 0
    capped = 0
    inactive = 0
    budgets: dict = {}  # user_id -> remaining catch-up touches allowed today
    for touch_id, user_id in approved:
        if user_id not in active_user_ids:
            # DEBUG + a counted funnel stage, not a WARNING: this drip beats every 20 minutes, so a
            # single queued touch on a disconnected account would re-emit at ERROR and file a grouped
            # $exception ~72x a day. `inactive` on the run report is the durable signal.
            inactive += 1
            log_debug("Skipping catch-up touch — user not active/connected",
                      user_id=user_id, task_name=task_name)
            continue
        if user_id not in budgets:
            # 10/day is a premium-plan allowance; every other plan tops out at 5 (see db.py).
            cap = min(int(get_engagement_preferences(user_id).get("max_catchup_touches_per_day") or 0),
                      max_catchup_touches_allowed(user_id))
            budgets[user_id] = max(0, cap - count_catchup_touches_sent_today(user_id))
        if budgets[user_id] <= 0:
            capped += 1
            continue  # cap met for today — the rest stay 'approved' for tomorrow
        # Durable send claim: once we dispatch a touch, mark it `sending` so a lost worker can't
        # send it again without passing the `catchup_send_attempts` guard inside send_catchup_touch.
        if not update_catchup_touch_status(touch_id, CatchupTouchStatus.SENDING):
            continue
        budgets[user_id] -= 1
        send_catchup_touch.apply_async(kwargs={'touch_id': touch_id})
        dispatched += 1

    # Re-queue touches stuck in 'sending' whose send task was lost (container restart) — mirrors the
    # orphaned connection-request recovery. The 2-hour gap avoids racing an in-flight task. A claim row
    # in catchup_send_attempts will stop the second send if the first one actually landed.
    orphaned = get_orphaned_catchup_touches(lookback_hours=2)
    for touch_id, user_id in orphaned:
        log_warning(f"Re-queueing orphaned catch-up touch {touch_id}",
                    user_id=user_id, task_name=task_name)
        if update_catchup_touch_status(touch_id, CatchupTouchStatus.SENDING):
            send_catchup_touch.apply_async(kwargs={'touch_id': touch_id})

    # The drafted-but-unapproved backlog, counted on EVERY beat (issue #792). The scan reports its
    # `drafted` count once a day; for the other 23 hours a full approval queue and an empty lane both
    # looked like `nothing_to_send` — which is the reported symptom ("not sending, not in any queue").
    pending = count_pending_catchup_touches()

    if dispatched or orphaned:
        status = CATCHUP_STATUS_DISPATCHED
    elif capped:
        status = CATCHUP_STATUS_CAPPED
    elif inactive:
        status = CATCHUP_STATUS_INACTIVE  # a queue that exists but whose owners can't be sent for
    elif pending:
        status = CATCHUP_STATUS_AWAITING_APPROVAL  # working as designed — the user has to approve
    else:
        status = CATCHUP_STATUS_NOTHING_TO_SEND
    report_catchup_run(None, {"phase": CATCHUP_PHASE_SEND, "status": status,
                              "dispatched": dispatched, "capped": capped, "inactive": inactive,
                              "pending": pending, "requeued": len(orphaned)}, task_name)
    if dispatched == 0 and len(orphaned) == 0:
        return "No Catch-up Touches to Send"
    return f"Dispatched {dispatched} catch-up touch(es); re-queued {len(orphaned)} orphaned"


@shared_task.task
def auto_sync_brand_account():
    """Daily: hold the LEM brand (dogfooding) account's outbound volume at whatever the current
    LAUNCH_PHASE allows (issue #504).

    Everything the brand actually DOES — the 30-day content plan, feed commenting, connect targeting,
    appreciation/outreach DMs + follow-ups, the newsletter, company-page invites — already reaches it
    through the same per-active-user beats as any paying customer, under the same caps, 429 backoff
    and per-user proxy. So this task adds no outreach of its own: it only seeds the phase's caps and
    connect posture onto a brand account that has never saved engagement preferences of its own, and
    holds every account under the shipped per-user ceilings.

    It deliberately does NOT re-assert the phase over settings the owner saved (issue #952): user 1
    is his ordinary account too, so the Settings hub is the sign-off, and a nightly re-assertion just
    reverted what the SPA had recommended to him hours earlier.

    The brand user is user 1 by convention (issue #736), so this always has an account to sync — no
    env var can leave it dormant, and the only reported failures are an unreadable row and a failed
    write.
    """
    from cqc_lem.utilities.brand_account import CAP_FIELDS, brand_user_id, current_launch_phase, sync_brand_preferences

    user_id = brand_user_id()
    phase = current_launch_phase()
    applied = sync_brand_preferences(phase)
    if applied is None:
        return f"Brand account sync failed (phase {phase})"

    # The brand only engages while it counts as an active user — surface the gap rather than letting
    # a disconnected/lapsed brand account look like it is quietly marketing.
    if user_id not in set(get_active_user_ids()):
        log_warning("Brand account is not active/connected — its automation will not run",
                    user_id=user_id, task_name="auto_sync_brand_account")

    labels = {"max_comments_per_day": "comments/day", "max_dms_per_day": "DMs/day",
              "max_invites_per_day": "invites/day"}
    # Only the caps this run actually wrote — an account whose own saved caps are already within
    # policy has none, and reporting the phase's numbers there would read as an edit that never
    # happened.
    caps = ", ".join(f"{labels[f]} {applied[f]}" for f in CAP_FIELDS if f in applied)
    if caps:
        summary = f"Brand account {user_id} synced to phase {phase} ({caps})"
    elif applied:
        # Caps were left alone but something else was (the content seeding) — "nothing changed"
        # would be as wrong here as reporting caps this run never wrote.
        summary = (f"Brand account {user_id} checked against phase {phase} — its own caps stand; "
                   f"seeded {', '.join(sorted(applied))}")
    else:
        summary = (f"Brand account {user_id} checked against phase {phase} — its own caps are "
                   f"within policy, nothing changed")
    log_info(summary, user_id=user_id, task_name="auto_sync_brand_account")
    return summary


@shared_task.task
def auto_notify_missing_linkedin_session():
    """Email active users who have no validated LinkedIn session cookie, prompting them
    to connect — automation can't run without one. Throttled per-user inside
    notify_linkedin_session, so this can run daily without spamming.
    """
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
def auto_refresh_linkedin_tokens():
    """Daily: renew every connected user's LinkedIn authorization BEFORE it lapses, and email the
    ones we cannot renew for them (issue #600).

    LinkedIn fixes the access token at 60 days and gives no way to ask for a longer one, so a
    rolling refresh is the only thing that makes the connection outlive it. Until now the ONLY
    thing that ever refreshed a token was the SPA hitting /user/token_status — a user who didn't
    open the app simply lapsed, and the in-app warning they eventually saw had no email behind it.
    The email is throttled per-user inside notify_linkedin_token_expiring, so this runs daily
    without spamming.
    """
    from cqc_lem.utilities.db import get_linkedin_token_user_ids
    from cqc_lem.utilities.linkedin.token_refresh import resolve_token_status
    from cqc_lem.utilities.notifications import notify_linkedin_token_expiring

    user_ids = get_linkedin_token_user_ids()
    refreshed = 0
    notified = 0
    for user_id in user_ids:
        try:
            status = resolve_token_status(user_id, auto_refresh=True)
            if status["refresh_succeeded"]:
                refreshed += 1
                continue
            # Still short after the attempt (or never refreshable at all) — only a human reconnect
            # fixes it from here, so say so out of band rather than only in the SPA.
            if status["is_expired"] or status["is_expiring_soon"]:
                if notify_linkedin_token_expiring(user_id, status["days_remaining"],
                                                  status["token_expiry_date"]):
                    notified += 1
        except Exception as e:
            log_warning("Failed to renew LinkedIn token", exc=e, user_id=user_id,
                        task_name="auto_refresh_linkedin_tokens")
    log_info(f"LinkedIn tokens: refreshed {refreshed}, emailed {notified} of {len(user_ids)} "
             f"connected user(s)", task_name="auto_refresh_linkedin_tokens")
    return (f"Refreshed {refreshed} and emailed {notified} of {len(user_ids)} "
            f"connected user(s)")


@shared_task.task
def auto_onboarding_nudges():
    """Daily: advance every not-yet-activated user's checklist (persisting steps + emitting the
    activation funnel to PostHog) and email the ONE next-best nudge to those who stalled (issue
    #500). Each nudge is one-shot per user and capped at one per NUDGE_COOLDOWN_HOURS, so this can
    run daily without spamming — same posture as auto_notify_missing_linkedin_session.
    """
    from cqc_lem.utilities.db import get_onboarding_candidate_user_ids
    from cqc_lem.utilities.onboarding import next_nudge_for_user, send_onboarding_nudge, sync_onboarding_state

    users = get_onboarding_candidate_user_ids()
    nudged = 0
    for user_id in users:
        try:
            state = sync_onboarding_state(user_id)
            nudge = next_nudge_for_user(user_id, state)
            if nudge and send_onboarding_nudge(user_id, nudge):
                nudged += 1
        except Exception as e:
            log_warning("Failed to process onboarding nudge", exc=e, user_id=user_id,
                        task_name="auto_onboarding_nudges")
    log_info(f"Onboarding: nudged {nudged} of {len(users)} un-activated user(s)",
             task_name="auto_onboarding_nudges")
    return f"Nudged {nudged} of {len(users)} un-activated user(s)"


@shared_task.task
def auto_survey_prompts():
    """Daily: email the ONE survey worth asking each subscriber — the day-3 NPS after activation, the
    trial T-3d NPS, or the review that unlocks the extended trial (issue #501). Each survey is
    one-shot per user and capped at one ask per SURVEY_COOLDOWN_HOURS, so this is safe to run daily;
    a user who already answered (or dismissed the in-app modal) is never emailed about it.
    """
    from cqc_lem.utilities.db import get_survey_candidate_user_ids
    from cqc_lem.utilities.surveys import next_survey_for_user, send_survey_prompt

    users = get_survey_candidate_user_ids()
    asked = 0
    for user_id in users:
        try:
            survey = next_survey_for_user(user_id)
            if survey and send_survey_prompt(user_id, survey):
                asked += 1
        except Exception as e:
            log_warning("Failed to process survey prompt", exc=e, user_id=user_id,
                        task_name="auto_survey_prompts")
    log_info(f"Surveys: asked {asked} of {len(users)} subscriber(s)",
             task_name="auto_survey_prompts")
    return f"Asked {asked} of {len(users)} subscriber(s)"


@shared_task.task
def auto_backfill_missing_assets():
    """Safety net: regenerate missing media for unposted video/carousel posts before they
    publish, so a post never reaches its scheduled time without its asset (e.g. when the
    original generation failed).
    """
    from cqc_lem.app.run_content_plan import regenerate_post_carousel_task, regenerate_post_video_task
    from cqc_lem.utilities.db import get_unposted_posts_missing_assets

    posts = get_unposted_posts_missing_assets()
    queued = 0
    for post_id, user_id, post_type, buyer_stage, scheduled_time in posts:
        pt = str(post_type).lower()
        if pt == 'video':
            regenerate_post_video_task.apply_async(kwargs={'post_id': post_id})
            queued += 1
        elif pt in ('carousel', 'document'):  # documents share the carousel slide pipeline
            regenerate_post_carousel_task.apply_async(kwargs={'post_id': post_id})
            queued += 1
        log_warning("Backfilling missing media asset for unposted post",
                    post_id=post_id, user_id=user_id, task_name="auto_backfill_missing_assets")
    log_info(f"Asset backfill: queued {queued} regeneration(s) across {len(posts)} post(s)",
             task_name="auto_backfill_missing_assets")
    return f"Queued {queued} asset regeneration(s)"


@shared_task.task(bind=True, base=QueueOnce, once={'graceful': True})
def auto_encrypt_secrets_at_rest(self):
    """Encrypt (and re-key) every LinkedIn secret still stored unprotected (issue #745, PR 2a).

    Scheduled daily rather than hand-run once because the same pass is BOTH the backfill and the
    key rotation: after `LEM_SECRET_KEY_PREVIOUS` is set and `LEM_SECRET_KEY` replaced, the next
    run re-seals every row under the new version. It is idempotent, so on an ordinary day it reads
    a few rows and writes nothing.

    `plaintext_remaining > 0` is logged as a WARNING on purpose — that number is the gate on
    flipping `ENCRYPTION_REQUIRED`, and a silent backfill that quietly skipped rows would make the
    fail-closed switch look safe when it isn't.
    """
    from cqc_lem.utilities.db import encrypt_secrets_at_rest
    stats = encrypt_secrets_at_rest()
    if not stats.get("enabled"):
        return "Encryption disabled (no LEM_SECRET_KEY) — nothing done"
    if stats.get("plaintext_remaining"):
        log_warning(f"{stats['plaintext_remaining']} secret(s) still unencrypted at rest "
                    f"({stats.get('failed', 0)} failed to re-encrypt, "
                    f"{stats.get('orphaned', 0)} orphaned cookie row(s) to delete)",
                    task_name="auto_encrypt_secrets_at_rest")
    return (f"Re-encrypted {stats.get('rewritten', 0)} secret(s); "
            f"{stats.get('plaintext_remaining', 0)} still unprotected")


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
        return "No Active Users"
    else:
        return f"Started Process for {len(users)} user(s)"


@shared_task.task
def auto_clean_stale_profiles():
    """Cleans up stale profiles for each active user"""
    # Get all active users and loop through them
    users = get_active_user_ids()

    for user_id in users:
        log_info("Cleaning stale profiles", user_id=user_id, task_name="auto_clean_stale_profiles")

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
        return "No Active Users"
    else:
        return f"Started Process for {len(users)} user(s)"


@shared_task.task
def auto_invite_to_company_pages():
    """DAILY company-page invite drip, at each user's own staggered slot (issue #732).

    This used to be one crontab on the 1st of the month at 05:00 UTC that drained the whole
    invitation-credit pool in a single sitting — the loudest velocity pattern in the product, and
    the only outbound lane that consulted neither `max_invites_per_day` nor the #626 pacing engine.
    Now it ticks every STAGGER_TICK_MINUTES like the other fan-outs and dispatches only the users
    whose slot has come up, so the fleet spreads across the window instead of landing on
    `se_outreach` in one minute; the per-run volume is decided inside the task itself
    (`plan_daily_invites`), which is also what makes a second dispatch today a no-op.
    """
    if _skip_if_throttled("auto_invite_to_company_pages"):
        return "Automation throttled"

    # Get all active users and loop through them
    users = get_active_user_ids()

    started = 0
    for user_id in users:
        # Only invite for users who have actually set a company page — otherwise the
        # inviter would build "<None>?invite=true" and fail.
        if not get_company_linked_in_url_for_user(user_id):
            log_debug("Skipping company page invites — no company page set",
                      user_id=user_id, task_name="auto_invite_to_company_pages")
            continue
        # Page check first: the slot claim is spent for the day, so a user who has nothing to
        # invite to must not burn theirs — setting a page later still earns that day's run.
        if not _stagger_due(user_id, STAGGER_COMPANY_INVITE, "auto_invite_to_company_pages"):
            continue

        log_info("Starting company page invites", user_id=user_id, task_name="auto_invite_to_company_pages")
        automate_invites_to_company_page_for_user.apply_async(kwargs={'user_id': user_id},
                                         countdown=dispatch_jitter_seconds(),
                                         retry=True,
                                         retry_policy={
                                             'max_retries': 3,
                                             'interval_start': 60,
                                             'interval_step': 30
                                         })
        started += 1

    if started == 0:
        return "No active users with a company page"
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
    """Flatten Selenium's per-run recording folders into one folder per video name.

    The grid drops each session's `.mp4` into its own throwaway folder, so a given session name's
    history ends up scattered across dozens of them. Each file is re-homed to
    `<name>/<mtime as %Y_%m_%d_%H_%M_%S>.mp4` and its now-empty source folder deleted. The file's
    mtime IS its identity in the destination, so two recordings of the same name in the same second
    collide.

    Folders whose name starts with `CQC_LEM` (the `se:name` prefix set in `selenium_util.py`) are
    skipped entirely.
    """
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
        fetch_subscription,
        get_subscription_tier_from_price,
        stripe_status_to_db,
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


@shared_task.task(bind=True, base=QueueOnce, once={'graceful': True})
def auto_weekly_margin_report(self, days: int = 7):
    """Weekly unit-economics report (issue #491, plan §D.2): joins `cost_ledger` spend to Stripe MRR
    for per-user contribution margin, system gross margin, cohort engagement lift and LTV:CAC, then
    delivers it to the owner (email + a PostHog `margin_report` scorecard event).
    """
    from cqc_lem.utilities.margin import collect_margin_report, send_weekly_margin_report

    report = collect_margin_report(days=days)
    result = send_weekly_margin_report(report)
    system = report.get("system", {})
    return (f"Margin report {report.get('period', {}).get('start')}→"
            f"{report.get('period', {}).get('end')}: {system.get('active_users')} user(s), "
            f"gross margin {system.get('gross_margin_usd')} USD "
            f"(emailed={result['emailed']}, tracked={result['tracked']})")


@shared_task.task(bind=True, base=QueueOnce, once={'graceful': True})
def auto_daily_cost_alerts(self, days: int = 7):
    """Daily budget/anomaly guardrails (issue #493, plan §E.2): per-user cost ceiling, system
    gross-margin floor, spend anomaly (μ+Nσ / absolute budget), LLM cache-hit collapse and
    unattributed spend. Evaluates the last COMPLETE UTC day and delivers only actual breaches
    (owner email + a PostHog `cost_alert` event each).
    """
    from cqc_lem.utilities.cost_alerts import collect_cost_alert_report, send_cost_alerts

    report = collect_cost_alert_report(days=days)
    result = send_cost_alerts(report)
    return (f"Cost alerts {report.get('date')}: {result['alerts']} alert(s), "
            f"{report.get('critical_count')} critical, "
            f"skipped {report.get('skipped_checks')} "
            f"(emailed={result['emailed']}, tracked={result['tracked']})")


@shared_task.task(bind=True, base=QueueOnce, once={'graceful': True})
def auto_capacity_watch(self):
    """Sample the Chrome pool + Selenium lane depths and judge them over the rolling window (issue
    #552, docs/scaling-plan.md §5e). Every tick samples; only a SUSTAINED breach delivers — and then
    it files/updates one GitHub issue asking for a lane/cap review, because raising them spends real
    resources on a shared box.
    """
    from cqc_lem.utilities.capacity_alerts import collect_capacity_report, send_capacity_alerts

    report = collect_capacity_report()
    if not report.get("alert_count"):
        return (f"Capacity ok: {report.get('sample_count')} sample(s), "
                f"skipped {report.get('skipped_checks')}")
    result = send_capacity_alerts(report)
    return (f"Capacity alerts: {result['alerts']} breach(es), "
            f"{report.get('critical_count')} critical "
            f"(github={result['github'].get('action')}, tracked={result['tracked']})")


@shared_task.task(bind=True, base=QueueOnce, once={'graceful': True})
def auto_file_feedback_issues(self, limit: int = 25):
    """Drain the captured-feedback queue into the work pipeline (issue #498, plan §B.2/B.3): classify
    each new report, dedup it against the open clusters (+1 the existing issue) and open ONE
    `MODE=start`-shaped issue per genuinely new problem. QueueOnce so two beat ticks can never file
    the same report twice.

    The same pass repairs already-filed issues whose labels/assignee/Decision Comment never landed
    (issue #718) — an unlabeled issue is invisible to the agent pipeline, so filing alone is not the
    job being done.
    """
    from cqc_lem.utilities.feedback.issue_service import process_new_feedback, repair_auto_filed_issues

    result = process_new_feedback(limit=limit)
    repair = repair_auto_filed_issues()
    return (f"Feedback filing: {result['processed']} row(s) — "
            f"{result['counts'] or 'nothing to do'}; repair: {repair['scanned']} checked, "
            f"{repair['repaired']} repaired, {repair['failed']} still broken")


@shared_task.task(bind=True, base=QueueOnce, once={'graceful': True})
def auto_recluster_feedback(self, limit: int = 200):
    """Nightly reclustering of the feedback backlog (issue #498, plan §B.3): backfill missing
    embeddings and attach leftover/untriaged reports to the open cluster they now match, bumping that
    issue's demand. Files nothing — that stays with `auto_file_feedback_issues`.
    """
    from cqc_lem.utilities.feedback.issue_service import recluster_feedback

    result = recluster_feedback(limit=limit)
    return (f"Feedback recluster: {result['scanned']} scanned, {result['attached']} attached to "
            f"{result['clusters']} open cluster(s), {result['embedded']} embedding(s) backfilled")


@shared_task.task(bind=True, base=QueueOnce, once={'graceful': True})
def auto_changelog_notify(self, hours: int = None):
    """Close the feedback loop on shipped work (issue #502, plan §B.4): scan recently merged PRs for
    `Closes #<issue>`, generate the changelog line for each issue that came from user feedback, and
    tell every reporter behind that cluster — once — by email + an in-app "you asked, we shipped"
    notice carrying the micro-CSAT ("did this fix it?"). QueueOnce + the recipient ledger mean an
    overlapping window can never notify the same reporter twice.
    """
    from cqc_lem.utilities.feedback.shipped import process_shipped_fixes

    result = process_shipped_fixes(hours=hours)
    return (f"Shipped-fix notify: {result['pull_requests']} merged PR(s) — "
            f"{result['counts'] or 'nothing to do'}")


@shared_task.task(bind=True, base=QueueOnce, once={'graceful': True})
def auto_update_faq(self, limit: int = 50):
    """Keep the public FAQ answering what people actually ask (issue #507, plan §C.7): cluster the
    support questions the auto-filer left behind, and once the same question recurs, write or revise
    a grounded answer, version it, and reply to everyone who asked. Anything implying a
    product/policy/ToS claim is held for a human instead of published. QueueOnce so two beat ticks
    can never answer the same cluster twice.
    """
    from cqc_lem.utilities.feedback.faq_service import process_faq_feedback

    result = process_faq_feedback(limit=limit)
    return (f"Auto-FAQ: {result['questions']} question(s) in {result['clusters']} cluster(s) — "
            f"{result['counts'] or 'nothing to do'}")


def _env_rate(name: str) -> float:
    """A monthly cost rate from the environment. Prices are deployment-specific, so they are never
    hardcoded — an unset (or malformed) rate is 0 and simply accrues nothing.
    """
    try:
        return float(os.getenv(name, "0") or 0)
    except ValueError:
        log_warning(f"Invalid {name} — treating as 0", task_name="auto_accrue_monthly_costs")
        return 0.0


def _monthly_cost_accruals(user_rows: list, proxy_rate: float, region_rate: float,
                           infra_monthly: float) -> list:
    """Per-user proxy + infra accruals for one month (issue #490, plan §A.2).

    A user with their own `users.proxy_url` is on the paid per-user path and carries the full
    PROXY_COST_PER_USER_MONTH; users sharing a regional proxy split REGION_PROXY_COST across
    everyone routed through that same proxy. Infra is total fixed / active users.
    """
    from cqc_lem.utilities.db import CostCategory
    from cqc_lem.utilities.proxy import resolve_proxy

    accruals = []
    shared: dict = {}
    for row in user_rows:
        if row.get("proxy_url"):
            if proxy_rate:
                accruals.append({"user_id": row["user_id"], "category": CostCategory.PROXY,
                                 "usd": proxy_rate, "provider": "per_user_proxy", "qty": 1})
            continue
        resolved = resolve_proxy(None, row.get("country"))
        if resolved:
            shared.setdefault(resolved, []).append(row["user_id"])

    if region_rate:
        for user_ids in shared.values():
            share = region_rate / len(user_ids)
            accruals.extend({"user_id": uid, "category": CostCategory.PROXY, "usd": share,
                             "provider": "region_proxy", "qty": 1} for uid in user_ids)

    if infra_monthly and user_rows:
        share = infra_monthly / len(user_rows)
        accruals.extend({"user_id": row["user_id"], "category": CostCategory.INFRA, "usd": share,
                         "provider": "hosting", "qty": 1} for row in user_rows)

    return [a for a in accruals if a["usd"] > 0]


@shared_task.task(bind=True, base=QueueOnce, once={'graceful': True})
def auto_accrue_monthly_costs(self):
    """Monthly: accrue the fixed costs no per-call event can capture — each active user's egress
    proxy and their share of the amortized infra bill — into cost_ledger (issue #490).

    Idempotent per (user, category, month), so a re-run (or a manual catch-up) never double-charges.
    """
    from cqc_lem.utilities.db import accrue_monthly_fixed_costs, get_users_proxy_config

    user_ids = get_active_user_ids()
    if not user_ids:
        log_info("Monthly cost accrual: no active users", task_name="auto_accrue_monthly_costs")
        return 0

    period = datetime.now(timezone.utc).date().replace(day=1)
    accruals = _monthly_cost_accruals(
        get_users_proxy_config(user_ids),
        _env_rate("PROXY_COST_PER_USER_MONTH"),
        _env_rate("REGION_PROXY_COST"),
        _env_rate("INFRA_FIXED_MONTHLY"),
    )
    written = accrue_monthly_fixed_costs(period, accruals)
    log_info(f"Monthly cost accrual {period}: {written} row(s) for {len(user_ids)} active user(s)",
             task_name="auto_accrue_monthly_costs")
    return written


@shared_task.task(bind=True, base=QueueOnce, once={'graceful': True})
def auto_rollup_llm_costs(self):
    """Daily: collapse yesterday's per-call LLM spend (accumulated in Redis) into cost_ledger —
    one row per user x feature x tier x day, so the durable table stays small (issue #490).
    """
    from cqc_lem.utilities.observability import flush_llm_cost_rollup

    written = flush_llm_cost_rollup()
    log_info(f"LLM cost rollup: wrote {written} ledger row(s)", task_name="auto_rollup_llm_costs")
    return written


@shared_task.task(bind=True, base=QueueOnce, once={'graceful': True})
def auto_weekly_cost_routing(self, days: int = COST_ROUTING_WINDOW_DAYS):
    """Weekly cost-aware routing optimization (issue #494, plan §D.1(1)): scores every running
    down-route experiment against the §D.3 quality gate (engagement non-inferiority +
    `posts.authenticity_score`), promotes the ones that hold, **auto-rolls-back** the ones that
    don't, opens at most `COST_ROUTING_MAX_EXPERIMENTS` new ones, and publishes the policy the
    LiteLLM complexity router reads. While the `cost-routing-enabled` flag is off (its fallback is
    COST_ROUTING_ENABLED) it observes nothing and only republishes the parked policy, so the loop is
    dormant until the flag is turned on — and because that is resolved per run, turning it on takes
    effect on the next weekly beat rather than the next deploy.
    """
    from cqc_lem.utilities.cost_routing import apply_routing_report, collect_routing_report

    report = collect_routing_report(days=days)
    result = apply_routing_report(report)
    return (f"Cost routing {report.get('date')}: {result['changes']} change(s), "
            f"{result['rollbacks']} rollback(s) over {report.get('observations')} post(s) "
            f"(published={result['published']}, emailed={result['emailed']})")


@shared_task.task(bind=True, base=QueueOnce, once={'graceful': True}, queue='se_content',
                  reject_on_worker_lost=True)
def auto_produce_feature_tutorial(self, flow_key: str = None):
    """Weekly: produce ONE automated SPA feature tutorial — headless capture, grounded script, TTS
    voice-over, ffmpeg MP4 + vertical clip, YouTube publish (issue #505). Runs on the se_content
    Selenium lane because it drives a real browser; it never touches LinkedIn, so the 429 breaker
    does not gate it. Uncovered features come first, then anything whose UI has changed.
    """
    from cqc_lem.utilities.marketing.video_tutorials import (
        TutorialCaptureError,
        TutorialGuardrailError,
        TutorialRenderError,
        produce_tutorial,
    )

    try:
        record = produce_tutorial(flow_key=flow_key)
    except (TutorialCaptureError, TutorialGuardrailError, TutorialRenderError) as e:
        # Fail-closed and loudly: a stale flow / bad script must be fixed, not published.
        log_warning("Feature tutorial production aborted", exc=e,
                    task_name="auto_produce_feature_tutorial")
        return f"Aborted: {e}"
    if not record:
        return "No tutorial produced"
    return (f"Produced tutorial '{record['flow']}' ({record['duration_seconds']}s, "
            f"${record['total_usd']}, youtube={'yes' if record.get('youtube_url') else 'no'})")


@shared_task.task(bind=True, base=QueueOnce, once={'graceful': True})
def auto_weekly_youtube_token_check(self):
    """Weekly YouTube OAuth refresh-token health probe (issue #742). One token exchange — no upload,
    no quota — that both proves the grant is alive and IS the keep-alive against Google's 6-month
    disuse expiry, which is why it must keep running while the tutorial feature is off. Alerts the
    owner only when the grant is provably gone; an unreachable token endpoint stays `unknown`.
    """
    from cqc_lem.utilities.marketing.youtube_auth import run_health_probe

    state = run_health_probe()
    return (f"YouTube token {state.get('status')} at {state.get('checked_at')} "
            f"({state.get('reason')}, emailed={state.get('emailed')})")


if __name__ == "__main__":
    print("Process finished")
