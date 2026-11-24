"""Calendly booking hook storage (#2658): the encrypted PAT and the applied-booking ledger."""

from datetime import datetime, timezone
from typing import Optional

import mysql.connector

from cqc_lem.platform.db.connection import db_cursor
from cqc_lem.platform.db.enums import LeadStage, LeadStageSource
from cqc_lem.utilities.crypto import decrypt_secret, encrypt_secret, encryption_enabled
from cqc_lem.utilities.logger import log_error

# AAD for the Calendly personal access token. Renaming it orphans every stored token.
CALENDLY_TOKEN_FIELD = "calendly_credentials.access_token"

BOOKING_APPLIED = "booked"
BOOKING_CREATED = "created"
BOOKING_CANCELED = "canceled"
BOOKING_NOOP = "noop"
BOOKING_ERROR = "error"


def upsert_calendly_token(user_id: int, access_token: str) -> bool:
    """Store the user's Calendly PAT encrypted and bound to this user and column."""
    if not encryption_enabled():
        log_error("Refusing Calendly token storage without LEM_SECRET_KEY", user_id=user_id)
        return False
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                "INSERT INTO calendly_credentials (user_id, access_token) VALUES (%s, %s) "
                "ON DUPLICATE KEY UPDATE access_token = VALUES(access_token)",
                (user_id, encrypt_secret(access_token, user_id, CALENDLY_TOKEN_FIELD)))
            return True
    except mysql.connector.Error as err:
        log_error("Could not store Calendly token", error_type=type(err).__name__, user_id=user_id)
        return False


def get_calendly_token(user_id: int) -> Optional[str]:
    """The decrypted PAT, or None when absent or unreadable."""
    try:
        with db_cursor() as cursor:
            cursor.execute("SELECT access_token FROM calendly_credentials WHERE user_id = %s", (user_id,))
            row = cursor.fetchone()
    except mysql.connector.Error as err:
        log_error("Could not read Calendly token", error_type=type(err).__name__, user_id=user_id)
        return None
    if not row:
        return None
    return decrypt_secret(row[0], user_id, CALENDLY_TOKEN_FIELD)


def get_calendly_user_ids() -> list:
    """Ids of users who have stored a Calendly token."""
    try:
        with db_cursor() as cursor:
            cursor.execute("SELECT user_id FROM calendly_credentials")
            return [r[0] for r in (cursor.fetchall() or [])]
    except mysql.connector.Error as err:
        log_error("Could not list Calendly users", error_type=type(err).__name__)
        return []


def touch_calendly_polled(user_id: int) -> None:
    """Record that a poll completed."""
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute("UPDATE calendly_credentials SET last_polled_at = %s WHERE user_id = %s",
                           (datetime.now(timezone.utc).replace(tzinfo=None), user_id))
    except mysql.connector.Error as err:
        log_error("Could not record Calendly poll time", error_type=type(err).__name__, user_id=user_id)


def _find_lead(cursor, user_id: int, email: str, name: Optional[str], window_days: int) -> Optional[int]:
    """Lead id by email key, then by a UNIQUE name match with a signal inside the window."""
    if email:
        cursor.execute("SELECT id FROM leads WHERE user_id=%s AND person_key=%s",
                       (user_id, f"email:{email.lower()}"[:255]))
        row = cursor.fetchone()
        if row:
            return row[0]
    if name:
        cursor.execute(
            "SELECT id FROM leads WHERE user_id=%s AND LOWER(person_name)=LOWER(%s) "
            "AND last_signal_at >= DATE_SUB(UTC_TIMESTAMP(), INTERVAL %s DAY)",
            (user_id, name, window_days))
        rows = cursor.fetchall() or []
        if len(rows) == 1:  # ambiguous is treated as unmatched: a new lead beats mislabelling one
            return rows[0][0]
    return None


def apply_booking(user_id: int, invitee_uri: str, event_uri: str, email: Optional[str],
                  name: Optional[str], canceled: bool, window_days: int = 90) -> str:
    """Apply one Calendly invitee to the lead board, idempotently, in one transaction.

    A live booking moves the matched lead to `meeting_booked` (creating a lead for an unmatched
    invitee) and writes a `booking` stage event. A cancel of a booking we applied restores the lead's
    prior manual stage with a second event. Replays and cancels of unseen bookings change nothing.
    Returns one of the BOOKING_* outcomes.
    """
    if not invitee_uri:
        return BOOKING_NOOP
    insert_event = ("INSERT INTO lead_stage_events (lead_id, user_id, from_stage, to_stage, source, "
                    "evidence_ref) VALUES (%s,%s,%s,%s,%s,%s)")
    source = LeadStageSource.BOOKING.value
    booked = LeadStage.MEETING_BOOKED.value
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute("SELECT lead_id, status, prior_manual_stage FROM calendly_bookings "
                           "WHERE user_id=%s AND invitee_uri=%s FOR UPDATE", (user_id, invitee_uri))
            seen = cursor.fetchone()
            if canceled:
                if not seen or seen[1] == "canceled":
                    return BOOKING_NOOP
                lead_id, _, prior = seen
                cursor.execute("SELECT manual_stage, stage FROM leads WHERE id=%s FOR UPDATE", (lead_id,))
                lead = cursor.fetchone()
                if lead and lead[0] == booked:
                    cursor.execute("UPDATE leads SET manual_stage=%s WHERE id=%s", (prior, lead_id))
                    cursor.execute(insert_event, (lead_id, user_id, booked, prior or lead[1], source,
                                                  f"canceled:{invitee_uri}"[:255]))
                cursor.execute("UPDATE calendly_bookings SET status='canceled' WHERE user_id=%s "
                               "AND invitee_uri=%s", (user_id, invitee_uri))
                return BOOKING_CANCELED
            if seen:
                return BOOKING_NOOP
            lead_id = _find_lead(cursor, user_id, email or "", name, window_days)
            outcome = BOOKING_APPLIED
            if lead_id is None:
                key = f"email:{email.lower()}" if email else f"calendly:{invitee_uri}"
                cursor.execute(
                    "INSERT INTO leads (user_id, person_key, person_name, stage) VALUES (%s,%s,%s,%s) "
                    "ON DUPLICATE KEY UPDATE id=LAST_INSERT_ID(id)",
                    (user_id, key[:255], name or None, LeadStage.COLD.value))
                lead_id = cursor.lastrowid
                outcome = BOOKING_CREATED
            cursor.execute("SELECT manual_stage, stage FROM leads WHERE id=%s FOR UPDATE", (lead_id,))
            prior, computed = cursor.fetchone()
            cursor.execute("UPDATE leads SET manual_stage=%s WHERE id=%s", (booked, lead_id))
            cursor.execute(insert_event, (lead_id, user_id,
                                          None if outcome == BOOKING_CREATED else (prior or computed),
                                          booked, source, invitee_uri[:255]))
            cursor.execute(
                "INSERT INTO calendly_bookings (user_id, invitee_uri, event_uri, lead_id, prior_manual_stage) "
                "VALUES (%s,%s,%s,%s,%s)", (user_id, invitee_uri, (event_uri or "")[:255], lead_id, prior))
            return outcome
    except mysql.connector.Error as err:
        log_error("Could not apply Calendly booking", error_type=type(err).__name__, user_id=user_id)
        return BOOKING_ERROR
