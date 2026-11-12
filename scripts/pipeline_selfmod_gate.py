#!/usr/bin/env python3
"""Decide whether a PR that modifies the pipeline may merge without a human.

The agent pipeline merges its own PRs. `required_approving_review_count` is **0** on `main`, and
`.github/CODEOWNERS` enforces nothing at that setting (`docs/codeowners-enforcement-limit`). That is
tolerable while a merged change can only alter the *application* — the pipeline's own trust boundary
still needs a human to reach the box.

Automating the deploy (#1398) removes that last step. Then the chain closes: an agent could write a
change to `scripts/agent-pipeline/**` or `.github/workflows/**`, merge it unattended, and have it
running with the owner's credentials minutes later. Every other control in
`docs/contribution-security.md` is downstream of code the agent would by then control.

One thing changed in our favour and makes this expressible at all: since #1251 the pipeline
authenticates as its own GitHub App (`cqc-lem-agent-pipeline[bot]`), distinct from the owner. Before
that, "the author must not be the approver" was unenforceable because every PR was authored by the
owner.

The threat this gate defends against is the pipeline's OWN identity — `author != owner` — merging a
self-modifying change unattended. It is not the owner acting under their own name: the owner already
holds every credential and control surface this repo has, and in a single-human-maintainer repo there
is no second reviewer to demand one from. So an owner-authored change to a protected path is exempt
outright (#1692's decision comment); only the bot's own PRs are gated on an owner approval. Revisit
this exemption the day a second human maintainer joins — see `docs/contribution-security.md`.

The verdict lives here rather than inline in YAML so it can be tested. Stdlib only — CI runs it
directly.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import sys
from dataclasses import dataclass

#: Paths whose contents decide what the pipeline is allowed to do. A change to any of them is a
#: change to the machine that would deploy it, so it does not get to merge itself.
#:
#: `.github/workflows/**` is here for the same reason and one more: those workflows hold the
#: credentials, so a rewrite there reaches every secret the repository has.
PROTECTED = (
    "scripts/agent-pipeline/*",
    ".github/workflows/*",
    ".github/CODEOWNERS",
)


#: The one non-human author with an allowance (`action_ref_bump`). Exact login, as the workflow
#: reads it from `github.event.pull_request.user.login`.
DEPENDABOT = "dependabot[bot]"

#: Where the allowance applies. Nothing else Dependabot touches is protected anyway.
_WORKFLOWS = ".github/workflows/"

#: One `uses: <action>@<ref>` line, optionally a list item, optionally quoted, optionally followed by
#: the `# vX.Y.Z` comment Dependabot keeps next to a SHA pin. The prefix (indentation and `- `) is
#: captured so a bump that re-indents or restructures the YAML does not pass as a ref change.
_USES = re.compile(
    r"""^(?P<prefix>\s*(?:-\s+)?)uses:\s*(?P<q>["']?)(?P<action>[A-Za-z0-9][A-Za-z0-9._/-]*)"""
    r"""@(?P<ref>[A-Za-z0-9._/-]+)(?P=q)(?:\s+#[^\n]*)?\s*$"""
)

#: Per-file header lines a plain in-place edit carries. A rename, a new or deleted file, a mode
#: change or a binary patch has others, and is refused.
_HEADER_OK = ("index ", "--- a/", "+++ b/")


@dataclass(frozen=True)
class Verdict:
    """The gate's answer, and the sentence explaining it."""

    allowed: bool
    reason: str


def touches_protected(paths: list[str]) -> list[str]:
    """Which of these changed files are inside a protected path."""
    hits = []
    for path in paths:
        for pattern in PROTECTED:
            if fnmatch.fnmatch(path, pattern) or path.startswith(pattern.rstrip("*")):
                hits.append(path)
                break
    return hits


def action_ref_bump(changed: list[str], diff: str | None) -> bool:
    """Is this diff ONLY `uses: <action>@<ref>` changes inside `.github/workflows/`?

    Fails closed: no diff, a file outside `.github/workflows/`, a diff naming a file `changed` does
    not (or the reverse), a rename/new/deleted/mode/binary change, any changed line that is not a
    `uses:` line, or a file whose removed and added `uses:` lines do not pair up one-for-one on the
    SAME action and the same indentation — all refused. So a bump may move a ref, never swap the
    action for another one or add a step.
    """
    if not diff or not changed or not all(p.startswith(_WORKFLOWS) for p in changed):
        return False
    removed: dict[str, list[re.Match[str]]] = {}
    added: dict[str, list[re.Match[str]]] = {}
    current: str | None = None
    in_hunk = False
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            m = re.fullmatch(r"diff --git a/(\S+) b/(\S+)", line)
            if not m or m.group(1) != m.group(2) or not m.group(1).startswith(_WORKFLOWS):
                return False
            current, in_hunk = m.group(1), False
            removed.setdefault(current, [])
            added.setdefault(current, [])
            continue
        if current is None:
            return False
        if line.startswith("@@ "):
            in_hunk = True
            continue
        if not in_hunk:
            if line.startswith(_HEADER_OK):
                continue
            return False
        if line.startswith((" ", "\\")) or line == "":
            continue
        if line[0] not in "+-":
            return False
        m = _USES.match(line[1:])
        if not m:
            return False
        (added if line[0] == "+" else removed)[current].append(m)
    if set(removed) != set(changed):
        return False
    total = 0
    for path in changed:
        old, new = removed[path], added[path]
        if len(old) != len(new):
            return False
        for o, n in zip(old, new, strict=True):
            if (o["prefix"], o["action"]) != (n["prefix"], n["action"]) or o["ref"] == n["ref"]:
                return False
        total += len(new)
    return total > 0


def decide(*, changed: list[str], author: str, approvers: list[str], owner: str,
           diff: str | None = None) -> Verdict:
    """Should this PR be allowed to merge?

    Args:
        changed: Paths the PR modifies.
        author: The PR author's login.
        approvers: Logins that have submitted an APPROVED review.
        owner: The login whose approval counts — the ONLY one. No other trusted login (for example
            `LEMD_TRUSTED_ANSWER_LOGINS`, which answers park questions) counts here.
        diff: The PR's unified diff, used only for the Dependabot action-ref allowance.

    Returns:
        A `Verdict`. Anything not touching a protected path is allowed untouched — this gate is
        deliberately narrow, because a gate that fires on everything gets routed around.
    """
    hits = touches_protected(changed)
    if not hits:
        return Verdict(True, "touches no protected path")

    # Owner-authored changes are exempt outright: the owner already holds every credential and
    # control surface a merge could reach, and a single-human-maintainer repo has no second person
    # to demand a review from. What this gate actually defends against is the PIPELINE'S OWN
    # identity (author != owner, i.e. the bot) merging a self-modifying change unattended — that
    # path is still fully gated below.
    if author == owner:
        return Verdict(
            True,
            f"{author} is the repo owner — sole human maintainer, no second reviewer to require",
        )

    if owner in approvers:
        return Verdict(True, f"approved by {owner}")

    # Dependabot's GitHub Actions bumps: a ref moving on a `uses:` line, nothing else. The action
    # stays the same one, so this changes WHICH release of a step runs, never what the workflow
    # does or which secrets it reaches — the review the owner would give is the one the
    # Dependabot PR's own release notes already are.
    if author == DEPENDABOT and action_ref_bump(changed, diff):
        return Verdict(True, "Dependabot bump that only moves `uses:` action refs in workflows")

    return Verdict(
        False,
        f"changes {len(hits)} protected path(s) ({', '.join(sorted(hits)[:3])}"
        f"{'…' if len(hits) > 3 else ''}) and has no approving review from {owner}",
    )


def main(argv: list[str]) -> int:
    """CLI: read the PR facts as JSON and exit non-zero when the gate refuses."""
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--changed", required=True, help="JSON array of changed paths")
    ap.add_argument("--author", required=True)
    ap.add_argument("--approvers", required=True, help="JSON array of approving logins")
    ap.add_argument("--owner", default="gitchrisqueen")
    ap.add_argument("--diff", default=None,
                    help="the PR's unified diff, or a path to a file holding it (only read for "
                         "Dependabot action-ref bumps; the workflow passes a path because a diff can "
                         "exceed the argument-length limit)")
    args = ap.parse_args(argv[1:])
    diff = args.diff
    if diff and "\n" not in diff and os.path.isfile(diff):
        with open(diff, encoding="utf-8") as fh:
            diff = fh.read()

    verdict = decide(
        changed=json.loads(args.changed),
        author=args.author,
        approvers=json.loads(args.approvers),
        owner=args.owner,
        diff=diff,
    )
    # Written to stdout either way: a refusal that does not say what it wants is a refusal people
    # work around instead of satisfying.
    print(f"{'PASS' if verdict.allowed else 'REFUSED'}: {verdict.reason}")
    if not verdict.allowed:
        print(
            "::error title=Pipeline self-modification::"
            f"{verdict.reason}. This PR changes the machinery that would deploy it."
        )
    return 0 if verdict.allowed else 1


if __name__ == "__main__":  # pragma: no cover - CLI
    raise SystemExit(main(sys.argv))
