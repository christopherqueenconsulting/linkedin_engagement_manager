"""An agent must not be able to merge a change to its own trust boundary (#1397).

The pipeline merges its own PRs — `required_approving_review_count` is 0 and CODEOWNERS enforces
nothing at that setting. That is survivable only because a merged pipeline change still needs a
human to reach the box. Automating the deploy (#1398) removes that step and closes the loop: an
agent could rewrite `scripts/agent-pipeline/**`, merge it unattended, and have it running with the
owner's credentials minutes later.

So this gate is the precondition for that automation, not an accessory to it.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "scripts"))

from pipeline_selfmod_gate import PROTECTED, decide, main, touches_protected  # noqa: E402

OWNER = "gitchrisqueen"
BOT = "cqc-lem-agent-pipeline[bot]"


# ---------------------------------------------------------------- what counts as protected


@pytest.mark.parametrize("path", [
    "scripts/agent-pipeline/tick.sh",
    "scripts/agent-pipeline/v2/lemd/observe.py",
    "scripts/agent-pipeline/v2/actions/agent_run.sh",
    ".github/workflows/deploy-vps.yml",
    ".github/CODEOWNERS",
])
def test_the_machinery_and_its_credentials_are_protected(path):
    """The pipeline's own code, and the workflows that hold the secrets it would use."""
    assert touches_protected([path]) == [path]


@pytest.mark.parametrize("path", [
    "src/cqc_lem/api/main.py",
    "docs/agent-pipeline-v2.md",
    "tests/unit/test_agent_pipeline_v2_observe.py",
    "README.md",
])
def test_ordinary_changes_are_not_protected(path):
    """Deliberately narrow. A gate that fires on everything is one people route around.

    Note the doc and the pipeline's TESTS are not protected: they cannot change what the runner
    does. Only the code it executes can.
    """
    assert touches_protected([path]) == []


# ---------------------------------------------------------------- the verdict


def test_an_untouched_pr_passes_without_review():
    """The overwhelming majority of PRs, which this must not slow down."""
    got = decide(changed=["src/cqc_lem/api/main.py"], author=BOT, approvers=[], owner=OWNER)
    assert got.allowed


def test_the_bot_cannot_merge_a_pipeline_change_unreviewed():
    """The whole point."""
    got = decide(changed=["scripts/agent-pipeline/v2/lemd/observe.py"],
                 author=BOT, approvers=[], owner=OWNER)
    assert not got.allowed
    assert "no approving review" in got.reason


def test_the_bot_may_merge_a_pipeline_change_the_owner_approved():
    """The gate asks for a human, not for the work to stop."""
    got = decide(changed=["scripts/agent-pipeline/v2/lemd/observe.py"],
                 author=BOT, approvers=[OWNER], owner=OWNER)
    assert got.allowed


def test_an_approval_from_anyone_else_is_not_enough():
    """Only the owner's approval counts; the allowlist is one name for a reason."""
    got = decide(changed=["scripts/agent-pipeline/tick.sh"],
                 author=BOT, approvers=["some-contributor"], owner=OWNER)
    assert not got.allowed


def test_the_owner_is_exempt_from_their_own_gate():
    """The threat is the bot's identity acting unattended, not the owner acting under their own.

    Single-human-maintainer repo (#1692): the owner already holds every credential and control
    surface a merge could reach, and there is no second person to demand a review from. Revisit
    the day a second human maintainer joins.
    """
    got = decide(changed=["scripts/agent-pipeline/v2/lemd/daemon.py"],
                 author=OWNER, approvers=[], owner=OWNER)
    assert got.allowed
    assert "sole human maintainer" in got.reason


def test_a_workflow_change_by_the_owner_is_exempt_too():
    """Workflows hold the credentials, but the owner already holds those credentials directly."""
    got = decide(changed=[".github/workflows/build-and-push.yml"],
                 author=OWNER, approvers=[], owner=OWNER)
    assert got.allowed


def test_a_mixed_pr_is_gated_on_its_protected_half():
    """One protected file is enough; the rest of the diff does not dilute it."""
    got = decide(changed=["README.md", "scripts/agent-pipeline/install.sh", "src/x.py"],
                 author=BOT, approvers=[], owner=OWNER)
    assert not got.allowed


def test_the_refusal_names_what_to_do_about_it():
    """A refusal that does not say what it wants gets worked around rather than satisfied."""
    got = decide(changed=["scripts/agent-pipeline/tick.sh"], author=BOT, approvers=[], owner=OWNER)
    assert OWNER in got.reason
    assert "scripts/agent-pipeline/tick.sh" in got.reason


# ---------------------------------------------------------------- the CLI


def _run(changed, author, approvers):
    """Invoke the gate the way CI does."""
    return subprocess.run(
        [sys.executable, str(_ROOT / "scripts" / "pipeline_selfmod_gate.py"),
         "--changed", json.dumps(changed), "--author", author,
         "--approvers", json.dumps(approvers), "--owner", OWNER],
        capture_output=True, text=True, timeout=30,
    )


def test_the_cli_exits_non_zero_on_a_refusal():
    """CI reads the exit code; anything else is advisory."""
    got = _run(["scripts/agent-pipeline/tick.sh"], BOT, [])
    assert got.returncode == 1
    assert "REFUSED" in got.stdout
    assert "::error title=Pipeline self-modification::" in got.stdout


def test_the_cli_exits_zero_when_allowed():
    """And must not fail the ordinary case."""
    got = _run(["src/cqc_lem/api/main.py"], BOT, [])
    assert got.returncode == 0
    assert "PASS" in got.stdout


def test_main_is_importable_and_returns_the_code():
    """Kept callable in-process so the logic is testable without a subprocess."""
    rc = main(["gate", "--changed", json.dumps(["scripts/agent-pipeline/x.sh"]),
               "--author", BOT, "--approvers", "[]", "--owner", OWNER])
    assert rc == 1


def test_the_protected_list_is_not_silently_empty():
    """An empty list would make the gate pass everything while looking installed."""
    assert PROTECTED
    assert any("agent-pipeline" in p for p in PROTECTED)
    assert any("workflows" in p for p in PROTECTED)
