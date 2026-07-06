"""Unit tests for the DURABLE profile synthesis: the generator that distills a profile into a stable
voice brief (excluding volatile recent-activity/project noise), the lazy get-or-create cache helper,
and the generation functions using the synthesis in place of the full profile JSON."""

import pytest
from unittest.mock import MagicMock, patch

pytestmark = pytest.mark.unit

_AI = "cqc_lem.utilities.ai.ai_helper"
_DB = "cqc_lem.utilities.db"


def _resp(text):
    r = MagicMock()
    r.choices = [MagicMock(message=MagicMock(content=text))]
    return r


def _mock_profile():
    """MagicMock profile whose full-JSON dump is a distinctive sentinel so tests can assert the raw
    JSON is NOT smuggled into a prompt when a synthesis is supplied."""
    p = MagicMock()
    p.model_dump_json.return_value = '{"RAW_PROFILE_JSON_SENTINEL": true}'
    return p


class TestSynthesizeProfile:
    def test_excludes_recent_activity_project_noise(self):
        from cqc_lem.utilities.ai import ai_helper
        from cqc_lem.utilities.linkedin.profile import LinkedInProfile, LinkedInActivity
        profile = LinkedInProfile(
            full_name="Jane Doe", job_title="COO", company_name="Acme Logistics",
            recent_activities=[LinkedInActivity(text="Currently building SECRETPROJECTX right now")])
        with patch(f"{_AI}.get_industries_of_profile_from_ai", return_value="Logistics"), \
             patch(f"{_AI}._call_llm", return_value=_resp("- COO in logistics\n- plain-spoken")) as m:
            out = ai_helper.synthesize_profile(profile)
        assert "COO in logistics" in out
        kwargs = m.call_args.kwargs
        assert kwargs["model"] == "lem-medium"          # balanced tier
        system = kwargs["messages"][0]["content"]
        user = kwargs["messages"][1]["content"]
        # Volatile "currently building" project noise is stripped from the synthesis INPUT entirely.
        assert "SECRETPROJECTX" not in user and "SECRETPROJECTX" not in system
        # And the prompt hard-excludes such content + carries the anti-self-promo guardrail.
        assert "currently building" in system.lower()
        assert ai_helper._NO_SELF_PROMO_GUARDRAIL in system
        # Durable industry enrichment is present.
        assert "Logistics" in user

    def test_returns_empty_on_none_content(self):
        from cqc_lem.utilities.ai import ai_helper
        from cqc_lem.utilities.linkedin.profile import LinkedInProfile
        profile = LinkedInProfile(full_name="Jane Doe")
        with patch(f"{_AI}.get_industries_of_profile_from_ai", return_value=""), \
             patch(f"{_AI}._call_llm", return_value=_resp(None)):
            assert ai_helper.synthesize_profile(profile) == ""


class TestGetOrCreateProfileSynthesis:
    def test_returns_fresh_cache_without_regenerating(self):
        from datetime import datetime
        from cqc_lem.utilities.ai import ai_helper
        with patch(f"{_DB}.get_profile_synthesis", return_value=("cached brief", datetime.now())), \
             patch(f"{_AI}.synthesize_profile") as syn:
            out = ai_helper.get_or_create_profile_synthesis(1, _mock_profile())
        assert out == "cached brief"
        syn.assert_not_called()

    def test_regenerates_when_stale(self):
        from datetime import datetime, timedelta
        from cqc_lem.utilities.ai import ai_helper
        old = datetime.now() - timedelta(days=30)
        with patch(f"{_DB}.get_profile_synthesis", return_value=("old brief", old)), \
             patch(f"{_AI}.synthesize_profile", return_value="new brief") as syn, \
             patch(f"{_DB}.set_profile_synthesis", return_value=True) as setter:
            out = ai_helper.get_or_create_profile_synthesis(1, _mock_profile())
        assert out == "new brief"
        syn.assert_called_once()
        setter.assert_called_once_with(1, "new brief")

    def test_regenerates_when_missing(self):
        from cqc_lem.utilities.ai import ai_helper
        with patch(f"{_DB}.get_profile_synthesis", return_value=None), \
             patch(f"{_AI}.synthesize_profile", return_value="fresh brief") as syn, \
             patch(f"{_DB}.set_profile_synthesis", return_value=True):
            out = ai_helper.get_or_create_profile_synthesis(2, _mock_profile())
        assert out == "fresh brief"
        syn.assert_called_once()

    def test_falls_back_to_cached_when_synthesis_fails(self):
        from datetime import datetime, timedelta
        from cqc_lem.utilities.ai import ai_helper
        old = datetime.now() - timedelta(days=30)
        with patch(f"{_DB}.get_profile_synthesis", return_value=("old brief", old)), \
             patch(f"{_AI}.synthesize_profile", side_effect=RuntimeError("boom")), \
             patch(f"{_DB}.set_profile_synthesis") as setter:
            out = ai_helper.get_or_create_profile_synthesis(1, _mock_profile())
        assert out == "old brief"
        setter.assert_not_called()

    def test_none_when_no_profile_and_no_cache(self):
        from cqc_lem.utilities.ai import ai_helper
        with patch(f"{_DB}.get_profile_synthesis", return_value=None), \
             patch("cqc_lem.utilities.linkedin.helper.load_profile_for_user", return_value=None), \
             patch(f"{_AI}.synthesize_profile") as syn:
            out = ai_helper.get_or_create_profile_synthesis(9, None)
        assert out is None
        syn.assert_not_called()


