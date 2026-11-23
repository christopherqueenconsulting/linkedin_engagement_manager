"""An owner's GitHub APPROVED review answers a parked PR's Decision Comment as `ok`.

Before this, un-parking a PR took two owner touches: reply `ok` under the Decision Comment AND
approve on GitHub. Now the approval alone is the answer — held to the comment answer's trust rules
plus three of its own: the owner (or a listed answer login) and never a Bot or the App, the PR's
CURRENT head, and submitted after the latest Decision Comment (and after every trusted reply to it).
It routes through the same `unpark.sh`, spent once under `review:<id>`, and is re-checked at
execution time by `v2_owner_approved` — whose shipped bash runs below against a stub `gh`.
"""

from __future__ import annotations

import itertools
import json
import re
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

_PIPELINE = Path(__file__).resolve().parents[2] / "scripts" / "agent-pipeline"
_V2 = _PIPELINE / "v2"
sys.path.insert(0, str(_V2))

from lemd import answers, github, observe  # noqa: E402

TTLS = dict(ttl_ci=1800, ttl_review=3600, ttl_queue=900, ttl_parked=21600)
OWNER = "gitchrisqueen"
APP = "cqc-lem-agent-pipeline[bot]"
HEAD = "a" * 40
OLD = "b" * 40
DECISION = "🛑 **Human decision needed** — reply with option letters"
T0, T1, T2, T3 = ("2026-11-20T10:00:00Z", "2026-11-20T11:00:00Z",
                  "2026-11-20T12:00:00Z", "2026-11-20T13:00:00Z")


def comment(body: str, login: str = OWNER, at: str = T1, cid: str = "c1") -> dict:
    """One comment as `gh --json comments` returns it."""
    return {"id": cid, "body": body, "author": {"login": login}, "createdAt": at,
            "url": f"https://github.com/o/r/pull/7#issuecomment-{next(_IDS)}"}


_IDS = itertools.count(1)


def review(rid: int = 11, login: str = OWNER, state: str = "APPROVED", commit: str = HEAD,
           at: str = T2, user_type: str | None = None, body: str = "") -> dict:
    """One review as REST `pulls/{n}/reviews` returns it — the author is TYPED."""
    if user_type is None:
        user_type = "Bot" if login.endswith("[bot]") else "User"
    return {"id": rid, "user": {"login": login, "type": user_type}, "state": state,
            "commit_id": commit, "submitted_at": at, "body": body}


MENU = [comment(DECISION, APP.removesuffix("[bot]"), at=T0, cid="menu")]


def approve(reviews, comments=None, head=HEAD, **kw):
    return answers.approval(MENU if comments is None else comments, reviews, head, OWNER, **kw)


# ---------------------------------------------------------------- the pure rule


def test_the_owners_approval_of_the_current_head_after_the_menu_is_ok():
    got = approve([review()])
    assert got is not None and got.actionable
    assert (got.comment_id, got.verdict) == ("review:11", "approval")


@pytest.mark.parametrize("login,user_type", [
    (APP, "Bot"),                                   # the pipeline App answering its own question
    ("codecov[bot]", "Bot"),                        # any bot
    (APP.removesuffix("[bot]"), "User"),            # a human squatting the App's bare slug
    (OWNER, "Bot"),                                 # the owner's login, but not typed a User
    ("writer", "User"),                             # a write collaborator is not the owner's sign-off
])
def test_an_approval_from_anyone_but_the_owner_is_not_an_answer(login, user_type):
    assert approve([review(login=login, user_type=user_type)]) is None


def test_a_listed_answer_login_approves_but_a_listed_bot_never_does():
    assert approve([review(login="owner-second")], answer_logins=frozenset({"owner-second"}))
    assert approve([review(login="cqc-ea[bot]")], answer_logins=frozenset({"cqc-ea[bot]"})) is None


def test_a_stale_approval_of_an_older_head_is_not_an_answer():
    assert approve([review(commit=OLD)]) is None


