"""Unit tests for database utility functions."""

from datetime import datetime, timezone
from unittest.mock import MagicMock, call, patch

import pytest

_GET_CONN = "cqc_lem.utilities.db.get_db_connection"


@pytest.mark.unit
class TestPostUrlFromLogForUser:
    """Issue #800: missing POST success log used to raise TypeError on cursor.fetchone()[0]."""

    def test_returns_url_when_log_row_exists(self, mock_database_connection):
        from cqc_lem.utilities.db import get_post_url_from_log_for_user

        with patch(_GET_CONN, return_value=mock_database_connection["connection"]):
            mock_database_connection["cursor"].fetchone.return_value = ("https://www.linkedin.com/feed/update/urn:li:ugcPost:123",)

            result = get_post_url_from_log_for_user(7, 42)

        assert result == "https://www.linkedin.com/feed/update/urn:li:ugcPost:123"

    def test_returns_none_when_no_log_row_exists(self, mock_database_connection):
        from cqc_lem.utilities.db import get_post_url_from_log_for_user

        with patch(_GET_CONN, return_value=mock_database_connection["connection"]):
            mock_database_connection["cursor"].fetchone.return_value = None

            result = get_post_url_from_log_for_user(7, 42)

        assert result is None

    def test_returns_none_on_db_error(self, mock_database_connection):
        import mysql.connector

        from cqc_lem.utilities.db import get_post_url_from_log_for_user

        with patch(_GET_CONN, return_value=mock_database_connection["connection"]):
            mock_database_connection["cursor"].execute.side_effect = mysql.connector.Error("boom")

            result = get_post_url_from_log_for_user(7, 42)

        assert result is None


@pytest.mark.unit
class TestUserOwnsPosts:
    """Issue #914: the authorisation read standing between one account and another's drafts."""

    def test_true_when_every_id_belongs_to_the_user(self, mock_database_connection):
        from cqc_lem.utilities.db import user_owns_posts

        with patch(_GET_CONN, return_value=mock_database_connection["connection"]):
            mock_database_connection["cursor"].fetchone.return_value = (3,)
            assert user_owns_posts(7, [1, 2, 3]) is True

    def test_false_when_one_id_is_missing_or_foreign(self, mock_database_connection):
        from cqc_lem.utilities.db import user_owns_posts

        with patch(_GET_CONN, return_value=mock_database_connection["connection"]):
            mock_database_connection["cursor"].fetchone.return_value = (2,)
            assert user_owns_posts(7, [1, 2, 4242]) is False

    def test_duplicate_ids_do_not_inflate_the_expected_count(self, mock_database_connection):
        """COUNT(DISTINCT id) is compared against the DISTINCT input, or [5, 5] would never match."""
        from cqc_lem.utilities.db import user_owns_posts

        with patch(_GET_CONN, return_value=mock_database_connection["connection"]):
            mock_database_connection["cursor"].fetchone.return_value = (1,)
            assert user_owns_posts(7, [5, 5]) is True

    def test_query_is_scoped_by_user_id(self, mock_database_connection):
        from cqc_lem.utilities.db import user_owns_posts

        with patch(_GET_CONN, return_value=mock_database_connection["connection"]):
            mock_database_connection["cursor"].fetchone.return_value = (1,)
            user_owns_posts(7, [5])
        sql, params = mock_database_connection["cursor"].execute.call_args[0]
        assert "user_id = %s" in sql
        assert params[0] == 7

    def test_empty_inputs_are_false(self, mock_database_connection):
        from cqc_lem.utilities.db import user_owns_posts

        with patch(_GET_CONN, return_value=mock_database_connection["connection"]):
            assert user_owns_posts(7, []) is False
            assert user_owns_posts(None, [1]) is False
        mock_database_connection["cursor"].execute.assert_not_called()

    def test_a_db_error_raises_rather_than_answering_false(self, mock_database_connection):
        """Still a refusal at the call site, but a truthful one: the query never ran, so nothing was
        proved either way and reporting that as "not owned" (403) hides an outage behind a
        permission error.
        """
        import mysql.connector

        from cqc_lem.utilities.db import OwnershipUnprovable, user_owns_posts

        with patch(_GET_CONN, return_value=mock_database_connection["connection"]):
            mock_database_connection["cursor"].execute.side_effect = mysql.connector.Error("boom")
            with pytest.raises(OwnershipUnprovable):
                user_owns_posts(7, [1])

    def test_a_db_error_still_closes_the_cursor_and_connection(self, mock_database_connection):
        import mysql.connector

        from cqc_lem.utilities.db import OwnershipUnprovable, user_owns_posts

        with patch(_GET_CONN, return_value=mock_database_connection["connection"]):
            mock_database_connection["cursor"].execute.side_effect = mysql.connector.Error("boom")
            with pytest.raises(OwnershipUnprovable):
                user_owns_posts(7, [1])
        mock_database_connection["cursor"].close.assert_called_once()
        mock_database_connection["connection"].close.assert_called_once()

    def test_false_when_the_row_is_missing(self, mock_database_connection):
        from cqc_lem.utilities.db import user_owns_posts

        with patch(_GET_CONN, return_value=mock_database_connection["connection"]):
            mock_database_connection["cursor"].fetchone.return_value = None
            assert user_owns_posts(7, [1]) is False


