"""The CLAUDE.md fixed-shape gate.

This is the enforcement point. It lives in `tests/unit/` on purpose: that lane is the
already-required `Unit Tests (Python 3.12)` context, which runs on `pull_request` AND
`merge_group` with no `paths:` filter. A dedicated workflow would have to be added to branch
protection by the owner, and a required check WITH a paths filter never reports on a PR that
does not match it — GitHub then waits forever, and inside the merge queue it evicts the PR.
Riding the unit lane gets real enforcement with no branch-protection change at all.

`scripts/check_claude_md_size.py` holds the rules and their messages; this file only asserts
that the repository currently satisfies them.
"""

import importlib.util
import pathlib

import pytest

pytestmark = pytest.mark.unit

_ROOT = pathlib.Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "check_claude_md_size", _ROOT / "scripts" / "check_claude_md_size.py")
guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(guard)


def _errors():
    return [v for v in guard.run_checks(guard.load_schema()) if v.level == "error"]


class TestClaudeMdIsFixedShape:
    def test_no_structure_violations(self):
        errors = _errors()
        assert not errors, "\n".join(
            f"{v.file}:{v.line} {v.code} {v.message}" for v in errors)

    def test_the_check_is_actually_looking_at_something(self):
        """A guard that silently stopped parsing would report a permanently clean file."""
        text = (_ROOT / "CLAUDE.md").read_text(encoding="utf-8")
        _, sections = guard.parse_markdown(text)
        assert len(sections) > 10, "the section parser stopped matching"
        rows = sum(len(t.rows) for s in sections for t in s.tables)
        assert rows > 40, "the row parser stopped matching"

    def test_a_planted_violation_would_be_caught(self):
        """The negative control for the assertion above: break it and it must fail."""
        text = (_ROOT / "CLAUDE.md").read_text(encoding="utf-8")
        broken = text + "\n## Invented Section\n\nbody\n"
        codes = {v.code for v in guard.check_structure(
            _ROOT / "CLAUDE.md", broken, guard.load_schema())}
        assert "CM001" in codes


class TestBudgetsStayUnderTheCeilings:
    def test_targets_sum_under_the_hard_total_budget(self):
        schema = guard.load_schema()
        total = sum(s["target"] for s in schema["sections"]) + schema["preamble"]["target"]
        assert total <= guard.HARD_TOTAL_BUDGET

    def test_budgets_never_reach_the_harness_cap(self):
        """The enforced ceiling has to leave real headroom, or the guard is decorative."""
        schema = guard.load_schema()
        total = sum(s["budget"] for s in schema["sections"]) + schema["preamble"]["budget"]
        assert guard.MAX_CHARS - total >= 4_000

    def test_the_file_is_well_under_the_harness_cap(self):
        size = len((_ROOT / "CLAUDE.md").read_text(encoding="utf-8"))
        assert size <= guard.HARD_TOTAL_BUDGET
