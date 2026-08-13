"""Tests for the structured `Phase` field and its ONE real consumer today, `phase_guard_ok`.

The field is declared in `.github/ISSUE_TEMPLATE/agent-task.yml`; the consumer is the
`phase_leftover` helper in `scripts/agent-pipeline/tick.sh`.

Also covers the RUNBOOK.md prompt-text invariant that makes this addition safe to ship onto the
existing backlog: a `template:agent-task` gate MUST be a complete no-op on any issue that lacks the
label. `MODE=start`/`MODE=selfreview` are prose an LLM agent reads, not code pytest exercises, but
the STRUCTURE of that prose (which paragraph the label-gate lives in, that it explicitly states the
no-op) is grep-able and worth pinning down the same way `test_agent_pipeline_phasefix.py` pins
`tick.sh`'s prose-adjacent shell.

`phase_leftover` itself still shells out to `gh issue view`, so it is exercised here by shadowing
`gh` with a bash function before sourcing the extracted block — same technique the existing
phasefix tests use for `phasefix_bump`/`phasefix_clear`, just stubbing the one `gh` call this
function makes instead of avoiding `gh` altogether.
"""

from __future__ import annotations

import re
import subprocess
import textwrap
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[2]
TICK_SH = _ROOT / "scripts" / "agent-pipeline" / "tick.sh"
RUNBOOK = _ROOT / "scripts" / "agent-pipeline" / "RUNBOOK.md"
TEMPLATE_YAML = _ROOT / ".github" / "ISSUE_TEMPLATE" / "agent-task.yml"

TICK_SOURCE = TICK_SH.read_text(encoding="utf-8")
RUNBOOK_SOURCE = RUNBOOK.read_text(encoding="utf-8")

PHASE_FIELD_VALUE = re.search(
    r"\nphase_field_value\(\) \{.*?\n\}\n", TICK_SOURCE, re.S
).group(0)
PHASE_LEFTOVER = re.search(
    r"\nphase_leftover\(\) \{.*?\n\}\n", TICK_SOURCE, re.S
).group(0)


def _run(script: str) -> str:
    """Run a bash snippet built on the extracted helpers and return trimmed stdout."""
    result = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, check=True
    )
    return result.stdout.rstrip("\n")


def _field_value(body: str, label: str) -> str:
    script = (
        f"{PHASE_FIELD_VALUE}\n"
        f'BODY={_bash_single_quote(body)}\n'
        f'phase_field_value "$BODY" {_bash_single_quote(label)}\n'
    )
    return _run(script)


def _bash_single_quote(value: str) -> str:
    """Safely single-quote `value` for embedding in a generated bash script."""
    return "'" + value.replace("'", "'\\''") + "'"


def _leftover(body: str) -> str:
    """Run `phase_leftover` with `gh issue view` stubbed to return `body`."""
    script = textwrap.dedent(
        f"""
        SLUG="owner/repo"
        gh() {{ printf '%s' "$TEST_BODY"; }}
        export TEST_BODY={_bash_single_quote(body)}
        {PHASE_FIELD_VALUE}
        {PHASE_LEFTOVER}
        phase_leftover 1
        """
    )
    return _run(script)


# --------------------------------------------------------------------------- tick.sh sanity


def test_tick_sh_is_syntactically_valid():
    subprocess.run(["bash", "-n", str(TICK_SH)], check=True)


# --------------------------------------------------------------------------- phase_field_value


def test_extracts_single_line_answer_under_matching_header():
    body = "### Phase\n\nphase 2 of 3\n\n### Remaining phases (only if multi-phase)\n\nMore later.\n"
    assert _field_value(body, "Phase") == "phase 2 of 3"


def test_stops_at_the_next_header_rather_than_swallowing_it():
    body = "### Phase\n\nsingle-phase\n\n### Context\n\nunrelated context text\n"
    assert _field_value(body, "Phase") == "single-phase"


def test_no_response_placeholder_reads_as_empty():
    body = "### Remaining phases (only if multi-phase)\n\n_No response_\n"
    assert _field_value(body, "Remaining phases (only if multi-phase)") == ""


def test_missing_header_returns_empty():
    body = "### Context\n\nsome context\n"
    assert _field_value(body, "Phase") == ""


# --------------------------------------------------------------------------- phase_leftover


def test_structured_multi_phase_field_is_preferred_and_carries_remaining_text():
    body = (
        "### Phase\n\nphase 2 of 3\n\n"
        "### Remaining phases (only if multi-phase)\n\nPhase 3 adds the rest.\n"
    )
    out = _leftover(body)
    assert out.startswith('phase: "phase 2 of 3"')
    assert "Phase 3 adds the rest." in out


def test_structured_single_phase_with_no_boxes_clears_the_guard():
    body = "### Phase\n\nsingle-phase\n\n### Acceptance\n\n- [x] done\n"
    assert _leftover(body) == ""


