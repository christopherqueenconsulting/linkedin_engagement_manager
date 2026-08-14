"""Unit tests for the content_quality_scores DB helpers (issue #630): reading the day's shipped
content off all three surfaces, upserting one scored row, and reading the two-period history back for
the weekly rollup.
"""

from datetime import date
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit


class TestGetShippedContentForQuality:
    def _batches(self):
        return [
            [{"id": 5, "content": "Post body", "archetype": "build_receipt",
              "post_type": "text", "video_url": None,
              "authenticity_score": 91, "shipped_on": date(2026, 7, 26), "reactions": 12,
              "comments": 3, "reposts": 1, "impressions": 2200}],
            [{"id": 71, "message": "Comment body", "shipped_on": date(2026, 7, 26)}],
            [{"id": 9, "body": "Edition body", "format": "deep_dive",
              "shipped_on": date(2026, 7, 25)}],
        ]

    def test_returns_all_three_surfaces_in_one_stream(self, fake_cursor):
        from cqc_lem.utilities.db import get_shipped_content_for_quality
        conn, cur = fake_cursor(fetch_all_side_effect=self._batches())
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            rows = get_shipped_content_for_quality(1, days=2)
        assert [r["surface"] for r in rows] == ["post", "comment", "newsletter"]
        assert cur.execute.call_count == 3

    def test_post_rows_carry_the_stats_and_the_stored_authenticity_score(self, fake_cursor):
        from cqc_lem.utilities.db import get_shipped_content_for_quality
        conn, _ = fake_cursor(fetch_all_side_effect=self._batches())
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            post = get_shipped_content_for_quality(1)[0]
        assert post["ref_id"] == "5" and post["text"] == "Post body"
        assert post["format_key"] == "build_receipt"
        assert post["post_type"] == "text"
        assert post["video_url"] is None
        assert post["authenticity_score"] == 91
        assert post["impressions"] == 2200

    def test_comment_and_newsletter_rows_have_no_per_item_engagement(self, fake_cursor):
        from cqc_lem.utilities.db import get_shipped_content_for_quality
        conn, _ = fake_cursor(fetch_all_side_effect=self._batches())
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            _post, comment, edition = get_shipped_content_for_quality(1)
        assert comment["impressions"] is None and comment["authenticity_score"] is None
        assert edition["format_key"] == "deep_dive"
        assert edition["impressions"] is None

    def test_posts_are_left_joined_so_an_unscraped_post_is_still_returned(self, fake_cursor):
        from cqc_lem.utilities.db import get_shipped_content_for_quality
        conn, cur = fake_cursor(fetch_all_side_effect=[[], [], []])
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            get_shipped_content_for_quality(1)
        assert "LEFT JOIN post_stats" in cur.execute.call_args_list[0][0][0]

    def test_window_is_floored_at_one_day(self, fake_cursor):
        from cqc_lem.utilities.db import get_shipped_content_for_quality
        conn, cur = fake_cursor(fetch_all_side_effect=[[], [], []])
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            get_shipped_content_for_quality(1, days=0)
        assert cur.execute.call_args_list[0][0][1][-1] == 1

    def test_only_successful_comment_logs_are_read(self, fake_cursor):
        from cqc_lem.utilities.db import get_shipped_content_for_quality
        conn, cur = fake_cursor(fetch_all_side_effect=[[], [], []])
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            get_shipped_content_for_quality(1)
        sql, params = cur.execute.call_args_list[1][0]
        assert "FROM logs" in sql
        assert params[1:3] == ("comment", "success")

    def test_partial_results_survive_a_db_error_midway(self, fake_cursor):
        # The three queries share a connection; a failure on the newsletter read must not throw away
        # the posts and comments already collected.
        import mysql.connector

        from cqc_lem.utilities.db import get_shipped_content_for_quality
        batches = self._batches()
        conn, cur = fake_cursor(fetch_all_side_effect=[batches[0], batches[1]])
        cur.execute.side_effect = [None, None, mysql.connector.Error("boom")]
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            rows = get_shipped_content_for_quality(1)
        assert [r["surface"] for r in rows] == ["post", "comment"]

    def test_empty_on_a_first_query_error(self, fake_cursor):
        import mysql.connector

        from cqc_lem.utilities.db import get_shipped_content_for_quality
        conn, cur = fake_cursor(fetch_all_side_effect=[[], [], []])
        cur.execute.side_effect = mysql.connector.Error("boom")
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            assert get_shipped_content_for_quality(1) == []


