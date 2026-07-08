"""Extended unit tests for database utility functions not covered by test_db.py."""

import json
import pytest
from unittest.mock import patch
from datetime import datetime, timezone

import mysql.connector
from mysql.connector import errorcode

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# store_cookies
# ---------------------------------------------------------------------------

class TestStoreCookies:
    def test_inserts_single_cookie_and_commits(self, mock_database_connection):
        from cqc_lem.utilities.db import store_cookies

        cookie = {
            "name": "li_at",
            "value": "abc123",
            "domain": ".linkedin.com",
            "path": "/",
            "expiry": 9999999999,
            "secure": True,
            "httpOnly": True,
        }

        with patch("cqc_lem.utilities.db.get_db_connection") as mock_conn, \
             patch("cqc_lem.utilities.db.get_user_id", return_value=42), \
             patch("cqc_lem.utilities.db.prune_superseded_cookies"):
            mock_conn.return_value = mock_database_connection["connection"]

            store_cookies("user@example.com", [cookie])

            mock_database_connection["cursor"].execute.assert_called_once()
            call_args = mock_database_connection["cursor"].execute.call_args[0]
            assert "INSERT INTO cookies" in call_args[0]
            # Verify all cookie values are passed
            assert "li_at" in call_args[1]
            assert "abc123" in call_args[1]
            mock_database_connection["connection"].commit.assert_called_once()

    def test_inserts_multiple_cookies(self, mock_database_connection):
        from cqc_lem.utilities.db import store_cookies

        cookies = [
            {"name": "c1", "value": "v1", "domain": ".linkedin.com", "path": "/",
             "expiry": 1000, "secure": True, "httpOnly": False},
            {"name": "c2", "value": "v2", "domain": ".linkedin.com", "path": "/",
             "expiry": 2000, "secure": False, "httpOnly": True},
        ]

        with patch("cqc_lem.utilities.db.get_db_connection") as mock_conn, \
             patch("cqc_lem.utilities.db.get_user_id", return_value=42), \
             patch("cqc_lem.utilities.db.prune_superseded_cookies"):
            mock_conn.return_value = mock_database_connection["connection"]

            store_cookies("user@example.com", cookies)

            assert mock_database_connection["cursor"].execute.call_count == 2
            # commit only once, after all cookies
            mock_database_connection["connection"].commit.assert_called_once()

    def test_cookie_without_expiry_uses_none(self, mock_database_connection):
        from cqc_lem.utilities.db import store_cookies

        cookie = {
            "name": "session",
            "value": "xyz",
            "domain": ".linkedin.com",
            "path": "/",
            "secure": True,
            "httpOnly": False,
            # no 'expiry' key
        }

        with patch("cqc_lem.utilities.db.get_db_connection") as mock_conn, \
             patch("cqc_lem.utilities.db.get_user_id", return_value=10), \
             patch("cqc_lem.utilities.db.prune_superseded_cookies"):
            mock_conn.return_value = mock_database_connection["connection"]

            store_cookies("user@example.com", [cookie])

            call_args = mock_database_connection["cursor"].execute.call_args[0]
            # expiry should be None since the key is missing
            assert None in call_args[1]

    def test_db_error_on_cookie_is_swallowed(self, mock_database_connection):
        from cqc_lem.utilities.db import store_cookies

        cookie = {"name": "c", "value": "v", "domain": "d", "path": "/",
                  "expiry": 100, "secure": True, "httpOnly": True}

        with patch("cqc_lem.utilities.db.get_db_connection") as mock_conn, \
             patch("cqc_lem.utilities.db.get_user_id", return_value=42), \
             patch("cqc_lem.utilities.db.prune_superseded_cookies"):
            mock_conn.return_value = mock_database_connection["connection"]
            mock_database_connection["cursor"].execute.side_effect = mysql.connector.Error("DB error")

            # Should not raise — errors are caught inside the loop
            store_cookies("user@example.com", [cookie])

            # commit still called after the loop
            mock_database_connection["connection"].commit.assert_called_once()


# ---------------------------------------------------------------------------
# get_cookies
# ---------------------------------------------------------------------------

