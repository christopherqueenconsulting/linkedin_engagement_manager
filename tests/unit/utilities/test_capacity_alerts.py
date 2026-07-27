"""Unit tests for the Selenium/lane capacity monitor (issue #552, docs/scaling-plan.md §5e).

Every check is fired on a SYNTHETIC breach here, and pinned on the no-breach and not-enough-data
paths, so a check can neither go quiet nor cry wolf unnoticed. The GitHub delivery is pinned on all
four outcomes (file / comment / cooldown / no token), because the whole point of this module is that
a squeeze reaches a human exactly once.
"""

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from cqc_lem.utilities import capacity_alerts as ca

pytestmark = pytest.mark.unit

_MOD = "cqc_lem.utilities.capacity_alerts"

NOW = datetime(2026, 7, 25, 12, 0, tzinfo=timezone.utc)


def _fake_file(content):
    """Minimal context-manager stand-in for open() — /proc reads are line-iterated."""
    from io import StringIO
    handle = StringIO(content)
    handle.__exit__ = lambda *exc: None
    handle.__enter__ = lambda: handle
    return handle


def _samples(busy, cap=6, queues=None, start=NOW):
    """One sample per entry in `busy`, 15 minutes apart (the beat cadence)."""
    return [{"ts": (start + timedelta(minutes=15 * i)).isoformat(),
             "max_sessions": cap, "busy_sessions": b,
             "queues": dict(queues or {"se_engage": 0, "se_outreach": 0, "se_content": 0})}
            for i, b in enumerate(busy)]


def _queue_samples(depths, lane="se_engage", start=NOW):
    return [{"ts": (start + timedelta(minutes=15 * i)).isoformat(),
             "max_sessions": 6, "busy_sessions": 0, "queues": {lane: d}}
            for i, d in enumerate(depths)]


class TestSessionSaturation:
    def test_fires_when_the_pool_is_claimed_on_a_sustained_share_of_samples(self):
        check = ca.evaluate_session_saturation(_samples([6] * 5 + [0] * 5),
                                               saturation_pct=0.85, sustained_pct=0.25,
                                               min_samples=4)
        assert check["status"] == ca.STATUS_ALERT
        alert = check["alerts"][0]
        assert alert["value"] == pytest.approx(0.5) and alert["threshold"] == 0.25
        assert alert["max_sessions"] == 6 and alert["peak_busy"] == 6

    def test_quiet_on_a_single_saturated_spike(self):
        # Utilisation of a pool we paid for is not a breach — only a SUSTAINED share is.
        check = ca.evaluate_session_saturation(_samples([6] + [1] * 9),
                                               saturation_pct=0.85, sustained_pct=0.25,
                                               min_samples=4)
        assert check["status"] == ca.STATUS_OK and check["alerts"] == []
        assert check["breach_share"] == pytest.approx(0.1)

    def test_sustained_breach_at_double_the_share_is_critical(self):
        check = ca.evaluate_session_saturation(_samples([6] * 8 + [0] * 2),
                                               saturation_pct=0.85, sustained_pct=0.25,
                                               min_samples=4)
        assert check["alerts"][0]["severity"] == ca.SEVERITY_CRITICAL

    def test_partial_saturation_under_the_pct_does_not_count(self):
        # 4/6 = 67% busy is under the 85% "claimed" line.
        check = ca.evaluate_session_saturation(_samples([4] * 10), saturation_pct=0.85,
                                               sustained_pct=0.25, min_samples=4)
        assert check["status"] == ca.STATUS_OK

    def test_skipped_under_the_minimum_sample_count(self):
        check = ca.evaluate_session_saturation(_samples([6, 6]), min_samples=24)
        assert check["status"] == ca.STATUS_SKIPPED and "need 24" in check["reason"]

    def test_unreadable_samples_are_dropped_not_counted_as_idle(self):
        # A Grid that was down produces samples with no cap — those must not dilute the share.
        blind = [{"ts": NOW.isoformat(), "queues": {}} for _ in range(10)]
        check = ca.evaluate_session_saturation(_samples([6] * 4) + blind, min_samples=4,
                                               saturation_pct=0.85, sustained_pct=0.25)
        assert check["samples"] == 4 and check["status"] == ca.STATUS_ALERT

    def test_missing_samples_are_skipped_not_ok(self):
        assert ca.evaluate_session_saturation(None)["status"] == ca.STATUS_SKIPPED


