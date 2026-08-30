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

# Phase 2 (issue #1770): a surface that has gone unmeasured for `STALE_THRESHOLD` consecutive
# sweeps is its OWN kind of finding — "we cannot see this" is a different defect from "this
# rotted", so it gets a separate marker prefix rather than piggybacking on `MARKER_PREFIX`. A
# surface graded `ok` (still working) or `drift` (broken, but SEEN — the drift filer above already
# owns it) anywhere in the window is measured; only silence for the WHOLE window is a finding.
STALE_MARKER_PREFIX = "sdui-stale-"
STALE_THRESHOLD = 3

# `risk:live-linkedin` is deliberate: re-grounding a rotated locator cannot be verified without a
# live probe run, so the merge belongs to the owner (RUNBOOK escalation), not the runner.
LABELS = ("agent:ready", "bug", "priority:high", "risk:live-linkedin")
# The blind-spot issue is a coverage gap, not a live-LinkedIn re-grounding — filing it needs no
# live probe run to verify, so it carries no `risk:live-linkedin` and can merge like any other fix.
STALE_LABELS = ("agent:ready", "bug", "priority:high")

STATE_DRIFT = "drift"
STATE_OK = "ok"
# The fences `linkedin_live_validation.py` prints around its report. Duplicated as a literal rather
# than imported, deliberately: this script must keep running on a host clone with no app env. The
# probe shares stdout with `cqc_lem.utilities.logger`, so the report has to be cut out of the noise
# — without this, one "Getting Updated Profile" line ahead of the JSON loses the whole week's sweep.
REPORT_JSON_BEGIN = "===LEM-PROBE-JSON-BEGIN==="
REPORT_JSON_END = "===LEM-PROBE-JSON-END==="

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
                     "flag": meta.get("flag") or "",
                     # The value the flag takes, when it takes one. An issue whose repro line is
                     # `--profile-scrape` with nothing after it is an argparse error, not a repro.
                     "arg": meta.get("arg") or ""})
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
    # `arg` comes from the surface matrix; the `-url` suffix is the fallback for a sweep written
    # before it carried one.
    arg = row.get("arg") or ("<target-url>" if flag.endswith("-url") else "")
    target = f" {arg}" if arg else ""
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
        "- Re-ground the locator chain from the evidence below — `data-testid` / `aria-label` / "
        "href / TEXT only, never class names, and scoped to the owning card or dialog "
        "(`docs/sdui-selenium-notes.md`).",
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


def surface_state(sweep: Optional[dict], key: str) -> str:
    """The state `key` graded in ONE sweep — `unmeasured` when it is missing entirely (an older
    sweep from before this surface existed, one the sweep itself named `skipped`, or a key a
    malformed history file dropped) rather than raising or silently reading as `ok`.
    """
    reading = (dict(sweep or {}).get("probes") or {}).get(key)
    if not isinstance(reading, dict):
        return "unmeasured"
    return str(reading.get("state") or "unmeasured")


def stale_rows(current: Optional[dict], history: Optional[list],
               threshold: int = STALE_THRESHOLD) -> list:
    """Surfaces that graded neither `ok` nor `drift` in the trailing `threshold` sweeps (issue
    #1770 Phase 2) — a coverage BLIND SPOT, not a rotted locator, so it never piggybacks on
    `drift_rows`.

    `history` is the sweeps immediately before `current`, OLDEST FIRST. With fewer than
    `threshold - 1` of them on hand this says nothing rather than guess staleness from a short
    tail — a fresh install with no sweep history yet must not immediately file a blind-spot issue
    for every surface.
    """
    window = list(history or [])[-(threshold - 1):] + [dict(current or {})]
    if len(window) < threshold:
        return []
    keys = set()
    for sweep in window:
        keys.update((dict(sweep or {}).get("probes") or {}))
        keys.update((dict(sweep or {}).get("skipped") or []))
    surfaces = (dict(current or {}).get("surfaces") or {})
    rows = []
    for key in sorted(keys):
        states = [surface_state(sweep, key) for sweep in window]
        if any(s in (STATE_OK, STATE_DRIFT) for s in states):
            continue
        meta = surfaces.get(key) or {}
        rows.append({"key": key, "states": states, "weeks": len(window),
                     "surface": meta.get("surface") or key, "flag": meta.get("flag") or ""})
    return rows


