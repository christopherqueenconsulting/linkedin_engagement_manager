"""Profile-view-to-call attribution (#2668): the pure walk-back and its storage step."""

from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import mysql.connector

from cqc_lem.platform.db.repositories import booking
from cqc_lem.utilities import lead_scoring as ls, weekly_report as wr

_BOOKED = datetime(2026, 11, 20, 12, 0, 0)


def _act(kind, days_before):
    return ls.LeadActivity(kind=kind, occurred_at=_BOOKED - timedelta(days=days_before))


def test_viewer_connection_reply_then_call_gives_first_and_last_touch():
    acts = [_act("reply", 2), _act("profile_view", 20), _act("connect", 10)]
    out = ls.attribute_touches(acts, _BOOKED)
    assert out["attribution"] == "attributed"
    assert out["first_touch"] == "profile_view@2026-10-31T12:00:00"
    assert out["last_touch"] == "reply@2026-11-18T12:00:00"


def test_no_touch_is_unattributed_never_guessed():
    assert ls.attribute_touches([], _BOOKED) == {
        "attribution": "unattributed", "first_touch": None, "last_touch": None}


def test_touch_outside_window_or_after_call_or_undated_does_not_count():
    acts = [_act("profile_view", 45), _act("dm", -1), ls.LeadActivity(kind="connect"),
            _act("funnel", 3)]
    assert ls.attribute_touches(acts, _BOOKED)["attribution"] == "unattributed"


class _Cur:
    def __init__(self, lead, event, replies):
        self.rows = [lead, event]
        self.replies = replies
        self.sql = []

    def execute(self, sql, params=None):
        self.sql.append((sql, params))

    def fetchone(self):
        return self.rows.pop(0)

    def fetchall(self):
        return self.replies


def _ctx(cur):
    c = MagicMock()
    c.__enter__.return_value = cur
    return c


def _run(cur, activity_rows):
    with patch.object(booking, "db_cursor", side_effect=lambda **k: _ctx(cur)), \
            patch("cqc_lem.platform.db.repositories.outreach.get_lead_activity",
                  return_value=activity_rows):
        return booking.attribute_booking(1, 7, "inv-1")


def test_attribute_booking_writes_touches_from_activity_and_replies():
    cur = _Cur(("in:ann",), (99, _BOOKED), [(_BOOKED - timedelta(days=1), "reply:dm")])
    rows = [{"kind": "profile_view", "person_name": "Ann",
             "person_profile_url": "https://www.linkedin.com/in/ann/",
             "occurred_at": _BOOKED - timedelta(days=9), "detail": ""}]
    assert _run(cur, rows) is True
    update = [p for s, p in cur.sql if s.startswith("UPDATE lead_stage_events")][0]
    assert update == ("profile_view@2026-11-11T12:00:00", "reply@2026-11-19T12:00:00",
                      "attributed", 99)


def test_attribute_booking_with_no_activity_is_unattributed():
    cur = _Cur(("email:a@b.co",), (99, _BOOKED), [])
    assert _run(cur, []) is True
    update = [p for s, p in cur.sql if s.startswith("UPDATE lead_stage_events")][0]
    assert update == (None, None, "unattributed", 99)


def test_attribute_booking_missing_event_writes_nothing():
    cur = _Cur(("in:ann",), None, [])
    assert _run(cur, []) is False
    assert not [s for s, _ in cur.sql if s.startswith("UPDATE")]


def test_attribute_booking_fault_writes_nothing():
    with patch.object(booking, "db_cursor", side_effect=mysql.connector.Error("x")):
        assert booking.attribute_booking(1, 7, "inv-1") is False


def test_report_carries_conversions_and_names_unreadable_as_not_measured():
    kw = dict(week_start=_BOOKED - timedelta(days=7), week_end=_BOOKED, this_week_rows=[],
              last_week_rows=[], follower_snapshots=None, engager_icp_scores=None, pending_drafts=0)
    got = {"new_conversations": 2, "calls_booked": 1, "attributed": 1, "unattributed": 0,
           "unevaluated": 0}
    ok = wr.build_weekly_report(conversions=got, **kw)
    assert ok["conversions"] == got
    assert "new_conversations" not in {m["key"] for m in ok["not_measured"]}
    bad = wr.build_weekly_report(conversions=None, **kw)
    assert bad["conversions"] is None
    assert "new_conversations" in {m["key"] for m in bad["not_measured"]}
