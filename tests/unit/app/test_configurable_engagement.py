"""Configurability-audit tests for the engagement side: env-tunable feed-scoring weights
(FEED_SCORE_W_* / FEED_RECENCY_HALFLIFE_MIN, defaults preserved when unset/invalid), the
profile-viewer comment path honoring engagement preferences, and the create_text_post profile
fallback preferring the cached DB profile over a hardcoded persona.
"""

from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit

_OUT = "cqc_lem.app.engagement.outreach"
_RCP = "cqc_lem.app.run_content_plan"


class TestFeedScoreEnvOverrides:
    def _prefs(self):
        return {"include_authors": [], "include_keywords": [], "include_topics": [],
                "exclude_authors": [], "exclude_keywords": [], "exclude_topics": []}

    def test_defaults_when_env_unset(self, monkeypatch):
        from cqc_lem.app.engagement.feed import _score_feed_post
        for var in ("FEED_SCORE_W_RECENCY", "FEED_SCORE_W_RELEVANCE",
                    "FEED_SCORE_W_RECIPROCITY", "FEED_SCORE_W_ACTIVITY"):
            monkeypatch.delenv(var, raising=False)
        meta = {"author": "A", "age_minutes": 0, "comments": 5, "relevant": True}
        # 0.5*1.0 + 0.2*1.0 + 0.2*0.0 + 0.1*1.0 = 0.8 with the documented defaults
        assert _score_feed_post(meta, self._prefs()) == pytest.approx(0.8, abs=1e-6)

    def test_weight_override_changes_ranking(self, monkeypatch):
        from cqc_lem.app.engagement.feed import _score_feed_post
        monkeypatch.setenv("FEED_SCORE_W_RECENCY", "0")
        monkeypatch.setenv("FEED_SCORE_W_RELEVANCE", "0")
        monkeypatch.setenv("FEED_SCORE_W_ACTIVITY", "0")
        monkeypatch.setenv("FEED_SCORE_W_RECIPROCITY", "1.0")
        meta = {"author": "Jane Doe", "age_minutes": 0, "comments": 5, "relevant": True}
        assert _score_feed_post(meta, self._prefs(), engagers={"jane doe"}) == pytest.approx(1.0)
        assert _score_feed_post(meta, self._prefs(), engagers=set()) == pytest.approx(0.0)

    def test_invalid_env_falls_back_to_default(self, monkeypatch):
        from cqc_lem.app.engagement.feed import _score_feed_post
        meta = {"author": "A", "age_minutes": 0, "comments": 5, "relevant": True}
        baseline = _score_feed_post(meta, self._prefs())
        monkeypatch.setenv("FEED_SCORE_W_RECENCY", "not-a-number")
        assert _score_feed_post(meta, self._prefs()) == pytest.approx(baseline)

    def test_halflife_override_steepens_decay(self, monkeypatch):
        from cqc_lem.app.engagement.feed import _recency_score
        monkeypatch.delenv("FEED_RECENCY_HALFLIFE_MIN", raising=False)
        default_60 = _recency_score(60)
        monkeypatch.setenv("FEED_RECENCY_HALFLIFE_MIN", "10")
        assert _recency_score(60) < default_60
        assert _recency_score(None) == 0.4  # unknown-age behavior untouched


