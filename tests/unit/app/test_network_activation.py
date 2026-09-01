"""Network activation (issue #623).

The production audit found the whole outbound layer idle: one connection request EVER (aimed at an
existing 1st-degree connection, scored below the user's own ICP floor), no outreach funnel targets,
no scheduled DMs. These cover the four fixes — degree skip, ICP floor at file time, funnel sourcing,
and the reply check that unblocks the nurture queue — plus the logging that makes a silent early
exit impossible.
"""

import logging
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit

_OUT = "cqc_lem.app.engagement.outreach"
# The connect rail moved to its own module (#1154); patches for it must bind THERE, because that
# is the module whose globals the invite code reads.
_INV = "cqc_lem.app.engagement.invites"
_RS = "cqc_lem.app.run_scheduler"


def _prefs(**over):
    prefs = {"connection_targeting_mode": "suggest", "connection_request_mode": "auto_approve",
             "max_invites_per_day": 10, "min_connection_icp_score": 55,
             "connection_target_authors": [], "focus_topics": ["revenue operations"]}
    prefs.update(over)
    return prefs


def _engager(name="Jane Doe", slug="jane-doe", degree=None):
    return {"person_name": name, "person_profile_url": f"https://www.linkedin.com/in/{slug}",
            "connection_degree": degree, "occurred_at": datetime(2026, 7, 25, 9, 0, 0)}


def _scan_connections(prefs=None, engagers=None, facts=None):
    from cqc_lem.app.engagement import outreach as ra
    patches = {
        "get_engagement_preferences": prefs if prefs is not None else _prefs(),
        "count_invites_sent_today": 0,
        "count_open_connection_requests": 0,
        "get_engager_candidates": [_engager()] if engagers is None else engagers,
        "get_requested_person_keys": set(),
        "get_profile_facts": facts or {},
    }
    with patch.multiple(_OUT, **{k: MagicMock(return_value=v) for k, v in patches.items()}), \
         patch(f"{_OUT}.get_ai_message_refinement", return_value="Refined note"), \
         patch(f"{_OUT}.insert_connection_request", return_value=7) as insert:
        return ra.scan_connection_candidates.run(user_id=1), insert


class TestFirstDegreeIsNeverInvited:
    def test_an_already_connected_engager_is_not_filed(self):
        out, insert = _scan_connections(engagers=[_engager(degree="1st")])
        insert.assert_not_called()
        assert "No new connection candidates" in out

    def test_a_second_degree_engager_is_still_filed(self):
        _out, insert = _scan_connections(engagers=[_engager(degree="2nd")])
        assert insert.call_count == 1
        assert "2nd-degree" in insert.call_args.kwargs["reasons"]

    def test_an_unbadged_engager_is_still_filed(self):
        # A missing badge is unknown, not connected — failing closed here would stop all outreach.
        _out, insert = _scan_connections(engagers=[_engager(degree=None)])
        assert insert.call_count == 1

    def test_the_send_path_aborts_on_a_first_degree_profile(self):
        from cqc_lem.app.engagement import invites as ra
        from cqc_lem.utilities.db import ALREADY_CONNECTED_MESSAGE
        badge = MagicMock()
        badge.text = "1st"
        driver = MagicMock()
        driver.find_elements.return_value = [badge]
        with patch(f"{_INV}.get_user_password_pair_by_id", return_value=("e@x", "pw")), \
             patch(f"{_INV}.get_driver_wait_pair", return_value=(driver, MagicMock())), \
             patch(f"{_INV}.login_to_linkedin"), \
             patch(f"{_INV}.click_element_wait_retry") as click, \
             patch(f"{_INV}.insert_new_log") as log, \
             patch(f"{_INV}.quit_gracefully"):
            sent, reason = ra.invite_to_connect_now(1, "https://x/in/jane")
        assert sent is False and reason == ALREADY_CONNECTED_MESSAGE
        click.assert_not_called()  # never hunts for a Connect button that cannot exist
        log.assert_called_once()

    def test_an_unreadable_badge_does_not_block_the_invite(self):
        from cqc_lem.app.engagement import invites as ra
        driver = MagicMock()
        driver.find_elements.side_effect = Exception("stale element")
        assert ra._profile_is_first_degree(driver) is False


