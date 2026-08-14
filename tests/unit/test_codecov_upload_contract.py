"""`codecov.yml` must describe the uploads the workflows actually make.

Two numbers in `codecov.yml` are maintained by hand against `.github/workflows/`, and both fail
silently in a way that reads as a coverage problem rather than a config problem:

* `codecov.notify.after_n_builds` counts **uploads, not lanes** — the unit lane shards across two
  jobs that both upload under the `unit` flag. Set too high, Codecov waits forever for an upload
  that is never coming and posts **no status at all**; set too low, it publishes a number computed
  from a partial set, which reads as a coverage drop that isn't one.
* the `flags:` block. A declared flag with no uploader keeps carrying a stale report forward into
  the project number; an undeclared flag merges in a report nobody scoped.

That is not hypothetical. #1340 was filed on a measured 94.98% -> 86.86% project drop, attributed to
import-time coverage donated by the e2e lane. When that lane was actually deleted (#1215) the drop
did not happen — main reads 95.06% from unit + integration with the `e2e` flag at zero sessions. The
8 points were a property of that PR's upload set, so a whole issue, and the #1338 change it blocked,
came out of the two numbers below drifting from the workflows.

The counts are derived from the workflows rather than restated here, so neither side can be changed
alone.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[2]
_CODECOV = _ROOT / "codecov.yml"
_WORKFLOWS = _ROOT / ".github" / "workflows"

_UPLOAD_ACTION = "codecov/codecov-action"


def _load(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _triggers(workflow: dict[str, Any]) -> dict[str, Any]:
    """Return a workflow's trigger block.

    `on:` is a YAML 1.1 boolean, so PyYAML parses the key as `True` — reading `workflow["on"]`
    finds nothing and would make every assertion below vacuously true.
    """
    raw = workflow.get("on", workflow.get(True)) or {}
    return raw if isinstance(raw, dict) else dict.fromkeys(raw if isinstance(raw, list) else [raw])


def _job_copies(job: dict[str, Any]) -> int:
    """How many times a job runs, i.e. how many uploads one upload step in it produces.

    A matrix job uploads once per combination, minus the combinations `exclude` removes: an
    exclude entry that names every axis drops one combination, and one that names a subset drops
    the product of the axes it leaves unnamed.

    `include` entries that add a new axis rather than refine an existing one are not modelled — no
    workflow here uses one, and the parity assertion would catch it as a mismatch rather than pass
    on a wrong number.
    """
    matrix = (job.get("strategy") or {}).get("matrix")
    if not isinstance(matrix, dict):
        return 1
    axes = {k: v for k, v in matrix.items() if k not in {"include", "exclude"} and isinstance(v, list)}
    if not axes:
        return max(len(matrix.get("include") or []), 1)

    combinations = math.prod(len(axis) for axis in axes.values())
    for entry in matrix.get("exclude") or []:
        if not isinstance(entry, dict):
            continue
        combinations -= math.prod(len(axis) for name, axis in axes.items() if name not in entry)
    return max(combinations, 1)


def _step_flags(step: dict[str, Any]) -> list[str]:
    raw = (step.get("with") or {}).get("flags")
    if not raw:
        return []
    return [flag.strip() for flag in str(raw).split(",") if flag.strip()]


def _workflow_files(directory: Path) -> list[Path]:
    """Every workflow file GitHub would run, both spellings of the extension.

    Actions reads `.yml` AND `.yaml`, so scanning one spelling would let a lane added under the
    other upload a report this guard never counts — silently, which is the failure the guard exists
    to make loud.
    """
    return sorted(set(directory.glob("*.yml")) | set(directory.glob("*.yaml")))


def _pull_request_uploads(directory: Path = _WORKFLOWS) -> tuple[int, set[str], list[str]]:
    """Count the coverage uploads a pull request produces, and the flags they carry.

    Only workflows with a `pull_request` trigger count: the project status is posted on a PR, so a
    lane that never runs there can never satisfy `after_n_builds`.

    Args:
        directory: The workflow directory to scan. Defaults to the repo's own.

    Returns:
        The upload count, the set of flags used, and the workflow files they came from.
    """
    uploads = 0
    flags: set[str] = set()
    sources: list[str] = []

    for path in _workflow_files(directory):
        workflow = _load(path)
        if "pull_request" not in _triggers(workflow):
            continue
        for job in (workflow.get("jobs") or {}).values():
            copies = _job_copies(job)
            for step in job.get("steps") or []:
                if not str(step.get("uses", "")).startswith(_UPLOAD_ACTION):
                    continue
                uploads += copies
                flags.update(_step_flags(step))
                sources.append(path.name)

    return uploads, flags, sources


def _write_uploader(directory: Path, name: str, *, flag: str = "unit") -> None:
    """Write a minimal PR-triggered workflow with one Codecov upload step."""
    directory.joinpath(name).write_text(
        "name: fixture\n"
        "on:\n"
        "  pull_request:\n"
        "    branches: [main]\n"
        "jobs:\n"
        "  lane:\n"
        "    steps:\n"
        f"      - uses: {_UPLOAD_ACTION}@v7\n"
        "        with:\n"
        f"          flags: {flag}\n",
        encoding="utf-8",
    )


class TestTheScanSeesEveryWorkflowGitHubWouldRun:
    """The derivation is only a guard if nothing can upload outside its field of view."""

    def test_both_extensions_are_scanned(self, tmp_path: Path):
        _write_uploader(tmp_path, "a.yml")
        _write_uploader(tmp_path, "b.yaml", flag="integration")

        uploads, flags, sources = _pull_request_uploads(tmp_path)

        assert uploads == 2, "a lane added as .yaml uploads too — GitHub reads both spellings"
        assert flags == {"unit", "integration"}
        assert sorted(sources) == ["a.yml", "b.yaml"]

    def test_a_workflow_without_a_pull_request_trigger_is_ignored(self, tmp_path: Path):
        tmp_path.joinpath("nightly.yml").write_text(
            "name: nightly\non:\n  schedule:\n    - cron: '0 3 * * *'\n"
            "jobs:\n  lane:\n    steps:\n"
            f"      - uses: {_UPLOAD_ACTION}@v7\n        with:\n          flags: slow\n",
            encoding="utf-8",
        )

        assert _pull_request_uploads(tmp_path) == (0, set(), [])


class TestJobCopies:
    """`after_n_builds` counts uploads, so one upload step in a matrix job is several uploads."""

    def test_a_plain_job_uploads_once(self):
        assert _job_copies({"steps": []}) == 1

    def test_a_matrix_multiplies_its_axes(self):
        job = {"strategy": {"matrix": {"shard": [1, 2], "python": ["3.12", "3.13"]}}}
        assert _job_copies(job) == 4

    def test_a_fully_specified_exclude_removes_one_combination(self):
        job = {
            "strategy": {
                "matrix": {
                    "shard": [1, 2],
                    "python": ["3.12", "3.13"],
                    "exclude": [{"shard": 2, "python": "3.13"}],
                }
            }
        }
        assert _job_copies(job) == 3, "an ignored exclude overcounts and blames codecov.yml"

    def test_a_partial_exclude_removes_the_axes_it_leaves_unnamed(self):
        job = {
            "strategy": {
                "matrix": {
                    "shard": [1, 2],
                    "python": ["3.12", "3.13"],
                    "exclude": [{"python": "3.13"}],
                }
            }
        }
        assert _job_copies(job) == 2

    def test_an_include_only_matrix_counts_its_entries(self):
        job = {"strategy": {"matrix": {"include": [{"os": "ubuntu"}, {"os": "macos"}]}}}
        assert _job_copies(job) == 2


class TestAfterNBuildsTracksTheUploads:
    def test_the_declared_count_is_what_a_pull_request_actually_uploads(self):
        uploads, _, sources = _pull_request_uploads()
        declared = _load(_CODECOV)["codecov"]["notify"]["after_n_builds"]
        assert uploads == declared, (
            f"codecov.yml declares after_n_builds: {declared}, but the workflows upload "
            f"{uploads} report(s) on a pull request ({sorted(set(sources))}). Too high and "
            "Codecov posts no status at all; too low and it posts a partial number."
        )

    def test_the_upload_scan_found_something(self):
        """Anti-vacuity: a scan that matched nothing would satisfy any small declared count."""
        uploads, flags, sources = _pull_request_uploads()
        assert uploads >= 2, "expected at least the sharded unit lane plus integration"
        assert sources, "no workflow uploads coverage — the scan is matching nothing"
        assert flags, "uploads carry no flags, so per-lane coverage is unattributable"


class TestFlagsAreDeclaredExactlyOnce:
    def test_every_uploaded_flag_is_declared(self):
        _, used, _ = _pull_request_uploads()
        declared = set(_load(_CODECOV)["flags"])
        assert used <= declared, (
            f"workflows upload under undeclared flag(s) {sorted(used - declared)}; an undeclared "
            "flag merges an unscoped report into the project number"
        )

    def test_every_declared_flag_has_an_uploader(self):
        _, used, _ = _pull_request_uploads()
        declared = set(_load(_CODECOV)["flags"])
        assert declared <= used, (
            f"codecov.yml declares flag(s) {sorted(declared - used)} that nothing uploads; with "
            "carryforward that keeps a stale report alive in the project number — the shape of "
            "the #1340 measurement"
        )

    def test_the_retired_e2e_flag_stays_gone(self):
        """#1215 deleted the lane; leaving its flag declared would carry its last report forever."""
        assert "e2e" not in set(_load(_CODECOV)["flags"])


