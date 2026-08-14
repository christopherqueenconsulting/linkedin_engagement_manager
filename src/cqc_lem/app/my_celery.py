"""The Celery app itself: broker wiring, the whole beat schedule, and per-task telemetry.

Every recurring behaviour in LEM is scheduled from `app.conf.beat_schedule` below, so this file is
the one place that answers "what runs, and when". The signal handlers are the other half of that:
Celery swallows a failed task's traceback into its own logger, so `on_task_failure` / `on_task_retry`
are what turn a crashed task into a grouped PostHog issue (issue #648) instead of a log line nobody
counts, and the prerun/postrun pair is what gives every task a duration.

The broker is Redis locally and SQS on AWS; the result backend must stay Redis either way, because
SQS is fire-and-forget and the API layer queries task state.
"""

import time as _time
from datetime import datetime, timedelta, timezone
from typing import Optional

from celery import Celery
from celery.schedules import crontab
from celery.signals import (
    task_failure,
    task_postrun,
    task_prerun,
    task_received,
    task_retry,
    worker_process_init,
)

from cqc_lem.app import celeryconfig
from cqc_lem.app.celeryconfig import broker_url
from cqc_lem.utilities.engagement_window import STAGGER_TICK_MINUTES
from cqc_lem.utilities.env_constants import AWS_REGION
from cqc_lem.utilities.logger import logger
from cqc_lem.utilities.observability import capture_exception, track_task
from cqc_lem.utilities.utils import get_cloudwatch_client

# AWS deployment: uses SQS as broker (see celeryconfig.py CELERY_BROKER_URL)
# and Redis (ElastiCache) as result backend (CELERY_RESULT_BACKEND env var).
# Do NOT use SQS as the result backend — SQS is fire-and-forget; Redis supports
# task state queries and result retrieval required by the API layer.

# Create default Celery app
app = Celery(
    'cqc_lem',
    broker=broker_url,

)



# Set the Celery configuration
app.config_from_object(celeryconfig)

# When we use the following in Django, it loads all the <appname>.tasks
# files and registers any tasks it finds in them. We can import the
# tasks files some other way if we prefer.
app.autodiscover_tasks(['cqc_lem'])

# Setup Celery Once for task that should only be queued once per parameters sent

app.conf.ONCE = {
    # celery-once uses Redis for deduplication lock tracking even when the Celery broker is SQS.
    # On AWS, point CELERY_BROKER_URL at the ElastiCache Redis instance (not SQS) so celery-once works.
    'backend': 'celery_once.backends.Redis',
    'settings': {
        'url': broker_url,
        'default_timeout': 60 * 60,
        'blocking': True,
        'blocking_timeout': 30,
    }
}