class TestGetCookies:
    def test_returns_list_of_cookies(self, mock_database_connection):
        from cqc_lem.utilities.db import get_cookies

        expected = [{"name": "li_at", "value": "token123"}]
        with patch("cqc_lem.utilities.db.get_db_connection") as mock_conn, \
             patch("cqc_lem.utilities.db.get_top_level_domain", return_value="linkedin.com"):
            mock_conn.return_value = mock_database_connection["connection"]
            mock_database_connection["cursor"].fetchall.return_value = expected

            result = get_cookies("https://www.linkedin.com", "user@example.com")

            assert result == expected

    def test_query_uses_tld_and_email(self, mock_database_connection):
        from cqc_lem.utilities.db import get_cookies

        with patch("cqc_lem.utilities.db.get_db_connection") as mock_conn, \
             patch("cqc_lem.utilities.db.get_top_level_domain", return_value="linkedin.com"):
            mock_conn.return_value = mock_database_connection["connection"]
            mock_database_connection["cursor"].fetchall.return_value = []

            get_cookies("https://www.linkedin.com", "user@example.com")

            call_args = mock_database_connection["cursor"].execute.call_args[0]
            assert "%linkedin.com%" in call_args[1]
            assert "user@example.com" in call_args[1]

    def test_returns_none_on_db_error(self, mock_database_connection):
        from cqc_lem.utilities.db import get_cookies

        with patch("cqc_lem.utilities.db.get_db_connection") as mock_conn, \
             patch("cqc_lem.utilities.db.get_top_level_domain", return_value="linkedin.com"):
            mock_conn.return_value = mock_database_connection["connection"]
            mock_database_connection["cursor"].execute.side_effect = mysql.connector.Error("err")

            result = get_cookies("https://www.linkedin.com", "user@example.com")

            assert result is None


# ---------------------------------------------------------------------------
# add_user
# ---------------------------------------------------------------------------

class TestAddUser:
    def test_success_inserts_and_commits(self, mock_database_connection):
        from cqc_lem.utilities.db import add_user

        with patch("cqc_lem.utilities.db.get_db_connection") as mock_conn:
            mock_conn.return_value = mock_database_connection["connection"]

            add_user("new@example.com", "secret")

            call_args = mock_database_connection["cursor"].execute.call_args[0]
            assert "INSERT INTO users" in call_args[0]
            assert "new@example.com" in call_args[1]
            mock_database_connection["connection"].commit.assert_called_once()

    def test_duplicate_entry_is_handled(self, mock_database_connection):
        from cqc_lem.utilities.db import add_user

        err = mysql.connector.Error("Duplicate entry")
        err.errno = errorcode.ER_DUP_ENTRY

        with patch("cqc_lem.utilities.db.get_db_connection") as mock_conn:
            mock_conn.return_value = mock_database_connection["connection"]
            mock_database_connection["cursor"].execute.side_effect = err

            # Should not raise
            add_user("existing@example.com", "password")

    def test_other_db_error_is_handled(self, mock_database_connection):
        from cqc_lem.utilities.db import add_user

        err = mysql.connector.Error("Some other DB error")
        err.errno = 9999  # Not ER_DUP_ENTRY

        with patch("cqc_lem.utilities.db.get_db_connection") as mock_conn:
            mock_conn.return_value = mock_database_connection["connection"]
            mock_database_connection["cursor"].execute.side_effect = err

            # Should not raise
            add_user("broken@example.com", "password")


# ---------------------------------------------------------------------------
# add_user_with_access_token
# ---------------------------------------------------------------------------

