"""Every SQL statement LEM runs against the auth tables.

Split out of `cqc_lem.utilities.db` (issue #1154). The fail-soft reader contract and the
secret-sealing rules described there apply here unchanged; `cqc_lem.utilities.db`
re-exports every name below, so existing importers and patch targets keep resolving.
"""

import json
from datetime import (
    datetime,
    timedelta,
    timezone,
)
from typing import Optional

import mysql.connector

from cqc_lem.platform.db import connection as _connection
from cqc_lem.platform.db.connection import db_cursor
from cqc_lem.platform.db.enums import AuthAuditEvent
from cqc_lem.platform.db.shared import (
    AUTH_FACTOR_PASSKEY,
    SESSION_SCOPE_FULL,
)
from cqc_lem.utilities.crypto import (
    decrypt_secret,
    encrypt_secret,
    hash_client_ip,
    hash_session_token,
)
from cqc_lem.utilities.env_constants import (
    SESSION_ABSOLUTE_MAX_DAYS,
    SESSION_IDLE_HOURS,
)
from cqc_lem.utilities.logger import log_error, log_info, log_warning


def create_pin_for_email(email: str, pin_hash: str) -> bool:
    """Issue a login PIN for an email address, expiring in 10 minutes.

    Any unused PIN for that address is deleted first, so only the newest code can ever be redeemed — a
    resend invalidates the code in the earlier email instead of leaving two live at once. The caller
    passes the HASH; the plaintext PIN never reaches this table.
    """
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute("DELETE FROM email_pin_auth WHERE email = %s AND used = 0", (email,))
            expires_at = datetime.now(timezone.utc) + timedelta(minutes=10)
            cursor.execute(
                "INSERT INTO email_pin_auth (email, pin, expires_at) VALUES (%s, %s, %s)",
                (email, pin_hash, expires_at),
            )
            return cursor.rowcount == 1
    except mysql.connector.Error as err:
        log_info(f"Could not create PIN for {email} | Error: {err}")
        return False
def delete_pin_for_email(email: str) -> None:
    """Remove all unused PINs for an email — called when email send fails after DB write."""
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute("DELETE FROM email_pin_auth WHERE email = %s AND used = 0", (email,))
    except mysql.connector.Error as err:
        log_info(f"Could not delete PIN for {email} | Error: {err}")
def get_pin_lockout(email: str) -> Optional[datetime]:
    """When this email's PIN entry is locked until, or None. Read by the API so a locked account
    gets a 429 with a wait time instead of an indistinguishable 401.
    """
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute(
                """SELECT MAX(locked_until) AS locked_until FROM email_pin_auth
                   WHERE email = %s AND used = 0 AND locked_until > %s""",
                (email, datetime.now(timezone.utc)),
            )
            row = cursor.fetchone()
            return row.get('locked_until') if row else None
    except mysql.connector.Error as err:
        log_info(f"Could not read PIN lockout for {email} | Error: {err}")
        return None
def verify_pin_for_email(email: str, pin_hash: str) -> bool:
    """Consume a PIN. Wrong guesses increment `attempts` and lock the outstanding PIN once
    PIN_MAX_ATTEMPTS is reached (issue #745, 2b) — a 6-digit space is otherwise walkable.

    A new /auth/email/init clears the unused rows and therefore the lock; that path is bounded
    separately by the per-email request limiter in `utilities/auth_rate_limit.py`.
    """
    from cqc_lem.utilities.env_constants import PIN_LOCKOUT_MINUTES, PIN_MAX_ATTEMPTS
    connection = _connection.get_db_connection()
    cursor = connection.cursor(dictionary=True)
    try:
        now = datetime.now(timezone.utc)
        cursor.execute(
            """SELECT id FROM email_pin_auth
               WHERE email = %s AND used = 0 AND locked_until > %s LIMIT 1""",
            (email, now),
        )
        if cursor.fetchone():
            return False

        cursor.execute(
            """SELECT id FROM email_pin_auth
               WHERE email = %s AND pin = %s AND used = 0 AND expires_at > %s
               ORDER BY id DESC LIMIT 1""",
            (email, pin_hash, now),
        )
        row = cursor.fetchone()
        if row:
            cursor.execute("UPDATE email_pin_auth SET used = 1 WHERE id = %s", (row['id'],))
            connection.commit()
            return True

        cursor.execute(
            """UPDATE email_pin_auth
                  SET attempts = attempts + 1,
                      locked_until = CASE WHEN attempts + 1 >= %s THEN %s ELSE locked_until END
                WHERE email = %s AND used = 0""",
            (PIN_MAX_ATTEMPTS, now + timedelta(minutes=PIN_LOCKOUT_MINUTES), email),
        )
        connection.commit()
        return False
    except mysql.connector.Error as err:
        log_info(f"Could not verify PIN for {email} | Error: {err}")
        return False
    finally:
        cursor.close()
        connection.close()
