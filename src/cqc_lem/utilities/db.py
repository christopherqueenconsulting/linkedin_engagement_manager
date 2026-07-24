import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Optional

import mysql.connector
from cqc_lem.utilities.env_constants import (
    AWS_MYSQL_SECRET_NAME,
    AWS_REGION,
    SESSION_ABSOLUTE_MAX_DAYS,
    SESSION_IDLE_HOURS,
)
from cqc_lem.utilities.linkedin.profile import LinkedInProfile
from cqc_lem.utilities.logger import myprint
from cqc_lem.utilities.utils import get_top_level_domain, get_aws_ssm_secret
from dotenv import load_dotenv
from mysql.connector import errorcode

# Load .env file
load_dotenv()

MAX_WAIT_RETRY = 3
WAIT_TIMEOUT = 3

# Retrieve MySQL connection details from environment variables
MYSQL_HOST = os.getenv('MYSQL_HOST')
MYSQL_USER = os.getenv('MYSQL_USER')
MYSQL_PASSWORD = os.getenv('MYSQL_PASSWORD')
MYSQL_DATABASE = os.getenv('MYSQL_DATABASE')
MYSQL_PORT = os.getenv('MYSQL_PORT')


def get_db_connection():
    """Establishes a connection to the MySQL database and returns the connection object.

    Raises:
        mysql.connector.Error: If there is an error connecting to the database.
    """

    global MYSQL_HOST, MYSQL_USER, MYSQL_PASSWORD, MYSQL_DATABASE, MYSQL_PORT

    # if MYSQL_USER and MYSQL_PASSWORD are empty try to get it from AWS using get_secret function
    if AWS_MYSQL_SECRET_NAME is not None and AWS_REGION is not None:
        secret_dict = get_aws_ssm_secret(AWS_MYSQL_SECRET_NAME, AWS_REGION)
        MYSQL_HOST = secret_dict['host']
        MYSQL_USER = secret_dict['username']
        MYSQL_PASSWORD = secret_dict['password']
        MYSQL_DATABASE = secret_dict['dbname']
        MYSQL_PORT = secret_dict['port']

    return mysql.connector.connect(
        host=MYSQL_HOST,
        user=MYSQL_USER,
        password=MYSQL_PASSWORD,
        database=MYSQL_DATABASE,
        port=MYSQL_PORT,
        time_zone='+00:00',
    )


class PostType(StrEnum):
    TEXT = 'text'
    CAROUSEL = 'carousel'
    VIDEO = 'video'
    DOCUMENT = 'document'  # native PDF/document post — highest-reach 2026 format


class PostStatus(StrEnum):
    PLANNING = 'planning'
    PENDING = 'pending'
    APPROVED = 'approved'
    REJECTED = 'rejected'
    SCHEDULED = 'scheduled'
    POSTED = 'posted'
    ERROR = 'error'  # generation/posting failed (e.g. no real carousel images) — needs manual/dev fix


class ScheduledDmStatus(StrEnum):
    """Status for a scheduled 1:1 DM (issue #306), mirroring PostStatus."""
    PENDING = 'pending'      # draft awaiting approval
    APPROVED = 'approved'    # approved, waiting for its scheduled_time
    SCHEDULED = 'scheduled'  # scanner dispatched the send task
    SENT = 'sent'            # delivered
    FAILED = 'failed'        # send failed
    CANCELED = 'canceled'    # canceled before send


class ConnectionRequestStatus(StrEnum):
    """Status for a proactive, approval-gated connection request (issue #398), mirroring ScheduledDmStatus."""
    PENDING = 'pending'      # draft awaiting approval
    APPROVED = 'approved'    # approved, waiting for the scanner
    SENDING = 'sending'      # scanner dispatched the send task
    SENT = 'sent'            # invitation sent
    FAILED = 'failed'        # send failed
    CANCELED = 'canceled'    # canceled before send


# Enum for log actions types
class LogActionType(StrEnum):
    COMMENT = 'comment'
    DM = 'dm'
    REPLY = 'reply'
    POST = 'post'
    ENGAGED = 'engaged'
    FOLLOWUP = 'followup'


# ENum for log result options
class LogResultType(StrEnum):
    SUCCESS = 'success'
    FAILURE = 'failure'


# Marker message logged (as ENGAGED/SUCCESS) whenever a LinkedIn invite is actually sent — reactive
# profile-viewer AND proactive (issue #398) sends both flow through invite_to_connect_now, so the
# combined daily invite budget is counted from these log rows (see count_invites_sent_today).
CONNECTION_REQUEST_SENT_MESSAGE = "Connection Request Sent Successfully"


def store_cookies(user_email: str, cookies: list[dict]):
    connection = get_db_connection()
    cursor = connection.cursor()

    user_id = get_user_id(user_email)

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
                cookie['value'],
                cookie['domain'],
                cookie['path'],
                cookie['expiry'] if 'expiry' in cookie else None,
                cookie['secure'],
                cookie['httpOnly'],
                user_id
            ))
        except mysql.connector.Error as err:
            myprint(f"Could not add cookie to database | Error: {err}")

    connection.commit()
    cursor.close()
    connection.close()

    if user_id is not None:
        prune_superseded_cookies(user_id)


def prune_superseded_cookies(user_id: int) -> int:
    """Keep only the most-recently-updated row per (user_id, name), deleting older duplicates
    left behind when the same cookie is re-stored under a different domain scope — e.g. the
    extension writes li_at on '.linkedin.com' while a prior Selenium login stored it on
    '.www.linkedin.com'. get_cookies matches on `domain LIKE %tld%`, so a stale variant would
    otherwise be returned alongside the fresh one and could shadow it at login.

    Conservative by design: it only deletes a row when a STRICTLY newer sibling of the same
    name exists for the same user, so it never removes the newest copy and never touches a
    uniquely-named cookie. Best-effort — a failure here never breaks the cookie write."""
    connection = get_db_connection()
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
            myprint(f"Pruned {deleted} superseded cookie(s) for user_id {user_id}")
    except mysql.connector.Error as err:
        myprint(f"Could not prune superseded cookies for user_id {user_id} | Error: {err}")
    finally:
        cursor.close()
        connection.close()
    return deleted


def get_cookies(url: str, user_email: str):
    connection = get_db_connection()
    cursor = connection.cursor(dictionary=True)

    # Extract the top-level domain from the URL
    tld = get_top_level_domain(url)

    try:
        cursor.execute("""
            SELECT c.name, c.value, c.domain, c.path, UNIX_TIMESTAMP(c.expiry) AS expiry, c.secure, c.http_only
            FROM cookies c
            JOIN users u ON c.user_id = u.id
            WHERE c.domain LIKE %s AND u.email = %s
        """, (f"%{tld}%", user_email))

        cookies = cursor.fetchall()
    except mysql.connector.Error as err:
        myprint(f"Could not get cookies from DB | Error: {err}")
        cookies = None
    finally:
        cursor.close()
        connection.close()

    return cookies


def store_linkedin_li_at(user_id: int, li_at: str, jsessionid: Optional[str] = None) -> bool:
    """Persist a user-supplied LinkedIn session cookie (li_at, optionally JSESSIONID).

    Lets login_to_linkedin resume an already-trusted session instead of doing a fresh
    password login — which is what triggers LinkedIn's new-device challenge. Reuses the
    standard cookie store so the existing cookie-first login path picks it up.
    """
    email = get_user_email(user_id)
    if not email:
        myprint(f"store_linkedin_li_at: no email for user_id {user_id}")
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
        store_cookies(email, cookies)
        return True
    except Exception as e:
        myprint(f"Could not store LinkedIn session cookie for user_id {user_id}: {e}")
        return False


def has_linkedin_session(user_id: int) -> bool:
    """True if the user has a stored LinkedIn session cookie (li_at) to log in with."""
    connection = get_db_connection()
    cursor = connection.cursor()
    try:
        cursor.execute(
            "SELECT 1 FROM cookies WHERE user_id = %s AND name = 'li_at' LIMIT 1",
            (user_id,),
        )
        return cursor.fetchone() is not None
    except mysql.connector.Error as err:
        myprint(f"Could not check linkedin session for user_id {user_id} | Error: {err}")
        return False
    finally:
        cursor.close()
        connection.close()


def get_linkedin_session_email_sent_at(user_id: int):
    """Return the datetime the last session notification email was sent, or None."""
    connection = get_db_connection()
    cursor = connection.cursor()
    try:
        cursor.execute(
            "SELECT linkedin_session_email_sent_at FROM users WHERE id = %s", (user_id,)
        )
        row = cursor.fetchone()
        return row[0] if row else None
    except mysql.connector.Error as err:
        myprint(f"Could not read session email timestamp for user_id {user_id} | Error: {err}")
        return None
    finally:
        cursor.close()
        connection.close()


def set_linkedin_session_email_sent_at(user_id: int) -> bool:
    """Stamp now() as the last session notification email time (throttle)."""
    connection = get_db_connection()
    cursor = connection.cursor()
    try:
        cursor.execute(
            "UPDATE users SET linkedin_session_email_sent_at = NOW() WHERE id = %s", (user_id,)
        )
        connection.commit()
        return True
    except mysql.connector.Error as err:
        myprint(f"Could not set session email timestamp for user_id {user_id} | Error: {err}")
        return False
    finally:
        cursor.close()
        connection.close()


def add_user(email: str, password: str):
    connection = get_db_connection()
    cursor = connection.cursor()

    try:
        cursor.execute("INSERT INTO users (email, password) VALUES (%s, %s)", (email, password))
        connection.commit()
    except mysql.connector.Error as e:
        if e.errno == errorcode.ER_DUP_ENTRY:
            myprint(f"User with email {email} already exists.")
        else:
            myprint(f"An error occurred: {e}")
    finally:
        cursor.close()
        connection.close()


def add_user_with_access_token(email: str, linked_sub_id: str, access_token: str, access_token_expires_in: str,
                               refresh_token: str = None,
                               refresh_token_expires_in: str = None):
    connection = get_db_connection()
    cursor = connection.cursor()

    access_token_created_at = datetime.now(timezone.utc)

    if refresh_token is not None:
        refresh_token_created_at = datetime.now(timezone.utc)
    else:
        refresh_token_created_at = None

    try:
        cursor.execute("""INSERT INTO users (email, linked_sub_id, access_token, access_token_expires_in, access_token_created_at, refresh_token, refresh_token_expires_in, refresh_token_created_at, last_login, linkedin_connection_status)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'connected')
        ON DUPLICATE KEY UPDATE
                linked_sub_id = VALUES(linked_sub_id),
                access_token = VALUES(access_token),
                access_token_expires_in = VALUES(access_token_expires_in),
                access_token_created_at = VALUES(access_token_created_at),
                refresh_token = VALUES(refresh_token),
                refresh_token_expires_in = VALUES(refresh_token_expires_in),
                refresh_token_created_at = VALUES(refresh_token_created_at),
                last_login = VALUES(last_login),
                linkedin_connection_status = 'connected'

        """, (
            email,
            linked_sub_id,
            access_token, access_token_expires_in, access_token_created_at,
            refresh_token, refresh_token_expires_in, refresh_token_created_at,
            datetime.now(timezone.utc)))
        connection.commit()
    except mysql.connector.Error as e:
        if e.errno == errorcode.ER_DUP_ENTRY:
            myprint(f"User with email {email} already exists.")
        else:
            myprint(f"An error occurred: {e}")
    finally:
        cursor.close()
        connection.close()


def get_user_linked_sub_id(user_id: int):
    connection = get_db_connection()
    cursor = connection.cursor(dictionary=True)

    try:
        cursor.execute("SELECT linked_sub_id FROM users WHERE id = %s", (user_id,))

        linked_sub_id = cursor.fetchone()
    except mysql.connector.Error as err:
        myprint(f"Could not get user linked sub id | Error: {err}")
        linked_sub_id = None
    finally:
        cursor.close()
        connection.close()

    return linked_sub_id['linked_sub_id'] if linked_sub_id else None


def get_user_access_token(user_id: int):
    connection = get_db_connection()
    cursor = connection.cursor(dictionary=True)

    try:
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
        myprint(f"Could not get user access token | Error: {err}")
        access_token = None
    finally:
        cursor.close()
        connection.close()

    return access_token['access_token'] if access_token else None


def get_user_id(email: str):
    connection = get_db_connection()
    cursor = connection.cursor(dictionary=True)

    try:
        cursor.execute("SELECT id FROM users WHERE email = %s", (email,))

        user_id = cursor.fetchone()
    except mysql.connector.Error as err:
        myprint(f"Could not get user id | Error: {err}")
        user_id = None
    finally:
        cursor.close()
        connection.close()

    return user_id['id'] if user_id else None


def insert_post(email: str, content: str, scheduled_time: datetime, post_type: PostType,
                video_url: Optional[str] = None, carousel_slides: Optional[list[str]] = None,
                video_quality: str = "standard", status: PostStatus = PostStatus.PENDING) -> bool:
    user_id = get_user_id(email)

    success = False

    if not user_id:
        myprint(f"User with email {email} not found.")
        return success

    connection = get_db_connection()
    cursor = connection.cursor()

    try:
        if scheduled_time.tzinfo is None:
            scheduled_time = scheduled_time.replace(tzinfo=timezone.utc)
        else:
            scheduled_time = scheduled_time.astimezone(timezone.utc)

        slides_json = json.dumps(carousel_slides) if carousel_slides else None

        cursor.execute("""
            INSERT INTO posts (content, scheduled_time, post_type, user_id, video_url, carousel_slides, video_quality, status)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """, (content, scheduled_time, post_type.value, user_id, video_url, slides_json,
              video_quality or "standard", status.value))

        connection.commit()
        success = cursor.rowcount == 1
    except mysql.connector.Error as e:
        success = False
        myprint(f"Count not insert post. An error occurred: {e}")
    finally:
        cursor.close()
        connection.close()

    return success


def insert_planned_post(user_id: int, scheduled_time: datetime, post_type: PostType, buyer_stage: str) -> bool:
    connection = get_db_connection()
    cursor = connection.cursor()

    success = False

    try:
        # Convert scheduled_time to UTC
        if scheduled_time.tzinfo is None:
            scheduled_time = scheduled_time.replace(tzinfo=timezone.utc)
        else:
            scheduled_time = scheduled_time.astimezone(timezone.utc)

        cursor.execute("""
            INSERT INTO posts (scheduled_time, post_type, user_id, buyer_stage, status, content)
            VALUES (%s, %s, %s, %s, %s, %s)
        """, (scheduled_time, post_type.value, user_id, buyer_stage, PostStatus.PLANNING.value, 'TBD'))

        connection.commit()
        success = cursor.rowcount == 1
    except mysql.connector.Error as e:
        success = False
        myprint(f"Count not insert planned post. An error occurred: {e}")
    finally:
        cursor.close()
        connection.close()
    return success


def update_db_post(content: str, video_url: str, scheduled_time: datetime, post_type: PostType, post_id: int,
                   post_status: PostStatus) -> bool:
    connection = get_db_connection()
    cursor = connection.cursor()

    success = False

    try:

        # Convert scheduled_time to UTC
        if scheduled_time.tzinfo is None:
            scheduled_time = scheduled_time.replace(tzinfo=timezone.utc)
        else:
            scheduled_time = scheduled_time.astimezone(timezone.utc)

        cursor.execute(
            "UPDATE posts SET content = %s, video_url = %s, scheduled_time =%s, post_type = %s, status = %s WHERE id = %s",
            (content, video_url, scheduled_time, post_type.value, post_status.value, post_id)
        )

        connection.commit()
        success = cursor.rowcount == 1
    except mysql.connector.Error as e:
        success = False
        myprint(f"Count not update post. An error occurred: {e}")
    finally:
        cursor.close()
        connection.close()

    return success


def update_db_post_content(post_id: int, content: str) -> bool:
    connection = get_db_connection()
    cursor = connection.cursor()

    try:
        cursor.execute(
            "UPDATE posts SET content = %s WHERE id = %s",
            (content, post_id)
        )

        connection.commit()
        success = cursor.rowcount == 1
    except mysql.connector.Error as e:
        success = False
        myprint(f"Count not update post content. An error occurred: {e}")
    finally:
        cursor.close()
        connection.close()

    return success


def update_db_post_video_url(post_id: int, video_url: str) -> bool:
    connection = get_db_connection()
    cursor = connection.cursor()

    try:
        cursor.execute(
            "UPDATE posts SET video_url = %s WHERE id = %s",
            (video_url, post_id)
        )

        connection.commit()
        success = cursor.rowcount == 1
    except mysql.connector.Error as e:
        success = False
        myprint(f"Count not update post video url. An error occurred: {e}")
    finally:
        cursor.close()
        connection.close()

    return success


def update_db_post_status(post_id: int, post_status: PostStatus) -> bool:
    connection = get_db_connection()
    cursor = connection.cursor()

    # TODO: Why Enum doesnt work inside this function (have to convert to string first but why) ????
    status_str = "posted"
    try:
        status_str = post_status.value
    except Exception:
        myprint(f"Error converting post_status to string: {post_status}")

    try:
        cursor.execute(
            """UPDATE posts SET status = %s WHERE id = %s""",
            (status_str, post_id)
        )

        connection.commit()
        success = cursor.rowcount == 1
    except mysql.connector.Error as e:
        success = False
        myprint(f"Count not update post status. An error occurred: {e}")
    finally:
        cursor.close()
        connection.close()

    return success


# Columns the Review & Edit list may be ordered by. Whitelisted to keep the
# ORDER BY clause injection-safe (the value is interpolated, not parameterized).
_POST_SORT_COLUMNS = {
    'scheduled_time': 'scheduled_time',
    'status': 'status',
    'post_type': 'post_type',
    'id': 'id',
}

_SEARCH_MAX_TERMS = 20


def _escape_like(term: str) -> str:
    """Escape LIKE wildcards so a search term matches literally (default '\\' escape)."""
    return term.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')


def build_content_search_clause(search: Optional[str], column: str = 'content') -> tuple[Optional[str], list]:
    """Parse a boolean keyword query into a parameterized SQL condition over *column*.

    Supports AND / OR / NOT (case-insensitive), implicit AND between adjacent terms,
    parentheses for grouping, and "quoted phrases". Each bare term matches the column
    case-insensitively via LIKE %term% (wildcards in the term are escaped). Returns
    ``(sql_fragment, params)`` — the fragment is already fully parenthesized and safe
    to drop into a WHERE — or ``(None, [])`` when the query is empty. Unparseable input
    falls back to a single LIKE over the raw string so search never hard-errors.
    """
    if not search or not search.strip():
        return None, []

    # ── Tokenize: parens, "quoted phrases", and bare words (AND/OR/NOT are keywords).
    tokens: list[tuple[str, str]] = []
    i, n = 0, len(search)
    while i < n:
        ch = search[i]
        if ch.isspace():
            i += 1
        elif ch == '(':
            tokens.append(('LPAREN', ch)); i += 1
        elif ch == ')':
            tokens.append(('RPAREN', ch)); i += 1
        elif ch == '"':
            j = i + 1
            while j < n and search[j] != '"':
                j += 1
            phrase = search[i + 1:j]
            if phrase.strip():
                tokens.append(('TERM', phrase.strip()))
            i = j + 1
        else:
            j = i
            while j < n and not search[j].isspace() and search[j] not in '()"':
                j += 1
            word = search[i:j]
            upper = word.upper()
            if upper in ('AND', 'OR', 'NOT'):
                tokens.append((upper, word))
            else:
                tokens.append(('TERM', word))
            i = j

    if not tokens:
        return None, []

    params: list = []
    term_count = 0

    class _ParseError(Exception):
        pass

    pos = 0

    def _peek() -> Optional[str]:
        return tokens[pos][0] if pos < len(tokens) else None

    def _term_sql(value: str) -> str:
        nonlocal term_count
        term_count += 1
        if term_count > _SEARCH_MAX_TERMS:
            raise _ParseError('too many terms')
        params.append(f"%{_escape_like(value)}%")
        return f"{column} LIKE %s"

    def _parse_or() -> str:
        node = _parse_and()
        while _peek() == 'OR':
            nonlocal_advance()
            node = f"({node} OR {_parse_and()})"
        return node

    def _parse_and() -> str:
        node = _parse_not()
        while _peek() in ('AND', 'NOT', 'TERM', 'LPAREN'):
            if _peek() == 'AND':
                nonlocal_advance()
            node = f"({node} AND {_parse_not()})"
        return node

    def _parse_not() -> str:
        if _peek() == 'NOT':
            nonlocal_advance()
            return f"(NOT {_parse_not()})"
        return _parse_atom()

    def _parse_atom() -> str:
        tok = _peek()
        if tok == 'LPAREN':
            nonlocal_advance()
            inner = _parse_or()
            if _peek() != 'RPAREN':
                raise _ParseError('unbalanced parens')
            nonlocal_advance()
            return f"({inner})"
        if tok == 'TERM':
            value = tokens[pos][1]
            nonlocal_advance()
            return _term_sql(value)
        raise _ParseError(f'unexpected token {tok}')

    def nonlocal_advance() -> None:
        nonlocal pos
        pos += 1

    try:
        sql = _parse_or()
        if pos != len(tokens):
            raise _ParseError('trailing tokens')
        return sql, params
    except _ParseError:
        # Fallback: treat the whole raw query as one literal term.
        return f"{column} LIKE %s", [f"%{_escape_like(search.strip())}%"]


