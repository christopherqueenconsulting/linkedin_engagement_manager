#!/usr/bin/env python3
"""Flag a migration-bearing or `risk:*`-labelled release before it auto-deploys (#1133).

`.github/workflows/build-and-push.yml`'s `deploy` job runs on `environment: production` with its
required-reviewer gate deliberately removed — "green releases auto-deploy" per the workflow's own
comment. That is the right default for the overwhelming majority of releases, but a Flyway migration
is one-way against production data, and a release carrying a `risk:security` /
`risk:live-linkedin` / `risk:product-decision` PR deserves a second look before it reaches every
user's traffic. This script is the `release-risk-check` job's brain, called between `build-and-push`
and `deploy` — see `docs/graphs/deploy-release.md` for the reviewed design (3 gauntlet-loop rounds).

Diff source, deliberately: **this run's own tag** (`needs.build-and-push.outputs.tag`), never
`.last_good_tag` (VPS-local, unreachable from the Actions runner). `gh release list` is sorted
EXPLICITLY by `createdAt` here — never trusted as already sorted, because a second release (a
`release:now` fast-lane release, or the next scheduled window) can publish mid-build and reorder the
API's default response. The "previous release" is the next-older entry from THIS tag's own position
in that sorted list, not list index 0/1.

A release is flagged when the commit range between the previous release and this tag contains
EITHER:
  (a) a newly ADDED file under `compose/local/database/migrations/`, or
  (b) a merged PR that closed an issue labelled `risk:security` / `risk:live-linkedin` /
      `risk:product-decision`.

On a flag, the workflow skips the automatic `deploy` job (this script only writes the `flagged`
GitHub Actions output the job's `if:` reads — it never exits non-zero for a flag, since that would
turn the *workflow run* red for working exactly as designed) and this script posts a Decision Comment
on the release PR naming what triggered it. That comment is AUDIT / NOTIFICATION ONLY: nothing in
this repo watches replies on a release-please PR (it carries no `agent:ready`/`needs-human` flow
label, and `tick.sh` never reads it) — the comment says so plainly rather than implying an automated
unblock exists. The unblock is the owner running the existing manual entrypoint:

    gh workflow run deploy-vps.yml -f tag=vX.Y.Z

Every read here (release list, compare, the release PR lookup, PR/issue label lookups) fails OPEN —
an unreadable GitHub API degrades to "nothing found here", never to blocking a routine deploy. That
mirrors every other convenience gate in this repo (`docs/feature-flags.md`, `codeql_pr_gate.py`): a
transient API hiccup must not become a new single point of failure for the 4x-daily release cadence
the batching window exists to protect. The one thing that does NOT fail open is an already-decided
flag: once evidence of a migration or a risk:* PR has been found, nothing here backs off from it.

CLI:
  --tag TAG              This run's own release tag (required; == `needs.build-and-push.outputs.tag`).
  --repo OWNER/REPO      Defaults to $GITHUB_REPOSITORY, else the hardcoded default.
  --release-limit N      `gh release list --limit` (default 20, per the acceptance criteria).
  --no-comment           Skip posting the Decision Comment even when flagged (used by tests / a
                          dry run); the `flagged` output is still written.

Env:
  GH_TOKEN / GITHUB_TOKEN  Read by the `gh` CLI itself. Missing = fail open with a loud warning.
  GITHUB_OUTPUT            Where `flagged`/`previous_tag` are written for the workflow's `if:`.

Exit: always 0. The verdict is carried entirely in the `flagged` GitHub Actions output, never in the
process exit code — a flag is expected, routine behavior for a `risk:*` release, not a script error.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass

DEFAULT_REPO = "christopherqueenconsulting/linkedin_engagement_manager"

#: The label vocabulary this check reads. Deliberately the same three names
#: `docs/graphs/agent-issue-shipping.md`'s risk:* gate uses — one vocabulary, not two.
RISK_LABELS = ("risk:security", "risk:live-linkedin", "risk:product-decision")

MIGRATIONS_PREFIX = "compose/local/database/migrations/"

#: The head branch release-please always uses for its standing accumulator PR.
RELEASE_PR_HEAD_REF = "release-please--branches--main"

GH_TIMEOUT_SECONDS = 60


# ────────────────────────────────────────────────────────────── pure decision logic


@dataclass(frozen=True)
class RiskyPR:
    """A merged PR in the commit range that closed a `risk:*`-labelled issue."""

    number: int
    issue_numbers: tuple[int, ...]
    labels: tuple[str, ...]


@dataclass(frozen=True)
class Verdict:
    """The gate's answer: whether to skip the automatic deploy, and why."""

    flagged: bool
    migration_files: tuple[str, ...]
    risky_prs: tuple[RiskyPR, ...]


