"""#1605: the newest-ref parity fix, and the `approach_rejected` dismiss escape.

`unpark.sh`'s own linked-PR lookups (`pr_for_issue` in `lib/guards.sh`, and its own `DONEPR` guard)
used to read the FIRST linked ref while `github.linked_pr_state()` (#1405) reads the NEWEST — so an
issue whose first ref is an older closed PR and whose newest is merged slipped past the merged guard
and re-parked forever. Fixing that alone was not enough for `approach_rejected`: un-parking does not
change the GitHub fact `decide()` reads, so the very next observation re-reads the same closed PR and
parks it again on the spot. This file covers both halves: the newest-ref parity, and the durable
per-PR-number dismissal that makes a retry answer actually retry.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_PIPELINE = _ROOT / "scripts" / "agent-pipeline"
_V2 = _PIPELINE / "v2"
sys.path.insert(0, str(_V2))

from lemd import github, observe  # noqa: E402

GUARDS_SRC = (_PIPELINE / "lib" / "guards.sh").read_text()
UNPARK_SRC = (_V2 / "actions" / "unpark.sh").read_text()

PR_FOR_ISSUE = re.search(r"\npr_for_issue\(\) \{.*?\n\}\n", GUARDS_SRC, re.S).group(0)
# The block between "resolve TPR" and the trust gate that follows it — extracted so this exercises
# the real newest-ref lookup and dismiss write without also having to satisfy `author_trusted` /
# `v2_owner_answered`'s own `gh` calls.
DISMISS_BLOCK = UNPARK_SRC[
    UNPARK_SRC.index("# ---- no PR:"):UNPARK_SRC.index("# `agent:ready` GRANTS work")
]


def _run(script: str) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)


def _refs_json(*numbers: int) -> str:
    """`closedByPullRequestsReferences` JSON for the given PR numbers, in GitHub's own shape."""
    return json.dumps({"closedByPullRequestsReferences": [
        {"id": f"PR_{n}", "number": n,
         "repository": {"name": "r", "owner": {"login": "o"}},
         "url": f"https://github.com/o/r/pull/{n}"} for n in numbers
    ]})


# ------------------------------------------------------------------ lib/guards.sh: pr_for_issue


def test_pr_for_issue_picks_the_newest_ref_not_the_first():
    """The first ref is an older CLOSED PR; only the newest, OPEN one is live and must win."""
    script = f"""
SLUG="o/r"
gh() {{
  if [ "$1" = issue ] && [ "$2" = view ]; then printf '%s' '{_refs_json(1592, 1597)}'
  elif [ "$1" = pr ] && [ "$2" = view ]; then
    case "$3" in 1592) printf CLOSED ;; 1597) printf OPEN ;; esac
  fi
}}
{PR_FOR_ISSUE}
pr_for_issue 1091
"""
    got = _run(script)
    assert got.stdout.strip() == "1597"


def test_pr_for_issue_is_indifferent_to_array_order():
    """The API's ordering is not a documented guarantee — sorted by NUMBER, not by position."""
    script = f"""
SLUG="o/r"
gh() {{
  if [ "$1" = issue ] && [ "$2" = view ]; then printf '%s' '{_refs_json(1597, 1592)}'
  elif [ "$1" = pr ] && [ "$2" = view ]; then
    case "$3" in 1592) printf CLOSED ;; 1597) printf OPEN ;; esac
  fi
}}
{PR_FOR_ISSUE}
pr_for_issue 1091
"""
    got = _run(script)
    assert got.stdout.strip() == "1597"


# ------------------------------------------------------------------ unpark.sh: DONEPR + dismiss


def _dismiss_run(*, refs: tuple[int, ...], states: dict[int, str], park_reason: str,
                 tmp_path: Path, tiss: int = 42) -> subprocess.CompletedProcess:
    state_cases = "\n".join(f'    {n}) printf {s} ;;' for n, s in states.items())
    script = f"""
set -uo pipefail
BASE="{tmp_path}"
mkdir -p "$BASE/state"
SLUG="o/r"
TISS="{tiss}"
PARK_REASON="{park_reason}"
log() {{ :; }}
gh() {{
  if [ "$1" = issue ] && [ "$2" = view ]; then printf '%s' '{_refs_json(*refs)}'
  elif [ "$1" = pr ] && [ "$2" = view ]; then
    case "$3" in
{state_cases}
    esac
  fi
}}
{DISMISS_BLOCK}
"""
    return _run(script)


