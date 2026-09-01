"""Write-only-lane ratchet (issue #1816).

`scan_outreach_funnel_targets` filed nothing for 14 straight days and looked identical, from
outside, to a lane that was never running at all — every early exit only ever logged at DEBUG. That
shape isn't unique to this one task: every `se_*`-queued task whose only record of a run is a
returned string has the same blind spot. A hard gate requiring an outcome event on all of them would
need an exemption list the size of the backlog, which is theatre — so this is a RATCHET instead, the
same idiom `.ruff-baseline` uses: `selenium_lane_event_baseline.json` is the checked-in list of
known offenders, and the gate only fails when a NEW one is added. Fixing one shrinks the true set
below the baseline — update the file in the same PR, the same way a ruff cleanup ratchets
`.ruff-baseline` down.
"""

import json
import pathlib

import pytest

from scripts.selenium_lane_event_coverage import (
    selenium_lane_wire_names,
    tasks_with_no_outcome_event,
)

pytestmark = pytest.mark.unit

_BASELINE_PATH = pathlib.Path(__file__).with_name("selenium_lane_event_baseline.json")


def _baseline() -> set:
    return set(json.loads(_BASELINE_PATH.read_text(encoding="utf-8")))


class TestWriteOnlyLaneRatchet:
    def test_baseline_file_is_a_sorted_json_list_of_wire_names(self):
        raw = json.loads(_BASELINE_PATH.read_text(encoding="utf-8"))
        assert isinstance(raw, list) and raw, "baseline must be a non-empty JSON list"
        assert raw == sorted(raw), "keep it sorted so a diff shows exactly what changed"
        assert all(name.startswith("cqc_lem.app.") for name in raw)

    def test_no_new_se_task_ships_without_an_outcome_event(self):
        """The gate.

        A PR that adds an `se_*` task with no `track_*` call anywhere it reaches widens
        `tasks_with_no_outcome_event()` beyond the checked-in baseline, and fails here.
        """
        current = tasks_with_no_outcome_event()
        new_offenders = current - _baseline()
        assert not new_offenders, (
            "these se_* task(s) ship with no outcome event and aren't in the baseline "
            f"({_BASELINE_PATH.name}): {sorted(new_offenders)} — add one (model it on "
            "track_stale_invite_run / track_feed_scan in observability.py), or if this is a "
            "deliberate, temporary exemption, add the wire name to the baseline file and say why."
        )

    def test_a_fixed_lane_is_removed_from_the_baseline(self):
        """The other half of the ratchet.

        Don't let a baseline entry go stale once it's fixed — that is exactly the debt list this
        test exists to keep honest, per the write-only-lane follow-on in issue #1816.
        """
        current = tasks_with_no_outcome_event()
        stale = _baseline() - current
        assert not stale, (
            f"these baseline entries now emit an outcome event and must be removed from "
            f"{_BASELINE_PATH.name}: {sorted(stale)}"
        )

    def test_the_baseline_only_names_real_se_lanes(self):
        """Anti-vacuity.

        A stale wire name (task renamed/removed) would silently stop being checked at all, the
        same blind spot #1013 calls out for a selector.
        """
        assert _baseline() <= set(selenium_lane_wire_names())
