#!/usr/bin/env python3
"""Weekly TODO->GitHub-issues sweep.

Sibling of `posthog_error_issues.py` (same shape: pure logic / I/O split, marker-in-body dedup,
dry-run by default, per-run cap). Where that cron turns runtime errors into backlog, this one turns
UNFINISHED-CODE COMMENTS into backlog: TODO/FIXME-class markers and "not yet implemented"-class
phrases are exactly how the catch-up card locators shipped broken for months — the code SAID it
needed a live-grounding pass ("NOTE: needs a supervised live-grounding pass ... (see #478)"), but
nothing tracked that work, so nothing ever did it (fixed in PR #964; this cron exists so the next
one is filed automatically).

Dedup: every hit gets a stable fingerprint `todo-sweep-<sha1[:12]>` of (path + normalized comment
text) — line-number independent, so moving code never refiles; editing the comment text does, which
is acceptable (an edited TODO is new information). The fingerprint is written into the filed body
and searched for on later runs across open AND closed issues: closing an issue without removing the
comment means "judged — don't refile".

One GitHub issue per hit (not per file): a 10k-line module can carry unrelated TODOs that belong to
different owners/issues.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Optional

DEFAULT_REPO = "christopherqueenconsulting/linkedin_engagement_manager"

# Written into every filed body and searched for on later runs. Do not change the prefix without
# migrating the issues already carrying it.
MARKER_PREFIX = "todo-sweep-"

LABELS = ("agent:ready", "cleanup")

DEFAULT_MAX_NEW = 5
MAX_TITLE_CHARS = 120
GH_TIMEOUT_SECONDS = 60
CONTEXT_LINES = 6  # lines of surrounding code quoted in the issue body

# Trees swept. git-tracked files only (the scan shells out to `git grep`), so build output and
# vendored node_modules never appear.
SCAN_ROOTS = ("src", "scripts", ".litellm", "compose", ".github")

# Paths never swept, with the reason each is excluded:
EXCLUDE_PATHS = (
    "src/cqc_lem/streamlit/",                  # legacy tree, deletion tracked whole in #972
    "src/cqc_lem/aws/",                        # CDK path's ~20 TODOs tracked as ONE decision in #973
    "compose/local/celery/flower/static/",     # vendored Flower assets — not our code
    "scripts/todo_issue_sweep.py",             # this script names its own markers
    "scripts/todo_to_issues.sh",
)

# Token markers must appear in a comment (after # or //) so identifiers like `todo_list` or hex
# blobs never match. Phrase markers match anywhere — they live in docstrings as often as comments.
TOKEN_RE = re.compile(r"(?:#|//).*?\b(TODO|FIXME|XXX|HACK|TBD)\b", re.IGNORECASE)
PHRASE_RE = re.compile(
    r"not yet implemented|no-op stub|not implemented — no-op"
    r"|needs a supervised live-grounding", re.IGNORECASE)


# ─────────────────────────────── pure logic (unit-tested) ───────────────────────────────

def is_excluded(path: str) -> bool:
    p = str(path).replace("\\", "/")
    return any(p == e or p.startswith(e) for e in EXCLUDE_PATHS)


def classify_line(line: str) -> Optional[str]:
    """The marker this line carries, or None. Token markers only count inside a comment."""
    m = TOKEN_RE.search(line or "")
    if m:
        return m.group(1).upper()
    if PHRASE_RE.search(line or ""):
        return "STUB"
    return None


def normalize(text: str) -> str:
    """Fingerprint text: lowercase, alphanumerics collapsed to single-space words. Whitespace,
    punctuation and line-number drift never change the fingerprint."""
    return " ".join(re.findall(r"[a-z0-9]+", (text or "").lower()))


def fingerprint(path: str, text: str) -> str:
    digest = hashlib.sha1(f"{path}::{normalize(text)}".encode()).hexdigest()[:12]
    return f"{MARKER_PREFIX}{digest}"


def parse_grep(output: str) -> list:
    """`git grep -n` lines -> [{path, line, text}], excluded paths dropped."""
    hits = []
    for raw in (output or "").splitlines():
        parts = raw.split(":", 2)
        if len(parts) != 3:
            continue
        path, line_no, text = parts
        if is_excluded(path) or not line_no.isdigit():
            continue
        kind = classify_line(text)
        if kind is None:
            continue
        hits.append({"path": path, "line": int(line_no), "text": text.strip(), "kind": kind,
                     "marker": fingerprint(path, text)})
    return hits


def dedupe_hits(hits: list) -> list:
    """Same normalized comment in the same file counts once, whatever line it sits on."""
    seen = set()
    unique = []
    for hit in hits or []:
        if hit["marker"] in seen:
            continue
        seen.add(hit["marker"])
        unique.append(hit)
    return unique


def build_title(hit: dict) -> str:
    head = re.sub(r"^\W+", "", hit["text"])[:70].rstrip() or hit["path"]
    title = f"chore(todo-sweep): {hit['path']} — {hit['kind']}: {head}"
    return title[:MAX_TITLE_CHARS]


def build_body(hit: dict, context: str = "") -> str:
    lines = [
        f"Weekly unfinished-code sweep hit in `{hit['path']}` line {hit['line']}:",
        "",
        "```",
        hit["text"],
        "```",
    ]
    if context:
        lines += ["", f"Context ({CONTEXT_LINES} lines around the marker):", "```", context, "```"]
    lines += [
        "",
        "This comment says the code is unfinished, partial, or needs follow-up work. Implement "
        "what it asks for (or delete the marker with a rationale if it no longer applies). "
        "If this belongs to an existing issue, close this one as a duplicate — closing it "
        "prevents refiling.",
        "",
        f"<!-- {hit['marker']} -->",
        f"Dedup key: `{hit['marker']}` (do not remove; the sweep searches for it).",
        "",
        "Filed automatically by `scripts/todo_to_issues.sh` (weekly).",
    ]
    return "\n".join(lines)


def plan_actions(hits: list, filed: set, max_new: int = DEFAULT_MAX_NEW) -> list:
    """One `create` per unfiled hit, capped at max_new; the rest defer to next week's run."""
    already = {str(m) for m in (filed or set())}
    actions = []
    created = 0
    for hit in dedupe_hits(hits):
        if hit["marker"] in already:
            actions.append({"action": "skip", "reason": "already filed", "hit": hit})
        elif created >= max(0, int(max_new)):
            actions.append({"action": "deferred", "reason": "max-new reached", "hit": hit})
        else:
            actions.append({"action": "create", "hit": hit})
            created += 1
    return actions


