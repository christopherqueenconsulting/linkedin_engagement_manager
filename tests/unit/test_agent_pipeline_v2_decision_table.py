"""`docs/agent-pipeline-v2.md` §4 is a contract, not a snapshot.

The v2 pipeline shipped without a design document, and the one document it did have
(`v2/README.md`) said "Status: skeleton… Nothing here dispatches work yet" for a full day after the
daemon went live and started merging PRs. Every defect found on 2026-08-10/11 — the unported answer
lane, the invisible self-review marker, the credential fall-through, the stranded branches — was
invisible partly because no artifact listed what `decide()` could actually conclude.

So the decision table is tested, not trusted. If a branch is added to `observe.decide` without a row
in the table, or a row survives the branch it described, this fails. That is the whole point: a
document nobody can forget to update.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_V2 = _ROOT / "scripts" / "agent-pipeline" / "v2"
_DOC = _ROOT / "docs" / "agent-pipeline-v2.md"
sys.path.insert(0, str(_V2))

from lemd import observe  # noqa: E402

#: Reasons constructed at the DISPATCH site rather than inside `decide` — `act()` parks an item as
#: `{mode}_exhausted` when its ledger is spent. They belong in §6's exit-code table, not §4.
#:
#: Spelled out per mode rather than matched on the `_exhausted` suffix: `park_laps_exhausted` IS a
#: `decide()` reason (#1390) and a suffix match called it a violation. A checker that cannot tell
#: the two apart teaches people to rename their reasons around it.
NOT_DECIDE_REASONS = tuple(
    f"{mode}_exhausted" for mode in
    ("start", "fix", "review", "selfreview", "rebase", "revise", "depfix", "docfix", "merge")
)


def _reasons_in_code() -> set[str]:
    """Every `reason` string `observe.decide` can return.

    Read out of the source rather than by exercising the function: the point is to catch a branch
    somebody ADDED, and a test that only walks known branches cannot do that by construction.
    """
    src = (_V2 / "lemd" / "observe.py").read_text()
    # Both `decide` and the helper it delegates to — a reason returned from a helper is still a
    # reason `decide` can return, and scoping to `decide` alone was this extractor's own first bug.
    body = src[src.index("def _work_in_flight_or_stranded("):src.index("def snapshot_pr(")]
    found: set[str] = set()
    # Positional: Decision(ACT_X, db.STATE_Y, "reason", ...)
    found.update(re.findall(r'Decision\(\s*ACT_[A-Z]+,\s*db\.[A-Z_]+,\s*"([a-z_:]+)"', body))
    # f-string form: Decision(ACT_X, db.STATE_Y, f"prefix:{...}")
    found.update(
        m + ":" for m in re.findall(r'Decision\(\s*ACT_[A-Z]+,\s*db\.[A-Z_]+,\s*f"([a-z_]+):', body)
    )
    # Reasons that travel as a PARAMETER rather than a literal at the Decision() call — the helper's
    # default, and the keyword the other call site overrides it with. Missing these is how a
    # source-scanning test quietly under-reports: it sees `reason`, not the string.
    found.update(re.findall(r'reason: str = "([a-z_]+)"', body))
    found.update(re.findall(r'\breason="([a-z_]+)"', body))
    # Reasons that live in a LOOKUP TABLE rather than at the Decision() call. The lane labels moved
    # into `LANE_LABEL_MODES` when their precedence was made explicit (#1393), and a source scanner
    # that only understands call sites silently reported all three as undocumented.
    found.update(re.findall(r'"agent:[a-z]+": \("[a-z]+", "([a-z_]+)"\)', src))
    return {_family(r) for r in found}


def _family(reason: str) -> str:
    """Collapse a namespaced reason to its family.

    `human_hold` has one literal member (`human_hold:answer_already_routed`) and one f-string form
    (`human_hold:{verdict}`). The table documents the family and gives the shape; normalising only
    one side made the two disagree about the same rows, which is a bug in the checker, not the doc.
    """
    return reason.split(":", 1)[0] + ":" if ":" in reason else reason


def _reasons_in_doc() -> set[str]:
    """Every reason the decision table claims exists.

    The table renders them in backticks; f-string rows are written `human_hold:{verdict}`, which
    normalises to the same prefix the code produces.
    """
    text = _DOC.read_text()
    table = text[text.index("## 4. The decision table"):text.index("## 5. The GitHub field matrix")]
    found: set[str] = set()
    # The REASON COLUMN only — not every backticked token in the section. Scanning the prose swept
    # up function names (`review_state`) and snapshot fields and reported them as stale rows, which
    # would have pushed a future author into renaming things to appease the checker instead of
    # fixing the table. Rows are `| # | Condition | Action | Reason | Wake |`.
    for line in table.splitlines():
        cells = [c.strip() for c in line.split("|")]
        if len(cells) < 6 or not cells[1].isdigit():
            continue
        for cell in re.findall(r"`([a-z_]+(?::\{?[a-z_,{}]+\}?)?)`", cells[4]):
            found.add(_family(cell))
    return found


def test_the_doc_lists_every_reason_the_code_can_return():
    """A branch added to `decide` without a table row fails here."""
    missing = _reasons_in_code() - _reasons_in_doc()
    assert not missing, (
        f"observe.decide can return {sorted(missing)}, which docs/agent-pipeline-v2.md §4 "
        "does not document. Add a row to the decision table."
    )


def test_the_doc_lists_no_reason_the_code_cannot_return():
    """A table row that outlived its branch fails here.

    Filtered to reason-shaped strings: the table also mentions modes, labels and states in
    backticks, and those are checked by their own tests below.
    """
    code = _reasons_in_code()
    modes = {"start", "fix", "review", "selfreview", "rebase", "revise", "depfix", "docfix",
             "merge", "park", "unpark", "phasefix"}
    labels = {"needs", "agent", "human", "hold", "verdict", "close", "dispatch", "none"}
    stale = {
        r for r in _reasons_in_doc() - code
        if r not in modes and not any(r.startswith(p) for p in labels) and "_" in r
    }
    assert not stale, (
        f"docs/agent-pipeline-v2.md §4 documents {sorted(stale)}, which observe.decide never "
        "returns. Remove the row or fix the reason."
    )


def test_exhaustion_reasons_are_documented_outside_the_decision_table():
    """`{mode}_exhausted` is written by `act()`, not `decide()`.

    It belongs in the exit-code table in §6. Listing it in §4 would claim `decide` can conclude
    something it cannot, which is the exact class of error this file exists to catch.
    """
    text = _DOC.read_text()
    table = text[text.index("## 4. The decision table"):text.index("## 5. The GitHub field matrix")]
    for marker in NOT_DECIDE_REASONS:
        assert marker not in table, f"§4 lists {marker}, which `decide()` never returns"
    assert "_exhausted" in text, "the exhaustion park must still be documented somewhere"


def test_every_action_constant_appears_in_the_doc():
    """The doc must name every action `decide` can ask for.

    Derived from the module rather than hardcoded, so adding an `ACT_` constant — the shape of
    every gap fix queued behind this doc — fails until the document describes it.
    """
    text = _DOC.read_text().lower()
    actions = {
        v for k, v in vars(observe).items()
        if k.startswith("ACT_") and isinstance(v, str) and v != observe.ACT_NONE
    }
    assert actions, "no ACT_ constants found — the extractor broke, not the doc"
    missing = {a for a in actions if a not in text}
    assert not missing, f"docs/agent-pipeline-v2.md never mentions {sorted(missing)}"


def test_the_doc_records_the_live_status_not_the_skeleton_status():
    """The failure this document exists to prevent, asserted directly.

    `v2/README.md` claimed "Status: skeleton … Nothing here dispatches work yet" while the daemon
    was live and merging. A status line that cannot go stale is one that names the observable fact.
    """
    head = _DOC.read_text()[:2000]
    assert "skeleton" not in head.lower()
    assert "LEMD_SHADOW=0" in head


def test_the_gap_section_says_something_either_way():
    """§7 is what makes the doc honest, and honesty is not a row count.

    This used to require a minimum number of rows, on the theory that an empty table meant someone
    had deleted the bad news. But the whole point of the section is to be worked DOWN — it went from
    thirteen rows to a handful in one night — so the floor started failing the build for the right
    thing happening. What it must never do is go silent: either it lists gaps, or it says plainly
    that there are none left.
    """
    text = _DOC.read_text()
    gaps = text[text.index("## 7. Intended state"):text.index("## 8. Deploy and operate")]
    has_rows = "| **" in gaps
    says_none = "no known gaps" in gaps.lower()
    assert has_rows or says_none, (
        "§7 lists nothing and does not say the list is empty — a gap table that goes quiet is "
        "indistinguishable from one nobody maintains"
    )


def test_the_budget_table_matches_policy():
    """§6's numbers must be the real budgets and timeouts, not something a renumber walked over.

    They were, briefly: a regex that renumbered §4's decision rows was written unscoped and ran to
    the end of the file, rewriting the BUDGET column of §6 into row numbers — `start` claimed a
    budget of 35. Nothing caught it, because every test at the time checked reason strings and the
    numbers were only prose. They are not prose; they are the contract an operator reads before
    changing a lane's retry count.
    """
    from lemd import policy  # noqa: PLC0415

    table = _DOC.read_text()
    table = table[table.index("## 6."):table.index("## 7.")]
    for line in table.splitlines():
        cells = [c.strip() for c in line.split("|")]
        if len(cells) < 5 or not cells[1].startswith("`"):
            continue
        mode = cells[1].strip("`")
        if mode not in policy.MODE_BUDGET or not cells[3].isdigit():
            continue
        assert int(cells[3]) == policy.MODE_BUDGET[mode], (
            f"§6 says {mode} has a budget of {cells[3]}, policy says {policy.MODE_BUDGET[mode]}"
        )
        timeout = cells[4].rstrip("s")
        if timeout.isdigit() and mode in policy.MODE_TIMEOUT:
            assert int(timeout) == policy.MODE_TIMEOUT[mode], (
                f"§6 says {mode} times out at {timeout}s, policy says {policy.MODE_TIMEOUT[mode]}"
            )
