"""Unit tests for LinkedIn Catch-up automation (issue #482): classification, scoring, dedup,
approval gating, the capped send drip and the reply->funnel routing."""

import pytest
from unittest.mock import MagicMock, patch

pytestmark = pytest.mark.unit

_RA = "cqc_lem.app.run_automation"
_RS = "cqc_lem.app.run_scheduler"


def _prefs(**kw):
    base = {"max_comments_per_day": 50, "max_dms_per_day": 20, "max_catchup_touches_per_day": 5,
            "catchup_touch_mode": "pre_review", "catchup_message_source": "linkedin",
            "catchup_event_types": ["job_change", "promotion"],
            "focus_topics": [], "include_topics": [], "include_keywords": [],
            "exclude_topics": [], "exclude_keywords": [], "exclude_authors": []}
    base.update(kw)
    return base


def _moment(**kw):
    base = {"name": "Jane Doe", "profile_url": "https://www.linkedin.com/in/jane",
            "text": "Jane Doe started a new position as VP of Sales at Acme"}
    base.update(kw)
    return base


class TestClassifyCatchupMoment:
    @pytest.mark.parametrize("text,expected", [
        ("Jane Doe started a new position as VP of Sales at Acme", "job_change"),
        ("John Smith is now a Principal Engineer at Globex", "job_change"),
        ("Amy joined Initech as Head of Product", "job_change"),
        ("Dana was promoted to Director of Marketing at Acme", "promotion"),
        ("Sam is celebrating 5 years at Acme", "work_anniversary"),
        ("Kim celebrates their work anniversary", "work_anniversary"),
        ("Wish Pat a happy birthday", "birthday"),
        ("Lee graduated from MIT", "education"),
        ("Robin earned a Project Management certificate", "education"),
        ("Alex was featured in Forbes", "in_the_news"),
        ("Chris is in the news", "in_the_news"),
    ])
    def test_classifies_known_moments(self, text, expected):
        from cqc_lem.app.run_automation import _classify_catchup_moment
        assert _classify_catchup_moment(text) == expected

    @pytest.mark.parametrize("text", ["", None, "People you may know", "Suggested for you"])
    def test_returns_none_for_non_moments(self, text):
        from cqc_lem.app.run_automation import _classify_catchup_moment
        assert _classify_catchup_moment(text) is None

    def test_promotion_wins_over_new_position_phrasing(self):
        """'promoted to X at Y' also matches the new-position 'at' pattern — order must favour promotion."""
        from cqc_lem.app.run_automation import _classify_catchup_moment
        assert _classify_catchup_moment("Dana was promoted to VP and is now a VP at Acme") == "promotion"

    def test_anniversary_wins_over_new_position_phrasing(self):
        from cqc_lem.app.run_automation import _classify_catchup_moment
        assert _classify_catchup_moment("Sam is now celebrating 10 years at Acme") == "work_anniversary"


class TestCatchupEventPeriod:
    def test_annual_events_bucket_by_year(self):
        from cqc_lem.app.run_automation import _catchup_event_period
        from datetime import datetime, timezone
        now = datetime(2026, 7, 24, tzinfo=timezone.utc)
        assert _catchup_event_period("birthday", now) == "2026"
        assert _catchup_event_period("work_anniversary", now) == "2026"

    def test_one_off_events_bucket_by_month(self):
        from cqc_lem.app.run_automation import _catchup_event_period
        from datetime import datetime, timezone
        now = datetime(2026, 7, 24, tzinfo=timezone.utc)
        assert _catchup_event_period("job_change", now) == "2026-07"
        assert _catchup_event_period("promotion", now) == "2026-07"

    def test_defaults_to_now_when_no_timestamp_given(self):
        from cqc_lem.app.run_automation import _catchup_event_period
        assert len(_catchup_event_period("job_change")) == len("2026-07")


class TestNormalizeProfileUrl:
    @pytest.mark.parametrize("raw,expected", [
        ("https://www.linkedin.com/in/jane/?trk=abc", "https://www.linkedin.com/in/jane"),
        ("https://www.linkedin.com/in/jane#top", "https://www.linkedin.com/in/jane"),
        (" https://www.linkedin.com/in/jane/ ", "https://www.linkedin.com/in/jane"),
        ("", ""),
        (None, ""),
    ])
    def test_strips_tracking_and_trailing_slash(self, raw, expected):
        from cqc_lem.app.run_automation import _normalize_profile_url
        assert _normalize_profile_url(raw) == expected


