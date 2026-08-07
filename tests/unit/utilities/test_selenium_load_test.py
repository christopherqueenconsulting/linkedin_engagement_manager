"""Unit tests for the Selenium concurrency/scale load-test harness (issue #556).

The harness is the evidence a VPS-upgrade / second-box decision gets made on, so the parts under
test here are the ones that would make that evidence wrong: the queueing model, the "sessions
needed" search, and the resource projection.
"""

import json
import math
from unittest.mock import patch

import pytest

from cqc_lem.utilities import selenium_load_test as slt

pytestmark = pytest.mark.unit


def _job(lane: str, ready: float, duration: float = 10.0, tolerance: float = 30.0,
         user_id: int = 1, name: str = "job") -> slt.Job:
    return slt.Job(user_id=user_id, name=name, lane=lane, ready_at=ready,
                   duration=duration, tolerance=tolerance)


def _topology(lanes: dict, cap: int = None) -> slt.Topology:
    return slt.Topology(lanes=lanes, session_cap=cap if cap is not None else sum(lanes.values()))


# A 15-minute loop that must start within a minute of the fan-out: more users than browsers can ever
# start at once, so no session count reaches 100% on-time and the search has no answer to give.
IMPOSSIBLE = slt.JobSpec("j", "se_engage", 15.0, 1.0, starts=(0,))


def _unreachable_row() -> dict:
    return slt.run_scale(70, slt.default_topology(), specs=(IMPOSSIBLE,), target_on_time_pct=100)


