"""Unit tests for the leads DB helpers (issue #484) — activity aggregation across every existing
source, ICP fact lookup, the idempotent upsert that preserves operator columns, board listing, the
hot list, and partial updates.
"""

from datetime import datetime
from unittest.mock import patch

import mysql.connector
import pytest

pytestmark = pytest.mark.unit

_OUTREACH = "cqc_lem.platform.db.repositories.outreach"
_GET_CONN = "cqc_lem.platform.db.connection.get_db_connection"


class TestGetLeadActivity:
    def test_reads_every_source_and_tags_each_row_with_its_kind(self, fake_cursor):
        conn, cursor = fake_cursor()
        cursor.fetchall.side_effect = lambda: [{"person_name": "Jane", "person_profile_url": None,
                                                "occurred_at": None, "detail": ""}]
        with patch(f"{_GET_CONN}", return_value=conn):
            from cqc_lem.utilities.db import _LEAD_ACTIVITY_SOURCES, get_lead_activity
            rows = get_lead_activity(4, days=30)
        assert len(rows) == len(_LEAD_ACTIVITY_SOURCES)
        assert {r["kind"] for r in rows} == {str(k) for k, _ in _LEAD_ACTIVITY_SOURCES}
        assert all(c[0][1] == (4, 30) for c in cursor.execute.call_args_list)

    def test_sources_cover_engagers_intent_dms_views_invites_and_the_funnel(self):
        from cqc_lem.utilities.db import _LEAD_ACTIVITY_SOURCES
        tables = " ".join(sql for _, sql in _LEAD_ACTIVITY_SOURCES)
        for table in ("post_engagers", "lead_signals", "scheduled_dms", "dm_followups",
                      "connection_requests", "outreach_funnel_targets"):
            assert table in tables

    def test_one_broken_source_does_not_lose_the_others(self, fake_cursor):
        conn, cursor = fake_cursor(fetch_all=[{"person_name": "Jane"}])
        cursor.execute.side_effect = [mysql.connector.Error("no such table")] + [None] * 10
        with patch(f"{_GET_CONN}", return_value=conn), \
                patch("cqc_lem.platform.db.repositories.outreach.log_error"):
            from cqc_lem.utilities.db import _LEAD_ACTIVITY_SOURCES, get_lead_activity
            rows = get_lead_activity(1)
        assert len(rows) == len(_LEAD_ACTIVITY_SOURCES) - 1

    def test_dm_followups_split_profile_views_from_dms(self):
        from cqc_lem.utilities.db import _LEAD_ACTIVITY_SOURCES, LeadSignalKind
        views = [sql for kind, sql in _LEAD_ACTIVITY_SOURCES
                 if kind == LeadSignalKind.PROFILE_VIEW]
        assert len(views) == 1 and "event_type='profile_viewer'" in views[0]