class TestGenerationUsesSynthesisNotRawJson:
    def test_comment_uses_synthesis_and_keeps_guardrail(self):
        from cqc_lem.utilities.ai import ai_helper
        with patch(f"{_AI}._call_llm", return_value=_resp("What changed your mind?")) as m:
            ai_helper.generate_ai_response("A POST ABOUT ROBOTS", _mock_profile(),
                                           profile_synthesis="STABLE VOICE BRIEF")
        system = m.call_args.kwargs["messages"][0]["content"]
        user_text = m.call_args.kwargs["messages"][1]["content"][0]["text"]
        assert "STABLE VOICE BRIEF" in user_text
        assert "RAW_PROFILE_JSON_SENTINEL" not in user_text     # raw JSON no longer inlined
        assert ai_helper._NO_SELF_PROMO_GUARDRAIL in system      # guardrail intact

    def test_comment_falls_back_to_full_json_without_synthesis(self):
        from cqc_lem.utilities.ai import ai_helper
        with patch(f"{_AI}._call_llm", return_value=_resp("Hmm?")) as m:
            ai_helper.generate_ai_response("A POST", _mock_profile())  # no synthesis supplied
        user_text = m.call_args.kwargs["messages"][1]["content"][0]["text"]
        # Lazy fallback keeps working before the first weekly refresh: full JSON still used.
        assert "RAW_PROFILE_JSON_SENTINEL" in user_text

    def test_seed_comment_uses_synthesis(self):
        from cqc_lem.utilities.ai import ai_helper
        with patch(f"{_AI}._call_llm", return_value=_resp("Backstory — thoughts?")) as m:
            ai_helper.generate_seed_comment("my post", _mock_profile(),
                                            profile_synthesis="STABLE VOICE BRIEF")
        user_text = m.call_args.kwargs["messages"][1]["content"]
        assert "STABLE VOICE BRIEF" in user_text and "RAW_PROFILE_JSON_SENTINEL" not in user_text

    def test_thread_reply_uses_synthesis(self):
        from cqc_lem.utilities.ai import ai_helper
        with patch(f"{_AI}._call_llm", return_value=_resp("How so?")) as m:
            ai_helper.generate_thread_reply("my post", "their comment", _mock_profile(),
                                            profile_synthesis="STABLE VOICE BRIEF")
        user_text = m.call_args.kwargs["messages"][1]["content"]
        assert "STABLE VOICE BRIEF" in user_text and "RAW_PROFILE_JSON_SENTINEL" not in user_text

    def test_thought_leadership_post_uses_synthesis(self):
        from cqc_lem.utilities.ai import ai_helper
        trends = {"industry": "Logistics", "analysis": "robots everywhere"}
        with patch(f"{_AI}.get_industry_trend_analysis_based_on_user_profile", return_value=trends), \
             patch(f"{_AI}._call_llm", return_value=_resp("A viral post")) as m:
            ai_helper.get_thought_leadership_post_from_ai(_mock_profile(), "awareness",
                                                          profile_synthesis="STABLE VOICE BRIEF")
        user_text = m.call_args.kwargs["messages"][1]["content"][0]["text"]
        assert "STABLE VOICE BRIEF" in user_text
        assert "RAW_PROFILE_JSON_SENTINEL" not in user_text
        assert "Content alignment rules" in user_text           # anti-self-promo alignment intact
