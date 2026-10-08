#!/usr/bin/env python3
"""Publish a prompt-eval run: one results PR, and one deduplicated issue per failing prompt.

Phase 4 of docs/prompt-evals.md. `.github/workflows/prompt-evals.yml` runs
`scripts/benchmark_prompts.py --run --results-out results.json`, then this script, so the run's
outcome lands where people look:

* **Results PR.** When the run changed the eval state, the reports or the inventory, those paths are
  committed to ONE fixed branch (`bot/prompt-evals`), force-pushed, and its PR is opened if none is
  open. A week whose PR is still unmerged is superseded, never stacked.
* **Failure issues.** Every champion or fallback that missed a floor (`results["failing"]`) gets one
  issue per prompt@version, labelled `prompt-eval:failing`. A hidden marker in the body is the dedup
  key: a re-run comments on the existing issue instead of filing another.

Bodies carry scores, versions and the floors missed — never generated text (public repo).

All git and GitHub I/O goes through one injected runner, so the logic is unit-tested offline.

Usage (from the workflow, with GH_TOKEN set):
    python scripts/prompt_eval_publish.py --results results.json [--repo owner/name] [--no-pr]
"""

from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import sys
from collections.abc import Callable, Sequence
from typing import Any

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
BRANCH = "bot/prompt-evals"
LABEL = "prompt-eval:failing"
RESULT_PATHS = ("docs/prompt-evals", "tests/benchmarks/prompts/eval_state.json")
BOT_NAME = "lem-prompt-evals[bot]"
BOT_EMAIL = "prompt-evals@users.noreply.github.com"

FIX_HINT = ("- Fix: edit the prompt, run `python scripts/prompt_capture.py --write --changed-in \"PR #<fix>\"`, "
            "and the next weekly run re-grades the new version automatically (docs/prompt-evals.md §8).")
KIND_HINT = ("- `prompt-fails` = the tier's champion missed a floor; `fails-on-fallback` = a deployed "
             "fallback did. A prompt-wording change that alters voice or tone is `risk:product-decision`.")

#: Runs one command and returns its stdout. Raises `subprocess.CalledProcessError` on failure.
Runner = Callable[[Sequence[str]], str]


def run_command(args: Sequence[str]) -> str:
    """The real runner: execute in the repo root and return stdout."""
    return subprocess.run(list(args), cwd=REPO_ROOT, check=True, capture_output=True,
                          text=True).stdout


def marker(prompt_id: str, version: Any) -> str:
    """The hidden dedup key of a prompt@version's failure issue."""
    return f"<!-- prompt-eval:{prompt_id}@{version} -->"


def group_failures(failing: list[dict[str, Any]]) -> dict[tuple[str, Any], list[dict[str, Any]]]:
    """Group failures by prompt@version — one issue covers every model that failed it."""
    groups: dict[tuple[str, Any], list[dict[str, Any]]] = {}
    for item in failing:
        groups.setdefault((item["prompt_id"], item["version"]), []).append(item)
    return groups


def render_issue(prompt_id: str, version: Any, failures: list[dict[str, Any]],
                 run: dict[str, Any], run_url: str) -> tuple[str, str]:
    """Return ``(title, body)`` for one prompt@version's failure issue. Scores only — no text."""
    kinds = {f["kind"] for f in failures}
    what = "fails its floors" if "prompt-fails" in kinds else "fails on a fallback model"
    title = f"Prompt eval: {prompt_id}@{version} {what}"
    report = f"docs/prompt-evals/{run['date']}-{run['run_id']}.md"
    lines = [
        marker(prompt_id, version),
        f"`{prompt_id}` at version **{version}** missed a floor in prompt-eval run "
        f"`{run['run_id']}` ({run['date']}).",
        "",
        "| Model | Kind | Floors missed |",
        "|---|---|---|",
    ]
    for f in sorted(failures, key=lambda x: (x["kind"], x["model"])):
        reasons = "; ".join(str(r).replace("|", "\\|") for r in f.get("reasons") or [])
        lines.append(f"| `{f['model']}` | {f['kind']} | {reasons or '—'} |")
    lines += [
        "",
        f"- Report: `{report}` (lands with the results PR on `{BRANCH}`)",
        f"- Run: {run_url}" if run_url else "- Run: (local)",
        FIX_HINT,
        KIND_HINT,
    ]
    return title, "\n".join(lines) + "\n"


