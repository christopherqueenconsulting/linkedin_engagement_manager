"""Every SQL statement LEM runs against the avatar tables.

Split out of `cqc_lem.utilities.db` (issue #1154). The fail-soft reader contract and the
secret-sealing rules described there apply here unchanged; `cqc_lem.utilities.db`
re-exports every name below, so existing importers and patch targets keep resolving.
"""

import json
from typing import Optional

import mysql.connector

from cqc_lem.platform.db import connection as _connection
from cqc_lem.platform.db.connection import db_cursor
from cqc_lem.platform.db.shared import AVATAR_APPROVAL_PENDING
from cqc_lem.utilities.logger import log_info


def get_avatar_credit_ledger_entry_by_session(stripe_session_id: str) -> Optional[dict]:
    """Return an existing credit ledger entry for a Stripe session (idempotency check)."""
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute(
                "SELECT id, user_id, delta FROM avatar_credit_ledger "
                "WHERE stripe_session_id = %s AND delta > 0 LIMIT 1",
                (stripe_session_id,),
            )
            return cursor.fetchone()
    except mysql.connector.Error as err:
        log_info(f"Could not look up ledger entry for session={stripe_session_id} | Error: {err}")
        return None
def get_avatar_credit_balance(user_id: int) -> int:
    """Avatar credits on hand, as `SUM(delta)` over the append-only ledger.

    There is no balance column by design: every grant, spend and refund is its own row, so a double-spend
    stays visible instead of disappearing into an overwritten total. A read error returns 0, which blocks
    spending rather than granting it.
    """
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute(
                "SELECT COALESCE(SUM(delta), 0) AS balance FROM avatar_credit_ledger WHERE user_id = %s",
                (user_id,),
            )
            row = cursor.fetchone()
            return int(row["balance"]) if row else 0
    except mysql.connector.Error as err:
        log_info(f"Could not fetch avatar credit balance for user_id {user_id} | Error: {err}")
        return 0
def add_avatar_credits(
    user_id: int,
    amount: int,
    reason: str,
    stripe_session_id: Optional[str] = None,
) -> bool:
    """Append a positive ledger entry — a purchase or a grant.

    Nothing here deduplicates: `stripe_session_id` is only recorded, so the idempotency check against a
    replayed webhook is `get_avatar_credit_ledger_entry_by_session`, and it has to run first.
    """
    try:
        with db_cursor(dictionary=True, commit=True) as cursor:
            cursor.execute(
                """INSERT INTO avatar_credit_ledger (user_id, delta, reason, stripe_session_id)
                   VALUES (%s, %s, %s, %s)""",
                (user_id, amount, reason, stripe_session_id),
            )
            return True
    except mysql.connector.Error as err:
        log_info(f"Could not add avatar credits for user_id {user_id} | Error: {err}")
        return False
def deduct_avatar_credit(user_id: int, training_id: str) -> bool:
    """Spend one credit on a training run, as a -1 ledger row tagged with the training id.

    The training id is what lets `refund_avatar_credit` reverse exactly this spend later.
    """
    try:
        with db_cursor(dictionary=True, commit=True) as cursor:
            cursor.execute(
                """INSERT INTO avatar_credit_ledger (user_id, delta, reason, training_id)
                   VALUES (%s, -1, 'training_start', %s)""",
                (user_id, training_id),
            )
            return True
    except mysql.connector.Error as err:
        log_info(f"Could not deduct avatar credit for user_id {user_id} | Error: {err}")
        return False
def refund_avatar_credit(user_id: int, training_id: str) -> bool:
    """Give the credit a training spent back, as a +1 row tagged with the same training id.

    Written automatically when a training lands in 'failed' or 'canceled' (see
    `update_avatar_training_status`). Nothing checks whether a refund was already recorded — two calls
    write two +1 rows.
    """
    try:
        with db_cursor(dictionary=True, commit=True) as cursor:
            cursor.execute(
                """INSERT INTO avatar_credit_ledger (user_id, delta, reason, training_id)
                   VALUES (%s, 1, 'training_refund', %s)""",
                (user_id, training_id),
            )
            return True
    except mysql.connector.Error as err:
        log_info(f"Could not refund avatar credit for user_id {user_id} | Error: {err}")
        return False
