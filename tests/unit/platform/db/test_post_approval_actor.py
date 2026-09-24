"""Issue #2116: every move of a post INTO 'approved' records who made it.

Post 105 was held, then published with a slop HARD finding, and nothing said what approved it. The
writers below are the only ways a post reaches 'approved'; the call-site guard at the bottom is what
keeps a new approval path from shipping without an actor.
"""

import ast
from pathlib import Path
from unittest.mock import patch

import pytest

from cqc_lem.utilities.db import (
    PostApprover,
    PostStatus,
    PostType,
    bulk_update_posts,
    insert_post,
    update_db_post,
    update_db_post_status,
    user_approver,
)

pytestmark = pytest.mark.unit

_GET_CONN = "cqc_lem.platform.db.connection.get_db_connection"
_POSTS = "cqc_lem.platform.db.repositories.posts"
_SRC = Path(__file__).resolve().parents[4] / "src" / "cqc_lem"


def _executed(cursor) -> tuple[str, tuple]:
    sql, params = cursor.execute.call_args[0]
    return " ".join(sql.split()), tuple(params)


def test_user_approver_names_the_account():
    assert user_approver(12) == "user:12"


class TestUpdateDbPostStatus:
    def test_approving_records_the_actor_before_the_status_changes(self, fake_cursor):
        conn, cur = fake_cursor()
        with patch(_GET_CONN, return_value=conn):
            assert update_db_post_status(5, PostStatus.APPROVED, approved_by=PostApprover.RESCORE)
        sql, params = _executed(cur)
        # The actor clause has to read the OLD status, so it must come before `status = %s`.
        assert sql.index("approved_by = IF(status = 'approved'") < sql.index("status = %s WHERE")
        assert params[0] == "system:rescore"
        assert params[1] is not None  # approved_at
        assert params[-2:] == ("approved", 5)

    def test_a_requeue_without_an_actor_leaves_the_approver_alone(self, fake_cursor):
        conn, cur = fake_cursor()
        with patch(_GET_CONN, return_value=conn):
            update_db_post_status(5, PostStatus.APPROVED)
        sql, params = _executed(cur)
        assert "approved_by" not in sql
        assert params == ("approved", 5)

    def test_a_non_approval_never_touches_the_actor(self, fake_cursor):
        conn, cur = fake_cursor()
        with patch(_GET_CONN, return_value=conn):
            update_db_post_status(5, PostStatus.PENDING, approved_by=PostApprover.AUTO_SCHEDULE)
        sql, params = _executed(cur)
        assert "approved_by" not in sql
        assert params == ("pending", 5)


class TestBulkUpdatePosts:
    def test_approving_records_the_author(self, fake_cursor):
        conn, cur = fake_cursor(rowcount=2)
        with patch(_GET_CONN, return_value=conn):
            assert bulk_update_posts([1, 2], status=PostStatus.APPROVED, user_id=7,
                                     approved_by=user_approver(7))
        sql, params = _executed(cur)
        assert sql.index("approved_by = IF(") < sql.index("status = %s")
        assert params[0] == "user:7"
        assert params[2:] == ("approved", 1, 2, 7)

    def test_approving_without_an_actor_warns_and_still_writes(self, fake_cursor):
        conn, cur = fake_cursor()
        with patch(_GET_CONN, return_value=conn), patch(f"{_POSTS}.log_warning") as warn:
            assert bulk_update_posts([1], status=PostStatus.APPROVED)
        sql, _ = _executed(cur)
        assert "approved_by" not in sql
        warn.assert_called_once()

    def test_a_reschedule_only_writes_no_actor(self, fake_cursor):
        from datetime import datetime, timezone
        conn, cur = fake_cursor()
        with patch(_GET_CONN, return_value=conn):
            bulk_update_posts([1], scheduled_time=datetime(2026, 9, 25, tzinfo=timezone.utc),
                              approved_by=user_approver(7))
        sql, _ = _executed(cur)
        assert "approved_by" not in sql


