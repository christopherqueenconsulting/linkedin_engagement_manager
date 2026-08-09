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

# The park's Decision Comment is only useful if the reply it requests reaches route_owner_answer,
# so the shape judge is lifted verbatim too and the two are tested against each other.
ANSWER_VERDICT_BLOCK = re.search(r"^answer_verdict\(\) \{.*?^\}$", SOURCE, re.S | re.M).group(0)

# A stub `gh` covering exactly the reads/writes merge_pr makes, driven by three env vars so each
# test can pose a different GitHub state. Every invocation is appended to $BASE/calls.
HARNESS = """
set -uo pipefail
BASE="__BASE__"
SLUG="o/n"; OWNER="o"; NAME="n"
ASSIGNEE="owner-login"
DRY_RUN="${DRY_RUN:-0}"
TICK_OUTCOME="dispatched"; TICK_REASON=""; TICK_PR=""
mkdir -p "$BASE/state"
: > "$BASE/calls"
log() { echo "LOG: $*"; }
sleep() { printf 'sleep %s\\n' "$*" >> "$BASE/calls"; }
# Defined ABOVE the block in tick.sh (line ~440), so the park path can call it.
issue_for_pr() { echo "${FAKE_ISSUE:-}"; }
gh() {
  printf 'gh %s\\n' "$*" >> "$BASE/calls"
  case "$*" in
    "run list"*)                       echo "${FAKE_RUN_URL:-}" ;;
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

# Anything that changes GitHub state. A read is not a mutation — DRY_RUN may read.
_MUTATIONS = ("gh pr merge", "gh pr comment", "gh pr edit", "gh pr ready", "gh issue edit")


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


def _park_comments(tmp_path: Path) -> list[str]:
    return [c for c in _comments(tmp_path) if "Human decision needed" in c]


def _merging_comments(tmp_path: Path) -> list[str]:
    return [c for c in _comments(tmp_path) if c.endswith("--body merging.")]


def _mutations(tmp_path: Path) -> list[str]:
    return [c for c in _calls(tmp_path) if c.startswith(_MUTATIONS)]


# --- classify_merge_state: the pure core -------------------------------------------------------

@pytest.mark.parametrize(
    "pr_state,queue,armed,expected",
    [
        ("MERGED", "", "0", "merged"),
        ("MERGED", "QUEUED", "1", "merged"),
        ("CLOSED", "", "0", "closed"),
        ("OPEN", "QUEUED", "0", "queued"),
        ("OPEN", "AWAITING_CHECKS", "1", "queued"),
        ("OPEN", "LOCKED", "1", "queued"),
        ("OPEN", "UNMERGEABLE", "1", "unmergeable"),
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


# --- queue budget: requests counted per head SHA ------------------------------------------------

def test_attempt_counter_is_per_head_sha(tmp_path):
    """A new push earns a fresh chance at the queue; nothing else resets the count."""
    out = _run(
        tmp_path,
        """
        merge_attempt_count 1067 sha-a
        merge_attempt_bump 1067 sha-a; merge_attempt_bump 1067 sha-a
        merge_attempt_count 1067 sha-a
        merge_attempt_count 1067 sha-b
        merge_attempt_bump 1067 sha-b; merge_attempt_count 1067 sha-b
        merge_attempt_clear 1067; merge_attempt_count 1067 sha-b
        """,
    )
    assert out.stdout.split() == ["0", "2", "0", "1", "0"]


def test_garbage_attempt_counter_reads_as_zero(tmp_path):
    out = _run(
        tmp_path,
        """
        printf 'junk\\n' > "$BASE/state/mergeattempt-1067"
        merge_attempt_count 1067 sha-a
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
    assert "WAITING IN THE MERGE QUEUE (entry: AWAITING_CHECKS" in out.stdout
    assert len(_comments(tmp_path)) == 1
    assert not (tmp_path / "state" / "mergestall-1067.count").exists()


