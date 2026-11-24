"""Weekly-report reads: engager ICP scores match leads on person_key, never the raw URL (#2632)."""

import datetime as dt
from unittest.mock import MagicMock, patch

import mysql.connector
import pytest

from cqc_lem.platform.db.repositories import posts as repo

pytestmark = pytest.mark.unit

_CUR = "cqc_lem.platform.db.repositories.posts.db_cursor"
_START = dt.datetime(2026, 11, 16)
_END = dt.datetime(2026, 11, 23)


def _ctx(*fetches):
    cur = MagicMock()
    cur.fetchall.side_effect = list(fetches)
    ctx = MagicMock()
    ctx.__enter__.return_value = cur
    return ctx


def test_icp_scores_match_on_person_key_not_raw_url():
    engagers = [
        {"engager_name": "Ann", "engager_profile_url": "https://www.linkedin.com/in/ann/?trk=x"},
        {"engager_name": "Bob Roe", "engager_profile_url": None},
        {"engager_name": "Cy", "engager_profile_url": "https://www.linkedin.com/in/cy"},
    ]
    leads = [{"person_key": "in:ann", "icp_score": 80},
             {"person_key": "name:bob roe", "icp_score": 20}]
    with patch(_CUR, return_value=_ctx(engagers, leads)):
        assert repo.get_weekly_engager_icp_scores(1, _START, _END) == [80, 20, None]


def test_icp_scores_none_on_db_fault():
    with patch(_CUR, side_effect=mysql.connector.Error("boom")):
        assert repo.get_weekly_engager_icp_scores(1, _START, _END) is None
