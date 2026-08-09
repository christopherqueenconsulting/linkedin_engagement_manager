"""Unit tests for inbound hot-lead routing — issue #483. Covers the dedup key, the flag/draft
helper, the DM-body read that rides the existing follow-up thread open, and the approval-gated
responder (DM + reply channels). Selenium DOM targeting itself is validated on a supervised run.
"""

from unittest.mock import MagicMock, patch

import pytest

from cqc_lem.utilities.linkedin.message_thread import ThreadState

pytestmark = pytest.mark.unit

_RA = "cqc_lem.app.run_automation"
# The own-post reply rail moved to `app.engagement.posting` (#1154); the lead RESPONDER
# (`_send_lead_response`, `_reply_to_person_on_post`) stayed with the DM cluster, so both
# spellings are live here and each test uses the one its own code reads.
_POST = "cqc_lem.app.engagement.posting"
# Flagging moved down to `utilities/lead_scoring.py` (#1154) — beside `profile_slug`, of which
# `run_automation._profile_slug` was a byte-identical copy. It took its DB / classifier / LLM
# imports with it, so those are the bindings it reads.
_LS = "cqc_lem.utilities.lead_scoring"


@pytest.fixture(autouse=True)
def _no_sleep():
    with patch(f"{_RA}.time.sleep"):
        yield


def _fn(name):
    import importlib
    return getattr(importlib.import_module(_RA), name)


def _post(name):
    """Same, for the half that moved to `app.engagement.posting`."""
    import importlib
    return getattr(importlib.import_module(_POST), name)


def _ls(name):
    """Same, for the half that now lives in `utilities.lead_scoring`."""
    import importlib
    return getattr(importlib.import_module(_LS), name)


class TestProfileSlug:
    """The one surviving implementation.

    `run_automation._profile_slug` was deleted in #1154 as a byte-identical duplicate, so these
    assertions follow `lead_scoring.profile_slug` rather than being deleted with the copy.
    """

    def test_extracts_and_lowercases(self):
        assert _ls("profile_slug")("https://www.linkedin.com/in/Jane-Doe/") == "jane-doe"

    def test_strips_query_string(self):
        assert _ls("profile_slug")("https://x/in/jane?trk=abc") == "jane"

    def test_missing_returns_empty(self):
        assert _ls("profile_slug")("https://www.linkedin.com/company/acme") == ""
        assert _ls("profile_slug")(None) == ""


class TestLeadThreadKey:
    def test_same_person_same_thread_is_one_key(self):
        f = _ls("_lead_thread_key")
        a = f("post_comment", "post:7", "https://www.linkedin.com/in/jane/")
        b = f("post_comment", "post:7", "https://x/in/JANE?trk=1")
        assert a == b

    def test_different_threads_differ(self):
        f = _ls("_lead_thread_key")
        assert f("post_comment", "post:7", "https://x/in/jane") != \
               f("post_comment", "post:8", "https://x/in/jane")

    def test_falls_back_to_name_when_no_profile_url(self):
        f = _ls("_lead_thread_key")
        key = f("dm", "thread", "", "Jane Doe")
        assert key.startswith("lead:dm:thread:") and len(key) > len("lead:dm:thread:")
        assert key == f("dm", "thread", "", " jane doe ")

    def test_key_ignores_message_text_entirely(self):
        f = _ls("_lead_thread_key")
        assert f("comment_reply", "feedurn://urn:li:activity:1", "https://x/in/jane") == \
               f("comment_reply", "feedurn://urn:li:activity:1", "https://x/in/jane")


