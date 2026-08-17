"""The ONE Celery settings object — `my_celery.py` loads it with `config_from_object(celeryconfig)`.

Celery reads the MODULE NAMESPACE, so a module-level name here IS a setting and anything that wants
to change one has to rebind a module attribute (see `setup_aws_sqs_config`, which does not).

The queue block below is the other half of the fixed Chrome session pool: each `se_*` lane's
`SELENIUM_CONCURRENCY` in docker-compose must sum to `SE_NODE_MAX_SESSIONS`, and
`tests/unit/app/test_selenium_capacity.py` fails the build when they drift apart. `task_routes` is
only the safety net — a `queue=` passed to `apply_async` wins over it, which is how the same task
runs on two different lanes.
"""

import os

import boto3
from kombu import Queue
from kombu.utils.url import safequote

# Redis port
REDIS_PORT = os.getenv('REDIS_PORT', '6379')

# Broker settings.
broker_url = os.getenv('CELERY_BROKER_URL', f'redis://redis:{REDIS_PORT}/0')
broker_connection_retry_on_startup = True
broker_connection_max_retries = None
broker_connection_timeout = 30
redis_socket_connect_timeout = 30
redis_socket_timeout = 30

# AWS deployment decision: use SQS as the broker and ElastiCache Redis as the result backend.
# Set CELERY_BROKER_URL=sqs:// and CELERY_RESULT_BACKEND=redis://<elasticache-host>:6379/1 in AWS secrets.
# celery-once still requires CELERY_BROKER_URL to point at Redis for lock tracking (see my_celery.py).
result_backend = os.getenv('CELERY_RESULT_BACKEND', f'redis://redis:{REDIS_PORT}/1')

# The Redis backend visibility timout
result_backend_transport_options = {'visibility_timeout': (
        60 * 60 * 3)}  # 3 hours (This is important. If scheduled items arent handled they will duplicate by being sent back to the queue)

# Backend will try to retry on the event of recoverable exceptions instead of propagating the exception. It will use an exponential backoff sleep time between 2 retries.
result_backend_always_retry = True

# This is the maximum of retries in case of recoverable exceptions.
result_backend_max_retries = 3

# The Redis backend health checks every 5 minutes (300 seconds)
redis_backend_health_check_interval = 300

# Beat schedules (my_celery.py crontabs) are authored in UTC — keep the scheduler on UTC and do
# NOT inherit the container's TZ (which sets local wall-clock / Python datetime.now()), or hour-based
# crontabs fire at the wrong instant. Post publishing uses aware-UTC ETAs and is unaffected either way.
timezone = os.getenv('CELERY_TIMEZONE') or 'UTC'
enable_utc = True

# Prevent task messages from being lost. acks_late defers the broker ACK until AFTER the task
# returns, so a worker killed mid-task (deploy recreate, OOM, SIGKILL) leaves the message
# un-acked and the broker re-delivers it instead of dropping it. Was `True,` — a one-tuple; it
# happened to be truthy, but a stray comma is not a config value.
task_acks_late = True

# Re-queue a task whose worker process died mid-execution (the pool child was killed) rather
# than marking it failed and losing the work. Several tasks already set this per-decorator;
# making it global means every task — including new ones — survives a deploy the same way.
#
# Safe because the re-runnable tasks are idempotent against a SECOND delivery: posting
# short-circuits on PostStatus.POSTED, comments/DMs dedup on the commented_posts / dm ledgers,
# group posts and newsletter editions gate on a durable DB status, and appreciation claims every
# recipient in appreciation_touches BEFORE dispatch.
#
# What does NOT belong in that list is celery-once, which this comment used to cite. QueueOnce
# takes its lock in `apply_async` — the PRODUCER side (celery_once/tasks.py:100-107) — and
# `Task.__call__` only ever CLEARS a lock, never checks one. It stops two dispatchers racing; it
# is not a redelivery guard, which is the only failure mode this setting creates. The remaining
# gap is `send_private_dm`, whose log row is written in a `finally` AFTER the send, so a kill in
# that window can cost one duplicate DM. The deploy drain window (stop_grace_period 8m) is the
# mitigation; a durable pre-send claim is the real fix and is tracked separately.
task_reject_on_worker_lost = True