SESSION_SCOPE_ENROLL = "enroll"
# An `agent` session (issue #1026) belongs to a headless automation — no browser, no ceremony, no
# mailbox round trip. It is minted ONCE by a human in the SPA (step-up gated, like the extension
# token) and then held by a machine, so it is scoped to the queueing surface and nothing else: it
# can read the review queues and CREATE pending work for a human to approve. It can never approve,
# touch a credential, move the account, or mint another token.
SESSION_SCOPE_AGENT = "agent"
def resolve_session(token: str) -> Optional[dict]:
    """Validate a session token and, if live, slide its expiry forward — returning WHO it is and
    WHAT it may do in one read.

    Sliding idle window (SESSION_IDLE_HOURS) so an active user never has to request a new PIN,
    bounded by an absolute cap (SESSION_ABSOLUTE_MAX_DAYS from first login) for security.
    Expired/unknown/revoked tokens return None so the caller forces a fresh PIN.

    An `agent` session is the ONE exception and does not slide at all — its expiry is whatever
    `create_session(ttl_hours=...)` granted, fixed from mint. See the branch below for why.

    The presented token is hashed before lookup (#745, 2b) — the plaintext never touches SQL.

    `scope` rides along because the API resolver has to decide, on the SAME request, whether a
    restricted session (`extension`, `enroll`) may reach the path it is calling (2c.1, issue #905).
    Reading it separately would double a query every authenticated request already makes.
    """
    token_hash = hash_session_token(token)
    if not token_hash:
        return None
    connection = _connection.get_db_connection()
    cursor = connection.cursor(dictionary=True)
    try:
        now = datetime.now(timezone.utc)
        cursor.execute(
            "SELECT user_id, created_at, scope FROM sessions "
            "WHERE session_token = %s AND expires_at > %s AND revoked_at IS NULL",
            (token_hash, now),
        )
        row = cursor.fetchone()
        if not row:
            return None

        scope = row.get('scope') or SESSION_SCOPE_FULL

        if scope == SESSION_SCOPE_AGENT:
            # An agent session's life is FIXED at mint, never slid (issue #1026). Sliding it is
            # wrong in both directions. Sliding by SESSION_IDLE_HOURS — what every other session
            # gets — would rewrite the 90-day expiry to 24 hours on the FIRST request, silently
            # destroying the one thing `ttl_hours` exists to provide and leaving the weekly agent
            # dead every run exactly as before. Sliding by the granted TTL instead would be worse:
            # a machine calling on a schedule renews forever, so the credential with the widest
            # time window would be the only one with no ceiling at all. A fixed expiry gives a real
            # deadline that only a human ceremony can extend, which is also why the absolute cap
            # (SESSION_ABSOLUTE_MAX_DAYS, counted from first login) is not applied here — it exists
            # to bound a sliding window, and there is none to bound.
            cursor.execute(
                "UPDATE sessions SET last_seen_at = %s WHERE session_token = %s",
                (now, token_hash),
            )
            connection.commit()
            return {"user_id": row['user_id'], "scope": scope}

        new_expiry = now + timedelta(hours=SESSION_IDLE_HOURS)
        created_at = row.get('created_at')
        if created_at is not None:
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc)
            absolute_cap = created_at + timedelta(days=SESSION_ABSOLUTE_MAX_DAYS)
            if new_expiry > absolute_cap:
                new_expiry = absolute_cap
        cursor.execute(
            "UPDATE sessions SET expires_at = %s, last_seen_at = %s WHERE session_token = %s",
            (new_expiry, now, token_hash),
        )
        connection.commit()
        return {"user_id": row['user_id'], "scope": scope}
    except mysql.connector.Error as err:
        log_info(f"Could not validate session token | Error: {err}")
        return None
    finally:
        cursor.close()
        connection.close()
