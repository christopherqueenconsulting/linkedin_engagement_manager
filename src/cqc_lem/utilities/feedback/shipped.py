"""Tell the people who asked that their fix shipped — issue #502, the LAST stage of the
feedback->auto-work loop (docs/launch-and-marketing-plan.md §B.4).

    capture (#496) ─► classify (#497) ─► file (#498) ─► ship ─► THIS ─► micro-CSAT ─► back to #496

A merged PR that says `Closes #<issue>` is the only signal that something the pipeline built for a
feedback cluster actually landed. This stage turns that into:

    merged PR ──► closing issue #N ──► the cluster's reporters
                     │                     ├─ one changelog line ("**Fixed:** … (#N)")
                     │                     ├─ one email + one in-app notice, ONCE per reporter
                     │                     └─ a scheduled micro-CSAT: "did this fix it?"
                     └─ the cluster's feedback rows move to `resolved`

The micro-CSAT answer is written back as an ordinary `feedback` row (source `csat`), so a "no, still
broken" re-enters the very loop that produced the fix instead of dying in an analytics dashboard.

Notify-once is a DB fact, not a timer: `shipped_notice_recipients` has (notice_id, user_id) as its
primary key, so re-running this pass over the same merged PR adds nothing. Everything here is
best-effort — a GitHub outage or a bounced email must never lose the notice, it just retries next
pass (the recipient row is written only when the email actually went out).

Privacy: nothing about a reporter ever reaches GitHub — this stage only READS merged PRs.

Env:
  FEEDBACK_GITHUB_TOKEN / GITHUB_TOKEN — token used to read merged PRs; no token == nothing runs
  FEEDBACK_GITHUB_REPO                 — owner/name (default is this repo)
  FEEDBACK_SHIPPED_LOOKBACK_HOURS      — how far back merged PRs are scanned (default 48)
  FEEDBACK_FIX_CSAT_DELAY_HOURS        — how long after the notice the in-app CSAT is asked
                                         (default 24; 0 = ask immediately)
"""

import os
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

from cqc_lem.utilities.db import (
    get_feedback_reporters_for_issue, get_shipped_notice_by_issue,
    get_shipped_notice_recipient_ids, mark_feedback_resolved_for_issue, record_shipped_notice,
    record_shipped_notice_recipient,
)
from cqc_lem.utilities.feedback.issue_service import github_request, github_token
from cqc_lem.utilities.logger import log_debug, log_error, log_info, log_warning

LOOKBACK_HOURS_DEFAULT = 48
FIX_CSAT_DELAY_HOURS_DEFAULT = 24
MAX_PRS_PER_PASS = 50
MAX_CHANGELOG_CHARS = 512

