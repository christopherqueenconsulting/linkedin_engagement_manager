"""Unit tests for the content-plan side of dwell-time optimization (issue #391 — C2): the
dwell-proxy score is computed for every generated post and persisted next to the authenticity
score, the deterministic wall-of-text repair runs on whatever draft ships, and neither may ever
block the pipeline."""

import pytest
from unittest.mock import MagicMock, patch

pytestmark = pytest.mark.unit

_RCP = "cqc_lem.app.run_content_plan"

_DISABLED_LM = {"enabled": False, "keyword": None, "message": None}

# Scannable draft: hook inside the fold, short paragraphs, a numbered block.
_SHAPED = ("Most teams measure LinkedIn the wrong way.\n\n"
           "Likes are the cheapest signal on the platform.\n\n"
           "Last quarter I rewrote 12 posts to open a loop instead of closing it, and average read "
           "time went from 14 seconds to 71.\n\n"
           "1. Open a gap in the first line.\n"
           "2. Keep paragraphs under three lines.\n\n"
           "What is the longest a post has ever held you?")

# One 400+ char block — the wall-of-text shape shape_for_dwell must reflow.
_WALL = ("Most teams measure LinkedIn the wrong way and it costs them reach. Likes are the cheapest "
         "signal on the platform because a reader can tap one in half a second. Hold time is the "
         "signal that actually moves distribution now, and the way you earn it is by opening a gap "
         "in the first line and refusing to close it until the reader has committed. I rewrote 12 "
         "posts that way last quarter and average read time went from 14 seconds to 71 seconds.")


class TestScoreAndPersistDwell:
    def test_persists_the_computed_score(self):
        from cqc_lem.app import run_content_plan as rcp
        from cqc_lem.utilities.ai.content_framework import dwell_score
        with patch(f"{_RCP}.update_db_post_dwell_score") as upd:
            returned = rcp._score_and_persist_dwell(1, 42, _SHAPED)
        expected = dwell_score(_SHAPED)
        assert returned == expected
        upd.assert_called_once_with(42, expected)
        assert 0 <= expected <= 100

    def test_weak_post_logs_the_specific_reasons(self):
        from cqc_lem.app import run_content_plan as rcp
        log = MagicMock()
        with patch(f"{_RCP}.update_db_post_dwell_score"), patch(f"{_RCP}.log_warning", log):
            score = rcp._score_and_persist_dwell(1, 42, _WALL)
        assert score < rcp.dwell_score_min()
        assert log.called
        assert "wall-of-text" in log.call_args[0][0]

    def test_healthy_post_does_not_warn(self):
        from cqc_lem.app import run_content_plan as rcp
        log = MagicMock()
        with patch(f"{_RCP}.update_db_post_dwell_score"), patch(f"{_RCP}.log_warning", log):
            rcp._score_and_persist_dwell(1, 42, _SHAPED)
        log.assert_not_called()

    @pytest.mark.parametrize("content,post_id", [(None, 42), ("", 42), (_SHAPED, None)])
    def test_nothing_to_score_is_a_no_op(self, content, post_id):
        from cqc_lem.app import run_content_plan as rcp
        with patch(f"{_RCP}.update_db_post_dwell_score") as upd:
            assert rcp._score_and_persist_dwell(1, post_id, content) is None
        upd.assert_not_called()

    def test_db_failure_never_blocks_the_pipeline(self):
        from cqc_lem.app import run_content_plan as rcp
        with patch(f"{_RCP}.update_db_post_dwell_score", side_effect=RuntimeError("db down")):
            assert rcp._score_and_persist_dwell(1, 42, _SHAPED) is None


