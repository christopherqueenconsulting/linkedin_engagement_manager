"""Strong authentication factors and the step-up verdict (issue #745, PR 2c).

This is the ONE place a user's factor state is decided. The login path, the step-up gate and the
Security card all read the same three questions from here, so what a user is told and what the
server enforces can never disagree:

1. **Does this account hold a strong factor?** — `has_strong_factor`. The moment it does, an email
   PIN stops being a way IN and becomes a bootstrap: `/auth/email/verify` mints no session, it
   hands back a pending handle and the list of factors that will finish the login (design §4, C
   "demoted to bootstrap-only").
2. **Which factors can finish it?** — `available_methods`. Never fabricated: an account with no
   TOTP is never offered TOTP, and passkeys disappear entirely in a deployment with no secure
   public origin.
3. **Has THIS session recently proved one?** — `step_up_satisfied`. That is the answer the
   credential-touching endpoints need before they will write.

Two rules in here are load-bearing and easy to get backwards:

- **Adding a factor never needs step-up; removing one always does.** A recovery code logs you in
  precisely so you can enrol a new passkey after losing the old one (design §6.8) — gating
  enrolment behind a factor you no longer have is a lockout, not a control. Removal, regeneration
  and every LinkedIn-credential write are the other direction: they are what an attacker holding a
  stolen session would do, so they need the physical factor.
- **An account with no strong factor passes the step-up gate.** The rollout is opt-in first
  (design §7 Stage 2), and there is nothing to prove with; gating those accounts would brick the
  cookie paste and the email change for everyone who has not enrolled yet. The gate is what makes
  enrolment WORTH something — it is not a retroactive lock. `STRONG_AUTH_ENABLED=false` returns
  the whole surface to its 2b behaviour without touching an enrolled factor.
"""

import hmac
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

import pyotp
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

from cqc_lem.utilities.db import (
    AUTH_FACTOR_PASSKEY, AUTH_FACTOR_TOTP, SESSION_SCOPE_EXTENSION,
    confirm_totp_factor, consume_recovery_code, count_auth_factors, count_recovery_codes,
    get_session_auth_state, get_totp_factor, get_unused_recovery_codes, list_auth_factors,
    mark_session_verified, replace_recovery_codes, update_factor_counter, upsert_totp_factor,
)
from cqc_lem.utilities.env_constants import (
    RECOVERY_CODE_COUNT, STEP_UP_MAX_AGE_MINUTES, STRONG_AUTH_ENABLED, WEBAUTHN_RP_NAME,
)
from cqc_lem.utilities.logger import log_debug, log_info

METHOD_PASSKEY = AUTH_FACTOR_PASSKEY
METHOD_TOTP = AUTH_FACTOR_TOTP
METHOD_RECOVERY = "recovery_code"

# Base32 without the characters people mistype off a printed sheet.
_RECOVERY_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
_RECOVERY_CODE_LENGTH = 10

_hasher = PasswordHasher()


def strong_auth_enabled() -> bool:
    return bool(STRONG_AUTH_ENABLED)


def has_strong_factor(user_id: int) -> bool:
    """True when the account holds at least one CONFIRMED passkey or authenticator app.

    Recovery codes deliberately do not count. They are the way back in when every factor is gone,
    so treating them as a factor would mean a user who saved codes and enrolled nothing else had
    their email PIN demoted with nothing to replace it."""
    if not strong_auth_enabled():
        return False
    return count_auth_factors(user_id) > 0


def available_methods(user_id: int) -> list[str]:
    """Which factors can actually finish a login for this account, in the order the UI offers them.

    Passkey first: it is the only phishing-resistant one, and the only one where the user does
    nothing but touch a sensor. Recovery codes last, and only when unused ones exist — offering a
    method that will certainly fail is worse than not offering it."""
    if not strong_auth_enabled():
        return []
    methods: list[str] = []
    kinds = {f.get("kind") for f in list_auth_factors(user_id)}
    if AUTH_FACTOR_PASSKEY in kinds:
        from cqc_lem.utilities.webauthn_util import passkeys_available
        if passkeys_available():
            methods.append(METHOD_PASSKEY)
    if AUTH_FACTOR_TOTP in kinds:
        methods.append(METHOD_TOTP)
    if methods and count_recovery_codes(user_id)[0] > 0:
        methods.append(METHOD_RECOVERY)
    return methods


