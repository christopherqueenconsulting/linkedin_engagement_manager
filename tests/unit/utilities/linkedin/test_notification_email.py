"""Unit tests for the comment-notification email helpers (event-driven reply feature)."""

import pytest
from unittest.mock import patch

pytestmark = pytest.mark.unit

_M = "cqc_lem.utilities.linkedin.notification_email"  # lgtm[py/unused-global-variable]


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


class TestIsLinkedInNotification:
    """The evidence test for issue #813 — broader than is_comment_notification (a forwarded
    reaction email proves the forwarding chain too) but not blanket."""

    def test_true_for_linkedin_sender(self):
        from cqc_lem.utilities.linkedin.notification_email import is_linkedin_notification
        assert is_linkedin_notification("LinkedIn <messages-noreply@linkedin.com>",
                                        "Jane liked your post") is True

    def test_true_for_reactions_and_mentions(self):
        from cqc_lem.utilities.linkedin.notification_email import is_linkedin_notification
        assert is_linkedin_notification("news@linkedin.com", "Jane mentioned you in a comment") is True

    def test_true_from_body_when_sender_rewritten(self):
        from cqc_lem.utilities.linkedin.notification_email import is_linkedin_notification
        assert is_linkedin_notification("chris@example.com", "Fwd: new activity",
                                        "View this on LinkedIn to reply") is True

    def test_false_for_unrelated_mail(self):
        from cqc_lem.utilities.linkedin.notification_email import is_linkedin_notification
        assert is_linkedin_notification("billing@vendor.com", "Your invoice is ready",
                                        "Payment due in 14 days") is False

    def test_false_for_empty_input(self):
        from cqc_lem.utilities.linkedin.notification_email import is_linkedin_notification
        assert is_linkedin_notification("", "", "") is False


_GMAIL_BODY = (
    "forwarding-noreply@google.com has requested to automatically forward mail to your address.\n"
    "Confirmation code: 987654321\n\n"
    "To allow christopher.queen@gmail.com to automatically forward mail, please click the link "
    "below to confirm the request:\n"
    "https://mail.google.com/mail/vf-%5BANGjdJ8xyz%5D-abc123def456\n\n"
    "If the link is broken, copy and paste it into a new browser window."
)


class TestGmailForwardingConfirmation:
    def test_detects_from_sender(self):
        from cqc_lem.utilities.linkedin.notification_email import is_gmail_forwarding_confirmation
        assert is_gmail_forwarding_confirmation("Gmail Team <forwarding-noreply@google.com>",
                                                "(#123) Gmail Forwarding Confirmation", "") is True

    def test_detects_from_subject(self):
        from cqc_lem.utilities.linkedin.notification_email import is_gmail_forwarding_confirmation
        assert is_gmail_forwarding_confirmation("x@y.com", "Gmail Forwarding Confirmation - Receive Mail", "") is True

    def test_not_confirmation_for_comment(self):
        from cqc_lem.utilities.linkedin.notification_email import is_gmail_forwarding_confirmation
        assert is_gmail_forwarding_confirmation("notify@linkedin.com", "Jane commented on your post", "hi") is False

    def test_extracts_verify_url(self):
        from cqc_lem.utilities.linkedin.notification_email import extract_gmail_confirmation_url
        url = extract_gmail_confirmation_url(_GMAIL_BODY)
        assert url == "https://mail.google.com/mail/vf-%5BANGjdJ8xyz%5D-abc123def456"

    def test_extracts_code(self):
        from cqc_lem.utilities.linkedin.notification_email import extract_gmail_confirmation_code
        assert extract_gmail_confirmation_code(_GMAIL_BODY) == "987654321"

    def test_no_url_or_code(self):
        from cqc_lem.utilities.linkedin.notification_email import (
            extract_gmail_confirmation_url, extract_gmail_confirmation_code)
        assert extract_gmail_confirmation_url("nothing here") is None
        assert extract_gmail_confirmation_code("nothing here") is None
