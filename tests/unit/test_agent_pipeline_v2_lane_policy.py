"""Lane routing and the start-retry gate — the two decisions that spend money.

Both were found by watching the live pipeline rather than by reading it, and both had the same
shape: a decision made from a signal that could not answer the question being asked.

* Routing asked "has the Claude lane been failing?" when the question was "how much of the weekly
  subscription is left". Four runs that never reached a model answered the first question wrongly,
  and 30 consecutive dispatches went to Ollama while the subscription sat at 23.5% used.
* The start lane asked "did the run exit non-zero?" when the question was "did the run produce
  anything". #1290 exited 0 at 05:43:27 and was re-dispatched at 05:43:34.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_PIPE = _ROOT / "scripts" / "agent-pipeline"
_V2 = _PIPE / "v2"
sys.path.insert(0, str(_V2))

from lemd import db, lane, observe, policy, spend  # noqa: E402

pytestmark = pytest.mark.unit


def _usage(week: float | None, *, readable: bool = True) -> spend.Usage:
    """A usage reading with just the week window set."""
    return spend.Usage(week_pct=week, session_pct=10.0, readable=readable, week_resets="Aug 13")


# --------------------------------------------------------------------------- the 50% flip

def test_at_fifty_percent_the_claude_subscription_leaves_rotation_entirely():
    """The owner's rule, and v1's behaviour. A GLOBAL flip, not a per-mode preference."""
    for mode in ("start", "revise", "rebase", "selfreview", "review", "docfix"):
        choice = lane.decide(mode, _usage(50.0))
        assert choice.lane == "ollama", f"{mode} should leave Claude at the flip"
        assert "weekly_flip" in choice.reason


def test_just_below_the_flip_the_split_still_applies():
    """49% is not 50%. The boundary has to be exact or the rule is a suggestion."""
    assert lane.decide("start", _usage(49.9)).lane == "claude"
    assert lane.decide("start", _usage(50.0)).lane == "ollama"


def test_the_flip_reason_names_the_reset_so_it_can_be_waited_out():
    """An operator seeing everything on Ollama must be able to tell WHEN that ends."""
    assert "resets=Aug 13" in lane.decide("start", _usage(80.0)).reason


# --------------------------------------------------------------------------- fail-safe

def test_an_unreadable_probe_picks_NEITHER_lane():
    """The one honest default: no override, and say so.

    Defaulting to Claude spends a window nobody measured; defaulting to Ollama is exactly the
    failure this module was written to fix. Both are silent. Declining to answer is not.
    """
    choice = lane.decide("start", _usage(None, readable=False))
    assert choice.lane is None
    assert not choice.overrides
    assert choice.reason == "probe_unavailable"


def test_a_readable_probe_with_no_week_window_is_also_unavailable():
    """Session-only is not enough: the owner's rule is about the WEEK."""
    partial = spend.Usage(session_pct=12.0, week_pct=None, readable=True)
    assert lane.decide("start", partial).lane is None


# --------------------------------------------------------------------------- the split

@pytest.mark.parametrize("mode", ["start", "revise", "rebase"])
def test_quality_modes_prefer_claude_below_the_flip(mode):
    """A fresh build, acting on the owner's words, and a conflict resolution all need the good model."""
    assert lane.decide(mode, _usage(20.0)).lane == "claude"


@pytest.mark.parametrize("mode", ["review", "phasefix", "docfix", "depfix"])
def test_mechanical_modes_are_ollama_eligible(mode):
    """The cheap lane exists for exactly this work."""
    choice = lane.decide(mode, _usage(20.0))
    assert choice.lane == "ollama"
    assert choice.tier == "lem-agent-tier2"


def test_an_unmapped_mode_defers_rather_than_guessing():
    """`fix`, and anything added later, keeps the existing health routing instead of inheriting a guess."""
    assert lane.decide("fix", _usage(20.0)).lane is None


# --------------------------------------------------------------------------- H4, the livelock

def test_selfreview_prefers_claude_but_the_wait_is_BOUNDED():
    """An unbounded wait here is a designed livelock.

    selfreview produces the merge gate's review evidence, so starving it does not slow merges — it
    stops them, pipeline-wide, for the whole pause.
    """
    unpaused = lane.decide("selfreview", _usage(20.0), paused_seconds=0, usage_pause_minutes=60)
    assert unpaused.lane == "claude"

    waiting = lane.decide("selfreview", _usage(20.0), paused_seconds=3600, usage_pause_minutes=60)
    assert waiting.lane == "claude"
    assert "waiting" in waiting.reason

    # 2x the pause is the bound.
    exhausted = lane.decide("selfreview", _usage(20.0), paused_seconds=7201, usage_pause_minutes=60)
    assert exhausted.lane == "ollama"
    assert exhausted.tier == "lem-agent-tier3"
    assert "DEGRADED" in exhausted.marker, "a quality trade must be loud enough to find in a log"


# --------------------------------------------------------------------------- shell handoff