class TestGetProfileFacts:
    def test_keys_the_facts_by_profile_url(self, fake_cursor):
        conn, cursor = fake_cursor(fetch_all=[{"profile_url": "u1", "job_title": "CTO",
                                              "company_name": "Acme", "industry": "AI"}])
        with patch(f"{_GET_CONN}", return_value=conn):
            from cqc_lem.utilities.db import get_profile_facts
            facts = get_profile_facts(["u1", "u2"])
        assert facts["u1"]["job_title"] == "CTO"
        assert set(cursor.execute.call_args[0][1]) == {"u1", "u1/", "u2", "u2/"}

    def test_looks_up_querystring_and_trailing_slash_variants(self, fake_cursor):
        conn, cursor = fake_cursor(fetch_all=[])
        with patch(f"{_GET_CONN}", return_value=conn):
            from cqc_lem.utilities.db import get_profile_facts
            get_profile_facts(["https://www.linkedin.com/in/jane-doe?trk=feed"])
        params = set(cursor.execute.call_args[0][1])
        assert "https://www.linkedin.com/in/jane-doe" in params
        assert "https://www.linkedin.com/in/jane-doe/" in params

    def test_variants_of_the_same_person_are_not_queried_twice(self, fake_cursor):
        conn, cursor = fake_cursor(fetch_all=[])
        with patch(f"{_GET_CONN}", return_value=conn):
            from cqc_lem.utilities.db import get_profile_facts
            get_profile_facts(["https://linkedin.com/in/jane/",
                               "https://linkedin.com/in/jane?trk=x"])
        params = cursor.execute.call_args[0][1]
        assert len(params) == len(set(params))

    def test_size_geography_and_timing_facts_stay_neutral_when_unscraped(self, fake_cursor):
        """#2655: unscraped values read None (neutral credit), never "null" / False."""
        conn, _ = fake_cursor(fetch_all=[
            {"profile_url": "a", "company_size": "null", "location": None,
             "recent_job_change": None},
            {"profile_url": "b", "company_size": "120", "location": "Austin, US",
             "recent_job_change": "true"},
            {"profile_url": "c", "recent_job_change": "false"}])
        with patch(f"{_GET_CONN}", return_value=conn):
            from cqc_lem.utilities.db import get_profile_facts
            facts = get_profile_facts(["a", "b", "c"])
        assert (facts["a"]["company_size"], facts["a"]["location"],
                facts["a"]["recent_job_change"]) == (None, None, None)
        assert facts["b"]["company_size"] == "120" and facts["b"]["recent_job_change"] is True
        assert facts["c"]["recent_job_change"] is False

    def test_no_urls_skips_the_query(self):
        with patch(f"{_GET_CONN}") as get:
            from cqc_lem.utilities.db import get_profile_facts
            assert get_profile_facts([]) == {} and get_profile_facts(None) == {}
        get.assert_not_called()

    def test_db_error_returns_no_facts(self, fake_cursor):
        conn, _ = fake_cursor(execute_error=mysql.connector.Error("boom"))
        with patch(f"{_GET_CONN}", return_value=conn), patch(f"{_OUTREACH}.log_error"):
            from cqc_lem.utilities.db import get_profile_facts
            assert get_profile_facts(["u1"]) == {}


class TestResetLeadScores:
    def test_zeroes_computed_columns_only(self, fake_cursor):
        conn, cursor = fake_cursor()
        with patch(f"{_GET_CONN}", return_value=conn):
            from cqc_lem.utilities.db import reset_lead_scores
            assert reset_lead_scores(3) is True
        sql = cursor.execute.call_args[0][0]
        assert "score=0" in sql and "stage='cold'" in sql
        for operator_col in ("manual_stage", "notes", "dismissed"):
            assert operator_col not in sql

    def test_clears_the_stale_recommended_action(self, fake_cursor):
        conn, cursor = fake_cursor()
        with patch(f"{_GET_CONN}", return_value=conn):
            from cqc_lem.utilities.db import reset_lead_scores
            reset_lead_scores(3)
        assert "next_action=NULL" in cursor.execute.call_args[0][0]

    def test_db_error_is_reported_not_raised(self, fake_cursor):
        conn, _ = fake_cursor(execute_error=mysql.connector.Error("boom"))
        with patch(f"{_GET_CONN}", return_value=conn), patch(f"{_OUTREACH}.log_error"):
            from cqc_lem.utilities.db import reset_lead_scores
            assert reset_lead_scores(3) is False


