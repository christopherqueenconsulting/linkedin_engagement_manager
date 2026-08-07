"""Unit tests for scripts/triage_issues.py — daily issue triage (issue #748)."""

import importlib.util
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

pytestmark = pytest.mark.unit

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "triage_issues.py"


@pytest.fixture(scope="module")
def mod():
    spec = importlib.util.spec_from_file_location("triage_issues", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["triage_issues"] = module
    spec.loader.exec_module(module)
    return module


def _issue(mod, number=1, title="title", labels=None, milestone=None, updated_at="", body="",
           author_association="OWNER"):
    return mod.Issue(
        number=number,
        title=title,
        body=body,
        state="open",
        labels=list(labels or []),
        milestone=milestone,
        created_at="",
        updated_at=updated_at,
        author="gitchrisqueen",
        author_association=author_association,
        is_pull_request=False,
    )


class TestIssueGap:
    def test_fully_structured_issue_has_no_gap(self, mod):
        issue = _issue(mod, labels=["bug", "priority:high", "agent:ready"],
                       milestone={"number": 16, "title": "M16"})
        gap = mod.issue_gap(issue)
        assert not gap.missing_milestone
        assert not gap.missing_priority
        assert not gap.missing_flow
        assert not gap.missing_topical

    def test_missing_everything(self, mod):
        issue = _issue(mod, labels=[])
        gap = mod.issue_gap(issue)
        assert gap.missing_milestone
        assert gap.missing_priority
        assert gap.missing_flow
        assert gap.missing_topical

    def test_priority_label_counts(self, mod):
        issue = _issue(mod, labels=["priority:medium"])
        assert not mod.issue_gap(issue).missing_priority

    def test_flow_label_counts(self, mod):
        issue = _issue(mod, labels=["needs-human"])
        assert not mod.issue_gap(issue).missing_flow

    def test_topical_label_excludes_meta_labels(self, mod):
        issue = _issue(mod, labels=["priority:high", "agent:ready", "risk:product-decision"])
        assert mod.issue_gap(issue).missing_topical


class TestNeedsTriage:
    def test_open_issue_with_missing_priority_needs_triage(self, mod):
        assert mod.needs_triage(_issue(mod, labels=["bug"])) is True

    def test_closed_issue_is_skipped(self, mod):
        issue = _issue(mod, labels=[])
        issue.state = "closed"
        assert mod.needs_triage(issue) is False

    def test_pull_request_is_skipped(self, mod):
        issue = _issue(mod, labels=[])
        issue.is_pull_request = True
        assert mod.needs_triage(issue) is False

    def test_fully_tagged_issue_does_not_need_triage(self, mod):
        assert mod.needs_triage(_issue(mod, labels=["bug", "priority:high", "agent:ready"],
                                       milestone={"number": 1, "title": "M"})) is False


class TestStaleness:
    def test_recent_issue_is_not_stale(self, mod):
        now = datetime(2026, 7, 28, tzinfo=timezone.utc)
        issue = _issue(mod, updated_at="2026-07-27T00:00:00Z")
        assert mod.is_stale(issue, now, days=30) is False

    def test_old_issue_is_stale(self, mod):
        now = datetime(2026, 7, 28, tzinfo=timezone.utc)
        issue = _issue(mod, updated_at="2026-06-01T00:00:00Z")
        assert mod.is_stale(issue, now, days=30) is True

    def test_missing_dates_are_not_stale(self, mod):
        assert mod.is_stale(_issue(mod), datetime.now(timezone.utc)) is False


class TestPhaseDrop:
    def test_open_issue_in_closed_milestone_is_phase_drop(self, mod):
        milestones = [{"number": 15, "title": "M15", "state": "closed"}]
        issue = _issue(mod, milestone={"number": 15, "title": "M15"})
        assert mod.is_phase_drop(issue, milestones) is True

    def test_open_issue_in_open_milestone_is_not_phase_drop(self, mod):
        milestones = [{"number": 16, "title": "M16", "state": "open"}]
        issue = _issue(mod, milestone={"number": 16, "title": "M16"})
        assert mod.is_phase_drop(issue, milestones) is False

    def test_issue_without_milestone_is_not_phase_drop(self, mod):
        assert mod.is_phase_drop(_issue(mod), []) is False


class TestLabelSelection:
    def test_priority_respects_existing(self, mod):
        decision = SimpleNamespace(priority="priority:high")
        assert mod.select_priority_label(decision, ["priority:medium"]) is None

    def test_priority_adds_when_missing(self, mod):
        decision = SimpleNamespace(priority="priority:high")
        assert mod.select_priority_label(decision, ["bug"]) == "priority:high"

    def test_flow_respects_existing(self, mod):
        decision = SimpleNamespace(flow="agent:ready")
        assert mod.select_flow_label(decision, ["agent:working"]) is None

    def test_flow_adds_when_missing(self, mod):
        decision = SimpleNamespace(flow="needs-human")
        assert mod.select_flow_label(decision, ["bug"]) == "needs-human"

    def test_topical_skips_already_present_and_meta(self, mod):
        decision = SimpleNamespace(topical_labels=["bug", "observability", "agent:ready"])
        assert mod.select_topical_labels(decision, ["observability"], {"bug", "observability"}) == ["bug"]


class TestMilestoneResolution:
    def test_select_milestone_matches_existing_by_title(self, mod):
        milestones = [{"number": 17, "title": "M17"}]
        decision = SimpleNamespace(milestone_title="M17", milestone_number=None)
        assert mod.select_milestone(decision, milestones) == 17

    def test_select_milestone_returns_none_for_proposal(self, mod):
        decision = SimpleNamespace(milestone_title="Brand new", milestone_number=None)
        assert mod.select_milestone(decision, []) is None


class TestParseLLMPlan:
    def test_parses_valid_plan(self, mod):
        issues = [_issue(mod, number=750), _issue(mod, number=754)]
        milestones = [{"number": 16, "title": "Stability & Trust"}]
        raw = json.dumps({
            "issues": [
                {"number": 750, "priority": "priority:high", "milestone_title": "Stability & Trust",
                 "flow": "needs-human", "topical_labels": ["bug"], "reason": "product decision"},
                {"number": 754, "priority": "priority:low", "milestone_title": "Stability & Trust",
                 "flow": "agent:ready", "topical_labels": ["ui"], "reason": "ui polish"},
            ],
            "proposed_milestones": [{"title": "New theme", "description": "exit"}]
        })
        decisions, proposed = mod.parse_llm_plan(raw, issues, milestones)
        assert len(decisions) == 2
        assert decisions[0].milestone_number == 16
        assert len(proposed) == 1

    def test_unknown_issue_numbers_are_dropped(self, mod):
        issues = [_issue(mod, number=1)]
        raw = json.dumps({"issues": [{"number": 999, "priority": "priority:low"}], "proposed_milestones": []})
        decisions, _ = mod.parse_llm_plan(raw, issues, [])
        assert decisions == []

    def test_markdown_fences_are_stripped(self, mod):
        issues = [_issue(mod, number=1)]
        raw = "```json\n" + json.dumps({"issues": [{"number": 1, "priority": "priority:low"}],
                                         "proposed_milestones": []}) + "\n```"
        decisions, _ = mod.parse_llm_plan(raw, issues, [])
        assert len(decisions) == 1
        assert decisions[0].priority == "priority:low"

    def test_invalid_json_raises(self, mod):
        with pytest.raises(ValueError):
            mod.parse_llm_plan("not json", [], [])


class TestPromptBuild:
    def test_prompt_includes_milestones_and_issues(self, mod):
        issue = _issue(mod, number=1, title="x", body="y", labels=["bug"])
        prompt = mod.build_prompt([issue], [{"number": 16, "title": "M16", "open_issues": 3}],
                                  ["bug", "feature"])
        assert "M16" in prompt
        assert "bug" in prompt
        assert '"number": 1' in prompt


class TestBuildReport:
    def test_report_includes_decisions_and_staleness(self, mod):
        issue = _issue(mod, number=750, title="affiliate program", labels=["feature"])
        decision = mod.TriageDecision(number=750, priority="priority:high",
                                      milestone_title="M16", milestone_number=16,
                                      flow="needs-human", topical_labels=["feature"],
                                      reason="product decision")
        stale = [_issue(mod, number=1, title="old issue")]
        report = mod.build_report("2026-07-28", [issue], [decision], [], stale, [], applied=False)
        assert "affiliate program" in report
        assert "M16" in report
        assert "old issue" in report
        assert "dry-run" in report

    def test_report_handles_no_decisions(self, mod):
        report = mod.build_report("2026-07-28", [], [], [], [], [], applied=False)
        assert "No triage decisions" in report


class TestPlanChanges:
    def test_plan_adds_priority_flow_and_topical(self, mod):
        issue = _issue(mod, number=750, title="affiliate", labels=[])
        decision = mod.TriageDecision(number=750, priority="priority:high",
                                      milestone_title="M16", milestone_number=16,
                                      flow="needs-human", topical_labels=["feature"],
                                      reason="x")
        changes = mod.plan_changes([issue], [decision], [{"number": 16, "title": "M16"}], {"feature"})
        assert len(changes) == 1
        assert changes[0]["add_labels"] == ["priority:high", "needs-human", "feature"]
        assert changes[0]["milestone_number"] == 16

    def test_plan_preserves_existing_priority(self, mod):
        issue = _issue(mod, number=750, title="x", labels=["priority:medium"])
        decision = mod.TriageDecision(number=750, priority="priority:high", flow="agent:ready")
        changes = mod.plan_changes([issue], [decision], [], {"bug"})
        assert "priority:high" not in changes[0]["add_labels"]
        assert "agent:ready" in changes[0]["add_labels"]


class TestApplyChanges:
    def test_dry_run_counts_as_applied(self, mod, monkeypatch):
        gh = MagicMock()
        changes = [{"number": 1, "add_labels": ["priority:high"], "milestone_number": 16,
                    "milestone_title": "M16", "title": "x"}]
        assert mod.apply_changes(gh, changes, dry_run=True) == 1
        gh.add_labels.assert_not_called()

    def test_apply_calls_github(self, mod):
        gh = MagicMock()
        gh.add_labels.return_value = True
        gh.set_milestone.return_value = True
        changes = [{"number": 1, "add_labels": ["priority:high", "agent:ready"],
                    "milestone_number": 16, "milestone_title": "M16", "title": "x"}]
        assert mod.apply_changes(gh, changes, dry_run=False) == 1
        gh.add_labels.assert_called_once_with(1, ["priority:high", "agent:ready"])
        gh.set_milestone.assert_called_once_with(1, 16)

    def test_partial_failure_does_not_count(self, mod):
        gh = MagicMock()
        gh.add_labels.return_value = False
        changes = [{"number": 1, "add_labels": ["priority:high"], "milestone_number": None,
                    "milestone_title": None, "title": "x"}]
        assert mod.apply_changes(gh, changes, dry_run=False) == 0


class TestDeterministicFlags:
    def test_separates_triaged_stale_phase_drop(self, mod):
        now = datetime(2026, 7, 28, tzinfo=timezone.utc)
        issues = [
            _issue(mod, number=1, labels=["bug"]),
            _issue(mod, number=2, labels=["bug", "priority:high", "agent:ready"],
                   milestone={"number": 15, "title": "M15"}, updated_at="2026-06-01T00:00:00Z"),
            _issue(mod, number=3, labels=["bug", "priority:high", "agent:ready"],
                   milestone={"number": 15, "title": "M15"}, updated_at="2026-07-27T00:00:00Z"),
        ]
        milestones = [{"number": 15, "title": "M15", "state": "closed"}]
        triaged, stale, phase_drops = mod.deterministic_flags(issues, milestones, now)
        assert {i.number for i in triaged} == {1}
        assert {i.number for i in stale} == {2}
        assert {i.number for i in phase_drops} == {2, 3}


class TestMainDryRun:
    def test_dry_run_returns_2_when_changes_pending(self, mod, monkeypatch, capsys):
        issues = [_issue(mod, number=1, title="x", labels=["bug"])]
        milestones = [{"number": 16, "title": "M16", "state": "open", "open_issues": 0}]
        labels = ["bug", "feature", "priority:high", "agent:ready"]

        gh = MagicMock()
        gh.list_open_issues.return_value = issues
        gh.list_milestones.return_value = milestones
        gh.list_labels.return_value = labels

        monkeypatch.setattr(mod, "GitHubClient", lambda repo: gh)
        monkeypatch.setattr(mod, "LLMClient", lambda **kw: MagicMock(classify=lambda p: json.dumps({
            "issues": [{"number": 1, "priority": "priority:high", "milestone_title": "M16",
                        "flow": "agent:ready", "topical_labels": [], "reason": "r"}],
            "proposed_milestones": []
        })))

        monkeypatch.setenv("LITELLM_MASTER_KEY", "test")
        rc = mod.main(["--repo", "owner/repo", "--report-dir", "/tmp/lem-test-triage"])
        assert rc == 2
        assert "1 issues need structure" in capsys.readouterr().out

    def test_main_without_llm_key_is_idempotent_report(self, mod, monkeypatch, capsys):
        issue = _issue(mod, number=1, title="x", labels=["bug"])
        gh = MagicMock()
        gh.list_open_issues.return_value = [issue]
        gh.list_milestones.return_value = []
        gh.list_labels.return_value = ["bug", "priority:high", "agent:ready"]
        monkeypatch.setattr(mod, "GitHubClient", lambda repo: gh)
        monkeypatch.delenv("LITELLM_MASTER_KEY", raising=False)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)

        report_dir = Path("/tmp/lem-test-triage-idem")
        report_dir.mkdir(exist_ok=True)
        rc = mod.main(["--repo", "owner/repo", "--report-dir", str(report_dir)])
        assert rc == 0
        out = capsys.readouterr().out
        assert "1 issues need structure" in out
        assert (report_dir / f"{mod.date.today()}.md").exists()