def get_video_credit_balance(user_id: int) -> int:
    """Premium-video credits on hand, as `SUM(delta)` over the append-only ledger.

    Same shape as the avatar ledger: no balance column, and a read error returns 0 so a fault blocks
    spending rather than granting it.
    """
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute(
                "SELECT COALESCE(SUM(delta), 0) AS balance FROM video_credit_ledger WHERE user_id = %s",
                (user_id,),
            )
            row = cursor.fetchone()
            return int(row["balance"]) if row else 0
    except mysql.connector.Error as err:
        log_info(f"Could not get video credit balance for user_id {user_id} | Error: {err}")
        return 0
def get_video_credit_ledger_entry_by_session(stripe_session_id: str) -> Optional[dict]:
    """Return an existing purchase ledger entry for a Stripe session (idempotency check)."""
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute(
                "SELECT id, user_id, delta FROM video_credit_ledger WHERE stripe_session_id = %s AND delta > 0 LIMIT 1",
                (stripe_session_id,),
            )
            return cursor.fetchone()
    except mysql.connector.Error as err:
        log_info(f"Could not look up video credit ledger by session | Error: {err}")
        return None
def add_video_credits(user_id: int, amount: int, reason: str,
                      stripe_session_id: Optional[str] = None) -> bool:
    """Append a positive video-credit ledger entry.

    Nothing here deduplicates — `get_video_credit_ledger_entry_by_session` is the replay check, and it
    has to run first.
    """
    try:
        with db_cursor(dictionary=True, commit=True) as cursor:
            cursor.execute(
                """INSERT INTO video_credit_ledger (user_id, delta, reason, stripe_session_id)
                   VALUES (%s, %s, %s, %s)""",
                (user_id, amount, reason, stripe_session_id),
            )
            return True
    except mysql.connector.Error as err:
        log_info(f"Could not add video credits for user_id {user_id} | Error: {err}")
        return False
def deduct_video_credits(user_id: int, amount: int, post_id: Optional[int] = None,
                         reason: str = "premium_video") -> bool:
    """Spend video credits, as a negative ledger row optionally tagged with the post that spent them.

    The amount is written as `-abs(amount)`, so passing it already-negative cannot accidentally GRANT
    credits.
    """
    try:
        with db_cursor(dictionary=True, commit=True) as cursor:
            cursor.execute(
                """INSERT INTO video_credit_ledger (user_id, delta, reason, post_id)
                   VALUES (%s, %s, %s, %s)""",
                (user_id, -abs(amount), reason, post_id),
            )
            return True
    except mysql.connector.Error as err:
        log_info(f"Could not deduct video credits for user_id {user_id} | Error: {err}")
        return False
def refund_video_credits(user_id: int, amount: int, post_id: Optional[int] = None,
                         reason: str = "premium_video_refund") -> bool:
    """Give video credits back, as a positive ledger row (`+abs(amount)`).

    Nothing checks whether the same spend was already refunded.
    """
    try:
        with db_cursor(dictionary=True, commit=True) as cursor:
            cursor.execute(
                """INSERT INTO video_credit_ledger (user_id, delta, reason, post_id)
                   VALUES (%s, %s, %s, %s)""",
                (user_id, abs(amount), reason, post_id),
            )
            return True
    except mysql.connector.Error as err:
        log_info(f"Could not refund video credits for user_id {user_id} | Error: {err}")
        return False
AVATAR_APPROVAL_APPROVED = "approved"
AVATAR_APPROVAL_REJECTED = "rejected"
def insert_avatar_training(user_id: int, training_id: str, trigger_word: str) -> Optional[int]:
    """Record a started avatar training and return its row id; None when the insert failed."""
    try:
        with db_cursor(dictionary=True, commit=True) as cursor:
            cursor.execute(
                """INSERT INTO avatar_trainings (user_id, training_id, trigger_word)
                   VALUES (%s, %s, %s)""",
                (user_id, training_id, trigger_word),
            )
            return cursor.lastrowid
    except mysql.connector.Error as err:
        log_info(f"Could not insert avatar training for user_id {user_id} | Error: {err}")
        return None