class TestBuildWorkload:
    def test_every_user_gets_every_occurrence_of_every_job(self):
        occurrences = sum(max(1, len(spec.starts)) for spec in slt.WORKLOAD)
        assert len(slt.build_workload(7)) == 7 * occurrences

    def test_daily_selenium_minutes_per_user_match_the_plan(self):
        # §4's table totals ~65-70 Selenium-minutes/user/day; every projection downstream is built
        # on it, so a drifting workload must fail here rather than quietly re-scale the curve.
        jobs = slt.build_workload(1)
        assert 65 <= sum(job.duration for job in jobs) <= 70

    def test_explicit_zero_stagger_reproduces_the_pre_554_single_fanout(self):
        # `--stagger-hours 0` is the deliberate "before" baseline (scaling-plan §5c/§5d) — every user
        # landing on the golden-hour crontab's one minute, as it did before issue #554 shipped.
        golden = [job for job in slt.build_workload(20, stagger_hours=0) if job.name == "golden_hour_commenting"]
        assert {job.ready_at for job in golden} == {13 * 60}

    def test_default_stagger_uses_each_fanouts_own_shipped_window(self):
        # No override → each fan-out uses ITS OWN production window (issue #634): golden-hour is
        # staggered across 180 min, appreciation DMs across 120 min — not a single uniform value.
        from cqc_lem.utilities.engagement_window import STAGGER_APPRECIATION_DM
        dm_anchor = STAGGER_APPRECIATION_DM[1] * 60
        jobs = slt.build_workload(20)
        golden = [job.ready_at for job in jobs if job.name == "golden_hour_commenting"]
        dms = [job.ready_at for job in jobs if job.name == "appreciation_dms"]
        # <= on the upper bound: tick-quantization can round a slot right up to the window's edge.
        assert min(golden) >= 13 * 60 and max(golden) <= 13 * 60 + 180
        assert min(dms) >= dm_anchor and max(dms) <= dm_anchor + 120
        # A single crontab minute would mean everyone lands on one value; staggering spreads them.
        assert len(set(golden)) > 1
        assert len(set(dms)) > 1

    def test_default_stagger_reuses_productions_own_hash_and_tick_quantization(self):
        # The model must give the SAME user_id the SAME offset production's plan_daily_slot would —
        # reusing stagger_offset_minutes AND production's own salt (`stagger_config(fanout).name`,
        # e.g. "GOLDEN_HOUR" — NOT this JobSpec's own display `name`, which is a different string and
        # would silently hash every user_id to a different offset than the real beat) — and then
        # round up to the next STAGGER_TICK_MINUTES boundary, since the beat only ticks every 15 min.
        from cqc_lem.utilities.engagement_window import (
            STAGGER_APPRECIATION_DM,
            STAGGER_GOLDEN_HOUR,
            STAGGER_TICK_MINUTES,
            stagger_offset_minutes,
        )
        jobs = slt.build_workload(5)
        golden = {job.user_id: job.ready_at for job in jobs if job.name == "golden_hour_commenting"}
        for user_id, ready_at in golden.items():
            raw = 13 * 60 + stagger_offset_minutes(user_id, 180, salt=STAGGER_GOLDEN_HOUR[0])
            expected = math.ceil(raw / STAGGER_TICK_MINUTES) * STAGGER_TICK_MINUTES
            assert ready_at == expected
            assert ready_at % STAGGER_TICK_MINUTES == 0
        dms = {job.user_id: job.ready_at for job in jobs if job.name == "appreciation_dms"}
        for user_id, ready_at in dms.items():
            raw = (STAGGER_APPRECIATION_DM[1] * 60
                   + stagger_offset_minutes(user_id, 120, salt=STAGGER_APPRECIATION_DM[0]))
            expected = math.ceil(raw / STAGGER_TICK_MINUTES) * STAGGER_TICK_MINUTES
            assert ready_at == expected

    def test_an_explicit_override_replaces_the_fanouts_own_window_uniformly(self):
        # A what-if run (e.g. modelling a wider/narrower window than what shipped) overrides EVERY
        # staggerable fan-out's window with the same value.
        golden = [job.ready_at for job in slt.build_workload(20, stagger_hours=4)
                  if job.name == "golden_hour_commenting"]
        dms = [job.ready_at for job in slt.build_workload(20, stagger_hours=4)
               if job.name == "appreciation_dms"]
        from cqc_lem.utilities.engagement_window import STAGGER_APPRECIATION_DM
        # <= on the upper bound: tick-quantization can round a slot right up to the window's edge
        # (see test_default_stagger_uses_each_fanouts_own_shipped_window).
        assert max(golden) <= 13 * 60 + 4 * 60
        assert max(dms) <= STAGGER_APPRECIATION_DM[1] * 60 + 4 * 60
        assert len(set(golden)) > 1

    def test_post_anchored_jobs_ignore_the_stagger_and_keep_their_eta_offset(self):
        # The pre-post warm-up is pinned to the user's post, not to a crontab — staggering the
        # crontabs must not move it, or the simulation would claim a fix the code cannot deliver.
        jobs = slt.build_workload(4, stagger_hours=4)
        prepost = sorted(job.ready_at for job in jobs if job.name == "prepost_commenting")
        viewer = sorted(job.ready_at for job in jobs if job.name == "profile_viewer_engagement")
        assert prepost == sorted(slt._post_time(i, 4, slt.POST_BAND) - 15 for i in range(4))
        assert viewer == sorted(slt._post_time(i, 4, slt.POST_BAND) - 10 for i in range(4))

    def test_zero_users_is_an_empty_day_not_an_error(self):
        assert slt.build_workload(0) == []

    @pytest.mark.parametrize("users,stagger", [(-1, 0), (5, -1)])
    def test_negative_inputs_are_rejected(self, users, stagger):
        with pytest.raises(ValueError):
            slt.build_workload(users, stagger_hours=stagger)


