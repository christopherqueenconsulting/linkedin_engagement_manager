"""Un-grounded first-person metrics in comment drafts (issue #1834).

A `comment_generation` trace audit found invented operating history in roughly 8 of 12 drafts read —
"we logged 1,200 errors per week until we added observability, then 300, a 75% drop", "cut the
ticket age from 12 days to 3 days". None of it traced to the story bank, and a comment publishes
under the user's name with no review step between the draft and the feed.

These tests pin both halves of that, because a check that blocks everything numeric is as wrong as
one that blocks nothing: an invented first-person metric is regenerated on the #617 budget and then
SKIPS the post, while a number the target post, the supplied research, or the user's own story bank
actually contains still ships on the first attempt.
"""

from unittest.mock import MagicMock, patch

import pytest

from cqc_lem.utilities.ai import story_bank as sb

pytestmark = pytest.mark.unit

_AI = "cqc_lem.utilities.ai.ai_helper"
_DB_BANK = "cqc_lem.utilities.db.get_story_bank_entries"

# The target post. It states one number of its own ("3 days to 12"), which is what makes the
# "quotes the post's number" case distinguishable from the "invented it" case.
_POST = ("Our support queue got worse after we added a second triage tier — median ticket age went "
         "from 3 days to 12. Nobody warns you that more routing means more waiting.")

# Grounded: the first-person sentence repeats the post's own numbers.
_QUOTES_THE_POST = (
    "The second tier being the thing that slowed it down is the part most queue posts miss. "
    "We watched our own median ticket age go from 3 days to 12 after we added routing. "
    "What finally made you unwind it?")

# Un-grounded: a first-person metric that appears nowhere we gave the model.
_INVENTED = (
    "The second tier being the thing that slowed it down is the part most queue posts miss. "
    "We logged 1,200 tickets a week before triage and 300 after, a 75% drop. "
    "What finally made you unwind it?")

# Grounded because it carries no checkable specific at all — the common case at comment volume.
_NO_NUMBERS = (
    "The second tier being the thing that slowed it down is the part most queue posts miss. "
    "We unwound ours for the same reason and the queue moved again. "
    "What finally made you unwind it?")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in ("COMMENT_GATE_MAX_ATTEMPTS", "FACT_GROUNDING_SEVERITY",
                 "FACT_GROUNDING_SEVERITY_COMMENT", "FACT_GROUNDING_SEVERITY_POST"):
        monkeypatch.delenv(name, raising=False)


def _resp(text):
    r = MagicMock()
    r.choices = [MagicMock(message=MagicMock(content=text))]
    return r


def _profile():
    p = MagicMock()
    p.model_dump_json.return_value = '{"full_name": "Jane", "job_title": "COO"}'
    return p


def _generate(draft, *, bank=None, **kwargs):
    """Run one drafted comment through the real gate, with the story bank read stubbed."""
    from cqc_lem.utilities.ai import ai_helper
    with patch(f"{_AI}._call_llm", return_value=_resp(draft)) as llm, \
         patch(_DB_BANK, return_value=list(bank or [])) as read_bank:
        out = ai_helper.generate_ai_response(_POST, _profile(), user_id=7, **kwargs)
    return out, llm, read_bank


