"""`.github/workflows/pipeline-selfmod-gate.yml` must actually wire the verdict helper (#1397).

`scripts/pipeline_selfmod_gate.py` (#1413) is the tested logic; this file exists to catch the gap
between "the logic is correct" and "the workflow calls it with the arguments that logic expects".
That gap is exactly the failure mode #1397 exists to close — a workflow that never re-derives the
verdict inline, always defers to the one place it is decided.

Also guards the trigger set: this gate has to see both the events that can make a PR's protected-path
verdict change — a new/changed diff (`pull_request`) and a fresh approval (`pull_request_review`) —
or a PR could sit gated after the owner approves it, with nothing left to re-run the check.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[2]
_WORKFLOW_PATH = _ROOT / ".github" / "workflows" / "pipeline-selfmod-gate.yml"
_GATE_SCRIPT = _ROOT / "scripts" / "pipeline_selfmod_gate.py"


def _load() -> dict[str, Any]:
    return yaml.safe_load(_WORKFLOW_PATH.read_text(encoding="utf-8"))


def _triggers(workflow: dict[str, Any]) -> dict[str, Any]:
    """Return the workflow's trigger block.

    `on:` is a YAML 1.1 boolean, so PyYAML parses the key as `True` — reading `workflow["on"]`
    would find nothing and make every assertion below vacuously pass on a workflow with no
    triggers at all (see `test_agent_pipeline_trust_boundary.py`'s codecov sibling for the same
    trap).
    """
    return workflow.get("on", workflow.get(True)) or {}


def _all_run_steps(workflow: dict[str, Any]) -> str:
    """Concatenate every `run:` block in the workflow, for substring assertions."""
    chunks: list[str] = []
    for job in (workflow.get("jobs") or {}).values():
        for step in job.get("steps") or []:
            if "run" in step:
                chunks.append(str(step["run"]))
    return "\n".join(chunks)


class TestTheWorkflowFileExists:
    def test_it_is_present(self):
        assert _WORKFLOW_PATH.is_file(), (
            "issue #1397 requires .github/workflows/pipeline-selfmod-gate.yml to exist"
        )


class TestTriggerSet:
    """Both events that can change the verdict must re-run it."""

    def test_it_triggers_on_pull_request(self):
        triggers = _triggers(_load())
        assert "pull_request" in triggers

    def test_it_triggers_on_pull_request_review(self):
        # A PR opened while already touching a protected path is gated. If an APPROVAL from the
        # owner never re-ran this check, the gate would stay red forever regardless of review.
        triggers = _triggers(_load())
        assert "pull_request_review" in triggers

    def test_pull_request_covers_a_diff_changing_after_the_gate_last_ran(self):
        # `synchronize` is the push-to-an-open-PR event; without it, a PR that ADDS a protected
        # path after opening would keep the stale (and possibly passing) verdict from its first
        # run.
        types = _triggers(_load())["pull_request"]["types"]
        assert "synchronize" in types
        assert "opened" in types

    def test_pull_request_review_covers_both_directions(self):
        # `submitted` is a fresh approval; `dismissed` is a withdrawn one -- both can flip the
        # verdict and both must re-run the check.
        types = _triggers(_load())["pull_request_review"]["types"]
        assert "submitted" in types
        assert "dismissed" in types

    def test_no_write_permissions_are_granted(self):
        # This gate only reads PR facts and exits non-zero; it must never be handed a permission
        # that would let it merge, comment, or push on the pipeline's behalf.
        permissions = _load().get("permissions") or {}
        assert permissions, "an unset permissions block inherits the default token's full grants"
        for scope, level in permissions.items():
            assert level == "read", f"{scope}: {level} grants write access this gate never needs"


class TestItCallsTheExistingHelperRatherThanReimplementing:
    def test_the_verdict_script_exists_where_the_workflow_expects_it(self):
        assert _GATE_SCRIPT.is_file(), (
            "the workflow calls scripts/pipeline_selfmod_gate.py; #1413 must still ship it there"
        )

    def test_the_workflow_invokes_the_gate_script(self):
        run_text = _all_run_steps(_load())
        assert "scripts/pipeline_selfmod_gate.py" in run_text

    def test_the_workflow_does_not_reimplement_the_verdict_inline(self):
        # The whole point of #1413 landing first: no second copy of "author == owner" or
        # "owner in approvers" logic living in YAML where it can drift from the tested version.
        run_text = _all_run_steps(_load())
        assert "PROTECTED" not in run_text
        assert re.search(r"==\s*['\"]?gitchrisqueen", run_text) is None

    @pytest.mark.parametrize("flag", ["--changed", "--author", "--approvers", "--owner"])
    def test_every_argument_the_cli_requires_is_passed(self, flag):
        # scripts/pipeline_selfmod_gate.py's argparse requires --changed/--author/--approvers and
        # accepts --owner (defaulted to gitchrisqueen, but the workflow passes it explicitly so the
        # allowlisted identity lives in one visible place).
        run_text = _all_run_steps(_load())
        assert flag in run_text

    def test_the_owner_argument_names_the_real_account(self):
        run_text = _all_run_steps(_load())
        assert "gitchrisqueen" in run_text

    def test_the_call_reads_the_pr_author_not_a_hardcoded_login(self):
        workflow_text = _WORKFLOW_PATH.read_text(encoding="utf-8")
        assert "github.event.pull_request.user.login" in workflow_text


class TestItActsOnAPullRequestNotAPush:
    def test_no_push_trigger_is_present(self):
        # This gate's entire model is "a PR touching a protected path needs the owner's review
        # before merge" -- it has nothing to decide on a direct push to main.
        triggers = _triggers(_load())
        assert "push" not in triggers
