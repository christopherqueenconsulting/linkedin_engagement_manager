#!/usr/bin/env python3
"""CodeQL per-PR gate: compare head vs base alerts, auto-fix quality issues.

This is the prevention half of LEM's CodeQL cleanup: the existing sweep PR
(#781) fixes the 97 open alerts; this script makes sure no new mechanical
quality alerts reach main.

It runs in a GitHub Actions workflow
(`.github/workflows/codeql-pr-gate.yml`) and:

1. Waits for the existing CodeQL workflows to upload SARIF *for the commit being
   gated* — not merely for the ref (see `wait_for_analysis`).
2. Fetches open code-scanning alerts for the head and base refs.
3. Computes newly introduced alerts by (rule, file, line, message) — plus the
   repo-global alert number, so an alert whose line merely moved is not new
   (`compare_alerts`, #1087).
4. Buckets them:
   - Security-severity alerts: always blocking, never auto-fixed.
   - Mechanical quality alerts: auto-fixed where safe (unused imports /
     variables, repeated imports, implicit string concatenation in lists,
     empty except blocks).
   - Judgment alerts: posted to the PR for human review (false positives need
     an `# lgtm[py/...]` suppression comment).
5. Commits safe fixes back to the PR branch and reports the rest.

Env:
  GITHUB_TOKEN  Token with `security-events:read`, `contents:write`,
                `pull-requests:write`.
  GITHUB_OUTPUT  GitHub Actions output file (optional).

CLI:
  --repo OWNER/REPO
  --head-ref REF         Git REF the code-scanning API resolves (e.g. refs/pull/<n>/merge).
                         NOT a commit SHA — the API returns zero analyses for a bare SHA.
  --head-sha SHA         Commit SHA that --head-ref resolves to (for a PR that is the
                         MERGE commit, i.e. GITHUB_SHA — not the PR head). Without it the
                         wait degrades to "any analysis for the ref", which is the #904 bug.
  --base-ref REF         Git REF to diff against (e.g. refs/heads/main).
  --pr-number NUMBER     PR to post comments to (optional).
  --branch BRANCH        Branch to push fixes to (optional; no push if
                         omitted).
  --fix                  Apply mechanical auto-fixes.
  --wait-timeout SEC     Max seconds to wait for CodeQL (default 300).
  --wait-interval SEC    Seconds between polls (default 30).
  --ruff-cmd CMD         Ruff command (default `ruff`).

Exit:
  0 = gate passed (no actionable new alerts, or fail-open).
  1 = new security or judgment alerts remain, or fixes could not be pushed.
"""
from __future__ import annotations

import argparse
import logging
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Optional

import requests

from cqc_lem.utilities.logger import log_error, log_info, log_warning

logger = logging.getLogger(__name__)

# Mechanical CodeQL quality rules we will try to auto-fix.
# Security-severity alerts are handled separately and always block.
AUTO_FIXABLE_RULES = {
    "py/unused-import",
    "py/unused-global-variable",
    "py/unused-local-variable",
    "py/repeated-import",
    "py/multiple-definition",
    "py/implicit-string-concatenation-in-list",
    "py/empty-except",
}

# CodeQL quality rules that need a human eye (false positives get an
# `lgtm[...]` comment).
JUDGMENT_RULES = {
    "py/ineffectual-statement",
    "py/bad-tag-filter",
}

