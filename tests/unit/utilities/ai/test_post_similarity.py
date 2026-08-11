"""Unit tests for the post-history uniqueness engine in content_framework (embedding-first
similarity report, deterministic token-set similarity, opener/subject fingerprints, avoid directive)
and the focus-alignment heuristic in content_alignment.
"""

from unittest.mock import patch

import pytest

from cqc_lem.utilities import content_quality as cq
from cqc_lem.utilities.ai import content_framework as fw
from cqc_lem.utilities.ai.content_alignment import content_matches_focus

pytestmark = pytest.mark.unit

_FW = "cqc_lem.utilities.ai.content_framework"

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


class TestEmbeddingThresholdEnv:
    def test_default(self, monkeypatch):
        monkeypatch.delenv("POST_EMBEDDING_SIMILARITY_MAX", raising=False)
        assert fw.post_embedding_similarity_max() == fw.POST_EMBEDDING_SIMILARITY_MAX_DEFAULT

    def test_calibration_sits_between_the_measured_samples(self):
        # The #1138 audit's five scored posts: 0.633-0.657 are distinct posts, 0.832/0.848 are the
        # reworded pair. A default outside that band would either hold everything or nothing.
        assert 0.657 < fw.POST_EMBEDDING_SIMILARITY_MAX_DEFAULT < 0.832

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("POST_EMBEDDING_SIMILARITY_MAX", "0.9")
        assert fw.post_embedding_similarity_max() == 0.9

    def test_garbage_env_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("POST_EMBEDDING_SIMILARITY_MAX", "")
        assert fw.post_embedding_similarity_max() == fw.POST_EMBEDDING_SIMILARITY_MAX_DEFAULT
        monkeypatch.setenv("POST_EMBEDDING_SIMILARITY_MAX", "not-a-number")
        assert fw.post_embedding_similarity_max() == fw.POST_EMBEDDING_SIMILARITY_MAX_DEFAULT

    def test_user_pct_preference_never_reaches_the_cosine_ceiling(self, monkeypatch):
        # post_similarity_max_pct is a percentage on the TOKEN-OVERLAP scale; applied to cosine it
        # would reject nearly every post the user writes.
        monkeypatch.delenv("POST_EMBEDDING_SIMILARITY_MAX", raising=False)
        assert fw.post_similarity_max({"post_similarity_max_pct": 30}) == 0.3
        assert fw.post_embedding_similarity_max() == fw.POST_EMBEDDING_SIMILARITY_MAX_DEFAULT


