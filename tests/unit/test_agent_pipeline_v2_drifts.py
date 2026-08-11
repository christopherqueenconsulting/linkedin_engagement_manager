"""Five places where two halves of the pipeline disagreed (#1394).

Each is small. Together they made the system lie about itself: a success branch that never ran, a
config value that never arrived, a slot knob nobody read, a pool count that charged the wrong pool,
and two implementations of "did the owner answer" with nothing keeping them in step.

The pattern is always the same — one fact spelled two ways in two files, edited by different people
at different times. So these tests assert the AGREEMENT, not the value.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_PIPELINE = _ROOT / "scripts" / "agent-pipeline"
_V2 = _PIPELINE / "v2"
sys.path.insert(0, str(_V2))

from lemd import answers, daemon as daemon_mod  # noqa: E402

# ---------------------------------------------------------------- 1. merge vs merge_enable


def test_the_merge_success_branch_is_reachable():
    """`collect()` tested `child.mode == "merge"` and children are spawned as `merge_enable`.

    So the branch never ran: every armed PR fell into the generic tail and was re-observed with
    `dirty=1` — exactly the re-observation loop the branch exists to prevent (#1295).
    """
    src = (_V2 / "lemd" / "daemon.py").read_text()
    assert 'child.mode == "merge"' not in src, "the unreachable literal is back"
    assert "child.mode == GH_MERGE_ACTION" in src
    assert daemon_mod.GH_MERGE_ACTION == "merge_enable"


def test_the_action_name_is_defined_once():
    """One constant, used by both the dispatch and the collect side."""
    src = (_V2 / "lemd" / "daemon.py").read_text()
    # Count CODE only: the comment above the constant quotes the old call site to explain the bug,
    # and a test that cannot tell prose from code would push someone into deleting the explanation.
    code = "\n".join(ln for ln in src.splitlines() if not ln.lstrip().startswith("#"))
    assert code.count('"merge_enable"') == 1, "the spelling is literal in more than one place again"
    assert "action=GH_MERGE_ACTION" in src


def test_an_exhausted_merge_parks_under_the_item_s_own_mode():
    """`merge_enable_exhausted` matches nothing an operator or a budget lookup would search for."""
    assert daemon_mod._item_mode("merge_enable") == "merge"
    assert daemon_mod._item_mode("selfreview") == "selfreview"


# ---------------------------------------------------------------- 2. USAGE_PAUSE_MINUTES


def test_the_pause_minutes_are_forwarded_to_the_lane_script():
    """`common.sh` sources config.env without `export`, and the invocation set only BASE.

    So the box could say 120 while `lane_for.py` used its own default of 60, and the bounded
    self-review wait was computed against the wrong pause length with nothing to show for it.
    """
    src = (_PIPELINE / "v2" / "actions" / "agent_run.sh").read_text()
    # Join line continuations first: the eval spans two lines, so no single line holds both the
    # `eval` and the script path — and matching either one alone finds the `[ -x ... ]` guard or
    # the comment instead of the call.
    joined = src.replace("\\\n", " ")
    invocation = next(ln for ln in joined.splitlines()
                      if "lane_for.py" in ln and ln.lstrip().startswith("eval"))
    assert "USAGE_PAUSE_MINUTES" in invocation, (
        "the lane script is invoked without the pause forwarded to it"
    )


def test_the_pause_is_only_forwarded_when_it_is_set():
    """An EMPTY forward is worse than none: the default applies to an absent var, not an empty one.

    `USAGE_PAUSE_MINUTES=""` would make `int("")` raise inside the lane script.
    """
    src = (_PIPELINE / "v2" / "actions" / "agent_run.sh").read_text()
    assert "${USAGE_PAUSE_MINUTES:+USAGE_PAUSE_MINUTES=" in src


@pytest.mark.parametrize("value,expected", [("120", 120), ("", 60), ("  90 ", 90),
                                            ("garbage", 60), (None, 60)])
def test_the_lane_script_survives_an_unreadable_pause(monkeypatch, value, expected):
    """A crash here would take the lane decision down and leave the run unrouted."""
    sys.path.insert(0, str(_V2))
    import lane_for  # noqa: PLC0415

    if value is None:
        monkeypatch.delenv("USAGE_PAUSE_MINUTES", raising=False)
    else:
        monkeypatch.setenv("USAGE_PAUSE_MINUTES", value)
    assert lane_for._pause_minutes() == expected


# ---------------------------------------------------------------- 3 & 4. status.sh vs the daemon


def _pool_modes(sql_fragment: str) -> set[str]:
    """The mode names inside a `mode IN (...)` clause."""
    return set(re.findall(r"'([a-z_]+)'", sql_fragment))


def test_status_counts_the_same_gh_pool_the_daemon_dispatches():
    """`status.sh` partitioned on ('merge_enable','park') while dispatch classifies four modes.

    An in-flight un-park or disarm was reported against the AGENT pool, which is the scarce one — so
    the number an operator reads to decide whether the pipeline is saturated was wrong in the
    direction that matters.
    """
    status = (_PIPELINE / "status.sh").read_text()
    dispatch = (_V2 / "lemd" / "dispatch.py").read_text()
    gh_clause = next(ln for ln in status.splitlines() if "ended_at IS NULL AND mode IN (" in ln)
    from_status = _pool_modes(gh_clause)
    from_dispatch = set(re.findall(
        r'"([a-z_]+)"', re.search(r'row\["mode"\] in \(([^)]*)\)', dispatch).group(1)))
    assert from_status == from_dispatch, (
        f"status.sh counts {sorted(from_status)} as gh, dispatch.py counts {sorted(from_dispatch)}"
    )


def test_the_agent_pool_clause_is_the_exact_complement():
    """The two clauses must partition the runs table, or a mode is counted twice or not at all."""
    status = (_PIPELINE / "status.sh").read_text()
    gh = _pool_modes(next(ln for ln in status.splitlines()
                          if "ended_at IS NULL AND mode IN (" in ln))
    agent = _pool_modes(next(ln for ln in status.splitlines()
                             if "ended_at IS NULL AND mode NOT IN (" in ln))
    assert gh == agent


def test_status_reads_the_slot_knob_the_daemon_reads():
    """`LEMD_GH_SLOTS` was status.sh's own invention; the daemon reads `MAX_GH_ACTIONS`.

    Neither is in config.env today, so both fall to 2 and agree by luck — the first operator to set
    one would have been misled by the other.
    """
    status = (_PIPELINE / "status.sh").read_text()
    config = (_V2 / "lemd" / "config.py").read_text()
    assert 'MAX_GH_ACTIONS' in config
    assert "'^MAX_GH_ACTIONS='" in status, "status.sh does not read the real knob"


# ---------------------------------------------------------------- 5. the two answer parsers


ANSWER_FIXTURES = [
    ("1B", True),
    ("ok", True),
    ("@claude rebase and retry", True),
    ("thanks, looking at this now", False),
    ("B", False),
]


@pytest.mark.skipif(not shutil.which("jq"), reason="jq is the shell parser's engine")
@pytest.mark.parametrize("body,is_answer", ANSWER_FIXTURES)
def test_both_answer_parsers_agree_on_the_same_thread(tmp_path, body, is_answer):
    """`v2_owner_answered` (jq) and `answers.parse` (Python) implement the same three rules twice.

    They already differed once: the Python parser returns None when the newest owner comment is not
    classifiable as a decision, while the jq only checked authorship. Nothing enforced parity, so
    the daemon and the action could disagree about the same thread — the daemon dispatching an
    un-park that the action then refuses, for ever.

    This pins the half they must share: whether an OWNER answer exists after the Decision Comment.
    """
    thread = [
        {"id": "d1", "body": "## Human decision needed — reply with option letters",
         "author": {"login": "owner"}},
        {"id": "a1", "body": body, "author": {"login": "owner"}},
    ]
    parsed = answers.parse(thread, "owner")

    # The jq half, run exactly as `common.sh` runs it.
    prog = (
        'def isdecision: ((.body // "") | test("Human decision needed"; "i"));\n'
        'def isagent:    ((.body // "") | test("Generated with \\\\[Claude Code\\\\]"));\n'
        '((.comments // []) | to_entries) as $c\n'
        '| (($c | map(select(.value | isdecision)) | last | .key) // -1) as $di\n'
        '| [ $c[] | select(.key > $di) | .value\n'
        '    | select((.author.login // "") == "owner")\n'
        '    | select(isdecision | not) | select(isagent | not) ]\n'
        '| (last // empty) | (.author.login // "")'
    )
    got = subprocess.run(["jq", "-r", prog], input=json.dumps({"comments": thread}),
                         capture_output=True, text=True, timeout=20)
    shell_found_owner = got.stdout.strip() == "owner"

    # The jq answers "is there an owner reply", the Python answers "is it a DECISION". The first is
    # a precondition of the second, so Python saying yes while the shell says no is the divergence
    # that would strand an un-park.
    if parsed is not None:
        assert shell_found_owner, "the daemon would dispatch an un-park the action refuses"
    if is_answer:
        assert parsed is not None and shell_found_owner


# ------------------------------------------------- the park reason the un-park routes on


def test_the_generic_hold_reason_does_not_erase_a_specific_one():
    """#1389 routes an un-park by park reason, and the evidence was being deleted.

    `park.sh` parks a PR as `selfreview_exhausted` and applies the hold labels. The very next
    observation sees those labels, takes the hold branch, reports `needs_human`, and the generic
    write stored that over the top. By the time an owner answered, the only reason left was the
    generic one — which means "a question was asked, apply the answer" — so a budget park still went
    to the revise lane with nothing to apply, spent its two runs and re-parked.

    Caught live: PRs #1368 and #1283 both showed `parked_reason='needs_human'` in the queue while
    their Decision Comments said `selfreview_exhausted`.
    """
    assert daemon_mod._keep_specific_park_reason(
        "selfreview_exhausted", "needs_human") == "selfreview_exhausted"


def test_a_specific_reason_still_replaces_another_specific_one():
    """A genuine change of reason must not be frozen by the guard."""
    assert daemon_mod._keep_specific_park_reason(
        "selfreview_exhausted", "revise_exhausted") == "revise_exhausted"


def test_the_generic_reason_is_kept_when_there_is_nothing_better():
    """A hold a human applied by hand has no prior reason, and `needs_human` is the true one."""
    assert daemon_mod._keep_specific_park_reason(None, "needs_human") == "needs_human"
    assert daemon_mod._keep_specific_park_reason("", "needs_human") == "needs_human"


def test_clearing_the_reason_is_still_possible():
    """An item leaving a park writes None, and that must not be blocked."""
    assert daemon_mod._keep_specific_park_reason("selfreview_exhausted", None) is None


def test_the_daemon_uses_the_guard_on_the_generic_write():
    """The guard only helps on the path that was doing the overwriting."""
    src = (_V2 / "lemd" / "daemon.py").read_text()
    assert "parked_reason=_keep_specific_park_reason(row[\"parked_reason\"]" in src
