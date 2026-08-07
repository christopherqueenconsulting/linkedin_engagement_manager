"""Unit tests for the feedback auto-classifier — issue #497. Covers the deterministic halves (schema
validation, alias normalization, label mapping, routing) exhaustively, and the single LLM call with
a mocked client (happy path, off-contract answers, and the fail-safe path).
"""

import json
from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit

_AI = "cqc_lem.utilities.ai.ai_helper._call_llm"

# Every label this module can emit must exist in the repo's label set (gh label list).
REAL_REPO_LABELS = {
    'bug', 'feature', 'enhancement', 'cleanup', 'question', 'documentation', 'duplicate',
    'priority:critical', 'priority:high', 'priority:medium', 'priority:low',
    'risk:product-decision', 'risk:live-linkedin', 'risk:migration', 'risk:security',
    'needs-human',
}

# Literals so parametrization never imports cqc_lem at collection time; asserted against the real
# schema/enums in test_required_fields_match_the_schema / test_categories_match_the_enum.
REQUIRED_FIELDS = ["category", "severity", "component", "title", "summary", "risk",
                   "duplicate_of", "confidence"]
CATEGORIES = ["bug", "feature", "enhancement", "cleanup", "question", "noise"]


def _mod():
    """The module under test, imported inside the test (per tests.instructions.md)."""
    from cqc_lem.utilities.feedback import classifier
    return classifier


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
        assert _mod().validate_classification(_valid_payload()) == []

    def test_rejects_non_objects(self):
        validate_classification = _mod().validate_classification
        assert validate_classification(["bug"])
        assert validate_classification("bug")

    def test_required_fields_match_the_schema(self):
        assert _mod().FEEDBACK_CLASSIFICATION_SCHEMA["required"] == REQUIRED_FIELDS

    @pytest.mark.parametrize("missing", REQUIRED_FIELDS)
    def test_rejects_missing_required_field(self, missing):
        payload = _valid_payload()
        payload.pop(missing)
        errors = _mod().validate_classification(payload)
        assert any(missing in e for e in errors)

    @pytest.mark.parametrize("field,value", [
        ("category", "wontfix"),
        ("severity", "sev9"),
        ("component", "telepathy"),
        ("risk", "risk:unknown"),
    ])
    def test_rejects_values_outside_the_enum(self, field, value):
        errors = _mod().validate_classification(_valid_payload(**{field: value}))
        assert any(e.startswith(field) for e in errors)

    @pytest.mark.parametrize("value", [-0.1, 1.5])
    def test_rejects_confidence_out_of_range(self, value):
        assert _mod().validate_classification(_valid_payload(confidence=value))

    def test_rejects_wrong_types(self):
        validate_classification = _mod().validate_classification
        assert validate_classification(_valid_payload(title=42))
        assert validate_classification(_valid_payload(duplicate_of="12"))
        # bool is an int subclass — it must not satisfy integer/number fields.
        assert validate_classification(_valid_payload(confidence=True))
        assert validate_classification(_valid_payload(duplicate_of=True))

    def test_accepts_null_duplicate_and_integer_duplicate(self):
        validate_classification = _mod().validate_classification
        assert validate_classification(_valid_payload(duplicate_of=None)) == []
        assert validate_classification(_valid_payload(duplicate_of=7)) == []

    def test_rejects_overlong_title(self):
        assert _mod().validate_classification(_valid_payload(title="x" * 200))

    def test_schema_enums_match_the_enums(self):
        classifier = _mod()
        assert classifier.FEEDBACK_CLASSIFICATION_SCHEMA["properties"]["category"]["enum"] == \
               [c.value for c in classifier.FeedbackCategory]
        assert classifier.FEEDBACK_CLASSIFICATION_SCHEMA["properties"]["risk"]["enum"] == \
               [r.value for r in classifier.FeedbackRisk]

    def test_categories_match_the_enum(self):
        assert [c.value for c in _mod().FeedbackCategory] == CATEGORIES


