"""Unit tests for the feed-post scoring matrix (recency-dominant prioritization)."""

import pytest

pytestmark = pytest.mark.unit


class TestRecencyAndActivity:
    def test_recency_fresh_beats_old_and_unknown_is_mid(self):
        from cqc_lem.app.engagement.feed import _recency_score
        assert _recency_score(0) > 0.99
        assert _recency_score(60) > _recency_score(600)          # 1h fresher than 10h
        assert _recency_score(None) == 0.4                        # unknown = mid, never top
        assert _recency_score(10000) < 0.05                       # week-old ≈ 0

    def test_activity_prefers_forming_thread(self):
        from cqc_lem.app.engagement.feed import _activity_score
        assert _activity_score(5) == 1.0                          # forming thread
        assert _activity_score(0) == 0.4                          # no traction
        assert _activity_score(200) == 0.3                        # oversaturated / buried


class TestScoreFeedPost:
    def _prefs(self, **kw):
        base = {"include_authors": [], "include_keywords": [], "include_topics": [],
                "exclude_authors": [], "exclude_keywords": [], "exclude_topics": []}
        base.update(kw); return base

    def test_recent_post_scores_higher_than_old(self):
        from cqc_lem.app.engagement.feed import _score_feed_post
        prefs = self._prefs()
        recent = _score_feed_post({"author": "A", "age_minutes": 20, "comments": 3, "relevant": True}, prefs)
        old = _score_feed_post({"author": "A", "age_minutes": 2000, "comments": 3, "relevant": True}, prefs)
        assert recent > old

    def test_reciprocity_boost_for_engager(self):
        from cqc_lem.app.engagement.feed import _score_feed_post
        prefs = self._prefs()
        meta = {"author": "Jane Doe", "age_minutes": 120, "comments": 3, "relevant": True}
        with_boost = _score_feed_post(meta, prefs, engagers={"jane doe"})
        without = _score_feed_post(meta, prefs, engagers=set())
        assert with_boost > without

    def test_include_author_counts_as_reciprocity(self):
        from cqc_lem.app.engagement.feed import _score_feed_post
        prefs = self._prefs(include_authors=["Jane"])
        meta = {"author": "Jane Doe", "age_minutes": 120, "comments": 3, "relevant": True}
        assert _score_feed_post(meta, prefs, engagers=set()) > _score_feed_post(
            {**meta, "author": "Bob"}, prefs, engagers=set())

    def test_relevance_boost(self):
        from cqc_lem.app.engagement.feed import _score_feed_post
        prefs = self._prefs()
        meta = {"author": "A", "age_minutes": 120, "comments": 3}
        assert _score_feed_post({**meta, "relevant": True}, prefs) > _score_feed_post({**meta, "relevant": False}, prefs)


class TestGates:
    def _prefs(self, **kw):
        base = {"include_authors": [], "include_keywords": [], "include_topics": [],
                "exclude_authors": [], "exclude_keywords": [], "exclude_topics": []}
        base.update(kw); return base

    def test_hard_excludes(self):
        from cqc_lem.app.engagement.feed import _passes_hard_excludes
        assert _passes_hard_excludes("about crypto", "Jane", self._prefs(exclude_keywords=["crypto"])) is False
        assert _passes_hard_excludes("great content", "Spammy", self._prefs(exclude_authors=["spammy"])) is False
        assert _passes_hard_excludes("normal post", "Jane", self._prefs()) is True

    def test_literal_relevant(self):
        from cqc_lem.app.engagement.feed import _literal_relevant
        assert _literal_relevant("anything", "Jane", self._prefs()) is True          # no constraints
        assert _literal_relevant("about AI agents", "Jane", self._prefs(include_keywords=["ai"])) is True
        assert _literal_relevant("gardening", "Jane", self._prefs(include_keywords=["ai"])) is False
