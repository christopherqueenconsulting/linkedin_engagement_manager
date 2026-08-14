"""`github.post_comment` — the one write `_notify_owner_review_needed` (#1501) depends on.

A thin wrapper, but the two things it must get right are exactly the two `run_gh` does not check
for itself: which subcommand (`pr` vs `issue`) and that the body actually reaches `--body` rather
than being swallowed by `gh`'s own flag parsing on a body containing `--` or backticks.
"""

from __future__ import annotations

import sys
from pathlib import Path

_V2 = Path(__file__).resolve().parents[2] / "scripts" / "agent-pipeline" / "v2"
sys.path.insert(0, str(_V2))

from lemd import github  # noqa: E402


class _FakeCompleted:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_a_pr_comment_uses_the_pr_subcommand(monkeypatch):
    captured = {}

    def fake_run(args, **kw):
        captured["args"] = args
        return _FakeCompleted()

    monkeypatch.setattr(github.subprocess, "run", fake_run)
    github.post_comment("owner/repo", "pr", 1483, "hello")
    assert captured["args"][:3] == ["gh", "pr", "comment"]
    assert "1483" in captured["args"]
    assert "--body" in captured["args"]
    assert "hello" in captured["args"]


def test_an_issue_comment_uses_the_issue_subcommand(monkeypatch):
    captured = {}

    def fake_run(args, **kw):
        captured["args"] = args
        return _FakeCompleted()

    monkeypatch.setattr(github.subprocess, "run", fake_run)
    github.post_comment("owner/repo", "issue", 42, "hello")
    assert captured["args"][:3] == ["gh", "issue", "comment"]


def test_a_failed_post_raises_githubunavailable_not_a_bare_exception(monkeypatch):
    """Callers (`daemon._notify_owner_review_needed`) catch `GitHubUnavailable` specifically."""
    monkeypatch.setattr(
        github.subprocess, "run",
        lambda *a, **k: _FakeCompleted(returncode=1, stderr="rate limited"),
    )
    try:
        github.post_comment("owner/repo", "pr", 1, "hello")
        raise AssertionError("expected GitHubUnavailable")
    except github.GitHubUnavailable:
        pass
