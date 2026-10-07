#!/usr/bin/env python3
"""When the media benchmark runs: monthly, on a model-config change, on open PRs (issue #2251).

Called once per run of the host orchestrator (`scripts/weekly_model_check.sh`). It is not a GitHub
workflow, on purpose: a real run spends OpenAI money with a key that lives only in `/opt/lem/.env`,
and the pipeline's credential has neither secrets nor workflow scope
(docs/contribution-security.md). It is also never a required check - its only voice on a PR is an
advisory comment.

Three triggers, decided by ``plan_triggers`` from a small state file:

* **monthly** - the first orchestrator run in a new calendar month benchmarks main's roster;
* **main changed** - `.litellm/config.yaml` or `.litellm/model_upgrades.yaml` has a different blob on
  `origin/main` than at the last main run;
* **open PR** - an open PR that touches either file and has a head SHA not benchmarked yet. Only an
  upstream branch (never a fork) by an OWNER/MEMBER/COLLABORATOR qualifies, and only the PR's CONFIG
  is taken from its head: prices and the provider snapshot come from main, so a PR can never lower
  the spend bound of the run that measures it. An unreadable PR listing runs no PR benchmark
  (fails closed). At most ``MODEL_EVAL_MAX_PR_RUNS`` (default 2) per orchestrator run.

The orchestrator runs weekly, so a PR is picked up within a week; add a daily cron line for this
script alone if that is too slow (docs/model-benchmarks/README.md, "Schedule").

Prints the main run's results-file path on stdout (the orchestrator renders it into a docs PR).
Exit: 0 nothing ran / nothing to publish, 2 a main run produced results, 1 a benchmark errored.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from typing import Optional

_HERE = os.path.dirname(os.path.abspath(__file__))

WATCHED_PATHS = (".litellm/config.yaml", ".litellm/model_upgrades.yaml")
TRUSTED_ASSOCIATIONS = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})
DEFAULT_REPO = "christopherqueenconsulting/linkedin_engagement_manager"
COMMENT_MARKER = "<!-- lem-model-eval -->"
GIT_TIMEOUT = 120
BENCH_TIMEOUT = 3600


def max_pr_runs() -> int:
    """``MODEL_EVAL_MAX_PR_RUNS`` (default 2, 0 disables PR runs)."""
    raw = (os.environ.get("MODEL_EVAL_MAX_PR_RUNS") or "").strip()
    try:
        return max(0, int(raw)) if raw else 2
    except ValueError:
        return 2


# ─────────────────────────────── pure ───────────────────────────────

def plan_triggers(state: dict, *, month: str, main_blobs: dict, prs: Optional[list],
                  limit: int) -> dict:
    """Decide which benchmark runs are due.

    Args:
        state: The saved state (``{"month", "main_blobs", "prs": {number: sha}}``).
        month: ``YYYY-MM`` of this run.
        main_blobs: ``{path: blob sha}`` of the watched files on origin/main (None = absent).
        prs: Open PRs as ``{number, sha, association, cross_repo, files}``, or None when the listing
            could not be read.
        limit: Maximum PR runs.

    Returns:
        ``{"main": [reason, ...], "prs": [{number, sha}], "skipped": [str]}``.
    """
    reasons = []
    if state.get("month") != month:
        reasons.append("monthly")
    previous = state.get("main_blobs")
    if previous and previous != main_blobs:
        changed = sorted(p for p in set(previous) | set(main_blobs)
                         if previous.get(p) != main_blobs.get(p))
        reasons.append("changed on main: " + ", ".join(changed))
    done = {str(k): v for k, v in (state.get("prs") or {}).items()}
    runs, skipped = [], []
    if prs is None:
        skipped.append("open-PR listing unreadable - no PR benchmark this run")
    for pr in prs or []:
        number, sha = pr.get("number"), pr.get("sha")
        if not set(pr.get("files") or []) & set(WATCHED_PATHS):
            continue
        if done.get(str(number)) == sha:
            continue
        if pr.get("cross_repo"):
            skipped.append(f"#{number}: fork PR - never benchmarked with the owner's key")
            continue
        if pr.get("association") not in TRUSTED_ASSOCIATIONS:
            skipped.append(f"#{number}: author association {pr.get('association')!r} not trusted")
            continue
        if len(runs) >= limit:
            skipped.append(f"#{number}: over the per-run PR cap ({limit})")
            continue
        runs.append({"number": number, "sha": sha})
    return {"main": reasons, "prs": runs, "skipped": skipped}


def next_state(state: dict, *, month: str, main_blobs: dict, ran_main: bool,
               done_prs: dict, open_numbers: Optional[set]) -> dict:
    """The state to save after this run.

    The month and blobs only advance when the main run actually completed, so a failed or skipped
    run is retried next time rather than silently marked done. PR entries for closed PRs are pruned.
    """
    out = {"month": state.get("month"), "main_blobs": state.get("main_blobs"),
           "prs": {str(k): v for k, v in (state.get("prs") or {}).items()}}
    if ran_main or not out["main_blobs"]:
        out["main_blobs"] = main_blobs
    if ran_main:
        out["month"] = month
    out["prs"].update({str(k): v for k, v in done_prs.items()})
    if open_numbers is not None:
        out["prs"] = {k: v for k, v in out["prs"].items() if int(k) in open_numbers}
    return out


def pr_comment_body(number: int, sha: str, rc: int, report: Optional[str], output: str) -> str:
    """The advisory PR comment for one benchmark of a PR's config."""
    if rc == 1:
        status = "**did not run to a verdict** (refused or errored)"
    else:
        status = ("**recommends a swap**" if rc == 2 else "ran - no swap recommended")
    lines = [COMMENT_MARKER,
             f"### Media-tier benchmark for `{sha[:10]}` - {status}", "",
             ("Advisory only (issue #2251): this is NOT a check and never blocks a merge. The "
              "PR's `.litellm/config.yaml` was benchmarked on `lem-vision` / `lem-image` against "
              "main's pinned prices and spend cap."), ""]
    if report:
        lines += ["<details><summary>Report</summary>", "", report.strip(), "", "</details>"]
    tail = "\n".join(output.strip().splitlines()[-25:])
    if tail:
        lines += ["", "<details><summary>Run output</summary>", "", "```", tail, "```",
                  "", "</details>"]
    return "\n".join(lines)


