"""The first migrated cohort of toggles (issue #651) actually follows its flag.

A flag that nothing reads is worse than no flag at all, and the classic way to ship one is to leave
the toggle read at IMPORT time — which is unflippable. These tests drive each migrated toggle
through `flags.flag_enabled` and prove three things per toggle: PostHog wins when it answers, the
env var wins when it doesn't, and the value is resolved at CALL time.
"""

from contextlib import contextmanager
from datetime import date
from unittest.mock import MagicMock, patch

import pytest

from cqc_lem.utilities import flags

pytestmark = pytest.mark.unit

_PH = "cqc_lem.utilities.flags.posthog"


@contextmanager
def _posthog_says(value):
    """A faked, CONFIGURED SDK returning one value for every flag lookup."""
    with patch(_PH) as ph:
        ph.disabled = False
        ph.setup.return_value = MagicMock()
        ph.get_feature_flag.return_value = value
        yield ph


@pytest.fixture(autouse=True)
def _backend_available(monkeypatch):
    monkeypatch.setenv("POSTHOG_FLAGS_ENABLED", "true")
    monkeypatch.setenv("POSTHOG_API_KEY", "phc_test")
    monkeypatch.setenv("POSTHOG_PERSONAL_API_KEY", "phx_test")
    flags.reset_flag_state()
    yield
    flags.reset_flag_state()


class TestCommentResearch:
    def test_flag_on_enables_research_for_a_comment_the_env_var_would_block(self, monkeypatch):
        from cqc_lem.utilities.ai import content_research as cr
        monkeypatch.delenv("COMMENT_RESEARCH_ENABLED", raising=False)
        with _posthog_says(True):
            assert cr.research_enabled("comment", user_id=5) is True

    def test_flag_off_blocks_research_the_env_var_would_allow(self, monkeypatch):
        from cqc_lem.utilities.ai import content_research as cr
        monkeypatch.setenv("COMMENT_RESEARCH_ENABLED", "true")
        with _posthog_says(False):
            assert cr.research_enabled("comment", user_id=5) is False

    def test_master_switch_still_wins_over_the_flag(self, monkeypatch):
        """CONTENT_RESEARCH_ENABLED is the kill switch for ALL research and was deliberately not
        migrated — a flag must never be able to turn research back on against it.
        """
        from cqc_lem.utilities.ai import content_research as cr
        monkeypatch.setenv("CONTENT_RESEARCH_ENABLED", "false")
        with _posthog_says(True):
            assert cr.research_enabled("comment", user_id=5) is False

    def test_user_id_reaches_the_lookup_so_a_rollout_can_target_a_cohort(self, monkeypatch):
        from cqc_lem.utilities.ai import content_research as cr
        with _posthog_says(True) as ph:
            cr.research_enabled("comment", user_id=77)
        assert ph.get_feature_flag.call_args.args == (flags.COMMENT_RESEARCH, "77")

    def test_newsletter_and_post_toggles_stay_env_only(self, monkeypatch):
        from cqc_lem.utilities.ai import content_research as cr
        monkeypatch.setenv("POST_RESEARCH_ENABLED", "false")
        with _posthog_says(True) as ph:
            assert cr.research_enabled("post") is False
        ph.get_feature_flag.assert_not_called()


class TestTutorialVideos:
    def test_flag_off_skips_production_even_with_the_env_var_on(self, monkeypatch):
        from cqc_lem.utilities.marketing import video_tutorials as vt
        monkeypatch.setenv("TUTORIAL_VIDEOS_ENABLED", "true")
        with _posthog_says(False):
            assert vt.produce_tutorial() is None

    def test_flag_is_read_per_call_not_at_import(self, monkeypatch):
        """The toggle used to be a module constant. If it still were, the second call below would
        return the first call's answer.
        """
        from cqc_lem.utilities.marketing import video_tutorials as vt
        monkeypatch.setenv("TUTORIAL_VIDEOS_ENABLED", "false")
        with _posthog_says(False):
            assert vt.produce_tutorial() is None
        # Reaching PAST the gate is all this asserts — an unknown flow key raises only once the
        # toggle has let the call through. Producing a real tutorial needs the whole capture stack.
        with _posthog_says(True) as ph, pytest.raises(ValueError, match="Unknown tutorial flow"):
            vt.produce_tutorial(flow_key="nope-not-a-flow")
        assert ph.get_feature_flag.called


class TestCostRouting:
    def test_routing_enabled_follows_the_flag(self, monkeypatch):
        from cqc_lem.utilities import cost_routing as cr
        monkeypatch.delenv("COST_ROUTING_ENABLED", raising=False)
        with _posthog_says(True):
            assert cr.routing_enabled() is True
        with _posthog_says(False):
            assert cr.routing_enabled() is False

    def test_report_resolves_the_flag_at_call_time(self, monkeypatch):
        """`enabled=None` (the default) must consult the flag NOW — the old default argument froze
        COST_ROUTING_ENABLED at import, so a flip needed a deploy.
        """
        from cqc_lem.utilities import cost_routing as cr
        monkeypatch.delenv("COST_ROUTING_ENABLED", raising=False)
        with patch.object(cr, "collect_quality_observations", return_value=[]) as observe, \
             patch.object(cr, "load_policy", return_value={}), \
             patch("cqc_lem.utilities.db.cost_ledger_available", return_value=False), \
             _posthog_says(True):
            report = cr.collect_routing_report(days=7, today=date(2026, 8, 1))
        observe.assert_called_once()
        assert report["policy"]["enabled"] is True

    def test_explicit_enabled_argument_still_wins(self, monkeypatch):
        from cqc_lem.utilities import cost_routing as cr
        with patch.object(cr, "collect_quality_observations") as observe, \
             patch.object(cr, "load_policy", return_value={}), \
             _posthog_says(True):
            report = cr.collect_routing_report(days=7, today=date(2026, 8, 1), enabled=False)
        observe.assert_not_called()
        assert report["policy"]["enabled"] is False


class TestFeedFallbackDefault:
    """The flag moves the FLEET default only; a user's saved row always wins."""

    def _prefs(self, row):
        from cqc_lem.utilities import db
        with patch.object(db, "_select_engagement_row", return_value=row):
            return db.get_engagement_preferences(1)

    def test_flag_sets_the_default_for_a_user_with_no_saved_row(self, monkeypatch):
        monkeypatch.delenv("FEED_FALLBACK_WHEN_EMPTY_DEFAULT", raising=False)
        with _posthog_says(False):
            assert self._prefs(None)["feed_fallback_when_empty"] is False
        with _posthog_says(True):
            assert self._prefs(None)["feed_fallback_when_empty"] is True

    def test_a_saved_row_beats_the_flag(self):
        from cqc_lem.utilities.db import _ENGAGEMENT_DEFAULTS
        saved = {**_ENGAGEMENT_DEFAULTS, "feed_fallback_when_empty": False}
        with _posthog_says(True):
            assert self._prefs(saved)["feed_fallback_when_empty"] is False

    def test_env_var_is_the_fallback_when_posthog_has_no_answer(self, monkeypatch):
        monkeypatch.setenv("FEED_FALLBACK_WHEN_EMPTY_DEFAULT", "false")
        with _posthog_says(None):
            assert self._prefs(None)["feed_fallback_when_empty"] is False