class TestScoreCatchupMoment:
    def test_event_type_sets_the_base_score(self):
        from cqc_lem.app.run_automation import _score_catchup_moment
        job = _score_catchup_moment(_moment(event_type="job_change"), _prefs())
        birthday = _score_catchup_moment(_moment(event_type="birthday"), _prefs())
        assert job > birthday

    def test_literal_targeting_match_adds_icp_bonus_without_an_llm_call(self):
        from cqc_lem.app.run_automation import _score_catchup_moment
        with patch(f"{_RA}.post_is_relevant") as rel:
            plain = _score_catchup_moment(_moment(event_type="job_change"), _prefs())
            boosted = _score_catchup_moment(_moment(event_type="job_change"),
                                            _prefs(focus_topics=["sales"]))
        assert boosted == plain + 25
        rel.assert_not_called()

    def test_llm_relevance_only_runs_when_literal_match_missed(self):
        from cqc_lem.app.run_automation import _score_catchup_moment
        with patch(f"{_RA}.post_is_relevant", return_value=True) as rel:
            score = _score_catchup_moment(_moment(event_type="job_change"),
                                          _prefs(include_topics=["fintech"]))
        rel.assert_called_once()
        assert score == 50 + 15

    def test_no_llm_call_when_no_include_topics(self):
        from cqc_lem.app.run_automation import _score_catchup_moment
        with patch(f"{_RA}.post_is_relevant") as rel:
            assert _score_catchup_moment(_moment(event_type="promotion"), _prefs()) == 50
        rel.assert_not_called()


class TestCatchupExcluded:
    def test_excluded_author_is_skipped(self):
        from cqc_lem.app.run_automation import _catchup_excluded
        assert _catchup_excluded(_moment(), _prefs(exclude_authors=["Jane Doe"])) is True

    def test_excluded_keyword_is_skipped(self):
        from cqc_lem.app.run_automation import _catchup_excluded
        assert _catchup_excluded(_moment(), _prefs(exclude_keywords=["acme"])) is True

    def test_clean_moment_is_not_excluded(self):
        from cqc_lem.app.run_automation import _catchup_excluded
        assert _catchup_excluded(_moment(), _prefs(exclude_keywords=["  "])) is False


class TestScrapeCatchupMoments:
    @pytest.fixture(autouse=True)
    def _no_sleeps(self):
        """The scrape paces itself for LinkedIn; the unit lane must stay hermetic AND fast (#480)."""
        with patch("time.sleep"):
            yield

    def _card(self, text, href="https://www.linkedin.com/in/jane?trk=x", link_text="Jane Doe"):
        link = MagicMock()
        link.get_attribute.return_value = href
        link.text = link_text
        card = MagicMock()
        card.find_elements.return_value = [link]
        card.text = text
        return card

    def test_collects_and_dedupes_cards(self):
        from cqc_lem.app.run_automation import _scrape_catchup_moments
        card = self._card("Jane Doe started a new position at Acme")
        driver = MagicMock()
        with patch(f"{_RA}.find_all_first", return_value=[card, card]):
            moments = _scrape_catchup_moments(driver, max_moments=10, user_id=1)
        assert len(moments) == 1
        assert moments[0]["profile_url"] == "https://www.linkedin.com/in/jane"
        assert moments[0]["name"] == "Jane Doe"

    def test_skips_cards_without_a_profile_link(self):
        from cqc_lem.app.run_automation import _scrape_catchup_moments
        card = MagicMock()
        card.find_elements.return_value = []
        driver = MagicMock()
        with patch(f"{_RA}.find_all_first", return_value=[card]), \
             patch(f"{_RA}.log_warning") as warn:
            assert _scrape_catchup_moments(driver, max_moments=10, user_id=1) == []
        warn.assert_called_once()

    def test_stops_at_max_moments(self):
        from cqc_lem.app.run_automation import _scrape_catchup_moments
        cards = [self._card(f"Person {i} started a new position at Acme",
                            href=f"https://www.linkedin.com/in/p{i}", link_text=f"Person {i}")
                 for i in range(5)]
        driver = MagicMock()
        with patch(f"{_RA}.find_all_first", return_value=cards):
            moments = _scrape_catchup_moments(driver, max_moments=2, user_id=1)
        assert len(moments) == 2

    def test_classifies_each_card_and_skips_harvesting_when_no_types_are_enabled(self):
        from cqc_lem.app.run_automation import _scrape_catchup_moments
        card = self._card("Jane Doe started a new position at Acme")
        driver = MagicMock()
        with patch(f"{_RA}.find_all_first", return_value=[card]), \
             patch(f"{_RA}._harvest_linkedin_draft") as harvest:
            moments = _scrape_catchup_moments(driver, max_moments=10, user_id=1)
        assert moments[0]["event_type"] == "job_change"
        assert moments[0]["suggested_message"] == ""
        harvest.assert_not_called()

    def test_harvests_linkedins_draft_only_for_enabled_milestone_types(self):
        from cqc_lem.app.run_automation import _scrape_catchup_moments
        job = self._card("Jane Doe started a new position at Acme")
        birthday = self._card("Wish Pat a happy birthday", href="https://www.linkedin.com/in/pat",
                              link_text="Pat Roe")
        driver = MagicMock()
        with patch(f"{_RA}.find_all_first", return_value=[job, birthday]), \
             patch(f"{_RA}._card_suggested_message", return_value=""), \
             patch(f"{_RA}._harvest_linkedin_draft", return_value="Congrats on the new role!") as harvest:
            moments = _scrape_catchup_moments(driver, max_moments=10, user_id=1,
                                              enabled_event_types={"job_change"})
        assert harvest.call_count == 1
        assert moments[0]["suggested_message"] == "Congrats on the new role!"
        assert moments[1]["suggested_message"] == ""

    def test_card_chip_wins_over_opening_the_composer(self):
        from cqc_lem.app.run_automation import _scrape_catchup_moments
        card = self._card("Jane Doe started a new position at Acme")
        driver = MagicMock()
        with patch(f"{_RA}.find_all_first", return_value=[card]), \
             patch(f"{_RA}._card_suggested_message", return_value="Congrats Jane!"), \
             patch(f"{_RA}._harvest_linkedin_draft") as harvest:
            moments = _scrape_catchup_moments(driver, max_moments=10, user_id=1,
                                              enabled_event_types={"job_change"})
        assert moments[0]["suggested_message"] == "Congrats Jane!"
        harvest.assert_not_called()