# Celery configuration
app.conf.update(
    result_expires=3600,
    beat_schedule={
        # Comment error tracing out
        # 'test-error-tracing': {
        #    'task': 'cqc_lem.app.run_scheduler.test_error_tracing',
        #    'schedule': timedelta(minutes=1),  # Run every 1 minutes
        # },
        'check-scheduled-posts': {
            'task': 'cqc_lem.app.run_scheduler.auto_check_scheduled_posts',
            # Run every 10 min so the 20-min lookahead always covers the next run — no
            # blind window where a post is picked up only after its time (was '0,30':
            # a 30-min cadence vs 20-min lookahead left a 10-min gap → posts up to ~10
            # min late, and any non-:00/:30 scheduled time could slip).
            'schedule': crontab(minute='*/10')
        },
        'check-scheduled-dms': {
            'task': 'cqc_lem.app.run_scheduler.auto_check_scheduled_dms',
            # Same 10-min cadence as scheduled posts (20-min lookahead covers each run).
            'schedule': crontab(minute='*/10')
        },
        'check-connection-requests': {
            'task': 'cqc_lem.app.run_scheduler.auto_check_connection_requests',
            # Approved proactive connect requests (issue #398); daily-capped at dispatch. 5-min
            # cadence keeps the drip human-paced while staying responsive (no volume prospecting).
            'schedule': crontab(minute='*/5')
        },
        'sync-brand-account': {
            'task': 'cqc_lem.app.run_scheduler.auto_sync_brand_account',
            # Just before the content plan runs, so the brand's phase caps/posture are already in
            # place for the day's dogfooding run (issue #504). The brand user is user 1 by
            # convention (issue #736), so this needs no configuration to do its work.
            'schedule': crontab(hour='0', minute='45')
        },
        'generate-content-plan': {
            'task': 'cqc_lem.app.run_content_plan.auto_generate_content',
            'schedule': crontab(hour='1', minute='0')  # Run every day at 1:00 AM
        },
        'create-content-from-plan': {
            'task': 'cqc_lem.app.run_content_plan.auto_create_weekly_content',
            'schedule': crontab(hour='1', minute='30')  # Run every day at 1:30 AM
        },
        'backfill-missing-assets': {
            'task': 'cqc_lem.app.run_scheduler.auto_backfill_missing_assets',
            'schedule': crontab(minute='15', hour='*/3')  # Every 3 hours — regen any missing video/carousel media
        },
        'encrypt-secrets-at-rest': {
            'task': 'cqc_lem.app.run_scheduler.auto_encrypt_secrets_at_rest',
            # Daily 03:10 UTC — quiet window between the 02:40 content-quality pass and the 03:30
            # lead re-score. Idempotent backfill AND key rotation (issue #745): a normal day is a
            # no-op, the day after a key swap it re-seals every row under the new version.
            'schedule': crontab(hour='3', minute='10')
        },
        'clean-up-stale-invites': {
            'task': 'cqc_lem.app.run_scheduler.auto_clean_stale_invites',
            'schedule': crontab(hour='2', minute='0', )  # Run every day at 2:00 AM
        },
        'send-due-dm-followups': {
            'task': 'cqc_lem.app.run_scheduler.auto_send_due_followups',
            'schedule': crontab(minute='*/30')  # Send due multi-touch DM follow-ups every 30 min
        },
        'process-outreach-funnel': {
            'task': 'cqc_lem.app.run_scheduler.auto_process_outreach_funnel',
            'schedule': crontab(minute='*/30')  # Advance approved comment->connect->DM funnel steps
        },
        'scan-catchup-moments': {
            'task': 'cqc_lem.app.run_scheduler.auto_scan_catchup_moments',
            # Daily draft-only pass over the LinkedIn Catch-up feed (issue #482). Nothing sends here —
            # it queues approval-gated congratulations for the review UI.
            'schedule': crontab(hour='12', minute='30')
        },
        'send-catchup-touches': {
            'task': 'cqc_lem.app.run_scheduler.auto_check_catchup_touches',
            # Slow drip of APPROVED catch-up congratulations, per-user daily capped
            'schedule': crontab(minute='*/20')
        },
        'daily-golden-hour-engagement': {
            'task': 'cqc_lem.app.run_scheduler.auto_daily_engagement',
            # Ticks every STAGGER_TICK_MINUTES; the task itself dispatches only the users whose
            # staggered slot has come up, spreading peak-hour feed commenting across each user's
            # local window instead of handing se_engage the whole fleet at once (issue #554).
            'schedule': crontab(minute=f'*/{STAGGER_TICK_MINUTES}')
        },
        'dispatch-comment-followups': {
            'task': 'cqc_lem.app.run_scheduler.dispatch_comment_followups',
            # Every 6h; a per-user 12h Redis interval gates it to ~twice a day. React + answer
            # question-replies on our automated comments (issue #478).
            'schedule': crontab(minute='0', hour='*/6')
        },
        'dispatch-comment-outcome-sweeps': {
            'task': 'cqc_lem.app.run_scheduler.dispatch_comment_outcome_sweeps',
            # Daily 08:00 UTC; a per-user 20h Redis interval gates it to ~once a day. Read-only
            # revisit of ~24h-old comments to record replies/likes/'Most relevant' visibility
            # (issue #628). Clear of the 06:00 Stripe sync and the golden-hour engagement windows.
            'schedule': crontab(hour='8', minute='0')
        },
        'weekly-comment-quality': {
            'task': 'cqc_lem.app.run_scheduler.auto_weekly_comment_quality',
            # Mondays 08:45 UTC — after that morning's outcome sweep has landed, so the week's score
            # includes the freshest readings. Holds commenting for a user whose comments are being
            # demoted out of 'Most relevant' (issue #628).
            'schedule': crontab(hour='8', minute='45', day_of_week='monday')
        },
        'suppression-tripwire': {
            'task': 'cqc_lem.app.run_scheduler.auto_suppression_tripwire',
            # Daily 09:15 UTC — after the 08:00 comment-outcome sweep (so the demotion signal is
            # fresh) and the previous night's 23:00 post-stats scrape, and between the 09:00
            # missing-session email and the 09:30 onboarding nudge so a user never gets two
            # automated emails in the same minute. Auto-pauses engagement on a reach collapse (#629).
            'schedule': crontab(hour='9', minute='15')
        },
        'nightly-content-quality': {
            'task': 'cqc_lem.app.run_scheduler.auto_nightly_content_quality',
            # Nightly 02:40 UTC — after the 23:00 post-stats scrape and the 23:40 follower capture, so
            # yesterday's posts already have impressions and can be scored for engagement-per-impression
            # (issue #630). Ahead of the 03:30 lead re-score so the two nightly passes don't overlap.
            'schedule': crontab(hour='2', minute='40')
        },
        'weekly-content-quality': {
            'task': 'cqc_lem.app.run_scheduler.auto_weekly_content_quality',
            # Mondays 09:45 UTC — after the 08:45 comment-quality score and the 09:15 suppression
            # tripwire, so the quality rollup is the last of the Monday reports and reads a fully
            # scored week (issue #630).
            'schedule': crontab(hour='9', minute='45', day_of_week='monday')
        },
        'generate-newsletter-drafts': {
            'task': 'cqc_lem.app.run_scheduler.auto_generate_newsletter_drafts',
            'schedule': crontab(hour='10', minute='0')  # Daily: top up each user's newsletter draft queue for review
        },
        'notify-pending-newsletter-covers': {
            'task': 'cqc_lem.app.run_scheduler.auto_notify_pending_covers',
            # Daily 10:30 UTC — after the 10:00 draft top-up, so a cover queued by that run has
            # rendered before this pass reads its status (issue #1432).
            'schedule': crontab(hour='10', minute='30')
        },
        'publish-scheduled-newsletters': {
            'task': 'cqc_lem.app.run_scheduler.auto_publish_scheduled_editions',
            'schedule': crontab(minute='5')  # Hourly: publish any edition whose scheduled slot has arrived
        },
        'track-newsletter-subscribers': {
            'task': 'cqc_lem.app.run_scheduler.auto_track_newsletter_subscribers',
            # Weekly (Mon 11:00 UTC): snapshot subscriber counts + run opted-in invites within caps
            'schedule': crontab(hour='11', minute='0', day_of_week='1')
        },
        'dispatch-scheduled-reply-sweeps': {
            'task': 'cqc_lem.app.run_scheduler.dispatch_scheduled_reply_sweeps',
            # Every 30 min; a per-user Redis interval key gates it to reply_sweeps_per_day (2–12) sweeps.
            'schedule': crontab(minute='*/30')
        },
        'sync-user-groups': {
            'task': 'cqc_lem.app.run_scheduler.auto_sync_groups',
            'schedule': crontab(hour='7', minute='0', day_of_week='monday')  # Refresh joined groups weekly
        },
        'refresh-profile-syntheses': {
            'task': 'cqc_lem.app.run_scheduler.auto_refresh_profile_syntheses',
            # Weekly: regenerate each active user's DURABLE voice synthesis (missing/stale >7d) used as
            # the voice source for every comment/post — Mondays at 4:30 AM, off-peak.
            'schedule': crontab(hour='4', minute='30', day_of_week='monday')
        },
        'group-engagement': {
            'task': 'cqc_lem.app.run_scheduler.auto_group_engagement',
            # Daily value-add commenting in enabled groups, at each user's staggered slot (#554)
            'schedule': crontab(minute=f'*/{STAGGER_TICK_MINUTES}')
        },
        'group-post-drafts': {
            'task': 'cqc_lem.app.run_scheduler.auto_group_post_drafts',
            # Two days before the publish slot: write the week's group post so the user has a real
            # review window to read and revise it in the SPA (issue #932).
            'schedule': crontab(hour='15', minute='0', day_of_week='sunday')
        },
        'group-posts': {
            'task': 'cqc_lem.app.run_scheduler.auto_group_posts',
            # Weekly value-add group post — publishes the reviewed draft, generates nothing itself.
            'schedule': crontab(hour='15', minute='0', day_of_week='tuesday')
        },
        'scrape-post-stats': {
            'task': 'cqc_lem.app.run_scheduler.auto_scrape_stats',
            'schedule': crontab(hour='23', minute='0')  # Nightly: capture post engagement for time recs
        },
        'capture-follower-stats': {
            'task': 'cqc_lem.app.run_scheduler.auto_capture_follower_stats',
            # Nightly at 23:40 — after the 23:00 post-stats scrape, so a day's audience snapshot and
            # its content outcomes land in the same night and the growth panel can overlay them.
            'schedule': crontab(hour='23', minute='40')
        },
        'rebuild-lead-scores': {
            'task': 'cqc_lem.app.run_scheduler.rebuild_lead_scores',
            # Nightly at 3:30 AM — after the 11 PM stats scrape, so the morning hot-leads list
            # reflects everything that happened yesterday.
            'schedule': crontab(hour='3', minute='30')
        },
        'scan-connection-candidates': {
            'task': 'cqc_lem.app.run_scheduler.auto_scan_connection_candidates',
            # Daily at 4:00 AM — after the 3:30 lead re-score, so targeting reads a fresh view of who
            # is actually engaging. Files a few targets a day; sending stays daily-capped + approved.
            'schedule': crontab(hour='4', minute='0')
        },
        'scan-outreach-funnel-targets': {
            'task': 'cqc_lem.app.run_scheduler.auto_scan_outreach_funnel_targets',
            # Daily at 4:20 AM — after the 4:00 connection-candidate scan, so anyone already filed
            # as a connection request that run is excluded from the funnel rather than worked twice.
            'schedule': crontab(hour='4', minute='20')
        },
        'clen-up-stale-profiles': {
            'task': 'cqc_lem.app.run_scheduler.auto_clean_stale_profiles',
            'schedule': crontab(hour='3', minute='0', )  # Run every day at 3:00 AM
        },
        #'clen-up-old_videos': {
        #    'task': 'cqc_lem.app.run_scheduler.auto_clean_old_videos',
        #    'schedule': crontab(hour='4', minute='0', )  # Run every day at 4:00 AM
        #},
        'invite_to_company_pages': {
            'task': 'cqc_lem.app.run_scheduler.auto_invite_to_company_pages',
            # DAILY drip at each user's staggered slot (issue #732), replacing the once-a-month
            # 05:00-UTC blast that drained the whole invitation-credit pool in one sitting. The
            # per-day volume is capped and paced inside the task.
            'schedule': crontab(minute=f'*/{STAGGER_TICK_MINUTES}')
        },
        'notify-missing-linkedin-session': {
            'task': 'cqc_lem.app.run_scheduler.auto_notify_missing_linkedin_session',
            'schedule': crontab(hour='9', minute='0')  # Daily 9:00 AM — throttled per-user to 1/week
        },
        'refresh-linkedin-tokens': {
            'task': 'cqc_lem.app.run_scheduler.auto_refresh_linkedin_tokens',
            # Daily 8:30 AM — BEFORE the 9:00 missing-session pass, so a token renewed here never
            # produces a reconnect email half an hour later. Email is throttled per-user (#600).
            'schedule': crontab(hour='8', minute='30')
        },
        'onboarding-nudges': {
            'task': 'cqc_lem.app.run_scheduler.auto_onboarding_nudges',
            # Daily 9:30 AM — right after the missing-session email, so a user who just got that one
            # isn't emailed twice in the same minute. Each nudge is one-shot per user (issue #500).
            'schedule': crontab(hour='9', minute='30')
        },
        'survey-prompts': {
            'task': 'cqc_lem.app.run_scheduler.auto_survey_prompts',
            # Daily 9:45 AM — after the onboarding nudge, so a stalled user gets the setup reminder
            # rather than a survey in the same pass. Each survey is one-shot per user (issue #501).
            'schedule': crontab(hour='9', minute='45')
        },
        'send-appreciation-dms': {
            'task': 'cqc_lem.app.run_scheduler.auto_appreciate_dms',
            'schedule': crontab(minute=f'*/{STAGGER_TICK_MINUTES}')  # per-user staggered slot (#554)
        },
        'rollup-llm-costs': {
            'task': 'cqc_lem.app.run_scheduler.auto_rollup_llm_costs',
            # Daily at 00:20 UTC — collapses the finished day's Redis cost buckets into cost_ledger.
            'schedule': crontab(hour='0', minute='20')
        },
        'accrue-monthly-costs': {
            'task': 'cqc_lem.app.run_scheduler.auto_accrue_monthly_costs',
            # 1st of the month at 5:15 AM — proxy + infra accruals for the month just started.
            'schedule': crontab(hour='5', minute='15', day_of_month='1')
        },
        'sync-stripe-subscriptions': {
            'task': 'cqc_lem.app.run_scheduler.sync_stripe_subscriptions',
            'schedule': crontab(hour='6', minute='0')  # Daily at 6:00 AM — safety-net for missed webhooks
        },
        'weekly-margin-report': {
            'task': 'cqc_lem.app.run_scheduler.auto_weekly_margin_report',
            # Mondays 12:00 UTC — after the 6:00 Stripe sync, so MRR reflects current tiers, and after
            # Sunday night's stats scrape, so cohort engagement lift covers the full week.
            'schedule': crontab(hour='12', minute='0', day_of_week='monday')
        },
        'file-feedback-issues': {
            'task': 'cqc_lem.app.run_scheduler.auto_file_feedback_issues',
            # Every 30 min — captured feedback becomes a pipeline-ready issue the same hour it lands
            # (issue #498). Deduped against open clusters, so a burst of the same report is one issue.
            'schedule': crontab(minute='*/30')
        },
        'recluster-feedback': {
            'task': 'cqc_lem.app.run_scheduler.auto_recluster_feedback',
            # Nightly 2:45 AM — regroups the backlog (embedding backfill + late duplicates) between
            # the 2:00 stale-invite clean-up and the 3:00 stale-profile pass. Files nothing.
            'schedule': crontab(hour='2', minute='45')
        },
        'changelog-notify': {
            'task': 'cqc_lem.app.run_scheduler.auto_changelog_notify',
            # Daily 10:00 AM — after the 9:45 survey pass, so a reporter never gets a survey invite
            # and a "we shipped it" email in the same minute. The 48h lookback overlaps deliberately;
            # each reporter is notified once via shipped_notice_recipients (issue #502).
            'schedule': crontab(hour='10', minute='0')
        },
        'update-faq': {
            'task': 'cqc_lem.app.run_scheduler.auto_update_faq',
            # Hourly at :50 — after both `file-feedback-issues` ticks (:00/:30) have triaged the
            # queue, since the FAQ pass only ever reads what the filer already left behind (#507).
            # Cheap: an answer is generated only when the same question recurs.
            'schedule': crontab(minute='50')
        },
        'capacity-watch': {
            'task': 'cqc_lem.app.run_scheduler.auto_capacity_watch',
            # Every 15 min — one sample per tick builds the rolling window (672 samples ≈ 7 days),
            # and only a SUSTAINED breach files the lane/cap review issue (#552). Cheap: one
            # /status GET + one LLEN per lane.
            'schedule': crontab(minute='*/15')
        },
        'daily-cost-alerts': {
            'task': 'cqc_lem.app.run_scheduler.auto_daily_cost_alerts',
            # Daily 13:00 UTC — scores YESTERDAY (the last complete UTC day), after the 6:00 Stripe
            # sync so tier MRR is current, and after the weekly margin report so they never overlap.
            'schedule': crontab(hour='13', minute='0')
        },
        'produce-feature-tutorial': {
            'task': 'cqc_lem.app.run_scheduler.auto_produce_feature_tutorial',
            # Wednesdays 20:00 UTC — one tutorial a week (issue #505), off-peak and clear of the
            # Selenium engagement lanes' busy hours. Runs until every top-level feature is covered,
            # then only re-films a flow whose UI actually changed.
            'schedule': crontab(hour='20', minute='0', day_of_week='wednesday')
        },
        'youtube-token-check': {
            'task': 'cqc_lem.app.run_scheduler.auto_weekly_youtube_token_check',
            # Wednesdays 19:30 UTC — 30 min BEFORE the tutorial run, so a dead grant is alerted on
            # (and the run aborts at its own preflight) rather than discovered after the render
            # spend. Weekly on purpose: the probe is also the keep-alive against Google's
            # 6-month-disuse expiry, so it must keep running while the feature is off (issue #742).
            'schedule': crontab(hour='19', minute='30', day_of_week='wednesday')
        },
        'weekly-cost-routing': {
            'task': 'cqc_lem.app.run_scheduler.auto_weekly_cost_routing',
            # Mondays 14:00 UTC — after the margin report (12:00) and the alert sweep (13:00), so a
            # routing change is judged on the same week's cost/quality picture those just reported.
            'schedule': crontab(hour='14', minute='0', day_of_week='monday')
        },

    }
)


