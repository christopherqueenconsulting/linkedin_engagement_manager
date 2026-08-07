"""Strong-factor policy and the step-up verdict (issue #745, phase 2c).

These cover the decisions, not the plumbing: when the email PIN stops being a way in, which factors
are offered, what makes a TOTP code single-use, that a recovery code is spent exactly once, and the
four ways a request may pass the step-up gate — plus, more importantly, the ways it must not.
"""

import time
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pyotp
import pytest

from cqc_lem.utilities import auth_factors as af

pytestmark = pytest.mark.unit

_M = "cqc_lem.utilities.auth_factors"
_UID = 42
_TOKEN = "session-token"


# ---------------------------------------------------------------------------
# Does this account hold a strong factor, and what can finish a login?
# ---------------------------------------------------------------------------

class TestFactorState:
    def test_no_factor_means_the_pin_is_still_a_full_login(self):
        with patch(f"{_M}.count_auth_factors", return_value=0):
            assert af.has_strong_factor(_UID) is False

    def test_one_confirmed_factor_demotes_the_pin(self):
        with patch(f"{_M}.count_auth_factors", return_value=1):
            assert af.has_strong_factor(_UID) is True

    def test_the_kill_switch_returns_the_account_to_2b_behaviour(self):
        """STRONG_AUTH_ENABLED=false must not read as "this account has no factor and never did" —
        it has to leave the enrolled rows alone and simply stop enforcing.
        """
        with patch(f"{_M}.STRONG_AUTH_ENABLED", False), \
             patch(f"{_M}.count_auth_factors", return_value=3) as counted:
            assert af.has_strong_factor(_UID) is False
            assert af.available_methods(_UID) == []
        counted.assert_not_called()

    def test_recovery_codes_alone_are_not_a_strong_factor(self):
        """A user who saved codes but enrolled nothing else must keep their PIN login — otherwise
        the demotion happens with nothing to replace it.
        """
        with patch(f"{_M}.count_auth_factors", return_value=0), \
             patch(f"{_M}.count_recovery_codes", return_value=(10, 10)):
            assert af.has_strong_factor(_UID) is False

    def test_methods_are_only_what_the_account_can_actually_use(self):
        with patch(f"{_M}.list_auth_factors", return_value=[{"kind": "totp"}]), \
             patch(f"{_M}.count_recovery_codes", return_value=(0, 0)):
            assert af.available_methods(_UID) == [af.METHOD_TOTP]

    def test_passkeys_disappear_when_the_deployment_cannot_serve_them(self):
        """...but the recovery codes do NOT. A deployment that loses its secure public origin
        drops `passkey` from the list, and an account whose only factor is a passkey would then be
        offered nothing at all — a demoted PIN with no way to finish it is a total lockout, which
        is the exact thing the sheet of codes exists to prevent.
        """
        with patch(f"{_M}.list_auth_factors", return_value=[{"kind": "passkey"}]), \
             patch("cqc_lem.utilities.webauthn_util.passkeys_available", return_value=False), \
             patch(f"{_M}.count_recovery_codes", return_value=(10, 10)):
            assert af.available_methods(_UID) == [af.METHOD_RECOVERY]

    def test_an_account_with_no_factor_is_offered_nothing_even_holding_codes(self):
        """The other direction: recovery codes are not a factor, so they never appear on their own
        — an account with none of the real ones never reaches the second stage at all.
        """
        with patch(f"{_M}.list_auth_factors", return_value=[]), \
             patch(f"{_M}.count_recovery_codes", return_value=(10, 10)):
            assert af.available_methods(_UID) == []

    def test_spent_recovery_codes_are_not_offered(self):
        with patch(f"{_M}.list_auth_factors", return_value=[{"kind": "totp"}]), \
             patch(f"{_M}.count_recovery_codes", return_value=(0, 10)):
            assert af.METHOD_RECOVERY not in af.available_methods(_UID)

    def test_summary_reports_the_demotion_the_user_needs_to_see(self):
        with patch(f"{_M}.list_auth_factors", return_value=[
                    {"id": 1, "kind": "passkey", "label": "iPhone", "created_at": None,
                     "last_used_at": None}]), \
             patch(f"{_M}.count_recovery_codes", return_value=(8, 10)), \
             patch("cqc_lem.utilities.webauthn_util.passkeys_available", return_value=True):
            summary = af.factor_summary(_UID)
        assert summary.has_strong_factor is True
        assert summary.pin_is_bootstrap_only is True
        assert summary.recovery_unused == 8