def get_session_id(token: str) -> Optional[int]:
    """Row id of a live session, for audit rows that name the session that acted."""
    token_hash = hash_session_token(token)
    if not token_hash:
        return None
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute(
                "SELECT id FROM sessions WHERE session_token = %s AND revoked_at IS NULL",
                (token_hash,),
            )
            row = cursor.fetchone()
            return row['id'] if row else None
    except mysql.connector.Error as err:
        log_info(f"Could not resolve session id | Error: {err}")
        return None
def delete_session(token: str) -> bool:
    """Revoke one session by its token.

    `sessions.session_token` stores an unkeyed SHA-256, so the token is hashed before the DELETE. True
    means the statement ran, NOT that a session existed — a caller cannot use it to probe whether a token
    was valid.
    """
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute("DELETE FROM sessions WHERE session_token = %s", (hash_session_token(token),))
            return True
    except mysql.connector.Error as err:
        log_info(f"Could not delete session | Error: {err}")
        return False
def revoke_session(user_id: int, session_id: int) -> bool:
    """Revoke ONE session. Scoped by user_id on purpose — a session id from another account must
    never be revocable by guessing the number.
    """
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                "UPDATE sessions SET revoked_at = %s WHERE id = %s AND user_id = %s "
                "AND revoked_at IS NULL",
                (datetime.now(timezone.utc), session_id, user_id),
            )
            return cursor.rowcount > 0
    except mysql.connector.Error as err:
        log_info(f"Could not revoke session {session_id} for user_id {user_id} | Error: {err}")
        return False
def revoke_other_sessions(user_id: int, keep_token: Optional[str] = None) -> int:
    """Revoke every session except the one presenting `keep_token` (None revokes all). Returns how
    many rows were revoked — "sign out everywhere", and what an email change triggers.
    """
    keep_hash = hash_session_token(keep_token)
    try:
        with db_cursor(commit=True) as cursor:
            now = datetime.now(timezone.utc)
            if keep_hash:
                cursor.execute(
                    "UPDATE sessions SET revoked_at = %s WHERE user_id = %s AND revoked_at IS NULL "
                    "AND session_token <> %s",
                    (now, user_id, keep_hash),
                )
            else:
                cursor.execute(
                    "UPDATE sessions SET revoked_at = %s WHERE user_id = %s AND revoked_at IS NULL",
                    (now, user_id),
                )
            return cursor.rowcount or 0
    except mysql.connector.Error as err:
        log_info(f"Could not revoke sessions for user_id {user_id} | Error: {err}")
        return 0
def record_auth_event(event: AuthAuditEvent, user_id: Optional[int] = None,
                      email: Optional[str] = None, ip: Optional[str] = None,
                      user_agent: Optional[str] = None, session_id: Optional[int] = None,
                      success: bool = True, details: Optional[dict] = None) -> bool:
    """Append one row to `auth_audit_log`. Best effort — an audit write must never fail a login,
    but a failure is logged so a silently blind audit trail is visible.
    """
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                "INSERT INTO auth_audit_log (user_id, email, event, ip_hash, user_agent, session_id, "
                "success, details) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                (user_id, email, str(event), hash_client_ip(ip),
                 user_agent[:512] if user_agent else None, session_id, 1 if success else 0,
                 json.dumps(details) if details else None),
            )
            return True
    except mysql.connector.Error as err:
        log_warning(f"Could not write auth audit row for {event}", user_id=user_id)
        log_info(f"Could not write auth audit row | Error: {err}")
        return False
def get_auth_audit_events(user_id: int, limit: int = 20) -> list[dict]:
    """Recent auth history for the account page — what a user needs to spot a login they didn't
    make. Returns no IP hash: it is stored for forensics, not for display.
    """
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute(
                "SELECT event, success, user_agent, created_at FROM auth_audit_log "
                "WHERE user_id = %s ORDER BY id DESC LIMIT %s",
                (user_id, int(limit)),
            )
            return cursor.fetchall() or []
    except mysql.connector.Error as err:
        log_info(f"Could not read auth audit for user_id {user_id} | Error: {err}")
        return []
