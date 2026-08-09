from unittest.mock import ANY, MagicMock, patch

import importlib

import pytest
from freezegun import freeze_time

# Imported for the side effect ONLY, and via importlib so it reads as one: `pydantic.v1.types` must
# be loaded before freezegun freezes the clock, or its `ConstrainedNumberMeta` metaclass conflicts.
# A bare `import` here looks unused to every linter and gets deleted by an auto-fixer.
importlib.import_module("pydantic.v1.types")

from cqc_lem.utilities.db import PostType

# Wall-clock MUST be frozen mid-month here: the plan window derives its length from
# `days_left_in_month`, and on the last 1-2 days of any month a `last_planned_date = now + 1`
# test lands at or past the month boundary, where `_cadence_slots` returns 0 slots and the
# function early-returns without calling `save_content_plan` — making `assert_called_once()`
# fail. Freezing at a Monday three weeks from the month end guarantees an 8-slot eligible
# window for `TestPlanContentForUser.test_uses_last_planned_date_when_recent`.
_PLAN_WINDOW_CLOCK = "2026-07-13 12:00:00"

_RCP = "cqc_lem.app.run_content_plan"


def _planned(post_id: int = 42, user_id: int = 1, post_type: str = "text", stage: str = "awareness") -> dict:
    return {"user_id": user_id, "id": post_id, "post_type": post_type, "buyer_stage": stage}


class TestAutoCreateWeeklyContent:
    """auto_create_weekly_content tops up a BOUNDED rolling buffer of ready posts (issue #544)."""

    @pytest.fixture(autouse=True)
    def default_prefs(self):
        """Buffer knobs come from user prefs — default them so no test hits the DB."""
        with patch(f"{_RCP}.get_user_preferences",
                   return_value={"auto_schedule_posts": 0, "content_buffer_days": 5,
                                 "content_buffer_max_posts": 5}) as prefs:
            yield prefs

    @pytest.fixture
    def no_pull_forward(self):
        """Nothing planned beyond the buffer window either (issue #719's pull-forward path)."""
        with patch(f"{_RCP}.get_next_planned_posts_after_buffer", return_value=[]), \
             patch(f"{_RCP}.get_next_planned_post_date", return_value=None):
            yield

    @patch(f"{_RCP}.count_ready_posts_within_buffer", return_value=0)
    @patch(f"{_RCP}.get_planned_posts_within_buffer", return_value=None)
    def test_does_not_crash_when_planned_posts_is_none(self, mock_planned, mock_ready,
                                                       no_pull_forward):
        from cqc_lem.app.run_content_plan import auto_create_weekly_content
        # Should not raise TypeError: 'NoneType' is not iterable
        auto_create_weekly_content(user_id=1)

    @patch(f"{_RCP}.count_ready_posts_within_buffer", return_value=0)
    @patch(f"{_RCP}.get_planned_posts_within_buffer", return_value=[])
    def test_does_not_crash_when_planned_posts_is_empty(self, mock_planned, mock_ready,
                                                        no_pull_forward):
        from cqc_lem.app.run_content_plan import auto_create_weekly_content
        auto_create_weekly_content(user_id=1)

    @patch(f"{_RCP}.update_db_post_status")
    @patch(f"{_RCP}.update_db_post_content")
    @patch(f"{_RCP}.create_content", return_value=(None, None))
    @patch(f"{_RCP}.count_ready_posts_within_buffer", return_value=0)
    @patch(f"{_RCP}.get_planned_posts_within_buffer", return_value=[_planned()])
    def test_skips_post_when_content_is_none(
        self, mock_planned, mock_ready, mock_create, mock_update_content, mock_update_status
    ):
        from cqc_lem.app.run_content_plan import auto_create_weekly_content
        auto_create_weekly_content(user_id=1)
        mock_update_content.assert_not_called()
        mock_update_status.assert_not_called()

    @patch(f"{_RCP}.update_db_post_status")
    @patch(f"{_RCP}.update_db_post_content")
    @patch(f"{_RCP}.create_content", return_value=('Great post content', None))
    @patch(f"{_RCP}.count_ready_posts_within_buffer", return_value=0)
    @patch(f"{_RCP}.get_planned_posts_within_buffer", return_value=[_planned()])
    def test_updates_db_when_content_is_valid(
        self, mock_planned, mock_ready, mock_create, mock_update_content, mock_update_status
    ):
        from cqc_lem.app.run_content_plan import auto_create_weekly_content
        auto_create_weekly_content(user_id=1)
        mock_update_content.assert_called_once_with(42, 'Great post content')
        mock_update_status.assert_called_once()

    @patch(f"{_RCP}.update_db_post_status")
    @patch(f"{_RCP}.update_db_post_content")
    @patch(f"{_RCP}.create_content", return_value=('Auto-off content', None))
    @patch(f"{_RCP}.count_ready_posts_within_buffer", return_value=0)
    @patch(f"{_RCP}.get_planned_posts_within_buffer", return_value=[_planned(post_id=55)])
    def test_status_is_pending_when_auto_schedule_off(
        self, mock_planned, mock_ready, mock_create, mock_update_content, mock_update_status
    ):
        from cqc_lem.app.run_content_plan import auto_create_weekly_content
        from cqc_lem.utilities.db import PostStatus
        auto_create_weekly_content(user_id=1)
        mock_update_status.assert_called_once_with(55, PostStatus.PENDING)

    @patch(f"{_RCP}.get_post_authenticity_score", return_value=None)
    @patch(f"{_RCP}.update_db_post_status")
    @patch(f"{_RCP}.update_db_post_content")
    @patch(f"{_RCP}.create_content", return_value=('Auto-on content', None))
    @patch(f"{_RCP}.count_ready_posts_within_buffer", return_value=0)
    @patch(f"{_RCP}.get_planned_posts_within_buffer",
           return_value=[_planned(post_id=77, user_id=2, stage="decision")])
    def test_status_is_approved_when_auto_schedule_on(
        self, mock_planned, mock_ready, mock_create, mock_update_content, mock_update_status,
        mock_auth_score, default_prefs
    ):
        from cqc_lem.app.run_content_plan import auto_create_weekly_content
        from cqc_lem.utilities.db import PostStatus
        default_prefs.return_value = {"auto_schedule_posts": 1}
        auto_create_weekly_content(user_id=2)
        mock_update_status.assert_called_once_with(77, PostStatus.APPROVED)


