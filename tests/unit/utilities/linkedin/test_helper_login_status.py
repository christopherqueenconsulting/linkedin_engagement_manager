"""The login flow publishes its device-approval state to the SPA (issue #933).

The approval email was the only place LinkedIn's "Did you just try to sign in?" challenge was
ever visible, so a user who had already tapped Yes could not confirm LEM received it. These
tests pin the three transitions the Account page renders.
"""

import pytest
from unittest.mock import MagicMock, call, patch

pytestmark = pytest.mark.unit

_MODULE = "cqc_lem.utilities.linkedin.helper"


@pytest.fixture(autouse=True)
def _no_real_sleep():
    with patch(f"{_MODULE}.time.sleep"):
        yield


@pytest.fixture(autouse=True)
def _breaker_closed():
    with patch(f"{_MODULE}.rate_limit_cooldown_remaining", return_value=0), \
         patch(f"{_MODULE}.mark_rate_limited"), \
         patch(f"{_MODULE}.is_automation_paused", return_value=False), \
         patch(f"{_MODULE}.clear_rate_limit"):
        yield


@pytest.fixture(autouse=True)
def _stub_approval_email():
    with patch("cqc_lem.utilities.email.send_login_approval_email", return_value=True):
        yield


@pytest.fixture
def status_marks():
    with patch(f"{_MODULE}.mark_signed_in") as signed_in, \
         patch(f"{_MODULE}.mark_approval_pending") as pending, \
         patch(f"{_MODULE}.mark_approval_timed_out") as timed_out, \
         patch(f"{_MODULE}._user_id_for_email", return_value=42):
        yield {"signed_in": signed_in, "pending": pending, "timed_out": timed_out}


def _make_driver(url: str) -> MagicMock:
    driver = MagicMock()
    driver.current_url = url
    driver.get_cookies.return_value = [{"name": "li_at", "value": "tok"}]
    return driver


class TestSignedIn:
    def test_resumed_session_records_a_sign_in(self, status_marks, monkeypatch):
        monkeypatch.setenv("LINKEDIN_APPROVAL_WAIT_SECONDS", "0")
        driver = _make_driver("https://www.linkedin.com/feed/")
        with patch(f"{_MODULE}.get_cookies", return_value=[{"name": "x"}]), \
             patch(f"{_MODULE}.load_cookies"), \
             patch(f"{_MODULE}.store_cookies", return_value=True):
            from cqc_lem.utilities.linkedin.helper import login_to_linkedin
            login_to_linkedin(driver, MagicMock(), "user@e.com", "pw")

        status_marks["signed_in"].assert_called_once_with(42)
        status_marks["pending"].assert_not_called()

    def test_recorded_even_when_the_cookie_write_is_lost(self, status_marks, monkeypatch):
        """The approval landed whether or not the cookies persisted — the sign-in is the fact
        the user is asking about, so a failed cookie write must not suppress it."""
        monkeypatch.setenv("LINKEDIN_APPROVAL_WAIT_SECONDS", "0")
        driver = _make_driver("https://www.linkedin.com/feed/")
        with patch(f"{_MODULE}.get_cookies", return_value=[{"name": "x"}]), \
             patch(f"{_MODULE}.load_cookies"), \
             patch(f"{_MODULE}.store_cookies", return_value=False), \
             patch(f"{_MODULE}.log_error"):
            from cqc_lem.utilities.linkedin.helper import login_to_linkedin
            login_to_linkedin(driver, MagicMock(), "user@e.com", "pw")

        status_marks["signed_in"].assert_called_once_with(42)

    def test_unresolvable_user_is_a_silent_skip(self, monkeypatch):
        monkeypatch.setenv("LINKEDIN_APPROVAL_WAIT_SECONDS", "0")
        driver = _make_driver("https://www.linkedin.com/feed/")
        with patch(f"{_MODULE}.mark_signed_in") as signed_in, \
             patch(f"{_MODULE}._user_id_for_email", return_value=None), \
             patch(f"{_MODULE}.get_cookies", return_value=[{"name": "x"}]), \
             patch(f"{_MODULE}.load_cookies"), \
             patch(f"{_MODULE}.store_cookies", return_value=True):
            from cqc_lem.utilities.linkedin.helper import login_to_linkedin
            login_to_linkedin(driver, MagicMock(), "user@e.com", "pw")

        signed_in.assert_not_called()