class TestIcpFloorAtFileTime:
    def test_a_scored_candidate_below_the_floor_is_never_filed(self):
        facts = {"https://www.linkedin.com/in/jane-doe": {"job_title": "intern",
                                                          "industry": "retail"}}
        out, insert = _scan_connections(facts=facts, prefs=_prefs(min_connection_icp_score=90))
        insert.assert_not_called()
        assert "No new connection candidates" in out

    def test_an_unscored_candidate_files_with_no_icp_score(self):
        # The one production row stored ICP_UNKNOWN (50) against a floor of 55, so it read as
        # below the user's own threshold. No facts now means no number at all.
        _out, insert = _scan_connections()
        assert insert.call_args.kwargs["icp_score"] is None
        assert "ICP fit unknown" in insert.call_args.kwargs["reasons"]

    def test_a_scored_candidate_above_the_floor_keeps_its_score(self):
        facts = {"https://www.linkedin.com/in/jane-doe": {
            "job_title": "VP Revenue Operations", "company_name": "Acme",
            "industry": "revenue operations"}}
        _out, insert = _scan_connections(facts=facts)
        assert insert.call_args.kwargs["icp_score"] >= 55


class TestFailureReasonIsRecorded:
    def test_a_failed_send_stores_why(self):
        # Issue #1814 — a real attempt's reason is recorded via record_connection_request_attempt,
        # which decides terminal-vs-retry off the attempt ceiling (covered in
        # tests/unit/app/test_connection_requests.py).
        from cqc_lem.app.engagement import invites as ra
        req = {"id": 3, "user_id": 1, "recipient_profile_url": "https://x/in/jane",
               "message": "hi", "status": "approved"}
        with patch("cqc_lem.utilities.db.get_connection_request", return_value=req), \
             patch("cqc_lem.utilities.db.count_invites_sent_today", return_value=0), \
             patch(f"{_INV}.get_engagement_preferences", return_value={"max_invites_per_day": 10}), \
             patch(f"{_INV}.invite_to_connect_now", return_value=(False, "Already connected")), \
             patch("cqc_lem.utilities.db.record_connection_request_attempt", return_value=(False, 1)) as rec:
            ra.send_connection_request(3)
        # `terminal=False`: only a PROVEN-unreachable target skips the ceiling (issue #1813).
        rec.assert_called_once_with(3, "Already connected", terminal=False)


