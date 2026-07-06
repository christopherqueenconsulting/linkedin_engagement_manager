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


def _run_generate(*, settings, pending, latest=None, gen_now=True,
                  edition=None, edition_side_effect=None, create_ret=1, create_side_effect=None,
                  user_ids=None, tz_side_effect=None):
    """Drive auto_generate_newsletter_drafts with the new pure-count collaborators mocked out."""
    from contextlib import ExitStack
    from cqc_lem.app.run_scheduler import auto_generate_newsletter_drafts
    if edition is None:
        edition = {"title": "T", "subtitle": "S", "body": "B"}
    if user_ids is None:
        user_ids = [1]
    gen_kw = {"side_effect": edition_side_effect} if edition_side_effect else {"return_value": edition}
    create_kw = {"side_effect": create_side_effect} if create_side_effect else {"return_value": create_ret}
    tz_kw = {"side_effect": tz_side_effect} if tz_side_effect else {"return_value": "UTC"}
    with ExitStack() as es:
        p = es.enter_context
        p(patch("cqc_lem.utilities.db.get_enabled_newsletter_user_ids", return_value=user_ids))
        p(patch("cqc_lem.utilities.db.get_newsletter_settings", return_value=settings))
        p(patch("cqc_lem.utilities.db.get_user_timezone", **tz_kw))
        p(patch("cqc_lem.utilities.db.count_pending_newsletter_editions", return_value=pending))
        p(patch("cqc_lem.utilities.db.get_latest_edition_scheduled_for", return_value=latest))
        create = p(patch("cqc_lem.utilities.db.create_newsletter_edition", **create_kw))
        p(patch("cqc_lem.utilities.linkedin.helper.load_profile_for_user", return_value=MagicMock()))
        p(patch("cqc_lem.utilities.ai.ai_helper.generate_newsletter_edition", **gen_kw))
        p(patch("cqc_lem.utilities.newsletter.should_generate_now", return_value=gen_now))
        notify = p(patch("cqc_lem.utilities.notifications.notify_newsletter_draft_ready"))
        result = auto_generate_newsletter_drafts()
    return result, create, notify


_SETTINGS = {"publish_day": 1, "publish_hour": 9, "cadence": "weekly", "last_published_at": None,
             "topic": "reach", "max_queued_drafts": 1, "generate_lead_days": 3}


def _settings(**overrides):
    return {**_SETTINGS, **overrides}


class TestGenerateNewsletterDrafts:
    def test_bootstrap_generates_one(self):
        result, create, notify = _run_generate(settings=_settings(max_queued_drafts=1), pending=0)
        create.assert_called_once()
        notify.assert_called_once()
        assert "Generated 1 newsletter draft" in result

    def test_fills_queue_to_cap(self):
        # cap 5, empty queue → generate all 5 upcoming slots in one run.
        result, create, notify = _run_generate(settings=_settings(max_queued_drafts=5), pending=0)
        assert create.call_count == 5
        assert notify.call_count == 5
        assert "Generated 5 newsletter draft" in result

    def test_skips_when_queue_full(self):
        # pending == cap → nothing to add.
        result, create, _ = _run_generate(settings=_settings(max_queued_drafts=3), pending=3)
        create.assert_not_called()
        assert "Generated 0 newsletter draft" in result

    def test_bootstrap_gate_blocks_when_not_time_yet(self):
        # First-ever draft (pending 0) waits for the lead window.
        result, create, _ = _run_generate(settings=_settings(max_queued_drafts=2), pending=0, gen_now=False)
        create.assert_not_called()
        assert "Generated 0 newsletter draft" in result

    def test_rolling_refill_ignores_lead_gate(self):
        # Queue already rolling (pending > 0) → top up to cap even though the lead gate would say "not yet".
        result, create, _ = _run_generate(settings=_settings(max_queued_drafts=3), pending=2, gen_now=False)
        create.assert_called_once()
        assert "Generated 1 newsletter draft" in result

    def test_stops_when_generation_fails_midway(self):
        result, create, notify = _run_generate(
            settings=_settings(max_queued_drafts=3), pending=0,
            edition_side_effect=[{"title": "T", "subtitle": "S", "body": "B"}, None])
        create.assert_called_once()
        assert "Generated 1 newsletter draft" in result

    def test_stops_on_duplicate_slot(self):
        # create returning 0 (uq_user_slot collision) halts this user's run.
        result, create, _ = _run_generate(
            settings=_settings(max_queued_drafts=3), pending=0, create_side_effect=[10, 0])
        assert create.call_count == 2
        assert "Generated 1 newsletter draft" in result

    def test_one_user_failure_does_not_stop_loop(self):
        result, create, _ = _run_generate(
            settings=_settings(max_queued_drafts=1), pending=0, user_ids=[1, 2],
            tz_side_effect=[Exception("boom"), "UTC"])
        create.assert_called_once()
        assert "Generated 1 newsletter draft" in result


