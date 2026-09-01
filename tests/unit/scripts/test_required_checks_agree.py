"""The required-check list exists in three places, and only means something while they agree.

1. **Branch protection on `main`** — what GitHub actually blocks a merge on.
2. **`scripts/agent-pipeline/tick.sh`** — `REQUIRED_CHECKS_JQ`, what the v1 runner waits for.
3. **`scripts/agent-pipeline/v2/lemd/github.py`** — `REQUIRED_CHECKS`, what the v2 daemon waits for.

The pipeline decides whether a PR is mergeable from its OWN copy, never by asking GitHub what is
required. So a name added to branch protection alone leaves the daemon requesting merges the queue
refuses, and a name dropped from branch protection alone leaves the daemon holding green work
forever. #1878 added `Docstring & Lint Gate` to all three at once for exactly that reason.

This file can only reach two of the three — nothing in CI may read or write branch protection, and
changing it is an owner action. It guards what it can:

* the two code copies are byte-for-byte the same SET, read out of both files rather than restated
  here (a literal list in this test would be a fourth place to drift);
* every name is a real job `name:` in `.github/workflows/`, because a context that no job ever
  reports is not a stricter gate — it is a permanent `pending` that parks every PR;
* every one of those workflows declares `merge_group:`, because a required check that does not run
  in the merge queue deadlocks the queue rather than gating it;
* `CLAUDE.md`'s CI Gates section names the same set, with a count word that has to be updated.
"""

from __future__ import annotations

import pathlib
import re
import sys

import pytest
import yaml

pytestmark = pytest.mark.unit

_ROOT = pathlib.Path(__file__).resolve().parents[3]
_TICK = _ROOT / "scripts/agent-pipeline/tick.sh"
_WORKFLOWS = _ROOT / ".github/workflows"
_CLAUDE_MD = _ROOT / "CLAUDE.md"

sys.path.insert(0, str(_ROOT / "scripts/agent-pipeline/v2"))
from lemd import github  # noqa: E402

#: Spelled-out counts, so the CI Gates paragraph cannot keep saying SIX with seven names under it.
_COUNT_WORDS = {
    4: "four",
    5: "five",
    6: "six",
    7: "seven",
    8: "eight",
    9: "nine",
    10: "ten",
}

#: Every `.n=="…"` inside the jq selector tick.sh builds its check rollup filter from.
_JQ_NAME = re.compile(r'\.n=="([^"]+)"')


def _tick_required_checks(text: str) -> frozenset[str]:
    """Read the v1 runner's required-check set out of its jq selector.

    Args:
        text: The full contents of `tick.sh`.

    Returns:
        Every context named in the `REQUIRED_CHECKS_JQ` assignment.

    Raises:
        AssertionError: If the assignment is missing or names nothing — either would make the
            comparison below pass against an empty set.
    """
    line = next(
        (ln for ln in text.splitlines() if ln.startswith("REQUIRED_CHECKS_JQ=")),
        None,
    )
    assert line is not None, "tick.sh no longer defines REQUIRED_CHECKS_JQ"
    names = _JQ_NAME.findall(line)
    assert names, f"REQUIRED_CHECKS_JQ names no contexts: {line}"
    return frozenset(names)


def _workflow_jobs() -> dict[str, list[dict]]:
    """Map every job's reported check name to the workflow definitions that declare it.

    The value is a LIST because two workflows may name a job identically, and GitHub then posts two
    checks under that one context — both of which must pass. Collapsing them to one document would
    let the second escape the `merge_group:` assertion.

    Returns:
        Job display name (falling back to the job id, which is what GitHub reports when a job
        declares no `name:`) → every parsed workflow document declaring it.
    """
    jobs: dict[str, list[dict]] = {}
    for path in sorted([*_WORKFLOWS.glob("*.yml"), *_WORKFLOWS.glob("*.yaml")]):
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for job_id, job in (doc.get("jobs") or {}).items():
            if not isinstance(job, dict):
                continue
            jobs.setdefault(str(job.get("name") or job_id), []).append(doc)
    return jobs


def _ci_gates_section() -> str:
    """Return the body of CLAUDE.md's `## CI Gates` section as ONE whitespace-normalised line.

    CLAUDE.md is hard-wrapped, so a check name is regularly split across two lines mid-backticks
    (`` `Unit Tests\\n(Python 3.12)` ``). Matching the raw text would report that name as missing
    and push the file into a reflow it does not need — the file is a fixed-shape, size-capped index.

    Raises:
        AssertionError: If the section heading is gone, so the assertions below cannot silently
            start matching an empty string.
    """
    text = _CLAUDE_MD.read_text(encoding="utf-8")
    start = text.find("\n## CI Gates\n")
    assert start != -1, "CLAUDE.md has no `## CI Gates` section"
    end = text.find("\n## ", start + 1)
    return re.sub(r"\s+", " ", text[start : end if end != -1 else len(text)])