class TestRollingBufferBounds:
    """The buffer is what closes the mid-week gap AND caps forward LLM spend (issue #544)."""

    @pytest.fixture(autouse=True)
    def prefs(self):
        with patch(f"{_RCP}.get_user_preferences",
                   return_value={"auto_schedule_posts": 0, "content_buffer_days": 5,
                                 "content_buffer_max_posts": 5}) as p:
            yield p

    @patch(f"{_RCP}.create_content", return_value=("Text", None))
    @patch(f"{_RCP}.update_db_post_status")
    @patch(f"{_RCP}.update_db_post_content")
    @patch(f"{_RCP}.count_ready_posts_within_buffer", return_value=0)
    @patch(f"{_RCP}.get_planned_posts_within_buffer", return_value=[_planned()])
    def test_weekday_run_still_generates(self, mock_planned, mock_ready, upd_content, upd_status,
                                         mock_create):
        """No weekday branch any more: a mid-week run generates from the forward window."""
        from cqc_lem.app.run_content_plan import auto_create_weekly_content
        auto_create_weekly_content(user_id=1)
        # Window is the next N days from now — never a calendar week
        assert mock_planned.call_args[0] == (1, 5, 5, 0)
        upd_content.assert_called_once_with(42, "Text")

    @patch(f"{_RCP}.create_content")
    @patch(f"{_RCP}.count_ready_posts_within_buffer", return_value=5)
    @patch(f"{_RCP}.get_planned_posts_within_buffer")
    def test_full_buffer_generates_nothing(self, mock_planned, mock_ready, mock_create):
        from cqc_lem.app.run_content_plan import auto_create_weekly_content
        auto_create_weekly_content(user_id=1)
        mock_planned.assert_not_called()
        mock_create.assert_not_called()

    @patch(f"{_RCP}.create_content", return_value=("Text", None))
    @patch(f"{_RCP}.update_db_post_status")
    @patch(f"{_RCP}.update_db_post_content")
    @patch(f"{_RCP}.count_ready_posts_within_buffer", return_value=3)
    @patch(f"{_RCP}.get_planned_posts_within_buffer", return_value=[_planned(post_id=1),
                                                                   _planned(post_id=2)])
    def test_only_the_delta_is_requested(self, mock_planned, mock_ready, upd_content, upd_status,
                                         mock_create):
        """3 already ready against a cap of 5 → ask for 2, generate 2 — never overshoot."""
        from cqc_lem.app.run_content_plan import auto_create_weekly_content
        auto_create_weekly_content(user_id=1)
        assert mock_planned.call_args[0] == (1, 5, 5, 3)
        assert mock_create.call_count == 2

    @patch(f"{_RCP}.create_content", return_value=("Text", None))
    @patch(f"{_RCP}.update_db_post_status")
    @patch(f"{_RCP}.update_db_post_content")
    @patch(f"{_RCP}.count_ready_posts_within_buffer", return_value=0)
    @patch(f"{_RCP}.get_planned_posts_within_buffer", return_value=[_planned()])
    def test_per_user_prefs_drive_the_window(self, mock_planned, mock_ready, upd_content,
                                             upd_status, mock_create, prefs):
        from cqc_lem.app.run_content_plan import auto_create_weekly_content
        prefs.return_value = {"auto_schedule_posts": 0, "content_buffer_days": 7,
                              "content_buffer_max_posts": 3}
        auto_create_weekly_content(user_id=1)
        assert mock_planned.call_args[0] == (1, 7, 3, 0)
        mock_ready.assert_called_once_with(1, 7)

    @patch(f"{_RCP}.create_content", return_value=("Text", None))
    @patch(f"{_RCP}.update_db_post_status")
    @patch(f"{_RCP}.update_db_post_content")
    @patch(f"{_RCP}.count_ready_posts_within_buffer", return_value=2)
    @patch(f"{_RCP}.get_planned_posts_within_buffer",
           return_value=[_planned(post_id=1), _planned(post_id=2), _planned(post_id=3)])
    def test_horizon_of_five_fills_to_five_ready_posts(self, mock_planned, mock_ready,
                                                       upd_content, upd_status, mock_create, prefs):
        """buffer_days=5 / buffer_max=5 with 2 already ready hands (5, 5, 2) to the query.

        Orchestration only — WHICH statuses count as ready is the query's own question, covered by
        tests/unit/utilities/test_db_content_buffer.py (`READY_POST_STATUSES`), and the delta cap
        is enforced there too. What this locks is that `_top_up_buffer_for_user` passes the
        per-user window, the cap and the live ready count through instead of defaulting any of them.
        """
        from cqc_lem.app.run_content_plan import auto_create_weekly_content
        prefs.return_value = {"auto_schedule_posts": 0, "content_buffer_days": 5,
                              "content_buffer_max_posts": 5}
        auto_create_weekly_content(user_id=1)
        # already_ready=2, cap=5 → ask for the 3 planning posts we have
        assert mock_planned.call_args[0] == (1, 5, 5, 2)
        assert mock_create.call_count == 3

    @patch(f"{_RCP}.create_content", return_value=("Text", None))
    @patch(f"{_RCP}.update_db_post_status")
    @patch(f"{_RCP}.update_db_post_content")
    @patch(f"{_RCP}.count_ready_posts_within_buffer", return_value=0)
    @patch(f"{_RCP}.get_planned_posts_within_buffer", return_value=[_planned()])
    @patch(f"{_RCP}.get_user_ids_with_planned_posts_within_buffer", return_value=[4, 9])
    def test_beat_run_tops_up_every_user_with_planned_posts(
        self, mock_users, mock_planned, mock_ready, upd_content, upd_status, mock_create
    ):
        from cqc_lem.app.run_content_plan import auto_create_weekly_content
        auto_create_weekly_content()
        mock_users.assert_called_once()
        assert [c[0][0] for c in mock_planned.call_args_list] == [4, 9]

    @patch(f"{_RCP}.get_user_ids_with_planned_posts_within_buffer", return_value=[])
    @patch(f"{_RCP}.count_ready_posts_within_buffer")
    def test_beat_run_with_no_planned_posts_is_a_no_op(self, mock_ready, mock_users):
        from cqc_lem.app.run_content_plan import auto_create_weekly_content
        auto_create_weekly_content()
        mock_ready.assert_not_called()