def open_failure_issues(gh: Runner, repo: str) -> dict[str, int]:
    """Return ``{marker: issue number}`` for every open `prompt-eval:failing` issue."""
    raw = gh(["gh", "issue", "list", "--repo", repo, "--label", LABEL, "--state", "open",
              "--limit", "200", "--json", "number,body"])
    out: dict[str, int] = {}
    for issue in json.loads(raw or "[]"):
        body = str(issue.get("body") or "")
        start = body.find("<!-- prompt-eval:")
        end = body.find("-->", start) if start >= 0 else -1
        if end >= 0:  # a hand-edited body with a broken marker is ignored, never a crash
            out[body[start:end + 3]] = int(issue["number"])
    return out


def publish_issues(gh: Runner, repo: str, run: dict[str, Any], run_url: str) -> list[str]:
    """File or update one issue per failing prompt@version; returns what was done, per issue."""
    groups = group_failures(run.get("failing") or [])
    if not groups:
        return []
    gh(["gh", "label", "create", LABEL, "--repo", repo, "--color", "B60205", "--force",
        "--description", "A prompt missed its eval floor (docs/prompt-evals.md)"])
    existing = open_failure_issues(gh, repo)
    done = []
    for (pid, version), failures in sorted(groups.items(), key=lambda kv: (kv[0][0], str(kv[0][1]))):
        title, body = render_issue(pid, version, failures, run, run_url)
        number = existing.get(marker(pid, version))
        if number:
            gh(["gh", "issue", "comment", str(number), "--repo", repo, "--body",
                f"Still failing in run `{run['run_id']}` ({run['date']}).\n\n" + body])
            done.append(f"commented #{number} ({pid}@{version})")
        else:
            gh(["gh", "issue", "create", "--repo", repo, "--title", title, "--label", LABEL,
                "--body", body])
            done.append(f"filed {pid}@{version}")
    return done


def changed_result_paths(git: Runner) -> list[str]:
    """Return the result paths the run changed (tracked or new)."""
    raw = git(["git", "status", "--porcelain", "--", *RESULT_PATHS])
    return [line[3:] for line in raw.splitlines() if line.strip()]


def publish_results_pr(git: Runner, gh: Runner, repo: str, run: dict[str, Any],
                       run_url: str) -> str:
    """Commit the run's results to `bot/prompt-evals` and make sure one PR is open for it."""
    if not changed_result_paths(git):
        return "no result changes — no PR"
    git(["git", "checkout", "-B", BRANCH])
    git(["git", "add", "--", *RESULT_PATHS])
    git(["git", "-c", f"user.name={BOT_NAME}", "-c", f"user.email={BOT_EMAIL}", "commit", "-m",
         f"chore(prompt-evals): results of run {run['run_id']} ({run['date']})"])
    git(["git", "push", "--force", "origin", f"HEAD:refs/heads/{BRANCH}"])
    open_prs = json.loads(gh(["gh", "pr", "list", "--repo", repo, "--head", BRANCH, "--state",
                              "open", "--json", "number"]) or "[]")
    if open_prs:
        return f"updated PR #{open_prs[0]['number']}"
    failing = len(group_failures(run.get("failing") or []))
    body = (f"Results of prompt-eval run `{run['run_id']}` ({run['date']}): "
            f"{len(run.get('results') or [])} prompt × model item(s) graded, {failing} "
            f"prompt@version(s) failing a floor (filed as `{LABEL}` issues).\n\n"
            f"Updates the eval state, the run report and the leaderboard under `docs/prompt-evals/`. "
            f"Scores only — no generated text.\n\n{run_url}\n")
    gh(["gh", "pr", "create", "--repo", repo, "--head", BRANCH, "--base", "main", "--title",
        f"chore(prompt-evals): results of run {run['run_id']}", "--body", body])
    return "opened PR"


def main(argv: list[str] | None = None, *, git: Runner = run_command,
         gh: Runner = run_command) -> int:
    """CLI entry point; see the module docstring."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--results", type=pathlib.Path, required=True)
    parser.add_argument("--repo", default="christopherqueenconsulting/linkedin_engagement_manager")
    parser.add_argument("--run-url", default="")
    parser.add_argument("--no-pr", action="store_true", help="file issues only")
    args = parser.parse_args(argv)
    if not args.results.exists():
        sys.stdout.write("no results file — nothing ran, nothing to publish\n")
        return 0
    run = json.loads(args.results.read_text(encoding="utf-8"))
    missing = [k for k in ("run_id", "date") if k not in run]
    if missing:
        sys.stderr.write(f"{args.results} is not a benchmark_prompts.py results file "
                         f"(missing {', '.join(missing)})\n")
        return 1
    if not args.no_pr:
        sys.stdout.write(publish_results_pr(git, gh, args.repo, run, args.run_url) + "\n")
    for line in publish_issues(gh, args.repo, run, args.run_url):
        sys.stdout.write(line + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
