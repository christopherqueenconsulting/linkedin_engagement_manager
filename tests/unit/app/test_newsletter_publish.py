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
        p(patch("cqc_lem.utilities.db.get_pending_newsletter_editions", return_value=[]))
        p(patch("cqc_lem.utilities.db.get_recent_newsletter_subjects", return_value=[]))
        p(patch("cqc_lem.utilities.db.get_recent_newsletter_blueprint_history", return_value=[]))
        p(patch("cqc_lem.utilities.db.get_engagement_preferences", return_value={}))
        p(patch("cqc_lem.utilities.linkedin.helper.load_profile_for_user", return_value=MagicMock()))
        p(patch("cqc_lem.utilities.ai.ai_helper.get_or_create_profile_synthesis", return_value="voice brief"))
        p(patch("cqc_lem.utilities.ai.ai_helper.plan_newsletter_topics", return_value=[]))
        p(patch("cqc_lem.utilities.ai.ai_helper.generate_newsletter_edition", **gen_kw))
        p(patch("cqc_lem.utilities.ai.content_research.research_topic",
                return_value={"findings": "", "sources": []}))
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
        p(patch("cqc_lem.utilities.db.get_pending_newsletter_editions", return_value=[]))
        p(patch("cqc_lem.utilities.db.get_recent_newsletter_subjects", return_value=[]))
        p(patch("cqc_lem.utilities.db.get_recent_newsletter_blueprint_history", return_value=[]))
        p(patch("cqc_lem.utilities.db.get_engagement_preferences", return_value={}))
        p(patch("cqc_lem.utilities.linkedin.helper.load_profile_for_user", return_value=MagicMock()))
        p(patch("cqc_lem.utilities.ai.ai_helper.get_or_create_profile_synthesis", return_value="voice brief"))
        p(patch("cqc_lem.utilities.ai.ai_helper.plan_newsletter_topics", return_value=[]))
        p(patch("cqc_lem.utilities.ai.ai_helper.generate_newsletter_edition", return_value=edition))
        p(patch("cqc_lem.utilities.ai.content_research.research_topic",
                return_value={"findings": "", "sources": []}))
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


