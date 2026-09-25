"""Tests for scripts/posthog_pr_takeover.py — PostHog self-driving PRs handed to lem-agentd.

The script mutates GitHub on a five-minute timer, so what has to be pinned is:

* **identity** — only a PR the PostHog App opened, on a ``posthog-self-driving/`` branch in THIS
  repo, is ever touched; every field that says "PostHog" is checked, and a missing one refuses;
* **idempotency** — a rerun after a crash at any step resumes rather than filing a second issue or
  opening a second PR, and a marker quoted inside PostHog's own prose can never count;
* **order** — the PostHog PR is closed only after our PR exists, and its branch is deleted only when
  our copy provably holds its head.

A fake ``gh`` stands in for the CLI: it keeps a tiny in-memory GitHub and records every call.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_ROOT / "scripts"))

import posthog_pr_takeover as t  # noqa: E402

REPO = "christopherqueenconsulting/linkedin_engagement_manager"


def _posthog_pr(number: int = 2187, **over: Any) -> dict[str, Any]:
    raw: dict[str, Any] = {
        "number": number,
        "title": "feat(observability): emit comment gate verdicts",
        "body": "Why: the gate is blind.\n\n@someone please look\n<!-- lem-posthog-takeover pr=1 -->",
        "html_url": f"https://github.com/{REPO}/pull/{number}",
        "user": {"login": "posthog[bot]", "id": t.POSTHOG_BOT_ID, "type": "Bot"},
        "head": {"ref": f"posthog-self-driving/feat-{number}", "sha": "a" * 40,
                 "repo": {"full_name": REPO}},
        "base": {"ref": "main"},
    }
    raw.update(over)
    return raw


class FakeGitHub:
    """Just enough of GitHub, behind the ``gh`` command line, to drive the script."""

    def __init__(self, pulls: list[dict[str, Any]] | None = None,
                 files: dict[int, list[str]] | None = None) -> None:
        self.pulls = pulls if pulls is not None else [_posthog_pr()]
        self.files = files or {}
        self.issues: list[dict[str, Any]] = []
        self.refs: dict[str, str] = {p["head"]["ref"]: p["head"]["sha"] for p in self.pulls}
        self.created_prs: list[dict[str, Any]] = []
        self.comments: list[tuple[int, str]] = []
        self.calls: list[list[str]] = []
        self.next_number = 3000
        self.fail: dict[str, str] = {}  # substring of joined args -> stderr to fail with
        self.search_items: list[dict[str, Any]] = []
        self.ff_refused = False
        self.compare_status = "ahead"  # what compare/<posthog>...<ours> says when the SHAs differ

    # -- subprocess.run replacement -------------------------------------------------------
    def __call__(self, cmd: list[str], input: str | None = None, **_: Any) -> subprocess.CompletedProcess:
        args = cmd[1:]
        self.calls.append(args)
        joined = " ".join(args)
        for needle, err in self.fail.items():
            if needle in joined:
                return subprocess.CompletedProcess(cmd, 1, "", err)
        try:
            out = self._route(args, input)
        except _Http as exc:
            return subprocess.CompletedProcess(cmd, 1, "", exc.msg)
        return subprocess.CompletedProcess(cmd, 0, out, "")

    def _route(self, args: list[str], stdin: str | None) -> str:
        if args[:2] == ["issue", "create"]:
            n = self._num()
            self.issues.insert(0, {"number": n, "title": _opt(args, "--title"), "body": stdin,
                                   "labels": _opts(args, "--label")})
            return f"https://github.com/{REPO}/issues/{n}\n"
        if args[:2] == ["pr", "create"]:
            n = self._num()
            self.created_prs.append({"number": n, "title": _opt(args, "--title"), "body": stdin,
                                     "head": _opt(args, "--head"), "labels": _opts(args, "--label")})
            return f"https://github.com/{REPO}/pull/{n}\n"
        assert args[0] == "api", args
        method, url = args[2], args[3]
        fields = dict(a.split("=", 1) for a in _opts(args, "-f"))
        path = url.removeprefix(f"repos/{REPO}/")
        result = self._api(method, path, fields)
        return "" if result is None else json.dumps(result)

    def _api(self, method: str, path: str, fields: dict[str, str]) -> Any:
        if method == "GET" and path.startswith("pulls?state=open"):
            return self.pulls if "page=1" in path else []
        if method == "GET" and re.match(r"pulls/\d+/files", path):
            n = int(path.split("/")[1])
            return [{"filename": f} for f in self.files.get(n, ["src/x.py"])] if "page=1" in path else []
        if method == "GET" and path.startswith("issues?state=all"):
            return self.issues if "page=1" in path else []
        if method == "GET" and path.startswith("search/issues"):
            return {"items": self.search_items}
        if method == "GET" and path.startswith("git/ref/heads/"):
            ref = path.removeprefix("git/ref/heads/")
            if ref not in self.refs:
                raise _Http("gh: Not Found (HTTP 404)")
            return {"object": {"sha": self.refs[ref]}}
        if method == "GET" and path.startswith("compare/"):
            base, head = path.removeprefix("compare/").split("...")
            head_sha = self.refs.get(head)
            return {"status": "identical" if head_sha == base else self.compare_status}
        if method == "POST" and path == "git/refs":
            self.refs[fields["ref"].removeprefix("refs/heads/")] = fields["sha"]
            return {}
        if method == "PATCH" and path.startswith("git/refs/heads/"):
            if self.ff_refused:
                raise _Http("gh: Update is not a fast forward (HTTP 422)")
            self.refs[path.removeprefix("git/refs/heads/")] = fields["sha"]
            return {}
        if method == "DELETE" and path.startswith("git/refs/heads/"):
            self.refs.pop(path.removeprefix("git/refs/heads/"), None)
            return None
        if method == "GET" and path.startswith("pulls?state=all&head="):
            head = path.split("head=")[1].split("&")[0].split(":", 1)[1]
            return [{"number": p["number"], "head": {"ref": p["head"]}}
                    for p in self.created_prs if p["head"] == head]
        if method == "PATCH" and re.match(r"pulls/\d+$", path):
            n = int(path.split("/")[1])
            for p in self.pulls:
                if p["number"] == n:
                    p["state"] = fields["state"]
            return {}
        if method == "POST" and re.match(r"issues/\d+/comments$", path):
            self.comments.append((int(path.split("/")[1]), fields["body"]))
            return {}
        raise AssertionError(f"unrouted: {method} {path}")

    def _num(self) -> int:
        self.next_number += 1
        return self.next_number

    def mutations(self) -> list[list[str]]:
        return [c for c in self.calls
                if c[:2] in (["issue", "create"], ["pr", "create"])
                or (c[0] == "api" and c[2] != "GET")]


class _Http(Exception):
    def __init__(self, msg: str) -> None:
        super().__init__(msg)
        self.msg = msg


def _opt(args: list[str], name: str) -> str:
    return args[args.index(name) + 1]


def _opts(args: list[str], name: str) -> list[str]:
    return [args[i + 1] for i, a in enumerate(args) if a == name]


def _prs(fake: FakeGitHub) -> list[t.PosthogPr]:
    return t.list_posthog_prs(t.Gh(REPO, runner=fake))


# ---------------------------------------------------------------- identity


def test_a_genuine_posthog_pr_passes_every_check():
    assert t.identity_refusal(_posthog_pr(), REPO) is None


@pytest.mark.parametrize("mutate,reason", [
    (lambda r: r["user"].update(login="posthog"), "author_login"),
    (lambda r: r["user"].update(id=1), "author_id"),
    (lambda r: r["user"].pop("id"), "author_id"),
    (lambda r: r["user"].update(type="User"), "author_type"),
    (lambda r: r["head"]["repo"].update(full_name="evil/fork"), "fork_head"),
    (lambda r: r["head"].update(repo=None), "fork_head"),
    (lambda r: r["head"].update(ref="feature/whatever"), "branch_prefix"),
    (lambda r: r["head"].update(sha=""), "head_sha_unreadable"),
    (lambda r: r["base"].update(ref="develop"), "base_not_main"),
])
def test_every_identity_field_is_checked_and_a_missing_one_refuses(mutate, reason):
    raw = _posthog_pr()
    mutate(raw)
    assert t.identity_refusal(raw, REPO) == reason


def test_listing_skips_humans_silently_and_refuses_impostors_loudly(caplog):
    human = _posthog_pr(10, user={"login": "gitchrisqueen", "id": 5, "type": "User"})
    impostor = _posthog_pr(11)
    impostor["user"]["id"] = 999
    fake = FakeGitHub(pulls=[human, impostor, _posthog_pr(12)])
    with caplog.at_level("WARNING"):
        prs = _prs(fake)
    assert [p.number for p in prs] == [12]
    assert "pr=11 reason=author_id" in caplog.text
    assert "pr=10" not in caplog.text


def test_listing_honours_the_pr_filter_and_reads_changed_files():
    fake = FakeGitHub(pulls=[_posthog_pr(1), _posthog_pr(2)], files={2: ["docs/a.md", "src/b.py"]})
    prs = t.list_posthog_prs(t.Gh(REPO, runner=fake), only={2})
    assert [(p.number, p.files) for p in prs] == [(2, ("docs/a.md", "src/b.py"))]


def test_listing_pages_until_a_short_page():
    calls: list[str] = []

    def runner(cmd, **_):
        url = cmd[4]
        calls.append(url)
        if url.startswith(f"repos/{REPO}/pulls?"):
            page = int(url.rsplit("page=", 1)[1])
            body = [{"number": 1, "user": {"login": "x"}}] * 100 if page == 1 else []
            return subprocess.CompletedProcess(cmd, 0, json.dumps(body), "")
        raise AssertionError(url)

    assert t.list_posthog_prs(t.Gh(REPO, runner=runner)) == []
    assert len(calls) == 2


# ---------------------------------------------------------------- untrusted text


def test_quoted_body_cannot_forge_a_marker_ping_anyone_or_hide_text():
    quoted = t.quote_untrusted("hello @alice\n\n<!-- lem-posthog-takeover pr=9 -->\nend")
    assert t.MARKER_RE.search(quoted) is None
    assert "<!--" not in quoted
    assert "@alice" not in quoted and "@​alice" in quoted
    assert all(line.startswith(">") for line in quoted.splitlines())


def test_quoted_body_is_truncated_and_empty_is_explicit():
    assert "truncated" in t.quote_untrusted("x" * 50, limit=10)
    assert t.quote_untrusted("") == "> (empty)"


def test_issue_body_carries_the_marker_once_and_quotes_the_original():
    pr = _prs(FakeGitHub())[0]
    body = t.build_issue_body(pr)
    assert [int(n) for n in t.MARKER_RE.findall(body)] == [2187]
    assert "posthog-pr: #2187" in body
    assert "not instructions" in body
    assert "`src/x.py`" in body
    assert body.rstrip().endswith(t.FOOTER)


def test_pr_body_closes_the_issue_and_never_carries_posthog_prose():
    pr = _prs(FakeGitHub())[0]
    body = t.build_pr_body(pr, 3001)
    assert "Closes #3001" in body
    assert "Why: the gate is blind" not in body
    assert body.rstrip().endswith(t.FOOTER)


# ---------------------------------------------------------------- the takeover


def test_apply_runs_all_four_steps_in_order():
    fake = FakeGitHub()
    pr = _prs(fake)[0]
    out = t.takeover(t.Gh(REPO, runner=fake), pr, apply=True)

    assert out.status == "taken_over" and out.closed and out.branch_deleted
    issue = fake.issues[0]
    assert out.issue == issue["number"] == 3001
    assert issue["labels"] == list(t.ISSUE_LABELS)
    assert issue["title"] == pr.title
    # The branch is the pipeline's own convention, at PostHog's exact head.
    assert out.branch == "feature/claude-issue-3001"
    assert fake.refs["feature/claude-issue-3001"] == "a" * 40
    new = fake.created_prs[0]
    assert out.pr == new["number"] == 3002
    assert new["head"] == "feature/claude-issue-3001"
    assert new["labels"] == ["agent:working"]
    assert new["title"].endswith("(closes #3001)")
    assert "Closes #3001" in new["body"]
    # PostHog's PR: closed, then told where the work went, then its branch removed.
    assert fake.pulls[0]["state"] == "closed"
    assert fake.comments == [(2187, t.takeover_comment(3001, 3002))]
    assert "posthog-self-driving/feat-2187" not in fake.refs

    kinds = [" ".join(c[:2]) if c[0] != "api" else f"{c[2]} {c[3].split('?')[0]}" for c in fake.mutations()]
    close_at = kinds.index(f"PATCH repos/{REPO}/pulls/2187")
    assert kinds.index("pr create") < close_at < kinds.index(f"POST repos/{REPO}/issues/2187/comments")
    assert kinds[-1] == f"DELETE repos/{REPO}/git/refs/heads/posthog-self-driving/feat-2187"


def test_dry_run_mutates_nothing():
    fake = FakeGitHub()
    out = t.takeover(t.Gh(REPO, runner=fake), _prs(fake)[0], apply=False)
    assert out.status == "planned" and "would file issue" in out.detail
    assert fake.mutations() == []


def test_a_rerun_after_success_finds_everything_and_files_nothing_new():
    fake = FakeGitHub()
    gh = t.Gh(REPO, runner=fake)
    pr = _prs(fake)[0]
    t.takeover(gh, pr, apply=True)
    fake.refs[pr.head_ref] = pr.head_sha  # pretend the delete had not happened yet
    before = len(fake.issues), len(fake.created_prs)
    out = t.takeover(gh, pr, apply=True)
    assert (len(fake.issues), len(fake.created_prs)) == before
    assert (out.issue, out.pr) == (3001, 3002)


@pytest.mark.parametrize("apply,expect", [(False, "would copy"), (True, None)])
def test_resumes_after_a_crash_between_issue_and_branch(apply, expect):
    fake = FakeGitHub()
    pr = _prs(fake)[0]
    fake.issues.append({"number": 50, "body": t.build_issue_body(pr)})
    out = t.takeover(t.Gh(REPO, runner=fake), pr, apply=apply)
    assert out.issue == 50
    if expect:
        assert expect in out.detail
        assert fake.mutations() == []
    else:
        assert fake.refs["feature/claude-issue-50"] == pr.head_sha
        assert not any(c[:2] == ["issue", "create"] for c in fake.calls)


def test_resumes_after_a_crash_between_branch_and_pr_without_rewriting_the_branch():
    fake = FakeGitHub()
    pr = _prs(fake)[0]
    fake.issues.append({"number": 50, "body": t.build_issue_body(pr)})
    fake.refs["feature/claude-issue-50"] = "b" * 40  # the pipeline already pushed on top
    out = t.takeover(t.Gh(REPO, runner=fake), pr, apply=False)
    assert "would open PR" in out.detail
    fake2 = FakeGitHub()
    fake2.issues = fake.issues
    fake2.refs["feature/claude-issue-50"] = "b" * 40
    t.takeover(t.Gh(REPO, runner=fake2), _prs(fake2)[0], apply=True)
    assert fake2.refs["feature/claude-issue-50"] == "b" * 40
    assert not any(c[0] == "api" and c[2] == "POST" and c[3].endswith("git/refs") for c in fake2.calls)


def test_dry_run_with_issue_branch_and_pr_already_present_only_plans_the_close():
    fake = FakeGitHub()
    pr = _prs(fake)[0]
    fake.issues.append({"number": 50, "body": t.build_issue_body(pr)})
    fake.refs["feature/claude-issue-50"] = pr.head_sha
    fake.created_prs.append({"number": 51, "head": "feature/claude-issue-50"})
    out = t.takeover(t.Gh(REPO, runner=fake), pr, apply=False)
    assert (out.issue, out.pr) == (50, 51) and "would close" in out.detail
    assert fake.mutations() == []


def test_a_marker_quoted_in_someone_elses_body_or_on_a_pr_is_not_a_takeover():
    fake = FakeGitHub()
    pr = _prs(fake)[0]
    fake.issues = [
        {"number": 7, "body": "> <!-- lem-posthog-takeover pr=2187 -->"},
        {"number": 8, "body": t.MARKER_TEMPLATE.format(number=2187), "pull_request": {}},
        {"number": 9, "body": t.MARKER_TEMPLATE.format(number=21870)},
    ]
    assert t.find_takeover_issue(t.Gh(REPO, runner=fake), pr.number) is None


def test_search_is_the_fallback_for_an_issue_older_than_the_scan():
    fake = FakeGitHub()
    fake.search_items = [{"number": 12, "body": t.MARKER_TEMPLATE.format(number=2187)}]
    assert t.find_takeover_issue(t.Gh(REPO, runner=fake), 2187) == 12


def test_an_unreadable_search_refuses_rather_than_filing_a_duplicate():
    fake = FakeGitHub()
    fake.fail["search/issues"] = "HTTP 502"
    with pytest.raises(t.GhError):
        t.takeover(t.Gh(REPO, runner=fake), _prs(fake)[0], apply=True)
    assert fake.issues == []


def test_protected_paths_are_skipped_untouched(caplog):
    fake = FakeGitHub(files={2187: ["src/a.py", ".github/workflows/ci.yml"]})
    with caplog.at_level("WARNING"):
        out = t.takeover(t.Gh(REPO, runner=fake), _prs(fake)[0], apply=True)
    assert out.status == "skipped_protected" and ".github/workflows/ci.yml" in out.detail
    assert fake.mutations() == []
    assert t.protected_paths(["scripts/agent-pipeline/tick.sh", "scripts/x.py"]) == [
        "scripts/agent-pipeline/tick.sh"]


# ---------------------------------------------------------------- retiring PostHog's branch


def test_a_branch_posthog_moved_is_fast_forwarded_before_deletion():
    fake = FakeGitHub()
    pr = _prs(fake)[0]
    gh = t.Gh(REPO, runner=fake)
    fake.refs["feature/claude-issue-9"] = pr.head_sha
    fake.refs[pr.head_ref] = "c" * 40  # PostHog pushed after we copied
    fake.compare_status = "behind"
    assert t._retire_posthog_branch(gh, pr, "feature/claude-issue-9") is True
    assert fake.refs["feature/claude-issue-9"] == "c" * 40
    assert pr.head_ref not in fake.refs
    patch = next(c for c in fake.calls if c[0] == "api" and c[2] == "PATCH")
    assert "force=false" in patch


def test_our_branch_already_ahead_is_never_rewritten():
    """A resumed run whose branch already carries pipeline commits deletes PostHog's, touches ours not."""
    fake = FakeGitHub()
    pr = _prs(fake)[0]
    fake.refs["ours"] = "b" * 40
    fake.compare_status = "ahead"
    assert t._retire_posthog_branch(t.Gh(REPO, runner=fake), pr, "ours") is True
    assert fake.refs["ours"] == "b" * 40
    assert not any(c[0] == "api" and c[2] == "PATCH" for c in fake.calls)


