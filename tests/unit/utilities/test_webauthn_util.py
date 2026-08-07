"""The WebAuthn seam (issue #745, phase 2c).

The signature maths belongs to py_webauthn and is not re-tested here. What IS tested is everything
the library does not decide: where the relying party lives, that a deployment with no secure public
origin refuses to offer passkeys at all rather than half-offering them, that a rejected ceremony
returns None instead of a half-built credential, and that the sign counter is handed back so the
caller can persist it.
"""

from unittest.mock import patch

import pytest

from cqc_lem.utilities import webauthn_util as wu

pytestmark = pytest.mark.unit

_M = "cqc_lem.utilities.webauthn_util"


class TestRelyingParty:
    def test_the_rp_is_derived_from_the_public_base_url(self):
        """Derived, not separately configured: a passkey registered against one hostname is
        worthless against another, so the two facts must not be able to drift.
        """
        with patch(f"{_M}.PUBLIC_BASE_URL", "https://lem.example.com/"), \
             patch(f"{_M}.WEBAUTHN_RP_ID", ""), patch(f"{_M}.WEBAUTHN_EXTRA_ORIGINS", ""):
            rp = wu.relying_party()
        assert rp.rp_id == "lem.example.com"
        assert rp.origins == ["https://lem.example.com"]

    def test_an_explicit_rp_id_wins_for_a_split_host_setup(self):
        with patch(f"{_M}.PUBLIC_BASE_URL", "https://app.example.com"), \
             patch(f"{_M}.WEBAUTHN_RP_ID", "example.com"), \
             patch(f"{_M}.WEBAUTHN_EXTRA_ORIGINS", ""):
            assert wu.relying_party().rp_id == "example.com"

    def test_extra_origins_are_added_for_local_development(self):
        with patch(f"{_M}.PUBLIC_BASE_URL", "https://lem.example.com"), \
             patch(f"{_M}.WEBAUTHN_RP_ID", ""), \
             patch(f"{_M}.WEBAUTHN_EXTRA_ORIGINS", "http://localhost:5173 , "):
            rp = wu.relying_party()
        assert rp.origins == ["https://lem.example.com", "http://localhost:5173"]

    def test_no_public_origin_means_no_passkeys(self):
        with patch(f"{_M}.PUBLIC_BASE_URL", ""), patch(f"{_M}.WEBAUTHN_RP_ID", ""), \
             patch(f"{_M}.WEBAUTHN_EXTRA_ORIGINS", ""):
            with pytest.raises(wu.WebAuthnUnavailable):
                wu.relying_party()
            assert wu.passkeys_available() is False

    def test_a_plain_http_public_origin_is_refused(self):
        """A browser will not run the ceremony against a non-secure context. Failing here turns an
        unreadable client-side error into a message an operator can act on.
        """
        with patch(f"{_M}.PUBLIC_BASE_URL", "http://lem.example.com"), \
             patch(f"{_M}.WEBAUTHN_RP_ID", ""), patch(f"{_M}.WEBAUTHN_EXTRA_ORIGINS", ""):
            with pytest.raises(wu.WebAuthnUnavailable):
                wu.relying_party()

    def test_localhost_is_a_secure_context(self):
        with patch(f"{_M}.PUBLIC_BASE_URL", ""), patch(f"{_M}.WEBAUTHN_RP_ID", ""), \
             patch(f"{_M}.WEBAUTHN_EXTRA_ORIGINS", "http://localhost:5173"):
            assert wu.passkeys_available() is True


