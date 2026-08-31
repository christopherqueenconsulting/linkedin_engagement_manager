"""Tests for scripts/error_to_issues.sh.

Key resolution (issue #1453): the wrapper does not decide which key wins —
`posthog_error_issues.py` does — so what has to hold here is that it exports BOTH the
purpose-scoped `POSTHOG_QUERY_API_KEY` and the shared `POSTHOG_PERSONAL_API_KEY` fallback, and
that it only skips the run when neither exists.

Failure alerting (2026-08-31 08:30 UTC outage): a transient PostHog 503 killed a whole day's run
with nothing but a line in a log file nobody reads. `alert()` is best-effort — it shells out to
`sudo -n docker exec ... web_app` (unavailable in this test sandbox), so what these tests can prove
without a live stack is the SHAPE of the decision: `alert()` (and therefore its "ALERT:" log line)
fires exactly when the Python script exits non-zero, never on a clean rc=0 run — not whether the
email itself was delivered, which needs a running `web_app` container this suite does not have.
"""

import os
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_SH = Path(__file__).resolve().parents[3] / "scripts" / "error_to_issues.sh"


def _stub_python(tmp_path: Path) -> Path:
    """An executable stand-in for the interpreter, printing the keys it was handed."""
    stub = tmp_path / "fake-python"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        'echo "QUERY=${POSTHOG_QUERY_API_KEY:-}"\n'
        'echo "PERSONAL=${POSTHOG_PERSONAL_API_KEY:-}"\n',
        encoding="utf-8",
    )
    stub.chmod(0o755)
    return stub


def _run(tmp_path: Path, **env_overrides) -> subprocess.CompletedProcess:
    """Run the wrapper against a stub interpreter and an empty env file (no /opt/lem read)."""
    env_file = tmp_path / "env"
    env_file.write_text("UNRELATED=1\n", encoding="utf-8")
    env = os.environ.copy()
    for name in ("POSTHOG_QUERY_API_KEY", "POSTHOG_PERSONAL_API_KEY"):
        env.pop(name, None)
    env.update({
        "REPO": str(tmp_path),
        "ERROR_ISSUES_DIR": str(tmp_path / "state"),
        "ERROR_ISSUES_PY": str(_stub_python(tmp_path)),
        "LEM_ENV_FILE": str(env_file),
    })
    env.update(env_overrides)
    return subprocess.run(["bash", str(_SH)], capture_output=True, text=True, env=env)


class TestKeyResolution:
    def test_no_key_at_all_skips_and_names_the_var_to_add(self, tmp_path):
        # The skip line names the ONE var worth adding. It used to name the shared key too, which
        # after the 2026-08-31 revoke would be advice to install a revoked credential (issue #1453).
        result = _run(tmp_path)
        assert result.returncode == 0
        assert "POSTHOG_QUERY_API_KEY" in result.stderr
        assert "POSTHOG_PERSONAL_API_KEY" not in result.stderr
        assert "skipping" in result.stderr

    def test_the_scoped_key_alone_is_enough_to_run(self, tmp_path):
        result = _run(tmp_path, POSTHOG_QUERY_API_KEY="phx_query")
        assert result.returncode == 0
        assert "QUERY=phx_query" in result.stdout + result.stderr

    def test_the_shared_key_alone_is_still_enough_to_run(self, tmp_path):
        # Additive rollout: the cron keeps working before the scoped key exists anywhere.
        result = _run(tmp_path, POSTHOG_PERSONAL_API_KEY="phx_shared")
        assert result.returncode == 0
        assert "PERSONAL=phx_shared" in result.stdout + result.stderr

    def test_both_reach_the_interpreter_so_python_picks(self, tmp_path):
        result = _run(tmp_path, POSTHOG_QUERY_API_KEY="phx_query",
                      POSTHOG_PERSONAL_API_KEY="phx_shared")
        output = result.stdout + result.stderr
        assert "QUERY=phx_query" in output
        assert "PERSONAL=phx_shared" in output


def _stub_python_exit(tmp_path: Path, rc: int, stdout: str = "") -> Path:
    """An executable stand-in for `posthog_error_issues.py` that just exits `rc`."""
    stub = tmp_path / "fake-python-exit"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        f'echo "{stdout}"\n'
        f"exit {rc}\n",
        encoding="utf-8",
    )
    stub.chmod(0o755)
    return stub


def _run_with_rc(tmp_path: Path, rc: int, stdout: str = "") -> subprocess.CompletedProcess:
    env_file = tmp_path / "env"
    env_file.write_text("UNRELATED=1\n", encoding="utf-8")
    env = os.environ.copy()
    for name in ("POSTHOG_QUERY_API_KEY", "POSTHOG_PERSONAL_API_KEY"):
        env.pop(name, None)
    env.update({
        "REPO": str(tmp_path),
        "ERROR_ISSUES_DIR": str(tmp_path / "state"),
        "ERROR_ISSUES_PY": str(_stub_python_exit(tmp_path, rc, stdout)),
        "LEM_ENV_FILE": str(env_file),
        "POSTHOG_QUERY_API_KEY": "phx_query",
    })
    return subprocess.run(["bash", str(_SH)], capture_output=True, text=True, env=env)


class TestFailureAlert:
    def test_a_clean_run_never_alerts(self, tmp_path):
        result = _run_with_rc(tmp_path, rc=0, stdout="0 error-tracking issues in the window")
        assert result.returncode == 0
        assert "ALERT:" not in result.stdout + result.stderr

    def test_a_failed_run_alerts_and_names_the_exit_code(self, tmp_path):
        result = _run_with_rc(tmp_path, rc=1, stdout="PostHog query failed: 503")
        assert result.returncode == 1
        output = result.stdout + result.stderr
        assert "ALERT:" in output
        assert "exit 1" in output

    def test_a_repo_not_found_still_alerts(self, tmp_path):
        env_file = tmp_path / "env"
        env_file.write_text("UNRELATED=1\n", encoding="utf-8")
        env = os.environ.copy()
        for name in ("POSTHOG_QUERY_API_KEY", "POSTHOG_PERSONAL_API_KEY"):
            env.pop(name, None)
        missing_repo = tmp_path / "does-not-exist"
        env.update({
            "REPO": str(missing_repo),
            "ERROR_ISSUES_DIR": str(tmp_path / "state"),
            "ERROR_ISSUES_PY": str(_stub_python_exit(tmp_path, 0)),
            "LEM_ENV_FILE": str(env_file),
            "POSTHOG_QUERY_API_KEY": "phx_query",
        })
        result = subprocess.run(["bash", str(_SH)], capture_output=True, text=True, env=env)
        assert result.returncode == 1
        assert "ALERT:" in result.stdout + result.stderr
