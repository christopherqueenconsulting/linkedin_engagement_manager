"""Unit tests for cqc_lem.utilities.observability."""

import pytest
from types import SimpleNamespace
from unittest.mock import patch

pytestmark = pytest.mark.unit

_MOD = "cqc_lem.utilities.observability"


def _usage(prompt, completion):
    return SimpleNamespace(usage=SimpleNamespace(prompt_tokens=prompt, completion_tokens=completion))


class TestTrackLlmCall:
    def test_captures_llm_call_event(self):
        with patch(f"{_MOD}.posthog") as mock_ph:
            from cqc_lem.utilities.observability import track_llm_call
            track_llm_call(model="lem-simple", prompt_tokens=10, completion_tokens=20, latency_ms=150)

        mock_ph.capture.assert_called_once()
        _, kwargs = mock_ph.capture.call_args
        assert kwargs["event"] == "llm_call"
        props = kwargs["properties"]
        assert props["model"] == "lem-simple"
        assert props["prompt_tokens"] == 10
        assert props["completion_tokens"] == 20
        assert props["total_tokens"] == 30
        assert props["latency_ms"] == 150
        assert props["success"] is True
        assert props["cost_usd"] > 0

    def test_cost_is_zero_when_no_tokens(self):
        with patch(f"{_MOD}.posthog") as mock_ph:
            from cqc_lem.utilities.observability import track_llm_call
            track_llm_call(model="lem-simple", prompt_tokens=0, completion_tokens=0, latency_ms=10)

        _, kwargs = mock_ph.capture.call_args
        assert kwargs["properties"]["cost_usd"] == 0.0

    def test_uses_system_distinct_id_when_no_user(self):
        with patch(f"{_MOD}.posthog") as mock_ph:
            from cqc_lem.utilities.observability import track_llm_call
            track_llm_call(model="lem-medium", prompt_tokens=5, completion_tokens=5, latency_ms=50)

        _, kwargs = mock_ph.capture.call_args
        assert kwargs["distinct_id"] == "system"

    def test_uses_user_id_as_distinct_id(self):
        with patch(f"{_MOD}.posthog") as mock_ph:
            from cqc_lem.utilities.observability import track_llm_call
            track_llm_call(model="lem-complex", prompt_tokens=100, completion_tokens=200,
                           latency_ms=500, user_id=42)

        _, kwargs = mock_ph.capture.call_args
        assert kwargs["distinct_id"] == "42"

    def test_success_false_propagates(self):
        with patch(f"{_MOD}.posthog") as mock_ph:
            from cqc_lem.utilities.observability import track_llm_call
            track_llm_call(model="lem-simple", prompt_tokens=0, completion_tokens=0,
                           latency_ms=10, success=False)

        _, kwargs = mock_ph.capture.call_args
        assert kwargs["properties"]["success"] is False


class TestTrackTask:
    def test_captures_celery_task_event(self):
        with patch(f"{_MOD}.posthog") as mock_ph:
            from cqc_lem.utilities.observability import track_task
            track_task(task_name="auto_check_scheduled_posts", duration_ms=200)

        mock_ph.capture.assert_called_once()
        _, kwargs = mock_ph.capture.call_args
        assert kwargs["event"] == "celery_task"
        props = kwargs["properties"]
        assert props["task"] == "auto_check_scheduled_posts"
        assert props["duration_ms"] == 200
        assert props["success"] is True

    def test_extra_kwargs_included_in_properties(self):
        with patch(f"{_MOD}.posthog") as mock_ph:
            from cqc_lem.utilities.observability import track_task
            track_task(task_name="some_task", duration_ms=100, user_id=7, post_count=5)

        _, kwargs = mock_ph.capture.call_args
        props = kwargs["properties"]
        assert props["post_count"] == 5

    def test_uses_user_id_as_distinct_id(self):
        with patch(f"{_MOD}.posthog") as mock_ph:
            from cqc_lem.utilities.observability import track_task
            track_task(task_name="some_task", duration_ms=50, user_id=99)

        _, kwargs = mock_ph.capture.call_args
        assert kwargs["distinct_id"] == "99"

    def test_system_distinct_id_when_no_user(self):
        with patch(f"{_MOD}.posthog") as mock_ph:
            from cqc_lem.utilities.observability import track_task
            track_task(task_name="sys_task", duration_ms=10)

        _, kwargs = mock_ph.capture.call_args
        assert kwargs["distinct_id"] == "system"


