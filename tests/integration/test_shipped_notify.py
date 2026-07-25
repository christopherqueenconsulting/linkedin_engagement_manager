"""Integration test for the "you asked, we shipped" loop (issue #502).

Drives the REAL `utilities/feedback/shipped.py` pass (a merged PR that says `Closes #498`) and the
REAL `GET /api/user/shipped` + `POST /api/shipped/ack` handlers through the REAL db.py SQL into a
stateful fake cursor that models `feedback`, `shipped_notices`, `shipped_notice_recipients` and
`survey_prompts` — so it is provable, without a MySQL container, that:

  * only the reporters behind the cluster are notified, and only ONCE across repeated passes,
  * the changelog line is generated and stored,
  * the cluster's feedback rows end up `resolved`,
  * the micro-CSAT is withheld until the reporter has had the fix (the delay), and
  * a "not fixed" answer lands as a NEW feedback row — back at the top of the auto-work loop.
"""
import json
from datetime import datetime, timedelta

import pytest
from unittest.mock import patch

pytestmark = pytest.mark.integration

_DB = "cqc_lem.utilities.db"
_SHIPPED = "cqc_lem.utilities.feedback.shipped"

_PR = {"number": 539, "title": "fix(feed): comments now post reliably (closes #498)",
       "body": "Closes #498", "merged_at": "2026-07-25T09:00:00Z"}


class _FakeCursor:
    """Minimal MySQL-ish cursor over the in-memory feedback / shipped / survey_prompts store."""

    def __init__(self, store, dictionary=False):
        self.store = store
        self.dictionary = dictionary
        self._result = []
        self.rowcount = 0
        self.lastrowid = None

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        params = params or ()
        self._result = []
        self.rowcount = 0
        self.lastrowid = None

        if s.startswith("SELECT DISTINCT f.user_id FROM feedback"):
            issue = params[0]
            seeds = {r["id"] for r in self.store["feedback"]
                     if r.get("github_issue_number") == issue}
            users = []
            for row in self.store["feedback"]:
                if row.get("user_id") is None:
                    continue
                if row.get("github_issue_number") == issue or row.get("cluster_id") in seeds:
                    if row["user_id"] not in users:
                        users.append(row["user_id"])
            self._result = [(u,) for u in users]
        elif s.startswith("UPDATE feedback f LEFT JOIN feedback s ON s.id = f.cluster_id"):
            status, issue = params[0], params[1]
            seeds = {r["id"] for r in self.store["feedback"]
                     if r.get("github_issue_number") == issue}
            for row in self.store["feedback"]:
                if row["status"] in ("resolved", "dismissed"):
                    continue
                if row.get("github_issue_number") == issue or row.get("cluster_id") in seeds:
                    row["status"] = status
                    self.rowcount += 1
        elif s.startswith("UPDATE feedback SET status=%s WHERE id=%s"):
            for row in self.store["feedback"]:
                if row["id"] == params[1]:
                    row["status"] = params[0]
                    self.rowcount = 1
        elif s.startswith("INSERT INTO feedback"):
            row = {"id": len(self.store["feedback"]) + 1, "user_id": params[0],
                   "source": params[1], "type_hint": params[2], "body": params[3],
                   "context_json": json.loads(params[4]) if params[4] else None,
                   "sentiment": params[5], "status": "new", "cluster_id": None,
                   "github_issue_number": None, "created_at": datetime.now()}
            self.store["feedback"].append(row)
            self.lastrowid = row["id"]
            self.rowcount = 1
        elif s.startswith("SELECT id, github_issue_number, pr_number, title, changelog_line, "
                          "shipped_at FROM shipped_notices WHERE github_issue_number"):
            self._result = [dict(n) for n in self.store["notices"]
                            if n["github_issue_number"] == params[0]]
        elif s.startswith("INSERT IGNORE INTO shipped_notices"):
            if any(n["github_issue_number"] == params[0] for n in self.store["notices"]):
                return
            notice = {"id": len(self.store["notices"]) + 1, "github_issue_number": params[0],
                      "pr_number": params[1], "title": params[2], "changelog_line": params[3],
                      "shipped_at": datetime.now()}
            self.store["notices"].append(notice)
            self.lastrowid = notice["id"]
            self.rowcount = 1
        elif s.startswith("SELECT id FROM shipped_notices WHERE github_issue_number"):
            self._result = [(n["id"],) for n in self.store["notices"]
                            if n["github_issue_number"] == params[0]]
        elif s.startswith("SELECT id, github_issue_number, pr_number, title, changelog_line, "
                          "shipped_at FROM shipped_notices ORDER BY"):
            self._result = [dict(n) for n in reversed(self.store["notices"])][:params[0]]
        elif s.startswith("SELECT user_id FROM shipped_notice_recipients"):
            self._result = [(uid,) for (nid, uid) in self.store["recipients"]
                            if nid == params[0]]
        elif s.startswith("INSERT IGNORE INTO shipped_notice_recipients"):
            key = (params[0], params[1])
            if key in self.store["recipients"]:
                return
            self.store["recipients"][key] = {"notified_at": self.store["notified_at"],
                                             "seen_at": None}
            self.rowcount = 1
        elif s.startswith("SELECT n.id, n.github_issue_number"):
            user_id, delay_hours, limit = params
            cutoff = datetime.now() - timedelta(hours=delay_hours)
            rows = []
            for (notice_id, uid), rec in self.store["recipients"].items():
                if uid != user_id or rec["seen_at"] or rec["notified_at"] > cutoff:
                    continue
                notice = next(n for n in self.store["notices"] if n["id"] == notice_id)
                rows.append({**notice, "notified_at": rec["notified_at"]})
            self._result = rows[:limit]
        elif s.startswith("UPDATE shipped_notice_recipients SET seen_at"):
            rec = self.store["recipients"].get((params[0], params[1]))
            if rec and not rec["seen_at"]:
                rec["seen_at"] = datetime.now()
                self.rowcount = 1
        elif s.startswith("INSERT IGNORE INTO survey_prompts"):
            if params[1] not in self.store["prompts"]:
                self.store["prompts"][params[1]] = datetime.now()
                self.rowcount = 1
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
        return _FakeCursor(self.store, dictionary=k.get("dictionary", False))

    def commit(self):
        pass

    def close(self):
        pass


