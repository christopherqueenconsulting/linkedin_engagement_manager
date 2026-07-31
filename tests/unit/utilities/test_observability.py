"""Unit tests for cqc_lem.utilities.observability."""

import pytest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

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

    def test_vendored_map_prices_by_serving_model(self):
        from cqc_lem.utilities.observability import estimate_llm_cost_usd
        # openai/gpt-4o-mini from the vendored LiteLLM map: 1.5e-7 in / 6e-7 out per token.
        assert estimate_llm_cost_usd("openai/gpt-4o-mini", 1000, 1000) == pytest.approx(0.00075)

    def test_bare_serving_model_name_resolves_via_vendored_map(self):
        from cqc_lem.utilities.observability import estimate_llm_cost_usd
        # LiteLLM may return just "gpt-4o-mini"; the snapshot is keyed by "openai/gpt-4o-mini".
        assert estimate_llm_cost_usd("gpt-4o-mini", 1000, 1000) == pytest.approx(0.00075)

    def test_ollama_serving_model_is_zero_marginal_cost(self):
        from cqc_lem.utilities.observability import estimate_llm_cost_usd
        assert estimate_llm_cost_usd("openai/qwen3.5:397b", 1000, 1000) == 0.0

    def test_map_miss_falls_back_to_legacy_tier_alias(self):
        from cqc_lem.utilities.observability import estimate_llm_cost_usd
        # An unknown serving model that contains a tier alias substring still resolves.
        assert estimate_llm_cost_usd("openai/lem-simple-v2", 1000, 0) == pytest.approx(0.00015)

    def test_env_override_takes_precedence_over_vendored_map(self):
        with patch.dict("os.environ", {"LLM_COST_PER_1K": '{"openai/gpt-4o-mini": [1.0, 2.0]}'}):
            from cqc_lem.utilities.observability import estimate_llm_cost_usd
            assert estimate_llm_cost_usd("openai/gpt-4o-mini", 1000, 1000) == pytest.approx(3.0)


class TestEstimateShadowCost:
    def test_zero_tokens_no_shadow(self):
        from cqc_lem.utilities.observability import estimate_shadow_cost_usd
        assert estimate_shadow_cost_usd("openai/qwen3.5:397b", 0, 0) is None

    def test_ollama_model_emits_shadow_at_reference(self):
        from cqc_lem.utilities.observability import estimate_shadow_cost_usd
        # qwen3.5:397b maps to openai/gpt-4o as the hand-picked metered reference.
        assert estimate_shadow_cost_usd("openai/qwen3.5:397b", 1000, 1000) == pytest.approx(0.0125)

    def test_small_ollama_model_emits_shadow_at_cheaper_reference(self):
        from cqc_lem.utilities.observability import estimate_shadow_cost_usd
        # gpt-oss:20b maps to openai/gpt-4o-mini.
        assert estimate_shadow_cost_usd("openai/gpt-oss:20b", 1000, 1000) == pytest.approx(0.00075)

    def test_metered_model_has_no_shadow(self):
        from cqc_lem.utilities.observability import estimate_shadow_cost_usd
        assert estimate_shadow_cost_usd("openai/gpt-4o-mini", 1000, 1000) is None

    def test_tier_alias_has_no_shadow(self):
        from cqc_lem.utilities.observability import estimate_shadow_cost_usd
        assert estimate_shadow_cost_usd("lem-simple", 1000, 1000) is None

    def test_unknown_model_has_no_shadow(self):
        from cqc_lem.utilities.observability import estimate_shadow_cost_usd
        assert estimate_shadow_cost_usd("some-unknown", 1000, 1000) is None


