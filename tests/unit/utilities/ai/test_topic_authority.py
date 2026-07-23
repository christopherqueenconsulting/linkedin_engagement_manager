"""Unit tests for the Topic Authority (Topic DNA) governor (issue #384): the deterministic, NO-LLM
scoring of how tightly a draft sits inside the user's niche (declared focus topics + profile
headline/about), plus the profile-derived subject steering that keeps drafts on-niche. Off-niche
drafts must score low; on-niche drafts must clear the threshold."""

import pytest
from unittest.mock import MagicMock

from cqc_lem.utilities.ai.content_alignment import (
    topic_authority_score, topic_authority_min, topic_dna_tokens, is_on_niche,
    profile_topic_dna, profile_niche_anchors, select_focus_topic)

pytestmark = pytest.mark.unit

_FOCUS = ["LLM cost efficiency", "AI reliability and observability"]

_ON_NICHE = ("AI reliability starts with observability. We traced a silent failure spike to one "
             "retry loop; distributed tracing caught what dashboards missed.")
_OFF_NICHE = ("I planted tomatoes with my kids last weekend and the garden taught me patience. "
              "Seedlings grow slowly; compounding beats intensity.")


class _Skill:
    def __init__(self, name):
        self.name = name


class _Profile:
    """Minimal stand-in for LinkedInProfile — only the fields the Topic-DNA helpers read."""
    def __init__(self, job_title=None, industry=None, skills=None):
        self.job_title = job_title
        self.industry = industry
        self.skills = skills if skills is not None else []


class TestTopicAuthorityScore:
    def test_on_niche_draft_scores_high(self):
        assert topic_authority_score(_ON_NICHE, _FOCUS) >= 0.35

    def test_off_niche_draft_scores_low(self):
        score = topic_authority_score(_OFF_NICHE, _FOCUS)
        assert score < topic_authority_min()

    def test_on_niche_passes_off_niche_flagged(self):
        assert is_on_niche(_ON_NICHE, _FOCUS)
        assert not is_on_niche(_OFF_NICHE, _FOCUS)

    def test_score_bounded_zero_to_one(self):
        for content in (_ON_NICHE, _OFF_NICHE, "", "ai ai ai reliability observability"):
            s = topic_authority_score(content, _FOCUS)
            assert 0.0 <= s <= 1.0

    def test_empty_dna_is_noop_scores_one(self):
        # No focus topics and no profile headline/about → nothing to judge, never flag.
        assert topic_authority_score(_OFF_NICHE, []) == 1.0
        assert topic_authority_score(_OFF_NICHE, None, None, None) == 1.0

    def test_empty_content_is_noop_scores_one(self):
        assert topic_authority_score("", _FOCUS) == 1.0
        assert topic_authority_score(None, _FOCUS) == 1.0

    def test_headline_and_about_broaden_the_dna(self):
        # A draft off the focus topics but squarely in the profile's headline/about niche still scores.
        draft = "Our supply-chain logistics network cut delivery times across every regional warehouse."
        assert topic_authority_score(draft, _FOCUS) < topic_authority_min()
        rescued = topic_authority_score(
            draft, _FOCUS, headline="Supply-chain logistics leader",
            about="warehouse network delivery optimization")
        assert rescued >= topic_authority_min()

    def test_dna_tokens_union_of_all_sources(self):
        dna = topic_dna_tokens(["ai routing"], headline="observability", about="tracing")
        assert {"ai", "routing", "observability", "tracing"} <= dna

    def test_stopwords_and_noise_excluded_from_dna(self):
        dna = topic_dna_tokens(["the and of a"])  # all stopwords → nothing survives
        assert dna == set()


class TestThresholdConfig:
    def test_default_threshold(self):
        assert topic_authority_min() == pytest.approx(0.15)

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("TOPIC_AUTHORITY_MIN", "0.9")
        assert topic_authority_min() == pytest.approx(0.9)
        # A moderately on-niche draft now falls below a stricter bar.
        assert not is_on_niche(_ON_NICHE, _FOCUS)

    def test_invalid_env_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("TOPIC_AUTHORITY_MIN", "not-a-number")
        assert topic_authority_min() == pytest.approx(0.15)

    @pytest.mark.parametrize("bad", ["nan", "inf", "-inf", "-0.5", "1.5", "2.0"])
    def test_non_finite_or_out_of_range_falls_back_to_default(self, monkeypatch, bad):
        monkeypatch.setenv("TOPIC_AUTHORITY_MIN", bad)
        assert topic_authority_min() == pytest.approx(0.15)

    @pytest.mark.parametrize("good", ["0", "0.0", "1", "1.0", "0.5"])
    def test_in_range_values_are_honored(self, monkeypatch, good):
        monkeypatch.setenv("TOPIC_AUTHORITY_MIN", good)
        assert topic_authority_min() == pytest.approx(float(good))

    def test_explicit_threshold_arg_overrides_env(self, monkeypatch):
        monkeypatch.setenv("TOPIC_AUTHORITY_MIN", "0.99")
        assert is_on_niche(_ON_NICHE, _FOCUS, threshold=0.1)


class TestProfileTopicDna:
    def test_headline_and_about_assembled_from_profile(self):
        p = _Profile(job_title="AI Reliability Engineer", industry="Software",
                     skills=[_Skill("Observability"), "Distributed Tracing"])
        headline, about = profile_topic_dna(p, profile_synthesis="I build resilient AI systems.")
        assert "AI Reliability Engineer" in headline and "Software" in headline
        assert "resilient AI systems" in about
        assert "Observability" in about and "Distributed Tracing" in about

    def test_missing_profile_yields_empty(self):
        assert profile_topic_dna(None, None) == ("", "")

    def test_partial_profile_and_mock_do_not_raise(self):
        headline, about = profile_topic_dna(_Profile(job_title="Coach"))
        assert headline == "Coach" and about == ""
        # A MagicMock's auto-attrs are not strings → they must be filtered out, not stringified.
        headline, about = profile_topic_dna(MagicMock(), profile_synthesis="voice")
        assert headline == "" and about == "voice"


class TestProfileNicheAnchors:
    def test_derives_ordered_deduped_skill_anchors(self):
        p = _Profile(skills=[_Skill("AI Routing"), "Observability", _Skill("AI Routing")])
        assert profile_niche_anchors(p) == ["AI Routing", "Observability"]

    def test_none_and_mock_profile_yield_no_anchors(self):
        assert profile_niche_anchors(None) == []
        assert profile_niche_anchors(MagicMock()) == []

    def test_capped_at_five(self):
        p = _Profile(skills=[f"skill{i}" for i in range(9)])
        assert len(profile_niche_anchors(p)) == 5


class TestSelectFocusTopicProfileSteering:
    def test_falls_back_to_profile_anchors_when_no_focus_topics(self):
        p = _Profile(skills=["AI Routing", "Observability"])
        assert select_focus_topic({}, 0, profile=p) == "AI Routing"
        assert select_focus_topic({}, 1, profile=p) == "Observability"
        assert select_focus_topic({}, None, profile=p) == "AI Routing"

    def test_declared_focus_topics_win_over_profile(self):
        p = _Profile(skills=["Gardening"])
        assert select_focus_topic({"focus_topics": _FOCUS}, 0, profile=p) == _FOCUS[0]

    def test_no_anchors_and_no_focus_returns_none(self):
        # Mock profile → no usable anchors → None, preserving the industry-only behavior.
        assert select_focus_topic({}, 1, profile=MagicMock()) is None
        assert select_focus_topic({}, 1, profile=None) is None
