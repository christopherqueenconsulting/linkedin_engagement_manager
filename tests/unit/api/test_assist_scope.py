"""LEM Assist (#2647 P4): the `assist` scope, the two routes, and the no-Post static guarantee."""

import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import mysql.connector
import pytest
from fastapi import HTTPException

pytestmark = pytest.mark.unit

_A = "cqc_lem.api.routers.assist"
_CUR = "cqc_lem.platform.db.repositories.comment_candidates.db_cursor"
_EXT = Path(__file__).resolve().parents[3] / "src" / "cqc_lem" / "assist_extension"


@pytest.fixture(scope="module")
def main_mod():
    from cqc_lem.api import main
    return main


@pytest.fixture(scope="module")
def assist():
    from cqc_lem.api.routers import assist as mod
    return mod


class TestScopeTable:
    def test_scope_is_registered_and_restricted(self, main_mod):
        from cqc_lem.platform.db.repositories.auth import FIXED_TTL_SCOPES
        from cqc_lem.utilities import db
        scope = main_mod.SESSION_SCOPE_ASSIST
        assert scope == "assist" == db.SESSION_SCOPE_ASSIST
        assert scope in main_mod._SCOPE_SURFACES
        assert scope in main_mod._SCOPE_REFUSAL_CODE
        assert scope not in main_mod._UNRESTRICTED_SCOPES
        assert scope in FIXED_TTL_SCOPES

    @pytest.mark.parametrize("path,method", [
        ("/api/assist/queue", "GET"), ("/api/assist/queue/", "GET"),
        ("/api/review/comment-posted", "POST")])
    def test_the_two_routes_take_their_one_verb(self, main_mod, path, method):
        assert main_mod._scope_allows("assist", path, method)

    @pytest.mark.parametrize("path,method", [
        ("/api/assist/queue", "POST"), ("/api/assist/queue", "DELETE"),
        ("/api/review/comment-posted", "GET"), ("/api/review/comment-posted", "PUT"),
        ("/api/dms", "GET"), ("/api/schedule_dm", "POST"), ("/api/user/linkedin-cookie", "POST"),
        ("/api/user/assist-token", "POST"), ("/api/assist/queue-x", "GET"), (None, "GET")])
    def test_everything_else_is_refused(self, main_mod, path, method):
        assert not main_mod._scope_allows("assist", path, method)

    def test_an_unreadable_method_is_refused(self, main_mod):
        assert not main_mod._scope_allows("assist", "/api/assist/queue", None)


