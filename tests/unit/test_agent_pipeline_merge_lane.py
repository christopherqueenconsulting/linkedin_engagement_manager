"""Regression tests for the merge lane in scripts/agent-pipeline/tick.sh (#1082).

`main` merges through a GitHub merge queue, so `gh pr merge --auto` means ENQUEUE and exits 0
whether the PR entered the queue, was already in it, or holds an entry GitHub already evicted.
The lane used to read that exit code as "merged": PR #1067 sat green-and-unmerged for 47h while
561 identical "merging" comments were posted and the single concurrency slot starved.

So the contract under test is: the OUTCOME decides, never the exit code; an unreadable state is
never "merged"; a stall is counted and force-recovered; and one merge attempt can only ever
produce one comment (keyed on the head SHA).

tick.sh hardcodes BASE=/home/lem/agent-pipeline and sources $BASE/lib/*.sh, so it can't run
end-to-end in the unit lane. The merge-execution block IS executable, though: it is lifted
verbatim and run against a stubbed `gh`.
"""

import re
import subprocess
import textwrap
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

TICK_SH = Path(__file__).resolve().parents[2] / "scripts" / "agent-pipeline" / "tick.sh"
SOURCE = TICK_SH.read_text(encoding="utf-8")

BLOCK = re.search(
    r"# ---- MERGE EXECUTION:.*?\n# ---- end merge execution -+\n", SOURCE, re.S
).group(0)

# A stub `gh` covering exactly the reads/writes merge_pr makes, driven by three env vars so each
# test can pose a different GitHub state. Every invocation is appended to $BASE/calls.
HARNESS = """
set -uo pipefail
BASE="__BASE__"
SLUG="o/n"; OWNER="o"; NAME="n"
DRY_RUN="${DRY_RUN:-0}"
TICK_OUTCOME="dispatched"; TICK_REASON=""
mkdir -p "$BASE/state"
: > "$BASE/calls"
log() { echo "LOG: $*"; }
sleep() { printf 'sleep %s\\n' "$*" >> "$BASE/calls"; }
gh() {
  printf 'gh %s\\n' "$*" >> "$BASE/calls"
  case "$*" in
    *"--json url"*)                    echo "https://example.test/pr/$2" ;;
    *"--json headRefOid"*)             echo "$FAKE_SHA" ;;
    *"--json state,autoMergeRequest"*) echo "$FAKE_PR_STATE" ;;
    "api graphql"*)                    echo "$FAKE_QUEUE" ;;
  esac
}
FAKE_SHA="${FAKE_SHA:-cafe1234}"
FAKE_PR_STATE="${FAKE_PR_STATE:-OPEN|0}"
FAKE_QUEUE="${FAKE_QUEUE:-}"
__BLOCK__
"""


def _run(tmp_path: Path, body: str, **env: str) -> subprocess.CompletedProcess:
    script = (
        HARNESS.replace("__BASE__", str(tmp_path)).replace("__BLOCK__", BLOCK)
        + textwrap.dedent(body)
    )
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), **env},
    )


def _calls(tmp_path: Path) -> list[str]:
    f = tmp_path / "calls"
    return f.read_text(encoding="utf-8").splitlines() if f.exists() else []


def _comments(tmp_path: Path) -> list[str]:
    return [c for c in _calls(tmp_path) if c.startswith("gh pr comment")]


# --- classify_merge_state: the pure core -------------------------------------------------------

@pytest.mark.parametrize(
    "pr_state,queue,armed,expected",
    [
        ("MERGED", "", "0", "merged"),
        ("MERGED", "QUEUED", "1", "merged"),
        ("CLOSED", "", "0", "closed"),
        ("OPEN", "QUEUED", "0", "queued"),
        ("OPEN", "AWAITING_CHECKS", "1", "queued"),
        ("OPEN", "", "1", "armed"),
        ("OPEN", "", "0", "unqueued"),
    ],
)
def test_classify_merge_state(tmp_path, pr_state, queue, armed, expected):
    out = _run(tmp_path, f'classify_merge_state "{pr_state}" "{queue}" "{armed}"')
    assert out.stdout.strip() == expected


