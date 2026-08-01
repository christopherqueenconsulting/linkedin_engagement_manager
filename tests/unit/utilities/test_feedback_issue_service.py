"""Unit tests for the feedback auto-filer — issue #498.

Covers the deterministic halves exhaustively (similarity, dedup selection, label logic, MODE=start
body shape, Decision Comment shape) and the orchestrator with GitHub + DB + the classifier mocked.
"""

import json
import pytest
from unittest.mock import MagicMock, patch

pytestmark = pytest.mark.unit

_SVC = "cqc_lem.utilities.feedback.issue_service"

# The labels this module can emit must all exist in the repo (gh label list) — same guard as #497.
REAL_REPO_LABELS = {
    'bug', 'feature', 'enhancement', 'cleanup', 'question', 'documentation', 'duplicate',
    'feedback-loop', 'priority:critical', 'priority:high', 'priority:medium', 'priority:low',
    'risk:product-decision', 'risk:live-linkedin', 'risk:migration', 'risk:security',
    'needs-human', 'agent:ready', 'agent:blocked', 'agent:working',
}


def _mod():
    from cqc_lem.utilities.feedback import issue_service
    return issue_service


@pytest.fixture(autouse=True)
def _clear_transport_cool_off():
    """Issue #767: the unreachable cool-off is process-global by design, so one test's simulated
    outage would silently short-circuit the next test's request."""
    _mod()._unreachable_until = 0.0
    yield
    _mod()._unreachable_until = 0.0


def _classification(**overrides):
    from cqc_lem.utilities.feedback.classifier import (
        FeedbackCategory, FeedbackClassification, FeedbackRisk, FeedbackSeverity)
    kwargs = {
        "category": FeedbackCategory.BUG,
        "severity": FeedbackSeverity.HIGH,
        "component": "feed-commenting",
        "title": "Stop feed commenting after the first comment",
        "summary": "Only one comment is posted per automation run.",
        "risk": FeedbackRisk.NONE,
        "duplicate_of": None,
        "confidence": 0.9,
    }
    kwargs.update(overrides)
    return FeedbackClassification(**kwargs)


def _cluster(cluster_id=7, body="comments never post to the feed", issue=101,
             embedding=None, reporters=1, items=1):
    return {"cluster_id": cluster_id, "body": body, "github_issue_number": issue,
            "embedding": embedding, "reporter_count": reporters, "item_count": items}


class TestCosineSimilarity:
    def test_identical_vectors_score_one_and_orthogonal_score_zero(self):
        svc = _mod()
        assert svc.cosine_similarity([1.0, 0.0, 2.0], [1.0, 0.0, 2.0]) == pytest.approx(1.0)
        assert svc.cosine_similarity([1.0, 0.0], [0.0, 1.0]) == 0.0

    def test_scaled_vector_is_still_identical(self):
        assert _mod().cosine_similarity([1.0, 2.0], [3.0, 6.0]) == pytest.approx(1.0)

    def test_opposite_vectors_clamp_to_zero(self):
        assert _mod().cosine_similarity([1.0, 1.0], [-1.0, -1.0]) == 0.0

    @pytest.mark.parametrize("a,b", [
        ([1.0, 2.0], [1.0]),      # length mismatch
        ([], [1.0]),              # empty
        (None, [1.0]),            # missing
        ([0.0, 0.0], [1.0, 1.0]),  # zero norm
    ])
    def test_unusable_inputs_are_zero_not_an_error(self, a, b):
        assert _mod().cosine_similarity(a, b) == 0.0

    def test_non_numeric_values_are_zero(self):
        assert _mod().cosine_similarity([1.0, "x"], [1.0, 2.0]) == 0.0


class TestAsVector:
    def test_parses_json_string_and_list(self):
        svc = _mod()
        assert svc.as_vector("[1, 2.5]") == [1.0, 2.5]
        assert svc.as_vector([1, 2]) == [1.0, 2.0]

    @pytest.mark.parametrize("raw", ["not json", "[]", [], None, {"a": 1}, ["x"], "{}"])
    def test_junk_becomes_none(self, raw):
        assert _mod().as_vector(raw) is None


class TestSimilarity:
    def test_uses_cosine_when_both_embeddings_exist(self):
        # Texts share no vocabulary; only the vectors can say they are the same problem.
        score = _mod().similarity("comments never post", "replies do not go through",
                                  [1.0, 0.0], [1.0, 0.0])
        assert score == pytest.approx(1.0)

    def test_falls_back_to_token_overlap_when_an_embedding_is_missing(self):
        svc = _mod()
        from cqc_lem.utilities.ai.content_framework import text_similarity
        a, b = "feed commenting stops after one comment", "commenting stops after the first comment"
        assert svc.similarity(a, b, [1.0, 0.0], None) == pytest.approx(text_similarity(a, b))
        assert svc.similarity(a, b) > 0.5

    def test_mismatched_embedding_lengths_fall_back_instead_of_scoring_zero(self):
        svc = _mod()
        same = "feed commenting stops after one comment"
        assert svc.similarity(same, same, [1.0, 0.0], [1.0, 0.0, 0.0]) == pytest.approx(1.0)

    def test_unrelated_texts_are_not_duplicates(self):
        assert _mod().similarity("billing charged me twice", "add dark mode to the UI") < 0.3


class TestFindDuplicateCluster:
    def test_returns_the_best_matching_cluster_above_threshold(self):
        svc = _mod()
        clusters = [_cluster(1, "billing charged me twice", 11, embedding=[0.0, 1.0]),
                    _cluster(2, "comments never post", 22, embedding=[1.0, 0.0])]
        match, score = svc.find_duplicate_cluster("replies do not post", [1.0, 0.0], clusters)
        assert match["cluster_id"] == 2
        assert score == pytest.approx(1.0)

    def test_below_threshold_is_not_a_duplicate_but_reports_the_best_score(self):
        svc = _mod()
        clusters = [_cluster(1, "billing charged me twice", 11, embedding=[0.0, 1.0])]
        match, score = svc.find_duplicate_cluster("comments never post", [1.0, 0.0], clusters)
        assert match is None
        assert score == 0.0

    def test_cluster_without_a_filed_issue_is_skipped(self):
        svc = _mod()
        clusters = [_cluster(1, "comments never post", issue=None, embedding=[1.0, 0.0])]
        match, _ = svc.find_duplicate_cluster("comments never post", [1.0, 0.0], clusters)
        assert match is None

    def test_empty_and_none_cluster_lists_are_safe(self):
        svc = _mod()
        assert svc.find_duplicate_cluster("x", None, None) == (None, 0.0)
        assert svc.find_duplicate_cluster("x", None, [None]) == (None, 0.0)

    def test_threshold_override_is_honored(self):
        svc = _mod()
        clusters = [_cluster(1, "feed commenting stops after one comment", 11)]
        text = "comments stop after one comment"
        assert svc.find_duplicate_cluster(text, None, clusters)[0] is None
        assert svc.find_duplicate_cluster(text, None, clusters,
                                          threshold=0.2)[0]["cluster_id"] == 1

    def test_env_threshold_is_read_at_call_time(self, monkeypatch):
        svc = _mod()
        clusters = [_cluster(1, "feed commenting stops after one comment", 11)]
        monkeypatch.setenv("FEEDBACK_DUPLICATE_SIMILARITY", "0.2")
        assert svc.duplicate_similarity_min() == pytest.approx(0.2)
        assert svc.find_duplicate_cluster("comments stop after one comment", None,
                                          clusters)[0] is not None


class TestClusterLookupHelpers:
    def test_cluster_by_id_honors_the_classifier_verdict(self):
        svc = _mod()
        clusters = [_cluster(3, issue=33)]
        assert svc.cluster_by_id(clusters, 3)["github_issue_number"] == 33
        assert svc.cluster_by_id(clusters, 4) is None
        assert svc.cluster_by_id(clusters, None) is None

    def test_cluster_by_id_ignores_clusters_with_no_issue(self):
        assert _mod().cluster_by_id([_cluster(3, issue=None)], 3) is None

    def test_duplicate_candidates_shape_matches_the_classifier_contract(self):
        svc = _mod()
        got = svc.duplicate_candidates([_cluster(5, "billing double charge", 55),
                                        _cluster(6, "no issue yet", None)])
        assert got == [{"id": 5, "title": "billing double charge"}]


