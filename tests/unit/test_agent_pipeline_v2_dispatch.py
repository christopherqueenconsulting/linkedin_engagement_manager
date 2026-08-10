"""Tests for the v2 execution path: budgets, pools, refusal vocabulary and the failsafe.

These cover the parts where a mistake is expensive rather than merely wrong. Three themes:

* **The budget cannot be read two ways.** `lib/ledger.sh` writes it and `policy.py` reads it, and
  the parity test below generates files with the SHELL and reads them back in Python. Two
  implementations of one format is a drift hazard; the answer is a test that fails when they
  disagree, not a comment asking future readers to be careful.
* **A refusal is not a failure.** The actions return distinct codes for "you may not", "no budget
  left", "someone else holds the branch" and "setup broke". v1 collapsed all of those into non-zero
  and retried each of them every five minutes.
* **Two dispatchers must be impossible.** The failsafe tick stands down while the daemon's
  heartbeat is fresh, and an unreadable heartbeat reads as stale — the safe direction.
"""

from __future__ import annotations

import fcntl
import os
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_PIPE = _ROOT / "scripts" / "agent-pipeline"
_V2 = _PIPE / "v2"
sys.path.insert(0, str(_V2))

from lemd import db, dispatch, policy  # noqa: E402

# --------------------------------------------------------------------------- budgets

def _charge(base: Path, kind: str, number: int, mode: str, key: str = "-") -> str:
    """Charge one run through the REAL shell ledger, so parity is tested against the writer."""
    script = f'BASE="{base}"; . "{_PIPE}/lib/ledger.sh"; ledger_charge {kind} {number} {mode} {key}'
    out = subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=True)
    return out.stdout.strip()


def test_python_reader_agrees_with_the_shell_ledger(tmp_path):
    """The one format, read by two languages, must produce one number.

    A divergence here does not look like a bug: it looks like a PR parked at two attempts while
    another retries forever, which is precisely the failure that made the TSV the single budget
    store in the first place.
    """
    for expected in ("1", "2", "3"):
        assert _charge(tmp_path, "pr", 42, "fix") == expected
        assert policy.ledger_count(tmp_path, "pr", 42, "fix") == int(expected)


def test_a_rotated_reset_key_is_the_reset(tmp_path):
    """A budget renews on an event the agent cannot produce, and the key rotation IS that event."""
    _charge(tmp_path, "pr", 7, "merge", "sha-aaa")
    _charge(tmp_path, "pr", 7, "merge", "sha-aaa")
    assert policy.ledger_count(tmp_path, "pr", 7, "merge", "sha-aaa") == 2
    # A new head is a genuinely new question for the merge QUEUE — and only for it.
    assert policy.ledger_count(tmp_path, "pr", 7, "merge", "sha-bbb") == 0


def test_only_merge_is_keyed_per_head():
    """Per-head keys refill the meter on the agent's own commits (finding H1).

    Agents push heads. Keying `fix` per head means four attempts become unlimited attempts, which
    measured out at eight to ten run-hours on one item. The merge lane is the sole exception because
    the queue is what judges heads.
    """
    assert policy.PER_HEAD_MODES == frozenset({"merge"})


def test_missing_ledger_reads_as_zero_not_as_exhausted(tmp_path):
    """An absent file means "no runs yet", never "no runs left" — the pipeline must start."""
    assert policy.ledger_count(tmp_path, "issue", 999, "start") == 0
    assert not policy.exhausted(tmp_path, "issue", 999, "start")


def test_a_corrupt_count_reads_as_zero(tmp_path):
    """Garbage must not become a huge number that parks an item forever."""
    led = policy.ledger_path(tmp_path, "pr", 5)
    led.parent.mkdir(parents=True, exist_ok=True)
    led.write_text("fix\tNOT_A_NUMBER\t123\t-\n")
    assert policy.ledger_count(tmp_path, "pr", 5, "fix") == 0


def test_timeouts_stretch_under_contention_but_stay_bounded():
    """CPU starvation must not be charged to the item as a failed attempt (M6)."""
    idle = policy.timeout_for("fix", occupancy=0.0)
    busy = policy.timeout_for("fix", occupancy=1.0)
    assert idle == policy.MODE_TIMEOUT["fix"]
    assert busy == int(idle * 1.5)
    # An out-of-range occupancy must not produce an unbounded deadline.
    assert policy.timeout_for("fix", occupancy=9.0) == busy


# --------------------------------------------------------------------------- refusals

