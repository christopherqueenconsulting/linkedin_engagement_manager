"""The Optional-typing guard (issue #1221) is only worth having while it stays cheap and advisory.

Three things make it that, and all three are the kind that rot silently: the scope stays a NAMED
list of modules whose returns are genuinely `Optional`, the runner never fails a build, and no
workflow turns it into a gate. The #1154 audit found ZERO live instances of the bug this prevents,
so a guard that starts costing PRs has inverted its own justification.
"""

import pathlib
import re

import pytest
import tomllib

pytestmark = pytest.mark.unit

_ROOT = pathlib.Path(__file__).resolve().parents[2]
_PYPROJECT = _ROOT / "pyproject.toml"
_SCRIPT = _ROOT / "scripts/mypy_check.sh"
_WORKFLOWS = _ROOT / ".github/workflows"

# A return annotation where None carries a third meaning: `-> Optional[X]`, `-> X | None`, or the
# same two spelled as a string annotation.
_OPTIONAL_RETURN = re.compile(r"->\s*\"?(?:Optional\[|[\w\[\], .]+\|\s*None)")


def _mypy_config() -> dict:
    return tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))["tool"]["mypy"]


class TestTheScopeIsNamedAndReal:
    def test_the_scope_is_an_explicit_file_list(self):
        files = _mypy_config()["files"]
        assert files, "[tool.mypy] files must name the scope — an empty list checks the whole tree"
        for entry in files:
            assert entry.endswith(".py"), f"{entry} is a directory: the scope is per-module on purpose"
            assert entry.startswith("src/cqc_lem/"), f"{entry} is outside the app package"

    def test_every_scoped_module_exists(self):
        missing = [f for f in _mypy_config()["files"] if not (_ROOT / f).is_file()]
        assert not missing, f"scoped modules that no longer exist (moved? renamed?): {missing}"

    def test_every_scoped_module_has_an_optional_return(self):
        """The stated admission rule: None means UNKNOWN somewhere in this module's public surface."""
        without = [f for f in _mypy_config()["files"]
                   if not _OPTIONAL_RETURN.search((_ROOT / f).read_text(encoding="utf-8"))]
        assert not without, (
            f"these modules declare no Optional return, so the guard has nothing to protect "
            f"there — drop them from [tool.mypy] files: {without}")

    def test_the_optional_settings_the_guard_exists_for_stay_on(self):
        config = _mypy_config()
        for setting in ("strict_optional", "no_implicit_optional", "warn_no_return"):
            assert config.get(setting) is True, f"{setting} is the guard; without it the scope checks nothing"


class TestItCannotBecomeAGate:
    def test_the_runner_exists_and_is_executable(self):
        assert _SCRIPT.is_file(), "scripts/mypy_check.sh is the documented way to run the guard"
        assert _SCRIPT.stat().st_mode & 0o111, "scripts/mypy_check.sh must be executable"

    def test_the_runner_never_exits_non_zero(self):
        """Advisory BY CONSTRUCTION — a caller cannot make this fail a build by forgetting a flag."""
        codes = set(re.findall(r"^\s*exit\s+(\S+)", _SCRIPT.read_text(encoding="utf-8"), re.MULTILINE))
        # `exit 2` is the cd-failed arm: the script could not reach the repo, so it ran nothing.
        assert codes <= {"0", "2"}, f"scripts/mypy_check.sh can exit non-zero on findings: {codes}"

    def test_no_workflow_runs_mypy_as_a_blocking_step(self):
        offenders = [path.name for path in sorted(_WORKFLOWS.glob("*.yml"))
                     if "mypy" in path.read_text(encoding="utf-8")
                     and "continue-on-error" not in path.read_text(encoding="utf-8")]
        assert not offenders, (
            f"these workflows run the advisory typing guard without continue-on-error, which makes "
            f"it a gate for a bug with zero measured instances: {offenders}")
