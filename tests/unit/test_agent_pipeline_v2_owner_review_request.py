"""Asking the owner for the review GitHub never asks for (#1642).

`main` runs `require_code_owner_reviews: true` with `required_approving_review_count: 0`, and
GitHub's auto-request only fires at count >= 1. Measured 2026-08-17: five pipeline-authored PRs
(#1600, #1602, #1616, #1618, #1620) sat green, armed and `BLOCKED` for hours with an EMPTY Reviewers
sidebar, `reviewDecision: null` and no notification — discoverable only by grepping the daemon's own
decision log. #1616 also carried a `DISMISSED` review from the owner, which is `dismiss_stale_reviews`
silently invalidating a prior approval on the next push: the ask is not once, it recurs.

So three properties are tested here rather than assumed:

* **The ask happens at OPEN**, off a CODEOWNERS path match, not at the end of CI.
* **Only an OPINIONATED review suppresses the ask** — `APPROVED` or `CHANGES_REQUESTED`, a pass
  list. A dismissed approval is pending again, and so is a `COMMENTED` one: submitting either
  removes the owner from `reviewRequests` without satisfying the code-owner gate, so reading one as
  "reviewed" restores the exact silence this exists to end.
* **A missed path match is not a silent wait**: `awaiting_owner_review` is GitHub's own verdict that
  a code-owner review is the last gate, and it asks there too.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_V2 = _ROOT / "scripts" / "agent-pipeline" / "v2"
sys.path.insert(0, str(_V2))

from lemd import codeowners, daemon as daemon_mod, db, github, observe  # noqa: E402
from lemd.config import load  # noqa: E402

GREEN = github.ChecksState(failed=0, pending=0, total=6)
RUNNING = github.ChecksState(failed=0, pending=3, total=6)

#: The real file, trimmed to the shapes it actually uses.
CODEOWNERS_TEXT = """
# Order matters — the LAST matching pattern wins.
/.github/                               @gitchrisqueen
/scripts/agent-pipeline/                @gitchrisqueen
/src/cqc_lem/api/main.py                @gitchrisqueen
/compose/local/database/migrations/     @gitchrisqueen
"""


class _FakeCompleted:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _rules():
    return codeowners.parse(CODEOWNERS_TEXT)


def _facts(**kw) -> dict:
    """A `pr_facts` payload for a pipeline-authored PR nobody has reviewed."""
    base = {
        "number": 1616, "state": "OPEN", "isDraft": False,
        "author": {"login": "app/cqc-lem-agent-pipeline"},
        "files": [{"path": "docs/agent-pipeline-v2.md"}],
        "reviewRequests": [],
        "latestReviews": [],
    }
    base.update(kw)
    return base


def _pr(**kw) -> observe.Snapshot:
    base = dict(kind="pr", number=1616, labels=frozenset({"agent:working"}), branch="feature/x",
                head_sha="abc", checks=GREEN, review_fresh=True, merge_state="CLEAN")
    base.update(kw)
    return observe.Snapshot(**base)


def _acting_daemon(tmp_path: Path) -> daemon_mod.Daemon:
    (tmp_path / "config.env").write_text(
        f"LEMD_DB={tmp_path}/queue.db\nLEMD_SHADOW=0\n"
        "SLUG=christopherqueenconsulting/linkedin_engagement_manager\n"
        "ASSIGNEE=gitchrisqueen\n"
    )
    return daemon_mod.Daemon(load(tmp_path))


def _ledger(base: Path) -> list[dict]:
    path = base / "logs" / "lemd-decisions.ndjson"
    if not path.exists():
        return []
    return [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()]


# ---------------------------------------------------------------- the path matcher


def test_an_anchored_directory_rule_owns_everything_beneath_it():
    rules = _rules()
    assert codeowners.owners_for("scripts/agent-pipeline/v2/lemd/daemon.py", rules) == ("@gitchrisqueen",)
    assert codeowners.owners_for(".github/workflows/tests.yml", rules) == ("@gitchrisqueen",)


def test_an_anchored_file_rule_owns_exactly_that_file():
    rules = _rules()
    assert codeowners.owners_for("src/cqc_lem/api/main.py", rules) == ("@gitchrisqueen",)
    assert codeowners.owners_for("src/cqc_lem/api/routers/admin.py", rules) == ()


def test_an_unowned_path_has_no_owner():
    """The repo deliberately has NO `*` catch-all, so most of the tree is unowned."""
    rules = _rules()
    assert codeowners.owners_for("docs/agent-pipeline-v2.md", rules) == ()
    assert codeowners.owners_for("tests/unit/test_x.py", rules) == ()


def test_an_anchored_rule_does_not_match_the_same_name_deeper_in_the_tree():
    rules = codeowners.parse("/scripts/deploy.sh @o\n")
    assert codeowners.owners_for("scripts/deploy.sh", rules) == ("@o",)
    assert codeowners.owners_for("vendor/scripts/deploy.sh", rules) == ()


def test_an_unanchored_rule_matches_at_any_depth():
    rules = codeowners.parse("secrets.env @o\n")
    assert codeowners.owners_for("secrets.env", rules) == ("@o",)
    assert codeowners.owners_for("compose/local/secrets.env", rules) == ("@o",)


def test_a_star_does_not_cross_a_path_separator():
    rules = codeowners.parse("/scripts/*.sh @o\n")
    assert codeowners.owners_for("scripts/deploy.sh", rules) == ("@o",)
    assert codeowners.owners_for("scripts/agent-pipeline/v2/cutover.sh", rules) == ()


def test_a_double_star_does_cross_path_separators():
    rules = codeowners.parse("/scripts/**/*.sh @o\n")
    assert codeowners.owners_for("scripts/agent-pipeline/v2/cutover.sh", rules) == ("@o",)


def test_the_last_matching_rule_wins():
    """CODEOWNERS precedence, and the reason `owners_for` does not return early."""
    rules = codeowners.parse("/.github/ @first\n/.github/CODEOWNERS @second\n")
    assert codeowners.owners_for(".github/CODEOWNERS", rules) == ("@second",)
    assert codeowners.owners_for(".github/dependabot.yml", rules) == ("@first",)


def test_a_later_rule_with_no_owner_un_owns_the_path():
    """An ownerless line is kept, or the broader earlier rule would keep winning."""
    rules = codeowners.parse("/.github/ @first\n/.github/notes.md\n")
    assert codeowners.owners_for(".github/notes.md", rules) == ()


def test_comments_and_blank_lines_are_skipped():
    rules = codeowners.parse("# a comment\n\n   \n/x/ @o  # trailing note\n")
    assert len(rules) == 1
    assert codeowners.owners_for("x/y", rules) == ("@o",)


def test_matches_any_is_true_when_one_of_many_paths_is_owned():
    rules = _rules()
    changed = ("CLAUDE.md", "docs/x.md",
               "compose/local/database/migrations/V20260816224626__add.sql")
    assert codeowners.matches_any(changed, rules) is True
    assert codeowners.matches_any(("CLAUDE.md", "docs/x.md"), rules) is False


def test_a_pattern_that_is_only_separators_is_skipped():
    assert codeowners.parse("/ @o\n//  @o\n") == ()


def test_matches_any_is_false_with_no_rules():
    """The fail-safe direction: no rules read means no match, never 'assume owned'."""
    assert codeowners.matches_any(("scripts/agent-pipeline/x.py",), ()) is False


# ---------------------------------------------------------------- fetching the rules


def test_rules_are_fetched_once_and_cached(monkeypatch):
    codeowners.clear_cache()
    calls = []

    def fake_json(args, **kw):
        calls.append(args)
        import base64
        return {"content": base64.b64encode(CODEOWNERS_TEXT.encode()).decode()}

    monkeypatch.setattr(codeowners.github, "gh_json", fake_json)
    first = codeowners.rules_for("o/r", now=1000.0)
    second = codeowners.rules_for("o/r", now=1000.0 + codeowners.CACHE_TTL_SECONDS - 1)
    assert first == second
    assert len(calls) == 1
    assert ".github/CODEOWNERS" in calls[0][1]


def test_the_cache_expires(monkeypatch):
    codeowners.clear_cache()
    calls = []

    def fake_json(args, **kw):
        calls.append(args)
        import base64
        return {"content": base64.b64encode(CODEOWNERS_TEXT.encode()).decode()}

    monkeypatch.setattr(codeowners.github, "gh_json", fake_json)
    codeowners.rules_for("o/r", now=1000.0)
    codeowners.rules_for("o/r", now=1000.0 + codeowners.CACHE_TTL_SECONDS + 1)
    assert len(calls) == 2


def test_a_root_codeowners_is_read_when_the_github_one_is_absent(monkeypatch):
    """GitHub reads three locations; so does this, in GitHub's own precedence order."""
    codeowners.clear_cache()
    import base64

    def fake_json(args, **kw):
        if args[1].endswith(".github/CODEOWNERS"):
            raise github.GitHubUnavailable("404 Not Found")
        return {"content": base64.b64encode(b"/x/ @o\n").decode()}

    monkeypatch.setattr(codeowners.github, "gh_json", fake_json)
    assert codeowners.owners_for("x/y", codeowners.rules_for("o/r", now=1000.0)) == ("@o",)


