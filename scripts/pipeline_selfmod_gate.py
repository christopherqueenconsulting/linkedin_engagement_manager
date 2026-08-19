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


def decide(*, changed: list[str], author: str, approvers: list[str], owner: str) -> Verdict:
    """Should this PR be allowed to merge?

    Args:
        changed: Paths the PR modifies.
        author: The PR author's login.
        approvers: Logins that have submitted an APPROVED review.
        owner: The login whose approval counts.

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
    args = ap.parse_args(argv[1:])

    verdict = decide(
        changed=json.loads(args.changed),
        author=args.author,
        approvers=json.loads(args.approvers),
        owner=args.owner,
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
