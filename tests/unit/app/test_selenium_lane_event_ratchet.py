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
    swallows_failure,
    tasks_that_swallow_failure,
    tasks_with_no_outcome_event,
)

pytestmark = pytest.mark.unit

_BASELINE_PATH = pathlib.Path(__file__).with_name("selenium_lane_event_baseline.json")
_SWALLOW_BASELINE_PATH = pathlib.Path(__file__).with_name("selenium_lane_swallow_baseline.json")


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


def _swallow_baseline() -> set:
    return set(json.loads(_SWALLOW_BASELINE_PATH.read_text(encoding="utf-8")))


class TestSwallowedFailureRatchet:
    """The second half of the silent-success ratchet (issue #2097).

    A lane that catches `Exception` and returns a string ends in Celery SUCCESS however the run
    went — the shape behind 0 FAILURE in 20,841 runs. Same ratchet idiom as the class above: the
    checked-in baseline lists the known offenders, a NEW one fails, and a fixed one must leave it.
    """

    def test_baseline_file_is_a_sorted_json_list_of_wire_names(self):
        raw = json.loads(_SWALLOW_BASELINE_PATH.read_text(encoding="utf-8"))
        assert isinstance(raw, list)
        assert raw == sorted(raw), "keep it sorted so a diff shows exactly what changed"
        assert _swallow_baseline() <= set(selenium_lane_wire_names())

    def test_no_new_se_task_catches_exception_and_returns_a_string(self):
        new_offenders = tasks_that_swallow_failure() - _swallow_baseline()
        assert not new_offenders, (
            f"these se_* task(s) catch Exception and return a string, so a failed run reads as "
            f"Celery SUCCESS: {sorted(new_offenders)} — end the run through "
            f"cqc_lem.app.task_outcome.lane_result instead."
        )

    def test_a_fixed_lane_is_removed_from_the_baseline(self):
        stale = _swallow_baseline() - tasks_that_swallow_failure()
        assert not stale, (f"these entries no longer swallow a failure and must be removed from "
                           f"{_SWALLOW_BASELINE_PATH.name}: {sorted(stale)}")

    @pytest.mark.parametrize("wire_name", [
        "cqc_lem.app.run_automation.auto_publish_edition",
        "cqc_lem.app.run_automation.engage_with_profile_viewer",
        "cqc_lem.app.run_automation.auto_scrape_post_stats",
        "cqc_lem.app.run_automation.auto_comment_in_groups",
        "cqc_lem.app.run_automation.send_scheduled_dm",
    ])
    def test_the_lanes_fixed_by_2097_are_real_and_clean(self, wire_name):
        assert wire_name in selenium_lane_wire_names()
        assert wire_name not in tasks_that_swallow_failure()


class TestSwallowsFailure:
    """Anti-vacuity: the scanner has to recognise the shape it gates, and nothing else."""

    @pytest.mark.parametrize("handler", ["except Exception as e:", "except:",
                                         "except (ValueError, Exception):",
                                         "except BaseException:"])
    def test_a_broad_handler_returning_a_string_is_caught(self, handler):
        src = f"def t():\n    try:\n        go()\n    {handler}\n        return f'Failed: {{1}}'\n"
        assert swallows_failure(src)

    def test_a_plain_string_literal_is_caught(self):
        assert swallows_failure("def t():\n    try:\n        go()\n    except Exception:\n"
                                "        return 'Failed'\n")

    def test_the_outcome_helper_is_not(self):
        assert not swallows_failure("def t():\n    try:\n        go()\n    except Exception as e:\n"
                                    "        return lane_result(TaskOutcome.FAILED, 'x', cause=e)\n")

    def test_a_narrow_handler_is_not(self):
        assert not swallows_failure("def t():\n    try:\n        go()\n    except KeyError:\n"
                                    "        return 'missing'\n")

    def test_a_nested_function_inside_the_handler_is_not(self):
        assert not swallows_failure("def t():\n    try:\n        go()\n    except Exception:\n"
                                    "        def f():\n            return 'x'\n        raise\n")