class TestTrackPrePostEngagement:
    def test_captures_pre_post_engagement_event(self):
        with patch(f"{_MOD}.posthog") as mock_ph:
            from cqc_lem.utilities.observability import track_pre_post_engagement
            track_pre_post_engagement(42, 7, "ran", comments=3)

        mock_ph.capture.assert_called_once()
        _, kwargs = mock_ph.capture.call_args
        assert kwargs["event"] == "pre_post_engagement"
        assert kwargs["distinct_id"] == "7"
        props = kwargs["properties"]
        assert props["post_id"] == 42
        assert props["status"] == "ran"
        assert props["comments"] == 3

    def test_falls_back_to_system_distinct_id(self):
        with patch(f"{_MOD}.posthog") as mock_ph:
            from cqc_lem.utilities.observability import track_pre_post_engagement
            track_pre_post_engagement(42, None, "skipped", skip_reason="throttled")

        _, kwargs = mock_ph.capture.call_args
        assert kwargs["distinct_id"] == "system"
        assert kwargs["properties"]["skip_reason"] == "throttled"


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

    def test_serving_model_prices_cost_and_preserves_tier(self):
        with patch(f"{_MOD}.posthog") as mock_ph, patch(f"{_MOD}._accrue_llm_cost") as accrue:
            from cqc_lem.utilities.observability import track_llm_call
            track_llm_call(
                model="lem-simple",
                serving_model="openai/gpt-4o-mini",
                prompt_tokens=1000,
                completion_tokens=1000,
                latency_ms=100,
                user_id=7,
                feature="content",
            )

        props = mock_ph.capture.call_args[1]["properties"]
        assert props["model"] == "openai/gpt-4o-mini"  # serving model is the event model
        assert props["model_tier"] == "lem-simple"      # requested alias preserved
        # Cost priced by serving model, not the lem-simple fallback.
        assert props["cost_usd"] == pytest.approx(0.00075)
        # Accrual is keyed by the tier alias (model_tier) and passes only cost_usd.
        usd, tokens, user_id, feature, model_tier = accrue.call_args[0]
        assert model_tier == "lem-simple"
        assert usd == pytest.approx(0.00075)

    def test_down_routed_call_to_metered_provider_changes_cost(self):
        with patch(f"{_MOD}.posthog") as mock_ph:
            from cqc_lem.utilities.observability import track_llm_call
            track_llm_call(
                model="lem-complex",
                serving_model="openai/gpt-4o-mini",
                prompt_tokens=1000,
                completion_tokens=1000,
                latency_ms=100,
            )

        props = mock_ph.capture.call_args[1]["properties"]
        assert props["model_tier"] == "lem-complex"
        assert props["cost_usd"] == pytest.approx(0.00075)

    def test_ollama_serving_model_emits_shadow_cost(self):
        with patch(f"{_MOD}.posthog") as mock_ph, patch(f"{_MOD}._accrue_llm_cost") as accrue:
            from cqc_lem.utilities.observability import track_llm_call
            track_llm_call(
                model="lem-complex",
                serving_model="openai/qwen3.5:397b",
                prompt_tokens=1000,
                completion_tokens=1000,
                latency_ms=100,
            )

        props = mock_ph.capture.call_args[1]["properties"]
        assert props["model"] == "openai/qwen3.5:397b"
        assert props["cost_usd"] == 0.0
        assert props["shadow_cost_usd"] == pytest.approx(0.0125)
        # Shadow cost never enters the durable ledger.
        usd = accrue.call_args[0][0]
        assert usd == 0.0

    def test_cache_hit_zeroes_cost_and_shadow(self):
        with patch(f"{_MOD}.posthog") as mock_ph, patch(f"{_MOD}._accrue_llm_cost") as accrue:
            from cqc_lem.utilities.observability import track_llm_call
            track_llm_call(
                model="lem-complex",
                serving_model="openai/qwen3.5:397b",
                prompt_tokens=1000,
                completion_tokens=1000,
                latency_ms=100,
                cached=True,
            )

        props = mock_ph.capture.call_args[1]["properties"]
        assert props["cost_usd"] == 0.0
        assert "shadow_cost_usd" not in props
        assert accrue.call_args[0][0] == 0.0

    def test_no_serving_model_uses_requested_alias(self):
        with patch(f"{_MOD}.posthog") as mock_ph:
            from cqc_lem.utilities.observability import track_llm_call
            track_llm_call(model="lem-simple", prompt_tokens=1000, completion_tokens=1000,
                           latency_ms=10)

        props = mock_ph.capture.call_args[1]["properties"]
        assert props["model"] == "lem-simple"
        assert props["model_tier"] == "lem-simple"
        assert props["cost_usd"] == pytest.approx(0.00075)


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