class TestApprovalChallenge:
    def _run_challenge(self, urls):
        """Drive the challenge path: the driver reports `urls` in order as the wait polls."""
        driver = MagicMock()
        driver.get_cookies.return_value = [{"name": "li_at", "value": "tok"}]
        seq = list(urls)
        type(driver).current_url = property(
            lambda _self: seq.pop(0) if len(seq) > 1 else seq[0])
        with patch(f"{_MODULE}.get_cookies", return_value=[{"name": "x"}]), \
             patch(f"{_MODULE}.load_cookies"), \
             patch(f"{_MODULE}.store_cookies", return_value=True), \
             patch(f"{_MODULE}.solve_arkose_challenge", return_value=False), \
             patch(f"{_MODULE}.drive_email_pin_challenge", return_value=False):
            from cqc_lem.utilities.linkedin.helper import login_to_linkedin
            try:
                login_to_linkedin(driver, MagicMock(), "user@e.com", "pw")
            except RuntimeError:
                pass  # unsolvable challenge — expected on the give-up path

    def test_challenge_publishes_the_pending_approval(self, status_marks, monkeypatch):
        monkeypatch.setenv("LINKEDIN_APPROVAL_WAIT_SECONDS", "0")
        self._run_challenge(["https://www.linkedin.com/checkpoint/challenge"])

        status_marks["pending"].assert_called_once_with(42)

    def test_giving_up_closes_the_pending_record(self, status_marks, monkeypatch):
        """A pending record that only expired would leave the SPA saying "waiting for you" long
        after nothing was waiting."""
        monkeypatch.setenv("LINKEDIN_APPROVAL_WAIT_SECONDS", "0")
        self._run_challenge(["https://www.linkedin.com/checkpoint/challenge"])

        status_marks["timed_out"].assert_called_once_with(42)
        status_marks["signed_in"].assert_not_called()

    def test_approval_lands_and_the_sign_in_is_recorded(self, status_marks, monkeypatch):
        monkeypatch.setenv("LINKEDIN_APPROVAL_WAIT_SECONDS", "60")
        self._run_challenge([
            "https://www.linkedin.com/checkpoint/challenge",  # challenge detected
            "https://www.linkedin.com/feed/",                 # user tapped Yes
        ])

        status_marks["pending"].assert_called_once_with(42)
        status_marks["timed_out"].assert_not_called()
        # Twice for the ONE sign-in: when the approval cleared, then at the cookie persist. The
        # store carries the approval across the second write (test_login_status.py).
        assert status_marks["signed_in"].call_args_list == [call(42), call(42)]

    def test_a_landed_approval_is_recorded_even_if_the_login_dies_after_it(
            self, status_marks, monkeypatch):
        """The user tapped Yes; the login then fell over before it could persist cookies. Leaving
        the record PENDING would keep the Account page asking for an approval already given."""
        monkeypatch.setenv("LINKEDIN_APPROVAL_WAIT_SECONDS", "60")
        with patch(f"{_MODULE}._persist_session_cookies"):
            self._run_challenge([
                "https://www.linkedin.com/checkpoint/challenge",
                "https://www.linkedin.com/feed/",
            ])

        status_marks["signed_in"].assert_called_once_with(42)
        status_marks["timed_out"].assert_not_called()


class TestUserIdLookup:
    def test_lookup_failure_never_raises_into_login(self):
        from cqc_lem.utilities.linkedin.helper import _user_id_for_email
        with patch("cqc_lem.utilities.db.get_user_id", side_effect=RuntimeError("no db")):
            assert _user_id_for_email("user@e.com") is None

    def test_lookup_returns_the_id(self):
        from cqc_lem.utilities.linkedin.helper import _user_id_for_email
        with patch("cqc_lem.utilities.db.get_user_id", return_value=9):
            assert _user_id_for_email("user@e.com") == 9