def test_refusal_codes_are_distinct_from_each_other():
    """Each refusal has a different remedy, so each needs a different code.

    A trust refusal waits for a human; a budget exhaustion parks; a busy branch retries in seconds;
    a setup failure is a box problem. Collapsing them is how v1 retried a refused item forever.
    """
    codes = {dispatch.EX_TRUST, dispatch.EX_BUDGET, dispatch.EX_BUSY, dispatch.EX_SETUP}
    assert len(codes) == 4
    assert codes == set(dispatch.REFUSALS)


def test_action_scripts_export_the_same_codes():
    """The bash side and the Python side must not drift on the vocabulary they speak."""
    text = (_V2 / "actions" / "common.sh").read_text()
    for name, value in (("EX_TRUST", dispatch.EX_TRUST), ("EX_BUDGET", dispatch.EX_BUDGET),
                        ("EX_BUSY", dispatch.EX_BUSY), ("EX_SETUP", dispatch.EX_SETUP)):
        assert f"{name}={value}" in text


# --------------------------------------------------------------------------- pools

class _Cfg:
    """Just enough config for the supervisor."""

    def __init__(self, tmp_path):
        self.base = tmp_path
        self.repo = tmp_path
        self.slug = "o/r"
        self.max_agents = 3
        self.gh_slots = 2


def test_gh_work_never_queues_behind_agent_work(tmp_path):
    """The two pools are independent — that is why there are two of them.

    A merge-enable is two seconds of API call. Making it wait for a twenty-minute implementation run
    is a large part of how v1 turned a 3.8-minute merge queue into hours of latency.
    """
    conn = db.connect(tmp_path / "q.db")
    sup = dispatch.Supervisor(_Cfg(tmp_path), conn)
    sup.children = [
        dispatch.Child(proc=None, pool="agent", mode="fix", kind="pr", number=i, item_id=None,
                       run_id=None, deadline=0, started=0)
        for i in range(3)
    ]
    assert sup.free("agent") == 0
    assert sup.free("gh") == 2
    assert sup.occupancy == 1.0


# --------------------------------------------------------------------------- failsafe

def test_failsafe_gates_on_the_heartbeat_and_never_on_paused():
    """PAUSED must not be the cutover switch (finding C3).

    tick.sh exits unconditionally on PAUSED, so using it to retire v1 would have disabled the
    safety net at the exact moment it was needed. Cutover sets V1_RETIRED instead, and PAUSED keeps
    meaning "a human said stop everything" in both worlds.
    """
    text = (_PIPE / "tick.sh").read_text()
    assert "V1_RETIRED" in text
    assert "--failsafe" in text
    # The heartbeat gate must default to STALE when unreadable, or a lost file silently disarms it.
    assert 'case "$_hb" in \'\'|*[!0-9]*) _hb=0 ;; esac' in text


def test_a_normal_tick_stands_down_once_v1_is_retired():
    """Only a --failsafe tick may run after cutover; two dispatchers is the forbidden state."""
    text = (_PIPE / "tick.sh").read_text()
    idx = text.index("V1_RETIRED_FILE=")
    block = text[idx:idx + 2000]
    assert 'TICK_REASON="v1_retired"' in block
    assert 'if [ "$FAILSAFE" != "1" ]' in block


# --------------------------------------------------------------------------- guards extraction

def test_the_trust_boundary_lives_in_exactly_one_file():
    """v1 and v2 must run the same bytes, not two ports of the same intent."""
    guards = (_PIPE / "lib" / "guards.sh").read_text()
    tick = (_PIPE / "tick.sh").read_text()
    for fn in ("author_trusted()", "label_actor_trusted()", "pr_is_upstream()", "add_worktree()"):
        assert f"\n{fn}" in guards, f"{fn} must live in lib/guards.sh"
        assert f"\n{fn}" not in tick, f"{fn} must NOT be redefined in tick.sh"


def test_guards_are_sourced_strictly_by_both_runners():
    """`|| true` here would delete the trust boundary while every log line still looked normal."""
    tick = (_PIPE / "tick.sh").read_text()
    assert 'if ! . "$BASE/lib/guards.sh"; then' in tick
    common = (_V2 / "actions" / "common.sh").read_text()
    assert 'if ! . "$BASE/lib/guards.sh"; then' in common


@pytest.mark.parametrize("script", ["common.sh", "agent_run.sh", "merge_enable.sh", "park.sh"])
def test_actions_are_syntactically_valid_and_executable(script):
    """A syntax error in an action is a scheduler that decides correctly and can never act."""
    path = _V2 / "actions" / script
    assert os.access(path, os.X_OK), f"{script} must be executable"
    subprocess.run(["bash", "-n", str(path)], check=True)


def test_the_installer_ships_the_actions():
    """A daemon installed without its actions looks exactly like an idle pipeline."""
    assert 'v2/actions/' in (_PIPE / "install.sh").read_text()


