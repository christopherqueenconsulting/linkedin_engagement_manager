#!/usr/bin/env python3
"""Decide whether a PR that touches an owned path may merge without the owner.

This gate IS the enforced owner review on `main` (owner ruling, 2026-11-12). Branch protection's
`require_code_owner_reviews` is switched off once this gate is a required check, because GitHub's
version cannot tell the two cases apart that matter here. It demands the owner's approval from every
author, Dependabot included, and the owner cannot approve their own PR. This gate exempts the owner,
gives Dependabot narrow, diff-checked allowances, and holds everyone else to an owner approval.

Scope is `.github/CODEOWNERS`, read from the BASE branch (the workflow checks the base out, so a PR
cannot narrow the scope or rewrite this script to approve itself), plus `PROTECTED` below. An
unreadable CODEOWNERS is a refusal: a gate that cannot say what it protects does not get to pass.

The threat this gate defends against is a non-owner identity, above all the pipeline's own GitHub
App (`cqc-lem-agent-pipeline[bot]`, #1251), merging a change to a control surface unattended. Once
the deploy is automated (#1398) that closes the loop: an agent could change
`scripts/agent-pipeline/**`, merge it, and have it running with the owner's credentials minutes
later. The owner acting under their own name is not that threat. They already hold every credential,
and a single-human-maintainer repo has no second reviewer to ask for (#1692). Revisit that exemption
the day a second human maintainer joins (`docs/contribution-security.md`).

The verdict lives here rather than inline in YAML so it can be tested. Stdlib only, because CI runs
it directly.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

#: Always protected, whatever CODEOWNERS says. The gate's own code and tests are here so a PR cannot
#: loosen the gate by first removing them from CODEOWNERS.
PROTECTED = (
    "scripts/agent-pipeline/*",
    ".github/workflows/*",
    ".github/CODEOWNERS",
    "scripts/pipeline_selfmod_gate.py",
    "tests/unit/test_pipeline_selfmod_gate.py",
)

#: The CODEOWNERS this script ships beside. The workflow checks out the BASE commit, so this is the
#: base branch's copy, never the PR's.
CODEOWNERS = Path(__file__).resolve().parents[1] / ".github" / "CODEOWNERS"

#: The one non-human author with an allowance. Exact login, as the workflow reads it from
#: `github.event.pull_request.user.login`.
DEPENDABOT = "dependabot[bot]"

#: Where the `uses:` allowance applies.
_WORKFLOWS = ".github/workflows/"

#: One `uses: <action>@<ref>` line, optionally a list item, optionally quoted, optionally followed by
#: the `# vX.Y.Z` comment Dependabot keeps next to a SHA pin. The prefix (indentation and `- `) is
#: captured so a bump that re-indents or restructures the YAML does not pass as a ref change.
_USES = re.compile(
    r"""^(?P<prefix>\s*(?:-\s+)?)uses:\s*(?P<q>["']?)(?P<action>[A-Za-z0-9][A-Za-z0-9._/-]*)"""
    r"""@(?P<ref>[A-Za-z0-9._/-]+)(?P=q)(?:\s+#[^\n]*)?\s*$"""
)

#: A Docker image reference with a literal tag, digest, or both. A registry port (`host:5000/`)
#: only counts when a `/` follows it, so `python:3` reads as image `python`, tag `3`. No `${…}`
#: interpolation can match, so a bump that swaps a literal for a variable is refused.
_IMAGE_REF = (
    r"(?P<image>[a-z0-9][a-z0-9._-]*(?::[0-9]+(?=/))?(?:/[a-z0-9][a-z0-9._-]*)*)"  # preflight: ok — `/` delimits each repeat, so no backtracking blow-up
    r"(?::(?P<tag>[A-Za-z0-9_][A-Za-z0-9._-]{0,127}))?(?:@(?P<digest>sha256:[0-9a-f]{64}))?"
)

#: A Dockerfile `FROM <image>[:tag][@sha256:…] [AS stage]` line.
_FROM = re.compile(
    r"^(?P<prefix>\s*(?i:FROM)\s+(?:--platform=\S+\s+)?)" + _IMAGE_REF
    + r"(?P<suffix>\s+(?i:AS)\s+[A-Za-z0-9._-]+)?\s*$"
)

#: A compose `image: <image>[:tag][@sha256:…]` line, optionally quoted and commented.
_COMPOSE_IMAGE = re.compile(
    r"""^(?P<prefix>\s*(?:-\s+)?)image:\s*(?P<q>["']?)""" + _IMAGE_REF
    + r"""(?P=q)(?P<suffix>)(?:\s+#[^\n]*)?\s*$"""
)

#: Per-file header lines a plain in-place edit carries. A rename, a new or deleted file, a mode
#: change or a binary patch has others, and is refused.
_HEADER_OK = ("index ", "--- a/", "+++ b/")


class CodeownersUnreadable(Exception):
    """CODEOWNERS is missing, unreadable, or lists no path: the gate's scope is unknown."""


@dataclass(frozen=True)
class Verdict:
    """The gate's answer, and the sentence explaining it."""

    allowed: bool
    reason: str


def load_codeowners(path: Path | None = None) -> list[str]:
    """Read the path patterns a CODEOWNERS file lists, owners or not.

    Args:
        path: The CODEOWNERS file. `None` is `CODEOWNERS`, the base checkout's own copy.

    Returns:
        Every rule's pattern, in file order. A rule with no owner still counts: protecting more than
        GitHub would is the safe direction for a gate.

    Raises:
        CodeownersUnreadable: The file is missing, unreadable, or lists no pattern.
    """
    path = CODEOWNERS if path is None else path
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise CodeownersUnreadable(f"cannot read {path}: {exc}") from exc
    patterns = []
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            patterns.append(line.split()[0])
    if not patterns:
        raise CodeownersUnreadable(f"{path} lists no path")
    return patterns


def codeowners_regex(pattern: str) -> re.Pattern[str]:
    """Compile one CODEOWNERS pattern with gitignore semantics, matched against a file path.

    A leading `/`, or a `/` anywhere but the end, anchors the pattern at the repository root;
    otherwise it matches at any depth. A trailing `/` matches only what is inside that directory.
    Without one, the pattern matches the path itself or anything under it. `*` and `?` stay inside
    one path segment, and `**` crosses segments.
    """
    dir_only = pattern.endswith("/")
    body = pattern.strip("/")
    anchored = pattern.startswith("/") or "/" in body
    out = []
    i = 0
    while i < len(body):
        if body.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif body.startswith("**", i):
            out.append(".*")
            i += 2
        elif body[i] == "*":
            out.append("[^/]*")
            i += 1
        elif body[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(body[i]))
            i += 1
    prefix = "" if anchored else "(?:.*/)?"
    suffix = "/.*" if dir_only else "(?:/.*)?"
    return re.compile(prefix + "".join(out) + suffix)


def touches_protected(paths: list[str], codeowners: list[str] | None = None) -> list[str]:
    """Which of these changed files are owned: in CODEOWNERS or in `PROTECTED`.

    Args:
        paths: Changed file paths, relative to the repository root.
        codeowners: CODEOWNERS patterns. `None` reads the base branch's file.

    Returns:
        The owned paths, in input order.

    Raises:
        CodeownersUnreadable: `codeowners` is `None` and the file cannot be read.
    """
    if codeowners is None:
        codeowners = load_codeowners()
    owned = [codeowners_regex(p) for p in codeowners]
    hits = []
    for path in paths:
        if any(rx.fullmatch(path) for rx in owned) or any(
            fnmatch.fnmatch(path, p) or path.startswith(p.rstrip("*")) for p in PROTECTED
        ):
            hits.append(path)
    return hits


def diff_paths(diff: str | None) -> list[str]:
    """Every old and new path a unified diff names, so a rename's source is judged too."""
    paths: list[str] = []
    for line in (diff or "").splitlines():
        m = re.fullmatch(r"diff --git a/(\S+) b/(\S+)", line)
        if m:
            paths += [m.group(1), m.group(2)]
        elif line.startswith(("rename from ", "rename to ", "copy from ", "copy to ")):
            paths.append(line.split(" ", 2)[2])
    return list(dict.fromkeys(paths))


def _is_dockerfile(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    return name == "Dockerfile" or name.startswith("Dockerfile.") or name.endswith(".Dockerfile")


def _is_compose(path: str) -> bool:
    return fnmatch.fnmatch(path.rsplit("/", 1)[-1], "docker-compose*.y*ml")


def _bump_rule(path: str) -> re.Pattern[str] | None:
    """Which line shape a Dependabot bump may change in this file, if any."""
    if path.startswith(_WORKFLOWS):
        return _USES
    if _is_dockerfile(path):
        return _FROM
    if _is_compose(path):
        return _COMPOSE_IMAGE
    return None


def _identity(m: re.Match[str]) -> tuple[str | None, ...]:
    """What a bump must leave alone: the shape around the reference and the thing referenced."""
    if m.re is _USES:
        return (m["prefix"], m["action"])
    return (m["prefix"], m["image"], m["suffix"] or "")


def _version(m: re.Match[str]) -> tuple[str | None, ...]:
    """What a bump may move: the action ref, or the image tag and digest."""
    if m.re is _USES:
        return (m["ref"],)
    return (m["tag"], m["digest"])


def dependabot_bump(changed: list[str], diff: str | None) -> bool:
    """Does this diff ONLY move version references Dependabot owns?

    Allowed lines: `uses: <action>@<ref>` in `.github/workflows/`, `FROM <image>:<tag>` or
    `FROM <image>@sha256:…` in a Dockerfile, and `image: <name>:<tag>` in a `docker-compose*.yml`.

    Fails closed: no diff, a changed file of any other kind, a diff naming a file `changed` does not
    (or the reverse), a rename/new/deleted/mode/binary change, any changed line not of its file's
    allowed shape, or removed and added lines that do not pair up one-for-one on the SAME action or
    image with the same surrounding shape. So a bump may move a version, never swap what is
    referenced or add a line.
    """
    if not diff or not changed or not all(_bump_rule(p) for p in changed):
        return False
    removed: dict[str, list[re.Match[str]]] = {}
    added: dict[str, list[re.Match[str]]] = {}
    current: str | None = None
    rule: re.Pattern[str] | None = None
    in_hunk = False
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            m = re.fullmatch(r"diff --git a/(\S+) b/(\S+)", line)
            if not m or m.group(1) != m.group(2):
                return False
            current, rule, in_hunk = m.group(1), _bump_rule(m.group(1)), False
            if rule is None:
                return False
            removed.setdefault(current, [])
            added.setdefault(current, [])
            continue
        if current is None or rule is None:
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
        m = rule.match(line[1:])
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
            if _identity(o) != _identity(n) or _version(o) == _version(n):
                return False
        total += len(new)
    return total > 0


def _unreadable(exc: CodeownersUnreadable) -> Verdict:
    """Refuse every author: with no scope, the gate cannot tell an owned path from any other."""
    return Verdict(False, f"cannot tell which paths are owned ({exc}); restore .github/CODEOWNERS")


def decide(*, changed: list[str], author: str, approvers: list[str], owner: str,
           diff: str | None = None, codeowners: list[str] | None = None) -> Verdict:
    """Should this PR be allowed to merge?

    Args:
        changed: Paths the PR modifies. Paths the diff names are added, so a rename's source counts.
        author: The PR author's login.
        approvers: Logins that have submitted an APPROVED review.
        owner: The login whose approval counts, and the ONLY one. No other trusted login (for
            example `LEMD_TRUSTED_ANSWER_LOGINS`, which answers park questions) counts here.
        diff: The PR's unified diff. Used for rename sources and the Dependabot allowances.
        codeowners: CODEOWNERS patterns. `None` reads the base branch's file.

    Returns:
        A `Verdict`. Anything owning no path is allowed untouched, because a gate that fires on
        everything gets routed around. An unreadable CODEOWNERS is a refusal.
    """
    paths = list(dict.fromkeys([*changed, *diff_paths(diff)]))
    try:
        hits = touches_protected(paths, codeowners)
    except CodeownersUnreadable as exc:
        return _unreadable(exc)
    if not hits:
        return Verdict(True, "touches no owned path")

    # The owner is exempt outright: they already hold every credential and control surface a merge
    # could reach, and there is no second human to ask for a review. The gate defends against a
    # NON-owner identity (above all the pipeline's own App) merging an owned change unattended.
    if author == owner:
        return Verdict(
            True,
            f"{author} is the repo owner — sole human maintainer, no second reviewer to require",
        )

    if owner in approvers:
        return Verdict(True, f"approved by {owner}")

    # Dependabot: an action ref or image tag moving and nothing else. What is referenced stays the
    # same, so this changes WHICH release runs, never what the workflow or stack does or which
    # secrets it reaches. The Dependabot PR's own release notes are the review the owner would give.
    if author == DEPENDABOT and dependabot_bump(changed, diff):
        return Verdict(True, "Dependabot bump that only moves `uses:` / `FROM` / `image:` versions")

    return Verdict(
        False,
        f"changes {len(hits)} owned path(s) ({', '.join(sorted(hits)[:3])}"
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
                    help="the PR's unified diff, or a path to a file holding it (rename sources "
                         "and Dependabot allowances; the workflow passes a path because a diff can "
                         "exceed the argument-length limit)")
    ap.add_argument("--codeowners", default=str(CODEOWNERS),
                    help="the CODEOWNERS file that sets the gate's scope (default: the base "
                         "checkout's own .github/CODEOWNERS)")
    args = ap.parse_args(argv[1:])
    diff = args.diff
    if diff and "\n" not in diff and os.path.isfile(diff):
        with open(diff, encoding="utf-8") as fh:
            diff = fh.read()
    try:
        codeowners = load_codeowners(Path(args.codeowners))
    except CodeownersUnreadable as exc:
        verdict = _unreadable(exc)
    else:
        verdict = decide(
            changed=json.loads(args.changed),
            author=args.author,
            approvers=json.loads(args.approvers),
            owner=args.owner,
            diff=diff,
            codeowners=codeowners,
        )
    # Written to stdout either way: a refusal that does not say what it wants is a refusal people
    # work around instead of satisfying.
    print(f"{'PASS' if verdict.allowed else 'REFUSED'}: {verdict.reason}")
    if not verdict.allowed:
        print(
            "::error title=Pipeline self-modification::"
            f"{verdict.reason}. This PR changes a path the owner must approve."
        )
    return 0 if verdict.allowed else 1


if __name__ == "__main__":  # pragma: no cover - CLI
    raise SystemExit(main(sys.argv))
