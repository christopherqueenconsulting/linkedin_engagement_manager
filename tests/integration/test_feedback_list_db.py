"""Issue #1868 — the admin triage list query, proved against a live MySQL server.

The defect only exists on a real server. `ORDER BY created_at` over the reporter join cannot use an
index, so MySQL 8 filesorts, and it packs EVERY selected column into `sort_buffer_size` — `body` and
`context_json` included. In production that overflowed on 91 rows and the whole page died with
`1038 (HY001): Out of sort memory`. A fake cursor cannot hold that opinion: it returns whatever the
test handed it whatever the SQL says, which is why the unit tests can only assert the query's SHAPE.

So this reproduces the mechanism instead of describing it. It seeds rows wide enough that ONE packed
record does not fit a deliberately tiny session sort buffer, then runs the pre-fix query text as a
control. If that control does NOT fail, this server cannot express the bug and the test SKIPS rather
than passing on nothing.

It has already earned that. The first attempt at the fix kept an `ORDER BY k.created_at DESC` on the
outer select, reasoning that ordering by the driving table's own columns lets MySQL sort that table
and then join. This test failed it on a live server: MySQL 8 sorted the JOINED output instead and
raised the identical 1038. The outer sort is gone and the page is ordered in Python — and if an
outer ORDER BY ever comes back, this fails again the way production did.
"""

import mysql.connector
import pytest

from cqc_lem.utilities import db

pytestmark = pytest.mark.integration

_EMAIL = "feedback-list-1868@example.test"
_MIN_SORT_BUFFER = 32768  # MySQL's floor for the session variable.
# One row must not fit in _MIN_SORT_BUFFER once packed, which is the production mechanism in
# miniature: `body` is TEXT (65KB ceiling) and the widget's context routinely carries a screenshot.
_WIDE_BODY = "sort buffer filler " * 2000        # ~38 KB
_WIDE_CONTEXT = "screenshot data url filler " * 1500  # ~40 KB

# The query as it stood before this fix. Kept verbatim as the control: it is what must fail.
_PRE_FIX_SQL = (
    "SELECT f.id, f.user_id, f.source, f.type_hint, f.body, f.context_json, "
    "f.cluster_id, f.github_issue_number, f.status, f.sentiment, "
    "f.reviewed_by, f.reviewed_at, f.created_at, u.email, u.is_admin "
    "FROM feedback f LEFT JOIN users u ON u.id = f.user_id "
    "ORDER BY f.created_at DESC LIMIT %s OFFSET %s"
)


class _NoCloseConnection:
    """Hands `db_cursor` a connection whose session settings survive the call.

    `db_cursor` closes what it is given, and a pooled connection resets its session on close — so a
    `SET SESSION sort_buffer_size` made outside would be gone before the query under test ran.
    """

    def __init__(self, connection):
        self._connection = connection

    def cursor(self, *args, **kwargs):
        return self._connection.cursor(*args, **kwargs)

    def commit(self):
        self._connection.commit()

    def close(self):
        """No-op — the fixture that opened this connection is the one that closes it."""


def _schema_available() -> bool:
    """A reachable server with the `feedback` table. An un-migrated DB skips rather than errors."""
    try:
        connection = mysql.connector.connect(connect_timeout=3, **db._get_mysql_config())
    except Exception:  # noqa: BLE001 - unset/incomplete DB env means "no server here", so skip
        return False
    try:
        cursor = connection.cursor()
        cursor.execute("SHOW COLUMNS FROM feedback LIKE 'context_json'")
        present = bool(cursor.fetchone())
        cursor.close()
        return present
    except Exception:  # noqa: BLE001
        return False
    finally:
        connection.close()


def _exec(sql: str, params=()):
    connection = db.get_db_connection()
    cursor = connection.cursor()
    try:
        cursor.execute(sql, params)
        connection.commit()
    finally:
        cursor.close()
        connection.close()