@pytest.mark.unit
class TestPostMutationsAreOwnerScoped:
    """Issue #914: the ownership check is the gate, the WHERE scope is what makes forgetting it
    harmless — and it closes the window between the check and the write.
    """

    def test_bulk_update_scopes_the_where_clause(self, mock_database_connection):
        from cqc_lem.utilities.db import PostStatus, bulk_update_posts

        with patch(_GET_CONN, return_value=mock_database_connection["connection"]):
            mock_database_connection["cursor"].rowcount = 2
            assert bulk_update_posts([1, 2], status=PostStatus.APPROVED, user_id=7) is True
        sql, params = mock_database_connection["cursor"].execute.call_args[0]
        assert "AND user_id = %s" in sql
        assert params[-1] == 7

    def test_bulk_update_without_a_user_id_is_unscoped(self, mock_database_connection):
        """The parameter is optional so the non-API callers keep working unchanged."""
        from cqc_lem.utilities.db import PostStatus, bulk_update_posts

        with patch(_GET_CONN, return_value=mock_database_connection["connection"]):
            mock_database_connection["cursor"].rowcount = 1
            bulk_update_posts([1], status=PostStatus.APPROVED)
        sql, _ = mock_database_connection["cursor"].execute.call_args[0]
        assert "user_id" not in sql

    def test_soft_delete_forwards_the_owner(self, mock_database_connection):
        from cqc_lem.utilities.db import soft_delete_posts

        with patch(_GET_CONN, return_value=mock_database_connection["connection"]):
            mock_database_connection["cursor"].rowcount = 1
            soft_delete_posts([9], rejection_reason="nope", user_id=7)
        sql, params = mock_database_connection["cursor"].execute.call_args[0]
        assert "AND user_id = %s" in sql
        assert params[-1] == 7

    def test_update_db_post_scopes_the_where_clause(self, mock_database_connection):
        from cqc_lem.utilities.db import PostStatus, PostType, update_db_post

        with patch(_GET_CONN, return_value=mock_database_connection["connection"]):
            mock_database_connection["cursor"].rowcount = 1
            assert update_db_post("body", None, datetime(2026, 8, 2, 12, 0), PostType.TEXT, 9,
                                  PostStatus.PENDING, user_id=7) is True
        sql, params = mock_database_connection["cursor"].execute.call_args[0]
        assert sql.rstrip().endswith("AND user_id = %s")
        assert params[-1] == 7

    def test_update_db_post_without_a_user_id_is_unscoped(self, mock_database_connection):
        from cqc_lem.utilities.db import PostStatus, PostType, update_db_post

        with patch(_GET_CONN, return_value=mock_database_connection["connection"]):
            mock_database_connection["cursor"].rowcount = 1
            update_db_post("body", None, datetime(2026, 8, 2, 12, 0), PostType.TEXT, 9,
                           PostStatus.PENDING)
        sql, _ = mock_database_connection["cursor"].execute.call_args[0]
        assert "user_id" not in sql

    def test_rejection_reason_scopes_the_where_clause(self, mock_database_connection):
        """The sibling write `/update_post/` makes right after `update_db_post` — every write on
        this table carries the scope or the claim "forgetting the gate is harmless" is not true.
        """
        from cqc_lem.utilities.db import update_db_post_rejection_reason

        with patch(_GET_CONN, return_value=mock_database_connection["connection"]):
            mock_database_connection["cursor"].rowcount = 1
            assert update_db_post_rejection_reason(9, "too salesy", user_id=7) is True
        sql, params = mock_database_connection["cursor"].execute.call_args[0]
        assert sql.rstrip().endswith("AND user_id = %s")
        assert params[-1] == 7

    def test_rejection_reason_without_a_user_id_is_unscoped(self, mock_database_connection):
        from cqc_lem.utilities.db import update_db_post_rejection_reason

        with patch(_GET_CONN, return_value=mock_database_connection["connection"]):
            mock_database_connection["cursor"].rowcount = 1
            update_db_post_rejection_reason(9, "too salesy")
        sql, _ = mock_database_connection["cursor"].execute.call_args[0]
        assert "user_id" not in sql


@pytest.mark.unit
class TestPostMessageFromLogForUser:
    def test_returns_message_when_log_row_exists(self, mock_database_connection):
        from cqc_lem.utilities.db import get_post_message_from_log_for_user

        with patch(_GET_CONN, return_value=mock_database_connection["connection"]):
            mock_database_connection["cursor"].fetchone.return_value = ("Hello world",)

            result = get_post_message_from_log_for_user(7, 42)

        assert result == "Hello world"

    def test_returns_none_when_no_log_row_exists(self, mock_database_connection):
        from cqc_lem.utilities.db import get_post_message_from_log_for_user

        with patch(_GET_CONN, return_value=mock_database_connection["connection"]):
            mock_database_connection["cursor"].fetchone.return_value = None

            result = get_post_message_from_log_for_user(7, 42)

        assert result is None

    def test_returns_none_on_db_error(self, mock_database_connection):
        import mysql.connector

        from cqc_lem.utilities.db import get_post_message_from_log_for_user

        with patch(_GET_CONN, return_value=mock_database_connection["connection"]):
            mock_database_connection["cursor"].execute.side_effect = mysql.connector.Error("boom")

            result = get_post_message_from_log_for_user(7, 42)

        assert result is None


@pytest.mark.unit
class TestPostStatusEnum:
    def test_enum_values(self):
        from cqc_lem.utilities.db import PostStatus
        assert PostStatus.PLANNING == "planning"
        assert PostStatus.PENDING == "pending"
        assert PostStatus.APPROVED == "approved"
        assert PostStatus.REJECTED == "rejected"
        assert PostStatus.SCHEDULED == "scheduled"
        assert PostStatus.POSTED == "posted"

    def test_enum_is_string(self):
        from cqc_lem.utilities.db import PostStatus
        assert isinstance(PostStatus.PENDING.value, str)
        assert str(PostStatus.PENDING) == "pending"


@pytest.mark.unit
class TestPostTypeEnum:
    def test_enum_values(self):
        from cqc_lem.utilities.db import PostType
        assert PostType.TEXT == "text"
        assert PostType.CAROUSEL == "carousel"
        assert PostType.VIDEO == "video"


@pytest.mark.unit
class TestLogActionTypeEnum:
    def test_enum_values(self):
        from cqc_lem.utilities.db import LogActionType
        assert LogActionType.COMMENT == "comment"
        assert LogActionType.DM == "dm"
        assert LogActionType.POST == "post"
        assert LogActionType.ENGAGED == "engaged"


@pytest.mark.unit
class TestUpdateDbPostStatus:
    def test_executes_correct_sql(self, mock_database_connection):
        from cqc_lem.utilities.db import PostStatus, update_db_post_status

        with patch("cqc_lem.utilities.db.get_db_connection") as mock_conn:
            mock_conn.return_value = mock_database_connection["connection"]
            mock_database_connection["cursor"].rowcount = 1

            result = update_db_post_status(19, PostStatus.APPROVED)

            assert result is True
            args = mock_database_connection["cursor"].execute.call_args[0]
            assert "UPDATE" in args[0].upper()
            assert PostStatus.APPROVED.value in args[1]
            assert 19 in args[1]
            mock_database_connection["connection"].commit.assert_called_once()

    def test_returns_false_on_db_error(self, mock_database_connection):
        import mysql.connector

        from cqc_lem.utilities.db import PostStatus, update_db_post_status

        with patch("cqc_lem.utilities.db.get_db_connection") as mock_conn:
            mock_conn.return_value = mock_database_connection["connection"]
            mock_database_connection["cursor"].execute.side_effect = mysql.connector.Error("DB error")

            result = update_db_post_status(19, PostStatus.APPROVED)

            assert result is False


