"""Unit tests for envelope encryption of secrets at rest (issue #745, PR 2a).

The security properties under test are the ones the design promises (docs/AUTH_SECURITY_DESIGN.md
§5.1): round-trip, row-binding AAD (a ciphertext cannot be grafted onto another user or column),
tamper detection, key rotation via the version tag, and a dual-mode read that keeps a rollback safe.
"""

import base64
import os
from unittest.mock import patch

import pytest

from cqc_lem.utilities.crypto import (
    SecretEncryptionError,
    decrypt_secret,
    encrypt_secret,
    encryption_enabled,
    encryption_required,
    envelope_key_version,
    generate_master_key,
    is_encrypted,
    needs_reencrypt,
)

pytestmark = pytest.mark.unit

KEY_A = base64.urlsafe_b64encode(b"A" * 32).decode()
KEY_B = base64.urlsafe_b64encode(b"B" * 32).decode()
FIELD = "cookies.value"


@pytest.fixture
def keyed(monkeypatch):
    monkeypatch.setenv("LEM_SECRET_KEY", KEY_A)
    monkeypatch.setenv("LEM_SECRET_KEY_VERSION", "1")
    monkeypatch.delenv("LEM_SECRET_KEY_PREVIOUS", raising=False)
    monkeypatch.delenv("ENCRYPTION_REQUIRED", raising=False)
    return KEY_A


@pytest.fixture
def keyless(monkeypatch):
    monkeypatch.delenv("LEM_SECRET_KEY", raising=False)
    monkeypatch.delenv("LEM_SECRET_KEY_PREVIOUS", raising=False)
    monkeypatch.delenv("ENCRYPTION_REQUIRED", raising=False)


class TestRoundTrip:
    def test_round_trip_returns_the_original(self, keyed):
        blob = encrypt_secret("AQEDA-li_at-value", 7, FIELD)
        assert blob != "AQEDA-li_at-value"
        assert is_encrypted(blob)
        assert decrypt_secret(blob, 7, FIELD) == "AQEDA-li_at-value"

    def test_envelope_shape_is_self_describing(self, keyed):
        blob = encrypt_secret("secret", 7, FIELD)
        marker, version, nonce, payload = blob.split(":")
        assert marker == "lemv1"
        assert version == "1"
        assert nonce and payload
        assert envelope_key_version(blob) == 1

    def test_nonce_is_random_so_equal_plaintexts_differ(self, keyed):
        assert encrypt_secret("same", 7, FIELD) != encrypt_secret("same", 7, FIELD)

    def test_unicode_survives(self, keyed):
        assert decrypt_secret(encrypt_secret("pässwörd–ü", 3, FIELD), 3, FIELD) == "pässwörd–ü"

    @pytest.mark.parametrize("value", [None, ""])
    def test_empty_values_pass_through_untouched(self, keyed, value):
        assert encrypt_secret(value, 7, FIELD) == value
        assert decrypt_secret(value, 7, FIELD) == value

    def test_encrypt_is_idempotent(self, keyed):
        once = encrypt_secret("secret", 7, FIELD)
        assert encrypt_secret(once, 7, FIELD) == once


class TestRowBinding:
    """AAD binds the ciphertext to (column, user, key version) — the property that stops an
    attacker with MySQL WRITE access from grafting one user's LinkedIn session onto another."""

    def test_other_users_row_cannot_decrypt_it(self, keyed):
        blob = encrypt_secret("victim-li-at", 7, FIELD)
        assert decrypt_secret(blob, 8, FIELD) is None

    def test_other_column_cannot_decrypt_it(self, keyed):
        blob = encrypt_secret("victim-li-at", 7, FIELD)
        assert decrypt_secret(blob, 7, "users.password") is None

    def test_tampered_ciphertext_is_rejected(self, keyed):
        marker, version, nonce, payload = encrypt_secret("secret", 7, FIELD).split(":")
        flipped = payload[:-2] + ("AA" if not payload.endswith("AA") else "BB")
        assert decrypt_secret(f"{marker}:{version}:{nonce}:{flipped}", 7, FIELD) is None

    def test_failure_never_returns_the_ciphertext(self, keyed):
        """A caller that got the envelope back would type it into LinkedIn's login form."""
        blob = encrypt_secret("secret", 7, FIELD)
        assert decrypt_secret(blob, 999, FIELD) is None

    def test_decrypt_without_user_id_fails_closed(self, keyed):
        blob = encrypt_secret("secret", 7, FIELD)
        assert decrypt_secret(blob, None, FIELD) is None

    def test_malformed_envelope_is_rejected(self, keyed):
        assert decrypt_secret("lemv1:1:onlythree", 7, FIELD) is None
        assert decrypt_secret("lemv1:notanint:aa:bb", 7, FIELD) is None

    def test_undecodable_payload_is_rejected(self, keyed):
        """Garbage that survives the shape check still must not raise into db.py."""
        assert decrypt_secret("lemv1:1:@@@@:####", 7, FIELD) is None


