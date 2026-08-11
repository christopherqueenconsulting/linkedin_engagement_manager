"""Unit tests for the pending-reason surface in the content plan (issue #421): the gates record WHY
a draft is held, the status-setter demotes on exactly the findings that demote, and 'edit &
re-score' can move a fixed post PENDING -> APPROVED without a full regenerate.
"""

from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit

_RCP = "cqc_lem.app.run_content_plan"
_DB = "cqc_lem.utilities.db"

_POST = ("Cutting AI cost is a routing problem, not a model problem.\n\nLast quarter I routed our "
         "simplest calls to a cheap model and dropped the bill by 38%.")
_NEAR_DUP = ("Cutting AI cost is a routing problem, not a model problem. Last quarter I routed the "
             "simplest calls to a cheap model and dropped our bill by 38% that way.")


class TestEvaluatePostGates:
    def _gates(self, **kwargs):
        from cqc_lem.app import run_content_plan as rcp
        kwargs.setdefault("post_type", "text")
        kwargs.setdefault("content", _POST)
        with patch(f"{_RCP}._post_missing_required_asset",
                   return_value=kwargs.pop("missing_asset", False)):
            return rcp.evaluate_post_gates(42, kwargs.pop("content"), kwargs.pop("post_type"),
                                           **kwargs)

    def test_a_clean_post_has_no_findings(self):
        assert self._gates(authenticity_score=90, recent_texts=["something else entirely"]) == []

    def test_low_authenticity_is_recorded_with_score_and_threshold(self):
        findings = self._gates(authenticity_score=41,
                               engagement_prefs={"authenticity_score_min": 60})
        assert [f["gate"] for f in findings] == ["authenticity"]
        assert findings[0]["score"] == 41 and findings[0]["threshold"] == 60
        assert findings[0]["demoted"] is True

    def test_the_users_own_threshold_decides(self):
        # Same score, looser threshold -> no hold. This is the point of the setting.
        assert self._gates(authenticity_score=41,
                           engagement_prefs={"authenticity_score_min": 30}) == []

    def test_unscored_posts_are_never_held(self):
        assert self._gates(authenticity_score=None) == []

    def test_disabled_gate_records_nothing(self, monkeypatch):
        monkeypatch.setenv("AUTHENTICITY_GATE_ENABLED", "off")
        assert self._gates(authenticity_score=1) == []

    def test_near_duplicate_is_recorded_against_the_post_history(self):
        findings = self._gates(content=_NEAR_DUP, recent_texts=[_POST])
        assert [f["gate"] for f in findings] == ["similarity"]
        assert findings[0]["score"] > findings[0]["threshold"]

    def test_similarity_is_skipped_without_history(self):
        assert self._gates(recent_texts=None) == []

    def test_a_semantic_duplicate_is_held_and_the_finding_names_the_measure(self):
        # Issue #1265. Lexically distinct from _POST, so only the embedding measure can catch it.
        reworded = ("Frontier pricing on trivial requests is where the money goes.\n\nI moved the "
                    "easy jobs onto a small model three months ago and watched what we spend drop "
                    "by more than a third.")
        vectors = [[1.0, 0.0], [0.9, (1 - 0.81) ** 0.5]]
        with patch("cqc_lem.utilities.ai.content_framework.embed_comments", return_value=vectors):
            findings = self._gates(content=reworded, recent_texts=[_POST])
        assert [f["gate"] for f in findings] == ["similarity"]
        assert findings[0]["threshold"] == 0.78  # the cosine ceiling, not the 0.55 overlap one
        assert any("embedding cosine" in d for d in findings[0]["details"])

    def test_the_finding_names_the_lexical_measure_when_embeddings_are_down(self):
        with patch("cqc_lem.utilities.ai.content_framework.embed_comments", return_value=None):
            findings = self._gates(content=_NEAR_DUP, recent_texts=[_POST])
        assert findings[0]["threshold"] == 0.55
        assert any("token overlap" in d for d in findings[0]["details"])

    def test_off_focus_is_advisory_only(self):
        findings = self._gates(engagement_prefs={"focus_topics": ["deep sea fishing"]})
        assert [f["gate"] for f in findings] == ["focus_alignment"]
        assert findings[0]["demoted"] is False

    def test_on_focus_post_is_not_flagged(self):
        assert self._gates(engagement_prefs={"focus_topics": ["routing", "AI cost"]}) == []

    def test_missing_media_is_recorded(self):
        findings = self._gates(post_type="video", missing_asset=True)
        assert [f["gate"] for f in findings] == ["missing_asset"]
        assert findings[0]["demoted"] is True

    def test_every_failing_gate_is_reported_not_just_the_first(self):
        findings = self._gates(content=_NEAR_DUP, post_type="video", missing_asset=True,
                               authenticity_score=10, recent_texts=[_POST],
                               engagement_prefs={"focus_topics": ["deep sea fishing"]})
        assert {f["gate"] for f in findings} == {"missing_asset", "authenticity", "similarity",
                                                 "focus_alignment"}