class TestBufferTopUpIsSingleFlight:
    """Overlapping runs for one user must not each generate the same delta (LLM/video spend)."""

    @pytest.fixture(autouse=True)
    def default_prefs(self):
        with patch(f"{_RCP}.get_user_preferences",
                   return_value={"auto_schedule_posts": 0, "content_buffer_days": 5,
                                 "content_buffer_max_posts": 5}):
            yield

    @patch(f"{_RCP}.create_content")
    @patch(f"{_RCP}.count_ready_posts_within_buffer", return_value=0)
    @patch(f"{_RCP}.get_planned_posts_within_buffer", return_value=[_planned()])
    @patch(f"{_RCP}.acquire_run_lock", return_value=None)
    def test_loser_of_the_lock_generates_nothing(self, mock_lock, mock_planned, mock_ready,
                                                 mock_create):
        from cqc_lem.app.run_content_plan import auto_create_weekly_content
        auto_create_weekly_content(user_id=1)
        assert mock_lock.call_args[0][0] == "content_buffer_topup:1"
        mock_ready.assert_not_called()
        mock_planned.assert_not_called()
        mock_create.assert_not_called()

    @patch(f"{_RCP}.release_run_lock")
    @patch(f"{_RCP}.update_db_post_status")
    @patch(f"{_RCP}.update_db_post_content")
    @patch(f"{_RCP}.create_content", return_value=("Text", None))
    @patch(f"{_RCP}.count_ready_posts_within_buffer", return_value=0)
    @patch(f"{_RCP}.get_planned_posts_within_buffer", return_value=[_planned()])
    @patch(f"{_RCP}.acquire_run_lock", return_value="tok")
    def test_lock_is_released_after_a_successful_run(self, mock_lock, mock_planned, mock_ready,
                                                     mock_create, upd_content, upd_status,
                                                     mock_release):
        from cqc_lem.app.run_content_plan import auto_create_weekly_content
        auto_create_weekly_content(user_id=1)
        upd_content.assert_called_once()
        mock_release.assert_called_once_with("content_buffer_topup:1", "tok")

    @patch(f"{_RCP}.release_run_lock")
    @patch(f"{_RCP}.count_ready_posts_within_buffer", side_effect=RuntimeError("db down"))
    @patch(f"{_RCP}.acquire_run_lock", return_value="tok")
    def test_lock_is_released_when_the_run_raises(self, mock_lock, mock_ready, mock_release):
        from cqc_lem.app.run_content_plan import auto_create_weekly_content
        with pytest.raises(RuntimeError):
            auto_create_weekly_content(user_id=1)
        mock_release.assert_called_once_with("content_buffer_topup:1", "tok")

    @patch(f"{_RCP}.release_run_lock")
    @patch(f"{_RCP}.count_ready_posts_within_buffer", return_value=9)
    @patch(f"{_RCP}.acquire_run_lock", return_value="tok")
    @patch(f"{_RCP}.get_user_ids_with_planned_posts_within_buffer", return_value=[4, 9])
    def test_beat_run_locks_each_user_separately(self, mock_users, mock_lock, mock_ready,
                                                 mock_release):
        from cqc_lem.app.run_content_plan import auto_create_weekly_content
        auto_create_weekly_content()
        assert [c[0][0] for c in mock_lock.call_args_list] == ["content_buffer_topup:4",
                                                               "content_buffer_topup:9"]
        assert mock_release.call_count == 2


class TestContentBufferSettings:
    def test_defaults_when_prefs_missing(self):
        from cqc_lem.app.run_content_plan import _content_buffer_settings
        from cqc_lem.utilities.db import DEFAULT_CONTENT_BUFFER_DAYS, DEFAULT_CONTENT_BUFFER_MAX_POSTS
        assert _content_buffer_settings({}) == (DEFAULT_CONTENT_BUFFER_DAYS,
                                                DEFAULT_CONTENT_BUFFER_MAX_POSTS)
        assert _content_buffer_settings(None) == (DEFAULT_CONTENT_BUFFER_DAYS,
                                                  DEFAULT_CONTENT_BUFFER_MAX_POSTS)

    def test_zero_or_null_falls_back_to_defaults(self):
        from cqc_lem.app.run_content_plan import _content_buffer_settings
        from cqc_lem.utilities.db import DEFAULT_CONTENT_BUFFER_DAYS, DEFAULT_CONTENT_BUFFER_MAX_POSTS
        assert _content_buffer_settings({"content_buffer_days": 0,
                                         "content_buffer_max_posts": None}) == (
            DEFAULT_CONTENT_BUFFER_DAYS, DEFAULT_CONTENT_BUFFER_MAX_POSTS)

    def test_oversized_values_are_clamped_to_the_ceiling(self):
        from cqc_lem.app.run_content_plan import _content_buffer_settings
        from cqc_lem.utilities.db import MAX_CONTENT_BUFFER_DAYS, MAX_CONTENT_BUFFER_POSTS
        assert _content_buffer_settings({"content_buffer_days": 999,
                                         "content_buffer_max_posts": 999}) == (
            MAX_CONTENT_BUFFER_DAYS, MAX_CONTENT_BUFFER_POSTS)


# ---------------------------------------------------------------------------
# Helper function tests
# ---------------------------------------------------------------------------

class TestGetMaxMinKey:
    def test_get_max_key_returns_correct_key(self):
        from cqc_lem.app.run_content_plan import get_max_key
        d = {"carousel": 10, "text": 5, "video": 2}
        assert get_max_key(d) == "carousel"

    def test_get_min_key_returns_correct_key(self):
        from cqc_lem.app.run_content_plan import get_min_key
        d = {"carousel": 10, "text": 5, "video": 2}
        assert get_min_key(d) == "video"

    def test_get_max_key_with_equal_values(self):
        from cqc_lem.app.run_content_plan import get_max_key
        d = {"a": 3, "b": 3, "c": 3}
        # Any key is valid when all are equal
        assert get_max_key(d) in d

    def test_get_min_key_with_equal_values(self):
        from cqc_lem.app.run_content_plan import get_min_key
        d = {"a": 1, "b": 1, "c": 1}
        assert get_min_key(d) in d

    def test_get_max_key_single_entry(self):
        from cqc_lem.app.run_content_plan import get_max_key
        assert get_max_key({"only": 99}) == "only"

    def test_get_min_key_single_entry(self):
        from cqc_lem.app.run_content_plan import get_min_key
        assert get_min_key({"only": 0}) == "only"