class TestDualModeAndKeylessOperation:
    def test_legacy_plaintext_reads_through(self, keyed):
        """Stage 0 of the migration: rows not yet backfilled must stay readable."""
        assert decrypt_secret("legacy-plaintext-li-at", 7, FIELD) == "legacy-plaintext-li-at"

    def test_no_key_leaves_values_untouched(self, keyless):
        assert not encryption_enabled()
        assert encrypt_secret("secret", 7, FIELD) == "secret"
        assert decrypt_secret("secret", 7, FIELD) == "secret"

    def test_encrypted_value_without_a_key_is_never_returned_raw(self, keyed, monkeypatch):
        blob = encrypt_secret("secret", 7, FIELD)
        monkeypatch.delenv("LEM_SECRET_KEY")
        assert decrypt_secret(blob, 7, FIELD) is None

    def test_missing_user_id_stores_as_is_rather_than_under_a_shared_key(self, keyed):
        assert encrypt_secret("orphan-cookie", None, FIELD) == "orphan-cookie"

    def test_invalid_key_is_refused_rather_than_truncated(self, monkeypatch):
        monkeypatch.setenv("LEM_SECRET_KEY", "too-short")
        monkeypatch.delenv("ENCRYPTION_REQUIRED", raising=False)
        assert not encryption_enabled()
        assert encrypt_secret("secret", 7, FIELD) == "secret"

    def test_hex_key_is_accepted(self, monkeypatch):
        monkeypatch.setenv("LEM_SECRET_KEY", "ab" * 32)
        monkeypatch.delenv("LEM_SECRET_KEY_PREVIOUS", raising=False)
        assert encryption_enabled()
        assert decrypt_secret(encrypt_secret("x", 1, FIELD), 1, FIELD) == "x"

    def test_generated_key_is_usable(self, monkeypatch):
        monkeypatch.setenv("LEM_SECRET_KEY", generate_master_key())
        monkeypatch.delenv("LEM_SECRET_KEY_PREVIOUS", raising=False)
        assert decrypt_secret(encrypt_secret("x", 1, FIELD), 1, FIELD) == "x"

    def test_non_numeric_version_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("LEM_SECRET_KEY", KEY_A)
        monkeypatch.setenv("LEM_SECRET_KEY_VERSION", "not-a-number")
        assert envelope_key_version(encrypt_secret("x", 1, FIELD)) == 1


class TestFailClosedMode:
    def test_plaintext_read_fails_closed_when_required(self, keyed, monkeypatch):
        monkeypatch.setenv("ENCRYPTION_REQUIRED", "true")
        assert encryption_required()
        assert decrypt_secret("legacy-plaintext", 7, FIELD) is None
        # ...and an actual envelope still reads fine.
        assert decrypt_secret(encrypt_secret("s", 7, FIELD), 7, FIELD) == "s"

    def test_missing_key_raises_rather_than_writing_plaintext(self, keyless, monkeypatch):
        monkeypatch.setenv("ENCRYPTION_REQUIRED", "true")
        with pytest.raises(SecretEncryptionError):
            encrypt_secret("secret", 7, FIELD)

    def test_missing_user_id_raises_rather_than_writing_plaintext(self, keyed, monkeypatch):
        """An unbindable row is the other way plaintext could reach MySQL in fail-closed mode —
        a cookie stored for an email with no user row would otherwise land in the clear."""
        monkeypatch.setenv("ENCRYPTION_REQUIRED", "true")
        with pytest.raises(SecretEncryptionError):
            encrypt_secret("orphan-cookie", None, FIELD)