class TestLaneBacklog:
    def test_fires_for_the_backed_up_lane_only(self):
        samples = _queue_samples([5] * 6 + [0] * 4)
        for sample, depth in zip(samples, [0] * 10):
            sample["queues"]["se_content"] = depth
        check = ca.evaluate_lane_backlog(samples, backlog_tasks=3, sustained_pct=0.25,
                                         min_samples=4)
        assert check["status"] == ca.STATUS_ALERT
        assert [a["lane"] for a in check["alerts"]] == ["se_engage"]
        assert check["alerts"][0]["peak_depth"] == 5

    def test_quiet_when_every_lane_drains(self):
        check = ca.evaluate_lane_backlog(_queue_samples([0, 1, 2, 0, 1, 0]), backlog_tasks=3,
                                         sustained_pct=0.25, min_samples=4)
        assert check["status"] == ca.STATUS_OK
        assert check["lanes"]["se_engage"]["peak"] == 2

    def test_permanent_backlog_is_critical(self):
        check = ca.evaluate_lane_backlog(_queue_samples([9] * 10), backlog_tasks=3,
                                         sustained_pct=0.25, min_samples=4)
        assert check["alerts"][0]["severity"] == ca.SEVERITY_CRITICAL

    def test_skipped_under_the_minimum_sample_count(self):
        check = ca.evaluate_lane_backlog(_queue_samples([9, 9]), min_samples=24)
        assert check["status"] == ca.STATUS_SKIPPED

    def test_missing_samples_are_skipped_not_ok(self):
        assert ca.evaluate_lane_backlog(None)["status"] == ca.STATUS_SKIPPED


class TestSessionWait:
    def test_fires_when_the_p95_acquisition_exceeds_the_budget(self):
        waits = [2.0] * 18 + [120.0, 300.0]
        check = ca.evaluate_session_wait(waits, wait_seconds=60, min_samples=20)
        assert check["status"] == ca.STATUS_ALERT
        assert check["p95_seconds"] == pytest.approx(120.0)
        assert check["alerts"][0]["severity"] == ca.SEVERITY_CRITICAL  # ≥ 2× the budget

    def test_warning_between_the_budget_and_double_it(self):
        check = ca.evaluate_session_wait([2.0] * 18 + [70.0, 80.0], wait_seconds=60,
                                         min_samples=20)
        assert check["alerts"][0]["severity"] == ca.SEVERITY_WARNING

    def test_quiet_when_slots_answer_promptly(self):
        check = ca.evaluate_session_wait([3.0] * 40, wait_seconds=60, min_samples=20)
        assert check["status"] == ca.STATUS_OK and check["p95_seconds"] == pytest.approx(3.0)

    def test_skipped_under_the_minimum_acquisition_count(self):
        check = ca.evaluate_session_wait([500.0, 500.0], wait_seconds=60)
        assert check["status"] == ca.STATUS_SKIPPED and "need 20" in check["reason"]

    def test_missing_waits_are_skipped_not_ok(self):
        assert ca.evaluate_session_wait(None)["status"] == ca.STATUS_SKIPPED

    def test_percentile_reports_a_measured_value(self):
        assert ca._percentile([1, 2, 3, 4], 0.5) == 2
        assert ca._percentile([1, 2, 3, 4], 0.95) == 4
        assert ca._percentile([], 0.95) is None


