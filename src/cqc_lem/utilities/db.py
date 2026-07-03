import json
import os
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Optional

import mysql.connector
from cqc_lem.utilities.env_constants import AWS_MYSQL_SECRET_NAME, AWS_REGION
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


class PostStatus(StrEnum):
    PLANNING = 'planning'
    PENDING = 'pending'
    APPROVED = 'approved'
    REJECTED = 'rejected'
    SCHEDULED = 'scheduled'
    POSTED = 'posted'
    ERROR = 'error'  # generation/posting failed (e.g. no real carousel images) — needs manual/dev fix


# Enum for log actions types
class LogActionType(StrEnum):
    COMMENT = 'comment'
    DM = 'dm'
    REPLY = 'reply'
    POST = 'post'
    ENGAGED = 'engaged'


# ENum for log result options
class LogResultType(StrEnum):
    SUCCESS = 'success'
    FAILURE = 'failure'


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
                video_quality: str = "standard") -> bool:
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
            INSERT INTO posts (content, scheduled_time, post_type, user_id, video_url, carousel_slides, video_quality)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
        """, (content, scheduled_time, post_type.value, user_id, video_url, slides_json,
              video_quality or "standard"))

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


def get_posts(user_id: int, limit: int = 10, offset: int = 0,
              sort_order: str = 'asc', status_filter: Optional[str] = None) -> tuple[list, int]:
    connection = get_db_connection()
    cursor = connection.cursor(dictionary=True)

    order = 'ASC' if sort_order.lower() != 'desc' else 'DESC'

    try:
        where = "WHERE user_id = %s"
        params: list = [user_id]
        if status_filter:
            where += " AND status = %s"
            params.append(status_filter.lower())

        cursor.execute(
            f"SELECT COUNT(*) AS total FROM posts {where}",
            params
        )
        total = cursor.fetchone()['total']

        cursor.execute(
            f"SELECT id, content, video_url, scheduled_time, post_type, status, carousel_slides "
            f"FROM posts {where} ORDER BY scheduled_time {order} LIMIT %s OFFSET %s",
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
                      sort_order: str = 'asc', status_filter: Optional[str] = None) -> tuple[list, int]:
    user_id = get_user_id(email)

    if not user_id:
        myprint(f"User with email {email} not found.")
        return [], 0

    return get_posts(user_id, limit=limit, offset=offset, sort_order=sort_order, status_filter=status_filter)


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
    connection = get_db_connection()
    cursor = connection.cursor()

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
        myprint(f"Could not remove linkedin profile by url | Error: {err}")
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
        post_counts = 0
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
    expires_at = now + timedelta(hours=24)
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
    connection = get_db_connection()
    cursor = connection.cursor(dictionary=True)
    try:
        cursor.execute(
            "SELECT user_id FROM sessions WHERE session_token = %s AND expires_at > %s",
            (token, datetime.now(timezone.utc)),
        )
        row = cursor.fetchone()
        return row['user_id'] if row else None
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
    "tone": None, "comment_length": "medium", "comment_style": None,
    "use_emojis": True, "use_hashtags": False,
    "include_topics": [], "exclude_topics": [], "include_keywords": [], "exclude_keywords": [],
    "include_authors": [], "exclude_authors": [], "post_types": [],
    "min_reactions": None, "reply_to_own_comments": True,
    "max_comments_per_day": 20, "max_dms_per_day": 20, "default_buyer_stage": None,
}
_ENGAGEMENT_JSON_FIELDS = ("include_topics", "exclude_topics", "include_keywords",
                           "exclude_keywords", "include_authors", "exclude_authors", "post_types")
_ENGAGEMENT_BOOL_FIELDS = ("use_emojis", "use_hashtags", "reply_to_own_comments")
_ENGAGEMENT_COLS = ("tone", "comment_length", "comment_style", "use_emojis", "use_hashtags",
                    "include_topics", "exclude_topics", "include_keywords", "exclude_keywords",
                    "include_authors", "exclude_authors", "post_types", "min_reactions",
                    "reply_to_own_comments", "max_comments_per_day", "max_dms_per_day",
                    "default_buyer_stage")


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
    """Return the IANA timezone string for the user, defaulting to UTC."""
    connection = get_db_connection()
    cursor = connection.cursor()
    try:
        cursor.execute("SELECT timezone FROM users WHERE id = %s", (user_id,))
        row = cursor.fetchone()
        return row[0] if row and row[0] else 'UTC'
    except mysql.connector.Error as err:
        myprint(f"Could not get timezone for user_id {user_id} | Error: {err}")
        return 'UTC'
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
                 OR (post_type = 'carousel' AND (
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
        cursor.execute(
            "UPDATE avatar_trainings SET is_active = 0 WHERE user_id = %s",
            (user_id,),
        )
        cursor.execute(
            "UPDATE avatar_trainings SET is_active = 1 WHERE id = %s AND user_id = %s",
            (avatar_id, user_id),
        )
        connection.commit()
        return cursor.rowcount > 0
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