AUTH_FACTOR_TOTP = "totp"
# `secret` is the TOTP seed at rest. The field name is the encryption AAD (see crypto.py) —
# renaming it orphans every enrolled authenticator, exactly like the 2a columns.
TOTP_SECRET_FIELD = "user_auth_factors.secret"
def get_user_passkey_credential_ids(user_id: int) -> list[str]:
    """Credential ids already enrolled — passed to the browser as `excludeCredentials` so the same
    authenticator cannot be registered twice, and as `allowCredentials` for a non-discoverable one.
    """
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute(
                """SELECT credential_id FROM user_auth_factors
                   WHERE user_id = %s AND kind = %s AND confirmed_at IS NOT NULL""",
                (user_id, AUTH_FACTOR_PASSKEY),
            )
            return [row["credential_id"] for row in cursor.fetchall() if row.get("credential_id")]
    except mysql.connector.Error as err:
        log_info(f"Could not list passkeys for user_id {user_id} | Error: {err}")
        return []
def update_factor_counter(factor_id: int, counter: int) -> bool:
    """Persist a factor's monotonic counter after a successful verification.

    ONE column for both kinds because both are the same idea: a passkey's WebAuthn signature count,
    and an authenticator app's accepted TOTP time step. Each must strictly INCREASE, and that is
    what makes a cloned authenticator (a counter that went backwards) and a re-typed TOTP code
    (the same 30-second step twice) fail instead of pass.
    """
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                "UPDATE user_auth_factors SET sign_count = %s, last_used_at = %s WHERE id = %s",
                (int(counter), datetime.now(timezone.utc), factor_id),
            )
            return cursor.rowcount >= 0
    except mysql.connector.Error as err:
        log_info(f"Could not update counter for factor {factor_id} | Error: {err}")
        return False
def upsert_totp_factor(user_id: int, secret: str, label: Optional[str] = None) -> Optional[int]:
    """Start (or restart) TOTP enrolment. The row lands UNCONFIRMED — `confirmed_at IS NULL` — so a
    secret that was generated and never proven can never satisfy a login. Restarting replaces any
    unconfirmed attempt rather than accumulating dead seeds.

    A CONFIRMED row is deliberately left alone here, and `auth_factors.begin_totp_enrollment` is
    what refuses to call this while one exists: an account holds at most one authenticator app, and
    silently deleting the working one to start an enrolment nobody may finish would hand a stolen
    session a way to take the factor off the account.
    """
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                "DELETE FROM user_auth_factors WHERE user_id = %s AND kind = %s AND confirmed_at IS NULL",
                (user_id, AUTH_FACTOR_TOTP),
            )
            cursor.execute(
                """INSERT INTO user_auth_factors (user_id, kind, label, secret)
                   VALUES (%s, %s, %s, %s)""",
                (user_id, AUTH_FACTOR_TOTP, (label or "Authenticator app")[:120],
                 encrypt_secret(secret, user_id, TOTP_SECRET_FIELD)),
            )
            return cursor.lastrowid
    except mysql.connector.Error as err:
        log_info(f"Could not start TOTP enrolment for user_id {user_id} | Error: {err}")
        return None
def get_totp_factor(user_id: int, confirmed_only: bool = True) -> Optional[dict]:
    """The account's TOTP factor with its secret decrypted. Returns None when the envelope cannot
    be opened — a caller that got the raw envelope back would compare a code against ciphertext and
    reject every valid one silently.
    """
    try:
        with db_cursor(dictionary=True) as cursor:
            sql = """SELECT id, secret, label, confirmed_at, sign_count FROM user_auth_factors
                     WHERE user_id = %s AND kind = %s"""
            if confirmed_only:
                sql += " AND confirmed_at IS NOT NULL"
            sql += " ORDER BY id DESC LIMIT 1"
            cursor.execute(sql, (user_id, AUTH_FACTOR_TOTP))
            row = cursor.fetchone()
            if not row:
                return None
            row["secret"] = decrypt_secret(row.get("secret"), user_id, TOTP_SECRET_FIELD)
            return row if row["secret"] else None
    except mysql.connector.Error as err:
        log_info(f"Could not read TOTP factor for user_id {user_id} | Error: {err}")
        return None
def confirm_totp_factor(factor_id: int, user_id: int) -> bool:
    """Mark a TOTP seed proven. Scoped by user_id so a guessed factor id cannot confirm someone
    else's enrolment, and idempotent-safe: only an unconfirmed row is touched.
    """
    try:
        with db_cursor(commit=True) as cursor:
            now = datetime.now(timezone.utc)
            cursor.execute(
                """UPDATE user_auth_factors SET confirmed_at = %s, last_used_at = %s
                   WHERE id = %s AND user_id = %s AND confirmed_at IS NULL""",
                (now, now, factor_id, user_id),
            )
            return cursor.rowcount > 0
    except mysql.connector.Error as err:
        log_info(f"Could not confirm TOTP factor {factor_id} | Error: {err}")
        return False
