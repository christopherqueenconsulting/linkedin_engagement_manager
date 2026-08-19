"""Every SQL statement LEM runs against the users tables.

Split out of `cqc_lem.utilities.db` (issue #1154). The fail-soft reader contract and the
secret-sealing rules described there apply here unchanged; `cqc_lem.utilities.db`
re-exports every name below, so existing importers and patch targets keep resolving.
"""

import json
import uuid
from datetime import (
    datetime,
    timedelta,
    timezone,
)
from typing import Optional

import mysql.connector
from mysql.connector import errorcode
from mysql.connector.abstracts import MySQLCursorAbstract

from cqc_lem.platform.db import connection as _connection
from cqc_lem.platform.db.connection import db_cursor
from cqc_lem.platform.db.enums import (
    CatchupEventType,
    OnboardingStep,
    PostStatus,
)
from cqc_lem.platform.db.shared import (
    DEFAULT_CONTENT_BUFFER_DAYS,
    DEFAULT_CONTENT_BUFFER_MAX_POSTS,
    MAX_CONTENT_BUFFER_DAYS,
    ONBOARDING_STEPS,
    VALID_VIDEO_QUALITIES,
)
from cqc_lem.utilities.crypto import (
    decrypt_secret,
    encrypt_secret,
    encryption_enabled,
    needs_reencrypt,
)
from cqc_lem.utilities.linkedin.profile import LinkedInProfile
from cqc_lem.utilities.logger import log_debug, log_error, log_info, log_warning
from cqc_lem.utilities.utils import get_top_level_domain

_ONBOARDING_COLS: tuple = tuple(f"{step.value}_at" for step in ONBOARDING_STEPS)
# Issue #745 (PR 2a) — the four columns holding a LinkedIn credential at rest. The names are the
# encryption AAD (`<table>.<column>`), so they are part of the ciphertext contract: renaming one
# makes every existing row undecryptable. db.py is the ONLY place encrypt/decrypt is called, so the
# ten modules that consume these secrets cannot accidentally bypass it. See utilities/crypto.py.
SECRET_FIELD_COOKIE_VALUE = "cookies.value"
SECRET_FIELD_ACCESS_TOKEN = "users.access_token"
SECRET_FIELD_REFRESH_TOKEN = "users.refresh_token"
SECRET_FIELD_PASSWORD = "users.password"
# The only statements the backfill/rotation pass may run — fixed literals, never composed from a
# row, so there is no path by which data becomes SQL.
_SECRET_UPDATE_SQL = {
    SECRET_FIELD_PASSWORD: "UPDATE users SET password = %s WHERE id = %s",
    SECRET_FIELD_ACCESS_TOKEN: "UPDATE users SET access_token = %s WHERE id = %s",
    SECRET_FIELD_REFRESH_TOKEN: "UPDATE users SET refresh_token = %s WHERE id = %s",
    SECRET_FIELD_COOKIE_VALUE: "UPDATE cookies SET value = %s WHERE id = %s",
}
def _store_cookie_rows(cursor: MySQLCursorAbstract, cookies: list[dict],
                       user_id: Optional[int]) -> list[str]:
    """Insert/update each cookie row; returns the names of the ones that could NOT be stored.

    Issue #745: a row with no user_id cannot be bound by the AAD, so encrypt_secret would store the
    value as PLAINTEXT — and get_cookies JOINs users, so nothing could ever read it back. That
    leaves a live li_at in the clear which encrypt_secrets_at_rest (scanning user-owned rows) would
    never find. Refuse the write instead of creating an unreadable plaintext credential.
    """
    if user_id is None:
        log_error("Refusing to store cookies with no user_id — the row could not be bound to a "
                  "user, so it would be a plaintext session no read path can return")
        return [str(cookie.get('name')) for cookie in cookies]

    failed: list[str] = []
    for cookie in cookies:
        try:
            cursor.execute("""
                INSERT INTO cookies (name, value, domain, path, expiry, secure, http_only, user_id)
                VALUES (%s, %s, %s, %s, FROM_UNIXTIME(%s), %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    value = VALUES(value),
                    path = VALUES(path),
                    expiry = VALUES(expiry),
                    secure = VALUES(secure),
                    http_only = VALUES(http_only)
               
            """, (

                cookie['name'],
                encrypt_secret(cookie['value'], user_id, SECRET_FIELD_COOKIE_VALUE),
                cookie['domain'],
                cookie['path'],
                cookie['expiry'] if 'expiry' in cookie else None,
                cookie['secure'],
                cookie['httpOnly'],
                user_id
            ))
        except mysql.connector.Error as err:
            log_error("Could not add cookie to database", exc=err)
            failed.append(str(cookie.get('name')))
    return failed
def prune_superseded_cookies(user_id: int) -> int:
    """Keep only the most-recently-updated row per (user_id, name), deleting older duplicates
    left behind when the same cookie is re-stored under a different domain scope — e.g. the
    extension writes li_at on '.linkedin.com' while a prior Selenium login stored it on
    '.www.linkedin.com'. get_cookies matches on `domain LIKE %tld%`, so a stale variant would
    otherwise be returned alongside the fresh one and could shadow it at login.

    Conservative by design: it only deletes a row when a STRICTLY newer sibling of the same
    name exists for the same user, so it never removes the newest copy and never touches a
    uniquely-named cookie. Best-effort — a failure here never breaks the cookie write.
    """
    connection = _connection.get_db_connection()
    cursor = connection.cursor()
    deleted = 0
    try:
        cursor.execute("""
            DELETE older
            FROM cookies older
            JOIN cookies newer
              ON older.user_id = newer.user_id
             AND older.name = newer.name
             AND (newer.updated_at > older.updated_at
                  OR (newer.updated_at = older.updated_at AND newer.id > older.id))
            WHERE older.user_id = %s
        """, (user_id,))
        deleted = cursor.rowcount
        connection.commit()
        if deleted:
            log_info(f"Pruned {deleted} superseded cookie(s) for user_id {user_id}")
    except mysql.connector.Error as err:
        log_error("Could not prune superseded cookies", exc=err, user_id=user_id)
    finally:
        cursor.close()
        connection.close()
    return deleted
def get_cookies(url: str, user_email: str):
    """Selenium-ready cookie dicts for `url`'s top-level domain, for one user's stored session.

    `user_id` is selected only to unseal the row and is popped before returning — `add_cookie()` rejects
    any key it does not know. A cookie whose value would not decrypt is DROPPED rather than handed back
    empty: an empty `li_at` would install a dead session that LEM then reports as signed in.

    Returns None (not []) when the query itself failed, so "nothing stored" and "could not read the
    cookie table" stay distinguishable.
    """
    connection = _connection.get_db_connection()
    cursor = connection.cursor(dictionary=True)

    # Extract the top-level domain from the URL
    tld = get_top_level_domain(url)

    try:
        cursor.execute("""
            SELECT c.name, c.value, c.domain, c.path, UNIX_TIMESTAMP(c.expiry) AS expiry, c.secure,
                   c.http_only, c.user_id
            FROM cookies c
            JOIN users u ON c.user_id = u.id
            WHERE c.domain LIKE %s AND u.email = %s
        """, (f"%{tld}%", user_email))

        cookies = cursor.fetchall()
        for cookie in cookies or []:
            # user_id is selected only to unseal the row — Selenium's add_cookie() rejects any
            # key it doesn't know, so it must not survive into the returned dict.
            cookie['value'] = decrypt_secret(
                cookie['value'], cookie.pop('user_id', None), SECRET_FIELD_COOKIE_VALUE)
        # A cookie that could not be decrypted is worse than a missing one: Selenium would set an
        # empty li_at and LEM would report "logged in" against a dead session.
        cookies = [c for c in (cookies or []) if c.get('value')]
    except mysql.connector.Error as err:
        log_error("Could not get cookies from DB", exc=err)
        cookies = None
    finally:
        cursor.close()
        connection.close()

    return cookies
def has_linkedin_session(user_id: int) -> bool:
    """True if the user has a stored LinkedIn session cookie (li_at) to log in with."""
    try:
        with db_cursor() as cursor:
            cursor.execute(
                "SELECT 1 FROM cookies WHERE user_id = %s AND name = 'li_at' LIMIT 1",
                (user_id,),
            )
            return cursor.fetchone() is not None
    except mysql.connector.Error as err:
        log_error("Could not check linkedin session", exc=err, user_id=user_id)
        return False
def get_linkedin_session_email_sent_at(user_id: int):
    """Return the datetime the last session notification email was sent, or None."""
    try:
        with db_cursor() as cursor:
            cursor.execute(
                "SELECT linkedin_session_email_sent_at FROM users WHERE id = %s", (user_id,)
            )
            row = cursor.fetchone()
            return row[0] if row else None
    except mysql.connector.Error as err:
        log_error("Could not read session email timestamp", exc=err, user_id=user_id)
        return None
def set_linkedin_session_email_sent_at(user_id: int) -> bool:
    """Stamp now() as the last session notification email time (throttle)."""
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                "UPDATE users SET linkedin_session_email_sent_at = NOW() WHERE id = %s", (user_id,)
            )
            return True
    except mysql.connector.Error as err:
        log_error("Could not set session email timestamp", exc=err, user_id=user_id)
        return False
def add_user(email: str, password: str):
    """Create a user from an email + password, sealing the password against the id the INSERT allocates.

    Two statements on purpose: the ciphertext is bound to `users.id` as AAD, which auto-increment only
    hands out once the row exists. A duplicate email is logged and swallowed, and nothing is returned
    either way — the caller learns the outcome by looking the user up.
    """
    try:
        with db_cursor(commit=True) as cursor:
        # The row has to exist before the password can be encrypted — the ciphertext is bound to
        # users.id (AAD), which auto-increment only hands out on INSERT.
            cursor.execute("INSERT INTO users (email) VALUES (%s)", (email,))
            user_id = cursor.lastrowid
            cursor.execute("UPDATE users SET password = %s WHERE id = %s",
                           (encrypt_secret(password, user_id, SECRET_FIELD_PASSWORD), user_id))
    except mysql.connector.Error as e:
        if e.errno == errorcode.ER_DUP_ENTRY:
            # DEBUG: the docstring calls a duplicate email logged-and-swallowed — the caller learns
            # the outcome by looking the user up, so a replayed signup is working behaviour.
            log_debug(f"User with email {email} already exists.")
        else:
            log_error("Could not create user", exc=e)
def add_user_with_access_token(email: str, linked_sub_id: str, access_token: str, access_token_expires_in: str,
                               refresh_token: str = None,
                               refresh_token_expires_in: str = None):
    """Upsert a user from a LinkedIn OAuth callback and store the sealed tokens.

    Split into an identity upsert and a token UPDATE because the tokens are sealed against `users.id`,
    which does not exist yet for a brand-new user. On the ON DUPLICATE KEY branch MySQL does not report
    the existing row's id in `lastrowid` — it can hand back the auto-increment value the failed insert
    allocated and burned — so `id = LAST_INSERT_ID(id)` pins it and an email lookup backs that up.
    Sealing against the wrong id stores a token bound to a row that does not exist, which is
    indistinguishable from storing no token at all.

    Errors are logged, not raised, and nothing is returned either way.
    """
    connection = _connection.get_db_connection()
    cursor = connection.cursor()

    access_token_created_at = datetime.now(timezone.utc)

    if refresh_token is not None:
        refresh_token_created_at = datetime.now(timezone.utc)
    else:
        refresh_token_created_at = None

    try:
        # Two statements on purpose (issue #745): the OAuth tokens are encrypted under a key
        # derived from users.id, which does not exist yet for a brand-new user. Upsert the
        # non-secret identity columns first, then write the ciphertext against the settled id.
        # `id = LAST_INSERT_ID(id)` is load-bearing: on the ON DUPLICATE KEY UPDATE branch MySQL
        # does NOT report the existing row's id in lastrowid — it can hand back the auto-increment
        # value that was allocated and burned by the failed insert. Sealing the tokens against that
        # id would bind them to a row that does not exist (silently storing no token at all), so
        # the id is pinned explicitly here and still cross-checked by email below.
        cursor.execute("""INSERT INTO users (email, linked_sub_id, last_login, linkedin_connection_status)
        VALUES (%s, %s, %s, 'connected')
        ON DUPLICATE KEY UPDATE
                id = LAST_INSERT_ID(id),
                linked_sub_id = VALUES(linked_sub_id),
                last_login = VALUES(last_login),
                linkedin_connection_status = 'connected'
        """, (email, linked_sub_id, datetime.now(timezone.utc)))
        user_id = cursor.lastrowid
        if not user_id:
            cursor.execute("SELECT id FROM users WHERE email = %s", (email,))
            row = cursor.fetchone()
            user_id = row[0] if row else None
        if user_id is None:
            raise mysql.connector.Error(f"Could not resolve user id for {email}")
        cursor.execute("""UPDATE users SET
                access_token = %s,
                access_token_expires_in = %s,
                access_token_created_at = %s,
                refresh_token = %s,
                refresh_token_expires_in = %s,
                refresh_token_created_at = %s
            WHERE id = %s""", (
            encrypt_secret(access_token, user_id, SECRET_FIELD_ACCESS_TOKEN),
            access_token_expires_in, access_token_created_at,
            encrypt_secret(refresh_token, user_id, SECRET_FIELD_REFRESH_TOKEN),
            refresh_token_expires_in, refresh_token_created_at,
            user_id))
        connection.commit()
    except mysql.connector.Error as e:
        if e.errno == errorcode.ER_DUP_ENTRY:
            # DEBUG: an OAuth callback for an existing account is the normal repeat path.
            log_debug(f"User with email {email} already exists.")
        else:
            log_error("Could not upsert user from OAuth callback", exc=e)
    finally:
        cursor.close()
        connection.close()
def get_user_linked_sub_id(user_id: int):
    """The LinkedIn OAuth subject id stored for this user.

    None covers both "no such user" and a failed read.
    """
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute("SELECT linked_sub_id FROM users WHERE id = %s", (user_id,))

            linked_sub_id = cursor.fetchone()
    except mysql.connector.Error as err:
        log_error("Could not get user linked sub id", exc=err)
        linked_sub_id = None

    return linked_sub_id['linked_sub_id'] if linked_sub_id else None
def get_user_access_token(user_id: int):
    """The user's decrypted LinkedIn access token, or None when it is missing, expired or unreadable.

    Expiry is evaluated in SQL against the database's own NOW(), so a lapsed token reads as ABSENT rather
    than as a token that will 401 later; a row with no recorded created_at/expires_in is treated as still
    valid. A token that will not decrypt also comes back None.
    """
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute(
                "SELECT access_token FROM users WHERE id = %s AND ("
                "access_token_created_at IS NULL "
                "OR access_token_expires_in IS NULL "
                "OR DATE_ADD(access_token_created_at, INTERVAL access_token_expires_in SECOND) > NOW()"
                ")",
                (user_id,),
            )

            access_token = cursor.fetchone()
    except mysql.connector.Error as err:
        log_error("Could not get user access token", exc=err)
        access_token = None

    if not access_token:
        return None
    return decrypt_secret(access_token['access_token'], user_id, SECRET_FIELD_ACCESS_TOKEN)
