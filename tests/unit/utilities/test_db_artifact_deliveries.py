"""Issue #624 — owned-asset delivery counts, the attribution side of the CTA loop."""

import mysql.connector
import pytest
from unittest.mock import MagicMock, patch

pytestmark = pytest.mark.unit

_DB = "cqc_lem.utilities.db"


def _mock_conn(rows):
    conn = MagicMock(); cur = MagicMock()
    cur.fetchone.side_effect = list(rows)
    conn.cursor.return_value = cur
    return conn, cur


class TestCountArtifactCtaDeliveries:
    def test_counts_both_mechanics(self):
        conn, cur = _mock_conn([(4,), (2,)])
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import count_artifact_cta_deliveries
            out = count_artifact_cta_deliveries(1, days=30, newsletter_url="https://li/news")
        assert out == {"window_days": 30, "lead_magnet_dms": 4, "newsletter_links": 2}
        # The DM side counts only what THIS automation drafted, never operator-written DMs.
        from cqc_lem.utilities.db import SCHEDULED_DM_SOURCE_ARTIFACT
        assert cur.execute.call_args_list[0][0][1][1] == SCHEDULED_DM_SOURCE_ARTIFACT
        # The link side matches posts that carried the subscribe URL, in EITHER column.
        assert cur.execute.call_args_list[1][0][1][2] == "%https://li/news%"
        sql = cur.execute.call_args_list[1][0][0]
        assert "content LIKE" in sql and "first_comment_link LIKE" in sql

    def test_a_linkedin_newsletter_is_counted_from_the_body(self):
        """`newsletter_url` is a linkedin.com article URL (mark_newsletter_published writes it from
        the publish flow's current_url), and #392's split leaves in-platform links in the BODY — so
        a first_comment_link-only count would report 0 forever for the mainline newsletter."""
        from cqc_lem.utilities.ai.content_alignment import (artifact_cta_line,
                                                            split_link_for_first_comment)
        url = "https://www.linkedin.com/newsletters/the-build-log-7123/"
        body = "Here is what I learned.\n\n" + artifact_cta_line(
            newsletter={"enabled": True, "title": "The Build Log", "newsletter_url": url})
        stripped, carried = split_link_for_first_comment(body)
        assert carried == [] and url in stripped   # stays in the body: no in-platform penalty

        conn, cur = _mock_conn([(0,), (3,)])
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import count_artifact_cta_deliveries
            out = count_artifact_cta_deliveries(1, newsletter_url=url)
        assert out["newsletter_links"] == 3
        params = cur.execute.call_args_list[1][0][1]
        assert params[2] == params[3] == f"%{url}%"   # both columns get the same literal pattern

    def test_percent_encoding_in_the_url_is_not_a_wildcard(self):
        """An unescaped '%' inside the LIKE pattern matches ANY text — silent over-counting."""
        conn, cur = _mock_conn([(0,), (0,)])
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import count_artifact_cta_deliveries
            count_artifact_cta_deliveries(1, newsletter_url="https://x.io/a%20b_c")
        assert cur.execute.call_args_list[1][0][1][2] == "%https://x.io/a!%20b!_c%"
        assert "ESCAPE '!'" in cur.execute.call_args_list[1][0][0]

    def test_no_newsletter_url_reports_none_not_zero(self):
        """Nothing to carry is a different fact from carried nothing — don't fake a zero."""
        conn, cur = _mock_conn([(1,)])
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import count_artifact_cta_deliveries
            out = count_artifact_cta_deliveries(1)
        assert out["newsletter_links"] is None and out["lead_magnet_dms"] == 1
        assert cur.execute.call_count == 1
        assert out["window_days"] == 90

    def test_blank_url_is_not_a_wildcard_query(self):
        conn, cur = _mock_conn([(0,)])
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import count_artifact_cta_deliveries
            count_artifact_cta_deliveries(1, newsletter_url="   ")
        assert cur.execute.call_count == 1   # a blank URL would have matched every post

    def test_window_floors_at_one_day(self):
        conn, cur = _mock_conn([(0,)])
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import count_artifact_cta_deliveries
            out = count_artifact_cta_deliveries(1, days=0)
        assert out["window_days"] == 1
        assert cur.execute.call_args_list[0][0][1][2] == 1

    def test_db_error_returns_the_empty_shape(self):
        conn = MagicMock()
        conn.cursor.return_value.execute.side_effect = mysql.connector.Error("boom")
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import count_artifact_cta_deliveries
            out = count_artifact_cta_deliveries(1, newsletter_url="https://li/news")
        assert out == {"window_days": 90, "lead_magnet_dms": 0, "newsletter_links": None}