class TestLinkedInDraftHarvest:
    """LinkedIn writes the congratulations; we only read it (PR #509 owner review)."""

    @pytest.fixture(autouse=True)
    def _no_sleeps(self):
        with patch("time.sleep"):
            yield

    @pytest.mark.parametrize("raw,expected", [
        ("  Congrats on the\n new role, Jane! ", "Congrats on the new role, Jane!"),
        ("", ""),
        ("Hi", ""),          # a bare label is not a message
        ("x" * 400, "x" * 300),
    ])
    def test_clean_suggested_message(self, raw, expected):
        from cqc_lem.app.run_automation import _clean_suggested_message
        assert _clean_suggested_message(raw) == expected

    def test_card_suggestion_is_read_without_clicking(self):
        from cqc_lem.app.run_automation import _card_suggested_message
        chip = MagicMock()
        chip.text = "Congrats on the promotion!"
        card = MagicMock()
        card.find_elements.return_value = [chip]
        assert _card_suggested_message(card) == "Congrats on the promotion!"
        chip.click.assert_not_called()

    def test_card_without_a_suggestion_returns_empty(self):
        from cqc_lem.app.run_automation import _card_suggested_message
        card = MagicMock()
        card.find_elements.return_value = []
        assert _card_suggested_message(card) == ""

    def _trigger(self, label="Say congrats"):
        trigger = MagicMock()
        trigger.text = label
        trigger.get_attribute.return_value = label
        return trigger

    def test_opens_the_composer_reads_the_draft_and_dismisses_without_sending(self):
        from cqc_lem.app.run_automation import _harvest_linkedin_draft
        trigger = self._trigger()
        card = MagicMock()
        card.find_elements.return_value = [trigger]
        compose = MagicMock()
        compose.text = "Congrats on the new role, Jane!"
        dismiss = MagicMock()
        driver = MagicMock()
        driver.find_elements.side_effect = [[compose], [dismiss]]
        assert _harvest_linkedin_draft(driver, card, user_id=1) == "Congrats on the new role, Jane!"
        trigger.click.assert_called_once()
        dismiss.click.assert_called_once()

    def test_never_clicks_a_control_that_isnt_the_congrats_composer(self):
        """A drifted selector must not let us click Connect/Follow/Send."""
        from cqc_lem.app.run_automation import _harvest_linkedin_draft
        trigger = self._trigger(label="Follow")
        card = MagicMock()
        card.find_elements.return_value = [trigger]
        driver = MagicMock()
        assert _harvest_linkedin_draft(driver, card, user_id=1) == ""
        trigger.click.assert_not_called()

    def test_no_trigger_on_the_card_returns_empty(self):
        from cqc_lem.app.run_automation import _harvest_linkedin_draft
        card = MagicMock()
        card.find_elements.return_value = []
        assert _harvest_linkedin_draft(MagicMock(), card, user_id=1) == ""

    def test_missing_composer_still_dismisses_and_returns_empty(self):
        from cqc_lem.app.run_automation import _harvest_linkedin_draft
        trigger = self._trigger()
        card = MagicMock()
        card.find_elements.return_value = [trigger]
        driver = MagicMock()
        driver.find_elements.return_value = []
        with patch(f"{_RA}.ActionChains") as chains:
            assert _harvest_linkedin_draft(driver, card, user_id=1) == ""
        chains.assert_called_once()  # fell back to Escape

    def test_a_selenium_error_is_swallowed(self):
        from cqc_lem.app.run_automation import _harvest_linkedin_draft
        from selenium.common import ElementNotInteractableException
        trigger = self._trigger()
        trigger.click.side_effect = ElementNotInteractableException("nope")
        card = MagicMock()
        card.find_elements.return_value = [trigger]
        with patch(f"{_RA}.log_warning") as warn:
            assert _harvest_linkedin_draft(MagicMock(), card, user_id=1) == ""
        warn.assert_called_once()

    def test_harvesting_can_be_switched_off(self):
        from cqc_lem.app import run_automation
        card = MagicMock()
        with patch.object(run_automation, "CATCHUP_HARVEST_LINKEDIN_DRAFT", False):
            assert run_automation._harvest_linkedin_draft(MagicMock(), card, user_id=1) == ""
        card.find_elements.assert_not_called()