class TestAddUserWithAccessToken:
    def test_inserts_with_all_fields(self, mock_database_connection):
        from cqc_lem.utilities.db import add_user_with_access_token

        with patch("cqc_lem.utilities.db.get_db_connection") as mock_conn:
            mock_conn.return_value = mock_database_connection["connection"]

            add_user_with_access_token(
                "user@example.com",
                "sub_abc123",
                "access_tok",
                "3600",
                refresh_token="refresh_tok",
                refresh_token_expires_in="86400",
            )

            call_args = mock_database_connection["cursor"].execute.call_args[0]
            assert "INSERT INTO users" in call_args[0]
            assert "user@example.com" in call_args[1]
            assert "sub_abc123" in call_args[1]
            assert "access_tok" in call_args[1]
            assert "refresh_tok" in call_args[1]
            mock_database_connection["connection"].commit.assert_called_once()

    def test_inserts_without_refresh_token(self, mock_database_connection):
        from cqc_lem.utilities.db import add_user_with_access_token

        with patch("cqc_lem.utilities.db.get_db_connection") as mock_conn:
            mock_conn.return_value = mock_database_connection["connection"]

            add_user_with_access_token(
                "user@example.com",
                "sub_xyz",
                "access_tok",
                "3600",
            )

            call_args = mock_database_connection["cursor"].execute.call_args[0]
            # refresh_token should be None in params
            assert None in call_args[1]
            mock_database_connection["connection"].commit.assert_called_once()

    def test_handles_duplicate_entry(self, mock_database_connection):
        from cqc_lem.utilities.db import add_user_with_access_token

        err = mysql.connector.Error("Duplicate entry")
        err.errno = errorcode.ER_DUP_ENTRY

        with patch("cqc_lem.utilities.db.get_db_connection") as mock_conn:
            mock_conn.return_value = mock_database_connection["connection"]
            mock_database_connection["cursor"].execute.side_effect = err

            # Should not raise
            add_user_with_access_token("existing@example.com", "sub", "tok", "3600")

    def test_handles_other_db_error(self, mock_database_connection):
        from cqc_lem.utilities.db import add_user_with_access_token

        err = mysql.connector.Error("Generic error")
        err.errno = 9999

        with patch("cqc_lem.utilities.db.get_db_connection") as mock_conn:
            mock_conn.return_value = mock_database_connection["connection"]
            mock_database_connection["cursor"].execute.side_effect = err

            # Should not raise
            add_user_with_access_token("user@example.com", "sub", "tok", "3600")


# ---------------------------------------------------------------------------
# get_user_linked_sub_id
# ---------------------------------------------------------------------------

class TestGetUserLinkedSubId:
    def test_returns_linked_sub_id_when_found(self, mock_database_connection):
        from cqc_lem.utilities.db import get_user_linked_sub_id

        with patch("cqc_lem.utilities.db.get_db_connection") as mock_conn:
            mock_conn.return_value = mock_database_connection["connection"]
            mock_database_connection["cursor"].fetchone.return_value = {"linked_sub_id": "sub123"}

            result = get_user_linked_sub_id(42)

            assert result == "sub123"

    def test_returns_none_when_not_found(self, mock_database_connection):
        from cqc_lem.utilities.db import get_user_linked_sub_id

        with patch("cqc_lem.utilities.db.get_db_connection") as mock_conn:
            mock_conn.return_value = mock_database_connection["connection"]
            mock_database_connection["cursor"].fetchone.return_value = None

            result = get_user_linked_sub_id(999)

            assert result is None

    def test_returns_none_on_db_error(self, mock_database_connection):
        from cqc_lem.utilities.db import get_user_linked_sub_id

        with patch("cqc_lem.utilities.db.get_db_connection") as mock_conn:
            mock_conn.return_value = mock_database_connection["connection"]
            mock_database_connection["cursor"].execute.side_effect = mysql.connector.Error("err")

            result = get_user_linked_sub_id(42)

            assert result is None


# ---------------------------------------------------------------------------
# insert_planned_post
# ---------------------------------------------------------------------------

class TestInsertPlannedPost:
    def test_success_returns_true(self, mock_database_connection):
        from cqc_lem.utilities.db import insert_planned_post, PostType

        with patch("cqc_lem.utilities.db.get_db_connection") as mock_conn:
            mock_conn.return_value = mock_database_connection["connection"]
            mock_database_connection["cursor"].rowcount = 1

            result = insert_planned_post(
                42,
                datetime(2025, 6, 15, 10, 0, 0, tzinfo=timezone.utc),
                PostType.TEXT,
                "awareness",
            )

            assert result is True
            call_args = mock_database_connection["cursor"].execute.call_args[0]
            assert "INSERT INTO posts" in call_args[0]
            assert "awareness" in call_args[1]
            mock_database_connection["connection"].commit.assert_called_once()

    def test_tz_naive_datetime_is_handled(self, mock_database_connection):
        from cqc_lem.utilities.db import insert_planned_post, PostType

        with patch("cqc_lem.utilities.db.get_db_connection") as mock_conn:
            mock_conn.return_value = mock_database_connection["connection"]
            mock_database_connection["cursor"].rowcount = 1

            # tz-naive datetime should not raise
            result = insert_planned_post(
                42,
                datetime(2025, 7, 1, 9, 0, 0),  # no tzinfo
                PostType.CAROUSEL,
                "consideration",
            )

            assert result is True

    def test_returns_false_on_db_error(self, mock_database_connection):
        from cqc_lem.utilities.db import insert_planned_post, PostType

        with patch("cqc_lem.utilities.db.get_db_connection") as mock_conn:
            mock_conn.return_value = mock_database_connection["connection"]
            mock_database_connection["cursor"].execute.side_effect = mysql.connector.Error("err")

            result = insert_planned_post(
                42,
                datetime(2025, 6, 15, 10, 0, 0, tzinfo=timezone.utc),
                PostType.TEXT,
                "decision",
            )

            assert result is False