class TestLabelMapping:
    def test_every_mapped_label_exists_in_the_repo(self):
        classifier = _mod()
        emitted = (set(classifier.CATEGORY_LABELS.values()) | set(classifier.SEVERITY_LABELS.values())
                   | set(classifier.RISK_LABELS.values()))
        assert emitted <= REAL_REPO_LABELS

    def test_noise_has_no_category_label(self):
        classifier = _mod()
        assert classifier.FeedbackCategory.NOISE not in classifier.CATEGORY_LABELS

    def test_bug_maps_to_bug_and_priority(self):
        classifier = _mod()
        assert classifier.labels_for(classifier.FeedbackCategory.BUG,
                                     classifier.FeedbackSeverity.CRITICAL) == \
               ['bug', 'priority:critical']

    @pytest.mark.parametrize("category,expected", [
        ("bug", 'bug'),
        ("feature", 'feature'),
        ("enhancement", 'enhancement'),
        ("cleanup", 'cleanup'),
    ])
    def test_category_label_per_category(self, category, expected):
        classifier = _mod()
        labels = classifier.labels_for(classifier.FeedbackCategory(category),
                                       classifier.FeedbackSeverity.LOW)
        assert labels[0] == expected

    @pytest.mark.parametrize("severity,expected", [
        ("critical", 'priority:critical'),
        ("high", 'priority:high'),
        ("medium", 'priority:medium'),
        ("low", 'priority:low'),
    ])
    def test_severity_label_per_severity(self, severity, expected):
        classifier = _mod()
        assert expected in classifier.labels_for(classifier.FeedbackCategory.BUG,
                                                 classifier.FeedbackSeverity(severity))

    @pytest.mark.parametrize("risk,expected", [
        ("product-decision", 'risk:product-decision'),
        ("live-linkedin", 'risk:live-linkedin'),
        ("migration", 'risk:migration'),
        ("security", 'risk:security'),
    ])
    def test_risk_label_added(self, risk, expected):
        classifier = _mod()
        labels = classifier.labels_for(classifier.FeedbackCategory.FEATURE,
                                       classifier.FeedbackSeverity.MEDIUM,
                                       classifier.FeedbackRisk(risk))
        assert labels[-1] == expected

    def test_no_risk_label_when_risk_is_none(self):
        classifier = _mod()
        assert classifier.labels_for(classifier.FeedbackCategory.FEATURE,
                                     classifier.FeedbackSeverity.MEDIUM,
                                     classifier.FeedbackRisk.NONE) == ['feature', 'priority:medium']

    def test_dropped_items_get_no_labels(self):
        classifier = _mod()
        assert classifier.labels_for(classifier.FeedbackCategory.NOISE,
                                     classifier.FeedbackSeverity.LOW,
                                     classifier.FeedbackRisk.NONE,
                                     classifier.FeedbackRoute.DROP) == []

    def test_questions_get_only_the_question_label(self):
        classifier = _mod()
        assert classifier.labels_for(classifier.FeedbackCategory.QUESTION,
                                     classifier.FeedbackSeverity.LOW,
                                     classifier.FeedbackRisk.NONE,
                                     classifier.FeedbackRoute.FAQ) == ['question']

    def test_human_triage_items_carry_needs_human(self):
        classifier = _mod()
        labels = classifier.labels_for(classifier.FeedbackCategory.BUG,
                                       classifier.FeedbackSeverity.MEDIUM,
                                       classifier.FeedbackRisk.NONE,
                                       classifier.FeedbackRoute.NEEDS_HUMAN)
        assert labels == ['bug', 'priority:medium', 'needs-human']


