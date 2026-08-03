"""Regression tests for the phase-guard -> MODE=phasefix lane in scripts/agent-pipeline/tick.sh.

The lane spends a Claude run per dispatch, so the thing that must never break is its BUDGET.
`phase_guard_ok` only ever sees a PR that still carries `agent:working`; a phasefix run that dies
(or escalates) without handing the PR back never returns there. So the counter has to be bumped
where the spend happens — in the lane, at dispatch — and the lane has to enforce the cap itself,
or a wedged PR re-dispatches every tick forever.

tick.sh hardcodes BASE=/home/lem/agent-pipeline and sources $BASE/lib/*.sh, so it can't be run
end-to-end in the unit lane. The counter contract IS executable, though: the helpers are extracted
and run for real; the lane's guards are asserted structurally.
"""

import re
import subprocess
import textwrap
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

TICK_SH = Path(__file__).resolve().parents[2] / "scripts" / "agent-pipeline" / "tick.sh"
SOURCE = TICK_SH.read_text(encoding="utf-8")

# The lane block: from its banner comment to the `done` that closes the for-loop.
LANE = re.search(
    r"# ---- PHASEFIX LANE:.*?\ndone\n", SOURCE, re.S
).group(0)
GUARD = re.search(r"\nphase_guard_ok\(\) \{.*?\n\}\n", SOURCE, re.S).group(0)


def _helpers() -> str:
    """The three counter helpers, lifted verbatim so the test runs the shipped code."""
    block = re.search(
        r"phasefix_attempts\(\).*?\nphasefix_clear\(\)[^\n]*\n", SOURCE, re.S
    )
    assert block, "counter helpers not found in tick.sh"
    return block.group(0)


def _run_counter_script(tmp_path: Path, body: str) -> str:
    script = f'BASE="{tmp_path}"\nmkdir -p "$BASE/state"\n{_helpers()}\n{textwrap.dedent(body)}'
    return subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, check=True
    ).stdout.strip()


def test_tick_sh_is_syntactically_valid():
    subprocess.run(["bash", "-n", str(TICK_SH)], check=True)


def test_counter_starts_at_zero_and_counts_dispatches(tmp_path):
    out = _run_counter_script(
        tmp_path,
        """
        phasefix_attempts 949
        phasefix_bump 949; phasefix_attempts 949
        phasefix_bump 949; phasefix_attempts 949
        """,
    )
    assert out.split() == ["0", "1", "2"]


def test_counter_is_per_pr_and_cleared_on_success(tmp_path):
    out = _run_counter_script(
        tmp_path,
        """
        phasefix_bump 949; phasefix_bump 949
        phasefix_attempts 950
        phasefix_clear 949; phasefix_attempts 949
        """,
    )
    assert out.split() == ["0", "0"]


def test_unreadable_counter_reads_as_zero_not_as_exhausted(tmp_path):
    """A truncated/garbage state file must not silently park a PR to the owner."""
    out = _run_counter_script(
        tmp_path,
        """
        printf 'not-a-number\\n' > "$BASE/state/phasefix-949.count"
        phasefix_attempts 949
        """,
    )
    assert out == "0"


def test_lane_bumps_the_counter_before_dispatching():
    dispatch = LANE.index("run_claude")
    assert "phasefix_bump" in LANE, "the lane must count its own dispatches"
    assert LANE.index("phasefix_bump") < dispatch


def test_lane_enforces_the_cap_itself():
    """phase_guard_ok can't bound a run that never hands the PR back — the lane must."""
    assert re.search(
        r'FCNT="\$\(phasefix_attempts "\$FPR"\)"\s*\n\s*if \[ "\$FCNT" -ge "\$MAX_PHASEFIX_ATTEMPTS" \]',
        LANE,
    )
    assert "phase_park_to_human" in LANE


def test_lane_skips_prs_already_parked_for_a_human():
    assert 'index("needs-human")|not' in LANE
    assert 'index("agent:blocked")|not' in LANE


def test_lane_returns_a_stale_hold_to_the_merge_loop_without_spending_a_run():
    """Dropping `Closes #N` clears the guard, so there is nothing left for an agent to do."""
    stale = LANE[LANE.index('if [ -z "$FISS" ]') : LANE.index("FCNT=")]
    assert "--add-label agent:working --remove-label agent:phasefix" in stale
    assert "run_claude" not in stale


def test_lane_never_runs_the_agent_from_an_empty_worktree_path():
    assert LANE.index('[ -z "$WT" ] || [ ! -d "$WT" ]') < LANE.index("run_claude")


def test_guard_does_not_also_count_holds():
    """Double-counting (guard + lane) would halve the budget the owner configured."""
    assert "phasefix_bump" not in GUARD
    assert "phasefix_clear" in GUARD  # a passing guard still clears the history