@pytest.mark.unit
class TestUpdateDbPostContent:
    def test_executes_update_with_content(self, mock_database_connection):
        from cqc_lem.utilities.db import update_db_post_content

        with patch("cqc_lem.utilities.db.get_db_connection") as mock_conn:
            mock_conn.return_value = mock_database_connection["connection"]
            mock_database_connection["cursor"].rowcount = 1

            result = update_db_post_content(19, "New content here")

            assert result is True
            args = mock_database_connection["cursor"].execute.call_args[0]
            assert "New content here" in args[1]
            assert 19 in args[1]

    def test_returns_false_on_db_error(self, mock_database_connection):
        import mysql.connector

        from cqc_lem.utilities.db import update_db_post_content

        with patch("cqc_lem.utilities.db.get_db_connection") as mock_conn:
            mock_conn.return_value = mock_database_connection["connection"]
            mock_database_connection["cursor"].execute.side_effect = mysql.connector.Error("err")

            assert update_db_post_content(19, "content") is False


@pytest.mark.unit
class TestUpdateDbPostRejectionReason:
    def test_executes_update_with_reason(self, mock_database_connection):
        from cqc_lem.utilities.db import update_db_post_rejection_reason

        with patch("cqc_lem.utilities.db.get_db_connection") as mock_conn:
            mock_conn.return_value = mock_database_connection["connection"]
            mock_database_connection["cursor"].rowcount = 1

            result = update_db_post_rejection_reason(19, "Too promotional")

            assert result is True
            args = mock_database_connection["cursor"].execute.call_args[0]
            assert "UPDATE" in args[0].upper()
            assert "rejection_reason" in args[0].lower()
            assert "Too promotional" in args[1]
            assert 19 in args[1]

    def test_blank_reason_stored_as_null(self, mock_database_connection):
        from cqc_lem.utilities.db import update_db_post_rejection_reason

        with patch("cqc_lem.utilities.db.get_db_connection") as mock_conn:
            mock_conn.return_value = mock_database_connection["connection"]
            mock_database_connection["cursor"].rowcount = 1

            update_db_post_rejection_reason(19, "   ")

            args = mock_database_connection["cursor"].execute.call_args[0]
            assert args[1][0] is None

    def test_returns_false_on_db_error(self, mock_database_connection):
        import mysql.connector

        from cqc_lem.utilities.db import update_db_post_rejection_reason

        with patch("cqc_lem.utilities.db.get_db_connection") as mock_conn:
            mock_conn.return_value = mock_database_connection["connection"]
            mock_database_connection["cursor"].execute.side_effect = mysql.connector.Error("err")

            assert update_db_post_rejection_reason(19, "reason") is False


@pytest.mark.unit
class TestGetPostRejectionReason:
    def test_returns_reason_when_present(self, mock_database_connection):
        from cqc_lem.utilities.db import get_post_rejection_reason

        with patch("cqc_lem.utilities.db.get_db_connection") as mock_conn:
            mock_conn.return_value = mock_database_connection["connection"]
            mock_database_connection["cursor"].fetchone.return_value = ("Too salesy",)

            assert get_post_rejection_reason(19) == "Too salesy"

    def test_returns_none_when_missing(self, mock_database_connection):
        from cqc_lem.utilities.db import get_post_rejection_reason

        with patch("cqc_lem.utilities.db.get_db_connection") as mock_conn:
            mock_conn.return_value = mock_database_connection["connection"]
            mock_database_connection["cursor"].fetchone.return_value = (None,)

            assert get_post_rejection_reason(19) is None

    def test_returns_none_on_db_error(self, mock_database_connection):
        import mysql.connector

        from cqc_lem.utilities.db import get_post_rejection_reason

        with patch("cqc_lem.utilities.db.get_db_connection") as mock_conn:
            mock_conn.return_value = mock_database_connection["connection"]
            mock_database_connection["cursor"].execute.side_effect = mysql.connector.Error("err")

            assert get_post_rejection_reason(19) is None


@pytest.mark.unit
class TestSoftDeletePosts:
    def test_passes_rejection_reason_to_bulk_update(self, mock_database_connection):
        from cqc_lem.utilities.db import PostStatus, soft_delete_posts

        with patch("cqc_lem.utilities.db.get_db_connection") as mock_conn:
            mock_conn.return_value = mock_database_connection["connection"]
            mock_database_connection["cursor"].rowcount = 1

            soft_delete_posts([7, 8], rejection_reason="Too long")

            args = mock_database_connection["cursor"].execute.call_args[0]
            assert PostStatus.REJECTED.value in args[1]
            assert "Too long" in args[1]


@pytest.mark.unit
class TestUpdateDbPostVideoUrl:
    def test_executes_update_with_url(self, mock_database_connection):
        from cqc_lem.utilities.db import update_db_post_video_url

        with patch("cqc_lem.utilities.db.get_db_connection") as mock_conn:
            mock_conn.return_value = mock_database_connection["connection"]
            mock_database_connection["cursor"].rowcount = 1

            result = update_db_post_video_url(19, "https://example.com/video.mp4")

            assert result is True
            args = mock_database_connection["cursor"].execute.call_args[0]
            assert "https://example.com/video.mp4" in args[1]
            assert 19 in args[1]