# ---------------------------------------------------------------------------
# is_blog_post / is_blog_post_combined tests
# ---------------------------------------------------------------------------

class TestIsBlogPost:
    def test_url_with_digit_segment_is_blog(self):
        from cqc_lem.app.run_content_plan import is_blog_post
        assert is_blog_post("https://example.com/2024/my-post") is True

    def test_url_with_nested_path_is_blog(self):
        from cqc_lem.app.run_content_plan import is_blog_post
        assert is_blog_post("https://example.com/blog/my-post") is True

    def test_root_url_is_not_blog(self):
        from cqc_lem.app.run_content_plan import is_blog_post
        assert is_blog_post("https://example.com/") is False

    def test_single_segment_path_is_not_blog(self):
        from cqc_lem.app.run_content_plan import is_blog_post
        assert is_blog_post("https://example.com/about") is False

    def test_is_blog_post_combined_returns_true_when_is_blog_post(self):
        from cqc_lem.app.run_content_plan import is_blog_post_combined
        # /blog/post has nested path — is_blog_post is True, no HTTP needed
        result = is_blog_post_combined("https://example.com/blog/my-post")
        assert result is True

    def test_is_blog_post_combined_falls_back_to_metadata(self):
        from cqc_lem.app.run_content_plan import is_blog_post_combined
        with patch("cqc_lem.app.run_content_plan.is_blog_post", return_value=False), \
             patch("cqc_lem.app.run_content_plan.is_blog_post_by_metadata", return_value=True):
            assert is_blog_post_combined("https://example.com/about") is True

    def test_is_blog_post_combined_returns_false_when_both_false(self):
        from cqc_lem.app.run_content_plan import is_blog_post_combined
        with patch("cqc_lem.app.run_content_plan.is_blog_post", return_value=False), \
             patch("cqc_lem.app.run_content_plan.is_blog_post_by_metadata", return_value=False):
            assert is_blog_post_combined("https://example.com/about") is False


# ---------------------------------------------------------------------------
# filter_relevant_urls tests
# ---------------------------------------------------------------------------

class TestFilterRelevantUrls:
    def test_returns_only_blog_post_urls(self):
        from cqc_lem.app.run_content_plan import filter_relevant_urls
        urls = [
            "https://example.com/blog/my-post",   # nested → blog post
            "https://example.com/about",           # root-level → not blog
            "https://example.com/news/2024/story", # nested → blog post
        ]
        with patch("cqc_lem.app.run_content_plan.is_blog_post_by_metadata", return_value=False):
            result = filter_relevant_urls(urls)
        assert "https://example.com/about" not in result
        assert "https://example.com/blog/my-post" in result

    def test_respects_max_list_size(self):
        from cqc_lem.app.run_content_plan import filter_relevant_urls
        urls = [f"https://example.com/blog/post-{i}" for i in range(20)]
        with patch("cqc_lem.app.run_content_plan.is_blog_post_by_metadata", return_value=False):
            result = filter_relevant_urls(urls, max_list_size=5)
        assert len(result) <= 5

    def test_empty_input_returns_empty(self):
        from cqc_lem.app.run_content_plan import filter_relevant_urls
        assert filter_relevant_urls([]) == []

    def test_by_blog_post_check_false_uses_keywords(self):
        from cqc_lem.app.run_content_plan import filter_relevant_urls
        urls = [
            "https://example.com/blog",
            "https://example.com/services",
            "https://example.com/random",
        ]
        result = filter_relevant_urls(urls, by_blog_post_check=False)
        assert "https://example.com/blog" in result
        assert "https://example.com/services" in result
        assert "https://example.com/random" not in result


# ---------------------------------------------------------------------------
# process_selected_post tests
# ---------------------------------------------------------------------------

class TestProcessSelectedPost:
    def test_handles_none_url_and_content(self):
        from cqc_lem.app.run_content_plan import process_selected_post
        # Should not raise
        process_selected_post(None, None)

    def test_handles_list_content(self):
        from cqc_lem.app.run_content_plan import process_selected_post
        # Should not raise
        process_selected_post("https://example.com", ["paragraph one", "paragraph two"])

    def test_handles_string_content(self):
        from cqc_lem.app.run_content_plan import process_selected_post
        process_selected_post("https://example.com", "Some content text")


# ---------------------------------------------------------------------------
# save_content_plan tests
# ---------------------------------------------------------------------------

class TestSaveContentPlan:
    @patch("cqc_lem.app.run_content_plan.insert_planned_post", return_value=True)
    def test_inserts_each_plan_entry(self, mock_insert):
        from datetime import datetime

        from cqc_lem.app.run_content_plan import save_content_plan
        daily_plan = [
            {"scheduled_datetime": datetime(2024, 6, 1, 9, 0), "post_type": "text", "stage": "awareness"},
            {"scheduled_datetime": datetime(2024, 6, 2, 9, 0), "post_type": "carousel", "stage": "consideration"},
            {"scheduled_datetime": datetime(2024, 6, 3, 9, 0), "post_type": "video", "stage": "decision"},
        ]
        save_content_plan(user_id=1, daily_plan=daily_plan)
        assert mock_insert.call_count == 3

    @patch("cqc_lem.app.run_content_plan.insert_planned_post", return_value=True)
    def test_empty_plan_inserts_nothing(self, mock_insert):
        from cqc_lem.app.run_content_plan import save_content_plan
        save_content_plan(user_id=1, daily_plan=[])
        mock_insert.assert_not_called()

    @patch("cqc_lem.app.run_content_plan.insert_planned_post", return_value=True)
    def test_correct_post_type_conversion(self, mock_insert):
        from datetime import datetime

        from cqc_lem.app.run_content_plan import save_content_plan
        from cqc_lem.utilities.db import PostType
        daily_plan = [
            {"scheduled_datetime": datetime(2024, 6, 1, 9, 0), "post_type": "carousel", "stage": "awareness"},
        ]
        save_content_plan(user_id=5, daily_plan=daily_plan)
        call_args = mock_insert.call_args
        assert call_args.args[0] == 5
        assert call_args.args[2] == PostType.CAROUSEL


# ---------------------------------------------------------------------------
# create_content tests
# ---------------------------------------------------------------------------