def test_park_is_draft_first():
    """A draft cannot hold auto-merge, so drafting first makes a racing re-arm fail closed (H6)."""
    # Anchored past the header, which explains the ordering and would otherwise match first.
    text = (_V2 / "actions" / "park.sh").read_text()
    body = text[text.index('log "PARKING'):]
    assert body.index("gh pr ready") < body.index("--disable-auto") \
        < body.index('--add-label "needs-human"')


def test_merge_budget_is_checked_before_the_enqueue_request():
    """v1's order was inverted: it re-enqueued first and checked the budget after (#1120, 45 requests)."""
    text = (_V2 / "actions" / "merge_enable.sh").read_text()
    assert text.index("MERGE BUDGET") < text.index("--auto --squash")


# --------------------------------------------------------------------------- WIP gate

def test_wip_counts_waiting_prs_not_just_running_ones(tmp_path):
    """Starts stay coupled to merge THROUGHPUT, which is the point of the gate.

    A PR in the merge queue is unfinished work. Counting only `running` items would let the
    scheduler open a PR for every ready issue the moment the agents finished writing them — 35 open
    PRs against a queue that merges one at a time, each one a rebase candidate as soon as main moves.
    """
    conn = db.connect(tmp_path / "q.db")
    for n, state in ((1, db.STATE_RUNNING), (2, db.STATE_WAIT_CI), (3, db.STATE_WAIT_QUEUE),
                     (4, db.STATE_WAIT_REVIEW), (5, db.STATE_CLAIMED)):
        db.upsert_item(conn, kind="pr", number=n, state=state)
    # Parked and merged PRs are NOT in flight — the pipeline is not carrying them.
    db.upsert_item(conn, kind="pr", number=6, state=db.STATE_PARKED)
    db.upsert_item(conn, kind="pr", number=7, state=db.STATE_MERGED)
    assert db.wip_count(conn) == 5


def test_ready_issues_do_not_count_against_the_wip_gate(tmp_path):
    """A backlog is a queue, not work in flight — counting it would close the gate forever."""
    conn = db.connect(tmp_path / "q.db")
    for n in range(10):
        db.upsert_item(conn, kind="issue", number=100 + n, state=db.STATE_READY,
                       pending_mode="start")
    assert db.wip_count(conn) == 0


# --------------------------------------------------------------------------- coexistence

def test_a_live_v1_tick_holds_back_the_agent_pool(tmp_path):
    """Cutover flips a sentinel; a tick already in flight runs for up to 45 minutes past it (M10).

    Correctness does not rest on this — both runners take the same per-branch flock — but adding
    three concurrent agents on top of three already running is twice the envelope this box has been
    measured at, and "wait for the slot locks" must be code rather than an operator's memory.
    """
    locks = tmp_path / "locks"
    locks.mkdir()
    lock = locks / "slot-1.lock"
    lock.write_text("")
    conn = db.connect(tmp_path / "q.db")
    sup = dispatch.Supervisor(_Cfg(tmp_path), conn)
    assert sup.v1_slots_busy() == 0

    fd = os.open(lock, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert sup.v1_slots_busy() == 1
    finally:
        os.close(fd)
    # Released: the daemon may take the agent pool again without an operator doing anything.
    assert sup.v1_slots_busy() == 0


def test_v2_trusts_the_app_bot_as_a_labeller_exactly_as_v1_does(tmp_path):
    """v1 and v2 must not disagree about who may mint `agent:ready`.

    The runner re-applies that label itself — the stale-claim reaper, the answered-Decision-Comment
    requeue, and every phasefix follow-up issue. Under the PAT those writes were the owner's; under
    the App they are the bot's, and tick.sh adds the bot to the allowlist for exactly that reason.

    v2 omitting it did not fail safe, it failed silently DIFFERENT: the same issue read as workable
    to v1 and untrusted to v2, so a rollback would quietly resurrect work v2 had written off.
    Measured live on issue #1292 within minutes of cutover.
    """
    tick = (_PIPE / "tick.sh").read_text()
    common = (_V2 / "actions" / "common.sh").read_text()
    grant = 'AGENT_LABEL_TRUSTED_ACTORS="$AGENT_LABEL_TRUSTED_ACTORS $GH_APP_BOT_LOGIN"'
    assert grant in tick
    assert grant in common
    # And both must gate it on the identity actually being active, not merely configured.
    for text in (tick, common):
        assert '[ "${GH_APP_IDENTITY_ACTIVE:-0}" = "1" ] && [ -n "${GH_APP_BOT_LOGIN:-}" ]' in text