@pytest.mark.unit
class TestGetPosts:
    def test_returns_tuple_of_list_and_total(self, mock_database_connection):
        from cqc_lem.utilities.db import get_posts

        with patch("cqc_lem.utilities.db.get_db_connection") as mock_conn:
            mock_conn.return_value = mock_database_connection["connection"]
            mock_database_connection["cursor"].fetchone.return_value = {"total": 1}
            mock_database_connection["cursor"].fetchall.return_value = [
                {"id": 1, "content": "Test", "status": "pending", "post_type": "text",
                 "scheduled_time": "2024-01-01 12:00:00", "video_url": None, "carousel_slides": None}
            ]

            posts, total = get_posts(60)

            assert isinstance(posts, list)
            assert total == 1

    def test_pagination_params_forwarded(self, mock_database_connection):
        from cqc_lem.utilities.db import get_posts

        with patch("cqc_lem.utilities.db.get_db_connection") as mock_conn:
            mock_conn.return_value = mock_database_connection["connection"]
            mock_database_connection["cursor"].fetchone.return_value = {"total": 0}
            mock_database_connection["cursor"].fetchall.return_value = []

            get_posts(42, limit=25, offset=50, sort_order='desc', status_filter='pending')

            calls = mock_database_connection["cursor"].execute.call_args_list
            # Second call is the data query; it should contain LIMIT/OFFSET params
            data_call_args = calls[1][0][1]
            assert 25 in data_call_args  # limit
            assert 50 in data_call_args  # offset

    def test_status_filter_applied(self, mock_database_connection):
        from cqc_lem.utilities.db import get_posts

        with patch("cqc_lem.utilities.db.get_db_connection") as mock_conn:
            mock_conn.return_value = mock_database_connection["connection"]
            mock_database_connection["cursor"].fetchone.return_value = {"total": 0}
            mock_database_connection["cursor"].fetchall.return_value = []

            get_posts(42, status_filter='approved')

            calls = mock_database_connection["cursor"].execute.call_args_list
            count_call_params = calls[0][0][1]
            assert 'approved' in count_call_params

    def test_post_type_filter_applied(self, mock_database_connection):
        from cqc_lem.utilities.db import get_posts

        with patch("cqc_lem.utilities.db.get_db_connection") as mock_conn:
            mock_conn.return_value = mock_database_connection["connection"]
            mock_database_connection["cursor"].fetchone.return_value = {"total": 0}
            mock_database_connection["cursor"].fetchall.return_value = []

            get_posts(42, post_type_filter='Video')

            calls = mock_database_connection["cursor"].execute.call_args_list
            count_sql, count_params = calls[0][0]
            assert "post_type = %s" in count_sql
            assert 'video' in count_params  # lowercased

    def test_search_adds_like_condition(self, mock_database_connection):
        from cqc_lem.utilities.db import get_posts

        with patch("cqc_lem.utilities.db.get_db_connection") as mock_conn:
            mock_conn.return_value = mock_database_connection["connection"]
            mock_database_connection["cursor"].fetchone.return_value = {"total": 0}
            mock_database_connection["cursor"].fetchall.return_value = []

            get_posts(42, search='ai AND marketing')

            count_sql, count_params = mock_database_connection["cursor"].execute.call_args_list[0][0]
            assert "content LIKE %s" in count_sql
            assert '%ai%' in count_params and '%marketing%' in count_params

    def test_sort_by_whitelisted_column(self, mock_database_connection):
        from cqc_lem.utilities.db import get_posts

        with patch("cqc_lem.utilities.db.get_db_connection") as mock_conn:
            mock_conn.return_value = mock_database_connection["connection"]
            mock_database_connection["cursor"].fetchone.return_value = {"total": 0}
            mock_database_connection["cursor"].fetchall.return_value = []

            get_posts(42, sort_by='status')

            data_sql = mock_database_connection["cursor"].execute.call_args_list[1][0][0]
            assert "ORDER BY status" in data_sql

    def test_sort_by_rejects_injection(self, mock_database_connection):
        from cqc_lem.utilities.db import get_posts

        with patch("cqc_lem.utilities.db.get_db_connection") as mock_conn:
            mock_conn.return_value = mock_database_connection["connection"]
            mock_database_connection["cursor"].fetchone.return_value = {"total": 0}
            mock_database_connection["cursor"].fetchall.return_value = []

            get_posts(42, sort_by='content; DROP TABLE posts')

            data_sql = mock_database_connection["cursor"].execute.call_args_list[1][0][0]
            assert "DROP TABLE" not in data_sql
            assert "ORDER BY scheduled_time" in data_sql  # safe default

    def test_db_error_returns_empty(self, mock_database_connection):
        import mysql.connector

        from cqc_lem.utilities.db import get_posts

        with patch("cqc_lem.utilities.db.get_db_connection") as mock_conn:
            mock_conn.return_value = mock_database_connection["connection"]
            mock_database_connection["cursor"].execute.side_effect = mysql.connector.Error("boom")

            posts, total = get_posts(42, search='ai')

            assert posts == [] and total == 0

    def test_start_date_filter_adds_lower_bound(self, mock_database_connection):
        from datetime import datetime, timezone

        from cqc_lem.utilities.db import get_posts

        with patch("cqc_lem.utilities.db.get_db_connection") as mock_conn:
            mock_conn.return_value = mock_database_connection["connection"]
            mock_database_connection["cursor"].fetchone.return_value = {"total": 0}
            mock_database_connection["cursor"].fetchall.return_value = []

            start = datetime(2026, 7, 1, 0, 0, 0, tzinfo=timezone.utc)
            get_posts(42, start_date=start)

            count_sql, count_params = mock_database_connection["cursor"].execute.call_args_list[0][0]
            assert "scheduled_time >= %s" in count_sql
            assert datetime(2026, 7, 1, 0, 0, 0) in count_params

    def test_end_date_filter_adds_upper_bound(self, mock_database_connection):
        from datetime import datetime, timezone

        from cqc_lem.utilities.db import get_posts

        with patch("cqc_lem.utilities.db.get_db_connection") as mock_conn:
            mock_conn.return_value = mock_database_connection["connection"]
            mock_database_connection["cursor"].fetchone.return_value = {"total": 0}
            mock_database_connection["cursor"].fetchall.return_value = []

            end = datetime(2026, 7, 31, 12, 30, 0, tzinfo=timezone.utc)
            get_posts(42, end_date=end)

            count_sql, count_params = mock_database_connection["cursor"].execute.call_args_list[0][0]
            assert "scheduled_time <= %s" in count_sql
            assert datetime(2026, 7, 31, 12, 30, 0) in count_params

    def test_date_range_converts_offset_to_naive_utc(self, mock_database_connection):
        from datetime import datetime, timedelta, timezone

        from cqc_lem.utilities.db import get_posts

        with patch("cqc_lem.utilities.db.get_db_connection") as mock_conn:
            mock_conn.return_value = mock_database_connection["connection"]
            mock_database_connection["cursor"].fetchone.return_value = {"total": 0}
            mock_database_connection["cursor"].fetchall.return_value = []

            # EDT (UTC-4) midnight -> 04:00 UTC naive
            edt = timezone(timedelta(hours=-4))
            start = datetime(2026, 7, 15, 0, 0, 0, tzinfo=edt)
            get_posts(42, start_date=start)

            count_params = mock_database_connection["cursor"].execute.call_args_list[0][0][1]
            assert datetime(2026, 7, 15, 4, 0, 0) in count_params


