"""Regression tests for scripts/deploy.sh converge/verify logic (issue #831)."""

import os
import subprocess
import textwrap
from pathlib import Path

import pytest

DEPLOY_SH = Path(__file__).resolve().parents[2] / "scripts" / "deploy.sh"


_FAKE_COMPOSE = textwrap.dedent(
    """\
    #!/usr/bin/env python3
    import os
    import sys

    args = sys.argv[1:]
    state_file = os.environ.get("DEPLOY_FAKE_STATE")
    mode = os.environ.get("DEPLOY_FAKE_MODE", "pass")
    fail_count = int(os.environ.get("DEPLOY_FAKE_UP_FAIL_COUNT", "0"))

    # `config --services` is profile-filtered by compose, so the parked standalone
    # `selenium-chrome` never appears here even when a stopped container of it still exists.
    _EXPECTED = ["web_app", "web_api_blue", "web_api_green", "celery_worker", "celery_beat"]
    _ALL_RUNNING = [svc + " running" for svc in _EXPECTED]

    _ALL_PS_A = {
        "pass": _ALL_RUNNING,
        "created": ["web_app running", "celery_worker created"],
        "exited": ["web_app running", "celery_worker exited"],
        "restarting": ["web_app running", "celery_worker restarting"],
        "missing": _ALL_RUNNING,
        # A container of a profile-disabled service the deploy never touches. `ps` labels by
        # PROJECT, not by profile, so it shows up Exited forever and must NOT fail the deploy.
        "profile_leftover": _ALL_RUNNING + ["selenium-chrome exited"],
    }

    _ALL_PS = {
        "pass": _EXPECTED,
        "created": _EXPECTED,
        "exited": _EXPECTED,
        "restarting": _EXPECTED,
        "missing": ["web_app", "web_api_blue", "web_api_green", "celery_worker"],
        "profile_leftover": _EXPECTED,
    }

    def inc_state() -> int:
        if not state_file:
            return 0
        n = 0
        if os.path.exists(state_file):
            with open(state_file, encoding="utf-8") as f:
                n = int((f.read().strip() or "0"))
        n += 1
        os.makedirs(os.path.dirname(state_file) or ".", exist_ok=True)
        with open(state_file, "w", encoding="utf-8") as f:
            f.write(str(n))
        return n

    if args[:2] == ["config", "--services"]:
        for svc in _EXPECTED + ["flyway"]:
            print(svc)
    elif "ps" in args and "-a" in args:
        for line in _ALL_PS_A.get(mode, _ALL_PS_A["pass"]):
            print(line)
    elif "ps" in args:
        for svc in _ALL_PS.get(mode, _ALL_PS["pass"]):
            print(svc)
    elif args[:3] == ["up", "-d", "--remove-orphans"]:
        n = inc_state()
        if n <= fail_count:
            msg = os.environ.get(
                "DEPLOY_FAKE_UP_ERROR",
                "Error response from daemon: No such container: 5edffe0695",
            )
            print(msg, file=sys.stderr)
            sys.exit(1)
        sys.exit(0)
    sys.exit(0)
    """
)


def _run(tmp_path: Path, env_overrides: dict[str, str], bash_code: str) -> subprocess.CompletedProcess[str]:
    """Source deploy.sh in a subshell, override COMPOSE to a fake, and run bash_code."""
    fake = tmp_path / "docker-compose-fake.py"
    fake.write_text(_FAKE_COMPOSE, encoding="utf-8")
    fake.chmod(0o755)

    # Avoid real sleeps in converge retry path.
    sleep_bin = tmp_path / "sleep"
    sleep_bin.write_text("#!/bin/bash\nexit 0\n", encoding="utf-8")
    sleep_bin.chmod(0o755)

    env = os.environ.copy()
    env.update(env_overrides)
    env["PATH"] = f"{tmp_path}{os.pathsep}{env.get('PATH', '')}"

    cmd = f'source "{DEPLOY_SH}"; COMPOSE="{fake}"; {bash_code}'
    return subprocess.run(
        ["bash", "-c", cmd],
        capture_output=True,
        text=True,
        cwd=str(DEPLOY_SH.parent.parent),
        env=env,
    )


