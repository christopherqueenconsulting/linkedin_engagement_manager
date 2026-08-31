"""Unit tests for the SDUI drift issue filer (scripts/sdui_drift_issues.py), issue #1013."""

import importlib.util
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "sdui_drift_issues.py"
_spec = importlib.util.spec_from_file_location("sdui_drift_issues", SCRIPT)
filer = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(filer)


def _sweep(**probes) -> dict:
    return {
        "user_id": 1,
        "probes": probes,
        "surfaces": {
            "catchup_cards": {"surface": "Catch-up moment cards",
                              "code": "outreach._CATCHUP_CARD_LOCATORS",
                              "flag": "--catchup-cards"},
            "feed_sort": {"surface": "Home feed sort", "code": "x", "flag": "--feed-sort"},
        },
        "summary": {"probed": len(probes), "ok": [], "drift": [], "unknown": []},
    }


@pytest.mark.unit
class TestDriftRows:
    def test_only_drift_becomes_a_row(self):
        sweep = _sweep(catchup_cards={"state": "drift", "verdict": "v"},
                       feed_sort={"state": "ok"},
                       sent_invites={"state": "unknown", "verdict": "did not render"})
        assert [r["key"] for r in filer.drift_rows(sweep)] == ["catchup_cards"]

    def test_unknown_is_never_filed(self):
        """A page that did not render grounds nothing. Filing it would put the same non-finding in
        the backlog every Monday until it buried the real drift underneath.
        """
        sweep = _sweep(a={"state": "unknown"}, b={"state": "unknown"})
        assert filer.drift_rows(sweep) == []

    def test_rows_are_read_from_probes_not_from_the_summary(self):
        """The summary is a convenience for humans; a filer that planned from a derived field would
        file nothing at all the day that field goes stale.
        """
        sweep = _sweep(catchup_cards={"state": "drift"})
        sweep["summary"]["drift"] = []
        assert len(filer.drift_rows(sweep)) == 1

    def test_a_surface_with_no_matrix_entry_still_files(self):
        sweep = _sweep(mystery={"state": "drift"})
        row = filer.drift_rows(sweep)[0]
        assert row["surface"] == "mystery" and row["flag"] == ""

    def test_malformed_probe_entries_are_skipped_not_crashed_on(self):
        sweep = _sweep(a="not a dict")
        assert filer.drift_rows(sweep) == []
        assert filer.drift_rows(None) == []


@pytest.mark.unit
class TestBodies:
    def test_the_body_carries_the_dedup_marker_the_next_run_searches_for(self):
        row = filer.drift_rows(_sweep(catchup_cards={"state": "drift", "verdict": "v"}))[0]
        body = filer.build_body(row, user_id=1)
        assert filer.marker("catchup_cards") in body
        assert "--catchup-cards" in body
        assert "outreach._CATCHUP_CARD_LOCATORS" in body

    def test_the_body_carries_both_fix_invariants(self):
        row = filer.drift_rows(_sweep(catchup_cards={"state": "drift"}))[0]
        body = filer.build_body(row)
        assert "OUTCOME being present" in body
        assert "names a different entity" in body

    def test_the_probe_reading_is_attached_as_evidence(self):
        row = filer.drift_rows(_sweep(catchup_cards={"state": "drift", "cards_matched": 0,
                                                     "profile_anchors": 12}))[0]
        assert '"profile_anchors": 12' in filer.build_body(row)

    def test_evidence_is_bounded_so_one_huge_reading_cannot_break_the_issue(self):
        row = filer.drift_rows(_sweep(catchup_cards={"state": "drift", "x": "y" * 50000}))[0]
        assert len(filer.build_body(row)) < filer.MAX_EVIDENCE_CHARS + 4000

    def test_the_title_names_the_surface(self):
        row = filer.drift_rows(_sweep(catchup_cards={"state": "drift"}))[0]
        assert filer.build_title(row) == "SDUI drift: Catch-up moment cards"


@pytest.mark.unit
class TestPlanning:
    def test_an_already_filed_marker_is_skipped(self):
        rows = filer.drift_rows(_sweep(catchup_cards={"state": "drift"}))
        actions = filer.plan_actions(rows, {filer.marker("catchup_cards")})
        assert actions[0]["action"] == "skip" and actions[0]["reason"] == "already filed"
        assert filer.pending(actions) == []

    def test_the_cap_holds_rows_rather_than_dropping_them_silently(self):
        rows = filer.drift_rows(_sweep(a={"state": "drift"}, b={"state": "drift"},
                                       c={"state": "drift"}))
        actions = filer.plan_actions(rows, set(), max_new=1)
        assert len(filer.pending(actions)) == 1
        assert "2 held for the next run" in filer.summarize(actions)

    def test_summarize_says_nothing_when_there_is_nothing(self):
        assert filer.summarize([]) == "no drift"


