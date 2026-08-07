"""WebAuthn / passkey ceremonies (issue #745, PR 2c).

Passkeys are the ONE authentication method in `docs/AUTH_SECURITY_DESIGN.md` §4 that structurally
defeats T3 (real-time phishing relay): the browser binds the credential to the origin, so a proxy
page holding a perfect copy of the login screen cannot use it. Everything else in the auth stack —
the PIN, TOTP, a recovery code — is a string a human can be talked into typing somewhere else.

This module is the thin wrapper over `webauthn` (duo-labs py_webauthn, BSD-3-Clause). It owns two
things the library deliberately does not:

- **Where the relying party lives.** The RP id and the expected origin are DERIVED from
  `PUBLIC_BASE_URL` rather than configured separately, because a passkey registered against one
  hostname is worthless against another — letting those two facts drift is how an entire user
  base gets locked out by a deploy. `WEBAUTHN_RP_ID` overrides it only for a split-host setup.
- **Refusing to run at all when the origin is not secure.** WebAuthn requires a secure context, so
  an `http://` public origin cannot register a credential; saying so here is better than a browser
  error nobody can read. (The prerequisite in issue #897 — a valid TLS certificate on the public
  hostname — was verified before this shipped; see docs/strong-authentication.md.)

The verification results themselves are the library's, unmodified: signature checks, challenge
matching and the cloned-authenticator sign-count regression are its job, not ours.
"""

from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse

from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from cqc_lem.utilities.env_constants import (
    PUBLIC_BASE_URL,
    WEBAUTHN_EXTRA_ORIGINS,
    WEBAUTHN_RP_ID,
    WEBAUTHN_RP_NAME,
)
from cqc_lem.utilities.logger import log_debug, log_warning


class WebAuthnUnavailable(RuntimeError):
    """No usable relying-party identity — passkeys cannot be offered in this deployment."""


@dataclass(frozen=True)
class RelyingParty:
    """The identity every passkey in this deployment is bound to, resolved from the environment.

    `rp_id` is the single hostname the credential is scoped to — change it and every enrolled
    passkey stops working, which is why `relying_party()` derives it from `PUBLIC_BASE_URL` instead
    of taking it as a separate setting. `origins` is a list because one relying party can be reached
    from more than one URL (`WEBAUTHN_EXTRA_ORIGINS`) and the library accepts a set of expected
    origins; `PUBLIC_BASE_URL`'s origin leads it when there is one.
    """

    rp_id: str
    rp_name: str
    origins: list[str]


def _origin_of(url: str) -> Optional[str]:
    parsed = urlparse(url.strip())
    if not parsed.scheme or not parsed.netloc:
        return None
    return f"{parsed.scheme}://{parsed.netloc}"


def relying_party() -> RelyingParty:
    """The RP identity for this deployment, or `WebAuthnUnavailable` when there is none.

    Read at CALL time, not at import: the same reason the feature flags are (root CLAUDE.md) — an
    env change lands on the next request rather than the next rebuild, and a test can set it
    per-case.
    """
    base = (PUBLIC_BASE_URL or "").strip()
    origins: list[str] = []
    rp_id = (WEBAUTHN_RP_ID or "").strip()

    if base:
        origin = _origin_of(base)
        if origin:
            origins.append(origin)
            if not rp_id:
                rp_id = urlparse(base).hostname or ""

    for extra in (WEBAUTHN_EXTRA_ORIGINS or "").split(","):
        origin = _origin_of(extra) if extra.strip() else None
        if origin and origin not in origins:
            origins.append(origin)
            if not rp_id:
                rp_id = urlparse(origin).hostname or ""

    if not rp_id or not origins:
        raise WebAuthnUnavailable(
            "Passkeys need a public origin: set PUBLIC_BASE_URL (or WEBAUTHN_RP_ID plus "
            "WEBAUTHN_EXTRA_ORIGINS) to the https:// hostname the app is served from.")

    if not any(o.startswith("https://") or o.startswith("http://localhost") for o in origins):
        # A browser will not even run the ceremony against a non-secure context, so failing here
        # turns an unreadable client-side error into a message an operator can act on.
        raise WebAuthnUnavailable(
            f"Passkeys need a secure context — {origins[0]} is neither https nor localhost.")

    return RelyingParty(rp_id=rp_id, rp_name=(WEBAUTHN_RP_NAME or "LinkedIn Engagement Manager"),
                        origins=origins)


def passkeys_available() -> bool:
    """Whether this deployment can offer passkeys at all. Used to decide what the login screen and
    the Security card show — an option a user cannot complete is worse than no option.
    """
    try:
        relying_party()
        return True
    except WebAuthnUnavailable as e:
        log_debug(f"Passkeys unavailable: {e}")
        return False


