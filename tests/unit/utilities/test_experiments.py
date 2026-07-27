"""Unit tests for the PostHog experiment surface (issue #652).

The load-bearing behaviours, in order of how badly a regression would hurt:

1. Nothing resolvable → the CONTROL arm, always. That is the whole safety contract.
2. "PostHog said control" and "PostHog said nothing" stay apart, so a metric event is never labelled
   with an arm nobody was enrolled in.
3. Exposure fires ONCE per person per arm — these run in feed loops.
"""
from unittest.mock import MagicMock, patch

import pytest

from cqc_lem.utilities import experiments as ex
from cqc_lem.utilities import routing_policy as rp

pytestmark = pytest.mark.unit

_MOD = "cqc_lem.utilities.experiments"


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    monkeypatch.delenv("EXPERIMENTS_ENABLED", raising=False)
    ex.reset_exposure_cache()
    yield
    ex.reset_exposure_cache()


@pytest.fixture
def enrolled():
    """PostHog is reachable and answers with whatever the test sets `.return_value` to."""
    with patch(f"{_MOD}._local_evaluation_ready", return_value=True), \
            patch(f"{_MOD}.posthog") as mock_ph, \
            patch(f"{_MOD}.track_exposure") as mock_exposure:
        yield mock_ph, mock_exposure


# ── registry ──

def test_registry_keys_match_their_specs_and_control_is_first():
    for key, spec in ex.EXPERIMENTS.items():
        assert spec.key == key
        assert spec.variants, f"{key} has no arms"
        assert spec.control == spec.variants[0]
        assert spec.metric_events, f"{key} has no metric event"


def test_registry_rows_expose_every_field_docs_and_provisioning_need():
    rows = {row["key"]: row for row in ex.registry_rows()}
    assert set(rows) == set(ex.EXPERIMENTS)
    row = rows[ex.COST_ROUTING_ARM]
    assert row["control"] == rp.ARM_CONTROL
    assert rp.ARM_TREATMENT in row["variants"]
    assert row["assignment"] == ex.ASSIGNMENT_FLAG
    assert "$ai_generation" in row["metric_events"]


def test_comment_variant_name_matches_the_content_core_literal():
    """content_framework keeps its own copy so the shared content core never imports PostHog — if
    these two drift, the treatment arm silently renders the control prompt."""
    from cqc_lem.utilities.ai.content_framework import COMMENT_CONTRACT_AUTHOR_QUESTION_VARIANT
    assert COMMENT_CONTRACT_AUTHOR_QUESTION_VARIANT == ex.COMMENT_CONTRACT_AUTHOR_QUESTION
    assert ex.COMMENT_CONTRACT_AUTHOR_QUESTION in ex.spec(ex.COMMENT_CONTRACT_PROMPT).variants


def test_unregistered_experiment_raises_instead_of_defaulting():
    with pytest.raises(KeyError):
        ex.spec("not-an-experiment")


# ── fail-to-control ──

def test_no_local_evaluation_means_the_control_arm_and_no_exposure():
    with patch(f"{_MOD}._local_evaluation_ready", return_value=False), \
            patch(f"{_MOD}.track_exposure") as mock_exposure:
        assert ex.resolve_variant(ex.COMMENT_CONTRACT_PROMPT, 7) == ex.CONTROL
        assert ex.experiment_properties(7, keys=(ex.COMMENT_CONTRACT_PROMPT,)) == {}
    mock_exposure.assert_not_called()


def test_kill_switch_forces_control_even_when_posthog_would_answer(monkeypatch, enrolled):
    mock_ph, mock_exposure = enrolled
    mock_ph.get_feature_flag.return_value = ex.COMMENT_CONTRACT_AUTHOR_QUESTION
    monkeypatch.setenv("EXPERIMENTS_ENABLED", "false")
    assert ex.resolve_variant(ex.COMMENT_CONTRACT_PROMPT, 7) == ex.CONTROL
    mock_ph.get_feature_flag.assert_not_called()
    mock_exposure.assert_not_called()


def test_lookup_failure_is_the_control_arm(enrolled):
    mock_ph, _ = enrolled
    mock_ph.get_feature_flag.side_effect = RuntimeError("posthog down")
    assert ex.resolve_variant(ex.COMMENT_CONTRACT_PROMPT, 7) == ex.CONTROL