def get_user_id(email: str):
    """Resolve an email address to a user id.

    None conflates "no such address" with "the lookup failed", so it is never on its own proof that an
    account does not exist.
    """
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute("SELECT id FROM users WHERE email = %s", (email,))

            user_id = cursor.fetchone()
    except mysql.connector.Error as err:
        log_error("Could not get user id", exc=err)
        user_id = None

    return user_id['id'] if user_id else None
def get_default_video_quality(user_id: int) -> str:
    """The user's preferred default video quality for AUTO-generated posts (engagement_preferences).
    Falls back to 'standard' when unset/invalid — premium is only ever honored when credits exist,
    which is enforced separately at render time.
    """
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute(
                "SELECT default_video_quality FROM engagement_preferences WHERE user_id = %s",
                (user_id,))
            row = cursor.fetchone()
            quality = row.get("default_video_quality") if row else None
            return quality if quality in VALID_VIDEO_QUALITIES else "standard"
    except mysql.connector.Error as err:
        log_error("Could not get default video quality", exc=err, user_id=user_id)
        return "standard"
def get_user_password_pair_by_id(user_id: int):
    """The (email, decrypted password) pair the Selenium login uses.

    Always a two-tuple: a missing row or a failed read is `(None, None)`, never a bare None, so the unpack
    at every call site holds. A password that will not decrypt comes back None with the email intact.
    """
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute("SELECT email, password FROM users WHERE id = %s", (user_id,))

            user_password_pair = cursor.fetchone()
    except mysql.connector.Error as err:
        log_error(f"Could not get user password pair for user id: {user_id}", exc=err)
        user_password_pair = None

    if user_password_pair:
        return (user_password_pair['email'],
                decrypt_secret(user_password_pair['password'], user_id, SECRET_FIELD_PASSWORD))
    else:
        return None, None
def add_linkedin_profile(profile: LinkedInProfile, user_id: Optional[int] = None):
    """Upsert a scraped LinkedIn profile.

    `user_id` is COALESCEd rather than overwritten, so re-scraping a profile with no account attached
    never unlinks a row that was already tied to one.
    """
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute("""
                INSERT INTO profiles (profile_url, email, data, user_id)
                VALUES (%s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                        profile_url = VALUES(profile_url),
                        email = VALUES(email),
                        data = VALUES(data),
                        user_id = COALESCE(VALUES(user_id), user_id)
                """,
                           (str(profile.profile_url), profile.email, profile.model_dump_json(), user_id))

            success = True
    except mysql.connector.Error as err:
        log_error("Could not add linkedin profile", exc=err)
        success = False
    return success
def get_linked_in_profile_by_url(profile_url: str, updated_less_than_days_ago: int = 1):
    """The stored profile JSON for a URL, as the one-column row tuple, but only while it is FRESH.

    `updated_less_than_days_ago` is a cache window, not a filter on the person: a row older than that
    reads as ABSENT so the caller re-scrapes instead of acting on stale headline/about text. Both slash
    spellings are queried because LinkedIn hands out both.
    """
    connection = _connection.get_db_connection()
    cursor = connection.cursor()

    profile_url_without_end_slash = profile_url.rstrip('/')
    profile_url_with_end_slash = profile_url_without_end_slash + '/'

    try:
        cursor.execute(
            "SELECT data FROM profiles WHERE (profile_url = %s or profile_url = %s) AND updated_at > NOW() - INTERVAL %s DAY",
            (profile_url_with_end_slash, profile_url_without_end_slash, updated_less_than_days_ago))
        profile_data = cursor.fetchone()
    except mysql.connector.Error as err:
        profile_data = None
        log_error("Could not get linkedin profile by url", exc=err)
    finally:
        cursor.close()
        connection.close()

    return profile_data
def get_linked_in_profile_by_email(profile_email: str, updated_less_than_days_ago: int = 1):
    """The stored profile JSON for an email, as the one-column row tuple.

    Same freshness window as `get_linked_in_profile_by_url` — a row older than
    `updated_less_than_days_ago` reads as absent rather than stale.
    """
    try:
        with db_cursor() as cursor:
            cursor.execute("SELECT data FROM profiles WHERE email = %s AND updated_at > NOW() - INTERVAL %s DAY",
                           (profile_email, updated_less_than_days_ago))
            profile_data = cursor.fetchone()
    except mysql.connector.Error as err:
        log_error("Could not get linkedin profile data by email", exc=err)
        profile_data = None

    return profile_data
def get_linked_in_profile_by_user_id(user_id: int, updated_less_than_days_ago: int = 1):
    """The stored profile JSON for a user, as the one-column row tuple.

    Same freshness window as `get_linked_in_profile_by_url` — a row older than
    `updated_less_than_days_ago` reads as absent rather than stale.
    """
    try:
        with db_cursor() as cursor:
            cursor.execute("SELECT data FROM profiles WHERE user_id = %s AND updated_at > NOW() - INTERVAL %s DAY",
                           (user_id, updated_less_than_days_ago))
            profile_data = cursor.fetchone()
    except mysql.connector.Error as err:
        log_error("Could not get linkedin profile data by user_id", exc=err)
        profile_data = None

    return profile_data
def get_profile_synthesis(user_id: int) -> Optional[tuple]:
    """Return the user's cached (synthesis_text, synthesis_generated_at) or None when there is no
    profile row / no synthesis yet. Kept separate from the profile-JSON getters so the small, stable
    voice brief can be read cheaply on every generation call without pulling the full profile blob.
    """
    try:
        with db_cursor() as cursor:
            cursor.execute(
                "SELECT synthesis, synthesis_generated_at FROM profiles WHERE user_id = %s", (user_id,))
            row = cursor.fetchone()
    except mysql.connector.Error as err:
        log_error("Could not get profile synthesis", exc=err, user_id=user_id)
        row = None

    if not row or row[0] is None:
        return None
    return row[0], row[1]
def set_profile_synthesis(user_id: int, synthesis: str) -> bool:
    """Persist a freshly generated voice synthesis and stamp synthesis_generated_at = NOW() (drives
    the weekly staleness selector). No-op-safe: returns False if the profile row doesn't exist yet.
    """
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                "UPDATE profiles SET synthesis = %s, synthesis_generated_at = NOW() WHERE user_id = %s",
                (synthesis, user_id))
            success = cursor.rowcount > 0
    except mysql.connector.Error as err:
        log_error("Could not set profile synthesis", exc=err, user_id=user_id)
        success = False
    return success


def get_last_recorded_skills(user_id: int) -> list:
    """The top-5 profile-skill snapshot from the last scrape (issue #1075), [] when there is none.

    Stored as JSON on the profile row so skill-change detection survives a Redis restart. Fails
    soft: an unreadable row reads as "no snapshot", which makes the next scrape a first scrape
    rather than a spurious change.
    """
    try:
        with db_cursor() as cursor:
            cursor.execute(
                "SELECT last_recorded_skills FROM profiles WHERE user_id = %s", (user_id,))
            row = cursor.fetchone()
    except mysql.connector.Error as err:
        log_error("Could not get last recorded skills", exc=err, user_id=user_id)
        return []

    if not row or row[0] is None:
        return []
    raw = row[0]
    if isinstance(raw, list):
        return raw
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return []
    return parsed if isinstance(parsed, list) else []


def set_last_recorded_skills(user_id: int, skills: list) -> bool:
    """Persist the top-5 skill snapshot as JSON.

    False when nothing was written — a DB error, or no `profiles` row for this user yet. Callers
    only write on a detected change, so an UPDATE that matches a row always reports one.
    """
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                "UPDATE profiles SET last_recorded_skills = %s WHERE user_id = %s",
                (json.dumps(skills or []), user_id))
            return cursor.rowcount > 0
    except mysql.connector.Error as err:
        log_error("Could not set last recorded skills", exc=err, user_id=user_id)
        return False


def get_user_ids_needing_profile_synthesis(stale_days: int = 7) -> list:
    """User IDs whose cached profile synthesis is MISSING or older than `stale_days` — the work list
    for the weekly refresh task. Only rows that actually have a profile (user_id NOT NULL) qualify.
    """
    try:
        with db_cursor() as cursor:
            cursor.execute(
                "SELECT user_id FROM profiles WHERE user_id IS NOT NULL AND ("
                "synthesis IS NULL OR synthesis_generated_at IS NULL "
                "OR synthesis_generated_at < NOW() - INTERVAL %s DAY)",
                (stale_days,))
            rows = cursor.fetchall()
    except mysql.connector.Error as err:
        log_error("Could not get user_ids needing profile synthesis", exc=err)
        rows = []
    return [row[0] for row in rows]
def remove_linked_in_profile_by_user_id(user_id: int):
    """Drop this user's cached profile row so the next read re-scrapes.

    True means the DELETE ran, not that a row existed.
    """
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute("DELETE FROM profiles WHERE user_id = %s", (user_id,))
            success = True
    except mysql.connector.Error as err:
        log_error("Could not remove linkedin profile by user_id", exc=err)
        success = False
    return success
def remove_linked_in_profile_by_url(profile_url: str):
    """Drop the cached profile row for a URL so the next read re-scrapes.

    True means the DELETE ran, not that a row existed.
    """
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute("DELETE FROM profiles WHERE profile_url = %s", (profile_url,))
            success = True
    except mysql.connector.Error as err:
        log_error("Could not remove linkedin profile by url", exc=err)
        success = False
    return success
def remove_linked_in_profile_by_email(profile_email: str):
    """Drop the cached profile row for an email so the next read re-scrapes.

    True means the DELETE ran, not that a row existed.
    """
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute("DELETE FROM profiles WHERE email = %s", (profile_email,))
            success = True
    except mysql.connector.Error as err:
        log_error("Could not remove linkedin profile by email", exc=err)
        success = False
    return success
MAX_CONTENT_BUFFER_POSTS = 30
def get_user_blog_url(user_id: int):
    """Query the database to get the blog URL for the given user."""
    try:
        with db_cursor() as cursor:
            cursor.execute("SELECT blog_url FROM users WHERE id = %s", (user_id,))
            blog_url = cursor.fetchone()
    except mysql.connector.Error as err:
        log_error("Could not get user blog url", exc=err)
        blog_url = None

    return blog_url[0] if blog_url else None
def get_user_sitemap_url(user_id: int):
    """Query the database to get the sitemap URL for the given user."""
    try:
        with db_cursor() as cursor:
            cursor.execute("SELECT sitemap_url FROM users WHERE id = %s", (user_id,))
            sitemap_url = cursor.fetchone()
    except mysql.connector.Error as err:
        log_error("Could not get user sitemap url", exc=err)
        sitemap_url = None

    return sitemap_url[0] if sitemap_url else None
def get_linkedin_profile_url_by_user_id(user_id: int) -> Optional[str]:
    """Return the user's own LinkedIn profile URL (e.g. https://www.linkedin.com/in/<vanity>/).
    Only the user's own scraped profile carries a non-null user_id in the profiles table.
    """
    try:
        with db_cursor() as cursor:
            cursor.execute("SELECT profile_url FROM profiles WHERE user_id = %s LIMIT 1", (user_id,))
            row = cursor.fetchone()
    except mysql.connector.Error as err:
        log_error("Could not get user linkedin profile url", exc=err)
        row = None

    return row[0] if row else None
# `email` is deliberately NOT here (issue #950). An address is not a profile field: moving one has
# to write `user_email_history`, PIN the NEW address and revoke the account's other sessions, and
# `change_user_email` is the only path that does all three. `update_user` used to take an `email=`
# and UPDATE the column directly, which walked around every one of them — #914 removed its last
# caller, so what was left was a loaded footgun one keyword argument from being fired again.
_ALLOWED_USER_CLAUSES = frozenset({"blog_url = %s", "sitemap_url = %s"})


class _Unset:
    """Type of `UNSET` — see `update_user`."""


#: "This column was not supplied, leave it alone." Distinct from `None`, which is the caller
#: asking for the column to be CLEARED (issue #1574).
UNSET = _Unset()


def update_user(user_id: int, blog_url: Optional[str] | _Unset = UNSET,
                sitemap_url: Optional[str] | _Unset = UNSET) -> bool:
    """Update the blog and/or sitemap URL on a user row; False when neither was supplied.

    Only fields that were passed become SET clauses, and each generated clause is re-checked against
    `_ALLOWED_USER_CLAUSES` before it is interpolated — see the note above that set for why `email` is
    deliberately not reachable from here.

    Two things a falsy check used to swallow, both of which read to the user as "saving my profile
    settings does nothing" (issue #1574). Passing `None` or `""` CLEARS the column, so removing a
    blog URL is a real write instead of a no-op the SPA still reports as saved — omitting the
    argument entirely is how a caller leaves a column alone. And a statement that MATCHED the row
    but changed nothing (re-saving the same URL) is a success: MySQL reports 0 changed rows for it,
    so returning False there answered a plain re-save with "Update failed".
    """
    supplied = [("blog_url = %s", blog_url), ("sitemap_url = %s", sitemap_url)]
    fields, values = [], []
    for clause, value in supplied:
        if isinstance(value, _Unset):
            continue
        fields.append(clause)
        # "" and None are the same intent — an empty column, stored as NULL.
        values.append(value or None)
    if not fields:
        return False
    for clause in fields:
        if clause not in _ALLOWED_USER_CLAUSES:
            raise ValueError(f"Disallowed SQL clause: {clause!r}")
    connection = _connection.get_db_connection()
    cursor = connection.cursor()
    values.append(user_id)
    try:
        cursor.execute(f"UPDATE users SET {', '.join(fields)} WHERE id = %s", values)
        connection.commit()
        if cursor.rowcount > 0:
            return True
        # 0 CHANGED rows is either "the values already matched" or "no such user". Only the second
        # is a failure, so ask which one it was rather than reporting a failed save either way.
        cursor.execute("SELECT 1 FROM users WHERE id = %s", (user_id,))
        return cursor.fetchone() is not None
    except mysql.connector.Error as err:
        log_error(f"Could not update user {user_id}", exc=err)
        return False
    finally:
        cursor.close()
        connection.close()
def get_active_user_ids():
    """Return user IDs eligible for automated posting/engagement.

    A user is active when ALL of:
      1. Has a valid LinkedIn connection (linkedin_connection_status = 'connected'
         AND access_token not expired)
      2. Has an active subscription OR an unexpired trial
      3. Has logged in within their configured inactivate delay
         (NULL delay = never auto-inactivate)
      4. Is not admin-disabled (issue #1603) — the ONE per-user gate every lane reads through
         this function, so `disabled_at` needs no per-lane wiring of its own.
    """
    try:
        with db_cursor() as cursor:
            cursor.execute("""
                SELECT id FROM users
                WHERE
                    -- Must have a live LinkedIn token
                    linkedin_connection_status = 'connected'
                    AND access_token IS NOT NULL
                    AND access_token_created_at IS NOT NULL
                    AND access_token_created_at + INTERVAL access_token_expires_in SECOND > NOW()

                    -- Must have an active or unexpired trial subscription
                    AND (
                        subscription_status = 'active'
                        OR (
                            subscription_status = 'trial'
                            AND (trial_ends_at IS NULL OR trial_ends_at > NOW())
                        )
                    )

                    -- Must have logged in within their configured inactivity window.
                    -- NULL last_login (pre-session-migration users) is treated as active
                    -- so existing connected users are not silently dropped.
                    AND (
                        last_login_inactivate_delay IS NULL
                        OR last_login IS NULL
                        OR last_login >= NOW() - INTERVAL last_login_inactivate_delay DAY
                    )

                    -- Must not be admin-disabled.
                    AND disabled_at IS NULL
            """)
            active_user_ids = [row[0] for row in cursor.fetchall()]
    except mysql.connector.Error as err:
        log_error("Could not get active user ids", exc=err)
        active_user_ids = []

    return active_user_ids