def stale_marker(key: str) -> str:
    return f"{STALE_MARKER_PREFIX}{str(key or '').strip()}"


def build_stale_title(row: dict) -> str:
    title = f"SDUI sweep blind spot: {(row or {}).get('surface') or (row or {}).get('key')}"
    return title[:MAX_TITLE_CHARS]


def build_stale_body(row: dict, user_id=None) -> str:
    # `user_id` is accepted, not used: it keeps this call-compatible with `build_body` so
    # `apply_actions` can take either body builder without a special case.
    """A DIFFERENT body from `build_body`: the finding is "we cannot see this surface", not "the
    page shows content the locator misses" — there is no page-native evidence to attach, only the
    per-week state history.
    """
    row = dict(row or {})
    weeks = row.get("weeks") or STALE_THRESHOLD
    states = row.get("states") or []
    return "\n".join([
        "## Why now",
        "",
        f"**{row.get('surface')}** has graded neither `ok` nor `drift` for the last {weeks} "
        "weekly SDUI drift sweeps — it went unmeasured, not merely unchanged. That is invisible "
        "in the weekly summary line today: an unmeasured surface reads the same as a healthy one "
        "until a human happens to notice, which is exactly how #1733 cost 17 days (issue #1770).",
        "",
        f"Trailing states, oldest first: `{', '.join(states) or '(none recorded)'}`",
        "",
        "## Scope",
        "",
        f"- Find out why `{row.get('key')}` keeps grading `unknown`: no resolvable target (see "
        "`target_resolution` in the sweep JSON), a page that never renders, or the probe itself "
        "raising every week.",
        "- Fix the resolver, the probe, or the underlying data gap so the surface is measured "
        "again — this issue is about COVERAGE, not about re-grounding a specific locator.",
        "",
        "## Acceptance",
        "",
        f"- [ ] The `{row.get('key')}` probe grades `ok` or `drift` (not `unknown`) in a live "
        "weekly sweep.",
        "",
        f"Auto-filed by `scripts/weekly_sdui_drift_check.sh` (issue #1770). Dedup marker (do not "
        f"remove): `{stale_marker(row.get('key'))}`",
    ])


def plan_actions(rows: list, filed: Optional[set] = None,
                 max_new: int = DEFAULT_MAX_NEW, marker_fn=marker) -> list:
    """What this run would do with each row (drift or, via `marker_fn=stale_marker`, staleness).
    `filed` is the set of markers already on an OPEN issue. The cap is per RUN, never a silent
    truncation — a capped row is planned as `skip` with its reason so `summarize()` says how many
    are waiting."""
    already = {str(m) for m in (filed or set())}
    actions, created = [], 0
    for row in rows or []:
        key = marker_fn(row.get("key"))
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


def filed_markers(github: GitHubIssues, rows: list, marker_fn=marker) -> set:
    found = set()
    for row in rows or []:
        key = marker_fn(row.get("key"))
        if github.is_filed(key):
            found.add(key)
    return found


def apply_actions(github: GitHubIssues, actions: list, user_id=None,
                  dry_run: bool = True, title_fn=build_title, body_fn=build_body,
                  labels=LABELS) -> list:
    """File the planned issues (or, in dry-run, just report them). Never raises on a GitHub
    failure: a row that fails to file is left unfiled and retried by the next sweep."""
    applied = []
    for action in pending(actions):
        title = title_fn(action["row"])
        if dry_run:
            print(f"  would file: {title}")
            continue
        url = github.create(title, body_fn(action["row"], user_id), labels=labels)
        if url:
            print(f"  filed: {url} — {title}")
            applied.append({**action, "url": url})
    return applied