@pytest.mark.parametrize("status", ["diverged", ""])
def test_a_branch_ours_does_not_contain_is_kept(status, caplog):
    fake = FakeGitHub()
    pr = _prs(fake)[0]
    fake.refs["ours"] = "b" * 40
    fake.compare_status = status
    with caplog.at_level("WARNING"):
        assert t._retire_posthog_branch(t.Gh(REPO, runner=fake), pr, "ours") is False
    assert pr.head_ref in fake.refs and fake.refs["ours"] == "b" * 40
    assert f"compare={status or 'unreadable'}" in caplog.text


def test_a_refused_fast_forward_keeps_the_branch(caplog):
    fake = FakeGitHub()
    pr = _prs(fake)[0]
    fake.refs["ours"] = "b" * 40
    fake.compare_status = "behind"
    fake.ff_refused = True
    with caplog.at_level("WARNING"):
        assert t._retire_posthog_branch(t.Gh(REPO, runner=fake), pr, "ours") is False
    assert pr.head_ref in fake.refs


def test_an_already_deleted_branch_counts_as_retired():
    fake = FakeGitHub()
    pr = _prs(fake)[0]
    del fake.refs[pr.head_ref]
    assert t._retire_posthog_branch(t.Gh(REPO, runner=fake), pr, "ours") is True