# Dont use for reds
'''
@worker_process_init.connect(weak=False)
def restore_all_unacknowledged_messages(*args, **kwargs):
    """
    Restores all the unacknowledged messages in the queue but with proper visibility timeout
    Taken from https://gist.github.com/mlavin/6671079
    """
    conn = app.connection(transport_options={'visibility_timeout': 3600})
    qos = conn.channel().qos
    qos.restore_visible()
    myprint('Unacknowledged messages restored')
'''


def get_queue_metric(name_space: str = 'cqc-lem/celery_queue/celery', metric_name: str = 'QueueLength',
                     period: int = 60, time_delta_minutes: int = 1, statistics: str = "Maximum") -> int:
    """The most recent CloudWatch datapoint for a queue metric, or 0 when there is nothing to read.

    0 stands for every non-answer — no `AWS_REGION` (the Docker Compose deployment, which has no
    CloudWatch at all), an empty window, or a failed API call. Nothing propagates: this is
    instrumentation, and reading a metric must never be the reason the caller fails.
    """
    if not AWS_REGION:
        return 0

    try:
        cloudwatch = get_cloudwatch_client(AWS_REGION)

        response = cloudwatch.get_metric_statistics(
            Namespace=name_space,
            MetricName=metric_name,
            StartTime=datetime.now() - timedelta(minutes=time_delta_minutes),
            EndTime=datetime.now(),
            Period=period,
            Statistics=[statistics]
        )

        if response['Datapoints']:
            # Get the most recent datapoint
            latest_datapoint = max(response['Datapoints'], key=lambda x: x['Timestamp'])
            return latest_datapoint[statistics]
        return 0  # Return 0 if no datapoints found

    except Exception as e:
        logger.error(f"Failed to get metric: {str(e)}")
        return 0


