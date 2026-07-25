"""Unit tests for deploy maintenance mode (utilities/maintenance.py, issue #549)."""

import json

import pytest
from unittest.mock import MagicMock, patch

pytestmark = pytest.mark.unit

_MOD = "cqc_lem.utilities.maintenance"


@pytest.fixture
def fake_redis():
    client = MagicMock()
    with patch(f"{_MOD}._redis_client", return_value=client):
        yield client


@pytest.fixture
def no_redis():
    with patch(f"{_MOD}._redis_client", return_value=None):
        yield


@pytest.fixture
def fake_control():
    control = MagicMock()
    with patch(f"{_MOD}._control", return_value=control):
        yield control


def _inspect_of(control):
    return control.inspect.return_value


class TestQueueNames:
    def test_reads_queues_from_celeryconfig(self):
        from cqc_lem.utilities.maintenance import queue_names
        names = queue_names()
        assert "celery" in names
        assert {"se_engage", "se_outreach", "se_content"} <= set(names)

    def test_falls_back_when_config_unavailable(self):
        from cqc_lem.utilities import maintenance
        with patch.dict("sys.modules", {"cqc_lem.app.celeryconfig": None}):
            assert maintenance.queue_names() == maintenance.FALLBACK_QUEUES


class TestMaintenanceFlag:
    def test_remaining_reads_ttl(self, fake_redis):
        fake_redis.ttl.return_value = 120
        from cqc_lem.utilities.maintenance import is_maintenance_mode, maintenance_remaining
        assert maintenance_remaining() == 120
        assert is_maintenance_mode() is True

    def test_not_set_is_zero(self, fake_redis):
        fake_redis.ttl.return_value = -2
        from cqc_lem.utilities.maintenance import is_maintenance_mode, maintenance_remaining
        assert maintenance_remaining() == 0
        assert is_maintenance_mode() is False

    def test_fails_open_without_redis(self, no_redis):
        from cqc_lem.utilities.maintenance import maintenance_remaining
        assert maintenance_remaining() == 0

    def test_redis_error_is_swallowed(self, fake_redis):
        fake_redis.ttl.side_effect = RuntimeError("boom")
        from cqc_lem.utilities.maintenance import maintenance_remaining
        assert maintenance_remaining() == 0


class TestInspection:
    def test_worker_queue_map(self, fake_control):
        _inspect_of(fake_control).active_queues.return_value = {
            "main-worker@a": [{"name": "celery"}],
            "selenium-se_engage-worker@b": [{"name": "se_engage"}, {"no_name": 1}],
        }
        from cqc_lem.utilities.maintenance import worker_queue_map
        assert worker_queue_map() == {
            "main-worker@a": ["celery"],
            "selenium-se_engage-worker@b": ["se_engage"],
        }

    def test_worker_queue_map_on_broker_error(self, fake_control):
        _inspect_of(fake_control).active_queues.side_effect = RuntimeError("no broker")
        from cqc_lem.utilities.maintenance import worker_queue_map
        assert worker_queue_map() == {}

    def test_active_task_count_sums_workers(self, fake_control):
        _inspect_of(fake_control).active.return_value = {"a": [{}, {}], "b": [{}], "c": None}
        from cqc_lem.utilities.maintenance import active_task_count
        assert active_task_count() == 3

    def test_active_task_count_no_replies_is_zero(self, fake_control):
        _inspect_of(fake_control).active.return_value = None
        from cqc_lem.utilities.maintenance import active_task_count
        assert active_task_count() == 0

    def test_active_task_count_on_broker_error(self, fake_control):
        _inspect_of(fake_control).active.side_effect = RuntimeError("no broker")
        from cqc_lem.utilities.maintenance import active_task_count
        assert active_task_count() == 0