@pytest.fixture
def client():
    from fastapi.testclient import TestClient
    from cqc_lem.api.main import app
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def store():
    """Two reporters (4 and 7) behind cluster/issue #498, plus one anonymous report, one report
    attached to the seed by `cluster_id` only (the issue number never propagated to it), and one
    unrelated report that must never be notified."""
    return {
        "feedback": [
            {"id": 1, "user_id": 4, "body": "comments never post", "status": "issue_created",
             "cluster_id": 1, "github_issue_number": 498, "source": "widget"},
            {"id": 2, "user_id": 7, "body": "my comments go nowhere", "status": "clustered",
             "cluster_id": 1, "github_issue_number": 498, "source": "widget"},
            {"id": 3, "user_id": None, "body": "same here", "status": "clustered",
             "cluster_id": 1, "github_issue_number": 498, "source": "widget"},
            {"id": 4, "user_id": 9, "body": "unrelated carousel bug", "status": "issue_created",
             "cluster_id": 4, "github_issue_number": 601, "source": "widget"},
            {"id": 5, "user_id": 4, "body": "comment button does nothing", "status": "clustered",
             "cluster_id": 1, "github_issue_number": None, "source": "widget"},
        ],
        "notices": [],
        "recipients": {},
        "prompts": {},
        "notified_at": datetime.now() - timedelta(hours=30),  # notified yesterday
    }


def _emails(store):
    return store.setdefault("_emails", [])


def _run_pass(store):
    """One real `process_shipped_fixes` pass over one merged PR, with GitHub + SMTP faked."""
    with patch(f"{_DB}.get_db_connection", side_effect=lambda *a, **k: _FakeConn(store)), \
         patch(f"{_SHIPPED}.github_token", return_value="tok"), \
         patch(f"{_SHIPPED}.github_request", return_value=[_PR]), \
         patch(f"{_SHIPPED}._parse_github_time", return_value=datetime.now().astimezone()), \
         patch("cqc_lem.utilities.notifications.get_user_email",
               side_effect=lambda uid: f"user{uid}@example.com"), \
         patch("cqc_lem.utilities.email._dispatch_email",
               side_effect=lambda to, *a, **k: _emails(store).append(to) or True), \
         patch("cqc_lem.utilities.observability.posthog"):
        from cqc_lem.utilities.feedback.shipped import process_shipped_fixes
        return process_shipped_fixes()


def _call(client, store, method, path, **kwargs):
    with patch(f"{_DB}.get_db_connection", side_effect=lambda *a, **k: _FakeConn(store)), \
         patch("cqc_lem.api.main.get_session_user_id", return_value=4), \
         patch("cqc_lem.utilities.observability.posthog"):
        return getattr(client, method)(path, **kwargs)