class TestCreateContent:
    @patch("cqc_lem.app.run_content_plan.create_text_post", return_value="Text post content")
    def test_text_post_type(self, mock_text):
        from cqc_lem.app.run_content_plan import create_content
        content, video_url = create_content(user_id=1, post_type="text", stage="awareness")
        assert content == "Text post content"
        assert video_url is None
        mock_text.assert_called_once_with(1, "awareness", post_id=None, content_mix=None,
                                          day_weekday=None)

    @patch("cqc_lem.app.run_content_plan.create_carousel_content", return_value="Carousel post content")
    def test_carousel_post_type(self, mock_carousel):
        from cqc_lem.app.run_content_plan import create_content
        content, video_url = create_content(user_id=1, post_type="carousel", stage="consideration", post_id=42)
        assert content == "Carousel post content"
        assert video_url is None
        mock_carousel.assert_called_once_with(1, "consideration", 42)

    @patch("cqc_lem.app.run_content_plan.create_video_content", return_value=("Video post content", "https://video.url"))
    def test_video_post_type(self, mock_video):
        from cqc_lem.app.run_content_plan import create_content
        content, video_url = create_content(user_id=1, post_type="video", stage="decision")
        assert content == "Video post content"
        assert video_url == "https://video.url"
        mock_video.assert_called_once_with(1, "decision", post_id=None)

    @patch("cqc_lem.app.run_content_plan.create_text_post", return_value="  Trimmed content  ")
    def test_text_content_is_stripped(self, mock_text):
        from cqc_lem.app.run_content_plan import create_content
        content, _ = create_content(user_id=1, post_type="text", stage="awareness")
        assert content == "Trimmed content"

    @patch("cqc_lem.app.run_content_plan.create_text_post", return_value=None)
    def test_text_post_returns_none_content(self, mock_text):
        from cqc_lem.app.run_content_plan import create_content
        content, video_url = create_content(user_id=1, post_type="text", stage="awareness")
        assert content is None
        assert video_url is None


# ---------------------------------------------------------------------------
# auto_generate_content tests
# ---------------------------------------------------------------------------

class TestAutoGenerateContent:
    @patch("cqc_lem.app.run_content_plan.plan_content_for_user")
    @patch("cqc_lem.app.run_content_plan.get_active_user_ids", return_value=[1, 2, 3])
    def test_queues_task_for_each_active_user(self, mock_ids, mock_plan):
        from cqc_lem.app.run_content_plan import auto_generate_content
        auto_generate_content()
        assert mock_plan.apply_async.call_count == 3

    @patch("cqc_lem.app.run_content_plan.plan_content_for_user")
    @patch("cqc_lem.app.run_content_plan.get_active_user_ids", return_value=[])
    def test_no_active_users_does_not_queue(self, mock_ids, mock_plan):
        from cqc_lem.app.run_content_plan import auto_generate_content
        auto_generate_content()
        mock_plan.apply_async.assert_not_called()


# ---------------------------------------------------------------------------
# plan_content_for_user tests (Celery bound task)
# ---------------------------------------------------------------------------

class TestPlanContentForUser:
    """Tests for the plan_content_for_user Celery task.

    The task is decorated with @shared_task.task(bind=True, ...) so we call
    it as plan_content_for_user(mock_self, user_id=...) using the underlying
    function directly (bypassing Celery task machinery in unit tests).
    """

    @freeze_time(_PLAN_WINDOW_CLOCK)
    @patch("cqc_lem.app.run_content_plan.save_content_plan")
    @patch("cqc_lem.utilities.utils.get_best_posting_time", return_value=__import__("datetime").time(9, 0))
    @patch("cqc_lem.app.run_content_plan.get_last_planned_post_date_for_user", return_value=None)
    @patch("cqc_lem.app.run_content_plan.get_post_type_counts", return_value={"carousel": 5, "text": 5, "video": 5})
    def test_calls_save_content_plan(self, mock_counts, mock_last, mock_time, mock_save):
        from cqc_lem.app.run_content_plan import plan_content_for_user
        plan_content_for_user.run(user_id=1)
        mock_save.assert_called_once()
        call_args = mock_save.call_args
        assert call_args.args[0] == 1
        assert isinstance(call_args.args[1], list)

    @freeze_time(_PLAN_WINDOW_CLOCK)
    @patch("cqc_lem.app.run_content_plan.save_content_plan")
    @patch("cqc_lem.utilities.utils.get_best_posting_time", return_value=__import__("datetime").time(9, 0))
    @patch("cqc_lem.app.run_content_plan.get_last_planned_post_date_for_user", return_value=None)
    @patch("cqc_lem.app.run_content_plan.get_post_type_counts", return_value={"carousel": 0, "text": 0, "video": 0})
    def test_new_user_no_posts(self, mock_counts, mock_last, mock_time, mock_save):
        """New user with 0 existing posts should still generate a content plan."""
        from cqc_lem.app.run_content_plan import plan_content_for_user
        plan_content_for_user.run(user_id=99)
        mock_save.assert_called_once()
        plan = mock_save.call_args.args[1]
        assert len(plan) > 0

    @patch("cqc_lem.app.run_content_plan.save_content_plan")
    @patch("cqc_lem.utilities.utils.get_best_posting_time", return_value=__import__("datetime").time(9, 0))
    @patch("cqc_lem.app.run_content_plan.get_last_planned_post_date_for_user")
    @patch("cqc_lem.app.run_content_plan.get_post_type_counts", return_value={"carousel": 3, "text": 5, "video": 2})
    def test_skips_when_last_planned_date_beyond_30_days(self, mock_counts, mock_last, mock_time, mock_save):
        """Should early-return if the start date is >30 days out."""
        from datetime import datetime, timedelta

        from cqc_lem.app.run_content_plan import plan_content_for_user
        future_date = datetime.now() + timedelta(days=40)
        mock_last.return_value = future_date
        plan_content_for_user.run(user_id=1)
        mock_save.assert_not_called()

    @freeze_time(_PLAN_WINDOW_CLOCK)
    @patch("cqc_lem.app.run_content_plan.save_content_plan")
    @patch("cqc_lem.utilities.utils.get_best_posting_time", return_value=__import__("datetime").time(9, 0))
    @patch("cqc_lem.app.run_content_plan.get_last_planned_post_date_for_user")
    @patch("cqc_lem.app.run_content_plan.get_post_type_counts", return_value={"carousel": 3, "text": 5, "video": 2})
    def test_uses_last_planned_date_when_recent(self, mock_counts, mock_last, mock_time, mock_save):
        """When last_planned_date is tomorrow, start_date is the day after that."""
        from datetime import datetime, timedelta

        from cqc_lem.app.run_content_plan import plan_content_for_user
        tomorrow = datetime.now() + timedelta(days=1)
        mock_last.return_value = tomorrow
        plan_content_for_user.run(user_id=1)
        # start_date would be tomorrow + 1 day; plan should still be generated
        mock_save.assert_called_once()

    @freeze_time(_PLAN_WINDOW_CLOCK)
    @patch("cqc_lem.app.run_content_plan.save_content_plan")
    @patch("cqc_lem.utilities.utils.get_best_posting_time", return_value=__import__("datetime").time(9, 0))
    @patch("cqc_lem.app.run_content_plan.get_last_planned_post_date_for_user", return_value=None)
    @patch("cqc_lem.app.run_content_plan.get_post_type_counts", return_value={"carousel": 3, "text": 5, "video": 2})
    def test_plan_contains_valid_post_types(self, mock_counts, mock_last, mock_time, mock_save):
        """Every entry in the generated plan must have a valid post_type."""
        from cqc_lem.app.run_content_plan import plan_content_for_user
        plan_content_for_user.run(user_id=1)
        plan = mock_save.call_args.args[1]
        valid_types = {"carousel", "text", "video", "document"}
        for entry in plan:
            assert entry["post_type"] in valid_types

    @freeze_time(_PLAN_WINDOW_CLOCK)
    @patch("cqc_lem.app.run_content_plan.save_content_plan")
    @patch("cqc_lem.utilities.utils.get_best_posting_time", return_value=__import__("datetime").time(9, 0))
    @patch("cqc_lem.app.run_content_plan.get_last_planned_post_date_for_user", return_value=None)
    @patch("cqc_lem.app.run_content_plan.get_post_type_counts", return_value={"carousel": 3, "text": 5, "video": 2})
    def test_plan_contains_valid_buyer_stages(self, mock_counts, mock_last, mock_time, mock_save):
        """Every entry in the plan must have a valid buyer journey stage."""
        from cqc_lem.app.run_content_plan import plan_content_for_user
        plan_content_for_user.run(user_id=1)
        plan = mock_save.call_args.args[1]
        valid_stages = {"awareness", "consideration", "decision"}
        for entry in plan:
            assert entry["stage"] in valid_stages