# Celery 5 deprecation: leaving this unset warns on every boot. False keeps a task RUNNING
# through a transient broker disconnect (it re-acks on reconnect) instead of cancelling it —
# which is the whole point of not losing in-flight work.
worker_cancel_long_running_tasks_on_connection_loss = False

# Set the maximum time in seconds that the ETA scheduler can sleep between rechecking the schedule.
# Default: 1.0 seconds.
worker_timer_precision = 10

# The number of concurrent worker processes/threads/green threads executing tasks.
# Default: Number of CPU cores.
worker_concurrency = 1
# Turned off for dynamic worker scaling

# How many messages to prefetch at a time multiplied by the number of concurrent processes.
# The default is 4 (four messages for each process)
worker_prefetch_multiplier = 1

# The maximum number of tasks a worker can execute before it’s replaced by a new process.
worker_max_tasks_per_child = 10

# The ceiling a task may actually RUN for. `max_timeout` was never a Celery setting name — it only
# ever fed the visibility_timeout arithmetic below — so until now NOTHING bounded a task, and a call
# that never returned parked a lane's only slot forever: worker_max_tasks_per_child recycles a child
# BETWEEN tasks, so it never fires on the task that is stuck. Per-call `timeout=` (added alongside
# this) is the first line of defence; this is the backstop for every call that still lacks one.
# 60 min is ~4x the longest deliberate loop in the tree (the 15-min golden-hour / pre-post windows,
# run_scheduler dispatch of `loop_for_duration`), so it bounds a hang without truncating real work.
max_timeout = 60 * 60

# The SOFT limit is the one that should ever fire: it raises SoftTimeLimitExceeded INSIDE the task,
# which is what lets the Selenium lanes quit Chrome in the `finally` they already have. The HARD
# limit sits above it and only matters for a task that swallowed the soft one (a broad
# `except Exception` catches it — SoftTimeLimitExceeded derives from Exception, not BaseException).
TASK_KILL_GRACE_SECONDS = 5 * 60
task_soft_time_limit = int(os.getenv('CELERY_TASK_SOFT_TIME_LIMIT', str(max_timeout)))
task_time_limit = int(os.getenv('CELERY_TASK_TIME_LIMIT', str(task_soft_time_limit + TASK_KILL_GRACE_SECONDS)))

# Verified against celery 5.6.3 rather than assumed, because the interaction is a real footgun:
# a hard time limit kills the pool child, and `task_reject_on_worker_lost = True` above REQUEUES a
# lost worker's message. It is safe ONLY because `task_acks_on_failure_or_timeout` defaults to True
# (celery/app/defaults.py:266) and `Request.on_timeout` ACKs on that path rather than rejecting
# (celery/worker/request.py:547-548), and because a TimeLimitExceeded is rejected-with-requeue only
# when that same setting is False (celery/worker/request.py:615-616).
# DO NOT set task_acks_on_failure_or_timeout = False: it would turn every hard timeout into an
# infinite redelivery of the task that timed out.

# Redis visibility timeout — how long the broker waits for an ACK before handing the message to
# ANOTHER worker. With task_acks_late this must stay comfortably ABOVE the longest task plus the
# deploy drain window (stop_grace_period 8m), or a long task still running past the timeout gets
# re-delivered and runs twice concurrently — the classic acks_late + Redis double-run trap.
# 60s of margin was not enough once workers may take a full grace period to shut down.
# Derived from the HARD limit, not from max_timeout: that is now the true longest a task can run,
# so the "comfortably above the longest task" claim above is arithmetic rather than aspiration.
longest_task_seconds = int(os.getenv('CELERY_LONGEST_TASK_SECONDS', str(task_time_limit)))
visibility_timeout = int(os.getenv('CELERY_VISIBILITY_TIMEOUT', str(longest_task_seconds + (15 * 60))))
broker_transport_options = {'visibility_timeout': visibility_timeout,
                            'max_retries': 5}


def get_aws_sqs(queue_name: str,
                # region_name: str,
                session: boto3.session.Session) -> dict:
    """Look a queue's URL up through an already-authenticated boto3 session.

    `service_name='sqs'` is load-bearing — it was once `'elasticcache'`, which asks a different
    service entirely and never resolved a queue.

    Returns:
        The raw `get_queue_url` response; the caller wants `['QueueUrl']` out of it.
    """
    sqs = session.client(
        service_name='sqs',
        # region_name=region_name
    )

    return sqs.get_queue_url(QueueName=queue_name)


