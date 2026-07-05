"""Unit tests for newsletter publish task + dispatcher."""

import pytest
from unittest.mock import MagicMock, patch

pytestmark = pytest.mark.unit

_RA = "cqc_lem.app.run_automation"
_RS = "cqc_lem.app.run_scheduler"


@pytest.fixture(autouse=True)
def _no_sleep():
    with patch(f"{_RA}.time.sleep"):
        yield


class TestAutoPublishNewsletterEdition:
    def test_skips_when_disabled(self):
        from cqc_lem.app.run_automation import auto_publish_newsletter_edition
        with patch(f"{_RA}.get_newsletter_settings", return_value={"enabled": False}), \
             patch(f"{_RA}.get_current_profile") as gp:
            result = auto_publish_newsletter_edition.run(user_id=1)
        assert "not enabled" in result
        gp.assert_not_called()

    def test_publishes_and_marks(self):
        from cqc_lem.app.run_automation import auto_publish_newsletter_edition
        edition = {"title": "5 Levers", "subtitle": "The reach levers most creators miss.",
                   "body": "Hook\n\nSECTION\n\nBody."}
        with patch(f"{_RA}.get_newsletter_settings", return_value={"enabled": True, "topic": "reach", "align_with_blog": False}), \
             patch(f"{_RA}.get_current_profile", return_value=(MagicMock(), MagicMock(), "e", MagicMock())), \
             patch(f"{_RA}.generate_newsletter_edition", return_value=edition), \
             patch(f"{_RA}._fill_and_publish_article", return_value="https://x/pulse/5-levers") as fill, \
             patch(f"{_RA}.mark_newsletter_published") as mark, \
             patch(f"{_RA}.quit_gracefully"):
            result = auto_publish_newsletter_edition.run(user_id=1)
        mark.assert_called_once()
        assert "Published newsletter: 5 Levers" in result
        # subtitle is threaded through to the publisher
        assert fill.call_args.kwargs.get("subtitle") == "The reach levers most creators miss."


class TestFillEditionDescription:
    def test_returns_false_on_empty_subtitle(self):
        from cqc_lem.app.run_automation import _fill_edition_description
        assert _fill_edition_description(MagicMock(), MagicMock(), "") is False

    def test_non_fatal_when_field_absent(self):
        from cqc_lem.app.run_automation import _fill_edition_description
        with patch(f"{_RA}.find_first", return_value=None):
            assert _fill_edition_description(MagicMock(), MagicMock(), "about this edition") is False

    def test_fills_when_field_present(self):
        from cqc_lem.app.run_automation import _fill_edition_description
        el = MagicMock()
        with patch(f"{_RA}.find_first", return_value=el):
            assert _fill_edition_description(MagicMock(), MagicMock(), "about this edition") is True
        el.send_keys.assert_called_once()


class TestAutoPublishDueNewsletters:
    def test_dispatches_due_users(self):
        from cqc_lem.app.run_scheduler import auto_publish_due_newsletters
        with patch("cqc_lem.utilities.db.get_newsletter_due_user_ids", return_value=[1, 7]), \
             patch("cqc_lem.app.run_automation.auto_publish_newsletter_edition") as task:
            result = auto_publish_due_newsletters()
        assert task.apply_async.call_count == 2
        assert "2 user" in result
