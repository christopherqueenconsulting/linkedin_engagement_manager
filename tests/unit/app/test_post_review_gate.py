"""Unit tests for the post review gate in create_text_post: pre-generation avoid steering built
from the user's recent posts, the deterministic similarity gate with its single avoid-directive
retry, and the cheap focus-alignment check — none of which may ever hard-block the pipeline."""

import pytest
from unittest.mock import MagicMock, patch

pytestmark = pytest.mark.unit

_RCP = "cqc_lem.app.run_content_plan"

_DISABLED_LM = {"enabled": False, "keyword": None, "message": None}

_RECENT = ("Why routing LLM calls by complexity cuts your cost per successful call.\n\n"
           "Most teams overpay for AI because every request hits the frontier model, even the "
           "trivial ones. Route the simple calls to cheap models and reserve the expensive ones "
           "for genuinely hard prompts.")

# Rewording of _RECENT — must trip the similarity gate.
_NEAR_DUP = ("Most teams overpay for AI: every request hits the frontier model, even trivial "
             "ones. Routing LLM calls by complexity cuts the cost per successful call — cheap "
             "models for simple calls, expensive models reserved for the genuinely hard prompts.")

_FRESH = ("AI reliability starts with observability.\n\nWe traced a silent failure spike in "
          "production to one retry loop nobody owned. Dashboards did not catch it; distributed "
          "tracing did. Instrument before you scale.")


def _run(outputs, recent=None, prefs=None, post_id=77, log=None):
    """Drive create_text_post with a generator that returns `outputs` in order."""
    from cqc_lem.app import run_content_plan as rcp
    gen = MagicMock(side_effect=list(outputs))
    patches = [
        patch(f"{_RCP}.get_engagement_preferences", return_value=prefs or {}),
        patch(f"{_RCP}.get_or_create_profile_synthesis", return_value="voice"),
        patch(f"{_RCP}.get_lead_magnet_settings", return_value=_DISABLED_LM),
        patch(f"{_RCP}.get_recent_post_texts", return_value=list(recent or [])),
        patch(f"{_RCP}.get_recent_post_shape_history", return_value=[]),
        patch(f"{_RCP}.update_db_post_shape"),
        patch(f"{_RCP}.get_thought_leadership_post_from_ai", gen),
        # Identity refinement passes so the gate sees the generator output verbatim.
        patch(f"{_RCP}.get_ai_linked_post_refinement", side_effect=lambda c: c),
        patch(f"{_RCP}.optimize_post_hook", side_effect=lambda c: c),
        patch(f"{_RCP}.sanitize_for_linkedin", side_effect=lambda c: c),
        patch(f"{_RCP}.strip_engagement_bait", side_effect=lambda c: c),
    ]
    if log is not None:
        patches.append(patch(f"{_RCP}.log_warning", log))
    for p in patches:
        p.start()
    try:
        out = rcp.create_text_post(1, "awareness", post_type="thought_leadership",
                                   user_profile=MagicMock(), post_id=post_id)
    finally:
        for p in patches:
            p.stop()
    return out, gen


class TestSteering:
    def test_recent_openers_and_subjects_fed_into_prompt(self):
        out, gen = _run([_FRESH], recent=[_RECENT])
        assert out == _FRESH
        directive = gen.call_args.kwargs["history_directive"]
        assert "UNIQUENESS RULES" in directive
        assert "Why routing LLM calls by complexity cuts your cost per successful call." in directive

    def test_no_history_no_directive(self):
        _, gen = _run([_FRESH], recent=[])
        assert gen.call_args.kwargs["history_directive"] == ""

    def test_history_fetch_failure_never_blocks_generation(self):
        from cqc_lem.app import run_content_plan as rcp
        with patch(f"{_RCP}.get_engagement_preferences", return_value={}), \
             patch(f"{_RCP}.get_or_create_profile_synthesis", return_value="voice"), \
             patch(f"{_RCP}.get_lead_magnet_settings", return_value=_DISABLED_LM), \
             patch(f"{_RCP}.get_recent_post_texts", side_effect=RuntimeError("db down")), \
             patch(f"{_RCP}.get_recent_post_shape_history", return_value=[]), \
             patch(f"{_RCP}.update_db_post_shape"), \
             patch(f"{_RCP}.get_thought_leadership_post_from_ai", return_value=_FRESH), \
             patch(f"{_RCP}.get_ai_linked_post_refinement", side_effect=lambda c: c), \
             patch(f"{_RCP}.optimize_post_hook", side_effect=lambda c: c), \
             patch(f"{_RCP}.sanitize_for_linkedin", side_effect=lambda c: c), \
             patch(f"{_RCP}.strip_engagement_bait", side_effect=lambda c: c):
            out = rcp.create_text_post(1, "awareness", post_type="thought_leadership",
                                       user_profile=MagicMock(), post_id=5)
        assert out == _FRESH


