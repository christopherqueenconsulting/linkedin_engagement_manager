"""Unit tests for newsletter subscriber-growth tracking + opt-in invite flow (issue #400)."""

import pytest
from unittest.mock import MagicMock, patch

pytestmark = pytest.mark.unit

_RA = "cqc_lem.app.run_automation"
_RS = "cqc_lem.app.run_scheduler"


@pytest.fixture(autouse=True)
def _no_sleep():
    with patch(f"{_RA}.time.sleep"), patch(f"{_RA}.random.uniform", return_value=0):
        yield


class TestParseSubscriberCount:
    @pytest.mark.parametrize("text,expected", [
        ("1,234 subscribers", 1234),
        ("1 subscriber", 1),
        ("3.2K subscribers", 3200),
        ("2M subscribers", 2_000_000),
        ("987 subscribers • Weekly", 987),
        ("", None),
        ("No count here", None),
        (None, None),
    ])
    def test_parse(self, text, expected):
        from cqc_lem.app.run_automation import _parse_subscriber_count
        assert _parse_subscriber_count(text) == expected


class TestReadSubscriberCount:
    def test_none_when_no_url(self):
        from cqc_lem.app.run_automation import _read_newsletter_subscriber_count
        assert _read_newsletter_subscriber_count(MagicMock(), MagicMock(), None) is None

    def test_reads_from_element(self):
        from cqc_lem.app.run_automation import _read_newsletter_subscriber_count
        el = MagicMock()
        with patch(f"{_RA}.find_first", return_value=el), \
             patch(f"{_RA}.getText", return_value="4,560 subscribers"):
            out = _read_newsletter_subscriber_count(MagicMock(), MagicMock(), "https://x/newsletter")
        assert out == 4560

    def test_falls_back_to_body_text(self):
        from cqc_lem.app.run_automation import _read_newsletter_subscriber_count
        driver = MagicMock()
        driver.find_element.return_value.text = "Header 88 subscribers footer"
        with patch(f"{_RA}.find_first", return_value=None):
            out = _read_newsletter_subscriber_count(driver, MagicMock(), "https://x/newsletter")
        assert out == 88

    def test_none_when_unreadable(self):
        from cqc_lem.app.run_automation import _read_newsletter_subscriber_count
        driver = MagicMock()
        driver.find_element.side_effect = Exception("no body")
        with patch(f"{_RA}.find_first", return_value=None):
            assert _read_newsletter_subscriber_count(driver, MagicMock(), "https://x") is None


class TestInviteConnections:
    def test_zero_cap_no_op(self):
        from cqc_lem.app.run_automation import _invite_connections_to_newsletter
        with patch(f"{_RA}.click_first") as cf:
            assert _invite_connections_to_newsletter(MagicMock(), MagicMock(), 0) == 0
        cf.assert_not_called()

    def test_zero_when_no_invite_button(self):
        from cqc_lem.app.run_automation import _invite_connections_to_newsletter
        with patch(f"{_RA}.click_first", return_value=None):
            assert _invite_connections_to_newsletter(MagicMock(), MagicMock(), 10) == 0

    def test_selects_up_to_cap_and_sends(self):
        from cqc_lem.app.run_automation import _invite_connections_to_newsletter
        boxes = [MagicMock() for _ in range(10)]
        with patch(f"{_RA}.click_first", return_value=MagicMock()), \
             patch(f"{_RA}.get_elements_as_list_wait_stale", return_value=boxes):
            invited = _invite_connections_to_newsletter(MagicMock(), MagicMock(), 3)
        assert invited == 3

    def test_zero_when_no_checkboxes(self):
        from cqc_lem.app.run_automation import _invite_connections_to_newsletter
        with patch(f"{_RA}.click_first", return_value=MagicMock()), \
             patch(f"{_RA}.get_elements_as_list_wait_stale", return_value=[]):
            assert _invite_connections_to_newsletter(MagicMock(), MagicMock(), 5) == 0

    def test_zero_when_send_button_missing(self):
        from cqc_lem.app.run_automation import _invite_connections_to_newsletter
        boxes = [MagicMock() for _ in range(3)]
        # First click (open dialog) succeeds; send-button click returns None.
        with patch(f"{_RA}.click_first", side_effect=[MagicMock(), None]), \
             patch(f"{_RA}.get_elements_as_list_wait_stale", return_value=boxes):
            assert _invite_connections_to_newsletter(MagicMock(), MagicMock(), 3) == 0