GITHUB_API = "https://api.github.com"


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="CodeQL per-PR quality gate.")
    parser.add_argument("--repo", required=True, help="Owner/repo slug.")
    parser.add_argument("--head-ref", required=True,
                        help="Git REF for code-scanning (refs/pull/<n>/merge), NOT a SHA.")
    parser.add_argument("--head-sha", default="",
                        help="Commit SHA --head-ref resolves to (GITHUB_SHA on a PR).")
    parser.add_argument("--base-ref", required=True,
                        help="Git REF to diff against (refs/heads/main), NOT a SHA.")
    parser.add_argument(
        "--pr-number", type=int, default=0, help="PR number for comments."
    )
    parser.add_argument(
        "--branch", default="", help="Branch to push fixes to."
    )
    parser.add_argument(
        "--fix", action="store_true", help="Apply mechanical auto-fixes."
    )
    parser.add_argument(
        "--wait-timeout",
        type=int,
        # Before #904 the wait returned instantly on any push after the first, so 300s was
        # never actually spent. Now that it waits for THIS commit it has to outlast a real
        # CodeQL run (~2-4 min) plus queue time, or the gate just fails open every time.
        default=900,
        help="Seconds to wait for CodeQL.",
    )
    parser.add_argument(
        "--wait-interval", type=int, default=30, help="Polling interval."
    )
    parser.add_argument(
        "--required-categories",
        default="",
        # Escape hatch for CHANGING the set of categories the repo produces. Normally the wait
        # self-calibrates off recent commits (`expected_categories`), which is right while the set
        # is stable and a trap the moment it shrinks: the calibration takes the LARGEST set among
        # the newest 3 commits, so a removed category keeps being demanded until three commits have
        # passed without it. Until then every run waits the full --wait-timeout and then fails
        # open — a vacuous required gate, which is exactly what #1168/#1171 cost.
        #
        # Pinning the expected set here overrides the calibration, so a category can be added or
        # removed in ONE pull request: this gate checks out the PR's own ref, so the PR that
        # changes the workflows carries the matching value.
        help="Comma-separated CodeQL categories to require, overriding self-calibration.",
    )
    parser.add_argument("--ruff-cmd", default="ruff", help="Ruff executable.")
    return parser.parse_args(argv)


@dataclass
class Alert:
    """Flattened CodeQL alert for comparison and reporting."""

    number: int
    rule_id: str
    severity: Optional[str]
    security_severity: Optional[str]
    path: str
    start_line: int
    end_line: int
    message: str
    html_url: str
    raw: dict = field(repr=False)

    @property
    def is_security(self) -> bool:
        return bool(self.security_severity)

    @property
    def is_mechanical(self) -> bool:
        return self.rule_id in AUTO_FIXABLE_RULES and not self.is_security

    @property
    def is_judgment(self) -> bool:
        return not self.is_security and not self.is_mechanical

    @property
    def key(self) -> tuple:
        # Normalize message to first 200 chars so tiny rendering differences
        # don't split duplicates.
        return (
            self.rule_id,
            self.path,
            self.start_line,
            self.end_line,
            self.message[:200],
        )


class GitHubClient:
    """Thin, fail-open wrapper around the code-scanning endpoints."""

    def __init__(self, token: str, repo: str) -> None:
        self.token = token
        self.repo = repo
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        self.session = requests.Session()
        self.session.headers.update(self.headers)

    def get(
        self, path: str, params: Optional[dict] = None
    ) -> Optional[dict | list]:
        if path.startswith("http"):
            url = path
        else:
            url = f"{GITHUB_API}/repos/{self.repo}{path}"
        for attempt in range(3):
            try:
                response = self.session.get(
                    url, params=params or {}, timeout=30
                )
                if response.status_code == 404:
                    return None
                response.raise_for_status()
                return response.json()
            except Exception as exc:
                log_warning(
                    "GitHub API request failed",
                    attempt=attempt + 1,
                    url=url,
                    exc=exc,
                )
                if attempt == 2:
                    return None
                time.sleep(2 ** attempt)
        return None

    def post(self, path: str, payload: dict) -> bool:
        url = f"{GITHUB_API}/repos/{self.repo}{path}"
        try:
            response = self.session.post(url, json=payload, timeout=30)
            response.raise_for_status()
            return True
        except Exception as exc:
            log_error("GitHub API POST failed", url=url, exc=exc)
            return False

    def list_analyses(self, ref: str) -> list[dict]:
        """Analyses uploaded for ``ref``, newest first (as the API returns them).

        One page is enough: the commit we are gating is the newest one on the ref, so
        its analyses sit at the top.
        """
        data = self.get(
            "/code-scanning/analyses",
            # sort/direction are the API defaults, sent explicitly because the
            # newest-first order is what "the previous commit" is read off.
            params={
                "ref": ref,
                "per_page": 100,
                "sort": "created",
                "direction": "desc",
            },
        )
        if not isinstance(data, list):
            return []
        return [entry for entry in data if isinstance(entry, dict)]

    def fetch_alerts(self, ref: str) -> list[Alert]:
        alerts: list[Alert] = []
        page = 1
        while True:
            data = self.get(
                "/code-scanning/alerts",
                params={
                    "ref": ref,
                    "state": "open",
                    "per_page": 100,
                    "page": page,
                },
            )
            if data is None:
                return []  # fail-open: treat as no alerts on API error
            if not isinstance(data, list):
                log_warning(
                    "Unexpected code-scanning/alerts response shape",
                    response_type=type(data).__name__,
                )
                return []
            if not data:
                break
            for raw in data:
                alert = _alert_from_raw(raw)
                if alert:
                    alerts.append(alert)
            if len(data) < 100:
                break
            page += 1
        return alerts


