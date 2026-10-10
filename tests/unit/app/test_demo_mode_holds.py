"""DEMO_MODE never leaves an outbound row for an orphan reaper to replay (#2372).

`run_scheduler` re-queues `scheduled_dms` stuck at 'scheduled', and `connection_requests` and
`catchup_touches` stuck at 'sending', with no upper bound on age. A send refused by demo mode used to
leave its row exactly there, so the reaper re-queued it on every beat and SENT it to a real person
the moment demo mode was turned off. Each send path now holds the row at 'pending', which only a
human can move back to 'approved'.

The refusal here is the REAL one: `send_dm_now` / `invite_to_connect_now` run unpatched down to
`get_docker_driver`, whose guard raises before the Grid is touched.
"""

from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit

_OUT = "cqc_lem.app.engagement.outreach"
_INV = "cqc_lem.app.engagement.invites"
_SEL = "cqc_lem.utilities.selenium_util"


@pytest.fixture
def demo_on(monkeypatch):
    monkeypatch.setenv("DEMO_MODE", "true")


@pytest.fixture
def no_grid():
    with patch(f"{_SEL}._wait_for_selenium_ready") as ready, \
         patch(f"{_SEL}.webdriver.Remote") as remote:
        yield ready, remote
    ready.assert_not_called()
    remote.assert_not_called()


def test_scheduled_dm_is_held_at_pending(demo_on, no_grid):
    from cqc_lem.app.engagement import outreach
    from cqc_lem.utilities.db import ScheduledDmStatus
    dm = {"id": 3, "user_id": 1, "recipient_profile_url": "https://www.linkedin.com/in/jane",
          "message": "Hi Jane, thanks for stopping by my profile.", "status": "scheduled"}
    with patch("cqc_lem.utilities.db.get_scheduled_dm", return_value=dm), \
         patch("cqc_lem.utilities.db.count_dms_sent_today", return_value=0), \
         patch(f"{_OUT}.get_engagement_preferences", return_value={"max_dms_per_day": 10}), \
         patch(f"{_OUT}.get_user_password_pair_by_id", return_value=("e@x", "pw")), \
         patch(f"{_OUT}.insert_new_log") as log_row, \
         patch("cqc_lem.utilities.db.update_scheduled_dm_status") as upd:
        out = outreach.send_scheduled_dm(3)
    upd.assert_called_once_with(3, ScheduledDmStatus.PENDING)
    log_row.assert_not_called()  # nothing was attempted, so no FAILURE row either
    assert "held at pending" in out and "DEMO_MODE" in out


def test_connection_request_is_held_at_pending_without_charging_an_attempt(demo_on, no_grid):
    from cqc_lem.app.engagement import invites
    from cqc_lem.utilities.db import ConnectionRequestStatus
    req = {"id": 4, "user_id": 1, "recipient_profile_url": "https://www.linkedin.com/in/jane",
           "message": None, "status": "sending", "recipient_email": None, "attempts": 0}
    with patch("cqc_lem.utilities.db.get_connection_request", return_value=req), \
         patch("cqc_lem.utilities.db.count_invites_sent_today", return_value=0), \
         patch(f"{_INV}.is_invites_held", return_value=False), \
         patch(f"{_INV}.get_engagement_preferences", return_value={"max_invites_per_day": 10}), \
         patch(f"{_INV}.get_user_password_pair_by_id", return_value=("e@x", "pw")), \
         patch("cqc_lem.utilities.db.update_connection_request_status") as upd, \
         patch("cqc_lem.utilities.db.record_connection_request_attempt") as rec:
        out = invites.send_connection_request(4)
    upd.assert_called_once_with(4, ConnectionRequestStatus.PENDING)
    rec.assert_not_called()
    assert "held at pending" in out