def get_posts(user_id: int, limit: int = 10, offset: int = 0,
              sort_order: str = 'asc', status_filter: Optional[str] = None,
              post_type_filter: Optional[str] = None, search: Optional[str] = None,
              sort_by: str = 'scheduled_time') -> tuple[list, int]:
    connection = get_db_connection()
    cursor = connection.cursor(dictionary=True)

    order = 'ASC' if sort_order.lower() != 'desc' else 'DESC'
    sort_col = _POST_SORT_COLUMNS.get((sort_by or '').lower(), 'scheduled_time')

    try:
        where = "WHERE user_id = %s"
        params: list = [user_id]
        if status_filter:
            where += " AND status = %s"
            params.append(status_filter.lower())
        if post_type_filter:
            where += " AND post_type = %s"
            params.append(post_type_filter.lower())
        search_sql, search_params = build_content_search_clause(search)
        if search_sql:
            where += f" AND ({search_sql})"
            params.extend(search_params)

        cursor.execute(
            f"SELECT COUNT(*) AS total FROM posts {where}",
            params
        )
        total = cursor.fetchone()['total']

        cursor.execute(
            f"SELECT id, content, video_url, scheduled_time, post_type, status, carousel_slides "
            f"FROM posts {where} ORDER BY {sort_col} {order}, id {order} LIMIT %s OFFSET %s",
            params + [limit, offset]
        )
        posts = cursor.fetchall()
    except mysql.connector.Error as err:
        myprint(f"Could not get posts for user id: {user_id} | Error: {err}")
        posts = []
        total = 0
    finally:
        cursor.close()
        connection.close()

    return posts, total


def get_dashboard_counts(user_id: int, week_start) -> dict:
    """Dashboard top-line counts via SQL aggregates over ALL of the user's posts. Replaces the old
    approach of counting in Python over get_posts()'s 10-oldest-posts slice (which made 'posted'
    cap near 10 and 'scheduled this week' read ~0). week_start is coerced to a naive UTC datetime so
    it compares cleanly against the naive UTC scheduled_time column (no tz TypeError)."""
    if week_start is not None and getattr(week_start, "tzinfo", None) is not None:
        week_start = week_start.astimezone(timezone.utc).replace(tzinfo=None)
    connection = get_db_connection()
    cursor = connection.cursor()
    try:
        cursor.execute(
            "SELECT "
            "  COALESCE(SUM(status IN (%s,%s) AND scheduled_time >= %s), 0) AS scheduled_this_week, "
            "  COALESCE(SUM(status = %s), 0) AS pending_review, "
            "  COALESCE(SUM(status = %s), 0) AS posted_total "
            "FROM posts WHERE user_id = %s",
            (PostStatus.APPROVED.value, PostStatus.PENDING.value, week_start,
             PostStatus.PENDING.value, PostStatus.POSTED.value, user_id))
        row = cursor.fetchone()
        return {"scheduled_this_week": int(row[0] or 0),
                "pending_review": int(row[1] or 0),
                "posted_total": int(row[2] or 0)}
    except mysql.connector.Error as err:
        myprint(f"Could not get dashboard counts for user {user_id} | Error: {err}")
        return {"scheduled_this_week": 0, "pending_review": 0, "posted_total": 0}
    finally:
        cursor.close()
        connection.close()


def get_planned_tasks(user_id: int, limit: int = 10) -> list[dict]:
    """Upcoming (future-dated, non-terminal) work for the dashboard "Planned Tasks" card:
    scheduled/approved/pending POSTS, scheduled DMs, and upcoming NEWSLETTER editions — each
    labeled by `kind` (Post / DM / Newsletter). Terminal states (posted/sent/published/etc.)
    are excluded, results are merged and sorted soonest-first, capped at `limit`."""
    connection = get_db_connection()
    cursor = connection.cursor(dictionary=True)
    tasks: list[dict] = []
    try:
        cursor.execute(
            "SELECT id, content, scheduled_time, status FROM posts "
            "WHERE user_id = %s AND status IN (%s, %s, %s) "
            "AND scheduled_time >= UTC_TIMESTAMP() ORDER BY scheduled_time ASC LIMIT %s",
            (user_id, PostStatus.PENDING.value, PostStatus.APPROVED.value,
             PostStatus.SCHEDULED.value, limit))
        for row in cursor.fetchall():
            tasks.append({
                "kind": "Post",
                "id": row["id"],
                "title": (row.get("content") or "").strip()[:120] or "Scheduled post",
                "scheduled_time": row["scheduled_time"],
                "status": row["status"],
            })

        cursor.execute(
            "SELECT id, recipient_name, message, scheduled_time, status FROM scheduled_dms "
            "WHERE user_id = %s AND status IN (%s, %s, %s) "
            "AND scheduled_time >= UTC_TIMESTAMP() ORDER BY scheduled_time ASC LIMIT %s",
            (user_id, ScheduledDmStatus.PENDING.value, ScheduledDmStatus.APPROVED.value,
             ScheduledDmStatus.SCHEDULED.value, limit))
        for row in cursor.fetchall():
            title = (row.get("recipient_name") or "").strip() or (row.get("message") or "").strip()[:120]
            tasks.append({
                "kind": "DM",
                "id": row["id"],
                "title": title or "Scheduled DM",
                "scheduled_time": row["scheduled_time"],
                "status": row["status"],
            })

        # newsletter_editions has no status enum in code; 'draft'/'approved' are the non-terminal
        # states (mirrors get_pending_newsletter_editions), 'published'/'failed'/'skipped' terminal.
        cursor.execute(
            "SELECT id, title, scheduled_for, status FROM newsletter_editions "
            "WHERE user_id = %s AND status IN ('draft', 'approved') "
            "AND scheduled_for >= UTC_TIMESTAMP() ORDER BY scheduled_for ASC LIMIT %s",
            (user_id, limit))
        for row in cursor.fetchall():
            tasks.append({
                "kind": "Newsletter",
                "id": row["id"],
                "title": (row.get("title") or "").strip() or "Newsletter edition",
                "scheduled_time": row["scheduled_for"],
                "status": row["status"],
            })
    except mysql.connector.Error as err:
        myprint(f"Could not get planned tasks for user {user_id} | Error: {err}")
        return []
    finally:
        cursor.close()
        connection.close()

    tasks.sort(key=lambda t: t["scheduled_time"])
    return tasks[:limit]


def get_default_video_quality(user_id: int) -> str:
    """The user's preferred default video quality for AUTO-generated posts (engagement_preferences).
    Falls back to 'standard' when unset/invalid — premium is only ever honored when credits exist,
    which is enforced separately at render time."""
    connection = get_db_connection()
    cursor = connection.cursor(dictionary=True)
    try:
        cursor.execute(
            "SELECT default_video_quality FROM engagement_preferences WHERE user_id = %s",
            (user_id,))
        row = cursor.fetchone()
        quality = row.get("default_video_quality") if row else None
        return quality if quality in VALID_VIDEO_QUALITIES else "standard"
    except mysql.connector.Error as err:
        myprint(f"Could not get default video quality for user {user_id} | Error: {err}")
        return "standard"
    finally:
        cursor.close()
        connection.close()


def set_default_video_quality(user_id: int, quality: str) -> bool:
    """Set the user's default video quality preference (upserts the engagement_preferences row).
    Invalid values are coerced to 'standard'."""
    if quality not in VALID_VIDEO_QUALITIES:
        quality = "standard"
    return update_engagement_preferences(user_id, {"default_video_quality": quality})


def get_posted_posts(user_id: int):
    connection = get_db_connection()
    cursor = connection.cursor(dictionary=True)

    try:
        cursor.execute(
            "SELECT id, content, scheduled_time, post_type, status FROM posts WHERE user_id = %s AND status = 'posted' ORDER BY scheduled_time asc",
            (user_id,))

        posts = cursor.fetchall()
    except mysql.connector.Error as err:
        myprint(f"Could not get posted posts for user id: {user_id} | Error: {err}")
        posts = None
    finally:
        cursor.close()
        connection.close()

    return posts


def get_post_by_email(email: str, limit: int = 10, offset: int = 0,
                      sort_order: str = 'asc', status_filter: Optional[str] = None,
                      post_type_filter: Optional[str] = None, search: Optional[str] = None,
                      sort_by: str = 'scheduled_time') -> tuple[list, int]:
    user_id = get_user_id(email)

    if not user_id:
        myprint(f"User with email {email} not found.")
        return [], 0

    return get_posts(user_id, limit=limit, offset=offset, sort_order=sort_order,
                     status_filter=status_filter, post_type_filter=post_type_filter,
                     search=search, sort_by=sort_by)


def get_post_content(post_id: int):
    connection = get_db_connection()
    cursor = connection.cursor(dictionary=True)

    try:
        cursor.execute("SELECT content FROM posts WHERE id = %s", (post_id,))

        post = cursor.fetchone()
    except mysql.connector.Error as err:
        myprint(f"Could not get post content for post id: {post_id} | Error: {err}")
        post = False
    finally:
        cursor.close()
        connection.close()

    return post['content'] if post else None


def get_post_user_id(post_id: int):
    connection = get_db_connection()
    cursor = connection.cursor(dictionary=True)

    try:
        cursor.execute("SELECT user_id FROM posts WHERE id = %s", (post_id,))

        post = cursor.fetchone()
    except mysql.connector.Error as err:
        myprint(f"Could not get post user id for post id: {post_id} | Error: {err}")
        post = False
    finally:
        cursor.close()
        connection.close()

    return post['user_id'] if post else None


def get_post_video_url(post_id: int):
    connection = get_db_connection()
    cursor = connection.cursor(dictionary=True)

    try:
        cursor.execute("SELECT video_url FROM posts WHERE id = %s", (post_id,))

        post = cursor.fetchone()
    except mysql.connector.Error as err:
        myprint(f"Could not get post video_url for post id: {post_id} | Error: {err}")
        post = False
    finally:
        cursor.close()
        connection.close()

    return post['video_url'] if post else None


def get_post_buyer_stage(post_id: int) -> Optional[str]:
    connection = get_db_connection()
    cursor = connection.cursor(dictionary=True)
    try:
        cursor.execute("SELECT buyer_stage FROM posts WHERE id = %s", (post_id,))
        row = cursor.fetchone()
    except mysql.connector.Error as err:
        myprint(f"Could not get buyer_stage for post id: {post_id} | Error: {err}")
        row = None
    finally:
        cursor.close()
        connection.close()
    return row['buyer_stage'] if row else None


def get_post_type(post_id: int) -> Optional[PostType]:
    connection = get_db_connection()
    cursor = connection.cursor(dictionary=True)

    try:
        cursor.execute("SELECT post_type FROM posts WHERE id = %s", (post_id,))
        row = cursor.fetchone()
    except mysql.connector.Error as err:
        myprint(f"Could not get post_type for post id: {post_id} | Error: {err}")
        row = None
    finally:
        cursor.close()
        connection.close()

    if row:
        try:
            return PostType(row['post_type'])
        except ValueError:
            return None
    return None


def get_carousel_slides(post_id: int) -> list[str]:
    connection = get_db_connection()
    cursor = connection.cursor(dictionary=True)

    try:
        cursor.execute("SELECT carousel_slides FROM posts WHERE id = %s", (post_id,))
        row = cursor.fetchone()
    except mysql.connector.Error as err:
        myprint(f"Could not get carousel_slides for post id: {post_id} | Error: {err}")
        row = None
    finally:
        cursor.close()
        connection.close()

    if row and row['carousel_slides']:
        try:
            slides = row['carousel_slides']
            if isinstance(slides, str):
                slides = json.loads(slides)
            return slides if isinstance(slides, list) else []
        except (json.JSONDecodeError, TypeError):
            return []
    return []


_ALLOWED_POST_CLAUSES = frozenset({"status = %s", "scheduled_time = %s"})


def bulk_update_posts(post_ids: list[int], status: Optional[PostStatus] = None,
                      scheduled_time: Optional[datetime] = None) -> bool:
    if not post_ids:
        return False

    connection = get_db_connection()
    cursor = connection.cursor()

    success = False
    try:
        sets = []
        params: list = []

        if status is not None:
            sets.append("status = %s")
            params.append(status.value)
        if scheduled_time is not None:
            if scheduled_time.tzinfo is None:
                scheduled_time = scheduled_time.replace(tzinfo=timezone.utc)
            else:
                scheduled_time = scheduled_time.astimezone(timezone.utc)
            sets.append("scheduled_time = %s")
            params.append(scheduled_time)

        if not sets:
            return False

        for clause in sets:
            if clause not in _ALLOWED_POST_CLAUSES:
                raise ValueError(f"Disallowed SQL clause: {clause!r}")

        placeholders = ', '.join(['%s'] * len(post_ids))
        params.extend(post_ids)

        cursor.execute(
            f"UPDATE posts SET {', '.join(sets)} WHERE id IN ({placeholders})",
            params
        )
        connection.commit()
        success = cursor.rowcount > 0
    except mysql.connector.Error as e:
        myprint(f"Could not bulk update posts. An error occurred: {e}")
        success = False
    finally:
        cursor.close()
        connection.close()

    return success


def soft_delete_posts(post_ids: list[int]) -> bool:
    return bulk_update_posts(post_ids, status=PostStatus.REJECTED)


def update_db_post_carousel_slides(post_id: int, slides: list[str]) -> bool:
    connection = get_db_connection()
    cursor = connection.cursor()
    try:
        cursor.execute(
            "UPDATE posts SET carousel_slides = %s WHERE id = %s",
            (json.dumps(slides), post_id)
        )
        connection.commit()
        success = cursor.rowcount == 1
    except mysql.connector.Error as e:
        success = False
        myprint(f"Could not update carousel_slides for post {post_id}. Error: {e}")
    finally:
        cursor.close()
        connection.close()
    return success


def update_db_post_shape(post_id: int, archetype: Optional[str], hook_style: Optional[str],
                         topic: Optional[str] = None) -> bool:
    """Persist the SHAPE (short-form archetype + hook style + topic) assigned to a generated post —
    the rotation history that keeps a user's next post from reusing a recently used shape (V51), and
    the topic attribution the feedback loop reads back off each captured stat row (#386)."""
    connection = get_db_connection()
    cursor = connection.cursor()
    try:
        cursor.execute(
            "UPDATE posts SET archetype = %s, hook_style = %s, topic = %s WHERE id = %s",
            (archetype, hook_style, topic, post_id)
        )
        connection.commit()
        success = cursor.rowcount == 1
    except mysql.connector.Error as e:
        success = False
        myprint(f"Could not update shape for post {post_id}. Error: {e}")
    finally:
        cursor.close()
        connection.close()
    return success


def update_db_post_authenticity_score(post_id: int, score: Optional[int]) -> bool:
    """Persist the authenticity gate's LLM-judged score (0-100, or NULL) for a post — the reader that
    gives the previously dead post-quality column a purpose (issue #382, V57 authenticity_score). The
    content-plan status-setter reads this back to demote a low-scoring auto-approve to PENDING."""
    connection = get_db_connection()
    cursor = connection.cursor()
    try:
        cursor.execute(
            "UPDATE posts SET authenticity_score = %s WHERE id = %s",
            (score, post_id)
        )
        connection.commit()
        success = cursor.rowcount == 1
    except mysql.connector.Error as e:
        success = False
        myprint(f"Could not update authenticity score for post {post_id}. Error: {e}")
    finally:
        cursor.close()
        connection.close()
    return success


def get_post_authenticity_score(post_id: int) -> Optional[int]:
    """The authenticity gate's persisted score for a post (0-100), or None when unscored (issue #382)."""
    connection = get_db_connection()
    cursor = connection.cursor()
    try:
        cursor.execute("SELECT authenticity_score FROM posts WHERE id = %s", (post_id,))
        row = cursor.fetchone()
        return int(row[0]) if row and row[0] is not None else None
    except mysql.connector.Error as err:
        myprint(f"Could not get authenticity score for post {post_id} | Error: {err}")
        return None
    finally:
        cursor.close()
        connection.close()


def update_db_post_dwell_score(post_id: int, score: Optional[int]) -> bool:
    """Persist the deterministic 0-100 dwell-proxy score for a post (issue #391, dwell_score column).
    Advisory metric stored next to authenticity_score — it is never read back to gate a status, so a
    failed write only costs the datapoint."""
    connection = get_db_connection()
    cursor = connection.cursor()
    try:
        cursor.execute(
            "UPDATE posts SET dwell_score = %s WHERE id = %s",
            (score, post_id)
        )
        connection.commit()
        success = cursor.rowcount == 1
    except mysql.connector.Error as e:
        success = False
        myprint(f"Could not update dwell score for post {post_id}. Error: {e}")
    finally:
        cursor.close()
        connection.close()
    return success


def get_post_dwell_score(post_id: int) -> Optional[int]:
    """The persisted dwell-proxy score for a post (0-100), or None when unscored (issue #391)."""
    connection = get_db_connection()
    cursor = connection.cursor()
    try:
        cursor.execute("SELECT dwell_score FROM posts WHERE id = %s", (post_id,))
        row = cursor.fetchone()
        return int(row[0]) if row and row[0] is not None else None
    except mysql.connector.Error as err:
        myprint(f"Could not get dwell score for post {post_id} | Error: {err}")
        return None
    finally:
        cursor.close()
        connection.close()


def update_db_post_first_comment_link(post_id: int, link: Optional[str]) -> bool:
    """Stash the external link(s) stripped from a post body at publish time (issue #392, C3) so the
    seed-comment task can deliver them in the author's first comment. Newline-separated for multiple
    links; None clears it."""
    connection = get_db_connection()
    cursor = connection.cursor()
    try:
        cursor.execute(
            "UPDATE posts SET first_comment_link = %s WHERE id = %s",
            (link, post_id)
        )
        connection.commit()
        success = cursor.rowcount == 1
    except mysql.connector.Error as e:
        success = False
        myprint(f"Could not update first comment link for post {post_id}. Error: {e}")
    finally:
        cursor.close()
        connection.close()
    return success


def get_post_first_comment_link(post_id: int) -> Optional[str]:
    """The link(s) held back from a post's body for its first comment, or None (issue #392)."""
    connection = get_db_connection()
    cursor = connection.cursor()
    try:
        cursor.execute("SELECT first_comment_link FROM posts WHERE id = %s", (post_id,))
        row = cursor.fetchone()
        return row[0] if row and row[0] else None
    except mysql.connector.Error as err:
        myprint(f"Could not get first comment link for post {post_id} | Error: {err}")
        return None
    finally:
        cursor.close()
        connection.close()


