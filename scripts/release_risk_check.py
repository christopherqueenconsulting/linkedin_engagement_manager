#!/usr/bin/env python3
"""Flag a migration-bearing release before it auto-deploys (#1133).

`.github/workflows/build-and-push.yml`'s `deploy` job runs on `environment: production` with its
required-reviewer gate deliberately removed — "green releases auto-deploy" per the workflow's own
comment. That is the right default for the overwhelming majority of releases, but a Flyway migration
is one-way against production data: rolling the image back to `.last_good_tag` does not roll back
applied DDL. This script is the `release-risk-check` job's brain, running alongside `build-and-push`
and read by `deploy`'s `if:` — see `docs/graphs/deploy-release.md` for the reviewed design (3
gauntlet-loop rounds).

Scope, deliberately narrowed by owner decision on PR #1590: **a newly ADDED migration file is the
only signal here.** The reviewed design also flagged a release carrying a `risk:security` /
`risk:live-linkedin` / `risk:product-decision` PR; replaying that rule against the last 14 real
releases flagged 10 of them (71%), which would have made manual dispatch the normal way LEM reaches
production. Those PRs are already held for a human to merge by
`scripts/agent-pipeline/stage-pr.sh`, so gating them again at deploy time re-asked a question the
owner had already answered on that exact change. A migration is the one thing in a release range
that no human re-reads at merge time AND that an image rollback cannot undo — so it is the one thing
gated here.

Diff base (#1859): **what production is actually running**, read from the public, unauthenticated
`GET {PUBLIC_BASE_URL}/api/app-info` — never `.last_good_tag` (VPS-local, unreachable from the
Actions runner). Diffing against the previous *tag* instead of the previous *deploy* was the bug:
a flagged release that is correctly skipped leaves no new migration file in the NEXT release's own
tag-to-tag range, so that next release auto-deploys and Flyway applies the held migration anyway —
the gate silently defeating itself the first time it works. Reading `/api/app-info` fixes that by
diffing across however many releases production is actually behind, however many that is.

`PUBLIC_BASE_URL` unreadable (unset, network failure, bad response) degrades to the **old**
tag-to-tag behaviour — diffing this run's own tag against the previous *release* tag, found via
`gh release list`, sorted EXPLICITLY by `createdAt` here and never trusted as already sorted
(a second release — a `release:now` fast-lane release, or the next scheduled window — can publish
mid-build and reorder the API's default response). The "previous release" is the next-older entry
from THIS tag's own position in that sorted list, not list index 0/1. In that degraded path, an
undeployed hold is carried forward rather than lost: `resolve_carried_migration_files` walks back
release by release — not just one hop (#1896) — until it reaches whichever earlier release actually
introduced the still-open migration, and this release is flagged regardless of what its own
tag-to-tag diff shows. Every log line and the Decision Comment name which of the two paths actually
produced the verdict (`describe_comparison_base`, #1896) — a degraded run usually agrees with a
working `/api/app-info` read, which is exactly how a silently-broken primary path can hide.

On a flag, the workflow skips the automatic `deploy` job (this script only writes the `flagged`
GitHub Actions output the job's `if:` reads — it never exits non-zero for a flag, since that would
turn the *workflow run* red for working exactly as designed) and this script posts a Decision Comment
on the release PR naming the migration file(s). That comment is AUDIT / NOTIFICATION ONLY: nothing in
this repo watches replies on a release-please PR (it carries no `agent:ready`/`needs-human` flow
label, and `tick.sh` never reads it) — the comment says so plainly rather than implying an automated
unblock exists. The unblock is the owner running the existing manual entrypoint:

    gh workflow run deploy-vps.yml -f tag=vX.Y.Z

Every read here (release list, compare, per-commit files, the release PR lookup) fails OPEN — an
unreadable GitHub API degrades to "nothing found here", never to blocking a routine deploy. That
mirrors every other convenience gate in this repo (`docs/feature-flags.md`, `codeql_pr_gate.py`): a
transient API hiccup must not become a new single point of failure for the 4x-daily release cadence
the batching window exists to protect. The one thing that does NOT fail open is an already-decided
flag: once a migration has been found, nothing here backs off from it.

CLI:
  --tag TAG              This run's own release tag (required).
  --repo OWNER/REPO      Defaults to $GITHUB_REPOSITORY, else the hardcoded default.
  --release-limit N      `gh release list --limit` (default 20, per the acceptance criteria).
  --app-url URL          Base URL to read `/api/app-info` from. Defaults to $PUBLIC_BASE_URL. Empty
                          = skip straight to the tag-to-tag fallback.
  --no-comment           Skip posting the Decision Comment even when flagged (used by tests / a
                          dry run); the `flagged` output is still written.

Env:
  GH_TOKEN / GITHUB_TOKEN  Read by the `gh` CLI itself. Missing = fail open with a loud warning.
  PUBLIC_BASE_URL          Default source for --app-url; a repo `vars.*`, not a secret.
  GITHUB_OUTPUT            Where `flagged`/`previous_tag` are written for the workflow's `if:`.

Exit: always 0. The verdict is carried entirely in the `flagged` GitHub Actions output, never in the
process exit code — a flag is expected, routine behavior for a migration release, not a script error.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass

DEFAULT_REPO = "christopherqueenconsulting/linkedin_engagement_manager"

#: `GET /api/app-info` is a plain, unauthenticated, no-store JSON read (docs/DEPLOYMENT.md) — a slow
#: or wedged origin must not stall the release-risk-check job, which runs concurrently with the
#: ~10 min image build precisely so an unflagged release keeps today's latency.
APP_INFO_TIMEOUT_SECONDS = 15

MIGRATIONS_PREFIX = "compose/local/database/migrations/"

#: The head branch release-please always uses for its standing accumulator PR.
RELEASE_PR_HEAD_REF = "release-please--branches--main"

GH_TIMEOUT_SECONDS = 60

#: GitHub's compare API returns AT MOST 300 entries in `.files`, silently, with no truncation flag
#: and no pagination escape (`?page=2` on that endpoint answers with an EMPTY files array — measured
#: against `v0.147.0...v0.148.0`, a real range that returns exactly 300). A release range that hits
#: the cap can therefore hide a migration from `added_migration_files`, which is precisely the miss
#: this gate exists to prevent — so at the cap we stop trusting `.files` and walk the range's commits
#: one at a time instead.
COMPARE_FILES_CAP = 300


# ────────────────────────────────────────────────────────────── pure decision logic


@dataclass(frozen=True)
class Verdict:
    """The gate's answer: whether to skip the automatic deploy, and why.

    `carried_migration_files`/`carried_introduced_tag` exist so the message can distinguish a
    migration THIS release introduced from one it merely inherited from a still-undeployed earlier
    release (#1893) — `migration_files` alone (the union of both) can't tell those apart, and
    reporting only the union read as "this release added it" even when it didn't.
    """

    flagged: bool
    migration_files: tuple[str, ...]
    #: Subset of `migration_files` folded in by `merge_carried_hold` rather than found in this
    #: release's own diff. Empty on the common, non-carried path.
    carried_migration_files: tuple[str, ...] = ()
    #: The release tag that actually introduced `carried_migration_files` — never `None` when that
    #: tuple is non-empty.
    carried_introduced_tag: str | None = None


def resolve_previous_release(releases: list[dict], tag: str) -> str | None:
    """Find the release immediately older than `tag`, sorting explicitly first.

    Args:
        releases: `gh release list --json tagName,createdAt` output — NOT assumed to already be in
            any particular order (an acceptance criterion of #1133: never trust API default order).
        tag: This run's own tag.

    Returns:
        The next-older tag's name, or `None` when `tag` is absent from `releases` (outside the
        `--limit` window) or is already the oldest entry present. Entries missing either field are
        dropped rather than raising — an unreadable release row must degrade to "fail open", the
        same as an unreadable API call, never to a traceback that skips an unflagged deploy.
    """
    usable = [
        r
        for r in releases
        if isinstance(r, dict) and isinstance(r.get("createdAt"), str) and r.get("tagName")
    ]
    ordered = sorted(usable, key=lambda r: r["createdAt"], reverse=True)
    names = [r["tagName"] for r in ordered]
    try:
        idx = names.index(tag)
    except ValueError:
        return None
    if idx + 1 >= len(names):
        return None
    return names[idx + 1]


def version_to_tag(version: str) -> str:
    """Normalize an `/api/app-info` version string into the `vX.Y.Z` tag form releases use.

    Args:
        version: `get_app_version()`'s output — bare (`"0.172.1"`), never `v`-prefixed today, but
            accepted either way so this stays correct if that ever changes.

    Returns:
        The `v`-prefixed tag.
    """
    version = version.strip()
    return version if version.startswith("v") else f"v{version}"


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


def decide(*, migration_files: list[str]) -> Verdict:
    """Should the automatic `deploy` job be skipped for this release?

    Args:
        migration_files: Newly added migration paths in the commit range.

    Returns:
        A `Verdict`. One added migration is enough — this is presence, not a severity score: a lone
        migration is exactly as one-way as five of them.
    """
    return Verdict(flagged=bool(migration_files), migration_files=tuple(migration_files))


def merge_carried_hold(
    verdict: Verdict,
    carried_migration_files: tuple[str, ...],
    introduced_tag: str | None = None,
) -> Verdict:
    """Fold a still-undeployed prior flag forward so honoring it — not just re-diffing — clears it.

    Reached only on the degraded, tag-to-tag fallback path (#1859): `/api/app-info` couldn't say
    what production is actually running, so this release's OWN tag-to-tag diff can no longer see a
    migration that landed in an earlier, still-undeployed release. `carried_migration_files` is that
    earlier hold, recomputed the same way this script always decides a flag — see
    `resolve_carried_migration_files`.

    Args:
        verdict: This release's own diff verdict.
        carried_migration_files: Migration files from a still-open hold on the previous release;
            empty when there is none to carry (the common case).
        introduced_tag: The release tag that actually introduced `carried_migration_files` — the
            caller's `previous_tag`. Recorded on the merged `Verdict` so the message can say a
            migration entered in an earlier release rather than implying this one did (#1893).

    Returns:
        `verdict` unchanged when there is nothing to carry. Otherwise a `Verdict` flagged
        unconditionally, naming the union of both file sets plus the carried subset and its origin
        tag — so the Decision Comment still points at the pending migration even when this
        release's own diff came back clean, and says where it actually came from.
    """
    if not carried_migration_files:
        return verdict
    merged = tuple(sorted(set(verdict.migration_files) | set(carried_migration_files)))
    return Verdict(
        flagged=True,
        migration_files=merged,
        carried_migration_files=tuple(sorted(carried_migration_files)),
        carried_introduced_tag=introduced_tag,
    )


def summarize(verdict: Verdict) -> str:
    """One-line reason string for logs, the `::warning::` annotation, and the Decision Comment.

    When `carried_migration_files` is non-empty, the count is not all "new" — folding an inherited
    hold into a single "N new migration file(s)" is exactly the wording that reads as THIS release
    introducing something it didn't (#1893's misdiagnosis: the sentence sent an operator hunting
    through the wrong release's PRs). Naming the origin tag here means every caller that builds off
    `summarize()` gets the distinction for free.
    """
    if not verdict.migration_files:
        return "nothing found"
    if not verdict.carried_migration_files:
        return f"{len(verdict.migration_files)} new migration file(s)"
    own = sorted(set(verdict.migration_files) - set(verdict.carried_migration_files))
    origin = verdict.carried_introduced_tag or "an earlier release"
    carried_note = f"{len(verdict.carried_migration_files)} inherited from {origin} (still undeployed)"
    if own:
        return f"{len(verdict.migration_files)} migration file(s) — {len(own)} new, {carried_note}"
    return f"{len(verdict.migration_files)} migration file(s) — {carried_note}"


def describe_comparison_base(base_tag: str, *, primary_path_used: bool) -> str:
    """One phrase naming which path produced `base_tag`, for the log and the Decision Comment (#1896).

    A degraded fallback that reads identically to the primary path in every log line is exactly how
    #1896 hid: the fallback usually agrees with a working `/api/app-info` read, so nothing on a
    green or a correctly-held run told anyone which path had actually produced that verdict — until
    the primary path had silently never run once (`PUBLIC_BASE_URL` missing from the workflow's
    `env:`) and the fallback's own one-hop limit ran out of chain to carry.

    Args:
        base_tag: The tag actually diffed against.
        primary_path_used: Whether `GET /api/app-info` was read successfully this run.

    Returns:
        `"production {base_tag} (read from /api/app-info)"` on the primary path, or
        `"{base_tag} (DEGRADED: /api/app-info unreadable, previous-release fallback)"` otherwise.
    """
    if primary_path_used:
        return f"production {base_tag} (read from /api/app-info)"
    return f"{base_tag} (DEGRADED: /api/app-info unreadable, previous-release fallback)"


def format_decision_comment(
    tag: str, base_tag: str, verdict: Verdict, *, primary_path_used: bool = True
) -> str:
    """Build the markdown body posted to the release PR when `verdict.flagged`.

    Args:
        tag: This run's tag.
        base_tag: The tag actually diffed against — production's actual deployed tag when
            `/api/app-info` was readable (#1859); the last release before a still-open carried hold
            began (#1893) when a hold was folded forward; otherwise the previous release tag.
        verdict: Must be flagged — this is only ever called after that check.
        primary_path_used: Whether `base_tag` came from a readable `/api/app-info` (the primary
            path) or the tag-to-tag fallback (#1896) — stated plainly rather than left for the
            reader to infer, since the two paths otherwise read identically. Always `False` when
            `verdict.carried_migration_files` is non-empty: that branch is only ever reached on the
            fallback path.

    Returns:
        Markdown naming the specific migration file(s) and the release each one entered in when
        that differs from `tag` (#1893), stating plainly that this comment is audit-only (nothing
        watches replies here), and giving the exact manual unblock command.
    """
    lines = [
        f"### :warning: `release-risk-check` flagged `{tag}`",
        "",
    ]
    if verdict.carried_migration_files:
        origin = verdict.carried_introduced_tag or "an earlier release"
        lines.append(
            f"The automatic `deploy` job was **skipped**: production still has an un-applied Flyway "
            f"migration, which is one-way against production data (rolling the image back does not "
            f"roll back applied DDL). Diffed against `{base_tag}` — the last release before this "
            f"still-open hold began (**DEGRADED**: `/api/app-info` was unreadable this run, so the "
            f"deployed tag itself is unconfirmed). The migration(s) below were introduced in "
            f"`{origin}`, **not** `{tag}` — this hold is INHERITED, carried "
            f"forward because it was never manually deployed, not newly added by this release."
        )
    elif primary_path_used:
        lines.append(
            f"The automatic `deploy` job was **skipped**: this release adds a Flyway migration, which "
            f"is one-way against production data (rolling the image back does not roll back applied "
            f"DDL). Diffed against `{base_tag}` — what production is actually running, read from "
            f"`GET /api/app-info`."
        )
    else:
        lines.append(
            f"The automatic `deploy` job was **skipped**: this release adds a Flyway migration, which "
            f"is one-way against production data (rolling the image back does not roll back applied "
            f"DDL). Diffed against `{base_tag}` — **DEGRADED**: `GET /api/app-info` was unreadable "
            f"this run, so this is the previous release tag instead (`gh release list`, sorted "
            f"explicitly by `createdAt` — never `.last_good_tag`, which is VPS-local and unreachable "
            f"from the Actions runner)."
        )
    lines.extend(["", f"**Migration file(s) still pending ({len(verdict.migration_files)}):**"])
    lines.extend(f"- `{path}`" for path in verdict.migration_files)
    audit_only_note = (
        "**This comment is audit / notification only.** Nothing in this repo watches replies on "
        "a release-please PR — it carries no `agent:ready`/`needs-human` flow label, and "
        "`tick.sh` never reads this thread. There is no automated unblock."
    )
    lines.extend(
        [
            "",
            audit_only_note,
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
    """One place every `gh` invocation goes through, so tests mock exactly here.

    A hung `gh` (`TimeoutExpired` at `GH_TIMEOUT_SECONDS`) or a missing/unrunnable binary
    (`OSError`) is reported as a non-zero `CompletedProcess`, never raised: every caller here
    already degrades a non-zero exit to "unreadable, fail open", and an escaping exception would do
    the opposite — a traceback exits non-zero, the job goes red, and `deploy`'s `needs:` skips a
    release that was never flagged. That is exactly the single point of failure this script's
    fail-open posture exists to avoid.

    Args:
        args: The full `gh` argv.
        input_text: Optional stdin payload.

    Returns:
        The completed process, or a synthetic failed one carrying the error text on stderr.
    """
    try:
        return subprocess.run(
            args,
            capture_output=True,
            text=True,
            input=input_text,
            timeout=GH_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(
            args=args, returncode=124, stdout="", stderr=f"timed out after {GH_TIMEOUT_SECONDS}s"
        )
    except OSError as exc:
        return subprocess.CompletedProcess(args=args, returncode=127, stdout="", stderr=str(exc))


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


def fetch_commit_files(repo: str, sha: str) -> list[dict] | None:
    """`GET /repos/{repo}/commits/{sha}` — one commit's own file list (with per-file status).

    Args:
        repo: `owner/name`.
        sha: The commit to read.

    Returns:
        The commit's `files` array, or `None` when the call or its shape is unreadable.
    """
    data = _gh_json(["gh", "api", f"repos/{repo}/commits/{sha}"])
    if not isinstance(data, dict):
        return None
    files = data.get("files")
    return files if isinstance(files, list) else None


def collect_migration_files(repo: str, compare: dict) -> list[str]:
    """Added migration paths in the range, working around the compare API's 300-file cap.

    Below the cap the compare payload is complete, so this is just `added_migration_files`. AT the
    cap the file list is truncated with no flag and no pagination (see `COMPARE_FILES_CAP`), so the
    range's commits are walked individually — a real release range carries ~20 commits, so this
    costs a handful of extra reads in the rare case it runs at all, and nothing in the common one.

    Args:
        repo: `owner/name`.
        compare: The compare-API payload (`files`, `commits`, `total_commits`).

    Returns:
        Sorted, de-duplicated migration paths added anywhere in the range.
    """
    files = compare.get("files") or []
    found = set(added_migration_files(files))
    if len(files) < COMPARE_FILES_CAP:
        return sorted(found)

    commits = compare.get("commits") or []
    print(
        f"::warning title=release-risk-check compare truncated::the compare API returned "
        f"{len(files)} files (its hard cap) — walking {len(commits)} commit(s) individually so a "
        "migration cannot hide past the cap."
    )
    total_commits = compare.get("total_commits")
    if isinstance(total_commits, int) and total_commits > len(commits):
        print(
            f"::warning title=release-risk-check commit list truncated::the compare API returned "
            f"{len(commits)} of {total_commits} commits — files touched only by the commits beyond "
            "that are not covered by this walk."
        )
    for commit in commits:
        sha = commit.get("sha") if isinstance(commit, dict) else None
        if not sha:
            continue
        commit_files = fetch_commit_files(repo, sha)
        if commit_files is None:
            print(
                f"::warning title=release-risk-check commit unreadable::could not read {sha}; its "
                "files are not covered by the migration check."
            )
            continue
        found.update(added_migration_files(commit_files))
    return sorted(found)


def fetch_deployed_version(app_url: str) -> str | None:
    """`GET {app_url}/api/app-info` — the version production is ACTUALLY running (#1859).

    Public, unauthenticated, `no-store` (docs/DEPLOYMENT.md, #1527) — no `gh` CLI involved, and safe
    to poll fresh every run. Not routed through `_run_gh`/`_gh_json`: this is a plain HTTP GET
    against the app itself, not the GitHub API.

    Args:
        app_url: The app's public base URL (`PUBLIC_BASE_URL`). Falsy is treated the same as any
            other unreadable case.

    Returns:
        The bare version string (e.g. `"0.172.1"`), or `None` on any failure — empty `app_url`,
        timeout, connection error, non-2xx, or an unreadable/`"unknown"` body. This is the one read
        in the whole script that talks to something other than GitHub, so it fails open the same
        way every `gh` read here does: unreadable degrades to the old tag-to-tag comparison, never
        to blocking the release.
    """
    if not app_url:
        return None
    url = app_url.rstrip("/") + "/api/app-info"
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "release-risk-check"})
        with urllib.request.urlopen(request, timeout=APP_INFO_TIMEOUT_SECONDS) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError, ValueError) as exc:
        print(f"::warning title=release-risk-check app-info unreadable::GET {url}: {exc}")
        return None
    detail = payload.get("detail") if isinstance(payload, dict) else None
    version = detail.get("version") if isinstance(detail, dict) else None
    if not isinstance(version, str) or not version or version == "unknown":
        print(f"::warning title=release-risk-check app-info unreadable::GET {url} returned no usable version")
        return None
    return version


def resolve_carried_migration_files(
    repo: str, releases: list[dict], previous_tag: str
) -> tuple[tuple[str, ...], str | None]:
    """Walk back through `previous_tag`'s own release history to find a still-open hold (#1896).

    Only reached on the degraded, tag-to-tag fallback path — a direct `/api/app-info` read already
    covers a hold spanning any number of releases by diffing straight from what's actually
    deployed; this is the approximation for when that read is unavailable. A single hop back
    (checking only whether `previous_tag` itself added a migration over its own previous release)
    is not enough once the hold's origin is MORE than one release behind `previous_tag`: an
    inheriting release's own tag-to-tag range is clean by definition — it never introduced anything,
    it only carried the hold forward — so a one-hop check reads that as nothing to carry. Measured
    on the real sequence that produced #1896: v0.172.6 introduced a migration (correctly flagged);
    v0.172.7 correctly carried it one hop back; v0.172.8's own one-hop check landed on v0.172.7's
    OWN clean range and lost the hold entirely, because the migration had actually entered two
    releases earlier. So this walks back release by release — following `resolve_previous_release`
    the same way `previous_tag` itself was found — until it reaches the release whose own diff
    actually introduces a migration (the hold's real origin), or runs out of earlier releases.

    Args:
        repo: `owner/name`.
        releases: The already-fetched `gh release list` rows (avoids a second network round trip).
        previous_tag: The release this run would otherwise have diffed against.

    Returns:
        `(carried_files, introduced_tag)` — the sorted migration paths from the pairwise diff that
        first introduces them walking backward from `previous_tag`, and that diff's newer tag (the
        hold's actual origin, which may be `previous_tag` itself or several releases further back).
        `((), None)` when there is no earlier release to check, or a read anywhere along the walk is
        unreadable — fails open the same as everywhere else in this script, discarding whatever was
        found before the failure rather than reporting a partial hold.
    """
    current = previous_tag
    while True:
        earlier = resolve_previous_release(releases, current)
        if earlier is None:
            return (), None
        compare = fetch_compare(repo, earlier, current)
        if compare is None:
            return (), None
        own_files = collect_migration_files(repo, compare)
        if own_files:
            return tuple(sorted(own_files)), current
        current = earlier


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
    ap.add_argument(
        "--app-url",
        default=os.getenv("PUBLIC_BASE_URL", ""),
        help="Base URL to read /api/app-info from. Empty skips straight to the tag-to-tag fallback.",
    )
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

    # Diff base (#1859): prefer what production is actually running over the previous *tag* — a
    # flagged release that is correctly skipped otherwise leaves no trace in the NEXT release's own
    # tag-to-tag range, and that next release auto-deploys the held migration anyway.
    base_tag = previous_tag
    carried_migration_files: tuple[str, ...] = ()
    deployed_version = fetch_deployed_version(args.app_url)
    compare = None
    primary_path_used = False
    if deployed_version is not None:
        deployed_tag = version_to_tag(deployed_version)
        compare = fetch_compare(args.repo, deployed_tag, args.tag)
        if compare is not None:
            base_tag = deployed_tag
            primary_path_used = True
        else:
            print(
                f"::warning title=release-risk-check degraded::could not diff deployed "
                f"{deployed_tag}...{args.tag}; falling back to the previous release tag."
            )

    # Only set when the fallback below finds a still-open hold: `carried_origin_tag` is the release
    # that actually introduced it (#1896: possibly several hops behind `previous_tag`, not
    # necessarily `previous_tag` itself), and `carried_base_tag` (its own previous release, per
    # `resolve_previous_release`) is the last point BEFORE that hold began — the honest "diffed
    # against" answer for those file(s) once `/api/app-info` can no longer say so directly (#1893).
    carried_introduced_tag: str | None = None
    if compare is None:
        # `/api/app-info` was unreadable, or its answer couldn't be diffed — degrade to the old
        # tag-to-tag comparison, but don't let a still-open hold on an earlier release evaporate
        # just because THIS release's own range looks clean.
        carried_migration_files, carried_origin_tag = resolve_carried_migration_files(
            args.repo, releases, previous_tag
        )
        carried_base_tag = (
            resolve_previous_release(releases, carried_origin_tag) if carried_origin_tag else None
        )
        compare = fetch_compare(args.repo, previous_tag, args.tag)
        # Only swap `base_tag` once THIS release's own diff (against `previous_tag`) actually came
        # back — otherwise the "could not diff" fallback-failure message below would name
        # `carried_base_tag` for a comparison that was never attempted against it (#1893 was exactly
        # this class of bug: a message naming the wrong bound for what was actually diffed).
        if compare is not None and carried_migration_files:
            carried_introduced_tag = carried_origin_tag
            if carried_base_tag is not None:
                base_tag = carried_base_tag

    if compare is None:
        print(
            f"::warning title=release-risk-check degraded::could not diff {base_tag}...{args.tag}; "
            "deploying unflagged (fail open)."
        )
        write_github_outputs({"flagged": "false"})
        return 0

    migration_files = collect_migration_files(args.repo, compare)

    verdict = merge_carried_hold(
        decide(migration_files=migration_files), carried_migration_files, carried_introduced_tag
    )
    write_github_outputs(
        {"flagged": "true" if verdict.flagged else "false", "previous_tag": previous_tag}
    )

    path_desc = describe_comparison_base(base_tag, primary_path_used=primary_path_used)

    if not verdict.flagged:
        print(f"PASS: no migration files added — diffed against {path_desc} (this release: {args.tag}).")
        return 0

    reason = summarize(verdict)
    if verdict.carried_migration_files:
        print(f"FLAGGED: {reason}. Diffed against {path_desc} (this release: {args.tag}).")
    else:
        print(f"FLAGGED: {reason} — diffed against {path_desc} (this release: {args.tag}).")
    print(
        f"::warning title=Release risk flagged::{reason} — automatic deploy of {args.tag} skipped. "
        f"The owner must run 'gh workflow run deploy-vps.yml -f tag={args.tag}' to ship it manually."
    )

    if args.no_comment:
        return 0

    pr_number = fetch_release_pr_number(args.repo, args.tag)
    body = format_decision_comment(args.tag, base_tag, verdict, primary_path_used=primary_path_used)
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