class TestRoutes:
    def test_queue_is_limited_to_the_remaining_budget(self, assist):
        row = {"id": 3, "post_url": "u", "author": "a", "draft_text": "hi"}
        with patch(f"{_A}._main.get_session_user_id", return_value=7), \
                patch(f"{_A}._main.get_engagement_preferences", return_value={"max_comments_per_day": 500}), \
                patch(f"{_A}.count_comments_today", return_value=40), \
                patch(f"{_A}.get_approved_comment_candidates", return_value=[row]) as get:
            out = assist.assist_queue("tok")
        assert get.call_args.kwargs["limit"] == 60  # clamped to 100, minus 40 spent
        assert out.detail["items"] == [
            {"candidate_id": 3, "post_url": "u", "author": "a", "text": "hi"}]

    def test_queue_is_empty_once_the_cap_is_spent(self, assist):
        with patch(f"{_A}._main.get_session_user_id", return_value=7), \
                patch(f"{_A}._main.get_engagement_preferences", return_value={"max_comments_per_day": 20}), \
                patch(f"{_A}.count_comments_today", return_value=20), \
                patch(f"{_A}.get_approved_comment_candidates") as get:
            out = assist.assist_queue("tok")
        get.assert_not_called()
        assert out.detail["items"] == []

    def test_a_failed_read_is_503_not_empty(self, assist):
        with patch(f"{_A}._main.get_session_user_id", return_value=7), \
                patch(f"{_A}._main.get_engagement_preferences", return_value={"max_comments_per_day": 20}), \
                patch(f"{_A}.count_comments_today", return_value=0), \
                patch(f"{_A}.get_approved_comment_candidates", return_value=None):
            with pytest.raises(HTTPException) as e:
                assist.assist_queue("tok")
        assert e.value.status_code == 503

    def test_no_session_is_401(self, assist):
        with patch(f"{_A}._main.get_session_user_id", return_value=None):
            with pytest.raises(HTTPException) as e:
                assist.assist_queue("tok")
        assert e.value.status_code == 401

    def test_done_rejects_anything_not_approved(self, assist):
        with patch(f"{_A}._main.get_session_user_id", return_value=7), \
                patch(f"{_A}.claim_approved_candidate_posted", return_value=None), \
                patch(f"{_A}.insert_new_log") as log, patch(f"{_A}.record_action") as act:
            with pytest.raises(HTTPException) as e:
                assist.comment_posted(assist.CommentPostedRequest(candidate_id=9))
        assert e.value.status_code == 409
        log.assert_not_called()
        act.assert_not_called()

    def test_done_counts_the_comment_everywhere_a_comment_counts(self, assist):
        row = {"post_key": "feedurn://x", "author": "https://www.linkedin.com/in/a",
               "draft_text": "hello"}
        with patch(f"{_A}._main.get_session_user_id", return_value=7), \
                patch(f"{_A}.claim_approved_candidate_posted", return_value=row), \
                patch(f"{_A}.claim_post_for_comment") as claim, \
                patch(f"{_A}.mark_post_commented") as mark, \
                patch(f"{_A}.insert_new_log") as log, \
                patch(f"{_A}.record_action") as act, \
                patch(f"{_A}.record_target_engagement") as target:
            out = assist.comment_posted(assist.CommentPostedRequest(candidate_id=9))
        assert out.detail == {"recorded": True}
        claim.assert_called_once_with(7, "feedurn://x")
        mark.assert_called_once_with(7, "feedurn://x")
        assert log.call_args.kwargs["message"] == "hello"
        assert str(log.call_args.kwargs["action_type"]) == "comment"
        act.assert_called_once_with(7, "comment")
        target.assert_called_once_with(7, "https://www.linkedin.com/in/a")

    def test_done_skips_the_author_cap_for_a_non_url_author(self, assist):
        row = {"post_key": "k", "author": "Some Name", "draft_text": "t"}
        with patch(f"{_A}._main.get_session_user_id", return_value=7), \
                patch(f"{_A}.claim_approved_candidate_posted", return_value=row), \
                patch(f"{_A}.claim_post_for_comment"), patch(f"{_A}.mark_post_commented"), \
                patch(f"{_A}.insert_new_log"), patch(f"{_A}.record_action"), \
                patch(f"{_A}.record_target_engagement") as target:
            assist.comment_posted(assist.CommentPostedRequest(candidate_id=1))
        target.assert_not_called()

    @pytest.mark.parametrize("prefs,expected", [
        ({"max_comments_per_day": 30}, 30), ({"max_comments_per_day": 900}, 100),
        ({"max_comments_per_day": "x"}, 0), (None, 0)])
    def test_daily_cap_is_clamped_server_side(self, assist, prefs, expected):
        with patch(f"{_A}._main.get_engagement_preferences", return_value=prefs):
            assert assist._daily_cap(1) == expected


class TestMint:
    def test_mints_an_assist_session_behind_a_step_up(self):
        from cqc_lem.api.routers import user
        req = user.AssistTokenRequest(session_token="human", ttl_days=7)
        with patch("cqc_lem.api.routers.user._main.get_session_user_id", return_value=5), \
                patch("cqc_lem.api.routers.user._require_step_up") as step, \
                patch("cqc_lem.api.routers.user._main._user_agent", return_value="ua"), \
                patch("cqc_lem.api.routers.user._main._client_ip", return_value="ip"), \
                patch("cqc_lem.api.routers.user.create_session", return_value="tok") as create, \
                patch("cqc_lem.api.routers.user.record_auth_event") as audit:
            out = user.mint_assist_token(req, None)
        step.assert_called_once()
        assert "extension_scope_ok" not in step.call_args.kwargs
        assert create.call_args.kwargs["scope"] == "assist"
        assert create.call_args.kwargs["ttl_hours"] == 168
        assert str(audit.call_args.args[0]) == "assist_token_minted"
        assert out.detail["session_token"] == "tok"

    def test_refuses_without_a_session_or_a_token(self):
        from cqc_lem.api.routers import user
        req = user.AssistTokenRequest(session_token="x")
        with patch("cqc_lem.api.routers.user._main.get_session_user_id", return_value=None):
            with pytest.raises(HTTPException) as e:
                user.mint_assist_token(req, None)
        assert e.value.status_code == 401
        with patch("cqc_lem.api.routers.user._main.get_session_user_id", return_value=5), \
                patch("cqc_lem.api.routers.user._require_step_up"), \
                patch("cqc_lem.api.routers.user._main._user_agent", return_value=None), \
                patch("cqc_lem.api.routers.user._main._client_ip", return_value=None), \
                patch("cqc_lem.api.routers.user.create_session", return_value=None):
            with pytest.raises(HTTPException) as e:
                user.mint_assist_token(req, None)
        assert e.value.status_code == 500


