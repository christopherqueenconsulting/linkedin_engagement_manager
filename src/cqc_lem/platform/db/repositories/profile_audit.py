"""Every SQL statement behind the profile audit history (issue #2631, Phase 1).

Re-exported by `cqc_lem.utilities.db`. Fail-soft like the rest of the facade: a DB fault logs and
returns the empty value. `insert_profile_audit` returns None BOTH for "this period already has an
audit" and for a fault, so a caller never believes a row was written when it was not.
"""

from typing import Any, Dict, List, Optional, Sequence

import mysql.connector

from cqc_lem.platform.db.connection import db_cursor
from cqc_lem.utilities.logger import log_error

PROFILE_AUDIT_KINDS = ("weekly", "monthly")


def insert_profile_audit(user_id: int, kind: str, period_key: str, rubric_version: str,
                         score: Optional[int], findings: Sequence[Dict[str, Any]],
                         previous_audit_id: Optional[int] = None, closed_count: int = 0,
                         opened_count: int = 0) -> Optional[int]:
    """Store one audit and its findings. None when the period already has one, or on a fault."""
    if kind not in PROFILE_AUDIT_KINDS:
        return None
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                "INSERT IGNORE INTO profile_audits (user_id, kind, period_key, rubric_version, "
                "score, previous_audit_id, closed_count, opened_count) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                (user_id, kind, period_key, rubric_version, score, previous_audit_id,
                 closed_count, opened_count))
            if cursor.rowcount <= 0:
                return None
            audit_id = int(cursor.lastrowid)
            cursor.executemany(
                "INSERT INTO profile_audit_findings (audit_id, check_id, status, weight, detail, "
                "fix_hint) VALUES (%s,%s,%s,%s,%s,%s)",
                [(audit_id, f["check_id"], f["status"], int(f["weight"]),
                  str(f.get("detail", ""))[:255], str(f.get("fix_hint", ""))[:255])
                 for f in findings])
            return audit_id
    except mysql.connector.Error as err:
        log_error("Could not store profile audit", exc=err, user_id=user_id)
        return None


def get_profile_audit_findings(audit_id: int) -> List[Dict[str, Any]]:
    """The findings of one audit, as dicts. A fault reads as an empty list."""
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute(
                "SELECT check_id, status, weight, detail, fix_hint FROM profile_audit_findings "
                "WHERE audit_id=%s ORDER BY check_id", (audit_id,))
            return list(cursor.fetchall() or [])
    except mysql.connector.Error as err:
        log_error("Could not read profile audit findings", exc=err)
        return []


def get_latest_profile_audit(user_id: int) -> Optional[Dict[str, Any]]:
    """The user's newest audit row (any kind), or None. A fault reads as None."""
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute(
                "SELECT id, user_id, kind, period_key, rubric_version, score, previous_audit_id, "
                "closed_count, opened_count, created_at FROM profile_audits "
                "WHERE user_id=%s ORDER BY id DESC LIMIT 1", (user_id,))
            return cursor.fetchone()
    except mysql.connector.Error as err:
        log_error("Could not read latest profile audit", exc=err, user_id=user_id)
        return None


def profile_audit_exists(user_id: int, kind: str, period_key: str) -> bool:
    """True when this user already has an audit for the period.

    A fault reads as True, so a flaky DB never turns into a duplicate attempt.
    """
    try:
        with db_cursor() as cursor:
            cursor.execute(
                "SELECT 1 FROM profile_audits WHERE user_id=%s AND kind=%s AND period_key=%s",
                (user_id, kind, period_key))
            return cursor.fetchone() is not None
    except mysql.connector.Error as err:
        log_error("Could not check profile audit period", exc=err, user_id=user_id)
        return True