# ---------------------------------------------------------------------------
# TOTP
# ---------------------------------------------------------------------------

class TestTotp:
    def test_enrollment_starts_unconfirmed_and_returns_a_scannable_uri(self):
        with patch(f"{_M}.get_totp_factor", return_value=None), \
             patch(f"{_M}.upsert_totp_factor", return_value=9) as upsert:
            factor_id, secret, uri = af.begin_totp_enrollment(_UID, "user@example.com")
        assert factor_id == 9
        assert uri.startswith("otpauth://totp/")
        assert secret in uri
        upsert.assert_called_once_with(_UID, secret)

    def test_a_second_enrollment_is_refused_while_one_is_confirmed(self):
        """One authenticator per account. Restarting enrolment only ever replaces an UNCONFIRMED
        attempt — letting it run against a confirmed factor leaves two rows of which only the
        newer one is ever verified.
        """
        with patch(f"{_M}.get_totp_factor",
                   return_value={"id": 9, "secret": "S", "confirmed_at": "now"}), \
             patch(f"{_M}.upsert_totp_factor") as upsert:
            assert af.begin_totp_enrollment(_UID, "user@example.com") is None
        upsert.assert_not_called()

    def test_a_correct_code_confirms_the_seed(self):
        secret = pyotp.random_base32()
        with patch(f"{_M}.get_totp_factor",
                   return_value={"id": 9, "secret": secret, "confirmed_at": None}), \
             patch(f"{_M}.confirm_totp_factor", return_value=True) as confirm, \
             patch(f"{_M}.update_factor_counter") as counter:
            assert af.confirm_totp_enrollment(_UID, pyotp.TOTP(secret).now()) is True
        confirm.assert_called_once_with(9, _UID)
        counter.assert_called_once()

    def test_an_already_confirmed_factor_cannot_be_re_confirmed(self):
        secret = pyotp.random_base32()
        with patch(f"{_M}.get_totp_factor", return_value={
                    "id": 9, "secret": secret, "confirmed_at": datetime.now(timezone.utc)}), \
             patch(f"{_M}.confirm_totp_factor") as confirm:
            assert af.confirm_totp_enrollment(_UID, pyotp.TOTP(secret).now()) is False
        confirm.assert_not_called()

    def test_a_wrong_code_is_refused(self):
        secret = pyotp.random_base32()
        with patch(f"{_M}.get_totp_factor",
                   return_value={"id": 9, "secret": secret, "confirmed_at": None}):
            assert af.confirm_totp_enrollment(_UID, "000000") is False

    def test_non_numeric_input_never_reaches_the_comparison(self):
        assert af._match_totp_step(pyotp.random_base32(), "abc def") is None

    def test_a_code_from_the_previous_window_still_works(self):
        """A phone whose clock is 20 seconds slow is a support ticket, not an attack."""
        secret = pyotp.random_base32()
        earlier = pyotp.TOTP(secret).at(int(time.time()) - 30)
        with patch(f"{_M}.get_totp_factor",
                   return_value={"id": 9, "secret": secret, "sign_count": 0}), \
             patch(f"{_M}.update_factor_counter"):
            assert af.verify_totp_code(_UID, earlier) is True

    def test_the_same_code_cannot_be_used_twice(self):
        """The replay guard: a code stays valid for up to 90 seconds across the drift window, which
        is ample time for a phishing proxy to relay it a second time.
        """
        secret = pyotp.random_base32()
        code = pyotp.TOTP(secret).now()
        spent_step = int(time.time()) // 30
        with patch(f"{_M}.get_totp_factor",
                   return_value={"id": 9, "secret": secret, "sign_count": spent_step}), \
             patch(f"{_M}.update_factor_counter") as counter:
            assert af.verify_totp_code(_UID, code) is False
        counter.assert_not_called()

    def test_the_accepted_step_is_persisted_so_the_next_replay_fails(self):
        secret = pyotp.random_base32()
        with patch(f"{_M}.get_totp_factor",
                   return_value={"id": 9, "secret": secret, "sign_count": 0}), \
             patch(f"{_M}.update_factor_counter") as counter:
            assert af.verify_totp_code(_UID, pyotp.TOTP(secret).now()) is True
        counter.assert_called_once()
        assert counter.call_args[0][1] >= int(time.time()) // 30 - 1

    def test_an_undecryptable_secret_verifies_nothing(self):
        """`get_totp_factor` returns None when the envelope will not open — a caller that compared
        a code against ciphertext would reject every valid one and look like a broken phone.
        """
        with patch(f"{_M}.get_totp_factor", return_value=None):
            assert af.verify_totp_code(_UID, "123456") is False


