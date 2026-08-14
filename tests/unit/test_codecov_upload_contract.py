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

    A matrix job uploads once per combination. `include` entries that add a new axis rather than
    refine an existing one are not modelled — no workflow here uses one, and the parity assertion
    would catch it as a mismatch rather than pass on a wrong number.
    """
    matrix = (job.get("strategy") or {}).get("matrix")
    if not isinstance(matrix, dict):
        return 1
    axes = [v for k, v in matrix.items() if k not in {"include", "exclude"} and isinstance(v, list)]
    combinations = math.prod(len(axis) for axis in axes) if axes else len(matrix.get("include", []))
    return max(combinations, 1)


def _step_flags(step: dict[str, Any]) -> list[str]:
    raw = (step.get("with") or {}).get("flags")
    if not raw:
        return []
    return [flag.strip() for flag in str(raw).split(",") if flag.strip()]


def _pull_request_uploads() -> tuple[int, set[str], list[str]]:
    """Count the coverage uploads a pull request produces, and the flags they carry.

    Only workflows with a `pull_request` trigger count: the project status is posted on a PR, so a
    lane that never runs there can never satisfy `after_n_builds`.

    Returns:
        The upload count, the set of flags used, and the workflow files they came from.
    """
    uploads = 0
    flags: set[str] = set()
    sources: list[str] = []

    for path in sorted(_WORKFLOWS.glob("*.yml")):
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

        Measured on main at 6a145efa from unit + integration alone: project 95.06%. The floor is a
        ratchet — raising it is the intended direction, lowering it needs a reason this test's
        failure message will ask for.
        """
        project = _load(_CODECOV)["coverage"]["status"]["project"]["default"]
        target = float(str(project["target"]).rstrip("%"))
        assert target >= 90, (
            f"project target dropped to {target}%. The two-lane baseline measured 95.06% with the "
            "e2e lane already deleted, so this is a real cut, not a re-baseline."
        )
        assert project["informational"] is False, "the floor only means something if it is enforced"