@dataclass(frozen=True)
class FactorSummary:
    factors: list[dict]
    recovery_unused: int
    recovery_total: int
    passkeys_supported: bool
    has_strong_factor: bool
    pin_is_bootstrap_only: bool


def factor_summary(user_id: int) -> FactorSummary:
    """Everything the Security card renders. `pin_is_bootstrap_only` is the state the user most
    needs to see, because it is the one that changed how they sign in."""
    from cqc_lem.utilities.webauthn_util import passkeys_available
    unused, total = count_recovery_codes(user_id)
    factors = [{
        "id": f["id"],
        "kind": f["kind"],
        "label": f.get("label"),
        "created_at": f.get("created_at"),
        "last_used_at": f.get("last_used_at"),
    } for f in list_auth_factors(user_id)]
    strong = bool(factors) and strong_auth_enabled()
    return FactorSummary(
        factors=factors,
        recovery_unused=unused,
        recovery_total=total,
        passkeys_supported=passkeys_available(),
        has_strong_factor=strong,
        pin_is_bootstrap_only=strong,
    )


# ---------------------------------------------------------------------------
# TOTP
# ---------------------------------------------------------------------------

def begin_totp_enrollment(user_id: int, account_name: str) -> Optional[tuple[int, str, str]]:
    """(factor_id, secret, otpauth URI). The row is UNCONFIRMED until a code proves the user
    actually scanned it — a seed nobody can generate codes for must never be able to gate a login.

    The QR is rendered in the browser from the URI: sending an image would put the seed through an
    extra encoding for no benefit, and the URI is what every authenticator app accepts anyway."""
    secret = pyotp.random_base32()
    factor_id = upsert_totp_factor(user_id, secret)
    if not factor_id:
        return None
    uri = pyotp.TOTP(secret).provisioning_uri(name=account_name,
                                              issuer_name=WEBAUTHN_RP_NAME or "LEM")
    return factor_id, secret, uri


def confirm_totp_enrollment(user_id: int, code: str) -> bool:
    """Prove the seed. Reads the UNCONFIRMED row — `verify_totp_code` reads only confirmed ones, so
    the two can never be used to bypass one another."""
    factor = get_totp_factor(user_id, confirmed_only=False)
    if not factor or factor.get("confirmed_at"):
        return False
    step = _match_totp_step(factor["secret"], code)
    if step is None:
        return False
    if not confirm_totp_factor(factor["id"], user_id):
        return False
    update_factor_counter(factor["id"], step)
    log_info("TOTP factor enrolled", user_id=user_id)
    return True


def _match_totp_step(secret: str, code: str) -> Optional[int]:
    """The 30-second time step a code belongs to, or None.

    One step of drift either way (±30s) — enough for a phone whose clock is slightly off, small
    enough that the code a user reads is the code they type. `compare_digest` because a timing
    oracle on a 6-digit space is a real one."""
    cleaned = "".join((code or "").split())
    if not cleaned.isdigit():
        return None
    totp = pyotp.TOTP(secret)
    now = int(time.time())
    for offset in (0, -1, 1):
        moment = now + offset * totp.interval
        if hmac.compare_digest(totp.at(moment), cleaned):
            return moment // totp.interval
    return None


def verify_totp_code(user_id: int, code: str) -> bool:
    """Verify a code from the confirmed authenticator, ONCE.

    The replay guard is the point: a code stays valid for up to 90 seconds across the drift window,
    which is ample time for a phishing proxy to relay it a second time. Accepting only a step
    STRICTLY LATER than the last one spent closes that — TOTP cannot be made phishing-resistant,
    but it can be made single-use."""
    factor = get_totp_factor(user_id)
    if not factor:
        return False
    step = _match_totp_step(factor["secret"], code)
    if step is None:
        return False
    if step <= int(factor.get("sign_count") or 0):
        log_debug("TOTP code replayed within its window — refused", user_id=user_id)
        return False
    update_factor_counter(factor["id"], step)
    return True