@pytest.mark.parametrize("value", [None, True, False, "", "some-other-arm"])
def test_unusable_flag_values_are_the_control_arm(enrolled, value):
    """A boolean means the flag isn't multivariate; an unknown string means it was reconfigured
    behind the code's back. Neither may become an arm the code has no branch for."""
    mock_ph, _ = enrolled
    mock_ph.get_feature_flag.return_value = value
    assert ex.resolve_variant(ex.COMMENT_CONTRACT_PROMPT, 7) == ex.CONTROL
    assert ex.experiment_properties(7, keys=(ex.COMMENT_CONTRACT_PROMPT,)) == {}


def test_missing_flag_surface_reports_once_and_reads_as_not_ready(monkeypatch):
    """Both halves of the lazy import have to be removed: the parent package caches an imported
    submodule as an attribute, so blanking only `sys.modules` still resolves the real module."""
    import sys

    import cqc_lem.utilities as pkg

    monkeypatch.delattr(pkg, "flags", raising=False)
    monkeypatch.setitem(sys.modules, "cqc_lem.utilities.flags", None)
    with patch(f"{_MOD}.log_warning") as mock_warn:
        ex.reset_exposure_cache()
        assert ex._local_evaluation_ready() is False
        assert ex._local_evaluation_ready() is False
    assert mock_warn.call_count == 1


def test_local_evaluation_requires_both_availability_and_a_loaded_definition_set(monkeypatch):
    import cqc_lem.utilities as pkg

    flags = MagicMock()
    flags.local_evaluation_available.return_value = True
    flags._ensure_loaded.return_value = False
    monkeypatch.setattr(pkg, "flags", flags)
    ex.reset_exposure_cache()
    assert ex._local_evaluation_ready() is False
    flags._ensure_loaded.return_value = True
    assert ex._local_evaluation_ready() is True


# ── assignment ──

def test_resolved_variant_is_returned_and_exposed(enrolled):
    mock_ph, mock_exposure = enrolled
    mock_ph.get_feature_flag.return_value = ex.COMMENT_CONTRACT_AUTHOR_QUESTION
    assert ex.resolve_variant(ex.COMMENT_CONTRACT_PROMPT, 7) == ex.COMMENT_CONTRACT_AUTHOR_QUESTION
    mock_exposure.assert_called_once_with(ex.COMMENT_CONTRACT_PROMPT,
                                         ex.COMMENT_CONTRACT_AUTHOR_QUESTION, 7)


def test_local_evaluation_only_never_makes_a_network_call(enrolled):
    mock_ph, _ = enrolled
    mock_ph.get_feature_flag.return_value = rp.ARM_TREATMENT
    ex.resolve_variant(ex.COST_ROUTING_ARM, 7)
    kwargs = mock_ph.get_feature_flag.call_args.kwargs
    assert kwargs["only_evaluate_locally"] is True
    # The SDK's own event would duplicate the deduped exposure this module emits.
    assert kwargs["send_feature_flag_events"] is False


def test_track_false_resolves_without_enrolling(enrolled):
    mock_ph, mock_exposure = enrolled
    mock_ph.get_feature_flag.return_value = rp.ARM_TREATMENT
    assert ex.is_treatment(ex.COST_ROUTING_ARM, 7, track=False) is True
    mock_exposure.assert_not_called()


def test_shipped_assignment_never_consults_a_flag(enrolled):
    mock_ph, _ = enrolled
    assert ex.resolve_variant(ex.POST_MEDIA_VARIANT, 7) == ex.CONTROL
    mock_ph.get_feature_flag.assert_not_called()


def test_assignments_omits_users_posthog_has_no_answer_for(enrolled):
    mock_ph, mock_exposure = enrolled
    mock_ph.get_feature_flag.side_effect = lambda key, distinct_id, **kw: (
        rp.ARM_TREATMENT if distinct_id == "7" else None)
    assert ex.assignments([7, 8, None], ex.COST_ROUTING_ARM) == {7: rp.ARM_TREATMENT}
    mock_exposure.assert_called_once_with(ex.COST_ROUTING_ARM, rp.ARM_TREATMENT, 7)


def test_distinct_id_matches_the_shared_system_sentinel():
    assert ex.distinct_id(7) == "7"
    assert ex.distinct_id() == rp.SYSTEM_USER_ID


# ── metric labelling ──

