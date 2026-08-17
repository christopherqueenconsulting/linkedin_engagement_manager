"""Unit tests for DM reply-intent classification (issue #485) — the branch that decides whether a
lead's reply becomes a call proposal, an answered objection, a light touch, or a hard stop.
"""

from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit

_MOD = "cqc_lem.utilities.ai.dm_nurture"


def _resp(text):
    r = MagicMock(); r.choices = [MagicMock(message=MagicMock(content=text))]
    return r


def _classify(text, **kw):
    from cqc_lem.utilities.ai.dm_nurture import classify_reply_intent
    return classify_reply_intent(text, **kw)


class TestHeuristicIntents:
    @pytest.mark.parametrize("text", [
        "Sounds good — let's talk next week",
        "I'm interested, tell me more about how this works",
        "How much would something like this cost?",
        "Yes please, send me the details",
        "When are you free for a quick call?",
    ])
    def test_interested(self, text):
        got = _classify(text, use_llm=False)
        assert got["intent"] == "interested"
        assert got["method"] == "heuristic"

    @pytest.mark.parametrize("text", [
        "Honestly that's too expensive for us right now in terms of budget",
        "We already work with an agency on this",
        "How is this different from what everyone else offers?",
        "Not sure this would work for a team our size",
    ])
    def test_objection(self, text):
        assert _classify(text, use_llm=False)["intent"] == "objection"

    @pytest.mark.parametrize("text", [
        "Not right now, but keep me posted",
        "Let's circle back next quarter",
        "We're too busy with the launch at the moment",
    ])
    def test_not_now(self, text):
        assert _classify(text, use_llm=False)["intent"] == "not_now"

    @pytest.mark.parametrize("text", [
        "Not interested, thanks",
        "Please stop messaging me",
        "Remove me from your list",
        "This isn't a good fit for us",
    ])
    def test_disinterest(self, text):
        from cqc_lem.utilities.ai.dm_nurture import is_stop_intent
        got = _classify(text, use_llm=False)
        assert got["intent"] == "disinterest"
        assert is_stop_intent(got["intent"]) is True

    def test_disinterest_wins_over_not_now(self):
        # "not interested right now" matches BOTH families; the only safe reading is stop.
        assert _classify("Not interested right now", use_llm=False)["intent"] == "disinterest"

    def test_matched_evidence_is_the_phrase_that_fired(self):
        got = _classify("Please stop messaging me about this", use_llm=False)
        assert got["matched"] == ["stop messaging"]


class TestNeutralAndShortText:
    def test_too_short_is_neutral_without_any_llm_call(self):
        with patch(f"{_MOD}._llm_intent") as llm:
            got = _classify("ok")
        assert got == {"intent": "neutral", "matched": [], "method": "none"}
        llm.assert_not_called()

    def test_empty_is_neutral(self):
        assert _classify(None)["intent"] == "neutral"

    def test_llm_disabled_falls_back_to_neutral(self):
        got = _classify("Appreciate you reaching out about the report", use_llm=False)
        assert got == {"intent": "neutral", "matched": [], "method": "heuristic"}


class TestLlmTier:
    def test_llm_classifies_ambiguous_text(self):
        with patch("cqc_lem.utilities.ai.ai_helper._call_llm", return_value=_resp("Objection")):
            got = _classify("We looked at something similar last year and it went sideways")
        assert got["intent"] == "objection"
        assert got["method"] == "llm"

    def test_llm_answer_normalizes_hyphens_and_punctuation(self):
        with patch("cqc_lem.utilities.ai.ai_helper._call_llm", return_value=_resp(" not-now. ")):
            assert _classify("Interesting timing on this one honestly")["intent"] == "not_now"

    def test_unrecognized_llm_answer_is_neutral(self):
        with patch("cqc_lem.utilities.ai.ai_helper._call_llm", return_value=_resp("maybe?")):
            assert _classify("Interesting timing on this one honestly")["intent"] == "neutral"

    def test_llm_failure_never_invents_disinterest(self):
        with patch("cqc_lem.utilities.ai.ai_helper._call_llm", side_effect=RuntimeError("llm down")):
            got = _classify("Interesting timing on this one honestly")
        assert got["intent"] == "neutral"

    def test_env_flag_disables_the_llm_tier(self, monkeypatch):
        monkeypatch.setenv("DM_NURTURE_INTENT_LLM_ENABLED", "false")
        with patch(f"{_MOD}._llm_intent") as llm:
            assert _classify("Interesting timing on this one honestly")["intent"] == "neutral"
        llm.assert_not_called()

    def test_blank_env_keeps_the_default_on(self, monkeypatch):
        monkeypatch.setenv("DM_NURTURE_INTENT_LLM_ENABLED", "  ")
        with patch(f"{_MOD}._llm_intent", return_value=None) as llm:
            _classify("Interesting timing on this one honestly")
        llm.assert_called_once()


