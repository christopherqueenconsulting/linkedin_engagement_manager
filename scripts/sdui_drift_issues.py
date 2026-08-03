#!/usr/bin/env python3
"""Open ONE GitHub issue per `drift` verdict in an SDUI drift sweep (issue #1013).

Input is the JSON `scripts/linkedin_live_validation.py --sweep` prints: every Selenium surface the
sweep can reach, graded `ok` / `drift` / `unknown`, with the page-native evidence each grade was
claimed against.

Filing rule — only `drift` files, and only when no OPEN issue already carries its marker:

* `drift` means the PAGE shows content the locator cannot see. That is a defect with an owner, a
  reproducible probe command and evidence attached, so it becomes an `agent:ready` issue.
* `unknown` is NEVER filed. A page that did not render grounds nothing, and filing it would put the
  same non-finding in the backlog every Monday until it buried the real drift underneath.
* Dedup is on OPEN issues only, unlike `scripts/posthog_error_issues.py` which counts closed ones
  too. A fixed exception that trickles in for another day must not reopen a backlog item — but a
  surface that rotted, got re-grounded, and rotted AGAIN six months later is a NEW defect, and a
  closed issue must not silence it forever.

Split like the other cron planners: PURE logic (planning, titles, bodies — unit-tested) and a thin
`gh` CLI layer (mocked in tests). Deliberately imports nothing from `cqc_lem` and nothing from the
probe script: it runs from a host cron clone with no app env, no selenium and no DB.

CLI (--dry-run and --apply are mutually exclusive):
  --sweep-file PATH   Sweep JSON to plan from ('-' or omitted reads stdin).
  --dry-run           Show what would be filed. No writes. (default)
  --apply             File the missing GitHub issues.
  --max-new N         Cap issues filed per run (default 5); the rest wait for the next run.
  --repo owner/name   Repo to file into (default this repo, or $SDUI_DRIFT_REPO).
Exit: 0 nothing to do / applied, 2 drift pending (--dry-run), 1 error.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from typing import Optional

DEFAULT_REPO = "christopherqueenconsulting/linkedin_engagement_manager"

# Written into every filed body and searched for on the next run. The PROBE KEY is the dedup key —
# do not change the prefix without migrating the issues already carrying it.
MARKER_PREFIX = "sdui-drift-"

# `risk:live-linkedin` is deliberate: re-grounding a rotated locator cannot be verified without a
# live probe run, so the merge belongs to the owner (RUNBOOK escalation), not the runner.
LABELS = ("agent:ready", "bug", "priority:high", "risk:live-linkedin")

STATE_DRIFT = "drift"
DEFAULT_MAX_NEW = 5
MAX_TITLE_CHARS = 120
MAX_EVIDENCE_CHARS = 6000
GH_TIMEOUT_SECONDS = 60


# ─────────────────────────── pure logic (unit-tested) ────────────────────────────

def marker(probe_key: str) -> str:
    return f"{MARKER_PREFIX}{str(probe_key or '').strip()}"


def drift_rows(sweep: Optional[dict]) -> list:
    """Every `drift` probe in a sweep, as {key, surface, code, flag, reading}.

    Reads `probes` directly rather than trusting `summary.drift`: the summary is a convenience for
    humans, and a filer that planned from a derived field would file nothing at all the day that
    field is missing or stale."""
    sweep = dict(sweep or {})
    surfaces = sweep.get("surfaces") or {}
    rows = []
    for key, reading in sorted((sweep.get("probes") or {}).items()):
        if not isinstance(reading, dict) or reading.get("state") != STATE_DRIFT:
            continue
        meta = surfaces.get(key) or {}
        rows.append({"key": key, "reading": reading,
                     "surface": meta.get("surface") or key,
                     "code": meta.get("code") or "",
                     "flag": meta.get("flag") or ""})
    return rows


def build_title(row: dict) -> str:
    title = f"SDUI drift: {(row or {}).get('surface') or (row or {}).get('key')}"
    return title[:MAX_TITLE_CHARS]


def build_body(row: dict, user_id=None) -> str:
    """The `MODE=start` body the pipeline reads: Why / Scope / Acceptance, the probe command that
    reproduces it, and the raw reading as evidence."""
    row = dict(row or {})
    reading = dict(row.get("reading") or {})
    flag = row.get("flag") or ""
    target = " <target-url>" if flag and flag.endswith("-url") else ""
    probe_cmd = (f"sudo docker exec -i celery_worker_selenium python - "
                 f"--user-id {user_id or 1} {flag}{target} "
                 f"< scripts/linkedin_live_validation.py")
    evidence = json.dumps(reading, indent=2, default=str)[:MAX_EVIDENCE_CHARS]
    return "\n".join([
        "## Why now",
        "",
        f"The weekly SDUI drift sweep graded **{row.get('surface')}** as `drift`: the page shows "
        f"content the shipped locator chain cannot see. This is the failure shape behind #964, "
        f"#1009 and #1012 — a lane that keeps running and quietly does nothing.",
        "",
        f"> {reading.get('verdict') or '(no verdict recorded)'}",
        "",
        "## Scope",
        "",
        f"- Code: `{row.get('code') or 'see the probe report'}`",
        f"- Re-ground the locator chain from the evidence below — `data-testid` / `aria-label` / "
        f"href / TEXT only, never class names, and scoped to the owning card or dialog "
        f"(`docs/sdui-selenium-notes.md`).",
        "- Success is the OUTCOME being present, never a click having landed.",
        "- Never click a control whose label names a different entity than the target.",
        "",
        "## Reproduce",
        "",
        "```bash",
        probe_cmd,
        "```",
        "",
        "## Acceptance",
        "",
        f"- [ ] The probe's `{row.get('key')}` verdict comes back `ok` against a live session.",
        "- [ ] The production path cross-checks a page-native signal before treating zero items "
        "as 'nothing to do'.",
        "- [ ] `docs/sdui-selenium-notes.md` records what the live DOM actually looks like now.",
        "",
        "## Probe reading",
        "",
        "```json",
        evidence,
        "```",
        "",
        f"Auto-filed by `scripts/weekly_sdui_drift_check.sh` (issue #1013). Dedup marker (do not "
        f"remove): `{marker(row.get('key'))}`",
    ])


def plan_actions(rows: list, filed: Optional[set] = None,
                 max_new: int = DEFAULT_MAX_NEW) -> list:
    """What this run would do with each drift row. `filed` is the set of markers already on an OPEN
    issue. The cap is per RUN, never a silent truncation — a capped row is planned as `skip` with
    its reason so `summarize()` says how many are waiting."""
    already = {str(m) for m in (filed or set())}
    actions, created = [], 0
    for row in rows or []:
        key = marker(row.get("key"))
        if key in already:
            actions.append({"action": "skip", "reason": "already filed", "row": row,
                            "marker": key})
        elif created >= max_new:
            actions.append({"action": "skip", "reason": "max-new reached", "row": row,
                            "marker": key})
        else:
            created += 1
            actions.append({"action": "create", "row": row, "marker": key})
    return actions


def pending(actions: Optional[list]) -> list:
    return [a for a in (actions or []) if a.get("action") == "create"]


def summarize(actions: Optional[list]) -> str:
    actions = list(actions or [])
    if not actions:
        return "no drift"
    filed = len([a for a in actions if a.get("reason") == "already filed"])
    capped = len([a for a in actions if a.get("reason") == "max-new reached"])
    parts = [f"{len(pending(actions))} to file"]
    if filed:
        parts.append(f"{filed} already filed")
    if capped:
        parts.append(f"{capped} held for the next run")
    return ", ".join(parts)


# ─────────────────────────────── thin I/O layer ──────────────────────────────────

class GitHubIssues:
    """GitHub via the `gh` CLI — the cron host is already authenticated with it, so this needs no
    token of its own (and never handles one)."""

    def __init__(self, repo: str = DEFAULT_REPO) -> None:
        self.repo = repo

    def _run(self, args: list) -> subprocess.CompletedProcess:
        return subprocess.run(args, capture_output=True, text=True, timeout=GH_TIMEOUT_SECONDS)

    def is_filed(self, issue_marker: str) -> bool:
        """True when an OPEN issue already carries this marker.

        GitHub's search tokenizes on hyphens, so `sdui-drift-feed_sort` can match a NEIGHBOURING
        issue; the returned bodies are re-checked for the literal marker so dedup stays exact."""
        result = self._run(["gh", "issue", "list", "--repo", self.repo, "--state", "open",
                            "--search", issue_marker, "--json", "number,body"])
        if result.returncode != 0:
            # Fail CLOSED: an unreadable search must never be read as "nothing filed yet", or a
            # GitHub outage turns into a duplicate for every drifting surface.
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
    found = set()
    for row in rows or []:
        key = marker(row.get("key"))
        if github.is_filed(key):
            found.add(key)
    return found


def apply_actions(github: GitHubIssues, actions: list, user_id=None,
                  dry_run: bool = True) -> list:
    """File the planned issues (or, in dry-run, just report them). Never raises on a GitHub
    failure: a row that fails to file is left unfiled and retried by the next sweep."""
    applied = []
    for action in pending(actions):
        title = build_title(action["row"])
        if dry_run:
            print(f"  would file: {title}")
            continue
        url = github.create(title, build_body(action["row"], user_id))
        if url:
            print(f"  filed: {url} — {title}")
            applied.append({**action, "url": url})
    return applied


def load_sweep(path: Optional[str]) -> dict:
    raw = sys.stdin.read() if not path or path == "-" else open(path, encoding="utf-8").read()
    if not (raw or "").strip():
        raise ValueError("sweep JSON is empty")
    sweep = json.loads(raw)
    if not isinstance(sweep, dict):
        raise ValueError("sweep JSON is not an object")
    return sweep


def main(argv: Optional[list] = None) -> int:
    import os

    parser = argparse.ArgumentParser(
        description="File GitHub issues from an SDUI drift sweep (issue #1013).")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Show what would be filed (default).")
    mode.add_argument("--apply", action="store_true", help="File the missing GitHub issues.")
    parser.add_argument("--sweep-file", default="-", help="Sweep JSON path ('-' = stdin).")
    parser.add_argument("--max-new", type=int, default=DEFAULT_MAX_NEW)
    parser.add_argument("--repo", default=os.getenv("SDUI_DRIFT_REPO", DEFAULT_REPO))
    args = parser.parse_args(argv)

    try:
        sweep = load_sweep(args.sweep_file)
    except Exception as e:
        print(f"Could not read the sweep JSON: {e}", file=sys.stderr)
        return 1

    rows = drift_rows(sweep)
    summary = sweep.get("summary") or {}
    print(f"Sweep: {summary.get('probed', '?')} probe(s) — "
          f"{len(summary.get('ok') or [])} ok, {len(rows)} drift, "
          f"{len(summary.get('unknown') or [])} unknown "
          f"(unknown is never filed: it grounds nothing)")
    if not rows:
        return 0

    github = GitHubIssues(args.repo)
    try:
        already = filed_markers(github, rows)
    except Exception as e:
        print(f"GitHub dedup lookup failed: {e}", file=sys.stderr)
        return 1

    actions = plan_actions(rows, already, args.max_new)
    print(f"Drift: {summarize(actions)}")
    dry_run = not args.apply
    apply_actions(github, actions, sweep.get("user_id"), dry_run=dry_run)
    if dry_run:
        return 2 if pending(actions) else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
