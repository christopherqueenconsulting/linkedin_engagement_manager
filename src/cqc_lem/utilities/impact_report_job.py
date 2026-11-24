"""The beat behind the impact report (issue #2643): email the owner, post a one-line GitHub note.

Two reports, both emailed and never posted: one per release whose `published_at` is 14 days old, and
a baseline (trailing 30 days against the 30 before) on the first of each month. GitHub gets ONLY
`impact_report.release_note(tag)` — no metric, name or URL — and only for a release report.

Each report is claimed in Redis BEFORE it is sent, so a re-run never sends twice; a failed send
releases its claim so the next beat retries. No Redis means no claim, which means nothing is sent.
"""

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from cqc_lem.utilities.env_constants import (
    IMPACT_REPORT_EMAIL,
    IMPACT_REPORT_ISSUE,
    IMPACT_REPORT_USER_ID,
    MARGIN_REPORT_EMAIL,
)
from cqc_lem.utilities.impact_report import (
    DEFAULT_DAYS,
    ImpactReadError,
    load_impact_report,
    release_note,
    render_impact_text,
)
from cqc_lem.utilities.logger import log_debug, log_error, log_info, log_warning

TASK_NAME = "auto_impact_report"
# A beat that missed a day still catches a release this many days late.
GRACE_DAYS = 3
BASELINE_DAYS = 30
CLAIM_TTL_SECONDS = 90 * 24 * 3600
_CLAIM_PREFIX = "impact_report:sent:"


def _parse_time(value: Optional[str]) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")) if value else None
    except ValueError:
        return None


def due_releases(releases: Any, now: datetime) -> list[tuple[str, datetime]]:
    """(tag, published_at) for published, non-draft releases aged DEFAULT_DAYS to +GRACE_DAYS."""
    if not isinstance(releases, list):
        return []
    due = []
    for rel in releases:
        if not isinstance(rel, dict) or rel.get("draft"):
            continue
        published, tag = _parse_time(rel.get("published_at")), rel.get("tag_name")
        if not published or not isinstance(tag, str):
            continue
        age = now - published
        # +1 day: the window's last ET day (deploy + DEFAULT_DAYS) must be COMPLETE, or its
        # per-day rates divide a partial day by a whole one.
        if timedelta(days=DEFAULT_DAYS + 1) <= age < timedelta(days=DEFAULT_DAYS + 1 + GRACE_DAYS):
            due.append((tag, published))
    return due


def _recipient() -> str:
    return (IMPACT_REPORT_EMAIL or MARGIN_REPORT_EMAIL or "").strip()


def _claim(client: Any, key: str) -> bool:
    try:
        return bool(client.set(_CLAIM_PREFIX + key, "1", nx=True, ex=CLAIM_TTL_SECONDS))
    except Exception as exc:  # Redis fault: claim nothing, send nothing
        log_warning("Impact report claim failed", exc=exc, task_name=TASK_NAME)
        return False


def _unclaim(client: Any, key: str) -> None:
    try:
        client.delete(_CLAIM_PREFIX + key)
    except Exception as exc:
        log_warning("Impact report claim release failed", exc=exc, task_name=TASK_NAME)


def _send(user_id: int, key: str, label: str, change_at: datetime, days: int,
          to_email: str, client: Any) -> str:
    """Claim, build, email one report. Returns `sent`, `already`, `unread` or `email_failed`."""
    from cqc_lem.utilities.email import send_impact_report_email
    if not _claim(client, key):
        return "already"
    try:
        report = load_impact_report(user_id, change_at, days)
    except ImpactReadError as exc:
        _unclaim(client, key)
        log_error("Impact report inputs unreadable", exc=exc, user_id=user_id, task_name=TASK_NAME)
        return "unread"
    except Exception:
        _unclaim(client, key)  # an unexpected fault must not hold the claim for 90 days
        raise
    try:
        delivered = send_impact_report_email(to_email, f"Impact report: {label}",
                                             render_impact_text(report, label))
    except Exception:
        _unclaim(client, key)
        raise
    if not delivered:
        _unclaim(client, key)
        return "email_failed"
    return "sent"


def run_impact_report_sweep(now: Optional[datetime] = None) -> dict[str, Any]:
    """One beat: every due release report and, on the 1st, the monthly baseline."""
    from cqc_lem.utilities.feedback.issue_service import comment_on_issue, github_request
    from cqc_lem.utilities.flags import IMPACT_REPORT, flag_enabled
    from cqc_lem.utilities.linkedin.rate_limit import shared_redis_client

    now = now or datetime.now(timezone.utc)
    user_id = int(IMPACT_REPORT_USER_ID or 1)
    if not flag_enabled(IMPACT_REPORT, user_id):
        log_debug("Impact report beat skipped: flag off", task_name=TASK_NAME)
        return {"action": "disabled"}
    to_email = _recipient()
    if not to_email:
        log_warning("IMPACT_REPORT_EMAIL/MARGIN_REPORT_EMAIL not set — impact report not sent",
                    task_name=TASK_NAME)
        return {"action": "skipped", "reason": "no recipient"}
    client = shared_redis_client()
    if client is None:
        log_warning("Impact report skipped — Redis unavailable, so nothing can be claimed",
                    task_name=TASK_NAME)
        return {"action": "skipped", "reason": "no redis"}

    results: dict[str, str] = {}
    for tag, published in due_releases(github_request("GET", "releases?per_page=30"), now):
        outcome = _send(user_id, f"release:{tag}", tag, published, DEFAULT_DAYS, to_email, client)
        results[tag] = outcome
        note = release_note(tag)
        if outcome == "sent" and note:
            comment_on_issue(int(IMPACT_REPORT_ISSUE), note)
    if now.day == 1:
        key = f"baseline:{now:%Y-%m}"
        results[key] = _send(user_id, key, f"monthly baseline {now:%Y-%m}",
                             now - timedelta(days=BASELINE_DAYS + 1), BASELINE_DAYS, to_email, client)
    log_info(f"Impact report beat finished: {len(results)} considered", user_id=user_id,
             task_name=TASK_NAME)
    return {"action": "ran", "results": results}