def test_an_approval_before_the_decision_comment_is_not_an_answer():
    assert approve([review(at=T0)]) is None
    assert approve([review(at="2026-11-20T09:00:00Z")]) is None


def test_no_menu_means_an_approval_answers_nothing():
    assert approve([review()], comments=[]) is None


@pytest.mark.parametrize("state", ["COMMENTED", "CHANGES_REQUESTED", "DISMISSED", "PENDING"])
def test_only_an_approved_state_answers(state):
    assert approve([review(state=state)]) is None


def test_a_later_non_approval_takes_the_approval_back():
    assert approve([review(rid=11, at=T2), review(rid=12, state="CHANGES_REQUESTED", at=T3)]) is None


def test_a_trusted_reply_after_the_approval_outranks_it():
    """"Hold off" typed after approving still holds — the newest trusted act decides."""
    later = MENU + [comment("hold off, not yet", at=T3, cid="h")]
    assert approve([review(at=T2)], comments=later) is None


def test_a_hold_in_the_review_body_holds():
    got = approve([review(body="looks good but don't merge until Friday")])
    assert got is not None and got.verdict == "hold" and not got.actionable


def test_the_wrong_head_or_owner_is_never_guessed():
    assert approve([review()], head="") is None
    assert answers.approval(MENU, [review()], HEAD, "") is None


# ---------------------------------------------------------------- the read


def _serve(monkeypatch, comments, reviews=None, fail_reviews=False):
    calls = []

    def gh(args, **_kw):
        calls.append(args)
        if args[0] == "api" and args[1].endswith("/reviews"):
            if fail_reviews:
                raise github.GitHubUnavailable("rc=1")
            return [reviews or []]
        if args[0] == "api":
            raise github.GitHubUnavailable("(HTTP 404)")
        return {"comments": comments}

    monkeypatch.setattr(github, "_PERMISSION_CACHE", {})
    monkeypatch.setattr(github, "gh_json", gh)
    return calls


def test_read_thread_routes_an_approval(monkeypatch):
    _serve(monkeypatch, MENU, [review()])
    got = answers.read_thread("o/r", "pr", 7, OWNER, head_sha=HEAD)
    assert got.answer.comment_id == "review:11" and got.menu_posted is True


def test_an_approval_outranks_an_older_comment_answer(monkeypatch):
    _serve(monkeypatch, MENU + [comment("1B", at=T1, cid="a1")], [review(at=T2)])
    assert answers.read_thread("o/r", "pr", 7, OWNER, head_sha=HEAD).answer.comment_id == "review:11"


def test_an_unreadable_review_read_is_not_an_answer(monkeypatch):
    _serve(monkeypatch, MENU, fail_reviews=True)
    got = answers.read_thread("o/r", "pr", 7, OWNER, head_sha=HEAD)
    assert got.answer is None and got.menu_posted is True


def test_an_unreadable_review_read_still_lets_a_comment_answer(monkeypatch):
    _serve(monkeypatch, MENU + [comment("1B", cid="a1")], fail_reviews=True)
    assert answers.read_thread("o/r", "pr", 7, OWNER, head_sha=HEAD).answer.comment_id == "a1"


def test_issues_never_read_reviews(monkeypatch):
    calls = _serve(monkeypatch, MENU, [review()])
    assert answers.read_thread("o/r", "issue", 7, OWNER, head_sha=HEAD).answer is None
    assert not any(a[0] == "api" and a[1].endswith("/reviews") for a in calls)


def test_no_menu_costs_no_review_read(monkeypatch):
    calls = _serve(monkeypatch, [], [review()])
    answers.read_thread("o/r", "pr", 7, OWNER, head_sha=HEAD)
    assert not any(a[0] == "api" for a in calls)