@pytest.mark.unit
class TestBuildContentSearchClause:
    def test_empty_returns_none(self):
        from cqc_lem.utilities.db import build_content_search_clause
        assert build_content_search_clause('') == (None, [])
        assert build_content_search_clause('   ') == (None, [])
        assert build_content_search_clause(None) == (None, [])

    def test_single_term(self):
        from cqc_lem.utilities.db import build_content_search_clause
        sql, params = build_content_search_clause('ai')
        assert sql == 'content LIKE %s'
        assert params == ['%ai%']

    def test_and_or_not(self):
        from cqc_lem.utilities.db import build_content_search_clause
        sql, params = build_content_search_clause('ai AND marketing')
        assert ' AND ' in sql and params == ['%ai%', '%marketing%']
        sql, _ = build_content_search_clause('ai OR marketing')
        assert ' OR ' in sql
        sql, _ = build_content_search_clause('NOT ai')
        assert 'NOT' in sql

    def test_implicit_and(self):
        from cqc_lem.utilities.db import build_content_search_clause
        sql, params = build_content_search_clause('ai marketing')
        assert ' AND ' in sql and params == ['%ai%', '%marketing%']

    def test_grouping_and_phrase(self):
        from cqc_lem.utilities.db import build_content_search_clause
        sql, params = build_content_search_clause('(ai OR ml) AND NOT "growth hacking"')
        assert params == ['%ai%', '%ml%', '%growth hacking%']
        assert ' OR ' in sql and ' AND ' in sql and 'NOT' in sql

    def test_wildcards_escaped(self):
        from cqc_lem.utilities.db import build_content_search_clause
        _, params = build_content_search_clause('50%_off')
        assert params == ['%50\\%\\_off%']

    def test_unbalanced_falls_back_to_literal(self):
        from cqc_lem.utilities.db import build_content_search_clause
        sql, params = build_content_search_clause('(unbalanced OR')
        assert sql == 'content LIKE %s'
        assert params == ['%(unbalanced OR%']

    def test_custom_column(self):
        from cqc_lem.utilities.db import build_content_search_clause
        sql, _ = build_content_search_clause('hi', column='message')
        assert sql == 'message LIKE %s'

    def test_empty_quotes_tokenize_to_nothing(self):
        from cqc_lem.utilities.db import build_content_search_clause
        # Non-blank input that yields no tokens (empty quoted phrase) → treated as no search.
        assert build_content_search_clause('""') == (None, [])

    def test_too_many_terms_falls_back_to_literal(self):
        from cqc_lem.utilities.db import build_content_search_clause
        query = " ".join(f"t{i}" for i in range(25))  # exceeds the term cap
        sql, params = build_content_search_clause(query)
        assert sql == 'content LIKE %s'
        assert params == [f"%{query}%"]

    def test_open_paren_without_close_falls_back(self):
        from cqc_lem.utilities.db import build_content_search_clause
        sql, params = build_content_search_clause('(ai')
        assert sql == 'content LIKE %s'
        assert params == ['%(ai%']

    def test_trailing_tokens_fall_back(self):
        from cqc_lem.utilities.db import build_content_search_clause
        sql, params = build_content_search_clause('ai )')
        assert sql == 'content LIKE %s'
        assert params == ['%ai )%']


@pytest.mark.unit
class TestGetPostByEmailIsGone:
    """`get_post_by_email` turned an ADDRESS into somebody's posts — the exact shape `GET /posts/`
    used to authenticate on. Its one caller resolves the session now, so the wrapper was deleted
    rather than deprecated (issue #914); this test is what stops it coming back.
    """

    def test_address_keyed_post_reader_no_longer_exists(self):
        import cqc_lem.utilities.db as db

        assert not hasattr(db, "get_post_by_email")


@pytest.mark.unit
class TestInsertPost:
    def test_inserts_post_and_returns_true(self, mock_database_connection):
        from cqc_lem.utilities.db import PostType, insert_post

        with patch("cqc_lem.utilities.db.get_db_connection") as mock_conn, \
             patch("cqc_lem.utilities.db.get_user_id") as mock_get_user:
            mock_conn.return_value = mock_database_connection["connection"]
            mock_get_user.return_value = 60
            mock_database_connection["cursor"].rowcount = 1

            result = insert_post(
                "test@example.com",
                "Test content",
                datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
                PostType.TEXT,
            )

            assert result is True
            assert mock_database_connection["cursor"].execute.called
            mock_database_connection["connection"].commit.assert_called_once()

    def test_returns_false_when_user_not_found(self, mock_database_connection):
        from cqc_lem.utilities.db import PostType, insert_post

        with patch("cqc_lem.utilities.db.get_user_id") as mock_get_user:
            mock_get_user.return_value = None

            result = insert_post(
                "unknown@example.com",
                "content",
                datetime.now(tz=timezone.utc),
                PostType.TEXT,
            )

            assert result is False

    def test_inserts_with_explicit_status(self, mock_database_connection):
        from cqc_lem.utilities.db import PostStatus, PostType, insert_post

        with patch("cqc_lem.utilities.db.get_db_connection") as mock_conn, \
             patch("cqc_lem.utilities.db.get_user_id", return_value=60):
            mock_conn.return_value = mock_database_connection["connection"]
            mock_database_connection["cursor"].rowcount = 1

            insert_post("test@example.com", "content",
                        datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
                        PostType.TEXT, status=PostStatus.APPROVED)

            sql, params = mock_database_connection["cursor"].execute.call_args[0]
            assert "status" in sql
            assert PostStatus.APPROVED.value in params

    def test_status_defaults_to_pending(self, mock_database_connection):
        from cqc_lem.utilities.db import PostStatus, PostType, insert_post

        with patch("cqc_lem.utilities.db.get_db_connection") as mock_conn, \
             patch("cqc_lem.utilities.db.get_user_id", return_value=60):
            mock_conn.return_value = mock_database_connection["connection"]
            mock_database_connection["cursor"].rowcount = 1

            insert_post("test@example.com", "content",
                        datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc), PostType.TEXT)

            _, params = mock_database_connection["cursor"].execute.call_args[0]
            assert PostStatus.PENDING.value in params


