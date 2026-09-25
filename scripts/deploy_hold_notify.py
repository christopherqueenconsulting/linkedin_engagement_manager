#!/usr/bin/env python3
"""Make a production deploy that is not happening VISIBLE: one `needs-human` issue + one email.

Production sat on v0.176.5 for ~24h while v0.177.0..v0.178.1 were tagged, because
`release_risk_check.py` held a migration-bearing release and the only trace was a `::warning` inside
a GREEN run plus a comment on an already-merged release PR — surfaces nobody reads. This module owns
the loud half of that gate, and a scheduled backstop that does not depend on the gate at all:

1. **Hold issue** (`upsert_hold_issue`). When the gate holds a release it opens — or UPDATES — ONE
   open issue labelled `needs-human`, identified by the `MARKER` HTML comment in its body (never by
   title or by search, which lags new issues by minutes). A later release re-flagging the same held
   state updates that same issue: the target tag moves forward, the file list grows, and the exact
   `gh workflow run deploy-vps.yml -f tag=...` command always names the NEWEST held tag, since that
   deploy carries every earlier one with it.
2. **Drift check** (`drift` subcommand, `.github/workflows/deploy-drift-check.yml`, hourly). Compares
   production's `/api/app-info` version against the published releases. If the oldest release
   production has NOT got is older than `--max-hours` (default 3 — releases batch 4x/day, see
   `docs/zero-downtime-deploys.md`), it opens/updates the same issue. This catches every other way a
   deploy silently does not happen (SSH exhaustion, a red build, a skipped job), not just the gate.
3. **Auto-close**. Every drift run first closes any open hold issue whose target tag production has
   reached or passed.
4. **Email** (`send_owner_email`). SendGrid's v3 HTTP API — the SAME provider the app
   (`utilities/email.py`) and the host watchdog (`scripts/stack_watchdog.sh`) use — sent only when
   an issue is CREATED or when a NEW non-additive migration joins an existing hold. A drift update or
   a re-flag of files already listed updates the issue silently: the inbox is for a state change.

Every GitHub/HTTP read or write here fails OPEN with a `::warning` — this is notification, and the
deploy decision was already written by the gate before any of it runs. Stdlib + `gh` CLI only.

Env:
  GH_TOKEN / GITHUB_TOKEN   For `gh` (needs `issues: write`).
  PUBLIC_BASE_URL           Where `/api/app-info` lives (drift + auto-close).
  SENDGRID_API_KEY          Actions SECRET. Unset = no email (warned), issue still filed.
  SENDGRID_FROM_EMAIL       Actions VARIABLE — the authenticated sender (`.env.example`).
  DEPLOY_ALERT_EMAIL        Actions SECRET — the owner's address. Never hardcoded here.
  DEPLOY_DRIFT_MAX_HOURS    Actions VARIABLE — the drift threshold (default 3).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone

import release_risk_check as rrc

#: Identifies THE hold issue. Attributes after it carry the state the next writer needs.
MARKER = "lem-deploy-hold"
HOLD_LABEL = "needs-human"
DEFAULT_DRIFT_MAX_HOURS = 3.0
SENDGRID_URL = "https://api.sendgrid.com/v3/mail/send"
EMAIL_TIMEOUT_SECONDS = 20

_MARKER_RE = re.compile(rf"<!--\s*{MARKER}\s+target=(\S*)\s+files=(\S*)\s*-->")
_SECTION_RE = r"<!-- section:{name} -->.*?<!-- /section:{name} -->"


# ────────────────────────────────────────────────────────────── pure helpers


def parse_version(value: str | None) -> tuple[int, ...] | None:
    """`v0.178.1` / `0.178.1` → `(0, 178, 1)`; `None` when it is not a plain dotted version."""
    if not value:
        return None
    match = re.fullmatch(r"v?(\d+(?:\.\d+)*)", value.strip())
    return tuple(int(p) for p in match.group(1).split(".")) if match else None


def version_reached(deployed: str | None, target: str | None) -> bool:
    """Whether production (`deployed`) is at or past `target`. Unparseable either side = False."""
    have, want = parse_version(deployed), parse_version(target)
    return have is not None and want is not None and have >= want


def newer_tag(a: str | None, b: str | None) -> str | None:
    """The later of two release tags by version; an unparseable one loses to a parseable one."""
    if parse_version(a) is None:
        return b if parse_version(b) is not None else a
    if parse_version(b) is None:
        return a
    return a if parse_version(a) >= parse_version(b) else b


@dataclass(frozen=True)
class HoldState:
    """What the hold issue currently records — parsed from, and rendered into, its marker."""

    target_tag: str | None = None
    files: frozenset[str] = field(default_factory=frozenset)


def parse_marker(body: str) -> HoldState | None:
    """The `HoldState` in an issue body, or `None` when the body carries no hold marker."""
    match = _MARKER_RE.search(body or "")
    if match is None:
        return None
    target = match.group(1) or None
    files = frozenset(f for f in match.group(2).split(",") if f)
    return HoldState(target_tag=target, files=files)


def render_marker(state: HoldState) -> str:
    """The marker line for `state` — file NAMES only, comma-joined, so it survives any Markdown."""
    return f"<!-- {MARKER} target={state.target_tag or ''} files={','.join(sorted(state.files))} -->"


def upsert_section(body: str, name: str, content: str) -> str:
    """Replace the `name` section of `body`, or append it — the other writer's section is untouched.

    The gate and the drift check both write this one issue; each owns a section, so the drift check
    refreshing its lag figure can never erase the gate's per-migration reasons.
    """
    block = f"<!-- section:{name} -->\n{content.strip()}\n<!-- /section:{name} -->"
    pattern = re.compile(_SECTION_RE.format(name=re.escape(name)), re.DOTALL)
    if pattern.search(body):
        return pattern.sub(lambda _: block, body, count=1)
    return f"{body.rstrip()}\n\n{block}\n"


def render_body(state: HoldState, previous_body: str, section: str, section_markdown: str) -> str:
    """The full issue body: marker, a header naming the one command to run, then the sections."""
    target = state.target_tag or "<latest release tag>"
    header = "\n".join(
        [
            render_marker(state),
            f"## Production is not running `{target}`",
            "",
            "A release is not deploying on its own. To ship it (and every release before it — a deploy "
            "applies all pending migrations and code at once), run:",
            "",
            "```",
            f"gh workflow run deploy-vps.yml -f tag={target}",
            "```",
            "",
            "This issue closes itself once production's `/api/app-info` reports this tag or later "
            "(checked hourly by `deploy-drift-check.yml`). Why it exists: `docs/DEPLOYMENT.md` "
            "§ Deploy hold & drift alerts.",
        ]
    )
    sections = previous_body.split("<!-- section:", 1)
    kept = "<!-- section:" + sections[1] if len(sections) == 2 else ""
    return upsert_section(f"{header}\n\n{kept}".rstrip() + "\n", section, section_markdown)


def format_hold_section(tag: str, deployed: str | None, held: list[tuple[str, list[str]]]) -> str:
    """The gate's section: which migration(s) held `tag`, and why each is NOT provably additive.

    Args:
        tag: The release the gate just held.
        deployed: What production runs, when `/api/app-info` said so.
        held: `(path, reasons)` for every non-additive migration in the range.

    Returns:
        Markdown for the `hold` section.
    """
    lines = [
        f"### Held by `release-risk-check` at `{tag}`",
        "",
        f"Production runs `{deployed or 'unknown (/api/app-info unreadable)'}`. The automatic deploy was "
        "skipped because these migration(s) are not provably additive — an image rollback cannot undo "
        "applied DDL, so a human looks first. Additive-only migrations (new table, NULLable/DEFAULTed "
        "column, non-unique index, ENUM append, seed INSERT) deploy automatically and are not listed.",
        "",
    ]
    for path, reasons in held:
        lines.append(f"- `{path}`")
        lines.extend(f"  - {reason}" for reason in reasons)
    return "\n".join(lines)


def format_drift_section(deployed: str, latest: str, oldest_pending: str, lag_hours: float, max_hours: float) -> str:
    """The drift check's section: how far production is behind, and for how long."""
    return "\n".join(
        [
            "### Deploy drift",
            "",
            f"Production runs `{deployed}`; the latest release is `{latest}`. The oldest release production "
            f"does not have, `{oldest_pending}`, was published **{lag_hours:.1f}h** ago (alert threshold "
            f"{max_hours:g}h). Last checked {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC.",
        ]
    )


