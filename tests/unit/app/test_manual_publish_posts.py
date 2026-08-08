"""Manual (native) publishing never reaches the automatic publish path (issue #1074).

An occasion post exists BECAUSE the REST API cannot create its entity, so publishing one through
that API is not a degraded outcome — it silently ships the wrong post. Two independent gates are
asserted here: the scheduler's query never returns such a row, and `post_to_linkedin` refuses one
that reaches it by any other route.
"""

from contextlib import ExitStack
from datetime import datetime, timezone
from unittest.mock import patch

import mysql.connector
import pytest

pytestmark = pytest.mark.unit


class TestSchedulerQueriesExcludeManualPublish:
    def test_ready_to_post_query_filters_manual_publish(self, mock_database_connection):
        from cqc_lem.utilities.db import get_ready_to_post_posts

        with patch("cqc_lem.platform.db.connection.get_db_connection") as mock_conn:
            mock_conn.return_value = mock_database_connection["connection"]
            mock_database_connection["cursor"].fetchall.return_value = []

            get_ready_to_post_posts()

        sql = " ".join(mock_database_connection["cursor"].execute.call_args[0][0].split())
        assert "manual_publish = 0" in sql

    def test_orphan_requeue_query_filters_manual_publish(self, mock_database_connection):
        from cqc_lem.utilities.db import get_orphaned_scheduled_posts

        with patch("cqc_lem.platform.db.connection.get_db_connection") as mock_conn:
            mock_conn.return_value = mock_database_connection["connection"]
            mock_database_connection["cursor"].fetchall.return_value = []

            get_orphaned_scheduled_posts()

        sql = " ".join(mock_database_connection["cursor"].execute.call_args[0][0].split())
        assert "manual_publish = 0" in sql


class TestGetPostManualPublish:
    @pytest.mark.parametrize("stored,expected", [(1, True), (0, False)])
    def test_reads_the_flag(self, mock_database_connection, stored, expected):
        from cqc_lem.utilities.db import get_post_manual_publish

        with patch("cqc_lem.platform.db.connection.get_db_connection") as mock_conn:
            mock_conn.return_value = mock_database_connection["connection"]
            mock_database_connection["cursor"].fetchone.return_value = (stored,)

            assert get_post_manual_publish(7) is expected

    def test_unknown_post_is_not_manual(self, mock_database_connection):
        from cqc_lem.utilities.db import get_post_manual_publish

        with patch("cqc_lem.platform.db.connection.get_db_connection") as mock_conn:
            mock_conn.return_value = mock_database_connection["connection"]
            mock_database_connection["cursor"].fetchone.return_value = None

            assert get_post_manual_publish(7) is False

    def test_db_error_falls_back_to_the_automatic_path(self, mock_database_connection):
        from cqc_lem.utilities.db import get_post_manual_publish

        with patch("cqc_lem.platform.db.connection.get_db_connection") as mock_conn:
            mock_conn.return_value = mock_database_connection["connection"]
            mock_database_connection["cursor"].execute.side_effect = mysql.connector.Error("boom")

            assert get_post_manual_publish(7) is False


class TestInsertOccasionPost:
    def test_returns_the_new_id_and_marks_the_row(self, mock_database_connection):
        from cqc_lem.utilities.db import insert_occasion_post

        cursor = mock_database_connection["cursor"]
        cursor.rowcount = 1
        cursor.lastrowid = 99
        with patch("cqc_lem.platform.db.connection.get_db_connection") as mock_conn:
            mock_conn.return_value = mock_database_connection["connection"]

            post_id = insert_occasion_post(3, datetime(2026, 8, 9, tzinfo=timezone.utc), "decision")

        assert post_id == 99
        sql = " ".join(cursor.execute.call_args[0][0].split())
        assert "manual_publish" in sql
        assert "planning" in cursor.execute.call_args[0][1]

    def test_failed_write_returns_none(self, mock_database_connection):
        from cqc_lem.utilities.db import insert_occasion_post

        with patch("cqc_lem.platform.db.connection.get_db_connection") as mock_conn:
            mock_conn.return_value = mock_database_connection["connection"]
            mock_database_connection["cursor"].execute.side_effect = mysql.connector.Error("boom")

            assert insert_occasion_post(3, datetime(2026, 8, 9, tzinfo=timezone.utc), "decision") is None


