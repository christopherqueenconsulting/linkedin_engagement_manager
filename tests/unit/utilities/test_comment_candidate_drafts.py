"""Phase 3 of #2647: drafts for pooled candidates, the review lane, and its approve/cancel."""

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import mysql.connector

from cqc_lem.platform.db.repositories import comment_candidates as repo
from cqc_lem.utilities import comment_candidate_drafts as cd, review_actions as ra
from cqc_lem.utilities.review_queue import LANE_IDS, get_review_queue

M = "cqc_lem.utilities.comment_candidate_drafts."
CUR = "cqc_lem.platform.db.repositories.comment_candidates.db_cursor"
NOW = datetime(2026, 11, 24, 12, 0, tzinfo=timezone.utc)


def _cursor(rowcount=1, rows=None):
    cur = MagicMock()
    cur.rowcount = rowcount
    cur.fetchall.return_value = rows or []
    ctx = MagicMock()
    ctx.__enter__.return_value = cur
    return ctx, cur


def _on():
    return patch(M + "drafts_enabled", return_value=True)


class TestDraftCandidates:
    def test_off_does_nothing(self):
        with patch(M + "drafts_enabled", return_value=False), \
                patch(M + "get_drafted_comment_candidates") as read:
            assert cd.draft_candidates(1)["reason"] == "off"
        read.assert_not_called()

    def test_unreadable_backlog_drafts_nothing(self):
        with _on(), patch(M + "get_drafted_comment_candidates", return_value=None), \
                patch(M + "get_candidates_to_draft") as rows:
            assert cd.draft_candidates(1)["reason"] == "backlog unreadable"
        rows.assert_not_called()

    def test_full_backlog_drafts_nothing(self):
        waiting = [{"draft_text": "x"}] * 3
        with _on(), patch(M + "get_drafted_comment_candidates", return_value=waiting), \
                patch(M + "draft_backlog_max", return_value=3), \
                patch(M + "get_candidates_to_draft") as rows:
            assert cd.draft_candidates(1)["reason"] == "backlog full"
        rows.assert_not_called()

    def test_unreadable_candidates_and_empty_pool(self):
        with _on(), patch(M + "get_drafted_comment_candidates", return_value=[]):
            with patch(M + "get_candidates_to_draft", return_value=None):
                assert cd.draft_candidates(1)["reason"] == "candidates unreadable"
            with patch(M + "get_candidates_to_draft", return_value=[]):
                assert cd.draft_candidates(1)["reason"] == "nothing to draft"

    def test_no_profile_drafts_nothing(self):
        rows = [{"id": 1, "post_key": "k", "post_text": "t"}]
        with _on(), patch(M + "get_drafted_comment_candidates", return_value=[]), \
                patch(M + "get_candidates_to_draft", return_value=rows), \
                patch("cqc_lem.utilities.linkedin.helper.load_profile_for_user", return_value=None), \
                patch("cqc_lem.utilities.ai.ai_helper.generate_ai_response") as gen:
            assert cd.draft_candidates(1)["reason"] == "no profile"
        gen.assert_not_called()

    def test_drafts_saves_and_skips_a_declined_post(self):
        rows = [{"id": 1, "post_key": "a", "post_text": "post a"},
                {"id": 2, "post_key": "b", "post_text": "post b"}]
        with _on(), patch(M + "get_drafted_comment_candidates", return_value=[]), \
                patch(M + "get_candidates_to_draft", return_value=rows), \
                patch(M + "get_engagement_preferences", return_value={}), \
                patch(M + "get_recent_comment_texts", return_value=["old"]), \
                patch("cqc_lem.utilities.linkedin.helper.load_profile_for_user", return_value=object()), \
                patch("cqc_lem.utilities.ai.ai_helper.generate_ai_response",
                      side_effect=["a draft", None]) as gen, \
                patch(M + "save_candidate_draft", return_value=True) as save, \
                patch(M + "skip_candidate", return_value=True) as skip:
            out = cd.draft_candidates(1)
        assert (out["drafted"], out["skipped"]) == (1, 1)
        save.assert_called_once_with(1, 1, "a draft")
        skip.assert_called_once_with(1, 2)
        assert gen.call_args_list[1].kwargs["recent_comments"][0] == "a draft"

    def test_a_fault_never_raises(self):
        with _on(), patch(M + "get_drafted_comment_candidates", side_effect=RuntimeError("x")):
            assert cd.draft_candidates(1)["reason"] == "error"


class TestEnabledAndEnv:
    def test_needs_both_flags(self):
        with patch(M + "flag_enabled", return_value=True), patch(M + "pool_enabled", return_value=False):
            assert cd.drafts_enabled(1) is False
        with patch(M + "flag_enabled", return_value=True), patch(M + "pool_enabled", return_value=True):
            assert cd.drafts_enabled(1) is True

    def test_flag_fault_is_off(self):
        with patch(M + "flag_enabled", side_effect=RuntimeError("x")):
            assert cd.drafts_enabled(1) is False

    def test_env_limits(self, monkeypatch):
        monkeypatch.setenv("COMMENT_DRAFT_BATCH", "7")
        monkeypatch.setenv("COMMENT_DRAFT_BACKLOG_MAX", "junk")
        assert cd.draft_batch_size() == 7
        assert cd.draft_backlog_max() == cd.DEFAULT_BACKLOG_MAX