# GitHub's own closing keywords (https://docs.github.com/issues/tracking-your-work-with-issues/
# using-keywords-in-issues-and-pull-requests) — the ONLY thing that counts as "this PR shipped that
# issue". A bare `#123` reference is deliberately NOT matched: mentioning an issue isn't fixing it.
_CLOSES_RE = re.compile(
    r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\b[\s:]+#(\d+)", re.IGNORECASE)

# `feat(scope): summary (closes #12)` -> `summary`. Both halves are stripped so the changelog line
# reads like a product note instead of a commit subject.
_COMMIT_PREFIX_RE = re.compile(r"^\s*(\w+)(?:\([^)]*\))?!?:\s*")
_TRAILING_REF_RE = re.compile(
    r"\s*[(\[]?\s*(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s*:?\s*#\d+\s*[)\]]?\s*$",
    re.IGNORECASE)

# What each conventional-commit type means to a USER — the changelog line's lead-in.
_CHANGE_LABELS: dict = {
    'fix': 'Fixed', 'feat': 'New', 'perf': 'Faster', 'refactor': 'Improved',
    'docs': 'Documented', 'style': 'Improved', 'chore': 'Improved', 'test': 'Improved',
    'build': 'Improved', 'ci': 'Improved', 'revert': 'Reverted',
}
DEFAULT_CHANGE_LABEL = 'Improved'


class ShipAction:
    """What `notify_shipped_issue` did — the contract callers/tests assert on."""
    NOTIFIED = 'notified'        # at least one reporter was told about this fix
    NO_REPORTERS = 'no_reporters'  # the issue has no feedback cluster behind it — nothing to say
    ALREADY_SENT = 'already_sent'  # every reporter had already been notified
    ERROR = 'error'              # the changelog line could not be recorded; retried next pass


def lookback_hours() -> int:
    raw = (os.environ.get("FEEDBACK_SHIPPED_LOOKBACK_HOURS") or "").strip()
    try:
        return max(1, min(720, int(float(raw)))) if raw else LOOKBACK_HOURS_DEFAULT
    except ValueError:
        return LOOKBACK_HOURS_DEFAULT


def fix_csat_delay_hours() -> int:
    """How long a reporter keeps the fix before the in-app "did this fix it?" appears. This is the
    'schedule' half of the micro-CSAT — asking in the same minute as the email measures nothing."""
    raw = (os.environ.get("FEEDBACK_FIX_CSAT_DELAY_HOURS") or "").strip()
    try:
        return max(0, min(720, int(float(raw)))) if raw else FIX_CSAT_DELAY_HOURS_DEFAULT
    except ValueError:
        return FIX_CSAT_DELAY_HOURS_DEFAULT


# --- Parsing / changelog copy (pure) -------------------------------------------------------------

def closing_issue_numbers(*texts: Optional[str]) -> list:
    """Every issue number a merged PR's title/body says it CLOSES, de-duplicated in first-seen
    order. Only GitHub's closing keywords count, so "related to #12" never claims a fix shipped."""
    found: list = []
    for text in texts:
        for match in _CLOSES_RE.finditer(text or ""):
            number = int(match.group(1))
            if number not in found:
                found.append(number)
    return found


def humanize_title(pr_title: Optional[str]) -> tuple:
    """(change_label, plain summary) from a conventional-commit PR title. `feat(dms): add X
    (closes #9)` -> ('New', 'Add X'). An unprefixed title keeps its own words."""
    title = (pr_title or "").strip()
    label = DEFAULT_CHANGE_LABEL
    prefix = _COMMIT_PREFIX_RE.match(title)
    if prefix:
        label = _CHANGE_LABELS.get(prefix.group(1).lower(), DEFAULT_CHANGE_LABEL)
        title = title[prefix.end():]
    title = _TRAILING_REF_RE.sub("", title).strip()
    if title:
        title = title[0].upper() + title[1:]
    return label, title


def build_changelog_line(pr_title: Optional[str], issue_number: int,
                         pr_number: Optional[int] = None) -> str:
    """The human-readable changelog entry for a shipped fix — what the reporter reads in the email
    and the in-app notice, and what the "what's new" feed lists."""
    label, summary = humanize_title(pr_title)
    summary = summary or "A fix you reported"
    refs = f"#{int(issue_number)}" + (f", PR #{int(pr_number)}" if pr_number else "")
    return f"**{label}:** {summary} ({refs})"[:MAX_CHANGELOG_CHARS]


# --- GitHub reads --------------------------------------------------------------------------------

def _parse_github_time(value: Optional[str]) -> Optional[datetime]:
    """GitHub's ISO-8601 `...Z` timestamp as an aware UTC datetime, or None when unparseable."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def merged_pull_requests(hours: int = None, limit: int = MAX_PRS_PER_PASS) -> list:
    """PRs merged into the default branch within the lookback window, newest first. Returns [] when
    there is no token or GitHub is unreachable — this stage is best-effort."""
    if not github_token():
        log_warning("Shipped-fix notify skipped — no FEEDBACK_GITHUB_TOKEN/GITHUB_TOKEN set",
                    api_provider="github")
        return []
    window = timedelta(hours=lookback_hours() if hours is None else max(1, int(hours)))
    cutoff = datetime.now(timezone.utc) - window
    data = github_request(
        "GET", f"pulls?state=closed&sort=updated&direction=desc&per_page={int(limit)}")
    if not isinstance(data, list):
        return []
    merged = []
    for pr in data:
        if not isinstance(pr, dict):
            continue
        merged_at = _parse_github_time(pr.get("merged_at"))
        if merged_at and merged_at >= cutoff:
            merged.append(pr)
    return merged


# --- Notification --------------------------------------------------------------------------------

def _result(action: str, issue_number: Optional[int], **extra) -> dict:
    return {"action": action, "issue_number": issue_number, **extra}


def notify_shipped_issue(issue_number: int, pr_title: str = None,
                         pr_number: int = None) -> dict:
    """Take ONE shipped issue to its reporters: record the changelog line, email + queue the in-app
    "you asked, we shipped" notice for every reporter who hasn't had one, and mark the cluster
    resolved. Never raises; safe to re-run (each reporter is notified at most once)."""
    reporters = get_feedback_reporters_for_issue(issue_number)
    if not reporters:
        log_debug(f"Issue #{issue_number} shipped but has no feedback reporters — no notice sent")
        return _result(ShipAction.NO_REPORTERS, issue_number)

    existing = get_shipped_notice_by_issue(issue_number)
    line = (existing or {}).get("changelog_line") or build_changelog_line(
        pr_title, issue_number, pr_number)
    notice_id = (existing or {}).get("id") or record_shipped_notice(
        issue_number, line, pr_number=pr_number, title=(pr_title or "").strip()[:255] or None)
    if not notice_id:
        return _result(ShipAction.ERROR, issue_number, reason="could not record changelog line")

    from cqc_lem.utilities.notifications import notify_shipped_fix
    # Read the ledger BEFORE sending: the recipient PK stops a duplicate ROW, but only this check
    # stops a duplicate EMAIL when an overlapping window re-scans the same merged PR.
    already = set(get_shipped_notice_recipient_ids(notice_id))
    pending = [user_id for user_id in reporters if user_id not in already]
    notified = 0
    for user_id in pending:
        try:
            if not notify_shipped_fix(user_id, line, issue_number):
                continue
            # Written only AFTER the email went out, so a mail outage retries next pass instead of
            # silently marking the reporter as told.
            if record_shipped_notice_recipient(notice_id, user_id):
                notified += 1
        except Exception as e:
            log_warning("Could not notify reporter of shipped fix", exc=e, user_id=user_id)

    resolved = mark_feedback_resolved_for_issue(issue_number)
    if not pending:
        return _result(ShipAction.ALREADY_SENT, issue_number, notice_id=notice_id,
                       reporters=len(reporters), resolved=resolved)
    if not notified:
        # Reporters were owed a notice and NONE could be delivered — a mail outage, not a no-op.
        # Left un-ledgered on purpose so the next pass retries them.
        return _result(ShipAction.ERROR, issue_number, notice_id=notice_id,
                       reason="no notice could be delivered", reporters=len(reporters),
                       resolved=resolved)
    log_info(f"Shipped fix for issue #{issue_number} — told {notified} of {len(reporters)} "
             f"reporter(s): {line}")
    return _result(ShipAction.NOTIFIED, issue_number, notice_id=notice_id, notified=notified,
                   reporters=len(reporters), resolved=resolved, changelog_line=line)


def process_shipped_fixes(hours: int = None, limit: int = MAX_PRS_PER_PASS) -> dict:
    """Scan recently merged PRs and close the loop on every feedback cluster they shipped (plan
    §B.4). Idempotent: reporters already told are skipped, so this can run daily over an overlapping
    window without re-emailing anyone."""
    prs = merged_pull_requests(hours=hours, limit=limit)
    counts: dict = {}
    lines: list = []
    for pr in prs:
        issues = closing_issue_numbers(pr.get("title"), pr.get("body"))
        for issue_number in issues:
            try:
                result = notify_shipped_issue(issue_number, pr_title=pr.get("title"),
                                              pr_number=pr.get("number"))
            except Exception as e:
                log_error(f"Shipped-fix notify failed for issue #{issue_number}", exc=e)
                counts[ShipAction.ERROR] = counts.get(ShipAction.ERROR, 0) + 1
                continue
            counts[result["action"]] = counts.get(result["action"], 0) + 1
            if result.get("changelog_line"):
                lines.append(result["changelog_line"])
    log_info(f"Shipped-fix notify pass: {len(prs)} merged PR(s) — {counts or 'nothing to do'}")
    return {"pull_requests": len(prs), "counts": counts, "changelog": lines}