class TestFlagLeadSignal:
    def _patches(self, **over):
        base = {
            "has_lead_signal": False,
            "detect": {"is_lead": True, "score": 55, "matched": ["pricing"], "method": "heuristic"},
            "draft": "Happy to help — what's the timeline?",
            "insert": 42,
        }
        base.update(over)
        return base

    def _run(self, text="How much do you charge?", **over):
        p = self._patches(**over)
        with patch(f"{_LS}.has_lead_signal", return_value=p["has_lead_signal"]) as has, \
             patch(f"{_LS}.detect_lead_signals", return_value=p["detect"]) as det, \
             patch(f"{_LS}.generate_lead_response", return_value=p["draft"]) as gen, \
             patch(f"{_LS}.insert_lead_signal", return_value=p["insert"]) as ins, \
             patch(f"{_LS}.log_info"), patch(f"{_LS}.log_warning") as warn:
            from cqc_lem.utilities.db import LeadSignalSource
            result = _ls("_flag_lead_signal")(1, text, LeadSignalSource.POST_COMMENT, "post:7",
                                              person_name="Jane", person_profile_url="https://x/in/jane",
                                              post_id=7, context_url="https://x/post/7",
                                              my_profile=MagicMock())
        return result, {"has": has, "detect": det, "gen": gen, "insert": ins, "warn": warn}

    def test_flags_and_stores_the_draft(self):
        result, m = self._run()
        assert result == 42
        kwargs = m["insert"].call_args.kwargs
        assert kwargs["draft_response"] == "Happy to help — what's the timeline?"
        assert kwargs["score"] == 55
        assert kwargs["matched_signals"] == "pricing"
        assert kwargs["post_id"] == 7

    def test_no_intent_means_no_row_and_no_draft(self):
        result, m = self._run(detect={"is_lead": False, "score": 0, "matched": [], "method": "heuristic"})
        assert result is None
        m["gen"].assert_not_called()
        m["insert"].assert_not_called()

    def test_already_flagged_thread_short_circuits_before_classifying(self):
        result, m = self._run(has_lead_signal=True)
        assert result is None
        m["detect"].assert_not_called()
        m["gen"].assert_not_called()

    def test_blank_text_is_ignored(self):
        result, m = self._run(text="   ")
        assert result is None
        m["has"].assert_not_called()

    def test_draft_failure_still_queues_the_signal(self):
        p = self._patches()
        with patch(f"{_LS}.has_lead_signal", return_value=False), \
             patch(f"{_LS}.detect_lead_signals", return_value=p["detect"]), \
             patch(f"{_LS}.generate_lead_response", side_effect=RuntimeError("llm down")), \
             patch(f"{_LS}.insert_lead_signal", return_value=9) as ins, \
             patch(f"{_LS}.log_info"), patch(f"{_LS}.log_warning") as warn:
            from cqc_lem.utilities.db import LeadSignalSource
            result = _ls("_flag_lead_signal")(1, "how much?", LeadSignalSource.DM, "thread",
                                              my_profile=MagicMock())
        assert result == 9
        assert ins.call_args.kwargs["draft_response"] is None
        warn.assert_called_once()

    def test_detection_errors_are_non_fatal(self):
        with patch(f"{_LS}.has_lead_signal", side_effect=RuntimeError("db down")), \
             patch(f"{_LS}.log_warning") as warn:
            from cqc_lem.utilities.db import LeadSignalSource
            assert _ls("_flag_lead_signal")(1, "how much?", LeadSignalSource.DM, "t") is None
        warn.assert_called_once()


class TestLastInboundMessage:
    def test_reads_the_newest_bubble(self):
        driver = MagicMock()
        driver.execute_script.return_value = "  Do you offer this?  "
        assert _fn("_last_inbound_message")(driver) == "Do you offer this?"

    def test_no_match_returns_empty(self):
        driver = MagicMock()
        driver.execute_script.return_value = None
        assert _fn("_last_inbound_message")(driver) == ""

    def test_script_error_is_non_fatal(self):
        # The read now lives in the shared #731 thread module, so that is where the warning comes from.
        driver = MagicMock()
        driver.execute_script.side_effect = RuntimeError("stale")
        with patch("cqc_lem.utilities.linkedin.message_thread.log_warning") as warn:
            assert _fn("_last_inbound_message")(driver) == ""
        warn.assert_called_once()


class TestReplyToPersonOnPost:
    def _driver_with(self, authors):
        driver, wait = MagicMock(), MagicMock()
        boxes = [MagicMock(name=f"tb{i}") for i in range(len(authors))]
        driver.find_elements.side_effect = lambda by, sel: (boxes if "expandable-text-box" in sel else [])
        return driver, wait, boxes

    def test_replies_under_the_right_persons_comment(self):
        driver, wait, boxes = self._driver_with(["https://x/in/bob", "https://x/in/jane"])
        conts = [MagicMock(name="c0"), MagicMock(name="c1")]
        with patch(f"{_RA}._comment_container", side_effect=conts), \
             patch(f"{_RA}._comment_header_author", side_effect=["https://x/in/bob", "https://x/in/jane"]), \
             patch(f"{_RA}._reply_under_comment_inline", return_value=True) as rep:
            ok = _fn("_reply_to_person_on_post")(driver, wait, "https://x/post/1",
                                                 "https://x/in/jane", "hi", user_id=1)
        assert ok is True
        assert rep.call_args.args[2] is conts[1]

    def test_person_not_found_returns_false(self):
        driver, wait, _ = self._driver_with(["https://x/in/bob"])
        with patch(f"{_RA}._comment_container", return_value=MagicMock()), \
             patch(f"{_RA}._comment_header_author", return_value="https://x/in/bob"), \
             patch(f"{_RA}._reply_under_comment_inline") as rep, \
             patch(f"{_RA}.log_warning") as warn:
            ok = _fn("_reply_to_person_on_post")(driver, wait, "https://x/post/1",
                                                 "https://x/in/jane", "hi")
        assert ok is False
        rep.assert_not_called()
        warn.assert_called_once()

    def test_no_slug_never_navigates(self):
        driver, wait = MagicMock(), MagicMock()
        with patch(f"{_RA}.log_warning") as warn:
            assert _fn("_reply_to_person_on_post")(driver, wait, "https://x/post/1", "", "hi") is False
        driver.get.assert_not_called()
        warn.assert_called_once()


