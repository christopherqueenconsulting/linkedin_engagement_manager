"""Unit tests for the content-generation-ready notification + email (issue #545)."""

import pytest
from unittest.mock import patch

pytestmark = pytest.mark.unit

_MOD = "cqc_lem.utilities.notifications"
_EMAIL = "cqc_lem.utilities.email"


class TestNotifyContentGenerationReady:
    def test_sends_with_counts(self):
        from cqc_lem.utilities.notifications import notify_content_generation_ready
        with patch(f"{_MOD}.get_user_email", return_value="u@e.com"), \
             patch(f"{_EMAIL}.send_content_generation_ready_email", return_value=True) as send:
            assert notify_content_generation_ready(4, 3, 1) is True
        send.assert_called_once_with("u@e.com", 3, 1)

    def test_skips_when_nothing_generated(self):
        from cqc_lem.utilities.notifications import notify_content_generation_ready
        with patch(f"{_MOD}.get_user_email") as get_email, \
             patch(f"{_EMAIL}.send_content_generation_ready_email") as send:
            assert notify_content_generation_ready(4, 0, 2) is False
        get_email.assert_not_called()
        send.assert_not_called()

    def test_skips_when_user_has_no_email(self):
        from cqc_lem.utilities.notifications import notify_content_generation_ready
        with patch(f"{_MOD}.get_user_email", return_value=None), \
             patch(f"{_EMAIL}.send_content_generation_ready_email") as send:
            assert notify_content_generation_ready(4, 1) is False
        send.assert_not_called()

    def test_send_failure_returns_false(self):
        from cqc_lem.utilities.notifications import notify_content_generation_ready
        with patch(f"{_MOD}.get_user_email", return_value="u@e.com"), \
             patch(f"{_EMAIL}.send_content_generation_ready_email", return_value=False):
            assert notify_content_generation_ready(4, 1) is False

    def test_exception_is_swallowed(self):
        from cqc_lem.utilities.notifications import notify_content_generation_ready
        with patch(f"{_MOD}.get_user_email", side_effect=RuntimeError("db down")):
            assert notify_content_generation_ready(4, 1) is False


class TestContentGenerationReadyEmail:
    def test_singular_copy_and_review_link(self, monkeypatch):
        monkeypatch.setenv("LEM_APP_URL", "https://app.example.com/")
        from cqc_lem.utilities.email import send_content_generation_ready_email
        with patch(f"{_EMAIL}._dispatch_email", return_value=True) as dispatch:
            assert send_content_generation_ready_email("u@e.com", 1) is True
        to_email, subject, html = dispatch.call_args[0]
        assert to_email == "u@e.com"
        assert "1 LinkedIn post is ready" in subject
        assert "https://app.example.com/content?tab=review" in html
        assert "couldn't be generated" not in html

    def test_plural_copy_and_failure_note(self, monkeypatch):
        monkeypatch.setenv("LEM_APP_URL", "https://app.example.com")
        from cqc_lem.utilities.email import send_content_generation_ready_email
        with patch(f"{_EMAIL}._dispatch_email", return_value=True) as dispatch:
            send_content_generation_ready_email("u@e.com", 3, 2)
        subject, html = dispatch.call_args[0][1], dispatch.call_args[0][2]
        assert "3 LinkedIn posts are ready" in subject
        assert "2 posts couldn't be generated" in html