class TestTopupPlansDistinctSubjects:
    def _drive(self, planned, pending=0, existing_subjects=None, recent=None,
               shape_history=None, research=None, gen_captures=None):
        from contextlib import ExitStack
        from cqc_lem.app.run_scheduler import auto_generate_newsletter_drafts

        def _gen(profile, topic=None, prefs=None, subject=None, avoid_subjects=None,
                 profile_synthesis=None, guidance=None, blueprint=None, avoid_openers=None,
                 research=None):
            if gen_captures is not None:
                gen_captures.append({"subject": subject, "blueprint": blueprint,
                                     "avoid_openers": avoid_openers, "research": research})
            # Echo the planned subject as the generated edition's canonical subject.
            base = subject.split(" — angle:")[0] if subject else "Auto"
            return {"title": f"Title for {base}", "subtitle": "S", "subject": base, "body": "B",
                    "format": (blueprint or {}).get("format"),
                    "hook_style": (blueprint or {}).get("hook_style"),
                    "opening_line": f"Opening for {base}"}

        with ExitStack() as es:
            p = es.enter_context
            p(patch("cqc_lem.utilities.db.get_enabled_newsletter_user_ids", return_value=[1]))
            p(patch("cqc_lem.utilities.db.get_newsletter_settings",
                    return_value=_settings(max_queued_drafts=max(1, len(planned)))))
            p(patch("cqc_lem.utilities.db.get_user_timezone", return_value="UTC"))
            p(patch("cqc_lem.utilities.db.count_pending_newsletter_editions", return_value=pending))
            p(patch("cqc_lem.utilities.db.get_latest_edition_scheduled_for", return_value=None))
            pending_eds = [{"id": i, "subject": s} for i, s in enumerate(existing_subjects or [])]
            p(patch("cqc_lem.utilities.db.get_pending_newsletter_editions", return_value=pending_eds))
            p(patch("cqc_lem.utilities.db.get_recent_newsletter_subjects", return_value=recent or []))
            p(patch("cqc_lem.utilities.db.get_recent_newsletter_blueprint_history",
                    return_value=shape_history or []))
            p(patch("cqc_lem.utilities.db.get_engagement_preferences", return_value={}))
            create = p(patch("cqc_lem.utilities.db.create_newsletter_edition", return_value=1))
            p(patch("cqc_lem.utilities.linkedin.helper.load_profile_for_user", return_value=MagicMock()))
            p(patch("cqc_lem.utilities.ai.ai_helper.get_or_create_profile_synthesis", return_value="brief"))
            plan = p(patch("cqc_lem.utilities.ai.ai_helper.plan_newsletter_topics", return_value=planned))
            p(patch("cqc_lem.utilities.ai.ai_helper.generate_newsletter_edition", side_effect=_gen))
            research_mock = p(patch("cqc_lem.utilities.ai.content_research.research_topic",
                                    return_value=research or {"findings": "", "sources": []}))
            p(patch("cqc_lem.utilities.newsletter.should_generate_now", return_value=True))
            p(patch("cqc_lem.utilities.notifications.notify_newsletter_draft_ready"))
            result = auto_generate_newsletter_drafts()
        self.research_mock = research_mock
        return result, create, plan

    def test_plans_then_persists_distinct_subjects(self):
        planned = [{"subject": "Alpha", "angle": "a"}, {"subject": "Beta", "angle": "b"}]
        result, create, plan = self._drive(planned)
        plan.assert_called_once()
        assert create.call_count == 2
        subjects = [c.kwargs["subject"] for c in create.call_args_list]
        assert subjects == ["Alpha", "Beta"]  # distinct, planned, and persisted
        assert "Generated 2 newsletter draft" in result

    def test_history_fed_into_planning(self):
        planned = [{"subject": "New1", "angle": "a"}]
        result, create, plan = self._drive(
            planned, pending=0, existing_subjects=["Queued Subj"], recent=["Published Subj"])
        prior = plan.call_args[0][3]  # (synthesis, description, prefs, prior_subjects, count)
        assert "Queued Subj" in prior and "Published Subj" in prior

    def test_falls_back_to_single_topic_when_planning_empty(self):
        # Planner returns [] (e.g. LLM failure) → still generate one edition per slot, subject=None ok.
        result, create, plan = self._drive([], pending=0)
        # _settings default max_queued_drafts=1 via max(1, 0)
        assert create.call_count == 1
        assert "Generated 1 newsletter draft" in result

    def test_blueprint_and_research_threaded_and_persisted(self):
        planned = [
            {"subject": "Alpha", "angle": "a", "format": "case_study", "hook_style": "micro_story",
             "cta_style": "reply_question"},
            {"subject": "Beta", "angle": "b", "format": "listicle", "hook_style": "bold_claim",
             "cta_style": "challenge"},
        ]
        captures = []
        result, create, _ = self._drive(
            planned, research={"findings": "Fresh stat: 42% (2026, Acme).", "sources": [{"url": "https://x"}]},
            gen_captures=captures)
        # Blueprint + research reach the writer for every edition.
        assert [c["blueprint"]["format"] for c in captures] == ["case_study", "listicle"]
        assert all(c["research"]["findings"].startswith("Fresh stat") for c in captures)
        # Exactly ONE research call per edition (cost guard).
        assert self.research_mock.call_count == 2
        # Shape persisted alongside the edition.
        kw = create.call_args_list[0].kwargs
        assert kw["edition_format"] == "case_study" and kw["hook_style"] == "micro_story"
        assert kw["opening_line"] == "Opening for Alpha"
        assert kw["blueprint"]["format"] == "case_study"
        assert "structure" not in kw["blueprint"]  # compact form persisted, skeleton lives in code
        assert "Generated 2 newsletter draft" in result

    def test_shape_history_fed_into_planning(self):
        history = [{"subject": "S1", "format": "deep_dive", "hook_style": "question",
                    "opening_line": "Old opener one."},
                   {"subject": "S2", "format": "framework", "hook_style": "surprising_stat",
                    "opening_line": "Old opener two."}]
        planned = [{"subject": "New", "angle": "x", "format": "case_study",
                    "hook_style": "micro_story", "cta_style": "debate"}]
        captures = []
        self._drive(planned, shape_history=history, gen_captures=captures)
        plan_kwargs = None
        # plan mock reference lives inside _drive's return; re-drive to inspect via captured kwargs
        _, _, plan = self._drive(planned, shape_history=history, gen_captures=[])
        plan_kwargs = plan.call_args.kwargs
        assert plan_kwargs["recent_formats"] == ["deep_dive", "framework"]
        assert plan_kwargs["recent_hook_styles"] == ["question", "surprising_stat"]
        # Prior openers are fed to the writer as an avoid list.
        assert "Old opener one." in captures[0]["avoid_openers"]
        assert "Old opener two." in captures[0]["avoid_openers"]

    def test_new_openers_accumulate_within_run(self):
        planned = [
            {"subject": "Alpha", "angle": "a", "format": "case_study", "hook_style": "micro_story",
             "cta_style": "reply_question"},
            {"subject": "Beta", "angle": "b", "format": "listicle", "hook_style": "bold_claim",
             "cta_style": "challenge"},
        ]
        captures = []
        self._drive(planned, gen_captures=captures)
        # The second edition must avoid the FIRST edition's just-written opening line too.
        assert "Opening for Alpha" in captures[1]["avoid_openers"]


