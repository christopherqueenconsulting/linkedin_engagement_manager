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

import json
import os
import re
import shutil
import stat
import subprocess
import sys
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
        # The WORKFLOW-LEVEL default: every job that does not restate permissions gets only reads,
        # so nothing can merge, comment, or push on the pipeline's behalf. The judge restates the
        # same reads (TestTheJudgeIsReadOnly); the one job-scoped write, `actions: write` on the
        # heal job, is pinned in TestItSelfHealsOnApproval.test_heal_holds_actions_write_only.
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
        # Branch protection requires the context `Pipeline Self-Modification Gate`, which is the
        # gate JOB's name (docs/codeowners-enforcement-limit.md, Owner steps). The heal job is
        # the only other job, and its name must never collide with that context.
        jobs = _load()["jobs"]
        assert list(jobs) == ["gate", "heal"]
        assert jobs["gate"]["name"] == "Pipeline Self-Modification Gate"
        assert jobs["heal"]["name"] != jobs["gate"]["name"]
        assert "if" not in jobs["gate"], "a skipped job-level `if` must not decide the context"
        assert "needs" not in jobs["gate"], "the required context must not wait on another job"

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


def _every_step(workflow: dict[str, Any]) -> list[dict[str, Any]]:
    return [s for job in workflow["jobs"].values() for s in job.get("steps") or []]


def _self_heal_step(workflow: dict[str, Any]) -> dict[str, Any]:
    heals = [s for s in _every_step(workflow) if "rerun-failed-jobs" in str(s.get("run", ""))]
    assert len(heals) == 1, "exactly one step may re-run gate runs"
    assert heals[0] in workflow["jobs"]["heal"]["steps"], "the re-run lives in the heal job"
    return heals[0]


class TestTheJudgeIsReadOnly:
    def test_the_gate_job_restates_only_the_two_reads(self):
        assert _load()["jobs"]["gate"]["permissions"] == {
            "contents": "read", "pull-requests": "read"}

    def test_the_gate_job_never_touches_actions_runs(self):
        for step in _steps(_load()):
            assert "actions/runs" not in str(step.get("run", "")), step.get("name")
            assert "rerun" not in str(step.get("run", "")), step.get("name")


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

    def test_heal_holds_actions_write_only(self):
        # Every call it makes is Actions API (read this run, list runs, POST rerun-failed-jobs),
        # which `actions: write` covers; contents and pull-requests are not needed.
        workflow = _load()
        assert "actions" not in (workflow.get("permissions") or {})
        assert workflow["jobs"]["heal"]["permissions"] == {"actions": "write"}

    def test_heal_checks_nothing_out(self):
        for step in _load()["jobs"]["heal"]["steps"]:
            assert "uses" not in step, f"the heal job runs no action: {step}"
            assert "checkout" not in str(step.get("run", ""))
            assert "git " not in str(step.get("run", ""))

    def test_no_other_step_touches_actions_runs(self):
        workflow = _load()
        heal = _self_heal_step(workflow)
        for step in _every_step(workflow):
            if step is heal:
                continue
            assert "actions/runs" not in str(step.get("run", "")), step.get("name")
            assert "gh run rerun" not in str(step.get("run", "")), step.get("name")

    def test_it_runs_only_after_a_passing_gate_on_a_review_event(self):
        heal = _load()["jobs"]["heal"]
        assert heal["needs"] == "gate"
        cond = str(heal.get("if", ""))
        assert "github.event_name == 'pull_request_review'" in cond
        assert "needs.gate.result == 'success'" in cond
        assert "always()" not in cond and "failure()" not in cond
        assert "if" not in _self_heal_step(_load()), "the job condition is the one gate"

    def test_heal_is_bounded_and_fail_soft_as_a_job(self):
        heal = _load()["jobs"]["heal"]
        assert heal.get("continue-on-error") is True
        assert 0 < int(heal["timeout-minutes"]) <= 10

    def test_it_polls_in_flight_runs_for_about_two_minutes_at_most(self):
        heal = _self_heal_step(_load())
        tries = int(heal["env"]["HEAL_POLL_TRIES"])
        interval = int(heal["env"]["HEAL_POLL_INTERVAL"])
        assert 0 < tries * interval <= 150
        run = str(heal["run"])
        assert run.index('.status != "completed"') < run.index("status=failure"), (
            "the poll must finish before failures are listed")

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

    def test_no_full_rerun_anywhere_in_heal(self):
        # A full re-run redoes passing jobs; only the REST rerun-failed-jobs endpoint may appear.
        for step in _load()["jobs"]["heal"]["steps"]:
            text = str(step.get("run", ""))
            assert re.search(r"/rerun(?!-failed-jobs)", text) is None, text
            for line in text.splitlines():
                if re.search(r"gh\s+run\s+rerun", line):
                    assert "--failed" in line, line

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