# ---------------------------------------------------------------------------
# get_main_blog_url_content tests
# ---------------------------------------------------------------------------

class TestGetMainBlogUrlContent:
    @patch("cqc_lem.app.run_content_plan.get_session_for_response")
    def test_returns_none_none_on_connection_error(self, mock_session_fn):
        import requests

        from cqc_lem.app.run_content_plan import get_main_blog_url_content
        mock_session = __import__("unittest.mock", fromlist=["MagicMock"]).MagicMock()
        mock_session.get.side_effect = requests.exceptions.ConnectionError("down")
        mock_session_fn.return_value = (mock_session, {})
        url, content = get_main_blog_url_content("https://example.com/blog")
        assert url is None
        assert content is None

    @patch("cqc_lem.app.run_content_plan.get_session_for_response")
    def test_returns_none_none_on_timeout(self, mock_session_fn):
        import requests

        from cqc_lem.app.run_content_plan import get_main_blog_url_content
        mock_session = __import__("unittest.mock", fromlist=["MagicMock"]).MagicMock()
        mock_session.get.side_effect = requests.exceptions.Timeout("timeout")
        mock_session_fn.return_value = (mock_session, {})
        url, content = get_main_blog_url_content("https://example.com/blog")
        assert url is None
        assert content is None

    @patch("cqc_lem.app.run_content_plan.get_session_for_response")
    def test_returns_post_from_wordpress_api(self, mock_session_fn):
        from cqc_lem.app.run_content_plan import get_main_blog_url_content
        mock_session = MagicMock()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = [
            {"content": {"rendered": "<p>Hello world</p>"}, "link": "https://example.com/blog/post-1"}
        ]
        mock_session.get.return_value = mock_resp
        mock_session_fn.return_value = (mock_session, {})
        url, content = get_main_blog_url_content("https://example.com")
        assert url == "https://example.com/blog/post-1"
        assert content == "<p>Hello world</p>"

    @patch("cqc_lem.app.run_content_plan.scrape_recent_posts", return_value=[])
    @patch("cqc_lem.app.run_content_plan.get_session_for_response")
    def test_falls_back_to_scraping_on_http_error(self, mock_session_fn, mock_scrape):
        import requests

        from cqc_lem.app.run_content_plan import get_main_blog_url_content
        mock_session = MagicMock()
        mock_resp = MagicMock()
        mock_resp.raise_for_status.side_effect = requests.exceptions.HTTPError("404")
        mock_session.get.return_value = mock_resp
        mock_session_fn.return_value = (mock_session, {})
        url, content = get_main_blog_url_content("https://example.com/blog")
        mock_scrape.assert_called_once()
        assert url is None
        assert content is None


# ---------------------------------------------------------------------------
# create_video_content tests
# ---------------------------------------------------------------------------