def _cursor(rowcount=1, rows=None, one=None):
    cur = MagicMock()
    cur.rowcount = rowcount
    cur.fetchall.return_value = rows
    cur.fetchone.return_value = one
    ctx = MagicMock()
    ctx.__enter__.return_value = cur
    return cur, ctx


class TestRepository:
    def test_approved_read_filters_on_status_text_and_the_ledger(self):
        from cqc_lem.platform.db.repositories import comment_candidates as repo
        cur, ctx = _cursor(rows=[{"id": 1}])
        with patch(_CUR, return_value=ctx):
            assert repo.get_approved_comment_candidates(1, 5) == [{"id": 1}]
        sql = cur.execute.call_args[0][0]
        assert "status = 'approved'" in sql and "commented_posts" in sql and "draft_text" in sql

    def test_approved_read_is_none_on_a_fault(self):
        from cqc_lem.platform.db.repositories import comment_candidates as repo
        with patch(_CUR, side_effect=mysql.connector.Error("boom")):
            assert repo.get_approved_comment_candidates(1) is None

    def test_claim_moves_only_an_approved_row(self):
        from cqc_lem.platform.db.repositories import comment_candidates as repo
        cur, ctx = _cursor(rowcount=1, one={"id": 4, "post_key": "k"})
        with patch(_CUR, return_value=ctx):
            assert repo.claim_approved_candidate_posted(1, 4) == {"id": 4, "post_key": "k"}
        sqls = [c[0][0] for c in cur.execute.call_args_list]
        assert "status = 'approved'" in sqls[0] and "FOR UPDATE" in sqls[0]
        assert "status = 'approved'" in sqls[1] and "SET status = 'posted'" in sqls[1]

    def test_claim_is_none_when_not_approved_lost_a_race_or_faulted(self):
        from cqc_lem.platform.db.repositories import comment_candidates as repo
        _, ctx = _cursor(one=None)
        with patch(_CUR, return_value=ctx):
            assert repo.claim_approved_candidate_posted(1, 4) is None
        _, ctx = _cursor(rowcount=0, one={"id": 4})
        with patch(_CUR, return_value=ctx):
            assert repo.claim_approved_candidate_posted(1, 4) is None
        with patch(_CUR, side_effect=mysql.connector.Error("boom")):
            assert repo.claim_approved_candidate_posted(1, 4) is None

    def test_facade_exports(self):
        from cqc_lem.platform.db.repositories import comment_candidates as repo
        from cqc_lem.utilities import db
        for name in ("get_approved_comment_candidates", "claim_approved_candidate_posted"):
            assert getattr(db, name) is getattr(repo, name) and name in db.__all__


# Anything that could press Post or submit a form. The extension may type text and read the page;
# it may not activate a control. Listening for a click on its OWN popup buttons is not activating one.
_FORBIDDEN = [
    (r"\.click\s*\(", "calls .click()"),
    (r"\.submit\s*\(", "calls .submit()"),
    (r"requestSubmit", "requestSubmit"),
    (r"dispatchEvent", "dispatchEvent (a synthetic event can press a button)"),
    (r"new\s+(Mouse|Pointer|Keyboard|Input)Event", "builds a synthetic input event"),
    (r"\.form\b", "reaches a form"),
    (r"<form\b", "contains a form"),
    (r"type\s*=\s*[\"']submit", "has a submit control"),
    (r"submit|Enter\b", "mentions submit or Enter"),
    (r"\bXMLHttpRequest\b|sendBeacon", "makes a request outside fetch()"),
]


def _sources():
    files = [p for p in _EXT.iterdir() if p.suffix in {".js", ".html"}]
    assert {p.name for p in files} >= {"background.js", "content.js", "popup.js", "popup.html"}
    return files


@pytest.mark.parametrize("path", sorted(p.name for p in _EXT.iterdir() if p.suffix in {".js", ".html"}))
def test_no_code_path_posts_or_submits(path):
    text = (_EXT / path).read_text()
    # Comments are prose about the rule; the rule is about code.
    code = re.sub(r"//[^\n]*|/\*.*?\*/|<!--.*?-->", "", text, flags=re.S)
    hits = [why for pattern, why in _FORBIDDEN if re.search(pattern, code)]
    assert not hits, f"{path}: {hits}"


def test_the_extension_calls_only_the_two_assist_paths():
    calls = set()
    for p in _sources():
        calls |= set(re.findall(r"/api/[a-z/-]+", p.read_text()))
    assert calls == {"/api/assist/queue", "/api/review/comment-posted"}


def test_manifest_is_mv3_with_no_cookie_or_scripting_permission():
    import json
    manifest = json.loads((_EXT / "manifest.json").read_text())
    assert manifest["manifest_version"] == 3
    assert set(manifest["permissions"]) == {"storage", "tabs"}