def _alert_from_raw(raw: dict) -> Optional[Alert]:
    rule = raw.get("rule", {})
    if not rule:
        return None
    instance = raw.get("most_recent_instance", {})
    location = instance.get("location", {})
    message = instance.get("message", {})
    return Alert(
        number=raw.get("number", 0),
        rule_id=rule.get("id", "unknown"),
        severity=rule.get("severity"),
        security_severity=rule.get("security_severity_level"),
        path=location.get("path", ""),
        start_line=location.get("start_line", 0),
        end_line=location.get("end_line", 0),
        message=(message.get("text") or "").strip(),
        html_url=raw.get("html_url", ""),
        raw=raw,
    )


def categories_for_commit(analyses: list[dict], commit_sha: str) -> set[str]:
    return {
        entry.get("category")
        for entry in analyses
        if entry.get("commit_sha") == commit_sha and entry.get("category")
    }


def expected_categories(
    analyses: list[dict], commit_sha: str, lookback: int = 3
) -> set[str]:
    """The category set commit ``commit_sha`` is expected to produce on this ref.

    Self-calibrates what "analysis complete" means instead of hardcoding a list: take the
    newest ``lookback`` commits on the ref that AREN'T ours and use the largest category
    set any of them produced. Largest, not newest — a single commit can legitimately be
    missing a category (a matrix leg failed, or its run was still uploading when the next
    push superseded it), and calibrating off that one would make a partial upload look
    complete for every commit after it.

    An empty result means we have nothing to calibrate against (a PR's very first push has
    no previous commit); the caller falls back to the base ref, which always does.
    """
    by_commit: dict[str, set[str]] = {}
    for entry in analyses:
        sha = entry.get("commit_sha") or ""
        category = entry.get("category")
        if not sha or sha == commit_sha or not category:
            continue
        if sha not in by_commit:
            if len(by_commit) >= lookback:
                continue
            by_commit[sha] = set()
        by_commit[sha].add(category)
    if not by_commit:
        return set()
    return max(by_commit.values(), key=len)


def wait_for_analysis(
    client: GitHubClient,
    ref: str,
    timeout: int,
    interval: int,
    commit_sha: str = "",
    base_ref: str = "",
    required_override: Optional[set[str]] = None,
) -> bool:
    """Poll until CodeQL has uploaded analysis for ``commit_sha`` on ``ref``.

    "An analysis exists for the ref" is NOT "an analysis exists for this commit" (#904):
    on every push after the first, the previous commit's analysis is already there, so the
    old check returned instantly and the gate then diffed the PREVIOUS commit's alerts —
    reporting alerts the push had already fixed, and (the dangerous direction) missing
    genuinely new ones whenever the previous commit was clean.

    The gate and `CodeQL Advanced` both fire on `pull_request` with no ordering between
    them, so this really is a race and not a rare one.

    "Complete" is every category the ref is expected to produce, not the first one to
    land: the categories upload seconds to a minute apart, and comparing head-with-two
    against a base-with-three hides every alert in the missing one. Observed on this PR's
    own gate run — it accepted at 07:10:36 with `/language:javascript-typescript` and
    `/language:python/advanced` in, while `/language:python` did not land until 07:10:54.

    Returns True once the commit's analysis is complete, False on timeout. Timing out is
    not fatal — the caller fails open, loudly.
    """
    deadline = time.time() + timeout
    base_required: Optional[set[str]] = None
    missing: list[str] = []
    while True:
        analyses = client.list_analyses(ref)
        if not commit_sha:
            # No SHA to pin to (workflow_call dispatch): the best we can do is the
            # pre-#904 behaviour.
            if analyses:
                log_info("CodeQL analysis available", ref=ref)
                return True
        else:
            # An explicit set wins over calibration, so the workflow that changes which categories
            # exist can say so in the same commit instead of waiting ~3 commits for the calibration
            # window to clear.
            required = set(required_override) if required_override else expected_categories(
                analyses, commit_sha
            )
            if not required and base_ref:
                # A PR's first push has no earlier commit on its own ref. The base ref
                # always has one, and runs the same CodeQL workflows, so it is the right
                # calibration source — without it the first category to land would count
                # as a complete analysis.
                if base_required is None:
                    base_required = expected_categories(
                        client.list_analyses(base_ref), commit_sha
                    )
                    log_info(
                        "Calibrating expected CodeQL categories off the base ref",
                        base_ref=base_ref,
                        categories=sorted(base_required),
                    )
                required = base_required
            available = categories_for_commit(analyses, commit_sha)
            if available and required <= available:
                log_info(
                    "CodeQL analysis available for commit",
                    ref=ref,
                    commit_sha=commit_sha,
                    categories=sorted(available),
                )
                return True
            missing = sorted(required - available)
        remaining = int(deadline - time.time())
        if remaining <= 0:
            break
        log_info(
            "Waiting for CodeQL analysis",
            ref=ref,
            commit_sha=commit_sha or "any",
            missing_categories=missing,
            remaining_seconds=remaining,
        )
        time.sleep(min(interval, remaining))
    log_warning(
        "Timed out waiting for CodeQL analysis",
        ref=ref,
        commit_sha=commit_sha or "any",
        missing_categories=missing,
        timeout=timeout,
    )
    return False