class TestReplyCheckUnblocksNurture:
    """The chain that left scheduled_dms empty since V53: stock DM templates are step-0 only, so
    enqueue_next_followup found no step 1, queued nothing, and process_user_followups never ran.
    """

    def test_an_appreciation_dm_now_leaves_a_reply_check_behind(self, monkeypatch):
        from cqc_lem.app.engagement import outreach as ra
        monkeypatch.delenv("DM_NURTURE_ENABLED", raising=False)
        with patch(f"{_OUT}.has_appreciation_touch", return_value=False), \
             patch(f"{_OUT}.claim_appreciation_touch", return_value=True), \
             patch(f"{_OUT}.build_dm_from_template", return_value="hi Jane"), \
             patch(f"{_OUT}.send_private_dm") as dm, \
             patch(f"{_OUT}.get_dm_template", return_value=None), \
             patch(f"{_OUT}.enqueue_followup") as enq:
            sent = ra._dispatch_appreciation_dms(1, MagicMock(), "connection_accepted",
                                                 {"https://x/in/jane": "Jane Doe • 1st"})
        assert sent == 1
        dm.apply_async.assert_called_once()
        enq.assert_called_once()  # the thread stays on the sequencer instead of going cold

    def test_the_recipient_name_is_cleaned_before_it_reaches_the_template(self):
        from cqc_lem.app.engagement import outreach as ra
        with patch(f"{_OUT}.has_appreciation_touch", return_value=False), \
             patch(f"{_OUT}.claim_appreciation_touch", return_value=True), \
             patch(f"{_OUT}.build_dm_from_template", return_value="hi") as build, \
             patch(f"{_OUT}.send_private_dm"), \
             patch(f"{_OUT}.enqueue_next_followup"):
            ra._dispatch_appreciation_dms(1, MagicMock(), "connection_accepted",
                                          {"https://x/in/h": "Harshal Karanpuriya Verified Profile 1st"})
        assert build.call_args[0][2] == "Harshal"

    def test_a_missing_template_is_logged_not_swallowed(self, caplog):
        from cqc_lem.app.engagement import outreach as ra
        with patch(f"{_OUT}.has_appreciation_touch", return_value=False), \
             patch(f"{_OUT}.claim_appreciation_touch") as claim, \
             patch(f"{_OUT}.build_dm_from_template", return_value=None), \
             patch(f"{_OUT}.send_private_dm") as dm:
            sent = ra._dispatch_appreciation_dms(1, MagicMock(), "collaboration",
                                                 {"https://x/in/jane": "Jane"})
        assert sent == 0
        dm.apply_async.assert_not_called()
        # #968: the ledger claim comes AFTER the message is written, so a template gap does not
        # burn this person's one shot at ever being thanked.
        claim.assert_not_called()
        assert "No 'collaboration' DM template" in caplog.text

    def test_an_empty_followup_run_says_so(self, caplog):
        from cqc_lem.app.engagement import outreach as ra
        with patch(f"{_OUT}.get_due_followups", return_value=[]):
            out = ra.process_user_followups.run(user_id=1)
        assert out == "No due follow-ups"
        assert "no nurture draft" in caplog.text

    def test_nurture_says_so_when_it_is_switched_off(self, monkeypatch, caplog):
        from cqc_lem.app.engagement import outreach as ra
        monkeypatch.setenv("DM_NURTURE_ENABLED", "false")
        assert ra._nurture_after_reply(1, {"profile_url": "https://x/in/j"}, "sure, tell me more",
                                       MagicMock()) is None
        assert "DM nurture is disabled" in caplog.text


def _roster(**over):
    row = {"profile_url": "https://www.linkedin.com/in/guru-gary", "name": "Guru Gary • 1st",
           "category": "creator", "active": True}
    row.update(over)
    return row


def _scan_funnel(prefs=None, roster=None, engagers=None, open_targets=0, requested=None,
                 existing=None, max_new=None, commenters=None, activity=None, profile_exc=None,
                 activity_exc=None):
    from cqc_lem.app.engagement import outreach as ra
    with patch(f"{_OUT}.get_engagement_preferences",
               return_value=prefs if prefs is not None else _prefs()), \
         patch(f"{_OUT}.count_open_outreach_targets", return_value=open_targets), \
         patch(f"{_OUT}.get_engagement_targets", return_value=roster if roster is not None else []), \
         patch(f"{_OUT}.get_engager_candidates", return_value=engagers or []), \
         patch(f"{_OUT}.get_requested_person_keys", return_value=requested or set()), \
         patch(f"{_OUT}.get_outreach_target_by_url", return_value=existing), \
         patch(f"{_OUT}.get_or_create_profile_synthesis", return_value="voice"), \
         patch(f"{_OUT}.generate_ai_response", return_value="A grounded comment."), \
         patch(f"{_OUT}.get_ai_message_refinement", return_value="Refined note"), \
         patch(f"{_OUT}.insert_outreach_target", return_value=11) as insert, \
         patch(f"{_OUT}._harvest_post_commenters", return_value=commenters or []), \
         patch("cqc_lem.utilities.linkedin.scrapper.get_profile_recent_activity",
               side_effect=activity_exc,
               return_value=activity if activity is not None
               else [{"link": "https://x/feed/update/1", "text": "Post body"}]), \
         patch(f"{_OUT}.get_current_profile") as profile, \
         patch(f"{_OUT}.quit_gracefully"):
        if profile_exc is not None:
            profile.side_effect = profile_exc
        else:
            profile.return_value = (MagicMock(), MagicMock(), "me@x.com",
                                    MagicMock(full_name="Me Myself"))
        return ra.scan_outreach_funnel_targets.run(user_id=1, max_new=max_new), insert


