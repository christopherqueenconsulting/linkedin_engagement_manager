"""Integration tests for content plan generation pipeline.
Requires MySQL + Redis service containers (run via docker-compose).
Uses real DB but mocks AI and Selenium.
"""
import pytest
from unittest.mock import patch, MagicMock
from datetime import datetime, timedelta

from cqc_lem.utilities.db import PostType, PostStatus


@pytest.mark.integration
class TestPlanContentForUser:
    def test_plan_content_fills_the_month_at_the_weekly_cadence(self, mock_database_connection):
        """The plan covers the rest of the month at 2-4 posts/week, never one a day (issue #621).

        June 2026 has 29 days left after the start date; the default 3/week calendar (Tue/Wed/Thu)
        turns that into ~12 slots instead of 29.
        """
        from cqc_lem.app.run_content_plan import plan_content_for_user
        # Direct module patch avoids pydantic v1 metaclass conflicts from freeze_time.
        fixed_now = datetime(2026, 6, 1, 0, 0, 0)
        captured = []

        def cap(user_id, scheduled_time, post_type, buyer_stage, content_mix=None):
            captured.append(scheduled_time)
            return True

        with patch('cqc_lem.app.run_content_plan.datetime') as mock_dt, \
             patch('cqc_lem.app.run_content_plan.get_last_planned_post_date_for_user', return_value=None), \
             patch('cqc_lem.app.run_content_plan.get_engagement_preferences',
                   return_value={"posts_per_week": 3}), \
             patch('cqc_lem.app.run_content_plan.insert_planned_post', side_effect=cap):
            mock_dt.now.return_value = fixed_now
            mock_dt.combine = datetime.combine
            plan_content_for_user(user_id=1)

        assert 8 <= len(captured) <= 16, f"Expected a weekly cadence, got {len(captured)} posts"
        dates = [st.date() for st in captured]
        assert len(dates) == len(set(dates)), "planned two posts on the same day"
        ordered = sorted(captured)
        for earlier, later in zip(ordered, ordered[1:]):
            assert later - earlier >= timedelta(hours=24), "planned two posts within 24 hours"

    def test_plan_content_balanced_post_types(self, mock_database_connection):
        """plan_content_for_user should use multiple post types."""
        from cqc_lem.app.run_content_plan import plan_content_for_user
        inserted_types = []
        def capture_insert(user_id, scheduled_time, post_type, buyer_stage, content_mix=None):
            inserted_types.append(post_type)
            return True
        # Freeze to June 1 so target_posts is large enough to span multiple types;
        # without this the test breaks near month-end when only ~1 post is planned.
        fixed_now = datetime(2026, 6, 1, 0, 0, 0)
        with patch('cqc_lem.app.run_content_plan.datetime') as mock_dt, \
             patch('cqc_lem.app.run_content_plan.get_last_planned_post_date_for_user', return_value=None), \
             patch('cqc_lem.app.run_content_plan.insert_planned_post', side_effect=capture_insert):
            mock_dt.now.return_value = fixed_now
            mock_dt.combine = datetime.combine
            plan_content_for_user(user_id=1)
            unique_types = set(inserted_types)
            assert len(unique_types) > 1, f"Expected multiple post types, got only: {unique_types}"

    def test_plan_content_mix_is_governed(self, mock_database_connection):
        """Every planned post is classified and promo stays at/below the 10% ceiling (issue #618)."""
        from cqc_lem.app.run_content_plan import plan_content_for_user
        from cqc_lem.utilities.ai.content_alignment import CONTENT_MIX_TARGET, PROMO_MAX_RATIO

        mixes = []
        def cap_mix(user_id, scheduled_time, post_type, buyer_stage, content_mix=None):
            mixes.append(content_mix)
            return True

        fixed_now = datetime(2026, 6, 1, 0, 0, 0)
        with patch('cqc_lem.app.run_content_plan.datetime') as mock_dt, \
             patch('cqc_lem.app.run_content_plan.get_last_planned_post_date_for_user', return_value=None), \
             patch('cqc_lem.app.run_content_plan.insert_planned_post', side_effect=cap_mix):
            mock_dt.now.return_value = fixed_now
            mock_dt.combine = datetime.combine
            plan_content_for_user(user_id=1)

        assert mixes, "no posts were planned"
        assert all(m in CONTENT_MIX_TARGET for m in mixes)
        assert mixes.count('promo') / len(mixes) <= PROMO_MAX_RATIO

    def test_scheduled_times_converted_from_user_local_to_utc(self, mock_database_connection):
        """Regression: best-times are the user's LOCAL audience times and must be stored
        as UTC, so posts fire at the intended local time (not hours off)."""
        import pytz
        from cqc_lem.app.run_content_plan import plan_content_for_user
        from cqc_lem.utilities.utils import get_best_posting_times

        captured = []
        def cap(user_id, scheduled_time, post_type, buyer_stage, content_mix=None):
            captured.append(scheduled_time)
            return True

        fixed_now = datetime(2026, 6, 1, 0, 0, 0)
        with patch('cqc_lem.app.run_content_plan.datetime') as mock_dt, \
             patch('cqc_lem.app.run_content_plan.get_last_planned_post_date_for_user', return_value=None), \
             patch('cqc_lem.app.run_content_plan.get_user_timezone', return_value='America/New_York'), \
             patch('cqc_lem.app.run_content_plan.insert_planned_post', side_effect=cap):
            mock_dt.now.return_value = fixed_now
            mock_dt.combine = datetime.combine
            plan_content_for_user(user_id=1)

        assert captured, "no posts were planned"
        tz = pytz.timezone('America/New_York')
        # Times carry 15-30 minutes of jitter (issue #621), so a stored instant lands NEAR its
        # local best-time rather than exactly on it — within an hour of one of them.
        best_minutes = {t.hour * 60 + t.minute for t in get_best_posting_times().values()}

        def near_a_best_time(moment) -> bool:
            minutes = moment.hour * 60 + moment.minute
            return any(abs(minutes - b) <= 60 for b in best_minutes)

        for st in captured:
            # Stored value is naive UTC; converting back to the user's tz must land on a best-time.
            local = pytz.utc.localize(st).astimezone(tz)
            assert near_a_best_time(local), (
                f"stored {st} (UTC) -> {local} ET, not near a configured local best-time"
            )
        # ET is offset from UTC, so the stored UTC hour must differ from the local hour for at
        # least one post — proving conversion actually happened (not stored as-is).
        assert any(not near_a_best_time(st) for st in captured), \
            "scheduled times were stored unconverted (still local-as-UTC)"


