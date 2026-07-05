"""Smoke tests: every Selenium-backed task must declare its reserved lane queue.

Selenium work is split across three reserved lanes so a long commenting loop
can't starve DMs/invites/content tasks:

  se_engage   — long commenting/reply loops
  se_outreach — DMs, invites, profile-viewer engagement, followups
  se_content  — seed comments, stats scrape, group sync/post, newsletter publish

If a task is dispatched without an explicit queue and task_routes hasn't been
applied (e.g. unit tests calling .apply_async directly), Celery falls back to
the queue embedded in the task object.  These tests confirm the queue attribute
is set so the task always lands in the right lane regardless of call-site.
"""

import pytest

pytestmark = pytest.mark.unit

# task name -> reserved Selenium lane queue
SELENIUM_TASK_LANES = {
    # se_engage
    "automate_commenting": "se_engage",
    "automate_reply_commenting": "se_engage",
    "comment_on_post": "se_engage",
    "auto_comment_in_groups": "se_engage",
    # se_outreach
    "automate_appreciation_dms_for_user": "se_outreach",
    "send_private_dm": "se_outreach",
    "automate_profile_viewer_engagement": "se_outreach",
    "engage_with_profile_viewer": "se_outreach",
    "invite_to_connect": "se_outreach",
    "process_user_followups": "se_outreach",
    "automate_invites_to_company_page_for_user": "se_outreach",
    "clean_stale_invites": "se_outreach",
    "update_stale_profile": "se_outreach",
    # se_content
    "auto_seed_comment_on_post": "se_content",
    "auto_scrape_post_stats": "se_content",
    "auto_sync_user_groups": "se_content",
    "auto_post_to_group": "se_content",
    "auto_publish_newsletter_edition": "se_content",
}

SELENIUM_LANES = {"se_engage", "se_outreach", "se_content"}

# REST task that must stay on the default 'celery' queue.
NON_SELENIUM_TASKS = [
    "post_to_linkedin",
]


@pytest.mark.parametrize("task_name,lane", sorted(SELENIUM_TASK_LANES.items()))
def test_selenium_task_routes_to_its_lane(task_name, lane):
    """Each Selenium task must have its reserved lane queue on the task object."""
    import importlib
    mod = importlib.import_module("cqc_lem.app.run_automation")
    task = getattr(mod, task_name)
    assert task.queue == lane, (
        f"{task_name}.queue is '{task.queue}', expected '{lane}'. "
        f"Set queue='{lane}' on its @shared_task.task() decorator."
    )


@pytest.mark.parametrize("task_name", NON_SELENIUM_TASKS)
def test_non_selenium_task_stays_on_default_queue(task_name):
    """Non-Selenium tasks must NOT be routed to a Selenium lane."""
    import importlib
    mod = importlib.import_module("cqc_lem.app.run_automation")
    task = getattr(mod, task_name)
    assert getattr(task, "queue", "celery") not in SELENIUM_LANES, (
        f"{task_name} should stay on the default queue, not a Selenium lane."
    )


def test_celeryconfig_declares_lane_queues():
    """celeryconfig.py must declare the default + three lane queues."""
    from cqc_lem.app import celeryconfig
    queue_names = {q.name for q in celeryconfig.task_queues}
    assert "celery" in queue_names, "task_queues must include the default 'celery' queue"
    for lane in SELENIUM_LANES:
        assert lane in queue_names, f"task_queues must include a Queue named '{lane}'"


def test_celeryconfig_task_routes_cover_all_selenium_tasks():
    """task_routes must map every known Selenium task to its lane."""
    from cqc_lem.app import celeryconfig
    routes = celeryconfig.task_routes
    for task_name, lane in SELENIUM_TASK_LANES.items():
        full_name = f"cqc_lem.app.run_automation.{task_name}"
        assert full_name in routes, f"{full_name} missing from task_routes"
        assert routes[full_name].get("queue") == lane, (
            f"{full_name} task_routes entry must map to queue='{lane}'"
        )