class TestTheTwoCodeCopiesAgree:
    def test_both_sides_name_the_same_contexts(self):
        """Derived from both files, never restated, so this cannot become true of nothing."""
        from_tick = _tick_required_checks(_TICK.read_text(encoding="utf-8"))
        from_v2 = frozenset(github.REQUIRED_CHECKS)
        assert from_tick == from_v2, (
            "tick.sh's REQUIRED_CHECKS_JQ and lemd/github.py's REQUIRED_CHECKS disagree.\n"
            f"  only in tick.sh: {sorted(from_tick - from_v2)}\n"
            f"  only in lemd/github.py: {sorted(from_v2 - from_tick)}\n"
            "Both decide whether the pipeline may merge. They move together, and branch protection "
            "moves with them (owner action) — see CLAUDE.md § CI Gates."
        )

    def test_the_v2_tuple_has_no_duplicates(self):
        """`REQUIRED_CHECKS` is a tuple, so a duplicated paste would survive the set comparison."""
        assert len(github.REQUIRED_CHECKS) == len(set(github.REQUIRED_CHECKS))

    def test_the_ratchet_is_one_of_them(self):
        """Anti-vacuity for #1878: the whole point was adding this name to every copy."""
        assert "Docstring & Lint Gate" in github.REQUIRED_CHECKS

    def test_the_parser_notices_a_dropped_name(self):
        """A parser that returned the same set for any input would make the comparison meaningless."""
        real = _TICK.read_text(encoding="utf-8")
        mutated = real.replace(' or .n=="Docstring & Lint Gate"', "", 1)
        assert mutated != real, "the mutation did not apply — this test is no longer testing anything"
        assert _tick_required_checks(mutated) != frozenset(github.REQUIRED_CHECKS)


class TestEveryRequiredContextIsReportable:
    """A required context nothing reports is not a stricter gate — it is a permanent `pending`."""

    def test_each_context_is_a_real_workflow_job(self):
        jobs = _workflow_jobs()
        missing = [n for n in github.REQUIRED_CHECKS if n not in jobs]
        assert not missing, (
            f"no job in .github/workflows/ reports {missing}. GitHub never posts a check for a "
            "context no job names, so branch protection blocks the PR forever and the pipeline "
            "counts it pending on every tick."
        )

    def test_each_workflow_also_runs_in_the_merge_queue(self):
        """Without `merge_group:` a required check never reports on a queue entry, so it deadlocks."""
        jobs = _workflow_jobs()
        for name in github.REQUIRED_CHECKS:
            for doc in jobs[name]:
                # PyYAML resolves the bare `on:` key to the boolean True (the YAML 1.1 spec), which
                # is why this reads both spellings rather than the obvious one.
                triggers = doc.get(True) or doc.get("on") or {}
                assert "merge_group" in triggers, (
                    f"the workflow reporting {name!r} has no `merge_group:` trigger — a required "
                    "check that cannot report inside the merge queue holds every entry until it "
                    "times out."
                )


class TestClaudeMdSaysTheSameThing:
    def test_every_required_context_is_named(self):
        section = _ci_gates_section()
        missing = [n for n in github.REQUIRED_CHECKS if f"`{n}`" not in section]
        assert not missing, f"CLAUDE.md § CI Gates does not name {missing}"

    def test_the_count_word_matches(self):
        """The prose leads with a spelled-out count; an eighth check has to move it."""
        section = _ci_gates_section()
        expected = _COUNT_WORDS[len(github.REQUIRED_CHECKS)]
        assert re.search(rf"\b{expected}\b", section, re.IGNORECASE), (
            f"CLAUDE.md § CI Gates should say {expected.upper()} contexts — there are "
            f"{len(github.REQUIRED_CHECKS)}."
        )

    def test_no_required_context_is_also_called_not_required(self):
        """The section names the non-required workflows too; a name cannot be in both halves.

        This is the assertion that would have caught the half-edit: adding a name to the required
        list while the "runs but is NOT required" sentence still names it leaves the doc telling a
        reader both things, and the reader believes the one that suits them.
        """
        for sentence in _ci_gates_section().split(". "):
            if "NOT required" not in sentence:
                continue
            for name in github.REQUIRED_CHECKS:
                assert f"`{name}`" not in sentence, (
                    f"CLAUDE.md § CI Gates calls {name!r} NOT required while it is in "
                    "REQUIRED_CHECKS."
                )