def get_recent_post_shape_history(user_id: int, limit: int = 10) -> list:
    """Recent posts' SHAPE history — {archetype, hook_style} dicts, most-recent first — fed to the
    shared content framework so a new post rotates away from recently used archetypes/hooks (the
    post-side twin of get_recent_newsletter_blueprint_history)."""
    connection = get_db_connection()
    cursor = connection.cursor(dictionary=True)
    try:
        cursor.execute(
            "SELECT archetype, hook_style FROM posts "
            "WHERE user_id = %s AND archetype IS NOT NULL "
            "ORDER BY id DESC LIMIT %s", (user_id, int(limit)))
        return cursor.fetchall()
    except mysql.connector.Error as err:
        myprint(f"Could not get post shape history for user {user_id} | Error: {err}")
        return []
    finally:
        cursor.close()
        connection.close()


def get_recent_post_texts(user_id: int, limit: int = 20) -> list:
    """Recent post CONTENT (pending/approved/posted, most-recent first) — the post-side dedup
    history (the newsletter's V49 subject dedup applied to posts). Feeds the opener/subject
    avoidance steering and the pre-persist similarity gate in create_text_post. Openers/subjects
    are derived from content on demand, so no new column is needed."""
    connection = get_db_connection()
    cursor = connection.cursor()
    try:
        cursor.execute(
            "SELECT content FROM posts "
            "WHERE user_id = %s AND content IS NOT NULL AND content <> '' "
            "AND status IN ('pending', 'approved', 'posted') "
            "ORDER BY id DESC LIMIT %s", (user_id, int(limit)))
        return [r[0] for r in cursor.fetchall()]
    except mysql.connector.Error as err:
        myprint(f"Could not get recent post texts for user {user_id} | Error: {err}")
        return []
    finally:
        cursor.close()
        connection.close()


def replace_video_url_base(old_base: str, new_base: str, user_id: Optional[int] = None) -> int:
    """Replace old_base URL prefix with new_base in video_url for all matching posts.

    Scoped to user_id when provided. Returns count of updated rows.
    """
    connection = get_db_connection()
    cursor = connection.cursor()
    try:
        if user_id is not None:
            cursor.execute(
                "UPDATE posts SET video_url = REPLACE(video_url, %s, %s) "
                "WHERE video_url LIKE %s AND user_id = %s",
                (old_base, new_base, f"{old_base}%", user_id)
            )
        else:
            cursor.execute(
                "UPDATE posts SET video_url = REPLACE(video_url, %s, %s) WHERE video_url LIKE %s",
                (old_base, new_base, f"{old_base}%")
            )
        connection.commit()
        updated = cursor.rowcount
    except mysql.connector.Error as e:
        updated = 0
        myprint(f"Could not replace video URL base. Error: {e}")
    finally:
        cursor.close()
        connection.close()
    return updated


def get_ready_to_post_posts(pre_post_time: datetime = None, post_time_delta_minutes=20) -> list:
    """Query the database for any pending posts that are scheduled to post now or earlier"""

    now = datetime.now(timezone.utc)
    if pre_post_time is None:
        # Get time for post_time_delta after now
        pre_post_time = now + timedelta(minutes=post_time_delta_minutes)

    yesterday = now - timedelta(days=1)

    myprint(f"Getting post between : {yesterday} and {pre_post_time} (UTC)")

    connection = get_db_connection()
    cursor = connection.cursor()

    try:
        # Get posts that have scheduled time between 24 hours ago and the pre_post_time
        cursor.execute(
            """SELECT p.id, p.scheduled_time, p.user_id 
                FROM posts AS p
                WHERE status = 'approved' AND scheduled_time BETWEEN %s AND %s 
                ORDER BY scheduled_time ASC 
                """,
            (yesterday, pre_post_time,))
        posts = cursor.fetchall()
        # Print the id's ready to post
        myprint(f"Posts ready to post: {[post[0] for post in posts]}")
    except mysql.connector.Error as err:
        myprint(f"Could not get ready to post posts| Error: {err}")
        posts = None
    finally:
        cursor.close()
        connection.close()

    return posts


def get_orphaned_scheduled_posts(lookback_hours: int = 2) -> list:
    """Return posts stuck in 'scheduled' status that never reached 'posted'.

    These arise when Celery tasks are purged on container restart while a post
    has already been transitioned from 'approved' → 'scheduled'. Without this
    recovery query, those posts stay stuck forever.
    """
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=lookback_hours)

    connection = get_db_connection()
    cursor = connection.cursor()
    try:
        cursor.execute(
            """SELECT p.id, p.scheduled_time, p.user_id
               FROM posts AS p
               WHERE status = 'scheduled'
                 AND scheduled_time <= %s
               ORDER BY scheduled_time ASC""",
            (cutoff,),
        )
        posts = cursor.fetchall()
        myprint(f"Orphaned scheduled posts to re-queue: {[p[0] for p in posts]}")
    except mysql.connector.Error as err:
        myprint(f"Could not get orphaned scheduled posts | Error: {err}")
        posts = []
    finally:
        cursor.close()
        connection.close()

    return posts


def get_user_password_pair_by_id(user_id: int):
    connection = get_db_connection()
    cursor = connection.cursor(dictionary=True)

    try:
        cursor.execute("SELECT email, password FROM users WHERE id = %s", (user_id,))

        user_password_pair = cursor.fetchone()
    except mysql.connector.Error as err:
        myprint(f"Could not get user password pair for user id: {user_id} | Error: {err}")
        user_password_pair = None
    finally:
        cursor.close()
        connection.close()

    if user_password_pair:
        return user_password_pair['email'], user_password_pair['password']
    else:
        return None, None


def get_active_user_password_pairs():
    user_password_pairs = []

    active_users = get_active_user_ids()

    for user_id in active_users:
        email, password = get_user_password_pair_by_id(user_id)
        if email and password:
            user_password_pairs.append([email, password])

    return user_password_pairs


def add_linkedin_profile(profile: LinkedInProfile, user_id: Optional[int] = None):
    connection = get_db_connection()
    cursor = connection.cursor()
    try:
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

        connection.commit()
        success = True
    except mysql.connector.Error as err:
        myprint(f"Could not add linkedin profile | Error: {err}")
        success = False
    finally:
        cursor.close()
        connection.close()
    return success


def get_linked_in_profile_by_url(profile_url: str, updated_less_than_days_ago: int = 1):
    connection = get_db_connection()
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
        myprint(f"Could not get linkedin profile by url | Error: {err}")
    finally:
        cursor.close()
        connection.close()

    return profile_data


def get_linked_in_profile_by_email(profile_email: str, updated_less_than_days_ago: int = 1):
    connection = get_db_connection()
    cursor = connection.cursor()

    try:
        cursor.execute("SELECT data FROM profiles WHERE email = %s AND updated_at > NOW() - INTERVAL %s DAY",
                       (profile_email, updated_less_than_days_ago))
        profile_data = cursor.fetchone()
    except mysql.connector.Error as err:
        myprint(f"Could not get linkedin profile data by email | Error: {err}")
        profile_data = None
    finally:
        cursor.close()
        connection.close()

    return profile_data


def get_linked_in_profile_by_user_id(user_id: int, updated_less_than_days_ago: int = 1):
    connection = get_db_connection()
    cursor = connection.cursor()

    try:
        cursor.execute("SELECT data FROM profiles WHERE user_id = %s AND updated_at > NOW() - INTERVAL %s DAY",
                       (user_id, updated_less_than_days_ago))
        profile_data = cursor.fetchone()
    except mysql.connector.Error as err:
        myprint(f"Could not get linkedin profile data by user_id | Error: {err}")
        profile_data = None
    finally:
        cursor.close()
        connection.close()

    return profile_data


def get_profile_synthesis(user_id: int) -> Optional[tuple]:
    """Return the user's cached (synthesis_text, synthesis_generated_at) or None when there is no
    profile row / no synthesis yet. Kept separate from the profile-JSON getters so the small, stable
    voice brief can be read cheaply on every generation call without pulling the full profile blob."""
    connection = get_db_connection()
    cursor = connection.cursor()
    try:
        cursor.execute(
            "SELECT synthesis, synthesis_generated_at FROM profiles WHERE user_id = %s", (user_id,))
        row = cursor.fetchone()
    except mysql.connector.Error as err:
        myprint(f"Could not get profile synthesis for user_id={user_id} | Error: {err}")
        row = None
    finally:
        cursor.close()
        connection.close()

    if not row or row[0] is None:
        return None
    return row[0], row[1]


def set_profile_synthesis(user_id: int, synthesis: str) -> bool:
    """Persist a freshly generated voice synthesis and stamp synthesis_generated_at = NOW() (drives
    the weekly staleness selector). No-op-safe: returns False if the profile row doesn't exist yet."""
    connection = get_db_connection()
    cursor = connection.cursor()
    try:
        cursor.execute(
            "UPDATE profiles SET synthesis = %s, synthesis_generated_at = NOW() WHERE user_id = %s",
            (synthesis, user_id))
        connection.commit()
        success = cursor.rowcount > 0
    except mysql.connector.Error as err:
        myprint(f"Could not set profile synthesis for user_id={user_id} | Error: {err}")
        success = False
    finally:
        cursor.close()
        connection.close()
    return success


def get_user_ids_needing_profile_synthesis(stale_days: int = 7) -> list:
    """User IDs whose cached profile synthesis is MISSING or older than `stale_days` — the work list
    for the weekly refresh task. Only rows that actually have a profile (user_id NOT NULL) qualify."""
    connection = get_db_connection()
    cursor = connection.cursor()
    try:
        cursor.execute(
            "SELECT user_id FROM profiles WHERE user_id IS NOT NULL AND ("
            "synthesis IS NULL OR synthesis_generated_at IS NULL "
            "OR synthesis_generated_at < NOW() - INTERVAL %s DAY)",
            (stale_days,))
        rows = cursor.fetchall()
    except mysql.connector.Error as err:
        myprint(f"Could not get user_ids needing profile synthesis | Error: {err}")
        rows = []
    finally:
        cursor.close()
        connection.close()
    return [row[0] for row in rows]


def remove_linked_in_profile_by_user_id(user_id: int):
    connection = get_db_connection()
    cursor = connection.cursor()

    try:
        cursor.execute("DELETE FROM profiles WHERE user_id = %s", (user_id,))
        connection.commit()
        success = True
    except mysql.connector.Error as err:
        myprint(f"Could not remove linkedin profile by user_id | Error: {err}")
        success = False
    finally:
        cursor.close()
        connection.close()
    return success


def remove_linked_in_profile_by_url(profile_url: str):
    connection = get_db_connection()
    cursor = connection.cursor()

    try:
        cursor.execute("DELETE FROM profiles WHERE profile_url = %s", (profile_url,))
        connection.commit()
        success = True
    except mysql.connector.Error as err:
        myprint(f"Could not remove linkedin profile by url | Error: {err}")
        success = False
    finally:
        cursor.close()
        connection.close()
    return success


def remove_linked_in_profile_by_email(profile_email: str):
    connection = get_db_connection()
    cursor = connection.cursor()

    try:
        cursor.execute("DELETE FROM profiles WHERE email = %s", (profile_email,))
        connection.commit()
        success = True
    except mysql.connector.Error as err:
        myprint(f"Could not remove linkedin profile by email | Error: {err}")
        success = False
    finally:
        cursor.close()
        connection.close()
    return success


def get_post_type_counts(user_id: int):
    """Query the database to get the count of each post_type in the 'posts' table for the given user id."""
    connection = get_db_connection()
    cursor = connection.cursor(dictionary=True)

    try:
        cursor.execute("SELECT post_type, COUNT(*) AS count FROM posts WHERE user_id = %s GROUP BY post_type",
                       (user_id,))
        post_counts = {row['post_type']: row['count'] for row in cursor.fetchall()}
    except mysql.connector.Error as err:
        myprint(f"Could not get post type counts | Error: {err}")
        post_counts = {}
    finally:
        cursor.close()
        connection.close()

    return post_counts


def get_planned_posts_for_current_week(user_id: int = None) -> list[dict]:
    """Return status=planning posts scheduled in the current ISO week."""
    connection = get_db_connection()
    cursor = connection.cursor(dictionary=True)
    try:
        if user_id:
            cursor.execute(
                "SELECT user_id, id, post_type, buyer_stage FROM posts"
                " WHERE status = 'planning' AND user_id = %s"
                " AND YEARWEEK(scheduled_time, 1) = YEARWEEK(NOW(), 1)",
                (user_id,),
            )
        else:
            cursor.execute(
                "SELECT user_id, id, post_type, buyer_stage FROM posts"
                " WHERE status = 'planning'"
                " AND YEARWEEK(scheduled_time, 1) = YEARWEEK(NOW(), 1)"
            )
        planned_content = cursor.fetchall()
    except mysql.connector.Error as err:
        myprint(f"Could not get planned post for current week | Error: {err}")
        planned_content = []
    finally:
        cursor.close()
        connection.close()

    return planned_content


def get_planned_posts_for_next_week(user_id: int = None) -> list[dict]:
    """Return status=planning posts scheduled in the next ISO week.

    Uses NOW() + INTERVAL (7 - WEEKDAY(NOW())) DAY to always land on the
    coming Monday regardless of what day today is, avoiding the +7-day
    same-weekday pitfall.
    """
    connection = get_db_connection()
    cursor = connection.cursor(dictionary=True)
    try:
        if user_id:
            cursor.execute(
                "SELECT user_id, id, post_type, buyer_stage FROM posts"
                " WHERE status = 'planning' AND user_id = %s"
                " AND YEARWEEK(scheduled_time, 1)"
                "   = YEARWEEK(NOW() + INTERVAL (7 - WEEKDAY(NOW())) DAY, 1)",
                (user_id,),
            )
        else:
            cursor.execute(
                "SELECT user_id, id, post_type, buyer_stage FROM posts"
                " WHERE status = 'planning'"
                " AND YEARWEEK(scheduled_time, 1)"
                "   = YEARWEEK(NOW() + INTERVAL (7 - WEEKDAY(NOW())) DAY, 1)"
            )
        planned_content = cursor.fetchall()
    except mysql.connector.Error as err:
        myprint(f"Could not get planned post for next week | Error: {err}")
        planned_content = []
    finally:
        cursor.close()
        connection.close()

    return planned_content


def get_last_planned_post_date_for_user(user_id: int):
    """Query the database to get the last planned post date for the given user."""
    connection = get_db_connection()
    cursor = connection.cursor()

    try:
        cursor.execute(
            "SELECT MAX(scheduled_time) AS last_planned_date FROM posts WHERE user_id = %s AND status != 'rejected'",
            (user_id,))
        last_planned_date = cursor.fetchone()
    except mysql.connector.Error as err:
        myprint(f"Could not get last planned post date for user | Error: {err}")
        last_planned_date = None
    finally:
        cursor.close()
        connection.close()

    return last_planned_date[0] if last_planned_date else None


def get_user_blog_url(user_id: int):
    """Query the database to get the blog URL for the given user."""
    connection = get_db_connection()
    cursor = connection.cursor()

    try:
        cursor.execute("SELECT blog_url FROM users WHERE id = %s", (user_id,))
        blog_url = cursor.fetchone()
    except mysql.connector.Error as err:
        myprint(f"Could not get user blog url | Error: {err}")
        blog_url = None
    finally:
        cursor.close()
        connection.close()

    return blog_url[0] if blog_url else None


def get_user_sitemap_url(user_id: int):
    """Query the database to get the sitemap URL for the given user."""
    connection = get_db_connection()
    cursor = connection.cursor()

    try:
        cursor.execute("SELECT sitemap_url FROM users WHERE id = %s", (user_id,))
        sitemap_url = cursor.fetchone()
    except mysql.connector.Error as err:
        myprint(f"Could not get user sitemap url | Error: {err}")
        sitemap_url = None
    finally:
        cursor.close()
        connection.close()

    return sitemap_url[0] if sitemap_url else None


def get_linkedin_profile_url_by_user_id(user_id: int) -> Optional[str]:
    """Return the user's own LinkedIn profile URL (e.g. https://www.linkedin.com/in/<vanity>/).
    Only the user's own scraped profile carries a non-null user_id in the profiles table."""
    connection = get_db_connection()
    cursor = connection.cursor()

    try:
        cursor.execute("SELECT profile_url FROM profiles WHERE user_id = %s LIMIT 1", (user_id,))
        row = cursor.fetchone()
    except mysql.connector.Error as err:
        myprint(f"Could not get user linkedin profile url | Error: {err}")
        row = None
    finally:
        cursor.close()
        connection.close()

    return row[0] if row else None


_ALLOWED_USER_CLAUSES = frozenset({"email = %s", "blog_url = %s", "sitemap_url = %s"})


def update_user(user_id: int, email: str = None, blog_url: str = None, sitemap_url: str = None) -> bool:
    if not any([email, blog_url, sitemap_url]):
        return False
    connection = get_db_connection()
    cursor = connection.cursor()
    fields, values = [], []
    if email:
        fields.append("email = %s")
        values.append(email)
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
        myprint(f"Could not update user {user_id} | Error: {err}")
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
    connection = get_db_connection()
    cursor = connection.cursor()
    try:
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
        myprint(f"Could not get active user ids | Error: {err}")
        active_user_ids = []
    finally:
        cursor.close()
        connection.close()

    return active_user_ids


def get_user_location(user_id: int) -> tuple[float, float] | None:
    connection = get_db_connection()
    cursor = connection.cursor()
    try:
        cursor.execute("SELECT latitude, longitude FROM users WHERE id = %s", (user_id,))
        row = cursor.fetchone()
    except mysql.connector.Error as err:
        myprint(f"Could not get user location | Error: {err}")
        row = None
    finally:
        cursor.close()
        connection.close()
    return (float(row[0]), float(row[1])) if row and row[0] and row[1] else None


def insert_new_log(user_id: int, action_type: LogActionType, result: LogResultType, post_id: int = None,
                   post_url: str = None, message: str = None):
    connection = get_db_connection()
    cursor = connection.cursor()

    try:
        cursor.execute("""
            INSERT INTO logs (user_id, action_type, post_id, post_url, message, result)
            VALUES (%s, %s, %s, %s, %s, %s)
        """, (user_id, action_type.value, post_id, post_url, message, result.value))

        connection.commit()
        success = cursor.rowcount == 1
    except mysql.connector.Error as err:
        myprint(f"Could not insert new log | Error: {err}")
        success = False
    finally:
        cursor.close()
        connection.close()

    return success


def has_user_commented_on_post_url(user_id: int, post_url: str):
    connection = get_db_connection()
    cursor = connection.cursor()

    try:
        cursor.execute(
            "SELECT COUNT(*) FROM logs WHERE user_id = %s AND post_url = %s AND action_type = %s AND result = %s",
            (user_id, post_url, LogActionType.COMMENT.value, LogResultType.SUCCESS.value))
        count = cursor.fetchone()[0]
    except mysql.connector.Error as err:
        myprint(f"Could not determine if user commented on post url | Error: {err}")
        count = 0
    finally:
        cursor.close()
        connection.close()

    return count > 0


def get_post_url_from_log_for_user(user_id: int, post_id: int):
    connection = get_db_connection()
    cursor = connection.cursor()

    try:
        cursor.execute("""SELECT post_url FROM logs 
            WHERE user_id = %s AND post_id = %s AND action_type = %s AND result = %s  
            ORDER BY created_at DESC 
            LIMIT 1""",
                       (user_id, post_id, LogActionType.POST.value, LogResultType.SUCCESS.value))
        post_url = cursor.fetchone()[0]
    except mysql.connector.Error as err:
        myprint(f"Could not get post url from log for user | Error: {err}")
        post_url = None
    finally:
        cursor.close()
        connection.close()

    return post_url


def get_post_message_from_log_for_user(user_id: int, post_id: int):
    connection = get_db_connection()
    cursor = connection.cursor()

    try:
        cursor.execute("""SELECT message FROM logs 
            WHERE user_id = %s AND post_id = %s AND action_type = %s AND result = %s
            ORDER BY created_at DESC 
            LIMIT 1""",
                       (user_id, post_id, LogActionType.POST.value, LogResultType.SUCCESS.value))
        message = cursor.fetchone()[0]
    except mysql.connector.Error as err:
        myprint(f"Could not get post message from log for user | Error: {err}")
        message = None
    finally:
        cursor.close()
        connection.close()

    return message


