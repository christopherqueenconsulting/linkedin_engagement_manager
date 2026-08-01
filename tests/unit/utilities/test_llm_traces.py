"""Pipeline traces — `$ai_trace` / `$ai_span` (issue #746).

A post costs six model calls, not one. These pin the property that makes the end-to-end cost
question answerable at all: every call a pipeline makes shares ONE trace id, and two pipelines never
share one. Everything else here guards the fail-soft posture — a trace is telemetry, so no failure
inside it may change what the generation chain returns.
"""
import pytest
from unittest.mock import patch

pytestmark = pytest.mark.unit

_MOD = "cqc_lem.utilities.observability"


def _events(mock_ph):
    """[(event_name, properties)] in capture order."""
    return [(kw["event"], kw["properties"]) for _, kw in mock_ph.capture.call_args_list]


class TestTraceIdentity:
    def test_every_step_of_one_pipeline_shares_the_trace_id(self):
        from cqc_lem.utilities.observability import current_llm_trace, llm_span, llm_trace
        seen = []
        with llm_trace("post_generation", user_id=7, feature="content"):
            for step in ("research", "draft", "humanize"):
                with llm_span(step):
                    seen.append(current_llm_trace()[0])
        assert len(set(seen)) == 1 and seen[0]

    def test_two_pipelines_get_different_trace_ids(self):
        from cqc_lem.utilities.observability import current_llm_trace, llm_trace
        with llm_trace("post_generation", user_id=7) as first:
            assert current_llm_trace()[0] == first
        with llm_trace("post_generation", user_id=7) as second:
            assert current_llm_trace()[0] == second
        assert first and second and first != second

    def test_the_scope_is_cleared_on_the_way_out(self):
        from cqc_lem.utilities.observability import current_llm_trace, llm_trace
        with llm_trace("post_generation", user_id=7):
            pass
        assert current_llm_trace() == (None, None)

    def test_a_nested_trace_is_a_span_not_a_second_trace(self):
        """create_text_post recurses into itself for post-type fallbacks. Two half-traces of one
        post answer nobody's question, so the inner one joins the outer."""
        from cqc_lem.utilities.observability import llm_trace
        with patch(f"{_MOD}.posthog") as mock_ph:
            with llm_trace("post_generation", user_id=7) as outer:
                with llm_trace("post_generation", user_id=7) as inner:
                    assert inner == outer
        names = [name for name, _ in _events(mock_ph)]
        assert names == ["$ai_span", "$ai_trace"]

    def test_a_span_outside_a_pipeline_is_a_no_op(self):
        """Every shared-core step carries @llm_step, and most calls into them are not a pipeline."""
        from cqc_lem.utilities.observability import llm_span
        with patch(f"{_MOD}.posthog") as mock_ph:
            with llm_span("humanize") as span_id:
                assert span_id is None
        mock_ph.capture.assert_not_called()

    def test_the_kill_switch_mints_no_trace_at_all(self):
        from cqc_lem.utilities.observability import current_llm_trace, llm_span, llm_trace
        with patch.dict("os.environ", {"LLM_TRACING_ENABLED": "false"}):
            with patch(f"{_MOD}.posthog") as mock_ph:
                with llm_trace("post_generation", user_id=7) as trace_id:
                    assert trace_id is None
                    assert current_llm_trace() == (None, None)
                    with llm_span("draft"):
                        pass
        mock_ph.capture.assert_not_called()

    def test_the_kill_switch_still_attributes_cost(self):
        """Tracing off must not also turn off the attribution scope llm_trace opens."""
        from cqc_lem.utilities.observability import current_llm_attribution, llm_trace
        with patch.dict("os.environ", {"LLM_TRACING_ENABLED": "false"}):
            with llm_trace("post_generation", user_id=7, feature="content"):
                assert current_llm_attribution() == (7, "content")