@pytest.mark.parametrize("needle", ["git/ref/heads/posthog", "DELETE", "compare/"])
def test_an_unreadable_or_undeletable_branch_is_kept(needle):
    fake = FakeGitHub()
    pr = _prs(fake)[0]
    fake.refs["ours"] = pr.head_sha
    fake.fail[needle] = "HTTP 500"
    assert t._retire_posthog_branch(t.Gh(REPO, runner=fake), pr, "ours") is False
    assert pr.head_ref in fake.refs


def test_ref_sha_raises_on_anything_but_404():
    fake = FakeGitHub()
    fake.fail["git/ref/heads/x"] = "HTTP 500 server error"
    with pytest.raises(t.GhError):
        t.ref_sha(t.Gh(REPO, runner=fake), "x")


# ---------------------------------------------------------------- CLI


def test_main_dry_run_lists_and_exits_zero(caplog):
    fake = FakeGitHub()
    with caplog.at_level("INFO"):
        assert t.main([], runner=fake) == 0
    assert "status=planned" in caplog.text
    assert fake.mutations() == []


def test_main_apply_takes_over_and_exits_zero():
    fake = FakeGitHub()
    assert t.main(["--apply", "--pr", "2187"], runner=fake) == 0
    assert fake.pulls[0]["state"] == "closed"


