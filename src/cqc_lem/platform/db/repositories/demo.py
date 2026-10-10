"""The SQL behind `scripts/seed_demo_account.py`, the synthetic account used for product recordings.

Split from the real aggregates on purpose: nothing in the app calls these, and every statement here is
bound to ONE synthetic account. The account is identified by an email on a reserved example domain
(RFC 2606), which no real signup can own, and every write repeats that email in its WHERE clause, so
a wrong `user_id` matches nothing rather than another person's rows. Everything else the seed does
(drafts, gate findings, rejection reasons, preferences) goes through the same repository functions
the app uses, so the SPA renders the seeded rows exactly as it renders real ones.

`cqc_lem.utilities.db` re-exports every public name below.
"""

import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

import mysql.connector

from cqc_lem.platform.db.connection import db_cursor, to_naive_utc
from cqc_lem.platform.db.enums import PostStatus, PostType
from cqc_lem.utilities.logger import log_error

#: The only email domains the demo statements accept. Both are reserved for documentation and can
#: never be registered, so no real account can hold an address on them.
DEMO_EMAIL_DOMAINS = ("@example.com", "@example.org")


class NotADemoAccount(ValueError):
    """Raised when a demo statement is handed an email outside the reserved example domains."""


def _require_demo_email(email: str) -> str:
    """Return the normalised email, or raise when it is not on a reserved example domain.

    Raises:
        NotADemoAccount: The email could belong to a real person.
    """
    normalised = (email or "").strip().lower()
    if not normalised.endswith(DEMO_EMAIL_DOMAINS) or normalised.startswith("@"):
        raise NotADemoAccount(f"refusing a demo write for a non-demo email: {email!r}")
    return normalised


def ensure_demo_user(email: str, trial_days: int = 14) -> Optional[int]:
    """Find or create the synthetic demo account and restart its trial window.

    Unlike `add_user_by_email` this never creates a Stripe customer, so the seed makes no network
    call. The trial is re-stamped on every run so a recording made weeks later still shows an active
    trial.

    Args:
        email: The demo address; must be on a reserved example domain.
        trial_days: Length of the restarted trial window.

    Returns:
        The account's id, or None when the database could not be written.

    Raises:
        NotADemoAccount: The email could belong to a real person.
    """
    email = _require_demo_email(email)
    now = datetime.now(timezone.utc)
    started, ends = to_naive_utc(now), to_naive_utc(now + timedelta(days=trial_days))
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute("SELECT id FROM users WHERE email = %s", (email,))
            row = cursor.fetchone()
            if row:
                user_id = int(row[0])
            else:
                cursor.execute("INSERT INTO users (email, public_uid) VALUES (%s, %s)",
                               (email, str(uuid.uuid4())))
                user_id = int(cursor.lastrowid)
            cursor.execute(
                "UPDATE users SET subscription_status = 'trial', subscription_tier = 'free_trial', "
                "trial_started_at = %s, trial_ends_at = %s WHERE id = %s AND email = %s",
                (started, ends, user_id, email))
            return user_id
    except mysql.connector.Error as err:
        log_error("Could not create the demo account", exc=err)
        return None


def reset_demo_user_rows(user_id: int, email: str) -> Optional[dict]:
    """Delete the demo account's posts and engagement preferences, and nothing else.

    Each DELETE joins `users` on BOTH the id and the demo email, so an id that belongs to anyone else
    deletes zero rows. Rows hanging off a post (logs, approvals, shipped variants) go with it through
    their ON DELETE CASCADE keys. The account row, and so any signed-in session, is kept.

    Args:
        user_id: The demo account's id, as `ensure_demo_user` returned it.
        email: The demo address; must be on a reserved example domain.

    Returns:
        Rows deleted per table, or None when the database could not be written.

    Raises:
        NotADemoAccount: The email could belong to a real person.
    """
    email = _require_demo_email(email)
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                "DELETE p FROM posts p JOIN users u ON u.id = p.user_id "
                "WHERE u.id = %s AND u.email = %s", (user_id, email))
            posts = cursor.rowcount
            cursor.execute(
                "DELETE ep FROM engagement_preferences ep JOIN users u ON u.id = ep.user_id "
                "WHERE u.id = %s AND u.email = %s", (user_id, email))
            return {"posts": posts, "engagement_preferences": cursor.rowcount}
    except mysql.connector.Error as err:
        log_error("Could not reset the demo account", exc=err, user_id=user_id)
        return None


def insert_demo_post(user_id: int, email: str, content: str, status: PostStatus,
                     scheduled_time: datetime, post_type: PostType = PostType.TEXT,
                     buyer_stage: Optional[str] = None,
                     content_mix: Optional[str] = None) -> Optional[int]:
    """Insert one fully written post for the demo account and return its id.

    `insert_planned_post` only writes the 'TBD' skeleton and returns no id, and the gate findings and
    rejection reason are then written onto this row by the app's own setters, which need one.

    Args:
        user_id: The demo account's id.
        email: The demo address; the INSERT only lands when it owns `user_id`.
        content: The post body.
        status: Where the post sits in the review flow.
        scheduled_time: The slot, timezone-aware or naive UTC.
        post_type: Text, carousel or video.
        buyer_stage: awareness / consideration / decision, or None.
        content_mix: value / authority / promo, or None.

    Returns:
        The new post's id, or None when nothing was written.

    Raises:
        NotADemoAccount: The email could belong to a real person.
    """
    email = _require_demo_email(email)
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                "INSERT INTO posts (scheduled_time, post_type, user_id, buyer_stage, content_mix, "
                "status, content) "
                "SELECT %s, %s, u.id, %s, %s, %s, %s FROM users u WHERE u.id = %s AND u.email = %s",
                (to_naive_utc(scheduled_time), post_type.value, buyer_stage, content_mix,
                 status.value, content, user_id, email))
            return int(cursor.lastrowid) if cursor.rowcount == 1 else None
    except mysql.connector.Error as err:
        log_error("Could not insert a demo post", exc=err, user_id=user_id)
        return None
