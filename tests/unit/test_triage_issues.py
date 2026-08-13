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


# ══════════════════════════ hourly mode (Part A) ══════════════════════════════


def _load_fresh_module(monkeypatch=None, env=None):
    """Load a NEW instance of triage_issues.py (not the module-scoped `mod` fixture).

    Needed for anything read at module import time (`TRUSTED_ASSOCIATIONS`), since the shared
    `mod` fixture is loaded once per test module and env changes after that point are invisible to
    already-bound module-level constants.
    """
    for key, value in (env or {}).items():
        if monkeypatch is not None:
            monkeypatch.setenv(key, value)
    spec = importlib.util.spec_from_file_location("triage_issues_fresh", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["triage_issues_fresh"] = module
    spec.loader.exec_module(module)
    return module


class TestOwnerAndRiskLabelInjectionIsStripped:
    """The gap this plan's Part A closes.

    `select_topical_labels()` filtered `PRIORITY_LABELS`/`FLOW_LABELS` but never `OWNER_LABELS` —
    an adversarial (or hallucinated) `topical_labels` payload containing `"agent:model:opus"`
    passed every filter and got written. Fixed in the SHARED function both the daily and hourly
    paths call.
    """

    def test_agent_model_opus_is_stripped_even_when_it_is_a_real_repo_label(self, mod):
        # The repo's real label vocabulary DOES contain "agent:model:opus" — `allowed` membership
        # alone was never going to be the gap; the label needs excluding regardless of whether it
        # is a real, existing label in the repo.
        decision = SimpleNamespace(topical_labels=["bug", "agent:model:opus"])
        allowed = {"bug", "agent:model:opus"}
        result = mod.select_topical_labels(decision, [], allowed)
        assert result == ["bug"]
        assert "agent:model:opus" not in result

    @pytest.mark.parametrize("owner_label", [
        "agent:model:sonnet", "agent:model:haiku", "agent:model:opus",
        "agent:model:claude-fable-5",  # not a real value today, but the prefix check must not care
    ])
    def test_every_agent_model_value_present_or_future_is_stripped(self, mod, owner_label):
        decision = SimpleNamespace(topical_labels=[owner_label])
        result = mod.select_topical_labels(decision, [], {owner_label})
        assert result == []

    @pytest.mark.parametrize("risk_label", [
        "risk:migration", "risk:security", "risk:live-linkedin", "risk:product-decision",
    ])
    def test_every_risk_value_is_stripped(self, mod, risk_label):
        decision = SimpleNamespace(topical_labels=[risk_label])
        result = mod.select_topical_labels(decision, [], {risk_label})
        assert result == []

    def test_a_mixed_adversarial_payload_keeps_only_the_legitimate_labels(self, mod):
        # The realistic shape of the gap: an LLM response (hallucinated OR prompt-injected from the
        # untrusted issue body) mixing real topical labels with owner-only ones in one list.
        decision = SimpleNamespace(
            topical_labels=["bug", "agent:model:opus", "observability", "risk:security", "ui"]
        )
        allowed = {"bug", "agent:model:opus", "observability", "risk:security", "ui"}
        result = mod.select_topical_labels(decision, [], allowed)
        assert result == ["bug", "observability", "ui"]

    def test_select_priority_label_rejects_a_non_priority_value_too(self, mod):
        # `decision.priority` is the SAME shape of LLM output `topical_labels` is — an unvalidated
        # pass-through here is the identical injection gap, just via a different JSON field.
        decision = SimpleNamespace(priority="agent:model:opus")
        assert mod.select_priority_label(decision, []) is None

    def test_select_priority_label_still_accepts_every_real_priority(self, mod):
        for p in ("priority:critical", "priority:high", "priority:medium", "priority:low"):
            decision = SimpleNamespace(priority=p)
            assert mod.select_priority_label(decision, []) == p


class TestTrustedAssociationsFromEnv:
    def test_default_matches_guards_sh(self, mod):
        # Same default `scripts/agent-pipeline/lib/guards.sh` hardcodes.
        assert mod.TRUSTED_ASSOCIATIONS == ("OWNER", "MEMBER", "COLLABORATOR")

    def test_env_override_is_read_space_separated_like_bash_word_splitting(self, monkeypatch):
        fresh = _load_fresh_module(monkeypatch, {"TRUSTED_ASSOCIATIONS": "OWNER MEMBER"})
        assert fresh.TRUSTED_ASSOCIATIONS == ("OWNER", "MEMBER")
        # And the narrower set actually changes trust decisions, not just the constant's value.
        d = fresh.TriageDecision(number=1, flow="agent:ready")
        assert fresh.select_flow_label(d, [], "COLLABORATOR") == "needs-human"

    def test_no_separately_hardcoded_python_tuple_survives(self, mod):
        # Regression guard against reintroducing a duplicated-knob literal beside the env read.
        import inspect
        src = inspect.getsource(mod)
        assert 'TRUSTED_ASSOCIATIONS = ("OWNER", "MEMBER", "COLLABORATOR")' not in src


class TestNeedsHourlyTriage:
    def test_missing_flow_label_needs_hourly_triage(self, mod):
        issue = _issue(mod, labels=["bug", "priority:high"])
        assert mod.needs_hourly_triage(issue) is True

    def test_flow_label_present_does_not_need_hourly_triage_even_with_other_gaps(self, mod):
        # Hourly scope is narrower than daily's `needs_triage`: missing milestone/priority/topical
        # alone must NOT pull an issue into the hourly pass — that reorg stays daily-only.
        issue = _issue(mod, labels=["needs-human"])
        assert mod.needs_triage(issue) is True          # daily would still triage it (missing prio)
        assert mod.needs_hourly_triage(issue) is False   # hourly ignores it — it has a flow label

    def test_closed_issue_is_out_of_hourly_scope(self, mod):
        issue = _issue(mod, labels=[])
        issue.state = "closed"
        assert mod.needs_hourly_triage(issue) is False

    def test_pull_request_is_out_of_hourly_scope(self, mod):
        issue = _issue(mod, labels=[])
        issue.is_pull_request = True
        assert mod.needs_hourly_triage(issue) is False


class TestIssueFingerprint:
    def test_same_updated_at_and_labels_give_the_same_fingerprint(self, mod):
        a = _issue(mod, number=1, labels=["bug"], updated_at="2026-08-01T00:00:00Z")
        b = _issue(mod, number=2, labels=["bug"], updated_at="2026-08-01T00:00:00Z")
        assert mod.issue_fingerprint(a) == mod.issue_fingerprint(b)

    def test_a_label_change_changes_the_fingerprint(self, mod):
        a = _issue(mod, labels=["bug"], updated_at="2026-08-01T00:00:00Z")
        b = _issue(mod, labels=["bug", "ui"], updated_at="2026-08-01T00:00:00Z")
        assert mod.issue_fingerprint(a) != mod.issue_fingerprint(b)

    def test_an_updated_at_change_changes_the_fingerprint(self, mod):
        a = _issue(mod, labels=["bug"], updated_at="2026-08-01T00:00:00Z")
        b = _issue(mod, labels=["bug"], updated_at="2026-08-02T00:00:00Z")
        assert mod.issue_fingerprint(a) != mod.issue_fingerprint(b)


class TestHourlyStateIO:
    def test_round_trips(self, mod, tmp_path):
        path = tmp_path / "state.json"
        mod.save_hourly_state(path, {"1": {"flow": "agent:ready"}})
        assert mod.load_hourly_state(path) == {"1": {"flow": "agent:ready"}}

    def test_missing_file_loads_empty(self, mod, tmp_path):
        assert mod.load_hourly_state(tmp_path / "nope.json") == {}

    def test_corrupt_file_loads_empty_rather_than_raising(self, mod, tmp_path):
        path = tmp_path / "state.json"
        path.write_text("not json", encoding="utf-8")
        assert mod.load_hourly_state(path) == {}


class TestPickReviewerModel:
    def test_picks_a_member_distinct_from_the_planner(self, mod):
        for served in mod.LEM_MEDIUM_MEMBERS:
            picked = mod.pick_reviewer_model(served)
            assert picked != served or served not in mod.LEM_MEDIUM_MEMBERS

    def test_a_none_or_unknown_served_model_still_yields_a_valid_member(self, mod):
        assert mod.pick_reviewer_model(None) in mod.LEM_MEDIUM_MEMBERS
        assert mod.pick_reviewer_model("some/unrelated-model") in mod.LEM_MEDIUM_MEMBERS

    def test_substring_match_catches_a_versioned_served_name(self, mod):
        # LiteLLM's `response.model` may come back with a build suffix; a candidate that is a
        # SUBSTRING of the served name must still be treated as "the same model".
        served = "openai/gpt-4o-mini-2026-08-01"
        picked = mod.pick_reviewer_model(served)
        assert picked != "openai/gpt-4o-mini"


class TestAdversarialReviewPromptAndParsing:
    def test_prompt_carries_the_same_issue_text_the_planner_saw(self, mod):
        issue = _issue(mod, number=42, title="t", body="x" * 2000)
        decision = mod.TriageDecision(number=42, priority="priority:high", flow="agent:ready",
                                      reason="planner said so")
        prompt = mod.build_adversarial_review_prompt([(issue, decision)])
        assert '"number": 42' in prompt
        assert "planner said so" in prompt
        # Truncated to 1200 chars, same as the planner's own Issue.to_dict().
        assert "x" * 1200 in prompt
        assert "x" * 1201 not in prompt

    def test_confirm_verdict_parses(self, mod):
        issue = _issue(mod, number=1)
        decision = mod.TriageDecision(number=1, flow="agent:ready")
        raw = json.dumps({"reviews": [{"number": 1, "verdict": "confirm", "reason": "fine"}]})
        assert mod.parse_adversarial_review(raw, [(issue, decision)]) == {1: "confirm"}

    def test_veto_verdict_parses(self, mod):
        issue = _issue(mod, number=1)
        decision = mod.TriageDecision(number=1, flow="agent:ready")
        raw = json.dumps({"reviews": [{"number": 1, "verdict": "veto", "reason": "too vague"}]})
        assert mod.parse_adversarial_review(raw, [(issue, decision)]) == {1: "veto"}

    def test_unreadable_response_fails_closed_to_veto(self, mod):
        issue = _issue(mod, number=1)
        decision = mod.TriageDecision(number=1, flow="agent:ready")
        for bad in (None, "", "not json", "{}"):
            assert mod.parse_adversarial_review(bad, [(issue, decision)]) == {1: "veto"}

    def test_an_issue_missing_from_the_response_fails_closed_to_veto(self, mod):
        issue1 = _issue(mod, number=1)
        issue2 = _issue(mod, number=2)
        d1 = mod.TriageDecision(number=1, flow="agent:ready")
        d2 = mod.TriageDecision(number=2, flow="agent:ready")
        raw = json.dumps({"reviews": [{"number": 1, "verdict": "confirm"}]})
        result = mod.parse_adversarial_review(raw, [(issue1, d1), (issue2, d2)])
        assert result == {1: "confirm", 2: "veto"}

    def test_markdown_fences_are_stripped(self, mod):
        issue = _issue(mod, number=1)
        decision = mod.TriageDecision(number=1, flow="agent:ready")
        raw = "```json\n" + json.dumps({"reviews": [{"number": 1, "verdict": "confirm"}]}) + "\n```"
        assert mod.parse_adversarial_review(raw, [(issue, decision)]) == {1: "confirm"}


class TestComputeAdmissionCap:
    """N = max(0, min(max_new_ready, target_inflight - current_inflight))."""

    def test_quiet_daemon_is_bounded_by_the_ceiling(self, mod):
        assert mod.compute_admission_cap(max_new_ready=2, target_inflight=10, current_inflight=0) == 2

    def test_nearly_full_queue_is_bounded_by_the_top_up_amount(self, mod):
        assert mod.compute_admission_cap(max_new_ready=5, target_inflight=3, current_inflight=2) == 1

    def test_full_queue_admits_zero(self, mod):
        assert mod.compute_admission_cap(max_new_ready=2, target_inflight=3, current_inflight=3) == 0

    def test_over_full_queue_never_goes_negative(self, mod):
        assert mod.compute_admission_cap(max_new_ready=2, target_inflight=3, current_inflight=9) == 0

    def test_exactly_one_slot_admits_exactly_one(self, mod):
        assert mod.compute_admission_cap(max_new_ready=2, target_inflight=1, current_inflight=0) == 1

    def test_unreadable_inflight_fails_closed_to_zero(self, mod):
        assert mod.compute_admission_cap(max_new_ready=2, target_inflight=10, current_inflight=None) == 0


class TestRankEligibleForAdmission:
    def test_priority_then_age_orders_admission(self, mod):
        old_high = _issue(mod, number=1)
        # `_issue` doesn't take created_at, so set it directly for the age comparison.
        old_high.created_at = "2026-01-01T00:00:00Z"
        new_high = _issue(mod, number=2)
        new_high.created_at = "2026-06-01T00:00:00Z"
        low = _issue(mod, number=3)
        low.created_at = "2026-01-01T00:00:00Z"

        pairs = [
            (low, mod.TriageDecision(number=3, priority="priority:low", flow="agent:ready")),
            (new_high, mod.TriageDecision(number=2, priority="priority:high", flow="agent:ready")),
            (old_high, mod.TriageDecision(number=1, priority="priority:high", flow="agent:ready")),
        ]
        admitted, pending, cap_hit = mod.rank_eligible_for_admission(pairs, cap=2)
        assert [issue.number for issue, _ in admitted] == [1, 2]   # both "high", older first
        assert [issue.number for issue, _ in pending] == [3]
        assert cap_hit is True

    def test_cap_zero_admits_nothing_and_hits_the_cap(self, mod):
        issue = _issue(mod, number=1)
        pairs = [(issue, mod.TriageDecision(number=1, priority="priority:high", flow="agent:ready"))]
        admitted, pending, cap_hit = mod.rank_eligible_for_admission(pairs, cap=0)
        assert admitted == []
        assert pending == pairs
        assert cap_hit is True

    def test_cap_covering_everyone_does_not_hit_the_cap(self, mod):
        issue = _issue(mod, number=1)
        pairs = [(issue, mod.TriageDecision(number=1, priority="priority:high", flow="agent:ready"))]
        admitted, pending, cap_hit = mod.rank_eligible_for_admission(pairs, cap=5)
        assert len(admitted) == 1
        assert pending == []
        assert cap_hit is False

    def test_unknown_priority_sorts_after_every_known_priority(self, mod):
        known = _issue(mod, number=1)
        unknown = _issue(mod, number=2)
        pairs = [
            (unknown, mod.TriageDecision(number=2, priority=None, flow="agent:ready")),
            (known, mod.TriageDecision(number=1, priority="priority:low", flow="agent:ready")),
        ]
        admitted, _pending, _hit = mod.rank_eligible_for_admission(pairs, cap=2)
        assert [issue.number for issue, _ in admitted] == [1, 2]


class TestCurrentInflightCount:
    def test_missing_queue_db_fails_closed_to_none(self, mod, tmp_path):
        assert mod.current_inflight_count(tmp_path / "nope" / "queue.db") is None

    def test_reads_the_daemons_own_wip_count(self, mod, tmp_path):
        v2_dir = Path(__file__).resolve().parents[2] / "scripts" / "agent-pipeline" / "v2"
        if str(v2_dir) not in sys.path:
            sys.path.insert(0, str(v2_dir))
        from lemd import db as lemd_db  # noqa: E402 (conditional path setup above)

        db_path = tmp_path / "queue.db"
        conn = lemd_db.connect(db_path)
        lemd_db.upsert_item(conn, kind="pr", number=1, state=lemd_db.STATE_RUNNING)
        lemd_db.upsert_item(conn, kind="pr", number=2, state=lemd_db.STATE_MERGED)  # not WIP
        lemd_db.upsert_item(conn, kind="issue", number=3, state=lemd_db.STATE_READY)  # not a PR
        expected = lemd_db.wip_count(conn)
        conn.close()

        assert expected == 1
        assert mod.current_inflight_count(db_path) == expected


class TestAcquireTriageLock:
    def test_acquire_and_release_round_trips(self, mod, tmp_path):
        lock_dir = tmp_path / "locks"
        with mod.acquire_triage_lock(lock_dir):
            assert (lock_dir / "triage.lock").exists()
        # Released — a second acquire must succeed.
        with mod.acquire_triage_lock(lock_dir):
            pass

    def test_a_second_concurrent_holder_is_refused(self, mod, tmp_path):
        lock_dir = tmp_path / "locks"
        with mod.acquire_triage_lock(lock_dir):
            with pytest.raises(mod.TriageLockBusy):
                with mod.acquire_triage_lock(lock_dir):
                    pass  # pragma: no cover — must never be entered


# ─────────────────────── main_hourly() end-to-end ─────────────────────────────


def _hourly_llm_factory(calls, planner_json, reviewer_json):
    """Build a `mod.LLMClient` replacement for --hourly's two calls.

    Distinguishes the planner call from the reviewer call by the `model=` kwarg (planner always
    requests `DEFAULT_LLM_MODEL`; the reviewer is pinned to one of `LEM_MEDIUM_MEMBERS`), and
    records every call for assertions on LLM spend.
    """
    def factory(**kwargs):
        model = kwargs.get("model")
        client = MagicMock()
        client.last_model = model
        is_planner = model == "lem-medium"

        def classify(prompt):
            calls.append(("planner" if is_planner else "reviewer", prompt))
            return planner_json() if is_planner else reviewer_json()

        client.classify = classify
        return client
    return factory


class TestMainHourlyEndToEnd:
    def _gh(self, issues, labels=None):
        gh = MagicMock()
        gh.list_open_issues.return_value = issues
        gh.list_milestones.return_value = []
        gh.list_labels.return_value = labels or ["bug", "priority:critical", "priority:high",
                                                  "priority:medium", "priority:low"]
        return gh

    def test_planner_and_reviewer_agree_admits_agent_ready(self, mod, monkeypatch, tmp_path):
        issue = _issue(mod, number=1, title="fix the thing", labels=[], author_association="OWNER")
        gh = self._gh([issue])
        monkeypatch.setattr(mod, "GitHubClient", lambda repo: gh)
        calls = []

        def planner():
            return json.dumps({"issues": [
                {"number": 1, "priority": "priority:high", "flow": "agent:ready",
                 "topical_labels": [], "reason": "clear scope"}], "proposed_milestones": []})

        def reviewer():
            return json.dumps({"reviews": [{"number": 1, "verdict": "confirm"}]})

        monkeypatch.setattr(mod, "LLMClient", _hourly_llm_factory(calls, planner, reviewer))
        monkeypatch.setenv("TRIAGE_HOURLY_MAX_NEW_READY", "2")
        monkeypatch.setenv("TRIAGE_HOURLY_TARGET_INFLIGHT", "2")
        monkeypatch.setattr(mod, "current_inflight_count", lambda path: 0)

        rc = mod.main(["--hourly", "--apply", "--repo", "o/r",
                       "--report-dir", str(tmp_path / "reports"),
                       "--state-file", str(tmp_path / "state.json"),
                       "--lock-dir", str(tmp_path / "locks")])
        assert rc == 0
        gh.add_labels.assert_called_once_with(1, ["priority:high", "agent:ready"])
        assert [kind for kind, _ in calls] == ["planner", "reviewer"]

    def test_reviewer_veto_downgrades_to_needs_human_not_agent_ready(self, mod, monkeypatch, tmp_path):
        issue = _issue(mod, number=1, title="vague", labels=[], author_association="OWNER")
        gh = self._gh([issue])
        monkeypatch.setattr(mod, "GitHubClient", lambda repo: gh)
        calls = []

        def planner():
            return json.dumps({"issues": [
                {"number": 1, "priority": "priority:high", "flow": "agent:ready",
                 "topical_labels": [], "reason": "planner thought it was fine"}],
                "proposed_milestones": []})

        def reviewer():
            return json.dumps({"reviews": [
                {"number": 1, "verdict": "veto", "reason": "underspecified scope"}]})

        monkeypatch.setattr(mod, "LLMClient", _hourly_llm_factory(calls, planner, reviewer))
        monkeypatch.setattr(mod, "current_inflight_count", lambda path: 0)

        rc = mod.main(["--hourly", "--apply", "--repo", "o/r",
                       "--report-dir", str(tmp_path / "reports"),
                       "--state-file", str(tmp_path / "state.json"),
                       "--lock-dir", str(tmp_path / "locks")])
        assert rc == 0
        gh.add_labels.assert_called_once_with(1, ["priority:high", "needs-human"])

    def test_cap_admits_only_n_and_leaves_the_rest_unlabeled(self, mod, monkeypatch, tmp_path):
        issues = [_issue(mod, number=n, title=f"issue {n}", labels=[], author_association="OWNER")
                 for n in (1, 2, 3)]
        gh = self._gh(issues)
        monkeypatch.setattr(mod, "GitHubClient", lambda repo: gh)
        calls = []

        def planner():
            return json.dumps({"issues": [
                {"number": n, "priority": "priority:high", "flow": "agent:ready",
                 "topical_labels": [], "reason": "r"} for n in (1, 2, 3)],
                "proposed_milestones": []})

        def reviewer():
            return json.dumps({"reviews": [
                {"number": n, "verdict": "confirm"} for n in (1, 2, 3)]})

        monkeypatch.setattr(mod, "LLMClient", _hourly_llm_factory(calls, planner, reviewer))
        monkeypatch.setattr(mod, "current_inflight_count", lambda path: 0)
        monkeypatch.setenv("TRIAGE_HOURLY_MAX_NEW_READY", "1")
        monkeypatch.setenv("TRIAGE_HOURLY_TARGET_INFLIGHT", "10")

        rc = mod.main(["--hourly", "--apply", "--repo", "o/r",
                       "--report-dir", str(tmp_path / "reports"),
                       "--state-file", str(tmp_path / "state.json"),
                       "--lock-dir", str(tmp_path / "locks")])
        assert rc == 0
        # Cap is 1: exactly one gets `agent:ready`; the same-priority tie breaks on age (all equal
        # here, so it's the stable-sort winner) — the important assertion is the COUNT.
        assert gh.add_labels.call_count == 1
        (called_number, called_labels), _ = gh.add_labels.call_args
        assert called_labels == ["priority:high", "agent:ready"]
        assert called_number in (1, 2, 3)

    def test_trust_downgrade_before_cap_frees_the_slot_for_the_next_candidate(self, mod, monkeypatch,
                                                                              tmp_path):
        # #2 would rank FIRST by priority (critical) if trusted, but its author is untrusted. With
        # the correct ordering it is downgraded to needs-human BEFORE the cap is applied, so it
        # never consumes one of the two admission slots — #1 (high) and #3 (medium) both get in.
        # A buggy "cap first, downgrade after" implementation would admit only #1, wasting a slot
        # on #2's doomed-to-fail agent:ready.
        issue1 = _issue(mod, number=1, title="high", labels=[], author_association="OWNER")
        issue2 = _issue(mod, number=2, title="critical but untrusted", labels=[],
                        author_association="NONE")
        issue3 = _issue(mod, number=3, title="medium", labels=[], author_association="MEMBER")
        issue4 = _issue(mod, number=4, title="low", labels=[], author_association="OWNER")
        gh = self._gh([issue1, issue2, issue3, issue4])
        monkeypatch.setattr(mod, "GitHubClient", lambda repo: gh)
        calls = []

        def planner():
            return json.dumps({"issues": [
                {"number": 1, "priority": "priority:high", "flow": "agent:ready",
                 "topical_labels": [], "reason": "r"},
                {"number": 2, "priority": "priority:critical", "flow": "agent:ready",
                 "topical_labels": [], "reason": "r"},
                {"number": 3, "priority": "priority:medium", "flow": "agent:ready",
                 "topical_labels": [], "reason": "r"},
                {"number": 4, "priority": "priority:low", "flow": "agent:ready",
                 "topical_labels": [], "reason": "r"},
            ], "proposed_milestones": []})

        def reviewer():
            return json.dumps({"reviews": [
                {"number": n, "verdict": "confirm"} for n in (1, 2, 3, 4)]})

        monkeypatch.setattr(mod, "LLMClient", _hourly_llm_factory(calls, planner, reviewer))
        monkeypatch.setattr(mod, "current_inflight_count", lambda path: 0)
        monkeypatch.setenv("TRIAGE_HOURLY_MAX_NEW_READY", "2")
        monkeypatch.setenv("TRIAGE_HOURLY_TARGET_INFLIGHT", "2")

        rc = mod.main(["--hourly", "--apply", "--repo", "o/r",
                       "--report-dir", str(tmp_path / "reports"),
                       "--state-file", str(tmp_path / "state.json"),
                       "--lock-dir", str(tmp_path / "locks")])
        assert rc == 0
        admitted_calls = {c.args[0]: c.args[1] for c in gh.add_labels.call_args_list}
        assert set(admitted_calls) == {1, 2, 3}
        assert admitted_calls[1] == ["priority:high", "agent:ready"]
        assert admitted_calls[3] == ["priority:medium", "agent:ready"]
        assert admitted_calls[2] == ["priority:critical", "needs-human"]  # trust-downgraded
        # #4 (low) never called at all — eligible but held back for a later hour.
        assert 4 not in admitted_calls

    def test_no_agent_model_label_is_ever_written_by_the_hourly_path(self, mod, monkeypatch,
                                                                     tmp_path):
        # Even an adversarial planner+reviewer response naming an owner label as the "priority"
        # must never reach `gh.add_labels` — belt-and-suspenders alongside the pure-function tests.
        issue = _issue(mod, number=1, labels=[], author_association="OWNER")
        gh = self._gh([issue])
        monkeypatch.setattr(mod, "GitHubClient", lambda repo: gh)
        calls = []

        def planner():
            return json.dumps({"issues": [
                {"number": 1, "priority": "agent:model:opus", "flow": "agent:ready",
                 "topical_labels": ["agent:model:opus", "risk:security"], "reason": "r"}],
                "proposed_milestones": []})

        def reviewer():
            return json.dumps({"reviews": [{"number": 1, "verdict": "confirm"}]})

        monkeypatch.setattr(mod, "LLMClient", _hourly_llm_factory(calls, planner, reviewer))
        monkeypatch.setattr(mod, "current_inflight_count", lambda path: 0)

        mod.main(["--hourly", "--apply", "--repo", "o/r",
                 "--report-dir", str(tmp_path / "reports"),
                 "--state-file", str(tmp_path / "state.json"),
                 "--lock-dir", str(tmp_path / "locks")])
        for c in gh.add_labels.call_args_list:
            for label in c.args[1]:
                assert not label.startswith("agent:model:")
                assert not label.startswith("risk:")

    def test_memoized_issue_is_not_replanned_on_the_next_run(self, mod, monkeypatch, tmp_path):
        issue = _issue(mod, number=1, title="steady", labels=[], updated_at="2026-08-01T00:00:00Z",
                       author_association="OWNER")
        gh = self._gh([issue])
        monkeypatch.setattr(mod, "GitHubClient", lambda repo: gh)
        calls = []

        def planner():
            return json.dumps({"issues": [
                {"number": 1, "priority": "priority:high", "flow": "agent:ready",
                 "topical_labels": [], "reason": "r"}], "proposed_milestones": []})

        def reviewer():
            return json.dumps({"reviews": [{"number": 1, "verdict": "confirm"}]})

        monkeypatch.setattr(mod, "LLMClient", _hourly_llm_factory(calls, planner, reviewer))
        monkeypatch.setattr(mod, "current_inflight_count", lambda path: 0)
        # Cap at 0 so the first run leaves the issue admitted-eligible-but-unlabeled — its flow
        # label never lands on the (static) mock, so it is still "missing a flow label" on the
        # second pass, which is exactly the scenario memoization has to survive.
        monkeypatch.setenv("TRIAGE_HOURLY_MAX_NEW_READY", "0")

        state_file = tmp_path / "state.json"
        common_args = ["--hourly", "--apply", "--repo", "o/r",
                       "--report-dir", str(tmp_path / "reports"),
                       "--state-file", str(state_file), "--lock-dir", str(tmp_path / "locks")]
        mod.main(common_args)
        assert len(calls) == 2  # one planner + one reviewer call

        mod.main(common_args)
        assert len(calls) == 2  # unchanged: NO new planner/reviewer call on the second pass

    def test_a_changed_issue_is_replanned(self, mod, monkeypatch, tmp_path):
        issue = _issue(mod, number=1, labels=[], updated_at="2026-08-01T00:00:00Z",
                       author_association="OWNER")
        gh = self._gh([issue])
        monkeypatch.setattr(mod, "GitHubClient", lambda repo: gh)
        calls = []

        def planner():
            return json.dumps({"issues": [
                {"number": 1, "priority": "priority:high", "flow": "agent:ready",
                 "topical_labels": [], "reason": "r"}], "proposed_milestones": []})

        def reviewer():
            return json.dumps({"reviews": [{"number": 1, "verdict": "confirm"}]})

        monkeypatch.setattr(mod, "LLMClient", _hourly_llm_factory(calls, planner, reviewer))
        monkeypatch.setattr(mod, "current_inflight_count", lambda path: 0)
        monkeypatch.setenv("TRIAGE_HOURLY_MAX_NEW_READY", "0")

        state_file = tmp_path / "state.json"
        common_args = ["--hourly", "--apply", "--repo", "o/r",
                       "--report-dir", str(tmp_path / "reports"),
                       "--state-file", str(state_file), "--lock-dir", str(tmp_path / "locks")]
        mod.main(common_args)
        assert len(calls) == 2

        issue.updated_at = "2026-08-02T00:00:00Z"  # the issue changed since the last verdict
        mod.main(common_args)
        assert len(calls) == 4  # re-planned + re-reviewed


class TestHourlyApplySharesTheDailyLock:
    """Shared lock, not mode-scoped.

    A manually-triggered DAILY run and an in-progress HOURLY tick must never both compute
    admission math off the same stale GitHub snapshot.
    """

    def _gh(self):
        gh = MagicMock()
        gh.list_open_issues.return_value = []
        gh.list_milestones.return_value = []
        gh.list_labels.return_value = []
        return gh

    def test_daily_apply_is_refused_while_hourly_holds_the_lock(self, mod, monkeypatch, tmp_path):
        gh = self._gh()
        monkeypatch.setattr(mod, "GitHubClient", lambda repo: gh)
        lock_dir = tmp_path / "locks"
        with mod.acquire_triage_lock(lock_dir):
            rc = mod.main(["--apply", "--repo", "o/r", "--report-dir", str(tmp_path / "reports"),
                          "--lock-dir", str(lock_dir)])
        assert rc == 1
        gh.add_labels.assert_not_called()

    def test_hourly_apply_is_refused_while_daily_holds_the_lock(self, mod, monkeypatch, tmp_path):
        gh = self._gh()
        monkeypatch.setattr(mod, "GitHubClient", lambda repo: gh)
        monkeypatch.setattr(mod, "current_inflight_count", lambda path: 0)
        lock_dir = tmp_path / "locks"
        with mod.acquire_triage_lock(lock_dir):
            rc = mod.main(["--hourly", "--apply", "--repo", "o/r",
                          "--report-dir", str(tmp_path / "reports"),
                          "--state-file", str(tmp_path / "state.json"),
                          "--lock-dir", str(lock_dir)])
        assert rc == 1
        gh.add_labels.assert_not_called()

    def test_dry_run_never_needs_the_lock(self, mod, monkeypatch, tmp_path):
        # Verification workflow: `--hourly` with no `--apply` must work even while a real apply
        # run holds the lock, since it never writes anything.
        gh = self._gh()
        monkeypatch.setattr(mod, "GitHubClient", lambda repo: gh)
        monkeypatch.setattr(mod, "current_inflight_count", lambda path: 0)
        lock_dir = tmp_path / "locks"
        with mod.acquire_triage_lock(lock_dir):
            rc = mod.main(["--hourly", "--repo", "o/r", "--report-dir", str(tmp_path / "reports"),
                          "--state-file", str(tmp_path / "state.json"),
                          "--lock-dir", str(lock_dir)])
        assert rc == 0