class TestWeeklyContentPersistsDwell:
    """Every post type flows through the weekly creator, so that is where the score is recorded."""

    def _post(self, post_type="text"):
        return [{"user_id": 1, "id": 42, "post_type": post_type, "buyer_stage": "awareness"}]

    @pytest.mark.parametrize("post_type", ["text", "carousel"])
    def test_score_stored_for_each_post_type(self, post_type):
        from cqc_lem.app import run_content_plan as rcp
        from cqc_lem.utilities.ai.content_framework import dwell_score
        with patch(f"{_RCP}.get_planned_posts_for_current_week", return_value=self._post(post_type)), \
             patch(f"{_RCP}.get_planned_posts_for_next_week", return_value=self._post(post_type)), \
             patch(f"{_RCP}.create_content", return_value=(_SHAPED, None)), \
             patch(f"{_RCP}.update_db_post_dwell_score") as upd_dwell, \
             patch(f"{_RCP}.update_db_post_content"), \
             patch(f"{_RCP}.update_db_post_status"), \
             patch(f"{_RCP}.get_user_preferences", return_value={"auto_schedule_posts": True}), \
             patch(f"{_RCP}._post_missing_required_asset", return_value=False), \
             patch(f"{_RCP}.get_post_authenticity_score", return_value=None):
            rcp.auto_create_weekly_content(user_id=1)
        upd_dwell.assert_called_once_with(42, dwell_score(_SHAPED))

    def test_scoring_failure_does_not_stop_the_post_being_saved(self):
        from cqc_lem.app import run_content_plan as rcp
        with patch(f"{_RCP}.get_planned_posts_for_current_week", return_value=self._post()), \
             patch(f"{_RCP}.get_planned_posts_for_next_week", return_value=self._post()), \
             patch(f"{_RCP}.create_content", return_value=(_SHAPED, None)), \
             patch(f"{_RCP}.update_db_post_dwell_score", side_effect=RuntimeError("db down")), \
             patch(f"{_RCP}.update_db_post_content") as upd_content, \
             patch(f"{_RCP}.update_db_post_status"), \
             patch(f"{_RCP}.get_user_preferences", return_value={"auto_schedule_posts": True}), \
             patch(f"{_RCP}._post_missing_required_asset", return_value=False), \
             patch(f"{_RCP}.get_post_authenticity_score", return_value=None):
            rcp.auto_create_weekly_content(user_id=1)
        upd_content.assert_called_once_with(42, _SHAPED)


class TestCreateTextPostShapesForDwell:
    def _run(self, generated):
        from cqc_lem.app import run_content_plan as rcp
        gen = MagicMock(return_value=generated)
        patches = [
            patch(f"{_RCP}.get_engagement_preferences", return_value={}),
            patch(f"{_RCP}.get_or_create_profile_synthesis", return_value="voice"),
            patch(f"{_RCP}.get_lead_magnet_settings", return_value=_DISABLED_LM),
            patch(f"{_RCP}.get_recent_post_texts", return_value=[]),
            patch(f"{_RCP}.get_recent_post_shape_history", return_value=[]),
            patch(f"{_RCP}.get_shape_performance", return_value=None),
            patch(f"{_RCP}.update_db_post_shape"),
            patch(f"{_RCP}.get_thought_leadership_post_from_ai", gen),
            patch(f"{_RCP}.get_ai_linked_post_refinement", side_effect=lambda c, **kw: c),
            patch(f"{_RCP}.optimize_post_hook", side_effect=lambda c, **kw: c),
            patch(f"{_RCP}.sanitize_for_linkedin", side_effect=lambda c, **kw: c),
            patch(f"{_RCP}.strip_engagement_bait", side_effect=lambda c, **kw: c),
            patch(f"{_RCP}._score_and_persist_authenticity"),
        ]
        for p in patches:
            p.start()
        try:
            return rcp.create_text_post(1, "awareness", post_type="thought_leadership",
                                        user_profile=MagicMock(), post_id=42)
        finally:
            for p in patches:
                p.stop()

    def test_wall_of_text_is_reflowed_before_it_ships(self):
        from cqc_lem.utilities.ai.content_framework import dwell_metrics
        out = self._run(_WALL)
        assert dwell_metrics(_WALL)["wall_of_text"] is True
        assert dwell_metrics(out)["wall_of_text"] is False
        # Repair reflows only — the lived-detail proof sentence must survive intact.
        assert "average read time went from 14 seconds to 71 seconds" in out

    def test_already_scannable_draft_is_untouched(self):
        assert self._run(_SHAPED) == _SHAPED


class TestCarouselSaveWorthyFraming:
    def test_carousel_prompt_carries_the_save_worthy_directive(self):
        from cqc_lem.utilities.ai import ai_helper
        resp = MagicMock()
        resp.choices = [MagicMock(message=MagicMock(
            content='{"post_text": "Hook line.\\n\\nBody line.", "carousel": {}}'))]
        with patch.object(ai_helper, "_call_llm", return_value=resp) as call, \
             patch.object(ai_helper, "get_user_password_pair_by_id", create=True,
                          side_effect=RuntimeError("no selenium")), \
             patch("cqc_lem.utilities.linkedin.helper.load_profile_for_user", return_value=None):
            ai_helper.generate_carousel_content(1, "awareness")
        prompt = call.call_args.kwargs["messages"][1]["content"][0]["text"]
        assert "DESIGN THIS CAROUSEL TO BE SAVED" in prompt
        assert "checklist" in prompt.lower()
        assert "save this for the next time you" in prompt.lower()