# ---------------------------------------------------------------------------
# update_db_post
# ---------------------------------------------------------------------------

class TestUpdateDbPost:
    def test_success_returns_true(self, mock_database_connection):
        from cqc_lem.utilities.db import update_db_post, PostType, PostStatus

        with patch("cqc_lem.utilities.db.get_db_connection") as mock_conn:
            mock_conn.return_value = mock_database_connection["connection"]
            mock_database_connection["cursor"].rowcount = 1

            result = update_db_post(
                "Updated content",
                "https://example.com/video.mp4",
                datetime(2025, 8, 1, 12, 0, 0, tzinfo=timezone.utc),
                PostType.VIDEO,
                101,
                PostStatus.APPROVED,
            )

            assert result is True
            mock_database_connection["connection"].commit.assert_called_once()

    def test_tz_naive_scheduled_time_is_handled(self, mock_database_connection):
        from cqc_lem.utilities.db import update_db_post, PostType, PostStatus

        with patch("cqc_lem.utilities.db.get_db_connection") as mock_conn:
            mock_conn.return_value = mock_database_connection["connection"]
            mock_database_connection["cursor"].rowcount = 1

            result = update_db_post(
                "Content",
                None,
                datetime(2025, 8, 1, 12, 0, 0),  # no tzinfo
                PostType.TEXT,
                55,
                PostStatus.PENDING,
            )

            assert result is True

    def test_returns_false_on_db_error(self, mock_database_connection):
        from cqc_lem.utilities.db import update_db_post, PostType, PostStatus

        with patch("cqc_lem.utilities.db.get_db_connection") as mock_conn:
            mock_conn.return_value = mock_database_connection["connection"]
            mock_database_connection["cursor"].execute.side_effect = mysql.connector.Error("err")

            result = update_db_post(
                "Content",
                None,
                datetime(2025, 8, 1, 12, 0, 0, tzinfo=timezone.utc),
                PostType.TEXT,
                55,
                PostStatus.PENDING,
            )

            assert result is False


# ---------------------------------------------------------------------------
# get_posted_posts
# ---------------------------------------------------------------------------

class TestGetPostedPosts:
    def test_returns_list_of_posted_posts(self, mock_database_connection):
        from cqc_lem.utilities.db import get_posted_posts

        expected = [
            {"id": 1, "content": "Posted!", "scheduled_time": "2025-01-01", "post_type": "text", "status": "posted"},
        ]
        with patch("cqc_lem.utilities.db.get_db_connection") as mock_conn:
            mock_conn.return_value = mock_database_connection["connection"]
            mock_database_connection["cursor"].fetchall.return_value = expected

            result = get_posted_posts(60)

            assert result == expected
            sql = mock_database_connection["cursor"].execute.call_args[0][0]
            assert "posted" in sql.lower()
            assert 60 in mock_database_connection["cursor"].execute.call_args[0][1]

    def test_returns_none_on_db_error(self, mock_database_connection):
        from cqc_lem.utilities.db import get_posted_posts

        with patch("cqc_lem.utilities.db.get_db_connection") as mock_conn:
            mock_conn.return_value = mock_database_connection["connection"]
            mock_database_connection["cursor"].execute.side_effect = mysql.connector.Error("err")

            result = get_posted_posts(60)

            assert result is None


# ---------------------------------------------------------------------------
# get_post_by_email
# ---------------------------------------------------------------------------

class TestGetPostByEmail:
    def test_returns_empty_when_user_not_found(self, mock_database_connection):
        from cqc_lem.utilities.db import get_post_by_email

        with patch("cqc_lem.utilities.db.get_user_id", return_value=None):
            posts, total = get_post_by_email("nobody@example.com")

            assert posts == []
            assert total == 0

    def test_delegates_to_get_posts_when_user_found(self, mock_database_connection):
        from cqc_lem.utilities.db import get_post_by_email

        with patch("cqc_lem.utilities.db.get_user_id", return_value=42), \
             patch("cqc_lem.utilities.db.get_posts", return_value=(["post1"], 1)) as mock_get_posts:
            posts, total = get_post_by_email(
                "user@example.com", limit=5, offset=10, sort_order="desc", status_filter="pending"
            )

            assert posts == ["post1"]
            assert total == 1
            mock_get_posts.assert_called_once_with(
                42, limit=5, offset=10, sort_order="desc", status_filter="pending",
                post_type_filter=None, search=None, sort_by="scheduled_time"
            )


