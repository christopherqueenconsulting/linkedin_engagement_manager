from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from cqc_lem.utilities.linkedin.token_refresh import (
    attempt_token_refresh,
    days_until_expiry,
    get_token_expiry,
    is_token_expired,
    is_token_expiring_soon,
    refresh_token_usable,
    resolve_token_status,
)


def make_token_info(
    seconds_until_expiry: int,
    refresh_token: str | None = None,
    refresh_seconds_remaining: int = 60 * 60 * 24 * 60,
):
    now = datetime.now(timezone.utc)
    info: dict = {
        'access_token': 'tok_abc',
        'access_token_created_at': now,
        'access_token_expires_in': seconds_until_expiry,
        'refresh_token': refresh_token,
        'refresh_token_created_at': now if refresh_token else None,
        'refresh_token_expires_in': refresh_seconds_remaining if refresh_token else None,
    }
    return info


class TestGetTokenExpiry:
    def test_returns_correct_expiry(self):
        now = datetime.now(timezone.utc)
        info = {'access_token_created_at': now, 'access_token_expires_in': 3600}
        expiry = get_token_expiry(info)
        delta = abs((expiry - (now + timedelta(seconds=3600))).total_seconds())
        assert delta < 1

    def test_returns_none_when_missing_fields(self):
        assert get_token_expiry({'access_token_created_at': None, 'access_token_expires_in': None}) is None
        assert get_token_expiry({}) is None

    def test_handles_naive_datetime(self):
        naive = datetime.now(timezone.utc).replace(tzinfo=None)
        info = {'access_token_created_at': naive, 'access_token_expires_in': 3600}
        expiry = get_token_expiry(info)
        assert expiry is not None


class TestIsTokenExpired:
    def test_fresh_token_not_expired(self):
        info = make_token_info(seconds_until_expiry=3600)
        assert not is_token_expired(info)

    def test_expired_token(self):
        info = make_token_info(seconds_until_expiry=-1)
        assert is_token_expired(info)

    def test_none_expiry_treated_as_expired(self):
        assert is_token_expired({})


class TestIsTokenExpiringSoon:
    def test_60_days_not_expiring_soon(self):
        info = make_token_info(seconds_until_expiry=60 * 24 * 3600)
        assert not is_token_expiring_soon(info)

    def test_15_days_is_expiring_soon(self):
        info = make_token_info(seconds_until_expiry=15 * 24 * 3600)
        assert is_token_expiring_soon(info)

    def test_custom_days_threshold(self):
        info = make_token_info(seconds_until_expiry=45 * 24 * 3600)
        assert not is_token_expiring_soon(info, days=30)
        assert is_token_expiring_soon(info, days=60)

    def test_none_treated_as_expiring_soon(self):
        assert is_token_expiring_soon({})


class TestAttemptTokenRefresh:
    # The DB functions are imported inside attempt_token_refresh to avoid circular
    # imports, so we patch them at their source module (cqc_lem.utilities.db).

    @patch('cqc_lem.utilities.linkedin.token_refresh.requests.post')
    @patch('cqc_lem.utilities.db.update_user_access_token')
    @patch('cqc_lem.utilities.db.get_user_token_info')
    def test_successful_refresh(self, mock_get_info, mock_update, mock_post):
        mock_get_info.return_value = make_token_info(
            seconds_until_expiry=3600, refresh_token='refresh_tok'
        )
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            'access_token': 'new_tok',
            'expires_in': 7200,
            'refresh_token': 'new_refresh',
            'refresh_token_expires_in': 60 * 24 * 3600,
        }
        mock_resp.raise_for_status = MagicMock()
        mock_post.return_value = mock_resp

        success, token = attempt_token_refresh(user_id=1)
        assert success is True
        assert token == 'new_tok'
        mock_update.assert_called_once()

    @patch('cqc_lem.utilities.db.get_user_token_info')
    def test_no_refresh_token_returns_false(self, mock_get_info):
        mock_get_info.return_value = make_token_info(
            seconds_until_expiry=3600, refresh_token=None
        )
        success, token = attempt_token_refresh(user_id=1)
        assert success is False
        assert token is None

    @patch('cqc_lem.utilities.db.get_user_token_info')
    def test_no_token_info_returns_false(self, mock_get_info):
        mock_get_info.return_value = None
        success, token = attempt_token_refresh(user_id=1)
        assert success is False

    @patch('cqc_lem.utilities.linkedin.token_refresh.requests.post')
    @patch('cqc_lem.utilities.db.get_user_token_info')
    def test_network_error_returns_false(self, mock_get_info, mock_post):
        import requests as req
        mock_get_info.return_value = make_token_info(
            seconds_until_expiry=3600, refresh_token='refresh_tok'
        )
        mock_post.side_effect = req.RequestException("timeout")
        success, token = attempt_token_refresh(user_id=1)
        assert success is False
        assert token is None

    @patch('cqc_lem.utilities.db.get_user_token_info')
    def test_expired_refresh_token_returns_false(self, mock_get_info):
        now = datetime.now(timezone.utc)
        mock_get_info.return_value = {
            'access_token': 'tok',
            'access_token_created_at': now,
            'access_token_expires_in': 3600,
            'refresh_token': 'expired_refresh',
            'refresh_token_created_at': now - timedelta(days=90),
            'refresh_token_expires_in': 60 * 24 * 3600,  # 60 days, but created 90 days ago
        }
        success, token = attempt_token_refresh(user_id=1)
        assert success is False