_task_start_times: dict = {}


@worker_process_init.connect(weak=False)
def configure_posthog_for_worker(**kwargs) -> None:
    """Put PostHog into sync mode in each forked worker, or everything it captures is lost."""
    # Celery forks worker processes; the PostHog background Consumer thread does not
    # survive fork. Sync mode sends each capture immediately instead of queuing.
    import posthog as _posthog
    _posthog.sync_mode = True


@task_prerun.connect(weak=False)
def on_task_prerun(task_id: str = None, task=None, **kwargs) -> None:
    """Stamp a task's start so `on_task_postrun` can report how long it actually took.

    Keyed by `task_id` because a worker runs several tasks at once; the postrun handler POPS the
    entry, which is the only thing keeping this dict from growing for the life of the process.
    """
    _task_start_times[task_id] = _time.time()


@task_postrun.connect(weak=False)
def on_task_postrun(task_id: str = None, task=None, state: str = None, **kwargs) -> None:
    """Emit the `celery_task` event for every task that ran, however it ended.

    `success` is strictly `state == "SUCCESS"`, so a RETRY or a REVOKED task is never counted as a
    win. A task whose prerun never fired has no start to pop and reports ~0ms rather than dropping
    the event — an unmeasured run still has to appear in the count.
    """
    start = _task_start_times.pop(task_id, _time.time())
    track_task(
        task_name=task.name,
        duration_ms=int((_time.time() - start) * 1000),
        success=(state == "SUCCESS"),
        state=state or "UNKNOWN",
    )