# ---------------------------------------------------------------------------
# Recovery codes
# ---------------------------------------------------------------------------

class TestRecoveryCodes:
    def test_a_fresh_sheet_is_hashed_before_it_is_stored(self):
        with patch(f"{_M}.replace_recovery_codes", return_value=True) as store:
            codes = af.generate_recovery_codes(_UID)
        assert len(codes) == af.RECOVERY_CODE_COUNT
        stored_hashes = store.call_args[0][1]
        assert len(stored_hashes) == len(codes)
        for code, code_hash in zip(codes, stored_hashes):
            assert code not in code_hash
            assert code_hash.startswith("$argon2")

    def test_codes_avoid_the_characters_people_mistype(self):
        with patch(f"{_M}.replace_recovery_codes", return_value=True):
            codes = af.generate_recovery_codes(_UID)
        assert not set("".join(codes)) & set("01IO")

    def test_a_failed_write_returns_no_codes_rather_than_codes_that_do_not_work(self):
        with patch(f"{_M}.replace_recovery_codes", return_value=False):
            assert af.generate_recovery_codes(_UID) == []

    def test_a_valid_code_is_spent_exactly_once(self):
        code = "ABCD234XYZ"
        stored = af._hasher.hash(code)
        with patch(f"{_M}.get_unused_recovery_codes", return_value=[{"id": 3, "code_hash": stored}]), \
             patch(f"{_M}.consume_recovery_code", return_value=True) as consume:
            assert af.verify_recovery_code(_UID, code) is True
        consume.assert_called_once_with(_UID, 3)

    def test_formatting_the_user_added_does_not_break_the_match(self):
        code = "ABCD234XYZ"
        stored = af._hasher.hash(code)
        with patch(f"{_M}.get_unused_recovery_codes", return_value=[{"id": 3, "code_hash": stored}]), \
             patch(f"{_M}.consume_recovery_code", return_value=True):
            assert af.verify_recovery_code(_UID, " abcd-234 xyz ") is True

    def test_losing_the_race_to_consume_is_a_refusal(self):
        """Two requests presenting the same code: the UPDATE only matches once, and the loser must
        be told no rather than let through on a code someone else just spent.
        """
        code = "ABCD234XYZ"
        stored = af._hasher.hash(code)
        with patch(f"{_M}.get_unused_recovery_codes", return_value=[{"id": 3, "code_hash": stored}]), \
             patch(f"{_M}.consume_recovery_code", return_value=False):
            assert af.verify_recovery_code(_UID, code) is False

    def test_a_wrong_code_matches_nothing(self):
        stored = af._hasher.hash("ABCD234XYZ")
        with patch(f"{_M}.get_unused_recovery_codes", return_value=[{"id": 3, "code_hash": stored}]), \
             patch(f"{_M}.consume_recovery_code") as consume:
            assert af.verify_recovery_code(_UID, "ZZZZ999ZZZ") is False
        consume.assert_not_called()

    def test_an_empty_code_is_refused_without_touching_the_database(self):
        with patch(f"{_M}.get_unused_recovery_codes") as unused:
            assert af.verify_recovery_code(_UID, "   ") is False
        unused.assert_not_called()

    def test_an_unreadable_stored_hash_is_skipped_not_fatal(self):
        good = af._hasher.hash("ABCD234XYZ")
        with patch(f"{_M}.get_unused_recovery_codes",
                   return_value=[{"id": 1, "code_hash": "not-a-hash"},
                                 {"id": 2, "code_hash": good}]), \
             patch(f"{_M}.consume_recovery_code", return_value=True) as consume:
            assert af.verify_recovery_code(_UID, "ABCD234XYZ") is True
        consume.assert_called_once_with(_UID, 2)