def test_unreadable_state_is_never_merged(tmp_path):
    """A gh read that failed must read as "not merged" — the whole point of #1082."""
    out = _run(tmp_path, 'classify_merge_state "" "" ""')
    assert out.stdout.strip() == "unqueued"


# --- stall counter ------------------------------------------------------------------------------

def test_stall_counter_counts_and_clears(tmp_path):
    out = _run(
        tmp_path,
        """
        merge_stall_count 1067
        merge_stall_bump 1067; merge_stall_bump 1067; merge_stall_count 1067
        merge_stall_clear 1067; merge_stall_count 1067
        """,
    )
    assert out.stdout.split() == ["0", "2", "0"]


def test_garbage_stall_counter_reads_as_zero(tmp_path):
    """A truncated state file must not fake a stall and force a needless re-enqueue."""
    out = _run(
        tmp_path,
        """
        printf 'not-a-number\\n' > "$BASE/state/mergestall-1067.count"
        merge_stall_count 1067
        """,
    )
    assert out.stdout.strip() == "0"


# --- comment keying: the 561-comment regression -------------------------------------------------

def test_comment_is_posted_once_per_head_sha(tmp_path):
    out = _run(
        tmp_path,
        """
        merge_comment_once 1067 sha-a "merging."; echo "rc=$?"
        merge_comment_once 1067 sha-a "merging."; echo "rc=$?"
        merge_comment_once 1067 sha-b "merging."; echo "rc=$?"
        """,
    )
    assert out.stdout.split() == ["rc=0", "rc=1", "rc=0"]
    assert len(_comments(tmp_path)) == 2  # one per distinct SHA, not one per call


def test_comment_key_is_per_pr(tmp_path):
    _run(
        tmp_path,
        """
        merge_comment_once 1067 sha-a "merging."
        merge_comment_once 1080 sha-a "merging."
        """,
    )
    assert len(_comments(tmp_path)) == 2


# --- merge_pr: outcome, not exit code ------------------------------------------------------------

def test_merge_pr_reports_a_live_queue_entry_distinctly(tmp_path):
    out = _run(
        tmp_path,
        'merge_pr 1067 "merging."; echo "rc=$?"',
        FAKE_PR_STATE="OPEN|1",
        FAKE_QUEUE="AWAITING_CHECKS",
    )
    assert "rc=0" in out.stdout
    assert "WAITING IN THE MERGE QUEUE (entry: AWAITING_CHECKS)" in out.stdout
    assert len(_comments(tmp_path)) == 1
    assert not (tmp_path / "state" / "mergestall-1067.count").exists()


def test_merge_pr_reports_a_merged_pr(tmp_path):
    out = _run(
        tmp_path, 'merge_pr 1067 "merging."; echo "rc=$?"', FAKE_PR_STATE="MERGED|0"
    )
    assert "rc=0" in out.stdout
    assert "PR #1067 is MERGED" in out.stdout
    assert len(_comments(tmp_path)) == 1


def test_merge_pr_treats_exit_zero_without_a_queue_entry_as_a_stall(tmp_path):
    """The #1067 state: gh exits 0, nothing is queued, nothing merges. Must NOT read as success."""
    out = _run(
        tmp_path,
        'merge_pr 1067 "merging."; echo "rc=$?"; echo "$TICK_OUTCOME/$TICK_REASON"',
        FAKE_PR_STATE="OPEN|0",
    )
    assert "rc=1" in out.stdout
    # The tick must report itself as failed, or the stall is invisible in telemetry again.
    assert "failed/merge_not_taken" in out.stdout
    assert "NEITHER merged NOR in the merge queue" in out.stdout
    assert _comments(tmp_path) == []  # a stall never claims "merging"
    assert (tmp_path / "state" / "mergestall-1067.count").read_text().strip() == "1"