class TestStatusSetterRecordsTheReason:
    """The reason has to be persisted where the review queue reads it, on the post itself."""

    def _run(self, authenticity_score=None, missing_asset=False, auto_schedule=True):
        from cqc_lem.app import run_content_plan as rcp
        post = [{"user_id": 1, "id": 42, "post_type": "text", "buyer_stage": "awareness"}]
        with patch(f"{_RCP}.get_planned_posts_within_buffer", return_value=post), \
             patch(f"{_RCP}.count_ready_posts_within_buffer", return_value=0), \
             patch(f"{_RCP}.create_content", return_value=(_POST, None)), \
             patch(f"{_RCP}._score_and_persist_dwell"), \
             patch(f"{_RCP}.update_db_post_content"), \
             patch(f"{_RCP}.update_db_post_status") as status, \
             patch(f"{_RCP}.update_db_post_gate_reason") as reason, \
             patch(f"{_RCP}.get_engagement_preferences", return_value={}), \
             patch(f"{_RCP}.get_user_preferences",
                   return_value={"auto_schedule_posts": auto_schedule}), \
             patch(f"{_RCP}._post_missing_required_asset", return_value=missing_asset), \
             patch(f"{_RCP}.get_post_authenticity_score", return_value=authenticity_score):
            rcp.auto_create_weekly_content(user_id=1)
        return status, reason

    def test_a_passing_post_is_approved_with_no_reason(self):
        from cqc_lem.utilities.db import PostStatus
        status, reason = self._run(authenticity_score=95)
        status.assert_called_once_with(42, PostStatus.APPROVED)
        reason.assert_called_once_with(42, [])

    def test_low_authenticity_demotes_and_explains(self, monkeypatch):
        from cqc_lem.utilities.db import PostStatus
        monkeypatch.setenv("AUTHENTICITY_SCORE_MIN", "60")
        status, reason = self._run(authenticity_score=30)
        status.assert_called_once_with(42, PostStatus.PENDING)
        findings = reason.call_args[0][1]
        assert findings[0]["gate"] == "authenticity" and findings[0]["score"] == 30
        assert findings[0]["remediation"]

    def test_missing_media_demotes_and_explains(self):
        from cqc_lem.utilities.db import PostStatus
        status, reason = self._run(missing_asset=True)
        status.assert_called_once_with(42, PostStatus.PENDING)
        assert reason.call_args[0][1][0]["gate"] == "missing_asset"

    def test_manual_review_users_still_get_the_reason(self):
        from cqc_lem.utilities.db import PostStatus
        status, reason = self._run(authenticity_score=30, auto_schedule=False)
        status.assert_called_once_with(42, PostStatus.PENDING)
        assert reason.call_args[0][1][0]["gate"] == "authenticity"

    def test_an_unreadable_score_still_leaves_the_media_gate_holding(self):
        # A DB hiccup on the score read must not let an assetless video post auto-approve.
        from cqc_lem.app import run_content_plan as rcp
        from cqc_lem.utilities.db import PostStatus
        post = [{"user_id": 1, "id": 42, "post_type": "video", "buyer_stage": "awareness"}]
        with patch(f"{_RCP}.get_planned_posts_within_buffer", return_value=post), \
             patch(f"{_RCP}.count_ready_posts_within_buffer", return_value=0), \
             patch(f"{_RCP}.create_content", return_value=(_POST, None)), \
             patch(f"{_RCP}._score_and_persist_dwell"), \
             patch(f"{_RCP}.update_db_post_content"), \
             patch(f"{_RCP}.update_db_post_status") as status, \
             patch(f"{_RCP}.update_db_post_gate_reason") as reason, \
             patch(f"{_RCP}.get_engagement_preferences", return_value={}), \
             patch(f"{_RCP}.get_user_preferences", return_value={"auto_schedule_posts": True}), \
             patch(f"{_RCP}._post_missing_required_asset", return_value=True), \
             patch(f"{_RCP}.get_post_authenticity_score", side_effect=RuntimeError("db down")):
            rcp.auto_create_weekly_content(user_id=1)
        status.assert_called_once_with(42, PostStatus.PENDING)
        assert reason.call_args[0][1][0]["gate"] == "missing_asset"

    def test_generation_never_holds_on_similarity_however_duplicated_the_draft(self):
        # Pins what #1265 did NOT change: the similarity gate needs the post history, and the only
        # caller that hands `evaluate_post_gates` one is the edit & re-score endpoint. At generation
        # a draft that is still too similar after its one retry SHIPS with a warning — it is never
        # demoted here. Whether it should be is issue #1452; this test is what makes that a
        # deliberate change rather than a silent one.
        from cqc_lem.app import run_content_plan as rcp
        from cqc_lem.utilities.db import PostStatus
        post = [{"user_id": 1, "id": 42, "post_type": "text", "buyer_stage": "awareness"}]
        with patch(f"{_RCP}.get_planned_posts_within_buffer", return_value=post), \
             patch(f"{_RCP}.count_ready_posts_within_buffer", return_value=0), \
             patch(f"{_RCP}.create_content", return_value=(_POST, None)), \
             patch(f"{_RCP}.get_recent_post_texts", return_value=[_POST]), \
             patch(f"{_RCP}._score_and_persist_dwell"), \
             patch(f"{_RCP}.update_db_post_content"), \
             patch(f"{_RCP}.update_db_post_status") as status, \
             patch(f"{_RCP}.update_db_post_gate_reason") as reason, \
             patch(f"{_RCP}.get_engagement_preferences", return_value={}), \
             patch(f"{_RCP}.get_user_preferences", return_value={"auto_schedule_posts": True}), \
             patch(f"{_RCP}._post_missing_required_asset", return_value=False), \
             patch(f"{_RCP}.get_post_authenticity_score", return_value=95):
            rcp.auto_create_weekly_content(user_id=1)
        status.assert_called_once_with(42, PostStatus.APPROVED)
        assert reason.call_args[0][1] == []

    def test_a_failed_reason_write_never_fails_the_post(self):
        from cqc_lem.app import run_content_plan as rcp
        post = [{"user_id": 1, "id": 42, "post_type": "text", "buyer_stage": "awareness"}]
        with patch(f"{_RCP}.get_planned_posts_within_buffer", return_value=post), \
             patch(f"{_RCP}.count_ready_posts_within_buffer", return_value=0), \
             patch(f"{_RCP}.create_content", return_value=(_POST, None)), \
             patch(f"{_RCP}._score_and_persist_dwell"), \
             patch(f"{_RCP}.update_db_post_content") as content, \
             patch(f"{_RCP}.update_db_post_status"), \
             patch(f"{_RCP}.update_db_post_gate_reason", side_effect=RuntimeError("db down")), \
             patch(f"{_RCP}.get_engagement_preferences", side_effect=RuntimeError("db down")), \
             patch(f"{_RCP}.get_user_preferences", return_value={"auto_schedule_posts": True}), \
             patch(f"{_RCP}._post_missing_required_asset", return_value=False), \
             patch(f"{_RCP}.get_post_authenticity_score", return_value=None):
            rcp.auto_create_weekly_content(user_id=1)
        content.assert_called_once_with(42, _POST)


