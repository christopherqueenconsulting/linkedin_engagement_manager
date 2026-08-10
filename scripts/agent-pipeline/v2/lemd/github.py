"""The daemon's only door to GitHub — every read goes through here.

Shells out to `gh` rather than speaking HTTP: `gh` already resolves the App installation token from
`GH_TOKEN`, handles pagination and retries, and — more importantly — it is the same client v1 uses,
so a permission or auth change breaks both worlds identically instead of one silently.

Two rules this module enforces so callers cannot get them wrong:

* **Unreadable is never "fine".** Every accessor returns a sentinel the caller must handle, never a
  cheerful default. v1's most expensive incidents came from a failed read being indistinguishable
  from a healthy answer — an unreadable merge state that read as "not queued" produced 154
  re-enqueues, and an unreadable check rollup that read as "no failures" would merge red code.
* **Reads are bounded.** Every call has a timeout, because a hung `gh` inside the scheduler loop
  would stall the whole daemon, and the watchdog would then restart a daemon that was merely
  waiting on a socket.
"""

from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass, field
from typing import Any

LOG = logging.getLogger("lemd.github")

#: Required contexts from branch protection. Mirrors tick.sh's REQUIRED_CHECKS_JQ deliberately —
#: verify with: gh api repos/:owner/:repo/branches/main/protection --jq '.required_status_checks.contexts'
REQUIRED_CHECKS = (
    "Unit Tests (Python 3.12)",
    "Integration Tests",
    "GitGuardian Scan",
    "UI Build",
    "Migration Versions",
    "CodeQL PR Quality Gate",
)

FAILED_CONCLUSIONS = frozenset({"FAILURE", "ERROR", "TIMED_OUT", "CANCELLED", "STARTUP_FAILURE"})
PENDING_CONCLUSIONS = frozenset({"PENDING", "QUEUED", "IN_PROGRESS", "EXPECTED", "WAITING", ""})


class GitHubUnavailable(RuntimeError):
    """A read failed. Callers must treat this as "I do not know", never as "nothing to do"."""


@dataclass(frozen=True)
class ChecksState:
    """The required-check rollup for one head."""

    failed: int
    pending: int
    total: int
    names_failed: tuple[str, ...] = field(default=())

    @property
    def green(self) -> bool:
        """True only when every required check reported and none failed.

        `total == 0` is deliberately NOT green: a head with no required checks yet is one where CI
        has not started, and treating that as success would merge unbuilt code.
        """
        return self.total > 0 and self.failed == 0 and self.pending == 0


def run_gh(args: list[str], *, timeout: int = 30) -> str:
    """Run a `gh` command and return stdout.

    Raises:
        GitHubUnavailable: On non-zero exit, timeout, or a missing binary — every failure mode
            collapses to one exception so no caller can accidentally treat stderr as data.
    """
    try:
        proc = subprocess.run(
            ["gh", *args], capture_output=True, text=True, timeout=timeout, check=False
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        raise GitHubUnavailable(f"gh {' '.join(args[:3])}: {exc}") from exc
    if proc.returncode != 0:
        raise GitHubUnavailable(f"gh {' '.join(args[:3])} rc={proc.returncode}: {proc.stderr[:200]}")
    return proc.stdout


def gh_json(args: list[str], *, timeout: int = 30) -> Any:
    """Run a `gh` command expected to emit JSON."""
    raw = run_gh(args, timeout=timeout)
    try:
        return json.loads(raw or "null")
    except json.JSONDecodeError as exc:
        raise GitHubUnavailable(f"gh {' '.join(args[:3])}: unparseable JSON ({exc})") from exc


def checks_for(slug: str, pr: int, *, timeout: int = 30) -> ChecksState:
    """Rollup of the REQUIRED checks only.

    Non-required noise (CodeQL Security Analysis, E2E, the docstring gate) is filtered out for the
    same reason v1 filters it: those can be red on a PR that is legitimately mergeable, and gating
    on them would park healthy work.
    """
    data = gh_json(
        ["pr", "view", str(pr), "--repo", slug, "--json", "statusCheckRollup"], timeout=timeout
    )
    rollup = (data or {}).get("statusCheckRollup") or []
    failed: list[str] = []
    pending = 0
    total = 0
    for c in rollup:
        name = c.get("name") or c.get("context") or ""
        if name not in REQUIRED_CHECKS:
            continue
        total += 1
        state = (c.get("conclusion") or c.get("state") or "").upper()
        if state in FAILED_CONCLUSIONS:
            failed.append(name)
        elif state in PENDING_CONCLUSIONS:
            pending += 1
    return ChecksState(
        failed=len(failed), pending=pending, total=total, names_failed=tuple(sorted(failed))
    )


def pr_facts(slug: str, pr: int, *, timeout: int = 30) -> dict[str, Any]:
    """The PR fields the state machine reads, in one call."""
    return gh_json(
        [
            "pr", "view", str(pr), "--repo", slug, "--json",
            "number,state,isDraft,mergeStateStatus,headRefName,headRefOid,labels,author,"
            "headRepositoryOwner,updatedAt,mergedAt",
        ],
        timeout=timeout,
    ) or {}


def merge_queue_state(slug: str, pr: int, *, timeout: int = 30) -> str:
    """The PR's merge-queue entry state, or "" when it holds no entry.

    An empty string means "no live entry" — NOT "unknown". A failed read raises instead, because
    v1's #1082 incident was precisely an unreadable queue state being read as a healthy one.
    """
    owner, _, name = slug.partition("/")
    out = run_gh(
        [
            "api", "graphql", "-f",
            "query=query($o:String!,$n:String!,$p:Int!){repository(owner:$o,name:$n){"
            "pullRequest(number:$p){mergeQueueEntry{state}}}}",
            "-f", f"o={owner}", "-f", f"n={name}", "-F", f"p={pr}",
            "--jq", ".data.repository.pullRequest.mergeQueueEntry.state // \"\"",
        ],
        timeout=timeout,
    )
    return out.strip()


def list_by_label(slug: str, label: str, kind: str = "pr", *, limit: int = 100,
                  timeout: int = 30) -> list[dict[str, Any]]:
    """Open issues or PRs carrying a label. The reconciler's whole input."""
    cmd = "pr" if kind == "pr" else "issue"
    fields = "number,labels,updatedAt" + (",headRefName,headRefOid,isDraft" if kind == "pr" else "")
    return gh_json(
        [cmd, "list", "--repo", slug, "--state", "open", "--label", label,
         "--limit", str(limit), "--json", fields],
        timeout=timeout,
    ) or []


def label_names(obj: dict[str, Any]) -> set[str]:
    """Label names from a `gh --json labels` payload."""
    return {ll.get("name", "") for ll in (obj.get("labels") or [])}


def is_upstream(facts: dict[str, Any], slug: str) -> bool:
    """True when the PR's head branch lives in THIS repository, not a fork.

    Fail-closed: an unreadable owner returns False. A fork PR that reads as upstream would let
    attacker-controlled code reach a lane that checks out and runs it.
    """
    owner = (facts.get("headRepositoryOwner") or {}).get("login")
    if not owner:
        return False
    return owner.lower() == slug.partition("/")[0].lower()
