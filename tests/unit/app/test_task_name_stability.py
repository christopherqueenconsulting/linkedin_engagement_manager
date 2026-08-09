"""A Celery task's NAME is a wire identifier, and moving its module silently changes it (#1154).

Celery derives a task's name from `<module>.<function>`. So the layered split renames every task it
moves, and nothing in the existing build notices:

- `celeryconfig.task_routes` keys are plain strings that simply stop matching. Routing still works
  anyway, because the decorator's own `queue=` wins at dispatch — so the route table quietly becomes
  decoration while looking correct.
- `test_selenium_queue_routing` rebuilds `f"cqc_lem.app.run_automation.{name}"` and asserts the
  string is a dict key. That stays true after a rename: it is checking the config against itself.
- The beat schedule names zero `run_automation` tasks, so `test_my_celery`'s resolution check is
  silent here too.

The first symptom would be in production: messages queued under the old name rejected `NotRegistered`
and dropped, and — because these tasks hold their `QueueOnce` lock across the run — a lock key that
re-keys mid-deploy, letting a duplicate invite through to a real person.

Hence a frozen snapshot. It is deliberately dumb: names, not modules. A task may move anywhere as
long as it keeps announcing itself by the name the rest of the system already knows.
"""

import pytest

pytestmark = pytest.mark.unit


# Every task whose name is spoken by something OUTSIDE its own module: a `task_routes` key, a beat
# entry, or a `.apply_async` from another module. Adding a task here is cheap; the cost of leaving
# one out is a silent rename.
FROZEN_TASK_NAMES = frozenset({
    "cqc_lem.app.run_automation.invite_to_connect",
    "cqc_lem.app.run_automation.send_roster_connect_invite",
    "cqc_lem.app.run_automation.send_connection_request",
    "cqc_lem.app.run_automation.clean_stale_invites",
    "cqc_lem.app.run_automation.automate_invites_to_company_page_for_user",
    "cqc_lem.app.run_automation.auto_publish_newsletter_edition",
    "cqc_lem.app.run_automation.auto_publish_edition",
    "cqc_lem.app.run_automation.track_newsletter_subscribers",
    # feed / groups / roster -> app.engagement.feed (#1154 step 3)
    "cqc_lem.app.run_automation.comment_on_post",
    "cqc_lem.app.run_automation.consolidate_duplicate_comments_for_user",
    "cqc_lem.app.run_automation.auto_seed_comment_on_post",
    "cqc_lem.app.run_automation.auto_second_wave_comment",
    "cqc_lem.app.run_automation.auto_sync_user_groups",
    "cqc_lem.app.run_automation.auto_comment_in_groups",
    "cqc_lem.app.run_automation.auto_draft_group_post",
    "cqc_lem.app.run_automation.auto_post_to_group",
    "cqc_lem.app.run_automation.automate_commenting",
    # posting + the post-publish sweeps -> app.engagement.posting (#1154 step 4)
    "cqc_lem.app.run_automation.post_to_linkedin",
    "cqc_lem.app.run_automation.update_stale_profile",
    "cqc_lem.app.run_automation.auto_scrape_post_stats",
    "cqc_lem.app.run_automation.capture_follower_stats",
    "cqc_lem.app.run_automation.sweep_reply_comments",
    "cqc_lem.app.run_automation.sweep_comment_followups",
    "cqc_lem.app.run_automation.process_comment_followups_for_url",
    "cqc_lem.app.run_automation.reconcile_recent_comment_urns",
    "cqc_lem.app.run_automation.sweep_comment_outcomes",
    "cqc_lem.app.run_automation.automate_reply_commenting",
})


@pytest.fixture(scope="module")
def celery_app():
    import cqc_lem.app  # noqa: F401  — importing the package is what registers the task modules
    from cqc_lem.app.my_celery import app

    app.loader.import_default_modules()
    return app


class TestFrozenTaskNames:
    def test_every_frozen_name_is_still_registered(self, celery_app):
        missing = sorted(FROZEN_TASK_NAMES - set(celery_app.tasks))
        assert missing == [], (
            "these task names are no longer registered — a task moved module without pinning "
            f"name='<old module>.<fn>': {missing}"
        )

    def test_a_registered_task_answers_to_the_name_it_is_registered_under(self, celery_app):
        """The one thing a re-export cannot fake.

        `from ...engagement.invites import invite_to_connect` in `run_automation` makes the SYMBOL
        importable from the old place, so an import-based check passes whether or not the wire name
        survived. `task.name` is what Celery actually puts on the message.
        """
        wrong = {
            name: celery_app.tasks[name].name
            for name in sorted(FROZEN_TASK_NAMES)
            if name in celery_app.tasks and celery_app.tasks[name].name != name
        }
        assert wrong == {}


class TestTheConfigStillNamesRealTasks:
    def test_no_task_routes_key_names_a_task_that_does_not_exist(self, celery_app):
        """The check `test_selenium_queue_routing` cannot make: it compares the config to itself."""
        from cqc_lem.app import celeryconfig

        unresolved = sorted(k for k in celeryconfig.task_routes if k not in celery_app.tasks)
        assert unresolved == [], f"task_routes names unregistered tasks: {unresolved}"

    def test_a_moved_task_still_routes_to_the_queue_its_key_names(self, celery_app):
        """Routing survives a rename by accident (the decorator's `queue=` wins), so assert the two
        agree rather than trusting either alone — a disagreement means the route table has started
        describing something that is not happening.
        """
        from cqc_lem.app import celeryconfig

        for key, route in celeryconfig.task_routes.items():
            task = celery_app.tasks.get(key)
            if task is None:
                continue
            declared = (task.queue or None) if hasattr(task, "queue") else None
            expected = route.get("queue") if isinstance(route, dict) else None
            if declared and expected:
                assert declared == expected, (
                    f"{key}: decorator says queue={declared!r}, task_routes says {expected!r}"
                )