class TestEmittedEvents:
    def test_the_trace_event_names_the_pipeline_and_carries_its_root_span(self):
        from cqc_lem.utilities.observability import llm_trace
        with patch(f"{_MOD}.posthog") as mock_ph:
            with llm_trace("post_generation", user_id=7, feature="content") as trace_id:
                pass
        (event, props), = _events(mock_ph)
        assert event == "$ai_trace"
        assert props["$ai_trace_id"] == trace_id
        assert props["$ai_span_name"] == "post_generation"
        assert props["$ai_span_id"] == trace_id
        assert "$ai_parent_id" not in props
        assert props["$ai_latency"] >= 0
        assert props["feature"] == "content"
        assert mock_ph.capture.call_args_list[0][1]["distinct_id"] == "7"

    def test_a_top_level_span_parents_onto_the_trace_id_itself(self):
        """PostHog collects a trace's children with `$ai_parent_id = $ai_trace_id` (see
        traces_query_runner.py). Parent a step onto a separate root-span uuid instead and the trace
        opens empty — every generation still carries the right trace id, and none of them show."""
        from cqc_lem.utilities.observability import llm_trace, llm_span
        with patch(f"{_MOD}.posthog") as mock_ph:
            with llm_trace("post_generation", user_id=7, feature="content") as trace_id:
                with llm_span("draft"):
                    pass
        (span_event, span), (trace_event, trace) = _events(mock_ph)
        assert (span_event, trace_event) == ("$ai_span", "$ai_trace")
        assert span["$ai_trace_id"] == trace["$ai_trace_id"] == trace_id
        assert span["$ai_parent_id"] == trace_id
        assert span["$ai_span_name"] == "draft"

    def test_nested_spans_parent_onto_each_other(self):
        from cqc_lem.utilities.observability import llm_trace, llm_span
        with patch(f"{_MOD}.posthog") as mock_ph:
            with llm_trace("post_generation", user_id=7):
                with llm_span("draft"):
                    with llm_span("humanize"):
                        pass
        (_, inner), (_, outer), _ = _events(mock_ph)
        assert inner["$ai_span_name"] == "humanize"
        assert inner["$ai_parent_id"] == outer["$ai_span_id"]

    def test_extra_span_properties_ride_along(self):
        from cqc_lem.utilities.observability import llm_trace, llm_span
        with patch(f"{_MOD}.posthog") as mock_ph:
            with llm_trace("post_generation", user_id=7):
                with llm_span("draft", post_type="personal_story"):
                    pass
        (_, span), _ = _events(mock_ph)
        assert span["post_type"] == "personal_story"

    def test_an_unattributed_pipeline_lands_on_the_system_sentinel(self):
        from cqc_lem.utilities.observability import llm_trace
        with patch(f"{_MOD}._current_task_context", return_value=(None, None)):
            with patch(f"{_MOD}.posthog") as mock_ph:
                with llm_trace("post_generation"):
                    pass
        _, kwargs = mock_ph.capture.call_args
        assert kwargs["distinct_id"] == "system"
        assert kwargs["properties"]["feature"] == "system"

    def test_a_failed_pipeline_still_reports_and_re_raises(self):
        from cqc_lem.utilities.observability import llm_trace, llm_span
        with patch(f"{_MOD}.posthog") as mock_ph:
            with pytest.raises(ValueError):
                with llm_trace("post_generation", user_id=7):
                    with llm_span("draft"):
                        raise ValueError("model said no")
        (_, span), (_, trace) = _events(mock_ph)
        assert span["$ai_is_error"] is True
        assert span["$ai_error"] == "ValueError: model said no"
        assert trace["$ai_is_error"] is True


class TestFailSoft:
    def test_a_capture_failure_never_reaches_the_caller(self):
        from cqc_lem.utilities.observability import llm_trace
        with patch(f"{_MOD}.posthog") as mock_ph:
            mock_ph.capture.side_effect = RuntimeError("posthog down")
            with llm_trace("post_generation", user_id=7):
                result = "the post"
        assert result == "the post"

    def test_the_scope_is_cleared_even_when_the_body_raises(self):
        from cqc_lem.utilities.observability import current_llm_trace, llm_trace
        with pytest.raises(ValueError):
            with llm_trace("post_generation", user_id=7):
                raise ValueError("boom")
        assert current_llm_trace() == (None, None)


