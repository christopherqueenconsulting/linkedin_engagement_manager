"""Issue #2664 (deferred from #2624): `nurture_dm_mode` against a real migrated MySQL.

With `nurture_dm_mode=auto_approve` a new nurture draft is inserted `approved`, and the scanner
(`auto_check_scheduled_dms`) dispatches it; under the default `pre_review` it stays `pending` and
the scanner never touches it.
"""

from unittest.mock import MagicMock, patch

import mysql.connector
import pytest

from cqc_lem.utilities import db

pytestmark = pytest.mark.integration

_EMAIL = "nurture-approval-mode-2664@example.test"
_PROFILE = "https://www.linkedin.com/in/nurture-approval-mode-2664"
_OUT = "cqc_lem.app.engagement.outreach"


def _schema_available() -> bool:
    """True when the configured MySQL is reachable AND carries the approval-mode columns."""
    try:
        connection = mysql.connector.connect(connect_timeout=3, **db._get_mysql_config())
    except Exception:  # noqa: BLE001 - unset/incomplete DB env means "no server here", so skip
        return False
    try:
        cursor = connection.cursor()
        cursor.execute("SHOW COLUMNS FROM engagement_preferences LIKE 'nurture_dm_mode'")
        present = bool(cursor.fetchone())
        cursor.close()
        return present
    except Exception:  # noqa: BLE001
        return False
    finally:
        connection.close()


def _exec(sql: str, params=(), fetch: bool = False):
    connection = db.get_db_connection()
    cursor = connection.cursor()
    try:
        cursor.execute(sql, params)
        rows = cursor.fetchall() if fetch else None
        connection.commit()
        return rows
    finally:
        cursor.close()
        connection.close()


@pytest.fixture
def user_id():
    if not _schema_available():
        pytest.skip("no migrated MySQL schema with engagement_preferences.nurture_dm_mode available")
    _exec("DELETE FROM users WHERE email=%s", (_EMAIL,))  # CASCADE clears prefs and DMs
    db.add_user(_EMAIL, "x")
    uid = db.get_user_id(_EMAIL)
    assert uid, "test user was not created"
    yield uid
    _exec("DELETE FROM users WHERE email=%s", (_EMAIL,))


def _draft_nurture(uid: int, monkeypatch) -> int:
    from cqc_lem.app.engagement import outreach

    monkeypatch.setenv("DM_NURTURE_ENABLED", "true")
    monkeypatch.delenv("DM_NURTURE_AUTO_APPROVE", raising=False)
    with patch(f"{_OUT}.classify_reply_intent", return_value={"intent": "interested"}), \
         patch(f"{_OUT}.recipient_context", return_value={"first_name": "Pat"}), \
         patch(f"{_OUT}.generate_nurture_dm",
               return_value="Glad that landed, Pat. Want me to send the checklist we used?"), \
         patch(f"{_OUT}.dm_gate_reasons", return_value=[]), \
         patch(f"{_OUT}._dm_fact_anchors", return_value=[]), \
         patch(f"{_OUT}.enqueue_followup"):
        dm_id = outreach._nurture_after_reply(
            uid, {"profile_url": _PROFILE, "first_name": "Pat", "event_type": "connection_accepted",
                  "next_step": 0},
            "Sounds interesting, tell me more", MagicMock(),
            prefs=db.get_engagement_preferences(uid), profile_synthesis=None)
    assert dm_id, "no nurture draft was filed"
    return dm_id


def _status(dm_id: int) -> str:
    rows = _exec("SELECT status FROM scheduled_dms WHERE id=%s", (dm_id,), fetch=True)
    return rows[0][0]


def test_auto_approve_files_an_approved_draft_the_scanner_dispatches(user_id, monkeypatch):
    from cqc_lem.app import run_scheduler

    assert db.update_engagement_preferences(user_id, {"nurture_dm_mode": "auto_approve"})
    dm_id = _draft_nurture(user_id, monkeypatch)
    assert _status(dm_id) == "approved"
    # The draft is filed with a future slot (nurture_delay_hours); make it due so the scanner sees it.
    _exec("UPDATE scheduled_dms SET scheduled_time = UTC_TIMESTAMP() - INTERVAL 1 MINUTE WHERE id=%s",
          (dm_id,))

    with patch.object(run_scheduler, "get_active_user_ids", return_value=[user_id]), \
         patch.object(run_scheduler, "skip_browser_lane", return_value=False), \
         patch.object(run_scheduler, "get_user_ids_with_dms_in_flight", return_value=set()), \
         patch.object(run_scheduler, "send_scheduled_dm") as send:
        run_scheduler.auto_check_scheduled_dms.run()
    dispatched = [c.kwargs.get("kwargs", {}).get("dm_id") for c in send.apply_async.call_args_list]
    assert dm_id in dispatched
    assert _status(dm_id) == "scheduled"


def test_a_new_user_defaults_to_review_and_the_scanner_ignores_the_draft(user_id, monkeypatch):
    from cqc_lem.app import run_scheduler

    assert db.get_engagement_preferences(user_id)["nurture_dm_mode"] == "pre_review"
    dm_id = _draft_nurture(user_id, monkeypatch)
    assert _status(dm_id) == "pending"

    with patch.object(run_scheduler, "get_active_user_ids", return_value=[user_id]), \
         patch.object(run_scheduler, "skip_browser_lane", return_value=False), \
         patch.object(run_scheduler, "get_user_ids_with_dms_in_flight", return_value=set()), \
         patch.object(run_scheduler, "send_scheduled_dm") as send:
        run_scheduler.auto_check_scheduled_dms.run()
    dispatched = [c.kwargs.get("kwargs", {}).get("dm_id") for c in send.apply_async.call_args_list]
    assert dm_id not in dispatched
    assert _status(dm_id) == "pending"
