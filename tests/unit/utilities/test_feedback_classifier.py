"""Unit tests for the feedback auto-classifier — issue #497. Covers the deterministic halves (schema
validation, alias normalization, label mapping, routing) exhaustively, and the single LLM call with
a mocked client (happy path, off-contract answers, and the fail-safe path)."""

import json
import pytest
from unittest.mock import MagicMock, patch

from cqc_lem.utilities.feedback.classifier import (
    CATEGORY_LABELS,
    COMPONENTS,
    FEEDBACK_CLASSIFICATION_SCHEMA,
    FeedbackCategory,
    FeedbackClassification,
    FeedbackRisk,
    FeedbackRoute,
    FeedbackSeverity,
    RISK_LABELS,
    SEVERITY_LABELS,
    classifier_model,
    classify_feedback,
    labels_for,
    min_confidence,
    parse_classification,
    route_for,
    validate_classification,
)

pytestmark = pytest.mark.unit

_AI = "cqc_lem.utilities.ai.ai_helper._call_llm"

# Every label this module can emit must exist in the repo's label set (gh label list).
REAL_REPO_LABELS = {
    'bug', 'feature', 'enhancement', 'cleanup', 'question', 'documentation', 'duplicate',
    'priority:critical', 'priority:high', 'priority:medium', 'priority:low',
    'risk:product-decision', 'risk:live-linkedin', 'risk:migration', 'risk:security',
    'needs-human',
}


def _valid_payload(**overrides) -> dict:
    payload = {
        "category": "bug",
        "severity": "high",
        "component": "feed-commenting",
        "title": "Feed commenting stops after the first comment",
        "summary": "User reports that only one comment is posted per run.",
        "risk": "none",
        "duplicate_of": None,
        "confidence": 0.9,
    }
    payload.update(overrides)
    return payload


def _llm_reply(payload) -> MagicMock:
    response = MagicMock()
    response.choices = [MagicMock()]
    response.choices[0].message.content = payload if isinstance(payload, str) else json.dumps(payload)
    return response


class TestSchemaValidation:
    def test_accepts_a_valid_payload(self):
        assert validate_classification(_valid_payload()) == []

    def test_rejects_non_objects(self):
        assert validate_classification(["bug"])
        assert validate_classification("bug")

    @pytest.mark.parametrize("missing", list(FEEDBACK_CLASSIFICATION_SCHEMA["required"]))
    def test_rejects_missing_required_field(self, missing):
        payload = _valid_payload()
        payload.pop(missing)
        errors = validate_classification(payload)
        assert any(missing in e for e in errors)

    @pytest.mark.parametrize("field,value", [
        ("category", "wontfix"),
        ("severity", "sev9"),
        ("component", "telepathy"),
        ("risk", "risk:unknown"),
    ])
    def test_rejects_values_outside_the_enum(self, field, value):
        errors = validate_classification(_valid_payload(**{field: value}))
        assert any(e.startswith(field) for e in errors)

    @pytest.mark.parametrize("value", [-0.1, 1.5])
    def test_rejects_confidence_out_of_range(self, value):
        assert validate_classification(_valid_payload(confidence=value))

    def test_rejects_wrong_types(self):
        assert validate_classification(_valid_payload(title=42))
        assert validate_classification(_valid_payload(duplicate_of="12"))
        # bool is an int subclass — it must not satisfy integer/number fields.
        assert validate_classification(_valid_payload(confidence=True))
        assert validate_classification(_valid_payload(duplicate_of=True))

    def test_accepts_null_duplicate_and_integer_duplicate(self):
        assert validate_classification(_valid_payload(duplicate_of=None)) == []
        assert validate_classification(_valid_payload(duplicate_of=7)) == []

    def test_rejects_overlong_title(self):
        assert validate_classification(_valid_payload(title="x" * 200))

    def test_schema_enums_match_the_enums(self):
        assert FEEDBACK_CLASSIFICATION_SCHEMA["properties"]["category"]["enum"] == \
               [c.value for c in FeedbackCategory]
        assert FEEDBACK_CLASSIFICATION_SCHEMA["properties"]["risk"]["enum"] == \
               [r.value for r in FeedbackRisk]