class TestRescorePost:
    def _rescore(self, content=_POST, score=90, status="pending", auto_schedule=True,
                 recent=None, prefs=None):
        from cqc_lem.app import run_content_plan as rcp
        with patch(f"{_DB}.get_post_user_id", return_value=1), \
             patch(f"{_RCP}.get_post_content", return_value=content), \
             patch(f"{_DB}.get_post_type", return_value="text"), \
             patch(f"{_DB}.get_post_video_url", return_value=None), \
             patch(f"{_DB}.get_post_status", return_value=status), \
             patch(f"{_RCP}.get_engagement_preferences", return_value=prefs or {}), \
             patch(f"{_RCP}.load_profile_for_user", return_value=MagicMock()), \
             patch(f"{_RCP}.get_or_create_profile_synthesis", return_value="voice"), \
             patch(f"{_RCP}._score_and_persist_authenticity") as judge, \
             patch(f"{_RCP}.get_post_authenticity_score", return_value=score), \
             patch(f"{_RCP}.get_recent_post_texts", return_value=list(recent or [])) as history, \
             patch(f"{_RCP}._post_missing_required_asset", return_value=False), \
             patch(f"{_RCP}.update_db_post_gate_reason") as reason, \
             patch(f"{_RCP}.update_db_post_status") as new_status, \
             patch(f"{_RCP}.get_user_preferences",
                   return_value={"auto_schedule_posts": auto_schedule}):
            result = rcp.rescore_post(42)
        return result, new_status, reason, judge, history

    def test_a_fixed_post_is_promoted_without_a_regenerate(self):
        from cqc_lem.utilities.db import PostStatus
        result, status, reason, judge, _ = self._rescore(score=88)
        assert result["passed"] is True and result["status"] == PostStatus.APPROVED.value
        status.assert_called_once_with(42, PostStatus.APPROVED)
        reason.assert_called_once_with(42, [])
        # The edited text is re-judged — a stale score would survive a genuine rewrite.
        assert judge.called

    def test_a_still_failing_post_keeps_a_fresh_reason(self, monkeypatch):
        monkeypatch.setenv("AUTHENTICITY_SCORE_MIN", "60")
        result, status, reason, _, _ = self._rescore(score=20)
        assert result["passed"] is False
        assert result["findings"][0]["gate"] == "authenticity"
        assert reason.call_args[0][1] == result["findings"]
        status.assert_not_called()

    def test_history_excludes_the_post_itself(self):
        # The post is already saved, so it would otherwise match itself at 100%.
        _, _, _, _, history = self._rescore(recent=["an older post"])
        assert history.call_args.kwargs["exclude_post_id"] == 42

    def test_manual_approval_users_are_not_auto_approved(self):
        result, status, _, _, _ = self._rescore(score=95, auto_schedule=False)
        assert result["passed"] is True and result["status"] == "pending"
        status.assert_not_called()
        assert "auto-scheduling is off" in result["detail"]

    def test_an_approved_post_that_passes_stays_approved(self):
        result, status, _, _, _ = self._rescore(score=95, status="approved")
        assert result["passed"] is True and result["status"] == "approved"
        status.assert_not_called()

    def test_empty_post_is_reported_not_crashed(self):
        from cqc_lem.app import run_content_plan as rcp
        with patch(f"{_DB}.get_post_user_id", return_value=1), \
             patch(f"{_RCP}.get_post_content", return_value=None):
            result = rcp.rescore_post(42)
        assert result["passed"] is False and result["findings"] == []