class TestUpsertLead:
    def test_upserts_on_the_person_key(self, fake_cursor):
        conn, cursor = fake_cursor()
        with patch(f"{_GET_CONN}", return_value=conn):
            from cqc_lem.utilities.db import LeadStage, upsert_lead
            assert upsert_lead(2, "in:jane", person_name="Jane", score=81,
                               stage=LeadStage.HOT, signals="engaged,intent") is True
        sql, params = cursor.execute.call_args[0]
        assert "INSERT INTO leads" in sql and "ON DUPLICATE KEY UPDATE" in sql
        assert params[1] == "in:jane" and params[7] == "hot"

    def test_never_overwrites_operator_columns(self, fake_cursor):
        conn, cursor = fake_cursor()
        with patch(f"{_GET_CONN}", return_value=conn):
            from cqc_lem.utilities.db import upsert_lead
            upsert_lead(2, "in:jane")
        update_clause = cursor.execute.call_args[0][0].split("ON DUPLICATE KEY UPDATE")[1]
        for operator_col in ("manual_stage", "notes", "dismissed"):
            assert operator_col not in update_clause

    def test_scores_are_clamped_into_their_columns(self, fake_cursor):
        conn, cursor = fake_cursor()
        with patch(f"{_GET_CONN}", return_value=conn):
            from cqc_lem.utilities.db import upsert_lead
            upsert_lead(2, "in:jane", score=9999, icp_score=-5, engagement_score=300)
        params = cursor.execute.call_args[0][1]
        assert (params[4], params[5], params[6]) == (255, 0, 255)

    def test_blank_person_key_is_refused(self):
        with patch(f"{_GET_CONN}") as get:
            from cqc_lem.utilities.db import upsert_lead
            assert upsert_lead(2, "") is False
        get.assert_not_called()

    def test_db_error_returns_false(self, fake_cursor):
        conn, _ = fake_cursor(execute_error=mysql.connector.Error("boom"))
        with patch(f"{_GET_CONN}", return_value=conn), patch(f"{_OUTREACH}.log_error"):
            from cqc_lem.utilities.db import upsert_lead
            assert upsert_lead(2, "in:jane") is False


class TestGetLeads:
    def _rows(self):
        return [{"id": 1, "user_id": 2, "person_key": "in:jane", "score": 80, "stage": "hot",
                 "dismissed": 0, "last_signal_at": datetime(2026, 7, 24, 9, 0),
                 "created_at": datetime(2026, 7, 20, 9, 0), "updated_at": None,
                 "first_signal_at": None}]

    def test_hides_dismissed_leads_by_default(self, fake_cursor):
        conn, cursor = fake_cursor(fetch_one={"c": 1}, fetch_all=self._rows())
        with patch(f"{_GET_CONN}", return_value=conn):
            from cqc_lem.utilities.db import get_leads
            result = get_leads(2)
        assert result["total"] == 1
        assert "dismissed = 0" in cursor.execute.call_args_list[0][0][0]

    def test_stage_filter_honors_a_manual_override(self, fake_cursor):
        conn, cursor = fake_cursor(fetch_one={"c": 0}, fetch_all=[])
        with patch(f"{_GET_CONN}", return_value=conn):
            from cqc_lem.utilities.db import get_leads
            get_leads(2, stage_filter="hot", include_dismissed=True)
        sql, params = cursor.execute.call_args_list[0][0]
        assert "COALESCE(manual_stage, stage) = %s" in sql
        assert "dismissed = 0" not in sql and params == (2, "hot")

    def test_serializes_timestamps_and_the_dismissed_flag(self, fake_cursor):
        conn, _ = fake_cursor(fetch_one={"c": 1}, fetch_all=self._rows())
        with patch(f"{_GET_CONN}", return_value=conn):
            from cqc_lem.utilities.db import get_leads
            lead = get_leads(2)["leads"][0]
        assert lead["last_signal_at"] == "2026-07-24T09:00:00"
        assert lead["dismissed"] is False

    def test_paginates(self, fake_cursor):
        conn, cursor = fake_cursor(fetch_one={"c": 40}, fetch_all=[])
        with patch(f"{_GET_CONN}", return_value=conn):
            from cqc_lem.utilities.db import get_leads
            get_leads(2, page=3, page_size=10)
        assert cursor.execute.call_args_list[1][0][1][-2:] == (10, 20)

    def test_db_error_returns_an_empty_board(self, fake_cursor):
        conn, _ = fake_cursor(execute_error=mysql.connector.Error("boom"))
        with patch(f"{_GET_CONN}", return_value=conn), patch(f"{_OUTREACH}.log_error"):
            from cqc_lem.utilities.db import get_leads
            assert get_leads(2) == {"leads": [], "total": 0, "page": 1, "page_size": 100}


