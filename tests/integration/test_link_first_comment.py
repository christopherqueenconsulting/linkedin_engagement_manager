"""Integration test for the link-in-first-comment mechanic (issue #392 — C3).

Acceptance: a post carrying an external link publishes a LINK-FREE body and the author's seeded
first comment carries the link. Exercises the real chain — post_to_linkedin → db.py (posts, logs,
engagement_preferences) → auto_seed_comment_on_post — over a stateful fake cursor, so the handoff
between publish and seed goes through the actual persisted columns without needing a live MySQL.
"""
import pytest
from contextlib import ExitStack
from unittest.mock import MagicMock, patch

pytestmark = pytest.mark.integration

_DB = "cqc_lem.utilities.db"
_RA = "cqc_lem.app.run_automation"

_BODY = ("Three lessons from the rebuild.\n\n"
         "We cut deploy time by 40%.\n\n"
         "Full breakdown: https://example.com/rebuild")


class _FakeCursor:
    """Minimal MySQL-ish cursor over in-memory posts / users / logs / prefs stores."""

    def __init__(self, store, dictionary=False):
        self.store = store
        self.dictionary = dictionary
        self._result = []
        self.rowcount = 0

    def _rows(self, dict_rows, order):
        self._result = [r if self.dictionary else tuple(r[c] for c in order) for r in dict_rows]

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        posts, logs = self.store["posts"], self.store["logs"]
        if s.startswith("SELECT status FROM posts"):
            self._rows([posts[params[0]]], ["status"])
        elif s.startswith("SELECT content FROM posts"):
            self._rows([posts[params[0]]], ["content"])
        elif s.startswith("SELECT post_type FROM posts"):
            self._rows([posts[params[0]]], ["post_type"])
        elif s.startswith("SELECT first_comment_link FROM posts"):
            self._rows([posts[params[0]]], ["first_comment_link"])
        elif s.startswith("UPDATE posts SET content"):
            posts[params[1]]["content"] = params[0]
            self.rowcount = 1
        elif s.startswith("UPDATE posts SET first_comment_link"):
            posts[params[1]]["first_comment_link"] = params[0]
            self.rowcount = 1
        elif s.startswith("UPDATE posts SET status"):
            posts[params[1]]["status"] = params[0]
            self.rowcount = 1
        elif s.startswith("SELECT email, password FROM users"):
            self._rows([self.store["users"][params[0]]], ["email", "password"])
        elif "FROM engagement_preferences" in s:
            row = self.store["prefs"].get(params[0])
            self._result = [row] if row else []
        elif s.startswith("INSERT INTO logs"):
            user_id, action_type, post_id, post_url, message, result = params
            logs.append({"user_id": user_id, "action_type": action_type, "post_id": post_id,
                         "post_url": post_url, "message": message, "result": result})
            self.rowcount = 1
        elif s.startswith("SELECT post_url FROM logs"):
            user_id, post_id, action_type, result = params
            match = [r for r in logs if (r["user_id"], r["post_id"], r["action_type"], r["result"])
                     == (user_id, post_id, action_type, result)]
            self._result = [(match[-1]["post_url"],)] if match else []
        elif s.startswith("SELECT COUNT(*) FROM logs"):
            user_id, post_url, action_type, result = params
            self._result = [(len([r for r in logs if (r["user_id"], r["post_url"], r["action_type"],
                                                      r["result"]) == (user_id, post_url, action_type,
                                                                       result)]),)]
        else:  # pragma: no cover - defensive
            raise AssertionError(f"unexpected SQL: {s}")

    def fetchone(self):
        return self._result[0] if self._result else None

    def fetchall(self):
        return list(self._result)

    def close(self):
        pass


class _FakeConn:
    def __init__(self, store):
        self.store = store

    def cursor(self, *a, **k):
        return _FakeCursor(self.store, dictionary=bool(k.get("dictionary")))

    def commit(self):
        pass

    def close(self):
        pass