def has_engaged_url_with_x_days(user_id: int, post_url: str, days: int):
    connection = get_db_connection()
    cursor = connection.cursor()

    try:
        cursor.execute(
            "SELECT COUNT(*) FROM logs WHERE user_id = %s AND post_url = %s AND action_type = %s AND result = %s AND created_at > NOW() - INTERVAL %s DAY",
            (user_id, post_url, LogActionType.ENGAGED.value, LogResultType.SUCCESS.value, days))
        count = cursor.fetchone()[0]
    except mysql.connector.Error as err:
        myprint(f"Could not determine if user engaged with url with x days | Error: {err}")
        count = 0
    finally:
        cursor.close()
        connection.close()

    return count > 0


def get_dm_history_for_profile(user_id: int, profile_url: str) -> list[str]:
    """Return all DM messages previously sent by user_id to profile_url, oldest first."""
    connection = get_db_connection()
    cursor = connection.cursor()
    try:
        cursor.execute(
            "SELECT message FROM logs WHERE user_id = %s AND post_url = %s AND action_type = %s ORDER BY created_at ASC",
            (user_id, profile_url, LogActionType.DM.value),
        )
        rows = cursor.fetchall()
    except mysql.connector.Error as err:
        myprint(f"Could not get DM history for profile | Error: {err}")
        rows = []
    finally:
        cursor.close()
        connection.close()
    return [row[0] for row in rows if row[0]]


def get_post_status(post_id: int) -> str | None:
    """Return the current status string of a post, or None if not found."""
    connection = get_db_connection()
    cursor = connection.cursor()
    try:
        cursor.execute("SELECT status FROM posts WHERE id = %s", (post_id,))
        row = cursor.fetchone()
    except mysql.connector.Error as err:
        myprint(f"Could not get post status | Error: {err}")
        row = None
    finally:
        cursor.close()
        connection.close()
    return row[0] if row else None


def get_company_linked_in_url_for_user(user_id: int):
    connection = get_db_connection()
    cursor = connection.cursor()

    try:
        cursor.execute("SELECT company_linked_in_url FROM users WHERE id = %s", (user_id,))
        company_linked_in_url = cursor.fetchone()
    except mysql.connector.Error as err:
        myprint(f"Could not get user company linked in url | Error: {err}")
        company_linked_in_url = None
    finally:
        cursor.close()
        connection.close()

    return company_linked_in_url[0] if company_linked_in_url else None


def update_company_linked_in_url_for_user(user_id: int, company_linked_in_url: Optional[str]) -> bool:
    """Set (or clear, when None/empty) the user's LinkedIn company page URL used by the
    monthly company-page invite automation."""
    connection = get_db_connection()
    cursor = connection.cursor()
    try:
        cursor.execute(
            "UPDATE users SET company_linked_in_url = %s WHERE id = %s",
            (company_linked_in_url or None, user_id),
        )
        connection.commit()
        return cursor.rowcount >= 0
    except mysql.connector.Error as err:
        myprint(f"Could not update company linked in url for user {user_id} | Error: {err}")
        return False
    finally:
        cursor.close()
        connection.close()


def get_recent_logs(user_id: int, limit: int = 20) -> list:
    connection = get_db_connection()
    cursor = connection.cursor(dictionary=True)

    try:
        cursor.execute(
            """SELECT id, action_type, result, post_id, post_url, message, created_at
               FROM logs WHERE user_id = %s ORDER BY created_at DESC LIMIT %s""",
            (user_id, limit)
        )
        rows = cursor.fetchall()
    except mysql.connector.Error as err:
        myprint(f"Could not get recent logs | Error: {err}")
        rows = []
    finally:
        cursor.close()
        connection.close()

    return rows


def update_user_linkedin_password(user_id: int, password: str) -> bool:
    """Store the user's LinkedIn login password for Selenium-driven automation.

    The password must be stored reversibly (not hashed) because Selenium types
    it directly into the LinkedIn login form. Only call this from authenticated
    API endpoints — never expose the value in any response payload.
    """
    connection = get_db_connection()
    cursor = connection.cursor()
    try:
        cursor.execute(
            "UPDATE users SET password = %s WHERE id = %s",
            (password, user_id),
        )
        connection.commit()
        return cursor.rowcount >= 0
    except mysql.connector.Error as err:
        myprint(f"Could not update LinkedIn password for user_id {user_id} | Error: {err}")
        return False
    finally:
        cursor.close()
        connection.close()


def update_user_settings(user_id: int, blog_url: str = None, sitemap_url: str = None) -> bool:
    connection = get_db_connection()
    cursor = connection.cursor()

    try:
        cursor.execute(
            "UPDATE users SET blog_url = %s, sitemap_url = %s WHERE id = %s",
            (blog_url, sitemap_url, user_id)
        )
        connection.commit()
        success = cursor.rowcount >= 0
    except mysql.connector.Error as err:
        myprint(f"Could not update user settings | Error: {err}")
        success = False
    finally:
        cursor.close()
        connection.close()

    return success


# ---------------------------------------------------------------------------
# PIN authentication
# ---------------------------------------------------------------------------

def create_pin_for_email(email: str, pin_hash: str) -> bool:
    connection = get_db_connection()
    cursor = connection.cursor()
    try:
        cursor.execute("DELETE FROM email_pin_auth WHERE email = %s AND used = 0", (email,))
        expires_at = datetime.now(timezone.utc) + timedelta(minutes=10)
        cursor.execute(
            "INSERT INTO email_pin_auth (email, pin, expires_at) VALUES (%s, %s, %s)",
            (email, pin_hash, expires_at),
        )
        connection.commit()
        return cursor.rowcount == 1
    except mysql.connector.Error as err:
        myprint(f"Could not create PIN for {email} | Error: {err}")
        return False
    finally:
        cursor.close()
        connection.close()


def delete_pin_for_email(email: str) -> None:
    """Remove all unused PINs for an email — called when email send fails after DB write."""
    connection = get_db_connection()
    cursor = connection.cursor()
    try:
        cursor.execute("DELETE FROM email_pin_auth WHERE email = %s AND used = 0", (email,))
        connection.commit()
    except mysql.connector.Error as err:
        myprint(f"Could not delete PIN for {email} | Error: {err}")
    finally:
        cursor.close()
        connection.close()


def verify_pin_for_email(email: str, pin_hash: str) -> bool:
    connection = get_db_connection()
    cursor = connection.cursor(dictionary=True)
    try:
        cursor.execute(
            """SELECT id FROM email_pin_auth
               WHERE email = %s AND pin = %s AND used = 0 AND expires_at > %s
               ORDER BY id DESC LIMIT 1""",
            (email, pin_hash, datetime.now(timezone.utc)),
        )
        row = cursor.fetchone()
        if row:
            cursor.execute("UPDATE email_pin_auth SET used = 1 WHERE id = %s", (row['id'],))
            connection.commit()
            return True
        return False
    except mysql.connector.Error as err:
        myprint(f"Could not verify PIN for {email} | Error: {err}")
        return False
    finally:
        cursor.close()
        connection.close()


# ---------------------------------------------------------------------------
# Session management
# ---------------------------------------------------------------------------

def create_session(user_id: int) -> Optional[str]:
    import secrets
    token = secrets.token_hex(32)
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(hours=SESSION_IDLE_HOURS)
    connection = get_db_connection()
    cursor = connection.cursor()
    try:
        cursor.execute(
            "INSERT INTO sessions (session_token, user_id, expires_at) VALUES (%s, %s, %s)",
            (token, user_id, expires_at),
        )
        cursor.execute(
            "UPDATE users SET last_login = %s WHERE id = %s",
            (now, user_id),
        )
        connection.commit()
        return token
    except mysql.connector.Error as err:
        myprint(f"Could not create session for user_id {user_id} | Error: {err}")
        return None
    finally:
        cursor.close()
        connection.close()


def get_session_user_id(token: str) -> Optional[int]:
    """Validate a session token and, if live, slide its expiry forward.

    Sliding idle window (SESSION_IDLE_HOURS) so an active user never has to request a new PIN,
    bounded by an absolute cap (SESSION_ABSOLUTE_MAX_DAYS from first login) for security.
    Expired/unknown tokens return None so the caller forces a fresh PIN."""
    connection = get_db_connection()
    cursor = connection.cursor(dictionary=True)
    try:
        now = datetime.now(timezone.utc)
        cursor.execute(
            "SELECT user_id, created_at FROM sessions "
            "WHERE session_token = %s AND expires_at > %s",
            (token, now),
        )
        row = cursor.fetchone()
        if not row:
            return None

        new_expiry = now + timedelta(hours=SESSION_IDLE_HOURS)
        created_at = row.get('created_at')
        if created_at is not None:
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc)
            absolute_cap = created_at + timedelta(days=SESSION_ABSOLUTE_MAX_DAYS)
            if new_expiry > absolute_cap:
                new_expiry = absolute_cap
        cursor.execute(
            "UPDATE sessions SET expires_at = %s WHERE session_token = %s",
            (new_expiry, token),
        )
        connection.commit()
        return row['user_id']
    except mysql.connector.Error as err:
        myprint(f"Could not validate session token | Error: {err}")
        return None
    finally:
        cursor.close()
        connection.close()


def delete_session(token: str) -> bool:
    connection = get_db_connection()
    cursor = connection.cursor()
    try:
        cursor.execute("DELETE FROM sessions WHERE session_token = %s", (token,))
        connection.commit()
        return True
    except mysql.connector.Error as err:
        myprint(f"Could not delete session | Error: {err}")
        return False
    finally:
        cursor.close()
        connection.close()


# ---------------------------------------------------------------------------
# User helpers
# ---------------------------------------------------------------------------

def add_user_by_email(email: str) -> Optional[int]:
    from cqc_lem.utilities.env_constants import FREE_TRIAL_DAYS
    now = datetime.now(timezone.utc)
    trial_ends = now + timedelta(days=FREE_TRIAL_DAYS)
    connection = get_db_connection()
    cursor = connection.cursor()
    try:
        cursor.execute(
            """INSERT INTO users
               (email, subscription_status, subscription_tier, trial_started_at, trial_ends_at)
               VALUES (%s, 'trial', 'free_trial', %s, %s)""",
            (email, now, trial_ends),
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
            myprint(f"Stripe customer creation non-fatal error for {email}: {se}")
        return user_id
    except mysql.connector.Error as err:
        if err.errno == errorcode.ER_DUP_ENTRY:
            return get_user_id(email)
        myprint(f"Could not create user for {email} | Error: {err}")
        return None
    finally:
        cursor.close()
        connection.close()


def get_user_email(user_id: int) -> Optional[str]:
    connection = get_db_connection()
    cursor = connection.cursor(dictionary=True)
    try:
        cursor.execute("SELECT email FROM users WHERE id = %s", (user_id,))
        row = cursor.fetchone()
        return row['email'] if row else None
    except mysql.connector.Error as err:
        myprint(f"Could not get email for user_id {user_id} | Error: {err}")
        return None
    finally:
        cursor.close()
        connection.close()


def get_user_token_info(user_id: int) -> Optional[dict]:
    connection = get_db_connection()
    cursor = connection.cursor(dictionary=True)
    try:
        cursor.execute(
            """SELECT access_token, access_token_expires_in, access_token_created_at,
                      refresh_token, refresh_token_expires_in, refresh_token_created_at
               FROM users WHERE id = %s""",
            (user_id,),
        )
        return cursor.fetchone()
    except mysql.connector.Error as err:
        myprint(f"Could not get token info for user_id {user_id} | Error: {err}")
        return None
    finally:
        cursor.close()
        connection.close()


def update_user_access_token(
    user_id: int,
    access_token: str,
    expires_in: int,
    refresh_token: Optional[str] = None,
    refresh_token_expires_in: Optional[int] = None,
) -> bool:
    now = datetime.now(timezone.utc)
    connection = get_db_connection()
    cursor = connection.cursor()
    try:
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
                (access_token, expires_in, now,
                 refresh_token, refresh_token_expires_in, now, user_id),
            )
        else:
            cursor.execute(
                """UPDATE users SET
                       access_token = %s,
                       access_token_expires_in = %s,
                       access_token_created_at = %s
                   WHERE id = %s""",
                (access_token, expires_in, now, user_id),
            )
        connection.commit()
        return cursor.rowcount > 0
    except mysql.connector.Error as err:
        myprint(f"Could not update access token for user_id {user_id} | Error: {err}")
        return False
    finally:
        cursor.close()
        connection.close()


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
    connection = get_db_connection()
    cursor = connection.cursor()
    try:
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
                (linked_sub_id, linkedin_email or None, access_token, expires_in, now,
                 refresh_token, refresh_token_expires_in, now, user_id),
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
                (linked_sub_id, linkedin_email or None, access_token, expires_in, now, user_id),
            )
        connection.commit()
        return cursor.rowcount > 0
    except mysql.connector.Error as err:
        myprint(f"Could not update LinkedIn token for user_id {user_id} | Error: {err}")
        return False
    finally:
        cursor.close()
        connection.close()


def update_linkedin_connection_status(user_id: int, status: str) -> bool:
    """Set linkedin_connection_status to 'connected', 'expired', or 'disconnected'."""
    connection = get_db_connection()
    cursor = connection.cursor()
    try:
        cursor.execute(
            "UPDATE users SET linkedin_connection_status = %s WHERE id = %s",
            (status, user_id),
        )
        connection.commit()
        return cursor.rowcount > 0
    except mysql.connector.Error as err:
        myprint(f"Could not update linkedin_connection_status for user_id {user_id} | Error: {err}")
        return False
    finally:
        cursor.close()
        connection.close()


def get_user_subscription_info(user_id: int) -> Optional[dict]:
    """Return subscription fields for the given user."""
    connection = get_db_connection()
    cursor = connection.cursor(dictionary=True)
    try:
        cursor.execute(
            """SELECT subscription_status, subscription_tier,
                      trial_started_at, trial_ends_at,
                      stripe_customer_id, stripe_subscription_id
               FROM users WHERE id = %s""",
            (user_id,),
        )
        return cursor.fetchone()
    except mysql.connector.Error as err:
        myprint(f"Could not get subscription info for user_id {user_id} | Error: {err}")
        return None
    finally:
        cursor.close()
        connection.close()


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
    connection = get_db_connection()
    cursor = connection.cursor()
    try:
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
        connection.commit()
        return cursor.rowcount > 0
    except mysql.connector.Error as err:
        myprint(f"Could not update subscription from Stripe for customer {stripe_customer_id} | Error: {err}")
        return False
    finally:
        cursor.close()
        connection.close()


def get_users_with_stripe_subscriptions() -> list[dict]:
    """Return all users that have a Stripe subscription ID (for periodic sync)."""
    connection = get_db_connection()
    cursor = connection.cursor(dictionary=True)
    try:
        cursor.execute(
            """SELECT id, stripe_customer_id, stripe_subscription_id,
                      subscription_status, subscription_tier
               FROM users
               WHERE stripe_subscription_id IS NOT NULL
                 AND subscription_status IN ('active', 'past_due')"""
        )
        return cursor.fetchall() or []
    except mysql.connector.Error as err:
        myprint(f"Could not fetch Stripe subscribers | Error: {err}")
        return []
    finally:
        cursor.close()
        connection.close()


def get_user_preferences(user_id: int) -> dict:
    """Return user preference fields with safe defaults.

    Defaults auto_schedule_posts=True so new users' content is automatically
    queued without requiring manual opt-in.
    """
    _defaults: dict = {"last_login_inactivate_delay": None, "auto_schedule_posts": True}
    connection = get_db_connection()
    cursor = connection.cursor(dictionary=True)
    try:
        cursor.execute(
            "SELECT last_login_inactivate_delay, auto_schedule_posts FROM users WHERE id = %s",
            (user_id,),
        )
        row = cursor.fetchone()
        return row if row is not None else _defaults
    except mysql.connector.Error as err:
        myprint(f"Could not get preferences for user_id {user_id} | Error: {err}")
        return _defaults
    finally:
        cursor.close()
        connection.close()


def update_user_preferences(
    user_id: int,
    inactivate_delay: Optional[int],
    auto_schedule_posts: bool,
) -> bool:
    """Persist user-configurable inactivity delay (None = never) and auto-schedule flag."""
    connection = get_db_connection()
    cursor = connection.cursor()
    try:
        cursor.execute(
            """UPDATE users
               SET last_login_inactivate_delay = %s,
                   auto_schedule_posts = %s
               WHERE id = %s""",
            (inactivate_delay, 1 if auto_schedule_posts else 0, user_id),
        )
        connection.commit()
        # rowcount==0 means the row existed but values were unchanged — still a success
        return cursor.rowcount >= 0
    except mysql.connector.Error as err:
        myprint(f"Could not update preferences for user_id {user_id} | Error: {err}")
        return False
    finally:
        cursor.close()
        connection.close()


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
    "min_reactions": None, "max_post_age_hours": 24, "reply_to_own_comments": True,
    "max_comments_per_day": 20, "max_dms_per_day": 20, "max_invites_per_day": 10,
    "connection_request_mode": "auto_approve",
    "default_buyer_stage": None,
    "default_video_quality": "standard",
    "reply_check_mode": "event", "reply_sweeps_per_day": 2, "reply_max_post_age_days": 2,
    "feed_fallback_when_empty": True, "link_in_first_comment": True,
}
_ENGAGEMENT_JSON_FIELDS = ("include_topics", "exclude_topics", "include_keywords",
                           "exclude_keywords", "include_authors", "exclude_authors", "post_types",
                           "focus_topics")
_ENGAGEMENT_BOOL_FIELDS = ("use_emojis", "use_hashtags", "reply_to_own_comments",
                           "feed_fallback_when_empty", "link_in_first_comment")
_ENGAGEMENT_COLS = ("tone", "comment_length", "comment_style", "use_emojis", "use_hashtags",
                    "include_topics", "exclude_topics", "include_keywords", "exclude_keywords",
                    "include_authors", "exclude_authors", "post_types", "focus_topics",
                    "business_goals", "personal_goals", "min_reactions",
                    "max_post_age_hours", "reply_to_own_comments", "max_comments_per_day",
                    "max_dms_per_day", "max_invites_per_day", "connection_request_mode",
                    "default_buyer_stage", "default_video_quality",
                    "reply_check_mode", "reply_sweeps_per_day", "reply_max_post_age_days",
                    "feed_fallback_when_empty", "link_in_first_comment")

VALID_VIDEO_QUALITIES = ("standard", "premium", "premium_top")
VALID_REPLY_MODES = ("event", "scheduled", "off")
# Approval posture for the proactive connect flow (issue #398 owner review).
VALID_CONNECTION_REQUEST_MODES = ("auto_approve", "pre_review")
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


def get_engagement_preferences(user_id: int) -> dict:
    """Return the user's engagement preferences (voice/targeting/caps) with code-level
    defaults when no row exists — so behaviour is unchanged until the user customizes."""
    connection = get_db_connection()
    cursor = connection.cursor(dictionary=True)
    try:
        cursor.execute(
            f"SELECT {', '.join(_ENGAGEMENT_COLS)} FROM engagement_preferences WHERE user_id = %s",
            (user_id,))
        row = cursor.fetchone()
        if row is None:
            return dict(_ENGAGEMENT_DEFAULTS)
        for f in _ENGAGEMENT_JSON_FIELDS:
            row[f] = _coerce_json_list(row.get(f))
        for f in _ENGAGEMENT_BOOL_FIELDS:
            row[f] = bool(row.get(f))
        return row
    except mysql.connector.Error as err:
        myprint(f"Could not get engagement prefs for user_id {user_id} | Error: {err}")
        return dict(_ENGAGEMENT_DEFAULTS)
    finally:
        cursor.close()
        connection.close()