class TestDraftCatchupMessage:
    @pytest.mark.parametrize("event_type,expected", [
        ("job_change", "Congrats on the new role, Jane!"),
        ("promotion", "Congrats on the promotion, Jane!"),
        ("work_anniversary", "Happy work anniversary, Jane!"),
        ("birthday", "Happy birthday, Jane!"),
        ("education", "Congrats on the milestone, Jane!"),
        ("in_the_news", "Great to see you in the news, Jane!"),
    ])
    def test_every_milestone_has_a_linkedin_style_fallback(self, event_type, expected):
        from cqc_lem.app.run_automation import _draft_catchup_message
        moment = _moment(event_type=event_type)
        with patch(f"{_RA}.build_dm_from_template") as ai:
            assert _draft_catchup_message(1, moment, MagicMock()) == expected
        ai.assert_not_called()

    def test_linkedins_own_draft_wins_over_the_fallback(self):
        from cqc_lem.app.run_automation import _draft_catchup_message
        moment = _moment(event_type="job_change", suggested_message="Congrats Jane — huge news!")
        assert _draft_catchup_message(1, moment, MagicMock()) == "Congrats Jane — huge news!"

    def test_unknown_event_type_without_a_suggestion_drafts_nothing(self):
        from cqc_lem.app.run_automation import _draft_catchup_message
        assert _draft_catchup_message(1, _moment(event_type="mystery"), MagicMock()) is None

    def test_ai_source_delegates_to_the_dm_template(self):
        from cqc_lem.app.run_automation import _draft_catchup_message
        moment = _moment(event_type="job_change", suggested_message="ignored")
        with patch(f"{_RA}.build_dm_from_template", return_value="In my voice") as ai:
            assert _draft_catchup_message(1, moment, MagicMock(), source="ai") == "In my voice"
        assert ai.call_args.kwargs["event_detail"] == moment["text"]


