import os
import shutil
from datetime import timedelta, datetime, timezone

from celery_once import QueueOnce

from cqc_lem import assets_dir
from cqc_lem.app.my_celery import app as shared_task
from cqc_lem.app.run_automation import automate_commenting, automate_profile_viewer_engagement, \
    automate_appreciation_dms_for_user, clean_stale_invites, update_stale_profile, post_to_linkedin, \
    automate_invites_to_company_page_for_user
from cqc_lem.utilities.db import (
    get_ready_to_post_posts, get_orphaned_scheduled_posts, update_db_post_status,
    get_active_user_ids, PostStatus, has_linkedin_session, has_scheduled_post_today,
    get_company_linked_in_url_for_user,
    get_users_with_stripe_subscriptions, update_subscription_from_stripe,
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
    """Standalone feed-commenting run for reciprocity on days with NO scheduled post. Days that
    do have a post are already covered by the pre-post commenting trigger (auto_check_scheduled_posts),
    so we skip them to avoid double-commenting. The per-day comment cap still applies inside
    comment_on_feed_inline."""
    users = get_active_user_ids()
    dispatched = 0
    for user_id in users:
        if has_scheduled_post_today(user_id):
            continue  # pre-post commenting already engages the feed today
        if not has_linkedin_session(user_id):
            continue  # no session → the Selenium task would just fail and waste a Chrome slot
        automate_commenting.apply_async(kwargs={'user_id': user_id, 'loop_for_duration': 60 * 15})
        dispatched += 1
    return f"Daily engagement dispatched for {dispatched}/{len(users)} active user(s)"


@shared_task.task
def auto_send_due_followups():
    """Dispatch a per-user Selenium task to send due DM follow-ups (each gated by reply-detection)."""
    from cqc_lem.app.run_automation import process_user_followups
    from cqc_lem.utilities.db import get_due_followups
    due = get_due_followups(datetime.now())
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
