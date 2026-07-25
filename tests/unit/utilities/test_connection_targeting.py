"""Unit tests for smart connection targeting's pure ranking logic (issue #486)."""

from datetime import datetime, timedelta

import pytest

from cqc_lem.utilities.connection_targeting import (CONNECT_NOTE_LIMIT, SOURCE_ADJACENT_POST,
                                                    SOURCE_OWN_POST, CandidateSignal,
                                                    default_connect_note, first_name,
                                                    group_signals, rank_candidates,
                                                    score_candidate, target_terms_from_prefs,
                                                    warmth_strength)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 7, 25, 12, 0, 0)


def _sig(name="Jane Doe", slug="jane-doe", source=SOURCE_OWN_POST, days_ago=0, author="",
         url=None, context_url="https://x/feed/update/1"):
    return CandidateSignal(person_name=name,
                           person_profile_url=(url if url is not None
                                               else f"https://www.linkedin.com/in/{slug}"),
                           source=source, context_url=context_url, context_author=author,
                           occurred_at=NOW - timedelta(days=days_ago))


class TestTargetTerms:
    def test_merges_focus_include_topics_and_keywords(self):
        prefs = {"focus_topics": ["AI adoption"], "include_topics": [" leadership "],
                 "include_keywords": ["ops", ""], "exclude_topics": ["crypto"]}
        assert target_terms_from_prefs(prefs) == ["AI adoption", "leadership", "ops"]

    def test_empty_prefs(self):
        assert target_terms_from_prefs({}) == []
        assert target_terms_from_prefs(None) == []


class TestWarmth:
    def test_own_post_beats_adjacent_post(self):
        own = warmth_strength([_sig()], NOW)
        adjacent = warmth_strength([_sig(source=SOURCE_ADJACENT_POST)], NOW)
        assert own > adjacent

    def test_recent_beats_stale(self):
        assert warmth_strength([_sig(days_ago=0)], NOW) > warmth_strength([_sig(days_ago=60)], NOW)

    def test_repeats_add_with_diminishing_returns(self):
        one = warmth_strength([_sig()], NOW)
        three = warmth_strength([_sig(), _sig(days_ago=1), _sig(days_ago=2)], NOW)
        assert one < three < one * 3

    def test_bounded_and_empty_safe(self):
        assert warmth_strength([], NOW) == 0
        assert warmth_strength([_sig() for _ in range(50)], NOW) == 100

    def test_unknown_timestamp_does_not_zero_the_signal(self):
        signal = CandidateSignal(person_name="Jane", person_profile_url="https://x/in/jane")
        assert warmth_strength([signal], NOW) > 0

    def test_tolerates_tz_aware_now(self):
        from datetime import timezone
        aware = NOW.replace(tzinfo=timezone.utc)
        assert warmth_strength([_sig(days_ago=1)], aware) > 0


class TestGroupSignals:
    def test_same_person_across_sources_collapses(self):
        grouped = group_signals([_sig(), _sig(source=SOURCE_ADJACENT_POST, author="Guru")])
        assert list(grouped) == ["in:jane-doe"]
        assert len(grouped["in:jane-doe"]) == 2

    def test_name_only_signal_stays_separate(self):
        grouped = group_signals([_sig(), _sig(url="")])
        assert set(grouped) == {"in:jane-doe", "name:jane doe"}

    def test_unidentifiable_signal_dropped(self):
        assert group_signals([CandidateSignal(person_name="", person_profile_url="")]) == {}


class TestScoreCandidate:
    def test_own_post_is_the_headline_source_even_when_older(self):
        candidate = score_candidate([_sig(source=SOURCE_ADJACENT_POST, days_ago=0, author="Guru"),
                                     _sig(source=SOURCE_OWN_POST, days_ago=5)], NOW)
        assert candidate.source == SOURCE_OWN_POST
        assert candidate.signal_count == 2

    def test_adjacent_context_is_kept_for_the_note(self):
        candidate = score_candidate([_sig(source=SOURCE_ADJACENT_POST, author="Guru",
                                          context_url="https://x/feed/update/9")], NOW)
        assert candidate.context_author == "Guru"
        assert candidate.context_url == "https://x/feed/update/9"
        assert "Guru" in candidate.reasons

    def test_facts_drive_icp_and_known_fit(self):
        facts = {"job_title": "VP of Revenue Operations", "industry": "SaaS"}
        candidate = score_candidate([_sig()], NOW, facts=facts, target_terms=["revenue operations"])
        assert candidate.known_fit is True
        assert candidate.icp_score > 50
        assert "strong ICP fit" in candidate.reasons

    def test_no_facts_is_neutral_and_flagged(self):
        candidate = score_candidate([_sig()], NOW, target_terms=["ops"])
        assert candidate.known_fit is False
        assert candidate.icp_score == 50
        assert "not scraped" in candidate.reasons

    def test_reasons_report_recency_and_repeat_count(self):
        candidate = score_candidate([_sig(days_ago=0), _sig(days_ago=3)], NOW)
        assert "engaged with your content (2×)" in candidate.reasons
        assert "engaged today" in candidate.reasons

    def test_reasons_report_days_since_last_engagement(self):
        assert "last engaged 4d ago" in score_candidate([_sig(days_ago=4)], NOW).reasons

    def test_reasons_tolerate_a_tz_aware_now(self):
        # MySQL hands back naive-UTC timestamps; a caller passing an aware `now` must not raise.
        from datetime import timezone
        candidate = score_candidate([_sig(days_ago=2)], NOW.replace(tzinfo=timezone.utc))
        assert "last engaged 2d ago" in candidate.reasons

    def test_weak_fit_is_called_out(self):
        candidate = score_candidate([_sig()], NOW, facts={"job_title": "intern"},
                                    target_terms=["revenue operations"])
        assert "weak ICP fit" in candidate.reasons


