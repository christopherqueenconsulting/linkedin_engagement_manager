"""The `comment_candidates` repository: SQL shape, ranked read, fail-soft behaviour (issue #2635)."""

from datetime import date
from unittest.mock import MagicMock, patch

import mysql.connector
import pytest

from cqc_lem.platform.db.repositories import comment_candidates as repo

pytestmark = pytest.mark.unit

_CUR = "cqc_lem.platform.db.repositories.comment_candidates.db_cursor"
_ERR = mysql.connector.Error("boom")


def _cursor(rowcount=1, rows=None, one=None):
    cur = MagicMock()
    cur.rowcount = rowcount
    cur.fetchall.return_value = rows
    cur.fetchone.return_value = one
    ctx = MagicMock()
    ctx.__enter__.return_value = cur
    return cur, ctx


def test_the_facade_re_exports_every_name():
    from cqc_lem.utilities import db
    for name in ("insert_comment_candidate", "get_comment_candidates", "count_comment_candidates"):
        assert getattr(db, name) is getattr(repo, name)
        assert name in db.__all__


def test_insert_is_insert_ignore_and_true_only_for_a_new_row():
    cur, ctx = _cursor(rowcount=1)
    with patch(_CUR, return_value=ctx):
        assert repo.insert_comment_candidate(1, "k", "feed", 0.5, components={"a": 1}) is True
    assert "INSERT IGNORE INTO comment_candidates" in cur.execute.call_args[0][0]
    cur, ctx = _cursor(rowcount=0)
    with patch(_CUR, return_value=ctx):
        assert repo.insert_comment_candidate(1, "k", "feed", 0.5) is False


def test_insert_skips_an_empty_key_and_swallows_a_db_error():
    assert repo.insert_comment_candidate(1, "", "feed", 0.5) is False
    with patch(_CUR, side_effect=_ERR):
        assert repo.insert_comment_candidate(1, "k", "feed", 0.5) is False


def test_get_ranks_by_score_and_excludes_the_commented_ledger():
    cur, ctx = _cursor(rows=[{"id": 1}])
    with patch(_CUR, return_value=ctx):
        assert repo.get_comment_candidates(1, date(2026, 11, 23), limit=150) == [{"id": 1}]
    sql, params = cur.execute.call_args[0]
    assert "ORDER BY c.score DESC" in sql
    assert "NOT EXISTS (SELECT 1 FROM commented_posts" in sql
    assert params[0] == 1 and params[-1] == 150


def test_get_is_none_on_a_fault_never_empty():
    with patch(_CUR, side_effect=_ERR):
        assert repo.get_comment_candidates(1) is None


def test_count_reads_the_day_and_is_none_on_a_fault():
    cur, ctx = _cursor(one=(7,))
    with patch(_CUR, return_value=ctx):
        assert repo.count_comment_candidates(1, date(2026, 11, 23)) == 7
    cur, ctx = _cursor(one=None)
    with patch(_CUR, return_value=ctx):
        assert repo.count_comment_candidates(1) == 0
    with patch(_CUR, side_effect=_ERR):
        assert repo.count_comment_candidates(1) is None


def test_assist_activity_splits_and_sorts_timestamps():
    from datetime import datetime
    s, e = datetime(2026, 11, 1), datetime(2026, 11, 8)
    a, b = datetime(2026, 11, 2), datetime(2026, 11, 3)
    cur, ctx = _cursor(rows=[(b, None), (a, b), (datetime(2026, 10, 1), None)])
    with patch(_CUR, return_value=ctx):
        out = repo.get_assist_activity(1, s, e)
    assert out == {"approved_at": [a, b], "posted_at": [b]}


def test_assist_activity_none_on_a_fault():
    from datetime import datetime
    with patch(_CUR, side_effect=_ERR):
        assert repo.get_assist_activity(1, datetime(2026, 11, 1), datetime(2026, 11, 8)) is None


def test_comment_log_times_reads_times_and_is_none_on_a_fault():
    from datetime import datetime

    from cqc_lem.platform.db.repositories import engagement
    t = datetime(2026, 11, 2)
    cur, ctx = _cursor(rows=[(t,)])
    target = "cqc_lem.platform.db.repositories.engagement.db_cursor"
    with patch(target, return_value=ctx):
        assert engagement.get_comment_log_times(1, t, t) == [t]
    with patch(target, side_effect=_ERR):
        assert engagement.get_comment_log_times(1, t, t) is None