class TestReportBuilder:
    def test_collects_every_check_and_counts_breaches(self):
        report = ca.build_capacity_report(
            _samples([6] * 10), [200.0] * 25,
            thresholds={"min_samples": 4, "sustained_pct": 0.25, "saturation_pct": 0.85,
                        "backlog_tasks": 3, "wait_seconds": 60},
            window_hours=2.5, now=NOW)
        assert report["alert_count"] == 2  # saturation + session_wait; lanes are empty
        assert report["critical_count"] == 2
        assert report["date"] == "2026-07-25" and report["window_hours"] == 2.5
        assert report["sample_count"] == 10
        assert {c["check"] for c in report["checks"]} == {
            ca.CHECK_SATURATION, ca.CHECK_BACKLOG, ca.CHECK_SESSION_WAIT}

    def test_no_data_reports_every_check_skipped_not_ok(self):
        report = ca.build_capacity_report(None, None, now=NOW)
        assert report["alert_count"] == 0
        assert set(report["skipped_checks"]) == {
            ca.CHECK_SATURATION, ca.CHECK_BACKLOG, ca.CHECK_SESSION_WAIT}

    def test_thresholds_merge_over_the_environment_defaults(self):
        report = ca.build_capacity_report(None, None, thresholds={"wait_seconds": 5}, now=NOW)
        assert report["thresholds"]["wait_seconds"] == 5
        assert report["thresholds"]["saturation_pct"] == ca.CAPACITY_SATURATION_PCT


class TestRendering:
    def _breach_report(self):
        return ca.build_capacity_report(
            _queue_samples([9] * 10), None,
            thresholds={"min_samples": 4, "sustained_pct": 0.25, "backlog_tasks": 3}, now=NOW)

    def test_text_digest_lists_alerts_and_check_status(self):
        text = ca.render_capacity_text(self._breach_report())
        assert "[CRITICAL]" in text and "se_engage" in text
        assert f"{ca.CHECK_SESSION_WAIT}: {ca.STATUS_SKIPPED}" in text

    def test_text_digest_says_so_when_nothing_breached(self):
        assert "No capacity thresholds breached." in ca.render_capacity_text(
            ca.build_capacity_report(None, None, now=NOW))

    def test_host_headroom_rides_along_as_decision_context(self):
        samples = _queue_samples([9] * 10)
        samples[0]["host"] = {"load1": 0.8, "cpus": 8, "mem_available_gb": 24.0}
        report = ca.build_capacity_report(
            samples, None, thresholds={"min_samples": 4, "sustained_pct": 0.25,
                                       "backlog_tasks": 3}, now=NOW)
        assert report["host"]["mem_available_gb"] == 24.0
        assert "24.0 GB RAM available" in ca.render_capacity_text(report)
        assert "24.0 GB available at the time" in ca.render_capacity_issue_body(report)

    def test_issue_body_names_the_lane_the_invariant_and_the_options(self):
        body = ca.render_capacity_issue_body(self._breach_report())
        assert "`se_engage`" in body
        assert "SE_NODE_MAX_SESSIONS" in body and ca.GUARD_TEST in body
        assert "**A." in body and "**B." in body and "**C." in body
        assert "My recommendation" in body