def update_avatar_training_status(
    training_id: str,
    status: str,
    model_ref: Optional[str] = None,
) -> bool:
    """Move a training to `status`, optionally recording the trained model reference.

    `model_ref` is only written when supplied, so a status-only update cannot blank a model that is
    already trained. A 'failed' or 'canceled' status ALSO refunds the credit the training spent, and that
    refund is not deduplicated — calling this twice with the same terminal status pays out twice.
    """
    try:
        with db_cursor(dictionary=True, commit=True) as cursor:
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
        log_info(f"Could not update avatar training status for {training_id} | Error: {err}")
        return False
def set_active_avatar(user_id: int, avatar_id: int) -> bool:
    """Make one avatar the account's active likeness.

    The target is validated BEFORE anything is deactivated, so an unknown id or an unapproved avatar
    leaves the current active one exactly as it was — an account is never stranded with no active avatar
    by a failed switch. Activation requires `approval_status == approved` (issue #744).
    """
    connection = _connection.get_db_connection()
    cursor = connection.cursor(dictionary=True)
    try:
        # Validate the target avatar BEFORE deactivating anything — a bad id must leave the
        # user's current active avatar untouched (never strand them with no active avatar).
        cursor.execute(
            "SELECT id, approval_status FROM avatar_trainings WHERE id = %s AND user_id = %s",
            (avatar_id, user_id),
        )
        row = cursor.fetchone()
        if row is None:
            log_info(f"set_active_avatar: avatar {avatar_id} not found for user_id {user_id}")
            return False
        # The approval gate (issue #744): activation used to be reachable straight from
        # 'succeeded', so the first time a user saw their avatar was on a published post.
        if row.get("approval_status") != AVATAR_APPROVAL_APPROVED:
            log_info(f"set_active_avatar: avatar {avatar_id} is not approved "
                    f"(status={row.get('approval_status')}) for user_id {user_id}")
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
        log_info(f"Could not set active avatar for user_id {user_id} | Error: {err}")
        return False
    finally:
        cursor.close()
        connection.close()
def update_avatar_attributes(user_id: int, avatar_id: int,
                             gender_presentation: Optional[str],
                             age_band: Optional[str]) -> bool:
    """Persist the user's SELF-DECLARED likeness attributes (issue #744, decision 3A).

    Values are normalized by ``utilities.avatar.attributes`` first; an unrecognized value is
    stored as NULL, which renders an empty subject clause rather than a guess.
    """
    from cqc_lem.utilities.avatar.attributes import normalize_age_band, normalize_gender_presentation
    gender = normalize_gender_presentation(gender_presentation)
    band = normalize_age_band(age_band)

    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                """UPDATE avatar_trainings
                   SET gender_presentation = %s, age_band = %s, attributes_confirmed_at = UTC_TIMESTAMP()
                   WHERE id = %s AND user_id = %s""",
                (gender, band, avatar_id, user_id),
            )
            return cursor.rowcount > 0
    except mysql.connector.Error as err:
        log_info(f"Could not update avatar attributes for avatar {avatar_id} | Error: {err}")
        return False