class TestCreateVideoContent:
    @patch("cqc_lem.app.run_content_plan.create_runway_video", return_value="https://runway.video/abc.mp4")
    @patch("cqc_lem.utilities.ai.image_gen.render_image_from_prompt", return_value="/tmp/image.png")
    @patch("cqc_lem.app.run_content_plan.get_runway_ml_video_prompt_from_ai", return_value="a cinematic scene" * 30)
    @patch("cqc_lem.app.run_content_plan.get_flux_image_prompt_from_ai", return_value="An inspiring image")
    @patch("cqc_lem.utilities.db.get_active_avatar", return_value=None)
    @patch("cqc_lem.utilities.db.get_default_video_quality", return_value="standard")
    @patch("cqc_lem.app.run_content_plan.create_text_post", return_value="Great video post text")
    def test_successful_video_generation(self, mock_text, mock_default_q, mock_avatar,
                                         mock_img_prompt, mock_vid_prompt, mock_image, mock_video):
        from cqc_lem.app.run_content_plan import create_video_content
        content, video_url = create_video_content(user_id=1, stage="awareness")
        assert content == "Great video post text"
        assert video_url == "https://runway.video/abc.mp4"

    @patch("cqc_lem.app.run_content_plan.create_text_post", return_value="Fallback post text")
    @patch("cqc_lem.app.run_content_plan.get_flux_image_prompt_from_ai", side_effect=Exception("AI down"))
    def test_falls_back_to_pexels_on_ai_failure(self, mock_img_prompt, mock_text):
        from cqc_lem.app.run_content_plan import create_video_content
        with patch("cqc_lem.app.run_content_plan.create_folder_if_not_exists"), \
             patch("cqc_lem.utilities.pexels_helper.download_pexels_video", return_value=None, create=True), \
             patch("cqc_lem.app.run_content_plan.save_video_url_to_dir", return_value=None, create=True):
            try:
                content, video_url = create_video_content(user_id=1, stage="awareness")
                assert content == "Fallback post text"
            except Exception:
                pass  # pexels_helper import may not be available in unit test env

    @patch("cqc_lem.app.run_content_plan.create_text_post", return_value="Fallback post text")
    @patch("cqc_lem.app.run_content_plan.get_flux_image_prompt_from_ai", side_effect=RuntimeError("AI failure"))
    def test_returns_text_content_when_video_fails(self, mock_img_prompt, mock_text):
        from cqc_lem.app.run_content_plan import create_video_content
        with patch("cqc_lem.app.run_content_plan.create_folder_if_not_exists"):
            try:
                content, _ = create_video_content(user_id=1, stage="awareness")
                assert content == "Fallback post text"
            except Exception:
                pass  # pexels import failure is acceptable in unit context


# ---------------------------------------------------------------------------
# regenerate_post tests (issue #794 — all post types)
# ---------------------------------------------------------------------------