class TestHotLeads:
    def test_hot_list_covers_hot_and_warmer_stages(self, fake_cursor):
        conn, cursor = fake_cursor(fetch_all=[{"id": 1}])
        with patch(f"{_GET_CONN}", return_value=conn):
            from cqc_lem.utilities.db import get_hot_leads
            assert get_hot_leads(2, limit=5) == [{"id": 1}]
        sql, params = cursor.execute.call_args[0]
        assert "('hot','in_conversation','opportunity')" in sql and params == (2, 5)

    def test_hot_list_db_error_is_empty(self, fake_cursor):
        conn, _ = fake_cursor(execute_error=mysql.connector.Error("boom"))
        with patch(f"{_GET_CONN}", return_value=conn), patch(f"{_OUTREACH}.log_error"):
            from cqc_lem.utilities.db import get_hot_leads
            assert get_hot_leads(2) == []

    def test_count_hot_leads(self, fake_cursor):
        conn, _ = fake_cursor(fetch_one=(3,))
        with patch(f"{_GET_CONN}", return_value=conn):
            from cqc_lem.utilities.db import count_hot_leads
            assert count_hot_leads(2) == 3

    def test_count_hot_leads_db_error_is_zero(self, fake_cursor):
        conn, _ = fake_cursor(execute_error=mysql.connector.Error("boom"))
        with patch(f"{_GET_CONN}", return_value=conn):
            from cqc_lem.utilities.db import count_hot_leads
            assert count_hot_leads(2) == 0


class TestGetAndUpdateLead:
    def test_get_lead_returns_the_row(self, fake_cursor):
        conn, _ = fake_cursor(fetch_one={"id": 9, "user_id": 2})
        with patch(f"{_GET_CONN}", return_value=conn):
            from cqc_lem.utilities.db import get_lead
            assert get_lead(9)["user_id"] == 2

    def test_get_lead_db_error_returns_none(self, fake_cursor):
        conn, _ = fake_cursor(execute_error=mysql.connector.Error("boom"))
        with patch(f"{_GET_CONN}", return_value=conn), patch(f"{_OUTREACH}.log_error"):
            from cqc_lem.utilities.db import get_lead
            assert get_lead(9) is None

    def test_updates_only_the_supplied_fields(self, fake_cursor):
        conn, cursor = fake_cursor()
        with patch(f"{_GET_CONN}", return_value=conn):
            from cqc_lem.utilities.db import update_lead
            assert update_lead(9, notes="met at a conference") is True
        sql, params = cursor.execute.call_args[0]
        assert sql == "UPDATE leads SET notes = %s WHERE id = %s"
        assert params == ("met at a conference", 9)

    def test_stage_override_and_dismissal(self, fake_cursor):
        conn, cursor = fake_cursor()
        with patch(f"{_GET_CONN}", return_value=conn):
            from cqc_lem.utilities.db import LeadStage, update_lead
            update_lead(9, manual_stage=LeadStage.OPPORTUNITY, dismissed=True)
        update = next(c[0] for c in cursor.execute.call_args_list if c[0][0].startswith("UPDATE"))
        assert update[1] == ("opportunity", 1, 9)

    def test_empty_stage_clears_the_override(self, fake_cursor):
        conn, cursor = fake_cursor()
        with patch(f"{_GET_CONN}", return_value=conn):
            from cqc_lem.utilities.db import update_lead
            update_lead(9, manual_stage="")
        update = next(c[0] for c in cursor.execute.call_args_list if c[0][0].startswith("UPDATE"))
        assert update[1] == (None, 9)

    def test_stage_change_writes_a_manual_history_row(self, fake_cursor):
        conn, cursor = fake_cursor()
        cursor.fetchone.side_effect = [(5, "hot"), ("meeting_booked",)]
        with patch(f"{_GET_CONN}", return_value=conn):
            from cqc_lem.utilities.db import LeadStage, update_lead
            update_lead(9, manual_stage=LeadStage.MEETING_BOOKED)
        sql, params = cursor.execute.call_args[0]
        assert "INSERT INTO lead_stage_events" in sql
        assert params == (9, 5, "hot", "meeting_booked", "manual")

    def test_same_stage_writes_no_history_row(self, fake_cursor):
        conn, cursor = fake_cursor()
        cursor.fetchone.side_effect = [(5, "won"), ("won",)]
        with patch(f"{_GET_CONN}", return_value=conn):
            from cqc_lem.utilities.db import LeadStage, update_lead
            update_lead(9, manual_stage=LeadStage.WON)
        assert not any("INSERT" in c[0][0] for c in cursor.execute.call_args_list)

    def test_notes_only_edit_never_touches_history(self, fake_cursor):
        conn, cursor = fake_cursor()
        with patch(f"{_GET_CONN}", return_value=conn):
            from cqc_lem.utilities.db import update_lead
            update_lead(9, notes="x")
        assert [c[0][0] for c in cursor.execute.call_args_list] == [
            "UPDATE leads SET notes = %s WHERE id = %s"]

    def test_deal_value_set_and_cleared(self, fake_cursor):
        conn, cursor = fake_cursor()
        with patch(f"{_GET_CONN}", return_value=conn):
            from cqc_lem.utilities.db import update_lead
            update_lead(9, deal_value=1500.5)
            assert cursor.execute.call_args[0][1] == (1500.5, 9)
            update_lead(9, deal_value="")
        assert cursor.execute.call_args[0][1] == (None, 9)