def set_avatar_approval(user_id: int, avatar_id: int, status: str) -> bool:
    """Record the user's verdict on their rendered samples.

    A REJECTED avatar is also deactivated in the same statement — leaving a rejected likeness
    active would keep publishing exactly the media the user just rejected.
    """
    if status not in (AVATAR_APPROVAL_PENDING, AVATAR_APPROVAL_APPROVED, AVATAR_APPROVAL_REJECTED):
        log_info(f"set_avatar_approval: refusing unknown approval status {status!r}")
        return False

    try:
        with db_cursor(commit=True) as cursor:
            if status == AVATAR_APPROVAL_APPROVED:
                cursor.execute(
                    """UPDATE avatar_trainings
                       SET approval_status = %s, approved_at = UTC_TIMESTAMP()
                       WHERE id = %s AND user_id = %s""",
                    (status, avatar_id, user_id),
                )
            else:
                cursor.execute(
                    """UPDATE avatar_trainings
                       SET approval_status = %s, approved_at = NULL, is_active = 0
                       WHERE id = %s AND user_id = %s""",
                    (status, avatar_id, user_id),
                )
            return cursor.rowcount > 0
    except mysql.connector.Error as err:
        log_info(f"Could not set approval {status} on avatar {avatar_id} | Error: {err}")
        return False
def update_avatar_samples(avatar_id: int, sample_paths: list[dict]) -> bool:
    """Persist rendered sample assets for an avatar (one JSON list per row).

    The regeneration counter is NOT touched here — it is reserved before any inference is paid
    for by :func:`claim_avatar_sample_render`.
    """
    try:
        with db_cursor(commit=True) as cursor:
            payload = json.dumps(sample_paths or [])
            cursor.execute(
                """UPDATE avatar_trainings
                   SET sample_paths = %s, samples_generated_at = UTC_TIMESTAMP()
                   WHERE id = %s""",
                (payload, avatar_id),
            )
            return cursor.rowcount > 0
    except mysql.connector.Error as err:
        log_info(f"Could not store avatar samples for avatar {avatar_id} | Error: {err}")
        return False
def claim_avatar_sample_render(user_id: int, avatar_id: int, *, regeneration: bool = False,
                               max_regenerations: int = 0) -> bool:
    """Reserve ONE sample render before any inference is paid for. True when the claim won.

    Both callers used to read a counter, decide, and only write it once the render FINISHED —
    minutes later. Two status syncs (a double-clicked Refresh) or two Regenerate clicks therefore
    both passed the same reading and each queued a full three-image render, so the cap the
    regeneration limit exists to be was not one. Reserving inside the UPDATE makes the decision
    atomic: exactly one caller can win.
    """
    try:
        with db_cursor(commit=True) as cursor:
            if regeneration:
                cursor.execute(
                    """UPDATE avatar_trainings
                       SET sample_regen_count = sample_regen_count + 1
                       WHERE id = %s AND user_id = %s AND sample_regen_count < %s""",
                    (avatar_id, user_id, max_regenerations),
                )
            else:
                # The FIRST render after a training succeeds: samples_generated_at is the claim marker,
                # so a second poll arriving mid-render finds it set and stands down.
                cursor.execute(
                    """UPDATE avatar_trainings
                       SET samples_generated_at = UTC_TIMESTAMP()
                       WHERE id = %s AND user_id = %s
                         AND samples_generated_at IS NULL AND sample_paths IS NULL""",
                    (avatar_id, user_id),
                )
            return cursor.rowcount > 0
    except mysql.connector.Error as err:
        log_info(f"Could not claim a sample render for avatar {avatar_id} | Error: {err}")
        return False
def release_avatar_sample_render(user_id: int, avatar_id: int, *,
                                 regeneration: bool = False) -> bool:
    """Give a reservation back when the render produced nothing.

    A user must not lose a regeneration to a render that shipped no images, and a failed first
    render must leave the automatic path able to try again.
    """
    try:
        with db_cursor(commit=True) as cursor:
            if regeneration:
                cursor.execute(
                    """UPDATE avatar_trainings
                       SET sample_regen_count = GREATEST(sample_regen_count - 1, 0)
                       WHERE id = %s AND user_id = %s""",
                    (avatar_id, user_id),
                )
            else:
                cursor.execute(
                    """UPDATE avatar_trainings
                       SET samples_generated_at = NULL
                       WHERE id = %s AND user_id = %s AND sample_paths IS NULL""",
                    (avatar_id, user_id),
                )
            return cursor.rowcount > 0
    except mysql.connector.Error as err:
        log_info(f"Could not release the sample-render claim for avatar {avatar_id} | Error: {err}")
        return False