class TestSimulate:
    def test_one_slot_serializes_and_reports_the_delay(self):
        jobs = [_job("se_engage", 0, duration=10) for _ in range(3)]
        results = slt.simulate(jobs, _topology({"se_engage": 1}))
        assert sorted(result.started_at for result in results) == [0, 10, 20]
        assert sorted(result.queue_delay for result in results) == [0, 10, 20]

    def test_lane_concurrency_runs_jobs_side_by_side(self):
        jobs = [_job("se_engage", 0, duration=10) for _ in range(3)]
        results = slt.simulate(jobs, _topology({"se_engage": 3}))
        assert all(result.started_at == 0 for result in results)
        assert all(result.on_time for result in results)

    def test_a_lane_cannot_borrow_another_lanes_slot(self):
        jobs = [_job("se_engage", 0, duration=10), _job("se_engage", 0, duration=10),
                _job("se_content", 0, duration=10)]
        results = slt.simulate(jobs, _topology({"se_engage": 1, "se_content": 1}))
        starts = {(result.job.lane, result.started_at) for result in results}
        assert starts == {("se_engage", 0), ("se_engage", 10), ("se_content", 0)}

    def test_an_under_provisioned_cap_shows_up_as_session_wait(self):
        # Two lanes with a slot each but only one browser: one job holds a lane slot while it waits
        # on Chrome. That gap is exactly the "cap < sum of lanes" failure §5a warns about.
        jobs = [_job("se_engage", 0, duration=10), _job("se_content", 0, duration=10)]
        results = slt.simulate(jobs, slt.Topology(lanes={"se_engage": 1, "se_content": 1}, session_cap=1))
        waits = sorted(result.session_wait for result in results)
        assert waits == [0, 10]

    def test_a_cap_that_covers_the_lanes_never_makes_anything_wait_for_a_browser(self):
        jobs = slt.build_workload(25)
        results = slt.simulate(jobs, slt.default_topology())
        assert max(result.session_wait for result in results) == 0

    def test_a_job_that_is_not_ready_yet_does_not_hold_the_slot(self):
        jobs = [_job("se_engage", 100, duration=10), _job("se_engage", 0, duration=10)]
        results = slt.simulate(jobs, _topology({"se_engage": 1}))
        assert sorted(result.started_at for result in results) == [0, 100]

    def test_every_job_eventually_starts(self):
        jobs = slt.build_workload(30)
        assert len(slt.simulate(jobs, slt.default_topology())) == len(jobs)

    def test_an_empty_day_simulates_to_nothing(self):
        assert slt.simulate([], slt.default_topology()) == []

    def test_a_lane_with_no_configured_concurrency_is_refused(self):
        with pytest.raises(ValueError, match="no concurrency configured"):
            slt.simulate([_job("se_nowhere", 0)], _topology({"se_engage": 1}))

    def test_a_zero_slot_lane_is_refused_rather_than_stranding_its_jobs(self):
        # Silently dropping them would report on-time % over the jobs that DID run — a confident
        # all-clear for a topology that runs nothing.
        with pytest.raises(ValueError, match="must be >= 1"):
            slt.simulate([_job("se_engage", 0)], _topology({"se_engage": 0}))


class TestConcurrencyPeak:
    def test_counts_simultaneous_sessions(self):
        results = slt.simulate([_job("se_engage", 0, duration=10) for _ in range(4)],
                               _topology({"se_engage": 4}))
        assert slt.concurrency_peak(results) == 4

    def test_a_handover_is_not_two_browsers(self):
        results = slt.simulate([_job("se_engage", 0, duration=10), _job("se_engage", 10, duration=10)],
                               _topology({"se_engage": 2}))
        assert slt.concurrency_peak(results) == 1

    def test_nothing_running_is_a_peak_of_zero(self):
        assert slt.concurrency_peak([]) == 0


class TestPercentile:
    def test_nearest_rank_returns_a_value_that_happened(self):
        assert slt._percentile([1, 2, 3, 4, 100], 0.95) == 100
        assert slt._percentile([1, 2, 3, 4], 0.5) == 2

    def test_empty_is_unknown_not_zero(self):
        assert slt._percentile([], 0.95) is None