def test_a_queue_that_keeps_taking_and_dropping_the_pr_is_parked(tmp_path):
    """The #1067 timeline: 154 enqueues, 153 evictions, a live entry at every read.

    Each tick asked for the merge, GitHub created an entry, a merge_group check failed ~3 min
    later and the entry was dropped — so a state read 20s after the request saw AWAITING_CHECKS
    every single time. "There is an entry right now" must therefore NOT be the whole answer, or
    the lane calls 47h of deadlock healthy.

    Detecting it is not enough (#1120 burned 45 requests against a budget of 12 because the
    diagnosis changed no state): a spent budget must PARK the PR and stop re-enqueueing it.
    """
    out = _run(
        tmp_path,
        'for _ in 1 2 3 4 5; do merge_pr 1067 "merging." || true; done; '
        'echo "$TICK_OUTCOME/$TICK_REASON"',
        FAKE_PR_STATE="OPEN|1",
        FAKE_QUEUE="AWAITING_CHECKS",
        MERGE_QUEUE_STUCK_TICKS="4",
    )
    assert "PARKING PR #1067" in out.stdout
    assert "queue took and dropped this head 4 times" in out.stdout
    assert "escalated/merge_parked" in out.stdout
    # The point of the park: the 5th tick does NOT ask the queue again.
    assert len([c for c in _calls(tmp_path) if c.startswith("gh pr merge --auto")]) == 4
    # …and it stays one "merging" comment plus exactly one Decision Comment, not one per tick.
    assert len(_merging_comments(tmp_path)) == 1
    assert len(_park_comments(tmp_path)) == 1


def test_the_park_drafts_the_pr_before_disabling_auto_merge(tmp_path):
    """Draft first: a draft can hold neither auto-merge nor a queue entry.

    A concurrent slot re-arming mid-park then fails closed instead of silently undoing it.
    """
    _run(
        tmp_path,
        'merge_pr 1067 "merging." || true',
        FAKE_PR_STATE="OPEN|1",
        FAKE_QUEUE="UNMERGEABLE",
        FAKE_ISSUE="1200",
    )
    calls = _calls(tmp_path)
    draft = next(i for i, c in enumerate(calls) if c.startswith("gh pr ready 1067") and "--undo" in c)
    disable = next(i for i, c in enumerate(calls) if "--disable-auto" in c)
    assert draft < disable
    labels = next(c for c in calls if c.startswith("gh pr edit 1067") and "--add-label" in c)
    for expected in ("--add-label needs-human", "--add-label agent:blocked",
                     "--add-label agent:merge-parked", "--remove-label agent:working"):
        assert expected in labels
    # The issue mirrors the FULL hold — route_owner_answer un-parks by adding agent:working and
    # removing needs-human + agent:blocked, so a park that left agent:working on makes the PR and
    # the issue disagree about whether the work is in flight.
    iss = next(c for c in calls if c.startswith("gh issue edit 1200"))
    for expected in ("--add-label needs-human", "--add-label agent:blocked",
                     "--remove-label agent:working"):
        assert expected in iss


def test_the_park_comment_asks_for_an_answer_the_answer_lane_can_parse(tmp_path):
    """The park is a one-way trapdoor if the reply it asks for is not a recognised answer.

    `answer_verdict()` accepts `ok` or `<number><letter>` (`1B`) — a BARE letter is deliberately
    rejected, because "B" and "A rebase would be better" are indistinguishable on the first line.
    So the Decision Comment must ask for the numbered form, or the owner follows the instructions,
    the answer lane ignores the reply, and the PR stays parked forever.
    """
    _run(
        tmp_path,
        'merge_pr 1067 "merging." || true',
        FAKE_PR_STATE="OPEN|1",
        FAKE_QUEUE="UNMERGEABLE",
    )
    # The stub logs one line per gh arg, so read the RAW file — the body spans several lines.
    body = (tmp_path / "calls").read_text(encoding="utf-8")
    assert len(_park_comments(tmp_path)) == 1
    assert "### 1." in body                       # the question is numbered, so `1B` names an option
    assert re.search(r"e\.g\.\s*`1B`", body)
    assert "**My recommendation: `1B`.**" in body
    # The detection key the answer lane matches on must survive any rewording.
    assert "Human decision needed" in body


def test_the_answer_lane_accepts_the_reply_the_park_comment_asks_for():
    """Bind the two halves: whatever the comment tells the owner to type must parse as an answer."""
    verdict = subprocess.run(
        ["bash", "-c", ANSWER_VERDICT_BLOCK + '\nanswer_verdict "1B"'],
        capture_output=True, text=True, env={"PATH": "/usr/bin:/bin"},
    )
    assert verdict.stdout.strip() == "answer"


