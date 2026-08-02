"""Smoke tests: every Selenium-backed task must declare its reserved lane queue.

Selenium work is split across four reserved lanes so a long commenting loop
can't starve DMs/invites/content tasks — or the deadline-bound pre-post warm-up:

  se_engage   — long commenting/reply loops
  se_prepost  — the eta-bound pre-post commenting warm-up only (issue #553)
  se_outreach — DMs, invites, profile-viewer engagement, followups
  se_content  — seed comments, stats scrape, group sync/post, newsletter publish

If a task is dispatched without an explicit queue and task_routes hasn't been
applied (e.g. unit tests calling .apply_async directly), Celery falls back to
the queue embedded in the task object.  These tests confirm the queue attribute
is set so the task always lands in the right lane regardless of call-site.
"""

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]

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
    "automate_catchup_touches": "se_outreach",
    "send_catchup_touch": "se_outreach",
    "update_stale_profile": "se_outreach",
    # se_content
    "auto_seed_comment_on_post": "se_content",
    "auto_scrape_post_stats": "se_content",
    "capture_follower_stats": "se_content",
    "auto_sync_user_groups": "se_content",
    "auto_post_to_group": "se_content",
    "auto_publish_newsletter_edition": "se_content",
}

SELENIUM_LANES = {"se_engage", "se_prepost", "se_outreach", "se_content"}

# REST tasks that must stay on the default 'celery' queue — they publish through the LinkedIn API
# and never open a browser, so they must not hold a Selenium session slot (issue #622).
NON_SELENIUM_TASKS = [
    "post_to_linkedin",
    "auto_second_wave_comment",
    # Writes the weekly group post off the CACHED profile (issue #932) — one LLM call, no browser,
    # so it must never occupy a Selenium session slot the publish run needs.
    "auto_draft_group_post",
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
    """celeryconfig.py must declare the default + four lane queues."""
    from cqc_lem.app import celeryconfig
    queue_names = {q.name for q in celeryconfig.task_queues}
    assert "celery" in queue_names, "task_queues must include the default 'celery' queue"
    for lane in SELENIUM_LANES:
        assert lane in queue_names, f"task_queues must include a Queue named '{lane}'"


def test_prepost_queue_constant_matches_declared_queue():
    """The name the dispatch site overrides with must be a queue a worker actually consumes —
    a typo here silently parks pre-post warm-ups in a queue nobody drains."""
    from cqc_lem.app import celeryconfig
    assert celeryconfig.SE_PREPOST_QUEUE == "se_prepost"
    assert celeryconfig.SE_PREPOST_QUEUE in {q.name for q in celeryconfig.task_queues}


class TestComposeLaneWiring:
    """A declared queue with no worker consuming it is a black hole — tasks sit there forever.
    These read the compose files so a lane can't be added in code without its worker (#553)."""

    @staticmethod
    def _compose(name: str) -> str:
        return (REPO_ROOT / name).read_text()

    @staticmethod
    def _lane_workers(compose: str) -> dict:
        """service name -> (queues, concurrency) for every Selenium lane worker."""
        workers = {}
        for block in re.split(r"\n  (?=\w)", compose):
            queues = re.search(r"SELENIUM_QUEUES=(\S+)", block)
            if not queues:
                continue
            concurrency = re.search(r"SELENIUM_CONCURRENCY=(\d+)", block)
            workers[block.split(":", 1)[0]] = (queues.group(1), int(concurrency.group(1)))
        return workers

    def test_every_declared_lane_has_a_worker(self):
        from cqc_lem.app import celeryconfig
        consumed = {q for q, _ in self._lane_workers(self._compose("docker-compose.yml")).values()}
        for queue in celeryconfig.task_queues:
            if queue.name == "celery":
                continue  # the main worker consumes the default queue without SELENIUM_QUEUES
            assert queue.name in consumed, (
                f"celeryconfig declares '{queue.name}' but no compose service consumes it"
            )

    def test_prepost_worker_gets_its_own_two_slots(self):
        workers = self._lane_workers(self._compose("docker-compose.yml"))
        assert workers.get("celery_worker_selenium_prepost") == ("se_prepost", 2)

    def test_chrome_session_cap_covers_every_lane(self):
        compose = self._compose("docker-compose.yml")
        requested = sum(c for _, c in self._lane_workers(compose).values())
        cap = int(re.search(r"SE_NODE_MAX_SESSIONS=(\d+)", compose).group(1))
        assert cap >= requested, (
            f"lanes request {requested} concurrent Chrome sessions but SE_NODE_MAX_SESSIONS={cap} — "
            f"a lane would block on session acquisition instead of on its own slot"
        )

    def test_prod_overlay_covers_the_prepost_worker(self):
        # Every app service needs the prod overlay's pinned image tag; a service missing here runs
        # the dev bind-mount config in production (see CLAUDE.md).
        assert "celery_worker_selenium_prepost:" in self._compose("docker-compose.prod.yml")


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
