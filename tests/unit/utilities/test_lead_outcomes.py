"""Unit tests for outcome tracking, Phase 1 (issue #2633).

Covers the funnel builder, the organic engagement summary, the derived-history hook in the nightly
rebuild, and the stage invariants.
"""

from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit


class TestBuildLeadFunnel:
    def test_counts_per_month_and_step_conversions(self):
        from cqc_lem.utilities.lead_scoring import build_lead_funnel
        funnel = build_lead_funnel([
            ("2026-10", "in_conversation", 10), ("2026-10", "meeting_booked", 4),
            ("2026-11", "in_conversation", 5), ("2026-11", "meeting_booked", 1),
            ("2026-11", "meeting_held", 1), ("2026-11", "won", 1),
            ("2026-11", "cold", 99),  # not a funnel stage
        ])
        assert [m["month"] for m in funnel["months"]] == ["2026-10", "2026-11"]
        oct_, nov = funnel["months"]
        assert oct_["in_conversation"] == 10 and oct_["conversion"]["conversation_to_booked"] == 0.4
        assert nov["won"] == 1 and nov["conversion"]["held_to_won"] == 1.0
        assert funnel["totals"]["in_conversation"] == 15
        assert funnel["totals"]["conversion"]["conversation_to_booked"] == round(5 / 15, 4)
        assert "cold" not in nov

    def test_a_zero_denominator_is_unknown_not_zero(self):
        from cqc_lem.utilities.lead_scoring import build_lead_funnel
        funnel = build_lead_funnel([("2026-11", "won", 1)])
        assert funnel["months"][0]["conversion"]["held_to_won"] is None

    def test_empty_history(self):
        from cqc_lem.utilities.lead_scoring import build_lead_funnel
        assert build_lead_funnel([])["months"] == []


class TestStageInvariants:
    def test_derive_stage_never_returns_an_outcome_stage(self):
        from cqc_lem.utilities.db import LeadSignalKind, LeadStage
        from cqc_lem.utilities.lead_scoring import FUNNEL_STAGES, LeadActivity, derive_stage
        acts = [LeadActivity(kind=LeadSignalKind.INTENT, detail="approved", occurred_at=None),
                LeadActivity(kind=LeadSignalKind.DM, detail="", occurred_at=None)]
        stage = derive_stage(acts, 100)
        assert stage not in (LeadStage.MEETING_BOOKED, LeadStage.MEETING_HELD,
                             LeadStage.WON, LeadStage.LOST)
        assert LeadStage.WON in FUNNEL_STAGES


class TestRebuildWritesDerivedHistory:
    def test_snapshot_is_taken_before_the_reset_and_diffed_after(self):
        from cqc_lem.app import run_scheduler
        order = []
        with patch("cqc_lem.utilities.db.get_lead_stage_map",
                   side_effect=lambda uid: order.append("snapshot") or {"in:a": "warm"}), \
             patch("cqc_lem.utilities.db.reset_lead_scores",
                   side_effect=lambda uid: order.append("reset")), \
             patch("cqc_lem.utilities.db.get_lead_activity", return_value=[]), \
             patch("cqc_lem.utilities.db.record_derived_stage_changes",
                   side_effect=lambda uid, before: order.append(("record", before))):
            assert run_scheduler._rebuild_leads_for_user(3) == 0
        assert order == ["snapshot", "reset", ("record", {"in:a": "warm"})]


    def test_unreadable_snapshot_records_no_history(self):
        from cqc_lem.app import run_scheduler
        with patch("cqc_lem.utilities.db.get_lead_stage_map", return_value=None), \
             patch("cqc_lem.utilities.db.reset_lead_scores"), \
             patch("cqc_lem.utilities.db.get_lead_activity", return_value=[]), \
             patch("cqc_lem.utilities.db.record_derived_stage_changes") as rec:
            assert run_scheduler._rebuild_leads_for_user(3) == 0
        rec.assert_not_called()


class TestOrganicEngagementSummary:
    def test_26_of_30_own_comments_leaves_4(self):
        from cqc_lem.utilities.post_stats import organic_engagement_summary
        out = organic_engagement_summary([
            {"reactions": 0, "comments": 30, "own_comments": 26, "reposts": 0, "impressions": 100}])
        assert out["organic_comments"] == 4
        assert out["organic_rate"] < out["raw_rate"]
        assert out["unknown_posts"] == 0 and out["measured_posts"] == 1

    def test_null_own_comments_is_unknown_not_zero(self):
        from cqc_lem.utilities.post_stats import organic_engagement_summary
        out = organic_engagement_summary([
            {"reactions": 3, "comments": 5, "own_comments": None, "reposts": 0, "impressions": 100}])
        assert out["organic_rate"] is None
        assert out["raw_rate"] is not None
        assert out["unknown_posts"] == 1

    def test_unknown_posts_are_left_out_of_the_organic_pool(self):
        from cqc_lem.utilities.post_stats import organic_engagement_summary
        known = {"reactions": 1, "comments": 2, "own_comments": 1, "reposts": 0, "impressions": 100}
        unknown = {"reactions": 9, "comments": 9, "own_comments": None, "reposts": 0, "impressions": 100}
        alone = organic_engagement_summary([known])
        assert organic_engagement_summary([known, unknown])["organic_rate"] == alone["organic_rate"]

    def test_posts_without_impressions_and_empty_rows_are_skipped(self):
        from cqc_lem.utilities.post_stats import organic_engagement_summary
        out = organic_engagement_summary([None, {"reactions": 1, "comments": 1, "own_comments": 0,
                                                 "impressions": None}])
        assert out["raw_rate"] is None and out["organic_rate"] is None
        assert out["measured_posts"] == 0

    def test_performance_table_carries_the_organic_columns(self):
        import datetime as dt

        from cqc_lem.utilities.post_stats import build_performance_table
        row = {"post_id": 1, "scheduled_time": dt.datetime(2026, 11, 1, 9), "reactions": 0,
               "comments": 30, "own_comments": 26, "reposts": 0, "impressions": 100}
        out = build_performance_table([row])[0]
        assert out["third_party_comments"] == 4 and out["organic_engagement_rate"] is not None
        row["own_comments"] = None
        out = build_performance_table([row])[0]
        assert out["third_party_comments"] is None and out["organic_engagement_rate"] is None