def undeployed_releases(releases: list[dict], deployed: str) -> list[dict]:
    """Releases newer than `deployed`, oldest first. Rows without a parseable tag/date are dropped."""
    have = parse_version(deployed)
    if have is None:
        return []
    usable = [
        r
        for r in releases
        if isinstance(r, dict)
        and isinstance(r.get("createdAt"), str)
        and parse_version(r.get("tagName")) is not None
        and parse_version(r.get("tagName")) > have
    ]
    return sorted(usable, key=lambda r: parse_version(r["tagName"]))


def hours_since(iso: str, now: datetime) -> float | None:
    """Hours from an ISO-8601 timestamp to `now`; `None` when it does not parse."""
    try:
        then = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    return (now - then).total_seconds() / 3600.0


# ────────────────────────────────────────────────────────────── GitHub I/O (via rrc._run_gh)


#: `find_hold_issue`'s answer when the issue list itself could not be read.
UNREADABLE = "unreadable"


def find_hold_issue(repo: str) -> tuple[int, str, HoldState] | str | None:
    """The ONE open hold issue as `(number, body, state)`.

    Returns:
        The issue; `None` when no open issue carries the marker; `UNREADABLE` when the list itself
        failed — distinct from `None`, because "none open" creates an issue and "could not look" may not.
    """
    data = rrc._gh_json(
        [
            "gh", "issue", "list", "--repo", repo, "--state", "open", "--label", HOLD_LABEL,
            "--json", "number,body", "--limit", "100",
        ]
    )
    if not isinstance(data, list):
        return UNREADABLE
    found = []
    for issue in data:
        if not isinstance(issue, dict):
            continue
        state = parse_marker(issue.get("body") or "")
        if state is not None and isinstance(issue.get("number"), int):
            found.append((issue["number"], issue.get("body") or "", state))
    return min(found, key=lambda f: f[0]) if found else None