def touch_auth_factor(factor_id: int) -> bool:
    """Stamp `last_used_at` on a factor so the Security card can show when it was last actually used.

    Reports True whenever the UPDATE ran, matched or not: this is bookkeeping alongside a verification
    that already succeeded, and a vanished factor id must not turn that into a failure.
    """
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute("UPDATE user_auth_factors SET last_used_at = %s WHERE id = %s",
                           (datetime.now(timezone.utc), factor_id))
            return cursor.rowcount >= 0
    except mysql.connector.Error as err:
        log_info(f"Could not touch auth factor {factor_id} | Error: {err}")
        return False
def list_auth_factors(user_id: int, confirmed_only: bool = True) -> list[dict]:
    """The account's strong factors for the Security card. Never returns a secret or a public key —
    only what a person needs to recognise a factor before removing it.
    """
    try:
        with db_cursor(dictionary=True) as cursor:
            sql = """SELECT id, kind, label, created_at, last_used_at, confirmed_at
                     FROM user_auth_factors WHERE user_id = %s"""
            if confirmed_only:
                sql += " AND confirmed_at IS NOT NULL"
            sql += " ORDER BY id"
            cursor.execute(sql, (user_id,))
            return cursor.fetchall() or []
    except mysql.connector.Error as err:
        log_info(f"Could not list auth factors for user_id {user_id} | Error: {err}")
        return []
def count_auth_factors(user_id: int) -> int:
    """How many CONFIRMED strong factors the account holds. The one question the login path and the
    step-up gate both ask, so it is one indexed COUNT rather than a list the caller measures.

    Raises:
        mysql.connector.Error: the count could not be read. It deliberately does NOT answer 0, which
            is the same answer as "this account enrolled nothing" — and `has_strong_factor` is the
            sole gate deciding whether an email PIN alone may mint a full session (issue #745
            phase 2c), so a swallowed error demoted an enrolled account's second factor exactly
            while the database was unhappy. The sibling `count_challenge_attempts` fails closed on
            a sentinel because its ONE caller tests `spent < 0`; this has 11, so the honest move is
            to refuse to answer and let the request surface as a server error — no session is
            minted either way, and that is what failing closed means here. Returning a truthy
            sentinel instead would be worse than the bug: `list_auth_factors` answers `[]` on this
            same fault, so the account would be handed a second-factor challenge offering no
            methods at all.
    """
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute(
                """SELECT COUNT(*) AS n FROM user_auth_factors
                   WHERE user_id = %s AND confirmed_at IS NOT NULL""",
                (user_id,),
            )
            row = cursor.fetchone()
            return int(row["n"]) if row else 0
    except mysql.connector.Error as err:
        log_error("Could not count auth factors — refusing to answer rather than report zero",
                  exc=err, user_id=user_id)
        raise
def delete_auth_factor(user_id: int, factor_id: int) -> bool:
    """Remove ONE factor. Scoped by user_id — a factor id from another account must never be
    removable by guessing the number.
    """
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute("DELETE FROM user_auth_factors WHERE id = %s AND user_id = %s",
                           (factor_id, user_id))
            return cursor.rowcount > 0
    except mysql.connector.Error as err:
        log_info(f"Could not delete auth factor {factor_id} for user_id {user_id} | Error: {err}")
        return False
def replace_recovery_codes(user_id: int, code_hashes: list[str]) -> bool:
    """Install a fresh set of recovery codes, invalidating every previous one — including the ones
    already spent, since a regenerate is the user saying "the old sheet is gone".
    """
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute("DELETE FROM user_recovery_codes WHERE user_id = %s", (user_id,))
            if code_hashes:
                cursor.executemany(
                    "INSERT INTO user_recovery_codes (user_id, code_hash) VALUES (%s, %s)",
                    [(user_id, h) for h in code_hashes],
                )
            return True
    except mysql.connector.Error as err:
        log_info(f"Could not store recovery codes for user_id {user_id} | Error: {err}")
        return False