def get_linkedin_token_user_ids() -> list[int]:
    """Subscribed users holding a LinkedIn access token, expired or not (issue #600).

    Deliberately NOT get_active_user_ids(): that one requires an unexpired token, so the users the
    renewal pass most needs to reach — the ones whose authorization already lapsed — are exactly
    the ones it filters out.
    """
    try:
        with db_cursor() as cursor:
            cursor.execute("""
                SELECT id FROM users
                WHERE linkedin_connection_status = 'connected'
                  AND access_token IS NOT NULL
                  AND (
                        subscription_status = 'active'
                        OR (
                            subscription_status = 'trial'
                            AND (trial_ends_at IS NULL OR trial_ends_at > NOW())
                        )
                  )
            """)
            return [row[0] for row in cursor.fetchall()]
    except mysql.connector.Error as err:
        log_error("Could not get linkedin token user ids", exc=err)
        return []
def get_user_location(user_id: int) -> tuple[float, float] | None:
    """The user's Login Location as `(latitude, longitude)`, or None when it is not usable.

    A missing row, a failed read and a stored 0 all read as None: 0/0 is a point in the Atlantic, not a
    place anyone logs in from, so it must never reach the proxy/geo logic as a real coordinate.
    """
    try:
        with db_cursor() as cursor:
            cursor.execute("SELECT latitude, longitude FROM users WHERE id = %s", (user_id,))
            row = cursor.fetchone()
    except mysql.connector.Error as err:
        log_error("Could not get user location", exc=err)
        row = None
    return (float(row[0]), float(row[1])) if row and row[0] and row[1] else None
def get_company_linked_in_url_for_user(user_id: int):
    """The user's LinkedIn company page URL.

    None when it was never set or the read failed — the invite drip has no page to open in either case.
    """
    try:
        with db_cursor() as cursor:
            cursor.execute("SELECT company_linked_in_url FROM users WHERE id = %s", (user_id,))
            company_linked_in_url = cursor.fetchone()
    except mysql.connector.Error as err:
        log_error("Could not get user company linked in url", exc=err)
        company_linked_in_url = None

    return company_linked_in_url[0] if company_linked_in_url else None
def update_company_linked_in_url_for_user(user_id: int, company_linked_in_url: Optional[str]) -> bool:
    """Set (or clear, when None/empty) the user's LinkedIn company page URL used by the
    monthly company-page invite automation.
    """
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                "UPDATE users SET company_linked_in_url = %s WHERE id = %s",
                (company_linked_in_url or None, user_id),
            )
            return cursor.rowcount >= 0
    except mysql.connector.Error as err:
        log_error("Could not update company linked in url", exc=err, user_id=user_id)
        return False
def get_user_linkedin_display_name(user_id: int) -> Optional[str]:
    """The user's own name exactly as LinkedIn renders it on their messages (issue #731), or None.

    This is what reply detection compares the last sender against, so it is stored per user rather
    than re-derived from a scrape that may be stale or unavailable.
    """
    try:
        with db_cursor() as cursor:
            cursor.execute("SELECT linkedin_display_name FROM users WHERE id = %s", (user_id,))
            row = cursor.fetchone()
    except mysql.connector.Error as err:
        log_error("Could not get LinkedIn display name", exc=err, user_id=user_id)
        return None
    name = (row[0] if row else None) or ""
    return name.strip() or None
def update_user_linkedin_display_name(user_id: int, display_name: Optional[str]) -> bool:
    """Set (or clear, when None/empty) the user's LinkedIn display name."""
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                "UPDATE users SET linkedin_display_name = %s WHERE id = %s",
                ((display_name or "").strip() or None, user_id),
            )
            return cursor.rowcount >= 0
    except mysql.connector.Error as err:
        log_error("Could not update LinkedIn display name", exc=err, user_id=user_id)
        return False
def update_user_linkedin_password(user_id: int, password: str) -> bool:
    """Store the user's LinkedIn login password for Selenium-driven automation.

    DEPRECATED (issue #745, design decision 2A): cookie-only (`li_at`) is the default now — see
    store_linkedin_li_at. The password must be stored reversibly because Selenium types it into
    the LinkedIn login form, so encryption at rest is the ceiling on how safe it can ever be;
    draining the column via clear_user_linkedin_password is the actual fix. Only call this from
    authenticated API endpoints — never expose the value in any response payload.
    """
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                "UPDATE users SET password = %s WHERE id = %s",
                (encrypt_secret(password, user_id, SECRET_FIELD_PASSWORD), user_id),
            )
            return cursor.rowcount >= 0
    except mysql.connector.Error as err:
        log_error("Could not update LinkedIn password", exc=err, user_id=user_id)
        return False
def clear_user_linkedin_password(user_id: int) -> bool:
    """Drop the stored LinkedIn password once the user has a session cookie instead (design §5.4).

    Encrypting the password still leaves a *decryptable* LinkedIn password in the DB, so the
    approved end state is to stop holding one at all. Called from the cookie-migration path, not
    on every cookie save — a user who has no li_at yet must keep their only working login.
    """
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute("UPDATE users SET password = NULL WHERE id = %s", (user_id,))
            log_info("Cleared stored LinkedIn password after cookie migration", user_id=user_id)
            return True
    except mysql.connector.Error as err:
        log_error(f"Could not clear LinkedIn password | Error: {err}", user_id=user_id)
        return False
def has_linkedin_password(user_id: int) -> bool:
    """True when a LinkedIn password is still stored for this user — the signal that drives the
    one-time 'paste a cookie instead' prompt (design §5.4 item 3).
    """
    try:
        with db_cursor() as cursor:
            cursor.execute(
                "SELECT 1 FROM users WHERE id = %s AND password IS NOT NULL AND password <> '' LIMIT 1",
                (user_id,),
            )
            return cursor.fetchone() is not None
    except mysql.connector.Error as err:
        log_error(f"Could not check stored LinkedIn password | Error: {err}", user_id=user_id)
        return False
def encrypt_secrets_at_rest(limit: Optional[int] = None) -> dict:
    """Backfill AND rotation in one pass (issue #745, design §5.1/§7 Stage 0).

    Rewrites every stored LinkedIn secret that is not already an envelope under the CURRENT key
    version: legacy plaintext gets encrypted, and a row still sealed under `LEM_SECRET_KEY_PREVIOUS`
    gets re-sealed under the new key. Both cases read through the same dual-mode `decrypt_secret`,
    which is why rotation needs no separate code path.

    **Idempotent** — a second run finds nothing to do, which is what makes it safe to schedule
    daily instead of hand-running once. A row that cannot be decrypted is counted and LEFT ALONE:
    overwriting it would destroy a secret that a corrected key might still recover.

    `plaintext_remaining` is the number the operator watches — once it reaches 0, `ENCRYPTION_REQUIRED`
    can be flipped and the legacy read path fails closed. It includes `orphaned` (see below), so the
    gate can never read 0 while a plaintext session is still sitting in a dump.
    """
    stats = {"enabled": encryption_enabled(), "scanned": 0, "rewritten": 0,
             "failed": 0, "orphaned": 0, "plaintext_remaining": 0}
    if not stats["enabled"]:
        log_warning("Secret encryption backfill skipped — no LEM_SECRET_KEY configured")
        return stats

    connection = _connection.get_db_connection()
    cursor = connection.cursor(dictionary=True)
    try:
        cursor.execute("SELECT id, password, access_token, refresh_token FROM users")
        user_rows = cursor.fetchall() or []
        cursor.execute("SELECT id, user_id, value FROM cookies WHERE user_id IS NOT NULL")
        cookie_rows = cursor.fetchall() or []
        # Rows with no user_id cannot be encrypted (nothing to bind the AAD to) and cannot be read
        # back (get_cookies JOINs users) — they are dead PLAINTEXT credentials. Counting them keeps
        # them out of the operator's blind spot: without this, the gate reports "0 unprotected"
        # while a legacy plaintext li_at is still in every dump. The remedy is deletion, not
        # encryption, so this pass reports them and never touches them.
        cursor.execute(
            "SELECT COUNT(*) AS n FROM cookies WHERE user_id IS NULL AND value IS NOT NULL "
            "AND value <> ''")
        orphan_row = cursor.fetchone() or {}
        stats["orphaned"] = int(orphan_row.get("n") or 0)
        stats["plaintext_remaining"] += stats["orphaned"]
    except mysql.connector.Error as err:
        log_error(f"Could not read secrets for encryption backfill | Error: {err}")
        cursor.close()
        connection.close()
        return stats

    # (UPDATE statement, row id, user id, field name, stored value). The statement is picked from
    # the fixed map above rather than composed from the row — nothing here is ever interpolated.
    targets: list[tuple[str, int, int, str, Optional[str]]] = []
    for row in user_rows:
        for column, field in (("password", SECRET_FIELD_PASSWORD),
                              ("access_token", SECRET_FIELD_ACCESS_TOKEN),
                              ("refresh_token", SECRET_FIELD_REFRESH_TOKEN)):
            targets.append((_SECRET_UPDATE_SQL[field], row["id"], row["id"], field, row[column]))
    for row in cookie_rows:
        targets.append((_SECRET_UPDATE_SQL[SECRET_FIELD_COOKIE_VALUE], row["id"], row["user_id"],
                        SECRET_FIELD_COOKIE_VALUE, row["value"]))

    try:
        for update_sql, row_id, user_id, field, value in targets:
            if not needs_reencrypt(value):
                continue
            stats["scanned"] += 1
            if limit is not None and stats["rewritten"] >= limit:
                stats["plaintext_remaining"] += 1
                continue
            plaintext = decrypt_secret(value, user_id, field)
            if not plaintext:
                stats["failed"] += 1
                stats["plaintext_remaining"] += 1
                continue
            try:
                cursor.execute(update_sql,
                               (encrypt_secret(plaintext, user_id, field), row_id))
                connection.commit()
                stats["rewritten"] += 1
            except mysql.connector.Error as err:
                log_error(f"Could not re-encrypt {field} for row {row_id} | Error: {err}",
                          user_id=user_id)
                stats["failed"] += 1
                stats["plaintext_remaining"] += 1
    finally:
        cursor.close()
        connection.close()

    if stats["orphaned"]:
        log_error(f"{stats['orphaned']} cookie row(s) have no user_id — plaintext sessions that "
                  f"cannot be encrypted or read. Delete them "
                  f"(DELETE FROM cookies WHERE user_id IS NULL) before flipping "
                  f"ENCRYPTION_REQUIRED.")
    log_info(f"Secret encryption backfill: {stats['rewritten']} rewritten, "
             f"{stats['failed']} failed, {stats['orphaned']} orphaned, "
             f"{stats['plaintext_remaining']} still unprotected")
    return stats
def update_user_settings(user_id: int, blog_url: str = None, sitemap_url: str = None) -> bool:
    """Write BOTH `blog_url` and `sitemap_url`, including as NULL when an argument is omitted.

    That is what separates it from `update_user`, which only touches the fields it was given: calling
    this with one URL CLEARS the other.
    """
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                "UPDATE users SET blog_url = %s, sitemap_url = %s WHERE id = %s",
                (blog_url, sitemap_url, user_id)
            )
            success = cursor.rowcount >= 0
    except mysql.connector.Error as err:
        log_error("Could not update user settings", exc=err)
        success = False

    return success
def add_user_by_email(email: str) -> Optional[int]:
    """Create a trial account for an email and return its id, minting the `public_uid` identity up front.

    The trial window is stamped here from `FREE_TRIAL_DAYS`, so the clock starts at signup rather than at
    first login. Stripe customer creation is best-effort — a Stripe outage must not cost us the account
    row — and a duplicate email returns the EXISTING user's id, which makes a replayed signup idempotent
    instead of an error.
    """
    from cqc_lem.utilities.env_constants import FREE_TRIAL_DAYS
    now = datetime.now(timezone.utc)
    trial_ends = now + timedelta(days=FREE_TRIAL_DAYS)
    connection = _connection.get_db_connection()
    cursor = connection.cursor()
    try:
        cursor.execute(
            """INSERT INTO users
               (email, public_uid, subscription_status, subscription_tier, trial_started_at,
                trial_ends_at)
               VALUES (%s, %s, 'trial', 'free_trial', %s, %s)""",
            (email, str(uuid.uuid4()), now, trial_ends),
        )
        connection.commit()
        user_id = cursor.lastrowid
        # Create a Stripe customer in the background (non-fatal if it fails)
        try:
            from cqc_lem.utilities.stripe_util import create_stripe_customer
            stripe_cid = create_stripe_customer(email, user_id)
            if stripe_cid:
                cursor.execute(
                    "UPDATE users SET stripe_customer_id = %s WHERE id = %s",
                    (stripe_cid, user_id),
                )
                connection.commit()
        except Exception as se:
            # WARNING, not INFO: create_stripe_customer swallows its own failures and returns None,
            # so anything reaching here is the stripe_customer_id UPDATE failing — the account is
            # created but unbillable until someone backfills it. Once is a warning; a run of them
            # is the defect.
            log_warning("Stripe customer id was not stored on the new user", exc=se, user_id=user_id)
        return user_id
    except mysql.connector.Error as err:
        if err.errno == errorcode.ER_DUP_ENTRY:
            return get_user_id(email)
        log_error(f"Could not create user for {email}", exc=err)
        return None
    finally:
        cursor.close()
        connection.close()
def get_user_public_uid(user_id: int) -> Optional[str]:
    """The account's public identifier (issue #745, 2b). Lazily minted for a row that predates the
    column and somehow escaped the migration backfill, so callers never have to handle None.
    """
    connection = _connection.get_db_connection()
    cursor = connection.cursor(dictionary=True)
    try:
        cursor.execute("SELECT public_uid FROM users WHERE id = %s", (user_id,))
        row = cursor.fetchone()
        if not row:
            return None
        if row.get('public_uid'):
            return row['public_uid']
        public_uid = str(uuid.uuid4())
        cursor.execute("UPDATE users SET public_uid = %s WHERE id = %s AND public_uid IS NULL",
                       (public_uid, user_id))
        connection.commit()
        return public_uid
    except mysql.connector.Error as err:
        log_error("Could not get public_uid", exc=err, user_id=user_id)
        return None
    finally:
        cursor.close()
        connection.close()
