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


class TestAutoPublishEdition:
    def test_skips_when_not_publishable(self):
        from cqc_lem.app.run_automation import auto_publish_edition
        with patch(f"{_RA}.get_newsletter_edition", return_value={"user_id": 1, "status": "published"}), \
             patch(f"{_RA}.get_current_profile") as gp:
            result = auto_publish_edition.run(edition_id=9)
        assert "not publishable" in result
        gp.assert_not_called()

    def test_skips_when_missing(self):
        from cqc_lem.app.run_automation import auto_publish_edition
        with patch(f"{_RA}.get_newsletter_edition", return_value=None), \
             patch(f"{_RA}.get_current_profile") as gp:
            result = auto_publish_edition.run(edition_id=9)
        assert "not publishable" in result
        gp.assert_not_called()

    def test_publishes_and_marks(self):
        from cqc_lem.app.run_automation import auto_publish_edition
        edition = {"id": 9, "user_id": 1, "status": "approved", "title": "5 Levers",
                   "subtitle": "The reach levers.", "body": "Hook\n\nSECTION\n\nBody."}
        with patch(f"{_RA}.get_newsletter_edition", return_value=edition), \
             patch(f"{_RA}.get_current_profile", return_value=(MagicMock(), MagicMock(), "e", MagicMock())), \
             patch(f"{_RA}._fill_and_publish_article", return_value="https://x/pulse/5-levers") as fill, \
             patch(f"{_RA}.mark_edition_published") as mark, \
             patch(f"{_RA}.mark_edition_failed") as fail, \
             patch(f"{_RA}.quit_gracefully"):
            result = auto_publish_edition.run(edition_id=9)
        mark.assert_called_once_with(9, "https://x/pulse/5-levers")
        fail.assert_not_called()
        assert "Published newsletter edition: 5 Levers" in result
        assert fill.call_args.kwargs.get("subtitle") == "The reach levers."

    def test_marks_failed_when_flow_incomplete(self):
        from cqc_lem.app.run_automation import auto_publish_edition
        edition = {"id": 9, "user_id": 1, "status": "draft", "title": "T", "subtitle": None, "body": "B"}
        with patch(f"{_RA}.get_newsletter_edition", return_value=edition), \
             patch(f"{_RA}.get_current_profile", return_value=(MagicMock(), MagicMock(), "e", MagicMock())), \
             patch(f"{_RA}._fill_and_publish_article", return_value=None), \
             patch(f"{_RA}.mark_edition_published") as mark, \
             patch(f"{_RA}.mark_edition_failed") as fail, \
             patch(f"{_RA}.quit_gracefully"):
            result = auto_publish_edition.run(edition_id=9)
        mark.assert_not_called()
        fail.assert_called_once_with(9)
        assert "did not complete" in result