def _run_regenerate(*, edition, guidance=None, others=None, recent=None, new_ed=None,
                    shape_history=None, research=None):
    from contextlib import ExitStack
    from cqc_lem.app.run_scheduler import regenerate_newsletter_edition
    if new_ed is None:
        new_ed = {"title": "New T", "subtitle": "New S", "subject": "New Subject", "body": "New B"}
    captured = {}

    def _gen(profile, topic=None, prefs=None, subject=None, avoid_subjects=None,
             profile_synthesis=None, guidance=None, blueprint=None, avoid_openers=None,
             research=None):
        captured["subject"] = subject
        captured["avoid"] = avoid_subjects
        captured["guidance"] = guidance
        captured["blueprint"] = blueprint
        captured["avoid_openers"] = avoid_openers
        captured["research"] = research
        return new_ed

    with ExitStack() as es:
        p = es.enter_context
        p(patch("cqc_lem.utilities.db.get_newsletter_edition", return_value=edition))
        p(patch("cqc_lem.utilities.db.get_newsletter_settings", return_value=_settings()))
        p(patch("cqc_lem.utilities.db.get_engagement_preferences", return_value={}))
        pending_eds = [{"id": o_id, "subject": s} for o_id, s in (others or [])]
        p(patch("cqc_lem.utilities.db.get_pending_newsletter_editions", return_value=pending_eds))
        p(patch("cqc_lem.utilities.db.get_recent_newsletter_subjects", return_value=recent or []))
        p(patch("cqc_lem.utilities.db.get_recent_newsletter_blueprint_history",
                return_value=shape_history or []))
        p(patch("cqc_lem.utilities.linkedin.helper.load_profile_for_user", return_value=MagicMock()))
        p(patch("cqc_lem.utilities.ai.ai_helper.get_or_create_profile_synthesis", return_value="brief"))
        p(patch("cqc_lem.utilities.ai.ai_helper.generate_newsletter_edition", side_effect=_gen))
        research_mock = p(patch("cqc_lem.utilities.ai.content_research.research_topic",
                                return_value=research or {"findings": "", "sources": []}))
        upd = p(patch("cqc_lem.utilities.db.update_newsletter_edition", return_value=True))
        result = regenerate_newsletter_edition.run(edition_id=edition["id"], guidance=guidance)
    captured["research_calls"] = research_mock.call_count
    return result, upd, captured