class TestTrackCostAlert:
    def test_per_user_alert_is_keyed_to_that_user(self):
        with patch(f"{_MOD}.posthog") as mock_ph:
            from cqc_lem.utilities.observability import track_cost_alert
            track_cost_alert({"check": "user_cost_ceiling", "severity": "warning", "user_id": 7,
                              "value": 0.42}, day="2026-07-24")

        kwargs = mock_ph.capture.call_args[1]
        assert kwargs["event"] == "cost_alert" and kwargs["distinct_id"] == "7"
        assert kwargs["properties"]["date"] == "2026-07-24"
        assert kwargs["properties"]["check"] == "user_cost_ceiling"

    def test_system_alert_falls_back_to_the_system_distinct_id(self):
        with patch(f"{_MOD}.posthog") as mock_ph:
            from cqc_lem.utilities.observability import track_cost_alert
            track_cost_alert({"check": "gross_margin_floor", "severity": "critical"})
        assert mock_ph.capture.call_args[1]["distinct_id"] == "system"


class TestTrackRoutingPolicy:
    def _report(self):
        return {"date": "2026-08-01", "window_days": 28, "observations": 42,
                "policy": {"enabled": True, "buckets": {
                    "content:lem-complex": {"state": "experiment", "to_tier": "lem-medium",
                                            "cohort_pct": 0.1}}},
                "changes": [{"bucket": "content:lem-complex", "action": "rollback",
                             "reason": "engagement fell", "comparison": {"treatment": {"n": 40}}}],
                "recommendations": [{"feature": "comment", "recommendation": "human call"}]}

    def test_captures_the_routing_decision(self):
        with patch(f"{_MOD}.posthog") as mock_ph:
            from cqc_lem.utilities.observability import track_routing_policy
            track_routing_policy(self._report())

        kwargs = mock_ph.capture.call_args[1]
        props = kwargs["properties"]
        assert kwargs["event"] == "routing_policy" and kwargs["distinct_id"] == "system"
        assert props["enabled"] is True and props["observations"] == 42
        assert props["change_count"] == 1 and props["rollback_count"] == 1
        assert props["buckets"][0] == {"bucket": "content:lem-complex", "state": "experiment",
                                       "to_tier": "lem-medium", "cohort_pct": 0.1,
                                       # None until #652's cohort resolution writes one.
                                       "assignment": None}
        # The full arm statistics stay out of the event body — the verdict is what a tile needs.
        assert "comparison" not in props["changes"][0]

    def test_empty_report_does_not_raise(self):
        with patch(f"{_MOD}.posthog") as mock_ph:
            from cqc_lem.utilities.observability import track_routing_policy
            track_routing_policy({})
        assert mock_ph.capture.call_args[1]["properties"]["change_count"] == 0


class TestSessionReplayUrl:
    def test_builds_the_replay_permalink(self):
        with patch.dict("os.environ", {"POSTHOG_PROJECT_ID": "475262",
                                       "POSTHOG_APP_HOST": "https://us.posthog.com/"}):
            from cqc_lem.utilities.observability import session_replay_url
            assert session_replay_url("0198f0aa-1b2c-7000-8000-abcdef012345") == (
                "https://us.posthog.com/project/475262/replay/"
                "0198f0aa-1b2c-7000-8000-abcdef012345")

    def test_defaults_the_app_host(self):
        with patch.dict("os.environ", {"POSTHOG_PROJECT_ID": "475262"}, clear=False):
            import os
            os.environ.pop("POSTHOG_APP_HOST", None)
            from cqc_lem.utilities.observability import session_replay_url
            assert session_replay_url("abcd1234").startswith("https://us.posthog.com/project/")

    @pytest.mark.parametrize("session_id", [None, "", "   ", "short", "has space",
                                            "a)](https://evil.example", "x" * 65])
    def test_a_missing_or_unshaped_session_id_is_not_linked(self, session_id):
        with patch.dict("os.environ", {"POSTHOG_PROJECT_ID": "475262"}):
            from cqc_lem.utilities.observability import session_replay_url
            assert session_replay_url(session_id) is None

    def test_no_project_means_no_link_rather_than_a_guess(self):
        with patch.dict("os.environ", {"POSTHOG_PROJECT_ID": ""}):
            from cqc_lem.utilities.observability import session_replay_url
            assert session_replay_url("0198f0aa-1b2c-7000-8000-abcdef012345") is None