def test_the_merged_guard_reads_the_newest_ref_not_the_first(tmp_path):
    """The exact regression the issue describes: first ref CLOSED, newest MERGED — must be caught.

    With the old FIRST-ref bug, `DONEPR` would resolve to the older #1592 (CLOSED, not MERGED), so
    the merged guard would never fire — and, worse, an `approach_rejected` answer would then dismiss
    #1592 as if THAT were the rejected approach, while the real state (#1597 merged) went unseen.
    The fixed NEWEST lookup must catch the merge first and never reach the dismiss write at all.
    """
    got = _dismiss_run(refs=(1592, 1597), states={1592: "CLOSED", 1597: "MERGED"},
                       park_reason="approach_rejected", tmp_path=tmp_path)
    assert got.returncode == 0
    assert not (tmp_path / "state").exists() or \
        not (tmp_path / "state" / "dismissed-issue-42.txt").exists()


def test_an_approach_rejected_answer_dismisses_the_rejected_pr_number(tmp_path):
    """The durable half: the closed PR's number is recorded so the NEXT read can skip it."""
    got = _dismiss_run(refs=(1600,), states={1600: "CLOSED"},
                       park_reason="approach_rejected", tmp_path=tmp_path)
    assert got.returncode == 0
    dismissed = (tmp_path / "state" / "dismissed-issue-42.txt").read_text().splitlines()
    assert dismissed == ["1600"]


def test_dismissing_twice_does_not_duplicate_the_line(tmp_path):
    """Idempotent: a re-observation that answers the same park again must not grow the file."""
    for _ in range(2):
        _dismiss_run(refs=(1600,), states={1600: "CLOSED"},
                    park_reason="approach_rejected", tmp_path=tmp_path)
    dismissed = (tmp_path / "state" / "dismissed-issue-42.txt").read_text().splitlines()
    assert dismissed == ["1600"]


def test_a_different_rejected_pr_gets_its_own_dismissal():
    """Criterion 3: a NEW rejection on a DIFFERENT PR must still be free to park normally later.

    Scoping by number is what makes that true — this asserts the file carries the number that was
    actually rejected THIS time, not a blanket marker for the issue.
    """
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        tmp_path = Path(td)
        _dismiss_run(refs=(1600,), states={1600: "CLOSED"},
                    park_reason="approach_rejected", tmp_path=tmp_path)
        _dismiss_run(refs=(1650,), states={1650: "CLOSED"},
                    park_reason="approach_rejected", tmp_path=tmp_path)
        dismissed = set((tmp_path / "state" / "dismissed-issue-42.txt").read_text().split())
    assert dismissed == {"1600", "1650"}


def test_a_non_approach_rejected_answer_never_writes_the_dismiss_file(tmp_path):
    """The escape is scoped to the one reason it exists for."""
    _dismiss_run(refs=(1600,), states={1600: "CLOSED"}, park_reason="needs_human",
                tmp_path=tmp_path)
    assert not (tmp_path / "state" / "dismissed-issue-42.txt").exists()


def test_a_race_where_the_newest_ref_is_no_longer_closed_is_not_dismissed(tmp_path):
    """Only dismiss what GitHub still calls CLOSED right now — an OPEN ref must never be ignored."""
    _dismiss_run(refs=(1600,), states={1600: "OPEN"}, park_reason="approach_rejected",
                tmp_path=tmp_path)
    assert not (tmp_path / "state" / "dismissed-issue-42.txt").exists()


# ------------------------------------------------------------------ github.linked_pr_state(ignore=)


def _gh_stub(refs, state):
    calls: list[list[str]] = []

    def fake(args, **_kw):
        calls.append(args)
        if "closedByPullRequestsReferences" in args:
            return {"closedByPullRequestsReferences": refs}
        return {"state": state}
    return fake, calls