def get_user_id_by_public_uid(public_uid: str) -> Optional[int]:
    """Resolve the public identity (`users.public_uid`) back to the internal row id.

    None when it matches nothing or the read failed.
    """
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute("SELECT id FROM users WHERE public_uid = %s", (public_uid,))
            row = cursor.fetchone()
            return row['id'] if row else None
    except mysql.connector.Error as err:
        log_error("Could not resolve public_uid", exc=err)
        return None
def mark_email_verified(user_id: int) -> bool:
    """Stamp `users.email_verified_at` — the email is an attribute of the account, and this is the
    proof that the current value was actually reached.
    """
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute("UPDATE users SET email_verified_at = %s WHERE id = %s",
                           (datetime.now(timezone.utc), user_id))
            return cursor.rowcount >= 0
    except mysql.connector.Error as err:
        log_error("Could not mark email verified", exc=err, user_id=user_id)
        return False
def change_user_email(user_id: int, new_email: str,
                      changed_by_session_id: Optional[int] = None) -> bool:
    """Point the account at a different email and record the move in `user_email_history`.

    The account identity is `users.id` / `public_uid`, so nothing else has to move. Returns False
    when the new address already belongs to another account — the caller must not merge accounts.
    """
    connection = _connection.get_db_connection()
    cursor = connection.cursor(dictionary=True)
    try:
        now = datetime.now(timezone.utc)
        cursor.execute("SELECT id FROM users WHERE email = %s AND id <> %s", (new_email, user_id))
        if cursor.fetchone():
            log_warning("Email change rejected — address already in use", user_id=user_id)
            return False

        cursor.execute("SELECT email FROM users WHERE id = %s", (user_id,))
        row = cursor.fetchone()
        if not row:
            return False
        old_email = row.get('email')

        cursor.execute(
            "UPDATE users SET email = %s, email_verified_at = %s WHERE id = %s",
            (new_email, now, user_id),
        )
        cursor.execute(
            "INSERT INTO user_email_history (user_id, old_email, new_email, changed_by_session_id) "
            "VALUES (%s, %s, %s, %s)",
            (user_id, old_email, new_email, changed_by_session_id),
        )
        connection.commit()
        return True
    except mysql.connector.Error as err:
        if err.errno == errorcode.ER_DUP_ENTRY:
            log_warning("Email change rejected — address already in use", user_id=user_id)
            return False
        log_error("Could not change email", exc=err, user_id=user_id)
        return False
    finally:
        cursor.close()
        connection.close()
def get_user_email(user_id: int) -> Optional[str]:
    """The account's current email address — an attribute of the account, not its identity (issue #745)."""
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute("SELECT email FROM users WHERE id = %s", (user_id,))
            row = cursor.fetchone()
            return row['email'] if row else None
    except mysql.connector.Error as err:
        log_error("Could not get email", exc=err, user_id=user_id)
        return None
def get_user_analytics_profile(user_id: int) -> dict:
    """The non-sensitive person facts the SPA sets on the PostHog person at $identify (issue #646):
    plan tier/status, timezone and the signup timestamp. `users` has no created_at column, so the
    signup time is trial_started_at falling back to updated_at — the same convention the cohort
    query uses. Never returns credentials; the SPA already knows the email.

    Issue #653 adds the two facts PostHog Surveys TARGET on: when onboarding actually completed (the
    activation "aha", not signup — a user who signed up and stalled has no opinion worth surveying)
    and how many posts the user has ever approved. "Ever approved" is deliberately not
    `status='approved'`: an approved post moves on to scheduled and then posted, so counting the
    current status alone would reset the tally the moment automation ran.
    """
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute(
                "SELECT u.subscription_tier, u.subscription_status, u.timezone, "
                "COALESCE(u.trial_started_at, u.updated_at) AS created_at, "
                "o.activated_at AS onboarding_completed_at, "
                "(SELECT COUNT(*) FROM posts p WHERE p.user_id = u.id "
                " AND p.status IN (%s, %s, %s)) AS posts_approved "
                "FROM users u LEFT JOIN onboarding_state o ON o.user_id = u.id "
                "WHERE u.id = %s",
                (str(PostStatus.APPROVED), str(PostStatus.SCHEDULED), str(PostStatus.POSTED), user_id))
            row = cursor.fetchone()
            return row or {}
    except mysql.connector.Error as err:
        log_error("Could not get analytics profile", exc=err, user_id=user_id)
        return {}
def get_user_token_info(user_id: int) -> Optional[dict]:
    """The LinkedIn OAuth token row with both tokens decrypted, or None.

    Deliberately does NOT filter on expiry the way `get_user_access_token` does: this is the input to the
    expiry decision (`resolve_token_status`), so an expired token has to come back for the SPA countdown
    and the renewal beat to be able to see it at all.
    """
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute(
                """SELECT access_token, access_token_expires_in, access_token_created_at,
                          refresh_token, refresh_token_expires_in, refresh_token_created_at
                   FROM users WHERE id = %s""",
                (user_id,),
            )
            row = cursor.fetchone()
            if row:
                row['access_token'] = decrypt_secret(
                    row.get('access_token'), user_id, SECRET_FIELD_ACCESS_TOKEN)
                row['refresh_token'] = decrypt_secret(
                    row.get('refresh_token'), user_id, SECRET_FIELD_REFRESH_TOKEN)
            return row
    except mysql.connector.Error as err:
        log_error("Could not get token info", exc=err, user_id=user_id)
        return None
def update_user_access_token(
    user_id: int,
    access_token: str,
    # Nullable on purpose: `users.access_token_expires_in` is `INT NULL`, and LinkedIn's refresh
    # response does not always carry `expires_in` — the renewal beat stores what it got either way.
    expires_in: Optional[int],
    refresh_token: Optional[str] = None,
    refresh_token_expires_in: Optional[int] = None,
) -> bool:
    """Store a refreshed LinkedIn access token, sealed, and restamp its created_at.

    The refresh token is only written when one was supplied: LinkedIn does not always return a new one,
    and blanking the stored one would end the renewal chain that is the only way auth outlives LinkedIn's
    60-day cap. False when no row matched.
    """
    now = datetime.now(timezone.utc)
    try:
        with db_cursor(commit=True) as cursor:
            if refresh_token:
                cursor.execute(
                    """UPDATE users SET
                           access_token = %s,
                           access_token_expires_in = %s,
                           access_token_created_at = %s,
                           refresh_token = %s,
                           refresh_token_expires_in = %s,
                           refresh_token_created_at = %s
                       WHERE id = %s""",
                    (encrypt_secret(access_token, user_id, SECRET_FIELD_ACCESS_TOKEN),
                     expires_in, now,
                     encrypt_secret(refresh_token, user_id, SECRET_FIELD_REFRESH_TOKEN),
                     refresh_token_expires_in, now, user_id),
                )
            else:
                cursor.execute(
                    """UPDATE users SET
                           access_token = %s,
                           access_token_expires_in = %s,
                           access_token_created_at = %s
                       WHERE id = %s""",
                    (encrypt_secret(access_token, user_id, SECRET_FIELD_ACCESS_TOKEN),
                     expires_in, now, user_id),
                )
            return cursor.rowcount > 0
    except mysql.connector.Error as err:
        log_error("Could not update access token", exc=err, user_id=user_id)
        return False
def update_user_linkedin_token(
    user_id: int,
    linked_sub_id: str,
    access_token: str,
    expires_in: int,
    refresh_token: Optional[str] = None,
    refresh_token_expires_in: Optional[int] = None,
    linkedin_email: Optional[str] = None,
) -> bool:
    """Write a fresh LinkedIn OAuth token to the user identified by user_id.

    Called from the OAuth callback so the token is always attached to the
    logged-in user, regardless of which email LinkedIn returns.
    """
    now = datetime.now(timezone.utc)
    try:
        with db_cursor(commit=True) as cursor:
            if refresh_token:
                cursor.execute(
                    """UPDATE users SET
                           linked_sub_id = %s,
                           linkedin_email = %s,
                           access_token = %s,
                           access_token_expires_in = %s,
                           access_token_created_at = %s,
                           refresh_token = %s,
                           refresh_token_expires_in = %s,
                           refresh_token_created_at = %s,
                           linkedin_connection_status = 'connected'
                       WHERE id = %s""",
                    (linked_sub_id, linkedin_email or None,
                     encrypt_secret(access_token, user_id, SECRET_FIELD_ACCESS_TOKEN),
                     expires_in, now,
                     encrypt_secret(refresh_token, user_id, SECRET_FIELD_REFRESH_TOKEN),
                     refresh_token_expires_in, now, user_id),
                )
            else:
                cursor.execute(
                    """UPDATE users SET
                           linked_sub_id = %s,
                           linkedin_email = %s,
                           access_token = %s,
                           access_token_expires_in = %s,
                           access_token_created_at = %s,
                           linkedin_connection_status = 'connected'
                       WHERE id = %s""",
                    (linked_sub_id, linkedin_email or None,
                     encrypt_secret(access_token, user_id, SECRET_FIELD_ACCESS_TOKEN),
                     expires_in, now, user_id),
                )
            return cursor.rowcount > 0
    except mysql.connector.Error as err:
        log_error("Could not update LinkedIn token", exc=err, user_id=user_id)
        return False
def update_linkedin_connection_status(user_id: int, status: str) -> bool:
    """Set linkedin_connection_status to 'connected', 'expired', or 'disconnected'."""
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                "UPDATE users SET linkedin_connection_status = %s WHERE id = %s",
                (status, user_id),
            )
            return cursor.rowcount > 0
    except mysql.connector.Error as err:
        log_error("Could not update linkedin_connection_status", exc=err, user_id=user_id)
        return False
def get_user_subscription_info(user_id: int) -> Optional[dict]:
    """Return subscription fields for the given user."""
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute(
                """SELECT subscription_status, subscription_tier,
                          trial_started_at, trial_ends_at,
                          stripe_customer_id, stripe_subscription_id
                   FROM users WHERE id = %s""",
                (user_id,),
            )
            return cursor.fetchone()
    except mysql.connector.Error as err:
        log_error("Could not get subscription info", exc=err, user_id=user_id)
        return None
def update_subscription_from_stripe(
    stripe_customer_id: str,
    status: str,
    tier: Optional[str],
    subscription_id: Optional[str],
    current_period_end: Optional[datetime] = None,
) -> bool:
    """Called from Stripe webhook handler to sync subscription state.

    When tier is None (e.g. subscription deleted) we preserve the existing tier so
    historical data is retained. Pass an explicit empty string to clear it.
    """
    try:
        with db_cursor(commit=True) as cursor:
            if tier is not None:
                cursor.execute(
                    """UPDATE users
                       SET subscription_status = %s,
                           subscription_tier = %s,
                           stripe_subscription_id = %s,
                           subscription_current_period_end = %s
                       WHERE stripe_customer_id = %s""",
                    (status, tier, subscription_id, current_period_end, stripe_customer_id),
                )
            else:
                # Don't overwrite the tier — preserve it for historical reference
                cursor.execute(
                    """UPDATE users
                       SET subscription_status = %s,
                           stripe_subscription_id = %s,
                           subscription_current_period_end = %s
                       WHERE stripe_customer_id = %s""",
                    (status, subscription_id, current_period_end, stripe_customer_id),
                )
            return cursor.rowcount > 0
    except mysql.connector.Error as err:
        log_error(f"Could not update subscription from Stripe for customer {stripe_customer_id}", exc=err)
        return False
def get_users_with_stripe_subscriptions() -> list[dict]:
    """Return all users that have a Stripe subscription ID (for periodic sync)."""
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute(
                """SELECT id, stripe_customer_id, stripe_subscription_id,
                          subscription_status, subscription_tier
                   FROM users
                   WHERE stripe_subscription_id IS NOT NULL
                     AND subscription_status IN ('active', 'past_due')"""
            )
            return cursor.fetchall() or []
    except mysql.connector.Error as err:
        log_error("Could not fetch Stripe subscribers", exc=err)
        return []
def get_user_preferences(user_id: int) -> dict:
    """Return user preference fields with safe defaults.

    Defaults auto_schedule_posts=True so new users' content is automatically
    queued without requiring manual opt-in.
    """
    _defaults: dict = {"last_login_inactivate_delay": None, "auto_schedule_posts": True,
                       "content_buffer_days": DEFAULT_CONTENT_BUFFER_DAYS,
                       "content_buffer_max_posts": DEFAULT_CONTENT_BUFFER_MAX_POSTS,
                       "content_language": None}
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute(
                "SELECT last_login_inactivate_delay, auto_schedule_posts,"
                " content_buffer_days, content_buffer_max_posts, content_language FROM users WHERE id = %s",
                (user_id,),
            )
            row = cursor.fetchone()
            return row if row is not None else _defaults
    except mysql.connector.Error as err:
        log_error("Could not get preferences", exc=err, user_id=user_id)
        return _defaults
def update_user_preferences(
    user_id: int,
    inactivate_delay: Optional[int],
    auto_schedule_posts: bool,
    content_buffer_days: Optional[int] = None,
    content_buffer_max_posts: Optional[int] = None,
    content_language: Optional[str] = None,
) -> bool:
    """Persist user-configurable inactivity delay (None = never) and auto-schedule flag.

    The content-buffer knobs and the content language are left untouched when None so a client
    that doesn't send them (the current Account UI) never resets them. An empty-string
    content_language DOES clear it, returning the user to the Login Location default.
    """
    sets = ["last_login_inactivate_delay = %s", "auto_schedule_posts = %s"]
    params: list = [inactivate_delay, 1 if auto_schedule_posts else 0]
    if content_buffer_days is not None:
        sets.append("content_buffer_days = %s")
        params.append(max(1, min(MAX_CONTENT_BUFFER_DAYS, int(content_buffer_days))))
    if content_buffer_max_posts is not None:
        sets.append("content_buffer_max_posts = %s")
        params.append(max(1, min(MAX_CONTENT_BUFFER_POSTS, int(content_buffer_max_posts))))
    if content_language is not None:
        sets.append("content_language = %s")
        params.append(content_language.strip()[:16] or None)
    params.append(user_id)

    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                f"UPDATE users SET {', '.join(sets)} WHERE id = %s",
                tuple(params),
            )
            # rowcount==0 means the row existed but values were unchanged — still a success
            return cursor.rowcount >= 0
    except mysql.connector.Error as err:
        log_error("Could not update preferences", exc=err, user_id=user_id)
        return False
def engagement_preferences_are_configured(user_id: int) -> Optional[bool]:
    """Whether the user has SAVED an engagement-preferences row of their own.

    The ONE existence check — `has_engagement_preferences` is this function with the unreadable
    case folded back into False, so the question is asked with one query and one semantics.

    Three-valued: None means the row could not be READ, which is NOT the same as "never configured"
    (issue #639). A caller that would otherwise write policy defaults over settings the user chose
    has to be able to tell those two apart (issue #952).
    """
    try:
        with db_cursor() as cursor:
            cursor.execute("SELECT 1 FROM engagement_preferences WHERE user_id = %s LIMIT 1", (user_id,))
            return cursor.fetchone() is not None
    except mysql.connector.Error as err:
        log_error("Could not read engagement prefs — configured state unknown",
                  exc=err, user_id=user_id)
        return None