def test_undecodable_codeowners_content_yields_no_rules(monkeypatch):
    codeowners.clear_cache()
    monkeypatch.setattr(codeowners.github, "gh_json", lambda *a, **k: {"content": "!!not base64!!"})
    assert codeowners.rules_for("o/r", now=1000.0) == ()


def test_an_unreadable_codeowners_yields_no_rules_rather_than_raising(monkeypatch):
    """A GitHub outage must not take an observation pass down, and must not invent ownership."""
    codeowners.clear_cache()

    def boom(args, **kw):
        raise github.GitHubUnavailable("500")

    monkeypatch.setattr(codeowners.github, "gh_json", boom)
    assert codeowners.rules_for("o/r", now=1000.0) == ()


# ---------------------------------------------------------------- who still owes a review


def test_an_unreviewed_pr_needs_the_request():
    assert github.owner_review_pending(_facts(), "gitchrisqueen") is True


def test_an_already_requested_reviewer_is_not_re_requested():
    facts = _facts(reviewRequests=[{"login": "gitchrisqueen"}])
    assert github.owner_review_pending(facts, "gitchrisqueen") is False


def test_a_dismissed_approval_is_pending_again():
    """`dismiss_stale_reviews` silently invalidates a prior approval on the next push (#1616)."""
    facts = _facts(latestReviews=[
        {"author": {"login": "github-advanced-security"}, "state": "COMMENTED"},
        {"author": {"login": "gitchrisqueen"}, "state": "DISMISSED"},
    ])
    assert github.owner_review_pending(facts, "gitchrisqueen") is True