class TestRequiredTopology:
    def test_it_finds_the_smallest_lane_concurrency_that_meets_the_target(self):
        # 4 jobs of 10 min ready together, tolerance 30: with 1 slot the last starts 30 min late
        # (on time), so one slot suffices; with tolerance 10 it needs two.
        spec = slt.JobSpec("j", "se_engage", 10.0, 30.0, starts=(0,))
        assert slt.required_topology(4, _topology({"se_engage": 1}), target_on_time_pct=100,
                                     specs=(spec,))["lanes"] == {"se_engage": 1}
        tight = slt.JobSpec("j", "se_engage", 10.0, 10.0, starts=(0,))
        assert slt.required_topology(4, _topology({"se_engage": 1}), target_on_time_pct=100,
                                     specs=(tight,))["lanes"] == {"se_engage": 2}

    def test_the_cap_is_the_sum_of_the_lanes_so_the_invariant_holds_by_construction(self):
        required = slt.required_topology(50, slt.default_topology())
        assert required["cap"] == sum(required["lanes"].values())

    def test_the_answer_actually_meets_the_target_it_was_asked_for(self):
        required = slt.required_topology(50, slt.default_topology(), target_on_time_pct=95)
        assert required["on_time_pct"] >= 95

    def test_more_users_never_need_fewer_sessions(self):
        caps = [slt.required_topology(users, slt.default_topology())["cap"] for users in (10, 50, 100)]
        assert caps == sorted(caps)

    def test_staggering_a_fanout_in_isolation_lowers_its_own_requirement(self):
        # §5d calls this the single highest-leverage change for the fan-out ITSELF: spread over a
        # window, the smallest concurrency that starts 95% of a burst on time is never worse than
        # the same burst landing on one crontab minute. Isolated to golden_hour_commenting's own
        # lane so the result is not confounded by cross-job contention on a SHARED lane (issue #634
        # found real cases of exactly that — see TestBuildWorkload — which is a separate, honest
        # finding and not something this property claims away).
        spec = (slt.JobSpec("golden_hour_commenting", "se_engage", 15.0, 120.0, starts=(13 * 60,),
                            stagger_window_minutes=180.0),)
        unstaggered = slt.required_topology(100, slt.default_topology(), stagger_hours=0, specs=spec)["cap"]
        staggered = slt.required_topology(100, slt.default_topology(), specs=spec)["cap"]
        assert staggered < unstaggered

    @pytest.mark.parametrize("users", [10, 25, 50, 75, 100, 150, 200])
    def test_the_shipped_dm_window_costs_se_outreach_nothing_against_its_own_baseline(self, users):
        # The regression #696 fixed, pinned at every scale. #634 found that se_outreach came out
        # EQUAL OR WORSE staggered: it carries both the staggered appreciation_dms and the
        # post-anchored profile_viewer_engagement, and spreading the DM arrivals over a window does
        # not shrink the total processing time the burst needs (workload ÷ concurrency is fixed) —
        # it pushed the batch's TAIL a full window later, into the window profile_viewer_engagement
        # arrives in, where the pre-#554 single-instant batch had already drained. Opening the window
        # half its width EARLY (STAGGER_APPRECIATION_DM anchored at 07:00 for a 120-min window) puts
        # the batch back where the unstaggered one sat, so the stagger is now free on this lane
        # rather than something profile_viewer_engagement pays for.
        outreach_specs = tuple(spec for spec in slt.WORKLOAD if spec.lane == "se_outreach")
        unstaggered = slt.required_topology(users, slt.default_topology(), stagger_hours=0,
                                            specs=outreach_specs)["lanes"]["se_outreach"]
        staggered = slt.required_topology(users, slt.default_topology(),
                                          specs=outreach_specs)["lanes"]["se_outreach"]
        assert staggered <= unstaggered

    def test_se_outreach_at_fifty_users_needs_no_more_than_the_pre_554_baseline(self):
        # Issue #696's acceptance criterion, spelled out against the whole fleet's workload rather
        # than the lane in isolation: 2 sessions, the number the pre-#554 fixed-time fan-out needed.
        required = slt.required_topology(50, slt.default_topology())
        assert required["lanes"]["se_outreach"] <= 2

    def test_the_model_reads_the_dm_anchor_off_the_shipped_constant(self):
        # se_outreach's whole problem was production and the model disagreeing about where the DM
        # batch starts. A retune of STAGGER_APPRECIATION_DM that left a literal behind here would
        # re-open that gap silently — the curve would keep reporting the old anchor's numbers.
        from cqc_lem.utilities.engagement_window import STAGGER_APPRECIATION_DM
        spec = next(spec for spec in slt.WORKLOAD if spec.name == "appreciation_dms")
        assert spec.starts == (STAGGER_APPRECIATION_DM[1] * 60,)

    def test_an_impossible_window_reports_no_answer_rather_than_a_huge_one(self):
        # A 15-minute loop that must start within 1 minute of a fan-out can never be met for
        # everyone by adding browsers — the harness must say so, not return the search ceiling.
        impossible = slt.JobSpec("j", "se_engage", 15.0, 1.0, starts=(0,))
        required = slt.required_topology(4, _topology({"se_engage": 1}), target_on_time_pct=100,
                                         specs=(impossible,), max_lane_concurrency=3)
        assert required["cap"] is None and required["unreachable_lane"] == "se_engage"

    def test_zero_users_need_nothing(self):
        assert slt.required_topology(0, slt.default_topology())["cap"] is None