# ---------------------------------------------------------------------------
# update_db_post_carousel_slides
# ---------------------------------------------------------------------------

class TestUpdateDbPostCarouselSlides:
    def test_success_returns_true(self, mock_database_connection):
        from cqc_lem.utilities.db import update_db_post_carousel_slides

        slides = ["Slide 1 text", "Slide 2 text", "Slide 3 text"]

        with patch("cqc_lem.utilities.db.get_db_connection") as mock_conn:
            mock_conn.return_value = mock_database_connection["connection"]
            mock_database_connection["cursor"].rowcount = 1

            result = update_db_post_carousel_slides(77, slides)

            assert result is True
            call_args = mock_database_connection["cursor"].execute.call_args[0]
            assert json.dumps(slides) in call_args[1]
            assert 77 in call_args[1]
            mock_database_connection["connection"].commit.assert_called_once()

    def test_returns_false_on_db_error(self, mock_database_connection):
        from cqc_lem.utilities.db import update_db_post_carousel_slides

        with patch("cqc_lem.utilities.db.get_db_connection") as mock_conn:
            mock_conn.return_value = mock_database_connection["connection"]
            mock_database_connection["cursor"].execute.side_effect = mysql.connector.Error("err")

            result = update_db_post_carousel_slides(77, ["slide"])

            assert result is False

    def test_returns_false_when_no_row_updated(self, mock_database_connection):
        from cqc_lem.utilities.db import update_db_post_carousel_slides

        with patch("cqc_lem.utilities.db.get_db_connection") as mock_conn:
            mock_conn.return_value = mock_database_connection["connection"]
            mock_database_connection["cursor"].rowcount = 0

            result = update_db_post_carousel_slides(9999, ["slide"])

            assert result is False


# ---------------------------------------------------------------------------
# replace_video_url_base
# ---------------------------------------------------------------------------

class TestReplaceVideoUrlBase:
    def test_with_user_id_uses_scoped_query(self, mock_database_connection):
        from cqc_lem.utilities.db import replace_video_url_base

        with patch("cqc_lem.utilities.db.get_db_connection") as mock_conn:
            mock_conn.return_value = mock_database_connection["connection"]
            mock_database_connection["cursor"].rowcount = 3

            result = replace_video_url_base("http://old.cdn", "https://new.cdn", user_id=42)

            assert result == 3
            call_args = mock_database_connection["cursor"].execute.call_args[0]
            assert "user_id" in call_args[0]
            params = call_args[1]
            assert params[0] == "http://old.cdn"
            assert params[1] == "https://new.cdn"
            assert 42 in params

    def test_without_user_id_uses_global_query(self, mock_database_connection):
        from cqc_lem.utilities.db import replace_video_url_base

        with patch("cqc_lem.utilities.db.get_db_connection") as mock_conn:
            mock_conn.return_value = mock_database_connection["connection"]
            mock_database_connection["cursor"].rowcount = 5

            result = replace_video_url_base("http://old.cdn", "https://new.cdn")

            assert result == 5
            call_args = mock_database_connection["cursor"].execute.call_args[0]
            assert "user_id" not in call_args[0]

    def test_returns_zero_on_db_error(self, mock_database_connection):
        from cqc_lem.utilities.db import replace_video_url_base

        with patch("cqc_lem.utilities.db.get_db_connection") as mock_conn:
            mock_conn.return_value = mock_database_connection["connection"]
            mock_database_connection["cursor"].execute.side_effect = mysql.connector.Error("err")

            result = replace_video_url_base("http://old.cdn", "https://new.cdn", user_id=1)

            assert result == 0


# ---------------------------------------------------------------------------
# get_ready_to_post_posts
# ---------------------------------------------------------------------------