@dataclass
class AlertComparison:
    """The head-vs-base diff, and HOW each head alert the gate dismissed was matched.

    `shift_matched` is the number the gate must never keep to itself: those head alerts were
    dismissed only because the base ref carries the same alert NUMBER, not because anything
    about their `Alert.key` agreed — usually a moved line, but a changed path or message lands
    here too. That is the line-shift case this tolerance exists for, and it is also the only way
    the comparison can over-match — so it is reported on every run rather than inferred from a
    gate that went quiet.
    """

    new_alerts: list[Alert]
    exact_matched: int
    shift_matched: int


def compare_alerts(
    head_alerts: list[Alert], base_alerts: list[Alert]
) -> AlertComparison:
    """Diff head against base, tolerating alerts whose line moved.

    Matched two ways, and the second one is load-bearing. `Alert.key` includes the LINE, so a
    pre-existing alert whose line moved because the PR added code ABOVE it reads as new — which
    fails the gate on debt the PR did not create, on any diff large enough to shift a line. The
    alert NUMBER is repo-global and stable across refs, so an alert carrying a number the base ref
    also carries is the same alert wherever it now sits.

    Keeping the line key as well is deliberate: it still catches a genuinely new alert that happens
    to arrive without a number.
    """
    base_keys = {a.key for a in base_alerts}
    base_numbers = {a.number for a in base_alerts if a.number}
    new_by_key: dict[tuple, Alert] = {}
    exact_matched = 0
    shift_matched = 0
    for alert in head_alerts:
        if alert.key in base_keys:
            exact_matched += 1
            continue
        if alert.number and alert.number in base_numbers:
            shift_matched += 1
            continue
        new_by_key[alert.key] = alert
    return AlertComparison(
        new_alerts=list(new_by_key.values()),
        exact_matched=exact_matched,
        shift_matched=shift_matched,
    )


def find_new_alerts(
    head_alerts: list[Alert], base_alerts: list[Alert]
) -> list[Alert]:
    """Alerts present on the head ref that the base ref does not already carry.

    The verdict half of `compare_alerts`, for callers that do not need the match counts.
    """
    return compare_alerts(head_alerts, base_alerts).new_alerts


def classify_alerts(
    alerts: list[Alert],
) -> tuple[list[Alert], list[Alert], list[Alert]]:
    """Return (security, mechanical, judgment) alert lists."""
    security = [a for a in alerts if a.is_security]
    mechanical = [a for a in alerts if a.is_mechanical]
    judgment = [a for a in alerts if a.is_judgment]
    return security, mechanical, judgment


def affected_files(alerts: list[Alert]) -> set[str]:
    return {a.path for a in alerts if a.path}


