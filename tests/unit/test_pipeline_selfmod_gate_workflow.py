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


def _steps(workflow: dict[str, Any]) -> list[dict[str, Any]]:
    return workflow["jobs"]["gate"]["steps"]


class TestItJudgesWithTheBaseBranchCode:
    """A PR must not be able to rewrite the gate script, or CODEOWNERS, and approve itself."""

    def test_the_checkout_is_the_base_commit(self):
        checkouts = [s for s in _steps(_load()) if str(s.get("uses", "")).startswith(
            "actions/checkout@")]
        assert len(checkouts) == 1
        assert checkouts[0]["with"]["ref"] == "${{ github.event.pull_request.base.sha }}"
        assert checkouts[0]["with"]["persist-credentials"] is False

    def test_the_pr_head_is_never_checked_out(self):
        # The ONE permitted reference to the head is its SHA, handed to the self-heal step as an
        # API filter value. Anything else naming the head (a ref, a checkout, a run line) fails.
        text = _WORKFLOW_PATH.read_text(encoding="utf-8")
        assert "refs/pull/" not in text
        assert text.count("pull_request.head") == 1
        heal = _self_heal_step(_load())
        assert heal["env"]["HEAD_SHA"] == "${{ github.event.pull_request.head.sha }}"
        assert "pull_request.head" not in str(heal["run"])

    def test_the_pr_files_and_diff_arrive_as_data(self):
        run_text = _all_run_steps(_load())
        assert "gh pr view" in run_text
        assert "gh pr diff" in run_text
        assert "--diff \"$DIFF_PATH\"" in run_text

    def test_the_diff_is_collected_for_every_author(self):
        # A rename's SOURCE is only in the diff, so it is not a Dependabot-only fact any more.
        diff_step = next(s for s in _steps(_load()) if s.get("id") == "diff")
        assert "dependabot" not in str(diff_step.get("if", ""))

    def test_a_truncated_file_list_fails_closed(self):
        run_text = _all_run_steps(_load())
        assert "changedFiles" in run_text


class TestItIsSafeAsARequiredCheckInTheMergeQueue:
    """A required check that never reports on `merge_group` blocks the queue."""

    def test_it_triggers_on_merge_group(self):
        assert "merge_group" in _triggers(_load())

    def test_the_merge_group_run_reports_the_same_job_name(self):
        jobs = _load()["jobs"]
        assert list(jobs) == ["gate"]
        assert jobs["gate"]["name"] == "Pipeline Self-Modification Gate"
        assert "if" not in jobs["gate"], "a skipped job-level `if` must not decide the context"

    def test_the_merge_group_run_decides_nothing(self):
        for step in _steps(_load()):
            cond = str(step.get("if", ""))
            if "scripts/pipeline_selfmod_gate.py" in str(step.get("run", "")) \
                    or "uses" in step:
                assert "github.event_name != 'merge_group'" in cond, step.get("name")

    def test_a_queue_run_is_never_cancelled(self):
        concurrency = _load()["concurrency"]
        assert "github.ref" in concurrency["group"]
        assert "merge_group" in str(concurrency["cancel-in-progress"])


def _self_heal_step(workflow: dict[str, Any]) -> dict[str, Any]:
    heals = [s for s in _steps(workflow) if "rerun-failed-jobs" in str(s.get("run", ""))]
    assert len(heals) == 1, "exactly one step may re-run gate runs"
    return heals[0]


class TestItSelfHealsOnApproval:
    """A passing review run re-runs the stale failed pull_request-event run (#2596, #2645)."""

    def test_concurrency_is_split_by_event(self):
        # Without the event name, a review-triggered run and a pull_request-triggered run (or its
        # self-heal re-run) share one group and cancel each other (#2645 attempt 2).
        concurrency = _load()["concurrency"]
        assert "github.event_name" in concurrency["group"]
        assert "github.event.pull_request.number" in concurrency["group"]
        # Superseded runs of the SAME event are still cancelled.
        assert str(concurrency["cancel-in-progress"]) == "${{ github.event_name != 'merge_group' }}"

    def test_actions_write_is_job_scoped_and_minimal(self):
        workflow = _load()
        assert "actions" not in (workflow.get("permissions") or {})
        job_perms = workflow["jobs"]["gate"]["permissions"]
        assert job_perms == {"contents": "read", "pull-requests": "read", "actions": "write"}

    def test_no_other_step_touches_actions_runs(self):
        workflow = _load()
        heal = _self_heal_step(workflow)
        for step in _steps(workflow):
            if step is heal:
                continue
            assert "actions/runs" not in str(step.get("run", "")), step.get("name")
            assert "gh run rerun" not in str(step.get("run", "")), step.get("name")

    def test_it_runs_only_on_a_passing_review_event(self):
        cond = str(_self_heal_step(_load()).get("if", ""))
        assert "github.event_name == 'pull_request_review'" in cond
        assert "success()" in cond
        assert "always()" not in cond and "failure()" not in cond

    def test_it_is_the_last_step_so_it_follows_the_verdict(self):
        steps = _steps(_load())
        assert steps[-1] == _self_heal_step(_load())
        decide = next(i for i, s in enumerate(steps)
                      if "scripts/pipeline_selfmod_gate.py" in str(s.get("run", ""))
                      and "--approvers" in str(s.get("run", "")))
        assert decide < len(steps) - 1

    def test_it_filters_to_this_sha_this_workflow_pull_request_failures(self):
        run = str(_self_heal_step(_load())["run"])
        # Server-side filter...
        assert "event=pull_request" in run
        assert "head_sha=$HEAD_SHA" in run
        assert "status=failure" in run
        assert "actions/workflows/$workflow_id/runs" in run
        # ...re-checked client-side, so an ignored query filter cannot widen the set.
        assert '.head_sha == $sha' in run
        assert '.event == "pull_request"' in run
        assert '.conclusion == "failure"' in run
        assert '.workflow_id == $wf' in run
        # The workflow id is THIS run's, never a hard-coded or other workflow.
        assert "actions/runs/$SELF_RUN_ID" in run
        assert _self_heal_step(_load())["env"]["SELF_RUN_ID"] == "${{ github.run_id }}"

    def test_it_reruns_failed_jobs_only(self):
        run = str(_self_heal_step(_load())["run"])
        assert "rerun-failed-jobs" in run
        assert re.search(r"/rerun[\"'\s]", run) is None, "a full re-run would redo passing jobs"

    def test_it_is_fail_soft(self):
        heal = _self_heal_step(_load())
        assert heal.get("continue-on-error") is True
        run = str(heal["run"])
        assert "set -e" not in run, "errexit would turn a re-run error into a failed gate"
        assert "::warning" in run
        assert run.rstrip().endswith("exit 0")


class TestItActsOnAPullRequestNotAPush:
    def test_no_push_trigger_is_present(self):
        # This gate's entire model is "a PR touching a protected path needs the owner's review
        # before merge" -- it has nothing to decide on a direct push to main.
        triggers = _triggers(_load())
        assert "push" not in triggers