class TestAutomateCatchupTouches:
    def _patches(self, moments, prefs=None):
        prefs = prefs or _prefs()
        return {
            "prefs": patch(f"{_RA}.get_engagement_preferences", return_value=prefs),
            "profile": patch(f"{_RA}.get_current_profile",
                             return_value=(MagicMock(), MagicMock(), "a@b.c", MagicMock())),
            "scrape": patch(f"{_RA}._scrape_catchup_moments", return_value=moments),
            "quit": patch(f"{_RA}.quit_gracefully"),
            "has": patch(f"{_RA}.has_catchup_touch", return_value=False),
            "draft": patch(f"{_RA}.build_dm_from_template", return_value="Congrats Jane!"),
            "insert": patch(f"{_RA}.insert_catchup_touch", return_value=7),
        }

    def test_drafts_pending_touch_from_linkedins_own_suggestion(self):
        from cqc_lem.app.run_automation import automate_catchup_touches
        from cqc_lem.utilities.db import CatchupTouchStatus
        p = self._patches([_moment(suggested_message="Congrats on the new role, Jane!")])
        with p["prefs"], p["profile"], p["scrape"], p["quit"], p["has"], \
             p["draft"] as ai_draft, p["insert"] as ins:
            out = automate_catchup_touches.run(user_id=1)
        assert "1 drafted" in out
        kwargs = ins.call_args.kwargs
        assert kwargs["status"] == CatchupTouchStatus.PENDING
        assert kwargs["message"] == "Congrats on the new role, Jane!"
        assert ins.call_args.args[2] == "job_change"
        ai_draft.assert_not_called()  # the default path costs no LLM call

    def test_ai_source_uses_the_dm_template_path(self):
        from cqc_lem.app.run_automation import automate_catchup_touches
        p = self._patches([_moment(suggested_message="Congrats on the new role, Jane!")],
                          prefs=_prefs(catchup_message_source="ai"))
        with p["prefs"], p["profile"], p["scrape"], p["quit"], p["has"], \
             p["draft"] as ai_draft, p["insert"] as ins:
            automate_catchup_touches.run(user_id=1)
        ai_draft.assert_called_once()
        assert ins.call_args.kwargs["message"] == "Congrats Jane!"

    def test_auto_approve_mode_queues_the_draft(self):
        from cqc_lem.app.run_automation import automate_catchup_touches
        from cqc_lem.utilities.db import CatchupTouchStatus
        p = self._patches([_moment()], prefs=_prefs(catchup_touch_mode="auto_approve"))
        with p["prefs"], p["profile"], p["scrape"], p["quit"], p["has"], p["draft"], \
             p["insert"] as ins:
            automate_catchup_touches.run(user_id=1)
        assert ins.call_args.kwargs["status"] == CatchupTouchStatus.APPROVED

    def test_disabled_event_type_is_never_drafted(self):
        from cqc_lem.app.run_automation import automate_catchup_touches
        p = self._patches([_moment(text="Wish Jane a happy birthday")])
        with p["prefs"], p["profile"], p["scrape"], p["quit"], p["has"], p["draft"], \
             p["insert"] as ins:
            automate_catchup_touches.run(user_id=1)
        ins.assert_not_called()

    def test_low_score_moment_is_tombstoned_not_drafted(self):
        from cqc_lem.app.run_automation import automate_catchup_touches
        from cqc_lem.utilities.db import CatchupTouchStatus
        prefs = _prefs(catchup_event_types=["birthday"])
        p = self._patches([_moment(text="Wish Jane a happy birthday")], prefs=prefs)
        with p["prefs"], p["profile"], p["scrape"], p["quit"], p["has"], \
             p["draft"] as draft, p["insert"] as ins:
            out = automate_catchup_touches.run(user_id=1)
        draft.assert_not_called()
        assert ins.call_args.kwargs["status"] == CatchupTouchStatus.SKIPPED
        assert "1 below the bar" in out

    def test_already_touched_milestone_is_skipped(self):
        from cqc_lem.app.run_automation import automate_catchup_touches
        p = self._patches([_moment()])
        with p["prefs"], p["profile"], p["scrape"], p["quit"], \
             patch(f"{_RA}.has_catchup_touch", return_value=True), p["draft"], p["insert"] as ins:
            automate_catchup_touches.run(user_id=1)
        ins.assert_not_called()

    def test_excluded_author_is_skipped(self):
        from cqc_lem.app.run_automation import automate_catchup_touches
        p = self._patches([_moment()], prefs=_prefs(exclude_authors=["Jane"]))
        with p["prefs"], p["profile"], p["scrape"], p["quit"], p["has"], p["draft"], \
             p["insert"] as ins:
            automate_catchup_touches.run(user_id=1)
        ins.assert_not_called()

    def test_no_enabled_event_types_short_circuits_before_selenium(self):
        from cqc_lem.app.run_automation import automate_catchup_touches
        p = self._patches([_moment()], prefs=_prefs(catchup_event_types=[]))
        with p["prefs"], p["profile"] as prof, p["scrape"], p["quit"], p["has"], p["draft"], p["insert"]:
            out = automate_catchup_touches.run(user_id=1)
        prof.assert_not_called()
        assert "disabled" in out

    def test_max_drafts_caps_a_run(self):
        from cqc_lem.app.run_automation import automate_catchup_touches
        moments = [_moment(profile_url=f"https://www.linkedin.com/in/p{i}") for i in range(5)]
        p = self._patches(moments)
        with p["prefs"], p["profile"], p["scrape"], p["quit"], p["has"], p["draft"], p["insert"] as ins:
            automate_catchup_touches.run(user_id=1, max_drafts=2)
        assert ins.call_count == 2

    def test_throttled_session_defers_without_scraping(self):
        from cqc_lem.app.run_automation import automate_catchup_touches
        from cqc_lem.utilities.linkedin.rate_limit import LinkedInRateLimited
        p = self._patches([_moment()])
        with p["prefs"], patch(f"{_RA}.get_current_profile", side_effect=LinkedInRateLimited("429")), \
             p["scrape"] as scrape, p["quit"], p["has"], p["draft"], p["insert"]:
            out = automate_catchup_touches.run(user_id=1)
        scrape.assert_not_called()
        assert "deferred" in out

    def test_missing_template_skips_the_moment_in_ai_mode(self):
        from cqc_lem.app.run_automation import automate_catchup_touches
        p = self._patches([_moment()], prefs=_prefs(catchup_message_source="ai"))
        with p["prefs"], p["profile"], p["scrape"], p["quit"], p["has"], \
             patch(f"{_RA}.build_dm_from_template", return_value=None), p["insert"] as ins:
            automate_catchup_touches.run(user_id=1)
        ins.assert_not_called()

    def test_no_linkedin_suggestion_falls_back_to_the_plain_congratulations(self):
        """LinkedIn didn't surface a draft — we still send its kind of one-liner, not an AI rewrite."""
        from cqc_lem.app.run_automation import automate_catchup_touches
        p = self._patches([_moment()])
        with p["prefs"], p["profile"], p["scrape"], p["quit"], p["has"], \
             p["draft"] as ai_draft, p["insert"] as ins:
            automate_catchup_touches.run(user_id=1)
        assert ins.call_args.kwargs["message"] == "Congrats on the new role, Jane!"
        ai_draft.assert_not_called()

    def test_enabled_types_are_passed_to_the_scrape_so_only_those_are_harvested(self):
        from cqc_lem.app.run_automation import automate_catchup_touches
        p = self._patches([_moment()])
        with p["prefs"], p["profile"], p["scrape"] as scrape, p["quit"], p["has"], p["draft"], p["insert"]:
            automate_catchup_touches.run(user_id=1)
        assert scrape.call_args.kwargs["enabled_event_types"] == {"job_change", "promotion"}

    def test_scrape_failure_still_quits_the_driver(self):
        from cqc_lem.app.run_automation import automate_catchup_touches
        p = self._patches([_moment()])
        with p["prefs"], p["profile"], patch(f"{_RA}._scrape_catchup_moments", side_effect=RuntimeError("boom")), \
             p["quit"] as quit_driver, p["has"], p["draft"], p["insert"], patch(f"{_RA}.log_error"):
            out = automate_catchup_touches.run(user_id=1)
        quit_driver.assert_called_once()
        assert "failed" in out