class TestPostSimilarityReport:
    """Issue #1265: the post gate grades semantic similarity embedding-first.

    It degrades to the token overlap it has always had — never to "nothing is similar".
    """

    def test_prefers_the_embedding_measure_and_names_the_match(self):
        vectors = [[1.0, 0.0], [0.0, 1.0], [1.0, 0.0]]  # draft matches the SECOND history entry
        with patch(f"{_FW}.embed_comments", return_value=vectors) as embed:
            report = fw.post_similarity_report(_POST_A, [_POST_B, _POST_A_REWORDED])
        assert embed.call_count == 1  # ONE call for the draft plus the whole history
        assert embed.call_args.args[0] == [_POST_A, _POST_B, _POST_A_REWORDED]
        assert report["measure"] == fw.SIMILARITY_MEASURE_EMBEDDING
        assert report["score"] == 1.0
        assert report["match"] == _POST_A_REWORDED
        assert report["threshold"] == fw.post_embedding_similarity_max()
        assert report["too_similar"] is True

    def test_catches_a_rewording_a_token_overlap_gate_would_pass(self, monkeypatch):
        # The gap the issue names: cosine 0.83 on a reworded post, token overlap below 0.55.
        monkeypatch.delenv("POST_SIMILARITY_MAX", raising=False)
        lexically_distinct = "Nothing here shares vocabulary with the other draft whatsoever."
        vectors = [[0.83, 0.5578], [1.0, 0.0]]  # cosine ≈ 0.83
        assert fw.text_similarity(_POST_A, lexically_distinct) < fw.POST_SIMILARITY_MAX_DEFAULT
        with patch(f"{_FW}.embed_comments", return_value=vectors):
            report = fw.post_similarity_report(_POST_A, [lexically_distinct])
        assert report["too_similar"] is True
        assert round(report["score"], 2) == 0.83

    def test_falls_back_to_token_overlap_when_embeddings_are_unavailable(self, monkeypatch):
        monkeypatch.delenv("POST_SIMILARITY_MAX", raising=False)
        with patch(f"{_FW}.embed_comments", return_value=None):
            report = fw.post_similarity_report(_POST_A, [_POST_B, _POST_A_REWORDED])
        assert report["measure"] == fw.SIMILARITY_MEASURE_LEXICAL
        assert report["threshold"] == fw.POST_SIMILARITY_MAX_DEFAULT
        assert report["match"] == _POST_A_REWORDED
        assert report["too_similar"] is True

    def test_a_dead_embedding_endpoint_degrades_rather_than_disarming_the_gate(self):
        # No patch on embed_comments at all: the unit lane blocks the real embeddings call, so this
        # runs the production failure branch end to end. The near-duplicate must still be caught.
        report = fw.post_similarity_report(_POST_A, [_POST_A_REWORDED])
        assert report["measure"] == fw.SIMILARITY_MEASURE_LEXICAL
        assert report["too_similar"] is True

    def test_the_fallback_honours_the_users_own_ceiling(self):
        with patch(f"{_FW}.embed_comments", return_value=None):
            strict = fw.post_similarity_report(_POST_A, [_POST_A_REWORDED],
                                               {"post_similarity_max_pct": 55})
            loosest = fw.post_similarity_report(_POST_A, [_POST_A_REWORDED],
                                                {"post_similarity_max_pct": 100})
        assert strict["threshold"] == 0.55 and strict["too_similar"] is True
        assert loosest["threshold"] == 1.0 and loosest["too_similar"] is False

    def test_a_distinct_post_passes_on_the_embedding_path(self):
        vectors = [[1.0, 0.0], [0.0, 1.0]]
        with patch(f"{_FW}.embed_comments", return_value=vectors):
            report = fw.post_similarity_report(_POST_A, [_POST_B])
        assert report["score"] == 0.0 and report["too_similar"] is False

    def test_no_history_costs_no_embedding_call(self):
        with patch(f"{_FW}.embed_comments") as embed:
            report = fw.post_similarity_report(_POST_A, [])
        assert not embed.called
        assert report == {"score": 0.0, "threshold": fw.post_embedding_similarity_max(),
                          "match": None, "measure": fw.SIMILARITY_MEASURE_NONE,
                          "too_similar": False}

    def test_blank_draft_and_blank_history_entries_are_never_embedded(self):
        with patch(f"{_FW}.embed_comments") as embed:
            assert fw.post_similarity_report("   ", [_POST_A])["measure"] == fw.SIMILARITY_MEASURE_NONE
            assert fw.post_similarity_report(_POST_A, ["", None, "  "])["measure"] == (
                fw.SIMILARITY_MEASURE_NONE)
        assert not embed.called

    def test_history_is_capped_so_one_gate_is_one_bounded_call(self):
        history = [f"an older post number {i}" for i in range(fw.POST_HISTORY_LIMIT + 10)]
        with patch(f"{_FW}.embed_comments", return_value=None) as embed:
            fw.post_similarity_report(_POST_A, history)
        assert len(embed.call_args.args[0]) == fw.POST_HISTORY_LIMIT + 1


class TestMeasureVocabularyIsShared:
    """Acceptance #1265: the trend line and the gate name the same measure for the same post.

    Two vocabularies would let a hold and the trend disagree with nobody able to see it.
    """

    def test_telemetry_aliases_the_shared_constants(self):
        assert cq.MEASURE_EMBEDDING is fw.SIMILARITY_MEASURE_EMBEDDING
        assert cq.MEASURE_LEXICAL is fw.SIMILARITY_MEASURE_LEXICAL
        assert cq.MEASURE_NONE is fw.SIMILARITY_MEASURE_NONE

    def test_the_comment_gate_speaks_the_same_vocabulary(self):
        with patch(f"{_FW}.embed_comments", return_value=None):
            report = fw.comment_similarity_report("a fresh comment", ["an older comment"])
        assert report["measure"] == cq.MEASURE_LEXICAL

    def test_the_pure_findings_module_mirrors_the_embedding_name(self):
        from cqc_lem.utilities import quality_gates

        assert quality_gates.SIMILARITY_MEASURE_EMBEDDING == fw.SIMILARITY_MEASURE_EMBEDDING


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
