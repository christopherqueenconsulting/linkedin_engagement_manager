"""Every Actions cache save in `.github/` must be scoped to `main`, or it can never be read back.

An Actions cache entry is readable only from the ref that wrote it, plus the default branch. So a
`save` that runs from a PR ref (`refs/pull/<n>/merge`) or a merge-queue ref
(`gh-readonly-queue/main/pr-<n>-<sha>`, deleted the instant its entry merges) is write-only by
construction. Two separate caches in this repo learned that the hard way:

* `.github/actions/poetry_setup/action.yml` — 17 live venv entries at ~352 MB, ~6 GB of the 10 GB
  budget, none readable again. Fixed by gating the save on `github.ref == 'refs/heads/main'`.
* `.github/workflows/ui-build.yml` — setup-node's built-in `cache: npm` saved from every ref.
  Measured 2026-09-02, before PR #1914: 236 `node-cache-Linux-x64-npm-*` entries, 16.09 GB (119 on
  `refs/pull/N`, 117 on `gh-readonly-queue/main/pr-N`, zero on `refs/heads/main`) against only 2
  distinct `package-lock.json` hashes. Total usage was 17.96 GB against the 10 GB budget, so GitHub
  was evicting LRU — and the entry it evicted was `main`'s venv cache, the one every branch CAN
  read. That is the documented cause of poetry_setup's "cache hit that still cost 19-29s". Accrual
  rate: 108 npm entries / ~7.3 GB in a single day, so an unguarded save refills the budget in under
  two days.

Both fixes were conventions living in YAML comments. Nothing failed the build if a third cache
landed unguarded, or if `ui-build.yml` went back to the one-line `cache: npm` — and the failure
mode is silent and slow (a budget creeping up over days, then someone else's cache going missing).
This is the standing guard (#1915), in the shape of `test_codecov_upload_contract.py`: three
contracts, each asserted on the real tree and each positive-controlled against a deliberately bad
fixture, so a helper that stops matching fails here instead of passing everything.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[2]

_SAVE_ACTION = "actions/cache/save"
_BUILTIN_CACHE_ACTIONS = ("actions/setup-node", "actions/setup-python")
_MAIN_ONLY_GUARD = "github.ref == 'refs/heads/main'"
# Whitespace- and quote-tolerant: `github.ref=="refs/heads/main"` is the same guard to Actions.
_MAIN_ONLY_GUARD_RE = re.compile(r"github\.ref\s*==\s*['\"]refs/heads/main['\"]")
_RESTORE_SAVE_SPLIT = ".github/actions/poetry_setup/action.yml"

# The two saves this guard was written against. A scan that finds fewer than these has gone blind.
_KNOWN_SAVES = frozenset(
    {
        ".github/actions/poetry_setup/action.yml: Save Poetry virtual environment",
        ".github/workflows/ui-build.yml: Save npm cache",
    }
)


def _load(path: Path) -> dict[str, Any]:
    """Parse one workflow or composite-action file.

    Args:
        path: The YAML file to read.

    Returns:
        The parsed document, or an empty dict for an empty file.
    """
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _triggers(workflow: dict[str, Any]) -> dict[str, Any]:
    """Return a workflow's trigger block.

    `on:` is a YAML 1.1 boolean, so PyYAML parses the key as `True` — reading `workflow["on"]`
    finds nothing and would make every trigger assertion below vacuously true.

    Args:
        workflow: The parsed workflow document.

    Returns:
        The trigger block keyed by event name; a bare-list or scalar `on:` becomes keys with
        `None` values.
    """
    raw = workflow.get("on", workflow.get(True)) or {}
    return raw if isinstance(raw, dict) else dict.fromkeys(raw if isinstance(raw, list) else [raw])


def _push_reaches_main(triggers: dict[str, Any]) -> bool:
    """Whether a `push` trigger would fire on a push to `main`.

    A bare `push:` runs on every branch, `main` included. An explicit `branches:` list must name
    `main`, and a `branches-ignore:` list must not — either filter that excludes `main` leaves the
    workflow with no run that can write a readable cache entry.

    Args:
        triggers: The trigger block from `_triggers`.

    Returns:
        True if a push to `main` runs this workflow.
    """
    if "push" not in triggers:
        return False
    push = triggers["push"]
    if not isinstance(push, dict):
        return True
    branches = push.get("branches")
    if branches is not None and "main" not in branches:
        return False
    return "main" not in (push.get("branches-ignore") or [])


def _steps(document: dict[str, Any]) -> list[dict[str, Any]]:
    """Every step in a workflow or a composite action, in file order.

    A composite action's steps live at `runs.steps`; a workflow's live under each job at
    `jobs.<name>.steps`. Both shapes are walked, so a save step is caught whichever kind of file
    it lands in.

    Args:
        document: The parsed workflow or composite-action document.

    Returns:
        A flat list of step mappings.
    """
    runs = document.get("runs")
    if isinstance(runs, dict):
        return [step for step in runs.get("steps") or [] if isinstance(step, dict)]
    steps: list[dict[str, Any]] = []
    for job in (document.get("jobs") or {}).values():
        if isinstance(job, dict):
            steps.extend(step for step in job.get("steps") or [] if isinstance(step, dict))
    return steps


def _uses(step: dict[str, Any]) -> str:
    """The action a step runs, or an empty string for a `run:` step.

    Args:
        step: One step mapping.

    Returns:
        The `uses:` value as a string.
    """
    return str(step.get("uses") or "")


def _label(root: Path, path: Path, step: dict[str, Any]) -> str:
    """Name a step the way a failure message must: root-relative file, then step name.

    Root-relative because every composite action is an `action.yml` — the bare filename cannot say
    which one.

    Args:
        root: The repository root the path is reported against.
        path: The file the step lives in.
        step: The step mapping.

    Returns:
        `<relative path>: <step name>`, with a placeholder for an unnamed step.
    """
    return f"{path.relative_to(root).as_posix()}: {step.get('name') or '<unnamed step>'}"


def _workflow_files(root: Path) -> list[Path]:
    """Every workflow file GitHub would run, both spellings of the extension.

    Actions reads `.yml` AND `.yaml`, so scanning one spelling would let a save added under the
    other write from every ref outside this guard's field of view.

    Args:
        root: The repository root to scan.

    Returns:
        Sorted workflow paths.
    """
    directory = root / ".github" / "workflows"
    return sorted(set(directory.glob("*.yml")) | set(directory.glob("*.yaml")))


def _action_files(root: Path) -> list[Path]:
    """Every local composite action, both spellings of the extension.

    Args:
        root: The repository root to scan.

    Returns:
        Sorted `action.yml` / `action.yaml` paths under `.github/actions/*/`.
    """
    directory = root / ".github" / "actions"
    return sorted(set(directory.glob("*/action.yml")) | set(directory.glob("*/action.yaml")))


def _ci_files(root: Path = _ROOT) -> list[Path]:
    """Every file a cache step can live in: workflows plus composite actions.

    Args:
        root: The repository root to scan. Defaults to this repo's own.

    Returns:
        Sorted paths.
    """
    return _workflow_files(root) + _action_files(root)


def _save_steps(root: Path = _ROOT) -> list[str]:
    """Label every `actions/cache/save*` step in the tree.

    This is the anti-vacuity probe: the three contracts below all pass trivially on a scan that
    matches nothing, so the real tree is first shown to contain the saves the guard was written
    against.

    Args:
        root: The repository root to scan. Defaults to this repo's own.

    Returns:
        One `_label` per save step.
    """
    return [
        _label(root, path, step)
        for path in _ci_files(root)
        for step in _steps(_load(path))
        if _uses(step).startswith(_SAVE_ACTION)
    ]


def _unguarded_saves(root: Path = _ROOT) -> list[str]:
    """Label every `actions/cache/save*` step whose `if:` does not restrict it to `main`.

    Two ways to fail: the guard is absent, or it is present but OR-ed with something (`||`) — an
    OR widens the set of refs that write, which is precisely the failure the guard exists to stop.

    Args:
        root: The repository root to scan. Defaults to this repo's own.

    Returns:
        One `_label` per offending step; empty means every save is gated.
    """
    offenders: list[str] = []
    for path in _ci_files(root):
        for step in _steps(_load(path)):
            if not _uses(step).startswith(_SAVE_ACTION):
                continue
            condition = str(step.get("if") or "")
            if "||" in condition or not _MAIN_ONLY_GUARD_RE.search(condition):
                offenders.append(_label(root, path, step))
    return offenders


def _builtin_cache_inputs(root: Path = _ROOT) -> list[str]:
    """Label every setup-node / setup-python step that uses the built-in `cache:` input.

    That all-in-one form saves from every ref that runs it and takes no condition, so it cannot be
    gated — it can only be split into `actions/cache/restore` + a gated `actions/cache/save`.

    Args:
        root: The repository root to scan. Defaults to this repo's own.

    Returns:
        One `_label` per offending step; empty means nothing uses the shorthand.
    """
    offenders: list[str] = []
    for path in _ci_files(root):
        for step in _steps(_load(path)):
            if not _uses(step).startswith(_BUILTIN_CACHE_ACTIONS):
                continue
            if "cache" in (step.get("with") or {}):
                offenders.append(_label(root, path, step))
    return offenders


def _saves_without_a_main_run(root: Path = _ROOT) -> list[str]:
    """Files whose save step has no `push` to `main` that could ever execute it.

    A gated save passes the guard check and still writes nothing if nothing runs it from `main` —
    the other half of #1913. A workflow needs its own `push: branches: [main]`. A composite action
    has no `on:` of its own, so it needs at least one CALLING workflow with that trigger; every
    caller is named in the failure so the reader sees which workflow to give a main run.

    Args:
        root: The repository root to scan. Defaults to this repo's own.

    Returns:
        One root-relative file path per offender, a composite action's suffixed with its callers.
    """
    workflows = {path: _load(path) for path in _workflow_files(root)}
    main_run_callers: set[str] = set()
    all_callers: dict[str, set[str]] = {}
    for path, document in workflows.items():
        for step in _steps(document):
            uses = _uses(step)
            if not uses.startswith("./"):
                continue
            all_callers.setdefault(uses, set()).add(path.name)
            if _push_reaches_main(_triggers(document)):
                main_run_callers.add(uses)

    offenders: list[str] = []
    for path, document in workflows.items():
        has_save = any(_uses(step).startswith(_SAVE_ACTION) for step in _steps(document))
        if has_save and not _push_reaches_main(_triggers(document)):
            offenders.append(path.relative_to(root).as_posix())
    for path in _action_files(root):
        if not any(_uses(step).startswith(_SAVE_ACTION) for step in _steps(_load(path))):
            continue
        uses = "./" + path.parent.relative_to(root).as_posix()
        if uses not in main_run_callers:
            callers = sorted(all_callers.get(uses, ())) or ["<no caller>"]
            offenders.append(f"{path.relative_to(root).as_posix()} (called by {callers})")
    return offenders


def _write(root: Path, relative: str, text: str) -> Path:
    """Write a fixture file under a scratch repo root, creating its directories.

    Args:
        root: The scratch repository root.
        relative: The root-relative path to write.
        text: The file body.

    Returns:
        The written path.
    """
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _save_workflow(*, guard: str | None = _MAIN_ONLY_GUARD, on: str = "  push:\n    branches: [main]\n") -> str:
    """A minimal workflow with one cache save step.

    Args:
        guard: The save step's `if:` expression, or None for an unconditional save.
        on: The indented body of the `on:` block.

    Returns:
        The workflow YAML.
    """
    condition = f"        if: {guard}\n" if guard is not None else ""
    return (
        "name: fixture\n"
        f"on:\n{on}"
        "jobs:\n  lane:\n    steps:\n"
        "      - name: Save widget cache\n"
        f"{condition}"
        "        uses: actions/cache/save@v5\n"
        "        with:\n          path: ~/.widget\n          key: k\n"
    )


def _save_action(*, guard: str | None = _MAIN_ONLY_GUARD) -> str:
    """A minimal composite action with one cache save step.

    Args:
        guard: The save step's `if:` expression, or None for an unconditional save.

    Returns:
        The action YAML.
    """
    condition = f"      if: {guard}\n" if guard is not None else ""
    return (
        "name: widget\nruns:\n  using: composite\n  steps:\n"
        "    - name: Save widget cache\n"
        f"{condition}"
        "      uses: actions/cache/save@v5\n"
        "      with:\n        path: ~/.widget\n        key: k\n"
    )


def _caller_workflow(on: str) -> str:
    """A minimal workflow that calls the `widget` composite action.

    Args:
        on: The indented body of the `on:` block.

    Returns:
        The workflow YAML.
    """
    return f"name: caller\non:\n{on}jobs:\n  lane:\n    steps:\n      - uses: ./.github/actions/widget\n"


class TestTheScanIsNotBlind:
    """Every contract below passes on an empty scan, so the scan is proven first."""

    def test_the_two_saves_this_guard_was_written_against_are_found(self):
        found = set(_save_steps())
        assert _KNOWN_SAVES <= found, (
            f"the scan no longer sees {sorted(_KNOWN_SAVES - found)}. Either that save moved (update "
            "_KNOWN_SAVES in the same commit) or the walk has gone blind, in which case every "
            "contract in this file passes while asserting nothing."
        )

    def test_both_extensions_and_both_file_kinds_are_scanned(self, tmp_path: Path):
        _write(tmp_path, ".github/workflows/a.yml", _save_workflow())
        _write(tmp_path, ".github/workflows/b.yaml", _save_workflow())
        _write(tmp_path, ".github/actions/widget/action.yml", _save_action())
        _write(tmp_path, ".github/actions/gadget/action.yaml", _save_action())

        assert sorted(_save_steps(tmp_path)) == [
            ".github/actions/gadget/action.yaml: Save widget cache",
            ".github/actions/widget/action.yml: Save widget cache",
            ".github/workflows/a.yml: Save widget cache",
            ".github/workflows/b.yaml: Save widget cache",
        ], "GitHub reads both spellings and both file kinds — a save under any of them must be seen"


class TestEverySaveIsMainOnly:
    """Box 1: an `actions/cache/save*` from any ref but `main` is write-only."""

    def test_every_cache_save_step_is_gated_on_main(self):
        offenders = _unguarded_saves()
        assert not offenders, (
            f"cache save step(s) not restricted to main: {offenders}. Add `{_MAIN_ONLY_GUARD}` to "
            "each step's `if:` (AND-ed, never OR-ed). An Actions cache is readable only from the ref "
            "that wrote it plus the default branch, so a save from a PR or merge-queue ref is "
            "write-only and burns the 10 GB budget — the #1913 failure, measured at ~7.3 GB/day."
        )

    def test_an_unguarded_save_in_a_workflow_is_caught(self, tmp_path: Path):
        """Positive control: the helper fails a deliberately unconditional save."""
        _write(tmp_path, ".github/workflows/bad.yml", _save_workflow(guard=None))

        assert _unguarded_saves(tmp_path) == [".github/workflows/bad.yml: Save widget cache"]

    def test_an_unguarded_save_in_a_composite_action_is_caught(self, tmp_path: Path):
        """Positive control: composite actions are where the first leak lived, so they count too."""
        _write(tmp_path, ".github/actions/widget/action.yml", _save_action(guard=None))

        assert _unguarded_saves(tmp_path) == [".github/actions/widget/action.yml: Save widget cache"]

    def test_a_guard_gated_on_something_else_is_caught(self, tmp_path: Path):
        """Positive control: a condition that never names main is not a main-only guard."""
        _write(tmp_path, ".github/workflows/bad.yml", _save_workflow(guard="steps.cache.outputs.cache-hit != 'true'"))

        assert _unguarded_saves(tmp_path) == [".github/workflows/bad.yml: Save widget cache"]

    def test_a_guard_widened_with_or_is_caught(self, tmp_path: Path):
        """Positive control: `main || merge_group` was the original poetry_setup condition and leaked."""
        widened = f"{_MAIN_ONLY_GUARD} || github.event_name == 'merge_group'"
        _write(tmp_path, ".github/workflows/bad.yml", _save_workflow(guard=widened))

        assert _unguarded_saves(tmp_path) == [".github/workflows/bad.yml: Save widget cache"]

    @pytest.mark.parametrize(
        "guard",
        [
            _MAIN_ONLY_GUARD,
            "steps.cache.outputs.cache-hit != 'true' && github.ref == 'refs/heads/main'",
            'github.ref=="refs/heads/main"',
        ],
    )
    def test_a_gated_save_is_not_flagged(self, tmp_path: Path, guard: str):
        """The guard is matched by meaning, so an AND-ed or re-quoted spelling still passes."""
        _write(tmp_path, ".github/workflows/good.yml", _save_workflow(guard=guard))

        assert _unguarded_saves(tmp_path) == []


class TestNoBuiltInSetupCache:
    """Box 2: `cache:` on setup-node / setup-python saves from every ref and cannot be gated."""

    def test_no_setup_action_uses_the_cache_input(self):
        offenders = _builtin_cache_inputs()
        assert not offenders, (
            f"setup-node/setup-python `cache:` input used at {offenders}. That all-in-one form saves "
            "from every ref that runs it and takes no `if:`, so it cannot be scoped to main — split it "
            f"into actions/cache/restore + a gated actions/cache/save the way {_RESTORE_SAVE_SPLIT} "
            "does (and ui-build.yml does for npm since #1914)."
        )

    @pytest.mark.parametrize(
        ("action", "cache"),
        [("actions/setup-node@v7", "npm"), ("actions/setup-python@v6", "poetry")],
    )
    def test_the_cache_input_is_caught(self, tmp_path: Path, action: str, cache: str):
        """Positive control: the one-line shorthand PR #1914 removed is flagged on either action."""
        _write(
            tmp_path,
            ".github/workflows/bad.yml",
            "name: fixture\non:\n  pull_request:\njobs:\n  lane:\n    steps:\n"
            f"      - name: Set up\n        uses: {action}\n        with:\n          cache: {cache}\n",
        )

        assert _builtin_cache_inputs(tmp_path) == [".github/workflows/bad.yml: Set up"]

    def test_a_setup_action_without_the_input_is_not_flagged(self, tmp_path: Path):
        _write(
            tmp_path,
            ".github/workflows/good.yml",
            "name: fixture\non:\n  pull_request:\njobs:\n  lane:\n    steps:\n"
            "      - uses: actions/setup-node@v7\n        with:\n          node-version: '24'\n",
        )

        assert _builtin_cache_inputs(tmp_path) == []