class TestUpdateDbPost:
    def _save(self, fake_cursor, status):
        from datetime import datetime, timezone
        conn, cur = fake_cursor()
        with patch(_GET_CONN, return_value=conn):
            update_db_post("body", None, datetime(2026, 9, 25, tzinfo=timezone.utc), PostType.TEXT, 9,
                           status, user_id=3, approved_by=user_approver(3))
        return _executed(cur)

    def test_saving_as_approved_records_the_author(self, fake_cursor):
        sql, params = self._save(fake_cursor, PostStatus.APPROVED)
        assert sql.index("approved_by = IF(") < sql.index("status = %s")
        assert params[4] == "user:3"
        assert params[-3:] == ("approved", 9, 3)

    def test_saving_as_pending_records_nothing(self, fake_cursor):
        sql, _ = self._save(fake_cursor, PostStatus.PENDING)
        assert "approved_by" not in sql


class TestInsertPost:
    def _insert(self, fake_cursor, status, approved_by):
        from datetime import datetime, timezone
        conn, cur = fake_cursor()
        with patch(_GET_CONN, return_value=conn), patch(f"{_POSTS}.get_user_id", return_value=3):
            assert insert_post("a@example.test", "body", datetime(2026, 9, 25, tzinfo=timezone.utc),
                               PostType.TEXT, status=status, approved_by=approved_by)
        return _executed(cur)

    def test_inserting_approved_records_the_author(self, fake_cursor):
        sql, params = self._insert(fake_cursor, PostStatus.APPROVED, user_approver(3))
        assert "approved_by, approved_at" in sql
        assert params[-2] == "user:3"
        assert params[-1] is not None

    def test_inserting_a_draft_records_no_actor(self, fake_cursor):
        _, params = self._insert(fake_cursor, PostStatus.PENDING, user_approver(3))
        assert params[-2:] == (None, None)

    def test_inserting_approved_without_an_actor_warns(self, fake_cursor):
        with patch(f"{_POSTS}.log_warning") as warn:
            _, params = self._insert(fake_cursor, PostStatus.APPROVED, None)
        assert params[-2:] == (None, None)
        warn.assert_called_once()


# ---- call-site guard -------------------------------------------------------------------------

# The ONE place an APPROVED write may omit its actor: the native occasion publish handing a claimed
# post back to the queue. That is a re-queue, not an approval, so the original approver stays.
_REQUEUE_SITES = {("app/engagement/posting.py", "auto_publish_occasion_post")}

# Writers that may set any status; only a literal non-APPROVED status is exempt from naming an actor.
_WRITERS = {"update_db_post_status": 1, "bulk_update_posts": None, "update_db_post": 5,
            "insert_post": None}


def _status_arg(call: ast.Call, name: str):
    for kw in call.keywords:
        if kw.arg in ("status", "post_status"):
            return kw.value
    position = _WRITERS[name]
    if position is not None and len(call.args) > position:
        return call.args[position]
    return None


def _is_literal_non_approval(node) -> bool:
    return (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
            and node.value.id == "PostStatus" and node.attr != "APPROVED")


def _unattributed_approvals() -> list[str]:
    offenders = []
    for path in _SRC.rglob("*.py"):
        rel = path.relative_to(_SRC).as_posix()
        if rel.startswith("platform/db/") or rel == "utilities/db.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for func in ast.walk(tree):
            if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for call in ast.walk(func):
                if not isinstance(call, ast.Call):
                    continue
                name = getattr(call.func, "id", None) or getattr(call.func, "attr", None)
                if name not in _WRITERS:
                    continue
                status = _status_arg(call, name)
                if status is None or _is_literal_non_approval(status):
                    continue
                if any(kw.arg == "approved_by" for kw in call.keywords):
                    continue
                if (rel, func.name) in _REQUEUE_SITES:
                    continue
                offenders.append(f"{rel}:{call.lineno} {func.name} -> {name}")
    return sorted(set(offenders))


def test_every_approval_names_its_actor():
    assert _unattributed_approvals() == [], (
        "A post can reach 'approved' here without recording who approved it (#2116). Pass "
        "approved_by=PostApprover.<source> or user_approver(user_id).")


def test_the_requeue_exemption_is_still_real():
    """The exemption must name a function that exists, or it silently exempts nothing."""
    source = (_SRC / "app/engagement/posting.py").read_text(encoding="utf-8")
    for _rel, func in _REQUEUE_SITES:
        assert f"def {func}(" in source
