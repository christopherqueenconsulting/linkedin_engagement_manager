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

    _ALL_PS_A = {
        "pass": [
            "web_app\\trunning",
            "web_api_blue\\trunning",
            "web_api_green\\trunning",
            "celery_worker\\trunning",
            "celery_beat\\trunning",
        ],
        "created": [
            "web_app\\trunning",
            "celery_worker\\tcreated",
        ],
        "exited": [
            "web_app\\trunning",
            "celery_worker\\texited",
        ],
        "missing": [
            "web_app\\trunning",
            "web_api_blue\\trunning",
            "web_api_green\\trunning",
            "celery_worker\\trunning",
            "celery_beat\\trunning",
        ],
    }

    _ALL_PS = {
        "pass": ["web_app", "web_api_blue", "web_api_green", "celery_worker", "celery_beat"],
        "missing": ["web_app", "web_api_blue", "web_api_green", "celery_worker"],
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
        for svc in _ALL_PS["pass"]:
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


class TestVerifyStackRunning:
    def test_passes_when_all_services_running(self, tmp_path: Path) -> None:
        result = _run(tmp_path, {"DEPLOY_FAKE_MODE": "pass"}, "verify_stack_running")
        assert result.returncode == 0, result.stderr + result.stdout
        assert "Verified all expected services are running" in result.stdout

    @pytest.mark.parametrize(
        ("mode", "needle"),
        [
            ("created", "Created"),
            ("exited", "Exited"),
            ("missing", "not running"),
        ],
    )
    def test_fails_on_unhealthy_service(self, tmp_path: Path, mode: str, needle: str) -> None:
        result = _run(tmp_path, {"DEPLOY_FAKE_MODE": mode}, "verify_stack_running")
        assert result.returncode != 0, result.stdout
        combined = result.stdout + result.stderr
        assert "celery_worker" in combined or mode == "missing"
        assert needle in combined
