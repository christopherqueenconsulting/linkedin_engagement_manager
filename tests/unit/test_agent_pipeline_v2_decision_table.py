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

#: Reasons built with an f-string, so the code carries a prefix and the doc carries the shape.
#: Matched by prefix on both sides rather than exact text.
PARAMETERISED = ("human_hold:", "not_admissible:")

#: Reason strings that are constructed at the dispatch site rather than inside `decide` —
#: `act()` parks an item as `{mode}_exhausted` when the ledger is spent. They are documented in §6
#: (the exit-code table) rather than §4, so the table must NOT list them.
NOT_DECIDE_REASONS = ("_exhausted",)


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
    for cell in re.findall(r"`([a-z_]+(?::\{?[a-z_,{}]+\}?)?)`", table):
        if cell.startswith(PARAMETERISED):
            found.add(cell.split(":", 1)[0] + ":")
        elif re.fullmatch(r"[a-z_]+", cell):
            found.add(cell)
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
        assert marker not in table
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


def test_the_gap_section_exists_and_is_not_empty():
    """§7 is what makes the doc honest. An empty gap table means someone deleted the bad news."""
    text = _DOC.read_text()
    gaps = text[text.index("## 7. Intended state"):text.index("## 8. Deploy and operate")]
    assert gaps.count("\n|") >= 8, "the intended-state table lost rows without them being fixed"
