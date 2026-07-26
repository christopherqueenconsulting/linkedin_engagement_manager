"""Guards for graceful worker shutdown + task durability (issue #549).

These assert the *infrastructure* contract that keeps a deploy from losing in-flight tasks:
Celery must receive Docker's SIGTERM (exec, not a wrapper shell), Docker must wait long enough
for the warm shutdown to finish, and an interrupted task must be re-queued rather than dropped.
Each of these regressed silently before — nothing in the app code would fail without them.
"""

import importlib
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]
WORKER_SCRIPTS = (
    "compose/local/celery/worker/start",
    "compose/local/celery/worker/start-selenium",
    "compose/local/celery/worker/start-solo-pool",
)


def _read(rel_path: str) -> str:
    return (REPO_ROOT / rel_path).read_text()


class TestWorkerStartScripts:
    @pytest.mark.parametrize("script", WORKER_SCRIPTS)
    def test_execs_the_worker_so_sigterm_reaches_celery(self, script: str):
        assert re.search(r"^exec /run-as-celery celery ", _read(script), re.MULTILINE)

    @pytest.mark.parametrize("script", WORKER_SCRIPTS)
    def test_never_launches_celery_under_a_non_exec_su(self, script: str):
        # The original bug: `su … -c "celery … worker"` left the shell as PID 1, so TERM never
        # reached Celery and in-flight tasks were SIGKILLed at the grace period.
        assert not re.search(r"^\s*su\s", _read(script), re.MULTILINE)

    def test_beat_is_exec_ed_too(self):
        assert re.search(r"^exec celery --app cqc_lem\.app\.my_celery beat",
                         _read("compose/local/celery/beat/start"), re.MULTILINE)

    def test_run_as_celery_execs_every_branch(self):
        body = _read("compose/local/celery/run-as-celery")
        # setpriv (preferred), su (fallback) and the already-unprivileged path must all exec.
        assert "exec setpriv" in body
        assert "exec su " in body
        assert "exec env" in body

    def test_setpriv_branch_keeps_the_celery_user_home(self):
        # setpriv (unlike su) does not set HOME — without this boto3 would look for the mounted
        # ~/.aws credentials under /root instead of /home/celeryworker.
        body = _read("compose/local/celery/run-as-celery")
        setpriv_line = body.split("exec setpriv")[1].split("\n\n")[0]
        assert 'HOME="$CELERY_HOME"' in setpriv_line

    def test_run_as_celery_is_shipped_and_executable(self):
        dockerfile = _read("compose/local/Dockerfile")
        assert "COPY ./compose/local/celery/run-as-celery /run-as-celery" in dockerfile
        assert "/run-as-celery" in dockerfile.split("RUN chmod +x")[1]


class TestStopGracePeriod:
    def test_every_worker_service_waits_out_a_long_task(self):
        compose = _read("docker-compose.yml")
        # Docker's 10s default is shorter than a ~6 min video generation.
        services = re.split(r"\n  (?=\w)", compose)
        worker_blocks = [s for s in services if s.startswith("celery_worker")]
        assert len(worker_blocks) == 5  # main + four Selenium lanes (se_prepost added by #553)
        for block in worker_blocks:
            assert "stop_grace_period: ${CELERY_STOP_GRACE_PERIOD:-8m}" in block, block[:60]

    def test_beat_has_its_own_short_grace(self):
        compose = _read("docker-compose.yml")
        beat = compose.split("\n  celery_beat:")[1]
        assert "stop_grace_period: 30s" in beat.split("\n  flower:")[0]


class TestTaskDurability:
    @pytest.fixture(autouse=True)
    def _config(self, monkeypatch):
        monkeypatch.delenv("CELERY_VISIBILITY_TIMEOUT", raising=False)
        monkeypatch.delenv("CELERY_LONGEST_TASK_SECONDS", raising=False)
        from cqc_lem.app import celeryconfig
        yield importlib.reload(celeryconfig)
        importlib.reload(celeryconfig)

    def test_acks_late_is_a_bool_not_a_tuple(self, _config):
        # Was `task_acks_late = True,` — truthy by accident, not a config value.
        assert _config.task_acks_late is True

    def test_worker_lost_tasks_are_requeued(self, _config):
        assert _config.task_reject_on_worker_lost is True

    def test_long_tasks_survive_a_broker_blip(self, _config):
        assert _config.worker_cancel_long_running_tasks_on_connection_loss is False

    def test_visibility_timeout_clears_the_longest_task_plus_drain(self, _config):
        visibility = _config.broker_transport_options["visibility_timeout"]
        # Must exceed the longest task AND the 8m stop_grace_period, or acks_late re-delivers a
        # still-running task to a second worker (the acks_late + Redis double-run trap).
        assert visibility >= _config.max_timeout + (8 * 60)

    def test_visibility_timeout_is_env_tunable(self, monkeypatch):
        monkeypatch.setenv("CELERY_VISIBILITY_TIMEOUT", "9999")
        from cqc_lem.app import celeryconfig
        reloaded = importlib.reload(celeryconfig)
        assert reloaded.broker_transport_options["visibility_timeout"] == 9999

    def test_longest_task_seconds_widens_the_window(self, monkeypatch):
        monkeypatch.setenv("CELERY_LONGEST_TASK_SECONDS", "7200")
        from cqc_lem.app import celeryconfig
        reloaded = importlib.reload(celeryconfig)
        assert reloaded.broker_transport_options["visibility_timeout"] == 7200 + (15 * 60)


class TestDeployMaintenanceWiring:
    """deploy.sh must actually pause → drain → deploy → resume."""

    def test_pauses_and_drains_before_recreating(self):
        deploy = _read("scripts/deploy.sh")
        begin = deploy.index("maint begin")
        drain = deploy.index("maint drain")
        recreate = deploy.index("${COMPOSE} up -d --remove-orphans")
        assert begin < drain < recreate

    def test_stops_beat_before_draining(self):
        deploy = _read("scripts/deploy.sh")
        assert deploy.index("${COMPOSE} stop celery_beat") < deploy.index("maint drain")

    def test_resumes_on_success_and_on_every_abort_path(self):
        # The property under guard: once maintenance began, NO exit path may leave the stack
        # paused. The blue/green flip added abort paths (standby unhealthy, bad nginx conf,
        # edge health fail) — every one of them must still `maint end`, as must success.
        deploy = _read("scripts/deploy.sh")
        assert deploy.count("maint end") >= 2
        began = deploy.index("maint begin")
        for i, line in enumerate(deploy[began:].splitlines()):
            if line.strip().startswith("exit 1"):
                window = "\n".join(deploy[began:].splitlines()[max(0, i - 6):i])
                assert "maint end" in window, f"exit path without maint end near: {line!r}"

    def test_calls_the_real_maintenance_module(self):
        assert "python -m cqc_lem.utilities.maintenance" in _read("scripts/deploy.sh")