def _task_user_id(kwargs) -> Optional[int]:
    """The user a task was dispatched for. Every per-user task in LEM carries `user_id` in its
    kwargs, which is the only attribution a signal handler can see.
    """
    if not isinstance(kwargs, dict):
        return None
    user_id = kwargs.get("user_id")
    try:
        return int(user_id) if user_id is not None else None
    except (TypeError, ValueError):
        return None


@task_failure.connect(weak=False)
def on_task_failure(task_id: str = None, exception: BaseException = None, sender=None,
                    kwargs: dict = None, einfo=None, **_) -> None:
    """File a failed task's exception as a grouped PostHog error-tracking issue (issue #648). Celery
    swallows the traceback into its own logger, so without this the only trace of a crashed task is
    a log line — nothing that groups, counts or alerts.
    """
    capture_exception(
        exception,
        user_id=_task_user_id(kwargs),
        task_name=getattr(sender, "name", None),
        task_id=task_id,
        source="celery.task_failure",
    )


@task_retry.connect(weak=False)
def on_task_retry(request=None, reason=None, sender=None, einfo=None, **_) -> None:
    """A retry is a failure that will be tried again — worth the same issue so a task that only ever
    succeeds on its 3rd attempt is still visible. `reason` is the exception when the retry was
    raised from one; anything else (a bare `self.retry()`) carries no exception to group and is
    skipped rather than filed as a synthetic one.
    """
    if not isinstance(reason, BaseException):
        return
    capture_exception(
        reason,
        user_id=_task_user_id(getattr(request, "kwargs", None)),
        task_name=getattr(sender, "name", None),
        task_id=getattr(request, "id", None),
        retries=getattr(request, "retries", None),
        source="celery.task_retry",
    )