task_create_missing_queues = True

# ---------------------------------------------------------------------------
# Queue definitions
# ---------------------------------------------------------------------------
# 'celery'      — default queue consumed by the main worker (scheduler tasks,
#                  API calls, content plan, Stripe sync, post_to_linkedin, etc.).
# Selenium lanes — every task that opens a Chrome session routes to one of four
#                  reserved lanes so a long-running loop can't starve the others.
#   'se_engage'   (3 sessions) — long commenting/reply loops.
#   'se_prepost'  (2 sessions) — ONLY the ETA-based pre-post commenting warm-up
#                                dispatched by auto_check_scheduled_posts (issue #553).
#   'se_outreach' (2 sessions) — DMs, invites, profile-viewer engagement, followups.
#   'se_content'  (1 session)  — seed comments, stats scrape, group sync/post,
#                                newsletter publishing.
# The per-lane session counts live in docker-compose.yml (SELENIUM_CONCURRENCY) and must sum to
# SE_NODE_MAX_SESSIONS — see docs/scaling-plan.md §5a and tests/unit/app/test_selenium_capacity.py.

# The pre-post warm-up is the only deadline-bound commenting run in the stack: its eta is
# post_time − 15 min and a late start means the warm-up lands during/after publication.
# On 'se_engage' it queues behind 15-minute golden-hour loops (issue #547), so it gets its
# own lane. Same task, different queue — the dispatch site passes queue= explicitly.
SE_PREPOST_QUEUE = 'se_prepost'

task_queues = (
    Queue('celery'),
    Queue('se_engage'),
    Queue(SE_PREPOST_QUEUE),
    Queue('se_outreach'),
    Queue('se_content'),
)
task_default_queue = 'celery'

# Explicit routing for every Selenium-backed task.  The queue= parameter on each
# task decorator already handles routing at dispatch time; this mapping is a
# belt-and-suspenders safety net for any send() call that doesn't pass queue=.
# A queue= passed to apply_async still wins over both (that's how the pre-post
# dispatch in run_scheduler.py lands on se_prepost while the golden-hour fan-out
# of the SAME task stays on se_engage).
task_routes = {
    # --- se_engage: long commenting loops --------------------------------
    'cqc_lem.app.run_automation.automate_commenting': {'queue': 'se_engage'},
    'cqc_lem.app.run_automation.automate_reply_commenting': {'queue': 'se_engage'},
    'cqc_lem.app.run_automation.comment_on_post': {'queue': 'se_engage'},
    'cqc_lem.app.run_automation.auto_comment_in_groups': {'queue': 'se_engage'},
    # --- se_outreach: DMs, invites, profile-viewer engagement, followups --
    'cqc_lem.app.run_automation.automate_appreciation_dms_for_user': {'queue': 'se_outreach'},
    'cqc_lem.app.run_automation.send_private_dm': {'queue': 'se_outreach'},
    'cqc_lem.app.run_automation.automate_profile_viewer_engagement': {'queue': 'se_outreach'},
    'cqc_lem.app.run_automation.engage_with_profile_viewer': {'queue': 'se_outreach'},
    # EVERY key here is a WIRE NAME, not a module path — and since #1206 deleted
    # `app/run_automation.py` there is no module by that name at all. The tasks live in
    # `app.engagement.{feed,invites,newsletter,outreach,posting}` and each pins `name=` back to this
    # spelling, so the keys stay correct and must NOT be "corrected" to the module that now defines
    # them: doing so renames every task, drops in-flight messages as `NotRegistered`, and re-keys
    # the `QueueOnce` locks mid-deploy. `tests/unit/app/test_task_name_stability.py` holds both
    # halves — the names still resolve, and no module answers to that path.
    'cqc_lem.app.run_automation.invite_to_connect': {'queue': 'se_outreach'},
    'cqc_lem.app.run_automation.process_user_followups': {'queue': 'se_outreach'},
    'cqc_lem.app.run_automation.automate_invites_to_company_page_for_user': {'queue': 'se_outreach'},
    'cqc_lem.app.run_automation.clean_stale_invites': {'queue': 'se_outreach'},
    'cqc_lem.app.run_automation.update_stale_profile': {'queue': 'se_outreach'},
    'cqc_lem.app.run_automation.send_lead_response': {'queue': 'se_outreach'},
    'cqc_lem.app.run_automation.scan_connection_candidates': {'queue': 'se_outreach'},
    'cqc_lem.app.run_automation.automate_catchup_touches': {'queue': 'se_outreach'},
    'cqc_lem.app.run_automation.send_catchup_touch': {'queue': 'se_outreach'},
    # --- se_content: seeding, stats, group + newsletter publishing --------
    'cqc_lem.app.run_automation.auto_seed_comment_on_post': {'queue': 'se_content'},
    'cqc_lem.app.run_automation.auto_scrape_post_stats': {'queue': 'se_content'},
    'cqc_lem.app.run_automation.capture_follower_stats': {'queue': 'se_content'},
    'cqc_lem.app.run_automation.auto_sync_user_groups': {'queue': 'se_content'},
    'cqc_lem.app.run_automation.auto_post_to_group': {'queue': 'se_content'},
    'cqc_lem.app.run_automation.auto_publish_occasion_post': {'queue': 'se_content'},
    'cqc_lem.app.run_automation.auto_publish_newsletter_edition': {'queue': 'se_content'},
    'cqc_lem.app.run_automation.auto_publish_edition': {'queue': 'se_content'},
    # Marketing tutorial capture drives a browser against OUR OWN SPA (issue #505), not LinkedIn,
    # but it still needs a Selenium session — so it shares the content lane.
    'cqc_lem.app.run_scheduler.auto_produce_feature_tutorial': {'queue': 'se_content'},
}