def resolve_previous_release(releases: list[dict], tag: str) -> str | None:
    """Find the release immediately older than `tag`, sorting explicitly first.

    Args:
        releases: `gh release list --json tagName,createdAt` output — NOT assumed to already be in
            any particular order (an acceptance criterion of #1133: never trust API default order).
        tag: This run's own tag.

    Returns:
        The next-older tag's name, or `None` when `tag` is absent from `releases` (outside the
        `--limit` window) or is already the oldest entry present.
    """
    ordered = sorted(releases, key=lambda r: r["createdAt"], reverse=True)
    names = [r["tagName"] for r in ordered]
    try:
        idx = names.index(tag)
    except ValueError:
        return None
    if idx + 1 >= len(names):
        return None
    return names[idx + 1]


def added_migration_files(files: list[dict]) -> list[str]:
    """Which entries of a GitHub compare-API `files` array are a newly ADDED migration.

    Args:
        files: `.files` from `GET /repos/{repo}/compare/{base}...{head}` — each entry has
            `filename` and `status` (`added`/`modified`/`removed`/`renamed`/...).

    Returns:
        Sorted migration paths whose status is `added`. Only ADDED counts — migrations are
        additive-only (root `CLAUDE.md`), so an edit to an existing one is a different kind of
        problem, not this check's job.
    """
    return sorted(
        f["filename"]
        for f in files
        if f.get("status") == "added" and f.get("filename", "").startswith(MIGRATIONS_PREFIX)
    )


def extract_pr_numbers(commit_messages: list[str]) -> list[int]:
    """Pull the squash-merge PR number off each commit's SUBJECT line.

    Root `CLAUDE.md`: "the repo squashes with COMMIT_MESSAGES" — every commit in the range therefore
    carries its PR number as a trailing `(#N)` on the first line. Only the subject is read; the
    squash body concatenates every commit message from the PR and can legitimately mention other
    issue/PR numbers that are not this commit's own.

    Args:
        commit_messages: Full (possibly multi-line) commit message text per commit.

    Returns:
        Sorted, de-duplicated PR numbers.
    """
    numbers = set()
    for message in commit_messages:
        subject = message.split("\n", 1)[0].strip()
        m = re.search(r"\(#(\d+)\)\s*$", subject)
        if m:
            numbers.add(int(m.group(1)))
    return sorted(numbers)


def risk_labels_for_issues(issue_labels: dict[int, list[str]]) -> list[str]:
    """Which `RISK_LABELS` appear across a PR's closed issues.

    Args:
        issue_labels: Map of issue number -> that issue's label names.

    Returns:
        Sorted risk labels found across every issue's label set.
    """
    found: set[str] = set()
    for labels in issue_labels.values():
        found.update(label for label in labels if label in RISK_LABELS)
    return sorted(found)


def decide(*, migration_files: list[str], risky_prs: list[RiskyPR]) -> Verdict:
    """Should the automatic `deploy` job be skipped for this release?

    Args:
        migration_files: Newly added migration paths in the commit range.
        risky_prs: Merged PRs in the range that closed a `risk:*`-labelled issue.

    Returns:
        A `Verdict`. Either signal alone is enough — this is deliberately an OR, not a severity
        score: a lone migration is exactly as one-way as a migration alongside a `risk:security` PR.
    """
    return Verdict(
        flagged=bool(migration_files) or bool(risky_prs),
        migration_files=tuple(migration_files),
        risky_prs=tuple(risky_prs),
    )


def summarize(verdict: Verdict) -> str:
    """One-line reason string for logs and the `::warning::` annotation."""
    parts = []
    if verdict.migration_files:
        parts.append(f"{len(verdict.migration_files)} new migration file(s)")
    if verdict.risky_prs:
        parts.append(f"{len(verdict.risky_prs)} risk:*-labelled PR(s)")
    return ", ".join(parts) if parts else "nothing found"


def format_decision_comment(tag: str, previous_tag: str, verdict: Verdict) -> str:
    """Build the markdown body posted to the release PR when `verdict.flagged`.

    Args:
        tag: This run's tag.
        previous_tag: The tag it was diffed against.
        verdict: Must be flagged — this is only ever called after that check.

    Returns:
        Markdown naming the specific migration file(s) / PR(s), stating plainly that this comment
        is audit-only (nothing watches replies here), and giving the exact manual unblock command.
    """
    lines = [
        f"### :warning: `release-risk-check` flagged `{tag}`",
        "",
        f"The automatic `deploy` job was **skipped**. Diffed against the previous release "
        f"`{previous_tag}` (`gh release list`, sorted explicitly by `createdAt`, the next-older "
        f"entry from this run's own tag — never `.last_good_tag`, which is VPS-local and "
        f"unreachable from the Actions runner).",
        "",
    ]
    if verdict.migration_files:
        lines.append(f"**Migration file(s) added ({len(verdict.migration_files)}):**")
        lines.extend(f"- `{path}`" for path in verdict.migration_files)
        lines.append("")
    if verdict.risky_prs:
        lines.append(f"**PR(s) closing a `risk:*`-labelled issue ({len(verdict.risky_prs)}):**")
        for pr in verdict.risky_prs:
            closes = ", ".join(f"#{n}" for n in pr.issue_numbers)
            labels = ", ".join(f"`{label}`" for label in pr.labels)
            lines.append(f"- #{pr.number} (closes {closes}) — {labels}")
        lines.append("")
    lines.extend(
        [
            "**This comment is audit / notification only.** Nothing in this repo watches replies on "
            "a release-please PR — it carries no `agent:ready`/`needs-human` flow label, and "
            "`tick.sh` never reads this thread. There is no automated unblock.",
            "",
            "To ship this release, the owner runs the existing manual entrypoint:",
            "",
            "```",
            f"gh workflow run deploy-vps.yml -f tag={tag}",
            "```",
        ]
    )
    return "\n".join(lines)