# ─────────────────────────────── I/O ───────────────────────────────

def _run(args: list, cwd: str, timeout: int = GIT_TIMEOUT) -> subprocess.CompletedProcess:
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=timeout)


def git_blob(repo_dir: str, ref: str, path: str) -> Optional[str]:
    """The blob sha of ``path`` at ``ref``, or None when it does not exist there."""
    result = _run(["git", "rev-parse", "--verify", "--quiet", f"{ref}:{path}"], repo_dir)
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def git_show(repo_dir: str, ref: str, path: str) -> Optional[str]:
    """File content at ``ref``, or None."""
    result = _run(["git", "show", f"{ref}:{path}"], repo_dir)
    return result.stdout if result.returncode == 0 else None


def list_open_prs(repo: str) -> Optional[list]:
    """Open PRs with author association, fork flag, head SHA and changed files; None if unreadable.

    Read through ``gh api`` (the cron host is already authenticated); any failure returns None,
    which ``plan_triggers`` treats as "run no PR benchmark" - never as "no PRs".
    """
    listing = subprocess.run(["gh", "api", f"repos/{repo}/pulls?state=open&per_page=100"],
                             capture_output=True, text=True, timeout=GIT_TIMEOUT)
    if listing.returncode != 0:
        print(f"  ! gh api pulls failed: {listing.stderr.strip()[:200]}", file=sys.stderr)
        return None
    try:
        pulls = json.loads(listing.stdout or "[]")
    except ValueError:
        return None
    out = []
    for pull in pulls:
        number = pull.get("number")
        files = subprocess.run(["gh", "api", f"repos/{repo}/pulls/{number}/files?per_page=100",
                                "--jq", ".[].filename"],
                               capture_output=True, text=True, timeout=GIT_TIMEOUT)
        if files.returncode != 0:
            print(f"  ! could not list files of #{number}", file=sys.stderr)
            return None
        head_repo = ((pull.get("head") or {}).get("repo") or {}).get("full_name")
        out.append({"number": number, "sha": (pull.get("head") or {}).get("sha"),
                    "association": pull.get("author_association"),
                    "cross_repo": head_repo != repo,
                    "files": [f for f in files.stdout.splitlines() if f.strip()]})
    return out


def run_benchmark(repo_dir: str, *, config: str, results: str, out_dir: str) -> tuple:
    """One media benchmark subprocess; returns ``(rc, combined output)``."""
    python = sys.executable
    cmd = [python, os.path.join(_HERE, "benchmark_models.py"), "--run",
           "--tiers", "lem-vision,lem-image", "--config", config,
           "--prices", os.path.join(repo_dir, ".litellm", "model_prices_snapshot.json"),
           "--provider-snapshot", os.path.join(repo_dir, ".litellm",
                                               "provider_models_snapshot.json"),
           "--results-out", results, "--out-dir", out_dir]
    try:
        done = subprocess.run(cmd, cwd=repo_dir, capture_output=True, text=True,
                              timeout=BENCH_TIMEOUT)
    except subprocess.TimeoutExpired:
        return 1, "benchmark timed out"
    return done.returncode, (done.stdout or "") + (done.stderr or "")