class TestSeverityPerSurface:
    """The SURFACE_SEVERITIES pattern: the same finding costs different things per surface."""

    def test_comments_are_hard_because_nobody_reviews_them(self):
        assert sb.fact_grounding_severity("comment") == sb.SEVERITY_HARD

    def test_posts_stay_warn_because_the_review_gate_already_holds_them(self):
        assert sb.fact_grounding_severity("post") == sb.SEVERITY_WARN

    def test_unknown_and_missing_surfaces_take_the_default(self):
        assert sb.fact_grounding_severity("dm") == sb.SEVERITY_WARN
        assert sb.fact_grounding_severity(None) == sb.SEVERITY_WARN

    def test_ops_can_overrule_a_surface_without_a_deploy(self, monkeypatch):
        monkeypatch.setenv("FACT_GROUNDING_SEVERITY_COMMENT", "off")
        assert sb.fact_grounding_severity("comment") == sb.SEVERITY_OFF

    def test_the_surface_specific_knob_beats_the_global_one(self, monkeypatch):
        monkeypatch.setenv("FACT_GROUNDING_SEVERITY", "off")
        monkeypatch.setenv("FACT_GROUNDING_SEVERITY_COMMENT", "hard")
        assert sb.fact_grounding_severity("comment") == sb.SEVERITY_HARD

    def test_an_unreadable_value_is_ignored_rather_than_obeyed(self, monkeypatch):
        monkeypatch.setenv("FACT_GROUNDING_SEVERITY_COMMENT", "maybe")
        assert sb.fact_grounding_severity("comment") == sb.SEVERITY_HARD


class TestUngroundedMetricDoesNotShip:
    def test_an_invented_first_person_metric_is_never_posted(self):
        out, llm, _ = _generate(_INVENTED)
        assert out is None                                   # the post is skipped, not commented on
        assert llm.call_count > 1                            # it spent the #617 regeneration budget

    def test_the_retry_names_the_numbers_the_rewrite_must_drop(self):
        from cqc_lem.utilities.ai import ai_helper
        drafts = [_resp(_INVENTED), _resp(_QUOTES_THE_POST)]
        with patch(f"{_AI}._call_llm", side_effect=drafts) as llm, \
             patch(_DB_BANK, return_value=[]):
            out = ai_helper.generate_ai_response(_POST, _profile(), user_id=7)
        assert out == _QUOTES_THE_POST
        retry = llm.call_args_list[1].kwargs["messages"][0]["content"]
        assert "YOUR PREVIOUS DRAFT WAS REJECTED" in retry
        assert "1200" in retry and "300" in retry and "75" in retry

    def test_the_skip_is_logged_with_the_reason(self):
        from cqc_lem.utilities.ai import ai_helper
        with patch(f"{_AI}._call_llm", return_value=_resp(_INVENTED)), \
             patch(_DB_BANK, return_value=[]), \
             patch(f"{_AI}.log_warning") as warn:
            assert ai_helper.generate_ai_response(_POST, _profile(), user_id=7) is None
        assert "skipping this post" in warn.call_args.args[0]
        assert "first-person specifics" in warn.call_args.args[0]

    def test_an_unreadable_story_bank_does_not_wave_the_metric_through(self):
        """Fail CLOSED on the number: with no bank we still hold the post and the research."""
        from cqc_lem.utilities.ai import ai_helper
        with patch(f"{_AI}._call_llm", return_value=_resp(_INVENTED)), \
             patch(_DB_BANK, side_effect=RuntimeError("bank down")), \
             patch(f"{_AI}.log_warning") as warn:
            assert ai_helper.generate_ai_response(_POST, _profile(), user_id=7) is None
        assert any("Story bank unreadable" in c.args[0] for c in warn.call_args_list)