class TestPostHogHogqlQuery:
    def test_returns_none_when_the_read_path_is_not_configured(self):
        with patch.dict("os.environ", {"POSTHOG_PERSONAL_API_KEY": "", "POSTHOG_PROJECT_ID": ""},
                        clear=False):
            from cqc_lem.utilities.observability import posthog_hogql_query
            assert posthog_hogql_query("SELECT 1") is None

    def test_posts_the_query_and_returns_result_rows(self):
        response = MagicMock()
        response.json.return_value = {"results": [["2026-07-24", 3, 10]]}
        with patch.dict("os.environ", {"POSTHOG_PERSONAL_API_KEY": "phx_key",
                                       "POSTHOG_PROJECT_ID": "475262",
                                       "POSTHOG_APP_HOST": "https://us.posthog.com/"}), \
             patch("requests.post", return_value=response) as post:
            from cqc_lem.utilities.observability import posthog_hogql_query
            assert posthog_hogql_query("SELECT 1") == [["2026-07-24", 3, 10]]

        url, kwargs = post.call_args[0][0], post.call_args[1]
        assert url == "https://us.posthog.com/api/projects/475262/query/"
        assert kwargs["headers"]["Authorization"] == "Bearer phx_key"
        assert kwargs["json"]["query"] == {"kind": "HogQLQuery", "query": "SELECT 1"}

    def test_none_on_failure_so_a_caller_never_reads_it_as_zero(self):
        with patch.dict("os.environ", {"POSTHOG_PERSONAL_API_KEY": "phx_key",
                                       "POSTHOG_PROJECT_ID": "475262"}), \
             patch("requests.post", side_effect=RuntimeError("posthog down")):
            from cqc_lem.utilities.observability import posthog_hogql_query
            assert posthog_hogql_query("SELECT 1") is None


class TestTrackOnboarding:
    def test_step_event_carries_the_step_and_time_to_here(self):
        with patch(f"{_MOD}.posthog") as mock_ph:
            from cqc_lem.utilities.observability import track_onboarding_step
            track_onboarding_step(42, "voice_set", hours_since_start=26.5)

        _, kwargs = mock_ph.capture.call_args
        assert kwargs["distinct_id"] == "42"
        assert kwargs["event"] == "onboarding_step"
        assert kwargs["properties"] == {"step": "voice_set", "hours_since_start": 26.5}

    def test_nudge_event_names_the_nudge(self):
        with patch(f"{_MOD}.posthog") as mock_ph:
            from cqc_lem.utilities.observability import track_onboarding_nudge
            track_onboarding_nudge(7, "connect_linkedin")

        _, kwargs = mock_ph.capture.call_args
        assert kwargs["event"] == "onboarding_nudge"
        assert kwargs["properties"]["nudge"] == "connect_linkedin"


class TestTrackCapacityAlert:
    def test_captures_a_system_scoped_capacity_alert(self):
        with patch(f"{_MOD}.posthog") as mock_ph:
            from cqc_lem.utilities.observability import track_capacity_alert
            track_capacity_alert({"check": "session_saturation", "severity": "critical",
                                  "value": 0.8}, generated_at="2026-07-25T12:00:00+00:00")

        mock_ph.capture.assert_called_once()
        _, kwargs = mock_ph.capture.call_args
        assert kwargs["event"] == "capacity_alert"
        # A full browser pool is an infra limit, never one user's problem.
        assert kwargs["distinct_id"] == "system"
        props = kwargs["properties"]
        assert props["check"] == "session_saturation" and props["severity"] == "critical"
        assert props["generated_at"] == "2026-07-25T12:00:00+00:00"

    def test_tolerates_an_empty_alert(self):
        with patch(f"{_MOD}.posthog") as mock_ph:
            from cqc_lem.utilities.observability import track_capacity_alert
            track_capacity_alert(None)

        assert mock_ph.capture.call_args[1]["properties"] == {"generated_at": None}


