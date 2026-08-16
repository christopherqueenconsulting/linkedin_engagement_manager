#!/usr/bin/env python3
"""Open ONE GitHub issue per PostHog error-tracking ISSUE (issue #648).

This replaces the log-grep scan the daily cron used to run. That version read PostHog *Logs* for
ERROR/FATAL bodies, grouped them by the exact message string and hand-rolled dedup with a sha1 of
that string — so one exception with a variable message ("Post 41 failed", "Post 42 failed") filed a
new GitHub issue every day. PostHog Error Tracking already groups `$exception` events into ISSUES by
fingerprint, so the grouping IS the dedup: one PostHog issue id maps to exactly one GitHub issue,
forever, and the marker in the body is that id.

Filing rule: an ACTIVE PostHog issue with at least `--min-occurrences` exceptions inside the window
and no GitHub issue carrying its marker. A brand-new issue therefore files on the next run, and a
long-running one is filed once and never again — spikes on an already-filed issue are PostHog's own
alert's job (docs/error-tracking.md), not a second GitHub issue.

Second dedup layer, for the trackers this script did NOT write (issue #1083): the marker is invisible
to a human who opened an issue for the same defect first, so an escalated warning also matches on its
normalized string. #1063 auto-filed `Selector miss: Comment sort control` while hand-filed #818
already tracked exactly that warning; the #874/#875/#877/#878 cluster filed four issues against the
one outage #816 tracked. When the normalized string appears in an OPEN issue's title or body, the
occurrence data is COMMENTED there instead — and the comment carries the marker, so the id-based
layer takes over from the next run.

Split like scripts/model_health_check.py and scripts/posthog_dashboards.py: PURE logic (query,
titles, bodies, planning — unit-tested) and a thin I/O layer (the PostHog query API and the `gh`
CLI, mocked in tests). Deliberately imports nothing from `cqc_lem`: this runs from a host cron clone
with no app env, DB or broker.

CLI (--dry-run and --apply are mutually exclusive):
  --dry-run              Show what would be filed. No writes. (default)
  --apply                File the missing GitHub issues.
  --print-sql            Print the HogQL and exit (no network).
Options:
  --hours N              Lookback window in hours (default 24).
  --min-occurrences N    Ignore issues with fewer exceptions than this in the window (default 1).
  --max-new N            Cap issues filed per run (default 10); the rest wait for the next run.
Env:
  POSTHOG_QUERY_API_KEY     Purpose-scoped personal API key with query:read (issue #1453).
                            Falls back to POSTHOG_PERSONAL_API_KEY when unset.
  POSTHOG_PERSONAL_API_KEY  The shared fallback key (required for network if the scoped one is unset).
  POSTHOG_PROJECT_ID        PostHog project id (default 475262 — "CQC LEM").
  POSTHOG_APP_HOST          App host for the API (default https://us.posthog.com).
  ERROR_ISSUE_REPO          owner/name to file into (default this repo).
Exit: 0 nothing to do / applied, 2 issues pending (--dry-run), 1 error.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Optional

# Run from a dedicated cron clone by scripts/error_to_issues.sh, whose python is a venv belonging to
# a DIFFERENT checkout — so reach the key resolver by path (inserted first, ahead of any editable
# install) rather than by whatever `cqc_lem` that venv happens to point at. posthog_keys.py is
# stdlib-only so this costs nothing.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from cqc_lem.utilities.posthog_keys import (  # noqa: E402
    missing_key_message,
    resolve_posthog_key,
)

DEFAULT_PROJECT_ID = "475262"  # "CQC LEM" — not a secret; the key that reaches it is.
DEFAULT_APP_HOST = "https://us.posthog.com"
DEFAULT_REPO = "christopherqueenconsulting/linkedin_engagement_manager"

# Written into every filed body and searched for on the next run. The PostHog issue id IS the dedup
# key — do not change the prefix without migrating the issues already carrying it.
MARKER_PREFIX = "posthog-issue-"

LABELS = ("agent:ready", "bug")

DEFAULT_HOURS = 24
DEFAULT_MIN_OCCURRENCES = 1
DEFAULT_MAX_NEW = 10
# Ceiling on rows pulled back; far above any realistic day, so it only ever caps a runaway.
QUERY_LIMIT = 100
MAX_TITLE_CHARS = 120
GH_TIMEOUT_SECONDS = 60
QUERY_TIMEOUT_SECONDS = 60

# The columns the query selects, in order — parse_rows() zips these onto each result row rather than
# trusting the API to echo a `columns` array back.
COLUMNS = ("issue_id", "name", "description", "status", "first_seen", "last_seen",
           "occurrences", "users", "lib", "task_name", "route", "session_id")

# What posthog-js hands out as a session id. A value that isn't this shape is not linked (#649).
SESSION_ID_RE = re.compile(r"[A-Za-z0-9._-]{8,64}")

# `log_escalation.RecurringWarning` — the ONLY exception type whose description is already the
# normalized warning string (volatile tokens masked to `<n>`/`<list>`/…), which is what makes a
# substring match against a human's issue text safe. Every other exception carries a raw interpolated
# message, so it keeps id-only dedup. Named as a string, not imported: this script runs from a host
# cron clone with no `cqc_lem` on the path.
ESCALATED_WARNING_NAME = "RecurringWarning"
# Floors on what may be matched by text. A short or single-word warning ("boom") is a phrase that
# turns up in unrelated issues, and a false merge HIDES a distinct defect — where a false miss only
# files the duplicate we file today.
MIN_SIGNATURE_CHARS = 16
MIN_SIGNATURE_WORDS = 3
# GitHub's search takes the phrase; the full signature is re-checked client-side, so truncating here
# can only widen the candidate set, never loosen the match.
MAX_SEARCH_CHARS = 120
MATCH_SEARCH_LIMIT = 30


# ─────────────────────────── pure logic (unit-tested) ────────────────────────────

def build_query(hours: int = DEFAULT_HOURS, min_occurrences: int = DEFAULT_MIN_OCCURRENCES,
                limit: int = QUERY_LIMIT) -> str:
    """The HogQL behind one run. `issue_*` are the error-tracking columns the events table exposes,
    so the grouping PostHog already did is read straight off the exception events — no second query
    against the issues table, and no client-side fingerprinting."""
    hours = max(1, int(hours or DEFAULT_HOURS))
    minimum = max(1, int(min_occurrences or 1))
    return (
        "SELECT issue_id, any(issue_name) AS name, any(issue_description) AS description, "
        "any(issue_status) AS status, min(issue_first_seen) AS first_seen, "
        "max(timestamp) AS last_seen, count() AS occurrences, uniq(distinct_id) AS users, "
        "any(properties.$lib) AS lib, any(properties.task_name) AS task_name, "
        "any(properties.route) AS route, any(properties.$session_id) AS session_id "
        "FROM events "
        f"WHERE event = '$exception' AND timestamp > now() - INTERVAL {hours} HOUR "
        "GROUP BY issue_id "
        f"HAVING occurrences >= {minimum} "
        f"ORDER BY occurrences DESC LIMIT {int(limit)}"
    )


def parse_rows(results: Optional[list]) -> list:
    """Query rows -> dicts keyed by COLUMNS. Rows with no issue id are dropped: an exception whose
    issue has not been created yet (ingestion lag) has nothing stable to dedup on, and it will be
    grouped by the next run."""
    rows = []
    for row in results or []:
        if not isinstance(row, (list, tuple)):
            continue
        item = {key: (row[index] if index < len(row) else None)
                for index, key in enumerate(COLUMNS)}
        issue_id = str(item.get("issue_id") or "").strip()
        if not issue_id:
            continue
        item["issue_id"] = issue_id
        rows.append(item)
    return rows


def marker(issue_id: str) -> str:
    return f"{MARKER_PREFIX}{issue_id}"


def is_actionable(row: dict) -> bool:
    """Only ACTIVE issues become GitHub work. One a human already resolved or suppressed in PostHog
    must never come back as a fresh backlog item just because a straggler event landed."""
    status = str((row or {}).get("status") or "active").strip().lower()
    return status in ("", "active")


def issue_url(issue_id: str, project_id: str = DEFAULT_PROJECT_ID,
              app_host: str = DEFAULT_APP_HOST) -> str:
    return f"{app_host.rstrip('/')}/project/{project_id}/error_tracking/{issue_id}"


def replay_url(session_id, project_id: str = DEFAULT_PROJECT_ID,
               app_host: str = DEFAULT_APP_HOST) -> Optional[str]:
    """The replay permalink for a browser session that threw (issue #649), or None. Backend
    exceptions carry no `$session_id`, and `any()` skips NULLs, so a mixed issue still links the
    browser session if one of its occurrences had one."""
    sid = str(session_id if session_id is not None else "").strip()
    if not sid or not SESSION_ID_RE.fullmatch(sid):
        return None
    return f"{app_host.rstrip('/')}/project/{project_id}/replay/{sid}"


def _text(value) -> str:
    return str(value if value is not None else "").strip()


def build_title(row: dict) -> str:
    """`fix(errors): <exception type>: <message> (Nx/24h)` — reads like the commit that closes it."""
    name = _text(row.get("name")) or "Unknown exception"
    description = _text(row.get("description"))
    summary = f"{name}: {description}" if description else name
    summary = " ".join(summary.split())
    suffix = f" ({int(row.get('occurrences') or 0)}x)"
    head = f"fix(errors): {summary}"
    if len(head) + len(suffix) > MAX_TITLE_CHARS:
        head = head[:MAX_TITLE_CHARS - len(suffix)]
    return head + suffix


def warning_signature(row: dict) -> Optional[str]:
    """The normalized warning string an existing tracker would be recognised by, or None.

    Only an escalated warning has one: `log_escalation` masks the volatile tokens BEFORE the
    exception is captured, so `RecurringWarning`'s description is a stable template
    (`Selector miss: Comment sort control`) that a human writing about the same defect quotes
    verbatim. Anything shorter or vaguer than the floors above is refused rather than matched
    loosely.
    """
    if _text(row.get("name")) != ESCALATED_WARNING_NAME:
        return None
    text = " ".join(_text(row.get("description")).split())
    if len(text) < MIN_SIGNATURE_CHARS or len(text.split()) < MIN_SIGNATURE_WORDS:
        return None
    return text


def issue_matches(signature: str, issue: dict) -> bool:
    """Whether this GitHub issue is already tracking that warning.

    Title or body only — deliberately NOT comments. A warning quoted in a comment is usually someone
    referring to a different issue's problem, and merging onto it would bury a real defect. Casefold
    on both sides: a hand-written title reformats the casing far more often than it changes a word.
    """
    if not signature:
        return False
    haystack = f"{(issue or {}).get('title') or ''}\n{(issue or {}).get('body') or ''}"
    return signature.casefold() in haystack.casefold()


def _issue_number(issue: dict) -> int:
    try:
        return int((issue or {}).get("number") or 0)
    except (TypeError, ValueError):
        return 0


def pick_match(signature: str, issues: Optional[list]) -> Optional[dict]:
    """The lowest-numbered open issue that really contains `signature` — the ORIGINAL tracker, so a
    third occurrence lands on the same thread as the second rather than on its duplicate. GitHub's
    search tokenizes, so its hits are candidates only; this is where the phrase is actually checked.
    """
    matched = [issue for issue in issues or []
               if isinstance(issue, dict) and _issue_number(issue) > 0
               and issue_matches(signature, issue)]
    if not matched:
        return None
    return min(matched, key=_issue_number)


def search_phrase(signature: str) -> str:
    """What to hand GitHub's search — the signature, truncated and stripped of the double quotes
    that would close the phrase early. `pick_match` re-checks the FULL string against the candidates,
    so weakening the query here can only widen the candidate set, never loosen the match.

    Truncation lands on a WORD boundary: GitHub's search tokenizes, so a phrase cut mid-word
    (`… selector dri`) matches nothing at all — narrowing the candidate set to zero rather than
    widening it, which is the one thing this function must not do.
    """
    phrase = " ".join(_text(signature).replace('"', " ").split())
    if len(phrase) > MAX_SEARCH_CHARS:
        phrase = phrase[:MAX_SEARCH_CHARS].rsplit(" ", 1)[0]
    return phrase.strip()


def _context_lines(row: dict, hours: int) -> list:
    """The occurrence facts shared by a filed body and a comment on an existing tracker."""
    context = [f"- Occurrences (last {hours}h): **{int(row.get('occurrences') or 0)}** across "
               f"{int(row.get('users') or 0)} distinct actor(s)",
               f"- First seen: `{_text(row.get('first_seen')) or 'unknown'}` · "
               f"last seen: `{_text(row.get('last_seen')) or 'unknown'}`"]
    if _text(row.get("task_name")):
        context.append(f"- Celery task: `{_text(row.get('task_name'))}`")
    if _text(row.get("route")):
        context.append(f"- API route: `{_text(row.get('route'))}`")
    if _text(row.get("lib")):
        context.append(f"- SDK: `{_text(row.get('lib'))}`")
    return context


def build_body(row: dict, hours: int = DEFAULT_HOURS, project_id: str = DEFAULT_PROJECT_ID,
               app_host: str = DEFAULT_APP_HOST) -> str:
    """The `MODE=start` body the pipeline reads: Why / Scope / Files / Acceptance, plus the marker
    that makes this run idempotent."""
    issue_id = _text(row.get("issue_id"))
    context = _context_lines(row, hours)

    lines = ["## Why",
             f"PostHog error tracking grouped this exception into an issue that is still active: "
             f"**{_text(row.get('name')) or 'Unknown exception'}**",
             "",
             f"> {_text(row.get('description')) or '(no message captured)'}",
             ""]
    lines += context
    lines += ["",
              f"[Open the issue in PostHog]({issue_url(issue_id, project_id, app_host)}) — it has "
              f"the stack trace, the grouped occurrences and the affected people.",
              ""]
    replay = replay_url(row.get("session_id"), project_id, app_host)
    if replay:
        lines += [f"[Watch the session replay]({replay}) — the browser session one of these "
                  f"exceptions was thrown in.", ""]
    lines += ["## Scope",
              "- Fix the root cause of the exception, not the symptom.",
              ("- If it is an EXPECTED best-effort failure that already degrades gracefully (a " +
               "Selenium selector miss, a third-party timeout the caller retries), stop raising it " +
               "into error tracking: catch it and `log_warning(...)` instead, or add a suppression " +
               "rule in PostHog."),
              "- Keep the change scoped to this exception — no unrelated refactors.",
              "",
              "## Files",
              "Start from the stack trace on the PostHog issue; add/extend the unit tests:",
              "- `tests/unit/`",
              "",
              "## Acceptance",
              "- The exception no longer occurs (or is deliberately downgraded to a warning).",
              "- A unit test covers the failing path (≥80% patch coverage).",
              "- All required CI gates pass.",
              "",
              f"Auto-filed from PostHog Error Tracking. Dedup marker (do not remove): "
              f"`{marker(issue_id)}`"]
    return "\n".join(lines)


def build_comment(row: dict, existing: dict, hours: int = DEFAULT_HOURS,
                  project_id: str = DEFAULT_PROJECT_ID,
                  app_host: str = DEFAULT_APP_HOST) -> str:
    """The occurrence report added to an issue that already tracks this warning (issue #1083).

    It carries the marker, which is what stops this from becoming a daily comment: from the next run
    the id-based layer sees the marker on this thread and skips the row entirely.
    """
    issue_id = _text(row.get("issue_id"))
    lines = [f"### Still occurring — PostHog error tracking, last {hours}h",
             "",
             ("This warning is already tracked here, so the occurrence data lands as a comment " +
              "rather than a new issue."),
             "",
             f"> {_text(row.get('description')) or '(no message captured)'}",
             ""]
    lines += _context_lines(row, hours)
    lines += ["",
              f"[Open the issue in PostHog]({issue_url(issue_id, project_id, app_host)}) — stack "
              f"trace, grouped occurrences and affected people."]
    replay = replay_url(row.get("session_id"), project_id, app_host)
    if replay:
        lines.append(f"[Watch the session replay]({replay}) — the browser session one of these "
                     f"exceptions was thrown in.")
    matched_on = _text((existing or {}).get("signature"))
    matched_on = f" `{matched_on}`" if matched_on else ""
    lines += ["",
              f"Matched this issue on the normalized warning string{matched_on}, so no duplicate "
              f"was opened. If this is NOT the same defect, open a separate issue for it and LEAVE "
              f"this comment here — the match is on the warning TEXT, which this issue still "
              f"carries, so deleting the comment only makes the next run post it again.",
              "",
              f"Dedup marker (do not remove): `{marker(issue_id)}`"]
    return "\n".join(lines)


def plan_actions(rows: list, filed_markers, max_new: int = DEFAULT_MAX_NEW,
                 existing_matches: Optional[dict] = None) -> list:
    """What this run would do, in order: one `create` per actionable issue that has no GitHub issue
    yet, `comment` where an open issue already tracks the same warning string, `skip` for everything
    else with the reason. Creates are capped at `max_new` so a bad deploy that produces 50 new issues
    does not open 50 tickets in one go — the rest are `deferred` and picked up by the next run.
    Comments are not capped: they add nothing to the backlog, and each one happens once per PostHog
    issue id because the comment carries the marker."""
    already = {str(m) for m in (filed_markers or set())}
    matches = existing_matches or {}
    seen = set()
    actions = []
    created = 0
    for row in rows or []:
        issue_id = _text(row.get("issue_id"))
        key = marker(issue_id)
        if key in seen:
            continue
        seen.add(key)
        if not is_actionable(row):
            actions.append({"action": "skip", "reason": "not active", "row": row, "marker": key})
        elif key in already:
            actions.append({"action": "skip", "reason": "already filed", "row": row,
                            "marker": key})
        elif matches.get(key):
            actions.append({"action": "comment", "row": row, "marker": key,
                            "existing": matches[key]})
        elif created >= max(0, int(max_new)):
            actions.append({"action": "deferred", "reason": "max-new reached", "row": row,
                            "marker": key})
        else:
            actions.append({"action": "create", "row": row, "marker": key})
            created += 1
    return actions


def pending(actions: list) -> list:
    """Everything this run would write to GitHub — a new issue or a comment on an existing one."""
    return [a for a in actions or [] if a.get("action") in ("create", "comment")]


def summarize(actions: list) -> str:
    counts: dict = {}
    for action in actions or []:
        counts[action["action"]] = counts.get(action["action"], 0) + 1
    if not counts:
        return "no error-tracking issues in the window"
    return ", ".join(f"{count} {name}" for name, count in sorted(counts.items()))


# ─────────────────────────────── I/O (mocked in tests) ───────────────────────────

class PostHogQueryClient:
    """Minimal client for the HogQL query endpoint — the same one the old shell scan used."""

    def __init__(self, api_key: str, project_id: str, app_host: str = DEFAULT_APP_HOST) -> None:
        self.api_key = api_key
        self.project_id = project_id
        self.app_host = app_host.rstrip("/")

    def query(self, hogql: str) -> list:
        import requests
        response = requests.post(
            f"{self.app_host}/api/projects/{self.project_id}/query/",
            headers={"Authorization": f"Bearer {self.api_key}",
                     "Content-Type": "application/json"},
            json={"query": {"kind": "HogQLQuery", "query": hogql}},
            timeout=QUERY_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        payload = response.json()
        return payload.get("results") or []


class GitHubIssues:
    """GitHub via the `gh` CLI — the cron host is already authenticated with it, so this needs no
    token of its own (and never handles one)."""

    def __init__(self, repo: str = DEFAULT_REPO) -> None:
        self.repo = repo

    def _run(self, args: list) -> subprocess.CompletedProcess:
        return subprocess.run(args, capture_output=True, text=True, timeout=GH_TIMEOUT_SECONDS)

    def _search(self, state: str, phrase: str, fields: str, limit: int) -> list:
        """One `gh issue list --search`, or a raised error. Fail CLOSED: an unreadable search must
        never be read as "nothing filed yet", or a GitHub outage turns into a duplicate for every
        issue in the window."""
        result = self._run(["gh", "issue", "list", "--repo", self.repo, "--state", state,
                            "--search", phrase, "--limit", str(limit), "--json", fields])
        if result.returncode != 0:
            raise RuntimeError(f"gh issue list failed: {(result.stderr or '').strip()[:200]}")
        try:
            found = json.loads(result.stdout or "[]")
        except ValueError:
            raise RuntimeError("gh issue list returned unparseable JSON")
        return [item for item in found if isinstance(item, dict)]

    def is_filed(self, issue_marker: str) -> bool:
        """True when ANY issue — open or closed — already carries this marker. Closed counts: a
        fixed exception that trickles in for another day must not reopen the backlog item.

        Comments count as well as bodies: when the marker landed on a hand-filed tracker as a comment
        (issue #1083), that thread IS this exception's GitHub issue and must not also get one of its
        own. GitHub's search tokenizes on hyphens, so a UUID marker can match a NEIGHBOURING issue;
        the returned text is re-checked for the literal marker so dedup stays exact."""
        found = self._search("all", issue_marker, "number,body,comments", MATCH_SEARCH_LIMIT)
        for item in found:
            if issue_marker in (item.get("body") or ""):
                return True
            for comment in item.get("comments") or []:
                if isinstance(comment, dict) and issue_marker in (comment.get("body") or ""):
                    return True
        return False

    def search_open(self, signature: str) -> list:
        """Open-issue candidates for a warning string. Open only: a CLOSED tracker says the defect
        was declared fixed, so a recurrence is news and deserves its own issue."""
        phrase = search_phrase(signature)
        if not phrase:
            return []
        return self._search("open", f'"{phrase}"', "number,title,body,url", MATCH_SEARCH_LIMIT)

    def _write(self, what: str, args: list) -> Optional[str]:
        """A `gh` write, or None with the reason on stderr. Never raises: a row that fails to write
        is left unhandled and retried by the next run — the run itself must still finish."""
        result = self._run(args)
        if result.returncode != 0:
            print(f"  ! gh issue {what} failed: {(result.stderr or '').strip()[:300]}",
                  file=sys.stderr)
            return None
        return (result.stdout or "").strip()

    def comment(self, number: int, body: str) -> Optional[str]:
        """Add the occurrence report to an existing tracker (issue #1083)."""
        return self._write("comment", ["gh", "issue", "comment", str(number), "--repo", self.repo,
                                       "--body", body])

    def create(self, title: str, body: str, labels=LABELS) -> Optional[str]:
        args = ["gh", "issue", "create", "--repo", self.repo, "--title", title, "--body", body]
        for label in labels:
            args += ["--label", label]
        return self._write("create", args)


def filed_markers(github: GitHubIssues, rows: list) -> set:
    """Which of these issues already have a GitHub issue. One search per issue id: exact-marker
    search is what makes the dedup id-based rather than text-similarity based."""
    found = set()
    for row in rows or []:
        key = marker(_text(row.get("issue_id")))
        if github.is_filed(key):
            found.add(key)
    return found


def open_matches(github: GitHubIssues, rows: list, already=None) -> dict:
    """Marker -> the OPEN issue already tracking that warning string, hand-filed or auto-filed.

    One search per candidate row. Rows already carrying a marker are skipped — the id is the stronger
    key and has the final say, so searching them would only cost a call.
    """
    filed = {str(m) for m in (already or set())}
    matches: dict = {}
    for row in rows or []:
        key = marker(_text(row.get("issue_id")))
        if key in matches or key in filed or not is_actionable(row):
            continue
        signature = warning_signature(row)
        if not signature:
            continue
        match = pick_match(signature, github.search_open(signature))
        if match:
            matches[key] = {**match, "signature": signature}
    return matches


def apply_actions(github: GitHubIssues, actions: list, dry_run: bool = True) -> list:
    """File the planned issues and comment on the trackers that already exist (or, in dry-run, just
    report them). Never raises on a GitHub failure: a row that fails to write is left unhandled and
    retried by the next run."""
    applied = []
    for action in pending(actions):
        title = build_title(action["row"])
        # A comment action without a usable issue number falls back to filing: an unaddressable
        # match is the "false miss" case, and a duplicate is the acceptable half of that trade.
        onto = _issue_number(action.get("existing") or {}) if action["action"] == "comment" else 0
        if dry_run:
            print(f"  would {f'comment on #{onto}' if onto else 'file'}: {title}")
            continue
        url = (github.comment(onto, action["body"]) if onto
               else github.create(title, action["body"]))
        if url:
            print(f"  {f'commented on #{onto}' if onto else 'filed'}: {url} — {title}")
            applied.append({**action, "url": url})
    return applied


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        description="File GitHub issues from PostHog error-tracking issues.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Show what would be filed (default).")
    mode.add_argument("--apply", action="store_true", help="File the missing GitHub issues.")
    parser.add_argument("--print-sql", action="store_true", help="Print the HogQL and exit.")
    parser.add_argument("--hours", type=int, default=DEFAULT_HOURS)
    parser.add_argument("--min-occurrences", type=int, default=DEFAULT_MIN_OCCURRENCES)
    parser.add_argument("--max-new", type=int, default=DEFAULT_MAX_NEW)
    args = parser.parse_args(argv)

    hogql = build_query(args.hours, args.min_occurrences)
    if args.print_sql:
        print(hogql)
        return 0

    api_key = resolve_posthog_key("query")
    if not api_key:
        print(f"{missing_key_message('query')} — cannot reach PostHog.", file=sys.stderr)
        return 1
    project_id = os.getenv("POSTHOG_PROJECT_ID", DEFAULT_PROJECT_ID)
    app_host = os.getenv("POSTHOG_APP_HOST", DEFAULT_APP_HOST)
    repo = os.getenv("ERROR_ISSUE_REPO", DEFAULT_REPO)

    try:
        results = PostHogQueryClient(api_key, project_id, app_host).query(hogql)
    except Exception as e:
        print(f"PostHog query failed: {e}", file=sys.stderr)
        return 1

    rows = parse_rows(results)
    github = GitHubIssues(repo)
    try:
        already = filed_markers(github, rows)
        matches = open_matches(github, rows, already)
    except Exception as e:
        print(f"GitHub dedup lookup failed: {e}", file=sys.stderr)
        return 1

    actions = plan_actions(rows, already, args.max_new, matches)
    for action in actions:
        if action["action"] == "create":
            action["body"] = build_body(action["row"], args.hours, project_id, app_host)
        elif action["action"] == "comment":
            action["body"] = build_comment(action["row"], action["existing"], args.hours,
                                           project_id, app_host)
    print(f"PostHog: {len(rows)} error-tracking issue(s) in the last {args.hours}h "
          f"— {summarize(actions)}")

    dry_run = not args.apply
    apply_actions(github, actions, dry_run=dry_run)
    if dry_run:
        return 2 if pending(actions) else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