def test_the_shell_rendering_quotes_safely():
    """A routing decision reaching bash through `eval` must not be a quoting bug."""
    choice = lane.LaneChoice("ollama", "it's 50% used", tier="lem-agent-tier3", marker="a 'loud' note")
    env = lane.emit_env(choice)
    assert "LEM_LANE_OVERRIDE='ollama'" in env
    assert "'\\''" in env, "single quotes in a reason must be escaped, not terminate the string"


def test_no_override_emits_only_a_reason():
    """`probe_unavailable` must leave the health routing untouched, not set an empty lane."""
    env = lane.emit_env(lane.LaneChoice(None, "probe_unavailable"))
    assert env == "LEM_LANE_REASON='probe_unavailable'"


def test_dispatch_sh_honours_the_override_and_v1_never_sets_it():
    """The bash consumer must exist, and must not change v1's behaviour by existing."""
    d = (_PIPE / "lib" / "dispatch.sh").read_text()
    assert 'if [ -n "${LEM_LANE_OVERRIDE:-}" ]; then' in d
    tick = (_PIPE / "tick.sh").read_text()
    assert "LEM_LANE_OVERRIDE=" not in tick, "v1 must never set the override"


# --------------------------------------------------------------------------- the retry gate

def _issue(work_exists):
    """A ready issue with a given work state."""
    return observe.Snapshot(kind="issue", number=1290, labels=frozenset({"agent:ready"}),
                            work_exists=work_exists)


def _decide(snap):
    """Run the state machine with production TTLs."""
    return observe.decide(snap, ttl_ci=1800, ttl_review=3600, ttl_queue=900, ttl_parked=21600)


def test_an_issue_that_already_produced_work_is_not_restarted():
    """#1290: rc=0 at 05:43:27, re-dispatched at 05:43:34, another 9-12 minute session burned."""
    d = _decide(_issue(True))
    assert d.action == observe.ACT_NONE
    assert d.reason == "start_already_produced_work"


def test_an_issue_with_nothing_behind_it_still_starts():
    """The gate must not disable the lane it protects."""
    d = _decide(_issue(False))
    assert d.action == observe.ACT_DISPATCH
    assert d.mode == "start"


def test_an_unreadable_work_state_WAITS_rather_than_restarting():
    """The asymmetry is the design.

    Waiting on a false positive costs one TTL. Re-dispatching on a false negative costs a full model
    session and risks two agents on one branch — an agent that pushed and died before opening a PR
    is work in progress, not a blank slate.
    """
    d = _decide(_issue(None))
    assert d.action == observe.ACT_NONE
    assert d.reason == "start_work_state_unreadable"


def test_the_start_budget_allows_exactly_one_retry():
    """Worst case is one retry, because the gate means a retry only happens on a run that produced nothing."""
    assert policy.budget_for("start") == 2


def test_work_existence_is_read_from_github_not_the_local_checkout():
    """A local branch proves `git checkout -b` ran, which every attempt does before doing anything."""
    src = (_V2 / "lemd" / "observe.py").read_text()
    assert "github.branch_exists" in src
    assert "closedByPullRequestsReferences" in src


def test_branch_existence_is_three_valued():
    """404 is a readable NO; an unreachable GitHub is not."""
    from lemd import github
    src = (_V2 / "lemd" / "github.py").read_text()
    assert "def branch_exists" in src
    assert "-> bool | None" in src
    assert hasattr(github, "branch_exists")


# --------------------------------------------------------------------------- the poisoned gauge

def test_an_unexecutable_run_is_not_recorded_as_a_lane_failure():
    """Three rc=127s in a row took the Claude subscription out of rotation for 30 minutes.

    capacity.sh constrains a lane after 3 consecutive failures. A run that never reached a model is
    not evidence about that lane's capacity, and treating it as such is self-harming: it routes
    every subsequent dispatch to the other lane and burns a quota the owner is watching.
    """
    src = (_PIPE / "lib" / "run_lane.sh").read_text()
    assert "if [ $rc -eq 127 ] || [ $rc -eq 126 ]; then" in src
    gated = src.split("if [ $rc -eq 127 ]")[1]
    assert "record_lane_outcome" in gated.split("else")[1].split("fi")[0]


def test_status_surfaces_the_effective_v2_cap():
    """Two knobs with one meaning is a trap a comment in a Python file cannot close."""
    src = (_PIPE / "status.sh").read_text()
    assert "LEMD_MAX_AGENTS" in src
    assert "v2 cap:" in src
    assert "dispatcher:" in src


def test_the_decision_log_reports_the_state_actually_persisted():
    """Logging `decide`'s intent made every restart look like a claim storm that never happened."""
    src = (_V2 / "lemd" / "daemon.py").read_text()
    assert '"intent": decision.next_state' in src
    assert 'persisted = db.STATE_READY' in src


def test_ready_state_names_stay_in_sync():
    """A cheap guard: these strings are compared across three files."""
    assert db.STATE_READY == "ready"
    assert db.STATE_WAIT_REVIEW == "awaiting_review"
