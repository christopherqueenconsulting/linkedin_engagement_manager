import time as _time
from datetime import datetime, timedelta, timezone

from celery import Celery
from celery import current_app
from celery.schedules import crontab
from celery.signals import worker_process_init, task_received, task_success, task_sent, task_prerun, task_postrun
from celery.app.control import Inspect

from cqc_lem.app import celeryconfig
from cqc_lem.app.celeryconfig import broker_url
from cqc_lem.utilities.env_constants import CODE_TRACING, AWS_REGION
from cqc_lem.utilities.jaeger_tracer_helper import get_jaeger_tracer
from cqc_lem.utilities.logger import myprint, logger
from cqc_lem.utilities.observability import track_task
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
            'schedule': crontab(hour='13', minute='0')  # Daily peak-hour feed commenting (~9am ET), on top of pre-post
        },
        'dispatch-comment-followups': {
            'task': 'cqc_lem.app.run_scheduler.dispatch_comment_followups',
            # Every 6h; a per-user 12h Redis interval gates it to ~twice a day. React + answer
            # question-replies on our automated comments (issue #478).
            'schedule': crontab(minute='0', hour='*/6')
        },
        'generate-newsletter-drafts': {
            'task': 'cqc_lem.app.run_scheduler.auto_generate_newsletter_drafts',
            'schedule': crontab(hour='10', minute='0')  # Daily: top up each user's newsletter draft queue for review
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
            'schedule': crontab(hour='16', minute='0')  # Daily value-add commenting in enabled groups
        },
        'group-posts': {
            'task': 'cqc_lem.app.run_scheduler.auto_group_posts',
            'schedule': crontab(hour='15', minute='0', day_of_week='tuesday')  # Weekly value-add group post
        },
        'scrape-post-stats': {
            'task': 'cqc_lem.app.run_scheduler.auto_scrape_stats',
            'schedule': crontab(hour='23', minute='0')  # Nightly: capture post engagement for time recs
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
            'schedule': crontab(hour='5', minute='0', day_of_month='1')  # Run on the 1st of the month at 5:00 AM
        },
        'notify-missing-linkedin-session': {
            'task': 'cqc_lem.app.run_scheduler.auto_notify_missing_linkedin_session',
            'schedule': crontab(hour='9', minute='0')  # Daily 9:00 AM — throttled per-user to 1/week
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
            'schedule': crontab(hour='8', minute='0')  # Run every day at 8:00 AM
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
        'daily-cost-alerts': {
            'task': 'cqc_lem.app.run_scheduler.auto_daily_cost_alerts',
            # Daily 13:00 UTC — scores YESTERDAY (the last complete UTC day), after the 6:00 Stripe
            # sync so tier MRR is current, and after the weekly margin report so they never overlap.
            'schedule': crontab(hour='13', minute='0')
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


@worker_process_init.connect(weak=False)
def init_celery_tracing(*args, **kwargs):
    if CODE_TRACING:

        try:
            tracer = get_jaeger_tracer("celery_worker", __name__)

            # Instrument Celery
            from opentelemetry.instrumentation.celery import CeleryInstrumentor
            ci = CeleryInstrumentor()
            ci.instrument()

            with tracer.start_as_current_span("init_celery_tracing"):
                myprint("Instrumented Celery for tracing")
        except ImportError:
            logger.debug("Celery tracing dependencies not found. Tracing Disabled")
    else:
        myprint("Tracing is disabled")


def get_queue_metric(name_space: str = 'cqc-lem/celery_queue/celery', metric_name: str = 'QueueLength',
                     period: int = 60, time_delta_minutes: int = 1, statistics: str = "Maximum") -> int:

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
    # Celery forks worker processes; the PostHog background Consumer thread does not
    # survive fork. Sync mode sends each capture immediately instead of queuing.
    import posthog as _posthog
    _posthog.sync_mode = True


@task_prerun.connect(weak=False)
def on_task_prerun(task_id: str = None, task=None, **kwargs) -> None:
    _task_start_times[task_id] = _time.time()


@task_postrun.connect(weak=False)
def on_task_postrun(task_id: str = None, task=None, state: str = None, **kwargs) -> None:
    start = _task_start_times.pop(task_id, _time.time())
    track_task(
        task_name=task.name,
        duration_ms=int((_time.time() - start) * 1000),
        success=(state == "SUCCESS"),
        state=state or "UNKNOWN",
    )


@task_sent.connect
@task_received.connect
@task_success.connect
def update_queue_length_metric(sender=None, headers=None, **kwargs) -> int:
    """
    Get the current queue length from Redis broker and push to CloudWatch
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