# --- The heal step's bash, executed against a fake `gh` -------------------------------------------

_REPO = "acme/lem"
_SHA = "a" * 40
_SELF = 500
_WF = 42

#: A fake `gh api`. It IGNORES every server-side query filter, so the script's own jq re-check is
#: what the assertions below exercise. Every call is appended to calls.log.
_FAKE_GH = r"""
import json, os, sys
state = os.environ["FAKE_GH_STATE"]
args = sys.argv[1:]
with open(os.path.join(state, "calls.log"), "a") as f:
    f.write(json.dumps(args) + "\n")
assert args[0] == "api", args
rest = args[1:]
if rest[:2] == ["-X", "POST"]:
    sys.exit(1 if os.environ.get("FAKE_GH_POST_FAILS") == "1" else 0)
path = rest[0]
fixture = json.load(open(os.path.join(state, "fixture.json")))
if path.endswith("/actions/runs/" + os.environ["SELF_RUN_ID"]):
    if fixture.get("self_fails"):
        sys.exit(1)
    print(json.dumps({"id": int(os.environ["SELF_RUN_ID"]), "workflow_id": fixture["wf"]}))
elif "status=failure" in path:
    print(json.dumps({"workflow_runs": fixture["runs"]}))
else:
    counter = os.path.join(state, "polls")
    n = int(open(counter).read()) if os.path.exists(counter) else 0
    open(counter, "w").write(str(n + 1))
    busy = n < fixture["busy_polls"]
    runs = [dict(r, status="in_progress" if busy else "completed") for r in fixture["runs"]]
    # Another SHA's run that never finishes: the poll must not wait on it.
    runs.append({"id": 900, "head_sha": "b" * 40, "event": "pull_request",
                 "workflow_id": fixture["wf"], "status": "in_progress", "conclusion": None})
    print(json.dumps({"workflow_runs": runs}))
"""


def _run(sha: str, event: str, conclusion: str, wf: int, run_id: int) -> dict[str, Any]:
    return {"id": run_id, "head_sha": sha, "event": event, "workflow_id": wf,
            "status": "completed", "conclusion": conclusion}


_FIXTURE_RUNS = [
    _run(_SHA, "pull_request", "failure", _WF, 101),         # the stale failure: re-run it
    _run("c" * 40, "pull_request", "failure", _WF, 102),     # another SHA
    _run(_SHA, "pull_request_review", "failure", _WF, 103),  # another event
    _run(_SHA, "pull_request", "failure", 99, 104),          # another workflow
    _run(_SHA, "pull_request", "success", _WF, 105),         # not a failure
    _run(_SHA, "pull_request", "failure", _WF, _SELF),       # this run itself
]


