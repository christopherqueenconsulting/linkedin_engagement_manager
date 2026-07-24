"""Unit tests for the nightly lead re-score (issue #484) — per-user rebuild, decay of people who
went quiet, ICP terms pulled from engagement preferences, and the fan-out task."""

from datetime import datetime, timedelta

import pytest
from unittest.mock import patch

pytestmark = pytest.mark.unit

_S = "cqc_lem.app.run_scheduler"
_DB = "cqc_lem.utilities.db"
_NOW = datetime(2026, 7, 24, 12, 0, 0)


def _rows():
    return [
        {"kind": "intent", "person_name": "Ada", "detail": "new",
         "person_profile_url": "https://linkedin.com/in/ada", "occurred_at": _NOW},
        {"kind": "engaged", "person_name": "Bob",
         "person_profile_url": "https://linkedin.com/in/bob",
         "occurred_at": _NOW - timedelta(days=50)},
    ]


class TestRebuildLeadsForUser:
    def test_scores_every_person_and_saves_them(self):
        with patch(f"{_DB}.get_lead_activity", return_value=_rows()), \
             patch(f"{_DB}.get_profile_facts", return_value={}), \
             patch(f"{_DB}.reset_lead_scores") as reset, \
             patch(f"{_DB}.upsert_lead", return_value=True) as upsert, \
             patch(f"{_S}.get_engagement_preferences", return_value={"focus_topics": ["ai"]}), \
             patch(f"{_S}.log_info"):
            from cqc_lem.app.run_scheduler import _rebuild_leads_for_user
            assert _rebuild_leads_for_user(7) == 2
        reset.assert_called_once_with(7)
        saved = {c.args[1]: c for c in upsert.call_args_list}
        assert set(saved) == {"in:ada", "in:bob"}
        assert saved["in:ada"].kwargs["stage"] == "hot"
        assert saved["in:ada"].kwargs["score"] > saved["in:bob"].kwargs["score"]
        assert saved["in:ada"].kwargs["signals"] == "intent"

    def test_resets_scores_even_when_nobody_engaged(self):
        """A quiet month must decay 'hot' people out — otherwise the board lies every morning."""
        with patch(f"{_DB}.get_lead_activity", return_value=[]), \
             patch(f"{_DB}.reset_lead_scores") as reset, \
             patch(f"{_DB}.upsert_lead") as upsert, \
             patch(f"{_S}.get_engagement_preferences", return_value={}):
            from cqc_lem.app.run_scheduler import _rebuild_leads_for_user
            assert _rebuild_leads_for_user(7) == 0
        reset.assert_called_once_with(7)
        upsert.assert_not_called()

    def test_looks_up_icp_facts_for_the_profiles_it_saw(self):
        facts = {"https://linkedin.com/in/ada": {"job_title": "Founder", "industry": "ai"}}
        with patch(f"{_DB}.get_lead_activity", return_value=_rows()), \
             patch(f"{_DB}.get_profile_facts", return_value=facts) as get_facts, \
             patch(f"{_DB}.reset_lead_scores"), \
             patch(f"{_DB}.upsert_lead", return_value=True) as upsert, \
             patch(f"{_S}.get_engagement_preferences", return_value={"focus_topics": ["ai"]}), \
             patch(f"{_S}.log_info"):
            from cqc_lem.app.run_scheduler import _rebuild_leads_for_user
            _rebuild_leads_for_user(7)
        assert get_facts.call_args[0][0] == ["https://linkedin.com/in/ada",
                                             "https://linkedin.com/in/bob"]
        ada = next(c for c in upsert.call_args_list if c.args[1] == "in:ada")
        assert ada.kwargs["icp_score"] == 100

    def test_counts_only_leads_that_actually_saved(self):
        with patch(f"{_DB}.get_lead_activity", return_value=_rows()), \
             patch(f"{_DB}.get_profile_facts", return_value={}), \
             patch(f"{_DB}.reset_lead_scores"), \
             patch(f"{_DB}.upsert_lead", side_effect=[True, False]), \
             patch(f"{_S}.get_engagement_preferences", return_value={}), \
             patch(f"{_S}.log_info"):
            from cqc_lem.app.run_scheduler import _rebuild_leads_for_user
            assert _rebuild_leads_for_user(7) == 1

    def test_missing_preferences_do_not_break_the_rebuild(self):
        with patch(f"{_DB}.get_lead_activity", return_value=_rows()), \
             patch(f"{_DB}.get_profile_facts", return_value={}), \
             patch(f"{_DB}.reset_lead_scores"), \
             patch(f"{_DB}.upsert_lead", return_value=True), \
             patch(f"{_S}.get_engagement_preferences", return_value=None), \
             patch(f"{_S}.log_info"):
            from cqc_lem.app.run_scheduler import _rebuild_leads_for_user
            assert _rebuild_leads_for_user(7) == 2


class TestLeadTargetTerms:
    def test_merges_focus_topics_with_targeting_filters(self):
        from cqc_lem.app.run_scheduler import _lead_target_terms
        terms = _lead_target_terms({"focus_topics": ["AI "], "include_topics": ["automation"],
                                    "include_keywords": ["saas", " "], "exclude_topics": ["crypto"]})
        assert terms == ["AI", "automation", "saas"]

    def test_empty_preferences_yield_no_terms(self):
        from cqc_lem.app.run_scheduler import _lead_target_terms
        assert _lead_target_terms({}) == []


class TestRebuildTasks:
    def test_per_user_task_reports_what_it_scored(self):
        with patch(f"{_S}._rebuild_leads_for_user", return_value=3):
            from cqc_lem.app.run_scheduler import rebuild_leads_for_user
            assert "3 lead(s)" in rebuild_leads_for_user(7)

    def test_nightly_task_fans_out_to_every_active_user(self):
        with patch(f"{_S}.get_active_user_ids", return_value=[1, 2]), \
             patch(f"{_S}.rebuild_leads_for_user.apply_async") as dispatch:
            from cqc_lem.app.run_scheduler import rebuild_lead_scores
            result = rebuild_lead_scores()
        assert dispatch.call_count == 2
        assert dispatch.call_args_list[0].kwargs["kwargs"] == {"user_id": 1}
        assert "2 user(s)" in result

    def test_scoring_is_not_gated_on_the_linkedin_throttle(self):
        """It reads stored data only — a rate-limited account still gets a fresh hot list."""
        with patch(f"{_S}.get_active_user_ids", return_value=[1]), \
             patch(f"{_S}._skip_if_throttled", return_value=True) as throttle, \
             patch(f"{_S}.rebuild_leads_for_user.apply_async") as dispatch:
            from cqc_lem.app.run_scheduler import rebuild_lead_scores
            rebuild_lead_scores()
        throttle.assert_not_called()
        dispatch.assert_called_once()


class TestBeatSchedule:
    def test_the_nightly_rebuild_is_scheduled(self):
        from cqc_lem.app.my_celery import app
        entry = app.conf.beat_schedule["rebuild-lead-scores"]
        assert entry["task"] == "cqc_lem.app.run_scheduler.rebuild_lead_scores"
