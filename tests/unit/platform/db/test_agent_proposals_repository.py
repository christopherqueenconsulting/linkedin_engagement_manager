"""The `agent_change_proposals` repository: SQL shape, the limit, the state machine, fail-soft (#2803)."""

import json
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import mysql.connector
import pytest

from cqc_lem.platform.db.repositories import agent_proposals as repo

pytestmark = pytest.mark.unit

_CUR = "cqc_lem.platform.db.repositories.agent_proposals.db_cursor"
_ERR = mysql.connector.Error("boom")
NOW = datetime(2026, 11, 25, 12, 0, 0)


def _cursor(rowcount=1, rows=None, one=None, lastrowid=7):
    cur = MagicMock()
    cur.rowcount = rowcount
    cur.lastrowid = lastrowid
    cur.fetchall.return_value = rows
    cur.fetchone.return_value = one
    ctx = MagicMock()
    ctx.__enter__.return_value = cur
    return cur, ctx


def _sql(cur):
    return " ".join(c[0][0] for c in cur.execute.call_args_list)


def test_facade_re_exports_every_name():
    from cqc_lem.utilities import db
    for name in ("create_agent_proposal", "decide_agent_proposal", "get_agent_proposal",
                 "list_pending_agent_proposals", "expire_agent_proposals", "set_agent_proposal_outcome",
                 "reopen_agent_proposal", "count_pending_agent_proposals", "MAX_OPEN_PROPOSALS",
                 "PROPOSAL_TTL_DAYS", "ProposalCreate"):
        assert getattr(db, name) is getattr(repo, name)
        assert name in db.__all__


def test_the_constants_are_the_rulings():
    assert repo.PROPOSAL_TTL_DAYS == 7 and repo.MAX_OPEN_PROPOSALS == 5


class TestCreate:
    def test_it_inserts_a_pending_row_that_expires_in_seven_days(self):
        cur, ctx = _cursor(one=(0,))
        with patch(_CUR, return_value=ctx):
            made = repo.create_agent_proposal(3, "settings", {"a": {"before": 1, "requested": 2}},
                                              "why", 11, now=NOW)
        assert made == repo.ProposalCreated(repo.ProposalCreate.CREATED, 7)
        insert = cur.execute.call_args_list[-1]
        params = insert[0][1]
        assert "INSERT INTO agent_change_proposals" in insert[0][0]
        assert params[0] == 3 and params[1] == "settings" and json.loads(params[2]) == {
            "a": {"before": 1, "requested": 2}}
        assert params[3] == "why" and params[4] == "pending" and params[5] == 11
        assert params[6] == NOW and params[7] == NOW + timedelta(days=7)

    def test_it_counts_only_open_unexpired_rows_of_this_user(self):
        cur, ctx = _cursor(one=(0,))
        with patch(_CUR, return_value=ctx):
            repo.create_agent_proposal(3, "settings", {}, None, None, now=NOW)
        count_sql, count_params = cur.execute.call_args_list[0][0]
        assert "status = %s" in count_sql and "expires_at > %s" in count_sql and "user_id = %s" in count_sql
        assert count_params == (3, "pending", NOW)

    def test_at_the_limit_nothing_is_inserted(self):
        cur, ctx = _cursor(one=(5,))
        with patch(_CUR, return_value=ctx):
            made = repo.create_agent_proposal(3, "settings", {}, "r", 1, now=NOW)
        assert made.status == repo.ProposalCreate.LIMIT and made.proposal_id is None
        assert "INSERT" not in _sql(cur)

    def test_the_reason_is_cut_to_its_bound_and_an_empty_one_is_null(self):
        cur, ctx = _cursor(one=(0,))
        with patch(_CUR, return_value=ctx):
            repo.create_agent_proposal(3, "settings", {}, "x" * 5000, 1, now=NOW)
            repo.create_agent_proposal(3, "settings", {}, "", 1, now=NOW)
        inserts = [c[0][1] for c in cur.execute.call_args_list if "INSERT" in c[0][0]]
        assert len(inserts[0][3]) == repo.MAX_PROPOSAL_REASON_CHARS and inserts[1][3] is None

    def test_a_database_fault_is_failed_not_an_exception(self):
        with patch(_CUR, side_effect=_ERR):
            assert repo.create_agent_proposal(3, "settings", {}, None, None).status == \
                repo.ProposalCreate.FAILED


