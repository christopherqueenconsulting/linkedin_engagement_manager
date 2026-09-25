"""Tests for scripts/lib/app_container.sh — the container host crons exec python into (#2160).

In prod `web_app` is the nginx edge and has no python, so every `docker exec web_app python …`
in a host cron failed and was swallowed: the alert emails never sent (#2160) and perf_snapshot's
margin block was null (#2109). The helper resolves the ACTIVE `web_api_<color>` instead.
"""

import re
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_SCRIPTS = Path(__file__).resolve().parents[3] / "scripts"
_LIB = _SCRIPTS / "lib" / "app_container.sh"

# Every host-cron script that execs python into the app container.
_CALLERS = (
    "perf_snapshot.sh",
    "weekly_sdui_drift_check.sh",
    "weekly_model_check.sh",
    "weekly_linkedin_version_check.sh",
    "error_to_issues.sh",
    "triage_issues.sh",
)


def _resolve(lem_root: Path) -> str:
    """Source the helper against `lem_root` and return what it resolves."""
    result = subprocess.run(
        ["bash", "-c", f'. "{_LIB}"; active_api_container'],
        capture_output=True, text=True, env={"LEM_ROOT": str(lem_root), "PATH": "/usr/bin:/bin"},
        check=True,
    )
    return result.stdout.strip()


class TestActiveApiContainer:
    @pytest.mark.parametrize("color", ["blue", "green"])
    def test_the_recorded_color_names_its_container(self, tmp_path, color):
        (tmp_path / ".active_color").write_text(f"{color}\n", encoding="utf-8")
        assert _resolve(tmp_path) == f"web_api_{color}"

    def test_whitespace_around_the_color_is_ignored(self, tmp_path):
        (tmp_path / ".active_color").write_text("  green \n\n", encoding="utf-8")
        assert _resolve(tmp_path) == "web_api_green"

    def test_a_missing_state_file_falls_back_to_blue(self, tmp_path):
        assert _resolve(tmp_path) == "web_api_blue"

    @pytest.mark.parametrize("junk", ["", "purple", "web_app", "blue; rm -rf /"])
    def test_an_unexpected_value_falls_back_to_blue(self, tmp_path, junk):
        # Never echoes the file's content back: it becomes a `docker exec` target.
        (tmp_path / ".active_color").write_text(junk, encoding="utf-8")
        assert _resolve(tmp_path) == "web_api_blue"

    def test_it_never_resolves_the_nginx_edge(self, tmp_path):
        (tmp_path / ".active_color").write_text("web_app", encoding="utf-8")
        assert _resolve(tmp_path) != "web_app"


class TestCallers:
    def test_no_host_cron_script_execs_python_into_web_app(self):
        pattern = re.compile(r"docker\s+exec\b[^\n]*\bweb_app\b[^\n]*\bpython")
        offenders = [
            f"{path.name}:{n}"
            for path in sorted(_SCRIPTS.glob("*.sh"))
            for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
            if pattern.search(line)
        ]
        assert offenders == []

    @pytest.mark.parametrize("name", _CALLERS)
    def test_each_caller_sources_the_shared_helper(self, name):
        text = (_SCRIPTS / name).read_text(encoding="utf-8")
        assert '. "$(dirname "${BASH_SOURCE[0]}")/lib/app_container.sh"' in text
        assert "active_api_container" in text
        # No private copy of the resolver drifting from the shared one.
        assert "active_api_container(){" not in text

    def test_the_version_bump_recreates_the_api_container_not_the_edge(self):
        # smoke() reads LI_API_VERSION off $APP_CONTAINER, so that is the one that must be
        # recreated with the new .env; nginx `web_app` has no env_file to re-read.
        text = (_SCRIPTS / "weekly_linkedin_version_check.sh").read_text(encoding="utf-8")
        assert 'APP_SERVICES="$APP_CONTAINER $APP_SERVICES_BASE"' in text
        base = re.search(r'^APP_SERVICES_BASE="([^"]*)"', text, re.M)
        assert base is not None and "web_app" not in base.group(1).split()
        assert 'for service in "$APP_CONTAINER" celery_worker; do' in text