def _run_generate_for_user(*, settings, pending, latest=None, gen_now=True, edition=None,
                           create_ret=1):
    """Drive generate_newsletter_drafts_for_user (the on-demand per-user top-up) with collaborators
    mocked out."""
    from contextlib import ExitStack
    from cqc_lem.app.run_scheduler import generate_newsletter_drafts_for_user
    if edition is None:
        edition = {"title": "T", "subtitle": "S", "body": "B"}
    with ExitStack() as es:
        p = es.enter_context
        p(patch("cqc_lem.utilities.db.get_newsletter_settings", return_value=settings))
        p(patch("cqc_lem.utilities.db.get_user_timezone", return_value="UTC"))
        p(patch("cqc_lem.utilities.db.count_pending_newsletter_editions", return_value=pending))
        p(patch("cqc_lem.utilities.db.get_latest_edition_scheduled_for", return_value=latest))
        create = p(patch("cqc_lem.utilities.db.create_newsletter_edition", return_value=create_ret))
        p(patch("cqc_lem.utilities.linkedin.helper.load_profile_for_user", return_value=MagicMock()))
        p(patch("cqc_lem.utilities.ai.ai_helper.generate_newsletter_edition", return_value=edition))
        p(patch("cqc_lem.utilities.newsletter.should_generate_now", return_value=gen_now))
        p(patch("cqc_lem.utilities.notifications.notify_newsletter_draft_ready"))
        result = generate_newsletter_drafts_for_user.run(user_id=1)
    return result, create


class TestGenerateNewsletterDraftsForUser:
    def test_disabled_user_generates_nothing(self):
        result, create = _run_generate_for_user(
            settings=_settings(enabled=False, max_queued_drafts=3), pending=1)
        create.assert_not_called()
        assert "disabled" in result

    def test_raising_count_adds_delta_when_one_exists(self):
        # User already has one draft; count raised to 3 → fill the two freed slots.
        result, create = _run_generate_for_user(
            settings=_settings(enabled=True, max_queued_drafts=3), pending=1)
        assert create.call_count == 2
        assert "Generated 2 newsletter draft" in result

    def test_bypasses_bootstrap_lead_gate(self):
        # Empty queue, outside the lead window: an explicit settings change still fills ahead.
        result, create = _run_generate_for_user(
            settings=_settings(enabled=True, max_queued_drafts=2), pending=0, gen_now=False)
        assert create.call_count == 2
        assert "Generated 2 newsletter draft" in result

    def test_skips_when_queue_full(self):
        result, create = _run_generate_for_user(
            settings=_settings(enabled=True, max_queued_drafts=2), pending=2)
        create.assert_not_called()
        assert "Generated 0 newsletter draft" in result


class TestPublishScheduledEditions:
    def test_dispatches_due_editions(self):
        from cqc_lem.app.run_scheduler import auto_publish_scheduled_editions
        with patch("cqc_lem.utilities.db.get_editions_due_to_publish",
                   return_value=[{"id": 3}, {"id": 8}]), \
             patch("cqc_lem.app.run_automation.auto_publish_edition") as task:
            result = auto_publish_scheduled_editions()
        assert task.apply_async.call_count == 2
        assert "2 newsletter edition" in result
