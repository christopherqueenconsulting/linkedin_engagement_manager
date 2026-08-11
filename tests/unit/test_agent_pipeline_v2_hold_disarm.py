"""A human hold must take auto-merge off the PR (#1387).

`park.sh` disables auto-merge as step two of parking, and for a long time that was the only place it
happened. So a hold the PIPELINE applied was safe and a hold a HUMAN applied was not: adding
`needs-human` to an armed PR is the natural "stop, I want to look at this" gesture, `decide()`
honoured it by refusing to act, and GitHub merged the PR anyway the moment its gate cleared.

The hold was respected by everything except the thing that actually merges.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_V2 = _ROOT / "scripts" / "agent-pipeline" / "v2"
sys.path.insert(0, str(_V2))

from lemd import answers, github, observe  # noqa: E402

TTLS = dict(ttl_ci=1800, ttl_review=3600, ttl_queue=900, ttl_parked=21600)
GREEN = github.ChecksState(failed=0, pending=0, total=6)


def held(**kw) -> observe.Snapshot:
    """A PR carrying a human hold, green and reviewed underneath."""
    base = dict(kind="pr", number=1, labels=frozenset({"agent:working", "needs-human"}),
                state="OPEN", branch="feature/x", head_sha="abc", merge_state="CLEAN",
                checks=GREEN, review_fresh=True)
    base.update(kw)
    return observe.Snapshot(**base)


def d(snap):
    """Run the decision under standard TTLs."""
    return observe.decide(snap, **TTLS)


# ---------------------------------------------------------------- the decision


def test_a_hold_on_an_armed_pr_disarms_it():
    """The defect. Everything else in the ladder is irrelevant while the arm is live."""
    got = d(held(auto_merge=True))
    assert (got.action, got.mode, got.reason) == (observe.ACT_DISARM, "disarm", "human_hold_armed")


def test_a_hold_on_an_unarmed_pr_is_an_ordinary_hold():
    """No arm, nothing to take off — the hold behaves exactly as before."""
    got = d(held(auto_merge=False))
    assert (got.action, got.reason) == (observe.ACT_NONE, "human_hold")


def test_disarming_outranks_even_an_actionable_answer():
    """Safety above the un-park, and the ordering is the argument.

    Un-parking one observation later costs a single pass. Leaving an armed hold costs a merge nobody
    authorised, and that cannot be undone — so the arm comes off first and the answer is read on the
    next observation, when the PR can no longer merge underneath it.
    """
    got = d(held(auto_merge=True, answer=answers.Answer("a1", "answer", "1B")))
    assert got.action == observe.ACT_DISARM


def test_disarming_does_not_consume_the_owners_answer():
    """The answer must still be there to route once the arm is off.

    `daemon._observe_one` only stashes an answer id on the ACT_UNPARK branch, so a disarm cannot
    mark it spent. Asserted through the decision because that is what the daemon keys on.
    """
    ans = answers.Answer("a1", "answer", "1B")
    got = d(held(auto_merge=True, answer=ans))
    assert got.action == observe.ACT_DISARM
    # ...and with the arm gone, the very same snapshot routes the answer.
    assert d(held(auto_merge=False, answer=ans)).action == observe.ACT_UNPARK


def test_an_armed_issue_is_not_a_thing():
    """`auto_merge` is meaningless on an issue; the branch must not fire for one."""
    snap = observe.Snapshot(kind="issue", number=7, labels=frozenset({"needs-human"}),
                            auto_merge=True)
    assert d(snap).action == observe.ACT_NONE


def test_a_merged_pr_is_still_terminal():
    """Terminal facts outrank the disarm, as they outrank every hold."""
    assert d(held(auto_merge=True, state="MERGED")).action == observe.ACT_CLOSE


def test_an_unreadable_snapshot_never_disarms():
    """`readable=False` is a decision to do nothing — the #1082 rule, unchanged."""
    assert d(held(auto_merge=True, readable=False)).action == observe.ACT_NONE


def test_the_disarm_keeps_the_park_reason():
    """The item is still the owner's; only the arm changed."""
    assert d(held(auto_merge=True)).park_reason == "needs_human"


# ---------------------------------------------------------------- the action