@pytest.mark.unit
class TestGitHubLayer:
    def _github(self, returncode=0, stdout="[]"):
        github = filer.GitHubIssues("owner/repo")
        github._run = MagicMock(return_value=MagicMock(returncode=returncode, stdout=stdout,
                                                       stderr=""))
        return github

    def test_dedup_searches_open_issues_only(self):
        """Unlike the PostHog error filer, a CLOSED issue must not suppress a re-file: a surface
        that rotted, got re-grounded, and rotted again six months later is a NEW defect.
        """
        github = self._github()
        github.is_filed("sdui-drift-feed_sort")
        args = github._run.call_args[0][0]
        assert "--state" in args and args[args.index("--state") + 1] == "open"

    def test_a_search_hit_on_a_neighbouring_issue_does_not_count(self):
        """GitHub's search tokenizes on hyphens, so the returned bodies are re-checked."""
        github = self._github(stdout=json.dumps([{"number": 1, "body": "sdui-drift-feed_reactions"}]))
        assert github.is_filed("sdui-drift-feed_sort") is False

    def test_the_literal_marker_counts(self):
        github = self._github(stdout=json.dumps([{"number": 1, "body": "x sdui-drift-feed_sort y"}]))
        assert github.is_filed("sdui-drift-feed_sort") is True

    def test_an_unreadable_search_fails_closed(self):
        """A GitHub outage read as 'nothing filed yet' would duplicate every drifting surface."""
        with pytest.raises(RuntimeError):
            self._github(returncode=1).is_filed("sdui-drift-feed_sort")
        with pytest.raises(RuntimeError):
            self._github(stdout="not json").is_filed("sdui-drift-feed_sort")

    def test_the_live_linkedin_risk_label_is_applied(self):
        """Re-grounding a rotated locator cannot be verified without a live probe run, so the merge
        belongs to the owner.
        """
        assert "risk:live-linkedin" in filer.LABELS
        assert "agent:ready" in filer.LABELS

    def test_a_create_failure_leaves_the_row_unfiled_for_the_next_run(self):
        github = self._github(returncode=1)
        rows = filer.drift_rows(_sweep(catchup_cards={"state": "drift"}))
        applied = filer.apply_actions(github, filer.plan_actions(rows, set()), dry_run=False)
        assert applied == []


@pytest.mark.unit
class TestMain:
    def test_a_clean_sweep_exits_zero_without_touching_github(self, monkeypatch, tmp_path, capsys):
        path = tmp_path / "sweep.json"
        path.write_text(json.dumps(_sweep(feed_sort={"state": "ok"})))
        monkeypatch.setattr(filer, "GitHubIssues", MagicMock(side_effect=AssertionError("no gh")))
        assert filer.main(["--sweep-file", str(path)]) == 0
        assert "never filed" in capsys.readouterr().out

    def test_drift_in_dry_run_reports_pending(self, monkeypatch, tmp_path):
        path = tmp_path / "sweep.json"
        path.write_text(json.dumps(_sweep(catchup_cards={"state": "drift"})))
        github = MagicMock()
        github.is_filed.return_value = False
        monkeypatch.setattr(filer, "GitHubIssues", lambda repo: github)
        assert filer.main(["--sweep-file", str(path), "--dry-run"]) == 2
        github.create.assert_not_called()

    def test_apply_files_the_issue(self, monkeypatch, tmp_path):
        path = tmp_path / "sweep.json"
        path.write_text(json.dumps(_sweep(catchup_cards={"state": "drift"})))
        github = MagicMock()
        github.is_filed.return_value = False
        github.create.return_value = "https://github.com/x/y/issues/1"
        monkeypatch.setattr(filer, "GitHubIssues", lambda repo: github)
        assert filer.main(["--sweep-file", str(path), "--apply"]) == 0
        assert github.create.call_count == 1

    def test_an_unreadable_sweep_is_an_error_not_a_clean_bill_of_health(self, tmp_path):
        path = tmp_path / "sweep.json"
        path.write_text("")
        assert filer.main(["--sweep-file", str(path)]) == 1

    def test_a_dedup_lookup_failure_stops_the_run(self, monkeypatch, tmp_path):
        path = tmp_path / "sweep.json"
        path.write_text(json.dumps(_sweep(catchup_cards={"state": "drift"})))
        github = MagicMock()
        github.is_filed.side_effect = RuntimeError("gh down")
        monkeypatch.setattr(filer, "GitHubIssues", lambda repo: github)
        assert filer.main(["--sweep-file", str(path), "--apply"]) == 1