class TestTrackNewsletterSubscribers:
    def test_skips_when_disabled(self):
        from cqc_lem.app.run_automation import track_newsletter_subscribers
        with patch(f"{_RA}.get_newsletter_settings", return_value={"enabled": False}), \
             patch(f"{_RA}.get_current_profile") as gp:
            result = track_newsletter_subscribers.run(user_id=1)
        assert "not enabled" in result
        gp.assert_not_called()

    def test_skips_when_no_url(self):
        from cqc_lem.app.run_automation import track_newsletter_subscribers
        with patch(f"{_RA}.get_newsletter_settings",
                   return_value={"enabled": True, "newsletter_url": None}), \
             patch(f"{_RA}.get_current_profile") as gp:
            result = track_newsletter_subscribers.run(user_id=1)
        assert "No newsletter URL" in result
        gp.assert_not_called()

    def test_captures_count_without_inviting_when_opted_out(self):
        from cqc_lem.app.run_automation import track_newsletter_subscribers
        settings = {"enabled": True, "newsletter_url": "https://x/nl",
                    "invite_connections_enabled": False, "max_invites_per_run": 50}
        with patch(f"{_RA}.get_newsletter_settings", return_value=settings), \
             patch(f"{_RA}.get_current_profile", return_value=(MagicMock(), MagicMock(), "e", MagicMock())), \
             patch(f"{_RA}._read_newsletter_subscriber_count", return_value=210), \
             patch(f"{_RA}._invite_connections_to_newsletter") as invite, \
             patch(f"{_RA}.record_newsletter_subscriber_stat") as rec, \
             patch(f"{_RA}.quit_gracefully"):
            result = track_newsletter_subscribers.run(user_id=1)
        invite.assert_not_called()
        rec.assert_called_once_with(1, subscriber_count=210, invites_sent=0)
        assert "Subscribers: 210" in result and "invited 0" in result

    def test_invites_within_cap_when_opted_in(self):
        from cqc_lem.app.run_automation import track_newsletter_subscribers
        settings = {"enabled": True, "newsletter_url": "https://x/nl",
                    "invite_connections_enabled": True, "max_invites_per_run": 25}
        with patch(f"{_RA}.get_newsletter_settings", return_value=settings), \
             patch(f"{_RA}.get_current_profile", return_value=(MagicMock(), MagicMock(), "e", MagicMock())), \
             patch(f"{_RA}._read_newsletter_subscriber_count", return_value=210), \
             patch(f"{_RA}._invite_connections_to_newsletter", return_value=25) as invite, \
             patch(f"{_RA}.record_newsletter_subscriber_stat") as rec, \
             patch(f"{_RA}.quit_gracefully"):
            result = track_newsletter_subscribers.run(user_id=1)
        invite.assert_called_once()
        assert invite.call_args[0][2] == 25  # cap threaded through
        rec.assert_called_once_with(1, subscriber_count=210, invites_sent=25)
        assert "invited 25" in result

    def test_records_unknown_when_count_unreadable(self):
        from cqc_lem.app.run_automation import track_newsletter_subscribers
        settings = {"enabled": True, "newsletter_url": "https://x/nl",
                    "invite_connections_enabled": False, "max_invites_per_run": 50}
        with patch(f"{_RA}.get_newsletter_settings", return_value=settings), \
             patch(f"{_RA}.get_current_profile", return_value=(MagicMock(), MagicMock(), "e", MagicMock())), \
             patch(f"{_RA}._read_newsletter_subscriber_count", return_value=None), \
             patch(f"{_RA}.record_newsletter_subscriber_stat") as rec, \
             patch(f"{_RA}.quit_gracefully"):
            result = track_newsletter_subscribers.run(user_id=1)
        rec.assert_called_once_with(1, subscriber_count=None, invites_sent=0)
        assert "unknown" in result


class TestAutoTrackFanout:
    def test_dispatches_per_enabled_user(self):
        from cqc_lem.app.run_scheduler import auto_track_newsletter_subscribers
        with patch(f"{_RS}._skip_if_throttled", return_value=False), \
             patch("cqc_lem.utilities.db.get_enabled_newsletter_user_ids", return_value=[1, 4, 7]), \
             patch("cqc_lem.app.run_automation.track_newsletter_subscribers") as task:
            result = auto_track_newsletter_subscribers()
        assert task.apply_async.call_count == 3
        assert "3 user" in result

    def test_skips_when_throttled(self):
        from cqc_lem.app.run_scheduler import auto_track_newsletter_subscribers
        with patch(f"{_RS}._skip_if_throttled", return_value=True), \
             patch("cqc_lem.app.run_automation.track_newsletter_subscribers") as task:
            result = auto_track_newsletter_subscribers()
        task.apply_async.assert_not_called()
        assert "throttled" in result.lower()