def fenced_report(raw: str) -> str:
    """The report, cut out of a stdout capture that also carries the app's own log lines.

    The probe runs inside the Celery worker, where `cqc_lem.utilities.logger` writes to stdout too,
    so a raw capture is `<log lines> <fence> <json> <fence>`. Unfenced input is passed through
    untouched — a hand-saved report from before the fences still loads."""
    text = raw or ""
    start = text.rfind(REPORT_JSON_BEGIN)
    if start < 0:
        return text
    body = text[start + len(REPORT_JSON_BEGIN):]
    end = body.find(REPORT_JSON_END)
    return body if end < 0 else body[:end]


def load_sweep(path: Optional[str]) -> dict:
    if not path or path == "-":
        raw = sys.stdin.read()
    else:
        with open(path, encoding="utf-8") as handle:
            raw = handle.read()
    if not (raw or "").strip():
        raise ValueError("sweep JSON is empty")
    sweep = json.loads(fenced_report(raw))
    if not isinstance(sweep, dict):
        raise ValueError("sweep JSON is not an object")
    return sweep


def load_recent_sweeps(directory: Optional[str], limit: int, exclude: Optional[str] = None) -> list:
    """The `limit` most recent `sweep-*.json` files in `directory`, OLDEST FIRST — the trailing
    window `stale_rows` needs. The STAMP in each filename sorts lexicographically, same as the shell
    sweep's own retention (`find ... -mtime +90 -delete`), so plain name order is chronological.

    Never raises: a missing directory, an unreadable file or a directory that doesn't exist yet (the
    very first run) all just mean "no history yet" — Phase 2 staying silent on a short tail is the
    point, not a bug to surface here.
    """
    if not directory:
        return []
    try:
        from pathlib import Path
        candidates = sorted(p for p in Path(directory).glob("sweep-*.json")
                            if p.name != (exclude or ""))
    except Exception:
        return []
    sweeps = []
    for path in candidates[-limit:]:
        try:
            sweeps.append(json.loads(fenced_report(path.read_text(encoding="utf-8"))))
        except Exception:
            continue
    return sweeps


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
    parser.add_argument("--history-dir", default=os.getenv("SDUI_DRIFT_DIR", ""),
                        help="Directory of past sweep-*.json files, for the Phase-2 staleness "
                             "check (issue #1770). Empty = staleness check skipped.")
    parser.add_argument("--stale-max-new", type=int, default=1,
                        help="Cap on blind-spot issues filed per run (default 1 — this is a "
                             "coverage gap, not an incident queue).")
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

    history = load_recent_sweeps(args.history_dir, STALE_THRESHOLD - 1,
                                 exclude=os.path.basename(args.sweep_file or ""))
    stale = stale_rows(sweep, history)
    if stale:
        print(f"Blind spot ({STALE_THRESHOLD}+ sweeps unmeasured): "
              f"{', '.join(r['key'] for r in stale)}")

    if not rows and not stale:
        return 0

    github = GitHubIssues(args.repo)
    dry_run = not args.apply
    exit_code = 0
    if rows:
        try:
            already = filed_markers(github, rows)
        except Exception as e:
            print(f"GitHub dedup lookup failed: {e}", file=sys.stderr)
            return 1
        actions = plan_actions(rows, already, args.max_new)
        print(f"Drift: {summarize(actions)}")
        apply_actions(github, actions, sweep.get("user_id"), dry_run=dry_run)
        if dry_run and pending(actions):
            exit_code = 2

    if stale:
        try:
            already_stale = filed_markers(github, stale, marker_fn=stale_marker)
        except Exception as e:
            print(f"GitHub dedup lookup failed (blind-spot check): {e}", file=sys.stderr)
            return 1
        stale_actions = plan_actions(stale, already_stale, args.stale_max_new,
                                     marker_fn=stale_marker)
        print(f"Blind spot: {summarize(stale_actions)}")
        apply_actions(github, stale_actions, sweep.get("user_id"), dry_run=dry_run,
                      title_fn=build_stale_title, body_fn=build_stale_body,
                      labels=STALE_LABELS)
        if dry_run and pending(stale_actions) and exit_code == 0:
            exit_code = 2

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