class TestProjectResources:
    def test_ten_users_worth_of_sessions_fit_the_current_box(self):
        # §5c: 10 users = 4-6 concurrent sessions = "fits comfortably".
        assert slt.project_resources(5, "standalone")["verdict"] == slt.VERDICT_FITS

    def test_fifty_users_worth_sits_at_the_ceiling(self):
        # §5c: 50 users = "at/over the ceiling" on 8 vCPU / 31 GB.
        assert slt.project_resources(14, "standalone")["verdict"] == slt.VERDICT_AT_CEILING

    def test_a_hundred_users_worth_exceeds_one_vps(self):
        # §5c: 100 users = "exceeds one VPS" — the second box / 16 vCPU decision point.
        assert slt.project_resources(27, "standalone")["verdict"] == slt.VERDICT_EXCEEDS

    def test_a_grid_costs_more_ram_than_a_standalone_for_the_same_sessions(self):
        # The node JVM per Chrome plus the hub — the price of fault isolation (§5b). Reporting them
        # as equal would understate the box a Grid needs.
        grid = slt.project_resources(8, "grid/8-nodes")
        standalone = slt.project_resources(8, "standalone")
        assert grid["chrome_mem_gb"] > standalone["chrome_mem_gb"]
        assert grid["host_cpu"] > standalone["host_cpu"]


class TestSummarizeAndCurve:
    def test_the_current_topology_degrades_as_users_are_added(self):
        rows = slt.run_curve([10, 50, 100], slt.default_topology())
        on_time = [row["on_time_pct"] for row in rows]
        assert on_time == sorted(on_time, reverse=True)
        assert on_time[0] > on_time[-1]

    def test_resources_are_projected_for_the_topology_that_would_meet_the_slo(self):
        row = slt.run_scale(50, slt.default_topology())
        assert row["chrome_mem_gb"] == pytest.approx(row["sessions_needed"] * slt.MEM_PER_SESSION_GB)
        # ...and NOT for the starving one it is currently running on.
        assert row["sessions_needed"] > row["session_cap"]

    def test_an_unreachable_slo_reports_no_sessions_needed_instead_of_a_number(self):
        # The search found no session count that works. Falling back to the simulated peak here
        # would read as "provision this many and you're fine" — the opposite of what it found.
        row = _unreachable_row()
        assert row["sessions_needed"] is None
        assert row["unreachable_lane"] == "se_engage"

    def test_an_unreachable_row_still_prices_what_it_actually_ran(self):
        row = _unreachable_row()
        assert row["projected_sessions"] == row["peak_sessions"]
        assert row["chrome_mem_gb"] == pytest.approx(row["peak_sessions"] * slt.MEM_PER_SESSION_GB)

    def test_the_row_records_whether_the_simulated_topology_holds_the_invariant(self):
        assert slt.run_scale(10, slt.default_topology())["cap_matches_lanes"] is True
        starved = slt.Topology(lanes=dict(slt.DEFAULT_LANES), session_cap=4)
        assert slt.run_scale(10, starved)["cap_matches_lanes"] is False

    def test_per_lane_detail_covers_every_lane_that_ran(self):
        row = slt.run_scale(10, slt.default_topology())
        assert set(row["per_lane"]) == set(slt.DEFAULT_LANES)
        assert all(0 <= stats["on_time_pct"] <= 100 for stats in row["per_lane"].values())

    def test_grid_topology_turns_node_count_into_the_session_cap(self):
        topology = slt.grid_topology(12)
        assert topology.session_cap == 12 * slt.GRID_NODE_SESSIONS
        assert topology.label == "grid/12-nodes"