def _newest_report(out_dir: str) -> Optional[str]:
    reports = sorted(p for p in glob.glob(os.path.join(out_dir, "*.md"))
                     if not p.endswith("README.md"))
    if not reports:
        return None
    with open(reports[-1]) as fh:
        return fh.read()


def main(argv: Optional[list] = None) -> int:
    """CLI entry point; see the module docstring."""
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--state", required=True)
    ap.add_argument("--results-dir", required=True)
    ap.add_argument("--repo-dir", default=os.path.dirname(_HERE))
    ap.add_argument("--repo", default=DEFAULT_REPO)
    ap.add_argument("--month", default=None)
    ap.add_argument("--plan-only", action="store_true")
    args = ap.parse_args(argv)

    month = args.month or datetime.now(timezone.utc).strftime("%Y-%m")
    state = {}
    if os.path.exists(args.state):
        try:
            with open(args.state) as fh:
                state = json.load(fh)
        except ValueError:
            state = {}
    _run(["git", "fetch", "-q", "origin", "main"], args.repo_dir)
    main_blobs = {p: git_blob(args.repo_dir, "origin/main", p) for p in WATCHED_PATHS}
    prs = list_open_prs(args.repo)
    plan = plan_triggers(state, month=month, main_blobs=main_blobs, prs=prs, limit=max_pr_runs())
    print(f"model-eval plan: main={plan['main'] or 'not due'} "
          f"prs={[p['number'] for p in plan['prs']]}", file=sys.stderr)
    for note in plan["skipped"]:
        print(f"  skipped {note}", file=sys.stderr)
    if args.plan_only:
        print(json.dumps(plan))
        return 0

    os.makedirs(args.results_dir, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    errored = ran_main = False
    published = None
    if plan["main"]:
        with tempfile.TemporaryDirectory() as tmp:
            config = os.path.join(tmp, "config.yaml")
            with open(config, "w") as fh:
                fh.write(git_show(args.repo_dir, "origin/main", WATCHED_PATHS[0]) or "")
            results = os.path.join(args.results_dir, f"media-benchmark-{stamp}.json")
            rc, output = run_benchmark(args.repo_dir, config=config, results=results,
                                       out_dir=os.path.join(tmp, "report"))
        print(output, file=sys.stderr)
        if rc not in (0, 2):
            errored = True
        elif os.path.exists(results):
            # Only a run that MEASURED something advances the month: a skipped run (no key,
            # BENCHMARK_ENABLED unset) is retried next time rather than marked done.
            ran_main = True
            published = results

    done_prs = {}
    for pr in plan["prs"]:
        _run(["git", "fetch", "-q", "origin", f"pull/{pr['number']}/head"], args.repo_dir)
        content = git_show(args.repo_dir, pr["sha"], WATCHED_PATHS[0])
        if content is None:
            print(f"  ! could not read #{pr['number']}'s config at {pr['sha']}", file=sys.stderr)
            continue
        with tempfile.TemporaryDirectory() as tmp:
            config = os.path.join(tmp, "config.yaml")
            with open(config, "w") as fh:
                fh.write(content)
            out_dir = os.path.join(tmp, "report")
            rc, output = run_benchmark(args.repo_dir, config=config,
                                       results=os.path.join(tmp, "results.json"), out_dir=out_dir)
            if rc == 0 and "nothing to do" in output:
                continue  # BENCHMARK_ENABLED unset - nothing measured, nothing to say
            body = pr_comment_body(pr["number"], pr["sha"], rc, _newest_report(out_dir), output)
            body_path = os.path.join(tmp, "comment.md")
            with open(body_path, "w") as fh:
                fh.write(body)
            posted = subprocess.run(["gh", "pr", "comment", str(pr["number"]), "--repo",
                                     args.repo, "--body-file", body_path],
                                    capture_output=True, text=True, timeout=GIT_TIMEOUT)
        if posted.returncode == 0:
            done_prs[pr["number"]] = pr["sha"]
        else:
            print(f"  ! comment on #{pr['number']} failed: {posted.stderr.strip()[:200]}",
                  file=sys.stderr)
        errored = errored or rc not in (0, 2)

    open_numbers = {p["number"] for p in prs} if prs is not None else None
    with open(args.state, "w") as fh:
        json.dump(next_state(state, month=month, main_blobs=main_blobs, ran_main=ran_main,
                             done_prs=done_prs, open_numbers=open_numbers), fh, indent=2)
    if published:
        print(published)
    if errored:
        return 1
    return 2 if published else 0


if __name__ == "__main__":
    sys.exit(main())