def update_engagement_preferences(user_id: int, prefs: dict) -> bool:
    """Upsert the user's engagement preferences (INSERT ... ON DUPLICATE KEY UPDATE)."""
    merged = {**_ENGAGEMENT_DEFAULTS, **{k: v for k, v in prefs.items() if k in _ENGAGEMENT_DEFAULTS}}

    # Clamp/validate reply-check config so a bad value can't overflow a column and roll back the
    # WHOLE single-row upsert (the V52 tone incident). Bad mode → the safe default; out-of-range
    # numbers → clamped to bounds.
    if merged.get("reply_check_mode") not in VALID_REPLY_MODES:
        merged["reply_check_mode"] = "event"
    if merged.get("connection_request_mode") not in VALID_CONNECTION_REQUEST_MODES:
        merged["connection_request_mode"] = "auto_approve"
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
    connection = get_db_connection()
    cursor = connection.cursor()
    try:
        cursor.execute(
            f"INSERT INTO engagement_preferences (user_id, {', '.join(_ENGAGEMENT_COLS)}) "
            f"VALUES ({placeholders}) ON DUPLICATE KEY UPDATE {updates}", values)
        connection.commit()
        return cursor.rowcount >= 0
    except mysql.connector.Error as err:
        myprint(f"Could not update engagement prefs for user_id {user_id} | Error: {err}")
        return False
    finally:
        cursor.close()
        connection.close()


def get_or_create_reply_inbound_token(user_id: int) -> Optional[str]:
    """The user's PERSISTENT inbound token for the comment-notification forwarding address
    (reply+<token>@parse-domain). Minted once and stored on the users row so the Gmail forward
    filter the user sets up keeps resolving to them. Returns None only on DB error."""
    connection = get_db_connection()
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
        myprint(f"Could not get/create reply inbound token for user_id {user_id} | Error: {err}")
        return None
    finally:
        cursor.close()
        connection.close()


def get_user_id_by_reply_token(token: str) -> Optional[int]:
    """Reverse lookup for the comment-notification webhook: token → user_id (unique index)."""
    if not token:
        return None
    connection = get_db_connection()
    cursor = connection.cursor()
    try:
        cursor.execute("SELECT id FROM users WHERE reply_inbound_token = %s", (token,))
        row = cursor.fetchone()
        return row[0] if row else None
    except mysql.connector.Error as err:
        myprint(f"Could not look up user by reply token | Error: {err}")
        return None
    finally:
        cursor.close()
        connection.close()


def get_users_with_reply_mode(mode: str) -> list:
    """user_ids whose engagement prefs set reply_check_mode = mode (drives the scheduled sweep
    dispatcher). Users with no prefs row default to 'event', so they never appear for 'scheduled'."""
    connection = get_db_connection()
    cursor = connection.cursor()
    try:
        cursor.execute("SELECT user_id FROM engagement_preferences WHERE reply_check_mode = %s", (mode,))
        return [r[0] for r in cursor.fetchall()]
    except mysql.connector.Error as err:
        myprint(f"Could not get users with reply mode {mode} | Error: {err}")
        return []
    finally:
        cursor.close()
        connection.close()


def _count_actions_today(user_id: int, action_type: "LogActionType") -> int:
    connection = get_db_connection()
    cursor = connection.cursor()
    try:
        cursor.execute(
            "SELECT COUNT(*) FROM logs WHERE user_id=%s AND action_type=%s AND result=%s "
            "AND created_at >= CURDATE()",
            (user_id, str(action_type), str(LogResultType.SUCCESS)))
        r = cursor.fetchone()
        return int(r[0]) if r else 0
    except mysql.connector.Error as err:
        myprint(f"Could not count actions for user_id {user_id} | Error: {err}")
        return 0
    finally:
        cursor.close()
        connection.close()


def count_comments_today(user_id: int) -> int:
    return _count_actions_today(user_id, LogActionType.COMMENT)


def count_dms_sent_today(user_id: int) -> int:
    return _count_actions_today(user_id, LogActionType.DM)


# --- Scheduled 1:1 DMs (issue #306) — mirrors the post scheduler ---
_SCHED_DM_COLS = ("id", "user_id", "recipient_profile_url", "recipient_name", "message",
                  "scheduled_time", "status", "created_at", "updated_at")


def insert_scheduled_dm(user_id: int, recipient_profile_url: str, message: str,
                        scheduled_time: datetime, recipient_name: str = None,
                        status: "ScheduledDmStatus" = ScheduledDmStatus.PENDING) -> Optional[int]:
    connection = get_db_connection()
    cursor = connection.cursor()
    try:
        cursor.execute(
            "INSERT INTO scheduled_dms (user_id, recipient_profile_url, recipient_name, message, "
            "scheduled_time, status) VALUES (%s,%s,%s,%s,%s,%s)",
            (user_id, recipient_profile_url, recipient_name, message, scheduled_time, str(status)))
        connection.commit()
        return cursor.lastrowid
    except mysql.connector.Error as err:
        myprint(f"Could not insert scheduled DM for user_id {user_id} | Error: {err}")
        return None
    finally:
        cursor.close()
        connection.close()


def get_scheduled_dm(dm_id: int) -> Optional[dict]:
    connection = get_db_connection()
    cursor = connection.cursor(dictionary=True)
    try:
        cursor.execute(f"SELECT {', '.join(_SCHED_DM_COLS)} FROM scheduled_dms WHERE id = %s", (dm_id,))
        return cursor.fetchone()
    except mysql.connector.Error as err:
        myprint(f"Could not get scheduled DM {dm_id} | Error: {err}")
        return None
    finally:
        cursor.close()
        connection.close()


def get_scheduled_dm_user_id(dm_id: int) -> Optional[int]:
    row = get_scheduled_dm(dm_id)
    return row["user_id"] if row else None


def get_scheduled_dms(user_id: int, status_filter: str = None, page: int = 1,
                      page_size: int = 25, sort_order: str = "asc") -> dict:
    """Paginated list of a user's scheduled DMs (mirrors the posts list response)."""
    order = "DESC" if str(sort_order).lower() == "desc" else "ASC"
    where = "WHERE user_id = %s"
    params: list = [user_id]
    if status_filter:
        where += " AND status = %s"
        params.append(status_filter)
    offset = max(0, (max(1, page) - 1) * page_size)
    connection = get_db_connection()
    cursor = connection.cursor(dictionary=True)
    try:
        cursor.execute(f"SELECT COUNT(*) AS c FROM scheduled_dms {where}", tuple(params))
        total = int(cursor.fetchone()["c"])
        cursor.execute(
            f"SELECT {', '.join(_SCHED_DM_COLS)} FROM scheduled_dms {where} "
            f"ORDER BY scheduled_time {order} LIMIT %s OFFSET %s",
            tuple(params + [page_size, offset]))
        rows = cursor.fetchall()
        for r in rows:
            if isinstance(r.get("scheduled_time"), datetime):
                r["scheduled_time"] = r["scheduled_time"].isoformat()
            for k in ("created_at", "updated_at"):
                if isinstance(r.get(k), datetime):
                    r[k] = r[k].isoformat()
        return {"dms": rows, "total": total, "page": page, "page_size": page_size}
    except mysql.connector.Error as err:
        myprint(f"Could not list scheduled DMs for user_id {user_id} | Error: {err}")
        return {"dms": [], "total": 0, "page": page, "page_size": page_size}
    finally:
        cursor.close()
        connection.close()


def get_due_scheduled_dms(post_time_delta_minutes: int = 20) -> list:
    """Approved DMs whose scheduled_time is at or before now+delta. Deliberately NO lower bound:
    an approved DM can drift arbitrarily far past its slot (e.g. deferred repeatedly by the daily
    DM cap) and must stay eligible until sent/canceled. Oldest first so overdue DMs drain in order.
    Returns (id, scheduled_time, user_id) tuples."""
    now = datetime.now(timezone.utc)
    window_end = now + timedelta(minutes=post_time_delta_minutes)
    connection = get_db_connection()
    cursor = connection.cursor()
    try:
        cursor.execute(
            "SELECT id, scheduled_time, user_id FROM scheduled_dms "
            "WHERE status = 'approved' AND scheduled_time <= %s "
            "ORDER BY scheduled_time ASC",
            (window_end,))
        return cursor.fetchall()
    except mysql.connector.Error as err:
        myprint(f"Could not get due scheduled DMs | Error: {err}")
        return []
    finally:
        cursor.close()
        connection.close()


def get_orphaned_scheduled_dms(lookback_hours: int = 2) -> list:
    """DMs stuck in 'scheduled' whose send task was lost (e.g. Celery queue purged on container
    restart) before reaching sent/failed. Mirrors get_orphaned_scheduled_posts — the lookback gap
    avoids racing a task that is still in flight. Returns (id, scheduled_time, user_id) tuples."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
    connection = get_db_connection()
    cursor = connection.cursor()
    try:
        cursor.execute(
            "SELECT id, scheduled_time, user_id FROM scheduled_dms "
            "WHERE status = 'scheduled' AND scheduled_time <= %s "
            "ORDER BY scheduled_time ASC",
            (cutoff,))
        return cursor.fetchall()
    except mysql.connector.Error as err:
        myprint(f"Could not get orphaned scheduled DMs | Error: {err}")
        return []
    finally:
        cursor.close()
        connection.close()


def update_scheduled_dm_status(dm_id: int, status: "ScheduledDmStatus") -> bool:
    connection = get_db_connection()
    cursor = connection.cursor()
    try:
        cursor.execute("UPDATE scheduled_dms SET status = %s WHERE id = %s", (str(status), dm_id))
        connection.commit()
        return cursor.rowcount >= 0
    except mysql.connector.Error as err:
        myprint(f"Could not update scheduled DM {dm_id} status | Error: {err}")
        return False
    finally:
        cursor.close()
        connection.close()


def update_scheduled_dm(dm_id: int, recipient_profile_url: str = None, recipient_name: str = None,
                        message: str = None, scheduled_time: datetime = None,
                        status: "ScheduledDmStatus" = None) -> bool:
    fields, params = [], []
    for col, val in (("recipient_profile_url", recipient_profile_url), ("recipient_name", recipient_name),
                     ("message", message), ("scheduled_time", scheduled_time)):
        if val is not None:
            fields.append(f"{col} = %s")
            params.append(val)
    if status is not None:
        fields.append("status = %s")
        params.append(str(status))
    if not fields:
        return False
    params.append(dm_id)
    connection = get_db_connection()
    cursor = connection.cursor()
    try:
        cursor.execute(f"UPDATE scheduled_dms SET {', '.join(fields)} WHERE id = %s", tuple(params))
        connection.commit()
        return cursor.rowcount >= 0
    except mysql.connector.Error as err:
        myprint(f"Could not update scheduled DM {dm_id} | Error: {err}")
        return False
    finally:
        cursor.close()
        connection.close()


def count_invites_sent_today(user_id: int) -> int:
    """Invitations actually sent today, counted as a COMBINED daily budget (issue #398 owner review):
    both the reactive profile-viewer flow and the proactive connect flow send via invite_to_connect_now,
    which logs an ENGAGED/SUCCESS row with CONNECTION_REQUEST_SENT_MESSAGE on every real send. Counting
    those immutable logs (by created_at) covers both flows without double-counting a proactive send (which
    also has a connection_requests row) and avoids the mutable connection_requests.updated_at clock."""
    connection = get_db_connection()
    cursor = connection.cursor()
    try:
        cursor.execute(
            "SELECT COUNT(*) FROM logs WHERE user_id=%s AND action_type=%s AND result=%s "
            "AND message=%s AND created_at >= CURDATE()",
            (user_id, LogActionType.ENGAGED.value, LogResultType.SUCCESS.value,
             CONNECTION_REQUEST_SENT_MESSAGE))
        r = cursor.fetchone()
        return int(r[0]) if r else 0
    except mysql.connector.Error as err:
        myprint(f"Could not count invites for user_id {user_id} | Error: {err}")
        return 0
    finally:
        cursor.close()
        connection.close()


# --- Proactive connection requests (issue #398) — approval-gated, daily-capped; reuses invite_to_connect ---
_CONN_REQ_COLS = ("id", "user_id", "recipient_profile_url", "recipient_name", "message",
                  "status", "created_at", "updated_at")


def insert_connection_request(user_id: int, recipient_profile_url: str, message: str = None,
                              recipient_name: str = None,
                              status: "ConnectionRequestStatus" = ConnectionRequestStatus.PENDING
                              ) -> Optional[int]:
    connection = get_db_connection()
    cursor = connection.cursor()
    try:
        cursor.execute(
            "INSERT INTO connection_requests (user_id, recipient_profile_url, recipient_name, "
            "message, status) VALUES (%s,%s,%s,%s,%s)",
            (user_id, recipient_profile_url, recipient_name, message, str(status)))
        connection.commit()
        return cursor.lastrowid
    except mysql.connector.Error as err:
        myprint(f"Could not insert connection request for user_id {user_id} | Error: {err}")
        return None
    finally:
        cursor.close()
        connection.close()


def get_connection_request(request_id: int) -> Optional[dict]:
    connection = get_db_connection()
    cursor = connection.cursor(dictionary=True)
    try:
        cursor.execute(
            f"SELECT {', '.join(_CONN_REQ_COLS)} FROM connection_requests WHERE id = %s", (request_id,))
        return cursor.fetchone()
    except mysql.connector.Error as err:
        myprint(f"Could not get connection request {request_id} | Error: {err}")
        return None
    finally:
        cursor.close()
        connection.close()


def get_connection_request_user_id(request_id: int) -> Optional[int]:
    row = get_connection_request(request_id)
    return row["user_id"] if row else None


def get_connection_requests(user_id: int, status_filter: str = None, page: int = 1,
                            page_size: int = 25, sort_order: str = "desc") -> dict:
    """Paginated list of a user's connection requests (mirrors get_scheduled_dms)."""
    order = "ASC" if str(sort_order).lower() == "asc" else "DESC"
    where = "WHERE user_id = %s"
    params: list = [user_id]
    if status_filter:
        where += " AND status = %s"
        params.append(status_filter)
    offset = max(0, (max(1, page) - 1) * page_size)
    connection = get_db_connection()
    cursor = connection.cursor(dictionary=True)
    try:
        cursor.execute(f"SELECT COUNT(*) AS c FROM connection_requests {where}", tuple(params))
        total = int(cursor.fetchone()["c"])
        cursor.execute(
            f"SELECT {', '.join(_CONN_REQ_COLS)} FROM connection_requests {where} "
            f"ORDER BY created_at {order} LIMIT %s OFFSET %s",
            tuple(params + [page_size, offset]))
        rows = cursor.fetchall()
        for r in rows:
            for k in ("created_at", "updated_at"):
                if isinstance(r.get(k), datetime):
                    r[k] = r[k].isoformat()
        return {"requests": rows, "total": total, "page": page, "page_size": page_size}
    except mysql.connector.Error as err:
        myprint(f"Could not list connection requests for user_id {user_id} | Error: {err}")
        return {"requests": [], "total": 0, "page": page, "page_size": page_size}
    finally:
        cursor.close()
        connection.close()


def get_approved_connection_requests() -> list:
    """Approved connection requests waiting to be sent, oldest first. Returns (id, user_id) tuples.
    The daily cap is enforced by the scanner/send task, not here."""
    connection = get_db_connection()
    cursor = connection.cursor()
    try:
        cursor.execute(
            "SELECT id, user_id FROM connection_requests WHERE status = 'approved' "
            "ORDER BY created_at ASC")
        return cursor.fetchall()
    except mysql.connector.Error as err:
        myprint(f"Could not get approved connection requests | Error: {err}")
        return []
    finally:
        cursor.close()
        connection.close()


def get_orphaned_connection_requests(lookback_hours: int = 2) -> list:
    """Requests stuck in 'sending' whose send task was lost (e.g. Celery queue purged on restart).
    Mirrors get_orphaned_scheduled_dms — the lookback gap avoids racing an in-flight task.
    Returns (id, user_id) tuples."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
    connection = get_db_connection()
    cursor = connection.cursor()
    try:
        cursor.execute(
            "SELECT id, user_id FROM connection_requests WHERE status = 'sending' "
            "AND updated_at <= %s ORDER BY updated_at ASC",
            (cutoff,))
        return cursor.fetchall()
    except mysql.connector.Error as err:
        myprint(f"Could not get orphaned connection requests | Error: {err}")
        return []
    finally:
        cursor.close()
        connection.close()


def update_connection_request_status(request_id: int, status: "ConnectionRequestStatus") -> bool:
    connection = get_db_connection()
    cursor = connection.cursor()
    try:
        cursor.execute("UPDATE connection_requests SET status = %s WHERE id = %s",
                       (str(status), request_id))
        connection.commit()
        return cursor.rowcount > 0
    except mysql.connector.Error as err:
        myprint(f"Could not update connection request {request_id} status | Error: {err}")
        return False
    finally:
        cursor.close()
        connection.close()


def update_connection_request(request_id: int, recipient_profile_url: str = None,
                              recipient_name: str = None, message: str = None,
                              status: "ConnectionRequestStatus" = None) -> bool:
    fields, params = [], []
    for col, val in (("recipient_profile_url", recipient_profile_url),
                     ("recipient_name", recipient_name), ("message", message)):
        if val is not None:
            fields.append(f"{col} = %s")
            params.append(val)
    if status is not None:
        fields.append("status = %s")
        params.append(str(status))
    if not fields:
        return False
    params.append(request_id)
    connection = get_db_connection()
    cursor = connection.cursor()
    try:
        cursor.execute(f"UPDATE connection_requests SET {', '.join(fields)} WHERE id = %s", tuple(params))
        connection.commit()
        return cursor.rowcount > 0
    except mysql.connector.Error as err:
        myprint(f"Could not update connection request {request_id} | Error: {err}")
        return False
    finally:
        cursor.close()
        connection.close()


def has_scheduled_post_today(user_id: int) -> bool:
    """True if the user has a post going out today (UTC) — those days are already covered by the
    pre-post commenting trigger, so the standalone daily engagement run should skip them. Fails
    safe to True (skip the standalone run) so an error never causes double-commenting."""
    connection = get_db_connection()
    cursor = connection.cursor()
    try:
        cursor.execute(
            "SELECT COUNT(*) FROM posts WHERE user_id=%s AND DATE(scheduled_time)=UTC_DATE() "
            "AND status IN ('approved','scheduled','posted')", (user_id,))
        r = cursor.fetchone()
        return bool(r and r[0])
    except mysql.connector.Error as err:
        myprint(f"Could not check today's posts for user {user_id} | Error: {err}")
        return True
    finally:
        cursor.close()
        connection.close()


def upsert_engager(user_id: int, engager_name: str, engager_profile_url: str = None) -> bool:
    """Record that `engager_name` engaged with the user's post (or refresh their last-engaged
    time). No-op on a blank name or if the table isn't present yet."""
    if not engager_name or not engager_name.strip():
        return False
    connection = get_db_connection()
    cursor = connection.cursor()
    try:
        cursor.execute(
            "INSERT INTO post_engagers (user_id, engager_name, engager_profile_url, last_engaged_at) "
            "VALUES (%s,%s,%s,NOW()) ON DUPLICATE KEY UPDATE "
            "engager_profile_url=COALESCE(VALUES(engager_profile_url), engager_profile_url), "
            "last_engaged_at=NOW()",
            (user_id, engager_name.strip()[:255], (engager_profile_url or None)))
        connection.commit()
        return True
    except mysql.connector.Error as err:
        myprint(f"Could not upsert engager for user {user_id} | Error: {err}")
        return False
    finally:
        cursor.close()
        connection.close()


def get_recent_engagers(user_id: int, days: int = 14) -> set:
    """Lowercased names of people who recently commented on the user's OWN posts — reciprocity
    targets to prioritize commenting back on. Empty set if the tracking table isn't present yet."""
    connection = get_db_connection()
    cursor = connection.cursor()
    try:
        cursor.execute(
            "SELECT LOWER(engager_name) FROM post_engagers "
            "WHERE user_id=%s AND last_engaged_at >= (NOW() - INTERVAL %s DAY)",
            (user_id, days))
        return {r[0] for r in cursor.fetchall() if r and r[0]}
    except mysql.connector.Error:
        return set()
    finally:
        cursor.close()
        connection.close()