# ---------------------------------------------------------------------------
# The step-up gate
# ---------------------------------------------------------------------------

def _session(minutes_ago=None, scope="full"):
    verified = (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
                if minutes_ago is not None else None)
    return {"user_id": _UID, "last_verified_at": verified, "scope": scope}


class TestStepUpGate:
    def test_an_account_with_no_factor_passes(self):
        """Opt-in rollout: gating accounts that have nothing to prove with would brick the cookie
        paste and the email change for everyone who has not enrolled yet.
        """
        with patch(f"{_M}.count_auth_factors", return_value=0), \
             patch(f"{_M}.get_session_auth_state") as state:
            assert af.step_up_satisfied(_UID, _TOKEN) is True
        state.assert_not_called()

    def test_the_kill_switch_passes_everything(self):
        with patch(f"{_M}.STRONG_AUTH_ENABLED", False):
            assert af.step_up_satisfied(_UID, None) is True

    def test_a_recently_verified_session_passes(self):
        with patch(f"{_M}.count_auth_factors", return_value=1), \
             patch(f"{_M}.get_session_auth_state", return_value=_session(minutes_ago=1)):
            assert af.step_up_satisfied(_UID, _TOKEN) is True

    def test_a_stale_verification_does_not(self):
        with patch(f"{_M}.count_auth_factors", return_value=1), \
             patch(f"{_M}.get_session_auth_state",
                   return_value=_session(minutes_ago=af.STEP_UP_MAX_AGE_MINUTES + 1)):
            assert af.step_up_satisfied(_UID, _TOKEN) is False

    def test_a_session_that_never_verified_does_not(self):
        with patch(f"{_M}.count_auth_factors", return_value=1), \
             patch(f"{_M}.get_session_auth_state", return_value=_session()):
            assert af.step_up_satisfied(_UID, _TOKEN) is False

    def test_a_naive_timestamp_from_mysql_is_read_as_utc(self):
        """MySQL hands back tz-naive datetimes. Treating one as local time would make a fresh
        verification look hours old (or hours in the future) depending on the host.
        """
        naive = (datetime.now(timezone.utc) - timedelta(minutes=1)).replace(tzinfo=None)
        with patch(f"{_M}.count_auth_factors", return_value=1), \
             patch(f"{_M}.get_session_auth_state",
                   return_value={"last_verified_at": naive, "scope": "full"}):
            assert af.step_up_satisfied(_UID, _TOKEN) is True

    def test_an_unknown_or_revoked_session_does_not(self):
        with patch(f"{_M}.count_auth_factors", return_value=1), \
             patch(f"{_M}.get_session_auth_state", return_value=None):
            assert af.step_up_satisfied(_UID, _TOKEN) is False

    def test_no_token_at_all_does_not(self):
        with patch(f"{_M}.count_auth_factors", return_value=1):
            assert af.step_up_satisfied(_UID, None) is False

    def test_the_extension_scoped_session_passes_where_it_is_opted_in(self):
        """The extension POSTs li_at holding nothing but its own token and cannot run WebAuthn. Its
        step-up happened once, in the SPA, when that token was minted (design §6.5).
        """
        with patch(f"{_M}.count_auth_factors", return_value=1), \
             patch(f"{_M}.get_session_auth_state",
                   return_value=_session(scope=af.SESSION_SCOPE_EXTENSION)):
            assert af.step_up_satisfied(_UID, _TOKEN, extension_scope_ok=True) is True

    def test_the_extension_scope_is_not_a_blanket_exemption(self):
        """An extension token is otherwise an ordinary session. If the scope satisfied the gate
        everywhere, a stolen one could change the email address and revoke every device — the exact
        escalation the gate exists to stop.
        """
        with patch(f"{_M}.count_auth_factors", return_value=1), \
             patch(f"{_M}.get_session_auth_state",
                   return_value=_session(scope=af.SESSION_SCOPE_EXTENSION)):
            assert af.step_up_satisfied(_UID, _TOKEN) is False

    def test_recording_a_step_up_needs_a_token(self):
        with patch(f"{_M}.mark_session_verified", return_value=True) as mark:
            assert af.record_step_up(None) is False
            mark.assert_not_called()
            assert af.record_step_up(_TOKEN) is True