def _descriptors(credential_ids: list[str]) -> list[PublicKeyCredentialDescriptor]:
    descriptors = []
    for cid in credential_ids:
        try:
            descriptors.append(PublicKeyCredentialDescriptor(id=base64url_to_bytes(cid)))
        except Exception as e:
            log_warning("Skipping unparseable stored credential id", exc=e)
    return descriptors


def build_registration_options(user_id: int, user_name: str, user_display_name: str,
                               existing_credential_ids: list[str]) -> tuple[dict, str]:
    """(options for `navigator.credentials.create`, base64url challenge to remember).

    `resident_key=preferred` + `user_verification=preferred` rather than `required`: a discoverable
    credential is what makes username-less login work, but demanding it would exclude the security
    keys that cannot store one, and the design keeps TOTP as the path for people passkeys don't fit.

    `exclude_credentials` is what stops the same authenticator being enrolled twice — without it a
    user "adds a second passkey", gets the same one back, and believes they have a spare.
    """
    import json

    rp = relying_party()
    options = generate_registration_options(
        rp_id=rp.rp_id,
        rp_name=rp.rp_name,
        user_id=str(user_id).encode("utf-8"),
        user_name=user_name,
        user_display_name=user_display_name or user_name,
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.PREFERRED,
            user_verification=UserVerificationRequirement.PREFERRED,
        ),
        exclude_credentials=_descriptors(existing_credential_ids),
    )
    return json.loads(options_to_json(options)), bytes_to_base64url(options.challenge)


def build_authentication_options(credential_ids: Optional[list[str]] = None) -> tuple[dict, str]:
    """(options for `navigator.credentials.get`, base64url challenge to remember).

    With no credential ids the ceremony is username-less: the browser offers whatever discoverable
    passkey it holds for this RP and the assertion tells us who signed in. That is deliberate —
    asking for the email first would leak which addresses have an account.
    """
    import json

    rp = relying_party()
    options = generate_authentication_options(
        rp_id=rp.rp_id,
        allow_credentials=_descriptors(credential_ids) if credential_ids else None,
        user_verification=UserVerificationRequirement.PREFERRED,
    )
    return json.loads(options_to_json(options)), bytes_to_base64url(options.challenge)


@dataclass(frozen=True)
class RegistrationResult:
    """A passkey that VERIFIED, in the shape the credential row stores it.

    `credential_id` and `public_key` are base64url text rather than bytes because that is the form
    that round-trips through the database and back into `_descriptors`. `sign_count` is the opening
    value of the clone-detection counter — it only detects anything if the caller keeps persisting
    the counter `verify_assertion` hands back, so storing this one is where that starts.
    """

    credential_id: str
    public_key: str
    sign_count: int


def verify_registration(credential: dict, expected_challenge: str) -> Optional[RegistrationResult]:
    """Verify a registration response. None on ANY failure — a caller that stored an unverified
    credential would have enrolled an attacker's key as a second factor.
    """
    rp = relying_party()
    try:
        verified = verify_registration_response(
            credential=credential,
            expected_challenge=base64url_to_bytes(expected_challenge),
            expected_rp_id=rp.rp_id,
            expected_origin=rp.origins,
        )
    except Exception as e:
        log_warning("Passkey registration rejected", exc=e)
        return None
    return RegistrationResult(
        credential_id=bytes_to_base64url(verified.credential_id),
        public_key=bytes_to_base64url(verified.credential_public_key),
        sign_count=int(verified.sign_count or 0),
    )


def verify_assertion(credential: dict, expected_challenge: str, public_key: str,
                     current_sign_count: int) -> Optional[int]:
    """Verify an authentication response and return the NEW signature counter, or None on failure.

    The counter matters: py_webauthn rejects an assertion whose counter did not advance past the
    stored one, which is the standard tell for a cloned authenticator. Returning it here is what
    makes the caller persist it — a counter that is never written back never detects anything.
    """
    rp = relying_party()
    try:
        verified = verify_authentication_response(
            credential=credential,
            expected_challenge=base64url_to_bytes(expected_challenge),
            expected_rp_id=rp.rp_id,
            expected_origin=rp.origins,
            credential_public_key=base64url_to_bytes(public_key),
            credential_current_sign_count=int(current_sign_count or 0),
        )
    except Exception as e:
        log_warning("Passkey assertion rejected", exc=e)
        return None
    return int(verified.new_sign_count or 0)


def credential_id_from_response(credential: dict) -> Optional[str]:
    """The base64url credential id the browser sent, used to find WHOSE passkey this is before any
    signature is checked. Untrusted until `verify_assertion` passes — it only selects the row.
    """
    raw = (credential or {}).get("id") or (credential or {}).get("rawId")
    if not isinstance(raw, str) or not raw.strip():
        return None
    return raw.strip()