def test_a_live_approval_is_not_re_requested():
    facts = _facts(latestReviews=[{"author": {"login": "gitchrisqueen"}, "state": "APPROVED"}])
    assert github.owner_review_pending(facts, "gitchrisqueen") is False


def test_changes_requested_is_not_re_requested():
    """Submitting a review REMOVES them from `reviewRequests`, so this would nag on a loop."""
    facts = _facts(latestReviews=[
        {"author": {"login": "gitchrisqueen"}, "state": "CHANGES_REQUESTED"}])
    assert github.owner_review_pending(facts, "gitchrisqueen") is False


def test_a_comment_only_review_is_pending_again():
    """The regression that would re-create #1642 in a shape nobody would look for.

    Leaving ONE inline remark submits a `COMMENTED` review. That removes the owner from
    `reviewRequests` while satisfying no part of the code-owner gate — so reading it as "reviewed"
    would put the PR back exactly where the issue found five of them: BLOCKED, green, armed, with
    an empty Reviewers sidebar and nothing left that would ever ask again.
    """
    facts = _facts(latestReviews=[
        {"author": {"login": "gitchrisqueen"}, "state": "COMMENTED"}])
    assert github.owner_review_pending(facts, "gitchrisqueen") is True


def test_an_unknown_review_state_is_pending_again():
    """A pass list, not a deny list: only APPROVED/CHANGES_REQUESTED suppress the ask."""
    facts = _facts(latestReviews=[{"author": {"login": "gitchrisqueen"}, "state": "PENDING"}])
    assert github.owner_review_pending(facts, "gitchrisqueen") is True
    assert github.OPINIONATED_REVIEW_STATES == {"APPROVED", "CHANGES_REQUESTED"}


def test_a_comment_only_review_still_suppresses_while_the_request_stands():
    """What bounds the re-ask: the request itself, so a COMMENTED review costs one, not a loop."""
    facts = _facts(reviewRequests=[{"login": "gitchrisqueen"}],
                   latestReviews=[{"author": {"login": "gitchrisqueen"}, "state": "COMMENTED"}])
    assert github.owner_review_pending(facts, "gitchrisqueen") is False


def test_the_owner_is_never_asked_to_review_their_own_pr():
    """GitHub refuses the request, so asking would only ever error."""
    facts = _facts(author={"login": "gitchrisqueen"})
    assert github.owner_review_pending(facts, "gitchrisqueen") is False


