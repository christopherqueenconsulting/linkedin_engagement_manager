"""Issue #1603 — per-user disable, proved against a live MySQL server.

Unit tests assert the SQL text carries `disabled_at IS NULL`; this proves the query actually
excludes a disabled account from `get_active_user_ids()` — the ONE per-user gate every automation
lane reads through this function, not just a badge nobody checks.
"""

from datetime import datetime, timedelta, timezone

import mysql.connector
import pytest

from cqc_lem.utilities import db

pytestmark = pytest.mark.integration

_EMAIL = "admin-disable-1603@example.test"


def _schema_available() -> bool:
    """A reachable server is not enough — this needs the migrated `disabled_at` column.

    An un-migrated DB (a bare local server) skips instead of erroring; CI provisions it before the
    suite runs.
    """
    try:
        config = db._get_mysql_config()
        connection = mysql.connector.connect(connect_timeout=3, **config)
    except Exception:  # noqa: BLE001 - unset/incomplete DB env means "no server here", so skip
        return False
    try:
        cursor = connection.cursor()
        cursor.execute("SHOW COLUMNS FROM users LIKE 'disabled_at'")
        present = bool(cursor.fetchone())
        cursor.close()
        return present
    except Exception:  # noqa: BLE001
        return False
    finally:
        connection.close()


def _exec(sql: str, params=()):
    connection = db.get_db_connection()
    cursor = connection.cursor()
    try:
        cursor.execute(sql, params)
        connection.commit()
    finally:
        cursor.close()
        connection.close()


@pytest.fixture
def active_user_id():
    if not _schema_available():
        pytest.skip("no migrated MySQL schema available for the admin-disable integration test")
    _exec("DELETE FROM users WHERE email=%s", (_EMAIL,))
    db.add_user(_EMAIL, "x")
    uid = db.get_user_id(_EMAIL)
    assert uid, "test user was not created"
    # Satisfy every OTHER get_active_user_ids() clause so disabled_at is the only variable.
    created = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=1)
    _exec(
        "UPDATE users SET linkedin_connection_status='connected', access_token='tok', "
        "access_token_created_at=%s, access_token_expires_in=86400, "
        "subscription_status='active', last_login_inactivate_delay=NULL WHERE id=%s",
        (created, uid))
    yield uid
    _exec("DELETE FROM users WHERE email=%s", (_EMAIL,))


class TestGetActiveUserIdsHonoursDisable:
    def test_an_enabled_user_is_active(self, active_user_id):
        assert active_user_id in db.get_active_user_ids()

    def test_a_disabled_user_is_absent(self, active_user_id):
        assert db.set_user_disabled(active_user_id, True) is True
        assert active_user_id not in db.get_active_user_ids()

    def test_re_enabling_restores_it(self, active_user_id):
        db.set_user_disabled(active_user_id, True)
        db.set_user_disabled(active_user_id, False)
        assert active_user_id in db.get_active_user_ids()
