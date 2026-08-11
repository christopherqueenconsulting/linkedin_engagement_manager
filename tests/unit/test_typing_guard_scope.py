"""The Optional-typing guard (issue #1221) is only worth having while it stays cheap and advisory.

Three things make it that, and all three are the kind that rot silently: the scope stays a NAMED
list of modules whose returns are genuinely `Optional`, the runner never fails a build, and no
workflow turns it into a gate. The #1154 audit found ZERO live instances of the bug this prevents,
so a guard that starts costing PRs has inverted its own justification.
"""

import os
import pathlib
import re
import subprocess

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
        # NOT anchored to the start of a line: `mypy … || exit 1` is exactly how someone would turn
        # this into a gate, and a `^\s*exit` pattern would wave it through. Comment lines are
        # dropped first — the header prose talks ABOUT exiting non-zero.
        code_lines = [line for line in _SCRIPT.read_text(encoding="utf-8").splitlines()
                      if not line.lstrip().startswith("#")]
        codes = set(re.findall(r"\bexit\s+(\S+)", "\n".join(code_lines)))
        # `exit 2` is the setup-failed arm: the script could not reach the repo or make a temp file,
        # so it ran nothing and has nothing to report.
        assert codes <= {"0", "2"}, f"scripts/mypy_check.sh can exit non-zero on findings: {codes}"

    def test_no_workflow_runs_mypy_as_a_blocking_step(self):
        workflows = sorted([*_WORKFLOWS.glob("*.yml"), *_WORKFLOWS.glob("*.yaml")])
        assert workflows, f"no workflows found under {_WORKFLOWS} — this guard would pass vacuously"
        offenders = []
        for path in workflows:
            text = path.read_text(encoding="utf-8")
            if "mypy" in text and "continue-on-error" not in text:
                offenders.append(path.name)
        assert not offenders, (
            f"these workflows run the advisory typing guard without continue-on-error, which makes "
            f"it a gate for a bug with zero measured instances: {offenders}")


class TestTheRunnerReportsWhatActuallyHappened:
    """The script always exits 0, so its OUTPUT is the only signal a reader gets.

    A run that never graded anything — mypy not installed, a `files` entry pointing at a module that
    moved, a typo in a setting — exits non-zero with zero finding lines. Reporting that as
    "0 finding(s)" would announce a clean sheet for a check that ran nothing.
    """

    @staticmethod
    def _run(tmp_path, stdout: str, code: int) -> str:
        """Run the real script against a stub `poetry` that replays `stdout` and exits `code`."""
        stub_dir = tmp_path / "bin"
        stub_dir.mkdir()
        stub = stub_dir / "poetry"
        stub.write_text("#!/usr/bin/env bash\n"
                        f"cat <<'MYPYOUT'\n{stdout}\nMYPYOUT\n"
                        f"exit {code}\n", encoding="utf-8")
        stub.chmod(0o755)
        env = {**os.environ, "PATH": f"{stub_dir}:{os.environ['PATH']}"}
        done = subprocess.run([str(_SCRIPT)], capture_output=True, text=True, env=env, timeout=60)
        assert done.returncode == 0, f"the runner exited {done.returncode} — it must always exit 0"
        return done.stdout + done.stderr

    def test_a_clean_run_says_nothing_extra(self, tmp_path):
        assert "finding(s)" not in self._run(tmp_path, "Success: no issues found in 13 source files", 0)

    def test_real_findings_are_counted(self, tmp_path):
        output = self._run(tmp_path, "src/cqc_lem/utilities/flags.py:12: error: bad\nFound 1 error", 1)
        assert "1 finding(s)" in output

    def test_a_run_that_graded_nothing_is_not_reported_as_zero_findings(self, tmp_path):
        output = self._run(tmp_path, "mypy: error: Cannot read file 'gone.py': No such file", 2)
        assert "0 finding(s)" not in output, "a run that checked nothing must not read as a clean sheet"
        assert "NOT a clean result" in output

    def test_a_missing_install_says_how_to_fix_it(self, tmp_path):
        output = self._run(tmp_path, "Command not found: mypy", 1)
        assert "poetry install --with lint" in output
        assert "0 finding(s)" not in output
