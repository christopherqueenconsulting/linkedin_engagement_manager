"""Unit tests for the asset-backfill safety net + missing-asset guard."""

from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit


def _db(fetchone=None, fetchall=None):
    cur = MagicMock()
    cur.fetchone.return_value = fetchone
    cur.fetchall.return_value = fetchall or []
    conn = MagicMock()
    conn.cursor.return_value = cur
    return conn, cur


class TestDbQueries:
    def test_missing_assets_query(self):
        rows = [(6, 1, 'video', 'awareness', 't'), (5, 1, 'carousel', 'awareness', 't')]
        conn, _ = _db(fetchall=rows)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_unposted_posts_missing_assets
            assert get_unposted_posts_missing_assets() == rows

    def test_carousel_slides_getter(self):
        conn, _ = _db(fetchone={"carousel_slides": '["a"]'})
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_post_carousel_slides
            assert get_post_carousel_slides(5) == '["a"]'


class TestMissingAssetGuard:
    def test_video_without_url_is_missing(self):
        from cqc_lem.app.run_content_plan import _post_missing_required_asset
        assert _post_missing_required_asset(6, "video", None) is True
        assert _post_missing_required_asset(6, "video", "https://x.mp4") is False

    def test_carousel_slides_states(self):
        from cqc_lem.app.run_content_plan import _post_missing_required_asset
        cases = [
            (None, True),
            ('[]', True),
            ('', True),
            ('["Just A Text Title", "Another Title"]', True),  # text slides count as missing now
            ('["https://x/assets?file_name=images/carousel/5/slide_01.png"]', False),
            ('["/app/assets/slide_01.png"]', False),
        ]
        for val, missing in cases:
            with patch("cqc_lem.utilities.db.get_post_carousel_slides", return_value=val):
                assert _post_missing_required_asset(5, "carousel", None) is missing

    def test_text_never_missing(self):
        from cqc_lem.app.run_content_plan import _post_missing_required_asset
        assert _post_missing_required_asset(4, "text", None) is False


class TestBackfillTask:
    def test_enqueues_regen_per_type(self):
        rows = [(6, 1, 'video', 'awareness', 't'),
                (5, 1, 'carousel', 'awareness', 't'),
                (4, 1, 'text', 'awareness', 't')]
        vid, car = MagicMock(), MagicMock()
        with patch("cqc_lem.utilities.db.get_unposted_posts_missing_assets", return_value=rows), \
             patch("cqc_lem.app.run_content_plan.regenerate_post_video_task", vid), \
             patch("cqc_lem.app.run_content_plan.regenerate_post_carousel_task", car):
            from cqc_lem.app.run_scheduler import auto_backfill_missing_assets
            result = auto_backfill_missing_assets()
        vid.apply_async.assert_called_once_with(kwargs={'post_id': 6})
        car.apply_async.assert_called_once_with(kwargs={'post_id': 5})
        assert "Queued 2" in result

    def test_no_missing_posts(self):
        with patch("cqc_lem.utilities.db.get_unposted_posts_missing_assets", return_value=[]):
            from cqc_lem.app.run_scheduler import auto_backfill_missing_assets
            assert "Queued 0" in auto_backfill_missing_assets()