def test_a_merged_or_closed_pr_owes_nothing():
    assert github.owner_review_pending(_facts(state="MERGED"), "gitchrisqueen") is False
    assert github.owner_review_pending(_facts(state="CLOSED"), "gitchrisqueen") is False


def test_a_draft_owes_nothing():
    """A park DRAFTS the PR, so this is what stops a ping on work just parked."""
    assert github.owner_review_pending(_facts(isDraft=True), "gitchrisqueen") is False


def test_no_owner_configured_asks_nobody():
    assert github.owner_review_pending(_facts(), "") is False


def test_the_login_comparison_is_case_insensitive():
    facts = _facts(reviewRequests=[{"login": "GitChrisQueen"}])
    assert github.owner_review_pending(facts, "gitchrisqueen") is False


# ---------------------------------------------------------------- the write


def test_request_reviewer_edits_the_pr_with_add_reviewer(monkeypatch):
    captured = {}

    def fake_run(args, **kw):
        captured["args"] = args
        return _FakeCompleted()

    monkeypatch.setattr(github.subprocess, "run", fake_run)
    github.request_reviewer("owner/repo", 1616, "gitchrisqueen")
    assert captured["args"][:3] == ["gh", "pr", "edit"]
    assert "--add-reviewer" in captured["args"]
    assert "gitchrisqueen" in captured["args"]
    assert "1616" in captured["args"]


def test_a_failed_request_raises_githubunavailable(monkeypatch):
    monkeypatch.setattr(
        github.subprocess, "run",
        lambda *a, **k: _FakeCompleted(returncode=1, stderr="rate limited"),
    )
    try:
        github.request_reviewer("owner/repo", 1, "gitchrisqueen")
    except github.GitHubUnavailable:
        return
    raise AssertionError("a failed reviewer request must not pass silently")


# ---------------------------------------------------------------- the snapshot


def test_snapshot_marks_a_codeowned_pr(monkeypatch):
    codeowners.clear_cache()
    facts = _facts(files=[{"path": "scripts/agent-pipeline/v2/lemd/daemon.py"}],
                   mergeStateStatus="BLOCKED",
                   headRepositoryOwner={"login": "christopherqueenconsulting"})
    monkeypatch.setattr(observe.github, "pr_facts", lambda *a, **k: facts)
    monkeypatch.setattr(observe.github, "checks_for", lambda *a, **k: GREEN)
    monkeypatch.setattr(observe.github, "merge_queue_state", lambda *a, **k: "")
    monkeypatch.setattr(observe.github, "review_state", lambda *a, **k: github.ReviewState(True, 0))
    monkeypatch.setattr(codeowners, "rules_for", lambda *a, **k: _rules())
    snap = observe.snapshot_pr("christopherqueenconsulting/linkedin_engagement_manager", 1616,
                               owner="gitchrisqueen")
    assert snap.owner_review_pending is True
    assert snap.codeowned is True


def test_snapshot_leaves_an_unowned_pr_alone(monkeypatch):
    codeowners.clear_cache()
    facts = _facts(files=[{"path": "docs/agent-pipeline-v2.md"}],
                   headRepositoryOwner={"login": "christopherqueenconsulting"})
    monkeypatch.setattr(observe.github, "pr_facts", lambda *a, **k: facts)
    monkeypatch.setattr(observe.github, "checks_for", lambda *a, **k: GREEN)
    monkeypatch.setattr(observe.github, "merge_queue_state", lambda *a, **k: "")
    monkeypatch.setattr(observe.github, "review_state", lambda *a, **k: github.ReviewState(True, 0))
    monkeypatch.setattr(codeowners, "rules_for", lambda *a, **k: _rules())
    snap = observe.snapshot_pr("christopherqueenconsulting/linkedin_engagement_manager", 1616,
                               owner="gitchrisqueen")
    assert snap.codeowned is False


def test_an_unreadable_pr_asks_for_nothing(monkeypatch):
    def boom(*a, **k):
        raise github.GitHubUnavailable("down")

    monkeypatch.setattr(observe.github, "pr_facts", boom)
    snap = observe.snapshot_pr("o/r", 1, owner="gitchrisqueen")
    assert snap.codeowned is False
    assert snap.owner_review_pending is False


# ---------------------------------------------------------------- the daemon