class TestLabelMapping:
    def test_every_mapped_label_exists_in_the_repo(self):
        emitted = set(CATEGORY_LABELS.values()) | set(SEVERITY_LABELS.values()) | set(RISK_LABELS.values())
        assert emitted <= REAL_REPO_LABELS

    def test_noise_has_no_category_label(self):
        assert FeedbackCategory.NOISE not in CATEGORY_LABELS

    def test_bug_maps_to_bug_and_priority(self):
        assert labels_for(FeedbackCategory.BUG, FeedbackSeverity.CRITICAL) == \
               ['bug', 'priority:critical']

    @pytest.mark.parametrize("category,expected", [
        (FeedbackCategory.BUG, 'bug'),
        (FeedbackCategory.FEATURE, 'feature'),
        (FeedbackCategory.ENHANCEMENT, 'enhancement'),
        (FeedbackCategory.CLEANUP, 'cleanup'),
    ])
    def test_category_label_per_category(self, category, expected):
        assert labels_for(category, FeedbackSeverity.LOW)[0] == expected

    @pytest.mark.parametrize("severity,expected", [
        (FeedbackSeverity.CRITICAL, 'priority:critical'),
        (FeedbackSeverity.HIGH, 'priority:high'),
        (FeedbackSeverity.MEDIUM, 'priority:medium'),
        (FeedbackSeverity.LOW, 'priority:low'),
    ])
    def test_severity_label_per_severity(self, severity, expected):
        assert expected in labels_for(FeedbackCategory.BUG, severity)

    @pytest.mark.parametrize("risk,expected", [
        (FeedbackRisk.PRODUCT_DECISION, 'risk:product-decision'),
        (FeedbackRisk.LIVE_LINKEDIN, 'risk:live-linkedin'),
        (FeedbackRisk.MIGRATION, 'risk:migration'),
        (FeedbackRisk.SECURITY, 'risk:security'),
    ])
    def test_risk_label_added(self, risk, expected):
        assert labels_for(FeedbackCategory.FEATURE, FeedbackSeverity.MEDIUM, risk)[-1] == expected

    def test_no_risk_label_when_risk_is_none(self):
        assert labels_for(FeedbackCategory.FEATURE, FeedbackSeverity.MEDIUM, FeedbackRisk.NONE) == \
               ['feature', 'priority:medium']

    def test_dropped_items_get_no_labels(self):
        assert labels_for(FeedbackCategory.NOISE, FeedbackSeverity.LOW,
                          FeedbackRisk.NONE, FeedbackRoute.DROP) == []

    def test_questions_get_only_the_question_label(self):
        assert labels_for(FeedbackCategory.QUESTION, FeedbackSeverity.LOW,
                          FeedbackRisk.NONE, FeedbackRoute.FAQ) == ['question']

    def test_human_triage_items_carry_needs_human(self):
        labels = labels_for(FeedbackCategory.BUG, FeedbackSeverity.MEDIUM,
                            FeedbackRisk.NONE, FeedbackRoute.NEEDS_HUMAN)
        assert labels == ['bug', 'priority:medium', 'needs-human']


class TestRouting:
    def test_confident_bug_is_auto_worked(self):
        assert route_for(FeedbackCategory.BUG, 0.9) == FeedbackRoute.AUTO_WORK

    def test_question_goes_to_faq(self):
        assert route_for(FeedbackCategory.QUESTION, 0.9) == FeedbackRoute.FAQ

    def test_noise_is_dropped(self):
        assert route_for(FeedbackCategory.NOISE, 0.95) == FeedbackRoute.DROP

    @pytest.mark.parametrize("category", list(FeedbackCategory))
    def test_low_confidence_always_goes_to_a_human(self, category):
        assert route_for(category, 0.1) == FeedbackRoute.NEEDS_HUMAN

    def test_threshold_is_env_tunable(self, monkeypatch):
        monkeypatch.setenv("FEEDBACK_CLASSIFIER_MIN_CONFIDENCE", "0.95")
        assert route_for(FeedbackCategory.BUG, 0.9) == FeedbackRoute.NEEDS_HUMAN
        monkeypatch.setenv("FEEDBACK_CLASSIFIER_MIN_CONFIDENCE", "0.1")
        assert route_for(FeedbackCategory.BUG, 0.9) == FeedbackRoute.AUTO_WORK

    @pytest.mark.parametrize("raw,expected", [
        ("", 0.6), ("not-a-number", 0.6), ("0.8", 0.8), ("-1", 0.0), ("5", 1.0),
    ])
    def test_min_confidence_parsing(self, monkeypatch, raw, expected):
        monkeypatch.setenv("FEEDBACK_CLASSIFIER_MIN_CONFIDENCE", raw)
        assert min_confidence() == expected

    def test_model_is_lem_medium_by_default(self, monkeypatch):
        monkeypatch.delenv("FEEDBACK_CLASSIFIER_MODEL", raising=False)
        assert classifier_model() == "lem-medium"
        monkeypatch.setenv("FEEDBACK_CLASSIFIER_MODEL", "lem-simple")
        assert classifier_model() == "lem-simple"