class TestOutreachFunnelSourcing:
    def test_a_roster_author_becomes_a_comment_stage_draft(self):
        from cqc_lem.utilities.db import OutreachStage, OutreachStatus
        out, insert = _scan_funnel(roster=[_roster()])
        assert insert.call_count == 1
        kwargs = insert.call_args.kwargs
        assert kwargs["stage"] == OutreachStage.COMMENT
        assert kwargs["context_url"] == "https://x/feed/update/1"
        assert kwargs["draft_text"] == "A grounded comment."
        assert kwargs["target_name"] == "Guru Gary"  # badge text stripped
        assert kwargs["status"] == OutreachStatus.APPROVED  # auto_approve mode
        assert "Filed 1" in out

    def test_pre_review_mode_files_a_draft_for_approval(self):
        from cqc_lem.utilities.db import OutreachStatus
        _out, insert = _scan_funnel(roster=[_roster()],
                                    prefs=_prefs(connection_request_mode="pre_review"))
        assert insert.call_args.kwargs["status"] == OutreachStatus.PENDING

    def test_commenters_on_a_roster_post_are_sourced_with_that_post(self):
        from cqc_lem.utilities.connection_targeting import CandidateSignal
        commenter = CandidateSignal(person_name="Guru Fan",
                                    person_profile_url="https://www.linkedin.com/in/guru-fan")
        _out, insert = _scan_funnel(roster=[_roster()], commenters=[commenter])
        urls = [c.args[1] for c in insert.call_args_list]
        assert "https://www.linkedin.com/in/guru-fan" in urls

    def test_post_engagers_start_at_the_connect_stage(self):
        from cqc_lem.utilities.db import OutreachStage
        _out, insert = _scan_funnel(engagers=[_engager(degree="2nd")])
        assert insert.call_args.kwargs["stage"] == OutreachStage.CONNECT
        assert insert.call_args.kwargs["context_url"] is None

    def test_an_already_connected_engager_is_not_sourced(self):
        out, insert = _scan_funnel(engagers=[_engager(degree="1st")])
        insert.assert_not_called()
        assert "No outreach funnel prospects" in out

    def test_someone_already_in_connection_requests_is_skipped(self):
        out, insert = _scan_funnel(engagers=[_engager(degree="2nd")],
                                   requested={"in:jane-doe"})
        insert.assert_not_called()
        assert "Filed 0" in out

    def test_a_target_already_in_the_funnel_is_skipped(self):
        _out, insert = _scan_funnel(engagers=[_engager(degree="2nd")], existing={"id": 1})
        insert.assert_not_called()

    def test_targeting_off_sources_nothing(self):
        out, insert = _scan_funnel(prefs=_prefs(connection_targeting_mode="off"),
                                   engagers=[_engager(degree="2nd")])
        insert.assert_not_called()
        assert "off" in out

    def test_a_deep_approval_backlog_stops_sourcing(self, caplog):
        caplog.set_level(logging.DEBUG, logger="cqc-lem")
        out, insert = _scan_funnel(engagers=[_engager(degree="2nd")], open_targets=25)
        insert.assert_not_called()
        assert "backlog full" in out
        assert "already waiting for approval" in caplog.text

    def test_the_per_scan_budget_is_respected(self):
        engagers = [_engager(name=f"P{i}", slug=f"p{i}", degree="2nd") for i in range(8)]
        _out, insert = _scan_funnel(engagers=engagers, max_new=2)
        assert insert.call_count == 2

    def test_an_empty_roster_and_no_engagers_is_logged(self, caplog):
        caplog.set_level(logging.DEBUG, logger="cqc-lem")
        out, insert = _scan_funnel()
        insert.assert_not_called()
        assert "No outreach funnel prospects" in out
        assert "only source from post engagers" in caplog.text

    def test_roster_scraping_failure_still_sources_engagers(self):
        from cqc_lem.app.engagement import outreach as ra
        with patch(f"{_OUT}.get_engagement_preferences", return_value=_prefs()), \
             patch(f"{_OUT}.count_open_outreach_targets", return_value=0), \
             patch(f"{_OUT}.get_engagement_targets", return_value=[_roster()]), \
             patch(f"{_OUT}.get_engager_candidates", return_value=[_engager(degree="2nd")]), \
             patch(f"{_OUT}.get_requested_person_keys", return_value=set()), \
             patch(f"{_OUT}.get_outreach_target_by_url", return_value=None), \
             patch(f"{_OUT}.get_ai_message_refinement", return_value="Refined note"), \
             patch(f"{_OUT}.get_current_profile", side_effect=RuntimeError("no session")), \
             patch(f"{_OUT}.insert_outreach_target", return_value=11) as insert:
            out = ra.scan_outreach_funnel_targets.run(user_id=1)
        assert insert.call_count == 1 and "Filed 1" in out

    def test_a_roster_sourcing_failure_still_warns(self):
        """The degraded path is a real failure — dropping the empty outcomes must not silence it."""
        with patch(f"{_OUT}.log_warning") as warn:
            _out, insert = _scan_funnel(roster=[_roster()], engagers=[_engager(degree="2nd")],
                                        profile_exc=RuntimeError("no session"))
        assert insert.call_count == 1
        assert any("Roster sourcing for the outreach funnel failed" in str(call.args[0])
                   for call in warn.call_args_list)

    def test_a_gated_comment_draft_leaves_the_text_to_the_operator(self):
        from cqc_lem.app.engagement import outreach as ra
        # generate_ai_response returns None when the #617 quality gate rejects every attempt.
        with patch(f"{_OUT}.generate_ai_response", return_value=None):
            assert ra._draft_funnel_comment(1, {"context_text": "Post body"}, MagicMock()) == ""
        with patch(f"{_OUT}.generate_ai_response", side_effect=RuntimeError("llm down")):
            assert ra._draft_funnel_comment(1, {"context_text": "Post body"}, MagicMock()) == ""
        assert ra._draft_funnel_comment(1, {"context_text": ""}, MagicMock()) == ""