def test_snapshot_pr_hands_the_head_to_the_thread_read(monkeypatch):
    seen = {}
    monkeypatch.setattr(answers, "read_thread",
                        lambda *a, **k: seen.update(k) or answers.Thread(None, True))
    observe._thread_for("o/r", "pr", 7, frozenset({"needs-human"}), OWNER, head_sha=HEAD)
    assert seen["head_sha"] == HEAD
    src = (_V2 / "lemd" / "observe.py").read_text()
    assert 'head_sha=facts.get("headRefOid")' in src


# ---------------------------------------------------------------- decide()


def _held(**kw) -> observe.Snapshot:
    base = dict(kind="pr", number=7, labels=frozenset({"needs-human"}), state="OPEN",
                branch="feature/x", head_sha=HEAD)
    base.update(kw)
    return observe.Snapshot(**base)


def test_an_approval_un_parks_through_the_same_action():
    got = observe.decide(_held(answer=answers.Answer("review:11", "approval", "APPROVED")), **TTLS)
    assert (got.action, got.mode, got.reason) == (observe.ACT_UNPARK, "unpark", "owner_approved")


def test_an_approval_is_spent_once():
    snap = _held(answer=answers.Answer("review:11", "approval", "x"), answer_routed="review:11")
    assert observe.decide(snap, **TTLS).reason == "human_hold:answer_already_routed"


def test_an_armed_hold_still_disarms_before_an_approval_un_parks():
    snap = _held(answer=answers.Answer("review:11", "approval", "x"), auto_merge=True)
    assert observe.decide(snap, **TTLS).action == observe.ACT_DISARM


# ---------------------------------------------------------------- the shipped execution-time re-check

GUARDS = (_PIPELINE / "lib" / "guards.sh").read_text(encoding="utf-8")
COMMON = (_V2 / "actions" / "common.sh").read_text(encoding="utf-8")


def _fn(src: str, name: str) -> str:
    m = re.search(rf"\n{name}\(\) \{{.*?\n\}}\n", "\n" + src, re.S)
    assert m, f"{name} not found"
    return m.group(0)


SHELL = "".join(_fn(GUARDS, n) for n in (
    "pipeline_app_login", "comment_login_is_app", "comment_login_names_app", "comment_author_trusted",
    "trusted_answer_login", "typed_answer_login", "answer_author_trusted",
)) + _fn(COMMON, "v2_owner_answered") + _fn(COMMON, "v2_owner_approved")

_GH_STUB = r'''#!/usr/bin/env bash
if [ "$1" = api ] && [ "${2%/reviews}" != "$2" ]; then
  [ -n "${REVIEWS_FAIL:-}" ] && exit 1
  printf '%s' "$REVIEWS"; exit 0
fi
if [ "$1" = api ]; then echo "gh: not a user (HTTP 404)" >&2; exit 1; fi
case " $* " in *" headRefOid "*) [ -n "${HEAD_FAIL:-}" ] && exit 1; echo "$HEAD"; exit 0 ;; esac
expr=""
while [ $# -gt 0 ]; do [ "$1" = "--jq" ] && { expr="$2"; shift; }; shift; done
printf '%s' "$COMMENTS" | jq -r "$expr"
'''


def _sh(tmp_path, call: str, comments, reviews, **env) -> bool:
    if shutil.which("jq") is None:
        pytest.skip("the shipped filters are jq")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    (bin_dir / "gh").write_text(_GH_STUB, encoding="utf-8")
    (bin_dir / "gh").chmod(0o755)
    script = ("set -uo pipefail\n" f'SLUG="o/r"; ASSIGNEE="{OWNER}"\n' 'log() { echo "LOG: $*"; }\n'
              f"{SHELL}\n{textwrap.dedent(call)} && echo YES || echo NO")
    run_env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": str(tmp_path), "HEAD": HEAD,
               "COMMENTS": json.dumps({"comments": comments}), "REVIEWS": json.dumps([reviews])}
    run_env.update(env)
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=run_env, timeout=60)
    return r.stdout.strip().splitlines()[-1] == "YES"


