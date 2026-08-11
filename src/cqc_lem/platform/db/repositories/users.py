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
def update_user(user_id: int, blog_url: Optional[str] = None,
                sitemap_url: Optional[str] = None) -> bool:
    """Update the blog and/or sitemap URL on a user row; False when neither was supplied.

    Only fields that were passed become SET clauses, and each generated clause is re-checked against
    `_ALLOWED_USER_CLAUSES` before it is interpolated — see the note above that set for why `email` is
    deliberately not reachable from here.
    """
    if not any([blog_url, sitemap_url]):
        return False
    connection = _connection.get_db_connection()
    cursor = connection.cursor()
    fields, values = [], []
    if blog_url:
        fields.append("blog_url = %s")
        values.append(blog_url)
    if sitemap_url:
        fields.append("sitemap_url = %s")
        values.append(sitemap_url)
    for clause in fields:
        if clause not in _ALLOWED_USER_CLAUSES:
            raise ValueError(f"Disallowed SQL clause: {clause!r}")
    values.append(user_id)
    try:
        cursor.execute(f"UPDATE users SET {', '.join(fields)} WHERE id = %s", values)
        connection.commit()
        return cursor.rowcount > 0
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
