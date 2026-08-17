"""Does a pull request touch a path `.github/CODEOWNERS` assigns an owner to?

GitHub will not answer this. With `required_approving_review_count: 0` and
`require_code_owner_reviews: true`, a code-owner-gated PR reports `reviewDecision: null` and an
EMPTY `reviewRequests` list while still sitting `BLOCKED` — measured on #1616/#1618/#1620 on
2026-08-17, all three authored by the pipeline's App identity. So the fact that a human approval is
owed exists only inside GitHub's merge gate, and the pipeline has to derive it to be able to ask for
that approval (#1642).

The matcher below implements the documented SUBSET of gitignore syntax this repository's CODEOWNERS
actually uses — anchored directory prefixes (`/scripts/agent-pipeline/`) and anchored file paths
(`/src/cqc_lem/api/main.py`) — plus the wildcards (`*`, `?`, `**`) a future rule might use. It is
deliberately not a complete gitignore engine.

Being wrong here is cheap in exactly one direction, and the daemon depends on that asymmetry: a MISS
only delays the reviewer request until the PR reaches `awaiting_owner_review`, which is GitHub's own
verdict that a code-owner review is the last gate, and the daemon asks again there. A false POSITIVE
sends the owner a review request they did not need. So an unreadable or unparseable CODEOWNERS
yields NO rules and therefore no match — the authoritative fallback still fires.
"""

from __future__ import annotations

import base64
import logging
import re
import time
from typing import Iterable

from . import github

LOG = logging.getLogger("lemd.codeowners")

#: The three locations GitHub reads CODEOWNERS from, in its own precedence order.
CODEOWNERS_LOCATIONS = (".github/CODEOWNERS", "CODEOWNERS", "docs/CODEOWNERS")

#: How long a fetched rule set is reused. CODEOWNERS changes are rare and every change to it is
#: itself an owned path, so a stale hour costs at most one delayed reviewer request; re-reading it
#: per observation would put an API call on the hot path of a scheduler whose whole design is that a
#: waiting item costs nothing.
CACHE_TTL_SECONDS = 3600

#: `(regex, owners)` in FILE ORDER. Order is the semantics: the LAST matching rule wins, which is
#: how a broad `/​.github/` rule can be narrowed (or un-owned) by a later line.
Rules = tuple[tuple[re.Pattern[str], tuple[str, ...]], ...]

_cache: dict[str, tuple[float, Rules]] = {}


def _translate(body: str) -> str:
    """Translate the glob body of a CODEOWNERS pattern to a regex fragment.

    `fnmatch.translate` is not usable here: its `*` crosses `/`, which would make `/scripts/*.sh`
    match `scripts/agent-pipeline/v2/x.sh`.
    """
    out: list[str] = []
    i = 0
    while i < len(body):
        if body.startswith("**/", i):
            # Any number of leading directories, including none.
            out.append("(?:[^/]+/)*")
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
    return "".join(out)


def _compile(pattern: str) -> re.Pattern[str] | None:
    """Compile one CODEOWNERS pattern into a regex matched against a repo-relative path.

    Returns:
        The compiled pattern, or None when it is empty after normalisation.
    """
    anchored = pattern.startswith("/")
    is_dir = pattern.endswith("/")
    body = pattern.strip("/")
    if not body:
        return None
    prefix = "^" if anchored else "^(?:.*/)?"
    # A directory pattern matches only what is INSIDE it. A file pattern matches the file itself —
    # and also everything under it, because a bare name with no trailing slash may still be a
    # directory (`/.litellm/` and `/.litellm` mean the same thing to a reader).
    suffix = "/.*$" if is_dir else "(?:/.*)?$"
    try:
        return re.compile(prefix + _translate(body) + suffix)
    except re.error as exc:
        LOG.warning("unusable CODEOWNERS pattern %r: %s", pattern, exc)
        return None


def parse(text: str) -> Rules:
    """Parse CODEOWNERS text into compiled rules, in file order.

    A line whose pattern parses but which names NO owner is kept with an empty owner tuple: that is
    how CODEOWNERS un-owns a path a broader earlier rule claimed, and dropping it would leave the
    broader rule winning.

    Args:
        text: The raw file contents.

    Returns:
        `(regex, owners)` pairs in the order they appear.
    """
    rules: list[tuple[re.Pattern[str], tuple[str, ...]]] = []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        rx = _compile(parts[0])
        if rx is None:
            continue
        rules.append((rx, tuple(parts[1:])))
    return tuple(rules)


def owners_for(path: str, rules: Rules) -> tuple[str, ...]:
    """The owners of one repo-relative path. LAST matching rule wins, as GitHub does it."""
    owners: tuple[str, ...] = ()
    for rx, who in rules:
        if rx.match(path):
            owners = who
    return owners


def matches_any(paths: Iterable[str], rules: Rules) -> bool:
    """Does any of these paths have an owner?"""
    return any(owners_for(p, rules) for p in paths if p)


def rules_for(slug: str, *, now: float | None = None) -> Rules:
    """Fetch (and cache) the repo's CODEOWNERS rules from its default branch.

    Read from GitHub rather than from the daemon's working checkout: the checkout may sit on any
    branch, while the rules that gate a merge are the ones on the base branch.

    Args:
        slug: `owner/repo`.
        now: Injectable clock, for tests.

    Returns:
        The parsed rules, or an empty tuple when no CODEOWNERS file could be read — which is the
        fail-safe direction (see the module docstring).
    """
    stamp = time.time() if now is None else now
    hit = _cache.get(slug)
    if hit and stamp - hit[0] < CACHE_TTL_SECONDS:
        return hit[1]
    rules: Rules = ()
    for location in CODEOWNERS_LOCATIONS:
        try:
            payload = github.gh_json(["api", f"repos/{slug}/contents/{location}"]) or {}
        except github.GitHubUnavailable:
            # 404 for the two locations this repo does not use is the normal case, so this is DEBUG,
            # not a warning: "once is a warning, repeatedly is a defect" applies to the daemon's log
            # too, and this would fire twice an hour forever.
            LOG.debug("no CODEOWNERS at %s in %s", location, slug)
            continue
        try:
            text = base64.b64decode(payload.get("content") or "").decode("utf-8", "replace")
        except (ValueError, TypeError) as exc:
            LOG.warning("CODEOWNERS at %s is undecodable: %s", location, exc)
            continue
        rules = parse(text)
        break
    _cache[slug] = (stamp, rules)
    return rules


def clear_cache() -> None:
    """Drop the cached rule sets. For tests, and for a daemon reload."""
    _cache.clear()