def get_or_create_reply_inbound_token(user_id: int) -> Optional[str]:
    """The user's PERSISTENT inbound token for the comment-notification forwarding address
    (reply+<token>@parse-domain). Minted once and stored on the users row so the Gmail forward
    filter the user sets up keeps resolving to them. Returns None only on DB error.
    """
    connection = _connection.get_db_connection()
    cursor = connection.cursor()
    try:
        cursor.execute("SELECT reply_inbound_token FROM users WHERE id = %s", (user_id,))
        row = cursor.fetchone()
        if row and row[0]:
            return row[0]
        token = uuid.uuid4().hex[:20]
        cursor.execute("UPDATE users SET reply_inbound_token = %s WHERE id = %s", (token, user_id))
        connection.commit()
        return token
    except mysql.connector.Error as err:
        log_error("Could not get/create reply inbound token", exc=err, user_id=user_id)
        return None
    finally:
        cursor.close()
        connection.close()
def get_user_id_by_reply_token(token: str) -> Optional[int]:
    """Reverse lookup for the comment-notification webhook: token → user_id (unique index)."""
    if not token:
        return None
    try:
        with db_cursor() as cursor:
            cursor.execute("SELECT id FROM users WHERE reply_inbound_token = %s", (token,))
            row = cursor.fetchone()
            return row[0] if row else None
    except mysql.connector.Error as err:
        log_error("Could not look up user by reply token", exc=err)
        return None
def get_users_with_reply_mode(mode: str) -> list:
    """user_ids whose engagement prefs set reply_check_mode = mode (drives the scheduled sweep
    dispatcher). Users with no prefs row default to 'event', so they never appear for 'scheduled'.
    """
    try:
        with db_cursor() as cursor:
            cursor.execute("SELECT user_id FROM engagement_preferences WHERE reply_check_mode = %s", (mode,))
            return [r[0] for r in cursor.fetchall()]
    except mysql.connector.Error as err:
        log_error(f"Could not get users with reply mode {mode}", exc=err)
        return []
def get_user_geo(user_id: int) -> Optional[dict]:
    """Return the user's full geo profile for Selenium spoofing.

    Keys: latitude, longitude (floats or None), timezone, locale, city, country.
    Returns None only if the user row is missing.
    """
    try:
        with db_cursor() as cursor:
            cursor.execute(
                "SELECT latitude, longitude, timezone, locale, city, country FROM users WHERE id = %s",
                (user_id,),
            )
            row = cursor.fetchone()
    except mysql.connector.Error as err:
        log_error("Could not get user geo", exc=err, user_id=user_id)
        row = None
    if not row:
        return None
    return {
        "latitude": float(row[0]) if row[0] is not None else None,
        "longitude": float(row[1]) if row[1] is not None else None,
        "timezone": row[2],
        "locale": row[3],
        "city": row[4],
        "country": row[5],
    }
def get_user_content_language(user_id: Optional[int]) -> str:
    """The BCP-47 language generated content must be produced in (issue #548).

    Precedence: the explicit users.content_language setting → the Login Location locale
    (users.locale) → 'en-US'. The explicit setting wins because location is not language:
    a US-based user may publish in Spanish.
    """
    from cqc_lem.utilities.geocoding import DEFAULT_CONTENT_LANGUAGE
    if not user_id:
        return DEFAULT_CONTENT_LANGUAGE
    # Fail-soft on the connection too: callers sit inside media-generation try/except blocks that
    # degrade to stock footage, so a DB blip here must not cost the user their generated video.
    try:
        connection = _connection.get_db_connection()
    except Exception as err:
        # Distinct from the query failure below so the two stay two problems: this one is the pool
        # refusing a connection at all.
        log_error("Could not connect to read content language", exc=err, user_id=user_id)
        return DEFAULT_CONTENT_LANGUAGE
    cursor = connection.cursor()
    try:
        cursor.execute("SELECT content_language, locale FROM users WHERE id = %s", (user_id,))
        row = cursor.fetchone()
    except mysql.connector.Error as err:
        log_error("Could not get content language", exc=err, user_id=user_id)
        row = None
    finally:
        cursor.close()
        connection.close()
    if not row:
        return DEFAULT_CONTENT_LANGUAGE
    return (row[0] or "").strip() or (row[1] or "").strip() or DEFAULT_CONTENT_LANGUAGE
def update_user_location(user_id: int, latitude: float, longitude: float,
                         city: Optional[str] = None, country: Optional[str] = None,
                         locale: Optional[str] = None, timezone: Optional[str] = None,
                         source: str = "manual") -> bool:
    """Persist the user's location. timezone is updated only when provided so the
    user's display-timezone preference is preserved unless autocapture supplies one.
    """
    try:
        with db_cursor(commit=True) as cursor:
            if timezone:
                cursor.execute(
                    "UPDATE users SET latitude=%s, longitude=%s, city=%s, country=%s, "
                    "locale=%s, timezone=%s, location_source=%s WHERE id=%s",
                    (latitude, longitude, city, country, locale, timezone, source, user_id),
                )
            else:
                cursor.execute(
                    "UPDATE users SET latitude=%s, longitude=%s, city=%s, country=%s, "
                    "locale=%s, location_source=%s WHERE id=%s",
                    (latitude, longitude, city, country, locale, source, user_id),
                )
            return cursor.rowcount >= 0
    except mysql.connector.Error as err:
        log_error("Could not update location", exc=err, user_id=user_id)
        return False
def get_user_proxy(user_id: int) -> Optional[str]:
    """Return the user's egress proxy URL (scheme://[user:pass@]host:port) or None.

    Used by Selenium to route a user's browser session through an IP near where they
    normally log in, reducing LinkedIn "new location" challenges. None = egress from
    the host directly.
    """
    try:
        with db_cursor() as cursor:
            cursor.execute("SELECT proxy_url FROM users WHERE id = %s", (user_id,))
            row = cursor.fetchone()
    except mysql.connector.Error as err:
        log_error("Could not get proxy", exc=err, user_id=user_id)
        row = None
    if not row or not row[0]:
        return None
    return row[0]
def update_user_proxy(user_id: int, proxy_url: Optional[str]) -> bool:
    """Set (or clear, when proxy_url is None/empty) the user's egress proxy URL."""
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                "UPDATE users SET proxy_url = %s WHERE id = %s",
                (proxy_url or None, user_id),
            )
            return cursor.rowcount >= 0
    except mysql.connector.Error as err:
        log_error("Could not update proxy", exc=err, user_id=user_id)
        return False
def get_user_timezone(user_id: int) -> str:
    """Return the IANA timezone string for the user. Defaults to America/New_York to match the
    users.timezone column default and the UI default (not UTC, which would misrender local times).
    """
    try:
        with db_cursor() as cursor:
            cursor.execute("SELECT timezone FROM users WHERE id = %s", (user_id,))
            row = cursor.fetchone()
            return row[0] if row and row[0] else 'America/New_York'
    except mysql.connector.Error as err:
        log_error("Could not get timezone", exc=err, user_id=user_id)
        return 'America/New_York'
def update_user_timezone(user_id: int, tz: str) -> bool:
    """Persist the user's preferred IANA timezone string."""
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute("UPDATE users SET timezone = %s WHERE id = %s", (tz, user_id))
            return cursor.rowcount >= 0
    except mysql.connector.Error as err:
        log_error("Could not update timezone", exc=err, user_id=user_id)
        return False
def get_user_by_stripe_customer_id(stripe_customer_id: str) -> Optional[dict]:
    """Return the user row matching a Stripe customer ID, regardless of subscription status."""
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute(
                "SELECT id, stripe_customer_id FROM users WHERE stripe_customer_id = %s LIMIT 1",
                (stripe_customer_id,),
            )
            return cursor.fetchone()
    except mysql.connector.Error as err:
        log_error(f"Could not look up user by stripe_customer_id={stripe_customer_id}", exc=err)
        return None
def get_avatar_preferences(user_id: int) -> dict:
    """Per-user avatar guardrails (issue #744, decision 4A).

    Every flag defaults OFF and an unreadable row returns the defaults, so a DB blip degrades to
    "don't use the avatar" rather than to publishing a synthetic likeness.
    """
    from cqc_lem.utilities.avatar.guardrails import DEFAULT_AVATAR_PREFERENCES
    prefs = dict(DEFAULT_AVATAR_PREFERENCES)
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute(
                """SELECT avatar_disabled, avatar_use_post_image, avatar_use_carousel,
                          avatar_use_video, avatar_use_newsletter, avatar_caption_overlay
                   FROM users WHERE id = %s""",
                (user_id,),
            )
            row = cursor.fetchone()
            if row:
                prefs = {key: bool(row.get(key)) for key in prefs}
            return prefs
    except mysql.connector.Error as err:
        log_error("Could not fetch avatar preferences", exc=err, user_id=user_id)
        return prefs
def update_avatar_preferences(user_id: int, prefs: dict) -> bool:
    """Update only the avatar guardrail flags the caller actually supplied."""
    from cqc_lem.utilities.avatar.guardrails import DEFAULT_AVATAR_PREFERENCES
    updates = {k: bool(v) for k, v in (prefs or {}).items()
               if k in DEFAULT_AVATAR_PREFERENCES and v is not None}
    if not updates:
        return False

    try:
        with db_cursor(commit=True) as cursor:
            assignments = ", ".join(f"{key} = %s" for key in updates)
            cursor.execute(
                f"UPDATE users SET {assignments} WHERE id = %s",
                (*[int(v) for v in updates.values()], user_id),
            )
            return True
    except mysql.connector.Error as err:
        log_error("Could not update avatar preferences", exc=err, user_id=user_id)
        return False
def get_users_proxy_config(user_ids: list) -> list:
    """(user_id, proxy_url, country) for the given users — the inputs proxy.resolve_proxy() needs
    to decide which egress proxy (and therefore which monthly cost) applies to each user.
    """
    if not user_ids:
        return []

    try:
        with db_cursor(dictionary=True) as cursor:
            placeholders = ", ".join(["%s"] * len(user_ids))
            cursor.execute(
                f"SELECT id, proxy_url, country FROM users WHERE id IN ({placeholders})",
                tuple(int(uid) for uid in user_ids),
            )
            return [
                {"user_id": row["id"], "proxy_url": row.get("proxy_url"), "country": row.get("country")}
                for row in (cursor.fetchall() or [])
            ]
    except mysql.connector.Error as err:
        log_error("Could not fetch proxy config for users", exc=err)
        return []
def get_margin_users() -> list:
    """Users the margin report covers: everyone on an active/past-due subscription or an open trial.
    Trials are included (tier `free_trial`, $0 MRR) so the cost they incur still lands in system
    margin instead of vanishing. `cohort` is the signup month — `users` has no created_at, so
    trial_started_at is the signup timestamp, falling back to updated_at for pre-trial rows.
    """
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute(
                """SELECT id, subscription_tier, subscription_status,
                          DATE_FORMAT(COALESCE(trial_started_at, updated_at), '%Y-%m') AS cohort
                   FROM users
                   WHERE subscription_status IN ('active', 'past_due', 'trial')"""
            )
            return cursor.fetchall() or []
    except mysql.connector.Error as err:
        log_error("Could not fetch margin users", exc=err)
        return []
def get_survey_candidate_user_ids() -> list:
    """Users worth surveying: on an active plan or an unexpired trial. Unlike the onboarding
    candidates this does NOT exclude activated users — activation is exactly what makes someone
    worth asking (the day-3 NPS fires off their activation timestamp).
    """
    try:
        with db_cursor() as cursor:
            cursor.execute("""
                SELECT id FROM users
                WHERE subscription_status = 'active'
                   OR (subscription_status = 'trial'
                       AND (trial_ends_at IS NULL OR trial_ends_at > NOW()))
            """)
            return [row[0] for row in cursor.fetchall()]
    except mysql.connector.Error as err:
        log_error("Could not get survey candidate user ids", exc=err)
        return []
# --- Onboarding / activation checklist (issue #500) ---------------------------------
def ensure_onboarding_state(user_id: int) -> bool:
    """Create the user's onboarding row if it doesn't exist. `started_at` is the trial start when we
    know it, so the nudge clock measures time-since-signup rather than time-since-first-scan.
    """
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                "INSERT IGNORE INTO onboarding_state (user_id, started_at) "
                "SELECT id, COALESCE(trial_started_at, NOW()) FROM users WHERE id = %s", (user_id,))
            return True
    except mysql.connector.Error as err:
        log_error(f"Could not ensure onboarding state for user_id {user_id}", exc=err)
        return False
def get_onboarding_state(user_id: int) -> dict:
    """The persisted checklist row (started_at + one completion timestamp per step). Empty dict when
    the user has no row yet.
    """
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute(
                f"SELECT user_id, started_at, {', '.join(_ONBOARDING_COLS)} "
                f"FROM onboarding_state WHERE user_id = %s", (user_id,))
            return cursor.fetchone() or {}
    except mysql.connector.Error as err:
        log_error(f"Could not get onboarding state for user_id {user_id}", exc=err)
        return {}
def mark_onboarding_step(user_id: int, step: "OnboardingStep") -> bool:
    """Stamp a checklist step as complete. Idempotent: only the FIRST completion writes, and True is
    returned only then — so the caller emits its PostHog event exactly once.
    """
    column = f"{OnboardingStep(step).value}_at"
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                f"UPDATE onboarding_state SET {column} = NOW() "
                f"WHERE user_id = %s AND {column} IS NULL", (user_id,))
            return cursor.rowcount > 0
    except mysql.connector.Error as err:
        log_error(f"Could not mark onboarding step {step} for user_id {user_id}", exc=err)
        return False
def get_onboarding_nudges_sent(user_id: int) -> dict:
    """nudge_key -> sent_at for every nudge already delivered to this user."""
    try:
        with db_cursor() as cursor:
            cursor.execute("SELECT nudge_key, sent_at FROM onboarding_nudges WHERE user_id = %s",
                           (user_id,))
            return {row[0]: row[1] for row in cursor.fetchall()}
    except mysql.connector.Error as err:
        log_error(f"Could not get onboarding nudges for user_id {user_id}", exc=err)
        return {}
def record_onboarding_nudge(user_id: int, nudge_key: str) -> bool:
    """Record that a nudge was sent. Returns False when this nudge was already sent (the PK makes
    each nudge one-shot per user).
    """
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute("INSERT IGNORE INTO onboarding_nudges (user_id, nudge_key) VALUES (%s, %s)",
                           (user_id, str(nudge_key)[:32]))
            return cursor.rowcount > 0
    except mysql.connector.Error as err:
        log_error(f"Could not record onboarding nudge {nudge_key} for user_id {user_id}", exc=err)
        return False