class TestCollectors:
    def test_selenium_status_is_parsed_into_busy_and_total_slots(self):
        response = MagicMock(status_code=200)
        response.json.return_value = {"value": {"nodes": [{"slots": [
            {"session": {"sessionId": "a"}}, {"session": None}, {"session": {"sessionId": "b"}}]}]}}
        with patch("requests.get", return_value=response):
            assert ca.collect_selenium_capacity() == {"max_sessions": 3, "busy_sessions": 2}

    def test_unreachable_grid_returns_none_rather_than_an_idle_pool(self):
        with patch("requests.get", side_effect=OSError("boom")):
            assert ca.collect_selenium_capacity() is None

    def test_the_debug_node_is_left_out_of_the_pool_it_sits_on_top_of(self):
        # docker-compose.grid.yml runs a NINTH node the lanes are never sized for. Counting its slot
        # here dilutes the one signal that says the pool is the constraint: 8 busy of a 9-slot
        # denominator reads 0.89 and the sample below the peak reads 0.78 — under the 0.85 threshold
        # that breached at 7/8 before the debug node existed.
        response = MagicMock(status_code=200)
        response.json.return_value = {"value": {"nodes": [
            {"uri": "http://172.20.0.4:5555", "slots": [{"session": {"sessionId": "a"}}]},
            {"uri": "http://172.20.0.5:5555", "slots": [{"session": None}]},
            {"uri": "http://selenium-node-debug:5555", "slots": [{"session": {"sessionId": "z"}}]},
        ]}}
        with patch("requests.get", return_value=response):
            assert ca.collect_selenium_capacity() == {"max_sessions": 2, "busy_sessions": 1}

    def test_a_grid_without_a_debug_node_counts_every_slot(self):
        # The standalone, and any box that never deployed the debug node: nothing matches, so the
        # exclusion must be a no-op rather than a silently smaller pool.
        nodes = [{"uri": "http://172.20.0.4:5555", "slots": [{"session": None}, {"session": None}]}]
        assert len(ca._pool_slots(nodes)) == 2

    def test_the_exclusion_can_be_switched_off(self):
        nodes = [{"uri": "http://selenium-node-debug:5555", "slots": [{"session": None}]}]
        assert len(ca._pool_slots(nodes, debug_host="")) == 1

    def test_a_node_named_after_the_debug_host_by_coincidence_is_not_dropped(self):
        # Matched on the URI's host position, not anywhere in the string — a hub URL or a path that
        # merely contains the name must not silently shrink the denominator.
        nodes = [{"uri": "http://gridhost:5555/selenium-node-debug", "slots": [{"session": None}]}]
        assert len(ca._pool_slots(nodes)) == 1

    def test_grid_with_no_slots_returns_none(self):
        response = MagicMock(status_code=200)
        response.json.return_value = {"value": {"nodes": []}}
        with patch("requests.get", return_value=response):
            assert ca.collect_selenium_capacity() is None

    def test_lane_depths_read_every_selenium_lane(self):
        client = MagicMock()
        client.llen.side_effect = [4, 1, 0]
        with patch(f"{_MOD}._redis", return_value=client), \
             patch(f"{_MOD}.selenium_lane_queues",
                   return_value=("se_engage", "se_outreach", "se_content")):
            assert ca.collect_lane_depths() == {"se_engage": 4, "se_outreach": 1, "se_content": 0}

    def test_lane_depths_none_without_redis(self):
        with patch(f"{_MOD}._redis", return_value=None):
            assert ca.collect_lane_depths() is None

    def test_host_headroom_is_read_from_proc(self):
        proc = {"/proc/loadavg": "1.20 0.90 0.80 1/900 12345\n",
                "/proc/meminfo": "MemTotal:       32000000 kB\nMemAvailable:   24117248 kB\n"}
        with patch("builtins.open", side_effect=lambda path, *a, **k: _fake_file(proc[path])), \
             patch("os.cpu_count", return_value=8):
            assert ca.collect_host_headroom() == {"load1": 1.2, "cpus": 8,
                                                  "mem_available_gb": 23.0}

    def test_host_headroom_is_optional_context_not_a_failure(self):
        with patch("builtins.open", side_effect=OSError("no /proc")):
            assert ca.collect_host_headroom() is None

    def test_only_selenium_lanes_are_monitored(self):
        with patch("cqc_lem.utilities.maintenance.queue_names",
                   return_value=("celery", "se_engage", "se_content")):
            assert ca.selenium_lane_queues() == ("se_engage", "se_content")