def claim_post_for_comment(user_id: int, post_key: str) -> bool:
    """Atomically claim a feed post for commenting. Returns True only for the caller that WON the
    claim (safe to comment); False if the post was already claimed/commented by any prior or
    concurrent run — the loser must back off. The UNIQUE (user_id, post_key) constraint resolves
    the race at the DB, so this is safe across sequential re-scans, retries, and parallel workers."""
    if not post_key or not str(post_key).strip():
        return False
    connection = get_db_connection()
    cursor = connection.cursor()
    try:
        cursor.execute(
            "INSERT INTO commented_posts (user_id, post_key, status) VALUES (%s,%s,'claimed')",
            (user_id, str(post_key)[:255]))
        connection.commit()
        return cursor.rowcount == 1
    except mysql.connector.IntegrityError:
        # Duplicate key — another run/worker already holds this post.
        return False
    except mysql.connector.Error as err:
        myprint(f"Could not claim post for comment for user {user_id} | Error: {err}")
        return False
    finally:
        cursor.close()
        connection.close()


def mark_post_commented(user_id: int, post_key: str) -> bool:
    """Promote a won claim to 'commented' once the comment has actually posted."""
    if not post_key:
        return False
    connection = get_db_connection()
    cursor = connection.cursor()
    try:
        cursor.execute(
            "UPDATE commented_posts SET status='commented' WHERE user_id=%s AND post_key=%s",
            (user_id, str(post_key)[:255]))
        connection.commit()
        return cursor.rowcount >= 1
    except mysql.connector.Error as err:
        myprint(f"Could not mark post commented for user {user_id} | Error: {err}")
        return False
    finally:
        cursor.close()
        connection.close()


def mark_post_reacted(user_id: int, post_key: str) -> bool:
    """Record that we also left a reaction on this post (audit + 'react at most once' tracking)."""
    if not post_key:
        return False
    connection = get_db_connection()
    cursor = connection.cursor()
    try:
        cursor.execute(
            "UPDATE commented_posts SET reacted=1 WHERE user_id=%s AND post_key=%s",
            (user_id, str(post_key)[:255]))
        connection.commit()
        return cursor.rowcount >= 1
    except mysql.connector.Error as err:
        myprint(f"Could not mark post reacted for user {user_id} | Error: {err}")
        return False
    finally:
        cursor.close()
        connection.close()


def release_post_claim(user_id: int, post_key: str) -> bool:
    """Release an in-flight claim (comment never posted) so a later run can retry the post. Only
    deletes rows still in the 'claimed' state — a successful 'commented' record is never released."""
    if not post_key:
        return False
    connection = get_db_connection()
    cursor = connection.cursor()
    try:
        cursor.execute(
            "DELETE FROM commented_posts WHERE user_id=%s AND post_key=%s AND status='claimed'",
            (user_id, str(post_key)[:255]))
        connection.commit()
        return cursor.rowcount >= 1
    except mysql.connector.Error as err:
        myprint(f"Could not release post claim for user {user_id} | Error: {err}")
        return False
    finally:
        cursor.close()
        connection.close()


def has_commented_post(user_id: int, post_key: str) -> bool:
    """True if this post is already claimed or commented for the user (persistent, cross-run
    dedup). Empty/False if the ledger table isn't present yet."""
    if not post_key:
        return False
    connection = get_db_connection()
    cursor = connection.cursor()
    try:
        cursor.execute(
            "SELECT COUNT(*) FROM commented_posts WHERE user_id=%s AND post_key=%s",
            (user_id, str(post_key)[:255]))
        row = cursor.fetchone()
        return bool(row and row[0])
    except mysql.connector.Error:
        return False
    finally:
        cursor.close()
        connection.close()


def get_duplicate_comment_posts(user_id: int, hours: int = 24):
    """Read-only report: posts the user commented on MORE THAN ONCE in the last `hours`, from the
    SUCCESS comment logs. Returns list of (post_url, comment_count, first_at, last_at) ordered by
    most-duplicated first. Used to size/verify the multiple-comment bug and drive consolidation."""
    connection = get_db_connection()
    cursor = connection.cursor()
    try:
        cursor.execute(
            "SELECT post_url, COUNT(*) AS c, MIN(created_at) AS first_at, MAX(created_at) AS last_at "
            "FROM logs WHERE user_id=%s AND action_type=%s AND result=%s "
            "AND post_url IS NOT NULL AND created_at >= (NOW() - INTERVAL %s HOUR) "
            "GROUP BY post_url HAVING c > 1 ORDER BY c DESC, last_at DESC",
            (user_id, LogActionType.COMMENT.value, LogResultType.SUCCESS.value, hours))
        return [tuple(r) for r in cursor.fetchall()]
    except mysql.connector.Error as err:
        myprint(f"Could not get duplicate comment posts for user {user_id} | Error: {err}")
        return []
    finally:
        cursor.close()
        connection.close()


def upsert_user_group(user_id: int, group_id: str, group_name: str = None) -> bool:
    """Record a joined group (new groups default to enabled=1). Refreshes name + last_synced_at
    without clobbering the user's enabled choice on an existing row."""
    if not group_id:
        return False
    connection = get_db_connection()
    cursor = connection.cursor()
    try:
        cursor.execute(
            "INSERT INTO user_groups (user_id, group_id, group_name, enabled, last_synced_at) "
            "VALUES (%s,%s,%s,1,NOW()) ON DUPLICATE KEY UPDATE "
            "group_name=COALESCE(VALUES(group_name), group_name), last_synced_at=NOW()",
            (user_id, str(group_id), group_name))
        connection.commit()
        return True
    except mysql.connector.Error as err:
        myprint(f"Could not upsert group for user {user_id} | Error: {err}")
        return False
    finally:
        cursor.close()
        connection.close()


def get_user_groups(user_id: int) -> list:
    connection = get_db_connection()
    cursor = connection.cursor(dictionary=True)
    try:
        cursor.execute(
            "SELECT group_id, group_name, enabled FROM user_groups WHERE user_id=%s ORDER BY group_name",
            (user_id,))
        rows = cursor.fetchall() or []
        for r in rows:
            r["enabled"] = bool(r.get("enabled"))
        return rows
    except mysql.connector.Error as err:
        myprint(f"Could not list groups for user {user_id} | Error: {err}")
        return []
    finally:
        cursor.close()
        connection.close()


def get_enabled_group_ids(user_id: int) -> list:
    connection = get_db_connection()
    cursor = connection.cursor()
    try:
        cursor.execute("SELECT group_id FROM user_groups WHERE user_id=%s AND enabled=1", (user_id,))
        return [r[0] for r in cursor.fetchall()]
    except mysql.connector.Error:
        return []
    finally:
        cursor.close()
        connection.close()


def set_groups_enabled(user_id: int, group_states: dict) -> bool:
    """Bulk-update per-group enabled flags: {group_id: bool}."""
    connection = get_db_connection()
    cursor = connection.cursor()
    try:
        for gid, enabled in group_states.items():
            cursor.execute("UPDATE user_groups SET enabled=%s WHERE user_id=%s AND group_id=%s",
                           (1 if enabled else 0, user_id, str(gid)))
        connection.commit()
        return True
    except mysql.connector.Error as err:
        myprint(f"Could not update group states for user {user_id} | Error: {err}")
        return False
    finally:
        cursor.close()
        connection.close()


def record_post_stats(user_id: int, post_id: int, reactions: Optional[int], comments: Optional[int],
                      reposts: Optional[int] = 0, impressions: Optional[int] = None,
                      saves: Optional[int] = 0) -> bool:
    connection = get_db_connection()
    cursor = connection.cursor()
    try:
        # Snapshot the post's content attributes at capture time so the feedback loop (#386) can
        # learn which shape/topic earned engagement even if the post is later edited.
        cursor.execute(
            "SELECT archetype, hook_style, post_type, topic, buyer_stage "
            "FROM posts WHERE id=%s AND user_id=%s",
            (post_id, user_id))
        row = cursor.fetchone()
        archetype, hook_style, fmt, topic, buyer_stage = row if row else (None, None, None, None, None)
        cursor.execute(
            "INSERT INTO post_stats (user_id, post_id, reactions, comments, reposts, impressions, "
            "saves, archetype, hook_style, `format`, topic, buyer_stage) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (user_id, post_id, int(reactions or 0), int(comments or 0), int(reposts or 0),
             impressions, int(saves or 0), archetype, hook_style, fmt, topic, buyer_stage))
        connection.commit()
        return True
    except mysql.connector.Error as err:
        myprint(f"Could not record post stats for user {user_id} | Error: {err}")
        return False
    finally:
        cursor.close()
        connection.close()


def get_recent_posted_post_ids(user_id: int, days: int = 21) -> list:
    connection = get_db_connection()
    cursor = connection.cursor()
    try:
        # Freshest first: the reply sweep prioritizes golden-hour posts, so a rate-limited or
        # capped session spends its budget on the posts still being distributed (#401).
        cursor.execute(
            "SELECT id FROM posts WHERE user_id=%s AND status='posted' "
            "AND scheduled_time >= (NOW() - INTERVAL %s DAY) ORDER BY scheduled_time DESC", (user_id, days))
        return [r[0] for r in cursor.fetchall()]
    except mysql.connector.Error:
        return []
    finally:
        cursor.close()
        connection.close()


def get_post_engagement_rows(user_id: int) -> list:
    """Latest stats per post joined with when it was posted → rows of
    (scheduled_time, reactions, comments, reposts, archetype, hook_style, format, topic,
    buyer_stage, impressions) for post-time and content-attribution analysis (#386). The
    attribution columns are the snapshot captured on the stat row, so they reflect the post as it
    was when scraped. `impressions` may be NULL (only the author's own view exposes it) — it
    trails the tuple so index-based readers of the older shape keep working, and it lets
    `post_stats` score by engagement RATE when coverage is complete (#388)."""
    connection = get_db_connection()
    cursor = connection.cursor()
    try:
        cursor.execute(
            "SELECT p.scheduled_time, s.reactions, s.comments, s.reposts, "
            "s.archetype, s.hook_style, s.`format`, s.topic, s.buyer_stage, s.impressions "
            "FROM posts p JOIN post_stats s ON s.post_id=p.id AND s.user_id=p.user_id "
            "WHERE p.user_id=%s AND s.id IN "
            "(SELECT MAX(id) FROM post_stats WHERE user_id=%s GROUP BY post_id)",
            (user_id, user_id))
        return cursor.fetchall() or []
    except mysql.connector.Error as err:
        myprint(f"Could not get post engagement rows for user {user_id} | Error: {err}")
        return []
    finally:
        cursor.close()
        connection.close()


def get_shape_performance(user_id: int, days: int = 90) -> dict:
    """Per-SHAPE engagement totals for a user's recently posted content — the outcomes side of the
    performance→content feedback loop (issue #389 / B4). Joins each posted post's assigned shape
    (`posts.archetype` = short-form FORMAT key, `posts.hook_style`) with its LATEST captured
    `post_stats` row and aggregates raw signal counts per shape key.

    Returns ``{"format": {archetype: agg}, "hook": {hook_style: agg}}`` where each ``agg`` is
    ``{"samples", "reactions", "comments", "reposts", "impressions", "impression_samples"}``.
    ``impressions`` sums only rows where impressions is non-NULL (``impression_samples`` counts
    them) so the caller can tell whether impression-normalized scoring is available yet (B2/B3).
    The engagement-metric/weighting policy lives in ``content_framework``; this stays pure access."""
    result = {"format": {}, "hook": {}}
    connection = get_db_connection()
    cursor = connection.cursor()
    try:
        for column, bucket in (("archetype", "format"), ("hook_style", "hook")):
            cursor.execute(
                f"SELECT p.{column}, COUNT(*), "
                "COALESCE(SUM(s.reactions),0), COALESCE(SUM(s.comments),0), "
                "COALESCE(SUM(s.reposts),0), COALESCE(SUM(s.impressions),0), "
                "SUM(CASE WHEN s.impressions IS NOT NULL THEN 1 ELSE 0 END) "
                "FROM posts p JOIN post_stats s "
                "ON s.post_id=p.id AND s.user_id=p.user_id "
                f"WHERE p.user_id=%s AND p.status='posted' AND p.{column} IS NOT NULL "
                "AND p.scheduled_time >= (NOW() - INTERVAL %s DAY) "
                "AND s.id IN (SELECT MAX(id) FROM post_stats WHERE user_id=%s GROUP BY post_id) "
                f"GROUP BY p.{column}",
                (user_id, days, user_id))
            for key, samples, reactions, comments, reposts, impressions, imp_samples in cursor.fetchall():
                result[bucket][key] = {
                    "samples": int(samples or 0),
                    "reactions": int(reactions or 0),
                    "comments": int(comments or 0),
                    "reposts": int(reposts or 0),
                    "impressions": int(impressions or 0),
                    "impression_samples": int(imp_samples or 0),
                }
        return result
    except mysql.connector.Error as err:
        myprint(f"Could not get shape performance for user {user_id} | Error: {err}")
        return {"format": {}, "hook": {}}
    finally:
        cursor.close()
        connection.close()


def get_post_performance_rows(user_id: int, days: Optional[int] = None) -> list:
    """Latest captured stat per POSTED post as attribution-tagged dicts for the analytics
    dashboard (issue #395) — the per-post performance table and the engagement-rate/impression
    trend both read this. Like ``get_post_engagement_rows`` it keeps only the newest stat row per
    post (``MAX(id)``), but returns dicts carrying ``post_id`` and ``saves`` so the UI can key each
    row and surface the save signal (#387). ``impressions`` may be NULL (only the author's own view
    exposes it). ``days`` optionally windows to posts scheduled within the last N days (None = all),
    newest first."""
    connection = get_db_connection()
    cursor = connection.cursor()
    try:
        window = "AND p.scheduled_time >= (NOW() - INTERVAL %s DAY) " if days is not None else ""
        params = (user_id, user_id, days) if days is not None else (user_id, user_id)
        cursor.execute(
            "SELECT p.id, p.scheduled_time, s.reactions, s.comments, s.reposts, s.impressions, "
            "s.saves, s.archetype, s.hook_style, s.`format`, s.topic, s.buyer_stage "
            "FROM posts p JOIN post_stats s ON s.post_id=p.id AND s.user_id=p.user_id "
            "WHERE p.user_id=%s AND p.status='posted' "
            "AND s.id IN (SELECT MAX(id) FROM post_stats WHERE user_id=%s GROUP BY post_id) "
            + window +
            "ORDER BY p.scheduled_time DESC",
            params)
        return [
            {"post_id": r[0], "scheduled_time": r[1], "reactions": r[2], "comments": r[3],
             "reposts": r[4], "impressions": r[5], "saves": r[6], "archetype": r[7],
             "hook_style": r[8], "format": r[9], "topic": r[10], "buyer_stage": r[11]}
            for r in (cursor.fetchall() or [])
        ]
    except mysql.connector.Error as err:
        myprint(f"Could not get post performance rows for user {user_id} | Error: {err}")
        return []
    finally:
        cursor.close()
        connection.close()


def record_shipped_variant(user_id: int, post_id: int, variant_key: str,
                           combo: Optional[dict] = None, batch_id: Optional[str] = None,
                           variant_index: Optional[int] = None) -> bool:
    """Persist which A/B variant actually SHIPPED for a post (issue #396 / D2) so its realized
    `post_stats` can be attributed back to that variant when picking winners. One row per post —
    re-recording overwrites. `combo` is stored as JSON for provenance."""
    connection = get_db_connection()
    cursor = connection.cursor()
    try:
        cursor.execute(
            "INSERT INTO post_variants (user_id, post_id, batch_id, variant_index, variant_key, combo) "
            "VALUES (%s,%s,%s,%s,%s,%s) "
            "ON DUPLICATE KEY UPDATE batch_id=VALUES(batch_id), variant_index=VALUES(variant_index), "
            "variant_key=VALUES(variant_key), combo=VALUES(combo), shipped_at=CURRENT_TIMESTAMP",
            (user_id, post_id, batch_id, variant_index, variant_key,
             json.dumps(combo, default=str) if combo is not None else None))
        connection.commit()
        return True
    except mysql.connector.Error as err:
        myprint(f"Could not record shipped variant for user {user_id} | Error: {err}")
        return False
    finally:
        cursor.close()
        connection.close()


def get_variant_outcome_rows(user_id: int) -> list:
    """Realized outcomes for shipped A/B variants (issue #396 / D2). Joins each recorded shipped
    variant (`post_variants`) with its post's LATEST captured `post_stats` row → dicts of
    ``{variant_key, scheduled_time, reactions, comments, reposts, impressions}`` that feed
    ``post_stats.select_variant_winners``. `impressions` may be NULL (only the author's own view
    exposes it), so winner selection falls back to raw counts until coverage is complete."""
    connection = get_db_connection()
    cursor = connection.cursor()
    try:
        cursor.execute(
            "SELECT v.variant_key, p.scheduled_time, s.reactions, s.comments, s.reposts, s.impressions "
            "FROM post_variants v "
            "JOIN posts p ON p.id=v.post_id AND p.user_id=v.user_id "
            "JOIN post_stats s ON s.post_id=v.post_id AND s.user_id=v.user_id "
            "WHERE v.user_id=%s AND s.id IN "
            "(SELECT MAX(id) FROM post_stats WHERE user_id=%s GROUP BY post_id)",
            (user_id, user_id))
        return [
            {"variant_key": r[0], "scheduled_time": r[1], "reactions": r[2],
             "comments": r[3], "reposts": r[4], "impressions": r[5]}
            for r in (cursor.fetchall() or [])
        ]
    except mysql.connector.Error as err:
        myprint(f"Could not get variant outcome rows for user {user_id} | Error: {err}")
        return []
    finally:
        cursor.close()
        connection.close()


_LEAD_MAGNET_DEFAULTS: dict = {"enabled": False, "keyword": None, "message": None}


def get_lead_magnet_settings(user_id: int) -> dict:
    connection = get_db_connection()
    cursor = connection.cursor(dictionary=True)
    try:
        cursor.execute("SELECT enabled, keyword, message FROM lead_magnet_settings WHERE user_id=%s", (user_id,))
        row = cursor.fetchone()
        if row is None:
            return dict(_LEAD_MAGNET_DEFAULTS)
        row["enabled"] = bool(row.get("enabled"))
        return row
    except mysql.connector.Error as err:
        myprint(f"Could not get lead magnet for user {user_id} | Error: {err}")
        return dict(_LEAD_MAGNET_DEFAULTS)
    finally:
        cursor.close()
        connection.close()


def update_lead_magnet_settings(user_id: int, settings: dict) -> bool:
    connection = get_db_connection()
    cursor = connection.cursor()
    try:
        cursor.execute(
            "INSERT INTO lead_magnet_settings (user_id, enabled, keyword, message) VALUES (%s,%s,%s,%s) "
            "ON DUPLICATE KEY UPDATE enabled=VALUES(enabled), keyword=VALUES(keyword), message=VALUES(message)",
            (user_id, 1 if settings.get("enabled") else 0, settings.get("keyword"), settings.get("message")))
        connection.commit()
        return cursor.rowcount >= 0
    except mysql.connector.Error as err:
        myprint(f"Could not update lead magnet for user {user_id} | Error: {err}")
        return False
    finally:
        cursor.close()
        connection.close()


def has_received_lead_magnet(user_id: int, recipient_profile: str) -> bool:
    connection = get_db_connection()
    cursor = connection.cursor()
    try:
        cursor.execute("SELECT 1 FROM lead_magnet_sent WHERE user_id=%s AND recipient_profile=%s LIMIT 1",
                       (user_id, recipient_profile))
        return cursor.fetchone() is not None
    except mysql.connector.Error:
        return True   # fail safe: assume sent (don't double-DM on error)
    finally:
        cursor.close()
        connection.close()


def record_lead_magnet_sent(user_id: int, recipient_profile: str, post_id: int = None) -> bool:
    connection = get_db_connection()
    cursor = connection.cursor()
    try:
        cursor.execute(
            "INSERT IGNORE INTO lead_magnet_sent (user_id, recipient_profile, post_id) VALUES (%s,%s,%s)",
            (user_id, recipient_profile, post_id))
        connection.commit()
        return True
    except mysql.connector.Error as err:
        myprint(f"Could not record lead magnet sent for user {user_id} | Error: {err}")
        return False
    finally:
        cursor.close()
        connection.close()


