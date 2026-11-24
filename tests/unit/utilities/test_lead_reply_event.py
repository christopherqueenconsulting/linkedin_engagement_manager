"""Inbound reply events (#2658 P4): record_lead_reply_event dedup and guards."""

from unittest.mock import MagicMock, patch

import mysql.connector

from cqc_lem.platform.db.repositories import outreach


class _Cur:
    def __init__(self, seen=None):
        self.seen = seen
        self.sql = []
        self.lastrowid = 7

    def execute(self, sql, params=None):
        self.sql.append((sql, params))

    def fetchone(self):
        return self.seen


def _run(cur, **kw):
    ctx = MagicMock()
    ctx.__enter__.return_value = cur
    args = dict(user_id=1, channel="dm", evidence_ref="dm:abc", person_name="Ann",
                person_profile_url="https://www.linkedin.com/in/ann/")
    args.update(kw)
    with patch.object(outreach, "db_cursor", return_value=ctx):
        return outreach.record_lead_reply_event(**args)


def _events(cur):
    return [p for s, p in cur.sql if s.startswith("INSERT INTO lead_stage_events")]


def test_first_reply_writes_one_derived_event():
    cur = _Cur(seen=None)
    assert _run(cur) is True
    assert _events(cur) == [("reply:dm", "derived", "dm:abc", 7)]


def test_same_reply_again_writes_nothing():
    cur = _Cur(seen=(1,))
    assert _run(cur) is False
    assert _events(cur) == []


def test_unidentifiable_person_or_channel_or_ref_writes_nothing():
    for kw in ({"person_name": None, "person_profile_url": None}, {"channel": "email"},
               {"evidence_ref": ""}):
        cur = _Cur()
        assert _run(cur, **kw) is False
        assert cur.sql == []


def test_db_error_is_false_not_raised():
    ctx = MagicMock()
    ctx.__enter__.side_effect = mysql.connector.Error("boom")
    with patch.object(outreach, "db_cursor", return_value=ctx):
        assert outreach.record_lead_reply_event(1, "dm", "dm:x", person_name="Ann") is False
