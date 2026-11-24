"""`GET /posts/{post_id}` (MCP P3, plan F7) and its repository read, `get_post_for_user`."""

from contextlib import contextmanager
from typing import Any, Iterator
from unittest.mock import MagicMock, patch

import mysql.connector
import pytest

from cqc_lem.platform.db.repositories import posts as posts_repo
from cqc_lem.platform.db.shared import OwnershipUnprovable

pytestmark = pytest.mark.unit

_M = "cqc_lem.api.main"
_UID = 31
_ROW = {"id": 8, "content": "mine", "video_url": None, "image_url": None, "scheduled_time": None,
        "post_type": "text", "status": "pending", "carousel_slides": None,
        "authenticity_score": 72, "gate_reason": None, "rejection_reason": None,
        "archetype": "story", "manual_publish": 0, "curated_source_id": None,
        "source_treatment": None}


class TestTheRoute:
    def test_the_owner_gets_the_list_row_shape(self, api_client: Any) -> None:
        with patch(f"{_M}.get_session_user_id", return_value=_UID), \
             patch(f"{_M}.get_post_for_user", return_value=dict(_ROW)) as read:
            resp = api_client.get("/api/posts/8")
        assert resp.status_code == 200, resp.text
        read.assert_called_once_with(_UID, 8)
        detail = resp.json()["detail"]
        assert detail["post_id"] == 8 and detail["authenticity_score"] == 72
        assert detail["manual_publish"] is False and detail["curated"] is None

    def test_it_matches_a_list_row_exactly(self, api_client: Any) -> None:
        with patch(f"{_M}.get_session_user_id", return_value=_UID), \
             patch(f"{_M}.get_post_for_user", return_value=dict(_ROW)), \
             patch(f"{_M}.get_posts", return_value=([dict(_ROW)], 1)):
            one = api_client.get("/api/posts/8").json()["detail"]
            listed = api_client.get("/api/posts/").json()["detail"]["posts"][0]
        assert one == listed

    def test_a_curated_post_carries_its_source(self, api_client: Any) -> None:
        row = {**_ROW, "curated_source_id": 4, "source_treatment": "commentary"}
        with patch(f"{_M}.get_session_user_id", return_value=_UID), \
             patch(f"{_M}.get_post_for_user", return_value=row), \
             patch(f"{_M}.get_curated_summaries", return_value={4: {"platform": "web"}}) as cur:
            detail = api_client.get("/api/posts/8").json()["detail"]
        cur.assert_called_once_with(_UID, [4])
        assert detail["curated"]["source_id"] == 4

    def test_someone_elses_post_is_a_plain_404(self, api_client: Any) -> None:
        with patch(f"{_M}.get_session_user_id", return_value=_UID), \
             patch(f"{_M}.get_post_for_user", return_value=None):
            resp = api_client.get("/api/posts/9")
        assert resp.status_code == 404

    def test_a_database_fault_is_503_not_404(self, api_client: Any) -> None:
        with patch(f"{_M}.get_session_user_id", return_value=_UID), \
             patch(f"{_M}.get_post_for_user", side_effect=OwnershipUnprovable("down")):
            resp = api_client.get("/api/posts/8")
        assert resp.status_code == 503

    def test_no_session_is_401(self, api_client: Any) -> None:
        with patch(f"{_M}.get_session_user_id", return_value=None):
            assert api_client.get("/api/posts/8").status_code == 401

    def test_a_non_integer_id_is_422(self, api_client: Any) -> None:
        with patch(f"{_M}.get_session_user_id", return_value=_UID):
            assert api_client.get("/api/posts/abc").status_code == 422


@contextmanager
def _cursor(row: Any = None, raises: bool = False) -> Iterator[MagicMock]:
    cursor = MagicMock()
    cursor.fetchone.return_value = row
    if raises:
        cursor.execute.side_effect = mysql.connector.Error("down")

    @contextmanager
    def _db_cursor(**_kwargs: Any) -> Iterator[MagicMock]:
        yield cursor

    with patch.object(posts_repo, "db_cursor", _db_cursor):
        yield cursor


class TestTheRepository:
    def test_scoped_to_the_owner_in_sql(self) -> None:
        with _cursor(dict(_ROW)) as cursor:
            assert posts_repo.get_post_for_user(_UID, 8) == _ROW
        sql, params = cursor.execute.call_args.args
        assert "WHERE id = %s AND user_id = %s" in sql
        assert params == (8, _UID)

    def test_nothing_asked_is_none_without_a_query(self) -> None:
        with _cursor() as cursor:
            assert posts_repo.get_post_for_user(0, 8) is None
            assert posts_repo.get_post_for_user(_UID, 0) is None
        cursor.execute.assert_not_called()

    def test_a_database_error_raises_ownership_unprovable(self) -> None:
        with _cursor(raises=True), patch.object(posts_repo, "log_error") as logged:
            with pytest.raises(OwnershipUnprovable):
                posts_repo.get_post_for_user(_UID, 8)
        assert logged.called

    def test_the_facade_re_exports_it(self) -> None:
        from cqc_lem.utilities import db

        assert db.get_post_for_user is posts_repo.get_post_for_user
