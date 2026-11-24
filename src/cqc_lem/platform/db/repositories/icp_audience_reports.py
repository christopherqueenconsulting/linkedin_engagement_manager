"""SQL behind the stored monthly ICP-audience report (issue #2758).

Re-exported by `cqc_lem.utilities.db`. Fail-soft: a DB fault logs and returns the empty value.
`insert_icp_audience_report` returns False BOTH for "this month already has one" and for a fault,
so a caller never believes a row was written when it was not.
"""

import json
from typing import Any, Dict, Optional

import mysql.connector

from cqc_lem.platform.db.connection import db_cursor
from cqc_lem.utilities.logger import log_error


def insert_icp_audience_report(user_id: int, period_key: str, report: Dict[str, Any]) -> bool:
    """Store one month's report. False when the month already has one, or on a fault."""
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                "INSERT IGNORE INTO icp_audience_reports (user_id, period_key, report_json) "
                "VALUES (%s,%s,%s)", (user_id, period_key, json.dumps(report, default=str)))
            return cursor.rowcount > 0
    except mysql.connector.Error as err:
        log_error("Could not store ICP-audience report", exc=err, user_id=user_id)
        return False


def icp_audience_report_exists(user_id: int, period_key: str) -> bool:
    """True when this user already has a report for the month.

    A fault reads as True, so a flaky DB never turns into a duplicate attempt.
    """
    try:
        with db_cursor() as cursor:
            cursor.execute(
                "SELECT 1 FROM icp_audience_reports WHERE user_id=%s AND period_key=%s",
                (user_id, period_key))
            return cursor.fetchone() is not None
    except mysql.connector.Error as err:
        log_error("Could not check ICP-audience report month", exc=err, user_id=user_id)
        return True


def get_latest_icp_audience_report(user_id: int) -> Optional[Dict[str, Any]]:
    """The user's newest stored monthly report as `{period_key, created_at, report}`, or None."""
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute(
                "SELECT period_key, created_at, report_json FROM icp_audience_reports "
                "WHERE user_id=%s ORDER BY period_key DESC LIMIT 1", (user_id,))
            row = cursor.fetchone()
        if not row:
            return None
        return {"period_key": row["period_key"], "created_at": row["created_at"],
                "report": json.loads(row["report_json"])}
    except (mysql.connector.Error, ValueError) as err:
        log_error("Could not read latest ICP-audience report", exc=err, user_id=user_id)
        return None