class TestDerivedStageHistory:
    def test_records_only_the_leads_whose_computed_stage_moved(self, fake_cursor):
        conn, cursor = fake_cursor()
        cursor.fetchall.return_value = [(1, "in:a", "hot"), (2, "in:b", "cold"), (3, "in:c", "warm")]
        before = {"in:a": "warm", "in:b": "cold"}  # in:c is new to this re-score
        with patch(f"{_GET_CONN}", return_value=conn):
            from cqc_lem.utilities.db import record_derived_stage_changes
            assert record_derived_stage_changes(7, before) == 2
        inserts = [c[0][1] for c in cursor.execute.call_args_list if "INSERT" in c[0][0]]
        assert inserts == [(1, 7, "warm", "hot", "derived"), (3, 7, None, "warm", "derived")]

    def test_stage_map_reads_person_key_to_stage(self, fake_cursor):
        conn, cursor = fake_cursor()
        cursor.fetchall.return_value = [("in:a", "hot")]
        with patch(f"{_GET_CONN}", return_value=conn):
            from cqc_lem.utilities.db import get_lead_stage_map
            assert get_lead_stage_map(7) == {"in:a": "hot"}

    def test_db_errors_degrade_quietly(self, fake_cursor):
        conn, _ = fake_cursor(execute_error=mysql.connector.Error("boom"))
        with patch(f"{_GET_CONN}", return_value=conn), patch(f"{_OUTREACH}.log_error"):
            from cqc_lem.utilities.db import (
                get_lead_funnel_counts,
                get_lead_stage_map,
                record_derived_stage_changes,
            )
            assert get_lead_stage_map(1) is None
            assert record_derived_stage_changes(1, {}) == 0
            assert get_lead_funnel_counts(1) is None

    def test_funnel_counts_read(self, fake_cursor):
        conn, cursor = fake_cursor()
        cursor.fetchall.return_value = [("2026-11", "won", 2)]
        with patch(f"{_GET_CONN}", return_value=conn):
            from cqc_lem.utilities.db import get_lead_funnel_counts
            assert get_lead_funnel_counts(1) == [("2026-11", "won", 2)]

    def test_nothing_to_update_is_refused(self):
        with patch(f"{_GET_CONN}") as get:
            from cqc_lem.utilities.db import update_lead
            assert update_lead(9) is False
        get.assert_not_called()

    def test_db_error_returns_false(self, fake_cursor):
        conn, _ = fake_cursor(execute_error=mysql.connector.Error("boom"))
        with patch(f"{_GET_CONN}", return_value=conn), patch(f"{_OUTREACH}.log_error"):
            from cqc_lem.utilities.db import update_lead
            assert update_lead(9, notes="x") is False