def get_onboarding_candidate_user_ids() -> list:
    """Users still working toward activation: paying or on an unexpired trial, and not yet activated.
    Deliberately NOT get_active_user_ids() — that requires a live LinkedIn connection, which is the
    very step most stalled users are stuck on.
    """
    try:
        with db_cursor() as cursor:
            cursor.execute("""
                SELECT u.id
                FROM users u
                LEFT JOIN onboarding_state o ON o.user_id = u.id
                WHERE (
                        u.subscription_status = 'active'
                        OR (u.subscription_status = 'trial'
                            AND (u.trial_ends_at IS NULL OR u.trial_ends_at > NOW()))
                      )
                  AND o.activated_at IS NULL
            """)
            return [row[0] for row in cursor.fetchall()]
    except mysql.connector.Error as err:
        log_error("Could not get onboarding candidate user ids", exc=err)
        return []


def _profile_url_variants(profile_url: str) -> list:
    """Every spelling of one profile URL worth looking up. Activity rows carry tracking
    querystrings and inconsistent trailing slashes (`/in/jane?trk=feed` vs `/in/jane/`) while
    `profiles` stores whichever form the scraper saw, so an exact match would miss most people —
    same reason get_linked_in_profile_by_url() queries both slash variants.
    """
    raw = str(profile_url or "").strip()
    if not raw:
        return []
    base = raw.split("#", 1)[0].split("?", 1)[0].rstrip("/")
    if not base:
        return [raw]
    return list(dict.fromkeys([raw, base, base + "/"]))
def get_profile_facts(profile_urls: list) -> dict:
    """ICP facts (title / company / industry) for the profiles we HAVE scraped, keyed by the
    profile URL as stored in `profiles` (callers match on the /in/ slug, not the raw string).
    People we never scraped simply aren't in the result — the scorer treats them as neutral.
    """
    urls = list(dict.fromkeys(v for u in (profile_urls or []) if u
                              for v in _profile_url_variants(u)))
    if not urls:
        return {}
    try:
        with db_cursor(dictionary=True) as cursor:
            placeholders = ", ".join(["%s"] * len(urls))
            cursor.execute(
                "SELECT profile_url, "
                "JSON_UNQUOTE(JSON_EXTRACT(data, '$.job_title')) AS job_title, "
                "JSON_UNQUOTE(JSON_EXTRACT(data, '$.company_name')) AS company_name, "
                "JSON_UNQUOTE(JSON_EXTRACT(data, '$.industry')) AS industry "
                f"FROM profiles WHERE profile_url IN ({placeholders})", tuple(urls))
            return {r["profile_url"]: r for r in cursor.fetchall() if r.get("profile_url")}
    except mysql.connector.Error as err:
        log_error("Could not read profile facts", exc=err)
        return {}
def set_user_admin(user_id: int, is_admin: bool) -> bool:
    """Flip `users.is_admin` for one account.

    The GUARDS are the route's job, not this function's — it is the write, and a write that also
    decided policy would be two things to keep in step.

    False when no row changed, so "user 999 does not exist" cannot read as a successful grant.
    """
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute("UPDATE users SET is_admin = %s WHERE id = %s",
                           (1 if is_admin else 0, int(user_id)))
            return cursor.rowcount > 0
    except mysql.connector.Error as err:
        log_error(f"Could not set admin flag for user_id {user_id}", exc=err)
        return False


def set_user_disabled(user_id: int, disabled: bool) -> bool:
    """Flip `users.disabled_at` for one account (issue #1603).

    NULL is "enabled"; a non-NULL timestamp is "disabled", read directly by
    `get_active_user_ids()`. Same shape as `set_user_admin`: the GUARDS (self-disable) are the
    route's job, this is only the write. False when no row changed, so a disable/enable of a
    nonexistent user cannot read as a successful write.
    """
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute("UPDATE users SET disabled_at = %s WHERE id = %s",
                           (datetime.now(timezone.utc) if disabled else None, int(user_id)))
            return cursor.rowcount > 0
    except mysql.connector.Error as err:
        log_error(f"Could not set disabled flag for user_id {user_id}", exc=err)
        return False


def grant_subscription_extension(user_id: int, days: int) -> bool:
    """One-time, time-boxed comp: bump `subscription_current_period_end` by `days` (issue #1603).

    A DIRECT bump applied at grant time, never a standing override — `subscription_status` /
    `subscription_tier` / `subscription_current_period_end` are Stripe-webhook-owned, and the next
    real webhook write is expected and correct after this. Extends from the LATER of "now" and the
    current period end, so granting on top of remaining paid time adds to it rather than
    discarding it; extends from "now" for a lapsed/absent period end rather than a NULL arithmetic
    no-op. No precedence logic and no new column — there is nothing standing here to revert.
    """
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                "UPDATE users SET subscription_current_period_end = "
                "GREATEST(COALESCE(subscription_current_period_end, NOW()), NOW()) "
                "+ INTERVAL %s DAY WHERE id = %s",
                (int(days), int(user_id)))
            return cursor.rowcount > 0
    except mysql.connector.Error as err:
        log_error(f"Could not grant subscription days for user_id {user_id}", exc=err)
        return False


def store_cookies(user_email: str, cookies: list[dict]) -> bool:
    """Persist the browser's cookies for this user. Returns False when any row failed to store.

    The return value is load-bearing since #745: the cookie-migration path DELETES the user's
    stored LinkedIn password once the session is "saved", so a swallowed per-row write error must
    not read as success — that would take away the only login they had left.
    """
    connection = _connection.get_db_connection()
    cursor = connection.cursor()

    user_id = get_user_id(user_email)

    try:
        failed = _store_cookie_rows(cursor, cookies, user_id)
        connection.commit()
    finally:
        cursor.close()
        connection.close()

    if user_id is not None:
        prune_superseded_cookies(user_id)

    return not failed
def store_linkedin_li_at(user_id: int, li_at: str, jsessionid: Optional[str] = None) -> bool:
    """Persist a user-supplied LinkedIn session cookie (li_at, optionally JSESSIONID).

    Lets login_to_linkedin resume an already-trusted session instead of doing a fresh
    password login — which is what triggers LinkedIn's new-device challenge. Reuses the
    standard cookie store so the existing cookie-first login path picks it up.
    """
    email = get_user_email(user_id)
    if not email:
        log_info(f"store_linkedin_li_at: no email for user_id {user_id}")
        return False

    import time
    expiry = int(time.time()) + 365 * 24 * 60 * 60  # ~1 year; load_cookies re-stamps anyway
    cookies = [{
        "name": "li_at", "value": li_at, "domain": ".linkedin.com", "path": "/",
        "expiry": expiry, "secure": True, "httpOnly": True,
    }]
    if jsessionid:
        cookies.append({
            "name": "JSESSIONID", "value": jsessionid, "domain": ".linkedin.com",
            "path": "/", "expiry": expiry, "secure": True, "httpOnly": False,
        })
    try:
        # Must reflect the actual write (issue #745): the caller drops the user's stored LinkedIn
        # password on a True, and per-row insert errors are swallowed inside _store_cookie_rows.
        if not store_cookies(email, cookies):
            log_error("Could not store LinkedIn session cookie — no row was written",
                      user_id=user_id)
            return False
        return True
    except Exception as e:
        # Same failure as the no-row branch above, which is already ERROR: the caller drops the
        # user's stored password on a True, so a swallowed write here costs them the session.
        log_error("Could not store LinkedIn session cookie", exc=e, user_id=user_id)
        return False
def set_default_video_quality(user_id: int, quality: str) -> bool:
    """Set the user's default video quality preference (upserts the engagement_preferences row).
    Invalid values are coerced to 'standard'.
    """
    if quality not in VALID_VIDEO_QUALITIES:
        quality = "standard"
    return update_engagement_preferences(user_id, {"default_video_quality": quality})
def get_active_user_password_pairs():
    """`[email, password]` for every active user that has BOTH.

    A user missing either half is skipped silently — there is nothing a browser login could do with half
    a credential.
    """
    user_password_pairs = []

    active_users = get_active_user_ids()

    for user_id in active_users:
        email, password = get_user_password_pair_by_id(user_id)
        if email and password:
            user_password_pairs.append([email, password])

    return user_password_pairs
# Catch-up milestone types eligible for a congratulations touch out of the box (issue #482): the two
# real trigger events. All six types are user-configurable; birthdays/anniversaries are opt-in because
# congratulating those at volume reads as spam.
DEFAULT_CATCHUP_EVENT_TYPES = ("job_change", "promotion")
VALID_CATCHUP_TOUCH_MODES = ("pre_review", "auto_approve")
# Where the congratulations text comes from. 'linkedin' = LinkedIn's own pre-drafted response for the
# moment (no LLM); 'ai' = the DM-template + voice-refinement path, for users who want more customization.
VALID_CATCHUP_MESSAGE_SOURCES = ("linkedin", "ai")
# Per-day cap bounds. 5/day is the ceiling on every plan; raising it to 10/day is a premium feature
# (owner review on PR #509: "3A, but use 3B as a premium subscribed user feature").
CATCHUP_TOUCHES_MIN = 0
CATCHUP_TOUCHES_MAX_STANDARD = 5
CATCHUP_TOUCHES_MAX_PREMIUM = 10
# Absolute ceiling accepted at the API boundary — the per-user allowance is applied on top of it.
CATCHUP_TOUCHES_MAX = CATCHUP_TOUCHES_MAX_PREMIUM
# Per-contact cooldown across ALL catch-up event types (issue #1078). A new congratulations to the
# same person is held until at least this many days have passed since the last one.
CATCHUP_MIN_CONTACT_INTERVAL_DAYS_DEFAULT = 7
CATCHUP_MIN_CONTACT_INTERVAL_DAYS_MIN = 0
CATCHUP_MIN_CONTACT_INTERVAL_DAYS_MAX = 365
# Per-contact rolling cap (issue #1078). At most this many catch-up messages may reach the same
# person within CATCHUP_CONTACT_CAP_WINDOW_DAYS. 0 means no cap.
#
# The cap window is deliberately NOT the cooldown window: the cooldown already blocks every send
# inside its own window, so a cap measured over the same span could never be reached (the first
# message would trip the cooldown long before the second reached the cap), and disabling the
# cooldown would silently disable the cap too. A month-long window makes the cap the second,
# independent bound the reporter asked for — "no more than N catch-ups to this person, ever, in a
# rolling month" — regardless of how the cooldown is set.
CATCHUP_MAX_PER_CONTACT_DAYS_DEFAULT = 2
CATCHUP_MAX_PER_CONTACT_DAYS_MIN = 0
CATCHUP_MAX_PER_CONTACT_DAYS_MAX = 365
# The rolling window the per-contact cap is measured over. Fixed, not a preference: the cap and the
# cooldown are two different questions, and one knob answering both is how the cap became unreachable.
CATCHUP_CONTACT_CAP_WINDOW_DAYS = 30
# Paid plans that unlock the premium catch-up allowance (see stripe_util.TIER_PRICE_MAP).
PREMIUM_SUBSCRIPTION_TIERS = ("professional", "enterprise")
ACTIVE_SUBSCRIPTION_STATUSES = ("active", "trial")
def is_premium_subscriber(user_id: int) -> bool:
    """True when the user is on a currently-active professional/enterprise plan. Anything else —
    free trial, starter, lapsed, unknown, or a DB error — is treated as NOT premium, so a premium-only
    allowance can never be granted by accident.
    """
    try:
        info = get_user_subscription_info(user_id)
    except Exception:
        return False
    if not info:
        return False
    return (str(info.get("subscription_tier") or "") in PREMIUM_SUBSCRIPTION_TIERS
            and str(info.get("subscription_status") or "") in ACTIVE_SUBSCRIPTION_STATUSES)
def max_catchup_touches_allowed(user_id: int) -> int:
    """The highest catch-up cap this user may set — 10/day on premium plans, 5/day otherwise."""
    return CATCHUP_TOUCHES_MAX_PREMIUM if is_premium_subscriber(user_id) else CATCHUP_TOUCHES_MAX_STANDARD
# Publishing cadence (issue #621 / G6). 2-4 high-effort posts a week beat daily volume in the 2026
# regime — van der Blom's 1.3M-post sample puts daily posting at roughly -26% average reach per
# post — so the default drops from one-a-day to 3/week. 7 (daily) stays reachable for users who
# insist on it, which is why the ceiling is a full week rather than 5; the SPA warns above 4.
POSTS_PER_WEEK_MIN, POSTS_PER_WEEK_MAX = 2, 7
DEFAULT_POSTS_PER_WEEK = 3
# WHICH weekdays those slots may land on (issue #581). Mon=0 … Sun=6, default Mon-Fri: weekends are
# opt-in rather than the automatic consequence of raising the cadence to 6-7/week. All seven days
# stay selectable — this is an allow-list, never a hardcoded work week. `posts_per_week` still
# decides how many of the allowed days are actually filled.
DEFAULT_POSTING_DAYS = [0, 1, 2, 3, 4]
POSTING_DAY_MIN, POSTING_DAY_MAX = 0, 6
def normalize_posting_days(value) -> list:
    """A de-duped, sorted list of valid weekday ints — or the Mon-Fri default when the input holds
    nothing usable. Never returns an empty set: an empty cadence would schedule no content at all,
    and a bad value must not be persisted into the one-row prefs upsert (the V52 lesson).
    """
    days = []
    for raw in _coerce_json_list(value):
        try:
            day = int(raw)
        except (TypeError, ValueError):
            continue
        if POSTING_DAY_MIN <= day <= POSTING_DAY_MAX and day not in days:
            days.append(day)
    return sorted(days) if days else list(DEFAULT_POSTING_DAYS)