@pytest.fixture
def action_tree(tmp_path: Path):
    """A scratch tree holding `disarm.sh` next to a STUBBED `common.sh`.

    `common.sh` hard-sets `PATH` (so `claude` resolves under systemd), which discards any stub
    binary a test puts in front of it — so the usual "fake `gh` on PATH" trick cannot reach these
    actions. Stubbing the bootstrap instead tests exactly what `disarm.sh` itself decides, with the
    contract it depends on made explicit. `test_the_stub_matches_the_real_bootstrap` below keeps the
    two from drifting.
    """
    actions = tmp_path / "v2" / "actions"
    actions.mkdir(parents=True)
    (tmp_path / "lib").mkdir()
    (tmp_path / "state").mkdir()
    calls = tmp_path / "calls.txt"
    labels = tmp_path / "labels.txt"
    labels.write_text("needs-human agent:blocked")

    (actions / "gh").write_text(
        "#!/usr/bin/env bash\n"
        f'echo "$@" >> {calls}\n'
        'case "$*" in\n'
        f'  *"--json labels"*) cat {labels} ;;\n'
        '  *"--json headRefOid"*) echo "abc123def456" ;;\n'
        'esac\nexit 0\n'
    )
    (actions / "gh").chmod(0o755)

    (actions / "common.sh").write_text(
        "set -uo pipefail\n"
        f'BASE="{tmp_path}"\nSLUG="o/r"\nDRY_RUN="${{DRY_RUN:-0}}"\n'
        f'export PATH="{actions}:$PATH"\n'
        "EX_TRUST=70; EX_BUDGET=71; EX_BUSY=72; EX_SETUP=73\n"
        'log() { echo "[v2/${V2_ACTION:-action}] $*"; }\n'
        'v2_paused() { [ -f "$BASE/PAUSED" ]; }\n'
        "v2_hold_present() {\n"
        '  local l; l="$(gh pr view "$2" --repo "$SLUG" --json labels --jq . 2>/dev/null)"\n'
        '  [ -n "$l" ] || { log "#$2 labels unreadable — treating as held."; return 0; }\n'
        '  case " $l " in *" needs-human "*|*" agent:blocked "*) return 0 ;; esac\n'
        "  return 1\n}\n"
        f'. "{_ROOT}/scripts/agent-pipeline/lib/ledger.sh"\n'
    )
    (actions / "disarm.sh").write_text((_V2 / "actions" / "disarm.sh").read_text())
    (actions / "disarm.sh").chmod(0o755)
    return tmp_path, actions, calls, labels


def _run(tree, *args: str):
    """Run the copied `disarm.sh` from the scratch tree."""
    tmp_path, actions, _, _ = tree
    return subprocess.run(
        ["bash", str(actions / "disarm.sh"), *args],
        capture_output=True, text=True, timeout=30,
        env={"PATH": f"{actions}:/usr/bin:/bin", "BASE": str(tmp_path), "HOME": str(tmp_path)},
    )


def test_the_action_disables_auto_merge(action_tree):
    """The one mutation it exists to make."""
    got = _run(action_tree, "42")
    assert got.returncode == 0, got.stdout + got.stderr
    assert "pr merge 42 --repo o/r --disable-auto" in action_tree[2].read_text()


def test_the_action_refuses_when_the_hold_has_gone(action_tree):
    """A hold lifted since the daemon's snapshot means the owner wants it to land.

    Taking the arm off then would be the pipeline fighting the person it is supposed to obey. Note
    the label set is READABLE and merely hold-free: an empty read is treated as held, deliberately.
    """
    action_tree[3].write_text("agent:working")
    got = _run(action_tree, "42")
    assert got.returncode == 0
    assert "not ours to disarm" in got.stdout
    assert "--disable-auto" not in action_tree[2].read_text()


def test_the_action_is_bounded_per_head(action_tree):
    """`--disable-auto` exits 0 on a PR that was never armed, so this cannot be unbounded.

    Without a meter, one mis-read of `autoMergeRequest` re-dispatches the action for ever.
    """
    for _ in range(3):
        assert _run(action_tree, "42").returncode == 0
    spent = _run(action_tree, "42")
    assert spent.returncode == 71, spent.stdout          # EX_BUDGET
    assert "refusing to loop" in spent.stdout


def test_a_paused_pipeline_disarms_nothing(action_tree):
    """PAUSED stops everything, this included."""
    (action_tree[0] / "PAUSED").touch()
    assert _run(action_tree, "42").returncode == 70       # EX_TRUST


def test_the_stub_matches_the_real_bootstrap():
    """Every name the stubbed `common.sh` provides must exist in the real one.

    A stub that has drifted tests nothing. This is the cheap half of that guarantee: the real
    bootstrap must still define each function and constant the stub stands in for.
    """
    real = (_V2 / "actions" / "common.sh").read_text()
    for name in ("v2_paused()", "v2_hold_present()", "log()",
                 "EX_TRUST=", "EX_BUDGET=", "EX_BUSY=", "EX_SETUP="):
        assert name in real, f"the stub provides {name}, the real common.sh no longer does"


def test_the_pool_sets_agree_across_all_three_readers():
    """A gh action mis-classified as an agent action steals a scarce agent slot across restarts.

    `daemon.act()` routes it, `dispatch.in_pool()` counts it after a restart, and they are edited in
    different files — exactly the drift that already put `unpark` in the wrong pool in `status.sh`.
    """
    import re as _re
    daemon_src = (_V2 / "lemd" / "daemon.py").read_text()
    dispatch_src = (_V2 / "lemd" / "dispatch.py").read_text()
    # Membership, not the literal tuple. Pinning it made every legitimate new gh mode look like a
    # regression — `abandon` (#1390) was the second one — which trains people to edit the test
    # rather than read it. What matters is that `disarm` is classified the same way in both.
    act = _re.search(r'"gh" if mode in \(([^)]*)\)', daemon_src).group(1)
    pool = _re.search(r'"gh" if row\["mode"\] in \(([^)]*)\)', dispatch_src).group(1)
    assert '"disarm"' in act and '"disarm"' in pool


def test_the_action_ships_to_the_box():
    """A new action not in `install.sh`'s file set silently never reaches the VPS.

    `v2/actions/*.sh` is globbed, so this passes today — the assertion exists so that a future move
    out of that directory fails here instead of in production.
    """
    script = _V2 / "actions" / "disarm.sh"
    assert script.is_file() and script.stat().st_mode & 0o111
    installer = (_ROOT / "scripts" / "agent-pipeline" / "install.sh").read_text()
    assert 'v2/actions/*.sh' in installer