class TestSimilarityGate:
    def test_sufficiently_different_post_passes_untouched(self):
        out, gen = _run([_FRESH], recent=[_RECENT])
        assert out == _FRESH
        assert gen.call_count == 1

    def test_near_duplicate_triggers_one_retry_with_avoid_directive(self):
        out, gen = _run([_NEAR_DUP, _FRESH], recent=[_RECENT])
        assert out == _FRESH
        assert gen.call_count == 2
        retry_directive = gen.call_args_list[1].kwargs["history_directive"]
        assert "TOO SIMILAR" in retry_directive
        assert _RECENT[:80] in retry_directive  # the offending post is named explicitly

    def test_second_attempt_still_similar_keeps_it_and_warns(self):
        log = MagicMock()
        out, gen = _run([_NEAR_DUP, _NEAR_DUP], recent=[_RECENT], log=log)
        assert out == _NEAR_DUP  # never loops, never blocks
        assert gen.call_count == 2
        assert any("still similar" in str(c.args[0]) for c in log.call_args_list)

    def test_retry_failure_keeps_first_draft(self):
        log = MagicMock()
        out, gen = _run([_NEAR_DUP, RuntimeError("llm down")], recent=[_RECENT], log=log)
        assert out == _NEAR_DUP
        assert gen.call_count == 2

    def test_threshold_env_override_allows_similar_content(self, monkeypatch):
        monkeypatch.setenv("POST_SIMILARITY_MAX", "0.99")
        out, gen = _run([_NEAR_DUP], recent=[_RECENT])
        assert out == _NEAR_DUP
        assert gen.call_count == 1  # ceiling raised → no retry

    def test_no_recent_posts_no_gate(self):
        out, gen = _run([_NEAR_DUP], recent=[])
        assert out == _NEAR_DUP
        assert gen.call_count == 1


class TestAlignmentCheck:
    _PREFS = {"focus_topics": ["LLM/AI cost efficiency", "AI reliability & observability"]}

    def test_on_focus_post_passes_silently(self):
        log = MagicMock()
        out, _ = _run([_FRESH], recent=[], prefs=self._PREFS, log=log)
        assert out == _FRESH
        assert not any("focus topic" in str(c.args[0]) for c in log.call_args_list)

    def test_off_focus_post_logs_warning_but_ships(self):
        log = MagicMock()
        off_focus = ("I planted tomatoes with my kids last weekend and the garden taught me "
                     "patience.\n\nSeedlings grow slowly; compounding beats intensity.")
        out, _ = _run([off_focus], recent=[], prefs=self._PREFS, log=log)
        assert out == off_focus  # never blocks
        assert any("focus topic" in str(c.args[0]) for c in log.call_args_list)

    def test_empty_focus_topics_is_noop(self):
        log = MagicMock()
        out, _ = _run(["Anything at all about any subject whatsoever."],
                      recent=[], prefs={"focus_topics": []}, log=log)
        assert not any("focus topic" in str(c.args[0]) for c in log.call_args_list)

    def test_llm_check_off_by_default(self):
        off_focus = "I planted tomatoes with my kids last weekend in the garden."
        with patch("cqc_lem.utilities.ai.ai_helper.post_is_relevant") as llm_check:
            _run([off_focus], recent=[], prefs=self._PREFS, log=MagicMock())
        llm_check.assert_not_called()

    def test_llm_check_rescues_when_enabled(self, monkeypatch):
        monkeypatch.setenv("POST_ALIGNMENT_LLM_CHECK_ENABLED", "true")
        log = MagicMock()
        off_focus = "I planted tomatoes with my kids last weekend in the garden."
        with patch("cqc_lem.utilities.ai.ai_helper.post_is_relevant", return_value=True) as llm_check:
            _run([off_focus], recent=[], prefs=self._PREFS, log=log)
        llm_check.assert_called_once()
        assert not any("focus topic" in str(c.args[0]) for c in log.call_args_list)