class TestEmbedText:
    def test_returns_the_provider_vector(self):
        svc = _mod()
        client = MagicMock()
        client.embeddings.create.return_value = MagicMock(
            data=[MagicMock(embedding=[0.1, 0.2])])
        with patch("cqc_lem.utilities.ai.client.client", client):
            assert svc.embed_text("comments never post") == [0.1, 0.2]
        assert client.embeddings.create.call_args.kwargs["model"] == "lem-embedding"

    def test_provider_failure_returns_none_so_dedup_falls_back(self):
        svc = _mod()
        client = MagicMock()
        client.embeddings.create.side_effect = RuntimeError("proxy down")
        with patch("cqc_lem.utilities.ai.client.client", client):
            assert svc.embed_text("comments never post") is None

    def test_empty_provider_vector_is_none(self):
        svc = _mod()
        client = MagicMock()
        client.embeddings.create.return_value = MagicMock(data=[MagicMock(embedding=[])])
        with patch("cqc_lem.utilities.ai.client.client", client):
            assert svc.embed_text("comments never post") is None

    def test_blank_body_never_calls_the_provider(self):
        svc = _mod()
        client = MagicMock()
        with patch("cqc_lem.utilities.ai.client.client", client):
            assert svc.embed_text("   ") is None
            assert svc.embed_text(None) is None
        client.embeddings.create.assert_not_called()

    def test_model_alias_is_configurable(self, monkeypatch):
        svc = _mod()
        monkeypatch.setenv("FEEDBACK_EMBEDDING_MODEL", "other-embed")
        assert svc.embedding_model() == "other-embed"


class TestLabelLogic:
    def test_confident_riskless_bug_is_agent_ready(self):
        svc = _mod()
        labels = svc.labels_for_issue(_classification())
        assert labels == ['bug', 'priority:high', 'feedback-loop', 'agent:ready']
        assert svc.hold_reason(_classification()) is None

    def test_risky_item_gets_the_risk_label_needs_human_and_no_agent_ready(self):
        from cqc_lem.utilities.feedback.classifier import FeedbackRisk
        svc = _mod()
        labels = svc.labels_for_issue(_classification(risk=FeedbackRisk.MIGRATION))
        assert 'risk:migration' in labels
        assert 'needs-human' in labels
        assert 'agent:ready' not in labels

    def test_confidence_below_the_agent_ready_floor_is_held(self):
        svc = _mod()
        held = _classification(confidence=0.65)
        assert svc.labels_for_issue(held) == ['bug', 'priority:high', 'feedback-loop',
                                             'needs-human']
        assert svc.hold_reason(held) == 'low-confidence'

    def test_feature_needs_demand_before_it_may_auto_work(self):
        from cqc_lem.utilities.feedback.classifier import FeedbackCategory
        svc = _mod()
        feature = _classification(category=FeedbackCategory.FEATURE, confidence=0.95)
        assert 'agent:ready' not in svc.labels_for_issue(feature, reporter_count=1)
        assert svc.hold_reason(feature, reporter_count=1) == 'unproven-demand'
        assert 'agent:ready' in svc.labels_for_issue(feature, reporter_count=2)

    def test_bug_auto_files_on_a_single_report(self):
        svc = _mod()
        assert svc.feature_demand_met(_classification().category, 1) is True

    def test_demand_threshold_is_configurable(self, monkeypatch):
        from cqc_lem.utilities.feedback.classifier import FeedbackCategory
        svc = _mod()
        monkeypatch.setenv("FEEDBACK_FEATURE_DEMAND_MIN", "5")
        feature = _classification(category=FeedbackCategory.FEATURE, confidence=0.95)
        assert 'agent:ready' not in svc.labels_for_issue(feature, reporter_count=4)
        assert 'agent:ready' in svc.labels_for_issue(feature, reporter_count=5)

    @pytest.mark.parametrize("risk", ['none', 'product-decision', 'live-linkedin', 'migration',
                                      'security'])
    @pytest.mark.parametrize("confidence", [0.6, 0.7, 1.0])
    def test_every_emitted_label_exists_in_the_repo(self, risk, confidence):
        from cqc_lem.utilities.feedback.classifier import FeedbackRisk
        svc = _mod()
        labels = svc.labels_for_issue(_classification(risk=FeedbackRisk(risk),
                                                     confidence=confidence))
        assert set(labels) <= REAL_REPO_LABELS
        # Exactly one of the two mutually exclusive pipeline verdicts.
        assert ('agent:ready' in labels) != ('needs-human' in labels)

    def test_labels_are_deduplicated(self):
        svc = _mod()
        labels = svc.labels_for_issue(_classification(confidence=0.1))
        assert len(labels) == len(set(labels))


class TestIssueTitle:
    @pytest.mark.parametrize("category,expected", [
        ('bug', 'fix'), ('feature', 'feat'), ('enhancement', 'feat'),
        ('cleanup', 'chore'), ('question', 'docs'),
    ])
    def test_conventional_commit_type_per_category(self, category, expected):
        from cqc_lem.utilities.feedback.classifier import FeedbackCategory
        svc = _mod()
        title = svc.build_issue_title(_classification(category=FeedbackCategory(category)))
        assert title.startswith(f"{expected}(feed-commenting): ")

    def test_title_is_bounded(self):
        svc = _mod()
        got = svc.build_issue_title(_classification(title="x" * 300))
        assert len(got) <= svc.MAX_TITLE_CHARS

    def test_missing_title_falls_back_to_the_summary(self):
        svc = _mod()
        assert "Only one comment" in svc.build_issue_title(_classification(title=""))


class TestIssueBody:
    def test_body_has_the_mode_start_sections_in_order(self):
        svc = _mod()
        body = svc.build_issue_body(_classification(), feedback_id=42)
        for section in ("## Why", "## Scope", "## Files", "## Acceptance"):
            assert section in body
        assert (body.index("## Why") < body.index("## Scope")
                < body.index("## Files") < body.index("## Acceptance"))

    def test_body_carries_provenance_the_plan_doc_and_the_feedback_id(self):
        svc = _mod()
        body = svc.build_issue_body(_classification(), feedback_id=42)
        assert svc.PLAN_DOC in body
        assert "#42" in body
        assert "bug/high" in body
        assert "confidence 0.90" in body

    def test_body_never_leaks_the_raw_report_only_the_summary(self):
        svc = _mod()
        body = svc.build_issue_body(_classification(summary="Only one comment posts."),
                                    feedback_id=1)
        assert "Only one comment posts." in body

    def test_files_hint_points_at_the_component(self):
        svc = _mod()
        body = svc.build_issue_body(_classification(), feedback_id=1)
        assert "src/cqc_lem/app/run_automation.py" in body
        assert "tests/unit/" in body

    def test_unknown_component_still_hints_the_tests(self):
        svc = _mod()
        body = svc.build_issue_body(_classification(component="other"), feedback_id=1)
        assert "tests/unit/" in body

    def test_migration_risk_adds_the_timestamp_naming_rule(self):
        from cqc_lem.utilities.feedback.classifier import FeedbackRisk
        svc = _mod()
        body = svc.build_issue_body(_classification(risk=FeedbackRisk.MIGRATION), feedback_id=1)
        assert "V<YYYYMMDDHHMMSS>__short_name.sql" in body
        assert "Decision Comment" in body

    def test_agent_ready_body_has_no_decision_gate_in_acceptance(self):
        svc = _mod()
        body = svc.build_issue_body(_classification(), feedback_id=1)
        assert "Decision Comment" not in body

    def test_demand_line_reflects_the_cluster(self):
        svc = _mod()
        body = svc.build_issue_body(_classification(), feedback_id=1, reporter_count=4,
                                    item_count=9)
        assert "4 distinct user(s) across 9 feedback item(s)" in body


class TestReplayLink:
    _SESSION = "0198f0aa-1b2c-7000-8000-abcdef012345"

    def _url(self, monkeypatch):
        monkeypatch.setenv("POSTHOG_PROJECT_ID", "475262")
        monkeypatch.setenv("POSTHOG_APP_HOST", "https://us.posthog.com")
        return f"https://us.posthog.com/project/475262/replay/{self._SESSION}"

    def test_context_session_id_becomes_a_replay_url(self, monkeypatch):
        expected = self._url(monkeypatch)
        assert _mod().replay_url_from_context(
            {"route": "/content", "posthog_session_id": self._SESSION}) == expected

    @pytest.mark.parametrize("context", [None, {}, "not-a-dict", {"posthog_session_id": None},
                                         {"posthog_session_id": ""}])
    def test_a_report_with_no_session_gets_no_link(self, context, monkeypatch):
        self._url(monkeypatch)
        assert _mod().replay_url_from_context(context) is None

    def test_the_filed_body_links_the_replay(self, monkeypatch):
        url = self._url(monkeypatch)
        body = _mod().build_issue_body(_classification(), feedback_id=3, replay_url=url)
        assert f"[Watch the session replay]({url})" in body
        # Still a MODE=start body — the link sits above Scope, not inside it.
        assert body.index(url) < body.index("## Scope")

    def test_a_body_without_a_replay_says_nothing_about_one(self):
        assert "session replay" not in _mod().build_issue_body(_classification(), feedback_id=3)

    def test_each_repeat_report_links_its_own_session(self, monkeypatch):
        url = self._url(monkeypatch)
        comment = _mod().build_duplicate_comment(_classification(), feedback_id=9, replay_url=url)
        assert url in comment
        assert _mod().PLAN_DOC in comment

    def test_filing_passes_the_report_session_through_to_github(self, monkeypatch):
        url = self._url(monkeypatch)
        svc = _mod()
        with patch(f"{_SVC}.classify_feedback", return_value=_classification()), \
                patch(f"{_SVC}.create_github_issue", return_value=55) as create, \
                patch(f"{_SVC}.embed_text", return_value=None), \
                patch(f"{_SVC}.count_feedback_filed_by_user", return_value=0), \
                patch(f"{_SVC}.update_feedback_triage"):
            svc.file_feedback_issue(
                {"id": 11, "user_id": 1, "body": "the approve button does nothing",
                 "context_json": json.dumps({"posthog_session_id": self._SESSION})}, clusters=[])
        assert url in create.call_args[0][1]

    def test_a_deduped_report_links_its_replay_on_the_existing_issue(self, monkeypatch):
        url = self._url(monkeypatch)
        svc = _mod()
        with patch(f"{_SVC}.classify_feedback",
                   return_value=_classification(duplicate_of=7)), \
                patch(f"{_SVC}.comment_on_issue", return_value=True) as comment, \
                patch(f"{_SVC}.embed_text", return_value=None), \
                patch(f"{_SVC}.count_feedback_filed_by_user", return_value=0), \
                patch(f"{_SVC}.update_feedback_triage"):
            result = svc.file_feedback_issue(
                {"id": 12, "user_id": 2, "body": "comments never post to the feed",
                 "context_json": json.dumps({"posthog_session_id": self._SESSION})},
                clusters=[_cluster()])
        assert result["action"] == "deduped"
        assert url in comment.call_args[0][1]