def test_experiment_properties_labels_only_enrolled_persons(enrolled):
    mock_ph, _ = enrolled
    mock_ph.get_feature_flag.return_value = rp.ARM_TREATMENT
    props = ex.experiment_properties(7, keys=(ex.COST_ROUTING_ARM,))
    assert props == {f"$feature/{ex.COST_ROUTING_ARM}": rp.ARM_TREATMENT}


def test_experiment_properties_slugifies_a_shipped_arm(enrolled):
    props = ex.experiment_properties(
        7, extra={ex.POST_MEDIA_VARIANT: "black-forest-labs/flux-dev|gen4_turbo|1:1"})
    assert props == {f"$feature/{ex.POST_MEDIA_VARIANT}":
                     "black-forest-labs-flux-dev-gen4-turbo-1-1"}


def test_experiment_properties_drops_an_empty_shipped_arm():
    assert ex.experiment_properties(7, extra={ex.POST_MEDIA_VARIANT: None}) == {}


# ── exposure emission ──

def test_exposure_is_emitted_once_per_person_and_arm():
    with patch("cqc_lem.utilities.observability.track_experiment_exposure") as mock_track:
        assert ex.track_exposure(ex.COST_ROUTING_ARM, rp.ARM_TREATMENT, 7) is True
        assert ex.track_exposure(ex.COST_ROUTING_ARM, rp.ARM_TREATMENT, 7) is False
        # A different person, and the same person in a different arm, are both new exposures.
        assert ex.track_exposure(ex.COST_ROUTING_ARM, rp.ARM_TREATMENT, 8) is True
        assert ex.track_exposure(ex.COST_ROUTING_ARM, rp.ARM_CONTROL, 7) is True
    assert mock_track.call_count == 3


def test_exposure_never_raises_when_capture_fails():
    with patch("cqc_lem.utilities.observability.track_experiment_exposure",
               side_effect=RuntimeError("boom")):
        assert ex.track_exposure(ex.COST_ROUTING_ARM, rp.ARM_TREATMENT, 7) is False


def test_exposure_cache_is_bounded():
    with patch("cqc_lem.utilities.observability.track_experiment_exposure"):
        for user_id in range(ex._EXPOSURE_CACHE_MAX + 5):
            ex.track_exposure(ex.COST_ROUTING_ARM, rp.ARM_TREATMENT, user_id)
    assert len(ex._exposed) <= ex._EXPOSURE_CACHE_MAX


def test_track_shipped_variant_reports_the_combo_that_shipped():
    with patch(f"{_MOD}.track_exposure") as mock_exposure:
        variant = ex.track_shipped_variant(ex.POST_MEDIA_VARIANT, "flux-dev|gen4_turbo|1:1",
                                           user_id=7, post_id=99)
    assert variant == "flux-dev-gen4-turbo-1-1"
    assert mock_exposure.call_args.args[:2] == (ex.POST_MEDIA_VARIANT, variant)
    assert mock_exposure.call_args.kwargs["post_id"] == 99


def test_track_shipped_variant_is_a_no_op_without_a_key_or_on_a_flag_experiment():
    with patch(f"{_MOD}.track_exposure") as mock_exposure:
        assert ex.track_shipped_variant(ex.POST_MEDIA_VARIANT, None, user_id=7) is None
        assert ex.track_shipped_variant(ex.COST_ROUTING_ARM, "anything", user_id=7) is None
    mock_exposure.assert_not_called()


# ── slugs ──

def test_variant_slug_is_posthog_safe_stable_and_collision_free():
    assert ex.variant_slug("Flux Dev / gen4_turbo") == "flux-dev-gen4-turbo"
    assert ex.variant_slug("") == ex.CONTROL
    assert ex.variant_slug(None) == ex.CONTROL
    long_a, long_b = "x" * 80 + "-a", "x" * 80 + "-b"
    slug_a, slug_b = ex.variant_slug(long_a), ex.variant_slug(long_b)
    assert len(slug_a) <= ex._SLUG_MAX and slug_a != slug_b
    assert slug_a == ex.variant_slug(long_a)  # deterministic


def test_enrollment_available_gates_the_expensive_cohort_path(monkeypatch):
    with patch(f"{_MOD}._local_evaluation_ready", return_value=True):
        assert ex.enrollment_available() is True
        monkeypatch.setenv("EXPERIMENTS_ENABLED", "off")
        assert ex.enrollment_available() is False
    with patch(f"{_MOD}._local_evaluation_ready", return_value=False):
        monkeypatch.delenv("EXPERIMENTS_ENABLED", raising=False)
        assert ex.enrollment_available() is False