def get_unused_recovery_codes(user_id: int) -> list[dict]:
    """The account's unspent recovery codes as `(id, code_hash)` rows — never a usable code.

    Verification hashes the candidate against each row and then spends the winning id through
    `consume_recovery_code`, which is where the single-use guarantee lives. [] on a read error, which
    fails closed (no code can be verified).
    """
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute(
                "SELECT id, code_hash FROM user_recovery_codes WHERE user_id = %s AND used_at IS NULL",
                (user_id,),
            )
            return cursor.fetchall() or []
    except mysql.connector.Error as err:
        log_info(f"Could not read recovery codes for user_id {user_id} | Error: {err}")
        return []
def consume_recovery_code(user_id: int, code_id: int) -> bool:
    """Spend one code. The `used_at IS NULL` predicate is the single-use guarantee: two requests
    racing the same code produce one winner, because MySQL only lets one UPDATE match.
    """
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                """UPDATE user_recovery_codes SET used_at = %s
                   WHERE id = %s AND user_id = %s AND used_at IS NULL""",
                (datetime.now(timezone.utc), code_id, user_id),
            )
            return cursor.rowcount > 0
    except mysql.connector.Error as err:
        log_info(f"Could not consume recovery code {code_id} | Error: {err}")
        return False
def count_recovery_codes(user_id: int) -> tuple[int, int]:
    """(unused, total) — what the account page shows so a user knows when to regenerate."""
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute(
                """SELECT COUNT(*) AS total, SUM(used_at IS NULL) AS unused
                   FROM user_recovery_codes WHERE user_id = %s""",
                (user_id,),
            )
            row = cursor.fetchone() or {}
            return int(row.get("unused") or 0), int(row.get("total") or 0)
    except mysql.connector.Error as err:
        log_info(f"Could not count recovery codes for user_id {user_id} | Error: {err}")
        return 0, 0
def create_auth_challenge(purpose: str, expires_at: datetime, user_id: Optional[int] = None,
                          challenge: Optional[str] = None,
                          initial_attempts: int = 0) -> Optional[str]:
    """Open a ceremony and return the HANDLE to the caller only — the row stores its SHA-256, the
    same posture as a session token. Expired rows are swept opportunistically here rather than by a
    beat: the table is only ever written on this path, so this is where growth happens.

    `initial_attempts` carries a guessing budget already spent into the new row. Without it the
    per-handle counter is no bound at all on a second-factor login: a fresh handle costs one more
    round of the stage before it, so the same 6-digit code space could be walked five guesses at a
    time forever (see `count_challenge_attempts`).
    """
    import secrets as _secrets
    handle = _secrets.token_urlsafe(24)
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute("DELETE FROM auth_challenges WHERE expires_at < %s",
                           (datetime.now(timezone.utc) - timedelta(hours=1),))
            cursor.execute(
                """INSERT INTO auth_challenges (handle_hash, user_id, purpose, challenge, expires_at,
                                                attempts)
                   VALUES (%s, %s, %s, %s, %s, %s)""",
                (hash_session_token(handle), user_id, purpose, challenge, expires_at,
                 max(0, int(initial_attempts))),
            )
            return handle
    except mysql.connector.Error as err:
        log_info(f"Could not create auth challenge ({purpose}) | Error: {err}")
        return None
def consume_auth_challenge(handle: str, purpose: str) -> Optional[dict]:
    """Claim a ceremony exactly once and return it, or None when it is unknown, expired, already
    used, or for a different purpose.

    The claim is an UPDATE with `consumed_at IS NULL` in the predicate, not a SELECT-then-UPDATE:
    two replays of one assertion must not both find an unconsumed row.
    """
    handle_hash = hash_session_token(handle)
    if not handle_hash:
        return None
    connection = _connection.get_db_connection()
    cursor = connection.cursor(dictionary=True)
    try:
        now = datetime.now(timezone.utc)
        cursor.execute(
            """UPDATE auth_challenges SET consumed_at = %s
               WHERE handle_hash = %s AND purpose = %s AND consumed_at IS NULL AND expires_at > %s""",
            (now, handle_hash, purpose, now),
        )
        if cursor.rowcount != 1:
            connection.commit()
            return None
        cursor.execute(
            "SELECT id, user_id, purpose, challenge FROM auth_challenges WHERE handle_hash = %s",
            (handle_hash,),
        )
        row = cursor.fetchone()
        connection.commit()
        return row
    except mysql.connector.Error as err:
        log_info(f"Could not consume auth challenge ({purpose}) | Error: {err}")
        return None
    finally:
        cursor.close()
        connection.close()