@pytest.mark.integration
class TestAutoGenerateContent:
    def test_auto_generate_calls_task_for_each_active_user(self, mock_database_connection):
        """auto_generate_content should dispatch plan_content_for_user for each active user."""
        from cqc_lem.app.run_content_plan import auto_generate_content
        with patch('cqc_lem.app.run_content_plan.get_active_user_ids', return_value=[1, 2]), \
             patch('cqc_lem.app.run_content_plan.plan_content_for_user') as mock_plan:
            mock_plan.apply_async = MagicMock()
            auto_generate_content()
            assert mock_plan.apply_async.call_count == 2


@pytest.mark.integration
class TestCreateTextPost:
    def test_create_text_post_calls_ai_helper(self, mock_database_connection, mock_openai_client):
        """create_text_post should call the AI function matching the selected post type."""
        from cqc_lem.app.run_content_plan import create_text_post
        mock_profile = MagicMock()
        with patch('cqc_lem.app.run_content_plan.get_thought_leadership_post_from_ai', return_value='AI content') as mock_ai, \
             patch('cqc_lem.app.run_content_plan.get_ai_linked_post_refinement', return_value='Refined content'):
            result = create_text_post(user_id=1, stage='awareness', post_type='thought_leadership', user_profile=mock_profile)
            assert mock_ai.called
            assert result == 'Refined content'
