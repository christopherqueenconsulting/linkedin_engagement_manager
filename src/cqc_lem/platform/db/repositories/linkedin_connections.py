"""OAuth credentials for separate LinkedIn app connections."""

from datetime import datetime, timezone
from typing import Optional

import mysql.connector

from cqc_lem.platform.db.connection import db_cursor
from cqc_lem.utilities.crypto import decrypt_secret, encrypt_secret, encryption_enabled
from cqc_lem.utilities.logger import log_error

_ACCESS_FIELD = "linkedin_member_analytics_tokens.access_token"


def upsert_member_analytics_token(user_id: int, linked_sub_id: str, access_token: str,
                                  access_token_expires_in: Optional[int]) -> bool:
    """Store the Development-app token encrypted and bound to this user and column."""
    if not encryption_enabled():
        log_error("Refusing member analytics token storage without LEM_SECRET_KEY", user_id=user_id)
        return False
    now = datetime.now(timezone.utc)
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                """INSERT INTO linkedin_member_analytics_tokens
                       (user_id, linked_sub_id, access_token, access_token_expires_in,
                        access_token_created_at)
                   VALUES (%s, %s, %s, %s, %s)
                   ON DUPLICATE KEY UPDATE
                       linked_sub_id = VALUES(linked_sub_id),
                       access_token = VALUES(access_token),
                       access_token_expires_in = VALUES(access_token_expires_in),
                       access_token_created_at = VALUES(access_token_created_at)""",
                (user_id, linked_sub_id,
                 encrypt_secret(access_token, user_id, _ACCESS_FIELD),
                 access_token_expires_in, now),
            )
            return cursor.rowcount > 0
    except mysql.connector.Error as err:
        log_error("Could not store member analytics OAuth token", error_type=type(err).__name__,
                  user_id=user_id)
        return False


def get_member_analytics_token_info(user_id: int) -> Optional[dict]:
    """Return decrypted token metadata for internal OAuth/refresh paths only."""
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute(
                """SELECT linked_sub_id, access_token, access_token_expires_in,
                          access_token_created_at
                   FROM linkedin_member_analytics_tokens WHERE user_id = %s""",
                (user_id,),
            )
            row = cursor.fetchone()
    except mysql.connector.Error as err:
        log_error("Could not read member analytics OAuth token", error_type=type(err).__name__,
                  user_id=user_id)
        return None
    if not row:
        return None
    row["access_token"] = decrypt_secret(row.get("access_token"), user_id, _ACCESS_FIELD)
    return row


def get_member_analytics_access_token(user_id: int) -> Optional[str]:
    """Return a non-expired Development-app token, or None when absent/expired."""
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute(
                """SELECT access_token FROM linkedin_member_analytics_tokens
                   WHERE user_id = %s AND (
                       access_token_created_at IS NULL
                       OR access_token_expires_in IS NULL
                       OR DATE_ADD(access_token_created_at,
                                   INTERVAL access_token_expires_in SECOND) > UTC_TIMESTAMP()
                   )""",
                (user_id,),
            )
            row = cursor.fetchone()
    except mysql.connector.Error as err:
        log_error("Could not read member analytics access token", error_type=type(err).__name__,
                  user_id=user_id)
        return None
    return decrypt_secret(row.get("access_token"), user_id, _ACCESS_FIELD) if row else None


def delete_member_analytics_token(user_id: int) -> bool:
    """Delete only the separate analytics connection; preserve the user's main LEM token."""
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute("DELETE FROM linkedin_member_analytics_tokens WHERE user_id = %s",
                           (user_id,))
            return True
    except mysql.connector.Error as err:
        log_error("Could not delete member analytics OAuth token", error_type=type(err).__name__,
                  user_id=user_id)
        return False