class TestRankCandidates:
    def test_ranks_best_first(self):
        signals = [_sig(name="Warm Wanda", slug="wanda"),
                   _sig(name="Cold Carl", slug="carl", source=SOURCE_ADJACENT_POST, days_ago=45)]
        ranked = rank_candidates(signals, NOW)
        assert [c.person_name for c in ranked] == ["Warm Wanda", "Cold Carl"]

    def test_excludes_already_requested_people(self):
        ranked = rank_candidates([_sig()], NOW, exclude_keys={"in:jane-doe"})
        assert ranked == []

    def test_drops_name_only_candidates(self):
        ranked = rank_candidates([_sig(url="")], NOW)
        assert ranked == []

    def test_icp_floor_applies_only_to_known_fit(self):
        facts = {"https://www.linkedin.com/in/carl": {"job_title": "intern", "industry": "retail"}}
        signals = [_sig(name="Carl", slug="carl"), _sig(name="Unknown Uma", slug="uma")]
        ranked = rank_candidates(signals, NOW, facts_by_url=facts, target_terms=["revenue ops"],
                                 min_icp=60)
        # Carl has facts and misses the floor; Uma has none and is judged on engagement instead.
        assert [c.person_name for c in ranked] == ["Unknown Uma"]

    def test_facts_match_on_slug_not_raw_url(self):
        facts = {"https://www.linkedin.com/in/jane-doe/": {"job_title": "Chief Revenue Officer"}}
        ranked = rank_candidates([_sig()], NOW, facts_by_url=facts, target_terms=["revenue"])
        assert ranked[0].known_fit is True and ranked[0].icp_score >= 70

    def test_limit_caps_the_result(self):
        signals = [_sig(name=f"P{i}", slug=f"p{i}") for i in range(5)]
        assert len(rank_candidates(signals, NOW, limit=2)) == 2

    def test_zero_limit_returns_nothing(self):
        signals = [_sig(name=f"P{i}", slug=f"p{i}") for i in range(5)]
        assert rank_candidates(signals, NOW, limit=0) == []

    def test_no_limit_returns_everything(self):
        signals = [_sig(name=f"P{i}", slug=f"p{i}") for i in range(5)]
        assert len(rank_candidates(signals, NOW, limit=None)) == 5

    def test_no_signals(self):
        assert rank_candidates([], NOW) == []


class TestConnectNote:
    def test_first_name(self):
        assert first_name("  Jane   Doe ") == "Jane"
        assert first_name(None) == "there"

    def test_own_post_note_thanks_them_for_engaging(self):
        candidate = score_candidate([_sig()], NOW)
        note = default_connect_note(candidate, topic="AI adoption")
        assert "Jane" in note and "engaging with my posts" in note and "AI adoption" in note

    def test_adjacent_note_names_the_author(self):
        candidate = score_candidate([_sig(source=SOURCE_ADJACENT_POST, author="Guru Gary")], NOW)
        assert "Guru Gary's post" in default_connect_note(candidate)

    def test_adjacent_note_without_a_known_author(self):
        candidate = score_candidate([_sig(source=SOURCE_ADJACENT_POST)], NOW)
        note = default_connect_note(candidate, topic="ops")
        assert "same conversations" in note and "ops" in note

    def test_note_respects_linkedin_note_limit(self):
        candidate = score_candidate([_sig(name="Jane Doe")], NOW)
        note = default_connect_note(candidate, topic="x" * 400)
        assert len(note) <= CONNECT_NOTE_LIMIT
