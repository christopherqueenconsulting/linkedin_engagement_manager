"""Envelope encryption for the secrets LEM stores at rest (issue #745, PR 2a).

The threat this defeats is a **DB leak** — a stray `mysqldump`, a snapshot, SQL injection, a
curious insider (T1/T6 in `docs/AUTH_SECURITY_DESIGN.md` §2). It deliberately does NOT defeat
root on the VPS (T5): the master key lives in `/opt/lem/.env`, which root can read. See §5.3.

Scheme (design §5.1):

    LEM_SECRET_KEY (32 random bytes, base64)
      └─ HKDF-SHA256(master, salt=b"lem:user:<user_id>", info=b"<table>.<column>") -> per-user DEK
         └─ AES-256-GCM(DEK, nonce=12 random bytes, aad=b"<table>.<column>:<user_id>:<version>")

Stored as ONE self-describing string in the existing column:

    lemv1:<key_version>:<base64url(nonce)>:<base64url(ciphertext||tag)>

Two properties matter and are tested:
- **AAD binds the ciphertext to its row.** Grafting user A's encrypted `li_at` into user B's row
  (or into a different column) fails to decrypt, so MySQL *write* access does not move sessions.
- **`key_version` makes rotation a background re-encrypt** with both keys in env
  (`LEM_SECRET_KEY` + `LEM_SECRET_KEY_PREVIOUS`) — no downtime, no schema change.

Called ONLY from `utilities/db.py`, so the ten modules that consume these secrets are unchanged
and cannot accidentally bypass encryption.
"""

import base64
import hashlib
import hmac
import os
import secrets
from typing import Optional

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from cqc_lem.utilities.env_constants import isTrue
from cqc_lem.utilities.logger import log_debug, log_error, log_warning

# Envelope marker. Bump only if the scheme itself changes (a new marker means a new reader).
SECRET_ENVELOPE_PREFIX = "lemv1"

MASTER_KEY_BYTES = 32
_NONCE_BYTES = 12