class TestTheProjectFloorIsNotQuietlyLowered:
    def test_the_enforced_target_is_still_the_ratcheted_floor(self):
        """#1340 proposed cutting this to ~87% on a number that never reproduced without e2e.

        Measured on main at 6a145efa (2026-08-14) from unit + integration alone: project 95.06%.
        #1488 ratcheted the floor 90% -> 93% against that baseline. The floor is a ratchet —
        raising it is the intended direction, lowering it needs a reason this test's failure
        message will ask for.
        """
        project = _load(_CODECOV)["coverage"]["status"]["project"]["default"]
        target = float(str(project["target"]).rstrip("%"))
        assert target >= 93, (
            f"project target dropped to {target}%. The two-lane baseline measured 95.06% with the "
            "e2e lane already deleted, and #1488 ratcheted this floor to 93% against it — so this "
            "is a real cut, not a re-baseline. Lowering it is the owner's call: take it to an "
            "issue and move this floor in the same commit, so the ratchet cannot be walked back "
            "silently."
        )
        assert project["informational"] is False, "the floor only means something if it is enforced"

    def test_the_threshold_cannot_hollow_out_the_floor(self):
        """The number that actually gates is `target - threshold`, not `target`.

        Codecov passes a project status whose coverage is below `target` but within `threshold`, so
        raising the threshold walks the floor back exactly as far as lowering the target would —
        without touching the number the test above guards. 93% - 1% = 92% is the effective floor
        #1488 chose, and it is what the "a PR may lose ~3 points of the 95.06% baseline" reasoning
        in `codecov.yml` and `tests/README.md` is computed from.
        """
        project = _load(_CODECOV)["coverage"]["status"]["project"]["default"]
        target = float(str(project["target"]).rstrip("%"))
        threshold = float(str(project.get("threshold", 0)).rstrip("%"))
        assert target - threshold >= 92, (
            f"the effective project floor is {target - threshold}% (target {target}%, threshold "
            f"{threshold}%), below the 92% #1488 set. Widening the threshold lowers the floor just "
            "as much as lowering the target does — take it to an issue and write the reason into "
            "codecov.yml, same as any other cut."
        )

    def test_the_component_floor_is_still_the_ratcheted_one(self):
        """#1488 raised the per-component target 80% -> 90% in the same pass.

        The lowest component measured 92.61% (Celery Tasks) at that baseline, so 90% is a floor
        every component clears. These stay `informational`: they report per-component drift, the
        project status is what gates.
        """
        rules = _load(_CODECOV)["component_management"]["default_rules"]["statuses"]
        project_rules = [rule for rule in rules if rule["type"] == "project"]
        assert project_rules, "component_management declares no project status to ratchet"
        for rule in project_rules:
            target = float(str(rule["target"]).rstrip("%"))
            assert target >= 90, (
                f"component target dropped to {target}%. #1488 raised it to 90% against a "
                "lowest-component baseline of 92.61%; lowering it needs a reason written into "
                "codecov.yml in the same commit."
            )
            assert rule["informational"] is True, (
                "component statuses are advisory by design — enforcing them turns one module's "
                "refactor into a red status on a PR the project floor already covers"
            )