class TestOptions:
    def test_registration_options_exclude_the_passkeys_already_enrolled(self):
        """Without excludeCredentials a user "adds a second passkey", gets the same one back, and
        believes they have a spare.
        """
        with patch(f"{_M}.PUBLIC_BASE_URL", "https://lem.example.com"), \
             patch(f"{_M}.WEBAUTHN_RP_ID", ""), patch(f"{_M}.WEBAUTHN_EXTRA_ORIGINS", ""):
            options, challenge = wu.build_registration_options(
                7, "user@example.com", "user@example.com", ["c2VuZGluZw"])
        assert options["rp"]["id"] == "lem.example.com"
        assert options["user"]["name"] == "user@example.com"
        assert len(options["excludeCredentials"]) == 1
        assert challenge

    def test_an_unparseable_stored_credential_id_is_skipped_not_fatal(self):
        with patch(f"{_M}.PUBLIC_BASE_URL", "https://lem.example.com"), \
             patch(f"{_M}.WEBAUTHN_RP_ID", ""), patch(f"{_M}.WEBAUTHN_EXTRA_ORIGINS", ""):
            options, _ = wu.build_registration_options(7, "u@e.com", "u@e.com", ["aaaaa"])
        assert options["excludeCredentials"] == []

    def test_login_options_name_no_credentials_so_nothing_can_be_probed(self):
        """Username-less by design: asking for the email first would leak which addresses have an
        account.
        """
        with patch(f"{_M}.PUBLIC_BASE_URL", "https://lem.example.com"), \
             patch(f"{_M}.WEBAUTHN_RP_ID", ""), patch(f"{_M}.WEBAUTHN_EXTRA_ORIGINS", ""):
            options, challenge = wu.build_authentication_options()
        assert options["rpId"] == "lem.example.com"
        assert not options.get("allowCredentials")
        assert challenge

    def test_step_up_options_are_scoped_to_the_users_own_credentials(self):
        with patch(f"{_M}.PUBLIC_BASE_URL", "https://lem.example.com"), \
             patch(f"{_M}.WEBAUTHN_RP_ID", ""), patch(f"{_M}.WEBAUTHN_EXTRA_ORIGINS", ""):
            options, _ = wu.build_authentication_options(["c2VuZGluZw"])
        assert len(options["allowCredentials"]) == 1


class TestVerification:
    def test_a_rejected_registration_returns_none(self):
        with patch(f"{_M}.PUBLIC_BASE_URL", "https://lem.example.com"), \
             patch(f"{_M}.WEBAUTHN_RP_ID", ""), patch(f"{_M}.WEBAUTHN_EXTRA_ORIGINS", ""), \
             patch(f"{_M}.verify_registration_response", side_effect=ValueError("bad attestation")):
            assert wu.verify_registration({"id": "x"}, "Y2hhbGxlbmdl") is None

    def test_a_verified_registration_is_returned_base64url_encoded(self):
        class _Verified:
            credential_id = b"\x01\x02\x03"
            credential_public_key = b"\x04\x05"
            sign_count = 4

        with patch(f"{_M}.PUBLIC_BASE_URL", "https://lem.example.com"), \
             patch(f"{_M}.WEBAUTHN_RP_ID", ""), patch(f"{_M}.WEBAUTHN_EXTRA_ORIGINS", ""), \
             patch(f"{_M}.verify_registration_response", return_value=_Verified()):
            result = wu.verify_registration({"id": "x"}, "Y2hhbGxlbmdl")
        assert result.credential_id == "AQID"
        assert result.sign_count == 4

    def test_a_rejected_assertion_returns_none(self):
        """py_webauthn raises on a bad signature AND on a sign counter that went backwards — the
        standard tell for a cloned authenticator. Both must land here.
        """
        with patch(f"{_M}.PUBLIC_BASE_URL", "https://lem.example.com"), \
             patch(f"{_M}.WEBAUTHN_RP_ID", ""), patch(f"{_M}.WEBAUTHN_EXTRA_ORIGINS", ""), \
             patch(f"{_M}.verify_authentication_response",
                   side_effect=ValueError("sign count did not increase")):
            assert wu.verify_assertion({"id": "x"}, "Y2hhbGxlbmdl", "cGs", 9) is None

    def test_a_verified_assertion_hands_back_the_new_counter(self):
        class _Verified:
            new_sign_count = 11

        with patch(f"{_M}.PUBLIC_BASE_URL", "https://lem.example.com"), \
             patch(f"{_M}.WEBAUTHN_RP_ID", ""), patch(f"{_M}.WEBAUTHN_EXTRA_ORIGINS", ""), \
             patch(f"{_M}.verify_authentication_response", return_value=_Verified()):
            assert wu.verify_assertion({"id": "x"}, "Y2hhbGxlbmdl", "cGs", 9) == 11


class TestCredentialId:
    @pytest.mark.parametrize("payload,expected", [
        ({"id": "abc"}, "abc"),
        ({"rawId": "abc"}, "abc"),
        ({"id": "  abc  "}, "abc"),
        ({}, None),
        ({"id": ""}, None),
        ({"id": 7}, None),
        (None, None),
    ])
    def test_only_a_usable_credential_id_is_returned(self, payload, expected):
        assert wu.credential_id_from_response(payload) == expected
