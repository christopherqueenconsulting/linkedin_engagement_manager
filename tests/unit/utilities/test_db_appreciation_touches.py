"""Unit tests for the appreciation-DM dedup ledger (issue #968).

The claim is the whole safety property: it must be granted exactly once, and a ledger we cannot
read must never read as "go ahead".
"""

from unittest.mock import patch

import mysql.connector
import pytest

pytestmark = pytest.mark.unit


class TestClaimAppreciationTouch:
    def test_first_claim_is_granted(self, fake_cursor):
        conn, cur = fake_cursor(rowcount=1)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import claim_appreciation_touch
            assert claim_appreciation_touch(1, "https://x/in/jane", "recommendation_received",
                                            person_name="Jane") is True
        assert "INSERT IGNORE INTO appreciation_touches" in cur.execute.call_args[0][0]
        conn.commit.assert_called_once()

    def test_duplicate_claim_is_refused_without_raising(self, fake_cursor):
        """INSERT IGNORE swallows the unique-key collision — rowcount 0 IS the dedup answer."""
        conn, _ = fake_cursor(rowcount=0)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import claim_appreciation_touch
            assert claim_appreciation_touch(1, "https://x/in/jane", "collaboration") is False

    def test_db_error_fails_closed(self, fake_cursor):
        """No claim, no DM — a thank-you missed is recoverable, one sent twenty times is not."""
        conn, cur = fake_cursor()
        cur.execute.side_effect = mysql.connector.Error(msg="boom")
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import claim_appreciation_touch
            assert claim_appreciation_touch(1, "u", "connection_accepted") is False


class TestHasAppreciationTouch:
    def test_true_when_a_row_exists(self, fake_cursor):
        conn, _ = fake_cursor(fetch_one=(1,))
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import has_appreciation_touch
            assert has_appreciation_touch(1, "https://x/in/jane", "collaboration") is True

    def test_false_when_absent(self, fake_cursor):
        conn, _ = fake_cursor(fetch_one=None)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import has_appreciation_touch
            assert has_appreciation_touch(1, "https://x/in/jane", "collaboration") is False

    def test_error_reads_as_not_thanked_so_the_claim_decides(self, fake_cursor):
        conn, cur = fake_cursor()
        cur.execute.side_effect = mysql.connector.Error(msg="boom")
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import has_appreciation_touch
            assert has_appreciation_touch(1, "u", "collaboration") is False


def test_event_types_match_the_migration_enum():
    from cqc_lem.utilities.db import APPRECIATION_EVENT_TYPES
    assert APPRECIATION_EVENT_TYPES == ("connection_accepted", "recommendation_received",
                                        "collaboration")


def test_collaboration_default_thanks_them_for_the_mention():
    """What fires this trigger is a mention, so the DEFAULT wording has to say that and not thank
    somebody for a project neither party worked on. A customized template is read from the DB and
    is untouched by this.
    """
    from cqc_lem.utilities.db import _DM_DEFAULT_TEMPLATES
    text = _DM_DEFAULT_TEMPLATES["collaboration"]
    assert "{first_name}" in text
    assert "mention" in text.lower()
    assert "collaborating with you" not in text