class TestBeginMaintenance:
    def test_pauses_cancels_and_snapshots(self, fake_redis, fake_control):
        _inspect_of(fake_control).active_queues.return_value = {
            "main-worker@a": [{"name": "celery"}],
            "selenium-se_engage-worker@b": [{"name": "se_engage"}],
        }
        from cqc_lem.utilities import maintenance
        with patch(f"{_MOD}.pause_automation", return_value=True) as pause:
            assert maintenance.begin_maintenance(pause_seconds=600) is True

        pause.assert_called_once_with(600, maintenance.MAINTENANCE_REASON)
        fake_redis.set.assert_any_call("lem:maintenance", "deploy", ex=600)
        # Consumers are cancelled per (worker, queue) — never broadcast, or the main worker
        # would come back consuming a Selenium lane.
        cancelled = {(c.args[0], tuple(c.kwargs["destination"]))
                     for c in fake_control.cancel_consumer.call_args_list}
        assert cancelled == {("celery", ("main-worker@a",)),
                             ("se_engage", ("selenium-se_engage-worker@b",))}
        snapshot = json.loads(
            [c for c in fake_redis.set.call_args_list if c.args[0] == "lem:maintenance:consumers"][0].args[1])
        assert snapshot == {"main-worker@a": ["celery"],
                            "selenium-se_engage-worker@b": ["se_engage"]}

    def test_filters_to_requested_queues(self, fake_redis, fake_control):
        _inspect_of(fake_control).active_queues.return_value = {
            "main-worker@a": [{"name": "celery"}, {"name": "se_engage"}],
        }
        from cqc_lem.utilities.maintenance import begin_maintenance
        with patch(f"{_MOD}.pause_automation", return_value=True):
            begin_maintenance(pause_seconds=60, queues=["se_engage"])
        fake_control.cancel_consumer.assert_called_once_with(
            "se_engage", destination=["main-worker@a"])

    def test_cancel_failure_does_not_abort(self, fake_redis, fake_control):
        _inspect_of(fake_control).active_queues.return_value = {"w": [{"name": "celery"}]}
        fake_control.cancel_consumer.side_effect = RuntimeError("broker down")
        from cqc_lem.utilities.maintenance import begin_maintenance
        with patch(f"{_MOD}.pause_automation", return_value=True):
            assert begin_maintenance(pause_seconds=60) is True

    def test_fails_open_without_redis(self, no_redis, fake_control):
        _inspect_of(fake_control).active_queues.return_value = {}
        from cqc_lem.utilities.maintenance import begin_maintenance
        with patch(f"{_MOD}.pause_automation", return_value=False):
            assert begin_maintenance(pause_seconds=60) is False

    def test_redis_set_error_reports_not_stored(self, fake_redis, fake_control):
        fake_redis.set.side_effect = RuntimeError("boom")
        _inspect_of(fake_control).active_queues.return_value = {}
        from cqc_lem.utilities.maintenance import begin_maintenance
        with patch(f"{_MOD}.pause_automation", return_value=True):
            assert begin_maintenance(pause_seconds=60) is False


class TestDrainWorkers:
    def test_returns_zero_once_drained(self):
        from cqc_lem.utilities.maintenance import drain_workers
        with patch(f"{_MOD}.active_task_count", side_effect=[2, 1, 0]):
            assert drain_workers(timeout_seconds=60, poll_seconds=1, sleep=lambda s: None) == 0

    def test_returns_remaining_on_timeout(self):
        from cqc_lem.utilities.maintenance import drain_workers
        with patch(f"{_MOD}.active_task_count", return_value=3):
            assert drain_workers(timeout_seconds=0, poll_seconds=1, sleep=lambda s: None) == 3

    def test_no_wait_when_already_idle(self):
        sleeps = []
        from cqc_lem.utilities.maintenance import drain_workers
        with patch(f"{_MOD}.active_task_count", return_value=0):
            assert drain_workers(timeout_seconds=60, poll_seconds=1, sleep=sleeps.append) == 0
        assert sleeps == []