class TestSendLeadResponse:
    _BASE = {"id": 3, "user_id": 1, "status": "approved", "channel": "dm",
             "draft_response": "Happy to help!", "person_profile_url": "https://x/in/jane",
             "context_url": "https://x/post/1", "post_id": None}

    def _signal(self, **over):
        s = dict(self._BASE)
        s.update(over)
        return s

    def test_dm_channel_sends_and_marks_sent(self):
        from cqc_lem.utilities.db import LeadSignalStatus
        with patch(f"{_RA}.get_lead_signal", return_value=self._signal()), \
             patch(f"{_RA}.send_dm_now", return_value=True) as send, \
             patch(f"{_RA}.update_lead_signal") as upd:
            result = _fn("_send_lead_response")(3)
        send.assert_called_once_with(1, "https://x/in/jane", "Happy to help!")
        assert upd.call_args.kwargs["status"] == LeadSignalStatus.SENT
        assert "sent" in result

    def test_failed_dm_marks_failed(self):
        from cqc_lem.utilities.db import LeadSignalStatus
        with patch(f"{_RA}.get_lead_signal", return_value=self._signal()), \
             patch(f"{_RA}.send_dm_now", return_value=False), \
             patch(f"{_RA}.update_lead_signal") as upd:
            _fn("_send_lead_response")(3)
        assert upd.call_args.kwargs["status"] == LeadSignalStatus.FAILED

    def test_reply_channel_posts_under_their_comment(self):
        from cqc_lem.utilities.db import LeadSignalStatus
        with patch(f"{_RA}.get_lead_signal", return_value=self._signal(channel="reply")), \
             patch(f"{_RA}.get_current_profile",
                   return_value=(MagicMock(), MagicMock(), "e", MagicMock())), \
             patch(f"{_RA}._reply_to_person_on_post", return_value=True) as rep, \
             patch(f"{_RA}.insert_new_log") as log, \
             patch(f"{_RA}.update_lead_signal") as upd, \
             patch(f"{_RA}.quit_gracefully") as quit_:
            result = _fn("_send_lead_response")(3)
        rep.assert_called_once()
        assert upd.call_args.kwargs["status"] == LeadSignalStatus.SENT
        log.assert_called_once()
        quit_.assert_called_once()
        assert "sent" in result

    def test_unapproved_signal_is_never_sent(self):
        with patch(f"{_RA}.get_lead_signal", return_value=self._signal(status="new")), \
             patch(f"{_RA}.send_dm_now") as send, \
             patch(f"{_RA}.update_lead_signal") as upd:
            result = _fn("_send_lead_response")(3)
        send.assert_not_called()
        upd.assert_not_called()
        assert "not sendable" in result

    def test_missing_signal(self):
        with patch(f"{_RA}.get_lead_signal", return_value=None):
            assert "not found" in _fn("_send_lead_response")(3)

    def test_empty_draft_fails_instead_of_sending_nothing(self):
        from cqc_lem.utilities.db import LeadSignalStatus
        with patch(f"{_RA}.get_lead_signal", return_value=self._signal(draft_response="  ")), \
             patch(f"{_RA}.send_dm_now") as send, \
             patch(f"{_RA}.update_lead_signal") as upd:
            _fn("_send_lead_response")(3)
        send.assert_not_called()
        assert upd.call_args.kwargs["status"] == LeadSignalStatus.FAILED

    def test_reply_without_a_post_url_fails(self):
        from cqc_lem.utilities.db import LeadSignalStatus
        with patch(f"{_RA}.get_lead_signal", return_value=self._signal(channel="reply", context_url=None)), \
             patch(f"{_RA}.get_current_profile") as gcp, \
             patch(f"{_RA}.update_lead_signal") as upd:
            _fn("_send_lead_response")(3)
        gcp.assert_not_called()
        assert upd.call_args.kwargs["status"] == LeadSignalStatus.FAILED

    def test_rate_limited_session_leaves_it_approved_for_a_retry(self):
        from cqc_lem.utilities.linkedin.rate_limit import LinkedInRateLimited
        with patch(f"{_RA}.get_lead_signal", return_value=self._signal(channel="reply")), \
             patch(f"{_RA}.get_current_profile", side_effect=LinkedInRateLimited("429")), \
             patch(f"{_RA}.update_lead_signal") as upd, \
             patch(f"{_RA}.log_warning") as warn:
            result = _fn("_send_lead_response")(3)
        upd.assert_not_called()  # still APPROVED, so a later run retries it
        warn.assert_called_once()
        assert "rate limited" in result.lower()

    def test_session_failure_reports_without_marking_sent(self):
        with patch(f"{_RA}.get_lead_signal", return_value=self._signal(channel="reply")), \
             patch(f"{_RA}.get_current_profile", side_effect=RuntimeError("no driver")), \
             patch(f"{_RA}.update_lead_signal") as upd, \
             patch(f"{_RA}.log_error"):
            result = _fn("_send_lead_response")(3)
        upd.assert_not_called()
        assert "Failed to start" in result


