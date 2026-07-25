"""Unit tests for the connection-candidate sourcing scan (issue #486)."""

from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit

_RA = "cqc_lem.app.run_automation"
_RS = "cqc_lem.app.run_scheduler"

_ENGAGERS = [{"person_name": "Jane Doe",
              "person_profile_url": "https://www.linkedin.com/in/jane-doe",
              "occurred_at": datetime(2026, 7, 25, 9, 0, 0)}]


def _prefs(**over):
    prefs = {"connection_targeting_mode": "suggest", "connection_request_mode": "auto_approve",
             "max_invites_per_day": 10, "min_connection_icp_score": 55,
             "connection_target_authors": [], "focus_topics": ["revenue operations"]}
    prefs.update(over)
    return prefs


def _scan(prefs=None, engagers=None, requested=None, open_reqs=0, sent_today=0, facts=None,
          insert_id=7, max_new=None, adjacent=None):
    """Run the scan with every I/O boundary mocked; returns (result, insert_mock)."""
    from cqc_lem.app import run_automation as ra
    patches = {
        "get_engagement_preferences": prefs if prefs is not None else _prefs(),
        "count_invites_sent_today": sent_today,
        "count_open_connection_requests": open_reqs,
        "get_engager_candidates": _ENGAGERS if engagers is None else engagers,
        "get_requested_person_keys": requested or set(),
        "get_profile_facts": facts or {},
    }
    with patch.multiple(_RA, **{k: MagicMock(return_value=v) for k, v in patches.items()}), \
         patch(f"{_RA}.get_ai_message_refinement", return_value="Refined note"), \
         patch(f"{_RA}.insert_connection_request", return_value=insert_id) as insert, \
         patch(f"{_RA}._adjacent_author_signals", return_value=adjacent or []), \
         patch(f"{_RA}.get_current_profile") as profile, \
         patch(f"{_RA}.quit_gracefully"):
        profile.return_value = (MagicMock(), MagicMock(), "me@x.com",
                               MagicMock(full_name="Me Myself"))
        return ra.scan_connection_candidates.run(user_id=1, max_new=max_new), insert


class TestScanGating:
    def test_mode_off_does_nothing(self):
        out, insert = _scan(prefs=_prefs(connection_targeting_mode="off"))
        insert.assert_not_called()
        assert "off" in out

    def test_no_budget_left_does_nothing(self):
        out, insert = _scan(sent_today=10)
        insert.assert_not_called()
        assert "budget" in out

    def test_open_targets_consume_the_budget(self):
        # 10 cap - 2 sent - 8 already queued = 0 left: never build a backlog.
        out, insert = _scan(sent_today=2, open_reqs=8)
        insert.assert_not_called()
        assert "budget" in out

    def test_no_candidates_found(self):
        out, insert = _scan(engagers=[])
        insert.assert_not_called()
        assert "No connection candidates" in out

    def test_all_candidates_already_requested(self):
        out, insert = _scan(requested={"in:jane-doe"})
        insert.assert_not_called()
        assert "deduped" in out


