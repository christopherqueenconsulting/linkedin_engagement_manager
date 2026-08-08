"""Unit tests for the appreciation-DM dedup ledger (issue #968).

The claim is the whole safety property: it must be granted exactly once, and a ledger we cannot
read must never read as "go ahead".
"""

from unittest.mock import MagicMock, patch

import mysql.connector
import pytest

pytestmark = pytest.mark.unit

_DB = "cqc_lem.utilities.db"


def _conn(rowcount=1, fetch_row=None):
    conn = MagicMock()
    cur = MagicMock()
    cur.rowcount = rowcount
    cur.fetchone.return_value = fetch_row
    conn.cursor.return_value = cur
    return conn, cur


class TestClaimAppreciationTouch:
    def test_first_claim_is_granted(self):
        conn, cur = _conn(rowcount=1)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import claim_appreciation_touch
            assert claim_appreciation_touch(1, "https://x/in/jane", "recommendation_received",
                                            person_name="Jane") is True
        assert "INSERT IGNORE INTO appreciation_touches" in cur.execute.call_args[0][0]
        conn.commit.assert_called_once()

    def test_duplicate_claim_is_refused_without_raising(self):
        """INSERT IGNORE swallows the unique-key collision — rowcount 0 IS the dedup answer."""
        conn, _ = _conn(rowcount=0)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import claim_appreciation_touch
            assert claim_appreciation_touch(1, "https://x/in/jane", "collaboration") is False

    def test_db_error_fails_closed(self):
        """No claim, no DM — a thank-you missed is recoverable, one sent twenty times is not."""
        conn, cur = _conn()
        cur.execute.side_effect = mysql.connector.Error(msg="boom")
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import claim_appreciation_touch
            assert claim_appreciation_touch(1, "u", "connection_accepted") is False


class TestHasAppreciationTouch:
    def test_true_when_a_row_exists(self):
        conn, _ = _conn(fetch_row=(1,))
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import has_appreciation_touch
            assert has_appreciation_touch(1, "https://x/in/jane", "collaboration") is True

    def test_false_when_absent(self):
        conn, _ = _conn(fetch_row=None)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import has_appreciation_touch
            assert has_appreciation_touch(1, "https://x/in/jane", "collaboration") is False

    def test_error_reads_as_not_thanked_so_the_claim_decides(self):
        conn, cur = _conn()
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