class TestDecisionComment:
    @pytest.mark.parametrize("risk", ['product-decision', 'live-linkedin', 'migration', 'security'])
    def test_risk_comment_is_letter_pickable_with_a_recommendation(self, risk):
        from cqc_lem.utilities.feedback.classifier import FeedbackRisk
        svc = _mod()
        comment = svc.build_decision_comment(_classification(risk=FeedbackRisk(risk)))
        assert "Human decision needed" in comment
        assert f"risk:{risk}" in comment
        assert "- **A." in comment and "- **B." in comment and "- **C." in comment
        assert "✅ *recommended*" in comment
        assert "**My recommendation: `1A`.**" in comment
        assert "### 1." in comment
        # Exactly ONE question — the RUNBOOK forbids inventing extra decisions.
        assert comment.count("### ") == 1

    def test_unproven_demand_asks_the_demand_question_not_a_risk_one(self):
        from cqc_lem.utilities.feedback.classifier import FeedbackCategory
        svc = _mod()
        comment = svc.build_decision_comment(
            _classification(category=FeedbackCategory.FEATURE, confidence=0.95),
            reporter_count=1)
        assert "unproven-demand" in comment
        assert "should the pipeline build it now?" in comment

    def test_low_confidence_asks_the_scope_question(self):
        svc = _mod()
        comment = svc.build_decision_comment(_classification(confidence=0.62))
        assert "low-confidence" in comment
        assert "is the scope right?" in comment


class TestDuplicateComment:
    def test_plus_one_carries_the_new_detail_and_the_demand_count(self):
        svc = _mod()
        comment = svc.build_duplicate_comment(_classification(), feedback_id=9,
                                             reporter_count=3, item_count=5)
        assert comment.startswith("**+1")
        assert "Only one comment is posted per automation run." in comment
        assert "3 distinct reporter(s), 5 report(s)" in comment
        assert "#9" in comment
        assert svc.PLAN_DOC in comment


class TestGithubIO:
    def _response(self, status=201, payload=None):
        response = MagicMock(status_code=status, text="")
        response.json.return_value = payload if payload is not None else {}
        return response

    def test_create_issue_attaches_labels_and_assignees_after_creation(self, monkeypatch):
        # Issue #598: a fine-grained PAT silently drops labels/assignees sent in the create
        # payload, so they MUST ride on their own endpoints afterwards.
        svc = _mod()
        monkeypatch.setenv("FEEDBACK_GITHUB_TOKEN", "tok")
        monkeypatch.setenv("FEEDBACK_GITHUB_REPO", "o/r")
        requests = MagicMock()
        requests.request.side_effect = [self._response(201, {"number": 777}),
                                        self._response(200, [{"name": "bug"}]),
                                        self._response(201, {"number": 777})]
        with patch.dict("sys.modules", {"requests": requests}):
            got = svc.create_github_issue("t", "b", ["bug", "agent:ready"],
                                          assignees=["gitchrisqueen"])
        assert got == 777
        calls = requests.request.call_args_list
        assert [c[0] for c in calls] == [
            ("POST", "https://api.github.com/repos/o/r/issues"),
            ("POST", "https://api.github.com/repos/o/r/issues/777/labels"),
            ("POST", "https://api.github.com/repos/o/r/issues/777/assignees")]
        assert calls[0].kwargs["json"] == {"title": "t", "body": "b"}
        assert calls[1].kwargs["json"] == {"labels": ["bug", "agent:ready"]}
        assert calls[2].kwargs["json"] == {"assignees": ["gitchrisqueen"]}
        assert calls[0].kwargs["headers"]["Authorization"] == "Bearer tok"

    def test_create_issue_skips_the_attach_calls_when_there_is_nothing_to_attach(self, monkeypatch):
        svc = _mod()
        monkeypatch.setenv("FEEDBACK_GITHUB_TOKEN", "tok")
        monkeypatch.setenv("FEEDBACK_GITHUB_REPO", "o/r")
        requests = MagicMock()
        requests.request.return_value = self._response(201, {"number": 12})
        with patch.dict("sys.modules", {"requests": requests}):
            assert svc.create_github_issue("t", "b", [], assignees=None) == 12
        assert requests.request.call_count == 1

    def test_failed_label_attach_still_returns_the_issue(self, monkeypatch):
        # The issue is already on GitHub — losing its number would strand the feedback row.
        svc = _mod()
        monkeypatch.setenv("FEEDBACK_GITHUB_TOKEN", "tok")
        monkeypatch.setenv("FEEDBACK_GITHUB_REPO", "o/r")
        requests = MagicMock()
        requests.request.side_effect = [self._response(201, {"number": 31}),
                                        self._response(403, {"message": "forbidden"}),
                                        self._response(403, {"message": "forbidden"})]
        with patch.dict("sys.modules", {"requests": requests}):
            assert svc.create_github_issue("t", "b", ["bug"], assignees=["gitchrisqueen"]) == 31
        assert requests.request.call_count == 3

    def test_a_failed_attach_is_an_error_not_a_soft_warning(self, monkeypatch):
        # Issue #718: a label-less issue is invisible to the agent pipeline, so the loop is BROKEN —
        # and only an ERROR reaches PostHog at the default POSTHOG_LOG_LEVEL.
        svc = _mod()
        monkeypatch.setenv("FEEDBACK_GITHUB_TOKEN", "tok")
        requests = MagicMock()
        requests.request.side_effect = [self._response(201, {"number": 31}),
                                        self._response(403, {"message": "forbidden"}),
                                        self._response(403, {"message": "forbidden"})]
        with patch.dict("sys.modules", {"requests": requests}), \
                patch(f"{_SVC}.log_error") as error, patch(f"{_SVC}.log_warning") as warning:
            svc.create_github_issue("t", "b", ["bug"], assignees=["gitchrisqueen"])
        messages = [call[0][0] for call in error.call_args_list]
        assert any("could not be labeled" in m and "Issues: Read and write" in m for m in messages)
        assert any("could not be assigned" in m for m in messages)
        warning.assert_not_called()

    def test_a_held_issue_with_no_decision_comment_is_an_error(self):
        # The owner is asked to answer a Decision Comment that never posted — a silent dead end.
        svc = _mod()
        classification = _classification(confidence=0.65)
        with patch(f"{_SVC}.classify_feedback", return_value=classification), \
                patch(f"{_SVC}.create_github_issue", return_value=91), \
                patch(f"{_SVC}.comment_on_issue", return_value=False), \
                patch(f"{_SVC}.embed_text", return_value=None), \
                patch(f"{_SVC}.update_feedback_triage"), \
                patch(f"{_SVC}.count_feedback_filed_by_user", return_value=0), \
                patch(f"{_SVC}.log_error") as error:
            svc.file_feedback_issue({"id": 1, "user_id": 2, "body": "x"}, clusters=[])
        assert "no Decision Comment" in error.call_args[0][0]

    def test_no_token_files_nothing(self, monkeypatch):
        svc = _mod()
        monkeypatch.delenv("FEEDBACK_GITHUB_TOKEN", raising=False)
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        requests = MagicMock()
        with patch.dict("sys.modules", {"requests": requests}):
            assert svc.create_github_issue("t", "b", []) is None
        requests.request.assert_not_called()

    def test_github_token_is_the_fallback_credential(self, monkeypatch):
        svc = _mod()
        monkeypatch.delenv("FEEDBACK_GITHUB_TOKEN", raising=False)
        monkeypatch.setenv("GITHUB_TOKEN", "fallback")
        assert svc.github_token() == "fallback"

    def test_error_status_returns_none(self, monkeypatch):
        svc = _mod()
        monkeypatch.setenv("FEEDBACK_GITHUB_TOKEN", "tok")
        requests = MagicMock()
        requests.request.return_value = self._response(422, {"message": "nope"})
        with patch.dict("sys.modules", {"requests": requests}):
            assert svc.create_github_issue("t", "b", []) is None

    def test_network_failure_returns_none(self, monkeypatch):
        svc = _mod()
        monkeypatch.setenv("FEEDBACK_GITHUB_TOKEN", "tok")
        requests = MagicMock()
        requests.request.side_effect = OSError("connection reset")
        with patch.dict("sys.modules", {"requests": requests}), patch(f"{_SVC}.time.sleep"):
            assert svc.comment_on_issue(5, "body") is False

    def test_comment_targets_the_issue_number(self, monkeypatch):
        svc = _mod()
        monkeypatch.setenv("FEEDBACK_GITHUB_TOKEN", "tok")
        monkeypatch.setenv("FEEDBACK_GITHUB_REPO", "o/r")
        requests = MagicMock()
        requests.request.return_value = self._response(201, {"id": 1})
        with patch.dict("sys.modules", {"requests": requests}):
            assert svc.comment_on_issue(42, "+1") is True
        assert requests.request.call_args[0][1] == \
            "https://api.github.com/repos/o/r/issues/42/comments"

    def test_non_json_success_response_still_counts_as_accepted(self, monkeypatch):
        svc = _mod()
        monkeypatch.setenv("FEEDBACK_GITHUB_TOKEN", "tok")
        response = MagicMock(status_code=204, text="")
        response.json.side_effect = ValueError("no body")
        requests = MagicMock()
        requests.request.return_value = response
        with patch.dict("sys.modules", {"requests": requests}):
            assert svc.comment_on_issue(42, "+1") is True

    def test_comment_without_an_issue_number_is_a_no_op(self):
        assert _mod().comment_on_issue(None, "+1") is False