@pytest.mark.unit
class TestGetUserId:
    def test_returns_user_id_for_known_email(self, mock_database_connection):
        from cqc_lem.utilities.db import get_user_id

        with patch("cqc_lem.utilities.db.get_db_connection") as mock_conn:
            mock_conn.return_value = mock_database_connection["connection"]
            # cursor uses dictionary=True so fetchone returns a dict
            mock_database_connection["cursor"].fetchone.return_value = {"id": 42}

            result = get_user_id("test@example.com")

            assert result == 42

    def test_returns_none_for_unknown_email(self, mock_database_connection):
        from cqc_lem.utilities.db import get_user_id

        with patch("cqc_lem.utilities.db.get_db_connection") as mock_conn:
            mock_conn.return_value = mock_database_connection["connection"]
            mock_database_connection["cursor"].fetchone.return_value = None

            result = get_user_id("nobody@example.com")

            assert result is None


@pytest.mark.unit
class TestInsertPostExtended:
    def test_inserts_with_video_url(self, mock_database_connection):
        from cqc_lem.utilities.db import PostType, insert_post

        with patch("cqc_lem.utilities.db.get_db_connection") as mock_conn, \
             patch("cqc_lem.utilities.db.get_user_id") as mock_uid:
            mock_conn.return_value = mock_database_connection["connection"]
            mock_uid.return_value = 10
            mock_database_connection["cursor"].rowcount = 1

            result = insert_post(
                "test@example.com",
                "Video post",
                datetime(2025, 6, 1, 12, 0, tzinfo=timezone.utc),
                PostType.VIDEO,
                video_url="https://cdn.example.com/video.mp4",
            )

            assert result is True
            call_args = mock_database_connection["cursor"].execute.call_args[0]
            assert "https://cdn.example.com/video.mp4" in call_args[1]

    def test_inserts_with_carousel_slides(self, mock_database_connection):
        import json

        from cqc_lem.utilities.db import PostType, insert_post

        with patch("cqc_lem.utilities.db.get_db_connection") as mock_conn, \
             patch("cqc_lem.utilities.db.get_user_id") as mock_uid:
            mock_conn.return_value = mock_database_connection["connection"]
            mock_uid.return_value = 10
            mock_database_connection["cursor"].rowcount = 1

            slides = ["Slide one text", "Slide two text"]
            result = insert_post(
                "test@example.com",
                "Carousel caption",
                datetime(2025, 6, 1, 12, 0, tzinfo=timezone.utc),
                PostType.CAROUSEL,
                carousel_slides=slides,
            )

            assert result is True
            call_args = mock_database_connection["cursor"].execute.call_args[0]
            # carousel_slides should be serialized as JSON
            assert json.dumps(slides) in call_args[1]


@pytest.mark.unit
class TestGetPostType:
    def test_returns_post_type_enum(self, mock_database_connection):
        from cqc_lem.utilities.db import PostType, get_post_type

        with patch("cqc_lem.utilities.db.get_db_connection") as mock_conn:
            mock_conn.return_value = mock_database_connection["connection"]
            mock_database_connection["cursor"].fetchone.return_value = {"post_type": "carousel"}

            result = get_post_type(5)

            assert result == PostType.CAROUSEL

    def test_returns_none_when_not_found(self, mock_database_connection):
        from cqc_lem.utilities.db import get_post_type

        with patch("cqc_lem.utilities.db.get_db_connection") as mock_conn:
            mock_conn.return_value = mock_database_connection["connection"]
            mock_database_connection["cursor"].fetchone.return_value = None

            result = get_post_type(999)

            assert result is None


@pytest.mark.unit
class TestGetCarouselSlides:
    def test_returns_parsed_slides(self, mock_database_connection):
        import json

        from cqc_lem.utilities.db import get_carousel_slides

        slides = ["First slide", "Second slide"]
        with patch("cqc_lem.utilities.db.get_db_connection") as mock_conn:
            mock_conn.return_value = mock_database_connection["connection"]
            mock_database_connection["cursor"].fetchone.return_value = {
                "carousel_slides": json.dumps(slides)
            }

            result = get_carousel_slides(5)

            assert result == slides

    def test_returns_empty_list_when_null(self, mock_database_connection):
        from cqc_lem.utilities.db import get_carousel_slides

        with patch("cqc_lem.utilities.db.get_db_connection") as mock_conn:
            mock_conn.return_value = mock_database_connection["connection"]
            mock_database_connection["cursor"].fetchone.return_value = {"carousel_slides": None}

            result = get_carousel_slides(5)

            assert result == []


@pytest.mark.unit
class TestBulkUpdatePosts:
    def test_updates_status_for_multiple_ids(self, mock_database_connection):
        from cqc_lem.utilities.db import PostStatus, bulk_update_posts

        with patch("cqc_lem.utilities.db.get_db_connection") as mock_conn:
            mock_conn.return_value = mock_database_connection["connection"]
            mock_database_connection["cursor"].rowcount = 3

            result = bulk_update_posts([1, 2, 3], status=PostStatus.APPROVED)

            assert result is True
            call_args = mock_database_connection["cursor"].execute.call_args[0]
            assert "approved" in call_args[1]
            mock_database_connection["connection"].commit.assert_called_once()

    def test_returns_false_for_empty_list(self, mock_database_connection):
        from cqc_lem.utilities.db import bulk_update_posts

        result = bulk_update_posts([])

        assert result is False

    def test_returns_false_when_no_fields_provided(self, mock_database_connection):
        from cqc_lem.utilities.db import bulk_update_posts

        result = bulk_update_posts([1, 2])

        assert result is False


@pytest.mark.unit
class TestSoftDeletePosts:
    def test_sets_status_to_rejected(self, mock_database_connection):
        from cqc_lem.utilities.db import soft_delete_posts

        with patch("cqc_lem.utilities.db.bulk_update_posts") as mock_bulk:
            mock_bulk.return_value = True

            result = soft_delete_posts([10, 11])

            assert result is True
            mock_bulk.assert_called_once()
            _, kwargs = mock_bulk.call_args
            assert kwargs["status"].value == "rejected"


@pytest.mark.unit
class TestUpdateLinkedinConnectionStatus:
    def test_updates_status(self, mock_database_connection):
        from cqc_lem.utilities.db import update_linkedin_connection_status

        with patch("cqc_lem.utilities.db.get_db_connection") as mock_conn:
            mock_conn.return_value = mock_database_connection["connection"]
            mock_database_connection["cursor"].rowcount = 1

            result = update_linkedin_connection_status(42, "connected")

            assert result is True
            args = mock_database_connection["cursor"].execute.call_args[0]
            assert "connected" in args[1]
            assert 42 in args[1]

    def test_returns_false_on_error(self, mock_database_connection):
        import mysql.connector

        from cqc_lem.utilities.db import update_linkedin_connection_status

        with patch("cqc_lem.utilities.db.get_db_connection") as mock_conn:
            mock_conn.return_value = mock_database_connection["connection"]
            mock_database_connection["cursor"].execute.side_effect = mysql.connector.Error("err")

            assert update_linkedin_connection_status(42, "disconnected") is False