class TestEndMaintenance:
    def test_restores_consumers_and_resumes(self, fake_redis, fake_control):
        fake_redis.get.return_value = json.dumps({"main-worker@a": ["celery"]}).encode()
        from cqc_lem.utilities.maintenance import end_maintenance
        with patch(f"{_MOD}.automation_pause_reason", return_value="deploy"), \
                patch(f"{_MOD}.resume_automation", return_value=True) as resume:
            assert end_maintenance() is True
        fake_control.add_consumer.assert_called_once_with("celery", destination=["main-worker@a"])
        resume.assert_called_once()
        fake_redis.delete.assert_called_once_with("lem:maintenance", "lem:maintenance:consumers")

    def test_leaves_a_foreign_pause_alone(self, fake_redis, fake_control):
        """A 429 breaker pause must survive a deploy — only our own pause is lifted."""
        fake_redis.get.return_value = None
        from cqc_lem.utilities.maintenance import end_maintenance
        with patch(f"{_MOD}.automation_pause_reason", return_value="429"), \
                patch(f"{_MOD}.automation_pause_remaining", return_value=900), \
                patch(f"{_MOD}.resume_automation") as resume:
            assert end_maintenance() is False
        resume.assert_not_called()

    def test_resumes_when_nothing_is_paused(self, fake_redis, fake_control):
        fake_redis.get.return_value = None
        from cqc_lem.utilities.maintenance import end_maintenance
        with patch(f"{_MOD}.automation_pause_reason", return_value=None), \
                patch(f"{_MOD}.resume_automation", return_value=True) as resume:
            assert end_maintenance() is True
        resume.assert_called_once()

    def test_add_consumer_failure_is_swallowed(self, fake_redis, fake_control):
        fake_redis.get.return_value = json.dumps({"w": ["celery"]}).encode()
        fake_control.add_consumer.side_effect = RuntimeError("gone")
        from cqc_lem.utilities.maintenance import end_maintenance
        with patch(f"{_MOD}.automation_pause_reason", return_value="deploy"), \
                patch(f"{_MOD}.resume_automation", return_value=True):
            assert end_maintenance() is True

    def test_only_restores_requested_queues(self, fake_redis, fake_control):
        fake_redis.get.return_value = json.dumps({"w": ["celery", "se_engage"]}).encode()
        from cqc_lem.utilities.maintenance import end_maintenance
        with patch(f"{_MOD}.automation_pause_reason", return_value="deploy"), \
                patch(f"{_MOD}.resume_automation", return_value=True):
            end_maintenance(queues=["se_engage"])
        fake_control.add_consumer.assert_called_once_with("se_engage", destination=["w"])

    def test_corrupt_snapshot_is_ignored(self, fake_redis, fake_control):
        fake_redis.get.return_value = b"not-json"
        from cqc_lem.utilities.maintenance import end_maintenance
        with patch(f"{_MOD}.automation_pause_reason", return_value="deploy"), \
                patch(f"{_MOD}.resume_automation", return_value=True):
            assert end_maintenance() is True
        fake_control.add_consumer.assert_not_called()

    def test_works_without_redis(self, no_redis, fake_control):
        from cqc_lem.utilities.maintenance import end_maintenance
        with patch(f"{_MOD}.automation_pause_reason", return_value=None), \
                patch(f"{_MOD}.resume_automation", return_value=False):
            assert end_maintenance() is True
        fake_control.add_consumer.assert_not_called()


class TestStatus:
    def test_reports_state(self, fake_redis, fake_control):
        fake_redis.ttl.return_value = 60
        _inspect_of(fake_control).active.return_value = {"w": [{}]}
        _inspect_of(fake_control).active_queues.return_value = {"w": [{"name": "celery"}]}
        from cqc_lem.utilities.maintenance import status
        with patch(f"{_MOD}.automation_pause_reason", return_value="deploy"), \
                patch(f"{_MOD}.automation_pause_remaining", return_value=60):
            state = status()
        assert state["maintenance"] is True
        assert state["active_tasks"] == 1
        assert state["worker_queues"] == {"w": ["celery"]}


class TestCli:
    def test_begin_subcommand(self):
        from cqc_lem.utilities.maintenance import main
        with patch(f"{_MOD}.begin_maintenance", return_value=True) as begin:
            assert main(["begin", "--pause-seconds", "300"]) == 0
        begin.assert_called_once_with(pause_seconds=300)

    def test_drain_subcommand_exit_codes(self):
        from cqc_lem.utilities.maintenance import main
        with patch(f"{_MOD}.drain_workers", return_value=0):
            assert main(["drain", "--timeout", "5", "--poll", "1"]) == 0
        with patch(f"{_MOD}.drain_workers", return_value=2):
            assert main(["drain"]) == 1

    def test_end_subcommand(self):
        from cqc_lem.utilities.maintenance import main
        with patch(f"{_MOD}.end_maintenance", return_value=True) as end:
            assert main(["end"]) == 0
        end.assert_called_once()

    def test_status_subcommand_prints_json(self, capsys):
        from cqc_lem.utilities.maintenance import main
        with patch(f"{_MOD}.status", return_value={"maintenance": False}):
            assert main(["status"]) == 0
        assert json.loads(capsys.readouterr().out) == {"maintenance": False}