class TestBranchPolicy:
    def test_guidance_differs_per_intent(self):
        from cqc_lem.utilities.ai.dm_nurture import nurture_guidance
        interested = nurture_guidance("interested")
        not_now = nurture_guidance("not_now")
        assert "call" in interested.lower()
        assert interested != not_now
        assert "do not" in nurture_guidance("objection").lower() or "no " in nurture_guidance("objection").lower()

    def test_unknown_intent_falls_back_to_the_neutral_brief_not_a_pitch(self):
        from cqc_lem.utilities.ai.dm_nurture import nurture_guidance
        assert nurture_guidance("who_knows") == nurture_guidance("neutral")

    def test_not_now_waits_far_longer_than_interest(self):
        from cqc_lem.utilities.ai.dm_nurture import nurture_delay_hours
        assert nurture_delay_hours("interested") < nurture_delay_hours("objection") \
               < nurture_delay_hours("neutral") < nurture_delay_hours("not_now")

    def test_unknown_intent_delay_falls_back_to_neutral(self):
        from cqc_lem.utilities.ai.dm_nurture import nurture_delay_hours
        assert nurture_delay_hours("who_knows") == nurture_delay_hours("neutral")

    def test_only_disinterest_stops(self):
        from cqc_lem.utilities.ai.dm_nurture import is_stop_intent
        assert is_stop_intent("disinterest") is True
        assert not any(is_stop_intent(i) for i in ("interested", "objection", "not_now", "neutral"))


class TestRecipientContext:
    """Issue #1625 — the draft has to know who it is writing to, from stored data only."""

    def _facts(self, **kw):
        base = {"job_title": "VP Engineering", "company_name": "Acme", "industry": "SaaS"}
        base.update(kw)
        return {"https://x/in/jane": base}

    def test_reads_title_company_and_industry_from_the_stored_profile(self):
        from cqc_lem.utilities.ai.dm_nurture import recipient_context
        with patch("cqc_lem.utilities.db.get_profile_facts", return_value=self._facts()):
            got = recipient_context(profile_url="https://x/in/jane", first_name="Jane")
        assert got["first_name"] == "Jane"
        assert got["job_title"] == "VP Engineering"
        assert got["company_name"] == "Acme"
        assert got["industry"] == "SaaS"

    def test_no_profile_visit_is_ever_opened(self):
        """The whole point of the design: context costs a DB read, never a Chrome session."""
        from cqc_lem.utilities.ai import dm_nurture
        with patch("cqc_lem.utilities.db.get_profile_facts", return_value={}), \
             patch("cqc_lem.utilities.selenium_util.get_docker_driver") as driver:
            dm_nurture.recipient_context(profile_url="https://x/in/jane", first_name="Jane")
        driver.assert_not_called()

    def test_an_unscraped_profile_degrades_instead_of_failing(self):
        from cqc_lem.utilities.ai.dm_nurture import recipient_context
        with patch("cqc_lem.utilities.db.get_profile_facts", return_value={}):
            got = recipient_context(profile_url="https://x/in/nobody", first_name="Sam")
        assert got == {"first_name": "Sam"}

    def test_nothing_known_at_all_is_an_empty_context_not_an_error(self):
        from cqc_lem.utilities.ai.dm_nurture import recipient_context
        with patch("cqc_lem.utilities.db.get_profile_facts", return_value={}):
            assert recipient_context() == {}

    def test_a_failed_read_is_swallowed(self):
        from cqc_lem.utilities.ai.dm_nurture import recipient_context
        with patch("cqc_lem.utilities.db.get_profile_facts", side_effect=RuntimeError("db down")):
            got = recipient_context(profile_url="https://x/in/jane", first_name="Jane")
        assert got == {"first_name": "Jane"}

    def test_the_json_null_string_is_not_a_fact(self):
        # JSON_UNQUOTE(JSON_EXTRACT(data, '$.company_name')) returns the STRING 'null' for a JSON
        # null — rendering it would put "Their company: null" in the prompt.
        from cqc_lem.utilities.ai.dm_nurture import recipient_context
        with patch("cqc_lem.utilities.db.get_profile_facts",
                   return_value=self._facts(company_name="null", industry="None")):
            got = recipient_context(profile_url="https://x/in/jane")
        assert "company_name" not in got and "industry" not in got
        assert got["job_title"] == "VP Engineering"

    @pytest.mark.parametrize("event_type,phrase", [
        ("connection_accepted", "accepted your connection request"),
        ("profile_viewer", "viewed your profile"),
        ("funnel", "commented on their post"),
    ])
    def test_the_event_type_says_why_the_thread_exists(self, event_type, phrase):
        from cqc_lem.utilities.ai.dm_nurture import recipient_context
        with patch("cqc_lem.utilities.db.get_profile_facts", return_value={}):
            got = recipient_context(profile_url="https://x/in/jane", event_type=event_type)
        assert phrase in got["thread_origin"]

    @pytest.mark.parametrize("event_type", ["nurture", "", None, "something_new"])
    def test_an_origin_we_cannot_name_is_omitted_never_guessed(self, event_type):
        from cqc_lem.utilities.ai.dm_nurture import recipient_context, thread_origin
        assert thread_origin(event_type) is None
        with patch("cqc_lem.utilities.db.get_profile_facts", return_value={}):
            assert "thread_origin" not in recipient_context(event_type=event_type)


class TestFormatRecipientContext:
    def test_renders_only_the_fields_present(self):
        from cqc_lem.utilities.ai.dm_nurture import format_recipient_context
        block = format_recipient_context({"first_name": "Jane", "job_title": "VP Engineering"})
        assert "Jane" in block and "VP Engineering" in block
        assert "Their company" not in block and "Their industry" not in block

    def test_it_forbids_inferring_from_what_it_carries(self):
        from cqc_lem.utilities.ai.dm_nurture import format_recipient_context
        block = format_recipient_context({"job_title": "VP Engineering"}).lower()
        assert "nothing more" in block
        assert "infer" in block

    @pytest.mark.parametrize("context", [None, {}, {"job_title": "  "}, {"job_title": None}])
    def test_no_context_renders_no_block(self, context):
        from cqc_lem.utilities.ai.dm_nurture import format_recipient_context
        assert format_recipient_context(context) == ""