def test_the_request_fires_on_the_first_observation_while_ci_is_still_running(tmp_path, monkeypatch):
    """Acceptance 1: the owner is in the Reviewers sidebar the moment the PR opens.

    The decision here is `ci_running` — nothing about this PR is waiting on a review yet — which is
    exactly the point: the ask must not wait for the green/BLOCKED state at the end of CI.
    """
    dm = _acting_daemon(tmp_path)
    asked = []
    monkeypatch.setattr(github, "request_reviewer", lambda *a, **k: asked.append(a))
    try:
        db.upsert_item(dm.conn, kind="pr", number=1616, state=db.STATE_READY, branch="feature/x")
        snap = _pr(checks=RUNNING, codeowned=True, owner_review_pending=True)
        monkeypatch.setattr(observe, "snapshot_pr", lambda *a, **k: snap)
        dm._observe_one(db.get_item(dm.conn, "pr", 1616))
        assert db.get_item(dm.conn, "pr", 1616)["state"] == db.STATE_WAIT_CI
        assert asked == [("christopherqueenconsulting/linkedin_engagement_manager", 1616,
                          "gitchrisqueen")]
    finally:
        dm.conn.close()


def test_the_request_is_recorded_in_the_decision_ledger(tmp_path, monkeypatch):
    """The absence of this signal was FOUND by grepping this file — so its presence lives there."""
    dm = _acting_daemon(tmp_path)
    monkeypatch.setattr(github, "request_reviewer", lambda *a, **k: None)
    try:
        db.upsert_item(dm.conn, kind="pr", number=1616, state=db.STATE_READY, branch="feature/x")
        snap = _pr(checks=RUNNING, codeowned=True, owner_review_pending=True)
        monkeypatch.setattr(observe, "snapshot_pr", lambda *a, **k: snap)
        dm._observe_one(db.get_item(dm.conn, "pr", 1616))
        rows = [r for r in _ledger(tmp_path) if r.get("stage") == "owner_review_request"]
        assert rows == [{"ts": rows[0]["ts"], "shadow": False, "stage": "owner_review_request",
                         "kind": "pr", "number": 1616, "reason": "codeowners_path",
                         "requested": True}]
    finally:
        dm.conn.close()


def test_a_failed_request_does_not_take_the_observation_pass_down(tmp_path, monkeypatch):
    dm = _acting_daemon(tmp_path)

    def boom(*a, **k):
        raise github.GitHubUnavailable("rate limited")

    monkeypatch.setattr(github, "request_reviewer", boom)
    try:
        db.upsert_item(dm.conn, kind="pr", number=1616, state=db.STATE_READY, branch="feature/x")
        snap = _pr(checks=RUNNING, codeowned=True, owner_review_pending=True)
        monkeypatch.setattr(observe, "snapshot_pr", lambda *a, **k: snap)
        dm._observe_one(db.get_item(dm.conn, "pr", 1616))
        assert db.get_item(dm.conn, "pr", 1616)["state"] == db.STATE_WAIT_CI
        rows = [r for r in _ledger(tmp_path) if r.get("stage") == "owner_review_request"]
        assert rows[0]["requested"] is False
    finally:
        dm.conn.close()


def test_nothing_is_asked_when_the_owner_already_holds_the_request(tmp_path, monkeypatch):
    dm = _acting_daemon(tmp_path)
    asked = []
    monkeypatch.setattr(github, "request_reviewer", lambda *a, **k: asked.append(a))
    try:
        db.upsert_item(dm.conn, kind="pr", number=1616, state=db.STATE_READY, branch="feature/x")
        snap = _pr(checks=RUNNING, codeowned=True, owner_review_pending=False)
        monkeypatch.setattr(observe, "snapshot_pr", lambda *a, **k: snap)
        dm._observe_one(db.get_item(dm.conn, "pr", 1616))
        assert asked == []
    finally:
        dm.conn.close()