class TestParsing:
    def test_parses_a_plain_json_object(self):
        data, errors = parse_classification(json.dumps(_valid_payload()))
        assert errors == []
        assert data["category"] == "bug"

    def test_tolerates_fences_and_prose(self):
        raw = "Sure! Here you go:\n```json\n" + json.dumps(_valid_payload()) + "\n```\nHope that helps."
        data, errors = parse_classification(raw)
        assert errors == []
        assert data["severity"] == "high"

    @pytest.mark.parametrize("raw", ["", None, "no json here", "{not json}", "[1, 2]"])
    def test_unusable_replies_return_errors(self, raw):
        data, errors = parse_classification(raw)
        assert data is None
        assert errors

    @pytest.mark.parametrize("raw,expected", [
        ("Fix", "bug"), ("DEFECT", "bug"), ("idea", "feature"), ("Update", "enhancement"),
        ("refactor", "cleanup"), ("support", "question"), ("praise", "noise"),
    ])
    def test_category_aliases_are_folded(self, raw, expected):
        data, errors = parse_classification(json.dumps(_valid_payload(category=raw)))
        assert errors == []
        assert data["category"] == expected

    @pytest.mark.parametrize("raw,expected", [
        ("P1", "high"), ("blocker", "critical"), ("Normal", "medium"), ("minor", "low"),
        ("HIGH", "high"),
    ])
    def test_severity_aliases_are_folded(self, raw, expected):
        data, _ = parse_classification(json.dumps(_valid_payload(severity=raw)))
        assert data["severity"] == expected

    @pytest.mark.parametrize("raw,expected", [
        ("risk:security", "security"), ("privacy", "security"), ("db", "migration"),
        ("live_linkedin", "live-linkedin"), ("policy", "product-decision"),
        ("", "none"), (None, "none"),
    ])
    def test_risk_aliases_are_folded(self, raw, expected):
        data, errors = parse_classification(json.dumps(_valid_payload(risk=raw)))
        assert errors == []
        assert data["risk"] == expected

    def test_unknown_component_falls_back_to_other(self):
        data, _ = parse_classification(json.dumps(_valid_payload(component="Telepathy")))
        assert data["component"] == 'other'

    def test_known_component_is_normalized(self):
        data, _ = parse_classification(json.dumps(_valid_payload(component="Feed_Commenting")))
        assert data["component"] == 'feed-commenting'

    def test_overlong_text_is_clamped_not_rejected(self):
        data, errors = parse_classification(json.dumps(_valid_payload(title="t" * 300,
                                                                     summary="s" * 900)))
        assert errors == []
        assert len(data["title"]) == 80
        assert len(data["summary"]) == 500

    def test_confidence_is_clamped(self):
        assert parse_classification(json.dumps(_valid_payload(confidence=1.7)))[0]["confidence"] == 1.0
        assert parse_classification(json.dumps(_valid_payload(confidence="nope")))[0]["confidence"] == 0.0

    def test_duplicate_of_must_be_a_known_candidate(self):
        data, _ = parse_classification(json.dumps(_valid_payload(duplicate_of=99)), {12, 13})
        assert data["duplicate_of"] is None
        data, _ = parse_classification(json.dumps(_valid_payload(duplicate_of="13")), {12, 13})
        assert data["duplicate_of"] == 13

    def test_missing_field_is_still_an_error_after_normalization(self):
        payload = _valid_payload()
        payload.pop("category")
        data, errors = parse_classification(json.dumps(payload))
        assert data is None
        assert errors