class TestReviewLane:
    def test_lane_is_registered(self):
        assert "comment_candidates" in LANE_IDS
        assert "comment_candidates" in ra.ACTION_LANES

    def test_items_carry_draft_and_low_score_hold(self):
        rows = [{"id": 5, "author": "Pat", "score": 0.1, "draft_text": "Nice take", "scanned_at": NOW},
                {"id": 6, "author": "Sam", "score": 0.9, "draft_text": "Agreed", "scanned_at": NOW}]
        with patch(M + "get_drafted_comment_candidates", return_value=rows):
            items = cd.review_items(1, NOW)
        assert items[0]["hold_reason"] == cd.LOW_SCORE_REASON and items[1]["hold_reason"] is None
        assert items[0]["allowed_actions"] == ["approve", "cancel"]

    def test_unreadable_rows_raise_so_the_queue_skips_the_lane(self):
        with patch(M + "get_drafted_comment_candidates", return_value=None):
            try:
                cd.review_items(1, NOW)
            except RuntimeError:
                return
        raise AssertionError("expected RuntimeError")

    def test_queue_flags_claims_numbers_names_and_low_score(self):
        rows = [{"id": 5, "author": "Pat", "score": 0.1, "scanned_at": NOW,
                 "draft_text": "We grew revenue 40% last year working with Acme Corp."}]
        with patch(M + "get_drafted_comment_candidates", return_value=rows):
            items = get_review_queue(1, lane="comment_candidates", now=NOW)
        flags = set(items[0]["risk_flags"])
        assert {"number", "first_person_claim", "named_entity", "gate_near_miss"} <= flags

    def test_approve_and_cancel_map_to_not_found_when_nothing_moved(self):
        with patch.object(ra, "approve_comment_candidate", return_value=True):
            assert ra.apply_review_action(1, "comment_candidates", 5, "approve")["ok"] is True
        with patch.object(ra, "cancel_comment_candidate", return_value=False):
            out = ra.apply_review_action(1, "comment_candidates", 5, "cancel")
        assert out["ok"] is False and out["error"] == ra.NOT_FOUND


class TestRepository:
    def test_save_draft_moves_new_to_drafted_only(self):
        ctx, cur = _cursor()
        with patch(CUR, return_value=ctx):
            assert repo.save_candidate_draft(1, 9, "  hi  ") is True
        sql, params = cur.execute.call_args[0]
        assert "status = %s" in sql and "AND status = %s" in sql
        assert params == ("drafted", "hi", 9, 1, "new")

    def test_save_draft_refuses_blank_text(self):
        with patch(CUR) as cur:
            assert repo.save_candidate_draft(1, 9, "  ") is False
        cur.assert_not_called()

    def test_approve_requires_drafted_and_stamps_time(self):
        ctx, cur = _cursor()
        with patch(CUR, return_value=ctx):
            assert repo.approve_comment_candidate(1, 9) is True
        sql, params = cur.execute.call_args[0]
        assert "approved_at = CURRENT_TIMESTAMP" in sql
        assert params == ("approved", 9, 1, "drafted")

    def test_move_is_false_when_no_row_changed_or_on_a_fault(self):
        ctx, _ = _cursor(rowcount=0)
        with patch(CUR, return_value=ctx):
            assert repo.cancel_comment_candidate(1, 9) is False
        with patch(CUR, side_effect=mysql.connector.Error("boom")):
            assert repo.skip_candidate(1, 9) is False

    def test_reads_filter_status_and_are_none_on_a_fault(self):
        ctx, cur = _cursor(rows=[{"id": 1}])
        with patch(CUR, return_value=ctx):
            assert repo.get_candidates_to_draft(1, 5) == [{"id": 1}]
            assert repo.get_drafted_comment_candidates(1) == [{"id": 1}]
        sqls = [c[0][0] for c in cur.execute.call_args_list]
        assert "status = 'new'" in sqls[0] and "draft_text IS NULL" in sqls[0]
        assert "status = 'drafted'" in sqls[1]
        with patch(CUR, side_effect=mysql.connector.Error("boom")):
            assert repo.get_candidates_to_draft(1) is None
            assert repo.get_drafted_comment_candidates(1) is None

    def test_insert_stores_post_text_clipped(self):
        ctx, cur = _cursor()
        with patch(CUR, return_value=ctx):
            repo.insert_comment_candidate(1, "k", "feed", 0.5, post_text="x" * 9000)
        assert len(cur.execute.call_args[0][1][-1]) == repo._POST_TEXT_MAX

    def test_facade_exports(self):
        from cqc_lem.utilities import db
        for name in ("approve_comment_candidate", "cancel_comment_candidate", "save_candidate_draft",
                     "skip_candidate", "get_candidates_to_draft", "get_drafted_comment_candidates"):
            assert name in db.__all__ and getattr(db, name) is getattr(repo, name)


class TestTask:
    def test_task_sums_per_user_results(self):
        from cqc_lem.app import run_scheduler as rs
        with patch.object(rs, "get_active_user_ids", return_value=[1, 2]), \
                patch("cqc_lem.utilities.comment_candidate_drafts.draft_candidates",
                      return_value={"drafted": 2, "skipped": 1, "reason": None}):
            assert "4 drafted, 2 skipped across 2" in rs.auto_draft_comment_candidates.run()

    def test_task_with_no_users(self):
        from cqc_lem.app import run_scheduler as rs
        with patch.object(rs, "get_active_user_ids", return_value=[]):
            assert rs.auto_draft_comment_candidates.run() == "No active users"