def test_a_healthy_queue_wait_is_never_called_stuck(tmp_path):
    """A slow merge queue is normal; only a spent budget is a diagnosis."""
    out = _run(
        tmp_path,
        'for _ in 1 2 3; do merge_pr 1067 "merging." || true; done; '
        'echo "$TICK_OUTCOME/$TICK_REASON"',
        FAKE_PR_STATE="OPEN|1",
        FAKE_QUEUE="AWAITING_CHECKS",
        MERGE_QUEUE_STUCK_TICKS="12",
    )
    assert "STUCK IN THE MERGE QUEUE" not in out.stdout
    assert "dispatched/" in out.stdout


def test_a_new_push_gives_the_queue_a_fresh_budget(tmp_path):
    out = _run(
        tmp_path,
        """
        for _ in 1 2 3; do merge_pr 1067 "merging." || true; done
        FAKE_SHA=beef5678
        TICK_OUTCOME="dispatched"; TICK_REASON=""   # each tick is its own process
        merge_pr 1067 "merging." || true
        echo "$TICK_OUTCOME/$TICK_REASON"
        """,
        FAKE_PR_STATE="OPEN|1",
        FAKE_QUEUE="AWAITING_CHECKS",
        MERGE_QUEUE_STUCK_TICKS="3",
    )
    assert "PARKING PR #1067" in out.stdout                  # the 3rd request at the old head
    assert "request 1/3 at this head" in out.stdout          # …the push earned a fresh budget
    assert out.stdout.strip().endswith("dispatched/")
    assert (tmp_path / "state" / "mergeattempt-1067").read_text().split() == ["beef5678", "1"]


def test_an_unmergeable_entry_is_not_progress(tmp_path):
    """GitHub has judged the head: the merge_group run failed. Re-requesting cannot fix that."""
    out = _run(
        tmp_path,
        'merge_pr 1067 "merging."; echo "rc=$?"; echo "$TICK_OUTCOME/$TICK_REASON"',
        FAKE_PR_STATE="OPEN|1",
        FAKE_QUEUE="UNMERGEABLE",
    )
    assert "rc=1" in out.stdout
    assert "escalated/merge_parked" in out.stdout
    assert "PARKING PR #1067 (merge-queue entry UNMERGEABLE" in out.stdout
    assert _merging_comments(tmp_path) == []  # never claim "merging" over a failing merge_group
    assert len(_park_comments(tmp_path)) == 1


def test_the_park_comment_carries_the_failing_merge_group_run(tmp_path):
    """The owner's first question is "which check?" — answer it in the Decision Comment."""
    out = _run(
        tmp_path,
        'merge_pr 1067 "merging." || true',
        FAKE_PR_STATE="OPEN|1",
        FAKE_QUEUE="UNMERGEABLE",
        FAKE_RUN_URL="https://example.test/run/9",
    )
    assert "https://example.test/run/9" in "\n".join(_calls(tmp_path))
    assert out.returncode == 0


def test_an_unreadable_merge_group_run_still_parks(tmp_path):
    """The link is best-effort; an unreadable Actions list must never block the park."""
    _run(
        tmp_path,
        'merge_pr 1067 "merging." || true',
        FAKE_PR_STATE="OPEN|1",
        FAKE_QUEUE="UNMERGEABLE",
        FAKE_RUN_URL="",
    )
    assert len(_park_comments(tmp_path)) == 1
    assert "not readable" in "\n".join(_calls(tmp_path))


def test_the_merge_request_is_always_repo_scoped(tmp_path):
    """$BASE is not a git checkout — a bare PR number would resolve against the wrong repo."""
    _run(tmp_path, 'merge_pr 1067 "merging." || true', FAKE_PR_STATE="MERGED|0")
    asked = [c for c in _calls(tmp_path) if c.startswith("gh pr merge")]
    assert asked and all("--repo o/n" in c for c in asked)


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
    """561 identical "merging" comments is the bug; ten stalled ticks must produce zero.

    The stall recovery gets its own budget: cycles 1 and 2 clear the dangling state and re-ask,
    the third exhaustion parks — one Decision Comment, never a stream of them.
    """
    out = _run(
        tmp_path,
        'for _ in 1 2 3 4 5 6 7 8 9 10; do merge_pr 1067 "merging." || true; done',
        FAKE_PR_STATE="OPEN|0",
        MERGE_STALE_TICKS="3",
    )
    assert out.returncode == 0
    assert _merging_comments(tmp_path) == []
    assert len(_park_comments(tmp_path)) == 1
    assert "after 3 disable-auto recovery cycles" in out.stdout