# Company-page invites per day (issue #732). LinkedIn Pages spend a MONTHLY credit pool that renews
# on the 1st and is refunded when an invite is accepted; LinkedIn is currently cutting the free-Page
# allowance from 250 to 50/month, so a drip has to survive both sizes. 5/day is the conservative
# ceiling: at 50 credits the credits/days-left spread binds first (~2/day), at 250 this binds, and
# the #626 budget draw (40-100% of cap) keeps the realised average lower still. 0 turns the lane off.
COMPANY_PAGE_INVITES_PER_DAY_DEFAULT = 5
COMPANY_PAGE_INVITES_PER_DAY_MIN, COMPANY_PAGE_INVITES_PER_DAY_MAX = 0, 50
# Roster auto-follows per day (issue #962). Far smaller than any other lane on purpose: a follow is
# the cheapest action to automate and the easiest to over-run, and LinkedIn's own follow limits are
# what a bulk-follower trips first. 3/day is a catch-up rate — a 50-account roster reaches full
# coverage in a few weeks — and the #626 budget draw (40-100% of cap, plus rest days) keeps the
# realised average below it. 0 turns the lane off without touching the toggle.
ROSTER_FOLLOWS_PER_DAY_DEFAULT = 3
ROSTER_FOLLOWS_PER_DAY_MIN, ROSTER_FOLLOWS_PER_DAY_MAX = 0, 20
_ENGAGEMENT_DEFAULTS: dict = {
    # Default to MEDIUM (issue #394): 2026 LinkedIn weights substantive ≥15-word comments ~2.5× short
    # one-liners, so the out-of-the-box length produces a real, specific reply rather than a throwaway.
    "tone": None, "comment_length": "medium", "comment_style": None,
    # use_hashtags stays OFF by default (issue #393): hashtags no longer expand reach in 2026 and
    # hashtag-free posts out-perform tagged ones. See content_framework.hashtag_directive.
    "use_emojis": True, "use_hashtags": False,
    "include_topics": [], "exclude_topics": [], "include_keywords": [], "exclude_keywords": [],
    "include_authors": [], "exclude_authors": [], "post_types": [],
    "focus_topics": [], "business_goals": None, "personal_goals": None,
    # Quality-gate thresholds (issue #421). None = follow the deploy default
    # (AUTHENTICITY_SCORE_MIN / POST_SIMILARITY_MAX), so the gates behave exactly as before until
    # the user tunes them.
    "authenticity_score_min": None, "post_similarity_max_pct": None,
    "min_reactions": None, "max_post_age_hours": 24, "reply_to_own_comments": True,
    "max_comments_per_day": 20, "max_dms_per_day": 20, "max_invites_per_day": 10,
    # Company-page invites (issue #732) run on their OWN small cap, and the effective ceiling is
    # min(this, max_invites_per_day) — see COMPANY_PAGE_INVITES_PER_DAY_DEFAULT for why 5.
    "max_company_page_invites_per_day": COMPANY_PAGE_INVITES_PER_DAY_DEFAULT,
    "connection_request_mode": "auto_approve",
    # Smart connection targeting (issue #486). 'suggest' sources candidates but always files them as
    # drafts, so enabling targeting can never send outbound on its own.
    "connection_targeting_mode": "suggest", "connection_target_authors": [],
    "min_connection_icp_score": 55,
    "default_buyer_stage": None,
    "default_video_quality": "standard",
    "reply_check_mode": "event", "reply_sweeps_per_day": 2, "reply_max_post_age_days": 2,
    # feed_fallback_when_empty's FLEET default is runtime-controlled by the
    # `feed-fallback-when-empty-default` flag (issue #651) via _code_engagement_defaults(); the
    # value here is what that flag falls back to. A saved row always wins over both.
    "feed_fallback_when_empty": True, "link_in_first_comment": True,
    # Catch-up congratulations (issue #482): small cap, human approval, and only the BD-relevant
    # milestone types out of the box — a generic "Congrats!" at volume is worse than nothing.
    # The message itself defaults to LinkedIn's own pre-drafted response (no LLM).
    "max_catchup_touches_per_day": CATCHUP_TOUCHES_MAX_STANDARD, "catchup_touch_mode": "pre_review",
    "catchup_event_types": list(DEFAULT_CATCHUP_EVENT_TYPES),
    "catchup_message_source": "linkedin",
    # Per-contact catch-up frequency guard (issue #1078). A new congratulations to the same person is
    # held until `min_catchup_contact_interval_days` have passed since the last one, and at most
    # `max_catchup_touches_per_contact_days` may land per rolling CATCHUP_CONTACT_CAP_WINDOW_DAYS.
    # Both default to small, safe values that rarely block normal usage but stop a burst across
    # multiple milestone types.
    "min_catchup_contact_interval_days": CATCHUP_MIN_CONTACT_INTERVAL_DAYS_DEFAULT,
    "max_catchup_touches_per_contact_days": CATCHUP_MAX_PER_CONTACT_DAYS_DEFAULT,
    "posts_per_week": DEFAULT_POSTS_PER_WEEK,
    "posting_days": list(DEFAULT_POSTING_DAYS),
    # AI image on generated TEXT posts (image-generation overhaul). ON by default — a bare text
    # post is the lowest-reach format; the review queue is still the human gate on every image.
    "text_post_images": True,
    # Opt-in auto-follow of roster targets (issue #962). OFF by default and small when on: bulk
    # following is a classic bot signature, so this only ever runs because the user asked for it.
    "roster_auto_follow": False,
    "max_follows_per_day": ROSTER_FOLLOWS_PER_DAY_DEFAULT,
    # Opt-in auto-connect for roster targets following did not unlock (issue #979). OFF by default
    # and independent of the follow toggle: an invite is heavier and less reversible than a follow,
    # and it spends the account's ONE combined invite budget.
    "roster_auto_connect": False,
    # Hold a post the review gate had to REPAIR (issue #1134). ON by default: a draft that failed a
    # deterministic check and passed only after the editor pass is precisely the post nobody has
    # read. Off restores the pre-#1134 behaviour — auto_schedule_posts alone decides.
    "hold_repaired_posts_for_review": True,
    # Direct dispatch for cold profile-viewer outreach (issue #1137). OFF by default: with it off
    # both branches of engage_with_profile_viewer file an approval-gated row instead of sending,
    # which is the only lane where a stranger hears from us with nobody having looked first.
    "profile_viewer_dm_auto_send": False,
}
_ENGAGEMENT_JSON_FIELDS = ("include_topics", "exclude_topics", "include_keywords",
                           "exclude_keywords", "include_authors", "exclude_authors", "post_types",
                           "focus_topics", "connection_target_authors", "catchup_event_types",
                           "posting_days")
_ENGAGEMENT_BOOL_FIELDS = ("use_emojis", "use_hashtags", "reply_to_own_comments",
                           "feed_fallback_when_empty", "link_in_first_comment",
                           "text_post_images", "roster_auto_follow", "roster_auto_connect",
                           "hold_repaired_posts_for_review",
                           "profile_viewer_dm_auto_send")
_ENGAGEMENT_COLS = ("tone", "comment_length", "comment_style", "use_emojis", "use_hashtags",
                    "include_topics", "exclude_topics", "include_keywords", "exclude_keywords",
                    "include_authors", "exclude_authors", "post_types", "focus_topics",
                    "business_goals", "personal_goals",
                    "authenticity_score_min", "post_similarity_max_pct", "min_reactions",
                    "max_post_age_hours", "reply_to_own_comments", "max_comments_per_day",
                    "max_dms_per_day", "max_invites_per_day",
                    "max_company_page_invites_per_day", "connection_request_mode",
                    "connection_targeting_mode", "connection_target_authors",
                    "min_connection_icp_score",
                    "default_buyer_stage", "default_video_quality",
                    "reply_check_mode", "reply_sweeps_per_day", "reply_max_post_age_days",
                    "feed_fallback_when_empty", "link_in_first_comment",
                    "max_catchup_touches_per_day", "catchup_touch_mode", "catchup_event_types",
                    "catchup_message_source", "min_catchup_contact_interval_days",
                    "max_catchup_touches_per_contact_days", "posts_per_week", "posting_days",
                    "text_post_images", "roster_auto_follow", "max_follows_per_day",
                    "roster_auto_connect", "hold_repaired_posts_for_review",
                    "profile_viewer_dm_auto_send")
VALID_REPLY_MODES = ("event", "scheduled", "off")
# Approval posture for the proactive connect flow (issue #398 owner review).
VALID_CONNECTION_REQUEST_MODES = ("auto_approve", "pre_review")
# Sourcing posture for smart connection targeting (issue #486): 'off' = no sourcing, 'suggest' =
# source but always file as drafts, 'auto_queue' = defer to connection_request_mode.
VALID_CONNECTION_TARGETING_MODES = ("off", "suggest", "auto_queue")
ICP_SCORE_MIN, ICP_SCORE_MAX = 0, 100
# Scheduled reply-sweep cadence bounds: floor 2×/day (as requested), cap 12×/day (every ~2h).
REPLY_SWEEPS_MIN, REPLY_SWEEPS_MAX = 2, 12
REPLY_MAX_AGE_DAYS_MIN, REPLY_MAX_AGE_DAYS_MAX = 1, 14
def _coerce_json_list(value) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, list) else []
    except (ValueError, TypeError):
        return []
def _select_engagement_row(user_id: int) -> Optional[dict]:
    """The user's SAVED engagement row, decoded — or None when they have never saved one.

    Deliberately lets `mysql.connector.Error` escape: a read failure is not the same as a missing
    row, and `update_engagement_preferences` must be able to tell them apart before it rewrites
    every column (issue #639).
    """
    connection = _connection.get_db_connection()
    cursor = connection.cursor(dictionary=True)
    try:
        cursor.execute(
            f"SELECT {', '.join(_ENGAGEMENT_COLS)} FROM engagement_preferences WHERE user_id = %s",
            (user_id,))
        row = cursor.fetchone()
        if row is None:
            return None
        # A NULL catchup_event_types (every row predating the V20260724211808 migration) means
        # "never configured" -> the default BD subset. An explicit empty list means the user turned
        # catch-up touches off, so only coerce the NULL case.
        if row.get("catchup_event_types") is None:
            row["catchup_event_types"] = list(DEFAULT_CATCHUP_EVENT_TYPES)
        if row.get("catchup_message_source") not in VALID_CATCHUP_MESSAGE_SOURCES:
            row["catchup_message_source"] = _ENGAGEMENT_DEFAULTS["catchup_message_source"]
        # A NULL cadence (a row written before the posts_per_week migration) means "never chosen",
        # so the planner gets the 3/week default rather than a falsy value it would read as zero.
        if row.get("posts_per_week") is None:
            row["posts_per_week"] = DEFAULT_POSTS_PER_WEEK
        # A NULL company-page invite cap (any row predating the V20260727175938 migration) means
        # "never chosen" -> the conservative default. Reading NULL as 0 would silently switch the
        # lane off for every existing user; an explicit 0 IS "off" and is preserved.
        if row.get("max_company_page_invites_per_day") is None:
            row["max_company_page_invites_per_day"] = COMPANY_PAGE_INVITES_PER_DAY_DEFAULT
        # Same reading for the follow cap (issue #962): NULL is "never chosen" -> the conservative
        # code default. An explicit 0 is the user switching the lane off and is preserved. The
        # TOGGLE is not read this way — it is NOT NULL DEFAULT 0, because "off" and "never chosen"
        # must behave identically for a feature that did not exist yesterday.
        if row.get("max_follows_per_day") is None:
            row["max_follows_per_day"] = ROSTER_FOLLOWS_PER_DAY_DEFAULT
        for f in _ENGAGEMENT_JSON_FIELDS:
            row[f] = _coerce_json_list(row.get(f))
        # A NULL/empty posting_days (any row predating the V20260727045811 migration) means "never
        # chosen" -> Mon-Fri. Unlike catchup_event_types, an empty set here is NOT a meaningful
        # choice: it would leave the planner with no day to publish on at all.
        row["posting_days"] = normalize_posting_days(row.get("posting_days"))
        for f in _ENGAGEMENT_BOOL_FIELDS:
            row[f] = bool(row.get(f))
        return row
    finally:
        cursor.close()
        connection.close()
def _code_engagement_defaults(user_id: int) -> dict:
    """`_ENGAGEMENT_DEFAULTS` with the one field whose FLEET default is runtime-controlled resolved
    for this user (issue #651). Only reached when the user has no saved row: once they save one, the
    column holds their own explicit 0/1 and the flag can never override it.
    """
    from cqc_lem.utilities.flags import FEED_FALLBACK_DEFAULT, flag_enabled
    defaults = dict(_ENGAGEMENT_DEFAULTS)
    defaults["feed_fallback_when_empty"] = flag_enabled(FEED_FALLBACK_DEFAULT, user_id=user_id)
    return defaults
def get_engagement_preferences(user_id: int) -> dict:
    """Return the user's engagement preferences (voice/targeting/caps) with code-level
    defaults when no row exists — so behaviour is unchanged until the user customizes.
    """
    try:
        row = _select_engagement_row(user_id)
    except mysql.connector.Error as err:
        log_error("Could not get engagement prefs", exc=err, user_id=user_id)
        return _code_engagement_defaults(user_id)
    return _code_engagement_defaults(user_id) if row is None else row