class TestGroundedMetricStillShips:
    """The half that keeps the check from becoming a ban on numbers."""

    def test_a_number_the_target_post_states_is_quotable(self):
        out, llm, _ = _generate(_QUOTES_THE_POST)
        assert out == _QUOTES_THE_POST
        assert llm.call_count == 1                           # no regeneration was burned

    def test_a_number_from_the_supplied_research_findings_is_quotable(self):
        draft = ("The second tier being the thing that slowed it down is the part most queue posts "
                 "miss. We measured the same 40% drift on our own queue the month after routing "
                 "landed. What finally made you unwind it?")
        research = {"findings": "Teams adding a routing tier see median ticket age rise 40%.",
                    "sources": []}
        out, llm, _ = _generate(draft, research=research)
        assert out == draft
        assert llm.call_count == 1

    def test_research_that_was_never_supplied_sanctions_nothing(self):
        """Only the findings block actually handed to the writer counts as grounding."""
        draft = ("The second tier being the thing that slowed it down is the part most queue posts "
                 "miss. We measured the same 40% drift on our own queue the month after routing "
                 "landed. What finally made you unwind it?")
        out, _, _ = _generate(draft, research={"findings": "", "sources": []})
        assert out is None

    def test_a_number_from_the_users_own_story_bank_is_quotable(self):
        draft = ("The second tier being the thing that slowed it down is the part most queue posts "
                 "miss. We cut our own ticket age from 9 days to 4 by deleting a routing rule. "
                 "What finally made you unwind it?")
        bank = [{"kind": "number", "title": "Triage rollback",
                 "body": "Cut ticket age from 9 days to 4 by deleting a routing rule.",
                 "happened_at": None}]
        out, llm, read_bank = _generate(draft, bank=bank)
        assert out == draft
        assert llm.call_count == 1
        read_bank.assert_called_once()

    def test_a_comment_with_no_specifics_ships_without_reading_the_bank(self):
        """The cheap sources answer the common case, so comment volume adds no DB traffic."""
        out, llm, read_bank = _generate(_NO_NUMBERS)
        assert out == _NO_NUMBERS
        assert llm.call_count == 1
        read_bank.assert_not_called()

    def test_a_third_party_statistic_is_not_a_first_person_claim(self):
        """Only a number attached to we/our/I has to trace back to something we supplied.

        A quoted stat is sourced elsewhere, and the tie is per SENTENCE, so a figure in the
        neighbouring sentence is not the author's own claim either.
        """
        draft = ("The second tier being the thing that slowed it down is the part most queue posts "
                 "miss. Industry surveys put the median at 30 days for teams that keep routing. "
                 "We unwound ours for exactly that reason. What finally made you unwind it?")
        out, _, _ = _generate(draft)
        assert out == draft

    def test_warn_severity_records_the_finding_and_still_ships(self, monkeypatch):
        monkeypatch.setenv("FACT_GROUNDING_SEVERITY_COMMENT", "warn")
        out, llm, _ = _generate(_INVENTED)
        assert out == _INVENTED
        assert llm.call_count == 1

    def test_off_severity_skips_the_check_entirely(self, monkeypatch):
        monkeypatch.setenv("FACT_GROUNDING_SEVERITY_COMMENT", "off")
        out, _, read_bank = _generate(_INVENTED)
        assert out == _INVENTED
        read_bank.assert_not_called()


class TestScopeIsTheFeedComment:
    """Which callers of the shared gate opted in, pinned so a later one is a DECISION."""

    def test_the_second_wave_is_deliberately_not_gated_yet(self):
        """The second wave keeps today's behaviour, and that is a decision, not an oversight.

        The author's follow-up on their OWN post is asked for the number behind their own claim,
        and the gate is handed no allow-list for it. Turning it on there is its own argument with
        its own evidence, not a side effect of the feed-comment fix.
        """
        from cqc_lem.utilities.ai import ai_helper
        with patch(f"{_AI}._call_llm", return_value=_resp(_INVENTED)), \
             patch(f"{_AI}._humanize_text", side_effect=lambda t, **k: t), \
             patch(_DB_BANK) as read_bank:
            out = ai_helper.generate_second_wave_comment(_POST, _profile(), user_id=7,
                                                         recent_comments=[])
        assert out == _INVENTED
        read_bank.assert_not_called()

    def test_a_reply_to_a_comment_is_not_gated_either(self):
        """Replies keep their own acknowledge-and-answer contract — one call, no gate."""
        from cqc_lem.utilities.ai import ai_helper
        reply = "We saw 12 of those a week before the change. Did yours settle after a month?"
        with patch(f"{_AI}._call_llm", return_value=_resp(reply)) as llm, \
             patch(_DB_BANK) as read_bank:
            out = ai_helper.generate_ai_response(_POST, _profile(), user_id=7,
                                                 post_comment="their comment")
        assert out == reply and llm.call_count == 1
        read_bank.assert_not_called()