def test_main_with_nothing_open_is_a_quiet_success(caplog):
    with caplog.at_level("INFO"):
        assert t.main([], runner=FakeGitHub(pulls=[])) == 0
    assert "no open PostHog" in caplog.text


def test_main_reports_a_listing_failure():
    fake = FakeGitHub()
    fake.fail["pulls?state=open"] = "HTTP 401 Bad credentials"
    assert t.main([], runner=fake) == 1


def test_main_exits_one_when_a_takeover_fails_but_still_tries_the_rest():
    fake = FakeGitHub(pulls=[_posthog_pr(1), _posthog_pr(2)])
    fake.fail["pr create"] = "HTTP 422"
    assert t.main(["--apply"], runner=fake) == 1
    assert sum(1 for c in fake.calls if c[:2] == ["issue", "create"]) == 2


def test_an_unparseable_create_output_is_an_error():
    with pytest.raises(ValueError):
        t._number_from_url("created!")


def test_gh_error_names_404_as_absent():
    assert t.GhError(["api"], 1, "gh: Not Found (HTTP 404)").not_found
    assert not t.GhError(["api"], 1, "HTTP 500").not_found


def test_the_timer_is_gated_and_runs_apply():
    unit = (_ROOT / "scripts/agent-pipeline/systemd/lem-posthog-takeover.service").read_text()
    assert 'POSTHOG_TAKEOVER_ENABLED:-0}" = "1"' in unit
    assert "scripts/posthog_pr_takeover.py --apply" in unit
    timer = (_ROOT / "scripts/agent-pipeline/systemd/lem-posthog-takeover.timer").read_text()
    assert "Unit=lem-posthog-takeover.service" in timer