def pending(actions: list) -> list:
    return [a for a in actions or [] if a.get("action") == "create"]


def summarize(actions: list) -> str:
    counts: dict = {}
    for action in actions or []:
        counts[action["action"]] = counts.get(action["action"], 0) + 1
    if not counts:
        return "no unfinished-code markers found"
    return ", ".join(f"{count} {name}" for name, count in sorted(counts.items()))


# ─────────────────────────────── I/O (mocked in tests) ───────────────────────────────

def scan_repo(repo_root: str = ".") -> list:
    """All marker hits in the tracked tree. `git grep` so only committed code is swept — a
    work-in-progress checkout never files issues for itself."""
    pattern = r"TODO\|FIXME\|XXX\|HACK\|TBD\|[Nn]ot yet implemented\|no-op stub\|supervised live-grounding"
    result = subprocess.run(
        ["git", "grep", "-n", "-e", pattern, "--", *SCAN_ROOTS],
        capture_output=True, text=True, cwd=repo_root, timeout=GH_TIMEOUT_SECONDS)
    # git grep exits 1 on "no matches" — only >1 is an error.
    if result.returncode > 1:
        raise RuntimeError(f"git grep failed: {(result.stderr or '').strip()[:200]}")
    return parse_grep(result.stdout)


def read_context(repo_root: str, hit: dict) -> str:
    try:
        lines = Path(repo_root, hit["path"]).read_text(errors="replace").splitlines()
    except OSError:
        return ""
    start = max(0, hit["line"] - 1 - CONTEXT_LINES // 2)
    return "\n".join(lines[start:start + CONTEXT_LINES])


class GitHubIssues:
    """GitHub via the `gh` CLI — the cron host is already authenticated with it."""

    def __init__(self, repo: str = DEFAULT_REPO) -> None:
        self.repo = repo

    def _run(self, args: list) -> subprocess.CompletedProcess:
        return subprocess.run(args, capture_output=True, text=True, timeout=GH_TIMEOUT_SECONDS)

    def is_filed(self, issue_marker: str) -> bool:
        """True when ANY issue — open or closed — carries this marker. Closed counts: a marker
        someone judged and closed must never come back weekly. Fail CLOSED on search errors, or a
        GitHub outage files a duplicate for every hit in the tree."""
        result = self._run(["gh", "issue", "list", "--repo", self.repo, "--state", "all",
                            "--search", issue_marker, "--json", "number,body"])
        if result.returncode != 0:
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


def filed_markers(github: GitHubIssues, hits: list) -> set:
    found = set()
    for hit in dedupe_hits(hits):
        if github.is_filed(hit["marker"]):
            found.add(hit["marker"])
    return found


def apply_actions(github: GitHubIssues, actions: list, repo_root: str = ".",
                  dry_run: bool = True) -> list:
    """File the planned issues (or report them in dry-run). A hit that fails to file stays
    unfiled and is retried next week."""
    applied = []
    for action in pending(actions):
        hit = action["hit"]
        title = build_title(hit)
        if dry_run:
            print(f"  would file: {title}  [{hit['marker']}]")
            continue
        url = github.create(title, build_body(hit, read_context(repo_root, hit)))
        if url:
            print(f"  filed: {url} — {title}")
            applied.append({**action, "url": url})
    return applied


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true",
                        help="actually file issues (default: dry-run report)")
    parser.add_argument("--max-new", type=int, default=DEFAULT_MAX_NEW)
    parser.add_argument("--repo", default=DEFAULT_REPO)
    parser.add_argument("--repo-root", default=".")
    args = parser.parse_args(argv)

    hits = scan_repo(args.repo_root)
    print(f"{len(dedupe_hits(hits))} unfinished-code marker(s) in the tree")

    github = GitHubIssues(repo=args.repo)
    try:
        filed = filed_markers(github, hits)
    except RuntimeError as e:
        print(f"GitHub dedup lookup failed: {e}", file=sys.stderr)
        return 1

    actions = plan_actions(hits, filed, max_new=args.max_new)
    print(f"plan: {summarize(actions)}")
    apply_actions(github, actions, repo_root=args.repo_root, dry_run=not args.apply)
    return 0


if __name__ == "__main__":
    sys.exit(main())