class TestTrackRateLimitTrip:
    """Issue #650: the 429 breaker's own event, so the Health dashboard and its spike alert have a
    signal — the trip's WARNING log never reaches PostHog at the default POSTHOG_LOG_LEVEL."""

    def test_captures_the_trip_system_scoped(self):
        with patch(f"{_MOD}.posthog") as mock_ph:
            from cqc_lem.utilities.observability import track_rate_limit_trip
            track_rate_limit_trip(1800, 3, "auth wall")

        mock_ph.capture.assert_called_once()
        _, kwargs = mock_ph.capture.call_args
        assert kwargs["event"] == "rate_limit_trip"
        # LinkedIn throttles by egress IP — a trip is account-wide, not one user's.
        assert kwargs["distinct_id"] == "system"
        props = kwargs["properties"]
        assert props["cooldown_seconds"] == 1800 and props["consecutive_trips"] == 3
        assert props["reason"] == "auth wall"

    def test_defaults_the_reason(self):
        with patch(f"{_MOD}.posthog") as mock_ph:
            from cqc_lem.utilities.observability import track_rate_limit_trip
            track_rate_limit_trip(60, 1, "")
        assert mock_ph.capture.call_args[1]["properties"]["reason"] == "429"

    def test_never_raises(self):
        with patch(f"{_MOD}.posthog") as mock_ph:
            mock_ph.capture.side_effect = RuntimeError("posthog down")
            from cqc_lem.utilities.observability import track_rate_limit_trip
            track_rate_limit_trip(60, 1)  # the breaker must still open


class TestTrackCommentOutcome:
    """Issue #628: one event per comment we read back, and one weekly scorecard per user."""

    def test_emits_the_reading(self):
        with patch(f"{_MOD}.posthog") as mock_ph:
            from cqc_lem.utilities.observability import track_comment_outcome
            track_comment_outcome(4, 99, {"status": "checked", "author_replied": True,
                                          "reply_count": 2, "like_count": 5,
                                          "visible_most_relevant": False, "our_reply_sent": 1},
                                  post_key="feedurn://urn:li:activity:1")

        _, kwargs = mock_ph.capture.call_args
        assert kwargs["event"] == "comment_outcome" and kwargs["distinct_id"] == "4"
        props = kwargs["properties"]
        assert props["log_id"] == 99 and props["author_replied"] is True
        assert props["reply_count"] == 2 and props["like_count"] == 5
        assert props["our_reply_sent"] is True
        assert props["visible_most_relevant"] is False
        assert props["post_key"] == "feedurn://urn:li:activity:1"

    def test_ambiguous_visibility_is_not_coerced(self):
        # None must survive as None — a False here would read as a confirmed demotion.
        with patch(f"{_MOD}.posthog") as mock_ph:
            from cqc_lem.utilities.observability import track_comment_outcome
            track_comment_outcome(None, None, {"status": "skipped", "skip_reason": "not-found"})

        _, kwargs = mock_ph.capture.call_args
        assert kwargs["distinct_id"] == "system"
        props = kwargs["properties"]
        assert props["visible_most_relevant"] is None
        assert props["status"] == "skipped" and props["skip_reason"] == "not-found"
        assert props["reply_count"] == 0 and props["author_replied"] is False

    def test_tolerates_an_empty_outcome(self):
        with patch(f"{_MOD}.posthog") as mock_ph:
            from cqc_lem.utilities.observability import track_comment_outcome
            track_comment_outcome(4, 99, None)

        assert mock_ph.capture.call_args[1]["properties"]["status"] is None


class TestTrackGoldenHourReport:
    """Issue #622: one event per reply sweep and per second wave, so the #401 amplifier's silence
    is a query instead of a log grep."""

    def test_emits_the_reading(self):
        with patch(f"{_MOD}.posthog") as mock_ph:
            from cqc_lem.utilities.observability import track_golden_hour_report
            track_golden_hour_report(4, {"phase": "reply_sweep", "post_id": 9, "sweep_slot": 1,
                                         "status": "ok", "latency_minutes": 22.5,
                                         "within_window": True, "window_minutes": 90.0,
                                         "comments_found": 3, "replies_sent": 2})

        _, kwargs = mock_ph.capture.call_args
        assert kwargs["event"] == "golden_hour_report" and kwargs["distinct_id"] == "4"
        props = kwargs["properties"]
        assert props["phase"] == "reply_sweep" and props["post_id"] == 9
        assert props["latency_minutes"] == 22.5 and props["within_window"] is True
        assert props["comments_found"] == 3 and props["replies_sent"] == 2

    def test_unknown_latency_is_not_coerced_to_on_time(self):
        with patch(f"{_MOD}.posthog") as mock_ph:
            from cqc_lem.utilities.observability import track_golden_hour_report
            track_golden_hour_report(None, {"phase": "second_wave", "latency_minutes": None})

        _, kwargs = mock_ph.capture.call_args
        assert kwargs["distinct_id"] == "system"
        props = kwargs["properties"]
        assert props["latency_minutes"] is None and props["within_window"] is False
        assert props["comments_found"] == 0 and props["replies_sent"] == 0

    def test_tolerates_an_empty_report(self):
        with patch(f"{_MOD}.posthog") as mock_ph:
            from cqc_lem.utilities.observability import track_golden_hour_report
            track_golden_hour_report(4, None)

        assert mock_ph.capture.call_args[1]["properties"]["phase"] is None