class TestRecordContentQualityScore:
    def _score(self, **kw):
        score = {"surface": "post", "ref_id": "5", "shipped_on": date(2026, 7, 26),
                 "slop_hard": 1, "slop_warn": 2, "slop_score": 5.0, "similarity": 0.42,
                 "similarity_measure": "embedding", "authenticity_score": 88, "hook_chars": 92,
                 "hook_within_budget": True, "engagement_rate": 0.021, "impressions": 2200,
                 "detector_score": None, "detector_provider": None,
                 "slop_checks": ["tada_transition"],
                 "video_render_ok": True, "video_model_tier": "gen4_turbo",
                 "video_duration_seconds": 5, "video_aspect_ratio": "9:16",
                 "video_asset_probe": "ok"}
        score.update(kw)
        return score

    def test_upserts_the_row(self, fake_cursor):
        from cqc_lem.utilities.db import record_content_quality_score
        conn, cur = fake_cursor()
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            assert record_content_quality_score(1, self._score()) is True
        sql, params = cur.execute.call_args[0]
        assert "ON DUPLICATE KEY UPDATE" in sql
        assert params[0:3] == (1, "post", "5")
        assert conn.commit.called

    def test_the_slop_checks_are_stored_as_json(self, fake_cursor):
        from cqc_lem.utilities.db import record_content_quality_score
        conn, cur = fake_cursor()
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            record_content_quality_score(1, self._score())
        assert cur.execute.call_args[0][1][16] == '["tada_transition"]'

    def test_video_dimensions_are_written_to_their_columns(self, fake_cursor):
        from cqc_lem.utilities.db import record_content_quality_score
        conn, cur = fake_cursor()
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            record_content_quality_score(1, self._score())
        params = cur.execute.call_args[0][1]
        assert params[17] == 1
        assert params[18] == "gen4_turbo"
        assert params[19] == 5
        assert params[20] == "9:16"
        assert params[21] == "ok"

    def test_unmeasured_dimensions_are_written_as_null(self, fake_cursor):
        from cqc_lem.utilities.db import record_content_quality_score
        conn, cur = fake_cursor()
        score = self._score(slop_hard=None, slop_warn=None, slop_score=None, similarity=None,
                            similarity_measure=None, authenticity_score=None,
                            hook_within_budget=None, engagement_rate=None, impressions=None,
                            video_render_ok=None, video_model_tier=None,
                            video_duration_seconds=None, video_aspect_ratio=None,
                            video_asset_probe=None)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            record_content_quality_score(1, score)
        params = cur.execute.call_args[0][1]
        # A 0 here would read as "clean" / "no reach" instead of "not scored".
        assert params[4:9] == (None, None, None, None, None)
        assert None in params[9:14]
        assert params[17:22] == (None, None, None, None, None)

    def test_a_false_hook_budget_is_written_as_zero_not_null(self, fake_cursor):
        from cqc_lem.utilities.db import record_content_quality_score
        conn, cur = fake_cursor()
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            record_content_quality_score(1, self._score(hook_within_budget=False))
        assert 0 in cur.execute.call_args[0][1]

    def test_a_row_with_no_ref_id_is_refused(self, fake_cursor):
        from cqc_lem.utilities.db import record_content_quality_score
        conn, cur = fake_cursor()
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn) as get_conn:
            assert record_content_quality_score(1, self._score(ref_id="")) is False
        assert not get_conn.called and not cur.execute.called

    def test_false_on_db_error(self, fake_cursor):
        import mysql.connector

        from cqc_lem.utilities.db import record_content_quality_score
        conn, cur = fake_cursor()
        cur.execute.side_effect = mysql.connector.Error("boom")
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            assert record_content_quality_score(1, self._score()) is False


class TestGetContentQualityScores:
    def test_returns_rows_with_numeric_decimals_coerced(self, fake_cursor):
        from decimal import Decimal

        from cqc_lem.utilities.db import get_content_quality_scores
        rows = [{"surface": "post", "ref_id": "5", "shipped_on": date(2026, 7, 26),
                 "slop_score": Decimal("5.000"), "similarity": Decimal("0.4200"),
                 "engagement_rate": Decimal("0.02100000"),
                 "video_render_ok": 1, "video_model_tier": "gen4_turbo",
                 "video_duration_seconds": 5, "video_aspect_ratio": "9:16",
                 "video_asset_probe": "ok"}]
        conn, cur = fake_cursor(fetch_all=rows)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            out = get_content_quality_scores(1, days=14)
        assert out[0]["slop_score"] == 5.0
        assert out[0]["similarity"] == 0.42
        assert out[0]["engagement_rate"] == 0.021
        assert out[0]["video_render_ok"] is True
        assert out[0]["video_model_tier"] == "gen4_turbo"
        assert cur.execute.call_args[0][1] == (1, 14)

    def test_unmeasured_decimals_stay_none(self, fake_cursor):
        from cqc_lem.utilities.db import get_content_quality_scores
        rows = [{"surface": "comment", "slop_score": None, "similarity": None,
                 "engagement_rate": None}]
        conn, _ = fake_cursor(fetch_all=rows)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            out = get_content_quality_scores(1)
        assert out[0]["slop_score"] is None and out[0]["engagement_rate"] is None

    def test_empty_when_the_table_is_not_there_yet(self, fake_cursor):
        import mysql.connector

        from cqc_lem.utilities.db import get_content_quality_scores
        conn, cur = fake_cursor()
        cur.execute.side_effect = mysql.connector.Error("no such table")
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            assert get_content_quality_scores(1) == []