class TestClassifyFeedback:
    def test_happy_path_returns_labels_and_route(self):
        with patch(_AI, return_value=_llm_reply(_valid_payload())) as mock_llm:
            result = classify_feedback("Commenting only posts one comment", type_hint="bug")
        assert result.category == FeedbackCategory.BUG
        assert result.severity == FeedbackSeverity.HIGH
        assert result.component == 'feed-commenting'
        assert result.route == FeedbackRoute.AUTO_WORK
        assert result.labels == ['bug', 'priority:high']
        assert result.needs_human is False
        assert mock_llm.call_count == 1
        assert mock_llm.call_args.kwargs["model"] == "lem-medium"
        assert mock_llm.call_args.kwargs["temperature"] == 0

    def test_prompt_carries_the_schema_hint_and_candidates(self):
        with patch(_AI, return_value=_llm_reply(_valid_payload())) as mock_llm:
            classify_feedback("It broke", type_hint="bug",
                              context={"route": "/dashboard", "app_version": "0.77.0",
                                       "screenshot": "data:image/png;base64,AAAA"},
                              duplicate_candidates=[{"id": 12, "title": "Commenting broken"}],
                              user_id=5)
        messages = mock_llm.call_args.kwargs["messages"]
        system, user = messages[0]["content"], messages[1]["content"]
        assert "duplicate_of" in system and "confidence" in system
        assert "/dashboard" in user and "0.77.0" in user
        # The screenshot blob must never be shipped to the classifier.
        assert "base64" not in user
        assert "12: Commenting broken" in user
        assert mock_llm.call_args.kwargs["_track_user_id"] == 5

    def test_question_routes_to_faq(self):
        payload = _valid_payload(category="question", severity="low",
                                 title="How do I change my posting time?")
        with patch(_AI, return_value=_llm_reply(payload)):
            result = classify_feedback("How do I change my posting time?", type_hint="other")
        assert result.route == FeedbackRoute.FAQ
        assert result.labels == ['question']

    def test_noise_is_dropped_with_no_labels(self):
        payload = _valid_payload(category="noise", severity="low", risk="none")
        with patch(_AI, return_value=_llm_reply(payload)):
            result = classify_feedback("love this tool!!", type_hint="praise")
        assert result.route == FeedbackRoute.DROP
        assert result.labels == []

    def test_low_confidence_goes_to_human_triage(self):
        with patch(_AI, return_value=_llm_reply(_valid_payload(confidence=0.2))):
            result = classify_feedback("something's off", type_hint="bug")
        assert result.route == FeedbackRoute.NEEDS_HUMAN
        assert 'needs-human' in result.labels

    def test_duplicate_is_kept_when_it_is_a_real_candidate(self):
        payload = _valid_payload(duplicate_of=12)
        with patch(_AI, return_value=_llm_reply(payload)):
            result = classify_feedback("Commenting broken again",
                                       duplicate_candidates=[{"id": 12, "body": "Commenting broken"},
                                                             {"id": "bad", "body": "ignored"}])
        assert result.duplicate_of == 12

    def test_off_contract_answer_fails_safe_to_human(self):
        with patch(_AI, return_value=_llm_reply("I think this is a bug, honestly.")):
            result = classify_feedback("The DM sequence stopped", type_hint="bug")
        assert result.route == FeedbackRoute.NEEDS_HUMAN
        assert result.confidence == 0.0
        assert result.errors
        assert result.summary == "The DM sequence stopped"

    def test_llm_error_fails_safe_to_human(self):
        with patch(_AI, side_effect=RuntimeError("proxy down")):
            result = classify_feedback("Scheduling is off by an hour", type_hint="idea")
        assert result.route == FeedbackRoute.NEEDS_HUMAN
        assert result.category == FeedbackCategory.FEATURE  # from the type hint
        assert any("proxy down" in e for e in result.errors)

    def test_unknown_type_hint_falls_back_to_bug(self):
        with patch(_AI, side_effect=RuntimeError("boom")):
            result = classify_feedback("Something happened", type_hint="wat")
        assert result.category == FeedbackCategory.BUG

    def test_explicit_hint_matching_a_category_is_used(self):
        with patch(_AI, side_effect=RuntimeError("boom")):
            result = classify_feedback("Please add dark mode", type_hint="feature")
        assert result.category == FeedbackCategory.FEATURE

    @pytest.mark.parametrize("body", ["", "   ", None])
    def test_empty_body_never_calls_the_llm(self, body):
        with patch(_AI) as mock_llm:
            result = classify_feedback(body)
        mock_llm.assert_not_called()
        assert result.route == FeedbackRoute.NEEDS_HUMAN
        assert result.errors == ["empty feedback body"]

    def test_body_is_truncated_before_the_call(self):
        with patch(_AI, return_value=_llm_reply(_valid_payload())) as mock_llm:
            classify_feedback("x" * 9000)
        assert mock_llm.call_args.kwargs["messages"][1]["content"].count("x") == 4000

    def test_to_dict_is_serializable(self):
        with patch(_AI, return_value=_llm_reply(_valid_payload())):
            result = classify_feedback("Commenting broke")
        as_dict = result.to_dict()
        assert json.loads(json.dumps(as_dict))["route"] == 'auto_work'
        assert as_dict["labels"] == ['bug', 'priority:high']
        assert as_dict["component"] in COMPONENTS


class TestClassificationDataclass:
    def test_defaults_route_to_human(self):
        result = FeedbackClassification(category=FeedbackCategory.BUG,
                                        severity=FeedbackSeverity.MEDIUM,
                                        component='other', title='t', summary='s')
        assert result.needs_human is True
        assert result.duplicate_of is None
