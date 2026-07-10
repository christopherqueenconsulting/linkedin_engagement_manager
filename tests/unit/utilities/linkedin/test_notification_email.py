"""Unit tests for the comment-notification email helpers (event-driven reply feature)."""

import pytest
from unittest.mock import patch

pytestmark = pytest.mark.unit

_M = "cqc_lem.utilities.linkedin.notification_email"


class TestAddress:
    def test_address_uses_env_parse_domain(self):
        from cqc_lem.utilities.linkedin.notification_email import reply_inbound_address
        with patch.dict("os.environ", {"LINKEDIN_PARSE_DOMAIN": "parse.example.com"}):
            assert reply_inbound_address("tok123") == "reply+tok123@parse.example.com"

    def test_token_round_trip(self):
        from cqc_lem.utilities.linkedin.notification_email import (
            reply_inbound_address, extract_reply_token_from_address)
        with patch.dict("os.environ", {"LINKEDIN_PARSE_DOMAIN": "parse.example.com"}):
            addr = reply_inbound_address("abc987")
        assert extract_reply_token_from_address(addr) == "abc987"

    def test_extract_from_envelope_json(self):
        from cqc_lem.utilities.linkedin.notification_email import extract_reply_token_from_address
        assert extract_reply_token_from_address('{"to":["reply+XY9@parse.example.com"]}') == "XY9"

    def test_extract_none_when_absent(self):
        from cqc_lem.utilities.linkedin.notification_email import extract_reply_token_from_address
        assert extract_reply_token_from_address("noreply@linkedin.com") is None
        assert extract_reply_token_from_address("") is None


class TestIsCommentNotification:
    @pytest.mark.parametrize("subject", [
        "Chris, someone commented on your post",
        "Jane Doe replied to your comment",
        "New comment on your post",
    ])
    def test_comment_subjects_true(self, subject):
        from cqc_lem.utilities.linkedin.notification_email import is_comment_notification
        assert is_comment_notification(subject) is True

    @pytest.mark.parametrize("subject", [
        "Jane Doe liked your post",
        "Your post reached 100 reactions",
        "Chris, John reacted to your post",
        "Jane mentioned you in a comment",  # mention, not a comment on our post
    ])
    def test_reaction_subjects_false(self, subject):
        from cqc_lem.utilities.linkedin.notification_email import is_comment_notification
        assert is_comment_notification(subject) is False

    def test_body_signal_when_subject_neutral(self):
        from cqc_lem.utilities.linkedin.notification_email import is_comment_notification
        assert is_comment_notification("LinkedIn", "Jane Doe commented on your post: great work!") is True

    def test_quoted_history_ignored(self):
        from cqc_lem.utilities.linkedin.notification_email import is_comment_notification
        body = "FYI\n> On Mon, someone commented on your post"
        assert is_comment_notification("Weekly digest", body) is False
