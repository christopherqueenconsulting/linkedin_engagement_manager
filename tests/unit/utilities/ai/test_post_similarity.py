"""Unit tests for the post-history uniqueness engine in content_framework (deterministic token-set
similarity, opener/subject fingerprints, avoid directive) and the focus-alignment heuristic in
content_alignment."""

import pytest

from cqc_lem.utilities.ai import content_framework as fw
from cqc_lem.utilities.ai.content_alignment import content_matches_focus

pytestmark = pytest.mark.unit

_POST_A = ("Why routing LLM calls by complexity cuts your cost per successful call.\n\n"
           "Most teams overpay for AI because every request hits the frontier model, even the "
           "trivial ones. Route the simple calls to cheap models and reserve the expensive ones "
           "for genuinely hard prompts.\n\nWhat does your routing setup look like?")

# A reworded near-duplicate of _POST_A: same vocabulary, different order and phrasing.
_POST_A_REWORDED = ("Most teams overpay for AI: every request hits the frontier model, even "
                    "trivial ones. Routing LLM calls by complexity cuts the cost per successful "
                    "call — cheap models for simple calls, expensive models for the genuinely "
                    "hard prompts. How do you route?")

_POST_B = ("I planted tomatoes with my kids last weekend and the garden taught me patience.\n\n"
           "Watching seedlings grow reminded me that compounding beats intensity in almost "
           "everything worth doing.")


class TestTextSimilarity:
    def test_near_duplicate_scores_above_default_threshold(self):
        assert fw.text_similarity(_POST_A, _POST_A_REWORDED) > fw.POST_SIMILARITY_MAX_DEFAULT

    def test_unrelated_posts_score_below_threshold(self):
        assert fw.text_similarity(_POST_A, _POST_B) < fw.POST_SIMILARITY_MAX_DEFAULT

    def test_identical_text_scores_one(self):
        assert fw.text_similarity(_POST_A, _POST_A) == 1.0

    def test_empty_inputs_score_zero(self):
        assert fw.text_similarity("", _POST_A) == 0.0
        assert fw.text_similarity(_POST_A, None) == 0.0

    def test_symmetric_and_deterministic(self):
        assert fw.text_similarity(_POST_A, _POST_B) == fw.text_similarity(_POST_B, _POST_A)
        assert fw.text_similarity(_POST_A, _POST_A_REWORDED) == fw.text_similarity(
            _POST_A, _POST_A_REWORDED)

    def test_stopwords_do_not_inflate_similarity(self):
        a = "the and of to in a is it for on this that we you"
        b = "the and of to in a is it for on this that we you"
        assert fw.text_similarity(a, b) == 0.0  # all stopwords → nothing meaningful shared


class TestFindMostSimilar:
    def test_returns_best_scoring_candidate(self):
        score, match = fw.find_most_similar(_POST_A, [_POST_B, _POST_A_REWORDED])
        assert match == _POST_A_REWORDED
        assert score == fw.text_similarity(_POST_A, _POST_A_REWORDED)

    def test_empty_candidates(self):
        assert fw.find_most_similar(_POST_A, []) == (0.0, None)
        assert fw.find_most_similar(_POST_A, None) == (0.0, None)


class TestThresholdEnv:
    def test_default(self, monkeypatch):
        monkeypatch.delenv("POST_SIMILARITY_MAX", raising=False)
        assert fw.post_similarity_max() == fw.POST_SIMILARITY_MAX_DEFAULT == 0.55

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("POST_SIMILARITY_MAX", "0.8")
        assert fw.post_similarity_max() == 0.8

    def test_garbage_env_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("POST_SIMILARITY_MAX", "not-a-number")
        assert fw.post_similarity_max() == fw.POST_SIMILARITY_MAX_DEFAULT


class TestOpenerAndSubject:
    def test_opening_line_takes_first_nonempty_line(self):
        assert fw.opening_line("\n\n  First real line.\nSecond line.") == "First real line."

    def test_opening_line_empty_text(self):
        assert fw.opening_line("") == ""
        assert fw.opening_line(None) == ""

    def test_infer_post_subject_returns_salient_keywords(self):
        subject = fw.infer_post_subject(_POST_A)
        assert "routing" in subject and "llm" in subject
        assert "the" not in subject.split(", ")

    def test_infer_post_subject_deterministic(self):
        assert fw.infer_post_subject(_POST_A) == fw.infer_post_subject(_POST_A)


class TestHistoryAvoidanceDirective:
    def test_contains_openers_and_subjects(self):
        directive = fw.history_avoidance_directive([_POST_A, _POST_B])
        assert "UNIQUENESS RULES" in directive
        assert fw.opening_line(_POST_A) in directive
        assert fw.opening_line(_POST_B) in directive
        assert fw.infer_post_subject(_POST_A) in directive

    def test_offending_text_added_on_retry(self):
        directive = fw.history_avoidance_directive([_POST_A], offending_text=_POST_A_REWORDED)
        assert "TOO SIMILAR" in directive
        assert _POST_A_REWORDED[:100] in directive

    def test_empty_history_returns_empty_string(self):
        assert fw.history_avoidance_directive([]) == ""
        assert fw.history_avoidance_directive(None) == ""

    def test_duplicate_openers_deduped(self):
        directive = fw.history_avoidance_directive([_POST_A, _POST_A])
        assert directive.count(fw.opening_line(_POST_A)) == 1


class TestContentMatchesFocus:
    _TOPICS = ["LLM/AI cost efficiency", "model & complexity routing"]

    def test_on_focus_content_passes(self):
        assert content_matches_focus(_POST_A, self._TOPICS) is True

    def test_off_focus_content_flags(self):
        assert content_matches_focus(_POST_B, self._TOPICS) is False

    def test_empty_focus_topics_is_noop(self):
        assert content_matches_focus(_POST_B, []) is True
        assert content_matches_focus(_POST_B, None) is True

    def test_assigned_subject_counts_as_alignment(self):
        assert content_matches_focus(_POST_B, self._TOPICS,
                                     subject="gardening patience compounding") is True

    def test_short_topic_matches_on_single_meaningful_token(self):
        assert content_matches_focus("We shipped a new AI feature today.", ["AI"]) is True