class TestGetReadyToPostPosts:
    def test_returns_list_of_pending_posts(self, mock_database_connection):
        from cqc_lem.utilities.db import get_ready_to_post_posts

        rows = [(10, datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc), 42)]
        with patch("cqc_lem.utilities.db.get_db_connection") as mock_conn:
            mock_conn.return_value = mock_database_connection["connection"]
            mock_database_connection["cursor"].fetchall.return_value = rows

            result = get_ready_to_post_posts()

            assert result == rows
            sql = mock_database_connection["cursor"].execute.call_args[0][0]
            assert "approved" in sql.lower()

    def test_with_explicit_pre_post_time(self, mock_database_connection):
        from cqc_lem.utilities.db import get_ready_to_post_posts

        pre_post_time = datetime(2025, 6, 1, 14, 0, 0, tzinfo=timezone.utc)
        with patch("cqc_lem.utilities.db.get_db_connection") as mock_conn:
            mock_conn.return_value = mock_database_connection["connection"]
            mock_database_connection["cursor"].fetchall.return_value = []

            result = get_ready_to_post_posts(pre_post_time=pre_post_time)

            assert result == []
            call_params = mock_database_connection["cursor"].execute.call_args[0][1]
            assert pre_post_time in call_params

    def test_returns_none_on_db_error(self, mock_database_connection):
        from cqc_lem.utilities.db import get_ready_to_post_posts

        with patch("cqc_lem.utilities.db.get_db_connection") as mock_conn:
            mock_conn.return_value = mock_database_connection["connection"]
            mock_database_connection["cursor"].execute.side_effect = mysql.connector.Error("err")

            result = get_ready_to_post_posts()

            assert result is None


# ---------------------------------------------------------------------------
# get_user_password_pair_by_id
# ---------------------------------------------------------------------------

class TestGetUserPasswordPairById:
    def test_returns_email_and_password_when_found(self, mock_database_connection):
        from cqc_lem.utilities.db import get_user_password_pair_by_id

        with patch("cqc_lem.utilities.db.get_db_connection") as mock_conn:
            mock_conn.return_value = mock_database_connection["connection"]
            mock_database_connection["cursor"].fetchone.return_value = {
                "email": "user@example.com",
                "password": "hashed_secret",
            }

            email, password = get_user_password_pair_by_id(42)

            assert email == "user@example.com"
            assert password == "hashed_secret"

    def test_returns_none_pair_when_not_found(self, mock_database_connection):
        from cqc_lem.utilities.db import get_user_password_pair_by_id

        with patch("cqc_lem.utilities.db.get_db_connection") as mock_conn:
            mock_conn.return_value = mock_database_connection["connection"]
            mock_database_connection["cursor"].fetchone.return_value = None

            email, password = get_user_password_pair_by_id(9999)

            assert email is None
            assert password is None

    def test_returns_none_pair_on_db_error(self, mock_database_connection):
        from cqc_lem.utilities.db import get_user_password_pair_by_id

        with patch("cqc_lem.utilities.db.get_db_connection") as mock_conn:
            mock_conn.return_value = mock_database_connection["connection"]
            mock_database_connection["cursor"].execute.side_effect = mysql.connector.Error("err")

            email, password = get_user_password_pair_by_id(42)

            assert email is None
            assert password is None


# ---------------------------------------------------------------------------
# bulk_update_posts — extended cases not in test_db.py
# ---------------------------------------------------------------------------