def test_structured_single_phase_still_reports_unchecked_boxes():
    body = "### Phase\n\nsingle-phase\n\n### Acceptance\n\n- [ ] not done yet\n"
    assert _leftover(body) == "boxes: 1 unchecked"


def test_structured_field_wins_over_unrelated_prose_elsewhere_in_the_body():
    """A structured 'single-phase' answer must not be re-flagged by prose the regex would catch."""
    body = (
        "### Phase\n\nsingle-phase\n\n"
        "### Context\n\nThis ships part 2 of 3 items in a batch, unrelated to issue phasing.\n"
    )
    assert _leftover(body) == ""


def test_falls_back_to_prose_regex_when_the_structured_field_is_absent():
    """Old-format issues (no template) keep exactly today's behavior."""
    body = "## Context\nThis is phase 2 of the migration, deferred to a follow-up PR.\n"
    out = _leftover(body)
    assert out.startswith("phase:")


def test_old_format_issue_with_only_unchecked_boxes_is_unaffected():
    body = "## Acceptance\n- [ ] box one\n- [ ] box two\n"
    assert _leftover(body) == "boxes: 2 unchecked"


def test_empty_body_reports_nothing():
    assert _leftover("") == ""


# --------------------------------------------------------------------------- issue template YAML


def test_agent_task_template_exists_and_parses_as_valid_yaml():
    assert TEMPLATE_YAML.exists(), "agent-task.yml issue template is missing"
    with TEMPLATE_YAML.open(encoding="utf-8") as fh:
        doc = yaml.safe_load(fh)
    assert isinstance(doc, dict)


def _load_template() -> dict:
    with TEMPLATE_YAML.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def test_template_auto_applies_the_template_agent_task_label():
    doc = _load_template()
    assert doc.get("labels") == ["template:agent-task"]


def test_template_declares_the_required_structured_fields():
    doc = _load_template()
    fields = {
        item["id"]: item
        for item in doc.get("body", [])
        if isinstance(item, dict) and "id" in item
    }
    for required_id in ("context", "scope", "acceptance", "verifier", "phase"):
        assert required_id in fields, f"missing field id={required_id}"
        assert fields[required_id]["validations"]["required"] is True

    # The free-text remaining-phases field is optional (only multi-phase issues need it).
    assert "remaining_phases" in fields
    assert fields["remaining_phases"]["validations"]["required"] is False


def test_template_field_labels_match_what_phase_field_value_greps_for():
    """`phase_field_value` matches on `### <label>` — the form's labels must produce those headers."""
    doc = _load_template()
    labels = {
        item["id"]: item["attributes"]["label"]
        for item in doc.get("body", [])
        if isinstance(item, dict) and "id" in item
    }
    assert labels["phase"] == "Phase"
    assert labels["remaining_phases"] == "Remaining phases (only if multi-phase)"


def test_template_acceptance_field_is_free_text_not_structured_checkboxes():
    """GitHub's `checkboxes` field type can't express a variable per-issue list (plan §1)."""
    doc = _load_template()
    fields = {item["id"]: item for item in doc.get("body", []) if isinstance(item, dict) and "id" in item}
    assert fields["acceptance"]["type"] == "textarea"


# --------------------------------------------------------------------------- RUNBOOK.md prose gate


def _section(heading_start: str, heading_end: str) -> str:
    start = RUNBOOK_SOURCE.index(heading_start)
    end = RUNBOOK_SOURCE.index(heading_end, start)
    return RUNBOOK_SOURCE[start:end]


MODE_START = _section("## MODE=start", "## MODE=fix")
MODE_SELFREVIEW = _section("## MODE=selfreview", "## MODE=rebase")


def test_mode_start_gate_is_explicitly_label_scoped():
    assert "template:agent-task" in MODE_START
    assert "label absent" in MODE_START.lower()
    # The regression-avoidance guarantee itself must be spelled out, not just implied.
    assert "no-op" in MODE_START.lower()


def test_mode_start_still_implements_the_smallest_correct_change_unmodified():
    """The core instruction non-template issues rely on must still be present verbatim."""
    assert "Implement the smallest correct change that satisfies the acceptance criteria" in MODE_START


def test_mode_start_still_stops_at_the_end():
    assert re.search(r"^\d+\. STOP\.", MODE_START, re.M)


def test_mode_selfreview_walks_acceptance_checklist_only_on_template_issues():
    assert "template:agent-task" in MODE_SELFREVIEW
    assert "item-by-item" in MODE_SELFREVIEW
    assert "WITHOUT that label" in MODE_SELFREVIEW


def test_mode_selfreview_keeps_fixing_in_place_and_never_hands_off_to_revise():
    """Adversarial review rejected a selfreview -> agent:revise hand-off (budget-starvation risk)."""
    assert "agent:revise" not in MODE_SELFREVIEW
    assert "FIX IT in the worktree" in MODE_SELFREVIEW