class TestOutreachFunnelScanEvent:
    """issue #1816: `outreach_funnel_scan` fires on EVERY exit, zeros included — the table had
    ZERO rows in production and every early exit only ever logged at DEBUG, so a broken scan and a
    healthy quiet one were indistinguishable from outside.
    """

    def test_targeting_off_emits_off_status(self):
        with patch(f"{_OUT}.track_outreach_funnel_scan") as track:
            _scan_funnel(prefs=_prefs(connection_targeting_mode="off"))
        report = track.call_args.args[1]
        assert report["status"] == "off"

    def test_backlog_full_emits_budget_and_status(self):
        with patch(f"{_OUT}.track_outreach_funnel_scan") as track:
            _scan_funnel(open_targets=25)
        report = track.call_args.args[1]
        assert report["status"] == "backlog_full"
        assert report["budget"] == 0

    def test_no_prospects_emits_the_stage_the_zero_happened_at(self):
        with patch(f"{_OUT}.track_outreach_funnel_scan") as track:
            _scan_funnel(roster=[_roster()], activity=[])
        report = track.call_args.args[1]
        assert report["status"] == "no_prospects"
        assert report["roster_authors_walked"] == 1
        assert report["authors_with_a_post"] == 0
        assert report["prospects"] == 0

    def test_a_filed_run_emits_the_full_funnel(self):
        with patch(f"{_OUT}.track_outreach_funnel_scan") as track:
            _scan_funnel(roster=[_roster()])
        report = track.call_args.args[1]
        assert report["status"] == "ok"
        assert report["roster_authors_walked"] == 1
        assert report["authors_with_a_post"] == 1
        assert report["prospects"] == 1
        assert report["filed"] == 1

    def test_engager_candidates_counted_before_the_first_degree_filter(self):
        # #1091: an all-1st-degree pool is an audience fact, not a sourcing failure — the pre-filter
        # count has to be visible separately from how many became prospects.
        with patch(f"{_OUT}.track_outreach_funnel_scan") as track:
            _scan_funnel(engagers=[_engager(degree="1st")])
        report = track.call_args.args[1]
        assert report["engager_candidates"] == 1
        assert report["prospects"] == 0