@pytest.fixture
def seeded_feedback():
    """Twelve wide rows from one reporter — enough to page three times."""
    if not _schema_available():
        pytest.skip("no migrated MySQL schema available for the feedback list integration test")
    _exec("DELETE FROM users WHERE email=%s", (_EMAIL,))
    db.add_user(_EMAIL, "x")
    user_id = db.get_user_id(_EMAIL)
    assert user_id, "test user was not created"
    _exec("DELETE FROM feedback WHERE user_id=%s", (user_id,))

    ids = []
    for n in range(12):
        feedback_id = db.insert_feedback(
            f"{_EMAIL} report {n} — {_WIDE_BODY}",
            user_id=user_id,
            source=db.FeedbackSource.WIDGET,
            type_hint="bug",
            context={"route": "/admin/feedback", "screenshot": _WIDE_CONTEXT},
        )
        assert feedback_id, "seed row was not inserted"
        ids.append(feedback_id)
    # `created_at` defaults to NOW() at second resolution, so these all tie and `id` is what
    # actually orders them — which is the tiebreak both halves of the paged query must agree on.
    yield {"user_id": user_id, "ids": ids}
    _exec("DELETE FROM feedback WHERE user_id=%s", (user_id,))
    _exec("DELETE FROM users WHERE email=%s", (_EMAIL,))


@pytest.fixture
def tight_sort_buffer(seeded_feedback, monkeypatch):
    """Point the repository at a connection whose sort buffer cannot hold one packed row.

    Skips unless the pre-fix query actually fails on this server: without that control the rest of
    the test would pass on a server where nothing was ever at risk, which proves nothing.
    """
    connection = mysql.connector.connect(**db._get_mysql_config())
    cursor = connection.cursor()
    cursor.execute(f"SET SESSION sort_buffer_size = {_MIN_SORT_BUFFER}")
    try:
        cursor.execute(_PRE_FIX_SQL, (50, 0))
        cursor.fetchall()
    except mysql.connector.Error as err:
        reproduced = err.errno == 1038
    else:
        reproduced = False
    cursor.close()
    if not reproduced:
        connection.close()
        pytest.skip("this MySQL server does not reproduce the 1038 the fix is for")
    monkeypatch.setattr("cqc_lem.platform.db.connection.get_db_connection",
                        lambda: _NoCloseConnection(connection))
    yield seeded_feedback
    connection.close()


def _seeded_order(rows, ids):
    """Just this test's rows, in the order the query returned them.

    The table is not assumed to be empty: another integration test in the same worker database may
    have left rows of its own, and none of these assertions need it to be otherwise.
    """
    seeded = set(ids)
    return [r["id"] for r in rows if r["id"] in seeded]


class TestGetFeedbackListSurvivesTheSortBuffer:
    def test_the_page_the_old_query_could_not_return(self, tight_sort_buffer):
        ids = tight_sort_buffer["ids"]
        rows = db.get_feedback_list(limit=50)
        assert _seeded_order(rows, ids) == sorted(ids, reverse=True)
        mine = [r for r in rows if r["id"] in set(ids)]
        assert all(r["body"] and r["context_json"] for r in mine), \
            "the wide columns must still come back"
        assert {r["email"] for r in mine} == {_EMAIL}, "the reporter join must still answer"

    def test_the_status_filter_narrows_inside_the_derived_table(self, tight_sort_buffer):
        ids = tight_sort_buffer["ids"]
        assert _seeded_order(db.get_feedback_list(status=db.FeedbackStatus.NEW, limit=50), ids) \
            == sorted(ids, reverse=True)
        assert _seeded_order(db.get_feedback_list(status=db.FeedbackStatus.RESOLVED, limit=50),
                             ids) == []

    def test_paging_neither_repeats_nor_skips_a_row(self, tight_sort_buffer):
        one_page = [r["id"] for r in db.get_feedback_list(limit=200)]
        assert set(tight_sort_buffer["ids"]) <= set(one_page)
        paged = [r["id"]
                 for offset in range(0, len(one_page), 5)
                 for r in db.get_feedback_list(limit=5, offset=offset)]
        assert paged == one_page