class TestTransportFailures:
    """Issue #767: a DNS/connect blip is the network, not a defect in this loop. It is retried in
    place and reported ONCE as a WARNING (recurrence escalation decides whether it is a defect) —
    an ERROR per lost request turned a 100ms outage into an auto-filed GitHub issue."""

    def _response(self, status=200, payload=None):
        response = MagicMock(status_code=status, text="")
        response.json.return_value = payload if payload is not None else {}
        return response

    def test_a_dns_failure_is_retried_then_warned_not_errored(self, monkeypatch):
        svc = _mod()
        monkeypatch.setenv("FEEDBACK_GITHUB_TOKEN", "tok")
        requests = MagicMock()
        requests.request.side_effect = ConnectionError(
            "Failed to resolve 'api.github.com' ([Errno -3] Temporary failure in name resolution)")
        with patch.dict("sys.modules", {"requests": requests}), \
                patch(f"{_SVC}.time.sleep") as sleep, \
                patch(f"{_SVC}.log_warning") as warning, patch(f"{_SVC}.log_error") as error:
            assert svc.github_request("GET", "issues/727") is None
        assert requests.request.call_count == svc.GITHUB_RETRY_ATTEMPTS
        assert sleep.call_count == svc.GITHUB_RETRY_ATTEMPTS - 1
        error.assert_not_called()
        assert "unreachable" in warning.call_args[0][0]
        assert warning.call_args.kwargs["exc"] is not None

    def test_a_transport_failure_that_recovers_is_never_reported(self, monkeypatch):
        svc = _mod()
        monkeypatch.setenv("FEEDBACK_GITHUB_TOKEN", "tok")
        requests = MagicMock()
        requests.request.side_effect = [TimeoutError("read timed out"),
                                        self._response(200, {"number": 5})]
        with patch.dict("sys.modules", {"requests": requests}), \
                patch(f"{_SVC}.time.sleep"), \
                patch(f"{_SVC}.log_warning") as warning, patch(f"{_SVC}.log_error") as error:
            assert svc.github_request("GET", "issues/5") == {"number": 5}
        assert requests.request.call_count == 2
        warning.assert_not_called()
        error.assert_not_called()
        assert svc.github_unreachable() is False

    def test_a_failure_that_is_not_the_transport_is_still_an_error(self, monkeypatch):
        # A bug in this module must not be retried three times and filed away as "the network".
        svc = _mod()
        monkeypatch.setenv("FEEDBACK_GITHUB_TOKEN", "tok")
        requests = MagicMock()
        requests.request.side_effect = ValueError("bad payload")
        with patch.dict("sys.modules", {"requests": requests}), \
                patch(f"{_SVC}.time.sleep") as sleep, patch(f"{_SVC}.log_error") as error:
            assert svc.github_request("POST", "issues") is None
        assert requests.request.call_count == 1
        sleep.assert_not_called()
        assert "failed" in error.call_args[0][0]

    def test_one_outage_is_reported_once_and_short_circuits_the_rest_of_the_pass(self,
                                                                                monkeypatch):
        # Three lost requests in one pass = three warnings = the escalation threshold, i.e. the
        # outage would file itself as a defect. The cool-off is what keeps it to one.
        svc = _mod()
        monkeypatch.setenv("FEEDBACK_GITHUB_TOKEN", "tok")
        requests = MagicMock()
        requests.request.side_effect = ConnectionError("no route to host")
        with patch.dict("sys.modules", {"requests": requests}), \
                patch(f"{_SVC}.time.sleep"), patch(f"{_SVC}.log_warning") as warning, \
                patch(f"{_SVC}.log_error") as error:
            assert svc.github_request("GET", "issues/101") is None
            assert svc.github_unreachable() is True
            assert svc.github_request("GET", "issues/102") is None
            assert svc.github_request("POST", "issues/103/comments", {"body": "x"}) is None
        assert requests.request.call_count == svc.GITHUB_RETRY_ATTEMPTS
        assert warning.call_count == 1
        error.assert_not_called()

    def test_a_successful_call_clears_the_cool_off(self, monkeypatch):
        svc = _mod()
        monkeypatch.setenv("FEEDBACK_GITHUB_TOKEN", "tok")
        svc._unreachable_until = 0.0
        requests = MagicMock()
        requests.request.return_value = self._response(200, {"ok": True})
        with patch.dict("sys.modules", {"requests": requests}):
            assert svc.github_request("GET", "issues/1") == {"ok": True}
        assert svc.github_unreachable() is False


