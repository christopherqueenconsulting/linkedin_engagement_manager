"""One LinkedIn profile, many stored spellings (issue #2684, MCP W1).

`normalise_profile_url` is how a queue-only seat's URL is stored from now on; existing rows keep
their spelling, so `get_outreach_target_by_profile` matches on `profile_url_key` instead.
"""

from typing import Any
from unittest.mock import patch

import mysql.connector
import pytest

pytestmark = pytest.mark.unit

_GET_CONN = "cqc_lem.platform.db.connection.get_db_connection"


class TestTheKeyEverySpellingShares:
    @pytest.mark.parametrize("url", [
        "https://linkedin.com/in/jane-doe",
        "https://www.linkedin.com/in/jane-doe/",
        "https://www.linkedin.com/in/Jane-Doe?originalSubdomain=uk",
        "http://WWW.LINKEDIN.COM/in/jane-doe#about",
        "  https://www.linkedin.com/in/jane-doe  ",
    ])
    def test_every_spelling_of_one_profile_has_one_key(self, url: str) -> None:
        from cqc_lem.utilities.linkedin.profile_url import profile_url_key

        assert profile_url_key(url) == "jane-doe"

    def test_a_unicode_and_a_percent_encoded_slug_share_a_key(self) -> None:
        from cqc_lem.utilities.linkedin.profile_url import profile_url_key

        assert profile_url_key("https://www.linkedin.com/in/jérôme/") == \
            profile_url_key("https://www.linkedin.com/in/j%C3%A9r%C3%B4me") == "jérôme"

    @pytest.mark.parametrize("url", [None, "", "https://example.com/in/jane",
                                     "https://www.linkedin.com/company/acme"])
    def test_anything_else_has_no_key(self, url: Any) -> None:
        from cqc_lem.utilities.linkedin.profile_url import profile_url_key

        assert profile_url_key(url) is None

    def test_like_wildcards_in_a_slug_are_escaped(self) -> None:
        from cqc_lem.utilities.linkedin.profile_url import profile_url_like_patterns

        assert profile_url_like_patterns("jane_doe") == ("%/in/jane\\_doe%",)
        assert profile_url_like_patterns("jérôme") == ("%/in/jérôme%", "%/in/j\\%c3\\%a9r\\%c3\\%b4me%")


class TestTheOutreachProfileLookup:
    def test_a_legacy_spelling_row_is_found_for_a_canonical_url(self, fake_cursor: Any) -> None:
        from cqc_lem.utilities.db import get_outreach_target_by_profile

        legacy = {"id": 7, "target_profile_url": "https://linkedin.com/in/jane-doe"}
        conn, cur = fake_cursor(fetch_all=[legacy])
        with patch(_GET_CONN, return_value=conn):
            assert get_outreach_target_by_profile(
                3, "https://www.linkedin.com/in/jane-doe/") == legacy
        sql, params = cur.execute.call_args.args
        assert "WHERE user_id = %s AND (LOWER(target_profile_url) LIKE %s)" in sql
        assert params == (3, "%/in/jane-doe%")

    def test_a_longer_slug_the_like_also_matched_is_not_a_duplicate(self,
                                                                    fake_cursor: Any) -> None:
        from cqc_lem.utilities.db import get_outreach_target_by_profile

        conn, _cur = fake_cursor(fetch_all=[
            {"id": 8, "target_profile_url": "https://www.linkedin.com/in/jane-doe-2/"}])
        with patch(_GET_CONN, return_value=conn):
            assert get_outreach_target_by_profile(3, "https://linkedin.com/in/jane-doe") is None

    def test_a_non_profile_url_reads_nothing(self, fake_cursor: Any) -> None:
        from cqc_lem.utilities.db import get_outreach_target_by_profile

        conn, cur = fake_cursor()
        with patch(_GET_CONN, return_value=conn):
            assert get_outreach_target_by_profile(3, "https://example.com/x") is None
        cur.execute.assert_not_called()

    def test_a_read_error_is_none_and_logged(self, fake_cursor: Any) -> None:
        from cqc_lem.utilities.db import get_outreach_target_by_profile

        conn, _cur = fake_cursor(execute_error=mysql.connector.Error("down"))
        with patch(_GET_CONN, return_value=conn), \
             patch("cqc_lem.platform.db.repositories.outreach.log_error") as err:
            assert get_outreach_target_by_profile(3, "https://linkedin.com/in/jane") is None
        err.assert_called_once()