class TestShippedNotifyLoop:
    def test_reporters_are_mapped_from_the_cluster_and_notified_exactly_once(self, store):
        result = _run_pass(store)

        assert result["counts"] == {"notified": 1}
        assert result["changelog"] == [
            "**Fixed:** Comments now post reliably (#498, PR #539)"]
        # Only the two IDENTIFIED reporters behind #498 — never the anonymous row, never user 9.
        assert sorted(_emails(store)) == ["user4@example.com", "user7@example.com"]
        assert sorted(uid for (_n, uid) in store["recipients"]) == [4, 7]
        # The cluster is closed out — including the row attached by cluster_id only, which is the
        # same row the reporter mapping counted; the unrelated report is untouched.
        assert [r["status"] for r in store["feedback"][:3]] == ["resolved"] * 3
        assert store["feedback"][4]["status"] == "resolved"
        assert store["feedback"][3]["status"] == "issue_created"

        # A second pass over the same (overlapping) window must not re-email anyone.
        _emails(store).clear()
        again = _run_pass(store)
        assert again["counts"] == {"already_sent": 1}
        assert _emails(store) == []
        assert len(store["notices"]) == 1  # one changelog line per shipped issue

    def test_the_micro_csat_waits_until_the_reporter_has_had_the_fix(self, client, store):
        _run_pass(store)
        store["recipients"][(1, 4)]["notified_at"] = datetime.now()  # notified just now

        with patch.dict("os.environ", {"FEEDBACK_FIX_CSAT_DELAY_HOURS": "24"}):
            detail = _call(client, store, "get",
                           "/api/user/shipped?session_token=sess").json()["detail"]
        assert detail["notices"] == []
        # The changelog itself is public — only the ASK is scheduled.
        assert detail["changelog"][0]["issue_number"] == 498

    def test_an_ack_inside_the_delay_cannot_burn_the_notice(self, client, store):
        """The ack endpoint applies the SAME delay gate as the GET — otherwise a client could ack
        (and CSAT) a notice it was never shown, and the notice would never surface afterwards."""
        _run_pass(store)
        store["recipients"][(1, 4)]["notified_at"] = datetime.now()  # notified just now

        with patch.dict("os.environ", {"FEEDBACK_FIX_CSAT_DELAY_HOURS": "24"}):
            ack = _call(client, store, "post", "/api/shipped/ack", json={
                "session_token": "sess", "notice_id": 1, "resolved": True})
        assert ack.status_code == 404
        assert store["recipients"][(1, 4)]["seen_at"] is None

        # Once the delay has elapsed the very same notice is surfaced and ackable.
        store["recipients"][(1, 4)]["notified_at"] = datetime.now() - timedelta(hours=30)
        with patch.dict("os.environ", {"FEEDBACK_FIX_CSAT_DELAY_HOURS": "24"}):
            detail = _call(client, store, "get",
                           "/api/user/shipped?session_token=sess").json()["detail"]
            assert detail["notices"][0]["id"] == 1
            assert _call(client, store, "post", "/api/shipped/ack", json={
                "session_token": "sess", "notice_id": 1, "resolved": True}).status_code == 200

    def test_a_not_fixed_answer_re_enters_the_auto_work_loop(self, client, store):
        _run_pass(store)

        with patch.dict("os.environ", {"FEEDBACK_FIX_CSAT_DELAY_HOURS": "24"}):
            detail = _call(client, store, "get",
                           "/api/user/shipped?session_token=sess").json()["detail"]
        notice = detail["notices"][0]
        assert notice["changelog_line"] == "**Fixed:** Comments now post reliably (#498, PR #539)"

        ack = _call(client, store, "post", "/api/shipped/ack", json={
            "session_token": "sess", "notice_id": notice["id"], "resolved": False,
            "comment": "still nothing on mobile"})
        assert ack.status_code == 200

        row = store["feedback"][-1]
        assert row["source"] == "csat"
        assert row["type_hint"] == "fix_csat"
        assert row["body"] == "still nothing on mobile"
        assert row["sentiment"] == "negative"
        assert row["context_json"] == {"issue_number": 498, "resolved": False,
                                       "survey_key": "fix_csat_498"}
        # Left at `new` on purpose: the auto-filer picks it up and re-opens the problem.
        assert row["status"] == "new"
        assert "fix_csat_498" in store["prompts"]

        # The notice is spent — the same user is not asked twice.
        detail = _call(client, store, "get",
                       "/api/user/shipped?session_token=sess").json()["detail"]
        assert detail["notices"] == []

    def test_a_fixed_answer_is_stored_resolved_and_never_re_classified(self, client, store):
        _run_pass(store)
        detail = _call(client, store, "get",
                       "/api/user/shipped?session_token=sess").json()["detail"]

        _call(client, store, "post", "/api/shipped/ack", json={
            "session_token": "sess", "notice_id": detail["notices"][0]["id"], "resolved": True})

        row = store["feedback"][-1]
        assert row["source"] == "csat"
        assert row["sentiment"] == "positive"
        assert row["status"] == "resolved"
        assert row["body"] == "Confirmed fixed (issue #498)"