class TestRouting:
    def test_confident_bug_is_auto_worked(self):
        classifier = _mod()
        assert classifier.route_for(classifier.FeedbackCategory.BUG, 0.9) == \
               classifier.FeedbackRoute.AUTO_WORK

    def test_question_goes_to_faq(self):
        classifier = _mod()
        assert classifier.route_for(classifier.FeedbackCategory.QUESTION, 0.9) == \
               classifier.FeedbackRoute.FAQ

    def test_noise_is_dropped(self):
        classifier = _mod()
        assert classifier.route_for(classifier.FeedbackCategory.NOISE, 0.95) == \
               classifier.FeedbackRoute.DROP

    @pytest.mark.parametrize("category", CATEGORIES)
    def test_low_confidence_always_goes_to_a_human(self, category):
        classifier = _mod()
        assert classifier.route_for(classifier.FeedbackCategory(category), 0.1) == \
               classifier.FeedbackRoute.NEEDS_HUMAN

    def test_threshold_is_env_tunable(self, monkeypatch):
        classifier = _mod()
        monkeypatch.setenv("FEEDBACK_CLASSIFIER_MIN_CONFIDENCE", "0.95")
        assert classifier.route_for(classifier.FeedbackCategory.BUG, 0.9) == \
               classifier.FeedbackRoute.NEEDS_HUMAN
        monkeypatch.setenv("FEEDBACK_CLASSIFIER_MIN_CONFIDENCE", "0.1")
        assert classifier.route_for(classifier.FeedbackCategory.BUG, 0.9) == \
               classifier.FeedbackRoute.AUTO_WORK

    @pytest.mark.parametrize("raw,expected", [
        ("", 0.6), ("not-a-number", 0.6), ("0.8", 0.8), ("-1", 0.0), ("5", 1.0),
    ])
    def test_min_confidence_parsing(self, monkeypatch, raw, expected):
        monkeypatch.setenv("FEEDBACK_CLASSIFIER_MIN_CONFIDENCE", raw)
        assert _mod().min_confidence() == expected

    def test_model_is_lem_medium_by_default(self, monkeypatch):
        classifier_model = _mod().classifier_model
        monkeypatch.delenv("FEEDBACK_CLASSIFIER_MODEL", raising=False)
        assert classifier_model() == "lem-medium"
        monkeypatch.setenv("FEEDBACK_CLASSIFIER_MODEL", "lem-simple")
        assert classifier_model() == "lem-simple"