class TestFileFeedbackIssue:
    """The orchestrator, with the classifier + GitHub + DB writes all mocked."""

    def _patches(self, classification=None, issue_number=555, commented=True):
        classification = classification or _classification()
        return {
            "classify": patch(f"{_SVC}.classify_feedback", return_value=classification),
            "create": patch(f"{_SVC}.create_github_issue", return_value=issue_number),
            "comment": patch(f"{_SVC}.comment_on_issue", return_value=commented),
            "embed": patch(f"{_SVC}.embed_text", return_value=[1.0, 0.0]),
            "triage": patch(f"{_SVC}.update_feedback_triage", return_value=True),
            "rate": patch(f"{_SVC}.count_feedback_filed_by_user", return_value=0),
        }

    def _run(self, feedback, clusters=None, **overrides):
        svc = _mod()
        patches = self._patches(**overrides)
        with patches["classify"] as classify, patches["create"] as create, \
                patches["comment"] as comment, patches["embed"], \
                patches["triage"] as triage, patches["rate"] as rate:
            result = svc.file_feedback_issue(feedback, clusters=clusters if clusters is not None
                                             else [])
        return result, {"classify": classify, "create": create, "comment": comment,
                        "triage": triage, "rate": rate}

    def test_new_problem_files_one_issue_and_stamps_it_back(self):
        from cqc_lem.utilities.db import FeedbackStatus
        result, mocks = self._run({"id": 12, "user_id": 3, "body": "comments never post"})
        assert result["action"] == "filed"
        assert result["issue_number"] == 555
        assert result["agent_ready"] is True
        assert 'agent:ready' in result["labels"]
        mocks["triage"].assert_called_once_with(
            12, status=FeedbackStatus.ISSUE_CREATED, cluster_id=12,
            github_issue_number=555, embedding=[1.0, 0.0])
        # agent:ready issues are NOT assigned to a human and get no Decision Comment.
        assert mocks["create"].call_args.kwargs["assignees"] is None
        mocks["comment"].assert_not_called()

    def test_filed_result_returns_a_cluster_for_in_batch_dedup(self):
        result, _ = self._run({"id": 12, "user_id": 3, "body": "comments never post"})
        assert result["cluster"] == {"cluster_id": 12, "body": "comments never post",
                                    "embedding": [1.0, 0.0], "github_issue_number": 555,
                                    "item_count": 1, "reporter_count": 1}

    def test_risky_item_is_assigned_and_gets_a_decision_comment(self):
        from cqc_lem.utilities.feedback.classifier import FeedbackRisk
        result, mocks = self._run({"id": 12, "user_id": 3, "body": "delete my data please"},
                                  classification=_classification(risk=FeedbackRisk.SECURITY))
        assert result["agent_ready"] is False
        assert mocks["create"].call_args.kwargs["assignees"] == ["gitchrisqueen"]
        body = mocks["comment"].call_args[0][1]
        assert "Human decision needed" in body
        assert "risk:security" in body

    def test_duplicate_comments_on_the_existing_issue_instead_of_filing(self):
        from cqc_lem.utilities.db import FeedbackStatus
        clusters = [_cluster(7, "comments never post", 101, embedding=[1.0, 0.0], reporters=2,
                             items=3)]
        result, mocks = self._run({"id": 15, "user_id": 4, "body": "replies do not post"},
                                  clusters=clusters)
        assert result["action"] == "deduped"
        assert result["issue_number"] == 101
        assert result["cluster_id"] == 7
        mocks["create"].assert_not_called()
        assert mocks["comment"].call_args[0][0] == 101
        assert "+1" in mocks["comment"].call_args[0][1]
        mocks["triage"].assert_called_once_with(
            15, status=FeedbackStatus.CLUSTERED, cluster_id=7, github_issue_number=101,
            embedding=[1.0, 0.0])

    def test_classifier_named_duplicate_wins_even_below_the_cosine_threshold(self):
        clusters = [_cluster(7, "billing double charge", 101, embedding=[0.0, 1.0])]
        result, mocks = self._run({"id": 15, "user_id": 4, "body": "comments never post"},
                                  clusters=clusters,
                                  classification=_classification(duplicate_of=7))
        assert result["action"] == "deduped"
        assert result["cluster_id"] == 7
        mocks["create"].assert_not_called()

    def test_anonymous_report_counts_zero_reporters_not_one(self):
        # reporter_count is DISTINCT identified users — an anonymous report is a report with no
        # attributable reporter, so it must not be floored up to 1 (that fakes the demand signal).
        result, mocks = self._run({"id": 12, "user_id": None, "body": "comments never post"})
        assert result["action"] == "filed"
        assert result["cluster"]["reporter_count"] == 0
        assert "0 distinct user(s) across 1 feedback item(s)" in mocks["create"].call_args[0][1]

    def test_dedup_onto_an_anonymous_cluster_does_not_inflate_the_demand_count(self):
        clusters = [_cluster(7, "comments never post", 101, embedding=[1.0, 0.0], reporters=0,
                             items=1)]
        _, mocks = self._run({"id": 15, "user_id": 4, "body": "replies do not post"},
                             clusters=clusters)
        assert "1 distinct reporter(s), 2 report(s)" in mocks["comment"].call_args[0][1]

    def test_two_anonymous_reports_still_add_up_to_zero_reporters(self):
        clusters = [_cluster(7, "comments never post", 101, embedding=[1.0, 0.0], reporters=0,
                             items=1)]
        _, mocks = self._run({"id": 15, "user_id": None, "body": "replies do not post"},
                             clusters=clusters)
        assert "0 distinct reporter(s), 2 report(s)" in mocks["comment"].call_args[0][1]

    def test_anonymous_feature_request_does_not_satisfy_the_demand_gate(self, monkeypatch):
        from cqc_lem.utilities.feedback.classifier import FeedbackCategory
        monkeypatch.setenv("FEEDBACK_FEATURE_DEMAND_MIN", "1")
        feature = _classification(category=FeedbackCategory.FEATURE, confidence=0.95)
        anonymous, _ = self._run({"id": 12, "user_id": None, "body": "add a dark mode"},
                                 classification=feature)
        assert anonymous["agent_ready"] is False
        assert 'needs-human' in anonymous["labels"]
        # …while one identified reporter does clear a min of 1.
        identified, _ = self._run({"id": 13, "user_id": 3, "body": "add a dark mode"},
                                  classification=feature)
        assert identified["agent_ready"] is True

    def test_failed_duplicate_comment_leaves_the_row_for_a_retry(self):
        clusters = [_cluster(7, "comments never post", 101, embedding=[1.0, 0.0])]
        result, mocks = self._run({"id": 15, "user_id": 4, "body": "replies do not post"},
                                  clusters=clusters, commented=False)
        assert result["action"] == "error"
        mocks["triage"].assert_not_called()

    def test_failed_issue_creation_leaves_the_row_for_a_retry(self):
        result, mocks = self._run({"id": 12, "user_id": 3, "body": "comments never post"},
                                  issue_number=None)
        assert result["action"] == "error"
        assert result["reason"] == "issue creation failed"
        mocks["triage"].assert_not_called()

    def test_noise_is_dismissed_without_touching_github(self):
        from cqc_lem.utilities.db import FeedbackStatus
        from cqc_lem.utilities.feedback.classifier import FeedbackCategory
        result, mocks = self._run({"id": 1, "user_id": 3, "body": "you guys rock"},
                                  classification=_classification(
                                      category=FeedbackCategory.NOISE, confidence=0.99))
        assert result["action"] == "dropped"
        mocks["create"].assert_not_called()
        mocks["comment"].assert_not_called()
        mocks["triage"].assert_called_once_with(1, status=FeedbackStatus.DISMISSED)

    def test_question_is_routed_to_the_faq_queue_without_an_issue(self):
        from cqc_lem.utilities.db import FeedbackStatus
        from cqc_lem.utilities.feedback.classifier import FeedbackCategory
        result, mocks = self._run({"id": 2, "user_id": 3, "body": "how do I schedule a post?"},
                                  classification=_classification(
                                      category=FeedbackCategory.QUESTION, confidence=0.99))
        assert result["action"] == "faq"
        mocks["create"].assert_not_called()
        mocks["triage"].assert_called_once_with(2, status=FeedbackStatus.TRIAGED)

    def test_low_confidence_goes_to_human_triage_not_the_backlog(self):
        from cqc_lem.utilities.db import FeedbackStatus
        result, mocks = self._run({"id": 3, "user_id": 3, "body": "idk it's weird"},
                                  classification=_classification(confidence=0.2))
        assert result["action"] == "needs_human"
        mocks["create"].assert_not_called()
        mocks["triage"].assert_called_once_with(3, status=FeedbackStatus.TRIAGED)

    def test_empty_body_is_dropped_without_classifying(self):
        result, mocks = self._run({"id": 4, "user_id": 3, "body": "   "})
        assert result["action"] == "dropped"
        mocks["classify"].assert_not_called()

    def test_over_the_per_user_cap_is_held_before_any_llm_call(self):
        svc = _mod()
        with patch(f"{_SVC}.count_feedback_filed_by_user", return_value=3), \
                patch(f"{_SVC}.classify_feedback") as classify, \
                patch(f"{_SVC}.create_github_issue") as create, \
                patch(f"{_SVC}.update_feedback_triage") as triage:
            result = svc.file_feedback_issue({"id": 5, "user_id": 9, "body": "another one"},
                                             clusters=[])
        assert result["action"] == "rate_limited"
        assert result["recent"] == 3
        classify.assert_not_called()
        create.assert_not_called()
        # Status untouched, so a later pass (fresh window) still gets to file it.
        triage.assert_not_called()

    def test_anonymous_feedback_skips_the_per_user_cap(self):
        svc = _mod()
        with patch(f"{_SVC}.count_feedback_filed_by_user") as counter, \
                patch(f"{_SVC}.classify_feedback", return_value=_classification()), \
                patch(f"{_SVC}.create_github_issue", return_value=1), \
                patch(f"{_SVC}.embed_text", return_value=None), \
                patch(f"{_SVC}.update_feedback_triage"):
            result = svc.file_feedback_issue({"id": 6, "user_id": None, "body": "comments break"},
                                            clusters=[])
        assert result["action"] == "filed"
        counter.assert_not_called()

    def test_stored_embedding_is_reused_instead_of_re_embedding(self):
        svc = _mod()
        with patch(f"{_SVC}.classify_feedback", return_value=_classification()), \
                patch(f"{_SVC}.create_github_issue", return_value=1), \
                patch(f"{_SVC}.embed_text") as embed, \
                patch(f"{_SVC}.count_feedback_filed_by_user", return_value=0), \
                patch(f"{_SVC}.update_feedback_triage"):
            svc.file_feedback_issue({"id": 7, "user_id": 1, "body": "x y z",
                                     "embedding": "[0.5, 0.5]"}, clusters=[])
        embed.assert_not_called()

    def test_context_json_string_is_passed_to_the_classifier_as_a_dict(self):
        svc = _mod()
        with patch(f"{_SVC}.classify_feedback", return_value=_classification()) as classify, \
                patch(f"{_SVC}.create_github_issue", return_value=1), \
                patch(f"{_SVC}.embed_text", return_value=None), \
                patch(f"{_SVC}.count_feedback_filed_by_user", return_value=0), \
                patch(f"{_SVC}.update_feedback_triage"):
            svc.file_feedback_issue({"id": 8, "user_id": 1, "body": "broken",
                                     "type_hint": "bug",
                                     "context_json": json.dumps({"route": "/content"})},
                                    clusters=[])
        assert classify.call_args.kwargs["context"] == {"route": "/content"}
        assert classify.call_args.kwargs["type_hint"] == "bug"

    def test_malformed_context_json_does_not_break_filing(self):
        svc = _mod()
        with patch(f"{_SVC}.classify_feedback", return_value=_classification()) as classify, \
                patch(f"{_SVC}.create_github_issue", return_value=1), \
                patch(f"{_SVC}.embed_text", return_value=None), \
                patch(f"{_SVC}.count_feedback_filed_by_user", return_value=0), \
                patch(f"{_SVC}.update_feedback_triage"):
            result = svc.file_feedback_issue({"id": 9, "user_id": 1, "body": "broken",
                                             "context_json": "{not json"}, clusters=[])
        assert result["action"] == "filed"
        assert classify.call_args.kwargs["context"] is None

    def test_clusters_are_loaded_from_the_db_when_not_injected(self):
        svc = _mod()
        with patch(f"{_SVC}.get_open_feedback_clusters", return_value=[]) as loader, \
                patch(f"{_SVC}.classify_feedback", return_value=_classification()), \
                patch(f"{_SVC}.create_github_issue", return_value=1), \
                patch(f"{_SVC}.embed_text", return_value=None), \
                patch(f"{_SVC}.count_feedback_filed_by_user", return_value=0), \
                patch(f"{_SVC}.update_feedback_triage"):
            svc.file_feedback_issue({"id": 10, "user_id": 1, "body": "broken"})
        loader.assert_called_once()