class TestReadPathWiring:
    def test_dm_followup_flags_their_reply(self):
        """A follow-up that detects a reply reads that reply once and checks it for intent (#483)."""
        from cqc_lem.app.run_automation import process_user_followups
        from cqc_lem.utilities.db import LeadSignalSource
        due = [{"id": 1, "user_id": 1, "profile_url": "https://x/in/jane", "first_name": "Jane",
                "event_type": "manual", "next_step": 1}]
        with patch(f"{_RA}.get_due_followups", return_value=due), \
             patch(f"{_RA}.get_current_profile", return_value=(MagicMock(), MagicMock(), "e", MagicMock())), \
             patch(f"{_RA}.get_engagement_preferences", return_value={}), \
             patch(f"{_RA}.get_or_create_profile_synthesis", return_value="synth"), \
             patch(f"{_RA}.check_dm_replied", return_value=ThreadState.REPLIED), \
             patch(f"{_RA}._last_inbound_message", return_value="How much for the full program?"), \
             patch(f"{_RA}._flag_lead_signal", return_value=5) as flag, \
             patch(f"{_RA}.stop_followups_for_profile"), \
             patch(f"{_RA}.mark_followup"), \
             patch(f"{_RA}.quit_gracefully"):
            process_user_followups.run(user_id=1)
        flag.assert_called_once()
        assert flag.call_args.args[1] == "How much for the full program?"
        assert flag.call_args.args[2] == LeadSignalSource.DM

    def test_own_post_comment_flags_a_lead(self):
        """A comment on our own post is classified before we decide whether to reply to it."""
        comment = MagicMock()
        comment.find_elements.return_value = []          # no text box -> falls back to .text
        comment.text = "Do you offer this for agencies?"
        link = MagicMock()
        link.text = "Jane Doe"
        link.get_attribute.return_value = "https://www.linkedin.com/in/jane?trk=x"
        comment.find_element.return_value = link
        profile = MagicMock()
        profile.profile_url = "https://www.linkedin.com/in/me/"
        profile.full_name = "Me Myself"
        with patch(f"{_POST}.get_post_url_from_log_for_user", return_value="https://x/post/7"), \
             patch(f"{_POST}.get_post_content", return_value="my post"), \
             patch(f"{_POST}.click_first", return_value=None), \
             patch(f"{_POST}._comment_items_from_thread", return_value=[comment]), \
             patch(f"{_POST}.get_engagement_preferences", return_value={}), \
             patch(f"{_POST}.get_lead_magnet_settings", return_value={"enabled": False}), \
             patch(f"{_POST}.upsert_engager"), \
             patch(f"{_POST}._flag_lead_signal", return_value=1) as flag, \
             patch(f"{_POST}.generate_thread_reply", return_value=None), \
             patch(f"{_POST}.insert_new_log"):
            driver = MagicMock()
            driver.current_url = "https://x/post/7"
            _post("_reply_to_comments_on_open_post")(driver, MagicMock(), 1, 7, profile, "synth")
        flag.assert_called_once()
        assert flag.call_args.args[1] == "Do you offer this for agencies?"
        assert flag.call_args.kwargs["person_name"] == "Jane Doe"
        assert flag.call_args.kwargs["post_id"] == 7
