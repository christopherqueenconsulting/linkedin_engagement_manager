"""Unit tests for the story bank / fact-intake core (issue #620): which entry anchors a post, how
the rotation drains the bank, what happens when the bank can't ground the post, and the
fabricated-specific detector that enforces the no-invention rule."""

import pytest
from datetime import date, datetime, timedelta

from cqc_lem.utilities.ai import story_bank as sb

pytestmark = pytest.mark.unit


def _entry(entry_id=1, kind="anecdote", title="Onboarding rewrite", body=None,
           used_count=0, last_used_at=None, happened_at=None, active=True):
    return {
        "id": entry_id,
        "kind": kind,
        "title": title,
        "body": body if body is not None else "We cut onboarding from 12 days to 3 for a client.",
        "happened_at": happened_at,
        "used_count": used_count,
        "last_used_at": last_used_at,
        "active": active,
    }


class TestSelection:
    def test_picks_the_only_relevant_entry(self):
        onboarding = _entry(1, title="Onboarding", body="We rebuilt onboarding for a fintech.")
        gardening = _entry(2, title="Tomatoes", body="My tomato plants died in the frost.")
        picked = sb.select_story([gardening, onboarding], focus_topics=["onboarding"])
        assert picked["id"] == 1

    def test_no_topic_signal_rotates_over_the_whole_bank(self):
        # Nothing to be off-topic ABOUT — the bank should still anchor the post.
        entries = [_entry(1, used_count=3), _entry(2, used_count=0)]
        assert sb.select_story(entries)["id"] == 2

    def test_least_used_entry_wins_among_relevant_ones(self):
        old = _entry(1, body="onboarding overhaul", used_count=4)
        fresh = _entry(2, body="onboarding rewrite", used_count=1)
        assert sb.select_story([old, fresh], focus_topics=["onboarding"])["id"] == 2

    def test_never_used_beats_recently_used_at_the_same_count(self):
        used = _entry(1, body="onboarding one", used_count=0,
                      last_used_at=datetime.now() - timedelta(days=1))
        never = _entry(2, body="onboarding two", used_count=0, last_used_at=None)
        assert sb.select_story([used, never], focus_topics=["onboarding"])["id"] == 2

    def test_longest_unused_wins_when_both_have_been_used(self):
        recent = _entry(1, body="onboarding one", used_count=2,
                        last_used_at=datetime(2026, 7, 20))
        stale = _entry(2, body="onboarding two", used_count=2,
                       last_used_at=datetime(2026, 1, 5))
        assert sb.select_story([recent, stale], focus_topics=["onboarding"])["id"] == 2

    def test_rotation_drains_the_bank_before_repeating(self):
        # Simulate three posts in a row: each use bumps the counter, so a different entry anchors
        # each post and only the fourth post can repeat the first entry.
        entries = [_entry(1, body="onboarding one"), _entry(2, body="onboarding two"),
                   _entry(3, body="onboarding three")]
        picked = []
        for _ in range(4):
            chosen = sb.select_story(entries, focus_topics=["onboarding"])
            picked.append(chosen["id"])
            chosen["used_count"] += 1
            chosen["last_used_at"] = datetime.now()
        assert sorted(picked[:3]) == [1, 2, 3]
        assert picked[3] in (1, 2, 3)

    def test_last_used_at_as_a_plain_date_still_sorts(self):
        recent = _entry(1, body="onboarding one", used_count=1, last_used_at=date(2026, 7, 20))
        stale = _entry(2, body="onboarding two", used_count=1, last_used_at=date(2026, 1, 5))
        assert sb.select_story([recent, stale], focus_topics=["onboarding"])["id"] == 2


class TestEmptyBankFallback:
    def test_empty_bank_returns_none(self):
        assert sb.select_story([]) is None
        assert sb.select_story(None) is None

    def test_inactive_entries_are_not_selectable(self):
        assert sb.select_story([_entry(1, active=False)], focus_topics=["onboarding"]) is None

    def test_entries_without_text_are_ignored(self):
        assert sb.select_story([_entry(1, title="", body="   ")]) is None

    def test_no_relevant_entry_returns_none(self):
        gardening = _entry(1, title="Tomatoes", body="My tomato plants died in the frost.")
        assert sb.select_story([gardening], focus_topics=["kubernetes"]) is None

    def test_relevance_threshold_is_configurable(self, monkeypatch):
        entry = _entry(1, body="We rebuilt onboarding for a fintech client.")
        assert sb.select_story([entry], focus_topics=["onboarding"]) is not None
        monkeypatch.setenv("STORY_RELEVANCE_MIN_TOKENS", "3")
        assert sb.select_story([entry], focus_topics=["onboarding"]) is None

    def test_bad_threshold_env_falls_back_to_the_default(self, monkeypatch):
        monkeypatch.setenv("STORY_RELEVANCE_MIN_TOKENS", "not-a-number")
        assert sb.relevance_min_tokens() == sb.STORY_RELEVANCE_MIN_TOKENS_DEFAULT