class TestConvergeStack:
    def test_retries_once_on_no_such_container(self, tmp_path: Path) -> None:
        state = tmp_path / "state"
        result = _run(
            tmp_path,
            {
                "DEPLOY_FAKE_UP_FAIL_COUNT": "1",
                "DEPLOY_FAKE_STATE": str(state),
            },
            "converge_stack",
        )
        assert result.returncode == 0, result.stderr + result.stdout
        assert state.read_text(encoding="utf-8") == "2"
        assert "retrying converge" in (result.stdout + result.stderr).lower()

    def test_fails_without_retry_on_other_error(self, tmp_path: Path) -> None:
        state = tmp_path / "state"
        result = _run(
            tmp_path,
            {
                "DEPLOY_FAKE_UP_FAIL_COUNT": "1",
                "DEPLOY_FAKE_STATE": str(state),
                "DEPLOY_FAKE_UP_ERROR": "Error: some unrelated compose failure",
            },
            "converge_stack",
        )
        assert result.returncode != 0
        assert state.read_text(encoding="utf-8") == "1"
        assert "No such container" not in result.stdout + result.stderr

    def test_retries_once_on_a_name_collision(self) -> None:
        """An orphaned `<hex>_<name>` container makes the next converge fail with
        'is already in use', not 'No such container'. Retrying only on the latter is why the
        v0.118.0 converge gave up after one attempt and left the worker tier in Created.
        """
        body = DEPLOY_SH.read_text(encoding="utf-8")
        retry_test = body.split("if [[ ${attempts} -lt ${max_attempts} ]]", 1)[1].split("then", 1)[0]
        assert "is already in use" in retry_test
        assert "no such container" in retry_test.lower()

    def test_orphan_sweep_survives_a_failing_docker(self, tmp_path: Path) -> None:
        """The sweep is a diagnostic, so it must degrade to 'sweep nothing' rather than abort the
        deploy. Under `set -e` an unguarded `x="$(docker ...)"` assignment exits the shell the
        moment docker errors — which took the whole converge down with it, `up` never running.

        `_run` puts tmp_path first on PATH, so this docker stub shadows the real binary.
        """
        docker_stub = tmp_path / "docker"
        docker_stub.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        docker_stub.chmod(0o755)
        state = tmp_path / "state"
        result = _run(tmp_path, {"DEPLOY_FAKE_STATE": str(state)}, "converge_stack")
        assert result.returncode == 0, result.stderr + result.stdout
        assert state.read_text(encoding="utf-8") == "1"  # the `up` still ran exactly once


class TestVerifyStackRunning:
    def test_passes_when_all_services_running(self, tmp_path: Path) -> None:
        result = _run(tmp_path, {"DEPLOY_FAKE_MODE": "pass"}, "verify_stack_running")
        assert result.returncode == 0, result.stderr + result.stdout
        assert "Verified all expected services are running" in result.stdout

    def test_ignores_a_stopped_container_of_a_profile_disabled_service(self, tmp_path: Path) -> None:
        # `compose ps` labels by project, not by profile: the standalone selenium-chrome the Grid
        # overlay parks for rollback sits Exited indefinitely. Failing on it would break every
        # deploy over a service the deploy never touches.
        result = _run(tmp_path, {"DEPLOY_FAKE_MODE": "profile_leftover"}, "verify_stack_running")
        assert result.returncode == 0, result.stderr + result.stdout
        assert "selenium-chrome" not in result.stdout + result.stderr

    @pytest.mark.parametrize(
        ("mode", "needle"),
        [
            ("created", "created"),
            ("exited", "exited"),
            ("restarting", "restarting"),
            ("missing", "not running"),
        ],
    )
    def test_fails_on_unhealthy_service(self, tmp_path: Path, mode: str, needle: str) -> None:
        result = _run(tmp_path, {"DEPLOY_FAKE_MODE": mode}, "verify_stack_running")
        assert result.returncode != 0, result.stdout
        combined = result.stdout + result.stderr
        if mode == "missing":
            assert "celery_beat" in combined
        else:
            assert "celery_worker" in combined
        assert needle in combined


class TestDeployScriptWiring:
    """The functions above are only worth anything if the deploy path actually calls them.

    Unit-testing them in isolation cannot catch a converge/verify call being dropped from `main`,
    which is the exact regression this issue is about (#831).
    """

    BODY = DEPLOY_SH.read_text(encoding="utf-8")

    def test_main_converges_through_the_retrying_helper(self) -> None:
        assert "if ! converge_stack; then" in self.BODY
        # No bare bulk converge left anywhere — including the rollback path, which recreates the
        # same worker tier and runs when something has already gone wrong.
        assert "${COMPOSE} up -d --remove-orphans" not in self.BODY.replace(
            '${COMPOSE} up -d --remove-orphans >"${logfile}" 2>&1', ""
        )

    def test_main_aborts_when_verification_fails(self) -> None:
        block = self.BODY.split("if ! verify_stack_running; then", 1)
        assert len(block) == 2, "main() no longer verifies the converged stack"
        body_lines = block[1].splitlines()
        end = next(i for i, line in enumerate(body_lines) if line.strip() == "fi")
        assert any(line.strip() == "exit 1" for line in body_lines[:end])

    def test_tag_baseline_is_persisted_before_the_worker_converge(self) -> None:
        # A partial deploy must still leave IMAGE_TAG/.last_good_tag matching what is serving.
        persist = self.BODY.index('persist_image_tag "${TAG}"')
        converge = self.BODY.index("if ! converge_stack; then")
        assert persist < converge