class TestEmptyFunnelScanIsNotAWarning:
    """Filing nothing is this scan's resting state, so none of its three empty outcomes may warn —
    a daily beat that warns escalates to ERROR and files a defect for working behaviour (#995).
    """

    @pytest.mark.parametrize("kwargs, marker", [
        ({"open_targets": 25}, "are already waiting for approval"),
        ({"engagers": [_engager(degree="2nd")]}, "No active engagement-roster targets"),
        ({}, "the engagement roster produced no"),
        # A roster author who hasn't posted lately is the common case, not a degraded one (#987).
        ({"roster": [_roster()], "activity": []}, "has no recent post to comment on"),
    ])
    def test_empty_outcome_logs_debug_not_warning(self, kwargs, marker):
        with patch(f"{_OUT}.log_warning") as warn, patch(f"{_OUT}.log_debug") as debug:
            _out, _insert = _scan_funnel(**kwargs)
        warn.assert_not_called()
        assert any(marker in str(call.args[0]) for call in debug.call_args_list)

    def test_a_quiet_roster_author_is_skipped_without_filing_a_target(self):
        """The skip itself must still hold: no post means nothing to comment on, so no draft."""
        out, insert = _scan_funnel(roster=[_roster()], activity=[])
        insert.assert_not_called()
        assert "No outreach funnel prospects" in out

    def test_an_unreadable_roster_author_still_warns(self):
        """Silencing the quiet author must not silence the profile that could not be read at all."""
        with patch(f"{_OUT}.log_warning") as warn:
            _scan_funnel(roster=[_roster()], activity_exc=RuntimeError("profile gone"))
        assert any("Could not read a roster author's recent activity" in str(call.args[0])
                   for call in warn.call_args_list)


class TestSourcingDispatch:
    def test_dispatches_per_active_user(self):
        from cqc_lem.app import run_scheduler as rs
        with patch(f"{_RS}._skip_if_throttled", return_value=False), \
             patch(f"{_RS}.get_active_user_ids", return_value=[1, 2]), \
             patch(f"{_OUT}.scan_outreach_funnel_targets.apply_async") as dispatch:
            out = rs.auto_scan_outreach_funnel_targets()
        assert dispatch.call_count == 2
        assert "2 user(s)" in out

    def test_skips_when_throttled(self):
        from cqc_lem.app import run_scheduler as rs
        with patch(f"{_RS}._skip_if_throttled", return_value=True), \
             patch(f"{_OUT}.scan_outreach_funnel_targets.apply_async") as dispatch:
            out = rs.auto_scan_outreach_funnel_targets()
        dispatch.assert_not_called()
        assert out == "Automation throttled"

    def test_beat_schedule_includes_the_scan(self):
        from cqc_lem.app.my_celery import app
        entry = app.conf.beat_schedule["scan-outreach-funnel-targets"]
        assert entry["task"] == "cqc_lem.app.run_scheduler.auto_scan_outreach_funnel_targets"

    def test_sourcing_runs_on_the_outreach_lane(self):
        from cqc_lem.app.engagement import outreach as ra
        assert ra.scan_outreach_funnel_targets.queue == "se_outreach"