@dataclass(frozen=True)
class UpsertResult:
    """What `upsert_hold_issue` did: `created`/`updated`/`failed`, the number, and whether to email."""

    action: str
    number: int | None
    notify: bool


def upsert_hold_issue(
    repo: str,
    *,
    target_tag: str,
    new_files: list[str],
    section: str,
    section_markdown: str,
    create_when_unreadable: bool,
) -> UpsertResult:
    """Open the hold issue, or fold this state into the one already open.

    Args:
        repo: `owner/name`.
        target_tag: The release that should be deployed; the stored target only ever moves FORWARD.
        new_files: Non-additive migration file names this writer is reporting (empty for drift).
        section: Which body section this writer owns (`hold` or `drift`).
        section_markdown: That section's new content.
        create_when_unreadable: What to do when the open-issue list cannot be read. The gate says
            yes — it runs once per release, so a possible duplicate beats a silent hold. The hourly
            drift check says no, and simply looks again next hour instead of filing one per hour.

    Returns:
        The outcome. `notify` is true on creation, or when `new_files` adds a file the open issue
        did not already list — a new reason to act, not the same one said again.
    """
    existing = find_hold_issue(repo)
    names = frozenset(os.path.basename(f) for f in new_files)
    if existing == UNREADABLE and not create_when_unreadable:
        print("::warning title=hold issue not checked::could not list open issues; retrying next run.")
        return UpsertResult("failed", None, False)
    if not isinstance(existing, tuple):
        state = HoldState(target_tag=target_tag, files=names)
        body = render_body(state, "", section, section_markdown)
        title = f"Production is not on {target_tag}: deploy needs a human"
        result = rrc._run_gh(
            ["gh", "issue", "create", "--repo", repo, "--title", title, "--label", HOLD_LABEL, "--body-file", "-"],
            input_text=body,
        )
        if result.returncode != 0:
            print(f"::warning title=hold issue not created::{(result.stderr or '').strip()[:300]}")
            return UpsertResult("failed", None, False)
        match = re.search(r"/issues/(\d+)", result.stdout or "")
        return UpsertResult("created", int(match.group(1)) if match else None, True)

    number, old_body, old_state = existing
    state = HoldState(target_tag=newer_tag(old_state.target_tag, target_tag), files=old_state.files | names)
    body = render_body(state, old_body, section, section_markdown)
    args = ["gh", "issue", "edit", str(number), "--repo", repo]
    if state.target_tag != old_state.target_tag:
        args += ["--title", f"Production is not on {state.target_tag}: deploy needs a human"]
    args += ["--body-file", "-"]
    result = rrc._run_gh(args, input_text=body)
    if result.returncode != 0:
        print(f"::warning title=hold issue not updated::#{number}: {(result.stderr or '').strip()[:300]}")
        return UpsertResult("failed", number, False)
    return UpsertResult("updated", number, bool(names - old_state.files))