class TestProcessNewFeedback:
    def test_identical_reports_in_one_batch_file_one_issue(self):
        svc = _mod()
        rows = [{"id": 1, "user_id": 1, "body": "feed commenting stops after one comment"},
                {"id": 2, "user_id": 2, "body": "feed commenting stops after one comment"}]
        with patch(f"{_SVC}.get_unprocessed_feedback", return_value=rows), \
                patch(f"{_SVC}.get_open_feedback_clusters", return_value=[]), \
                patch(f"{_SVC}.count_pending_admin_review", return_value=0), \
                patch(f"{_SVC}.classify_feedback", return_value=_classification()), \
                patch(f"{_SVC}.create_github_issue", return_value=900) as create, \
                patch(f"{_SVC}.comment_on_issue", return_value=True) as comment, \
                patch(f"{_SVC}.embed_text", return_value=None), \
                patch(f"{_SVC}.count_feedback_filed_by_user", return_value=0), \
                patch(f"{_SVC}.update_feedback_triage"):
            result = svc.process_new_feedback(limit=10)
        assert result == {"processed": 2, "counts": {"filed": 1, "deduped": 1}}
        create.assert_called_once()
        assert comment.call_args[0][0] == 900

    def test_a_row_that_explodes_does_not_stop_the_pass(self):
        svc = _mod()
        rows = [{"id": 1, "user_id": 1, "body": "a"}, {"id": 2, "user_id": 2, "body": "b"}]
        with patch(f"{_SVC}.get_unprocessed_feedback", return_value=rows), \
                patch(f"{_SVC}.get_open_feedback_clusters", return_value=[]), \
                patch(f"{_SVC}.count_pending_admin_review", return_value=0), \
                patch(f"{_SVC}.file_feedback_issue",
                      side_effect=[RuntimeError("boom"), {"action": "filed"}]):
            result = svc.process_new_feedback()
        assert result == {"processed": 2, "counts": {"error": 1, "filed": 1}}

    def test_only_admin_rows_are_asked_for_and_the_backlog_is_reported(self):
        """Issue #793: the admin filter must be pushed into the query. Non-admin rows keep their
        new/NULL-cluster shape forever, so a caller-side skip would refill `limit` with the same
        parked rows every pass and admin feedback would never be reached again."""
        svc = _mod()
        rows = [{"id": 1, "user_id": 1, "body": "admin feedback"}]
        with patch(f"{_SVC}.get_unprocessed_feedback", return_value=rows) as loader, \
                patch(f"{_SVC}.get_open_feedback_clusters", return_value=[]), \
                patch(f"{_SVC}.count_pending_admin_review", return_value=2), \
                patch(f"{_SVC}.file_feedback_issue", return_value={"action": "filed"}) as filer:
            result = svc.process_new_feedback(limit=10)
        assert loader.call_args.kwargs["admin_only"] is True
        assert result == {"processed": 1, "counts": {"filed": 1, "pending_review": 2}}
        filer.assert_called_once_with(rows[0], clusters=[])

    def test_empty_queue_is_a_no_op(self):
        svc = _mod()
        with patch(f"{_SVC}.get_unprocessed_feedback", return_value=[]), \
                patch(f"{_SVC}.get_open_feedback_clusters", return_value=[]), \
                patch(f"{_SVC}.count_pending_admin_review", return_value=0), \
                patch(f"{_SVC}.create_github_issue") as create:
            assert svc.process_new_feedback() == {"processed": 0, "counts": {}}
        create.assert_not_called()


class TestReclusterFeedback:
    def test_late_duplicate_is_attached_and_plus_ones_the_existing_issue(self):
        from cqc_lem.utilities.db import FeedbackStatus
        svc = _mod()
        clusters = [_cluster(7, "comments never post", 101, embedding=[1.0, 0.0])]
        rows = [{"id": 30, "user_id": 5, "body": "replies do not post", "embedding": [1.0, 0.0]}]
        with patch(f"{_SVC}.get_open_feedback_clusters", return_value=clusters), \
                patch(f"{_SVC}.get_unprocessed_feedback", return_value=rows), \
                patch(f"{_SVC}.comment_on_issue", return_value=True) as comment, \
                patch(f"{_SVC}.update_feedback_triage") as triage, \
                patch(f"{_SVC}.embed_text") as embed:
            result = svc.recluster_feedback()
        assert result == {"scanned": 1, "attached": 1, "embedded": 0, "clusters": 1}
        embed.assert_not_called()
        assert comment.call_args[0][0] == 101
        triage.assert_called_once_with(30, status=FeedbackStatus.CLUSTERED, cluster_id=7,
                                       github_issue_number=101)

    def test_missing_embeddings_are_backfilled(self):
        svc = _mod()
        rows = [{"id": 31, "user_id": 5, "body": "billing charged me twice"}]
        with patch(f"{_SVC}.get_open_feedback_clusters", return_value=[]), \
                patch(f"{_SVC}.get_unprocessed_feedback", return_value=rows), \
                patch(f"{_SVC}.embed_text", return_value=[0.0, 1.0]), \
                patch(f"{_SVC}.update_feedback_triage") as triage:
            result = svc.recluster_feedback()
        assert result["embedded"] == 1
        assert result["attached"] == 0
        triage.assert_called_once_with(31, embedding=[0.0, 1.0])

    def test_it_never_files_a_new_issue(self):
        svc = _mod()
        rows = [{"id": 32, "user_id": 5, "body": "something brand new"}]
        with patch(f"{_SVC}.get_open_feedback_clusters", return_value=[]), \
                patch(f"{_SVC}.get_unprocessed_feedback", return_value=rows), \
                patch(f"{_SVC}.embed_text", return_value=None), \
                patch(f"{_SVC}.create_github_issue") as create, \
                patch(f"{_SVC}.update_feedback_triage"):
            svc.recluster_feedback()
        create.assert_not_called()

    def test_it_reconsiders_rows_already_parked_in_triage(self):
        from cqc_lem.utilities.db import FeedbackStatus
        svc = _mod()
        with patch(f"{_SVC}.get_open_feedback_clusters", return_value=[]), \
                patch(f"{_SVC}.get_unprocessed_feedback", return_value=[]) as loader:
            svc.recluster_feedback(limit=50)
        assert loader.call_args[0][0] == 50
        assert loader.call_args.kwargs["statuses"] == (FeedbackStatus.NEW, FeedbackStatus.TRIAGED)

    def test_a_failed_comment_does_not_mark_the_row_clustered(self):
        svc = _mod()
        clusters = [_cluster(7, "comments never post", 101, embedding=[1.0, 0.0])]
        rows = [{"id": 33, "user_id": 5, "body": "replies do not post", "embedding": [1.0, 0.0]}]
        with patch(f"{_SVC}.get_open_feedback_clusters", return_value=clusters), \
                patch(f"{_SVC}.get_unprocessed_feedback", return_value=rows), \
                patch(f"{_SVC}.comment_on_issue", return_value=False), \
                patch(f"{_SVC}.update_feedback_triage") as triage:
            result = svc.recluster_feedback()
        assert result["attached"] == 0
        triage.assert_not_called()

    def test_blank_bodies_are_skipped(self):
        svc = _mod()
        with patch(f"{_SVC}.get_open_feedback_clusters", return_value=[]), \
                patch(f"{_SVC}.get_unprocessed_feedback",
                      return_value=[{"id": 34, "body": "  "}]), \
                patch(f"{_SVC}.embed_text") as embed:
            result = svc.recluster_feedback()
        assert result == {"scanned": 1, "attached": 0, "embedded": 0, "clusters": 0}
        embed.assert_not_called()

    def test_non_admin_feedback_is_not_silently_reclustered(self):
        """Issue #793: reclustering accepts a report into an existing issue, so it must be gated
        the same way filing is — and gated in SQL, not by skipping rows the query returned."""
        svc = _mod()
        with patch(f"{_SVC}.get_open_feedback_clusters", return_value=[]), \
                patch(f"{_SVC}.get_unprocessed_feedback", return_value=[]) as loader:
            svc.recluster_feedback()
        assert loader.call_args.kwargs["admin_only"] is True