class TestProfileViewerCommentPrefs:
    def test_generate_and_post_comment_passes_engagement_prefs(self):
        from cqc_lem.app.engagement import outreach as ra
        from cqc_lem.utilities.linkedin.profile import LinkedInProfile
        my_profile = LinkedInProfile(full_name="Me User", email="me@example.com")
        driver, wait = MagicMock(), MagicMock()
        driver.current_url = "https://www.linkedin.com/feed/update/urn:li:activity:1/"
        prefs = {"tone": "warm", "use_emojis": False, "use_hashtags": False,
                 "comment_length": "short"}
        el = MagicMock()
        el.get_attribute.return_value = None
        with patch(f"{_OUT}.get_user_id", return_value=1), \
             patch(f"{_OUT}.check_commented", return_value=False), \
             patch(f"{_OUT}.get_element_wait_retry", return_value=el), \
             patch(f"{_OUT}.getText", return_value="A post about supply chains."), \
             patch(f"{_OUT}.pace_read", return_value=0.0), \
             patch(f"{_OUT}.get_engagement_preferences", return_value=prefs) as get_prefs, \
             patch(f"{_OUT}.generate_ai_response", return_value="Nice point.") as gen, \
             patch.object(ra.comment_on_post, "apply_async"):
            ok = ra.generate_and_post_comment(driver, wait,
                                              driver.current_url, my_profile,
                                              profile_synthesis="voice")
        assert ok is True
        get_prefs.assert_called_once_with(1)
        assert gen.call_args.kwargs.get("prefs") == prefs

    def test_prefs_load_failure_never_blocks_comment(self):
        from cqc_lem.app.engagement import outreach as ra
        from cqc_lem.utilities.linkedin.profile import LinkedInProfile
        my_profile = LinkedInProfile(full_name="Me User", email="me@example.com")
        driver, wait = MagicMock(), MagicMock()
        driver.current_url = "https://www.linkedin.com/feed/update/urn:li:activity:1/"
        el = MagicMock()
        el.get_attribute.return_value = None
        with patch(f"{_OUT}.get_user_id", return_value=1), \
             patch(f"{_OUT}.check_commented", return_value=False), \
             patch(f"{_OUT}.get_element_wait_retry", return_value=el), \
             patch(f"{_OUT}.getText", return_value="A post."), \
             patch(f"{_OUT}.pace_read", return_value=0.0), \
             patch(f"{_OUT}.get_engagement_preferences", side_effect=RuntimeError("db down")), \
             patch(f"{_OUT}.generate_ai_response", return_value="Nice point.") as gen, \
             patch.object(ra.comment_on_post, "apply_async"):
            ok = ra.generate_and_post_comment(driver, wait,
                                              driver.current_url, my_profile,
                                              profile_synthesis="voice")
        assert ok is True
        assert gen.call_args.kwargs.get("prefs") is None


class TestCreateTextPostProfileFallback:
    _LM_OFF = {"enabled": False, "keyword": None, "message": None}

    def _run_create(self, cached_profile):
        """Drive create_text_post with the Selenium profile load failing."""
        from cqc_lem.app import run_content_plan as rcp
        gen = MagicMock(return_value="generated post")
        with patch(f"{_RCP}.get_user_password_pair_by_id", return_value=("e@x.z", "pw")), \
             patch(f"{_RCP}.get_driver_wait_pair", return_value=(MagicMock(), MagicMock())), \
             patch(f"{_RCP}.get_my_profile", side_effect=RuntimeError("selenium down")), \
             patch(f"{_RCP}.quit_gracefully"), \
             patch(f"{_RCP}.load_profile_for_user", return_value=cached_profile) as loader, \
             patch(f"{_RCP}.get_engagement_preferences", return_value={}), \
             patch(f"{_RCP}.get_or_create_profile_synthesis", return_value="voice"), \
             patch(f"{_RCP}.get_recent_post_texts", return_value=[]), \
             patch(f"{_RCP}.get_recent_post_shape_history", return_value=[]), \
             patch(f"{_RCP}.get_lead_magnet_settings", return_value=self._LM_OFF), \
             patch(f"{_RCP}.update_db_post_shape"), \
             patch(f"{_RCP}.get_thought_leadership_post_from_ai", gen):
            out = rcp.create_text_post(1, "awareness", post_type="thought_leadership",
                                       refine_final_post=False, post_id=5)
        return out, gen, loader

    def test_uses_cached_profile_when_selenium_load_fails(self):
        from cqc_lem.utilities.linkedin.profile import LinkedInProfile
        cached = LinkedInProfile(full_name="Jane Realtor", job_title="Realtor",
                                 industry="Real Estate")
        out, gen, loader = self._run_create(cached)
        assert out == "generated post"
        loader.assert_called_once_with(1)
        assert gen.call_args.args[0] is cached

    def test_neutral_last_resort_replaces_john_doe_persona(self):
        out, gen, _ = self._run_create(None)
        assert out == "generated post"
        profile = gen.call_args.args[0]
        assert profile.full_name == "LinkedIn Member"
        assert profile.job_title == "Professional"
        # The old hardcoded tech persona must be gone.
        assert "Software Developer" not in (profile.job_title or "")
        assert (profile.company_name or "") != "ABC Inc."