class TestTrackApiCall:
    def test_captures_api_call_event(self):
        with patch(f"{_MOD}.posthog") as mock_ph:
            from cqc_lem.utilities.observability import track_api_call
            track_api_call(route="/api/posts/", method="GET", status_code=200, latency_ms=30)

        mock_ph.capture.assert_called_once()
        _, kwargs = mock_ph.capture.call_args
        assert kwargs["event"] == "api_call"
        props = kwargs["properties"]
        assert props["route"] == "/api/posts/"
        assert props["method"] == "GET"
        assert props["status_code"] == 200
        assert props["latency_ms"] == 30

    def test_anonymous_distinct_id_when_no_user(self):
        with patch(f"{_MOD}.posthog") as mock_ph:
            from cqc_lem.utilities.observability import track_api_call
            track_api_call(route="/api/health", method="GET", status_code=200, latency_ms=5)

        _, kwargs = mock_ph.capture.call_args
        assert kwargs["distinct_id"] == "anonymous"

    def test_user_id_used_as_distinct_id(self):
        with patch(f"{_MOD}.posthog") as mock_ph:
            from cqc_lem.utilities.observability import track_api_call
            track_api_call(route="/api/posts/", method="POST", status_code=201, latency_ms=80, user_id=5)

        _, kwargs = mock_ph.capture.call_args
        assert kwargs["distinct_id"] == "5"


class TestLlmTrackedDecorator:
    def test_success_calls_track_with_success_true(self):
        with patch(f"{_MOD}.posthog") as mock_ph:
            from cqc_lem.utilities.observability import llm_tracked

            @llm_tracked("lem-simple")
            def my_fn():
                return "result"

            out = my_fn()

        assert out == "result"
        mock_ph.capture.assert_called_once()
        _, kwargs = mock_ph.capture.call_args
        assert kwargs["event"] == "llm_call"
        assert kwargs["properties"]["success"] is True
        assert kwargs["properties"]["model"] == "lem-simple"

    def test_exception_calls_track_with_success_false_and_reraises(self):
        with patch(f"{_MOD}.posthog") as mock_ph:
            from cqc_lem.utilities.observability import llm_tracked

            @llm_tracked("lem-complex")
            def failing_fn():
                raise ValueError("boom")

            with pytest.raises(ValueError, match="boom"):
                failing_fn()

        mock_ph.capture.assert_called_once()
        _, kwargs = mock_ph.capture.call_args
        assert kwargs["properties"]["success"] is False
        assert kwargs["properties"]["model"] == "lem-complex"

    def test_decorator_preserves_function_name(self):
        from cqc_lem.utilities.observability import llm_tracked

        @llm_tracked("lem-medium")
        def named_function():
            pass

        assert named_function.__name__ == "named_function"

    def test_latency_is_non_negative_integer(self):
        with patch(f"{_MOD}.posthog") as mock_ph:
            from cqc_lem.utilities.observability import llm_tracked

            @llm_tracked("lem-simple")
            def quick_fn():
                return 42

            quick_fn()

        _, kwargs = mock_ph.capture.call_args
        latency = kwargs["properties"]["latency_ms"]
        assert isinstance(latency, int)
        assert latency >= 0

    def test_real_tokens_extracted_from_response_usage(self):
        with patch(f"{_MOD}.posthog") as mock_ph:
            from cqc_lem.utilities.observability import llm_tracked

            @llm_tracked("lem-complex")
            def call():
                return _usage(120, 340)

            call()

        _, kwargs = mock_ph.capture.call_args
        props = kwargs["properties"]
        assert props["prompt_tokens"] == 120
        assert props["completion_tokens"] == 340
        assert props["total_tokens"] == 460
        assert props["cost_usd"] > 0

    def test_zero_tokens_when_result_has_no_usage(self):
        with patch(f"{_MOD}.posthog") as mock_ph:
            from cqc_lem.utilities.observability import llm_tracked

            @llm_tracked("lem-simple")
            def call():
                return "plain string"

            call()

        _, kwargs = mock_ph.capture.call_args
        props = kwargs["properties"]
        assert props["prompt_tokens"] == 0
        assert props["completion_tokens"] == 0
        assert props["cost_usd"] == 0.0