_NEWSLETTER_DEFAULTS: dict = {
    "enabled": False, "title": None, "topic": None, "cadence": "weekly",
    "align_with_blog": True, "newsletter_url": None, "last_published_at": None,
    "publish_day": 1, "publish_hour": 9, "generate_lead_days": 3, "max_queued_drafts": 1,
}
_NEWSLETTER_COLS = ("enabled", "title", "topic", "cadence", "align_with_blog", "newsletter_url",
                    "publish_day", "publish_hour", "generate_lead_days", "max_queued_drafts")


def get_newsletter_settings(user_id: int) -> dict:
    """Return the user's newsletter config with defaults (disabled) when no row exists."""
    connection = get_db_connection()
    cursor = connection.cursor(dictionary=True)
    try:
        cursor.execute(
            "SELECT enabled, title, topic, cadence, align_with_blog, newsletter_url, last_published_at, "
            "publish_day, publish_hour, generate_lead_days, max_queued_drafts "
            "FROM newsletter_settings WHERE user_id = %s", (user_id,))
        row = cursor.fetchone()
        if row is None:
            return dict(_NEWSLETTER_DEFAULTS)
        row["enabled"] = bool(row.get("enabled"))
        row["align_with_blog"] = bool(row.get("align_with_blog"))
        row["publish_day"] = int(row.get("publish_day") if row.get("publish_day") is not None else 1)
        row["publish_hour"] = int(row.get("publish_hour") if row.get("publish_hour") is not None else 9)
        row["generate_lead_days"] = int(
            row.get("generate_lead_days") if row.get("generate_lead_days") is not None else 3)
        row["max_queued_drafts"] = int(
            row.get("max_queued_drafts") if row.get("max_queued_drafts") is not None else 1)
        return row
    except mysql.connector.Error as err:
        myprint(f"Could not get newsletter settings for user {user_id} | Error: {err}")
        return dict(_NEWSLETTER_DEFAULTS)
    finally:
        cursor.close()
        connection.close()


def update_newsletter_settings(user_id: int, settings: dict) -> bool:
    """Upsert the user's newsletter config (title/topic/cadence/enabled/align_with_blog)."""
    merged = {**_NEWSLETTER_DEFAULTS, **{k: v for k, v in settings.items() if k in _NEWSLETTER_COLS}}
    values = [user_id] + [
        (1 if merged[c] else 0) if c in ("enabled", "align_with_blog") else merged[c]
        for c in _NEWSLETTER_COLS]
    placeholders = ", ".join(["%s"] * (len(_NEWSLETTER_COLS) + 1))
    updates = ", ".join(f"{c}=VALUES({c})" for c in _NEWSLETTER_COLS)
    connection = get_db_connection()
    cursor = connection.cursor()
    try:
        cursor.execute(
            f"INSERT INTO newsletter_settings (user_id, {', '.join(_NEWSLETTER_COLS)}) "
            f"VALUES ({placeholders}) ON DUPLICATE KEY UPDATE {updates}", values)
        connection.commit()
        return cursor.rowcount >= 0
    except mysql.connector.Error as err:
        myprint(f"Could not update newsletter settings for user {user_id} | Error: {err}")
        return False
    finally:
        cursor.close()
        connection.close()


def mark_newsletter_published(user_id: int, newsletter_url: str = None) -> bool:
    connection = get_db_connection()
    cursor = connection.cursor()
    try:
        if newsletter_url:
            cursor.execute("UPDATE newsletter_settings SET last_published_at=NOW(), newsletter_url=%s "
                           "WHERE user_id=%s", (newsletter_url, user_id))
        else:
            cursor.execute("UPDATE newsletter_settings SET last_published_at=NOW() WHERE user_id=%s", (user_id,))
        connection.commit()
        return cursor.rowcount >= 0
    except mysql.connector.Error as err:
        myprint(f"Could not mark newsletter published for user {user_id} | Error: {err}")
        return False
    finally:
        cursor.close()
        connection.close()


def get_newsletter_due_user_ids(now) -> list:
    """User IDs whose newsletter is enabled and due per its cadence (weekly/biweekly/monthly)."""
    connection = get_db_connection()
    cursor = connection.cursor()
    try:
        cursor.execute(
            "SELECT user_id FROM newsletter_settings WHERE enabled=1 AND ("
            "last_published_at IS NULL "
            "OR (cadence='weekly'   AND last_published_at <= %s - INTERVAL 7 DAY) "
            "OR (cadence='biweekly' AND last_published_at <= %s - INTERVAL 14 DAY) "
            "OR (cadence='monthly'  AND last_published_at <= %s - INTERVAL 1 MONTH))",
            (now, now, now))
        return [r[0] for r in cursor.fetchall()]
    except mysql.connector.Error as err:
        myprint(f"Could not get newsletter-due users | Error: {err}")
        return []
    finally:
        cursor.close()
        connection.close()


def get_enabled_newsletter_user_ids() -> list:
    """User IDs whose newsletter is enabled (regardless of cadence timing)."""
    connection = get_db_connection()
    cursor = connection.cursor()
    try:
        cursor.execute("SELECT user_id FROM newsletter_settings WHERE enabled=1")
        return [r[0] for r in cursor.fetchall()]
    except mysql.connector.Error as err:
        myprint(f"Could not get enabled newsletter users | Error: {err}")
        return []
    finally:
        cursor.close()
        connection.close()


def create_newsletter_edition(user_id: int, title: str, subtitle: str, body: str,
                              scheduled_for, subject: str = None, edition_format: str = None,
                              hook_style: str = None, opening_line: str = None,
                              blueprint: dict = None) -> int:
    """Insert a draft newsletter edition (status defaults to 'draft'). Returns its id. `subject` is
    the planned topic/angle; `edition_format`/`hook_style`/`opening_line`/`blueprint` record the
    edition's assigned SHAPE, so the planner can rotate formats/hooks/openers (not just subjects)
    against prior editions across runs."""
    connection = get_db_connection()
    cursor = connection.cursor()
    try:
        cursor.execute(
            "INSERT INTO newsletter_editions (user_id, title, subtitle, subject, `format`, "
            "hook_style, opening_line, blueprint, body, scheduled_for) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (user_id, title, subtitle, subject, edition_format, hook_style, opening_line,
             json.dumps(blueprint) if blueprint else None, body, scheduled_for))
        connection.commit()
        return cursor.lastrowid
    except mysql.connector.IntegrityError as err:
        # errno 1062 = ER_DUP_ENTRY: uq_user_slot already covers this user+slot — expected, not an
        # error. Other integrity failures (e.g. FK on user_id) are real problems worth surfacing.
        if getattr(err, "errno", None) != 1062:
            myprint(f"Could not create newsletter edition for user {user_id} | Error: {err}")
        return 0
    except mysql.connector.Error as err:
        myprint(f"Could not create newsletter edition for user {user_id} | Error: {err}")
        return 0
    finally:
        cursor.close()
        connection.close()


def get_pending_newsletter_edition(user_id: int) -> "dict | None":
    """The most recent edition still under review (status draft/approved) for this user."""
    connection = get_db_connection()
    cursor = connection.cursor(dictionary=True)
    try:
        cursor.execute(
            "SELECT id, title, subtitle, subject, `format`, hook_style, body, status, scheduled_for "
            "FROM newsletter_editions "
            "WHERE user_id = %s AND status IN ('draft', 'approved') "
            "ORDER BY id DESC LIMIT 1", (user_id,))
        return cursor.fetchone()
    except mysql.connector.Error as err:
        myprint(f"Could not get pending newsletter edition for user {user_id} | Error: {err}")
        return None
    finally:
        cursor.close()
        connection.close()


def count_pending_newsletter_editions(user_id: int) -> int:
    """How many editions are still queued (status draft/approved) for this user."""
    connection = get_db_connection()
    cursor = connection.cursor()
    try:
        cursor.execute(
            "SELECT COUNT(*) FROM newsletter_editions "
            "WHERE user_id = %s AND status IN ('draft', 'approved')", (user_id,))
        row = cursor.fetchone()
        return int(row[0]) if row else 0
    except mysql.connector.Error as err:
        myprint(f"Could not count pending newsletter editions for user {user_id} | Error: {err}")
        return 0
    finally:
        cursor.close()
        connection.close()


def get_pending_newsletter_editions(user_id: int) -> list:
    """All editions still under review (status draft/approved), soonest slot first — the review queue."""
    connection = get_db_connection()
    cursor = connection.cursor(dictionary=True)
    try:
        cursor.execute(
            "SELECT id, title, subtitle, subject, `format`, hook_style, body, status, scheduled_for "
            "FROM newsletter_editions "
            "WHERE user_id = %s AND status IN ('draft', 'approved') "
            "ORDER BY scheduled_for ASC", (user_id,))
        return cursor.fetchall()
    except mysql.connector.Error as err:
        myprint(f"Could not get pending newsletter editions for user {user_id} | Error: {err}")
        return []
    finally:
        cursor.close()
        connection.close()


def get_latest_edition_scheduled_for(user_id: int) -> "datetime | None":
    """The latest slot already covered by ANY edition (any status), so the next slot never re-covers it."""
    connection = get_db_connection()
    cursor = connection.cursor()
    try:
        cursor.execute(
            "SELECT MAX(scheduled_for) FROM newsletter_editions WHERE user_id = %s", (user_id,))
        row = cursor.fetchone()
        return row[0] if row else None
    except mysql.connector.Error as err:
        myprint(f"Could not get latest edition slot for user {user_id} | Error: {err}")
        return None
    finally:
        cursor.close()
        connection.close()


def update_newsletter_edition(edition_id: int, user_id: int, title: str = None,
                              subtitle: str = None, body: str = None,
                              status: str = None, subject: str = None,
                              edition_format: str = None, hook_style: str = None,
                              opening_line: str = None, blueprint: dict = None,
                              scheduled_for=None) -> bool:
    """Update only the provided fields on an edition, scoped to its owner (COALESCE-style)."""
    connection = get_db_connection()
    cursor = connection.cursor()
    try:
        if scheduled_for is not None and getattr(scheduled_for, "tzinfo", None) is not None:
            scheduled_for = scheduled_for.astimezone(timezone.utc).replace(tzinfo=None)
        cursor.execute(
            "UPDATE newsletter_editions SET "
            "title = COALESCE(%s, title), subtitle = COALESCE(%s, subtitle), "
            "subject = COALESCE(%s, subject), "
            "`format` = COALESCE(%s, `format`), hook_style = COALESCE(%s, hook_style), "
            "opening_line = COALESCE(%s, opening_line), blueprint = COALESCE(%s, blueprint), "
            "body = COALESCE(%s, body), status = COALESCE(%s, status), "
            "scheduled_for = COALESCE(%s, scheduled_for) "
            "WHERE id = %s AND user_id = %s",
            (title, subtitle, subject, edition_format, hook_style, opening_line,
             json.dumps(blueprint) if blueprint else None, body, status, scheduled_for,
             edition_id, user_id))
        connection.commit()
        return cursor.rowcount >= 0
    except mysql.connector.Error as err:
        myprint(f"Could not update newsletter edition {edition_id} | Error: {err}")
        return False
    finally:
        cursor.close()
        connection.close()


def get_recent_newsletter_subjects(user_id: int, limit: int = 20) -> list:
    """Recent edition SUBJECTS (published, queued draft/approved, AND skipped) for a user — the dedup
    history fed to the topic planner so a new edition never repeats a subject already covered or
    recently rejected. Most-recent first; NULL/blank subjects excluded."""
    connection = get_db_connection()
    cursor = connection.cursor()
    try:
        cursor.execute(
            "SELECT subject FROM newsletter_editions "
            "WHERE user_id = %s AND subject IS NOT NULL AND subject <> '' "
            "AND status IN ('draft', 'approved', 'published', 'skipped') "
            "ORDER BY id DESC LIMIT %s", (user_id, int(limit)))
        return [r[0] for r in cursor.fetchall()]
    except mysql.connector.Error as err:
        myprint(f"Could not get recent newsletter subjects for user {user_id} | Error: {err}")
        return []
    finally:
        cursor.close()
        connection.close()


def get_recent_newsletter_blueprint_history(user_id: int, limit: int = 12) -> list:
    """Recent editions' SHAPE history — {subject, format, hook_style, opening_line} dicts, most-recent
    first, across queued/published/skipped editions. Fed to the planner and regenerator so new
    editions rotate away from recently used formats, hook styles, AND actual opening lines (the
    'every edition opens the same way' bug), not just subjects."""
    connection = get_db_connection()
    cursor = connection.cursor(dictionary=True)
    try:
        cursor.execute(
            "SELECT subject, `format`, hook_style, opening_line FROM newsletter_editions "
            "WHERE user_id = %s AND status IN ('draft', 'approved', 'published', 'skipped') "
            "ORDER BY id DESC LIMIT %s", (user_id, int(limit)))
        return cursor.fetchall()
    except mysql.connector.Error as err:
        myprint(f"Could not get newsletter blueprint history for user {user_id} | Error: {err}")
        return []
    finally:
        cursor.close()
        connection.close()


def get_editions_due_to_publish(now) -> list:
    """Editions whose scheduled slot has arrived and are still awaiting publish (draft/approved)."""
    connection = get_db_connection()
    cursor = connection.cursor(dictionary=True)
    try:
        cursor.execute(
            "SELECT id, user_id, title, subtitle, body FROM newsletter_editions "
            "WHERE scheduled_for <= %s AND status IN ('draft', 'approved')", (now,))
        return cursor.fetchall()
    except mysql.connector.Error as err:
        myprint(f"Could not get editions due to publish | Error: {err}")
        return []
    finally:
        cursor.close()
        connection.close()


def get_newsletter_edition(edition_id: int) -> "dict | None":
    """Fetch a single newsletter edition by id."""
    connection = get_db_connection()
    cursor = connection.cursor(dictionary=True)
    try:
        cursor.execute(
            "SELECT id, user_id, title, subtitle, subject, `format`, hook_style, opening_line, "
            "body, status, scheduled_for, published_url "
            "FROM newsletter_editions WHERE id = %s", (edition_id,))
        return cursor.fetchone()
    except mysql.connector.Error as err:
        myprint(f"Could not get newsletter edition {edition_id} | Error: {err}")
        return None
    finally:
        cursor.close()
        connection.close()


def mark_edition_published(edition_id: int, url: str) -> bool:
    """Mark an edition published and roll the user's newsletter cadence forward."""
    connection = get_db_connection()
    cursor = connection.cursor()
    try:
        cursor.execute(
            "UPDATE newsletter_editions SET status='published', published_at=NOW(), published_url=%s "
            "WHERE id=%s", (url, edition_id))
        cursor.execute(
            "UPDATE newsletter_settings SET last_published_at=NOW() "
            "WHERE user_id = (SELECT user_id FROM newsletter_editions WHERE id=%s)", (edition_id,))
        connection.commit()
        return cursor.rowcount >= 0
    except mysql.connector.Error as err:
        myprint(f"Could not mark edition {edition_id} published | Error: {err}")
        return False
    finally:
        cursor.close()
        connection.close()


def mark_edition_failed(edition_id: int) -> bool:
    """Mark an edition as failed to publish."""
    connection = get_db_connection()
    cursor = connection.cursor()
    try:
        cursor.execute("UPDATE newsletter_editions SET status='failed' WHERE id=%s", (edition_id,))
        connection.commit()
        return cursor.rowcount >= 0
    except mysql.connector.Error as err:
        myprint(f"Could not mark edition {edition_id} failed | Error: {err}")
        return False
    finally:
        cursor.close()
        connection.close()


# Default DM templates = today's hard-coded strings, so behaviour is unchanged until a user
# customizes. {first_name},{headline},{blog_url} are filled at send time.
_DM_DEFAULT_TEMPLATES = {
    "connection_accepted": "Hi {first_name}, I appreciate you connecting with me on LinkedIn. "
                           "I look forward to learning more about you and your work.",
    "recommendation_received": "Hi {first_name}, thank you so much for the kind recommendation on LinkedIn! "
                               "I really appreciate you taking the time to share your experience working with me. "
                               "I hope we have the opportunity to collaborate again in the future.",
    "collaboration": "Hi {first_name}, it was a pleasure collaborating with you! "
                     "Your contributions made a real difference and I'm grateful for the opportunity. "
                     "Let's stay in touch — I'd love to explore future opportunities to work together.",
    "profile_viewer": "Hi {first_name}, I noticed you viewed my LinkedIn profile and wanted to reach out. "
                      "I share insights on {headline} and thought there might be synergy between our work. "
                      "Would love to connect more directly — feel free to share what you're working on!",
    "manual": "Hi {first_name}, thanks for connecting!",
}


def get_dm_template(user_id: int, event_type: str, step: int = 0) -> Optional[dict]:
    """Return {template_text, delay_hours, step} for (user, event, step). Falls back to the
    code default for step 0; None for higher steps that aren't configured."""
    connection = get_db_connection()
    cursor = connection.cursor(dictionary=True)
    try:
        cursor.execute(
            "SELECT template_text, delay_hours, step FROM dm_templates "
            "WHERE user_id=%s AND event_type=%s AND step=%s AND is_active=1",
            (user_id, str(event_type), step))
        row = cursor.fetchone()
        if row:
            return row
    except mysql.connector.Error as err:
        myprint(f"Could not get dm template for user_id {user_id} | Error: {err}")
    finally:
        cursor.close()
        connection.close()
    if step == 0 and event_type in _DM_DEFAULT_TEMPLATES:
        return {"template_text": _DM_DEFAULT_TEMPLATES[event_type], "delay_hours": 0, "step": 0}
    return None


def get_dm_templates(user_id: int) -> list:
    connection = get_db_connection()
    cursor = connection.cursor(dictionary=True)
    try:
        cursor.execute(
            "SELECT event_type, step, delay_hours, template_text, is_active "
            "FROM dm_templates WHERE user_id=%s ORDER BY event_type, step", (user_id,))
        rows = cursor.fetchall() or []
        for r in rows:
            r["is_active"] = bool(r.get("is_active"))
        return rows
    except mysql.connector.Error as err:
        myprint(f"Could not list dm templates for user_id {user_id} | Error: {err}")
        return []
    finally:
        cursor.close()
        connection.close()


def upsert_dm_templates(user_id: int, templates: list) -> bool:
    """Upsert a list of {event_type, step, delay_hours, template_text, is_active} for a user."""
    connection = get_db_connection()
    cursor = connection.cursor()
    try:
        for t in templates:
            cursor.execute(
                "INSERT INTO dm_templates (user_id, event_type, step, delay_hours, template_text, is_active) "
                "VALUES (%s,%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE "
                "delay_hours=VALUES(delay_hours), template_text=VALUES(template_text), is_active=VALUES(is_active)",
                (user_id, str(t.get("event_type")), int(t.get("step", 0)), int(t.get("delay_hours", 0)),
                 t.get("template_text", ""), 1 if t.get("is_active", True) else 0))
        connection.commit()
        return True
    except mysql.connector.Error as err:
        myprint(f"Could not upsert dm templates for user_id {user_id} | Error: {err}")
        return False
    finally:
        cursor.close()
        connection.close()


def enqueue_followup(user_id: int, profile_url: str, first_name: str, event_type: str,
                     next_step: int, due_at) -> bool:
    """Schedule a follow-up DM touch. `due_at` is a datetime."""
    connection = get_db_connection()
    cursor = connection.cursor()
    try:
        cursor.execute(
            "INSERT INTO dm_followups (user_id, profile_url, first_name, event_type, next_step, due_at, status) "
            "VALUES (%s,%s,%s,%s,%s,%s,'pending')",
            (user_id, profile_url, first_name, str(event_type), next_step, due_at))
        connection.commit()
        return True
    except mysql.connector.Error as err:
        myprint(f"Could not enqueue followup for user_id {user_id} | Error: {err}")
        return False
    finally:
        cursor.close()
        connection.close()


def get_due_followups(now) -> list:
    """Pending follow-ups whose due_at has passed. `now` is a datetime."""
    connection = get_db_connection()
    cursor = connection.cursor(dictionary=True)
    try:
        cursor.execute(
            "SELECT id, user_id, profile_url, first_name, event_type, next_step "
            "FROM dm_followups WHERE status='pending' AND due_at <= %s ORDER BY due_at", (now,))
        return cursor.fetchall() or []
    except mysql.connector.Error as err:
        myprint(f"Could not get due followups | Error: {err}")
        return []
    finally:
        cursor.close()
        connection.close()