class TestSendCatchupTouch:
    def _touch(self, **kw):
        base = {"id": 3, "user_id": 1, "profile_url": "https://www.linkedin.com/in/jane",
                "person_name": "Jane Doe", "event_type": "job_change", "message": "Congrats Jane!",
                "status": "approved"}
        base.update(kw)
        return base

    def _patches(self, touch, sent=True, catchup_today=0, dms_today=0, prefs=None, allowance=5):
        return {
            "get": patch(f"{_RA}.get_catchup_touch", return_value=touch),
            "prefs": patch(f"{_RA}.get_engagement_preferences", return_value=prefs or _prefs()),
            "allow": patch(f"{_RA}.max_catchup_touches_allowed", return_value=allowance),
            "cnt": patch(f"{_RA}.count_catchup_touches_sent_today", return_value=catchup_today),
            "dms": patch(f"{_RA}.count_dms_sent_today", return_value=dms_today),
            "send": patch(f"{_RA}.send_dm_now", return_value=sent),
            "upd": patch(f"{_RA}.update_catchup_touch_status"),
            "enq": patch(f"{_RA}.enqueue_next_followup"),
        }

    def test_sends_and_marks_sent(self):
        from cqc_lem.app.run_automation import send_catchup_touch
        from cqc_lem.utilities.db import CatchupTouchStatus
        p = self._patches(self._touch())
        with p["get"], p["prefs"], p["allow"], p["cnt"], p["dms"], p["send"] as send, p["upd"] as upd, p["enq"] as enq:
            out = send_catchup_touch.run(touch_id=3)
        send.assert_called_once_with(1, "https://www.linkedin.com/in/jane", "Congrats Jane!")
        upd.assert_called_once_with(3, CatchupTouchStatus.SENT)
        enq.assert_called_once()
        assert "sent" in out

    def test_failed_send_marks_failed_and_skips_followup(self):
        from cqc_lem.app.run_automation import send_catchup_touch
        from cqc_lem.utilities.db import CatchupTouchStatus
        p = self._patches(self._touch(), sent=False)
        with p["get"], p["prefs"], p["allow"], p["cnt"], p["dms"], p["send"], p["upd"] as upd, p["enq"] as enq:
            out = send_catchup_touch.run(touch_id=3)
        upd.assert_called_once_with(3, CatchupTouchStatus.FAILED)
        enq.assert_not_called()
        assert "failed" in out

    def test_catchup_cap_defers_back_to_approved(self):
        from cqc_lem.app.run_automation import send_catchup_touch
        from cqc_lem.utilities.db import CatchupTouchStatus
        p = self._patches(self._touch(), catchup_today=5)
        with p["get"], p["prefs"], p["allow"], p["cnt"], p["dms"], p["send"] as send, p["upd"] as upd, p["enq"]:
            out = send_catchup_touch.run(touch_id=3)
        send.assert_not_called()
        upd.assert_called_once_with(3, CatchupTouchStatus.APPROVED)
        assert "deferred" in out

    def test_dm_cap_defers_back_to_approved(self):
        from cqc_lem.app.run_automation import send_catchup_touch
        from cqc_lem.utilities.db import CatchupTouchStatus
        p = self._patches(self._touch(), dms_today=20)
        with p["get"], p["prefs"], p["allow"], p["cnt"], p["dms"], p["send"] as send, p["upd"] as upd, p["enq"]:
            out = send_catchup_touch.run(touch_id=3)
        send.assert_not_called()
        upd.assert_called_once_with(3, CatchupTouchStatus.APPROVED)
        assert "DM cap" in out

    def test_throttle_defers_back_to_approved(self):
        from cqc_lem.app.run_automation import send_catchup_touch
        from cqc_lem.utilities.db import CatchupTouchStatus
        from cqc_lem.utilities.linkedin.rate_limit import LinkedInRateLimited
        p = self._patches(self._touch())
        with p["get"], p["prefs"], p["allow"], p["cnt"], p["dms"], \
             patch(f"{_RA}.send_dm_now", side_effect=LinkedInRateLimited("429")), \
             p["upd"] as upd, p["enq"]:
            out = send_catchup_touch.run(touch_id=3)
        upd.assert_called_once_with(3, CatchupTouchStatus.APPROVED)
        assert "throttled" in out

    def test_unapproved_touch_is_never_sent(self):
        from cqc_lem.app.run_automation import send_catchup_touch
        p = self._patches(self._touch(status="pending"))
        with p["get"], p["prefs"], p["allow"], p["cnt"], p["dms"], p["send"] as send, p["upd"], p["enq"]:
            out = send_catchup_touch.run(touch_id=3)
        send.assert_not_called()
        assert "not sendable" in out

    def test_missing_touch_is_handled(self):
        from cqc_lem.app.run_automation import send_catchup_touch
        p = self._patches(None)
        with p["get"], p["prefs"], p["allow"], p["cnt"], p["dms"], p["send"] as send, p["upd"], p["enq"]:
            out = send_catchup_touch.run(touch_id=3)
        send.assert_not_called()
        assert "missing" in out

    def test_saved_cap_above_the_plan_allowance_is_pulled_back_down(self):
        """A downgrade must bite immediately: a saved 10/day on a standard plan still stops at 5."""
        from cqc_lem.app.run_automation import send_catchup_touch
        from cqc_lem.utilities.db import CatchupTouchStatus
        p = self._patches(self._touch(), catchup_today=5, allowance=5,
                          prefs=_prefs(max_catchup_touches_per_day=10))
        with p["get"], p["prefs"], p["allow"], p["cnt"], p["dms"], p["send"] as send, p["upd"] as upd, p["enq"]:
            out = send_catchup_touch.run(touch_id=3)
        send.assert_not_called()
        upd.assert_called_once_with(3, CatchupTouchStatus.APPROVED)
        assert "catch-up cap" in out

    def test_premium_allowance_lets_the_higher_cap_through(self):
        from cqc_lem.app.run_automation import send_catchup_touch
        p = self._patches(self._touch(), catchup_today=5, allowance=10,
                          prefs=_prefs(max_catchup_touches_per_day=10))
        with p["get"], p["prefs"], p["allow"], p["cnt"], p["dms"], p["send"] as send, p["upd"], p["enq"]:
            out = send_catchup_touch.run(touch_id=3)
        send.assert_called_once()
        assert "sent" in out

    def test_empty_message_is_skipped(self):
        from cqc_lem.app.run_automation import send_catchup_touch
        from cqc_lem.utilities.db import CatchupTouchStatus
        p = self._patches(self._touch(message="   "))
        with p["get"], p["prefs"], p["allow"], p["cnt"], p["dms"], p["send"] as send, p["upd"] as upd, p["enq"]:
            out = send_catchup_touch.run(touch_id=3)
        send.assert_not_called()
        upd.assert_called_once_with(3, CatchupTouchStatus.SKIPPED)
        assert "no message" in out