@pytest.mark.unit
class TestGetUserSubscriptionInfo:
    def test_returns_subscription_dict(self, mock_database_connection):
        from cqc_lem.utilities.db import get_user_subscription_info

        expected = {
            "subscription_status": "trial",
            "subscription_tier": "free_trial",
            "trial_started_at": None,
            "trial_ends_at": None,
            "stripe_customer_id": None,
            "stripe_subscription_id": None,
        }
        with patch("cqc_lem.utilities.db.get_db_connection") as mock_conn:
            mock_conn.return_value = mock_database_connection["connection"]
            mock_database_connection["cursor"].fetchone.return_value = expected

            result = get_user_subscription_info(7)

            assert result == expected

    def test_returns_none_on_error(self, mock_database_connection):
        import mysql.connector

        from cqc_lem.utilities.db import get_user_subscription_info

        with patch("cqc_lem.utilities.db.get_db_connection") as mock_conn:
            mock_conn.return_value = mock_database_connection["connection"]
            mock_database_connection["cursor"].execute.side_effect = mysql.connector.Error("err")

            assert get_user_subscription_info(7) is None


@pytest.mark.unit
class TestUpdateSubscriptionFromStripe:
    def test_updates_matching_customer(self, mock_database_connection):
        from cqc_lem.utilities.db import update_subscription_from_stripe

        with patch("cqc_lem.utilities.db.get_db_connection") as mock_conn:
            mock_conn.return_value = mock_database_connection["connection"]
            mock_database_connection["cursor"].rowcount = 1

            result = update_subscription_from_stripe("cus_123", "active", "starter", "sub_456")

            assert result is True
            args = mock_database_connection["cursor"].execute.call_args[0]
            assert "cus_123" in args[1]
            assert "active" in args[1]

    def test_returns_false_on_error(self, mock_database_connection):
        import mysql.connector

        from cqc_lem.utilities.db import update_subscription_from_stripe

        with patch("cqc_lem.utilities.db.get_db_connection") as mock_conn:
            mock_conn.return_value = mock_database_connection["connection"]
            mock_database_connection["cursor"].execute.side_effect = mysql.connector.Error("err")

            assert update_subscription_from_stripe("cus_123", "active", None, None) is False


@pytest.mark.unit
class TestGetUserPreferences:
    def test_returns_preferences(self, mock_database_connection):
        from cqc_lem.utilities.db import get_user_preferences

        expected = {"last_login_inactivate_delay": 90, "auto_schedule_posts": 0}
        with patch("cqc_lem.utilities.db.get_db_connection") as mock_conn:
            mock_conn.return_value = mock_database_connection["connection"]
            mock_database_connection["cursor"].fetchone.return_value = expected

            result = get_user_preferences(5)

            assert result == expected

    def test_returns_defaults_on_error(self, mock_database_connection):
        import mysql.connector

        from cqc_lem.utilities.db import get_user_preferences

        with patch("cqc_lem.utilities.db.get_db_connection") as mock_conn:
            mock_conn.return_value = mock_database_connection["connection"]
            mock_database_connection["cursor"].execute.side_effect = mysql.connector.Error("err")

            result = get_user_preferences(5)
            # On DB error, safe defaults are returned so automation is not silently broken
            from cqc_lem.utilities.db import DEFAULT_CONTENT_BUFFER_DAYS, DEFAULT_CONTENT_BUFFER_MAX_POSTS
            assert result == {"last_login_inactivate_delay": None, "auto_schedule_posts": True,
                              "content_buffer_days": DEFAULT_CONTENT_BUFFER_DAYS,
                              "content_buffer_max_posts": DEFAULT_CONTENT_BUFFER_MAX_POSTS,
                              "content_language": None}

    def test_returns_defaults_when_row_missing(self, mock_database_connection):
        from cqc_lem.utilities.db import get_user_preferences

        with patch("cqc_lem.utilities.db.get_db_connection") as mock_conn:
            mock_conn.return_value = mock_database_connection["connection"]
            mock_database_connection["cursor"].fetchone.return_value = None

            result = get_user_preferences(99)
            assert result["auto_schedule_posts"] is True


@pytest.mark.unit
class TestUpdateUserPreferences:
    def test_updates_with_delay_and_auto_schedule(self, mock_database_connection):
        from cqc_lem.utilities.db import update_user_preferences

        with patch("cqc_lem.utilities.db.get_db_connection") as mock_conn:
            mock_conn.return_value = mock_database_connection["connection"]
            mock_database_connection["cursor"].rowcount = 1

            result = update_user_preferences(10, 60, True)

            assert result is True
            args = mock_database_connection["cursor"].execute.call_args[0]
            assert 60 in args[1]
            assert 1 in args[1]   # auto_schedule_posts=True → 1
            assert 10 in args[1]

    def test_null_delay_for_never(self, mock_database_connection):
        from cqc_lem.utilities.db import update_user_preferences

        with patch("cqc_lem.utilities.db.get_db_connection") as mock_conn:
            mock_conn.return_value = mock_database_connection["connection"]
            mock_database_connection["cursor"].rowcount = 1

            result = update_user_preferences(10, None, False)

            assert result is True
            args = mock_database_connection["cursor"].execute.call_args[0]
            assert None in args[1]
            assert 0 in args[1]   # auto_schedule_posts=False → 0

    def test_returns_false_on_error(self, mock_database_connection):
        import mysql.connector

        from cqc_lem.utilities.db import update_user_preferences

        with patch("cqc_lem.utilities.db.get_db_connection") as mock_conn:
            mock_conn.return_value = mock_database_connection["connection"]
            mock_database_connection["cursor"].execute.side_effect = mysql.connector.Error("err")

            assert update_user_preferences(10, 90, False) is False