class TestEnvReaders:
    def test_defaults(self, monkeypatch):
        svc = _mod()
        for key in ("FEEDBACK_GITHUB_REPO", "FEEDBACK_ISSUE_ASSIGNEE",
                    "FEEDBACK_DUPLICATE_SIMILARITY", "FEEDBACK_FEATURE_DEMAND_MIN",
                    "FEEDBACK_MAX_ISSUES_PER_USER_PER_DAY", "FEEDBACK_EMBEDDING_MODEL"):
            monkeypatch.delenv(key, raising=False)
        assert svc.github_repo() == svc.DEFAULT_REPO
        assert svc.issue_assignee() == "gitchrisqueen"
        assert svc.duplicate_similarity_min() == svc.DUPLICATE_SIMILARITY_DEFAULT
        assert svc.feature_demand_min() == svc.FEATURE_DEMAND_MIN_DEFAULT
        assert svc.max_issues_per_user_per_day() == svc.MAX_ISSUES_PER_USER_PER_DAY_DEFAULT
        assert svc.embedding_model() == svc.DEFAULT_EMBEDDING_MODEL

    @pytest.mark.parametrize("raw,expected", [("junk", 0.82), ("", 0.82), ("2", 1.0), ("-1", 0.0),
                                              ("0.5", 0.5)])
    def test_similarity_env_is_clamped_and_junk_tolerant(self, monkeypatch, raw, expected):
        svc = _mod()
        monkeypatch.setenv("FEEDBACK_DUPLICATE_SIMILARITY", raw)
        assert svc.duplicate_similarity_min() == pytest.approx(expected)

    @pytest.mark.parametrize("raw,expected", [("junk", 3), ("0", 1), ("500", 100), ("7", 7)])
    def test_cap_env_is_clamped_and_junk_tolerant(self, monkeypatch, raw, expected):
        svc = _mod()
        monkeypatch.setenv("FEEDBACK_MAX_ISSUES_PER_USER_PER_DAY", raw)
        assert svc.max_issues_per_user_per_day() == expected

    def test_repo_and_assignee_are_configurable(self, monkeypatch):
        svc = _mod()
        monkeypatch.setenv("FEEDBACK_GITHUB_REPO", "me/mine")
        monkeypatch.setenv("FEEDBACK_ISSUE_ASSIGNEE", "someone")
        assert svc.github_repo() == "me/mine"
        assert svc.issue_assignee() == "someone"


class TestParseFiledIssue:
    """Reading a filed issue back well enough to recompute its labels — issue #718."""

    def _round_trip(self, classification, reporter_count=1, feedback_id=42):
        svc = _mod()
        title = svc.build_issue_title(classification)
        body = svc.build_issue_body(classification, feedback_id, reporter_count, item_count=2)
        return svc.parse_filed_issue(title, body)

    def test_round_trips_everything_the_label_set_depends_on(self):
        classification = _classification()
        parsed, reporters = self._round_trip(classification)
        assert parsed.category == classification.category
        assert parsed.severity == classification.severity
        assert parsed.risk == classification.risk
        assert parsed.confidence == pytest.approx(classification.confidence)
        assert parsed.component == classification.component
        assert reporters == 1

    @pytest.mark.parametrize("risk", ['product-decision', 'live-linkedin', 'migration', 'security'])
    def test_recomputed_labels_match_what_was_filed_for_every_risk(self, risk):
        from cqc_lem.utilities.feedback.classifier import FeedbackRisk
        svc = _mod()
        classification = _classification(risk=FeedbackRisk(risk))
        parsed, reporters = self._round_trip(classification, reporter_count=1)
        assert svc.labels_for_issue(parsed, reporters) == svc.labels_for_issue(classification, 1)

    def test_recomputed_labels_match_for_a_feature_held_on_demand(self):
        from cqc_lem.utilities.feedback.classifier import FeedbackCategory
        svc = _mod()
        classification = _classification(category=FeedbackCategory.FEATURE)
        parsed, reporters = self._round_trip(classification, reporter_count=1)
        assert reporters == 1
        labels = svc.labels_for_issue(parsed, reporters)
        assert 'needs-human' in labels and 'agent:ready' not in labels
        # ...and the same feature with proven demand comes back agent:ready.
        parsed, reporters = self._round_trip(classification, reporter_count=3)
        assert reporters == 3
        assert 'agent:ready' in svc.labels_for_issue(parsed, reporters)

    def test_anonymous_report_stays_at_zero_reporters(self):
        parsed, reporters = self._round_trip(_classification(), reporter_count=0)
        assert reporters == 0
        assert parsed is not None

    @pytest.mark.parametrize("body", ["", "not our issue at all",
                                      "Classifier: nonsense/high, risk `none`, confidence 0.90.",
                                      "Classifier: bug/high, risk `unknown`, confidence 0.90."])
    def test_unreadable_provenance_is_none_not_a_guess(self, body):
        assert _mod().parse_filed_issue("fix(api): x", body) is None

    def test_missing_demand_line_reads_as_no_proven_reporters(self):
        parsed, reporters = _mod().parse_filed_issue(
            "fix(api): x", "Classifier: bug/high, risk `none`, confidence 0.90.")
        assert reporters == 0
        assert parsed.category == 'bug'

    def test_a_summary_that_quotes_a_demand_line_cannot_inflate_the_repair(self):
        # The `## Why` summary is model-written from the reporter's own words and sits ABOVE both
        # parsed lines. Reading the first match anywhere would let a report claim its own demand —
        # and demand is exactly what decides whether a FEATURE is repaired into `agent:ready`.
        from cqc_lem.utilities.feedback.classifier import FeedbackCategory
        svc = _mod()
        classification = _classification(
            category=FeedbackCategory.FEATURE,
            summary="Reported by 9 distinct user(s) on my team, they all want CSV export.")
        parsed, reporters = self._round_trip(classification, reporter_count=1)
        assert reporters == 1
        assert 'agent:ready' not in svc.labels_for_issue(parsed, reporters)

    def test_a_summary_that_quotes_a_classifier_line_cannot_rewrite_the_verdict(self):
        from cqc_lem.utilities.feedback.classifier import FeedbackSeverity
        classification = _classification(
            severity=FeedbackSeverity.LOW,
            summary="The banner says: Classifier: bug/critical, risk `none`, confidence 0.99.")
        parsed, _ = self._round_trip(classification)
        assert parsed.severity == 'low'
        assert parsed.confidence == pytest.approx(0.9)

    def test_unparseable_title_still_yields_a_classification(self):
        svc = _mod()
        body = svc.build_issue_body(_classification(), 1, 1, 1)
        parsed, _ = svc.parse_filed_issue("a bare human title", body)
        assert parsed.component == 'other'
        assert parsed.title == "a bare human title"