class TestEstimateLlmCost:
    def test_zero_tokens_zero_cost(self):
        from cqc_lem.utilities.observability import estimate_llm_cost_usd
        assert estimate_llm_cost_usd("lem-complex", 0, 0) == 0.0

    def test_known_alias_nonzero_cost(self):
        from cqc_lem.utilities.observability import estimate_llm_cost_usd
        # lem-complex: (0.003 input, 0.015 output) per 1K → 1000*0.003 + 1000*0.015
        assert estimate_llm_cost_usd("lem-complex", 1000, 1000) == pytest.approx(0.018)

    def test_unknown_model_falls_back_to_medium(self):
        from cqc_lem.utilities.observability import estimate_llm_cost_usd
        # lem-medium: (0.0006, 0.0024) per 1K
        assert estimate_llm_cost_usd("totally-unknown", 1000, 1000) == pytest.approx(0.003)

    def test_substring_match_before_default(self):
        from cqc_lem.utilities.observability import estimate_llm_cost_usd
        # "openai/lem-simple-v2" contains "lem-simple" → simple rates, not the medium fallback
        assert estimate_llm_cost_usd("openai/lem-simple-v2", 1000, 0) == pytest.approx(0.00015)

    def test_env_override(self):
        with patch.dict("os.environ", {"LLM_COST_PER_1K": '{"lem-simple": [1.0, 2.0]}'}):
            from cqc_lem.utilities.observability import estimate_llm_cost_usd
            assert estimate_llm_cost_usd("lem-simple", 1000, 1000) == pytest.approx(3.0)

    def test_malformed_env_override_ignored(self):
        with patch.dict("os.environ", {"LLM_COST_PER_1K": "not json"}):
            from cqc_lem.utilities.observability import estimate_llm_cost_usd
            assert estimate_llm_cost_usd("lem-simple", 1000, 0) == pytest.approx(0.00015)

    def test_tiny_cost_not_collapsed_to_zero(self):
        # A few prompt tokens on the cheapest tier costs < 1e-6 USD; must stay non-zero (no rounding).
        from cqc_lem.utilities.observability import estimate_llm_cost_usd
        cost = estimate_llm_cost_usd("lem-simple", 3, 0)
        assert cost > 0.0
        assert cost == pytest.approx(3 / 1000.0 * 0.00015)


class TestTrackPostOutcome:
    def test_captures_post_outcome_event(self):
        with patch(f"{_MOD}.posthog") as mock_ph:
            from cqc_lem.utilities.observability import track_post_outcome
            track_post_outcome(post_id=9, reactions=10, comments=4, reposts=2,
                               impressions=500, saves=3, user_id=1)

        mock_ph.capture.assert_called_once()
        _, kwargs = mock_ph.capture.call_args
        assert kwargs["event"] == "post_outcome"
        assert kwargs["distinct_id"] == "1"
        props = kwargs["properties"]
        assert props["post_id"] == 9
        assert props["impressions"] == 500
        # engagement = 10 + 2*4 + 2*2 = 22 ; rate = 22/500
        assert props["engagement"] == 22
        assert props["engagement_rate"] == pytest.approx(22 / 500)

    def test_engagement_rate_none_without_impressions(self):
        with patch(f"{_MOD}.posthog") as mock_ph:
            from cqc_lem.utilities.observability import track_post_outcome
            track_post_outcome(post_id=1, reactions=3, comments=1)

        _, kwargs = mock_ph.capture.call_args
        props = kwargs["properties"]
        assert props["impressions"] is None
        assert props["engagement_rate"] is None
        assert kwargs["distinct_id"] == "system"

    def test_extra_attribution_passed_through(self):
        with patch(f"{_MOD}.posthog") as mock_ph:
            from cqc_lem.utilities.observability import track_post_outcome
            track_post_outcome(post_id=1, reactions=1, comments=0, archetype="how-to")

        _, kwargs = mock_ph.capture.call_args
        assert kwargs["properties"]["archetype"] == "how-to"