class TestDecorators:
    def test_llm_pipeline_opens_a_trace_and_attributes_it(self):
        from cqc_lem.utilities.observability import (current_llm_attribution, current_llm_trace,
                                                     llm_pipeline)

        @llm_pipeline("post_generation", feature="content")
        def generate(user_id: int, stage: str):
            return current_llm_trace()[0], current_llm_attribution()

        trace_id, attribution = generate(7, "awareness")
        assert trace_id and attribution == (7, "content")

    def test_llm_pipeline_reads_user_id_by_keyword_too(self):
        from cqc_lem.utilities.observability import current_llm_attribution, llm_pipeline

        @llm_pipeline("comment_generation", feature="comment")
        def respond(post_content, user_id: int = None):
            return current_llm_attribution()

        assert respond("x", user_id=9) == (9, "comment")

    def test_llm_step_opens_a_span_under_the_running_pipeline(self):
        from cqc_lem.utilities.observability import llm_pipeline, llm_step

        @llm_step("humanize")
        def humanize(text):
            return text.upper()

        @llm_pipeline("post_generation", feature="content")
        def generate(user_id: int):
            return humanize("draft")

        with patch(f"{_MOD}.posthog") as mock_ph:
            assert generate(7) == "DRAFT"
        (span_event, span), (trace_event, trace) = _events(mock_ph)
        assert (span_event, trace_event) == ("$ai_span", "$ai_trace")
        assert span["$ai_span_name"] == "humanize"
        assert span["$ai_trace_id"] == trace["$ai_trace_id"]

    def test_llm_step_outside_a_pipeline_just_runs_the_function(self):
        from cqc_lem.utilities.observability import llm_step

        @llm_step("humanize")
        def humanize(text):
            return text.upper()

        with patch(f"{_MOD}.posthog") as mock_ph:
            assert humanize("draft") == "DRAFT"
        mock_ph.capture.assert_not_called()


class TestTheDecoratedPipelines:
    """The decorators are the whole mechanism: strip one and its chain silently splinters back into
    isolated generations with nothing in PostHog to say a step went missing."""

    @pytest.mark.parametrize("module,name,pipeline", [
        ("cqc_lem.app.run_content_plan", "create_text_post", "post_generation"),
        ("cqc_lem.utilities.ai.ai_helper", "generate_newsletter_edition", "newsletter_edition"),
        ("cqc_lem.utilities.ai.ai_helper", "generate_ai_response", "comment_generation"),
    ])
    def test_pipeline_roots_open_a_trace(self, module, name, pipeline):
        import importlib
        fn = getattr(importlib.import_module(module), name)
        assert getattr(fn, "__llm_trace_name__", None) == pipeline

    @pytest.mark.parametrize("module,name,step", [
        ("cqc_lem.utilities.ai.content_research", "research_topic", "research"),
        ("cqc_lem.utilities.ai.ai_helper", "get_thought_leadership_post_from_ai", "draft"),
        ("cqc_lem.utilities.ai.ai_helper", "get_industry_news_post_from_ai", "draft"),
        ("cqc_lem.utilities.ai.ai_helper", "get_personal_story_post_from_ai", "draft"),
        ("cqc_lem.utilities.ai.ai_helper", "generate_engagement_prompt_post", "draft"),
        ("cqc_lem.utilities.ai.ai_helper", "get_blog_summary_post_from_ai", "draft"),
        ("cqc_lem.app.run_content_plan", "generate_website_content_post", "draft"),
        ("cqc_lem.utilities.ai.ai_helper", "get_ai_linked_post_refinement", "refine"),
        ("cqc_lem.utilities.ai.ai_helper", "optimize_post_hook", "hook"),
        ("cqc_lem.utilities.ai.content_alignment", "humanize_text", "humanize"),
        ("cqc_lem.utilities.ai.content_alignment", "score_authenticity", "authenticity"),
        ("cqc_lem.app.run_content_plan", "_review_generated_post", "review"),
    ])
    def test_shared_core_steps_open_a_span(self, module, name, step):
        import importlib
        fn = getattr(importlib.import_module(module), name)
        assert getattr(fn, "__llm_span_name__", None) == step