class TestRouteRepliedCatchupToFunnel:
    def _followup(self, event_type="job_change"):
        return {"user_id": 1, "profile_url": "https://www.linkedin.com/in/jane",
                "first_name": "Jane", "event_type": event_type}

    def test_high_value_reply_enters_the_funnel_at_the_dm_stage(self):
        from cqc_lem.app.run_automation import _route_replied_catchup_to_funnel
        from cqc_lem.utilities.db import OutreachStage, OutreachStatus
        with patch(f"{_RA}.get_outreach_target_by_url", return_value=None), \
             patch(f"{_RA}._draft_funnel_stage", return_value="draft dm"), \
             patch(f"{_RA}.insert_outreach_target") as ins:
            _route_replied_catchup_to_funnel(1, self._followup())
        assert ins.call_args.kwargs["stage"] == OutreachStage.DM
        assert ins.call_args.kwargs["status"] == OutreachStatus.PENDING
        assert ins.call_args.kwargs["draft_text"] == "draft dm"

    def test_low_value_event_is_not_routed(self):
        from cqc_lem.app.run_automation import _route_replied_catchup_to_funnel
        with patch(f"{_RA}.insert_outreach_target") as ins:
            _route_replied_catchup_to_funnel(1, self._followup(event_type="birthday"))
        ins.assert_not_called()

    def test_existing_funnel_target_is_not_duplicated(self):
        from cqc_lem.app.run_automation import _route_replied_catchup_to_funnel
        with patch(f"{_RA}.get_outreach_target_by_url", return_value={"id": 9}), \
             patch(f"{_RA}.insert_outreach_target") as ins:
            _route_replied_catchup_to_funnel(1, self._followup())
        ins.assert_not_called()

    def test_failure_never_propagates(self):
        from cqc_lem.app.run_automation import _route_replied_catchup_to_funnel
        with patch(f"{_RA}.get_outreach_target_by_url", side_effect=RuntimeError("db down")), \
             patch(f"{_RA}.log_warning") as warn:
            _route_replied_catchup_to_funnel(1, self._followup())
        warn.assert_called_once()


