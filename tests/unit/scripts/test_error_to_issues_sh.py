"""Tests for the key resolution in scripts/error_to_issues.sh (issue #1453).

The wrapper does not decide which key wins — `posthog_error_issues.py` does — so what has to hold
here is that it exports BOTH the purpose-scoped `POSTHOG_QUERY_API_KEY` and the shared
`POSTHOG_PERSONAL_API_KEY` fallback, and that it only skips the run when neither exists.
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
    def test_no_key_at_all_skips_and_names_both_vars(self, tmp_path):
        result = _run(tmp_path)
        assert result.returncode == 0
        assert "POSTHOG_QUERY_API_KEY" in result.stderr
        assert "POSTHOG_PERSONAL_API_KEY" in result.stderr
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