class TestRender:
    def test_the_curve_renders_a_row_per_scale(self):
        text = slt.render_curve(slt.run_curve([10, 50], slt.default_topology()))
        assert "| 10 |" in text and "| 50 |" in text
        assert "Sessions needed" in text

    def test_an_unreachable_slo_renders_as_a_marker_not_a_number(self):
        # Operators read the "Sessions needed" column as a target to provision to. When there is no
        # such number, the cell has to say so — and there is no lane split to hang off it either.
        text = slt.render_curve([_unreachable_row()])
        assert "unreachable (se_engage)" in text
        assert "| None |" not in text

    def test_a_broken_invariant_is_called_out_in_the_header(self):
        starved = slt.Topology(lanes=dict(slt.DEFAULT_LANES), session_cap=4)
        assert "invariant" in slt.render_curve(slt.run_curve([10], starved))

    def test_nothing_simulated_says_so(self):
        assert slt.render_curve([]) == "No scales simulated."

    def test_live_output_reports_failures_it_saw(self):
        text = slt.render_live(slt.summarize_live(
            2, [{"wait_seconds": 1.0, "error": None}, {"wait_seconds": 9.0, "error": "TimeoutError: no slot"}],
            {"host": {"cpus": 8, "mem_available_gb": 20.0}}, []))
        assert "1 acquired, 1 failed" in text
        assert "TimeoutError" in text


class TestParseLanes:
    def test_parses_a_what_if_lane_split(self):
        assert slt.parse_lanes("se_engage=4, se_content=2") == {"se_engage": 4, "se_content": 2}

    @pytest.mark.parametrize("raw", ["se_engage", "se_engage=x", ""])
    def test_rejects_junk(self, raw):
        with pytest.raises(ValueError):
            slt.parse_lanes(raw)