class TestRepairFiledIssue:
    """Re-attaching what a 403'd post-create pass dropped — issue #718."""

    def _issue(self, classification=None, number=713, labels=(), assignees=(), state='open',
               reporter_count=1):
        svc = _mod()
        classification = classification or _classification()
        return {"number": number, "state": state,
                "title": svc.build_issue_title(classification),
                "body": svc.build_issue_body(classification, 4, reporter_count, item_count=1),
                "labels": [{"name": name} for name in labels],
                "assignees": [{"login": login} for login in assignees]}

    def test_label_less_issue_gets_its_full_label_set_back(self):
        svc = _mod()
        with patch(f"{_SVC}.github_request", return_value={}) as request:
            assert svc.repair_filed_issue(self._issue()) == svc.RepairAction.REPAIRED
        request.assert_called_once_with(
            "POST", "issues/713/labels",
            {"labels": ['bug', 'priority:high', 'feedback-loop', 'agent:ready']})

    def test_an_issue_that_already_carries_our_label_is_left_alone(self):
        svc = _mod()
        with patch(f"{_SVC}.github_request") as request:
            assert svc.repair_filed_issue(
                self._issue(labels=['bug', 'feedback-loop'])) == svc.RepairAction.OK
        request.assert_not_called()

    def test_labels_a_human_added_are_kept_and_only_the_rest_attached(self):
        svc = _mod()
        with patch(f"{_SVC}.github_request", return_value={}) as request:
            svc.repair_filed_issue(self._issue(labels=['bug', 'agent:ready']))
        assert request.call_args[0][2] == {"labels": ['priority:high', 'feedback-loop']}

    @pytest.mark.parametrize("issue", [
        None, {}, {"number": 5, "state": "open", "body": "someone else's issue"},
        {"number": 5, "state": "open", "body": "Auto-filed from in-app feedback #1 ..."},
    ])
    def test_issues_that_are_not_ours_or_unreadable_are_never_touched(self, issue):
        svc = _mod()
        with patch(f"{_SVC}.github_request") as request:
            assert svc.repair_filed_issue(issue) == svc.RepairAction.SKIPPED
        request.assert_not_called()

    def test_a_closed_issue_is_never_repaired(self):
        svc = _mod()
        with patch(f"{_SVC}.github_request") as request:
            assert svc.repair_filed_issue(self._issue(state='closed')) == svc.RepairAction.SKIPPED
        request.assert_not_called()

    def test_a_held_issue_also_gets_its_assignee_and_decision_comment_back(self):
        from cqc_lem.utilities.feedback.classifier import FeedbackRisk
        svc = _mod()
        issue = self._issue(classification=_classification(risk=FeedbackRisk.MIGRATION))
        with patch(f"{_SVC}.github_request", side_effect=[{}, {}, []]) as request, \
                patch(f"{_SVC}.comment_on_issue", return_value=True) as comment:
            assert svc.repair_filed_issue(issue) == svc.RepairAction.REPAIRED
        assert [call[0][:2] for call in request.call_args_list] == [
            ("POST", "issues/713/labels"), ("POST", "issues/713/assignees"),
            ("GET", "issues/713/comments?per_page=100")]
        assert request.call_args_list[1][0][2] == {"assignees": ["gitchrisqueen"]}
        assert "Human decision needed" in comment.call_args[0][1]
        assert "risk:migration" in comment.call_args[0][1]

    def test_an_existing_decision_comment_is_not_duplicated(self):
        from cqc_lem.utilities.feedback.classifier import FeedbackRisk
        svc = _mod()
        issue = self._issue(classification=_classification(risk=FeedbackRisk.SECURITY),
                            assignees=['gitchrisqueen'])
        with patch(f"{_SVC}.github_request",
                   side_effect=[{}, [{"body": "## 🧑‍⚖️ Human decision needed — reply..."}]]), \
                patch(f"{_SVC}.comment_on_issue") as comment:
            assert svc.repair_filed_issue(issue) == svc.RepairAction.REPAIRED
        comment.assert_not_called()

    def test_an_unreadable_comment_list_does_not_post_a_second_decision_comment(self):
        from cqc_lem.utilities.feedback.classifier import FeedbackRisk
        svc = _mod()
        issue = self._issue(classification=_classification(risk=FeedbackRisk.SECURITY),
                            assignees=['gitchrisqueen'])
        with patch(f"{_SVC}.github_request", side_effect=[{}, None]), \
                patch(f"{_SVC}.comment_on_issue") as comment:
            assert svc.repair_filed_issue(issue) == svc.RepairAction.REPAIRED
        comment.assert_not_called()

    def test_an_agent_ready_issue_is_never_assigned_or_commented_on(self):
        svc = _mod()
        with patch(f"{_SVC}.github_request", return_value={}) as request, \
                patch(f"{_SVC}.comment_on_issue") as comment:
            assert svc.repair_filed_issue(self._issue()) == svc.RepairAction.REPAIRED
        assert request.call_count == 1
        comment.assert_not_called()

    def test_a_refused_repair_is_an_error_and_stops_there(self):
        svc = _mod()
        from cqc_lem.utilities.feedback.classifier import FeedbackRisk
        issue = self._issue(classification=_classification(risk=FeedbackRisk.SECURITY))
        with patch(f"{_SVC}.github_request", return_value=None) as request, \
                patch(f"{_SVC}.comment_on_issue") as comment, \
                patch(f"{_SVC}.log_error") as error:
            assert svc.repair_filed_issue(issue) == svc.RepairAction.ERROR
        assert request.call_count == 1
        comment.assert_not_called()
        assert "Issues: Read and write" in error.call_args[0][0]


class TestRepairAutoFiledIssues:
    """The sweep that runs on every filing beat — issue #718."""

    def _open_issue(self, number, labels=()):
        svc = _mod()
        classification = _classification()
        return {"number": number, "state": "open", "title": svc.build_issue_title(classification),
                "body": svc.build_issue_body(classification, 1, 1, 1),
                "labels": [{"name": name} for name in labels], "assignees": []}

    def test_no_token_reads_nothing_at_all(self, monkeypatch):
        svc = _mod()
        monkeypatch.delenv("FEEDBACK_GITHUB_TOKEN", raising=False)
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        with patch(f"{_SVC}.get_open_feedback_clusters") as clusters:
            assert svc.repair_auto_filed_issues() == {"scanned": 0, "repaired": 0, "failed": 0}
        clusters.assert_not_called()

    def test_repairs_the_broken_issues_and_leaves_the_healthy_ones(self, monkeypatch):
        svc = _mod()
        monkeypatch.setenv("FEEDBACK_GITHUB_TOKEN", "tok")
        clusters = [_cluster(1, issue=101), _cluster(2, issue=102)]
        with patch(f"{_SVC}.get_open_feedback_clusters", return_value=clusters), \
                patch(f"{_SVC}.github_request",
                      side_effect=[self._open_issue(101, labels=['bug', 'feedback-loop']),
                                   self._open_issue(102), {}]) as request:
            assert svc.repair_auto_filed_issues() == {"scanned": 2, "repaired": 1, "failed": 0}
        assert request.call_args_list[-1][0][:2] == ("POST", "issues/102/labels")

    def test_the_same_issue_is_only_checked_once_per_pass(self, monkeypatch):
        svc = _mod()
        monkeypatch.setenv("FEEDBACK_GITHUB_TOKEN", "tok")
        clusters = [_cluster(1, issue=101), _cluster(2, issue=101), {"cluster_id": 3}]
        with patch(f"{_SVC}.get_open_feedback_clusters", return_value=clusters), \
                patch(f"{_SVC}.github_request",
                      return_value=self._open_issue(101, labels=['feedback-loop'])) as request:
            assert svc.repair_auto_filed_issues() == {"scanned": 1, "repaired": 0, "failed": 0}
        assert request.call_count == 1

    def test_a_refusal_stops_the_sweep_instead_of_hammering_github(self, monkeypatch):
        svc = _mod()
        monkeypatch.setenv("FEEDBACK_GITHUB_TOKEN", "tok")
        clusters = [_cluster(1, issue=101), _cluster(2, issue=102), _cluster(3, issue=103)]
        with patch(f"{_SVC}.get_open_feedback_clusters", return_value=clusters), \
                patch(f"{_SVC}.github_request",
                      side_effect=[self._open_issue(101), None]) as request:
            assert svc.repair_auto_filed_issues() == {"scanned": 1, "repaired": 0, "failed": 1}
        assert request.call_count == 2

    def test_an_unreadable_issue_does_not_stop_the_sweep(self, monkeypatch):
        svc = _mod()
        monkeypatch.setenv("FEEDBACK_GITHUB_TOKEN", "tok")
        clusters = [_cluster(1, issue=404), _cluster(2, issue=102)]
        with patch(f"{_SVC}.get_open_feedback_clusters", return_value=clusters), \
                patch(f"{_SVC}.github_request",
                      side_effect=[None, self._open_issue(102, labels=['feedback-loop'])]):
            assert svc.repair_auto_filed_issues() == {"scanned": 2, "repaired": 0, "failed": 1}

    def test_a_run_of_unreadable_issues_stops_the_sweep(self, monkeypatch):
        # A token that cannot read issues either would otherwise cost 50 refused GETs — and 50
        # error logs — on every 30-minute filing beat, burying the one that says why.
        svc = _mod()
        monkeypatch.setenv("FEEDBACK_GITHUB_TOKEN", "tok")
        clusters = [_cluster(n, issue=100 + n) for n in range(1, 8)]
        with patch(f"{_SVC}.get_open_feedback_clusters", return_value=clusters), \
                patch(f"{_SVC}.github_request", return_value=None) as request, \
                patch(f"{_SVC}.log_error") as error:
            assert svc.repair_auto_filed_issues() == {"scanned": 3, "repaired": 0, "failed": 3}
        assert request.call_count == svc.REPAIR_READ_FAILURE_STOP
        assert "Issues: Read and write" in error.call_args[0][0]

    def test_an_unreachable_transport_defers_the_sweep_without_blaming_the_token(self, monkeypatch):
        # Issue #767: these reads were lost to the network, so the #718 token hint would be a lie —
        # and every remaining GET would only re-report the same outage.
        svc = _mod()
        monkeypatch.setenv("FEEDBACK_GITHUB_TOKEN", "tok")
        clusters = [_cluster(n, issue=100 + n) for n in range(1, 8)]
        with patch(f"{_SVC}.get_open_feedback_clusters", return_value=clusters), \
                patch(f"{_SVC}.github_request", return_value=None) as request, \
                patch(f"{_SVC}.github_unreachable", return_value=True), \
                patch(f"{_SVC}.log_warning") as warning, patch(f"{_SVC}.log_error") as error:
            assert svc.repair_auto_filed_issues() == {"scanned": 1, "repaired": 0, "failed": 1}
        assert request.call_count == 1
        assert "unreachable" in warning.call_args[0][0]
        error.assert_not_called()

    def test_the_work_list_is_bounded(self, monkeypatch):
        svc = _mod()
        monkeypatch.setenv("FEEDBACK_GITHUB_TOKEN", "tok")
        with patch(f"{_SVC}.get_open_feedback_clusters", return_value=[]) as clusters, \
                patch(f"{_SVC}.github_request"):
            svc.repair_auto_filed_issues(limit=7)
        clusters.assert_called_once_with(7)
