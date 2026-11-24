"""get_weekly_conversion_counts (#2668): shape and fault behaviour."""

from datetime import datetime
from unittest.mock import MagicMock, patch

import mysql.connector

from cqc_lem.platform.db.repositories import outreach

_S, _E = datetime(2026, 11, 13), datetime(2026, 11, 20)


class _Cur:
    def __init__(self):
        self.one = (3,)
        self.all = [("attributed", 2), ("unattributed", 1), (None, 1)]

    def execute(self, sql, params=None):
        pass

    def fetchone(self):
        return self.one

    def fetchall(self):
        return self.all


def test_counts_split_attribution_states():
    ctx = MagicMock()
    ctx.__enter__.return_value = _Cur()
    with patch.object(outreach, "db_cursor", return_value=ctx):
        assert outreach.get_weekly_conversion_counts(1, _S, _E) == {
            "new_conversations": 3, "calls_booked": 4, "attributed": 2, "unattributed": 1,
            "unevaluated": 1}


def test_read_fault_is_none_not_zero():
    with patch.object(outreach, "db_cursor", side_effect=mysql.connector.Error("x")):
        assert outreach.get_weekly_conversion_counts(1, _S, _E) is None
