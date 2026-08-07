"""Issue #628: the comment_outcomes table and its queries against a live MySQL server.

Unit tests mock the cursor, so they prove the Python but not the SQL. These prove the parts only a
real server can answer: that the migration's UNIQUE (user_id, log_id) really makes the outcome
check at-most-once, that a NULL `visible_most_relevant` survives the round trip as None (a coerced
0 would silently become a demotion), and that the age window + LEFT JOIN really exclude comments
that are too new, too old, un-navigable, or already checked.
"""

from datetime import datetime, timedelta, timezone

import mysql.connector
import pytest

from cqc_lem.utilities import db

pytestmark = pytest.mark.integration

_EMAIL = "comment-outcomes-628@example.test"


def _schema_available() -> bool:
    """A reachable server is not enough — these tests need the migrated schema. An un-migrated DB
    (a bare local server) skips instead of erroring; CI provisions it before the suite runs.
    """
    try:
        config = db._get_mysql_config()
        connection = mysql.connector.connect(connect_timeout=3, **config)
    except Exception:  # noqa: BLE001 - unset/incomplete DB env means "no server here", so skip
        return False
    try:
        cursor = connection.cursor()
        cursor.execute("SHOW TABLES LIKE 'comment_outcomes'")
        present = bool(cursor.fetchone())
        cursor.close()
        return present
    except Exception:  # noqa: BLE001
        return False
    finally:
        connection.close()


def _exec(sql: str, params=(), fetch: bool = False):
    connection = db.get_db_connection()
    cursor = connection.cursor()
    try:
        cursor.execute(sql, params)
        rows = cursor.fetchall() if fetch else None
        connection.commit()
        return rows, cursor.lastrowid
    finally:
        cursor.close()
        connection.close()


def _add_comment_log(user_id: int, post_url: str, message: str, hours_ago: int) -> int:
    created = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=hours_ago)
    _rows, log_id = _exec(
        "INSERT INTO logs (user_id, action_type, post_url, message, result, created_at) "
        "VALUES (%s,'comment',%s,%s,'success',%s)", (user_id, post_url, message, created))
    return log_id


@pytest.fixture
def user_id():
    if not _schema_available():
        pytest.skip("no migrated MySQL schema available for the comment-outcomes integration test")
    _exec("DELETE FROM users WHERE email=%s", (_EMAIL,))  # CASCADE clears logs + outcomes
    db.add_user(_EMAIL, "x")
    uid = db.get_user_id(_EMAIL)
    assert uid, "test user was not created"
    yield uid
    _exec("DELETE FROM users WHERE email=%s", (_EMAIL,))


@pytest.mark.integration
class TestOutcomeTargets:
    def test_only_navigable_comments_inside_the_age_window_are_due(self, user_id):
        due = _add_comment_log(user_id, "feedurn://urn:li:activity:1", "due", hours_ago=30)
        _add_comment_log(user_id, "feedurn://urn:li:activity:2", "too new", hours_ago=2)
        _add_comment_log(user_id, "feedurn://urn:li:activity:3", "too old", hours_ago=400)
        _add_comment_log(user_id, "feedpost://hash", "not navigable", hours_ago=30)
        ids = [r["log_id"] for r in db.get_comment_outcome_targets(user_id)]
        assert ids == [due]

    def test_a_checked_comment_is_never_offered_again(self, user_id):
        log_id = _add_comment_log(user_id, "feedurn://urn:li:activity:9", "done", hours_ago=30)
        assert db.record_comment_outcome(user_id, log_id, post_key="feedurn://urn:li:activity:9",
                                         reply_count=1) is True
        assert db.get_comment_outcome_targets(user_id) == []

    def test_a_skipped_check_also_stops_the_re_walk(self, user_id):
        log_id = _add_comment_log(user_id, "feedurn://urn:li:activity:8", "gone", hours_ago=30)
        db.record_comment_outcome(user_id, log_id, status="skipped",
                                  skip_reason="comment-not-found")
        assert db.get_comment_outcome_targets(user_id) == []

    def test_limit_is_respected(self, user_id):
        for i in range(4):
            _add_comment_log(user_id, f"feedurn://urn:li:activity:1{i}", "x", hours_ago=30)
        assert len(db.get_comment_outcome_targets(user_id, limit=2)) == 2

    def test_another_users_comments_are_not_returned(self, user_id):
        _add_comment_log(user_id, "feedurn://urn:li:activity:5", "mine", hours_ago=30)
        assert db.get_comment_outcome_targets(user_id + 10_000) == []


@pytest.mark.integration
class TestRecordAndRead:
    def test_round_trips_every_field(self, user_id):
        log_id = _add_comment_log(user_id, "feedurn://urn:li:activity:20", "c", hours_ago=30)
        db.record_comment_outcome(user_id, log_id, post_key="feedurn://urn:li:activity:20",
                                  author_replied=True, reply_count=3, like_count=7,
                                  visible_most_relevant=False, our_reply_sent=True)
        row = db.get_comment_outcomes(user_id)[0]
        assert row["log_id"] == log_id
        assert row["author_replied"] == 1 and row["our_reply_sent"] == 1
        assert row["reply_count"] == 3 and row["like_count"] == 7
        assert row["visible_most_relevant"] == 0
        assert row["status"] == "checked"

    def test_ambiguous_visibility_stays_null(self, user_id):
        log_id = _add_comment_log(user_id, "feedurn://urn:li:activity:21", "c", hours_ago=30)
        db.record_comment_outcome(user_id, log_id, visible_most_relevant=None)
        # NULL, not 0 — a coerced 0 would read as a confirmed demotion the DOM never gave us.
        assert db.get_comment_outcomes(user_id)[0]["visible_most_relevant"] is None

    def test_upsert_refreshes_instead_of_duplicating(self, user_id):
        log_id = _add_comment_log(user_id, "feedurn://urn:li:activity:22", "c", hours_ago=30)
        db.record_comment_outcome(user_id, log_id, reply_count=1, visible_most_relevant=True)
        db.record_comment_outcome(user_id, log_id, reply_count=4, visible_most_relevant=False)
        rows = db.get_comment_outcomes(user_id)
        assert len(rows) == 1
        assert rows[0]["reply_count"] == 4 and rows[0]["visible_most_relevant"] == 0

    def test_missing_log_id_is_rejected(self, user_id):
        assert db.record_comment_outcome(user_id, None) is False

    def test_reads_are_scoped_to_the_user(self, user_id):
        log_id = _add_comment_log(user_id, "feedurn://urn:li:activity:23", "c", hours_ago=30)
        db.record_comment_outcome(user_id, log_id)
        assert db.get_comment_outcomes(user_id + 10_000) == []

    def test_deleting_the_user_cascades_the_outcomes(self, user_id):
        log_id = _add_comment_log(user_id, "feedurn://urn:li:activity:24", "c", hours_ago=30)
        db.record_comment_outcome(user_id, log_id)
        _exec("DELETE FROM users WHERE id=%s", (user_id,))
        rows, _ = _exec("SELECT id FROM comment_outcomes WHERE user_id=%s", (user_id,), fetch=True)
        assert rows == []