class TestScanFiling:
    def test_files_a_pending_draft_in_suggest_mode(self):
        from cqc_lem.utilities.db import ConnectionRequestStatus
        out, insert = _scan()
        assert insert.call_count == 1
        kwargs = insert.call_args.kwargs
        assert insert.call_args.args == (1, "https://www.linkedin.com/in/jane-doe")
        assert kwargs["status"] == ConnectionRequestStatus.PENDING
        assert kwargs["recipient_name"] == "Jane Doe"
        assert kwargs["source"] == "own_post"
        assert kwargs["message"] == "Refined note"
        assert "engaged with your content" in kwargs["reasons"]
        assert "Filed 1" in out

    def test_auto_queue_mode_follows_connection_request_mode(self):
        from cqc_lem.utilities.db import ConnectionRequestStatus
        _out, insert = _scan(prefs=_prefs(connection_targeting_mode="auto_queue"))
        assert insert.call_args.kwargs["status"] == ConnectionRequestStatus.APPROVED

    def test_auto_queue_with_pre_review_still_needs_approval(self):
        from cqc_lem.utilities.db import ConnectionRequestStatus
        _out, insert = _scan(prefs=_prefs(connection_targeting_mode="auto_queue",
                                          connection_request_mode="pre_review"))
        assert insert.call_args.kwargs["status"] == ConnectionRequestStatus.PENDING

    def test_budget_caps_how_many_targets_are_filed(self):
        engagers = [{"person_name": f"P{i}",
                     "person_profile_url": f"https://www.linkedin.com/in/p{i}",
                     "occurred_at": datetime(2026, 7, 25, 9, 0, 0)} for i in range(8)]
        _out, insert = _scan(engagers=engagers, sent_today=8)  # 10 cap - 8 sent = 2
        assert insert.call_count == 2

    def test_explicit_max_new_overrides_the_per_scan_ceiling(self):
        engagers = [{"person_name": f"P{i}",
                     "person_profile_url": f"https://www.linkedin.com/in/p{i}",
                     "occurred_at": datetime(2026, 7, 25, 9, 0, 0)} for i in range(8)]
        _out, insert = _scan(engagers=engagers, max_new=1)
        assert insert.call_count == 1

    def test_failed_insert_is_not_counted(self):
        out, insert = _scan(insert_id=None)
        assert insert.call_count == 1 and "Filed 0" in out

    def test_below_icp_floor_is_skipped(self):
        facts = {"https://www.linkedin.com/in/jane-doe": {"job_title": "intern",
                                                         "industry": "retail"}}
        out, insert = _scan(facts=facts, prefs=_prefs(min_connection_icp_score=90))
        insert.assert_not_called()
        assert "ICP floor" in out

    def test_adjacent_signals_are_included(self):
        from cqc_lem.utilities.connection_targeting import CandidateSignal, SOURCE_ADJACENT_POST
        adjacent = [CandidateSignal(person_name="Guru Fan",
                                    person_profile_url="https://www.linkedin.com/in/guru-fan",
                                    source=SOURCE_ADJACENT_POST, context_author="Guru Gary",
                                    context_url="https://x/feed/update/1",
                                    occurred_at=datetime(2026, 7, 25, 8, 0, 0))]
        _out, insert = _scan(prefs=_prefs(connection_target_authors=["https://x/in/guru-gary"]),
                             engagers=[], adjacent=adjacent)
        assert insert.call_args.kwargs["source"] == "adjacent_post"
        assert "adjacent authors" in insert.call_args.kwargs["reasons"]

    def test_adjacent_sourcing_failure_still_files_own_post_engagers(self):
        from cqc_lem.app import run_automation as ra
        with patch(f"{_RA}.get_engagement_preferences",
                   return_value=_prefs(connection_target_authors=["https://x/in/guru"])), \
             patch(f"{_RA}.count_invites_sent_today", return_value=0), \
             patch(f"{_RA}.count_open_connection_requests", return_value=0), \
             patch(f"{_RA}.get_engager_candidates", return_value=_ENGAGERS), \
             patch(f"{_RA}.get_requested_person_keys", return_value=set()), \
             patch(f"{_RA}.get_profile_facts", return_value={}), \
             patch(f"{_RA}.get_ai_message_refinement", return_value="Refined note"), \
             patch(f"{_RA}.get_current_profile", side_effect=RuntimeError("no session")), \
             patch(f"{_RA}.insert_connection_request", return_value=7) as insert:
            out = ra.scan_connection_candidates.run(user_id=1)
        assert insert.call_count == 1 and "Filed 1" in out


class TestConnectNoteDrafting:
    def test_falls_back_to_the_template_when_the_llm_fails(self):
        from cqc_lem.app import run_automation as ra
        from cqc_lem.utilities.connection_targeting import CandidateSignal, score_candidate
        candidate = score_candidate([CandidateSignal(person_name="Jane Doe",
                                                     person_profile_url="https://x/in/jane-doe")],
                                    datetime(2026, 7, 25))
        with patch(f"{_RA}.get_ai_message_refinement", side_effect=RuntimeError("llm down")):
            note = ra._draft_connect_note(1, candidate, topic="ops")
        assert "Jane" in note and "ops" in note

    def test_blank_refinement_falls_back_to_the_template(self):
        from cqc_lem.app import run_automation as ra
        from cqc_lem.utilities.connection_targeting import CandidateSignal, score_candidate
        candidate = score_candidate([CandidateSignal(person_name="Jane Doe",
                                                     person_profile_url="https://x/in/jane-doe")],
                                    datetime(2026, 7, 25))
        with patch(f"{_RA}.get_ai_message_refinement", return_value="  "):
            assert "Jane" in ra._draft_connect_note(1, candidate)

    def test_refined_note_is_truncated_to_linkedins_limit(self):
        from cqc_lem.app import run_automation as ra
        from cqc_lem.utilities.connection_targeting import CONNECT_NOTE_LIMIT, CandidateSignal, \
            score_candidate
        candidate = score_candidate([CandidateSignal(person_name="Jane",
                                                     person_profile_url="https://x/in/jane")],
                                    datetime(2026, 7, 25))
        with patch(f"{_RA}.get_ai_message_refinement", return_value="x" * 500):
            assert len(ra._draft_connect_note(1, candidate)) == CONNECT_NOTE_LIMIT