class TestCatchupDispatchers:
    def test_scan_dispatcher_skips_users_with_no_enabled_types(self):
        from cqc_lem.app.run_scheduler import auto_scan_catchup_moments
        prefs = {1: _prefs(), 2: _prefs(catchup_event_types=[])}
        with patch(f"{_RS}._skip_if_throttled", return_value=False), \
             patch(f"{_RS}.get_active_user_ids", return_value=[1, 2]), \
             patch(f"{_RS}.get_engagement_preferences", side_effect=lambda uid: prefs[uid]), \
             patch(f"{_RA}.automate_catchup_touches") as task:
            out = auto_scan_catchup_moments()
        assert task.apply_async.call_count == 1
        assert "1 user" in out

    def test_scan_dispatcher_short_circuits_when_throttled(self):
        from cqc_lem.app.run_scheduler import auto_scan_catchup_moments
        with patch(f"{_RS}._skip_if_throttled", return_value=True), \
             patch(f"{_RA}.automate_catchup_touches") as task:
            assert auto_scan_catchup_moments() == "Automation throttled"
        task.apply_async.assert_not_called()

    def test_send_scanner_respects_the_daily_cap(self):
        from cqc_lem.app.run_scheduler import auto_check_catchup_touches
        with patch(f"{_RS}._skip_if_throttled", return_value=False), \
             patch(f"{_RS}.get_approved_catchup_touches", return_value=[(1, 7), (2, 7), (3, 7)]), \
             patch(f"{_RS}.get_active_user_ids", return_value=[7]), \
             patch(f"{_RS}.get_engagement_preferences",
                   return_value=_prefs(max_catchup_touches_per_day=2)), \
             patch(f"{_RS}.max_catchup_touches_allowed", return_value=5), \
             patch(f"{_RS}.count_catchup_touches_sent_today", return_value=0), \
             patch(f"{_RS}.get_orphaned_catchup_touches", return_value=[]), \
             patch(f"{_RS}.update_catchup_touch_status"), \
             patch(f"{_RS}.send_catchup_touch") as task:
            out = auto_check_catchup_touches()
        assert task.apply_async.call_count == 2
        assert "Dispatched 2" in out

    def test_send_scanner_budget_is_bounded_by_the_plan_allowance(self):
        """Saved cap 10 but a standard plan — the drip still dispatches at most 5 that day."""
        from cqc_lem.app.run_scheduler import auto_check_catchup_touches
        approved = [(i, 7) for i in range(1, 9)]
        with patch(f"{_RS}._skip_if_throttled", return_value=False), \
             patch(f"{_RS}.get_approved_catchup_touches", return_value=approved), \
             patch(f"{_RS}.get_active_user_ids", return_value=[7]), \
             patch(f"{_RS}.get_engagement_preferences",
                   return_value=_prefs(max_catchup_touches_per_day=10)), \
             patch(f"{_RS}.max_catchup_touches_allowed", return_value=5), \
             patch(f"{_RS}.count_catchup_touches_sent_today", return_value=0), \
             patch(f"{_RS}.get_orphaned_catchup_touches", return_value=[]), \
             patch(f"{_RS}.update_catchup_touch_status"), \
             patch(f"{_RS}.send_catchup_touch") as task:
            auto_check_catchup_touches()
        assert task.apply_async.call_count == 5

    def test_send_scanner_skips_inactive_users(self):
        from cqc_lem.app.run_scheduler import auto_check_catchup_touches
        with patch(f"{_RS}._skip_if_throttled", return_value=False), \
             patch(f"{_RS}.get_approved_catchup_touches", return_value=[(1, 7)]), \
             patch(f"{_RS}.get_active_user_ids", return_value=[]), \
             patch(f"{_RS}.get_orphaned_catchup_touches", return_value=[]), \
             patch(f"{_RS}.update_catchup_touch_status"), \
             patch(f"{_RS}.log_warning"), \
             patch(f"{_RS}.send_catchup_touch") as task:
            out = auto_check_catchup_touches()
        task.apply_async.assert_not_called()
        assert out == "No Catch-up Touches to Send"

    def test_send_scanner_requeues_orphans(self):
        from cqc_lem.app.run_scheduler import auto_check_catchup_touches
        from cqc_lem.utilities.db import CatchupTouchStatus
        with patch(f"{_RS}._skip_if_throttled", return_value=False), \
             patch(f"{_RS}.get_approved_catchup_touches", return_value=[]), \
             patch(f"{_RS}.get_active_user_ids", return_value=[7]), \
             patch(f"{_RS}.get_orphaned_catchup_touches", return_value=[(9, 7)]), \
             patch(f"{_RS}.update_catchup_touch_status") as upd, \
             patch(f"{_RS}.log_warning"), \
             patch(f"{_RS}.send_catchup_touch") as task:
            out = auto_check_catchup_touches()
        upd.assert_called_once_with(9, CatchupTouchStatus.SENDING)
        task.apply_async.assert_called_once()
        assert "1 orphaned" in out

    def test_send_scanner_short_circuits_when_throttled(self):
        from cqc_lem.app.run_scheduler import auto_check_catchup_touches
        with patch(f"{_RS}._skip_if_throttled", return_value=True), \
             patch(f"{_RS}.send_catchup_touch") as task:
            assert auto_check_catchup_touches() == "Automation throttled"
        task.apply_async.assert_not_called()


class TestCatchupBeatSchedule:
    def test_beat_schedule_registers_both_catchup_jobs(self):
        from cqc_lem.app.my_celery import app
        schedule = app.conf.beat_schedule
        assert schedule["scan-catchup-moments"]["task"] == "cqc_lem.app.run_scheduler.auto_scan_catchup_moments"
        assert schedule["send-catchup-touches"]["task"] == "cqc_lem.app.run_scheduler.auto_check_catchup_touches"