class TestRegenerateNewsletterEdition:
    def test_not_regenerable_when_published(self):
        result, upd, _ = _run_regenerate(
            edition={"id": 9, "user_id": 1, "status": "published", "subject": "X"})
        assert "not regenerable" in result
        upd.assert_not_called()

    def test_with_guidance_keeps_subject_and_applies_it(self):
        ed = {"id": 9, "user_id": 1, "status": "draft", "subject": "Original Subject"}
        result, upd, cap = _run_regenerate(edition=ed, guidance="Focus on hiring")
        assert cap["subject"] == "Original Subject"  # guidance -> current subject is the starting point
        assert cap["guidance"] == "Focus on hiring"
        assert upd.call_args.kwargs["status"] == "draft"  # resets to draft
        assert "Regenerated" in result

    def test_without_guidance_ai_decides_fresh(self):
        ed = {"id": 9, "user_id": 1, "status": "approved", "subject": "Original Subject"}
        result, upd, cap = _run_regenerate(edition=ed, guidance=None)
        assert cap["subject"] is None  # no guidance -> AI picks a fresh, distinct subject
        assert upd.call_args.kwargs["status"] == "draft"
        assert upd.call_args.kwargs["subject"] == "New Subject"  # persisted from the regenerated edition

    def test_avoids_other_editions_subjects(self):
        ed = {"id": 9, "user_id": 1, "status": "draft", "subject": "Mine"}
        result, upd, cap = _run_regenerate(
            edition=ed, others=[(9, "Mine"), (10, "Other Queued")], recent=["Past One"])
        assert "Other Queued" in cap["avoid"] and "Past One" in cap["avoid"]
        assert "Mine" not in cap["avoid"]  # this edition's own subject excluded

    def test_rotates_shape_away_from_history_and_persists(self):
        ed = {"id": 9, "user_id": 1, "status": "draft", "subject": "Mine"}
        history = [{"subject": "Mine", "format": "deep_dive", "hook_style": "question",
                    "opening_line": "Most founders treat X like Y."}]
        result, upd, cap = _run_regenerate(edition=ed, shape_history=history)
        # A fresh blueprint is built in code: not the edition's own previous shape.
        assert cap["blueprint"]["format"] != "deep_dive"
        assert cap["blueprint"]["hook_style"] != "question"
        assert "Most founders treat X like Y." in cap["avoid_openers"]
        # Exactly ONE research call per regenerate.
        assert cap["research_calls"] == 1
        # New shape persisted on the row (blueprint in compact form).
        kw = upd.call_args.kwargs
        assert kw["edition_format"] == cap["blueprint"]["format"] or kw["edition_format"] is None
        assert kw["blueprint"]["format"] == cap["blueprint"]["format"]
        assert "structure" not in kw["blueprint"]

    def test_guidance_format_hint_honored(self):
        ed = {"id": 9, "user_id": 1, "status": "draft", "subject": "Mine"}
        result, upd, cap = _run_regenerate(edition=ed, guidance="Rewrite this as a case study please")
        assert cap["blueprint"]["format"] == "case_study"

    def test_research_findings_reach_the_writer(self):
        ed = {"id": 9, "user_id": 1, "status": "draft", "subject": "Mine"}
        result, upd, cap = _run_regenerate(
            edition=ed, research={"findings": "In March 2026, 61% of B2B teams...", "sources": []})
        assert cap["research"]["findings"].startswith("In March 2026")


class TestPublishScheduledEditions:
    def test_dispatches_due_editions(self):
        from cqc_lem.app.run_scheduler import auto_publish_scheduled_editions
        with patch("cqc_lem.utilities.db.get_editions_due_to_publish",
                   return_value=[{"id": 3}, {"id": 8}]), \
             patch("cqc_lem.app.run_automation.auto_publish_edition") as task:
            result = auto_publish_scheduled_editions()
        assert task.apply_async.call_count == 2
        assert "2 newsletter edition" in result