class TestLiveMeasurement:
    def test_it_reduces_waits_slots_and_host_headroom(self):
        outcomes = [{"wait_seconds": 1.0, "error": None}, {"wait_seconds": 5.0, "error": None},
                    {"wait_seconds": 2.0, "error": None}]
        baseline = {"capacity": {"max_sessions": 8, "busy_sessions": 0},
                    "host": {"load1": 0.8, "cpus": 8, "mem_available_gb": 24.0}}
        samples = [baseline,
                   {"capacity": {"max_sessions": 8, "busy_sessions": 3},
                    "host": {"load1": 4.2, "cpus": 8, "mem_available_gb": 20.4}}]
        measured = slt.summarize_live(3, outcomes, baseline, samples)
        assert measured["acquired"] == 3 and measured["failed"] == 0
        assert measured["wait_max_seconds"] == 5.0
        assert measured["peak_busy_sessions"] == 3
        assert measured["peak_load1"] == 4.2
        assert measured["mem_consumed_gb"] == pytest.approx(3.6)

    def test_unreadable_grid_and_host_are_unknown_not_zero(self):
        measured = slt.summarize_live(1, [{"wait_seconds": 1.0, "error": None}], {}, [{}])
        assert measured["peak_busy_sessions"] is None
        assert measured["mem_consumed_gb"] is None

    def test_it_opens_the_requested_number_of_real_sessions(self):
        with patch.object(slt, "_sample_environment", return_value={"capacity": None, "host": None}), \
             patch.object(slt, "_open_and_hold",
                          side_effect=lambda hold, user_id, index: {"index": index, "wait_seconds": 1.0,
                                                                    "error": None}) as opener:
            measured = slt.measure_live_sessions(3, hold_seconds=0, sample_interval=0.01)
        assert opener.call_count == 3
        assert measured["acquired"] == 3

    def test_synthetic_load_never_feeds_the_production_capacity_monitor(self):
        # A load test that wrote into the monitor's rolling window would file a capacity issue
        # about itself and poison the evidence the real decision is made on.
        with patch.object(slt, "_sample_environment", return_value={"capacity": None, "host": None}), \
             patch.object(slt, "_open_and_hold", return_value={"index": 0, "wait_seconds": 1.0, "error": None}), \
             patch("cqc_lem.utilities.capacity_alerts.record_session_wait") as recorder:
            slt.measure_live_sessions(1, hold_seconds=0, sample_interval=0.01)
        recorder.assert_not_called()

    def test_one_session_is_timed_and_always_closed(self):
        driver = object()
        with patch("cqc_lem.utilities.selenium_util.get_docker_driver", return_value=driver) as opener, \
             patch("cqc_lem.utilities.selenium_util.quit_gracefully") as closer:
            outcome = slt._open_and_hold(0.0, user_id=7, index=2)
        assert outcome["error"] is None and outcome["wait_seconds"] >= 0
        assert opener.call_args.kwargs["user_id"] == 7
        closer.assert_called_once_with(driver)

    def test_a_session_that_never_opens_is_reported_not_raised(self):
        # A Grid at capacity rejects the request; that IS the measurement, not a crash.
        with patch("cqc_lem.utilities.selenium_util.get_docker_driver", side_effect=TimeoutError("no slot")), \
             patch("cqc_lem.utilities.selenium_util.quit_gracefully") as closer:
            outcome = slt._open_and_hold(0.0, user_id=None, index=0)
        assert outcome["error"] == "TimeoutError: no slot"
        closer.assert_not_called()

    def test_the_environment_sample_reuses_the_capacity_monitors_collectors(self):
        # Live numbers and the production alert's numbers must never disagree about "busy".
        with patch("cqc_lem.utilities.capacity_alerts.collect_selenium_capacity",
                   return_value={"max_sessions": 8, "busy_sessions": 2}), \
             patch("cqc_lem.utilities.capacity_alerts.collect_host_headroom", return_value={"load1": 1.0}):
            assert slt._sample_environment() == {"capacity": {"max_sessions": 8, "busy_sessions": 2},
                                                 "host": {"load1": 1.0}}

    @pytest.mark.parametrize("sessions", [0, -1])
    def test_it_refuses_a_nonsensical_session_count(self, sessions):
        with pytest.raises(ValueError):
            slt.measure_live_sessions(sessions)

    def test_a_failing_session_is_counted_not_raised(self):
        with patch.object(slt, "_sample_environment", return_value={"capacity": None, "host": None}), \
             patch.object(slt, "_open_and_hold",
                          side_effect=lambda hold, user_id, index: {"index": index, "wait_seconds": 12.0,
                                                                    "error": "SessionNotCreatedException"}):
            measured = slt.measure_live_sessions(2, hold_seconds=0, sample_interval=0.01)
        assert measured["acquired"] == 0 and measured["failed"] == 2
        assert measured["wait_max_seconds"] is None


class TestCli:
    def test_default_run_prints_the_curve(self, capsys):
        assert slt.main(["--users", "10"]) == 0
        assert "| 10 |" in capsys.readouterr().out

    def test_json_output_is_machine_readable(self, capsys):
        slt.main(["--users", "10", "--json"])
        payload = json.loads(capsys.readouterr().out)
        assert payload["live"] is None
        assert payload["curve"][0]["users"] == 10

    def test_it_exits_2_when_the_scale_exceeds_one_vps(self, capsys):
        # So a cron/CI caller can gate a cohort onboarding without parsing the table.
        assert slt.main(["--users", "100"]) == 2

    def test_nodes_models_a_grid_instead_of_the_standalone(self, capsys):
        slt.main(["--users", "10", "--nodes", "12"])
        assert "grid/12-nodes" in capsys.readouterr().out

    def test_live_is_opt_in(self, capsys):
        with patch.object(slt, "measure_live_sessions") as live:
            slt.main(["--users", "10"])
        live.assert_not_called()

    def test_live_measures_and_renders(self, capsys):
        with patch.object(slt, "measure_live_sessions", return_value=slt.summarize_live(
                1, [{"wait_seconds": 2.0, "error": None}], {"host": {"cpus": 8}}, [])) as live:
            slt.main(["--users", "10", "--live", "--live-sessions", "1", "--hold-seconds", "0"])
        live.assert_called_once()
        assert "Live Selenium load test" in capsys.readouterr().out