class TestTheStateMachineLivesInTheSql:
    def test_a_decision_needs_pending_and_not_expired_in_the_where(self):
        cur, ctx = _cursor(rowcount=1)
        with patch(_CUR, return_value=ctx):
            assert repo.decide_agent_proposal(3, 9, "approved", 5, NOW) is True
        sql, params = cur.execute.call_args[0]
        assert "WHERE id = %s AND user_id = %s AND status = %s" in sql and "expires_at > %s" in sql
        assert params == ("approved", NOW, 5, 9, 3, "pending", NOW)

    def test_a_row_that_is_not_pending_or_has_expired_changes_nothing(self):
        _cur, ctx = _cursor(rowcount=0)
        with patch(_CUR, return_value=ctx):
            assert repo.decide_agent_proposal(3, 9, "approved", 5, NOW) is False

    @pytest.mark.parametrize("status", ["pending", "expired", "bogus"])
    def test_only_approved_or_rejected_can_be_decided(self, status):
        with pytest.raises(ValueError):
            repo.decide_agent_proposal(3, 9, status)

    def test_expire_marks_only_pending_rows_past_their_time(self):
        cur, ctx = _cursor(rowcount=2)
        with patch(_CUR, return_value=ctx):
            assert repo.expire_agent_proposals(3, NOW) == 2
        sql, params = cur.execute.call_args[0]
        assert "SET status = %s" in sql and "expires_at <= %s" in sql
        assert params == ("expired", 3, "pending", NOW)

    def test_reopen_only_undoes_an_approval(self):
        cur, ctx = _cursor()
        with patch(_CUR, return_value=ctx):
            assert repo.reopen_agent_proposal(3, 9) is True
        assert cur.execute.call_args[0][1] == ("pending", 9, 3, "approved")

    def test_the_outcome_is_stored_as_json_for_this_user_only(self):
        cur, ctx = _cursor()
        with patch(_CUR, return_value=ctx):
            assert repo.set_agent_proposal_outcome(3, 9, {"applied": ["a"]}) is True
        sql, params = cur.execute.call_args[0]
        assert "user_id = %s" in sql and json.loads(params[0]) == {"applied": ["a"]}


class TestReads:
    def test_get_is_scoped_to_the_owner_and_decodes_json(self):
        row = {"id": 9, "payload": '{"a": {"before": 1, "requested": 2}}', "outcome": None}
        cur, ctx = _cursor(one=row)
        with patch(_CUR, return_value=ctx):
            got = repo.get_agent_proposal(3, 9)
        assert got["payload"] == {"a": {"before": 1, "requested": 2}}
        assert cur.execute.call_args[0][1] == (9, 3)
        assert "user_id = %s" in cur.execute.call_args[0][0]

    def test_a_missing_row_is_none(self):
        _cur, ctx = _cursor(one=None)
        with patch(_CUR, return_value=ctx):
            assert repo.get_agent_proposal(3, 9) is None

    def test_list_sweeps_expired_rows_first_and_reads_only_open_ones(self):
        cur, ctx = _cursor(rows=[{"id": 1, "payload": "{}", "outcome": None}])
        with patch(_CUR, return_value=ctx):
            rows = repo.list_pending_agent_proposals(3, now=NOW)
        statements = [c[0][0] for c in cur.execute.call_args_list]
        assert statements[0].startswith("UPDATE") and statements[1].startswith("SELECT")
        assert "status = %s AND expires_at > %s" in statements[1] and rows[0]["payload"] == {}

    def test_a_failed_list_is_none_never_an_empty_list(self):
        _cur, ctx = _cursor()
        with patch(_CUR, return_value=ctx), \
                patch.object(repo, "expire_agent_proposals", return_value=0):
            ctx.__enter__.side_effect = _ERR
            assert repo.list_pending_agent_proposals(3, now=NOW) is None

    def test_a_non_json_payload_reads_as_none_not_a_crash(self):
        _cur, ctx = _cursor(one={"id": 1, "payload": "{not json", "outcome": b'{"x": 1}'})
        with patch(_CUR, return_value=ctx):
            got = repo.get_agent_proposal(3, 1)
        assert got["payload"] is None and got["outcome"] == {"x": 1}

    def test_count_and_the_fault_paths(self):
        _cur, ctx = _cursor(one=(4,))
        with patch(_CUR, return_value=ctx):
            assert repo.count_pending_agent_proposals(3, NOW) == 4
        with patch(_CUR, side_effect=_ERR):
            assert repo.count_pending_agent_proposals(3) is None
            assert repo.get_agent_proposal(3, 1) is None
            assert repo.decide_agent_proposal(3, 1, "rejected") is False
            assert repo.expire_agent_proposals(3) == 0
            assert repo.set_agent_proposal_outcome(3, 1, {}) is False
            assert repo.reopen_agent_proposal(3, 1) is False