@pytest.mark.unit
class TestFencedReport:
    """The probe runs inside the Celery worker, where the app logger writes to stdout too. The
    week's sweep is `<log lines> <fence> <json> <fence>` — one unparsed line ahead of the JSON used
    to lose the entire run.
    """

    def test_the_report_is_cut_out_of_the_workers_log_noise(self, tmp_path):
        path = tmp_path / "sweep.json"
        path.write_text("\n".join([
            "Getting Updated Profile",
            "2026-08-03 06:40:01 INFO Connecting to selenium-chrome:4444",
            filer.REPORT_JSON_BEGIN,
            json.dumps(_sweep(catchup_cards={"state": "drift"})),
            filer.REPORT_JSON_END,
            "Session closed.",
        ]))
        assert len(filer.drift_rows(filer.load_sweep(str(path)))) == 1

    def test_an_unfenced_report_still_loads(self, tmp_path):
        path = tmp_path / "sweep.json"
        path.write_text(json.dumps(_sweep(feed_sort={"state": "ok"})))
        assert filer.load_sweep(str(path))["user_id"] == 1

    def test_log_noise_with_no_fence_is_an_error_not_a_silent_no_drift(self, tmp_path):
        path = tmp_path / "sweep.json"
        path.write_text("Getting Updated Profile\nselenium session died\n")
        assert filer.main(["--sweep-file", str(path)]) == 1


@pytest.mark.unit
class TestStaleRows:
    """Phase 2 (issue #1770): silence is its own finding.

    A surface that has gone unmeasured for 3 consecutive sweeps is a coverage blind spot, a
    different defect from a rotted locator — its own marker, its own body.
    """

    def test_three_consecutive_unmeasured_sweeps_is_stale(self):
        week1 = _sweep(connect_dialog={"state": "unknown"})
        week2 = _sweep(connect_dialog={"state": "unknown"})
        current = _sweep(connect_dialog={"state": "unknown"})
        rows = filer.stale_rows(current, [week1, week2])
        assert [r["key"] for r in rows] == ["connect_dialog"]

    def test_an_ok_anywhere_in_the_window_clears_it(self):
        week1 = _sweep(connect_dialog={"state": "ok"})
        week2 = _sweep(connect_dialog={"state": "unknown"})
        current = _sweep(connect_dialog={"state": "unknown"})
        assert filer.stale_rows(current, [week1, week2]) == []

    def test_a_drift_anywhere_in_the_window_also_clears_it(self):
        """Drift means the surface WAS seen and is broken.

        That is the drift filer's job, not a coverage blind spot, so it must not also file here.
        """
        week1 = _sweep(connect_dialog={"state": "drift"})
        week2 = _sweep(connect_dialog={"state": "unknown"})
        current = _sweep(connect_dialog={"state": "unknown"})
        assert filer.stale_rows(current, [week1, week2]) == []

    def test_missing_from_a_sweep_entirely_counts_as_unmeasured(self):
        """A surface missing from an older sweep file counts as unmeasured too.

        It is exactly as blind as one graded `unknown` — the surface simply was not covered that
        week either.
        """
        week1 = _sweep()
        week2 = _sweep()
        current = _sweep(connect_dialog={"state": "unknown"})
        rows = filer.stale_rows(current, [week1, week2])
        assert [r["key"] for r in rows] == ["connect_dialog"]

    def test_too_little_history_says_nothing(self):
        """A fresh install with no sweep history yet must not immediately file a blind-spot issue.

        Not for every surface on week one.
        """
        current = _sweep(connect_dialog={"state": "unknown"})
        assert filer.stale_rows(current, []) == []
        assert filer.stale_rows(current, [_sweep(connect_dialog={"state": "unknown"})]) == []

    def test_the_marker_uses_its_own_prefix_not_drifts(self):
        assert filer.stale_marker("connect_dialog") == "sdui-stale-connect_dialog"
        assert filer.stale_marker("connect_dialog") != filer.marker("connect_dialog")

    def test_the_stale_body_carries_its_own_marker_and_the_state_history(self):
        row = filer.stale_rows(
            _sweep(connect_dialog={"state": "unknown"}),
            [_sweep(connect_dialog={"state": "unknown"}),
             _sweep(connect_dialog={"state": "unknown"})])[0]
        body = filer.build_stale_body(row)
        assert filer.stale_marker("connect_dialog") in body
        assert filer.marker("connect_dialog") not in body
        assert "unmeasured, not merely unchanged" in body

    def test_the_stale_title_names_the_surface(self):
        row = {"key": "connect_dialog", "surface": "Connect invite dialog"}
        assert filer.build_stale_title(row) == "SDUI sweep blind spot: Connect invite dialog"

    def test_stale_issues_carry_no_live_linkedin_risk_label(self):
        """A coverage gap needs no live re-grounding to verify, unlike a drift fix."""
        assert "risk:live-linkedin" not in filer.STALE_LABELS
        assert "agent:ready" in filer.STALE_LABELS