class TestTrackCommentQuality:
    def test_emits_the_rates_and_the_verdict(self):
        with patch(f"{_MOD}.posthog") as mock_ph:
            from cqc_lem.utilities.observability import track_comment_quality
            track_comment_quality(4, {"days": 7, "sample_size": 20, "checked": 18, "skipped": 2,
                                      "author_reply_rate": 0.2, "reply_rate": 0.4,
                                      "like_rate": 0.6, "demotion_rate": 0.5,
                                      "visibility_sample": 12,
                                      "verdict": {"status": "hold", "reason": "demoted"}})

        _, kwargs = mock_ph.capture.call_args
        assert kwargs["event"] == "comment_quality" and kwargs["distinct_id"] == "4"
        props = kwargs["properties"]
        assert props["demotion_rate"] == 0.5 and props["visibility_sample"] == 12
        assert props["verdict"] == "hold" and props["verdict_reason"] == "demoted"

    def test_tolerates_an_empty_report(self):
        with patch(f"{_MOD}.posthog") as mock_ph:
            from cqc_lem.utilities.observability import track_comment_quality
            track_comment_quality(None, None)

        props = mock_ph.capture.call_args[1]["properties"]
        assert props["verdict"] is None and props["sample_size"] is None


class TestExperimentTelemetry:
    """Exposure + metric labelling for PostHog Experiments (issue #652)."""

    def test_exposure_uses_posthogs_own_feature_flag_event(self):
        with patch(f"{_MOD}.posthog") as mock_ph:
            from cqc_lem.utilities.observability import track_experiment_exposure
            track_experiment_exposure("cost-routing-arm", "treatment", user_id=7, post_id=3)

        _, kwargs = mock_ph.capture.call_args
        # These names are PostHog's, not ours: the experiment engine reads them to decide which
        # variant a person was in. Renaming them silently empties every readout.
        assert kwargs["event"] == "$feature_flag_called"
        assert kwargs["distinct_id"] == "7"
        props = kwargs["properties"]
        assert props["$feature_flag"] == "cost-routing-arm"
        assert props["$feature_flag_response"] == "treatment"
        assert props["$feature/cost-routing-arm"] == "treatment"
        assert props["experiment"] == "cost-routing-arm"
        assert props["post_id"] == 3

    def test_exposure_without_a_user_lands_on_the_system_person(self):
        with patch(f"{_MOD}.posthog") as mock_ph:
            from cqc_lem.utilities.observability import track_experiment_exposure
            track_experiment_exposure("cost-routing-arm", "control")
        assert mock_ph.capture.call_args.kwargs["distinct_id"] == "system"

    def test_post_outcome_carries_the_users_routing_arm(self):
        with patch(f"{_MOD}.posthog") as mock_ph, \
             patch(f"{_MOD}.experiment_props",
                   return_value={"$feature/cost-routing-arm": "treatment"}) as props_fn:
            from cqc_lem.utilities.observability import track_post_outcome
            track_post_outcome(post_id=9, reactions=1, comments=0, user_id=7)

        assert props_fn.call_args.kwargs["keys"] == ("cost-routing-arm",)
        assert mock_ph.capture.call_args.kwargs["properties"]["$feature/cost-routing-arm"] \
            == "treatment"

    def test_post_outcome_reports_the_shipped_media_variant_as_an_arm(self):
        with patch(f"{_MOD}.posthog") as mock_ph, \
             patch(f"{_MOD}.experiment_props", return_value={}) as props_fn:
            from cqc_lem.utilities.observability import track_post_outcome
            track_post_outcome(post_id=9, reactions=1, comments=0, user_id=7,
                               variant_key="flux-dev|gen4_turbo|1:1")

        assert props_fn.call_args.kwargs["shipped"] == {"post-media-variant":
                                                        "flux-dev|gen4_turbo|1:1"}
        props = mock_ph.capture.call_args.kwargs["properties"]
        # The raw key stays readable on the event; the slug is what PostHog groups on.
        assert props["variant_key"] == "flux-dev|gen4_turbo|1:1"

    def test_post_outcome_without_a_variant_reports_none_not_a_label(self):
        with patch(f"{_MOD}.posthog") as mock_ph, patch(f"{_MOD}.experiment_props") as props_fn:
            props_fn.return_value = {}
            from cqc_lem.utilities.observability import track_post_outcome
            track_post_outcome(post_id=9, reactions=1, comments=0, user_id=7)
        assert props_fn.call_args.kwargs["shipped"] is None
        assert mock_ph.capture.call_args.kwargs["properties"]["variant_key"] is None

    def test_comment_outcome_carries_the_prompt_arm(self):
        with patch(f"{_MOD}.posthog") as mock_ph, \
             patch(f"{_MOD}.experiment_props",
                   return_value={"$feature/comment-contract-prompt": "author-question"}) as props_fn:
            from cqc_lem.utilities.observability import track_comment_outcome
            track_comment_outcome(7, 11, {"status": "checked", "author_replied": True})

        assert props_fn.call_args.kwargs["keys"] == ("comment-contract-prompt",)
        props = mock_ph.capture.call_args.kwargs["properties"]
        assert props["$feature/comment-contract-prompt"] == "author-question"
        assert props["author_replied"] is True

    def test_experiment_props_never_breaks_the_outcome_event(self):
        """An experiment plane that is down must cost us the label, not the measurement."""
        from cqc_lem.utilities.observability import experiment_props
        with patch("cqc_lem.utilities.experiments.experiment_properties",
                   side_effect=RuntimeError("boom")):
            assert experiment_props(7, keys=("cost-routing-arm",)) == {}

    def test_routing_policy_event_reports_which_side_cohorted(self):
        with patch(f"{_MOD}.posthog") as mock_ph:
            from cqc_lem.utilities.observability import track_routing_policy
            track_routing_policy({
                "date": "2026-08-01",
                "policy": {"enabled": True, "buckets": {
                    "content:lem-complex": {"state": "experiment", "to_tier": "lem-medium",
                                            "cohort_pct": 0.1, "assignment": "flag"}}},
                "cohort": {"assignment": "flag", "enrolled": 2, "treatment": 1,
                           "treatment_share": 0.5},
            })

        props = mock_ph.capture.call_args.kwargs["properties"]
        assert props["cohort_assignment"] == "flag"
        assert props["cohort_enrolled"] == 2
        assert props["cohort_treatment_share"] == 0.5
        assert props["buckets"][0]["assignment"] == "flag"


