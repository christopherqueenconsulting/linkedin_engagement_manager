"""Unit tests for the QueueOnce subclass that fills a task's own parameter defaults into the
celery-once dedup key (issue #989 — `KeyError: 'sweep_slot'` raised inside apply_async, 500'ing the
inbound reply-notification webhook before the message was ever sent).
"""

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]
SRC = REPO_ROOT / "src" / "cqc_lem"
SHIM = SRC / "app" / "queue_once.py"


class TestRestrictedKeyDefaults:
    def test_omitted_defaulted_key_does_not_raise(self):
        from cqc_lem.app.engagement.posting import sweep_reply_comments
        # This is the exact enqueue shape that raised KeyError: 'sweep_slot'.
        assert sweep_reply_comments.get_key(kwargs={"user_id": 7})

    def test_omitted_default_and_explicit_default_are_the_same_lock(self):
        from cqc_lem.app.engagement.posting import sweep_reply_comments
        assert (sweep_reply_comments.get_key(kwargs={"user_id": 7})
                == sweep_reply_comments.get_key(kwargs={"user_id": 7, "sweep_slot": 0}))

    def test_distinct_slots_stay_distinct_locks(self):
        """The whole point of keying on sweep_slot: concurrent golden-hour sweeps must not collapse
        into one lock (the golden-hour dispatcher sends a distinct slot per sweep).
        """
        from cqc_lem.app.engagement.posting import sweep_reply_comments
        assert (sweep_reply_comments.get_key(kwargs={"user_id": 7, "sweep_slot": 1})
                != sweep_reply_comments.get_key(kwargs={"user_id": 7, "sweep_slot": 2}))

    def test_users_stay_distinct_locks(self):
        from cqc_lem.app.engagement.posting import sweep_reply_comments
        assert (sweep_reply_comments.get_key(kwargs={"user_id": 7})
                != sweep_reply_comments.get_key(kwargs={"user_id": 8}))


class TestUnrestrictedKeysUnchanged:
    def test_task_without_restrict_keys_keeps_upstream_key_shape(self):
        """Only keys named in once['keys'] are filled — a task with no `keys` must build the same
        key celery-once always built, so no live lock changes identity on deploy.
        """
        from cqc_lem.app.my_celery import app as shared_task
        from cqc_lem.app.queue_once import QueueOnce

        @shared_task.task(bind=True, base=QueueOnce, once={"graceful": True},
                          name="tests.queue_once.unrestricted")
        def unrestricted(self, user_id: int, limit: int = 10) -> None:
            return None

        key = unrestricted.get_key(kwargs={"user_id": 3})
        assert "user_id-3" in key
        assert "limit" not in key

    def test_restricted_key_without_a_default_is_left_alone(self):
        """A required key can't be missing (Signature.bind rejects it first), and a key we have no
        default for must never be invented.
        """
        from cqc_lem.app.my_celery import app as shared_task
        from cqc_lem.app.queue_once import QueueOnce

        @shared_task.task(bind=True, base=QueueOnce, once={"graceful": True, "keys": ["user_id"]},
                          name="tests.queue_once.required_key")
        def required_key(self, user_id: int, note: str = "x") -> None:
            return None

        key = required_key.get_key(kwargs={"user_id": 4})
        assert "user_id-4" in key
        assert "note" not in key


class TestEveryTaskUsesTheShim:
    """The fix only holds while every task base is OUR QueueOnce — a task that imports the upstream
    class straight from `celery_once` silently gets the KeyError back, and nothing at runtime says
    so. Source-text guard (same shape as test_selenium_queue_routing) because the failure is a
    missing import, not a behaviour a passing task can demonstrate.
    """

    def _sources(self) -> list[Path]:
        return [p for p in SRC.rglob("*.py") if p != SHIM]

    def test_no_module_imports_queueonce_from_celery_once(self):
        offenders = [
            str(path.relative_to(REPO_ROOT))
            for path in self._sources()
            if re.search(r"^\s*from\s+celery_once\s+import\s+.*\bQueueOnce\b", path.read_text(), re.M)
            or re.search(r"\bcelery_once\.QueueOnce\b", path.read_text())
        ]
        assert offenders == [], f"import QueueOnce from cqc_lem.app.queue_once instead: {offenders}"

    def test_every_module_declaring_a_queueonce_task_imports_the_shim(self):
        missing = [
            str(path.relative_to(REPO_ROOT))
            for path in self._sources()
            if "base=QueueOnce" in path.read_text()
            and "from cqc_lem.app.queue_once import QueueOnce" not in path.read_text()
        ]
        assert missing == [], f"modules using base=QueueOnce without the shim import: {missing}"