# Addition setting for AWS SQS Usage

# !!! NOTE !!! - The setup function below works but flower won't work with SQS and there are still some other bugs with using SQS Queue
def setup_aws_sqs_config():
    """Discover the SQS broker settings for the abandoned AWS path (see the NOTE above).

    A no-op unless `AWS_REGION` is set, and every boto3/credential failure is caught and printed —
    this runs inside a config module, so it must never raise.

    Note what it does NOT do: `broker_url`, `broker_transport_options` and
    `task_create_missing_queues` are assigned as function LOCALS, so calling this leaves the
    module-level settings Celery actually reads untouched. Nothing in the stack calls it today.
    """
    sqs_queue_url = os.getenv('AWS_SQS_QUEUE_URL')
    sqs_queue_name = os.getenv('AWS_SQS_QUEUE_NAME')
    aws_region = os.getenv('AWS_REGION')
    sqs_secret_name = os.getenv('AWS_SQS_SECRET_NAME')

    if aws_region:
        try:
            # Get the AWS session
            session = boto3.session.Session(region_name=aws_region)
            aws_access_key = safequote(session.get_credentials().access_key)
            aws_secret_key = safequote(session.get_credentials().secret_key)
            # Never log AWS credentials in clear text — only confirm presence + region.
            print(f"Session credentials loaded: {bool(aws_access_key and aws_secret_key)}")
            print(f"Session Region: {session.region_name}")

            if sqs_queue_name:
                print(f"Queue Name: {sqs_queue_name}")
                sqs_queue_url = get_aws_sqs(queue_name=sqs_queue_name, session=session)['QueueUrl']
                print(f"SQS Queue URL: {sqs_queue_url}")

                # Set broker_url, broker_transport_options and task_create_missing_queues
                broker_url = "elasticcache://"  # Note: Can use this when environment variables AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY are set
                print(f"Broker URL: {broker_url}")

                broker_transport_options = {
                    "region": aws_region,
                    "aws_access_key_id": aws_access_key,
                    "aws_secret_access_key": aws_secret_key,
                    "predefined_queues": {
                        "celery": {
                            "url": sqs_queue_url,
                            "access_key_id": aws_access_key,
                            "secret_access_key": aws_secret_key,
                        }
                    },
                    'visibility_timeout': 3600,  # 1 hour
                    'polling_interval': 15
                    # 15 seconds to sleep between unsuccessful polls. !More frequent polling is also more expensive, so increasing the polling interval can save you money.
                }

                task_create_missing_queues = False

        except Exception as e:
            print(f"Error getting AWS session: {e}")
            session = None