class TestEverySaveHasAMainRun:
    """Box 3: a gated save with no `push` to `main` passes box 1 while caching nothing."""

    def test_every_file_with_a_cache_save_step_gets_a_main_run(self):
        offenders = _saves_without_a_main_run()
        assert not offenders, (
            f"{offenders} contain(s) an actions/cache/save step that nothing runs from main. A "
            "workflow needs `push: branches: [main]` in its `on:`; a composite action needs at least "
            "one calling workflow with it. Without that the main-only guard is satisfied by never "
            "saving at all — the other half of #1913."
        )

    def test_a_pull_request_only_workflow_is_caught(self, tmp_path: Path):
        """Positive control: the guard is present, the run that could satisfy it is not."""
        _write(tmp_path, ".github/workflows/bad.yml", _save_workflow(on="  pull_request:\n"))

        assert _saves_without_a_main_run(tmp_path) == [".github/workflows/bad.yml"]

    @pytest.mark.parametrize(
        "on",
        [
            "  push:\n    branches: [develop]\n",
            "  push:\n    branches-ignore: [main]\n",
        ],
    )
    def test_a_push_filter_that_excludes_main_is_caught(self, tmp_path: Path, on: str):
        """Positive control: `push:` alone is not enough when its branch filter skips main."""
        _write(tmp_path, ".github/workflows/bad.yml", _save_workflow(on=on))

        assert _saves_without_a_main_run(tmp_path) == [".github/workflows/bad.yml"]

    def test_a_composite_action_with_no_main_run_caller_is_caught(self, tmp_path: Path):
        """Positive control: the action's callers are named so the reader knows which to fix."""
        _write(tmp_path, ".github/actions/widget/action.yml", _save_action())
        _write(tmp_path, ".github/workflows/pr.yml", _caller_workflow("  pull_request:\n"))

        assert _saves_without_a_main_run(tmp_path) == [".github/actions/widget/action.yml (called by ['pr.yml'])"]

    def test_a_composite_action_nothing_calls_is_caught(self, tmp_path: Path):
        _write(tmp_path, ".github/actions/widget/action.yml", _save_action())

        assert _saves_without_a_main_run(tmp_path) == [".github/actions/widget/action.yml (called by ['<no caller>'])"]

    def test_one_main_run_caller_is_enough(self, tmp_path: Path):
        _write(tmp_path, ".github/actions/widget/action.yml", _save_action())
        _write(tmp_path, ".github/workflows/pr.yml", _caller_workflow("  pull_request:\n"))
        _write(tmp_path, ".github/workflows/ci.yml", _caller_workflow("  push:\n    branches: [main]\n"))

        assert _saves_without_a_main_run(tmp_path) == []

    @pytest.mark.parametrize(
        "on",
        ["  push:\n", "  push:\n    branches: [main]\n", "  pull_request:\n  push:\n    branches: [main, release/*]\n"],
    )
    def test_a_workflow_that_pushes_to_main_is_not_flagged(self, tmp_path: Path, on: str):
        _write(tmp_path, ".github/workflows/good.yml", _save_workflow(on=on))

        assert _saves_without_a_main_run(tmp_path) == []

    def test_a_scalar_or_list_on_block_is_read_through_the_yaml_boolean_key(self):
        """`on:` parses as the key `True`; every spelling of it must still be readable."""
        assert _push_reaches_main(_triggers(yaml.safe_load("on: push\n"))) is True
        assert _push_reaches_main(_triggers(yaml.safe_load("on: [pull_request, push]\n"))) is True
        assert _push_reaches_main(_triggers(yaml.safe_load("on: pull_request\n"))) is False
        assert _push_reaches_main(_triggers({})) is False