def update_queue_length_metric(sender=None, headers=None, **kwargs) -> int:
    """Get the current queue length from Redis broker and push to CloudWatch.

    Connected only when `AWS_REGION` is set — see below. The Redis LLEN happens BEFORE the
    CloudWatch check, so on a deploy with nowhere to publish this is pure cost on the hot path.
    """
    # Use the global app
    global app

    base_namespace = "cqc-lem/celery_queue/"
    queue_name = getattr(sender, 'queue', 'celery')
    name_space = base_namespace + queue_name

    # For Redis broker, get the actual Redis connection
    with app.pool.acquire(block=True) as conn:
        redis = conn.default_channel.client

        # Get length of the main celery queue
        total_tasks = redis.llen(queue_name)
        #print(f"List length for {queue_name}: {total_tasks}")

    #print(f"Total tasks found: {total_tasks}")


    if AWS_REGION:
        try:
            cloudwatch = get_cloudwatch_client(AWS_REGION)

            # Push metric to CloudWatch
            cloudwatch.put_metric_data(
                Namespace=name_space,
                MetricData=[
                    {
                        'MetricName': 'QueueLength',
                        'Value': total_tasks,
                        'Unit': 'Count',
                        'Timestamp': datetime.now(timezone.utc),
                        'Dimensions': [
                            {
                                'Name': 'QueueName',
                                'Value': 'celery'
                            }
                        ]
                    }
                ]
            )
            logger.info(
                f"Successfully published metric to CloudWatch: Queue length [{queue_name}]: {total_tasks}")

        except Exception as e:
            logger.error(f"Failed to publish metric: {str(e)}")

    return total_tasks


# Connected ONLY when there is a CloudWatch to publish to. It used to hang off task_sent,
# task_received AND task_success unconditionally — three broker-connection acquisitions and three
# Redis LLENs per task, on the hot path, whose result was then discarded on the Compose deploy
# (the only supported one, where AWS_REGION is unset). One signal is enough to sample queue depth;
# on Compose, the `capacity-watch` beat already reports it every 15 minutes without touching the
# per-task path at all. `weak=False` keeps the receiver alive past this module's import scope.
if AWS_REGION:
    task_received.connect(update_queue_length_metric, weak=False)