def test_a_stalled_pr_never_accumulates_comments(tmp_path):
    """561 identical comments is the bug; ten stalled ticks must produce zero."""
    out = _run(
        tmp_path,
        'for _ in 1 2 3 4 5 6 7 8 9 10; do merge_pr 1067 "merging." || true; done',
        FAKE_PR_STATE="OPEN|0",
        MERGE_STALE_TICKS="3",
    )
    assert out.returncode == 0
    assert _comments(tmp_path) == []


def test_stale_queue_entry_is_cleared_and_re_enqueued_within_n_ticks(tmp_path):
    out = _run(
        tmp_path,
        'for _ in 1 2 3; do merge_pr 1067 "merging." || true; done',
        FAKE_PR_STATE="OPEN|0",
        MERGE_STALE_TICKS="2",
    )
    disable = [c for c in _calls(tmp_path) if "--disable-auto" in c]
    assert len(disable) == 1, "the dangling entry must be cleared exactly once at the budget"
    assert "clearing the dangling auto-merge/queue state" in out.stdout
    # …and the recovery re-asks for the merge, then restarts the budget.
    assert len([c for c in _calls(tmp_path) if c.startswith("gh pr merge --auto")]) == 3
    assert (tmp_path / "state" / "mergestall-1067.count").read_text().strip() == "1"


def test_armed_without_a_queue_entry_still_counts_toward_the_stall_budget(tmp_path):
    """Auto-merge set but nothing queued is indistinguishable from a dangling entry."""
    out = _run(
        tmp_path,
        'merge_pr 1067 "merging." || true',
        FAKE_PR_STATE="OPEN|1",
        FAKE_QUEUE="",
        MERGE_VERIFY_TRIES="2",
    )
    assert "auto-merge armed" in out.stdout
    # …but only after the poll gave the queue entry its chance to appear.
    assert len([c for c in _calls(tmp_path) if c.startswith("sleep")]) == 1
    assert (tmp_path / "state" / "mergestall-1067.count").read_text().strip() == "1"


def test_merge_pr_leaves_a_closed_pr_alone(tmp_path):
    out = _run(
        tmp_path,
        'merge_pr 1067 "merging."; echo "rc=$?"; echo "$TICK_OUTCOME/$TICK_REASON"',
        FAKE_PR_STATE="CLOSED|0",
    )
    assert "rc=1" in out.stdout
    assert "skipped/merge_pr_closed" in out.stdout
    assert _comments(tmp_path) == []


def test_merge_pr_is_inert_in_dry_run(tmp_path):
    out = _run(tmp_path, 'merge_pr 1067 "merging."; echo "rc=$?"', DRY_RUN="1")
    assert "rc=0" in out.stdout
    assert _calls(tmp_path) == []


def test_merge_pr_does_not_poll_forever_when_state_never_resolves(tmp_path):
    """Bounded by MERGE_VERIFY_TRIES so one wedged PR can't eat the tick."""
    _run(
        tmp_path,
        'merge_pr 1067 "merging." || true',
        FAKE_PR_STATE="OPEN|0",
        MERGE_VERIFY_TRIES="3",
    )
    assert len([c for c in _calls(tmp_path) if c.startswith("sleep")]) == 2


# --- structural: every merge site goes through merge_pr -----------------------------------------

def test_tick_sh_is_syntactically_valid():
    subprocess.run(["bash", "-n", str(TICK_SH)], check=True)


def test_only_merge_pr_asks_github_to_merge():
    """A second raw call site is how this bug comes back."""
    sites = re.findall(r"^\s*gh pr merge --auto", SOURCE, re.M)
    assert len(sites) == 1
    assert re.search(r"^\s*gh pr merge --auto", BLOCK, re.M)


def test_no_merge_site_pairs_the_exit_code_with_a_comment():
    assert not re.search(r"gh pr merge[^\n]*\n\s*&& gh pr comment", SOURCE)


def test_all_three_merge_lanes_call_merge_pr():
    assert len(re.findall(r"^\s*merge_pr \"\$", SOURCE, re.M)) == 3