def claim_auth_challenge_attempt(handle: str, purpose: str,
                                 max_attempts: int) -> Optional[dict]:
    """Count ONE attempt against a live challenge and return it, or None when the handle is
    unknown, expired, already finished, or out of attempts.

    This is `consume_auth_challenge`'s sibling for the one ceremony a user can legitimately get
    wrong: typing a 6-digit code. Consuming on first touch would mean a single mistyped digit ends
    the login with no way back but the whole email round trip, so the handle survives a wrong code
    and is burned by the `max_attempts`-th one — atomically, in the same statement that counts it,
    so two concurrent guesses cannot both be the last.

    The count is in MySQL rather than the Redis limiter in front of it on purpose: that limiter
    fails open (utilities/auth_rate_limit.py), and a guessing bound that disappears when Redis does
    is not a bound.
    """
    handle_hash = hash_session_token(handle)
    if not handle_hash:
        return None
    connection = _connection.get_db_connection()
    cursor = connection.cursor(dictionary=True)
    try:
        now = datetime.now(timezone.utc)
        cursor.execute(
            # consumed_at is assigned BEFORE attempts on purpose: MySQL evaluates SET expressions
            # left to right against the values assigned so far, so `attempts` here is still the
            # pre-increment count and `attempts + 1` is the attempt being claimed. Incrementing
            # first would make this read the new value and burn the handle one attempt early.
            """UPDATE auth_challenges
                  SET consumed_at = IF(attempts + 1 >= %s, %s, NULL),
                      attempts = attempts + 1
                WHERE handle_hash = %s AND purpose = %s AND consumed_at IS NULL
                  AND expires_at > %s""",
            (int(max_attempts), now, handle_hash, purpose, now),
        )
        if cursor.rowcount != 1:
            connection.commit()
            return None
        cursor.execute(
            "SELECT id, user_id, purpose, challenge, attempts FROM auth_challenges "
            "WHERE handle_hash = %s",
            (handle_hash,),
        )
        row = cursor.fetchone()
        connection.commit()
        return row
    except mysql.connector.Error as err:
        log_info(f"Could not claim auth challenge attempt ({purpose}) | Error: {err}")
        return None
    finally:
        cursor.close()
        connection.close()
def count_challenge_attempts(user_id: int, purpose: str, since: datetime) -> int:
    """How many guesses this account has already spent on `purpose` since `since`.

    The per-handle counter in `claim_auth_challenge_attempt` bounds ONE pending login; this bounds
    the ACCOUNT. They are not the same bound, and only this one is real: re-running the stage that
    issues the handle mints a fresh counter, so an attacker who can reach that stage (an unbounded
    PIN bypass, or a compromised mailbox — threat T2, the one 2c exists to defeat) otherwise gets
    five guesses per round with nothing accumulating.
    """
    try:
        with db_cursor() as cursor:
            cursor.execute(
                """SELECT COALESCE(SUM(attempts), 0) FROM auth_challenges
                    WHERE user_id = %s AND purpose = %s AND created_at >= %s""",
                (user_id, purpose, since),
            )
            row = cursor.fetchone()
            return int(row[0]) if row and row[0] is not None else 0
    except mysql.connector.Error as err:
        log_info(f"Could not count auth challenge attempts ({purpose}) | Error: {err}")
        # Fail CLOSED: an unreadable counter must not read as an empty one, or the bound it exists
        # to enforce disappears exactly when the database is unhappy.
        return -1
def clear_challenge_attempts(user_id: int, purpose: str) -> bool:
    """Zero this account's spent guesses — called only after a factor actually verified. A correct
    code is proof, and the same proof is what clears the Redis buckets on every other login path;
    without it a user who fat-fingered a code stays part-throttled into their next sign-in.
    """
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                "UPDATE auth_challenges SET attempts = 0 WHERE user_id = %s AND purpose = %s",
                (user_id, purpose),
            )
            return True
    except mysql.connector.Error as err:
        log_info(f"Could not clear auth challenge attempts ({purpose}) | Error: {err}")
        return False
def finish_auth_challenge(handle: str) -> bool:
    """Burn a challenge that has served its purpose — the success half of
    `claim_auth_challenge_attempt`, which leaves the handle live while attempts remain.
    """
    handle_hash = hash_session_token(handle)
    if not handle_hash:
        return False
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                "UPDATE auth_challenges SET consumed_at = %s "
                "WHERE handle_hash = %s AND consumed_at IS NULL",
                (datetime.now(timezone.utc), handle_hash),
            )
            return cursor.rowcount > 0
    except mysql.connector.Error as err:
        log_info(f"Could not finish auth challenge | Error: {err}")
        return False