def _exec_heal(tmp_path: Path, *, busy_polls: int = 0, tries: int = 12,
               post_fails: bool = False, self_fails: bool = False,
               ) -> tuple[subprocess.CompletedProcess[str], list[list[str]], int]:
    """Run the heal step's `run:` block under bash with a fake `gh` first on PATH.

    Returns:
        The finished process, every recorded `gh` argv, and how many poll calls were made.
    """
    state = tmp_path / "state"
    state.mkdir()
    (state / "fixture.json").write_text(json.dumps(
        {"wf": _WF, "runs": _FIXTURE_RUNS, "busy_polls": busy_polls, "self_fails": self_fails}))
    bindir = tmp_path / "bin"
    bindir.mkdir()
    gh = bindir / "gh"
    gh.write_text(f"#!{sys.executable}\n{_FAKE_GH}")
    gh.chmod(gh.stat().st_mode | stat.S_IXUSR)
    script = tmp_path / "heal.sh"
    script.write_text(str(_self_heal_step(_load())["run"]))
    env = {
        "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
        "FAKE_GH_STATE": str(state),
        "FAKE_GH_POST_FAILS": "1" if post_fails else "0",
        "REPO": _REPO,
        "HEAD_SHA": _SHA,
        "SELF_RUN_ID": str(_SELF),
        "HEAL_POLL_TRIES": str(tries),
        "HEAL_POLL_INTERVAL": "0",
    }
    proc = subprocess.run(["bash", str(script)], env=env, capture_output=True, text=True,
                          timeout=60, check=False)
    log = state / "calls.log"
    calls = [json.loads(ln) for ln in log.read_text().splitlines()] if log.exists() else []
    polls_file = state / "polls"
    polls = int(polls_file.read_text()) if polls_file.exists() else 0
    return proc, calls, polls


def _posts(calls: list[list[str]]) -> list[str]:
    return [c[3] for c in calls if c[1:3] == ["-X", "POST"]]


@pytest.mark.skipif(shutil.which("jq") is None or shutil.which("bash") is None,
                    reason="the heal step is bash + jq")
class TestTheHealScriptAgainstAFakeGh:
    def test_only_the_same_sha_pull_request_failure_of_this_workflow_is_rerun(self, tmp_path):
        proc, calls, _ = _exec_heal(tmp_path)
        assert proc.returncode == 0, proc.stderr
        assert _posts(calls) == [f"repos/{_REPO}/actions/runs/101/rerun-failed-jobs"]

    def test_a_failing_post_still_exits_zero_with_a_warning(self, tmp_path):
        proc, calls, _ = _exec_heal(tmp_path, post_fails=True)
        assert proc.returncode == 0, proc.stderr
        assert _posts(calls), "the POST must have been attempted for the warning to mean anything"
        assert "::warning title=Gate self-heal::re-run of gate run 101 failed" in proc.stdout

    def test_the_poll_exits_once_runs_complete_then_lists(self, tmp_path):
        proc, calls, polls = _exec_heal(tmp_path, busy_polls=2)
        assert proc.returncode == 0, proc.stderr
        # Two busy answers, then the one that shows everything complete; the other SHA's
        # never-finishing run is not waited on.
        assert polls == 3
        assert "Waiting for" in proc.stdout
        assert "still running" not in proc.stdout
        listing = next(i for i, c in enumerate(calls) if "status=failure" in c[1])
        last_poll = max(i for i, c in enumerate(calls)
                        if "/runs?" in c[1] and "status=failure" not in c[1])
        assert last_poll < listing
        assert _posts(calls) == [f"repos/{_REPO}/actions/runs/101/rerun-failed-jobs"]

    def test_the_poll_is_bounded_and_then_proceeds_with_a_warning(self, tmp_path):
        proc, calls, polls = _exec_heal(tmp_path, busy_polls=1000, tries=3)
        assert proc.returncode == 0, proc.stderr
        assert polls == 3
        assert "::warning title=Gate self-heal::a pull_request gate run" in proc.stdout
        assert _posts(calls) == [f"repos/{_REPO}/actions/runs/101/rerun-failed-jobs"]

    def test_an_unreadable_workflow_id_warns_and_posts_nothing(self, tmp_path):
        proc, calls, polls = _exec_heal(tmp_path, self_fails=True)
        assert proc.returncode == 0, proc.stderr
        assert "::warning title=Gate self-heal::could not read this run's workflow id" in proc.stdout
        assert polls == 0
        assert _posts(calls) == []