# ---------------------------------------------------------------------------
# May this session ADD a factor?
# ---------------------------------------------------------------------------

class TestEnrollmentGate:
    def test_an_account_with_no_factor_may_always_enrol(self):
        """The bootstrap case, and the reason the gate cannot simply be step-up: an account that
        holds nothing has nothing to prove with, so gating here would mean it never gets a first
        factor at all.
        """
        with patch(f"{_M}.count_auth_factors", return_value=0), \
             patch(f"{_M}.get_session_auth_state") as state:
            assert af.enrollment_allowed(_UID, _TOKEN) is True
        state.assert_not_called()

    def test_adding_another_one_needs_a_proved_factor(self):
        """Enrolment stamps the session as verified. Leaving it open would hand a stolen session a
        step-up it never proved — enrol your own passkey, then read the LinkedIn cookie (T4).
        """
        with patch(f"{_M}.count_auth_factors", return_value=1), \
             patch(f"{_M}.get_session_auth_state", return_value=_session()):
            assert af.enrollment_allowed(_UID, _TOKEN) is False

    def test_a_freshly_verified_session_may_add_another(self):
        with patch(f"{_M}.count_auth_factors", return_value=1), \
             patch(f"{_M}.get_session_auth_state", return_value=_session(minutes_ago=1)):
            assert af.enrollment_allowed(_UID, _TOKEN) is True

    def test_a_recovery_code_session_may_enrol_without_proving_anything(self):
        """The anti-lockout path (design §6.8): the only person who genuinely cannot prove a factor
        is the one who lost it, and this is how they get a new one.
        """
        with patch(f"{_M}.count_auth_factors", return_value=1), \
             patch(f"{_M}.get_session_auth_state",
                   return_value=_session(scope=af.SESSION_SCOPE_RECOVERY)):
            assert af.enrollment_allowed(_UID, _TOKEN) is True

    def test_the_extension_scope_is_not_an_enrolment_pass(self):
        """It is opted into at exactly one endpoint, and that endpoint is not this one."""
        with patch(f"{_M}.count_auth_factors", return_value=1), \
             patch(f"{_M}.get_session_auth_state",
                   return_value=_session(scope=af.SESSION_SCOPE_EXTENSION)):
            assert af.enrollment_allowed(_UID, _TOKEN) is False

    def test_the_kill_switch_leaves_enrolment_open(self):
        with patch(f"{_M}.STRONG_AUTH_ENABLED", False):
            assert af.enrollment_allowed(_UID, None) is True

    def test_an_unknown_session_is_not_a_recovery_session(self):
        with patch(f"{_M}.get_session_auth_state", return_value=None):
            assert af.session_signed_in_with_recovery_code(_TOKEN) is False
        assert af.session_signed_in_with_recovery_code(None) is False