@pytest.mark.unit
class TestGetActiveUserIds:
    def test_returns_user_ids_from_query(self, mock_database_connection):
        from cqc_lem.utilities.db import get_active_user_ids

        with patch("cqc_lem.utilities.db.get_db_connection") as mock_conn:
            mock_conn.return_value = mock_database_connection["connection"]
            mock_database_connection["cursor"].fetchall.return_value = [(1,), (2,), (3,)]

            result = get_active_user_ids()

            assert result == [1, 2, 3]

    def test_returns_empty_list_on_error(self, mock_database_connection):
        import mysql.connector

        from cqc_lem.utilities.db import get_active_user_ids

        with patch("cqc_lem.utilities.db.get_db_connection") as mock_conn:
            mock_conn.return_value = mock_database_connection["connection"]
            mock_database_connection["cursor"].execute.side_effect = mysql.connector.Error("err")

            assert get_active_user_ids() == []

    def test_query_includes_linkedin_and_subscription_checks(self, mock_database_connection):
        from cqc_lem.utilities.db import get_active_user_ids

        with patch("cqc_lem.utilities.db.get_db_connection") as mock_conn:
            mock_conn.return_value = mock_database_connection["connection"]
            mock_database_connection["cursor"].fetchall.return_value = []

            get_active_user_ids()

            sql = mock_database_connection["cursor"].execute.call_args[0][0]
            assert "linkedin_connection_status" in sql
            assert "subscription_status" in sql
            assert "last_login_inactivate_delay" in sql


# ---------------------------------------------------------------------------
# get_user_access_token — must use correct columns (not the old token_expiry)
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestGetUserAccessToken:
    def test_returns_token_when_not_expired(self, mock_database_connection):
        from cqc_lem.utilities.db import get_user_access_token

        with patch("cqc_lem.utilities.db.get_db_connection") as mock_conn:
            mock_conn.return_value = mock_database_connection["connection"]
            mock_database_connection["cursor"].fetchone.return_value = {
                "access_token": "my-access-token"
            }

            result = get_user_access_token(60)

        assert result == "my-access-token"

    def test_returns_none_when_token_missing(self, mock_database_connection):
        from cqc_lem.utilities.db import get_user_access_token

        with patch("cqc_lem.utilities.db.get_db_connection") as mock_conn:
            mock_conn.return_value = mock_database_connection["connection"]
            mock_database_connection["cursor"].fetchone.return_value = None

            result = get_user_access_token(99)

        assert result is None

    def test_sql_uses_access_token_created_at_not_token_expiry(self, mock_database_connection):
        """Regression: query must reference access_token_created_at, not the non-existent token_expiry."""
        from cqc_lem.utilities.db import get_user_access_token

        with patch("cqc_lem.utilities.db.get_db_connection") as mock_conn:
            mock_conn.return_value = mock_database_connection["connection"]
            mock_database_connection["cursor"].fetchone.return_value = None

            get_user_access_token(1)

        sql = mock_database_connection["cursor"].execute.call_args[0][0]
        assert "token_expiry" not in sql, (
            "token_expiry column does not exist; query references a non-existent column"
        )
        assert "access_token_created_at" in sql

    def test_returns_none_on_db_error(self, mock_database_connection):
        import mysql.connector

        from cqc_lem.utilities.db import get_user_access_token

        with patch("cqc_lem.utilities.db.get_db_connection") as mock_conn:
            mock_conn.return_value = mock_database_connection["connection"]
            mock_database_connection["cursor"].execute.side_effect = mysql.connector.Error("err")

            result = get_user_access_token(1)

        assert result is None


# ---------------------------------------------------------------------------
# get_orphaned_scheduled_posts — recovery for tasks lost on container restart
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestGetOrphanedScheduledPosts:
    def test_returns_posts_in_scheduled_status_past_cutoff(self, mock_database_connection):
        from cqc_lem.utilities.db import get_orphaned_scheduled_posts

        rows = [
            (1485, datetime(2026, 6, 19, 19, 45, 0, tzinfo=timezone.utc), 60),
        ]
        with patch("cqc_lem.utilities.db.get_db_connection") as mock_conn:
            mock_conn.return_value = mock_database_connection["connection"]
            mock_database_connection["cursor"].fetchall.return_value = rows

            result = get_orphaned_scheduled_posts(lookback_hours=2)

        assert result == rows

    def test_returns_empty_list_when_none_orphaned(self, mock_database_connection):
        from cqc_lem.utilities.db import get_orphaned_scheduled_posts

        with patch("cqc_lem.utilities.db.get_db_connection") as mock_conn:
            mock_conn.return_value = mock_database_connection["connection"]
            mock_database_connection["cursor"].fetchall.return_value = []

            result = get_orphaned_scheduled_posts()

        assert result == []

    def test_sql_filters_by_scheduled_status_and_cutoff(self, mock_database_connection):
        from cqc_lem.utilities.db import get_orphaned_scheduled_posts

        with patch("cqc_lem.utilities.db.get_db_connection") as mock_conn:
            mock_conn.return_value = mock_database_connection["connection"]
            mock_database_connection["cursor"].fetchall.return_value = []

            get_orphaned_scheduled_posts(lookback_hours=3)

        sql = mock_database_connection["cursor"].execute.call_args[0][0]
        assert "scheduled" in sql.lower()
        assert "scheduled_time" in sql

    def test_cutoff_is_lookback_hours_before_now(self, mock_database_connection):
        """The cutoff passed to the query must be approximately (now - lookback_hours)."""
        from datetime import timedelta

        from cqc_lem.utilities.db import get_orphaned_scheduled_posts

        with patch("cqc_lem.utilities.db.get_db_connection") as mock_conn:
            mock_conn.return_value = mock_database_connection["connection"]
            mock_database_connection["cursor"].fetchall.return_value = []

            before = datetime.now(timezone.utc)
            get_orphaned_scheduled_posts(lookback_hours=2)
            after = datetime.now(timezone.utc)

        cutoff_arg = mock_database_connection["cursor"].execute.call_args[0][1][0]
        # cutoff should be roughly (now - 2h)
        expected_lo = before - timedelta(hours=2, seconds=5)
        expected_hi = after - timedelta(hours=2) + timedelta(seconds=5)
        assert expected_lo <= cutoff_arg <= expected_hi

    def test_returns_empty_on_db_error(self, mock_database_connection):
        import mysql.connector

        from cqc_lem.utilities.db import get_orphaned_scheduled_posts

        with patch("cqc_lem.utilities.db.get_db_connection") as mock_conn:
            mock_conn.return_value = mock_database_connection["connection"]
            mock_database_connection["cursor"].execute.side_effect = mysql.connector.Error("err")

            result = get_orphaned_scheduled_posts()

        assert result == []