class TestTrackCatchupRun:
    def test_emits_the_catchup_run_event_with_the_funnel(self):
        with patch(f"{_MOD}.posthog") as mock_ph:
            from cqc_lem.utilities.observability import track_catchup_run
            track_catchup_run(7, {"phase": "scan", "status": "none_qualified", "moments": 12,
                                  "classified": 0, "message_source": "linkedin"})

        kwargs = mock_ph.capture.call_args.kwargs
        assert kwargs["event"] == "catchup_run"
        assert kwargs["distinct_id"] == "7"
        props = kwargs["properties"]
        assert props["phase"] == "scan"
        assert props["status"] == "none_qualified"
        assert props["moments"] == 12
        assert props["classified"] == 0
        assert props["message_source"] == "linkedin"

    def test_a_fleet_wide_beat_report_is_attributed_to_system(self):
        """The dispatchers report for the whole fleet, not a user — the counts still have to land."""
        with patch(f"{_MOD}.posthog") as mock_ph:
            from cqc_lem.utilities.observability import track_catchup_run
            track_catchup_run(None, {"phase": "send", "status": "capped", "capped": 3})

        kwargs = mock_ph.capture.call_args.kwargs
        assert kwargs["distinct_id"] == "system"
        assert kwargs["properties"]["capped"] == 3
        assert kwargs["properties"]["dispatched"] == 0  # absent counts read 0, never missing

    def test_an_empty_report_still_emits(self):
        with patch(f"{_MOD}.posthog") as mock_ph:
            from cqc_lem.utilities.observability import track_catchup_run
            track_catchup_run(7)
        mock_ph.capture.assert_called_once()
        assert mock_ph.capture.call_args.kwargs["properties"]["status"] is None