def mark_followup(followup_id: int, status: str) -> bool:
    connection = get_db_connection()
    cursor = connection.cursor()
    try:
        cursor.execute("UPDATE dm_followups SET status=%s WHERE id=%s", (str(status), followup_id))
        connection.commit()
        return cursor.rowcount >= 0
    except mysql.connector.Error as err:
        myprint(f"Could not mark followup {followup_id} | Error: {err}")
        return False
    finally:
        cursor.close()
        connection.close()


def stop_followups_for_profile(user_id: int, profile_url: str) -> int:
    """Stop all pending follow-ups to a profile (e.g. once they've replied). Returns count."""
    connection = get_db_connection()
    cursor = connection.cursor()
    try:
        cursor.execute("UPDATE dm_followups SET status='stopped' "
                       "WHERE user_id=%s AND profile_url=%s AND status='pending'",
                       (user_id, profile_url))
        connection.commit()
        return cursor.rowcount
    except mysql.connector.Error as err:
        myprint(f"Could not stop followups for user_id {user_id} | Error: {err}")
        return 0
    finally:
        cursor.close()
        connection.close()


def get_user_geo(user_id: int) -> Optional[dict]:
    """Return the user's full geo profile for Selenium spoofing.

    Keys: latitude, longitude (floats or None), timezone, locale, city, country.
    Returns None only if the user row is missing.
    """
    connection = get_db_connection()
    cursor = connection.cursor()
    try:
        cursor.execute(
            "SELECT latitude, longitude, timezone, locale, city, country FROM users WHERE id = %s",
            (user_id,),
        )
        row = cursor.fetchone()
    except mysql.connector.Error as err:
        myprint(f"Could not get user geo for user_id {user_id} | Error: {err}")
        row = None
    finally:
        cursor.close()
        connection.close()
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


def update_user_location(user_id: int, latitude: float, longitude: float,
                         city: Optional[str] = None, country: Optional[str] = None,
                         locale: Optional[str] = None, timezone: Optional[str] = None,
                         source: str = "manual") -> bool:
    """Persist the user's location. timezone is updated only when provided so the
    user's display-timezone preference is preserved unless autocapture supplies one."""
    connection = get_db_connection()
    cursor = connection.cursor()
    try:
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
        connection.commit()
        return cursor.rowcount >= 0
    except mysql.connector.Error as err:
        myprint(f"Could not update location for user_id {user_id} | Error: {err}")
        return False
    finally:
        cursor.close()
        connection.close()


def get_user_proxy(user_id: int) -> Optional[str]:
    """Return the user's egress proxy URL (scheme://[user:pass@]host:port) or None.

    Used by Selenium to route a user's browser session through an IP near where they
    normally log in, reducing LinkedIn "new location" challenges. None = egress from
    the host directly.
    """
    connection = get_db_connection()
    cursor = connection.cursor()
    try:
        cursor.execute("SELECT proxy_url FROM users WHERE id = %s", (user_id,))
        row = cursor.fetchone()
    except mysql.connector.Error as err:
        myprint(f"Could not get proxy for user_id {user_id} | Error: {err}")
        row = None
    finally:
        cursor.close()
        connection.close()
    if not row or not row[0]:
        return None
    return row[0]


def update_user_proxy(user_id: int, proxy_url: Optional[str]) -> bool:
    """Set (or clear, when proxy_url is None/empty) the user's egress proxy URL."""
    connection = get_db_connection()
    cursor = connection.cursor()
    try:
        cursor.execute(
            "UPDATE users SET proxy_url = %s WHERE id = %s",
            (proxy_url or None, user_id),
        )
        connection.commit()
        return cursor.rowcount >= 0
    except mysql.connector.Error as err:
        myprint(f"Could not update proxy for user_id {user_id} | Error: {err}")
        return False
    finally:
        cursor.close()
        connection.close()


def get_user_timezone(user_id: int) -> str:
    """Return the IANA timezone string for the user. Defaults to America/New_York to match the
    users.timezone column default and the UI default (not UTC, which would misrender local times)."""
    connection = get_db_connection()
    cursor = connection.cursor()
    try:
        cursor.execute("SELECT timezone FROM users WHERE id = %s", (user_id,))
        row = cursor.fetchone()
        return row[0] if row and row[0] else 'America/New_York'
    except mysql.connector.Error as err:
        myprint(f"Could not get timezone for user_id {user_id} | Error: {err}")
        return 'America/New_York'
    finally:
        cursor.close()
        connection.close()


def update_user_timezone(user_id: int, tz: str) -> bool:
    """Persist the user's preferred IANA timezone string."""
    connection = get_db_connection()
    cursor = connection.cursor()
    try:
        cursor.execute("UPDATE users SET timezone = %s WHERE id = %s", (tz, user_id))
        connection.commit()
        return cursor.rowcount >= 0
    except mysql.connector.Error as err:
        myprint(f"Could not update timezone for user_id {user_id} | Error: {err}")
        return False
    finally:
        cursor.close()
        connection.close()


# ---------------------------------------------------------------------------
# Avatar credit ledger
# ---------------------------------------------------------------------------

def get_user_by_stripe_customer_id(stripe_customer_id: str) -> Optional[dict]:
    """Return the user row matching a Stripe customer ID, regardless of subscription status."""
    connection = get_db_connection()
    cursor = connection.cursor(dictionary=True)
    try:
        cursor.execute(
            "SELECT id, stripe_customer_id FROM users WHERE stripe_customer_id = %s LIMIT 1",
            (stripe_customer_id,),
        )
        return cursor.fetchone()
    except mysql.connector.Error as err:
        myprint(f"Could not look up user by stripe_customer_id={stripe_customer_id} | Error: {err}")
        return None
    finally:
        cursor.close()
        connection.close()


def get_avatar_credit_ledger_entry_by_session(stripe_session_id: str) -> Optional[dict]:
    """Return an existing credit ledger entry for a Stripe session (idempotency check)."""
    connection = get_db_connection()
    cursor = connection.cursor(dictionary=True)
    try:
        cursor.execute(
            "SELECT id, user_id, delta FROM avatar_credit_ledger WHERE stripe_session_id = %s AND delta > 0 LIMIT 1",
            (stripe_session_id,),
        )
        return cursor.fetchone()
    except mysql.connector.Error as err:
        myprint(f"Could not look up ledger entry for session={stripe_session_id} | Error: {err}")
        return None
    finally:
        cursor.close()
        connection.close()


def get_avatar_credit_balance(user_id: int) -> int:
    connection = get_db_connection()
    cursor = connection.cursor(dictionary=True)
    try:
        cursor.execute(
            "SELECT COALESCE(SUM(delta), 0) AS balance FROM avatar_credit_ledger WHERE user_id = %s",
            (user_id,),
        )
        row = cursor.fetchone()
        return int(row["balance"]) if row else 0
    except mysql.connector.Error as err:
        myprint(f"Could not fetch avatar credit balance for user_id {user_id} | Error: {err}")
        return 0
    finally:
        cursor.close()
        connection.close()


def add_avatar_credits(
    user_id: int,
    amount: int,
    reason: str,
    stripe_session_id: Optional[str] = None,
) -> bool:
    connection = get_db_connection()
    cursor = connection.cursor(dictionary=True)
    try:
        cursor.execute(
            """INSERT INTO avatar_credit_ledger (user_id, delta, reason, stripe_session_id)
               VALUES (%s, %s, %s, %s)""",
            (user_id, amount, reason, stripe_session_id),
        )
        connection.commit()
        return True
    except mysql.connector.Error as err:
        myprint(f"Could not add avatar credits for user_id {user_id} | Error: {err}")
        return False
    finally:
        cursor.close()
        connection.close()


def deduct_avatar_credit(user_id: int, training_id: str) -> bool:
    connection = get_db_connection()
    cursor = connection.cursor(dictionary=True)
    try:
        cursor.execute(
            """INSERT INTO avatar_credit_ledger (user_id, delta, reason, training_id)
               VALUES (%s, -1, 'training_start', %s)""",
            (user_id, training_id),
        )
        connection.commit()
        return True
    except mysql.connector.Error as err:
        myprint(f"Could not deduct avatar credit for user_id {user_id} | Error: {err}")
        return False
    finally:
        cursor.close()
        connection.close()


def refund_avatar_credit(user_id: int, training_id: str) -> bool:
    connection = get_db_connection()
    cursor = connection.cursor(dictionary=True)
    try:
        cursor.execute(
            """INSERT INTO avatar_credit_ledger (user_id, delta, reason, training_id)
               VALUES (%s, 1, 'training_refund', %s)""",
            (user_id, training_id),
        )
        connection.commit()
        return True
    except mysql.connector.Error as err:
        myprint(f"Could not refund avatar credit for user_id {user_id} | Error: {err}")
        return False
    finally:
        cursor.close()
        connection.close()


# ---------------------------------------------------------------------------
# Premium video credits (mirrors avatar_credit_ledger; balance = SUM(delta))
# ---------------------------------------------------------------------------

def get_video_credit_balance(user_id: int) -> int:
    connection = get_db_connection()
    cursor = connection.cursor(dictionary=True)
    try:
        cursor.execute(
            "SELECT COALESCE(SUM(delta), 0) AS balance FROM video_credit_ledger WHERE user_id = %s",
            (user_id,),
        )
        row = cursor.fetchone()
        return int(row["balance"]) if row else 0
    except mysql.connector.Error as err:
        myprint(f"Could not get video credit balance for user_id {user_id} | Error: {err}")
        return 0
    finally:
        cursor.close()
        connection.close()


def get_video_credit_ledger_entry_by_session(stripe_session_id: str) -> Optional[dict]:
    """Return an existing purchase ledger entry for a Stripe session (idempotency check)."""
    connection = get_db_connection()
    cursor = connection.cursor(dictionary=True)
    try:
        cursor.execute(
            "SELECT id, user_id, delta FROM video_credit_ledger WHERE stripe_session_id = %s AND delta > 0 LIMIT 1",
            (stripe_session_id,),
        )
        return cursor.fetchone()
    except mysql.connector.Error as err:
        myprint(f"Could not look up video credit ledger by session | Error: {err}")
        return None
    finally:
        cursor.close()
        connection.close()


def add_video_credits(user_id: int, amount: int, reason: str,
                      stripe_session_id: Optional[str] = None) -> bool:
    connection = get_db_connection()
    cursor = connection.cursor(dictionary=True)
    try:
        cursor.execute(
            """INSERT INTO video_credit_ledger (user_id, delta, reason, stripe_session_id)
               VALUES (%s, %s, %s, %s)""",
            (user_id, amount, reason, stripe_session_id),
        )
        connection.commit()
        return True
    except mysql.connector.Error as err:
        myprint(f"Could not add video credits for user_id {user_id} | Error: {err}")
        return False
    finally:
        cursor.close()
        connection.close()


def deduct_video_credits(user_id: int, amount: int, post_id: Optional[int] = None,
                         reason: str = "premium_video") -> bool:
    connection = get_db_connection()
    cursor = connection.cursor(dictionary=True)
    try:
        cursor.execute(
            """INSERT INTO video_credit_ledger (user_id, delta, reason, post_id)
               VALUES (%s, %s, %s, %s)""",
            (user_id, -abs(amount), reason, post_id),
        )
        connection.commit()
        return True
    except mysql.connector.Error as err:
        myprint(f"Could not deduct video credits for user_id {user_id} | Error: {err}")
        return False
    finally:
        cursor.close()
        connection.close()


def refund_video_credits(user_id: int, amount: int, post_id: Optional[int] = None,
                         reason: str = "premium_video_refund") -> bool:
    connection = get_db_connection()
    cursor = connection.cursor(dictionary=True)
    try:
        cursor.execute(
            """INSERT INTO video_credit_ledger (user_id, delta, reason, post_id)
               VALUES (%s, %s, %s, %s)""",
            (user_id, abs(amount), reason, post_id),
        )
        connection.commit()
        return True
    except mysql.connector.Error as err:
        myprint(f"Could not refund video credits for user_id {user_id} | Error: {err}")
        return False
    finally:
        cursor.close()
        connection.close()


def get_post_video_quality(post_id: int) -> str:
    connection = get_db_connection()
    cursor = connection.cursor(dictionary=True)
    try:
        cursor.execute("SELECT video_quality FROM posts WHERE id = %s", (post_id,))
        row = cursor.fetchone()
        return (row["video_quality"] if row and row.get("video_quality") else "standard")
    except mysql.connector.Error as err:
        myprint(f"Could not get video_quality for post {post_id} | Error: {err}")
        return "standard"
    finally:
        cursor.close()
        connection.close()


def update_post_video_quality(post_id: int, quality: str) -> bool:
    connection = get_db_connection()
    cursor = connection.cursor(dictionary=True)
    try:
        cursor.execute("UPDATE posts SET video_quality = %s WHERE id = %s", (quality, post_id))
        connection.commit()
        return cursor.rowcount > 0
    except mysql.connector.Error as err:
        myprint(f"Could not update video_quality for post {post_id} | Error: {err}")
        return False
    finally:
        cursor.close()
        connection.close()


def get_post_carousel_slides(post_id: int):
    connection = get_db_connection()
    cursor = connection.cursor(dictionary=True)
    try:
        cursor.execute("SELECT carousel_slides FROM posts WHERE id = %s", (post_id,))
        row = cursor.fetchone()
        return row["carousel_slides"] if row else None
    except mysql.connector.Error as err:
        myprint(f"Could not get carousel_slides for post {post_id} | Error: {err}")
        return None
    finally:
        cursor.close()
        connection.close()


def get_unposted_posts_missing_assets(within_days: int = 14) -> list:
    """Posts not yet posted, due within `within_days`, whose required media asset is
    missing: video posts with no video_url, or carousel posts with no slides. Used by the
    backfill safety net. Returns (id, user_id, post_type, buyer_stage, scheduled_time)."""
    connection = get_db_connection()
    cursor = connection.cursor()
    try:
        # Include 'error' so failed posts get a regeneration attempt. A carousel needs
        # regeneration when its slides are empty OR are plain text titles with no real image
        # reference — real slides are stored as URLs (https .../api/assets/...png), so the
        # absence of any image marker means generation never produced images.
        cursor.execute("""
            SELECT id, user_id, post_type, buyer_stage, scheduled_time
            FROM posts
            WHERE status IN ('approved', 'pending', 'scheduled', 'error')
              AND scheduled_time > NOW()
              AND scheduled_time <= NOW() + INTERVAL %s DAY
              AND (
                    (post_type = 'video'    AND (video_url IS NULL OR video_url = ''))
                 OR (post_type IN ('carousel', 'document') AND (
                        carousel_slides IS NULL OR carousel_slides = '' OR carousel_slides = '[]'
                        OR (carousel_slides NOT LIKE '%%http%%'
                            AND carousel_slides NOT LIKE '%%/assets%%'
                            AND carousel_slides NOT LIKE '%%.png%%'
                            AND carousel_slides NOT LIKE '%%.jpg%%')
                    ))
              )
            ORDER BY scheduled_time
        """, (within_days,))
        return cursor.fetchall()
    except mysql.connector.Error as err:
        myprint(f"Could not get unposted posts missing assets | Error: {err}")
        return []
    finally:
        cursor.close()
        connection.close()


# ---------------------------------------------------------------------------
# Avatar training records
# ---------------------------------------------------------------------------

def insert_avatar_training(user_id: int, training_id: str, trigger_word: str) -> Optional[int]:
    connection = get_db_connection()
    cursor = connection.cursor(dictionary=True)
    try:
        cursor.execute(
            """INSERT INTO avatar_trainings (user_id, training_id, trigger_word)
               VALUES (%s, %s, %s)""",
            (user_id, training_id, trigger_word),
        )
        connection.commit()
        return cursor.lastrowid
    except mysql.connector.Error as err:
        myprint(f"Could not insert avatar training for user_id {user_id} | Error: {err}")
        return None
    finally:
        cursor.close()
        connection.close()


def update_avatar_training_status(
    training_id: str,
    status: str,
    model_ref: Optional[str] = None,
) -> bool:
    connection = get_db_connection()
    cursor = connection.cursor(dictionary=True)
    try:
        if model_ref:
            cursor.execute(
                """UPDATE avatar_trainings
                   SET status = %s, model_ref = %s
                   WHERE training_id = %s""",
                (status, model_ref, training_id),
            )
        else:
            cursor.execute(
                "UPDATE avatar_trainings SET status = %s WHERE training_id = %s",
                (status, training_id),
            )
        connection.commit()

        # Auto-refund credit if training failed or was canceled
        if status in ("failed", "canceled"):
            cursor.execute(
                "SELECT user_id FROM avatar_trainings WHERE training_id = %s",
                (training_id,),
            )
            row = cursor.fetchone()
            if row:
                refund_avatar_credit(row["user_id"], training_id)

        return cursor.rowcount > 0
    except mysql.connector.Error as err:
        myprint(f"Could not update avatar training status for {training_id} | Error: {err}")
        return False
    finally:
        cursor.close()
        connection.close()


def set_active_avatar(user_id: int, avatar_id: int) -> bool:
    connection = get_db_connection()
    cursor = connection.cursor(dictionary=True)
    try:
        # Validate the target avatar BEFORE deactivating anything — a bad id must leave the
        # user's current active avatar untouched (never strand them with no active avatar).
        cursor.execute(
            "SELECT id FROM avatar_trainings WHERE id = %s AND user_id = %s",
            (avatar_id, user_id),
        )
        if cursor.fetchone() is None:
            myprint(f"set_active_avatar: avatar {avatar_id} not found for user_id {user_id}")
            return False
        cursor.execute(
            "UPDATE avatar_trainings SET is_active = 0 WHERE user_id = %s",
            (user_id,),
        )
        cursor.execute(
            "UPDATE avatar_trainings SET is_active = 1 WHERE id = %s AND user_id = %s",
            (avatar_id, user_id),
        )
        connection.commit()
        return True
    except mysql.connector.Error as err:
        myprint(f"Could not set active avatar for user_id {user_id} | Error: {err}")
        return False
    finally:
        cursor.close()
        connection.close()


def get_avatar_trainings(user_id: int) -> list[dict]:
    connection = get_db_connection()
    cursor = connection.cursor(dictionary=True)
    try:
        cursor.execute(
            """SELECT id, training_id, model_ref, trigger_word, status, is_active,
                      created_at, updated_at
               FROM avatar_trainings
               WHERE user_id = %s
               ORDER BY created_at DESC""",
            (user_id,),
        )
        rows = cursor.fetchall()
        return [
            {
                "id": r["id"],
                "training_id": r["training_id"],
                "model_ref": r["model_ref"],
                "trigger_word": r["trigger_word"],
                "status": r["status"],
                "is_active": bool(r["is_active"]),
                "created_at": r["created_at"].isoformat() if r.get("created_at") else None,
                "updated_at": r["updated_at"].isoformat() if r.get("updated_at") else None,
            }
            for r in rows
        ]
    except mysql.connector.Error as err:
        myprint(f"Could not fetch avatar trainings for user_id {user_id} | Error: {err}")
        return []
    finally:
        cursor.close()
        connection.close()


def get_active_avatar(user_id: int) -> Optional[dict]:
    connection = get_db_connection()
    cursor = connection.cursor(dictionary=True)
    try:
        cursor.execute(
            """SELECT id, training_id, model_ref, trigger_word, status
               FROM avatar_trainings
               WHERE user_id = %s AND is_active = 1
               LIMIT 1""",
            (user_id,),
        )
        row = cursor.fetchone()
        if not row:
            return None
        return {
            "id": row["id"],
            "training_id": row["training_id"],
            "model_ref": row["model_ref"],
            "trigger_word": row["trigger_word"],
            "status": row["status"],
        }
    except mysql.connector.Error as err:
        myprint(f"Could not fetch active avatar for user_id {user_id} | Error: {err}")
        return None
    finally:
        cursor.close()
        connection.close()