# ── Auto-fix implementations ──────────────────────────────────────────────────


def run_ruff_fix(files: set[str], ruff_cmd: str) -> bool:
    """Run ruff on the affected files to fix Pyflakes (F) rules."""
    if not files:
        return False
    files = {f for f in files if os.path.isfile(f)}
    if not files:
        return False
    cmd = ruff_cmd.split() + ["check", "--select", "F", "--fix"] + sorted(files)
    log_info("Running ruff fix", command=" ".join(cmd))
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=120,
        )
        log_info(
            "Ruff completed", returncode=result.returncode, stdout=result.stdout[:500]
        )
        return True
    except Exception as exc:
        log_warning("Ruff fix failed", exc=exc, command=" ".join(cmd))
        return False


def fix_implicit_string_concatenation_in_list(alerts: list[Alert]) -> set[str]:
    """Insert a comma between adjacent string literals inside list literals.

    This is a conservative, line-local fix: CodeQL has already confirmed the
    pattern is in a list, so splitting the literals into separate items is the
    most likely intent. Returns the set of files that were modified.
    """
    # Match two string literals separated by whitespace.
    pattern = re.compile(
        r"(?P<q1>[\"'])(?P<s1>.*?)(?P=q1)\s+(?P<q2>[\"'])(?P<s2>.*?)(?P=q2)"
    )
    modified: set[str] = set()
    by_file: dict[str, list[Alert]] = {}
    for alert in alerts:
        if alert.rule_id == "py/implicit-string-concatenation-in-list":
            by_file.setdefault(alert.path, []).append(alert)

    for path, file_alerts in by_file.items():
        if not os.path.isfile(path):
            continue
        lines = {}
        try:
            with open(path, "r", encoding="utf-8") as fh:
                lines = {i + 1: line for i, line in enumerate(fh.readlines())}
        except Exception as exc:
            log_warning(
                "Could not read file for concat fix", path=path, exc=exc
            )
            continue

        changed = False
        for alert in file_alerts:
            for line_no in range(alert.start_line, alert.end_line + 1):
                original = lines.get(line_no, "")
                # Only fix lines that contain two adjacent quoted strings.
                if not pattern.search(original):
                    continue
                fixed = pattern.sub(
                    r"\g<q1>\g<s1>\g<q1>, \g<q2>\g<s2>\g<q2>",
                    original,
                )
                if fixed != original:
                    lines[line_no] = fixed
                    changed = True
                    log_info(
                        "Fixed implicit string concatenation in list",
                        path=path,
                        line=line_no,
                    )
        if changed:
            try:
                with open(path, "w", encoding="utf-8") as fh:
                    for i in range(1, max(lines) + 1):
                        fh.write(lines.get(i, "\n"))
                modified.add(path)
            except Exception as exc:
                log_warning(
                    "Could not write file for concat fix", path=path, exc=exc
                )
    return modified


