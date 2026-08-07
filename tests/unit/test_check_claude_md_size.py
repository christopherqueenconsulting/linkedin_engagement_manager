"""Unit tests for scripts/check_claude_md_size.py — the pure size/threshold/
baseline logic (issue #1000). Real I/O (git, $GITHUB_OUTPUT) is mocked.
"""

import importlib.util
import pathlib
import subprocess

import pytest

pytestmark = pytest.mark.unit

# The tool lives under scripts/ (not an importable package) — load it by path.
_ROOT = pathlib.Path(__file__).resolve().parents[2]
_PATH = _ROOT / "scripts" / "check_claude_md_size.py"
_spec = importlib.util.spec_from_file_location("check_claude_md_size", _PATH)
guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(guard)


class TestStrictCheck:
    def test_under_cap_ok(self, capsys):
        assert guard._strict_check(guard.MAX_CHARS - 1) == 0
        assert "ok:" in capsys.readouterr().out

    def test_at_cap_ok(self, capsys):
        assert guard._strict_check(guard.MAX_CHARS) == 0

    def test_over_cap_fails(self, capsys):
        assert guard._strict_check(guard.MAX_CHARS + 1) == 1
        assert "error:" in capsys.readouterr().err


class TestSoftCheck:
    def test_under_warn_is_ok_and_never_fails(self, capsys):
        rc = guard._soft_check(guard.DEFAULT_WARN_CHARS - 1, guard.DEFAULT_WARN_CHARS)
        assert rc == 0
        assert "ok:" in capsys.readouterr().out

    def test_in_warn_zone_warns_but_does_not_fail(self, capsys):
        rc = guard._soft_check(guard.DEFAULT_WARN_CHARS, guard.DEFAULT_WARN_CHARS)
        assert rc == 0
        out = capsys.readouterr().out
        assert "::warning::" in out
        assert "OVER" not in out

    def test_over_cap_still_does_not_fail_the_build(self, capsys):
        # The soft path is the early-warning shape (issue #1000): a docs-cap
        # regression on main must never redden the push run.
        rc = guard._soft_check(guard.MAX_CHARS + 1, guard.DEFAULT_WARN_CHARS)
        assert rc == 0
        out = capsys.readouterr().out
        assert "::warning::" in out and "OVER" in out

    def test_writes_github_output(self, tmp_path, monkeypatch):
        out_file = tmp_path / "gh_output.txt"
        monkeypatch.setenv("GITHUB_OUTPUT", str(out_file))
        guard._soft_check(guard.MAX_CHARS + 5, guard.DEFAULT_WARN_CHARS)
        content = out_file.read_text()
        assert "status=over" in content
        assert f"size={guard.MAX_CHARS + 5}" in content

    def test_no_github_output_env_does_not_raise(self, monkeypatch):
        monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
        assert guard._soft_check(100, guard.DEFAULT_WARN_CHARS) == 0


class TestBaselineReport:
    def test_inherited_overage_notes_it_was_already_over(self, capsys):
        over = guard.MAX_CHARS + 500
        guard._report_baseline("origin/main", over, over + 200)
        out = capsys.readouterr().out
        assert "already" in out and "inherited" in out

    def test_caused_overage_notes_this_diff_pushed_it_over(self, capsys):
        under = guard.MAX_CHARS - 100
        guard._report_baseline("origin/main", under, under + 300)
        out = capsys.readouterr().out
        assert "pushed CLAUDE.md over" in out

    def test_both_under_cap_prints_nothing(self, capsys):
        under = guard.MAX_CHARS - 500
        guard._report_baseline("origin/main", under, under + 400)
        assert capsys.readouterr().out == ""


class TestBaselineSize:
    def test_reads_size_from_git_show(self, monkeypatch):
        def fake_run(args, **kwargs):
            assert args[:2] == ["git", "show"]
            return subprocess.CompletedProcess(args, 0, stdout="abcde", stderr="")
        monkeypatch.setattr(guard.subprocess, "run", fake_run)
        assert guard._baseline_size("origin/main") == 5

    def test_missing_ref_returns_none(self, monkeypatch):
        def fake_run(args, **kwargs):
            return subprocess.CompletedProcess(args, 128, stdout="", stderr="bad rev")
        monkeypatch.setattr(guard.subprocess, "run", fake_run)
        assert guard._baseline_size("origin/does-not-exist") is None

    def test_git_not_available_returns_none(self, monkeypatch):
        def fake_run(args, **kwargs):
            raise FileNotFoundError("git not found")
        monkeypatch.setattr(guard.subprocess, "run", fake_run)
        assert guard._baseline_size("origin/main") is None


class TestMainCli:
    def test_default_invocation_is_strict_and_reads_real_file(self, monkeypatch):
        # No args: exercises the real CLAUDE.md, mirroring the documented
        # `python3 scripts/check_claude_md_size.py` local/CI invocation.
        monkeypatch.setattr("sys.argv", ["check_claude_md_size.py"])
        assert guard.main() in (0, 1)

    def test_warn_at_flag_alone_uses_default_threshold(self, monkeypatch):
        monkeypatch.setattr(guard, "_read_size", lambda: guard.DEFAULT_WARN_CHARS)
        monkeypatch.setattr("sys.argv", ["check_claude_md_size.py", "--warn-at"])
        assert guard.main() == 0

    def test_missing_target_file_fails(self, monkeypatch):
        monkeypatch.setattr(guard, "_read_size", lambda: None)
        monkeypatch.setattr("sys.argv", ["check_claude_md_size.py"])
        assert guard.main() == 1
