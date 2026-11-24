"""Issue #2753: the profile audit endpoints against a real migrated MySQL.

Covers what a mock cannot: the finding action persists across requests, an ignore survives into the
next audit's opened count, a done covers only the audit it was pressed on, and the follower join
reads real `follower_stats` rows.
"""

from unittest.mock import patch

import mysql.connector
import pytest

from cqc_lem.utilities import db

pytestmark = pytest.mark.integration

_EMAIL = "profile-audit-2753@example.test"
_M = "cqc_lem.api.main"
_F = "cqc_lem.utilities.flags.flag_enabled"
_URL = "/api/user/profile-audit"
_FINDING = {"check_id": "custom_url", "status": "fail", "weight": 10, "detail": "d", "fix_hint": "h"}


def _schema_available() -> bool:
    try:
        connection = mysql.connector.connect(connect_timeout=3, **db._get_mysql_config())
    except Exception:  # noqa: BLE001 - unset/incomplete DB env means "no server here", so skip
        return False
    try:
        cursor = connection.cursor()
        cursor.execute("SHOW TABLES LIKE 'profile_audit_finding_actions'")
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
def user_id():
    if not _schema_available():
        pytest.skip("no migrated MySQL schema with profile_audit_finding_actions available")
    _exec("DELETE FROM users WHERE email=%s", (_EMAIL,))  # CASCADE clears audits and actions
    db.add_user(_EMAIL, "x")
    uid = db.get_user_id(_EMAIL)
    assert uid, "test user was not created"
    yield uid
    _exec("DELETE FROM users WHERE email=%s", (_EMAIL,))


def _get(api_client, uid):
    with patch(f"{_M}.get_session_user_id", return_value=uid), patch(_F, return_value=True):
        return api_client.get(_URL, params={"session_token": "t"})


def _act(api_client, uid, action, check_id="custom_url"):
    with patch(f"{_M}.get_session_user_id", return_value=uid), patch(_F, return_value=True):
        return api_client.post(f"{_URL}/finding", json={
            "session_token": "t", "check_id": check_id, "action": action})


def test_empty_history_then_audit_then_actions_persist(api_client, user_id):
    assert _get(api_client, user_id).json()["detail"]["latest"] is None
    assert _act(api_client, user_id, "ignored").status_code == 404  # no audit yet

    first = db.insert_profile_audit(user_id, "weekly", "2026-W46", "1", 40, [_FINDING])
    detail = _get(api_client, user_id).json()["detail"]
    assert detail["latest"]["audit_id"] == first
    assert detail["latest"]["open_count"] == 1
    assert len(detail["trend"]) == 1

    assert _act(api_client, user_id, "done").status_code == 200
    states = {f["check_id"]: f["state"] for f in _get(api_client, user_id).json()["detail"]["latest"]["findings"]}
    assert states["custom_url"] == "done"

    # The next audit re-grades: the same failing check is open again, because a done covers one audit.
    db.insert_profile_audit(user_id, "weekly", "2026-W47", "1", 40, [_FINDING], previous_audit_id=first)
    latest = _get(api_client, user_id).json()["detail"]["latest"]
    assert latest["findings"][0]["state"] == "open" and latest["open_count"] == 1

    # An ignore holds across audits and drops out of the open count.
    assert _act(api_client, user_id, "ignored").status_code == 200
    db.insert_profile_audit(user_id, "weekly", "2026-W48", "1", 40, [_FINDING])
    latest = _get(api_client, user_id).json()["detail"]["latest"]
    assert latest["findings"][0]["state"] == "ignored" and latest["open_count"] == 0

    assert _act(api_client, user_id, "reopen").status_code == 200
    latest = _get(api_client, user_id).json()["detail"]["latest"]
    assert latest["findings"][0]["state"] == "open"


def test_unknown_check_and_bad_action_are_rejected(api_client, user_id):
    db.insert_profile_audit(user_id, "weekly", "2026-W46", "1", 40, [_FINDING])
    assert _act(api_client, user_id, "ignored", check_id="nope").status_code == 404
    assert _act(api_client, user_id, "delete").status_code == 422


def test_follower_join_reads_real_snapshots(api_client, user_id):
    _exec("INSERT INTO follower_stats (user_id, follower_count, captured_at) "
          "VALUES (%s, %s, NOW() - INTERVAL 1 HOUR)", (user_id, 120))
    db.insert_profile_audit(user_id, "weekly", "2026-W46", "1", 40, [_FINDING])
    rows = _get(api_client, user_id).json()["detail"]["correlation"]["rows"]
    assert rows[0]["follower_count"] == 120
    assert rows[0]["profile_views"] is None  # unread stays None, never 0
