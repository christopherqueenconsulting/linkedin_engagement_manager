"""Issue #2366: a new account's generated posts wait for approval by default.

The landing page says posts wait for the user's approval. That is only true while
`auto_schedule_posts` is 0 for a new account, so these tests hold every link of that chain: the
trial INSERT writes 0, the migration flips the column DEFAULT (and touches no row), the preferences
getter falls back to False, and a first generated post that passes every gate still lands PENDING.
"""

import re
from pathlib import Path
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit

_RCP = "cqc_lem.app.run_content_plan"
_CONN = "cqc_lem.platform.db.connection.get_db_connection"
_MIGRATIONS = Path(__file__).resolve().parents[3] / "compose" / "local" / "database" / "migrations"
_MIGRATION = _MIGRATIONS / "V20261010145149__default_auto_schedule_posts_off.sql"

_POST = ("Cutting AI cost is a routing problem, not a model problem.\n\nLast quarter I routed our "
         "simplest calls to a cheap model and dropped the bill by 38%.")


def _inserted_columns(sql: str) -> dict[str, str]:
    """Map each column of a single-row `INSERT INTO t (cols) VALUES (vals)` to its value token."""
    match = re.search(r"INSERT\s+INTO\s+\w+\s*\(([^)]*)\)\s*VALUES\s*\(([^)]*)\)", sql,
                      re.IGNORECASE | re.DOTALL)
    assert match, f"not a single-row INSERT: {sql!r}"
    columns = [c.strip() for c in match.group(1).split(",")]
    values = [v.strip() for v in match.group(2).split(",")]
    assert len(columns) == len(values), "column and value counts differ"
    return dict(zip(columns, values))


def _create_trial_user(fake_cursor) -> dict[str, str]:
    from cqc_lem.utilities.db import add_user_by_email
    conn, cur = fake_cursor(rowcount=1, lastrowid=7)
    with patch(_CONN, return_value=conn), \
         patch("cqc_lem.utilities.stripe_util.create_stripe_customer", return_value=None):
        assert add_user_by_email("trial@example.com") == 7
    return _inserted_columns(cur.execute.call_args_list[0][0][0])


class TestTrialInsert:
    def test_a_new_trial_account_is_written_with_auto_scheduling_off(self, fake_cursor):
        columns = _create_trial_user(fake_cursor)
        assert columns["subscription_status"] == "'trial'"
        # A literal 0, not a placeholder: the trial path does not depend on the column DEFAULT,
        # so it holds even on a database the new migration has not reached yet.
        assert columns["auto_schedule_posts"] == "0"


class TestPreferencesFallback:
    def test_a_missing_users_row_reads_as_auto_scheduling_off(self, fake_cursor):
        from cqc_lem.utilities.db import get_user_preferences
        conn, _ = fake_cursor(fetch_one=None)
        with patch(_CONN, return_value=conn):
            assert get_user_preferences(7)["auto_schedule_posts"] is False

    def test_an_unreadable_users_row_reads_as_auto_scheduling_off(self, fake_cursor):
        import mysql.connector

        from cqc_lem.utilities.db import get_user_preferences
        conn, _ = fake_cursor(execute_error=mysql.connector.Error("down"))
        with patch(_CONN, return_value=conn):
            assert get_user_preferences(7)["auto_schedule_posts"] is False


class TestMigrationChangesOnlyTheDefault:
    def _statements(self) -> list[str]:
        sql = _MIGRATION.read_text()
        body = "\n".join(line for line in sql.splitlines() if not line.strip().startswith("--"))
        return [s.strip() for s in body.split(";") if s.strip()]

    def test_the_only_statement_sets_the_column_default_to_zero(self):
        statements = self._statements()
        assert len(statements) == 1
        normalized = " ".join(statements[0].split())
        assert normalized == "ALTER TABLE users ALTER COLUMN auto_schedule_posts SET DEFAULT 0"

    def test_no_existing_row_is_rewritten(self):
        # `ALTER COLUMN ... SET DEFAULT` is metadata-only; anything that rewrites rows or the
        # column definition (type, NOT NULL) would change what existing accounts have today.
        upper = " ".join(self._statements()).upper()
        for verb in ("UPDATE ", "DELETE ", "INSERT ", "REPLACE ", "MODIFY ", "CHANGE ", "DROP ",
                     "TRUNCATE "):
            assert verb not in upper, f"migration must only change the default, found {verb!r}"

    def test_the_version_is_a_unique_timestamp(self):
        version = _MIGRATION.name.split("__", 1)[0][1:]
        assert re.fullmatch(r"\d{14}", version)
        versions = [p.name.split("__", 1)[0] for p in _MIGRATIONS.glob("V*.sql")]
        assert versions.count(f"V{version}") == 1


class TestFirstGeneratedPostWaitsForApproval:
    """A trial user's first generated post that passes every gate is still left PENDING."""

    def _generate_first_post(self, prefs_reader):
        from cqc_lem.app import run_content_plan as rcp
        post = [{"user_id": 7, "id": 42, "post_type": "text", "buyer_stage": "awareness"}]
        with patch(f"{_RCP}.get_planned_posts_within_buffer", return_value=post), \
             patch(f"{_RCP}.count_ready_posts_within_buffer", return_value=0), \
             patch(f"{_RCP}.create_content", return_value=(_POST, None)), \
             patch(f"{_RCP}._score_and_persist_dwell"), \
             patch(f"{_RCP}.update_db_post_content"), \
             patch(f"{_RCP}.update_db_post_status") as status, \
             patch(f"{_RCP}.update_db_post_gate_reason") as reason, \
             patch(f"{_RCP}.get_engagement_preferences", return_value={}), \
             patch(f"{_RCP}.get_user_preferences", side_effect=prefs_reader), \
             patch(f"{_RCP}._post_missing_required_asset", return_value=False), \
             patch(f"{_RCP}._fact_anchors", return_value=[_POST]), \
             patch(f"{_RCP}._post_material_sources", return_value=[]), \
             patch(f"{_RCP}.get_post_authenticity_score", return_value=95):
            rcp.auto_create_weekly_content(user_id=7)
        return status, reason

    def test_the_trial_users_stored_value_holds_the_post_pending(self, fake_cursor):
        from cqc_lem.utilities.db import PostApprover, PostStatus, get_user_preferences
        # The users row the trial INSERT wrote, read back through the real getter.
        stored = int(_create_trial_user(fake_cursor)["auto_schedule_posts"])
        row = {"last_login_inactivate_delay": None, "auto_schedule_posts": stored,
               "content_buffer_days": 5, "content_buffer_max_posts": 5, "content_language": None}

        def read_prefs(user_id: int) -> dict:
            conn, _ = fake_cursor(fetch_one=row)
            with patch(_CONN, return_value=conn):
                return get_user_preferences(user_id)

        status, reason = self._generate_first_post(read_prefs)
        # Every gate passed (no findings), so the ONLY thing holding it is the approval default.
        reason.assert_called_once_with(42, [])
        status.assert_called_once_with(42, PostStatus.PENDING,
                                       approved_by=PostApprover.AUTO_SCHEDULE)

    def test_prefs_without_the_key_hold_the_post_pending(self):
        from cqc_lem.utilities.db import PostApprover, PostStatus
        status, _ = self._generate_first_post(lambda user_id: {})
        status.assert_called_once_with(42, PostStatus.PENDING,
                                       approved_by=PostApprover.AUTO_SCHEDULE)