def test_linked_pr_state_ignores_a_dismissed_ref(monkeypatch):
    """The dismissed PR must read exactly as if it were never linked at all."""
    fake, _ = _gh_stub([{"id": "PR_1600", "number": 1600,
                         "repository": {"name": "r", "owner": {"login": "o"}},
                         "url": "https://github.com/o/r/pull/1600"}], "CLOSED")
    monkeypatch.setattr(github, "gh_json", fake)
    assert github.linked_pr_state("o/r", 1405, ignore=frozenset({1600})) == ""


def test_linked_pr_state_falls_back_to_the_next_newest_undismissed_ref(monkeypatch):
    """Dismissing the newest ref must not blind the read to a still-live older one."""
    refs = [
        {"id": "PR_1592", "number": 1592,
         "repository": {"name": "r", "owner": {"login": "o"}},
         "url": "https://github.com/o/r/pull/1592"},
        {"id": "PR_1597", "number": 1597,
         "repository": {"name": "r", "owner": {"login": "o"}},
         "url": "https://github.com/o/r/pull/1597"},
    ]
    fake, calls = _gh_stub(refs, "OPEN")
    monkeypatch.setattr(github, "gh_json", fake)
    assert github.linked_pr_state("o/r", 1091, ignore=frozenset({1597})) == "OPEN"
    assert "1592" in calls[1]


def test_linked_pr_state_ignore_defaults_to_nothing(monkeypatch):
    """Every existing caller (no `ignore` argument) must be byte-identical to before #1605."""
    fake, _ = _gh_stub([{"id": "PR_1600", "number": 1600,
                         "repository": {"name": "r", "owner": {"login": "o"}},
                         "url": "https://github.com/o/r/pull/1600"}], "CLOSED")
    monkeypatch.setattr(github, "gh_json", fake)
    assert github.linked_pr_state("o/r", 1405) == "CLOSED"


# ------------------------------------------------------------------ observe: reading the dismiss file


def test_dismissed_prs_reads_the_same_file_unpark_sh_writes(tmp_path, monkeypatch):
    """The python reader and the bash writer must agree on the path and the format."""
    monkeypatch.setattr(observe, "BASE", tmp_path)
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "dismissed-issue-1605.txt").write_text("1600\n1650\n")
    assert observe._dismissed_prs(1605) == frozenset({1600, 1650})


def test_dismissed_prs_is_empty_when_nothing_was_ever_written(tmp_path, monkeypatch):
    """No file is the common case — every issue that never parked `approach_rejected`."""
    monkeypatch.setattr(observe, "BASE", tmp_path)
    assert observe._dismissed_prs(999999) == frozenset()


def test_snapshot_issue_passes_the_dismissed_set_to_linked_pr_state(monkeypatch):
    """The wiring: `snapshot_issue` must hand its dismissed set to the read, not just compute it."""
    seen = {}

    def fake_linked_pr_state(slug, number, *, ignore=frozenset(), **_kw):
        seen["ignore"] = ignore
        return ""

    monkeypatch.setattr(observe, "_dismissed_prs", lambda n: frozenset({1600}))
    monkeypatch.setattr(github, "linked_pr_state", fake_linked_pr_state)
    monkeypatch.setattr(github, "gh_json", lambda *a, **k: {
        "number": 1605, "state": "OPEN", "labels": [{"name": "agent:ready"}]})
    monkeypatch.setattr(github, "branch_exists", lambda *a, **k: False)
    observe.snapshot_issue("o/r", 1605)
    assert seen["ignore"] == frozenset({1600})


# ------------------------------------------------------------------ the decision-table doc


def test_decision_table_still_documents_both_park_rows():
    """The reasons `decide()` returns are unchanged by #1605.

    Only what feeds `linked_pr_state` changed, so the existing rows 12-13 still cover it — this pins
    that no new undocumented reason was added, and that the doc records the fix itself.
    """
    doc = (_ROOT / "docs" / "agent-pipeline-v2.md").read_text()
    assert "`work_shipped_needs_close`" in doc
    assert "`approach_rejected`" in doc
    assert "1605" in doc, "the doc should record the fix, not just the original rows"