class TestDraftOccasionPost:
    """The drafting half: the human named a real event, so neither the shape nor the facts rotate."""

    _MOD = "cqc_lem.app.run_content_plan"
    _OCCASION = "Shipped the v2 scheduler after four weeks of nights."

    def _draft(self, occasion=None, archetype="project_launch", content="Drafted copy."):
        from cqc_lem.app.run_content_plan import draft_occasion_post

        with patch("cqc_lem.utilities.db.get_post_user_id", return_value=4), \
             patch("cqc_lem.utilities.db.get_post_buyer_stage", return_value=None), \
             patch(f"{self._MOD}.get_recent_post_shape_history", return_value=[]), \
             patch(f"{self._MOD}.get_shape_performance", return_value=None), \
             patch(f"{self._MOD}.load_profile_for_user", return_value=None), \
             patch(f"{self._MOD}.create_text_post", return_value=content) as create, \
             patch(f"{self._MOD}._finish_regenerated_post", side_effect=lambda *a, **k: a[2]) as finish, \
             patch(f"{self._MOD}.update_db_post_status") as status:
            result = draft_occasion_post(
                12, archetype, self._OCCASION if occasion is None else occasion)
        return result, create, finish, status

    def test_pins_the_archetype_and_anchors_on_what_the_author_typed(self):
        result, create, finish, _ = self._draft()

        assert result == "Drafted copy."
        blueprint = create.call_args.kwargs["blueprint"]
        assert blueprint["format"] == "project_launch"
        # The occasion is the ONLY fact the writer may state about the event.
        assert self._OCCASION in create.call_args.kwargs["story_directive"]
        assert any(self._OCCASION in str(a) for a in blueprint["fact_anchors"])
        finish.assert_called_once()

    def test_stage_comes_from_the_archetype_when_the_row_has_none(self):
        _, create, _, _ = self._draft(archetype="educational_milestone")
        assert create.call_args[0][1] == "awareness"

    @pytest.mark.parametrize("occasion,archetype", [
        ("", "project_launch"),
        ("   ", "project_launch"),
        ("A real launch happened here.", "case_snapshot"),
        ("A real launch happened here.", ""),
    ])
    def test_refuses_an_unusable_request_without_generating(self, occasion, archetype):
        result, create, _, _ = self._draft(occasion=occasion, archetype=archetype)
        assert result is None
        create.assert_not_called()

    def test_empty_generation_flags_the_post_for_a_human(self):
        from cqc_lem.utilities.db import PostStatus

        result, _, finish, status = self._draft(content=None)
        assert result is None
        finish.assert_not_called()
        status.assert_called_once_with(12, PostStatus.ERROR)


class TestPostToLinkedInRefusesManualPublish:
    def _run(self, manual: bool):
        from cqc_lem.app.run_automation import post_to_linkedin
        from cqc_lem.utilities.db import PostType

        targets = {
            "get_post_status": {"return_value": "approved"},
            "get_post_manual_publish": {"return_value": manual},
            "get_user_password_pair_by_id": {"return_value": ("u@example.com", "pw")},
            "get_post_content": {"return_value": "A draft with nothing special in it."},
            "get_engagement_preferences": {"return_value": {}},
            "get_post_type": {"return_value": PostType.TEXT},
            "share_on_linkedin": {"return_value": "urn:li:ugcPost:1"},
            "insert_new_log": {},
            "update_db_post_status": {},
            "update_db_post_content": {},
            "update_db_post_first_comment_link": {},
            "sweep_reply_comments": {},
            "auto_seed_comment_on_post": {},
            "auto_second_wave_comment": {},
        }
        with ExitStack() as stack:
            mocks = {name: stack.enter_context(patch(f"cqc_lem.app.run_automation.{name}", **kw))
                     for name, kw in targets.items()}
            result = post_to_linkedin.run(1, 5)
        return result, mocks

    def test_manual_publish_post_is_skipped_before_anything_is_shared(self):
        result, mocks = self._run(manual=True)
        assert "natively" in result
        mocks["share_on_linkedin"].assert_not_called()
        # Nothing downstream ran — not even the credential read the publish path starts with.
        mocks["get_user_password_pair_by_id"].assert_not_called()

    def test_a_normal_post_still_publishes(self):
        result, mocks = self._run(manual=False)
        assert "natively" not in str(result)
        mocks["share_on_linkedin"].assert_called_once()
