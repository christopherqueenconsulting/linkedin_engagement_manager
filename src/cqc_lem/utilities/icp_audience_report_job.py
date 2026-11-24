"""The monthly ICP-audience report beat's worker (issue #2758).

One stored report per user per month (`2026-11`); in-app only, nothing is sent. A report that could
not be read (`readable` False) is never stored, so the page shows the previous month rather than an
empty one, and the next run can try again.
"""

from datetime import datetime, timezone
from typing import Dict, Optional

from cqc_lem.utilities.logger import log_info, log_warning


def month_key(now: datetime) -> str:
    """`2026-11`."""
    return f"{now.year}-{now.month:02d}"


def store_user_report(user_id: int, now: datetime) -> str:
    """Build and store one user's report. Returns 'stored', 'exists', 'unreadable' or 'failed'."""
    from cqc_lem.utilities.db import icp_audience_report_exists, insert_icp_audience_report
    from cqc_lem.utilities.icp_audience_report import load_icp_audience_report
    key = month_key(now)
    if icp_audience_report_exists(user_id, key):
        return "exists"
    report = load_icp_audience_report(user_id, now)
    if not report.get("readable"):
        return "unreadable"
    return "stored" if insert_icp_audience_report(user_id, key, report) else "failed"


def run_monthly_icp_audience_reports(now: Optional[datetime] = None) -> Dict[str, int]:
    """Run the report for every active, flag-enabled user; one user's failure never stops the rest."""
    from cqc_lem.utilities.db import get_active_user_ids
    from cqc_lem.utilities.flags import WEEKLY_REPORT, flag_enabled
    stamp = now or datetime.now(timezone.utc)
    counts = {"stored": 0, "exists": 0, "unreadable": 0, "failed": 0, "skipped": 0}
    for user_id in get_active_user_ids() or []:
        try:
            if not flag_enabled(WEEKLY_REPORT, user_id):
                counts["skipped"] += 1
                continue
            counts[store_user_report(user_id, stamp)] += 1
        except Exception as exc:
            log_warning("ICP-audience monthly report failed for a user", exc=exc, user_id=user_id,
                        task_name="auto_icp_audience_report_monthly")
            counts["failed"] += 1
    if counts["stored"]:
        log_info(f"ICP-audience monthly reports: {counts}", task_name="auto_icp_audience_report_monthly")
    return counts