def test_a_dismissed_approval_re_triggers_the_request_on_the_next_observation(tmp_path, monkeypatch):
    """Acceptance 2: `dismiss_stale_reviews` must not send the PR silent again."""
    dm = _acting_daemon(tmp_path)
    asked = []
    monkeypatch.setattr(github, "request_reviewer", lambda *a, **k: asked.append(a))
    try:
        db.upsert_item(dm.conn, kind="pr", number=1616, state=db.STATE_READY, branch="feature/x")
        # Approved and requested: nothing owed.
        monkeypatch.setattr(observe, "snapshot_pr", lambda *a, **k: _pr(
            checks=RUNNING, codeowned=True, owner_review_pending=False))
        dm._observe_one(db.get_item(dm.conn, "pr", 1616))
        assert asked == []
        # A pushed commit dismissed that approval — the snapshot reports it pending again.
        monkeypatch.setattr(observe, "snapshot_pr", lambda *a, **k: _pr(
            checks=RUNNING, head_sha="def", codeowned=True, owner_review_pending=True))
        dm._observe_one(db.get_item(dm.conn, "pr", 1616))
        assert len(asked) == 1
    finally:
        dm.conn.close()


def test_owner_review_required_asks_even_when_the_path_match_missed(tmp_path, monkeypatch):
    """The authoritative fallback: GitHub says a code-owner review is the only gate left."""
    dm = _acting_daemon(tmp_path)
    asked = []
    monkeypatch.setattr(github, "request_reviewer", lambda *a, **k: asked.append(a))
    monkeypatch.setattr(github, "post_comment", lambda *a, **k: None)
    try:
        db.upsert_item(dm.conn, kind="pr", number=1616, state=db.STATE_WAIT_CI, branch="feature/x")
        snap = _pr(merge_state="BLOCKED", auto_merge=True, queue_state="",
                   codeowned=False, owner_review_pending=True)
        monkeypatch.setattr(observe, "snapshot_pr", lambda *a, **k: snap)
        dm._observe_one(db.get_item(dm.conn, "pr", 1616))
        assert db.get_item(dm.conn, "pr", 1616)["state"] == db.STATE_WAIT_OWNER_REVIEW
        assert len(asked) == 1
        rows = [r for r in _ledger(tmp_path) if r.get("stage") == "owner_review_request"]
        assert rows[0]["reason"] == "owner_review_required"
    finally:
        dm.conn.close()


def test_an_inadmissible_pr_is_never_asked_about(tmp_path, monkeypatch):
    """A fork PR or a release-please PR is not the pipeline's to route the owner at."""
    dm = _acting_daemon(tmp_path)
    asked = []
    monkeypatch.setattr(github, "request_reviewer", lambda *a, **k: asked.append(a))
    try:
        db.upsert_item(dm.conn, kind="pr", number=1616, state=db.STATE_READY,
                       branch="release-please--branches--main")
        snap = _pr(branch="release-please--branches--main", checks=RUNNING,
                   codeowned=True, owner_review_pending=True)
        monkeypatch.setattr(observe, "snapshot_pr", lambda *a, **k: snap)
        dm._observe_one(db.get_item(dm.conn, "pr", 1616))
        assert asked == []
    finally:
        dm.conn.close()


def test_shadow_mode_asks_nobody(tmp_path, monkeypatch):
    """Shadow mode observes and logs; it must not write to GitHub."""
    (tmp_path / "config.env").write_text(
        f"LEMD_DB={tmp_path}/queue.db\nLEMD_SHADOW=1\nASSIGNEE=gitchrisqueen\n")
    dm = daemon_mod.Daemon(load(tmp_path))
    asked = []
    monkeypatch.setattr(github, "request_reviewer", lambda *a, **k: asked.append(a))
    try:
        db.upsert_item(dm.conn, kind="pr", number=1616, state=db.STATE_READY, branch="feature/x")
        snap = _pr(checks=RUNNING, codeowned=True, owner_review_pending=True)
        monkeypatch.setattr(observe, "snapshot_pr", lambda *a, **k: snap)
        dm._observe_one(db.get_item(dm.conn, "pr", 1616))
        assert asked == []
    finally:
        dm.conn.close()


def test_an_issue_is_never_asked_about(tmp_path, monkeypatch):
    dm = _acting_daemon(tmp_path)
    asked = []
    monkeypatch.setattr(github, "request_reviewer", lambda *a, **k: asked.append(a))
    try:
        db.upsert_item(dm.conn, kind="issue", number=1642, state=db.STATE_READY)
        snap = observe.Snapshot(kind="issue", number=1642, labels=frozenset({"agent:ready"}),
                                work_exists=False, codeowned=True, owner_review_pending=True)
        monkeypatch.setattr(observe, "snapshot_issue", lambda *a, **k: snap)
        dm._observe_one(db.get_item(dm.conn, "issue", 1642))
        assert asked == []
    finally:
        dm.conn.close()