# ---------------------------------------------------------------------------
# Recovery codes
# ---------------------------------------------------------------------------

def generate_recovery_codes(user_id: int) -> list[str]:
    """A fresh sheet, returned in the clear EXACTLY ONCE — the rows hold argon2id hashes, so
    neither we nor a DB dump can ever show them again. Regenerating invalidates the old set."""
    codes = ["".join(secrets.choice(_RECOVERY_ALPHABET) for _ in range(_RECOVERY_CODE_LENGTH))
             for _ in range(max(1, RECOVERY_CODE_COUNT))]
    if not replace_recovery_codes(user_id, [_hasher.hash(_normalize_recovery(c)) for c in codes]):
        return []
    log_info("Recovery codes regenerated", user_id=user_id)
    return codes


def _normalize_recovery(code: str) -> str:
    """Codes are shown grouped and typed back by hand: strip whitespace and dashes, upper-case.
    Normalising BEFORE hashing means the same code entered either way matches its stored hash."""
    return "".join((code or "").split()).replace("-", "").upper()


def verify_recovery_code(user_id: int, code: str) -> bool:
    """Spend one recovery code. Every unused hash is checked because argon2 hashes carry their own
    salt — there is no way to look the code up. The `used_at IS NULL` UPDATE is what makes it
    single-use even under two simultaneous attempts."""
    cleaned = _normalize_recovery(code)
    if not cleaned:
        return False
    for row in get_unused_recovery_codes(user_id):
        try:
            _hasher.verify(row["code_hash"], cleaned)
        except VerifyMismatchError:
            continue
        except Exception as e:
            log_debug(f"Skipping unreadable recovery code hash: {e}")
            continue
        return consume_recovery_code(user_id, row["id"])
    return False


# ---------------------------------------------------------------------------
# Step-up gate (design §6.5)
# ---------------------------------------------------------------------------

def step_up_satisfied(user_id: int, token: Optional[str],
                      extension_scope_ok: bool = False) -> bool:
    """May THIS request touch a LinkedIn credential, the email address, or another session?

    Three ways to pass, and each is deliberate:
    - the feature is off, or the account holds no strong factor — nothing to prove with (see the
      module docstring);
    - the session proved a factor inside `STEP_UP_MAX_AGE_MINUTES`;
    - `extension_scope_ok` AND the caller is the browser extension's own scoped session, whose
      step-up happened once in the SPA when the token was minted. It POSTs `li_at` holding nothing
      but that token and cannot run a passkey ceremony, so the alternative is not "more security",
      it is a broken one-click reconnect and users back on stored passwords (design §6.5, "decide
      this in 2c").

    That third one is opt-in PER CALL SITE, and that is the whole point: an extension token is
    otherwise an ordinary session, so a blanket exemption would let a stolen one change the email
    address and revoke every device — the exact escalation the gate exists to stop. Only the cookie
    endpoint the extension actually calls passes `extension_scope_ok=True`.
    """
    if not strong_auth_enabled() or not has_strong_factor(user_id):
        return True
    if not token:
        return False
    state = get_session_auth_state(token)
    if not state:
        return False
    if extension_scope_ok and state.get("scope") == SESSION_SCOPE_EXTENSION:
        return True
    verified_at = state.get("last_verified_at")
    if not verified_at:
        return False
    if verified_at.tzinfo is None:
        verified_at = verified_at.replace(tzinfo=timezone.utc)
    return verified_at >= datetime.now(timezone.utc) - timedelta(minutes=STEP_UP_MAX_AGE_MINUTES)


def record_step_up(token: Optional[str]) -> bool:
    """Stamp this session as freshly verified. Called only after a factor actually verified."""
    return bool(token) and mark_session_verified(token)
