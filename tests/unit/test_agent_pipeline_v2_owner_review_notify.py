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


# ---------------------------------------------------------------- posted_comment_opening


def _thread(monkeypatch, comments: list[dict], seen: list | None = None) -> None:
    def fake(args, **k):
        if seen is not None:
            seen.append(args)
        return {"comments": comments}

    monkeypatch.setattr(github, "gh_json", fake)


OPENING = "🔔 **Owner approval needed**"


def test_an_earlier_notice_by_the_app_is_found(monkeypatch):
    """GraphQL `--json comments` gives the App's bare slug; that suppresses, never grants."""
    monkeypatch.setenv("GH_APP_BOT_LOGIN", "cqc-lem-agent-pipeline[bot]")
    seen: list = []
    _thread(monkeypatch, [{"author": {"login": "cqc-lem-agent-pipeline"},
                           "body": f"{OPENING} — every required check is green"}], seen)
    assert github.posted_comment_opening("o/r", "pr", 2568, OPENING) is True
    assert seen[0][:3] == ["pr", "view", "2568"]


def test_the_same_words_from_anyone_else_or_mid_comment_do_not_count(monkeypatch):
    monkeypatch.setenv("GH_APP_BOT_LOGIN", "cqc-lem-agent-pipeline[bot]")
    _thread(monkeypatch, [
        {"author": {"login": "stranger"}, "body": f"{OPENING} — fake"},
        {"author": {"login": "cqc-lem-agent-pipeline"}, "body": f"quoting: {OPENING}"},
    ])
    assert github.posted_comment_opening("o/r", "pr", 1, OPENING) is False
    _thread(monkeypatch, [{"author": {"login": "GitChrisQueen"}, "body": f"  {OPENING} — mine"}])
    assert github.posted_comment_opening("o/r", "pr", 1, OPENING, owner="gitchrisqueen") is True


def test_an_unreadable_thread_is_none(monkeypatch):
    def boom(*a, **k):
        raise github.GitHubUnavailable("down")

    monkeypatch.setattr(github, "gh_json", boom)
    assert github.posted_comment_opening("o/r", "pr", 1, OPENING) is None