def fix_empty_except_blocks(alerts: list[Alert]) -> set[str]:
    """Replace bare ``except:`` with ``except Exception:`` in empty blocks.

    A bare ``except:`` catches ``SystemExit`` and ``KeyboardInterrupt``;
    narrowing it to ``Exception`` is the safe mechanical correction. Only
    touches blocks that are actually empty (``pass`` or just a comment).
    Returns modified files.
    """
    modified: set[str] = set()
    by_file: dict[str, list[Alert]] = {}
    for alert in alerts:
        if alert.rule_id == "py/empty-except":
            by_file.setdefault(alert.path, []).append(alert)

    for path, file_alerts in by_file.items():
        if not os.path.isfile(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as fh:
                lines = fh.readlines()
        except Exception as exc:
            log_warning(
                "Could not read file for empty-except fix", path=path, exc=exc
            )
            continue

        changed = False
        for alert in file_alerts:
            for idx in range(
                alert.start_line - 1, min(alert.end_line, len(lines))
            ):
                line = lines[idx]
                stripped = line.strip()
                # Only fix the header line, and only when the block is empty.
                if re.match(r"^except\s*:\s*(?:#.*)?$", stripped):
                    indent = line[: len(line) - len(line.lstrip())]
                    lines[idx] = f"{indent}except Exception:\n"
                    # Ensure the next non-comment line is pass, but do not
                    # duplicate it.
                    nxt = idx + 1
                    while nxt < len(lines) and lines[nxt].strip().startswith(
                        "#"
                    ):
                        nxt += 1
                    if nxt < len(lines):
                        next_stripped = lines[nxt].strip()
                        if next_stripped and next_stripped not in (
                            "pass",
                            "raise",
                            "return",
                            "continue",
                            "break",
                        ):
                            # Block has real code; not a mechanical empty
                            # except.
                            lines[idx] = line
                            log_warning(
                                "Skipping non-empty except block",
                                path=path,
                                line=alert.start_line,
                            )
                            continue
                        if next_stripped == "":
                            lines[nxt] = f"{indent}    pass\n"
                    changed = True
                    log_info(
                        "Fixed empty except block",
                        path=path,
                        line=alert.start_line,
                    )
        if changed:
            try:
                with open(path, "w", encoding="utf-8") as fh:
                    fh.writelines(lines)
                modified.add(path)
            except Exception as exc:
                log_warning(
                    "Could not write file for empty-except fix",
                    path=path,
                    exc=exc,
                )
    return modified


def apply_mechanical_fixes(
    mechanical_alerts: list[Alert], ruff_cmd: str
) -> tuple[list[Alert], set[str]]:
    """Apply all safe auto-fixes and return (fixed_alerts, modified_files).

    We count an alert as fixed if its file was touched by a targeted fix or by
    ruff. This is intentionally optimistic: the verification step
    (ruff + local CodeQL) is the source of truth.
    """
    files_before = {
        f for f in affected_files(mechanical_alerts) if os.path.isfile(f)
    }

    # Targeted fixes first, then ruff cleans up the Pyflakes rules.
    modified: set[str] = set()
    modified |= fix_implicit_string_concatenation_in_list(mechanical_alerts)
    modified |= fix_empty_except_blocks(mechanical_alerts)
    run_ruff_fix(affected_files(mechanical_alerts), ruff_cmd)

    # Count any file that changed on disk as potentially fixed.
    changed_files = set()
    for path in files_before:
        try:
            result = subprocess.run(
                ["git", "diff", "--quiet", "--", path],
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                changed_files.add(path)
        except Exception as exc:
            log_warning("Could not diff file", path=path, exc=exc)

    # A mechanical alert is considered fixed if its file was touched.
    fixed_alerts = [a for a in mechanical_alerts if a.path in changed_files]
    return fixed_alerts, changed_files | modified


# ── Git operations ────────────────────────────────────────────────────────────


def git_config() -> None:
    subprocess.run(
        ["git", "config", "user.name", "github-actions[bot]"],
        check=False,
        capture_output=True,
    )
    subprocess.run(
        [
            "git",
            "config",
            "user.email",
            "github-actions[bot]@users.noreply.github.com",
        ],
        check=False,
        capture_output=True,
    )


def commit_and_push(branch: str) -> bool:
    """Commit any changes and push to the supplied branch."""
    git_config()
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        capture_output=True,
        text=True,
    )
    if not status.stdout.strip():
        log_info("No changes to commit")
        return True

    subprocess.run(["git", "add", "-u"], check=False, capture_output=True)
    commit = subprocess.run(
        [
            "git",
            "commit",
            "-m",
            "chore(codeql): auto-fix mechanical quality alerts",
        ],
        capture_output=True,
        text=True,
    )
    if commit.returncode != 0:
        log_warning(
            "Commit failed", stdout=commit.stdout, stderr=commit.stderr
        )
        return False

    push = subprocess.run(
        ["git", "push", "origin", f"HEAD:{branch}"],
        capture_output=True,
        text=True,
    )
    if push.returncode != 0:
        log_error(
            "Push failed",
            branch=branch,
            stdout=push.stdout,
            stderr=push.stderr,
        )
        return False

    log_info("Pushed fixes", branch=branch)
    return True


# ── Reporting ─────────────────────────────────────────────────────────────────


def _alert_bullet(alert: Alert) -> str:
    return (
        f"- [{alert.rule_id}] [{alert.path}:{alert.start_line}]"
        f"({alert.html_url}) — {alert.message[:120]}"
    )


def build_report(
    new_alerts: list[Alert],
    security: list[Alert],
    mechanical: list[Alert],
    judgment: list[Alert],
    fixed_alerts: list[Alert],
) -> str:
    fixed_keys = {a.key for a in fixed_alerts}
    lines = [
        "## CodeQL PR Gate",
        "",
        f"New alerts introduced by this PR: **{len(new_alerts)}**",
    ]
    if fixed_alerts:
        lines.append(f"- Auto-fixed mechanical alerts: **{len(fixed_alerts)}**")
    if security:
        lines.append(
            f"- Blocking security-severity alerts: **{len(security)}**"
        )
    if judgment:
        lines.append(f"- Alerts requiring human review: **{len(judgment)}**")
    if not security and not judgment and len(fixed_alerts) == len(
        mechanical
    ):
        lines.append("")
        lines.append(
            "All new mechanical alerts were auto-fixed. "
            "No further action needed."
        )
        return "\n".join(lines)

    if security:
        lines.append("")
        lines.append(
            "### Blocking security-severity alerts (never auto-fixed)"
        )
        for alert in security:
            lines.append(
                f"- [{alert.rule_id}] [{alert.path}:{alert.start_line}]"
                f"({alert.html_url}) — "
                f"{alert.security_severity or alert.severity}: "
                f"{alert.message[:120]}"
            )

    if judgment:
        lines.append("")
        lines.append("### Alerts requiring human review")
        for alert in judgment:
            lines.append(_alert_bullet(alert))
        lines.append("")
        lines.append(
            "If an alert is a false positive, add a suppression comment "
            "on the flagged line: `# lgtm[py/rule-id]`"
        )

    not_fixed = [a for a in mechanical if a.key not in fixed_keys]
    if not_fixed:
        lines.append("")
        lines.append(
            f"### Mechanical alerts not auto-fixed ({len(not_fixed)})"
        )
        for alert in not_fixed:
            lines.append(_alert_bullet(alert))

    return "\n".join(lines)


def _emit_report_to_the_run(report: str, new_alerts: list[Alert]) -> None:
    """Put the alert list where a merge_group run can show it: the step summary and annotations.

    Unconditional on purpose. A PR comment covers the `pull_request` ref only, and the merge_group
    ref is the one that blocks the queue.
    """
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        try:
            with open(summary, "a", encoding="utf-8") as handle:
                handle.write(f"\n{report}\n")
        except OSError:
            # Best-effort. The summary file is a convenience for whoever reads the run; losing it
            # must never turn a passing gate into a failing one, or an unwritable runner disk
            # starts blocking merges for a reason that has nothing to do with code quality.
            pass
    for alert in new_alerts:
        # ::error is what surfaces on the run's Summary tab, which is all a merge_group run has.
        print(
            f"::error file={alert.path},line={alert.start_line}::"
            f"[{alert.rule_id}] {alert.message}",
            flush=True,
        )


def post_comment(
    client: GitHubClient, pr_number: int, body: str
) -> bool:
    if pr_number <= 0:
        return False
    log_info("Posting PR comment", pr_number=pr_number, body_length=len(body))
    return client.post(f"/issues/{pr_number}/comments", {"body": body})


def write_github_outputs(outputs: dict[str, str]) -> None:
    path = os.getenv("GITHUB_OUTPUT", "")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as fh:
            for key, value in outputs.items():
                fh.write(f"{key}={value}\n")
    except Exception as exc:
        log_warning("Could not write GitHub outputs", exc=exc)


# ── Main orchestration ────────────────────────────────────────────────────────


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    token = os.getenv("GITHUB_TOKEN", "")
    if not token:
        log_warning("GITHUB_TOKEN not set; gate cannot read code-scanning alerts")
        return 0  # fail open

    client = GitHubClient(token, args.repo)

    # Wait for CodeQL results from the existing workflows.
    required_override = {
        c.strip() for c in args.required_categories.split(",") if c.strip()
    }
    if required_override:
        log_info(
            "Requiring an explicit CodeQL category set (calibration bypassed)",
            categories=sorted(required_override),
        )
    head_ready = wait_for_analysis(
        client,
        args.head_ref,
        args.wait_timeout,
        args.wait_interval,
        commit_sha=args.head_sha,
        base_ref=args.base_ref,
        required_override=required_override,
    )
    if not head_ready:
        # LOUD, not a quiet warning. This gate silently timed out on all 24 of its runs and reported
        # success every time, so the repo believed it had per-PR alert diffing that had never once
        # executed. Failing open is still right — a flaky analysis must not block a merge — but it
        # has to be visible in the job summary, not buried in a log line nobody reads.
        message = (
            f"::warning title=CodeQL gate did not run::No CodeQL analysis for "
            f"{args.head_ref} at commit {args.head_sha or '(any)'} after "
            f"{args.wait_timeout}s — alerts were NOT compared. Failing open. If this "
            f"repeats, either CodeQL is slower than the timeout, or the ref is wrong (the "
            f"code-scanning API needs a git ref such as refs/pull/<n>/merge, not a commit "
            f"SHA). Alerts from an EARLIER commit are deliberately not compared: they "
            f"report fixed issues and hide new ones (#904)."
        )
        print(message, flush=True)
        summary = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary:
            try:
                with open(summary, "a", encoding="utf-8") as handle:
                    handle.write(f"\n> [!WARNING]\n> {message.split('::')[-1]}\n")
            except OSError:
                pass
        log_warning(
            "CodeQL analysis not available for head ref; failing open",
            head_ref=args.head_ref,
        )
        return 0

    head_alerts = client.fetch_alerts(args.head_ref)
    base_alerts = client.fetch_alerts(args.base_ref)
    if head_alerts is None or base_alerts is None:
        log_warning("Could not fetch alerts; failing open")
        return 0

    comparison = compare_alerts(head_alerts, base_alerts)
    new_alerts = comparison.new_alerts
    security, mechanical, judgment = classify_alerts(new_alerts)

    log_info(
        "Compared alerts",
        head_count=len(head_alerts),
        base_count=len(base_alerts),
        new_count=len(new_alerts),
        # How the dismissals split. shift_matched is the tolerance doing its job (#1087);
        # a run where it is large is a run to look at, and it can only be looked at if the
        # gate says the number out loud.
        exact_matched=comparison.exact_matched,
        shift_matched=comparison.shift_matched,
        security=len(security),
        mechanical=len(mechanical),
        judgment=len(judgment),
    )

    fixed_alerts: list[Alert] = []
    pushed = True
    if mechanical and args.fix:
        fixed_alerts, _ = apply_mechanical_fixes(mechanical, args.ruff_cmd)
        if args.branch:
            pushed = commit_and_push(args.branch)

    report = build_report(new_alerts, security, mechanical, judgment, fixed_alerts)
    if new_alerts:
        post_comment(client, args.pr_number, report)
        # A merge_group run has no PR to comment on (`pr_number=0`), and `log_info(...,
        # report=report)` does not render kwargs to stdout — so on the one ref where this gate
        # actually has teeth, it failed with `Gate failed: new alerts require human review` and
        # nothing else. The queue evicted PRs with no way to see why: #1168 churned 24 times,
        # #1171 eleven. Both were one unused module-level variable in a test file.
        _emit_report_to_the_run(report, new_alerts)
    log_info("CodeQL PR gate report", report=report)

    fixed_count = len(fixed_alerts)
    remaining_security = len(security)
    remaining_judgment = len(judgment)
    remaining_mechanical = len(mechanical) - fixed_count
    failed_open = not (
        head_ready and head_alerts is not None and base_alerts is not None
    )

    write_github_outputs(
        {
            "mechanical_count": str(len(mechanical)),
            "security_count": str(len(security)),
            "judgment_count": str(len(judgment)),
            "fixed_count": str(fixed_count),
            "shift_matched_count": str(comparison.shift_matched),
            "failed_open": str(failed_open).lower(),
            "conclusion": "success"
            if not (
                remaining_security or remaining_judgment or remaining_mechanical
            )
            else "failure",
        }
    )

    if failed_open:
        log_warning("Gate failed open; allowing PR to proceed")
        return 0

    if not pushed:
        log_error("Could not push fixes; failing gate")
        return 1

    if remaining_security or remaining_judgment or remaining_mechanical:
        log_error(
            "Gate failed: new alerts require human review",
            security=remaining_security,
            judgment=remaining_judgment,
            mechanical=remaining_mechanical,
        )
        return 1

    log_info("Gate passed")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