class TestCostAttributionDimensions:
    def test_llm_call_carries_attribution_dims(self):
        with patch(f"{_MOD}.posthog") as mock_ph:
            from cqc_lem.utilities.observability import track_llm_call
            track_llm_call(model="lem-complex", prompt_tokens=100, completion_tokens=50, latency_ms=900,
                           user_id=7, feature="newsletter")

        _, kwargs = mock_ph.capture.call_args
        props = kwargs["properties"]
        assert props["user_id"] == 7
        assert props["feature"] == "newsletter"
        assert props["model_tier"] == "lem-complex"
        assert props["cached"] is False
        assert kwargs["distinct_id"] == "7"

    def test_feature_floors_to_system_when_caller_omits_it(self):
        with patch(f"{_MOD}.posthog") as mock_ph:
            from cqc_lem.utilities.observability import track_llm_call
            track_llm_call(model="lem-simple", prompt_tokens=1, completion_tokens=1, latency_ms=5)

        assert mock_ph.capture.call_args[1]["properties"]["feature"] == "system"

    def test_model_tier_none_for_raw_provider_model(self):
        with patch(f"{_MOD}.posthog") as mock_ph:
            from cqc_lem.utilities.observability import track_llm_call
            track_llm_call(model="gpt-4o-mini", prompt_tokens=10, completion_tokens=10, latency_ms=20)

        assert mock_ph.capture.call_args[1]["properties"]["model_tier"] is None

    def test_explicit_model_tier_wins(self):
        with patch(f"{_MOD}.posthog") as mock_ph:
            from cqc_lem.utilities.observability import track_llm_call
            track_llm_call(model="gpt-4o-mini", prompt_tokens=10, completion_tokens=10, latency_ms=20,
                           model_tier="lem-router")

        assert mock_ph.capture.call_args[1]["properties"]["model_tier"] == "lem-router"

    def test_cache_hit_costs_nothing(self):
        with patch(f"{_MOD}.posthog") as mock_ph:
            from cqc_lem.utilities.observability import track_llm_call
            track_llm_call(model="lem-complex", prompt_tokens=5000, completion_tokens=800,
                           latency_ms=12, cached=True)

        props = mock_ph.capture.call_args[1]["properties"]
        assert props["cached"] is True
        assert props["cost_usd"] == 0.0
        # Tokens are still reported — only the spend is zero.
        assert props["total_tokens"] == 5800

    def test_llm_cache_hit_reads_litellm_hidden_params(self):
        from cqc_lem.utilities.observability import llm_cache_hit
        assert llm_cache_hit(SimpleNamespace(_hidden_params={"cache_hit": True})) is True
        assert llm_cache_hit(SimpleNamespace(_hidden_params={"cache_hit": False})) is False
        assert llm_cache_hit(SimpleNamespace()) is False


class TestFeatureFromTaskName:
    @pytest.mark.parametrize("task_name,expected", [
        ("cqc_lem.app.run_scheduler.auto_generate_newsletter_drafts", "newsletter"),
        ("cqc_lem.app.run_automation.auto_publish_edition", "newsletter"),
        ("cqc_lem.app.run_automation.automate_commenting", "comment"),
        ("cqc_lem.app.run_scheduler.dispatch_comment_followups", "comment"),
        ("cqc_lem.app.run_automation.auto_seed_comment_on_post", "comment"),
        ("cqc_lem.app.run_automation.automate_profile_viewer_engagement", "dm"),
        ("cqc_lem.app.run_automation.automate_appreciation_dms_for_user", "dm"),
        ("cqc_lem.app.run_automation.send_lead_response", "dm"),
        ("cqc_lem.app.run_content_plan.plan_content_for_user", "content"),
        ("cqc_lem.app.run_content_plan.regenerate_post_carousel_task", "content"),
        ("cqc_lem.app.run_scheduler.sync_stripe_subscriptions", None),
        (None, None),
    ])
    def test_maps_task_name_to_feature(self, task_name, expected):
        from cqc_lem.utilities.observability import feature_from_task_name
        assert feature_from_task_name(task_name) == expected