def close_if_deployed(repo: str, deployed: str | None) -> int | None:
    """Close the open hold issue when production has reached its target tag. Returns the closed number."""
    existing = find_hold_issue(repo)
    if not isinstance(existing, tuple) or not version_reached(deployed, existing[2].target_tag):
        return None
    number, _, state = existing
    comment = (
        f"Production `/api/app-info` now reports `{deployed}`, at or past the held `{state.target_tag}`. "
        "Closing automatically."
    )
    result = rrc._run_gh(["gh", "issue", "close", str(number), "--repo", repo, "--comment", comment])
    if result.returncode != 0:
        print(f"::warning title=hold issue not closed::#{number}: {(result.stderr or '').strip()[:300]}")
        return None
    print(f"Closed hold issue #{number}: production on {deployed} >= {state.target_tag}.")
    return number


# ────────────────────────────────────────────────────────────── email (SendGrid, like the app)


def send_owner_email(subject: str, text: str) -> bool:
    """Email the owner through SendGrid's v3 API. Missing config or any failure = `False`, warned.

    Args:
        subject: The subject line.
        text: The plain-text body.

    Returns:
        Whether SendGrid accepted the message.
    """
    key = os.getenv("SENDGRID_API_KEY", "").strip()
    sender = os.getenv("SENDGRID_FROM_EMAIL", "").strip()
    to = os.getenv("DEPLOY_ALERT_EMAIL", "").strip()
    if not (key and sender and to):
        missing = [n for n, v in (("SENDGRID_API_KEY", key), ("SENDGRID_FROM_EMAIL", sender),
                                  ("DEPLOY_ALERT_EMAIL", to)) if not v]
        print(f"::warning title=hold email not sent::not configured — missing {', '.join(missing)}")
        return False
    payload = {
        "personalizations": [{"to": [{"email": to}]}],
        "from": {"email": sender, "name": "LEM deploy alerts"},
        "subject": subject,
        "content": [{"type": "text/plain", "value": text}],
    }
    request = urllib.request.Request(
        SENDGRID_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=EMAIL_TIMEOUT_SECONDS) as response:
            ok = 200 <= response.status < 300
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        print(f"::warning title=hold email not sent::SendGrid: {exc}")
        return False
    if not ok:
        print("::warning title=hold email not sent::SendGrid returned a non-2xx status")
    return ok


def notify(repo: str, result: UpsertResult, subject: str, summary: str) -> bool:
    """Email the owner about `result` when it is a state change; never on a routine update."""
    if not result.notify:
        return False
    link = f"https://github.com/{repo}/issues/{result.number}" if result.number else f"https://github.com/{repo}/issues"
    return send_owner_email(subject, f"{summary}\n\nIssue: {link}\n")


