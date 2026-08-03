#!/usr/bin/env python3
"""Sweep stale branches from `christopherqueenconsulting/linkedin_engagement_manager`.

The repo accumulates head branches from the agent pipeline + dependabot + ad-hoc work, and
`delete_branch_on_merge` was OFF through v0.106.0 — so merged-PR branches sat around forever,
closed-but-not-merged branches did too, and orphan worktree-agent-* and feature/claude-issue-*
references accumulated as the pipeline ran. Nothing in `.github/workflows/` cleans any of it.

Two layers solve this:
  1. `delete_branch_on_merge=true` (repo setting, flipped once) auto-deletes the head branch
     on every future merged PR.
  2. This script + `.github/workflows/stale-branches.yml` (the weekly cron equivalent) catch
     the cases the setting misses: closed-but-not-merged branches, orphans, abandoned PRs.

The script applies a 48-hour grace window on the branch tip's committer date so an in-flight
agent is never interrupted mid-edit, and never auto-deletes release-related, milestone/*, or
worktree-agent-* branches — those go into the manifest's per-branch review section instead.

Categorisation (see also `docs/branch-cleanup.md`):

  HELD   — within 48h cutoff OR matches the EXEMPT regex. Never touched.
  Class A — merged to main AND PR state MERGED. Safe to delete remote + local.
  Class B — PR closed (never merged). Safe to delete (the PR records the rejection).
  Class C — orphan (no PR ever). LIST ONLY in the manifest. Never auto-deleted.
  Class D — release-related (see EXEMPT_RE). LIST ONLY with per-branch recommendation.
  Class E — branch tip equals main tip (no unique work). Safe to delete.

CLI:
  --manifest-only           Re-derive and write the manifest. Default; no deletes. (no flag needed)
  --apply                   After writing the manifest, delete Class A/B/E in 25-branch batches.
                            Ctrl-C between batches to abort.
  --batch-size N            Override the per-batch deletion cap (default 25).
  --cutoff-hours N          Override the grace window (default 48).
  --keep-class-c NAME       Allow-list an additional Class-C branch into auto-deletion. Repeatable.
                            Use the same name as the survey agent ("feature/claude-issue-600").
  --manifest-path PATH      Override the manifest output path. Default docs/branch-cleanup-audit-<date>.md.
  --repo OWNER/NAME         Override the GitHub repo (default reads from `gh repo view`).
  --dry-run                 Same as --manifest-only (kept for parity with the other ops scripts).

Exit: 0 manifest written / deletions applied, 2 nothing to do, 1 error.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

# ---------- repo resolution ----------

DEFAULT_REPO = "christopherqueenconsulting/linkedin_engagement_manager"


def _parse_committer_date(date_str: str) -> datetime:
    """Parse `%(committerdate:iso8601)` ("YYYY-MM-DD HH:MM:SS ±HHMM") into a UTC datetime.

    Some refs come through without the space (e.g. "2026-07-0411:46:53+0000") — git's format
    placeholders are inconsistent across versions. The two known shapes are handled, then
    anything else falls back to "now" so a single bad date can't block the sweep (it gets
    classified as HELD, which is the safe outcome).
    """
    s = date_str.strip().replace(" ", "")
    if not s:
        return datetime.now(timezone.utc)
    m = re.match(r"^(\d{4}-\d{2}-\d{2})T?(\d{2}:\d{2}:\d{2})([+-]\d{4})$", s)
    if m:
        d, t, tz = m.groups()
        tz_norm = tz[:-2] + ":" + tz[-2:]
        return datetime.fromisoformat(f"{d}T{t}{tz_norm}").astimezone(timezone.utc)
    try:
        return datetime.fromisoformat(s).astimezone(timezone.utc)
    except ValueError:
        return datetime.now(timezone.utc)


# Branches matching this regex are ALWAYS held, regardless of any other signal. Mirrors
# `.github/workflows/stale-branches.yml` and `docs/branch-cleanup.md` — keep all three in sync.
# The 2026-07-28 sweep's historical pins (milestone/*, worktree-agent-*, four named release/pipeline
# branches) were deleted by hand on 2026-08-03 (owner-approved; all content on main), so they no
# longer need exempting. Active agent worktrees don't need the worktree-agent pin either: the 48h
# grace covers a live push, and an orphan (no PR) is held-and-surfaced, never deleted.
EXEMPT_RE = re.compile(
    r"^(?:"
    r"main|master|develop"
    r"|release-please--branches--main"
    r"|release/.*"
    r")$"
)


@dataclass
class Branch:
    name: str
    ref: str
    tip_sha: str
    author: str
    last_commit: datetime
    ahead: int = 0
    behind: int = 0
    is_local: bool = False
    pr_number: int | None = None
    pr_state: str | None = None
    pr_mergeable: str | None = None
    category: str = "?"
    action: str = "?"
    rationale: str = ""

    @property
    def is_protected(self) -> bool:
        return bool(EXEMPT_RE.match(self.name))


# ---------- subprocess helpers ----------


def _gh_json(args: list[str], repo: str) -> dict | list | None:
    cmd = ["gh", *args, "--repo", repo]
    r = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=120)
    if r.returncode != 0 and not r.stdout.strip():
        return None
    if r.returncode != 0:
        raise RuntimeError(f"gh {args} failed: {r.stderr.strip()}")
    out = r.stdout.strip()
    if not out:
        return None
    return json.loads(out)


def _git(*args: str, check: bool = True) -> str:
    r = subprocess.run(["git", *args], capture_output=True, text=True, check=check, timeout=120)
    return r.stdout


# ---------- data gathering ----------


def fetch_branches(repo: str) -> list[Branch]:
    """List every local + remote ref, excluding HEAD symbolic refs."""
    _git("fetch", "origin", "--prune")
    refs = _git("for-each-ref", "--format=%(refname)|%(objectname)|%(committerdate:iso8601)|%(authorname)",
                "refs/heads", "refs/remotes").strip().splitlines()
    branches: list[Branch] = []
    for line in refs:
        ref, sha, date_str, author = line.split("|", 3)
        if ref.endswith("/HEAD"):
            continue
        name = re.sub(r"^refs/(?:remotes/[^/]+|heads)/", "", ref)
        last_commit = _parse_committer_date(date_str)
        branches.append(Branch(
            name=name,
            ref=ref,
            tip_sha=sha,
            author=author,
            last_commit=last_commit,
            is_local=ref.startswith("refs/heads/"),
        ))
    return branches


def fetch_pr_map(repo: str) -> dict[str, dict]:
    """Map branch -> latest PR info. Uses --state all."""
    prs = _gh_json(["pr", "list", "--state", "all", "--limit", "500",
                    "--json", "number,headRefName,state,mergedAt,mergeable"], repo)
    if prs is None:
        return {}
    out: dict[str, dict] = {}
    for p in prs:
        prev = out.get(p["headRefName"])
        if prev is None or p["number"] > prev["number"]:
            out[p["headRefName"]] = {
                "number": p["number"],
                "state": p["state"],
                "merged_at": p.get("mergedAt"),
                "mergeable": p.get("mergeable"),
            }
    return out


def annotate_with_prs(branches: list[Branch], pr_map: dict[str, dict]) -> None:
    for b in branches:
        info = pr_map.get(b.name)
        if not info:
            continue
        b.pr_number = info["number"]
        b.pr_state = info["state"] if not info["merged_at"] else "MERGED"


def compute_ahead_behind(branches: list[Branch]) -> None:
    main_sha = _git("rev-parse", "--verify", "origin/main").strip()
    for b in branches:
        if b.tip_sha == main_sha:
            b.ahead = 0
            b.behind = 0
            continue
        out = _git(
            "rev-list", "--left-right", "--count",
            f"origin/main...{b.ref}",
            check=False,
        ).strip()
        if not out or "\t" not in out:
            b.ahead = 0
            b.behind = 0
            continue
        behind, ahead = out.split("\t")
        b.ahead = int(ahead)
        b.behind = int(behind)


# ---------- categorisation ----------


def categorise(branches: list[Branch], cutoff: datetime, keep_class_c: set[str],
                cutoff_hours: int) -> None:
    for b in branches:
        if b.is_protected:
            b.category = "HELD"
            b.action = "KEEP"
            b.rationale = "matches EXEMPT_RE"
            continue
        if b.last_commit >= cutoff:
            b.category = "HELD"
            b.action = "KEEP"
            b.rationale = f"within cutoff (touched {b.last_commit.isoformat(timespec='seconds')})"
            continue
        if b.ahead == 0 and b.behind == 0:
            b.category = "E"
            b.action = "DELETE"
            b.rationale = "tip equals origin/main — no unique work"
            continue
        if b.pr_state == "MERGED":
            b.category = "A"
            b.action = "DELETE"
            b.rationale = f"PR #{b.pr_number} merged; head branch stale"
            continue
        if b.pr_state == "CLOSED":
            b.category = "B"
            b.action = "DELETE"
            b.rationale = f"PR #{b.pr_number} closed without merge"
            continue
        if b.pr_state == "OPEN":
            b.category = "C"
            b.action = "REVIEW"
            b.rationale = (
                f"PR #{b.pr_number} still OPEN but branch tip is {b.last_commit.date()} "
                f"(>{cutoff_hours}h old). Confirm the author is paused, then decide."
            )
            continue
        if b.name in keep_class_c:
            b.category = "C"
            b.action = "DELETE"
            b.rationale = f"orphan; opted into deletion via --keep-class-c"
            continue
        b.category = "C"
        b.action = "REVIEW"
        b.rationale = "orphan (no PR); human decision required"


# ---------- manifest ----------


def write_manifest(branches: list[Branch], cutoff: datetime, path: str,
                   cutoff_hours: int) -> None:
    counts: dict[str, int] = {}
    for b in branches:
        counts[b.category] = counts.get(b.category, 0) + 1

    held = sorted([b for b in branches if b.category == "HELD"], key=lambda x: x.last_commit, reverse=True)
    deletes = sorted([b for b in branches if b.action == "DELETE"], key=lambda x: x.name)
    reviews = sorted([b for b in branches if b.action == "REVIEW"], key=lambda x: x.last_commit, reverse=True)

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    lines: list[str] = []
    lines.append(f"# Branch Cleanup Audit — {today}")
    lines.append("")
    lines.append("Generated by `scripts/branch_cleanup.py`. Read `docs/branch-cleanup.md` first.")
    lines.append("")
    lines.append("## Summary")
    lines.append("")
    lines.append(f"- cutoff: **{cutoff.isoformat(timespec='seconds')}** ({cutoff_hours}h grace window)")
    lines.append(f"- total refs surveyed: **{len(branches)}**")
    lines.append("")
    lines.append("| Class | Count | Action |")
    lines.append("|---|---|---|")
    for cls in ("HELD", "A", "B", "C", "E"):
        n = counts.get(cls, 0)
        action = {
            "HELD": "KEEP (cutoff / EXEMPT_RE)",
            "A": "DELETE (merged PR)",
            "B": "DELETE (closed-not-merged PR)",
            "C": "REVIEW per branch (orphan or stale-OPEN-PR)",
            "E": "DELETE (tip == main)",
        }[cls]
        lines.append(f"| {cls} | {n} | {action} |")
    lines.append("")

    lines.append("## Auto-deletion candidates (Class A / B / E)")
    lines.append("")
    lines.append("| Branch | Author | Last commit | ± vs main | PR | Class | Tip SHA | Rationale |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for b in deletes:
        lines.append(
            f"| `{b.name}` | {b.author} | {b.last_commit.strftime('%Y-%m-%d %H:%M UTC')} "
            f"| +{b.ahead}/−{b.behind} | {b.pr_number or '—'} / {b.pr_state or '—'} "
            f"| {b.category} | `{b.tip_sha[:12]}` | {b.rationale} |"
        )
    lines.append("")

    lines.append("## Held by cutoff / EXEMPT_RE (within grace window or release-related)")
    lines.append("")
    lines.append("| Branch | Author | Last commit | Reason |")
    lines.append("|---|---|---|---|")
    for b in held:
        lines.append(
            f"| `{b.name}` | {b.author} | {b.last_commit.strftime('%Y-%m-%d %H:%M UTC')} | {b.rationale} |"
        )
    lines.append("")

    lines.append("## Orphan (Class C) — review per branch")
    lines.append("")
    lines.append(
        "These have no PR OR have an OPEN PR whose branch tip is older than the cutoff. "
        "Re-run with `--keep-class-c <name>` to opt individual branches into auto-deletion. "
        "By default no Class-C branch is auto-deleted."
    )
    lines.append("")
    lines.append("| Branch | Author | Last commit | ± vs main | PR | Tip SHA | Decision |")
    lines.append("|---|---|---|---|---|---|---|")
    for b in reviews:
        lines.append(
            f"| `{b.name}` | {b.author} | {b.last_commit.strftime('%Y-%m-%d %H:%M UTC')} "
            f"| +{b.ahead}/−{b.behind} | {b.pr_number or '—'} / {b.pr_state or '—'} "
            f"| `{b.tip_sha[:12]}` | [ ] KEEP  [ ] DELETE |"
        )
    lines.append("")

    lines.append("## Recovery")
    lines.append("")
    lines.append("A deleted branch can be resurrected from its recorded SHA by anyone with push access:")
    lines.append("")
    lines.append("```")
    lines.append("git push origin <tip_sha>:refs/heads/<branch_name>")
    lines.append("```")
    lines.append("")
    lines.append("Reflog (`git reflog`) works locally too, within the local retention window.")

    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")


# ---------- deletion ----------


def apply_deletions(branches: list[Branch], batch_size: int) -> tuple[int, int]:
    """Delete remote + local copies of Class A/B/E branches in batches. Returns (deleted, failed)."""
    targets = [b for b in branches if b.action == "DELETE"]
    deleted = 0
    failed = 0

    seen_remote: set[str] = set()
    remote_deletes: list[Branch] = []
    for b in targets:
        if b.ref.startswith("refs/remotes/"):
            if b.name in seen_remote:
                continue
            seen_remote.add(b.name)
            remote_deletes.append(b)

    for i in range(0, len(remote_deletes), batch_size):
        batch = remote_deletes[i:i + batch_size]
        batch_n = i // batch_size + 1
        batch_total = (len(remote_deletes) + batch_size - 1) // batch_size
        print(f"\n=== Batch {batch_n}/{batch_total} ({len(batch)} branches) ===")
        for b in batch:
            r = subprocess.run(
                ["git", "push", "origin", "--delete", b.name],
                capture_output=True, text=True, check=False, timeout=30,
            )
            if r.returncode == 0:
                print(f"  [delete] origin/{b.name}")
                deleted += 1
            elif "remote ref does not exist" in (r.stderr or ""):
                print(f"  [skip]   origin/{b.name} (already gone)")
                deleted += 1
            else:
                print(f"  [fail]   origin/{b.name}: {(r.stderr or '').strip()}")
                failed += 1
        if i + batch_size < len(remote_deletes):
            print("  (Ctrl-C to abort between batches)")

    local_targets = [b for b in targets if b.is_local and b.ref.startswith("refs/heads/")]
    for b in local_targets:
        r = subprocess.run(
            ["git", "branch", "-d", b.name],
            capture_output=True, text=True, check=False, timeout=15,
        )
        if r.returncode == 0:
            print(f"  [delete] local/{b.name}")
            deleted += 1
        elif "not found" in (r.stderr or ""):
            pass
        else:
            print(f"  [skip]   local/{b.name}: {(r.stderr or '').strip()}")

    if deleted or failed:
        subprocess.run(["git", "remote", "prune", "origin"], capture_output=True, text=True, timeout=30)
        subprocess.run(["git", "worktree", "prune"], capture_output=True, text=True, timeout=30)

    return deleted, failed


# ---------- entry point ----------


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Sweep stale branches; emit a manifest before any deletion.")
    p.add_argument("--apply", action="store_true",
                   help="After writing the manifest, perform the Class A/B/E deletions.")
    p.add_argument("--manifest-only", action="store_true",
                   help="Write the manifest only; never delete. Default if neither flag is set.")
    p.add_argument("--dry-run", action="store_true", help="Alias for --manifest-only.")
    p.add_argument("--batch-size", type=int, default=25, help="Per-batch deletion cap (default 25).")
    p.add_argument("--cutoff-hours", type=int, default=48, help="Grace window in hours (default 48).")
    p.add_argument("--keep-class-c", action="append", default=[],
                   help="Orphan branch name to opt into deletion. Repeatable.")
    p.add_argument("--manifest-path", default=None,
                   help="Override the manifest output path. Default docs/branch-cleanup-audit-<date>.md.")
    p.add_argument("--repo", default=DEFAULT_REPO,
                   help=f"GitHub OWNER/NAME. Default {DEFAULT_REPO}.")
    args = p.parse_args()
    if args.apply and (args.manifest_only or args.dry_run):
        p.error("--apply is mutually exclusive with --manifest-only and --dry-run")
    return args


def main() -> int:
    args = parse_args()
    apply_deletes = bool(args.apply)

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    manifest_path = args.manifest_path or f"docs/branch-cleanup-audit-{today}.md"
    cutoff = datetime.now(timezone.utc) - timedelta(hours=args.cutoff_hours)
    keep_class_c = set(args.keep_class_c)

    print(f"repo:        {args.repo}")
    print(f"cutoff:      {cutoff.isoformat(timespec='seconds')}")
    print(f"manifest:    {manifest_path}")
    print(f"apply:       {apply_deletes}")

    branches = fetch_branches(args.repo)
    print(f"surveyed:    {len(branches)} refs")

    pr_map = fetch_pr_map(args.repo)
    annotate_with_prs(branches, pr_map)
    compute_ahead_behind(branches)
    categorise(branches, cutoff, keep_class_c, args.cutoff_hours)

    write_manifest(branches, cutoff, manifest_path, args.cutoff_hours)
    print(f"manifest written: {manifest_path}")

    counts = {c: sum(1 for b in branches if b.category == c)
              for c in ("HELD", "A", "B", "C", "E")}
    print(f"HELD={counts['HELD']}  A={counts['A']}  B={counts['B']}  C={counts['C']}  E={counts['E']}")

    if not apply_deletes:
        return 2 if counts["A"] + counts["B"] + counts["E"] == 0 else 0

    deleted, failed = apply_deletions(branches, args.batch_size)
    print(f"\ndeleted={deleted}  failed={failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())