class TestAdjacentAuthorHarvest:
    def _commenter(self, name="Guru Fan", href="https://www.linkedin.com/in/guru-fan?x=1"):
        link = MagicMock()
        link.text = name
        link.get_attribute.side_effect = lambda attr: href if attr == "href" else ""
        comment = MagicMock()
        comment.find_element.return_value = link
        return comment

    def test_harvests_commenters_with_post_context(self):
        from cqc_lem.app import run_automation as ra
        driver = MagicMock()
        with patch(f"{_RA}._comment_items_from_thread", return_value=[self._commenter()]), \
             patch(f"{_RA}.time.sleep"):
            signals = ra._harvest_post_commenters(driver, "https://x/feed/update/1", "Guru Gary",
                                                  datetime(2026, 7, 25))
        assert len(signals) == 1
        assert signals[0].person_profile_url == "https://www.linkedin.com/in/guru-fan"
        assert signals[0].context_author == "Guru Gary"
        assert signals[0].source == "adjacent_post"

    def test_skips_comments_with_no_author_link(self):
        from cqc_lem.app import run_automation as ra
        broken = MagicMock()
        broken.find_element.side_effect = Exception("no link")
        nameless = self._commenter(name="")
        with patch(f"{_RA}._comment_items_from_thread", return_value=[broken, nameless]), \
             patch(f"{_RA}.time.sleep"):
            signals = ra._harvest_post_commenters(MagicMock(), "https://x/p", "G",
                                                  datetime(2026, 7, 25))
        assert signals == []

    def test_walks_each_authors_recent_posts_and_drops_ourselves(self):
        from cqc_lem.app import run_automation as ra
        from cqc_lem.utilities.connection_targeting import CandidateSignal, SOURCE_ADJACENT_POST
        harvested = [CandidateSignal(person_name="Me Myself", person_profile_url="https://x/in/me",
                                     source=SOURCE_ADJACENT_POST),
                     CandidateSignal(person_name="Guru Fan", person_profile_url="https://x/in/fan",
                                     source=SOURCE_ADJACENT_POST)]
        with patch("cqc_lem.utilities.linkedin.scrapper.get_profile_recent_activity",
                   return_value=[{"link": "https://x/feed/update/1"},
                                 {"link": "https://x/feed/update/2"},
                                 {"link": "https://x/feed/update/3"}]), \
             patch(f"{_RA}._harvest_post_commenters", return_value=harvested) as harvest:
            signals = ra._adjacent_author_signals(MagicMock(), 1, ["https://x/in/guru-gary"],
                                                  "Me Myself", datetime(2026, 7, 25))
        assert harvest.call_count == 2  # capped at _MAX_ADJACENT_POSTS_PER_AUTHOR
        assert [s.person_name for s in signals] == ["Guru Fan", "Guru Fan"]

    def test_unreadable_author_and_post_are_skipped(self):
        from cqc_lem.app import run_automation as ra
        with patch("cqc_lem.utilities.linkedin.scrapper.get_profile_recent_activity",
                   side_effect=Exception("profile gone")):
            assert ra._adjacent_author_signals(MagicMock(), 1, ["https://x/in/a"], "Me",
                                               datetime(2026, 7, 25)) == []
        with patch("cqc_lem.utilities.linkedin.scrapper.get_profile_recent_activity",
                   return_value=[{"link": None}, {"link": "https://x/p"}]), \
             patch(f"{_RA}._harvest_post_commenters", side_effect=Exception("post gone")):
            assert ra._adjacent_author_signals(MagicMock(), 1, ["https://x/in/a"], "Me",
                                               datetime(2026, 7, 25)) == []

    def test_author_display_name_from_slug(self):
        from cqc_lem.app import run_automation as ra
        assert ra._author_display_name("https://www.linkedin.com/in/jane-doe-1a2b3c") == "Jane Doe"
        assert ra._author_display_name("not-a-profile") == ""


class TestSchedulerDispatch:
    def test_dispatches_per_active_user(self):
        from cqc_lem.app import run_scheduler as rs
        with patch(f"{_RS}._skip_if_throttled", return_value=False), \
             patch(f"{_RS}.get_active_user_ids", return_value=[1, 2]), \
             patch(f"{_RA}.scan_connection_candidates.apply_async") as dispatch:
            out = rs.auto_scan_connection_candidates()
        assert dispatch.call_count == 2
        assert "2 user(s)" in out

    def test_skips_when_throttled(self):
        from cqc_lem.app import run_scheduler as rs
        with patch(f"{_RS}._skip_if_throttled", return_value=True), \
             patch(f"{_RA}.scan_connection_candidates.apply_async") as dispatch:
            out = rs.auto_scan_connection_candidates()
        dispatch.assert_not_called()
        assert out == "Automation throttled"

    def test_beat_schedule_includes_the_scan(self):
        from cqc_lem.app.my_celery import app
        entry = app.conf.beat_schedule["scan-connection-candidates"]
        assert entry["task"] == "cqc_lem.app.run_scheduler.auto_scan_connection_candidates"

    def test_scan_runs_on_the_outreach_lane(self):
        from cqc_lem.app import run_automation as ra
        assert ra.scan_connection_candidates.queue == "se_outreach"