def mark_session_verified(token: str) -> bool:
    """Stamp `sessions.last_verified_at` — this session just proved a strong factor, which is what
    the step-up gate reads. Live sessions only: a revoked row must not become verified.
    """
    token_hash = hash_session_token(token)
    if not token_hash:
        return False
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                "UPDATE sessions SET last_verified_at = %s WHERE session_token = %s "
                "AND revoked_at IS NULL",
                (datetime.now(timezone.utc), token_hash),
            )
            return cursor.rowcount > 0
    except mysql.connector.Error as err:
        log_info(f"Could not mark session verified | Error: {err}")
        return False
def get_session_auth_state(token: str) -> Optional[dict]:
    """(`last_verified_at`, `scope`) for a live session — everything the step-up gate needs in one
    read. None when the token names no live session.
    """
    token_hash = hash_session_token(token)
    if not token_hash:
        return None
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute(
                "SELECT user_id, last_verified_at, scope FROM sessions "
                "WHERE session_token = %s AND revoked_at IS NULL AND expires_at > %s",
                (token_hash, datetime.now(timezone.utc)),
            )
            return cursor.fetchone()
    except mysql.connector.Error as err:
        log_info(f"Could not read session auth state | Error: {err}")
        return None
def release_enrollment_scope(token: str) -> bool:
    """Promote an `enroll`-held session to a full one — the account just enrolled a strong factor,
    so the hold that forced it here is over (2c.1, issue #905).

    Conditional on the CURRENT scope in the same statement: a `full`, `recovery` or `extension`
    session enrolling a factor must not be widened by this, and two concurrent enrolments cannot
    both promote. Returns False for the ordinary case where nothing was held — not an error.
    """
    token_hash = hash_session_token(token)
    if not token_hash:
        return False
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                "UPDATE sessions SET scope = %s WHERE session_token = %s AND scope = %s",
                (SESSION_SCOPE_FULL, token_hash, SESSION_SCOPE_ENROLL),
            )
            return cursor.rowcount > 0
    except mysql.connector.Error as err:
        log_info(f"Could not release enrollment scope | Error: {err}")
        return False
def set_session_scope(token: str, scope: str) -> bool:
    """Set a session's scope outright (issue #905 / #1026).

    Unlike `release_enrollment_scope` this does not check the scope it is replacing, so it can widen a
    session as easily as narrow one. False when the token does not hash or no row matched.
    """
    token_hash = hash_session_token(token)
    if not token_hash:
        return False
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute("UPDATE sessions SET scope = %s WHERE session_token = %s", (scope, token_hash))
            return cursor.rowcount > 0
    except mysql.connector.Error as err:
        log_info(f"Could not set session scope | Error: {err}")
        return False
def get_app_credential(name: str) -> Optional[str]:
    """The stored value for `name`, or None when unset/unreadable. A DB problem returns None so the
    caller falls back to its env seed rather than losing the credential entirely.
    """
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute("SELECT value FROM app_credentials WHERE name=%s", (name,))
            row = cursor.fetchone()
            value = (row or {}).get("value")
            return str(value) if value else None
    except mysql.connector.Error as err:
        log_warning(f"Could not read app credential {name}", exc=err)
        return None
def set_app_credential(name: str, value: Optional[str], note: Optional[str] = None) -> bool:
    """Upsert a named app credential. Returns True when it was stored."""
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                "INSERT INTO app_credentials (name, value, note) VALUES (%s,%s,%s) "
                "ON DUPLICATE KEY UPDATE value=VALUES(value), note=VALUES(note)",
                (name, value, note))
            return True
    except mysql.connector.Error as err:
        log_error(f"Could not store app credential {name}", exc=err)
        return False
def get_app_credential_updated_at(name: str) -> Optional[datetime]:
    """When `name` was last written, or None when it has never been stored here."""
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute("SELECT updated_at FROM app_credentials WHERE name=%s", (name,))
            row = cursor.fetchone()
            return (row or {}).get("updated_at")
    except mysql.connector.Error as err:
        log_warning(f"Could not read app credential timestamp for {name}", exc=err)
        return None
