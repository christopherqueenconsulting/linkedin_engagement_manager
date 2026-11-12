"""`dependabot-ci-autofix.yml` must watch every workflow that can block a merge.

`workflow_run.workflows` matches a workflow's `name:`, not a check name, so the list drifts silently
in both directions: a required check whose workflow is missing goes red with no `agent:depfix` label,
and a listed name whose workflow was deleted (`CI / E2E Test w/ Coverage`, #1215) matches nothing.
The required contexts are read from CLAUDE.md's CI Gates section, the documented source of truth.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[2]
_WORKFLOWS = _ROOT / ".github" / "workflows"
_AUTOFIX = _WORKFLOWS / "dependabot-ci-autofix.yml"


def _triggers(workflow: dict[str, Any]) -> dict[str, Any]:
    # `on:` is a YAML 1.1 boolean, so PyYAML parses the key as `True`.
    return workflow.get("on", workflow.get(True)) or {}


def _workflows() -> dict[str, dict[str, Any]]:
    """Every workflow under .github/workflows, keyed by file name."""
    out = {}
    for path in sorted([*_WORKFLOWS.glob("*.yml"), *_WORKFLOWS.glob("*.yaml")]):
        out[path.name] = yaml.safe_load(path.read_text(encoding="utf-8"))
    return out


def _required_contexts() -> list[str]:
    """The required status contexts named in CLAUDE.md's CI Gates paragraph."""
    text = (_ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    match = re.search(r"SEVEN contexts branch protection requires on `main`:(.*?)\.\s", text, re.S)
    assert match, "CLAUDE.md's CI Gates section no longer names the required contexts"
    contexts = [" ".join(c.split()) for c in re.findall(r"`([^`]+)`", match.group(1))]
    assert len(contexts) == 7, contexts
    return contexts


def _watched() -> list[str]:
    return _triggers(yaml.safe_load(_AUTOFIX.read_text(encoding="utf-8")))["workflow_run"]["workflows"]


def _job_contexts(workflow: dict[str, Any]) -> set[str]:
    """Check names a workflow's jobs report: the job's `name:`, else its id."""
    return {str(job.get("name") or job_id) for job_id, job in (workflow.get("jobs") or {}).items()}


def test_every_required_context_is_produced_by_some_workflow():
    produced = set().union(*(_job_contexts(w) for w in _workflows().values()))
    missing = [c for c in _required_contexts() if c not in produced]
    assert not missing, f"required contexts no workflow job produces: {missing}"


def test_every_workflow_producing_a_required_context_is_watched():
    required = set(_required_contexts())
    watched = set(_watched())
    unwatched = {
        wf["name"]: sorted(_job_contexts(wf) & required)
        for wf in _workflows().values()
        if _job_contexts(wf) & required and wf["name"] not in watched
    }
    assert not unwatched, f"workflows producing required contexts but not watched: {unwatched}"


def test_every_watched_name_is_a_real_workflow():
    names = {wf.get("name") for wf in _workflows().values()}
    stale = [n for n in _watched() if n not in names]
    assert not stale, f"watched names matching no workflow `name:` (deleted or renamed): {stale}"
