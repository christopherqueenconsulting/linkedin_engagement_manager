"""`scripts/ruff_count.sh` must count exactly what the Docstring & Lint Gate counts.

The gate grades `.ruff-baseline` against a finding-shaped regex. Humans and agents reach for
`ruff check ... | wc -l` instead, which is **2 too many** — ruff prints two trailing summary lines
(`Found N errors.` and `[*] N fixable ...`). Ratcheting the baseline with the inflated number leaves
that much silent slack in the gate.

That is not hypothetical: on 2026-08-09 the baseline sat at 2850 against a true count of 2848, and a
PR written specifically to fail the gate with 2 deliberate violations **passed**, because its
violations fit exactly inside the slack.

So the fix is one script, and this test is what keeps it honest — the regex is read out of the
workflow rather than restated here, so a change to either side has to be made to both.
"""

import pathlib
import re
import subprocess

import pytest

pytestmark = pytest.mark.unit

_ROOT = pathlib.Path(__file__).resolve().parents[2]
_GATE = _ROOT / ".github/workflows/docstring-lint.yml"
_SCRIPT = _ROOT / "scripts/ruff_count.sh"

# The counting line in either file: `grep -cE '<regex>' <file>`.
_COUNT_LINE = re.compile(r"grep -cE\s+'([^']+)'")


def _counting_regex(path: pathlib.Path) -> str:
    matches = _COUNT_LINE.findall(path.read_text(encoding="utf-8"))
    assert matches, f"no `grep -cE '...'` counting line found in {path.name}"
    return matches[0]


class TestTheCounterAgreesWithTheGate:
    def test_the_script_exists_and_is_executable(self):
        assert _SCRIPT.is_file(), "scripts/ruff_count.sh is the documented way to read the count"
        assert _SCRIPT.stat().st_mode & 0o111, "ruff_count.sh must be executable"

    def test_both_sides_count_with_the_same_regex(self):
        """Read it from BOTH files rather than asserting a literal here.

        A copy of the regex in this test would be a third place to drift; deriving it means the
        assertion cannot quietly become true of nothing.
        """
        assert _counting_regex(_SCRIPT) == _counting_regex(_GATE)

    def test_the_regex_actually_discriminates(self):
        """Anti-vacuity: a regex matching everything would make the counts agree and mean nothing."""
        pattern = re.compile(_counting_regex(_SCRIPT).replace("^[^ ]", "^[^ ]"))
        assert pattern.search("src/cqc_lem/api/main.py:12:5: E501 Line too long")
        # The two summary lines ruff appends — the entire reason the naive count is wrong.
        assert not pattern.search("Found 2848 errors.")
        assert not pattern.search("[*] 53 fixable with the `--fix` option")


class TestTheBaselineIsNotInflated:
    def test_baseline_is_an_integer(self):
        raw = (_ROOT / ".ruff-baseline").read_text(encoding="utf-8").strip()
        assert raw.isdigit(), f".ruff-baseline must be a bare integer, got {raw!r}"

    @pytest.mark.slow
    def test_baseline_matches_the_real_count(self):
        """Runs ruff, so it is marked slow and excluded from the PR lanes (`-m "not slow"`).

        Deliberately not a gate: the baseline is a RATCHET, and a PR that legitimately removes
        violations should not be forced to also ratchet in the same change. This exists so the
        nightly run says out loud when the two have drifted apart again.
        """
        out = subprocess.run([str(_SCRIPT)], capture_output=True, text=True, cwd=_ROOT)
        assert out.returncode == 0, out.stderr
        actual = int(out.stdout.strip())
        baseline = int((_ROOT / ".ruff-baseline").read_text().strip())
        assert actual <= baseline, f"tree has {actual} findings, above the {baseline} baseline"
        assert baseline - actual <= 5, (
            f"baseline {baseline} is {baseline - actual} above the real count {actual} — that much "
            "slack lets new violations in silently. Ratchet it with scripts/ruff_count.sh."
        )