class TestFlowLabelIsAPrivilegeBoundary:
    """`agent:ready` makes an issue body the prompt for an autonomous run holding the owner's
    credentials. On a public repo anyone can author that text, so this cron may not grant it to
    an outsider however confident the model was.
    """

    @pytest.mark.parametrize("assoc", ["OWNER", "MEMBER", "COLLABORATOR"])
    def test_a_trusted_author_may_receive_agent_ready(self, mod, assoc):
        d = mod.TriageDecision(number=1, flow="agent:ready")
        assert mod.select_flow_label(d, [], assoc) == "agent:ready"

    @pytest.mark.parametrize("assoc", ["CONTRIBUTOR", "FIRST_TIME_CONTRIBUTOR", "NONE", "MANNEQUIN",
                                       "", "owner_lowercase_typo"])
    def test_an_untrusted_author_is_downgraded_to_needs_human(self, mod, assoc):
        d = mod.TriageDecision(number=1, flow="agent:ready")
        assert mod.select_flow_label(d, [], assoc) == "needs-human"

    def test_an_unreadable_association_fails_toward_the_label_that_waits(self, mod):
        d = mod.TriageDecision(number=1, flow="agent:ready")
        assert mod.select_flow_label(d, [], "") == "needs-human"

    def test_case_is_normalised_so_a_lowercase_api_shape_still_passes(self, mod):
        d = mod.TriageDecision(number=1, flow="agent:ready")
        assert mod.select_flow_label(d, [], "owner") == "agent:ready"

    def test_needs_human_is_unaffected_by_author_standing(self, mod):
        d = mod.TriageDecision(number=1, flow="needs-human")
        assert mod.select_flow_label(d, [], "NONE") == "needs-human"

    def test_an_existing_flow_label_is_still_never_overwritten(self, mod):
        d = mod.TriageDecision(number=1, flow="agent:ready")
        assert mod.select_flow_label(d, ["needs-human"], "OWNER") is None

    def test_an_outsider_issue_is_still_triaged_just_not_granted(self, mod):
        # The point is a held label, not a refusal to help: priority and topical labels still land.
        issue = _issue(mod, number=9, labels=[], author_association="NONE")
        d = mod.TriageDecision(number=9, priority="priority:high", flow="agent:ready",
                               topical_labels=["bug"])
        changes = mod.plan_changes([issue], [d], [], {"bug"})
        assert changes[0]["add_labels"] == ["priority:high", "needs-human", "bug"]


class TestAuthorAssociationParsing:
    def test_rest_and_graphql_shapes_both_parse(self, mod):
        assert mod.parse_issue({"number": 1, "author_association": "MEMBER"}).author_association \
            == "MEMBER"
        assert mod.parse_issue({"number": 1, "authorAssociation": "OWNER"}).author_association \
            == "OWNER"

    def test_a_missing_association_is_empty_not_none(self, mod):
        assert mod.parse_issue({"number": 1}).author_association == ""

    def test_the_fetch_asks_for_the_rest_field_name(self, mod):
        # The endpoint is REST, so `author_association` is the spelling that actually arrives;
        # asking only for the camelCase name yields null and every issue reads as untrusted.
        import inspect
        assert "author_association" in inspect.getsource(mod.GitHubClient.list_open_issues)