@pytest.mark.unit
class TestLoadRecentSweeps:
    def test_reads_the_most_recent_files_oldest_first(self, tmp_path):
        for i, state in enumerate(["ok", "drift", "unknown"]):
            (tmp_path / f"sweep-{i:02d}.json").write_text(
                json.dumps(_sweep(feed_sort={"state": state})))
        history = filer.load_recent_sweeps(str(tmp_path), limit=2)
        assert [h["probes"]["feed_sort"]["state"] for h in history] == ["drift", "unknown"]

    def test_excludes_the_current_sweep_file_by_name(self, tmp_path):
        (tmp_path / "sweep-00.json").write_text(json.dumps(_sweep(feed_sort={"state": "ok"})))
        (tmp_path / "sweep-01.json").write_text(json.dumps(_sweep(feed_sort={"state": "drift"})))
        history = filer.load_recent_sweeps(str(tmp_path), limit=5, exclude="sweep-01.json")
        assert len(history) == 1
        assert history[0]["probes"]["feed_sort"]["state"] == "ok"

    def test_a_missing_directory_is_empty_history_not_an_error(self, tmp_path):
        assert filer.load_recent_sweeps(str(tmp_path / "nope"), limit=2) == []

    def test_no_directory_configured_is_empty_history(self):
        assert filer.load_recent_sweeps("", limit=2) == []

    def test_an_unreadable_file_is_skipped_not_fatal(self, tmp_path):
        (tmp_path / "sweep-00.json").write_text("not json")
        (tmp_path / "sweep-01.json").write_text(json.dumps(_sweep(feed_sort={"state": "ok"})))
        history = filer.load_recent_sweeps(str(tmp_path), limit=5)
        assert len(history) == 1


@pytest.mark.unit
class TestMainWithStaleness:
    def test_a_blind_spot_files_with_its_own_marker(self, monkeypatch, tmp_path):
        for i in range(2):
            (tmp_path / f"sweep-{i:02d}.json").write_text(
                json.dumps(_sweep(connect_dialog={"state": "unknown"})))
        current = tmp_path / "sweep-current.json"
        current.write_text(json.dumps(_sweep(connect_dialog={"state": "unknown"})))
        github = MagicMock()
        github.is_filed.return_value = False
        github.create.return_value = "https://github.com/x/y/issues/2"
        monkeypatch.setattr(filer, "GitHubIssues", lambda repo: github)
        rc = filer.main(["--sweep-file", str(current), "--apply",
                         "--history-dir", str(tmp_path)])
        assert rc == 0
        assert github.create.call_count == 1
        _title, body = github.create.call_args[0][:2]
        assert filer.stale_marker("connect_dialog") in body

    def test_no_history_dir_skips_the_blind_spot_check_entirely(self, monkeypatch, tmp_path):
        path = tmp_path / "sweep.json"
        path.write_text(json.dumps(_sweep(feed_sort={"state": "ok"})))
        monkeypatch.setattr(filer, "GitHubIssues", MagicMock(side_effect=AssertionError("no gh")))
        assert filer.main(["--sweep-file", str(path)]) == 0


@pytest.mark.unit
class TestReproduceCommand:
    def test_a_flag_that_takes_a_value_gets_one(self):
        sweep = _sweep(profile_scrape={"state": "drift"})
        sweep["surfaces"]["profile_scrape"] = {"surface": "Profile header scrape", "code": "x",
                                               "flag": "--profile-scrape", "arg": "<profile-url>"}
        body = filer.build_body(filer.drift_rows(sweep)[0], user_id=1)
        assert "--profile-scrape <profile-url>" in body

    def test_a_store_true_flag_gets_no_stray_placeholder(self):
        body = filer.build_body(filer.drift_rows(_sweep(catchup_cards={"state": "drift"}))[0])
        assert "--catchup-cards <target" not in body
        assert "--catchup-cards <profile" not in body