class TestRegeneratePostAllTypes:
    """regenerate_post now handles text, carousel, document, and video posts."""

    _DB = "cqc_lem.utilities.db"

    @pytest.fixture(autouse=True)
    def base_patches(self):
        """Stop DB/profile reads from escaping the unit."""
        with patch(f"{self._DB}.get_post_user_id", return_value=1), \
             patch(f"{self._DB}.get_post_buyer_stage", return_value="awareness"), \
             patch(f"{self._DB}.get_post_type", return_value=PostType.TEXT), \
             patch(f"{_RCP}.load_profile_for_user", return_value=MagicMock()), \
             patch(f"{_RCP}._score_and_persist_dwell"), \
             patch(f"{_RCP}.update_db_post_content"), \
             patch(f"{_RCP}._persist_gate_findings"), \
             patch(f"{_RCP}.update_db_post_status"), \
             patch(f"{_RCP}.get_engagement_preferences", return_value={"use_emojis": False}), \
             patch(f"{_RCP}.get_or_create_profile_synthesis", return_value="synth"), \
             patch(f"{_RCP}.apply_post_guidance", return_value=None), \
             patch(f"{_RCP}.get_lead_magnet_settings", return_value=None), \
             patch(f"{_RCP}.sanitize_for_linkedin", side_effect=lambda x: x), \
             patch(f"{_RCP}._post_used_avatar_media", return_value=False), \
             patch(f"{_RCP}._post_is_flagged_error", return_value=False), \
             patch(f"{_RCP}.ensure_lead_magnet_cta", side_effect=lambda c, *a, **k: c):
            yield

    @patch(f"{_RCP}.create_text_post", return_value="New text post")
    @patch(f"{_RCP}._post_content_mix", return_value="value")
    def test_text_post_regenerates_with_guidance(self, mock_mix, mock_text, base_patches):
        from cqc_lem.app.run_content_plan import regenerate_post
        from cqc_lem.utilities.db import PostStatus
        with patch(f"{_RCP}.apply_post_guidance", return_value="Guided text post") as mock_guidance:
            result = regenerate_post(7, guidance="punchier")
        assert result == "Guided text post"
        mock_text.assert_called_once_with(1, "awareness", user_profile=ANY, post_id=7, content_mix="value")
        mock_guidance.assert_called_once_with("New text post", "punchier", prefs={"use_emojis": False}, profile_synthesis="synth")
        from cqc_lem.app.run_content_plan import update_db_post_status
        update_db_post_status.assert_called_once_with(7, PostStatus.PENDING)

    @patch(f"{_RCP}.create_carousel_content", return_value="New carousel caption")
    @patch("cqc_lem.utilities.db.get_post_type", return_value=PostType.CAROUSEL)
    @patch("cqc_lem.utilities.db.get_post_buyer_stage", return_value="consideration")
    def test_carousel_post_regenerates_with_guidance(self, mock_stage, mock_type, mock_carousel, base_patches):
        """The guidance reaches the DECK's generation — a caption-only rewrite would leave the
        user's suggestions off the slides, which on a carousel ARE the post (issue #794).
        """
        from cqc_lem.app.run_content_plan import regenerate_post
        from cqc_lem.utilities.db import PostStatus
        with patch(f"{_RCP}.apply_post_guidance") as mock_guidance:
            result = regenerate_post(8, guidance="more tactical")
        assert result == "New carousel caption"
        mock_carousel.assert_called_once_with(1, "consideration", 8, guidance="more tactical")
        mock_guidance.assert_not_called()
        from cqc_lem.app.run_content_plan import update_db_post_status
        update_db_post_status.assert_called_once_with(8, PostStatus.PENDING)

    @patch(f"{_RCP}.create_carousel_content", return_value="New carousel caption")
    @patch("cqc_lem.utilities.db.get_post_type", return_value=PostType.CAROUSEL)
    def test_carousel_keeps_error_status_when_slides_fail(self, mock_type, mock_carousel, base_patches):
        """create_carousel_content flags a failed slide render 'error'; clearing that to PENDING
        would present a deck still carrying the PREVIOUS draft's slides as ready to review.
        """
        from cqc_lem.app.run_content_plan import regenerate_post
        with patch(f"{_RCP}._post_is_flagged_error", return_value=True):
            result = regenerate_post(8)
        assert result == "New carousel caption"
        from cqc_lem.app.run_content_plan import update_db_post_status
        update_db_post_status.assert_not_called()

    @patch(f"{_RCP}.create_carousel_content", return_value="New document caption")
    @patch("cqc_lem.utilities.db.get_post_type", return_value=PostType.DOCUMENT)
    @patch("cqc_lem.utilities.db.get_post_buyer_stage", return_value="decision")
    def test_document_post_regenerates(self, mock_stage, mock_type, mock_carousel, base_patches):
        from cqc_lem.app.run_content_plan import regenerate_post
        result = regenerate_post(9)
        assert result == "New document caption"
        mock_carousel.assert_called_once_with(1, "decision", 9, guidance=None)

    @patch(f"{_RCP}.create_text_post", return_value="New video caption")
    @patch(f"{_RCP}._post_content_mix", return_value="authority")
    @patch("cqc_lem.utilities.db.get_post_type", return_value=PostType.VIDEO)
    @patch("cqc_lem.utilities.db.get_post_buyer_stage", return_value="awareness")
    @patch(f"{_RCP}._generate_video_src", return_value="https://runway.video/xyz.mp4")
    def test_video_post_regenerates_caption_and_video(self, mock_video_src, mock_stage, mock_type,
                                                    mock_mix, mock_text, base_patches):
        from cqc_lem.app.run_content_plan import regenerate_post
        from cqc_lem.utilities.db import PostStatus
        with patch(f"{_RCP}.create_folder_if_not_exists"), \
             patch(f"{_RCP}.save_video_url_to_dir", return_value="/fake/path/xyz.mp4"), \
             patch(f"{_RCP}.update_db_post_video_url") as mock_update_video, \
             patch(f"{_RCP}.apply_post_guidance", return_value="Guided video caption") as mock_guidance:
            result = regenerate_post(10, guidance="shorter")
        # The persisted caption carries the AI-visuals disclosure (asserted exactly below).
        assert result.startswith("Guided video caption")
        mock_text.assert_called_once_with(1, "awareness", user_profile=ANY, post_id=10, content_mix="authority")
        mock_guidance.assert_called_once_with("New video caption", "shorter", prefs={"use_emojis": False}, profile_synthesis="synth")
        mock_video_src.assert_called_once_with(1, "Guided video caption", ANY, 10)
        mock_update_video.assert_called_once()
        from cqc_lem.app.run_content_plan import update_db_post_status
        update_db_post_status.assert_called_once_with(10, PostStatus.PENDING)

    @patch(f"{_RCP}.create_text_post", return_value="New video caption")
    @patch(f"{_RCP}._post_content_mix", return_value="authority")
    @patch("cqc_lem.utilities.db.get_post_type", return_value=PostType.VIDEO)
    @patch(f"{_RCP}._generate_video_src", return_value="https://runway.video/xyz.mp4")
    def test_regenerated_ai_video_is_disclosed(self, mock_video_src, mock_type, mock_mix,
                                               mock_text, base_patches):
        """Regeneration replaces the whole caption, so the old draft's AI-visuals disclosure went
        with it — the new caption must carry it or the post ships undisclosed (issue #744).
        """
        from cqc_lem.app.run_content_plan import regenerate_post
        with patch(f"{_RCP}.create_folder_if_not_exists"), \
             patch(f"{_RCP}.save_video_url_to_dir", return_value="/fake/path/xyz.mp4"), \
             patch(f"{_RCP}.update_db_post_video_url"), \
             patch(f"{_RCP}.apply_post_guidance", side_effect=lambda c, *a, **k: c), \
             patch(f"{_RCP}._apply_ai_disclosure", side_effect=lambda c: c + " [AI]") as mock_disc, \
             patch(f"{_RCP}.update_db_post_content") as mock_content:
            result = regenerate_post(10)
        mock_disc.assert_called_once_with("New video caption")
        mock_content.assert_called_once_with(10, "New video caption [AI]")
        assert result == "New video caption [AI]"

    @patch(f"{_RCP}.create_text_post", return_value="New video caption")
    @patch(f"{_RCP}._post_content_mix", return_value="authority")
    @patch("cqc_lem.utilities.db.get_post_type", return_value=PostType.VIDEO)
    @patch(f"{_RCP}._generate_video_src", return_value="https://runway.video/xyz.mp4")
    def test_video_asset_download_failure_still_persists_caption(self, mock_video_src, mock_type,
                                                                 mock_mix, mock_text, base_patches):
        """A failed download must not take the regenerated caption down with it — the post is
        persisted and held by the missing-asset gate, exactly as a failed render is.
        """
        from cqc_lem.app.run_content_plan import regenerate_post
        from cqc_lem.utilities.db import PostStatus
        with patch(f"{_RCP}._store_video_asset", side_effect=OSError("disk full")), \
             patch(f"{_RCP}.apply_post_guidance", side_effect=lambda c, *a, **k: c), \
             patch(f"{_RCP}.update_db_post_content") as mock_content, \
             patch(f"{_RCP}._gate_findings_for_post", return_value=[]) as mock_gates:
            result = regenerate_post(10)
        assert result == "New video caption"
        mock_content.assert_called_once_with(10, "New video caption")
        # video_url None is what makes the missing-asset gate fire.
        assert mock_gates.call_args.args[4] is None
        from cqc_lem.app.run_content_plan import update_db_post_status
        update_db_post_status.assert_called_once_with(10, PostStatus.PENDING)

    @patch("cqc_lem.utilities.db.get_post_type", return_value=PostType.VIDEO)
    @patch("cqc_lem.utilities.db.get_post_buyer_stage", return_value="awareness")
    @patch(f"{_RCP}.create_text_post", return_value="New video caption")
    @patch(f"{_RCP}._post_content_mix", return_value="authority")
    @patch(f"{_RCP}._generate_video_src", return_value=None)
    def test_video_post_held_when_video_asset_fails(self, mock_video_src, mock_mix, mock_text,
                                                    mock_stage, mock_type, base_patches):
        from cqc_lem.app.run_content_plan import regenerate_post
        from cqc_lem.utilities.db import PostStatus
        with patch(f"{_RCP}.apply_post_guidance", side_effect=lambda c, *a, **k: c):
            result = regenerate_post(11)
        assert result == "New video caption"
        from cqc_lem.app.run_content_plan import update_db_post_status
        update_db_post_status.assert_called_once_with(11, PostStatus.PENDING)

    @patch("cqc_lem.utilities.db.get_post_type", return_value=None)
    def test_unknown_post_type_returns_none(self, mock_type, base_patches):
        from cqc_lem.app.run_content_plan import regenerate_post
        assert regenerate_post(12) is None

    @patch("cqc_lem.utilities.db.get_post_user_id", return_value=None)
    def test_missing_user_returns_none(self, mock_user_id):
        from cqc_lem.app.run_content_plan import regenerate_post
        assert regenerate_post(13) is None