# ---------------------------------------------------------------------------
# Mandatory enrolment — the deadline (issue #905, design §7 Stage 2)
# ---------------------------------------------------------------------------

def _after(value: str):
    """REQUIRE_STRONG_FACTOR_AFTER is read at the call site, so the environment IS the control."""
    return patch.dict("os.environ", {"REQUIRE_STRONG_FACTOR_AFTER": value})


class TestMandatoryEnrollment:
    def test_no_date_is_the_default_and_means_never(self):
        with patch.dict("os.environ", {}, clear=False):
            import os
            os.environ.pop("REQUIRE_STRONG_FACTOR_AFTER", None)
            assert af.strong_factor_deadline() is None
            assert af.enrollment_hold_active() is False
            assert af.enrollment_required(_UID) is False

    def test_a_bare_date_is_midnight_utc(self):
        with _after("2026-10-01"):
            deadline = af.strong_factor_deadline()
        assert deadline == datetime(2026, 10, 1, tzinfo=timezone.utc)

    def test_a_full_timestamp_is_accepted_and_z_is_utc(self):
        with _after("2026-10-01T12:30:00Z"):
            assert af.strong_factor_deadline() == datetime(2026, 10, 1, 12, 30, tzinfo=timezone.utc)

    def test_a_naive_timestamp_is_read_as_utc(self):
        """Everything else in the auth surface is UTC; guessing the host's zone here would move the
        cutover by hours depending on where the container runs.
        """
        with _after("2026-10-01T12:30:00"):
            assert af.strong_factor_deadline().tzinfo is not None

    def test_an_unparseable_date_is_unset_and_warns(self):
        """Failing closed on a typo would force every account in the deployment into enrolment."""
        with _after("october-ish"), patch(f"{_M}.log_warning") as warned:
            assert af.strong_factor_deadline() is None
        assert warned.call_count == 1

    def test_a_future_date_holds_nobody_yet(self):
        with _after("2099-01-01"), patch(f"{_M}.count_auth_factors", return_value=0):
            assert af.enrollment_hold_active() is False
            assert af.enrollment_required(_UID) is False
            # ...but the pre-deadline nudge is due the moment a date exists.
            assert af.strong_factor_prompt_due(_UID) is True

    def test_past_the_date_an_account_with_nothing_must_enrol(self):
        with _after("2020-01-01"), patch(f"{_M}.count_auth_factors", return_value=0):
            assert af.enrollment_hold_active() is True
            assert af.enrollment_required(_UID) is True

    def test_an_account_that_already_enrolled_is_never_held(self):
        with _after("2020-01-01"), patch(f"{_M}.count_auth_factors", return_value=1):
            assert af.enrollment_required(_UID) is False
            # Nothing to nudge about either — enrolling is what ends the prompt for good.
            assert af.strong_factor_prompt_due(_UID) is False

    def test_the_kill_switch_beats_the_deadline(self):
        """STRONG_AUTH_ENABLED=false is the 2c rollback, and it has to roll this back with it —
        otherwise disabling strong auth would leave people held at a screen that cannot help them.
        It releases sessions ALREADY held, not just future logins, because every read goes through
        enrollment_hold_active().
        """
        with _after("2020-01-01"), patch(f"{_M}.STRONG_AUTH_ENABLED", False):
            assert af.enrollment_hold_active() is False
            assert af.enrollment_required(_UID) is False
            assert af.strong_factor_prompt_due(_UID) is False

    def test_clearing_the_date_releases_the_hold(self):
        with _after(""), patch(f"{_M}.count_auth_factors", return_value=0):
            assert af.enrollment_hold_active() is False

    def test_no_prompt_without_a_scheduled_date(self):
        with _after(""), patch(f"{_M}.count_auth_factors", return_value=0):
            assert af.strong_factor_prompt_due(_UID) is False