class TestDirectives:
    def test_story_directive_carries_the_facts_and_the_no_invention_rule(self):
        text = sb.story_directive(_entry(kind="client_win", happened_at=date(2026, 3, 4)))
        assert "12 days to 3" in text
        assert "March 04, 2026" in text
        assert "ONLY personal specifics" in text
        assert "client outcome" in text

    def test_missing_entry_falls_back_to_the_no_story_directive(self):
        assert sb.story_directive(None) == sb.no_story_directive()
        assert sb.story_directive({"title": "", "body": ""}) == sb.no_story_directive()

    def test_no_story_directive_forbids_inventing_experience(self):
        text = sb.no_story_directive()
        assert "do NOT invent a personal anecdote" in text
        assert "industry observation" in text

    def test_body_is_truncated_into_the_prompt(self):
        text = sb.story_directive(_entry(body="x" * 5000))
        assert text.count("x") == sb.STORY_BODY_PROMPT_CHARS

    def test_unknown_kind_still_produces_a_directive(self):
        assert "lived detail" in sb.story_directive(_entry(kind="something-else"))

    def test_repair_directive_names_the_invented_specifics(self):
        text = sb.fabrication_repair_directive(["47", "March"])
        assert "47, March" in text
        assert "story bank entry" in text

    def test_repair_directive_without_tokens_still_states_the_rule(self):
        assert "ONLY the numbers" in sb.fabrication_repair_directive([])


class TestFabricationDetection:
    _SOURCES = ["We cut onboarding from 12 days to 3 for a client in March."]

    def test_sourced_first_person_specifics_pass(self):
        content = "I cut a client's onboarding from 12 days to 3."
        assert sb.unsourced_specifics(content, self._SOURCES) == []
        assert sb.has_unsourced_specifics(content, self._SOURCES) is False

    def test_invented_number_is_flagged(self):
        content = "I cut a client's onboarding from 12 days to 3, and revenue rose 47%."
        assert "47" in sb.unsourced_specifics(content, self._SOURCES)

    def test_third_person_statistics_are_not_treated_as_personal_claims(self):
        # A stat quoted from the research layer is sourced elsewhere; only FIRST-PERSON claims
        # about the author have to trace back to the bank.
        content = "The market grew 63% last year. I cut onboarding from 12 days to 3."
        assert sb.unsourced_specifics(content, self._SOURCES) == []

    def test_spelled_numbers_match_their_digits(self):
        content = "I cut onboarding from twelve days to three."
        assert sb.unsourced_specifics(content, self._SOURCES) == []

    def test_thousands_separators_normalize(self):
        content = "I saved them 1,200 hours."
        assert sb.unsourced_specifics(content, ["We saved 1200 hours."]) == []

    def test_invented_month_is_flagged(self):
        content = "I ran that migration back in September with the team."
        assert "september" in sb.unsourced_specifics(content, self._SOURCES)

    def test_no_sources_flags_every_first_person_specific(self):
        assert sb.unsourced_specifics("I shipped 4 features last month.", []) == ["4"]

    def test_empty_content_is_clean(self):
        assert sb.unsourced_specifics("", self._SOURCES) == []
        assert sb.unsourced_specifics(None, self._SOURCES) == []

    def test_duplicates_are_reported_once(self):
        content = "I grew it 47%. I grew it 47% again."
        assert sb.unsourced_specifics(content, self._SOURCES) == ["47"]


class TestFactSources:
    def test_includes_the_entry_and_extras(self):
        sources = sb.fact_sources(_entry(), "profile synthesis text")
        assert any("12 days to 3" in s for s in sources)
        assert "profile synthesis text" in sources

    def test_happened_at_date_is_citable(self):
        sources = sb.fact_sources(_entry(happened_at=date(2026, 3, 4)))
        assert any("March" in s for s in sources)

    def test_string_happened_at_is_kept(self):
        assert "2026-03-04" in sb.fact_sources(_entry(happened_at="2026-03-04"))

    def test_no_entry_yields_only_the_extras(self):
        assert sb.fact_sources(None, "synthesis") == ["synthesis"]

    def test_blank_extras_are_dropped(self):
        assert sb.fact_sources(None, "", None) == []