class SecretEncryptionError(RuntimeError):
    """Raised when encryption is REQUIRED but cannot be performed (misconfigured key)."""


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64url(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _parse_master_key(raw: Optional[str]) -> Optional[bytes]:
    """Accept base64 (standard or url-safe, padded or not) or hex; must decode to 32 bytes."""
    if not raw:
        return None
    candidate = raw.strip()
    for decode in (lambda s: base64.urlsafe_b64decode(s + "=" * (-len(s) % 4)),
                   lambda s: base64.b64decode(s + "=" * (-len(s) % 4)),
                   bytes.fromhex):
        try:
            key = decode(candidate)
        except Exception:
            continue
        if len(key) == MASTER_KEY_BYTES:
            return key
    log_error("LEM_SECRET_KEY is set but is not 32 bytes of base64/hex — refusing to use it")
    return None


def _int_env(name: str, default: int) -> int:
    try:
        return int((os.environ.get(name) or "").strip() or default)
    except ValueError:
        log_warning(f"{name} is not an integer — falling back to {default}")
        return default


def encryption_required() -> bool:
    """Fail-closed mode (design §7): once the backfill reports zero plaintext rows, flipping this
    makes the legacy plaintext read path an error instead of a silent pass-through."""
    return isTrue(os.environ.get("ENCRYPTION_REQUIRED") or "False")


def _keyring() -> tuple[Optional[int], dict[int, bytes]]:
    """(current_version, {version: master_key}) read from env at CALL time — a key rotation lands
    on the next worker restart without a code change, and tests can set it per-case."""
    current_version = _int_env("LEM_SECRET_KEY_VERSION", 1)
    keys: dict[int, bytes] = {}
    current = _parse_master_key(os.environ.get("LEM_SECRET_KEY"))
    if current is None:
        return None, keys
    keys[current_version] = current
    previous = _parse_master_key(os.environ.get("LEM_SECRET_KEY_PREVIOUS"))
    if previous is not None:
        previous_version = _int_env("LEM_SECRET_KEY_PREVIOUS_VERSION", current_version - 1)
        if previous_version == current_version:
            # Two keys claiming one version is unrecoverable, not just confusing: the previous key
            # would take the current slot, so every NEW write is sealed under the OLD key while
            # tagged with the current version. needs_reencrypt() calls those rows done, and step 5
            # of the documented rotation (drop LEM_SECRET_KEY_PREVIOUS) then makes them
            # permanently undecryptable. Ignore the previous key instead — stale rows failing to
            # read is recoverable, fresh rows sealed under a key about to be deleted is not.
            log_error(f"LEM_SECRET_KEY_PREVIOUS_VERSION equals LEM_SECRET_KEY_VERSION "
                      f"({current_version}) — ignoring the previous key. Bump "
                      f"LEM_SECRET_KEY_VERSION so rotation can tell the two keys apart.")
        else:
            keys[previous_version] = previous
    return current_version, keys


def encryption_enabled() -> bool:
    """True when a usable master key is configured. False leaves every value untouched, which is
    exactly the pre-#745 behaviour — a dev box with no key keeps working."""
    return _keyring()[0] is not None


def generate_master_key() -> str:
    """A fresh base64 master key for `/opt/lem/.env`. Never called by the app at runtime."""
    return base64.urlsafe_b64encode(secrets.token_bytes(MASTER_KEY_BYTES)).decode("ascii")


def hash_session_token(token: Optional[str]) -> Optional[str]:
    """SHA-256 of a LEM session token — what `sessions.session_token` stores since #745 (2b).

    Deliberately UNKEYED: the token is 256 bits of `secrets.token_hex(32)`, so there is nothing to
    brute-force, and keying it would mean a lost or rotated `LEM_SECRET_KEY` logged every user out.
    Returns 64 lowercase hex chars, which is exactly the existing column width."""
    if not token:
        return None
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def hash_client_ip(ip: Optional[str]) -> Optional[str]:
    """Pseudonymised client IP for the audit log and the session list.

    Keyed with the master key when one is configured, because an IP is a ~2^32 space that a plain
    digest does not protect. With no key it falls back to an unkeyed digest — same pre-#745
    fail-open posture as the rest of this module; the value is only ever displayed, never compared
    across a key rotation."""
    if not ip:
        return None
    current_version, keys = _keyring()
    material = f"ip:{ip}".encode("utf-8")
    if current_version is not None:
        return hmac.new(keys[current_version], material, hashlib.sha256).hexdigest()
    return hashlib.sha256(material).hexdigest()


def _derive_key(master: bytes, user_id: int, field: str) -> bytes:
    return HKDF(
        algorithm=hashes.SHA256(),
        length=MASTER_KEY_BYTES,
        salt=f"lem:user:{user_id}".encode("utf-8"),
        info=field.encode("utf-8"),
    ).derive(master)


def _aad(field: str, user_id: int, key_version: int) -> bytes:
    return f"{field}:{user_id}:{key_version}".encode("utf-8")


def is_encrypted(value: Optional[str]) -> bool:
    """True when the stored value is one of our envelopes. Everything else is legacy plaintext."""
    return isinstance(value, str) and value.startswith(f"{SECRET_ENVELOPE_PREFIX}:")


def envelope_key_version(value: Optional[str]) -> Optional[int]:
    if not is_encrypted(value):
        return None
    parts = value.split(":", 3)
    if len(parts) != 4:
        return None
    try:
        return int(parts[1])
    except ValueError:
        return None


def needs_reencrypt(value: Optional[str]) -> bool:
    """True when a stored value should be (re-)written by the backfill: legacy plaintext, or an
    envelope under a superseded key version. Empty values and current-version envelopes are done —
    which is what makes the backfill idempotent."""
    if not value or not encryption_enabled():
        return False
    current_version = _keyring()[0]
    if not is_encrypted(value):
        return True
    return envelope_key_version(value) != current_version


def encrypt_secret(value: Optional[str], user_id: Optional[int], field: str) -> Optional[str]:
    """Envelope-encrypt `value` for `field` (`"<table>.<column>"`) bound to `user_id`.

    Returns the value UNCHANGED when there is nothing to protect (empty), when it is already an
    envelope (idempotent), or when no master key is configured — the last one keeps a keyless dev
    box working exactly as it did before. With `ENCRYPTION_REQUIRED=true` a missing key raises
    instead, because silently writing plaintext is the failure this whole PR exists to prevent.

    `user_id=None` cannot be bound to a row, so the value is left as-is with a warning rather than
    encrypted under a shared key that would defeat the AAD guarantee — unless
    `ENCRYPTION_REQUIRED=true`, where the same rule as a missing key applies: raise, never write
    plaintext.
    """
    if not value:
        return value
    if is_encrypted(value):
        return value

    current_version, keys = _keyring()
    if current_version is None:
        if encryption_required():
            raise SecretEncryptionError(
                f"ENCRYPTION_REQUIRED is set but LEM_SECRET_KEY is missing/invalid — refusing to "
                f"store {field} as plaintext")
        log_debug(f"No LEM_SECRET_KEY configured — {field} stored as-is")
        return value
    if user_id is None:
        # Fail-closed has to cover this branch too, or an unbindable row (a cookie stored for an
        # email with no user) writes a live li_at in the clear while the operator believes
        # ENCRYPTION_REQUIRED forbids exactly that.
        if encryption_required():
            raise SecretEncryptionError(
                f"ENCRYPTION_REQUIRED is set but {field} has no user_id to bind to — refusing to "
                f"store it as plaintext")
        log_warning(f"Cannot encrypt {field} without a user_id — value stored unencrypted")
        return value

    dek = _derive_key(keys[current_version], int(user_id), field)
    nonce = secrets.token_bytes(_NONCE_BYTES)
    blob = AESGCM(dek).encrypt(nonce, value.encode("utf-8"),
                               _aad(field, int(user_id), current_version))
    return f"{SECRET_ENVELOPE_PREFIX}:{current_version}:{_b64url(nonce)}:{_b64url(blob)}"


def decrypt_secret(value: Optional[str], user_id: Optional[int], field: str) -> Optional[str]:
    """Dual-mode read (design §7 Stage 0): an envelope is decrypted, anything else is returned as
    legacy plaintext so a rollback mid-backfill is safe.

    Every failure returns **None**, never the ciphertext — a caller that got the raw envelope back
    would type it into LinkedIn's login form or send it as a bearer token.
    """
    if not value:
        return value
    if not is_encrypted(value):
        if encryption_required():
            log_error(f"Plaintext {field} found with ENCRYPTION_REQUIRED set — refusing to read it")
            return None
        return value

    parts = value.split(":", 3)
    if len(parts) != 4:
        log_error(f"Malformed secret envelope for {field}")
        return None
    _, version_text, nonce_text, blob_text = parts

    current_version, keys = _keyring()
    if current_version is None:
        log_error(f"Encrypted {field} found but LEM_SECRET_KEY is missing/invalid")
        return None
    try:
        key_version = int(version_text)
    except ValueError:
        log_error(f"Malformed key version in secret envelope for {field}")
        return None
    if key_version not in keys:
        log_error(f"No master key for version {key_version} — cannot decrypt {field}. "
                  f"Set LEM_SECRET_KEY_PREVIOUS if you rotated the key.")
        return None
    if user_id is None:
        log_error(f"Cannot decrypt {field} without a user_id")
        return None

    try:
        dek = _derive_key(keys[key_version], int(user_id), field)
        plaintext = AESGCM(dek).decrypt(_unb64url(nonce_text), _unb64url(blob_text),
                                        _aad(field, int(user_id), key_version))
    except InvalidTag:
        # Wrong user_id, wrong column, wrong key, or a tampered row. All of them mean the
        # ciphertext does not belong here.
        log_error(f"Could not decrypt {field} for user_id {user_id} — "
                  f"authentication failed (wrong row, key, or tampered value)")
        return None
    except Exception as e:
        log_error(f"Could not decrypt {field} for user_id {user_id}", exc=e)
        return None
    return plaintext.decode("utf-8")
