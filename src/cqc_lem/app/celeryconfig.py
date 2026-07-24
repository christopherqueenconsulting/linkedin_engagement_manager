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

# Prevent task messages from being lost
task_acks_late = True,

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

# Gets the max between all the parameters of timeout in the tasks
max_timeout = 60 * 60  # This value must be bigger than the maximum soft timeout set for a task to prevent an infinity loop
broker_transport_options = {'visibility_timeout': max_timeout + 60,# 60 seconds of margin
                            'max_retries':5}


def get_aws_sqs(queue_name: str,
                # region_name: str,
                session: boto3.session.Session) -> dict:
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
# Selenium lanes — every task that opens a Chrome session routes to one of three
#                  reserved lanes so a long-running loop can't starve the others.
#   'se_engage'   (2 sessions) — long commenting/reply loops.
#   'se_outreach' (1 session)  — DMs, invites, profile-viewer engagement, followups.
#   'se_content'  (1 session)  — seed comments, stats scrape, group sync/post,
#                                newsletter publishing.
task_queues = (
    Queue('celery'),
    Queue('se_engage'),
    Queue('se_outreach'),
    Queue('se_content'),
)
task_default_queue = 'celery'

# Explicit routing for every Selenium-backed task.  The queue= parameter on each
# task decorator already handles routing at dispatch time; this mapping is a
# belt-and-suspenders safety net for any send() call that doesn't pass queue=.
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
    'cqc_lem.app.run_automation.auto_sync_user_groups': {'queue': 'se_content'},
    'cqc_lem.app.run_automation.auto_post_to_group': {'queue': 'se_content'},
    'cqc_lem.app.run_automation.auto_publish_newsletter_edition': {'queue': 'se_content'},
    'cqc_lem.app.run_automation.auto_publish_edition': {'queue': 'se_content'},
}


# Addition setting for AWS SQS Usage

# !!! NOTE !!! - The setup function below works but flower won't work with SQS and there are still some other bugs with using SQS Queue
def setup_aws_sqs_config():
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
