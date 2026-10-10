"""DEMO_MODE in the LinkedIn scripts that do not ride the package's `requests` guard (#2372).

`linkedin_version_check.py` and `linkedin_post_stats_api_probe.py` call LinkedIn over urllib, so
they refuse explicitly, before any urlopen, with a distinct exit code. The weekly version-check
wrapper must read that refusal as `skipped`: never a retired pin, never an `.env` rewrite, never a
container recreate.
"""

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from cqc_lem.utilities.demo_mode import DemoModeError

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = ROOT / "scripts"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f"{name}_demo_under_test", SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def lvc():
    return _load("linkedin_version_check")


@pytest.fixture
def stats_probe():
    return _load("linkedin_post_stats_api_probe")


class TestVersionCheckScript:
    def test_plan_is_skipped_before_token_or_probe(self, lvc, monkeypatch, capsys):
        monkeypatch.setenv("DEMO_MODE", "true")
        with patch.object(lvc.urllib.request, "urlopen") as urlopen, \
             patch.object(lvc, "_access_token") as token:
            rc = lvc.main(["--plan-json", "--current", "202601", "--token", "tok"])
        assert rc == lvc.DEMO_MODE_EXIT != 0
        plan = json.loads(capsys.readouterr().out)
        assert plan["action"] == "skipped" and "DEMO_MODE" in plan["reason"]
        urlopen.assert_not_called()
        token.assert_not_called()

    def test_probe_version_refuses_before_urlopen(self, lvc, monkeypatch):
        monkeypatch.setenv("DEMO_MODE", "1")
        with patch.object(lvc.urllib.request, "urlopen") as urlopen:
            with pytest.raises(DemoModeError):
                lvc.probe_version("tok", "202601", sleep=lambda _s: None)
        urlopen.assert_not_called()

    def test_probe_still_runs_when_off(self, lvc, monkeypatch):
        monkeypatch.delenv("DEMO_MODE", raising=False)
        response = patch.object(lvc.urllib.request, "urlopen")
        with response as urlopen:
            urlopen.return_value.__enter__.return_value.status = 200
            assert lvc.probe_version("tok", "202601", sleep=lambda _s: None) is True
        urlopen.assert_called_once()


class TestPostStatsProbeScript:
    def test_main_is_skipped_before_db_or_network(self, stats_probe, monkeypatch, capsys):
        monkeypatch.setenv("DEMO_MODE", "yes")
        with patch.object(stats_probe.urllib.request, "urlopen") as urlopen, \
             patch.object(stats_probe, "_resolve_from_db") as db:
            rc = stats_probe.main(["--entity-urn", "urn:li:share:1", "--token", "tok",
                                   "--version", "202606"])
        assert rc == stats_probe.DEMO_MODE_EXIT != 0
        report = json.loads(capsys.readouterr().out)
        assert report["verdict"]["outcome"] == "skipped"
        urlopen.assert_not_called()
        db.assert_not_called()

    def test_http_get_json_refuses_before_urlopen(self, stats_probe, monkeypatch):
        monkeypatch.setenv("DEMO_MODE", "true")
        with patch.object(stats_probe.urllib.request, "urlopen") as urlopen:
            with pytest.raises(DemoModeError):
                stats_probe.http_get_json(stats_probe.API_URL, "tok", "202606")
        urlopen.assert_not_called()


# --------------------------------------------------------------------------------------------------
# The weekly wrapper, run for real with `sudo` replaced by an exported bash function. A function
# wins over PATH lookup, so the wrapper's own `export PATH=...` cannot route around it, and the real
# sudo is never reached — which matters because this suite also runs on the box itself.
# --------------------------------------------------------------------------------------------------

_FAKE_SUDO = r'''
sudo() {
  printf '%s\n' "$*" >> "$FAKE_CALLS"
  if [ "$2" = "grep" ]; then echo "LI_API_VERSION=202601"; return 0; fi
  case " $* " in
    *" --plan-json "*)
      # -n docker exec -i <container> python - ARGS...  ->  run the REAL planner on its stdin.
      shift 6
      "$FAKE_PY" "$@"; return $? ;;
  esac
  return 0
}
export -f sudo
'''


def _run_wrapper(tmp_path: Path, demo_mode: str) -> tuple[subprocess.CompletedProcess, list, str]:
    calls = tmp_path / "calls.txt"
    calls.touch()
    env = {
        "HOME": str(tmp_path), "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "REPO": str(ROOT), "LEM_ROOT": str(tmp_path), "APP_CONTAINER": "web_api_blue",
        "LI_VERSION_CHECK_DIR": str(tmp_path / "log"), "FAKE_CALLS": str(calls),
        "FAKE_PY": sys.executable, "PYTHONPATH": str(ROOT / "src"), "DEMO_MODE": demo_mode,
    }
    result = subprocess.run(
        ["bash", "-c", _FAKE_SUDO + f'\nbash "{SCRIPTS / "weekly_linkedin_version_check.sh"}"'],
        capture_output=True, text=True, env=env, cwd=tmp_path, timeout=120,
    )
    log = (tmp_path / "log" / "li_version_check.log").read_text()
    return result, calls.read_text().splitlines(), log


class TestWeeklyWrapperTreatsTheRefusalAsSkipped:
    def test_demo_mode_is_skipped_and_touches_nothing(self, tmp_path):
        result, calls, log = _run_wrapper(tmp_path, "true")
        assert result.returncode == 0, result.stderr
        assert "action=skipped" in log and "done (skipped)" in log
        # Exactly the read of the current pin and the planner run — no .env backup or rewrite,
        # no recreate, no alert email.
        assert len(calls) == 2, calls
        assert calls[0].startswith("-n grep -E ^LI_API_VERSION=")
        assert "--plan-json" in calls[1]
        for verb in ("cp ", "sed ", "tee ", "compose", "ALERT"):
            assert not any(verb in c for c in calls), (verb, calls)
        assert "ALERT" not in log and "bump" not in log.split("plan:")[-1].split("\n")[0]

    def test_control_without_demo_mode_the_skip_branch_is_not_taken(self, tmp_path):
        # Without demo mode the real planner goes on to the token read and probe. Whatever it
        # answers in the unit lane (no DB, no token), it is not `skipped`, and the wrapper alerts
        # through a second `docker exec`. That is the path the skipped branch must never take.
        result, calls, log = _run_wrapper(tmp_path, "")
        assert result.returncode == 1
        assert "skipped" not in log and "ALERT" in log
        assert len(calls) == 3, calls
