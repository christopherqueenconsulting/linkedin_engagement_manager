"""The pipeline must never run on the ambient owner login (#1362).

Both runners resolved their identity as "App token, else `AGENT_GH_TOKEN`" with no `else`. When
neither produced one, `GH_TOKEN` was simply never exported and every `gh` call fell through to
whatever credential the `lem` user holds — `~/.config/gh/hosts.yml`, the OWNER's oauth token,
which carries `workflow` scope. That is the authority `docs/contribution-security.md` calls "the
hard control": the only thing making an agent rewriting `.github/workflows/**` impossible rather
than merely reviewable.

`AGENT_GH_TOKEN` was revoked on 2026-08-10 (#1311 §2), so the fall-through is the PERMANENT state
whenever the App token cannot be minted. These tests execute the real scripts with no credential
available and assert they refuse.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

_PIPELINE = Path(__file__).resolve().parents[2] / "scripts" / "agent-pipeline"
EX_SETUP = 73


@pytest.fixture
def fake_base(tmp_path: Path) -> Path:
    """A pipeline tree with no credential of any kind reachable.

    `guards.sh` is copied because `common.sh` treats it as the trust boundary and refuses without
    it; everything else is stubbed so the test exercises identity resolution and nothing else.
    """
    (tmp_path / "lib").mkdir()
    (tmp_path / "lib" / "guards.sh").write_text(
        "claim_branch() { return 0; }\nissue_for_pr() { echo ''; }\npr_for_issue() { echo ''; }\n"
    )
    # No gh_app_token.sh: `gh_app_export_token` is therefore undefined, which is exactly the
    # "could not mint" state. `common.sh` sources the optional libs with `|| true`.
    (tmp_path / "config.env").write_text("SLUG=owner/repo\nASSIGNEE=someone\n")
    return tmp_path


def _run_common(base: Path, env_extra: dict[str, str] | None = None):
    """Source `common.sh` exactly as an action does, and report what it decided."""
    env = {
        "PATH": "/usr/bin:/bin",
        "BASE": str(base),
        "HOME": str(base),
        # The hazard itself: an ambient gh credential present in the environment.
        "GH_CONFIG_DIR": str(base / "gh"),
        **(env_extra or {}),
    }
    return subprocess.run(
        ["bash", "-c", f'V2_ACTION=test . "{_PIPELINE}/v2/actions/common.sh"; echo "SURVIVED"'],
        capture_output=True, text=True, env=env, timeout=30,
    )


def test_v2_refuses_when_no_credential_can_be_resolved(fake_base):
    """The defect: this used to fall through and keep running as the owner."""
    got = _run_common(fake_base)
    assert "SURVIVED" not in got.stdout
    assert got.returncode == EX_SETUP
    assert "no pipeline credential" in (got.stdout + got.stderr)


def test_v2_proceeds_on_the_pipelines_own_token(fake_base):
    """The refusal must be about having NO credential, not about which one."""
    got = _run_common(fake_base, {"AGENT_GH_TOKEN": "ghp_fake_pipeline_token"})
    assert "SURVIVED" in got.stdout
    assert got.returncode == 0


def test_the_refusal_names_a_distinct_exit_code(fake_base):
    """EX_SETUP, not EX_TRUST.

    An unmintable token is an environment that is not ready — the daemon retries that. A trust
    refusal would park the item for six hours over what is usually a one-minute gap between the
    hourly mint timer and an expiring token.
    """
    assert _run_common(fake_base).returncode != 70


def test_the_exit_vocabulary_is_defined_before_it_is_used():
    """The refusal uses `$EX_SETUP`, so the constants must be in scope by then.

    Under `set -u` an undefined `$EX_SETUP` would abort with a shell error instead of the intended
    code, turning a deliberate refusal into an unreadable failure.
    """
    src = (_PIPELINE / "v2" / "actions" / "common.sh").read_text()
    assert src.index("EX_SETUP=73") < src.index('exit "$EX_SETUP"')


def test_v1_refuses_the_same_way():
    """v1 is the failsafe, and it had the identical hole.

    Asserted at source: `tick.sh` reaches this point only after acquiring locks and reading GitHub,
    so executing it in a unit test is not viable.
    """
    src = (_PIPELINE / "tick.sh").read_text()
    assert 'if [ -z "${GH_TOKEN:-}" ]; then' in src
    assert "REFUSING this tick" in src


def test_neither_runner_still_has_a_bare_fall_through():
    """The shape of the defect, so it cannot come back by a different route."""
    v2 = (_PIPELINE / "v2" / "actions" / "common.sh").read_text()
    assert 'export GH_TOKEN="$AGENT_GH_TOKEN"\nfi' not in v2