def _approved(tmp_path, reviews, comments=None, want="11", kind="pr", **env) -> bool:
    return _sh(tmp_path, f'v2_owner_answered {kind} 7 "review:{want}"',
               MENU if comments is None else comments, reviews, **env)


class TestV2OwnerApproved:
    def test_the_owners_current_approval_after_the_menu_is_accepted(self, tmp_path):
        assert _approved(tmp_path, [review()])

    @pytest.mark.parametrize("login,user_type", [(APP, "Bot"), ("codecov[bot]", "Bot"),
                                                 (APP.removesuffix("[bot]"), "User"), ("writer", "User")])
    def test_anyone_but_the_owner_is_refused(self, tmp_path, login, user_type):
        assert not _approved(tmp_path, [review(login=login, user_type=user_type)])

    def test_a_stale_head_is_refused(self, tmp_path):
        assert not _approved(tmp_path, [review(commit=OLD)])

    def test_an_approval_before_the_menu_is_refused(self, tmp_path):
        assert not _approved(tmp_path, [review(at="2026-11-20T09:00:00Z")])

    @pytest.mark.parametrize("state", ["COMMENTED", "CHANGES_REQUESTED", "DISMISSED"])
    def test_a_non_approved_state_is_refused(self, tmp_path, state):
        assert not _approved(tmp_path, [review(state=state)])

    def test_a_newer_non_approval_is_refused(self, tmp_path):
        assert not _approved(tmp_path, [review(rid=11, at=T2),
                                        review(rid=12, state="COMMENTED", at=T3)])

    def test_a_different_review_id_is_refused(self, tmp_path):
        assert not _approved(tmp_path, [review()], want="99")

    def test_a_trusted_reply_after_the_approval_is_refused(self, tmp_path):
        assert not _approved(tmp_path, [review(at=T2)],
                             comments=MENU + [comment("hold off", at=T3, cid="h")])

    def test_unreadable_reviews_are_refused(self, tmp_path):
        assert not _approved(tmp_path, [review()], REVIEWS_FAIL="1")

    def test_an_unreadable_head_is_refused(self, tmp_path):
        assert not _approved(tmp_path, [review()], HEAD_FAIL="1")

    def test_an_issue_is_refused(self, tmp_path):
        assert not _approved(tmp_path, [review()], kind="issue")

    def test_the_listed_login_is_accepted(self, tmp_path):
        assert _approved(tmp_path, [review(login="owner-second")],
                         LEMD_TRUSTED_ANSWER_LOGINS="owner-second")


@pytest.mark.parametrize("reviews,comments", [
    ([review()], MENU),
    ([review(commit=OLD)], MENU),
    ([review(at="2026-11-20T09:00:00Z")], MENU),
    ([review(login=APP)], MENU),
    ([review(state="COMMENTED")], MENU),
    ([review(at=T2)], MENU + [comment("noted", at=T3, cid="n")]),
    ([review(at=T3)], MENU + [comment("1B", at=T2, cid="n")]),
])
def test_the_daemon_never_routes_an_approval_the_action_refuses(tmp_path, monkeypatch, reviews, comments):
    """Python finding an approval implies the bash accepts the SAME review — and only then."""
    monkeypatch.setattr(github, "_PERMISSION_CACHE", {})
    got = answers.approval(comments, reviews, HEAD, OWNER, trusted=lambda _l: False)
    want = got.comment_id.removeprefix("review:") if got else str(reviews[-1]["id"])
    assert _approved(tmp_path, reviews, comments=comments, want=want) is (got is not None)


def test_the_unpark_action_routes_review_ids_through_the_one_gate():
    """No parallel mechanism: `unpark.sh` still calls `v2_owner_answered` with the routed id."""
    src = (_V2 / "actions" / "unpark.sh").read_text()
    assert "v2_owner_approved" not in src
    body = _fn(COMMON, "v2_owner_answered")
    assert 'review:*) v2_owner_approved "$kind" "$n" "${want#review:}"' in body