def test_a_refusal_raised_during_login_is_not_rewrapped_as_a_throttle(demo_on):
    # `invite_to_connect_now` re-wraps a login RuntimeError as LinkedInRateLimited, which defers
    # the row to 'approved'. DemoModeError is a RuntimeError too, so it must pass through untouched
    # for `send_connection_request` to hold the row at 'pending' instead.
    from unittest.mock import MagicMock

    from cqc_lem.app.engagement import invites
    from cqc_lem.utilities.db import ConnectionRequestStatus
    from cqc_lem.utilities.demo_mode import DemoModeError
    req = {"id": 6, "user_id": 1, "recipient_profile_url": "https://www.linkedin.com/in/jane",
           "message": None, "status": "sending", "recipient_email": None, "attempts": 0}
    with patch("cqc_lem.utilities.db.get_connection_request", return_value=req), \
         patch("cqc_lem.utilities.db.count_invites_sent_today", return_value=0), \
         patch(f"{_INV}.is_invites_held", return_value=False), \
         patch(f"{_INV}.get_engagement_preferences", return_value={"max_invites_per_day": 10}), \
         patch(f"{_INV}.get_user_password_pair_by_id", return_value=("e@x", "pw")), \
         patch(f"{_INV}.get_driver_wait_pair", return_value=(MagicMock(), MagicMock())), \
         patch(f"{_INV}.login_to_linkedin",
               side_effect=DemoModeError("http:www.linkedin.com")), \
         patch(f"{_INV}.quit_gracefully") as quit_driver, \
         patch(f"{_INV}.insert_new_log") as log_row, \
         patch("cqc_lem.utilities.db.update_connection_request_status") as upd, \
         patch("cqc_lem.utilities.db.record_connection_request_attempt") as rec:
        out = invites.send_connection_request(6)
    upd.assert_called_once_with(6, ConnectionRequestStatus.PENDING)
    assert ConnectionRequestStatus.APPROVED not in [c.args[1] for c in upd.call_args_list]
    rec.assert_not_called()
    log_row.assert_not_called()   # not recorded as a per-target failure either
    quit_driver.assert_called_once()  # the session that did open is still closed
    assert "held at pending" in out


def test_catchup_touch_is_held_at_pending_and_gives_the_claim_back(demo_on, no_grid):
    from cqc_lem.app.engagement import outreach
    from cqc_lem.utilities.db import CatchupTouchStatus
    touch = {"id": 5, "user_id": 1, "profile_url": "https://www.linkedin.com/in/jane",
             "person_name": "Jane Doe", "event_type": "job_change", "message": "Congrats Jane!",
             "status": "sending", "event_period": "2026-10"}
    prefs = {"max_catchup_touches_per_day": 5, "max_dms_per_day": 10,
             "min_catchup_contact_interval_days": 0, "max_catchup_touches_per_contact_days": 0}
    with patch(f"{_OUT}.get_catchup_touch", return_value=touch), \
         patch(f"{_OUT}.get_engagement_preferences", return_value=prefs), \
         patch(f"{_OUT}.max_catchup_touches_allowed", return_value=5), \
         patch(f"{_OUT}.count_catchup_touches_sent_today", return_value=0), \
         patch(f"{_OUT}.count_dms_sent_today", return_value=0), \
         patch(f"{_OUT}._catchup_contact_cooldown_active", return_value=False), \
         patch(f"{_OUT}._catchup_contact_cap_reached", return_value=False), \
         patch(f"{_OUT}.claim_catchup_send_attempt", return_value=True), \
         patch(f"{_OUT}.release_catchup_send_attempt") as release, \
         patch(f"{_OUT}.get_user_password_pair_by_id", return_value=("e@x", "pw")), \
         patch(f"{_OUT}.update_catchup_touch_status") as upd, \
         patch(f"{_OUT}.report_catchup_run") as report:
        out = outreach.send_catchup_touch.run(touch_id=5)
    release.assert_called_once_with(1, "https://www.linkedin.com/in/jane", "job_change", "2026-10")
    upd.assert_called_once_with(5, CatchupTouchStatus.PENDING)
    assert report.call_args.args[1]["status"] == outreach.CATCHUP_STATUS_DEMO_HELD
    assert "held at pending" in out


def test_control_off_the_dm_reaches_the_browser(monkeypatch):
    # Without demo mode the same path DOES reach the Grid, so the holds above are not vacuous.
    monkeypatch.delenv("DEMO_MODE", raising=False)
    from cqc_lem.app.engagement import outreach

    class _Stop(Exception):
        pass

    dm = {"id": 3, "user_id": 1, "recipient_profile_url": "https://www.linkedin.com/in/jane",
          "message": "Hi Jane, thanks for stopping by my profile.", "status": "scheduled"}
    with patch("cqc_lem.utilities.db.get_scheduled_dm", return_value=dm), \
         patch("cqc_lem.utilities.db.count_dms_sent_today", return_value=0), \
         patch(f"{_OUT}.get_engagement_preferences", return_value={"max_dms_per_day": 10}), \
         patch(f"{_OUT}.get_user_password_pair_by_id", return_value=("e@x", "pw")), \
         patch(f"{_OUT}.get_driver_wait_pair", side_effect=_Stop) as driver, \
         patch("cqc_lem.utilities.db.update_scheduled_dm_status") as upd:
        with pytest.raises(_Stop):
            outreach.send_scheduled_dm(3)
    driver.assert_called_once()
    upd.assert_not_called()