def report_gate_hold(repo: str, tag: str, deployed: str | None, held: list[tuple[str, list[str]]]) -> UpsertResult:
    """The gate's entry point: file/update the hold issue for `tag`, and email on a state change.

    Args:
        repo: `owner/name`.
        tag: The release the gate held.
        deployed: Production's version when readable.
        held: `(path, reasons)` for every non-additive migration.

    Returns:
        The upsert outcome.
    """
    result = upsert_hold_issue(
        repo,
        target_tag=tag,
        new_files=[path for path, _ in held],
        section="hold",
        section_markdown=format_hold_section(tag, deployed, held),
        create_when_unreadable=True,
    )
    files = ", ".join(os.path.basename(p) for p, _ in held)
    notify(
        repo,
        result,
        f"[LEM] Deploy held: {tag} needs a manual deploy",
        f"The release gate held {tag} (production: {deployed or 'unknown'}) — non-additive migration(s): "
        f"{files}.\n\nTo ship it: gh workflow run deploy-vps.yml -f tag={tag}",
    )
    return result


def run_drift_check(repo: str, app_url: str, max_hours: float, now: datetime | None = None) -> int:
    """The scheduled backstop: auto-close a resolved hold, then alert on a lagging production.

    Args:
        repo: `owner/name`.
        app_url: `PUBLIC_BASE_URL`.
        max_hours: How long the oldest undeployed release may wait before this alerts.
        now: Injectable clock for tests.

    Returns:
        Always 0 — a notification job never fails the workflow for what it observed.
    """
    now = now or datetime.now(timezone.utc)
    deployed = rrc.fetch_deployed_version(app_url)
    if deployed is None:
        print("::warning title=deploy drift unknown::/api/app-info unreadable — nothing compared this run.")
        return 0
    deployed_tag = rrc.version_to_tag(deployed)
    close_if_deployed(repo, deployed_tag)

    releases = rrc._gh_json(
        [
            "gh", "release", "list", "--repo", repo, "--exclude-drafts", "--exclude-pre-releases",
            "--json", "tagName,createdAt", "--limit", "30",
        ]
    )
    if not isinstance(releases, list):
        return 0
    pending = undeployed_releases(releases, deployed_tag)
    if not pending:
        print(f"PASS: production {deployed_tag} is on the latest release.")
        return 0
    oldest, latest = pending[0], pending[-1]
    lag = hours_since(oldest["createdAt"], now)
    if lag is None or lag <= max_hours:
        print(f"PASS: production {deployed_tag} is {len(pending)} release(s) behind, within {max_hours:g}h.")
        return 0
    print(f"DRIFT: production {deployed_tag}, latest {latest['tagName']}, oldest pending {lag:.1f}h old.")
    result = upsert_hold_issue(
        repo,
        target_tag=latest["tagName"],
        new_files=[],
        section="drift",
        section_markdown=format_drift_section(deployed_tag, latest["tagName"], oldest["tagName"], lag, max_hours),
        create_when_unreadable=False,
    )
    notify(
        repo,
        result,
        f"[LEM] Production is {lag:.0f}h behind: {deployed_tag} vs {latest['tagName']}",
        f"Production runs {deployed_tag}; {len(pending)} release(s) up to {latest['tagName']} are not deployed "
        f"(oldest published {lag:.1f}h ago).\n\nTo ship: gh workflow run deploy-vps.yml -f tag={latest['tagName']}",
    )
    return 0


def _max_hours(raw: str | None) -> float:
    try:
        value = float(raw) if raw not in (None, "") else DEFAULT_DRIFT_MAX_HOURS
    except ValueError:
        print(f"::warning title=bad DEPLOY_DRIFT_MAX_HOURS::{raw!r} — using {DEFAULT_DRIFT_MAX_HOURS:g}")
        return DEFAULT_DRIFT_MAX_HOURS
    return value if value > 0 else DEFAULT_DRIFT_MAX_HOURS


def main(argv: list[str]) -> int:
    """CLI: `drift` runs the scheduled check (the gate calls `report_gate_hold` directly)."""
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["drift"])
    ap.add_argument("--repo", default=os.getenv("GITHUB_REPOSITORY", rrc.DEFAULT_REPO))
    ap.add_argument("--app-url", default=os.getenv("PUBLIC_BASE_URL", ""))
    ap.add_argument("--max-hours", default=os.getenv("DEPLOY_DRIFT_MAX_HOURS", ""))
    args = ap.parse_args(argv[1:])
    return run_drift_check(args.repo, args.app_url, _max_hours(args.max_hours))


if __name__ == "__main__":  # pragma: no cover - CLI
    raise SystemExit(main(sys.argv))