class TestLlmAttributionScope:
    def test_scope_is_visible_to_current_attribution(self):
        from cqc_lem.utilities.observability import llm_attribution, current_llm_attribution
        with llm_attribution(user_id=3, feature="content"):
            assert current_llm_attribution() == (3, "content")
        assert current_llm_attribution() == (None, None)

    def test_nested_scope_inherits_and_overrides(self):
        from cqc_lem.utilities.observability import llm_attribution, current_llm_attribution
        with llm_attribution(user_id=3, feature="content"):
            with llm_attribution(feature="comment"):
                # user_id inherited from the outer scope, feature narrowed by the inner one
                assert current_llm_attribution() == (3, "comment")
            assert current_llm_attribution() == (3, "content")

    def test_falls_back_to_celery_task_name_and_kwargs(self):
        from cqc_lem.utilities.observability import current_llm_attribution
        with patch(f"{_MOD}._current_task_context",
                   return_value=("cqc_lem.app.run_automation.automate_commenting", 11)):
            assert current_llm_attribution() == (11, "comment")

    def test_explicit_scope_beats_celery_fallback(self):
        from cqc_lem.utilities.observability import llm_attribution, current_llm_attribution
        with patch(f"{_MOD}._current_task_context",
                   return_value=("cqc_lem.app.run_automation.automate_commenting", 11)):
            with llm_attribution(user_id=4, feature="dm"):
                assert current_llm_attribution() == (4, "dm")

    def test_decorator_reads_user_id_positionally_and_by_keyword(self):
        from cqc_lem.utilities.observability import attribute_llm_cost, current_llm_attribution

        @attribute_llm_cost("content")
        def generate(user_id: int, stage: str):
            return current_llm_attribution()

        assert generate(8, "awareness") == (8, "content")
        assert generate(user_id=9, stage="decision") == (9, "content")
        assert generate.__name__ == "generate"

    def test_decorator_still_sets_feature_without_a_user(self):
        from cqc_lem.utilities.observability import attribute_llm_cost, current_llm_attribution

        @attribute_llm_cost("newsletter")
        def generate(edition_id: int):
            return current_llm_attribution()

        assert generate(5) == (None, "newsletter")

    def test_scope_is_restored_when_the_wrapped_call_raises(self):
        from cqc_lem.utilities.observability import attribute_llm_cost, current_llm_attribution

        @attribute_llm_cost("content")
        def boom(user_id: int):
            raise ValueError("nope")

        with pytest.raises(ValueError):
            boom(1)
        assert current_llm_attribution() == (None, None)


class TestLlmTrackedAttribution:
    def test_decorator_emits_ambient_attribution(self):
        with patch(f"{_MOD}.posthog") as mock_ph:
            from cqc_lem.utilities.observability import llm_tracked, llm_attribution

            @llm_tracked("lem-medium")
            def call():
                return _usage(10, 10)

            with llm_attribution(user_id=6, feature="comment"):
                call()

        props = mock_ph.capture.call_args[1]["properties"]
        assert props["user_id"] == 6
        assert props["feature"] == "comment"
        assert props["model_tier"] == "lem-medium"

    def test_unattributed_call_falls_back_to_system_feature(self):
        with patch(f"{_MOD}.posthog") as mock_ph:
            from cqc_lem.utilities.observability import llm_tracked

            @llm_tracked("lem-simple")
            def call():
                return _usage(1, 1)

            call()

        assert mock_ph.capture.call_args[1]["properties"]["feature"] == "system"


class TestTrackMarginReport:
    def _report(self):
        return {
            "period": {"start": "2026-07-19", "end": "2026-07-25", "days": 7,
                       "basis": "monthly_run_rate"},
            "ledger_available": True,
            "users": [{"user_id": 1, "mrr_usd": 79.0, "cm_pct": 0.8}],
            "system": {"mrr_usd": 79.0, "gross_margin_pct": 0.62},
            "cohorts": [{"cohort": "2026-06", "users": 1, "avg_cm_usd": 63.0}],
            "unit_economics": {"ltv_usd": 600.0, "ltv_cac_ratio": 4.0, "payback_months": 3.0},
        }

    def test_captures_scorecard_properties(self):
        with patch(f"{_MOD}.posthog") as mock_ph:
            from cqc_lem.utilities.observability import track_margin_report
            track_margin_report(self._report())

        assert mock_ph.capture.call_args[1]["event"] == "margin_report"
        props = mock_ph.capture.call_args[1]["properties"]
        assert props["period_start"] == "2026-07-19" and props["period_days"] == 7
        assert props["system_gross_margin_pct"] == 0.62
        assert props["ltv_cac_ratio"] == 4.0
        assert props["cohorts"][0]["cohort"] == "2026-06"

    def test_omits_per_user_financials(self):
        with patch(f"{_MOD}.posthog") as mock_ph:
            from cqc_lem.utilities.observability import track_margin_report
            track_margin_report(self._report())

        # Per-user cost/revenue is internal-only (plan §E.5) — it must not leave in the event.
        assert "users" not in mock_ph.capture.call_args[1]["properties"]

    def test_empty_report_does_not_raise(self):
        with patch(f"{_MOD}.posthog") as mock_ph:
            from cqc_lem.utilities.observability import track_margin_report
            track_margin_report({})
        assert mock_ph.capture.call_args[1]["properties"]["cohorts"] == []