class TestGenerateNewsletterDrafts:
    def test_generates_and_notifies(self):
        from cqc_lem.app.run_scheduler import auto_generate_newsletter_drafts
        settings = {"publish_day": 1, "publish_hour": 9, "cadence": "weekly",
                    "last_published_at": None, "topic": "reach"}
        edition = {"title": "T", "subtitle": "S", "body": "B"}
        with patch("cqc_lem.utilities.db.get_enabled_newsletter_user_ids", return_value=[1]), \
             patch("cqc_lem.utilities.db.get_newsletter_settings", return_value=settings), \
             patch("cqc_lem.utilities.db.get_user_timezone", return_value="UTC"), \
             patch("cqc_lem.utilities.db.get_pending_newsletter_edition", return_value=None), \
             patch("cqc_lem.utilities.db.create_newsletter_edition", return_value=42) as create, \
             patch("cqc_lem.utilities.linkedin.helper.load_profile_for_user", return_value=MagicMock()), \
             patch("cqc_lem.utilities.ai.ai_helper.generate_newsletter_edition", return_value=edition), \
             patch("cqc_lem.utilities.newsletter.should_generate_now", return_value=True), \
             patch("cqc_lem.utilities.notifications.notify_newsletter_draft_ready") as notify:
            result = auto_generate_newsletter_drafts()
        create.assert_called_once()
        notify.assert_called_once()
        assert "1 user" in result

    def test_skips_when_pending_exists(self):
        from cqc_lem.app.run_scheduler import auto_generate_newsletter_drafts
        settings = {"publish_day": 1, "publish_hour": 9, "cadence": "weekly", "last_published_at": None}
        with patch("cqc_lem.utilities.db.get_enabled_newsletter_user_ids", return_value=[1]), \
             patch("cqc_lem.utilities.db.get_newsletter_settings", return_value=settings), \
             patch("cqc_lem.utilities.db.get_user_timezone", return_value="UTC"), \
             patch("cqc_lem.utilities.db.get_pending_newsletter_edition", return_value={"id": 5}), \
             patch("cqc_lem.utilities.db.create_newsletter_edition") as create, \
             patch("cqc_lem.utilities.newsletter.should_generate_now", return_value=True):
            result = auto_generate_newsletter_drafts()
        create.assert_not_called()
        assert "0 user" in result

    def test_skips_when_not_time_yet(self):
        from cqc_lem.app.run_scheduler import auto_generate_newsletter_drafts
        settings = {"publish_day": 1, "publish_hour": 9, "cadence": "weekly", "last_published_at": None}
        with patch("cqc_lem.utilities.db.get_enabled_newsletter_user_ids", return_value=[1]), \
             patch("cqc_lem.utilities.db.get_newsletter_settings", return_value=settings), \
             patch("cqc_lem.utilities.db.get_user_timezone", return_value="UTC"), \
             patch("cqc_lem.utilities.db.get_pending_newsletter_edition", return_value=None) as pend, \
             patch("cqc_lem.utilities.newsletter.should_generate_now", return_value=False):
            result = auto_generate_newsletter_drafts()
        pend.assert_not_called()
        assert "0 user" in result

    def test_one_user_failure_does_not_stop_loop(self):
        from cqc_lem.app.run_scheduler import auto_generate_newsletter_drafts
        settings = {"publish_day": 1, "publish_hour": 9, "cadence": "weekly",
                    "last_published_at": None, "topic": None}
        with patch("cqc_lem.utilities.db.get_enabled_newsletter_user_ids", return_value=[1, 2]), \
             patch("cqc_lem.utilities.db.get_newsletter_settings", return_value=settings), \
             patch("cqc_lem.utilities.db.get_user_timezone", side_effect=[Exception("boom"), "UTC"]), \
             patch("cqc_lem.utilities.db.get_pending_newsletter_edition", return_value=None), \
             patch("cqc_lem.utilities.db.create_newsletter_edition", return_value=1), \
             patch("cqc_lem.utilities.linkedin.helper.load_profile_for_user", return_value=MagicMock()), \
             patch("cqc_lem.utilities.ai.ai_helper.generate_newsletter_edition",
                   return_value={"title": "T", "subtitle": "S", "body": "B"}), \
             patch("cqc_lem.utilities.newsletter.should_generate_now", return_value=True), \
             patch("cqc_lem.utilities.notifications.notify_newsletter_draft_ready"):
            result = auto_generate_newsletter_drafts()
        assert "1 user" in result


class TestPublishScheduledEditions:
    def test_dispatches_due_editions(self):
        from cqc_lem.app.run_scheduler import auto_publish_scheduled_editions
        with patch("cqc_lem.utilities.db.get_editions_due_to_publish",
                   return_value=[{"id": 3}, {"id": 8}]), \
             patch("cqc_lem.app.run_automation.auto_publish_edition") as task:
            result = auto_publish_scheduled_editions()
        assert task.apply_async.call_count == 2
        assert "2 newsletter edition" in result