class TestDaysUntilExpiry:
    def test_floors_partial_days(self):
        info = make_token_info(seconds_until_expiry=int(2.9 * 86400))
        assert days_until_expiry(info) == 2

    def test_expired_never_goes_negative(self):
        assert days_until_expiry(make_token_info(seconds_until_expiry=-86400 * 5)) == 0

    def test_unknown_expiry_is_none_not_zero(self):
        assert days_until_expiry({}) is None


class TestRefreshTokenUsable:
    def test_missing_refresh_token(self):
        assert refresh_token_usable(make_token_info(3600)) is False

    def test_live_refresh_token(self):
        assert refresh_token_usable(make_token_info(3600, refresh_token='rt')) is True

    def test_lapsed_refresh_token(self):
        now = datetime.now(timezone.utc)
        assert refresh_token_usable({
            'refresh_token': 'rt',
            'refresh_token_created_at': now - timedelta(days=90),
            'refresh_token_expires_in': 60 * 24 * 3600,
        }) is False

    def test_unknown_refresh_window_assumed_usable(self):
        assert refresh_token_usable({'refresh_token': 'rt'}) is True


class TestResolveTokenStatus:
    @patch('cqc_lem.utilities.db.get_user_token_info')
    def test_no_token_reads_disconnected(self, mock_get_info):
        mock_get_info.return_value = None
        status = resolve_token_status(user_id=1)
        assert status["connected"] is False
        assert status["is_expired"] is True
        assert status["days_remaining"] is None
        assert status["can_auto_refresh"] is False

    @patch('cqc_lem.utilities.db.get_user_token_info')
    def test_row_without_access_token_reads_disconnected(self, mock_get_info):
        mock_get_info.return_value = {'access_token': None}
        assert resolve_token_status(user_id=1)["connected"] is False

    @patch('cqc_lem.utilities.linkedin.token_refresh.attempt_token_refresh')
    @patch('cqc_lem.utilities.db.get_user_token_info')
    def test_healthy_token_is_not_refreshed(self, mock_get_info, mock_refresh):
        mock_get_info.return_value = make_token_info(50 * 86400, refresh_token='rt')
        status = resolve_token_status(user_id=1)
        mock_refresh.assert_not_called()
        assert status["refresh_attempted"] is False
        assert status["is_expiring_soon"] is False
        assert status["days_remaining"] == 49

    @patch('cqc_lem.utilities.linkedin.token_refresh.attempt_token_refresh')
    @patch('cqc_lem.utilities.db.get_user_token_info')
    def test_expiring_token_is_refreshed_and_restated(self, mock_get_info, mock_refresh):
        mock_get_info.side_effect = [make_token_info(3 * 86400, refresh_token='rt'),
                                     make_token_info(60 * 86400, refresh_token='rt2')]
        mock_refresh.return_value = (True, 'new')
        status = resolve_token_status(user_id=1)
        assert status["refresh_attempted"] is True
        assert status["refresh_succeeded"] is True
        assert status["is_expiring_soon"] is False
        assert status["days_remaining"] == 59

    @patch('cqc_lem.utilities.linkedin.token_refresh.attempt_token_refresh')
    @patch('cqc_lem.utilities.db.get_user_token_info')
    def test_failed_refresh_keeps_expiring_state(self, mock_get_info, mock_refresh):
        mock_get_info.return_value = make_token_info(3 * 86400, refresh_token='rt')
        mock_refresh.return_value = (False, None)
        status = resolve_token_status(user_id=1)
        assert status["refresh_attempted"] is True
        assert status["refresh_succeeded"] is False
        assert status["is_expiring_soon"] is True

    @patch('cqc_lem.utilities.linkedin.token_refresh.attempt_token_refresh')
    @patch('cqc_lem.utilities.db.get_user_token_info')
    def test_no_refresh_token_never_attempts(self, mock_get_info, mock_refresh):
        mock_get_info.return_value = make_token_info(3 * 86400)
        status = resolve_token_status(user_id=1)
        mock_refresh.assert_not_called()
        assert status["refresh_attempted"] is False
        assert status["can_auto_refresh"] is False

    @patch('cqc_lem.utilities.linkedin.token_refresh.attempt_token_refresh')
    @patch('cqc_lem.utilities.db.get_user_token_info')
    def test_auto_refresh_disabled(self, mock_get_info, mock_refresh):
        mock_get_info.return_value = make_token_info(3 * 86400, refresh_token='rt')
        status = resolve_token_status(user_id=1, auto_refresh=False)
        mock_refresh.assert_not_called()
        assert status["refresh_attempted"] is False
        assert status["can_auto_refresh"] is True