class TestParsing:
    def test_parses_a_plain_json_object(self):
        data, errors = _mod().parse_classification(json.dumps(_valid_payload()))
        assert errors == []
        assert data["category"] == "bug"

    def test_tolerates_fences_and_prose(self):
        raw = "Sure! Here you go:\n```json\n" + json.dumps(_valid_payload()) + "\n```\nHope that helps."
        data, errors = _mod().parse_classification(raw)
        assert errors == []
        assert data["severity"] == "high"

    def test_trailing_prose_with_braces_does_not_break_extraction(self):
        raw = json.dumps(_valid_payload()) + "\n(note: the {component} field is a guess}"
        data, errors = _mod().parse_classification(raw)
        assert errors == []
        assert data["component"] == 'feed-commenting'

    def test_first_of_two_objects_wins(self):
        raw = json.dumps(_valid_payload()) + "\n" + json.dumps(_valid_payload(severity="low"))
        data, errors = _mod().parse_classification(raw)
        assert errors == []
        assert data["severity"] == "high"

    def test_skips_a_leading_non_json_brace_block(self):
        raw = "Reasoning: {the user says commenting is broken}\n" + json.dumps(_valid_payload())
        data, errors = _mod().parse_classification(raw)
        assert errors == []
        assert data["category"] == "bug"

    def test_braces_inside_string_values_are_not_counted(self):
        payload = _valid_payload(summary="The UI shows a literal {placeholder} instead of my name")
        data, errors = _mod().parse_classification(json.dumps(payload))
        assert errors == []
        assert data["summary"].endswith("instead of my name")

    def test_unterminated_object_is_an_error(self):
        raw = '{"category": "bug", "severity": "high"'
        data, errors = _mod().parse_classification(raw)
        assert data is None
        assert errors == ["no JSON object in the response"]

    @pytest.mark.parametrize("raw", ["", None, "no json here", "{not json}", "[1, 2]"])
    def test_unusable_replies_return_errors(self, raw):
        data, errors = _mod().parse_classification(raw)
        assert data is None
        assert errors

    @pytest.mark.parametrize("raw,expected", [
        ("Fix", "bug"), ("DEFECT", "bug"), ("idea", "feature"), ("Update", "enhancement"),
        ("refactor", "cleanup"), ("support", "question"), ("praise", "noise"),
    ])
    def test_category_aliases_are_folded(self, raw, expected):
        data, errors = _mod().parse_classification(json.dumps(_valid_payload(category=raw)))
        assert errors == []
        assert data["category"] == expected

    @pytest.mark.parametrize("raw,expected", [
        ("P1", "high"), ("blocker", "critical"), ("Normal", "medium"), ("minor", "low"),
        ("HIGH", "high"),
    ])
    def test_severity_aliases_are_folded(self, raw, expected):
        data, _ = _mod().parse_classification(json.dumps(_valid_payload(severity=raw)))
        assert data["severity"] == expected

    @pytest.mark.parametrize("raw,expected", [
        ("risk:security", "security"), ("privacy", "security"), ("db", "migration"),
        ("live_linkedin", "live-linkedin"), ("policy", "product-decision"),
        ("", "none"), (None, "none"),
    ])
    def test_risk_aliases_are_folded(self, raw, expected):
        data, errors = _mod().parse_classification(json.dumps(_valid_payload(risk=raw)))
        assert errors == []
        assert data["risk"] == expected

    def test_unknown_component_falls_back_to_other(self):
        data, _ = _mod().parse_classification(json.dumps(_valid_payload(component="Telepathy")))
        assert data["component"] == 'other'

    def test_known_component_is_normalized(self):
        data, _ = _mod().parse_classification(json.dumps(_valid_payload(component="Feed_Commenting")))
        assert data["component"] == 'feed-commenting'

    def test_overlong_text_is_clamped_not_rejected(self):
        data, errors = _mod().parse_classification(json.dumps(_valid_payload(title="t" * 300,
                                                                            summary="s" * 900)))
        assert errors == []
        assert len(data["title"]) == 80
        assert len(data["summary"]) == 500

    def test_confidence_is_clamped(self):
        parse_classification = _mod().parse_classification
        assert parse_classification(json.dumps(_valid_payload(confidence=1.7)))[0]["confidence"] == 1.0
        assert parse_classification(json.dumps(_valid_payload(confidence="nope")))[0]["confidence"] == 0.0

    def test_duplicate_of_must_be_a_known_candidate(self):
        parse_classification = _mod().parse_classification
        data, _ = parse_classification(json.dumps(_valid_payload(duplicate_of=99)), {12, 13})
        assert data["duplicate_of"] is None
        data, _ = parse_classification(json.dumps(_valid_payload(duplicate_of="13")), {12, 13})
        assert data["duplicate_of"] == 13

    def test_missing_field_is_still_an_error_after_normalization(self):
        payload = _valid_payload()
        payload.pop("category")
        data, errors = _mod().parse_classification(json.dumps(payload))
        assert data is None
        assert errors


