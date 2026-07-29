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
  POSTHOG_PERSONAL_API_KEY  Personal API key with query:read (required for network).
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
from typing import Optional

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


def build_body(row: dict, hours: int = DEFAULT_HOURS, project_id: str = DEFAULT_PROJECT_ID,
               app_host: str = DEFAULT_APP_HOST) -> str:
    """The `MODE=start` body the pipeline reads: Why / Scope / Files / Acceptance, plus the marker
    that makes this run idempotent."""
    issue_id = _text(row.get("issue_id"))
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


def plan_actions(rows: list, filed_markers, max_new: int = DEFAULT_MAX_NEW) -> list:
    """What this run would do, in order: one `create` per actionable issue that has no GitHub issue
    yet, `skip` for everything else with the reason. Capped at `max_new` so a bad deploy that
    produces 50 new issues does not open 50 tickets in one go — the rest are `deferred` and picked up
    by the next run."""
    already = {str(m) for m in (filed_markers or set())}
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
        elif created >= max(0, int(max_new)):
            actions.append({"action": "deferred", "reason": "max-new reached", "row": row,
                            "marker": key})
        else:
            actions.append({"action": "create", "row": row, "marker": key})
            created += 1
    return actions


def pending(actions: list) -> list:
    return [a for a in actions or [] if a.get("action") == "create"]


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

    def is_filed(self, issue_marker: str) -> bool:
        """True when ANY issue — open or closed — already carries this marker. Closed counts: a
        fixed exception that trickles in for another day must not reopen the backlog item.

        GitHub's search tokenizes on hyphens, so a UUID marker can match a NEIGHBOURING issue; the
        returned bodies are re-checked for the literal marker so dedup stays exact."""
        result = self._run(["gh", "issue", "list", "--repo", self.repo, "--state", "all",
                            "--search", issue_marker, "--json", "number,body"])
        if result.returncode != 0:
            # Fail CLOSED: an unreadable search must never be read as "nothing filed yet", or a
            # GitHub outage turns into a duplicate for every issue in the window.
            raise RuntimeError(f"gh issue list failed: {(result.stderr or '').strip()[:200]}")
        try:
            found = json.loads(result.stdout or "[]")
        except ValueError:
            raise RuntimeError("gh issue list returned unparseable JSON")
        return any(issue_marker in (item.get("body") or "") for item in found
                   if isinstance(item, dict))

    def create(self, title: str, body: str, labels=LABELS) -> Optional[str]:
        args = ["gh", "issue", "create", "--repo", self.repo, "--title", title, "--body", body]
        for label in labels:
            args += ["--label", label]
        result = self._run(args)
        if result.returncode != 0:
            print(f"  ! gh issue create failed: {(result.stderr or '').strip()[:300]}",
                  file=sys.stderr)
            return None
        return (result.stdout or "").strip()


def filed_markers(github: GitHubIssues, rows: list) -> set:
    """Which of these issues already have a GitHub issue. One search per issue id: exact-marker
    search is what makes the dedup id-based rather than text-similarity based."""
    found = set()
    for row in rows or []:
        key = marker(_text(row.get("issue_id")))
        if github.is_filed(key):
            found.add(key)
    return found


def apply_actions(github: GitHubIssues, actions: list, dry_run: bool = True) -> list:
    """File the planned issues (or, in dry-run, just report them). Never raises on a GitHub failure:
    a row that fails to file is left unfiled and retried by the next run."""
    applied = []
    for action in pending(actions):
        row = action["row"]
        title = build_title(row)
        if dry_run:
            print(f"  would file: {title}")
            continue
        url = github.create(title, action["body"])
        if url:
            print(f"  filed: {url} — {title}")
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

    api_key = os.getenv("POSTHOG_PERSONAL_API_KEY", "")
    if not api_key:
        print("POSTHOG_PERSONAL_API_KEY is not set — cannot reach PostHog.", file=sys.stderr)
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
    except Exception as e:
        print(f"GitHub dedup lookup failed: {e}", file=sys.stderr)
        return 1

    actions = plan_actions(rows, already, args.max_new)
    for action in actions:
        if action["action"] == "create":
            action["body"] = build_body(action["row"], args.hours, project_id, app_host)
    print(f"PostHog: {len(rows)} error-tracking issue(s) in the last {args.hours}h "
          f"— {summarize(actions)}")

    dry_run = not args.apply
    apply_actions(github, actions, dry_run=dry_run)
    if dry_run:
        return 2 if pending(actions) else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