# ────────────────────────────────────────────────────────────── thin I/O layer (gh CLI)


def _run_gh(args: list[str], *, input_text: str | None = None) -> subprocess.CompletedProcess:
    """One place every `gh` invocation goes through, so tests mock exactly here."""
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        input=input_text,
        timeout=GH_TIMEOUT_SECONDS,
        check=False,
    )


def _gh_json(args: list[str]) -> object | None:
    """Run a `gh` subcommand and parse its stdout as JSON.

    Returns:
        The parsed JSON, or `None` on any failure (non-zero exit, empty output, bad JSON) — the
        caller decides what "unreadable" means for that call; this layer never raises.
    """
    result = _run_gh(args)
    if result.returncode != 0:
        print(
            f"::warning title=release-risk-check gh call failed::`{' '.join(args)}` exited "
            f"{result.returncode}: {(result.stderr or '').strip()[:300]}"
        )
        return None
    out = (result.stdout or "").strip()
    if not out:
        return None
    try:
        return json.loads(out)
    except json.JSONDecodeError as exc:
        print(f"::warning title=release-risk-check gh call unreadable::{exc}")
        return None


def fetch_releases(repo: str, limit: int) -> list[dict] | None:
    """`gh release list --json tagName,createdAt --limit N`."""
    data = _gh_json(
        ["gh", "release", "list", "--repo", repo, "--json", "tagName,createdAt", "--limit", str(limit)]
    )
    return data if isinstance(data, list) else None


def fetch_compare(repo: str, base: str, head: str) -> dict | None:
    """`GET /repos/{repo}/compare/{base}...{head}` — files (with status) + commits, no local clone."""
    data = _gh_json(["gh", "api", f"repos/{repo}/compare/{base}...{head}"])
    return data if isinstance(data, dict) else None


def fetch_release_pr_number(repo: str, tag: str) -> int | None:
    """Which release-please PR's merge commit this tag points at.

    Uses "list pull requests associated with a commit" against the TAG directly (GitHub resolves
    it) rather than requiring a local checkout to dereference it to a SHA first.
    """
    data = _gh_json(["gh", "api", f"repos/{repo}/commits/{tag}/pulls"])
    if not isinstance(data, list) or not data:
        return None
    for pr in data:
        if isinstance(pr, dict) and pr.get("head", {}).get("ref") == RELEASE_PR_HEAD_REF:
            return pr.get("number")
    first = data[0]
    return first.get("number") if isinstance(first, dict) else None


def fetch_pr_closing_issues(repo: str, pr_number: int) -> list[int]:
    """Issue numbers this PR's merge closed, per GitHub's own linkage (not comment-text parsing)."""
    data = _gh_json(["gh", "pr", "view", str(pr_number), "--repo", repo, "--json", "closingIssuesReferences"])
    if not isinstance(data, dict):
        return []
    refs = data.get("closingIssuesReferences") or []
    return [r["number"] for r in refs if isinstance(r, dict) and "number" in r]


def fetch_issue_labels(repo: str, issue_number: int) -> list[str]:
    """Label names on one issue."""
    data = _gh_json(["gh", "issue", "view", str(issue_number), "--repo", repo, "--json", "labels"])
    if not isinstance(data, dict):
        return []
    labels = data.get("labels") or []
    return [label["name"] for label in labels if isinstance(label, dict) and "name" in label]


