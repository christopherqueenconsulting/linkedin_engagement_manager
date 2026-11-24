"""ICP-audience report: n on every row, an insufficient floor, and per-user isolation (#2667)."""

import datetime as dt
from unittest.mock import MagicMock, patch

import pytest

from cqc_lem.platform.db.repositories import posts as repo
from cqc_lem.utilities import icp_audience_report as mod

pytestmark = pytest.mark.unit

_CUR = "cqc_lem.platform.db.repositories.posts.db_cursor"


def _row(score, month=11, segment=None):
    return {"icp_score": score, "segment_id": segment, "engaged_at": dt.datetime(2026, month, 5)}


def test_unreadable_rows_are_not_a_zero_report():
    report = mod.build_icp_audience_report(None)
    assert report["readable"] is False and report["overall"] is None


def test_below_floor_has_no_share():
    report = mod.build_icp_audience_report([_row(80), _row(10)])
    assert report["overall"]["n"] == 2 and report["overall"]["share"] is None
    assert report["overall"]["insufficient"] is True
    assert report["gap_by_month"][0]["gap"] is None


def test_share_gap_and_unscored_excluded_from_denominator():
    rows = [_row(80)] * 3 + [_row(20)] * 2 + [_row(None)] * 4
    report = mod.build_icp_audience_report(rows)
    assert report["overall"] == {"engagers": 9, "n": 5, "inside": 3, "share": 0.6,
                                 "insufficient": False}
    month = report["gap_by_month"][0]
    assert month["month"] == "2026-11" and month["share"] == 0.6 and month["gap"] == pytest.approx(0.4)


def test_segments_top_with_names_and_floor():
    rows = [_row(70, segment="cto")] * 6 + [_row(70, segment="vp")] * 2
    report = mod.build_icp_audience_report(rows, {"cto": "CTOs"})
    top, small = report["segments"]
    assert (top["name"], top["n"], top["insufficient"]) == ("CTOs", 6, False)
    assert top["share_of_segmented"] == 0.75
    assert (small["name"], small["n"], small["share_of_segmented"]) == ("vp", 2, None)
    assert report["not_measured"][0]["key"] == "titles"


def test_load_reads_only_the_callers_rows():
    now = dt.datetime(2026, 11, 24, tzinfo=dt.timezone.utc)
    with patch("cqc_lem.utilities.db.get_icp_audience_rows", return_value=[]) as rows, \
         patch("cqc_lem.utilities.db.get_engagement_preferences", return_value={}) as prefs:
        mod.load_icp_audience_report(7, now)
    assert rows.call_args.args[0] == 7 and prefs.call_args.args[0] == 7
    assert rows.call_args.args[1] == dt.datetime(2026, 6, 1, tzinfo=dt.timezone.utc)


def test_repository_scopes_every_statement_by_user_id():
    cur = MagicMock()
    cur.fetchall.side_effect = [
        [{"engager_name": "Ann", "engager_profile_url": "https://www.linkedin.com/in/ann/",
          "last_engaged_at": dt.datetime(2026, 11, 2)}],
        [{"person_key": "in:ann", "icp_score": 80}],
        [{"recipient_profile_url": "https://www.linkedin.com/in/ann", "segment_id": "cto"}],
    ]
    ctx = MagicMock()
    ctx.__enter__.return_value = cur
    with patch(_CUR, return_value=ctx):
        rows = repo.get_icp_audience_rows(42, dt.datetime(2026, 6, 1), dt.datetime(2026, 11, 24))
    assert rows == [{"engaged_at": dt.datetime(2026, 11, 2), "icp_score": 80, "segment_id": "cto"}]
    statements = [c.args for c in cur.execute.call_args_list]
    assert len(statements) == 3
    for sql, params in statements:
        assert "user_id=%s" in sql and params[0] == 42


def test_repository_unidentifiable_engager_matches_nothing():
    cur = MagicMock()
    cur.fetchall.side_effect = [
        [{"engager_name": None, "engager_profile_url": None,
          "last_engaged_at": dt.datetime(2026, 11, 2)}],
        [{"person_key": "", "icp_score": 90}],
        [{"recipient_profile_url": "", "segment_id": "cto"}],
    ]
    ctx = MagicMock()
    ctx.__enter__.return_value = cur
    with patch(_CUR, return_value=ctx):
        rows = repo.get_icp_audience_rows(1, dt.datetime(2026, 6, 1), dt.datetime(2026, 11, 24))
    assert rows == [{"engaged_at": dt.datetime(2026, 11, 2), "icp_score": None, "segment_id": None}]


def test_repository_none_on_db_fault():
    import mysql.connector
    with patch(_CUR, side_effect=mysql.connector.Error("boom")):
        assert repo.get_icp_audience_rows(1, dt.datetime(2026, 6, 1), dt.datetime(2026, 11, 1)) is None