class TestClassifyFeedback:
    def test_happy_path_returns_labels_and_route(self):
        classifier = _mod()
        with patch(_AI, return_value=_llm_reply(_valid_payload())) as mock_llm:
            result = classifier.classify_feedback("Commenting only posts one comment",
                                                  type_hint="bug")
        assert result.category == classifier.FeedbackCategory.BUG
        assert result.severity == classifier.FeedbackSeverity.HIGH
        assert result.component == 'feed-commenting'
        assert result.route == classifier.FeedbackRoute.AUTO_WORK
        assert result.labels == ['bug', 'priority:high']
        assert result.needs_human is False
        assert mock_llm.call_count == 1
        assert mock_llm.call_args.kwargs["model"] == "lem-medium"
        assert mock_llm.call_args.kwargs["temperature"] == 0

    def test_prompt_carries_the_schema_hint_and_candidates(self):
        classify_feedback = _mod().classify_feedback
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
        classifier = _mod()
        payload = _valid_payload(category="question", severity="low",
                                 title="How do I change my posting time?")
        with patch(_AI, return_value=_llm_reply(payload)):
            result = classifier.classify_feedback("How do I change my posting time?",
                                                  type_hint="other")
        assert result.route == classifier.FeedbackRoute.FAQ
        assert result.labels == ['question']

    def test_noise_is_dropped_with_no_labels(self):
        classifier = _mod()
        payload = _valid_payload(category="noise", severity="low", risk="none")
        with patch(_AI, return_value=_llm_reply(payload)):
            result = classifier.classify_feedback("love this tool!!", type_hint="praise")
        assert result.route == classifier.FeedbackRoute.DROP
        assert result.labels == []

    def test_low_confidence_goes_to_human_triage(self):
        classifier = _mod()
        with patch(_AI, return_value=_llm_reply(_valid_payload(confidence=0.2))):
            result = classifier.classify_feedback("something's off", type_hint="bug")
        assert result.route == classifier.FeedbackRoute.NEEDS_HUMAN
        assert 'needs-human' in result.labels

    def test_duplicate_is_kept_when_it_is_a_real_candidate(self):
        classify_feedback = _mod().classify_feedback
        payload = _valid_payload(duplicate_of=12)
        with patch(_AI, return_value=_llm_reply(payload)):
            result = classify_feedback("Commenting broken again",
                                       duplicate_candidates=[{"id": 12, "body": "Commenting broken"},
                                                             {"id": "bad", "body": "ignored"}])
        assert result.duplicate_of == 12

    def test_off_contract_answer_fails_safe_to_human(self):
        classifier = _mod()
        with patch(_AI, return_value=_llm_reply("I think this is a bug, honestly.")):
            result = classifier.classify_feedback("The DM sequence stopped", type_hint="bug")
        assert result.route == classifier.FeedbackRoute.NEEDS_HUMAN
        assert result.confidence == 0.0
        assert result.errors
        assert result.summary == "The DM sequence stopped"

    def test_llm_error_fails_safe_to_human(self):
        classifier = _mod()
        with patch(_AI, side_effect=RuntimeError("proxy down")):
            result = classifier.classify_feedback("Scheduling is off by an hour", type_hint="idea")
        assert result.route == classifier.FeedbackRoute.NEEDS_HUMAN
        assert result.category == classifier.FeedbackCategory.FEATURE  # from the type hint
        assert any("proxy down" in e for e in result.errors)

    def test_unknown_type_hint_falls_back_to_bug(self):
        classifier = _mod()
        with patch(_AI, side_effect=RuntimeError("boom")):
            result = classifier.classify_feedback("Something happened", type_hint="wat")
        assert result.category == classifier.FeedbackCategory.BUG

    def test_explicit_hint_matching_a_category_is_used(self):
        classifier = _mod()
        with patch(_AI, side_effect=RuntimeError("boom")):
            result = classifier.classify_feedback("Please add dark mode", type_hint="feature")
        assert result.category == classifier.FeedbackCategory.FEATURE

    @pytest.mark.parametrize("body", ["", "   ", None])
    def test_empty_body_never_calls_the_llm(self, body):
        classifier = _mod()
        with patch(_AI) as mock_llm:
            result = classifier.classify_feedback(body)
        mock_llm.assert_not_called()
        assert result.route == classifier.FeedbackRoute.NEEDS_HUMAN
        assert result.errors == ["empty feedback body"]

    def test_body_is_truncated_before_the_call(self):
        classify_feedback = _mod().classify_feedback
        with patch(_AI, return_value=_llm_reply(_valid_payload())) as mock_llm:
            classify_feedback("x" * 9000)
        assert mock_llm.call_args.kwargs["messages"][1]["content"].count("x") == 4000

    def test_to_dict_is_serializable(self):
        classifier = _mod()
        with patch(_AI, return_value=_llm_reply(_valid_payload())):
            result = classifier.classify_feedback("Commenting broke")
        as_dict = result.to_dict()
        assert json.loads(json.dumps(as_dict))["route"] == 'auto_work'
        assert as_dict["labels"] == ['bug', 'priority:high']
        assert as_dict["component"] in classifier.COMPONENTS


class TestClassificationDataclass:
    def test_defaults_route_to_human(self):
        classifier = _mod()
        result = classifier.FeedbackClassification(category=classifier.FeedbackCategory.BUG,
                                                   severity=classifier.FeedbackSeverity.MEDIUM,
                                                   component='other', title='t', summary='s')
        assert result.needs_human is True
        assert result.duplicate_of is None