def update_engagement_preferences(user_id: int, prefs: dict) -> bool:
    """Upsert the user's engagement preferences (INSERT ... ON DUPLICATE KEY UPDATE)."""
    # The upsert writes EVERY column, so a partial `prefs` dict must merge over the user's own
    # SAVED row — merging over `_ENGAGEMENT_DEFAULTS` reset tone/targeting/caps/goals for anyone
    # calling with a single key (issue #639, e.g. set_default_video_quality). Code defaults are
    # the base only for a genuinely new row. An UNREADABLE row aborts the write: overwriting all
    # 39 columns with defaults because a SELECT failed is exactly the data loss being fixed.
    try:
        existing = _select_engagement_row(user_id)
    except mysql.connector.Error as err:
        # ERROR, not myprint: this silently ABORTS the user's save, so it has to reach PostHog
        # rather than sit at INFO under the default POSTHOG_LOG_LEVEL.
        log_error("Could not read engagement prefs before update — aborting write",
                  exc=err, user_id=user_id)
        return False
    base = {**_code_engagement_defaults(user_id),
            **{k: v for k, v in (existing or {}).items() if k in _ENGAGEMENT_DEFAULTS}}
    merged = {**base, **{k: v for k, v in prefs.items() if k in _ENGAGEMENT_DEFAULTS}}

    # Clamp/validate reply-check config so a bad value can't overflow a column and roll back the
    # WHOLE single-row upsert (the V52 tone incident). Bad mode → the safe default; out-of-range
    # numbers → clamped to bounds.
    if merged.get("reply_check_mode") not in VALID_REPLY_MODES:
        merged["reply_check_mode"] = "event"
    if merged.get("connection_request_mode") not in VALID_CONNECTION_REQUEST_MODES:
        merged["connection_request_mode"] = "auto_approve"
    if merged.get("connection_targeting_mode") not in VALID_CONNECTION_TARGETING_MODES:
        merged["connection_targeting_mode"] = "suggest"
    _icp = merged.get("min_connection_icp_score")
    try:
        merged["min_connection_icp_score"] = (min(ICP_SCORE_MAX, max(ICP_SCORE_MIN, int(_icp)))
                                              if _icp is not None else 55)
    except (TypeError, ValueError):
        merged["min_connection_icp_score"] = 55
    # Clamp numerics WITHOUT `or` fallbacks — 0 is falsy but is a real (out-of-range) value that must
    # clamp to the floor, not silently become the default (matches the API-layer validators).
    _sw = merged.get("reply_sweeps_per_day")
    try:
        merged["reply_sweeps_per_day"] = (min(REPLY_SWEEPS_MAX, max(REPLY_SWEEPS_MIN, int(_sw)))
                                          if _sw is not None else REPLY_SWEEPS_MIN)
    except (TypeError, ValueError):
        merged["reply_sweeps_per_day"] = REPLY_SWEEPS_MIN
    _age = merged.get("reply_max_post_age_days")
    try:
        merged["reply_max_post_age_days"] = (min(REPLY_MAX_AGE_DAYS_MAX, max(REPLY_MAX_AGE_DAYS_MIN, int(_age)))
                                             if _age is not None else 2)
    except (TypeError, ValueError):
        merged["reply_max_post_age_days"] = 2
    _ppw = merged.get("posts_per_week")
    try:
        merged["posts_per_week"] = (min(POSTS_PER_WEEK_MAX, max(POSTS_PER_WEEK_MIN, int(_ppw)))
                                    if _ppw is not None else DEFAULT_POSTS_PER_WEEK)
    except (TypeError, ValueError):
        merged["posts_per_week"] = DEFAULT_POSTS_PER_WEEK
    _cpi = merged.get("max_company_page_invites_per_day")
    try:
        merged["max_company_page_invites_per_day"] = (
            min(COMPANY_PAGE_INVITES_PER_DAY_MAX, max(COMPANY_PAGE_INVITES_PER_DAY_MIN, int(_cpi)))
            if _cpi is not None else COMPANY_PAGE_INVITES_PER_DAY_DEFAULT)
    except (TypeError, ValueError):
        merged["max_company_page_invites_per_day"] = COMPANY_PAGE_INVITES_PER_DAY_DEFAULT
    _fol = merged.get("max_follows_per_day")
    try:
        merged["max_follows_per_day"] = (
            min(ROSTER_FOLLOWS_PER_DAY_MAX, max(ROSTER_FOLLOWS_PER_DAY_MIN, int(_fol)))
            if _fol is not None else ROSTER_FOLLOWS_PER_DAY_DEFAULT)
    except (TypeError, ValueError):
        merged["max_follows_per_day"] = ROSTER_FOLLOWS_PER_DAY_DEFAULT
    # The publishing day allow-list (issue #581): de-duped, sorted, Mon..Sun only. Anything
    # unusable — an empty set, strings, out-of-range ints — falls back to Mon-Fri rather than
    # persisting a cadence that would schedule nothing or a value the column would reject.
    merged["posting_days"] = normalize_posting_days(merged.get("posting_days"))
    # Quality-gate thresholds (issue #421): None means "use the deploy default", anything else is
    # clamped to its valid band so an out-of-range slider can never make a gate un-passable.
    from cqc_lem.utilities.quality_gates import (
        AUTHENTICITY_SCORE_MIN_BOUNDS,
        SIMILARITY_MAX_PCT_BOUNDS,
        clamp_threshold,
    )
    merged["authenticity_score_min"] = clamp_threshold(
        merged.get("authenticity_score_min"), *AUTHENTICITY_SCORE_MIN_BOUNDS)
    merged["post_similarity_max_pct"] = clamp_threshold(
        merged.get("post_similarity_max_pct"), *SIMILARITY_MAX_PCT_BOUNDS)
    if merged.get("catchup_touch_mode") not in VALID_CATCHUP_TOUCH_MODES:
        merged["catchup_touch_mode"] = "pre_review"
    if merged.get("catchup_message_source") not in VALID_CATCHUP_MESSAGE_SOURCES:
        merged["catchup_message_source"] = "linkedin"
    # The cap ceiling is per-plan: 10/day only on an active premium plan, 5/day otherwise. Clamped
    # here (not just at the API boundary) so a downgrade silently pulls the saved cap back down.
    _cap_max = max_catchup_touches_allowed(user_id)
    _ct = merged.get("max_catchup_touches_per_day")
    try:
        merged["max_catchup_touches_per_day"] = (
            min(_cap_max, max(CATCHUP_TOUCHES_MIN, int(_ct))) if _ct is not None
            else min(_cap_max, _ENGAGEMENT_DEFAULTS["max_catchup_touches_per_day"]))
    except (TypeError, ValueError):
        merged["max_catchup_touches_per_day"] = min(
            _cap_max, _ENGAGEMENT_DEFAULTS["max_catchup_touches_per_day"])
    # Drop unknown milestone types before they hit the ENUM-validated ledger.
    merged["catchup_event_types"] = [t for t in (merged.get("catchup_event_types") or [])
                                     if t in tuple(CatchupEventType)]
    # Per-contact catch-up frequency guard (issue #1078). 0 disables the guard; otherwise clamp to
    # a sensible band so a malformed value can't lock the lane for a year or make it negative.
    _interval = merged.get("min_catchup_contact_interval_days")
    try:
        merged["min_catchup_contact_interval_days"] = (
            min(CATCHUP_MIN_CONTACT_INTERVAL_DAYS_MAX,
                max(CATCHUP_MIN_CONTACT_INTERVAL_DAYS_MIN, int(_interval)))
            if _interval is not None else CATCHUP_MIN_CONTACT_INTERVAL_DAYS_DEFAULT)
    except (TypeError, ValueError):
        merged["min_catchup_contact_interval_days"] = CATCHUP_MIN_CONTACT_INTERVAL_DAYS_DEFAULT
    _per_contact = merged.get("max_catchup_touches_per_contact_days")
    try:
        merged["max_catchup_touches_per_contact_days"] = (
            min(CATCHUP_MAX_PER_CONTACT_DAYS_MAX,
                max(CATCHUP_MAX_PER_CONTACT_DAYS_MIN, int(_per_contact)))
            if _per_contact is not None else CATCHUP_MAX_PER_CONTACT_DAYS_DEFAULT)
    except (TypeError, ValueError):
        merged["max_catchup_touches_per_contact_days"] = CATCHUP_MAX_PER_CONTACT_DAYS_DEFAULT

    def _val(col):
        v = merged[col]
        if col in _ENGAGEMENT_JSON_FIELDS:
            return json.dumps(v or [])
        if col in _ENGAGEMENT_BOOL_FIELDS:
            return 1 if v else 0
        return v

    values = [user_id] + [_val(c) for c in _ENGAGEMENT_COLS]
    placeholders = ", ".join(["%s"] * (len(_ENGAGEMENT_COLS) + 1))
    updates = ", ".join(f"{c}=VALUES({c})" for c in _ENGAGEMENT_COLS)
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                f"INSERT INTO engagement_preferences (user_id, {', '.join(_ENGAGEMENT_COLS)}) "
                f"VALUES ({placeholders}) ON DUPLICATE KEY UPDATE {updates}", values)
            return cursor.rowcount >= 0
    except mysql.connector.Error as err:
        log_error("Could not update engagement prefs", exc=err, user_id=user_id)
        return False
def admin_email_allowlist() -> set:
    """Emails from ADMIN_USER_EMAILS, lowercased (issue #793). Empty adds nobody."""
    from cqc_lem.utilities.env_constants import ADMIN_USER_EMAILS
    return {e.strip().lower() for e in (ADMIN_USER_EMAILS or "").split(",") if e.strip()}
def is_user_admin(user_id: int) -> bool:
    """Whether this user is designated as an admin (issue #793).

    Admin is the users.is_admin column OR a match in the ADMIN_USER_EMAILS allowlist — the latter
    exists so a deploy with no flagged user yet can still reach the triage panel and release the
    feedback the auto-filer is now parking.

    Fails CLOSED — a missing user or DB error is never interpreted as admin rights.
    """
    if user_id is None:
        return False
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute("SELECT is_admin, email FROM users WHERE id = %s", (int(user_id),))
            row = cursor.fetchone()
            if not row:
                return False
            if row.get("is_admin"):
                return True
            return (row.get("email") or "").strip().lower() in admin_email_allowlist()
    except mysql.connector.Error as err:
        log_error(f"Could not check admin status for user_id {user_id}", exc=err)
        return False
# Explicit field lists, never `SELECT *`. Every credential-bearing column is excluded BY NOT BEING
# NAMED: password/access_token/refresh_token (encrypted at rest), proxy_url (embeds user:pass),
# reply_inbound_token (a bearer secret in an email address), the Stripe ids, and latitude/longitude
# (someone's home, at 7 decimal places — city/country answers every question this screen has).
_ADMIN_USER_LIST_FIELDS = (
    "u.id, u.email, u.linkedin_email, u.is_admin, u.subscription_status, u.subscription_tier, "
    "u.trial_ends_at, u.linkedin_connection_status, u.last_login, u.disabled_at, "
    "o.started_at AS signed_up_at, o.activated_at"
)
_ADMIN_USER_DETAIL_FIELDS = (
    _ADMIN_USER_LIST_FIELDS + ", "
    "u.public_uid, u.linkedin_display_name, u.email_verified_at, u.trial_started_at, "
    "u.subscription_current_period_end, u.timezone, u.city, u.country, u.locale, "
    "u.content_language, u.location_source, u.blog_url, u.sitemap_url, u.company_linked_in_url, "
    "u.auto_schedule_posts, u.content_buffer_days, u.content_buffer_max_posts, "
    "u.last_login_inactivate_delay, u.avatar_disabled, u.avatar_use_post_image, "
    "u.avatar_use_carousel, u.avatar_use_video, u.avatar_use_newsletter, "
    "u.avatar_caption_overlay, u.updated_at, "
    "o.linkedin_connected_at, o.voice_set_at, o.first_post_approved_at, o.caps_enabled_at, "
    "p.max_comments_per_day, p.max_dms_per_day, p.posts_per_week, p.comment_length"
)
# One LEFT JOIN, never a lookup per row. `onboarding_state.started_at` is the closest thing to a
# signup date — `users` has no `created_at`, only an `updated_at` every write moves.
_ADMIN_USER_LIST_FROM = "FROM users u LEFT JOIN onboarding_state o ON o.user_id = u.id"
_ADMIN_USER_DETAIL_FROM = (_ADMIN_USER_LIST_FROM +
                           " LEFT JOIN engagement_preferences p ON p.user_id = u.id")
def _effective_admin_sql(prefix: str = "u") -> tuple:
    """The SQL half of `is_user_admin`: the column OR the allowlist, as a WHERE fragment + params.

    Returns the column test alone when the allowlist is empty — an `IN ()` is a MySQL syntax error,
    and an allowlist nobody configured adds nobody (see `admin_email_allowlist`).
    """
    allowed = sorted(admin_email_allowlist())
    if not allowed:
        return f"{prefix}.is_admin = 1", []
    placeholders = ", ".join(["%s"] * len(allowed))
    return f"({prefix}.is_admin = 1 OR LOWER({prefix}.email) IN ({placeholders}))", allowed
def _like_term(search: str) -> str:
    """A substring LIKE term with the caller's own wildcards neutralised.

    `%` and `_` in a support ticket's email fragment are literal characters, not operators — an
    unescaped `_` silently matches any character and widens the result set the operator is reading.
    """
    escaped = search.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"
def _admin_user_filters(search: Optional[str] = None,
                        subscription_status: Optional[str] = None,
                        linkedin_connection_status: Optional[str] = None,
                        is_admin: Optional[bool] = None) -> tuple:
    """The shared WHERE for the list and its COUNT, so the two can never disagree about the page.

    Status values are validated against their vocabularies at the route (FastAPI enums, 422 on
    anything else); they are parameters here regardless.
    """
    clauses: list = []
    params: list = []
    if search and search.strip():
        # Substring, not prefix: a support request often carries a domain, not a full address.
        clauses.append("(u.email LIKE %s ESCAPE '\\\\' OR u.linkedin_email LIKE %s ESCAPE '\\\\')")
        term = _like_term(search)
        params.extend([term, term])
    if subscription_status:
        clauses.append("u.subscription_status = %s")
        params.append(str(subscription_status))
    if linkedin_connection_status:
        clauses.append("u.linkedin_connection_status = %s")
        params.append(str(linkedin_connection_status))
    if is_admin is not None:
        # Filters on the EFFECTIVE answer, so the filter and the row's badge always agree.
        fragment, fragment_params = _effective_admin_sql()
        clauses.append(fragment if is_admin else f"NOT {fragment}")
        params.extend(fragment_params)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    return where, params
def list_users_for_admin(search: Optional[str] = None,
                         subscription_status: Optional[str] = None,
                         linkedin_connection_status: Optional[str] = None,
                         is_admin: Optional[bool] = None,
                         limit: int = 25, offset: int = 0) -> Optional[list]:
    """One page of the admin user list, newest signup first (issue #1450).

    Ordered by `u.id DESC`: the id is a monotonic AUTO_INCREMENT, so it IS the signup order —
    without the NULLs of a LEFT-JOINed timestamp and without depending on the join at all.

    `None`, never `[]`, when the page could not be read. This is the heaviest query on the surface
    (a LEFT JOIN plus an unindexable `LIKE '%…%'`), so it is the one that can time out while the
    session behind it still resolves — and an empty list is how the screen says "this deployment
    has no users matching that filter". The route answers 503 rather than render that lie.
    """
    where, params = _admin_user_filters(search, subscription_status,
                                        linkedin_connection_status, is_admin)
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute(
                f"SELECT {_ADMIN_USER_LIST_FIELDS} {_ADMIN_USER_LIST_FROM}{where} "
                "ORDER BY u.id DESC LIMIT %s OFFSET %s",
                (*params, int(limit), int(offset)))
            return cursor.fetchall() or []
    except mysql.connector.Error as err:
        log_error("Could not list users for the admin panel", exc=err)
        return None
def count_users_for_admin(search: Optional[str] = None,
                          subscription_status: Optional[str] = None,
                          linkedin_connection_status: Optional[str] = None,
                          is_admin: Optional[bool] = None) -> Optional[int]:
    """How many users match the same filters — the denominator the pager renders.

    `None`, never 0, on a fault, for the same reason as `list_users_for_admin`: a 0 total pages the
    screen to "No users" while the rows it fetched are sitting right above it.
    """
    where, params = _admin_user_filters(search, subscription_status,
                                        linkedin_connection_status, is_admin)
    try:
        with db_cursor() as cursor:
            cursor.execute(f"SELECT COUNT(*) {_ADMIN_USER_LIST_FROM}{where}", tuple(params))
            row = cursor.fetchone()
            return int(row[0]) if row else 0
    except mysql.connector.Error as err:
        log_error("Could not count users for the admin panel", exc=err)
        return None
def get_user_for_admin(user_id: int) -> Optional[dict]:
    """The detail drawer's row, or None when there is no such user."""
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute(
                f"SELECT {_ADMIN_USER_DETAIL_FIELDS} {_ADMIN_USER_DETAIL_FROM} WHERE u.id = %s",
                (int(user_id),))
            return cursor.fetchone()
    except mysql.connector.Error as err:
        log_error(f"Could not read user {user_id} for the admin panel", exc=err)
        return None
def count_admin_users() -> Optional[int]:
    """How many EFFECTIVE admins the deployment has — the input to the last-admin guard.

    `None`, never 0, when the count could not be read. 0 means "nobody is an admin", which is the
    one answer that must never be guessed: it would refuse every revoke on a DB hiccup, including
    the ones an operator is running to fix a lockout. The route answers 503 on `None`.
    """
    fragment, params = _effective_admin_sql()
    try:
        with db_cursor() as cursor:
            cursor.execute(f"SELECT COUNT(*) FROM users u WHERE {fragment}", tuple(params))
            row = cursor.fetchone()
            return int(row[0]) if row else 0
    except mysql.connector.Error as err:
        log_error("Could not count admin users", exc=err)
        return None
def has_engagement_preferences(user_id: int) -> bool:
    """True when the user has actually SAVED engagement preferences. get_engagement_preferences()
    returns code defaults for everyone, so only the row's existence proves they configured it.

    The two-valued view of `engagement_preferences_are_configured` for callers that only steer UI
    copy: an unreadable row reads as False, exactly as this has always behaved. A caller that would
    WRITE on the answer must use the three-valued function instead (issue #952).
    """
    return engagement_preferences_are_configured(user_id) is True