class TestSampling:
    def test_sample_is_stored_and_the_window_trimmed(self):
        client = MagicMock()
        with patch(f"{_MOD}._redis", return_value=client), \
             patch(f"{_MOD}.collect_selenium_capacity",
                   return_value={"max_sessions": 6, "busy_sessions": 5}), \
             patch(f"{_MOD}.collect_host_headroom", return_value={"load1": 0.8, "cpus": 8,
                                                                  "mem_available_gb": 24.0}), \
             patch(f"{_MOD}.collect_lane_depths", return_value={"se_engage": 2}):
            sample = ca.record_sample(now=NOW)
        assert sample["host"]["mem_available_gb"] == 24.0
        assert sample["busy_sessions"] == 5 and sample["queues"] == {"se_engage": 2}
        stored = json.loads(client.lpush.call_args[0][1])
        assert stored["max_sessions"] == 6 and stored["ts"] == NOW.isoformat()
        client.ltrim.assert_called_once_with(ca.SAMPLES_KEY, 0, ca.CAPACITY_WINDOW_SAMPLES - 1)

    def test_nothing_readable_records_nothing(self):
        with patch(f"{_MOD}.collect_selenium_capacity", return_value=None), \
             patch(f"{_MOD}.collect_lane_depths", return_value=None):
            assert ca.record_sample(now=NOW) is None

    def test_session_wait_is_recorded_on_the_rolling_window(self):
        client = MagicMock()
        with patch(f"{_MOD}._redis", return_value=client):
            ca.record_session_wait(42.5)
        assert json.loads(client.lpush.call_args[0][1])["seconds"] == 42.5
        client.ltrim.assert_called_once_with(ca.WAITS_KEY, 0, ca.WAIT_WINDOW_SAMPLES - 1)

    def test_session_wait_never_raises_into_the_selenium_hot_path(self):
        with patch(f"{_MOD}._redis", side_effect=RuntimeError("redis down")):
            ca.record_session_wait(1.0)  # must not raise

    def test_loaders_return_none_when_redis_is_unreachable(self):
        with patch(f"{_MOD}._redis", return_value=None):
            assert ca.load_samples() is None and ca.load_waits() is None

    def test_loaders_skip_corrupt_rows(self):
        client = MagicMock()
        client.lrange.return_value = [b'{"seconds": 3.5}', b"not-json", b'{"nope": 1}']
        with patch(f"{_MOD}._redis", return_value=client):
            assert ca.load_waits() == [3.5]

    def test_window_hours_spans_first_to_last_sample(self):
        assert ca._window_hours(_samples([0] * 5)) == 1.0
        assert ca._window_hours(_samples([0])) is None

    def test_collect_report_samples_then_evaluates_the_window(self):
        with patch(f"{_MOD}.record_sample") as sample, \
             patch(f"{_MOD}.load_samples", return_value=_samples([6] * 10)), \
             patch(f"{_MOD}.load_waits", return_value=None):
            report = ca.collect_capacity_report(thresholds={"min_samples": 4})
        sample.assert_called_once()
        assert report["sample_count"] == 10