class TestRotation:
    def test_previous_key_still_decrypts_after_rotation(self, keyed, monkeypatch):
        old_blob = encrypt_secret("secret", 7, FIELD)
        monkeypatch.setenv("LEM_SECRET_KEY", KEY_B)
        monkeypatch.setenv("LEM_SECRET_KEY_VERSION", "2")
        monkeypatch.setenv("LEM_SECRET_KEY_PREVIOUS", KEY_A)
        monkeypatch.setenv("LEM_SECRET_KEY_PREVIOUS_VERSION", "1")

        assert decrypt_secret(old_blob, 7, FIELD) == "secret"
        assert needs_reencrypt(old_blob) is True

        rotated = encrypt_secret(decrypt_secret(old_blob, 7, FIELD), 7, FIELD)
        assert envelope_key_version(rotated) == 2
        assert needs_reencrypt(rotated) is False

    def test_previous_version_defaults_to_one_below_current(self, keyed, monkeypatch):
        old_blob = encrypt_secret("secret", 7, FIELD)
        monkeypatch.setenv("LEM_SECRET_KEY", KEY_B)
        monkeypatch.setenv("LEM_SECRET_KEY_VERSION", "2")
        monkeypatch.setenv("LEM_SECRET_KEY_PREVIOUS", KEY_A)
        assert decrypt_secret(old_blob, 7, FIELD) == "secret"

    def test_previous_key_never_takes_the_current_version_slot(self, keyed, monkeypatch):
        """A misconfigured rotation that leaves both keys claiming one version must not seal NEW
        writes under the OLD key: needs_reencrypt() would call them current, and removing
        LEM_SECRET_KEY_PREVIOUS would then make every one of them undecryptable forever."""
        monkeypatch.setenv("LEM_SECRET_KEY", KEY_B)
        monkeypatch.setenv("LEM_SECRET_KEY_VERSION", "1")
        monkeypatch.setenv("LEM_SECRET_KEY_PREVIOUS", KEY_A)
        monkeypatch.setenv("LEM_SECRET_KEY_PREVIOUS_VERSION", "1")
        fresh = encrypt_secret("secret", 7, FIELD)

        # Step 5 of the documented rotation: the previous key goes away.
        monkeypatch.delenv("LEM_SECRET_KEY_PREVIOUS")
        monkeypatch.delenv("LEM_SECRET_KEY_PREVIOUS_VERSION")
        assert decrypt_secret(fresh, 7, FIELD) == "secret"

    def test_unknown_key_version_is_not_guessed(self, keyed, monkeypatch):
        old_blob = encrypt_secret("secret", 7, FIELD)
        monkeypatch.setenv("LEM_SECRET_KEY", KEY_B)
        monkeypatch.setenv("LEM_SECRET_KEY_VERSION", "2")
        monkeypatch.delenv("LEM_SECRET_KEY_PREVIOUS", raising=False)
        assert decrypt_secret(old_blob, 7, FIELD) is None


class TestNeedsReencrypt:
    def test_plaintext_needs_it_and_current_envelope_does_not(self, keyed):
        assert needs_reencrypt("legacy") is True
        assert needs_reencrypt(encrypt_secret("legacy", 7, FIELD)) is False

    @pytest.mark.parametrize("value", [None, ""])
    def test_empty_values_never_need_it(self, keyed, value):
        assert needs_reencrypt(value) is False

    def test_nothing_needs_it_without_a_key(self, keyless):
        assert needs_reencrypt("legacy") is False


class TestEnvelopeKeyVersion:
    @pytest.mark.parametrize("value", ["plaintext", None, "lemv1:1:onlythree", "lemv1:x:a:b"])
    def test_unreadable_versions_are_none_not_a_guess(self, value):
        assert envelope_key_version(value) is None


class TestIsEncrypted:
    @pytest.mark.parametrize("value,expected", [
        ("lemv1:1:a:b", True), ("plaintext", False), ("", False), (None, False), (12345, False),
    ])
    def test_marker_detection(self, value, expected):
        assert is_encrypted(value) is expected


def test_master_key_is_read_at_call_time_not_import_time():
    """A key rotation lands on worker restart without a code change — and tests can set it
    per-case — only because the keyring is read from os.environ on every call."""
    with patch.dict(os.environ, {"LEM_SECRET_KEY": KEY_A, "LEM_SECRET_KEY_VERSION": "1"},
                    clear=False):
        assert encryption_enabled()
    with patch.dict(os.environ, {"LEM_SECRET_KEY": ""}, clear=False):
        assert not encryption_enabled()
