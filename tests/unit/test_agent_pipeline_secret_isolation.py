"""Guards for the pipeline's credential custody (closes the C4 finding).

Agent runs execute as the same uid as the runner, with `--dangerously-skip-permissions`. That
makes file modes on anything that uid owns worthless as a boundary: `--add-dir` scopes the file
tools, but the Bash tool reads whatever the uid can read, and the prompt an agent follows is built
from issue text written by strangers.

So the property under test is CUSTODY, not permissions: the runner must not need the App private
key at all. Root mints a ~1h installation token into the cache; the runner consumes it. The worst
an agent can reach is a credential that expires within the hour and carries authority the pipeline
already has — rather than a key that never expires and survives rotation.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
PIPELINE = REPO / "scripts" / "agent-pipeline"
RUN_LANE = PIPELINE / "lib" / "run_lane.sh"
TOKEN_LIB = PIPELINE / "lib" / "gh_app_token.sh"


def _bash(script: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    """Run a bash snippet with the token library sourced."""
    full = f'set -u; BASE="${{BASE:-/tmp/nonexistent-base}}"; . "{TOKEN_LIB}"\n{script}'
    e = {**os.environ, "PATH": os.environ.get("PATH", "")}
    e.update(env or {})
    return subprocess.run(["bash", "-c", full], capture_output=True, text=True, env=e, timeout=30)


# ---------------------------------------------------------------- the dispatch surface


def test_agent_is_not_granted_the_pipeline_base_directory():
    """`--add-dir $BASE` would hand the agent config.env, secrets.env and the key's directory."""
    text = RUN_LANE.read_text()
    assert '--add-dir "$BASE"' not in text, "agents must not be granted $BASE"
    assert '--add-dir "$runbook_dir"' in text


def test_every_agent_dispatch_uses_the_scoped_directory():
    """Both lanes must be scoped — fixing one and leaving the other is the whole bug."""
    dispatches = re.findall(r"--dangerously-skip-permissions --add-dir \"([^\"]+)\"",
                            RUN_LANE.read_text())
    assert len(dispatches) >= 2, "expected both lane dispatches to be found"
    assert set(dispatches) == {"$runbook_dir"}


def test_runbook_dir_is_derived_not_hardcoded():
    """The RUNBOOK may move; the grant must follow it rather than pin a stale path."""
    assert 'runbook_dir="$(dirname "${RUNBOOK:-$BASE/RUNBOOK.md}")"' in RUN_LANE.read_text()


# ---------------------------------------------------------------- custody


def test_runner_prefers_the_root_owned_key_location():
    """/etc/lem is root-owned; $BASE/secrets is reachable by the agent's uid."""
    text = TOKEN_LIB.read_text()
    assert 'GH_APP_KEY:-/etc/lem/github-app.pem' in text
    assert "GH_APP_KEY_LEGACY" in text, "legacy path must remain as a migration fallback"


def test_token_is_served_from_cache_when_no_key_is_readable(tmp_path):
    """The hardened box: root mints, the runner only ever reads the cache."""
    cache = tmp_path / "gh-app-token"
    cache.write_text(f"{2**31} ghs_cached_token_value\n")
    r = _bash(
        'gh_app_token',
        {"GH_APP_TOKEN_CACHE": str(cache), "GH_APP_KEY": str(tmp_path / "absent.pem"),
         "GH_APP_KEY_LEGACY": str(tmp_path / "absent2.pem"), "BASE": str(tmp_path)},
    )
    assert r.returncode == 0
    assert r.stdout.strip() == "ghs_cached_token_value"


def test_no_key_and_no_cache_fails_soft(tmp_path):
    """Failure must degrade IDENTITY (fall back to the PAT), never halt the pipeline."""
    r = _bash(
        'gh_app_token || echo "NO_TOKEN"',
        {"GH_APP_TOKEN_CACHE": str(tmp_path / "absent-cache"),
         "GH_APP_KEY": str(tmp_path / "absent.pem"),
         "GH_APP_KEY_LEGACY": str(tmp_path / "absent2.pem"), "BASE": str(tmp_path)},
    )
    assert "NO_TOKEN" in r.stdout


def test_expired_cache_is_not_served(tmp_path):
    """A stale token must not be handed out; better no identity than a rejected one."""
    cache = tmp_path / "gh-app-token"
    cache.write_text("1 ghs_expired\n")
    r = _bash(
        'gh_app_token || echo "NO_TOKEN"',
        {"GH_APP_TOKEN_CACHE": str(cache), "GH_APP_KEY": str(tmp_path / "absent.pem"),
         "GH_APP_KEY_LEGACY": str(tmp_path / "absent2.pem"), "BASE": str(tmp_path)},
    )
    assert "ghs_expired" not in r.stdout
    assert "NO_TOKEN" in r.stdout


def test_garbage_cache_is_not_served(tmp_path):
    """A truncated or corrupt cache must read as 'no token', not as a token."""
    cache = tmp_path / "gh-app-token"
    cache.write_text("not-a-timestamp whatever\n")
    r = _bash(
        'gh_app_token || echo "NO_TOKEN"',
        {"GH_APP_TOKEN_CACHE": str(cache), "GH_APP_KEY": str(tmp_path / "absent.pem"),
         "GH_APP_KEY_LEGACY": str(tmp_path / "absent2.pem"), "BASE": str(tmp_path)},
    )
    assert "NO_TOKEN" in r.stdout


def test_export_is_opt_in(tmp_path):
    """USE_GH_APP=0 must leave whatever credential the caller already had."""
    cache = tmp_path / "gh-app-token"
    cache.write_text(f"{2**31} ghs_x\n")
    r = _bash(
        'gh_app_export_token && echo EXPORTED || echo SKIPPED',
        {"USE_GH_APP": "0", "GH_APP_TOKEN_CACHE": str(cache), "BASE": str(tmp_path)},
    )
    assert "SKIPPED" in r.stdout


def test_cache_is_written_private(tmp_path):
    """The token is the credential now; a world-readable cache would undo the whole change."""
    text = TOKEN_LIB.read_text()
    assert "umask 077" in text


# ---------------------------------------------------------------- the systemd contract


@pytest.mark.parametrize("unit", ["lem-gh-token.service", "lem-gh-token.timer"])
def test_token_units_ship_with_the_repo(unit):
    assert (PIPELINE / "systemd" / unit).is_file()


def test_minting_unit_runs_as_root_and_reads_the_protected_key():
    """If this unit ran as the runner's uid it would defeat its own purpose."""
    text = (PIPELINE / "systemd" / "lem-gh-token.service").read_text()
    assert "User=root" in text
    assert "GH_APP_KEY=/etc/lem/github-app.pem" in text


def test_refresh_beats_expiry():
    """Installation tokens last ~60 min; refreshing later than that leaves a credential gap."""
    text = (PIPELINE / "systemd" / "lem-gh-token.timer").read_text()
    m = re.search(r"OnUnitActiveSec=(\d+)min", text)
    assert m and int(m.group(1)) < 60
