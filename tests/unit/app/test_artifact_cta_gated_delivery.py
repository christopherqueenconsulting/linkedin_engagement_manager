"""Issue #624 — comment-gated artifact delivery is APPROVAL-GATED. 'Comment X and I'll send it' is
the mechanic that makes an artifact CTA worth writing, but DM-ing every commenter at volume is a
spam surface, so the payload lands as a pending draft in the operator's queue and never as a send.
"""
from unittest.mock import patch

# `_queue_artifact_delivery` moved to `app.engagement.posting` (#1154) — its own module
# globals are what it reads, so that is where its collaborators are patched.
_POST = "cqc_lem.app.engagement.posting"
_LM = {"enabled": True, "keyword": "AUDIT", "message": "Here you go: {blog_url}"}
_PREFS = {"max_dms_per_day": 5}
_PROFILE = "https://www.linkedin.com/in/jane"


def _queue(**overrides):
    from cqc_lem.app.engagement.posting import _queue_artifact_delivery
    kwargs = {"user_id": 1, "profile_url": _PROFILE, "first_name": "Jane Doe",
              "comment_text": "AUDIT please", "lead_magnet": _LM, "prefs": _PREFS,
              "post_id": 9, "blog_url": "https://blog"}
    kwargs.update(overrides)
    return _queue_artifact_delivery(**kwargs)


class TestQueueArtifactDelivery:
    def test_queues_a_pending_draft_and_records_the_recipient(self):
        from cqc_lem.utilities.db import SCHEDULED_DM_SOURCE_ARTIFACT, ScheduledDmStatus
        with patch(f"{_POST}.has_received_lead_magnet", return_value=False), \
             patch(f"{_POST}.has_open_scheduled_dm", return_value=False), \
             patch(f"{_POST}.count_scheduled_dms_created_today", return_value=0), \
             patch(f"{_POST}.record_lead_magnet_sent") as rec, \
             patch(f"{_POST}.insert_scheduled_dm", return_value=42) as ins:
            assert _queue() == 42
        assert ins.call_args.kwargs["status"] == ScheduledDmStatus.PENDING
        assert ins.call_args.kwargs["source"] == SCHEDULED_DM_SOURCE_ARTIFACT
        assert ins.call_args.args[1] == _PROFILE
        assert "https://blog" in ins.call_args.args[2]      # placeholders rendered
        # Recorded on QUEUE so the next sweep doesn't draft the same resource again.
        rec.assert_called_once_with(1, _PROFILE, 9)

    def test_no_keyword_in_the_comment_delivers_nothing(self):
        with patch(f"{_POST}.insert_scheduled_dm") as ins:
            assert _queue(comment_text="great post") is None
        ins.assert_not_called()

    def test_keyword_match_is_case_insensitive(self):
        with patch(f"{_POST}.has_received_lead_magnet", return_value=False), \
             patch(f"{_POST}.has_open_scheduled_dm", return_value=False), \
             patch(f"{_POST}.count_scheduled_dms_created_today", return_value=0), \
             patch(f"{_POST}.record_lead_magnet_sent"), \
             patch(f"{_POST}.insert_scheduled_dm", return_value=1):
            assert _queue(comment_text="send me the audit") == 1

    def test_already_delivered_person_is_skipped(self):
        with patch(f"{_POST}.has_received_lead_magnet", return_value=True), \
             patch(f"{_POST}.insert_scheduled_dm") as ins:
            assert _queue() is None
        ins.assert_not_called()

    def test_one_open_draft_per_thread(self):
        with patch(f"{_POST}.has_received_lead_magnet", return_value=False), \
             patch(f"{_POST}.has_open_scheduled_dm", return_value=True), \
             patch(f"{_POST}.insert_scheduled_dm") as ins:
            assert _queue() is None
        ins.assert_not_called()

    def test_an_open_nurture_draft_also_blocks_it(self):
        """Both mechanics write to the same thread — two queued messages for one person is spam."""
        from cqc_lem.utilities.db import SCHEDULED_DM_SOURCE_NURTURE
        def _open(user_id, url, source=None):
            return source == SCHEDULED_DM_SOURCE_NURTURE
        with patch(f"{_POST}.has_received_lead_magnet", return_value=False), \
             patch(f"{_POST}.has_open_scheduled_dm", side_effect=_open), \
             patch(f"{_POST}.insert_scheduled_dm") as ins:
            assert _queue() is None
        ins.assert_not_called()

    def test_daily_cap_is_the_users_dm_cap(self):
        with patch(f"{_POST}.has_received_lead_magnet", return_value=False), \
             patch(f"{_POST}.has_open_scheduled_dm", return_value=False), \
             patch(f"{_POST}.count_scheduled_dms_created_today", return_value=5), \
             patch(f"{_POST}.insert_scheduled_dm") as ins:
            assert _queue() is None
        ins.assert_not_called()

    def test_a_zero_cap_delivers_nothing(self):
        with patch(f"{_POST}.has_received_lead_magnet", return_value=False), \
             patch(f"{_POST}.has_open_scheduled_dm", return_value=False), \
             patch(f"{_POST}.count_scheduled_dms_created_today", return_value=0), \
             patch(f"{_POST}.insert_scheduled_dm") as ins:
            assert _queue(prefs={"max_dms_per_day": 0}) is None
        ins.assert_not_called()

    def test_disabled_lead_magnet_delivers_nothing(self):
        with patch(f"{_POST}.insert_scheduled_dm") as ins:
            assert _queue(lead_magnet={"enabled": False, "keyword": "AUDIT", "message": "x"}) is None
        ins.assert_not_called()

    def test_no_profile_url_delivers_nothing(self):
        with patch(f"{_POST}.insert_scheduled_dm") as ins:
            assert _queue(profile_url="") is None
        ins.assert_not_called()

    def test_empty_rendered_message_is_not_queued(self):
        with patch(f"{_POST}.has_received_lead_magnet", return_value=False), \
             patch(f"{_POST}.has_open_scheduled_dm", return_value=False), \
             patch(f"{_POST}.count_scheduled_dms_created_today", return_value=0), \
             patch(f"{_POST}.render_dm_placeholders", return_value="   "), \
             patch(f"{_POST}.insert_scheduled_dm") as ins:
            assert _queue() is None
        ins.assert_not_called()

    def test_failed_insert_does_not_mark_the_person_delivered(self):
        with patch(f"{_POST}.has_received_lead_magnet", return_value=False), \
             patch(f"{_POST}.has_open_scheduled_dm", return_value=False), \
             patch(f"{_POST}.count_scheduled_dms_created_today", return_value=0), \
             patch(f"{_POST}.record_lead_magnet_sent") as rec, \
             patch(f"{_POST}.insert_scheduled_dm", return_value=None):
            assert _queue() is None
        rec.assert_not_called()

    def test_a_db_error_never_breaks_the_reply_sweep(self):
        with patch(f"{_POST}.has_received_lead_magnet", side_effect=RuntimeError("db down")):
            assert _queue() is None