def _store(link_pref_row=None):
    return {
        "posts": {10: {"id": 10, "status": "approved", "content": _BODY, "post_type": "text",
                       "first_comment_link": None}},
        "users": {1: {"email": "u@example.com", "password": "pw"}},
        "prefs": ({1: link_pref_row} if link_pref_row else {}),
        "logs": [],
    }


def _run_publish_and_seed(store):
    """Publish the post, then run the seed-comment task the publish dispatched — inline, so the
    handoff happens through the persisted columns exactly as it does in the worker."""
    from cqc_lem.app.run_automation import post_to_linkedin, auto_seed_comment_on_post

    seed_task = MagicMock()
    seed_task.apply_async.side_effect = lambda kwargs=None, **kw: auto_seed_comment_on_post.run(**kwargs)
    published, commented = {}, {}

    def _share(user_id, content, *a, **k):
        published["body"] = content
        return "urn:li:ugcPost:7479519458164695040"

    def _comment(user_id, object_urn, text, *a, **k):
        commented["text"] = text
        return "urn:li:comment:(x,1)"

    with ExitStack() as stack:
        stack.enter_context(patch(f"{_DB}.get_db_connection", side_effect=lambda *a, **k: _FakeConn(store)))
        stack.enter_context(patch(f"{_RA}.share_on_linkedin", side_effect=_share))
        stack.enter_context(patch(f"{_RA}.comment_on_linkedin_post", side_effect=_comment))
        stack.enter_context(patch(f"{_RA}.auto_seed_comment_on_post", seed_task))
        stack.enter_context(patch(f"{_RA}.sweep_reply_comments"))
        stack.enter_context(patch("cqc_lem.utilities.utils.purge_post_assets"))
        stack.enter_context(patch(f"{_RA}.load_profile_for_user", return_value=MagicMock()))
        stack.enter_context(patch(f"{_RA}.get_or_create_profile_synthesis", return_value="synth"))
        stack.enter_context(patch(f"{_RA}.generate_seed_comment",
                                  return_value="What would you have cut first?"))
        post_to_linkedin.run(1, 10)

    return published, commented


class TestLinkInFirstComment:
    def test_link_free_body_publishes_and_first_comment_carries_the_link(self):
        store = _store()
        published, commented = _run_publish_and_seed(store)

        # 1) What LinkedIn received as the POST has no external link left in it.
        assert "https://example.com/rebuild" not in published["body"]
        assert "Three lessons from the rebuild." in published["body"]
        assert "We cut deploy time by 40%." in published["body"]

        # 2) The link round-tripped through the posts row to the seeded FIRST comment.
        assert store["posts"][10]["first_comment_link"] == "https://example.com/rebuild"
        assert "https://example.com/rebuild" in commented["text"]
        assert "What would you have cut first?" in commented["text"]

        # 3) The stored post + the POST log match what actually published (no phantom link).
        assert store["posts"][10]["content"] == published["body"]
        post_log = [r for r in store["logs"] if r["action_type"] == "post"][0]
        assert post_log["message"] == published["body"]

        # 4) The seeded comment is logged against the live post URL.
        comment_log = [r for r in store["logs"] if r["action_type"] == "comment"][0]
        assert comment_log["post_url"].endswith("urn:li:ugcPost:7479519458164695040/")
        assert "https://example.com/rebuild" in comment_log["message"]

    def test_opted_out_user_keeps_the_link_in_the_body(self):
        from cqc_lem.utilities.db import _ENGAGEMENT_DEFAULTS
        row = dict(_ENGAGEMENT_DEFAULTS, link_in_first_comment=0, reply_check_mode="off")
        store = _store(link_pref_row=row)
        published, commented = _run_publish_and_seed(store)

        assert "https://example.com/rebuild" in published["body"]
        assert store["posts"][10]["first_comment_link"] is None
        # The seed comment still runs — it just has no link to deliver.
        assert commented["text"] == "What would you have cut first?"