class TestGitHubDelivery:
    def _report(self):
        return ca.build_capacity_report(
            _queue_samples([9] * 10), None,
            thresholds={"min_samples": 4, "sustained_pct": 0.25, "backlog_tasks": 3}, now=NOW)

    def test_files_one_issue_when_none_is_open(self):
        with patch(f"{_MOD}.find_open_capacity_issue", return_value=None), \
             patch("cqc_lem.utilities.feedback.issue_service.github_token", return_value="t"), \
             patch(f"{_MOD}._apply_issue_labels") as label, \
             patch("cqc_lem.utilities.feedback.issue_service.create_github_issue",
                   return_value=777) as create:
            result = ca.file_capacity_issue(self._report(), now=NOW)
        assert result == {"action": "filed", "issue": 777}
        title, body, labels = create.call_args[0]
        assert title.startswith(ca.ISSUE_TITLE_PREFIX) and "2026-07-25" in title
        assert labels == list(ca.ISSUE_LABELS) and "se_engage" in body
        label.assert_called_once_with(777)

    def test_labels_are_re_applied_after_creation(self):
        # A fine-grained PAT drops labels from the create payload (#598); unlabelled, the issue
        # never reaches the needs-human queue.
        with patch("cqc_lem.utilities.feedback.issue_service.github_request") as request:
            ca._apply_issue_labels(42)
        assert request.call_args[0][:2] == ("POST", "issues/42/labels")
        assert request.call_args[0][2] == {"labels": list(ca.ISSUE_LABELS)}

    def test_re_breach_comments_on_the_open_issue_after_the_cooldown(self):
        stale = {"number": 42, "title": ca.ISSUE_TITLE_PREFIX + " (2026-07-01)",
                 "updated_at": (NOW - timedelta(days=30)).isoformat().replace("+00:00", "Z")}
        with patch(f"{_MOD}.find_open_capacity_issue", return_value=stale), \
             patch("cqc_lem.utilities.feedback.issue_service.github_token", return_value="t"), \
             patch("cqc_lem.utilities.feedback.issue_service.comment_on_issue",
                   return_value=True) as comment, \
             patch("cqc_lem.utilities.feedback.issue_service.create_github_issue") as create:
            result = ca.file_capacity_issue(self._report(), now=NOW)
        assert result == {"action": "commented", "issue": 42}
        assert "Still breaching" in comment.call_args[0][1]
        create.assert_not_called()

    def test_recent_activity_holds_the_alert_off_entirely(self):
        fresh = {"number": 42, "title": ca.ISSUE_TITLE_PREFIX,
                 "updated_at": (NOW - timedelta(hours=2)).isoformat().replace("+00:00", "Z")}
        with patch(f"{_MOD}.find_open_capacity_issue", return_value=fresh), \
             patch("cqc_lem.utilities.feedback.issue_service.github_token", return_value="t"), \
             patch("cqc_lem.utilities.feedback.issue_service.comment_on_issue") as comment:
            assert ca.file_capacity_issue(self._report(), now=NOW)["action"] == "cooldown"
        comment.assert_not_called()

    def test_no_token_skips_without_raising(self):
        with patch("cqc_lem.utilities.feedback.issue_service.github_token", return_value=None):
            assert ca.file_capacity_issue(self._report(), now=NOW)["action"] == "skipped"

    def test_disabled_by_flag(self):
        with patch(f"{_MOD}.CAPACITY_ISSUE_ENABLED", False):
            assert ca.file_capacity_issue(self._report(), now=NOW)["action"] == "disabled"

    def test_nothing_filed_without_alerts(self):
        clean = ca.build_capacity_report(None, None, now=NOW)
        with patch("cqc_lem.utilities.feedback.issue_service.github_token") as token:
            assert ca.file_capacity_issue(clean, now=NOW)["action"] == "none"
        token.assert_not_called()

    def test_a_rejected_creation_reports_failure_rather_than_pretending(self):
        with patch(f"{_MOD}.find_open_capacity_issue", return_value=None), \
             patch("cqc_lem.utilities.feedback.issue_service.github_token", return_value="t"), \
             patch("cqc_lem.utilities.feedback.issue_service.create_github_issue",
                   return_value=None):
            assert ca.file_capacity_issue(self._report(), now=NOW)["action"] == "failed"

    def test_a_rejected_comment_reports_failure(self):
        stale = {"number": 42, "title": ca.ISSUE_TITLE_PREFIX, "updated_at": "2026-01-01T00:00:00Z"}
        with patch(f"{_MOD}.find_open_capacity_issue", return_value=stale), \
             patch("cqc_lem.utilities.feedback.issue_service.github_token", return_value="t"), \
             patch("cqc_lem.utilities.feedback.issue_service.comment_on_issue",
                   return_value=False):
            assert ca.file_capacity_issue(self._report(), now=NOW)["action"] == "failed"

    def test_an_unparseable_timestamp_does_not_hold_the_alert(self):
        odd = {"number": 42, "title": ca.ISSUE_TITLE_PREFIX, "updated_at": "not-a-date"}
        assert ca._updated_within_cooldown(odd, NOW) is False
        assert ca._updated_within_cooldown({"number": 42}, NOW) is False

    def test_open_issue_lookup_matches_on_the_title_marker(self):
        listing = [{"number": 1, "title": "something else"},
                   {"number": 2, "title": ca.ISSUE_TITLE_PREFIX + " (2026-07-25)"}]
        with patch("cqc_lem.utilities.feedback.issue_service.github_request",
                   return_value=listing) as request:
            assert ca.find_open_capacity_issue()["number"] == 2
        path = request.call_args[0][1]
        assert "state=open" in path
        # NOT label-filtered: a fine-grained PAT drops labels at creation (#598), and a lookup that
        # can't see its own issue would file a new one every tick.
        assert "labels=" not in path

    def test_open_issue_lookup_pages_past_a_full_first_page(self):
        full_page = [{"number": n, "title": f"unrelated {n}"} for n in range(100)]
        second = [{"number": 601, "title": ca.ISSUE_TITLE_PREFIX}]
        with patch("cqc_lem.utilities.feedback.issue_service.github_request",
                   side_effect=[full_page, second]) as request:
            assert ca.find_open_capacity_issue()["number"] == 601
        assert "page=2" in request.call_args_list[1][0][1]

    def test_open_issue_lookup_stops_on_a_short_page(self):
        with patch("cqc_lem.utilities.feedback.issue_service.github_request",
                   return_value=[{"number": 1, "title": "unrelated"}]) as request:
            assert ca.find_open_capacity_issue() is None
        assert request.call_count == 1

    def test_open_issue_lookup_survives_an_unreadable_api(self):
        with patch("cqc_lem.utilities.feedback.issue_service.github_request", return_value=None):
            assert ca.find_open_capacity_issue() is None