def test_the_stall_recovery_gets_its_cycles_before_parking(tmp_path):
    """One recovery cycle is the automated fix that unwedged #1067 — don't park ahead of it."""
    out = _run(
        tmp_path,
        'for _ in 1 2 3 4; do merge_pr 1067 "merging." || true; done',
        FAKE_PR_STATE="OPEN|0",
        MERGE_STALE_TICKS="3",
        MERGE_STALL_CYCLES_MAX="2",
    )
    assert "recovery cycle 1/2" in out.stdout
    assert "PARKING" not in out.stdout


def test_a_new_push_gives_the_stall_recovery_a_fresh_budget(tmp_path):
    """The recovery allowance is HEAD-scoped, exactly like the queue budget.

    "The recovery does not work HERE" is a claim about a specific head. A lifetime counter would
    spend both cycles early, and then a PR that was fixed and pushed would park on its very FIRST
    stall at the new head — a park no human action can be expected to pre-empt.
    """
    out = _run(
        tmp_path,
        """
        printf 'dead0000 2\\n' > "$BASE/state/mergestallcycle-1067.count"
        printf '2\\n'          > "$BASE/state/mergestall-1067.count"
        merge_pr 1067 "merging." || true
        """,
        FAKE_SHA="beef5678",
        FAKE_PR_STATE="OPEN|0",
        MERGE_STALE_TICKS="2",
        MERGE_STALL_CYCLES_MAX="2",
    )
    assert "recovery cycle 1/2" in out.stdout
    assert "PARKING" not in out.stdout
    assert (tmp_path / "state" / "mergestallcycle-1067.count").read_text().split() == ["beef5678", "1"]


def test_a_pre_existing_plain_integer_cycle_record_is_lenient_not_a_park(tmp_path):
    """State written before the record gained a sha must never park a PR on sight."""
    out = _run(
        tmp_path,
        """
        printf '9\\n' > "$BASE/state/mergestallcycle-1067.count"
        printf '2\\n' > "$BASE/state/mergestall-1067.count"
        merge_pr 1067 "merging." || true
        """,
        FAKE_PR_STATE="OPEN|0",
        MERGE_STALE_TICKS="2",
        MERGE_STALL_CYCLES_MAX="2",
    )
    assert "recovery cycle 1/2" in out.stdout
    assert "PARKING" not in out.stdout


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


def test_merge_pr_mutates_nothing_in_dry_run(tmp_path):
    """A dry tick READS the head (the budget check needs it) but changes nothing on GitHub."""
    out = _run(tmp_path, 'merge_pr 1067 "merging."; echo "rc=$?"', DRY_RUN="1")
    assert "rc=0" in out.stdout
    assert _mutations(tmp_path) == []
    assert _calls(tmp_path) == ["gh pr view 1067 --repo o/n --json headRefOid --jq .headRefOid"]


def test_dry_run_decides_the_park_but_never_performs_it(tmp_path):
    """The budget check sits above the DRY_RUN return, so a dry tick proves the park path."""
    out = _run(
        tmp_path,
        """
        printf 'cafe1234 9\\n' > "$BASE/state/mergeattempt-1067"
        merge_pr 1067 "merging."; echo "rc=$?"
        """,
        DRY_RUN="1",
        MERGE_QUEUE_STUCK_TICKS="4",
    )
    assert "rc=1" in out.stdout
    assert "would park PR #1067" in out.stdout
    assert _mutations(tmp_path) == []


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


def test_no_merge_call_site_ends_the_tick_unconditionally():
    """Head-of-line: a PR that made no merge progress must yield to the next candidate.

    The old `merge_pr …` + unconditional `exit 0` burned 45 of 62 ticks in one 6h window
    re-polling ONE wedged PR while green PRs and 34 ready issues waited.
    """
    assert not re.search(r"^\s*merge_pr \"\$[^\n]*\n\s*exit 0", SOURCE, re.M)
    assert len(re.findall(r"^\s*merge_pr \"\$[^\n]*&& exit 0", SOURCE, re.M)) == 3


def test_the_park_label_is_bootstrapped():
    """A label that does not exist fails the whole `gh pr edit` — the #1228 trap.

    That would silently undo the park (labels unchanged, PR left draft and unlabelled).
    """
    labels_sh = TICK_SH.parent / "lib" / "labels.sh"
    assert "agent:merge-parked" in labels_sh.read_text(encoding="utf-8")