def gather_risky_prs(repo: str, pr_numbers: list[int]) -> list[RiskyPR]:
    """For each PR number, resolve the issues it closed and flag it if any carries a risk:* label.

    An issue's labels are looked up once per issue number even if several PRs in the range close
    the same issue — a cheap cache, not a correctness requirement (a repeated `gh issue view` would
    just answer the same way twice).
    """
    label_cache: dict[int, list[str]] = {}
    risky: list[RiskyPR] = []
    for pr_number in pr_numbers:
        issue_numbers = fetch_pr_closing_issues(repo, pr_number)
        if not issue_numbers:
            continue
        issue_labels: dict[int, list[str]] = {}
        for issue_number in issue_numbers:
            if issue_number not in label_cache:
                label_cache[issue_number] = fetch_issue_labels(repo, issue_number)
            issue_labels[issue_number] = label_cache[issue_number]
        hits = risk_labels_for_issues(issue_labels)
        if hits:
            risky.append(RiskyPR(number=pr_number, issue_numbers=tuple(issue_numbers), labels=tuple(hits)))
    return risky


def post_decision_comment(repo: str, pr_number: int, body: str) -> bool:
    """`gh pr comment` the release PR. Best-effort — a failure here never re-enables the deploy."""
    result = _run_gh(
        ["gh", "pr", "comment", str(pr_number), "--repo", repo, "--body-file", "-"], input_text=body
    )
    if result.returncode != 0:
        print(
            f"::warning title=Decision Comment not posted::gh pr comment on #{pr_number} exited "
            f"{result.returncode}: {(result.stderr or '').strip()[:300]}"
        )
        return False
    return True


def write_github_outputs(outputs: dict[str, str]) -> None:
    """Write `key=value` lines to `$GITHUB_OUTPUT`, the wire between this job and `deploy`'s `if:`."""
    path = os.getenv("GITHUB_OUTPUT", "")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as fh:
            for key, value in outputs.items():
                fh.write(f"{key}={value}\n")
    except OSError as exc:
        print(f"::warning title=release-risk-check could not write outputs::{exc}")


# ────────────────────────────────────────────────────────────── CLI


def parse_args(argv: list[str]) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tag", required=True, help="This run's own release tag.")
    ap.add_argument("--repo", default=os.getenv("GITHUB_REPOSITORY", DEFAULT_REPO))
    ap.add_argument("--release-limit", type=int, default=20)
    ap.add_argument("--no-comment", action="store_true", help="Never post the Decision Comment.")
    return ap.parse_args(argv[1:])


def main(argv: list[str]) -> int:
    args = parse_args(argv)

    if not (os.getenv("GH_TOKEN") or os.getenv("GITHUB_TOKEN")):
        print(
            "::warning title=release-risk-check skipped::no GH_TOKEN/GITHUB_TOKEN in scope; cannot "
            "read release history. Deploying unflagged (fail open)."
        )
        write_github_outputs({"flagged": "false"})
        return 0

    releases = fetch_releases(args.repo, args.release_limit)
    if releases is None:
        print(
            f"::warning title=release-risk-check degraded::could not list releases for {args.repo}; "
            "deploying unflagged (fail open)."
        )
        write_github_outputs({"flagged": "false"})
        return 0

    previous_tag = resolve_previous_release(releases, args.tag)
    if previous_tag is None:
        print(
            f"PASS: no earlier release found before {args.tag} within the last {args.release_limit} "
            "— nothing to diff, deploying."
        )
        write_github_outputs({"flagged": "false"})
        return 0

    compare = fetch_compare(args.repo, previous_tag, args.tag)
    if compare is None:
        print(
            f"::warning title=release-risk-check degraded::could not diff {previous_tag}...{args.tag}; "
            "deploying unflagged (fail open)."
        )
        write_github_outputs({"flagged": "false"})
        return 0

    migration_files = added_migration_files(compare.get("files", []))
    commit_messages = [c.get("commit", {}).get("message", "") for c in compare.get("commits", [])]
    pr_numbers = extract_pr_numbers(commit_messages)
    risky_prs = gather_risky_prs(args.repo, pr_numbers)

    verdict = decide(migration_files=migration_files, risky_prs=risky_prs)
    write_github_outputs(
        {"flagged": "true" if verdict.flagged else "false", "previous_tag": previous_tag}
    )

    if not verdict.flagged:
        print(f"PASS: no migration files or risk:*-labelled PRs between {previous_tag} and {args.tag}.")
        return 0

    reason = summarize(verdict)
    print(f"FLAGGED: {reason} between {previous_tag} and {args.tag}.")
    print(
        f"::warning title=Release risk flagged::{reason} — automatic deploy of {args.tag} skipped. "
        f"The owner must run 'gh workflow run deploy-vps.yml -f tag={args.tag}' to ship it manually."
    )

    if args.no_comment:
        return 0

    pr_number = fetch_release_pr_number(args.repo, args.tag)
    body = format_decision_comment(args.tag, previous_tag, verdict)
    if pr_number is None:
        print(
            f"::warning title=Decision Comment not posted::could not resolve the release PR for "
            f"{args.tag}; see the run log above for the same detail."
        )
        return 0
    post_decision_comment(args.repo, pr_number, body)
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI
    raise SystemExit(main(sys.argv))
