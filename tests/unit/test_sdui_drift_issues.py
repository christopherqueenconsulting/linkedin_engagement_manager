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
                              "code": "run_automation._CATCHUP_CARD_LOCATORS",
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
        the backlog every Monday until it buried the real drift underneath."""
        sweep = _sweep(a={"state": "unknown"}, b={"state": "unknown"})
        assert filer.drift_rows(sweep) == []

    def test_rows_are_read_from_probes_not_from_the_summary(self):
        """The summary is a convenience for humans; a filer that planned from a derived field would
        file nothing at all the day that field goes stale."""
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
        assert "run_automation._CATCHUP_CARD_LOCATORS" in body

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
        that rotted, got re-grounded, and rotted again six months later is a NEW defect."""
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
        belongs to the owner."""
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