class TestSendCapacityAlerts:
    def _report(self):
        return ca.build_capacity_report(
            _queue_samples([9] * 10), None,
            thresholds={"min_samples": 4, "sustained_pct": 0.25, "backlog_tasks": 3}, now=NOW)

    def test_logs_tracks_and_files_each_breach(self):
        with patch(f"{_MOD}.file_capacity_issue", return_value={"action": "filed", "issue": 9}), \
             patch("cqc_lem.utilities.observability.track_capacity_alert") as track, \
             patch(f"{_MOD}.log_error") as log_error:
            result = ca.send_capacity_alerts(self._report())
        assert result["alerts"] == 1 and result["tracked"] is True
        assert result["github"]["action"] == "filed"
        track.assert_called_once()
        log_error.assert_called_once()  # critical breach → PostHog via the logger

    def test_a_warning_breach_logs_at_warning_not_error(self):
        # 3 of 10 samples backed up: over the 25% sustained line, under the 50% critical line.
        warning = ca.build_capacity_report(
            _queue_samples([9] * 3 + [0] * 7), None,
            thresholds={"min_samples": 4, "sustained_pct": 0.25, "backlog_tasks": 3}, now=NOW)
        with patch(f"{_MOD}.file_capacity_issue", return_value={"action": "filed"}), \
             patch("cqc_lem.utilities.observability.track_capacity_alert"), \
             patch(f"{_MOD}.log_warning") as log_warning, patch(f"{_MOD}.log_error") as log_error:
            assert ca.send_capacity_alerts(warning)["alerts"] == 1
        log_error.assert_not_called()
        assert any("Capacity alert" in call.args[0] for call in log_warning.call_args_list)

    def test_a_github_failure_never_breaks_the_beat_run(self):
        with patch(f"{_MOD}.file_capacity_issue", side_effect=RuntimeError("502")), \
             patch("cqc_lem.utilities.observability.track_capacity_alert"):
            result = ca.send_capacity_alerts(self._report())
        assert result["github"]["action"] == "failed"

    def test_a_posthog_failure_never_breaks_the_beat_run(self):
        with patch(f"{_MOD}.file_capacity_issue", return_value={"action": "filed"}), \
             patch("cqc_lem.utilities.observability.track_capacity_alert",
                   side_effect=RuntimeError("posthog down")):
            assert ca.send_capacity_alerts(self._report())["tracked"] is False


class TestCli:
    def test_exit_code_2_on_a_breach(self, capsys):
        with patch(f"{_MOD}.collect_capacity_report", return_value=self._breach()):
            assert ca.main(["--json"]) == 2
        assert json.loads(capsys.readouterr().out)["alert_count"] == 1

    def test_exit_code_0_when_clean(self, capsys):
        with patch(f"{_MOD}.collect_capacity_report",
                   return_value=ca.build_capacity_report(None, None, now=NOW)):
            assert ca.main([]) == 0
        assert "No capacity thresholds breached." in capsys.readouterr().out

    def test_deliver_flag_sends_the_report(self):
        report = self._breach()
        with patch(f"{_MOD}.collect_capacity_report", return_value=report), \
             patch(f"{_MOD}.send_capacity_alerts") as send:
            ca.main(["--deliver"])
        send.assert_called_once_with(report)

    def _breach(self):
        return ca.build_capacity_report(
            _queue_samples([9] * 10), None,
            thresholds={"min_samples": 4, "sustained_pct": 0.25, "backlog_tasks": 3}, now=NOW)