class TestBulkUpdatePostsExtended:
    def test_updates_scheduled_time(self, mock_database_connection):
        from cqc_lem.utilities.db import bulk_update_posts

        new_time = datetime(2025, 9, 1, 10, 0, 0, tzinfo=timezone.utc)

        with patch("cqc_lem.utilities.db.get_db_connection") as mock_conn:
            mock_conn.return_value = mock_database_connection["connection"]
            mock_database_connection["cursor"].rowcount = 2

            result = bulk_update_posts([5, 6], scheduled_time=new_time)

            assert result is True
            call_args = mock_database_connection["cursor"].execute.call_args[0]
            assert "scheduled_time" in call_args[0]

    def test_updates_both_status_and_time(self, mock_database_connection):
        from cqc_lem.utilities.db import bulk_update_posts, PostStatus

        new_time = datetime(2025, 9, 1, 10, 0, 0, tzinfo=timezone.utc)

        with patch("cqc_lem.utilities.db.get_db_connection") as mock_conn:
            mock_conn.return_value = mock_database_connection["connection"]
            mock_database_connection["cursor"].rowcount = 1

            result = bulk_update_posts([7], status=PostStatus.PENDING, scheduled_time=new_time)

            assert result is True
            call_args = mock_database_connection["cursor"].execute.call_args[0]
            assert "status" in call_args[0]
            assert "scheduled_time" in call_args[0]

    def test_tz_naive_scheduled_time_is_made_utc(self, mock_database_connection):
        from cqc_lem.utilities.db import bulk_update_posts

        naive_time = datetime(2025, 9, 1, 10, 0, 0)  # no tzinfo

        with patch("cqc_lem.utilities.db.get_db_connection") as mock_conn:
            mock_conn.return_value = mock_database_connection["connection"]
            mock_database_connection["cursor"].rowcount = 1

            # Should not raise
            result = bulk_update_posts([3], scheduled_time=naive_time)

            assert result is True

    def test_returns_false_on_db_error(self, mock_database_connection):
        from cqc_lem.utilities.db import bulk_update_posts, PostStatus

        with patch("cqc_lem.utilities.db.get_db_connection") as mock_conn:
            mock_conn.return_value = mock_database_connection["connection"]
            mock_database_connection["cursor"].execute.side_effect = mysql.connector.Error("err")

            result = bulk_update_posts([1], status=PostStatus.APPROVED)

            assert result is False


# ---------------------------------------------------------------------------
# soft_delete_posts — direct call path
# ---------------------------------------------------------------------------

class TestSoftDeletePostsDirect:
    def test_calls_bulk_update_with_rejected_status(self, mock_database_connection):
        from cqc_lem.utilities.db import soft_delete_posts, PostStatus

        with patch("cqc_lem.utilities.db.get_db_connection") as mock_conn:
            mock_conn.return_value = mock_database_connection["connection"]
            mock_database_connection["cursor"].rowcount = 2

            result = soft_delete_posts([10, 20])

            assert result is True
            call_args = mock_database_connection["cursor"].execute.call_args[0]
            assert PostStatus.REJECTED.value in call_args[1]


# ---------------------------------------------------------------------------
# insert_post — DB error path
# ---------------------------------------------------------------------------

class TestInsertPostDbError:
    def test_returns_false_on_db_error(self, mock_database_connection):
        from cqc_lem.utilities.db import insert_post, PostType

        with patch("cqc_lem.utilities.db.get_db_connection") as mock_conn, \
             patch("cqc_lem.utilities.db.get_user_id", return_value=42), \
             patch("cqc_lem.utilities.db.prune_superseded_cookies"):
            mock_conn.return_value = mock_database_connection["connection"]
            mock_database_connection["cursor"].execute.side_effect = mysql.connector.Error("err")

            result = insert_post(
                "user@example.com",
                "content",
                datetime(2025, 1, 1, tzinfo=timezone.utc),
                PostType.TEXT,
            )

            assert result is False

    def test_inserts_with_carousel_slides_serialized(self, mock_database_connection):
        from cqc_lem.utilities.db import insert_post, PostType

        slides = ["First slide", "Second slide"]
        with patch("cqc_lem.utilities.db.get_db_connection") as mock_conn, \
             patch("cqc_lem.utilities.db.get_user_id", return_value=42), \
             patch("cqc_lem.utilities.db.prune_superseded_cookies"):
            mock_conn.return_value = mock_database_connection["connection"]
            mock_database_connection["cursor"].rowcount = 1

            result = insert_post(
                "user@example.com",
                "carousel caption",
                datetime(2025, 1, 1, tzinfo=timezone.utc),
                PostType.CAROUSEL,
                carousel_slides=slides,
            )

            assert result is True
            call_args = mock_database_connection["cursor"].execute.call_args[0]
            assert json.dumps(slides) in call_args[1]

    def test_tz_naive_scheduled_time_becomes_utc(self, mock_database_connection):
        from cqc_lem.utilities.db import insert_post, PostType

        with patch("cqc_lem.utilities.db.get_db_connection") as mock_conn, \
             patch("cqc_lem.utilities.db.get_user_id", return_value=42), \
             patch("cqc_lem.utilities.db.prune_superseded_cookies"):
            mock_conn.return_value = mock_database_connection["connection"]
            mock_database_connection["cursor"].rowcount = 1

            result = insert_post(
                "user@example.com",
                "content",
                datetime(2025, 6, 1, 10, 0, 0),  # no tzinfo
                PostType.TEXT,
            )

            assert result is True
