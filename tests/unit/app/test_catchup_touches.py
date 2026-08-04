"""Unit tests for LinkedIn Catch-up automation (issue #482): classification, scoring, dedup,
approval gating, the capped send drip and the reply->funnel routing."""

import pytest
from unittest.mock import MagicMock, patch

from cqc_lem.utilities.linkedin.message_thread import ThreadState

pytestmark = pytest.mark.unit

_RA = "cqc_lem.app.run_automation"
_RS = "cqc_lem.app.run_scheduler"


@pytest.fixture(autouse=True)
def _no_pending_backlog():
    """The send drip counts the drafted-but-unapproved backlog on every beat (issue #792). Default it
    to an empty queue so only the tests that care about it have to say so."""
    with patch(f"{_RS}.count_pending_catchup_touches", return_value=0):
        yield


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
        """An empty feed is a normal day and the scan runs daily per user, so this is DEBUG — three
        WARNINGs inside the escalation window re-emit at ERROR and file a grouped $exception for a
        no-op. The `no_moments` run report (issue #792) is what carries it."""
        from cqc_lem.app.run_automation import _scrape_catchup_moments
        card = MagicMock()
        card.find_elements.return_value = []
        driver = MagicMock()
        with patch(f"{_RA}.find_all_first", return_value=[card]), \
             patch(f"{_RA}.log_warning") as warn, patch(f"{_RA}.log_debug") as debug:
            assert _scrape_catchup_moments(driver, max_moments=10, user_id=1) == []
        debug.assert_called_once()
        warn.assert_not_called()

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

    @pytest.mark.parametrize("chrome", [
        "Say congrats", "Say congrats to Jane", "Say congrats to Jane Doe", "Congrats!",
        "Congratulate Jane", "Message", "Send a message", "Send message to Jane",
        "Write a message…", "Reply", "Send", "View profile", "See more",
    ])
    def test_button_chrome_is_never_mistaken_for_a_message(self, chrome):
        """`button[aria-label*='congrats']` matches LinkedIn's OWN trigger, and an empty composer
        renders its placeholder — both clear the length floor, so without this the card's chrome
        becomes the congratulations we queue (and, on auto-approve, SEND). Issue #792."""
        from cqc_lem.app.run_automation import _clean_suggested_message
        assert _clean_suggested_message(chrome) == ""

    @pytest.mark.parametrize("real", [
        "Congrats on the new role, Jane!",
        "Congrats to Jane on the new role!",
        "Happy work anniversary, Sam!",
        "Congratulations on the promotion — well earned.",
    ])
    def test_a_real_draft_survives_the_chrome_filter(self, real):
        from cqc_lem.app.run_automation import _clean_suggested_message
        assert _clean_suggested_message(real) == real

    def test_a_chrome_only_card_falls_back_to_the_plain_congratulations(self):
        """End-to-end of the same defect: the card's trigger label must not become the message."""
        from cqc_lem.app.run_automation import _draft_catchup_message
        moment = {"name": "Jane Doe", "event_type": "job_change", "suggested_message": "Say congrats"}
        assert _draft_catchup_message(1, moment, MagicMock()) == "Congrats on the new role, Jane!"

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
            "enq": patch(f"{_RA}._schedule_catchup_followup"),
        }

    def test_sends_and_marks_sent(self):
        from cqc_lem.app.run_automation import send_catchup_touch
        from cqc_lem.utilities.db import CatchupTouchStatus
        p = self._patches(self._touch())
        with p["get"], p["prefs"], p["allow"], p["cnt"], p["dms"], p["send"] as send, p["upd"] as upd, p["enq"] as enq:
            out = send_catchup_touch.run(touch_id=3)
        # The stored name rides along so the send path can seed the messaging-search fallback.
        send.assert_called_once_with(1, "https://www.linkedin.com/in/jane", "Congrats Jane!",
                                     person_name=self._touch()["person_name"])
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


class TestScheduleCatchupFollowup:
    """The row this schedules is what process_user_followups reads — and the reply check it drives is
    the ONLY thing that routes a replying prospect into the funnel, so it has to exist even for a user
    who never configured a step-1 template (the defaults only cover step 0)."""

    def test_configured_step_one_template_runs_the_normal_sequence(self):
        from cqc_lem.app.run_automation import _schedule_catchup_followup
        with patch(f"{_RA}.get_dm_template", return_value={"template_text": "…", "delay_hours": 72}), \
             patch(f"{_RA}.enqueue_next_followup") as nxt, patch(f"{_RA}.enqueue_followup") as enq:
            _schedule_catchup_followup(1, "https://www.linkedin.com/in/jane", "Jane", "job_change")
        nxt.assert_called_once_with(1, "https://www.linkedin.com/in/jane", "Jane", "job_change", 0)
        enq.assert_not_called()

    @pytest.mark.parametrize("event_type", ["job_change", "promotion"])
    def test_high_value_milestone_gets_a_reply_check_without_a_template(self, event_type):
        from datetime import datetime, timezone
        from cqc_lem.app.run_automation import _schedule_catchup_followup, CATCHUP_REPLY_CHECK_HOURS
        with patch(f"{_RA}.get_dm_template", return_value=None), \
             patch(f"{_RA}.enqueue_next_followup") as nxt, patch(f"{_RA}.enqueue_followup") as enq:
            _schedule_catchup_followup(1, "https://www.linkedin.com/in/jane", "Jane", event_type)
        nxt.assert_not_called()
        args = enq.call_args[0]
        assert args[:5] == (1, "https://www.linkedin.com/in/jane", "Jane", event_type, 1)
        hours = (args[5] - datetime.now(timezone.utc).replace(tzinfo=None)).total_seconds() / 3600
        assert CATCHUP_REPLY_CHECK_HOURS - 1 < hours <= CATCHUP_REPLY_CHECK_HOURS

    @pytest.mark.parametrize("event_type", ["work_anniversary", "birthday"])
    def test_low_value_milestone_without_a_template_schedules_nothing(self, event_type):
        from cqc_lem.app.run_automation import _schedule_catchup_followup
        with patch(f"{_RA}.get_dm_template", return_value=None), \
             patch(f"{_RA}.enqueue_next_followup") as nxt, patch(f"{_RA}.enqueue_followup") as enq:
            _schedule_catchup_followup(1, "https://www.linkedin.com/in/jane", "Jane", event_type)
        nxt.assert_not_called()
        enq.assert_not_called()

    def test_failure_never_propagates(self):
        from cqc_lem.app.run_automation import _schedule_catchup_followup
        with patch(f"{_RA}.get_dm_template", side_effect=RuntimeError("db down")), \
             patch(f"{_RA}.log_warning") as warn:
            _schedule_catchup_followup(1, "https://www.linkedin.com/in/jane", "Jane", "job_change")
        warn.assert_called_once()


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


class TestCatchupHandoffToNurture:
    """The reply path is shared with DM auto-nurture (issue #485): the nurture draft wins when there
    is one, the funnel hand-off is the fallback, and an explicit 'no' gets neither."""

    def _followup(self):
        return {"id": 3, "user_id": 1, "profile_url": "https://www.linkedin.com/in/jane",
                "first_name": "Jane", "event_type": "job_change", "next_step": 1}

    def _run(self, reply: str, nurture_id):
        from cqc_lem.app.run_automation import process_user_followups
        patches = {
            "get_due_followups": patch(f"{_RA}.get_due_followups", return_value=[self._followup()]),
            "get_current_profile": patch(f"{_RA}.get_current_profile",
                                         return_value=(MagicMock(), MagicMock(), "e", MagicMock())),
            "quit_gracefully": patch(f"{_RA}.quit_gracefully"),
            "check_dm_replied": patch(f"{_RA}.check_dm_replied", return_value=ThreadState.REPLIED),
            "_last_inbound_message": patch(f"{_RA}._last_inbound_message", return_value=reply),
            "get_engagement_preferences": patch(f"{_RA}.get_engagement_preferences", return_value={}),
            "get_or_create_profile_synthesis": patch(f"{_RA}.get_or_create_profile_synthesis",
                                                     return_value="voice"),
            "_flag_lead_signal": patch(f"{_RA}._flag_lead_signal"),
            "stop_followups_for_profile": patch(f"{_RA}.stop_followups_for_profile"),
            "mark_followup": patch(f"{_RA}.mark_followup"),
            "_nurture_after_reply": patch(f"{_RA}._nurture_after_reply", return_value=nurture_id),
            "_route_replied_catchup_to_funnel": patch(f"{_RA}._route_replied_catchup_to_funnel"),
        }
        mocks = {name: p.start() for name, p in patches.items()}
        try:
            process_user_followups.run(user_id=1)
            return mocks
        finally:
            patch.stopall()

    def test_a_nurture_draft_wins_over_the_funnel_handoff(self):
        mocks = self._run("Sounds good, let's talk", nurture_id=7)
        mocks["_route_replied_catchup_to_funnel"].assert_not_called()

    def test_no_nurture_draft_falls_back_to_the_funnel_handoff(self):
        mocks = self._run("Sounds good, let's talk", nurture_id=None)
        mocks["_route_replied_catchup_to_funnel"].assert_called_once_with(1, self._followup())

    def test_an_explicit_no_gets_neither_a_draft_nor_the_funnel(self):
        mocks = self._run("Not interested, please stop contacting me", nurture_id=None)
        mocks["_route_replied_catchup_to_funnel"].assert_not_called()


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


class TestCatchupRunReport:
    """Issue #792: EVERY catch-up run reports, including the ones that draft or send nothing —
    otherwise "the feed had nothing today" and "the lane is broken" are the same silence."""

    def _patches(self, moments, prefs=None):
        return TestAutomateCatchupTouches()._patches(moments, prefs=prefs)

    def _run_scan(self, moments, prefs=None, **overrides):
        from cqc_lem.app.run_automation import automate_catchup_touches
        p = self._patches(moments, prefs=prefs)
        p.update(overrides)
        with p["prefs"], p["profile"], p["scrape"], p["quit"], p["has"], p["draft"], p["insert"], \
             patch(f"{_RA}.track_catchup_run") as track, patch(f"{_RA}.log_error"):
            automate_catchup_touches.run(user_id=1)
        track.assert_called_once()
        return track.call_args.args[1]

    def test_a_drafting_run_reports_the_whole_funnel(self):
        report = self._run_scan([_moment(suggested_message="Congrats on the new role, Jane!")])
        assert report["phase"] == "scan"
        assert report["status"] == "drafted"
        assert report["moments"] == 1
        assert report["classified"] == 1
        assert report["enabled_type"] == 1
        assert report["drafted"] == 1
        assert report["message_source"] == "linkedin"
        assert report["auto_approve"] is False

    def test_an_empty_feed_reports_no_moments(self):
        report = self._run_scan([])
        assert report["status"] == "no_moments"
        assert report["moments"] == 0
        assert report["drafted"] == 0

    def test_unclassifiable_moments_report_none_qualified_with_zero_classified(self):
        """The feed rendered cards but none read as a milestone — a selector/copy drift, not a quiet
        day. `classified == 0` beside `moments > 0` is what separates the two."""
        report = self._run_scan([_moment(text="People you may know"),
                                 _moment(text="Suggested for you",
                                         profile_url="https://www.linkedin.com/in/pat")])
        assert report["status"] == "none_qualified"
        assert report["moments"] == 2
        assert report["classified"] == 0

    def test_a_deduped_milestone_is_counted_as_a_duplicate(self):
        report = self._run_scan([_moment()],
                                has=patch(f"{_RA}.has_catchup_touch", return_value=True))
        assert report["status"] == "none_qualified"
        assert report["duplicate"] == 1
        assert report["drafted"] == 0

    def test_an_excluded_author_is_counted_as_excluded(self):
        report = self._run_scan([_moment()], prefs=_prefs(exclude_authors=["Jane"]))
        assert report["excluded"] == 1
        assert report["status"] == "none_qualified"

    def test_a_below_bar_moment_is_counted(self):
        report = self._run_scan([_moment(text="Wish Jane a happy birthday")],
                                prefs=_prefs(catchup_event_types=["birthday"]))
        assert report["below_bar"] == 1
        assert report["status"] == "none_qualified"

    def test_a_disabled_user_still_reports(self):
        report = self._run_scan([_moment()], prefs=_prefs(catchup_event_types=[]))
        assert report["status"] == "disabled"

    def test_a_throttled_scan_reports_rather_than_going_silent(self):
        from cqc_lem.utilities.linkedin.rate_limit import LinkedInRateLimited
        report = self._run_scan(
            [_moment()],
            profile=patch(f"{_RA}.get_current_profile", side_effect=LinkedInRateLimited("429")))
        assert report["status"] == "throttled"

    def test_a_dead_session_reports_session_failed(self):
        report = self._run_scan(
            [_moment()], profile=patch(f"{_RA}.get_current_profile", side_effect=RuntimeError("boom")))
        assert report["status"] == "session_failed"

    def test_a_scrape_failure_reports_scrape_failed(self):
        report = self._run_scan(
            [_moment()], scrape=patch(f"{_RA}._scrape_catchup_moments", side_effect=RuntimeError("boom")))
        assert report["status"] == "scrape_failed"

    def test_a_telemetry_outage_never_fails_the_run(self):
        from cqc_lem.app.run_automation import report_catchup_run
        with patch(f"{_RA}.track_catchup_run", side_effect=RuntimeError("posthog down")), \
             patch(f"{_RA}.log_warning") as warn:
            report_catchup_run(1, {"phase": "scan", "status": "drafted", "drafted": 1}, "t")
        warn.assert_called_once()

    def test_scan_dispatcher_reports_when_throttled(self):
        from cqc_lem.app.run_scheduler import auto_scan_catchup_moments
        with patch(f"{_RS}._skip_if_throttled", return_value=True), \
             patch(f"{_RA}.track_catchup_run") as track:
            auto_scan_catchup_moments()
        assert track.call_args.args[1]["status"] == "throttled"
        assert track.call_args.args[1]["phase"] == "scan"

    def test_scan_dispatcher_reports_a_fleet_with_nobody_enabled(self):
        from cqc_lem.app.run_scheduler import auto_scan_catchup_moments
        with patch(f"{_RS}._skip_if_throttled", return_value=False), \
             patch(f"{_RS}.get_active_user_ids", return_value=[1]), \
             patch(f"{_RS}.get_engagement_preferences", return_value=_prefs(catchup_event_types=[])), \
             patch(f"{_RA}.automate_catchup_touches"), \
             patch(f"{_RA}.track_catchup_run") as track:
            auto_scan_catchup_moments()
        assert track.call_args.args[1]["status"] == "disabled"
        assert track.call_args.args[1]["dispatched"] == 0

    def test_send_drip_reports_a_cap_that_swallowed_every_approved_touch(self):
        from cqc_lem.app.run_scheduler import auto_check_catchup_touches
        with patch(f"{_RS}._skip_if_throttled", return_value=False), \
             patch(f"{_RS}.get_approved_catchup_touches", return_value=[(1, 7), (2, 7)]), \
             patch(f"{_RS}.get_active_user_ids", return_value=[7]), \
             patch(f"{_RS}.get_engagement_preferences", return_value=_prefs()), \
             patch(f"{_RS}.max_catchup_touches_allowed", return_value=5), \
             patch(f"{_RS}.count_catchup_touches_sent_today", return_value=5), \
             patch(f"{_RS}.get_orphaned_catchup_touches", return_value=[]), \
             patch(f"{_RS}.update_catchup_touch_status"), \
             patch(f"{_RS}.send_catchup_touch"), \
             patch(f"{_RA}.track_catchup_run") as track:
            auto_check_catchup_touches()
        report = track.call_args.args[1]
        assert report["phase"] == "send"
        assert report["status"] == "capped"
        assert report["capped"] == 2
        assert report["dispatched"] == 0

    def test_send_drip_reports_an_empty_queue(self):
        from cqc_lem.app.run_scheduler import auto_check_catchup_touches
        with patch(f"{_RS}._skip_if_throttled", return_value=False), \
             patch(f"{_RS}.get_approved_catchup_touches", return_value=[]), \
             patch(f"{_RS}.get_orphaned_catchup_touches", return_value=[]), \
             patch(f"{_RS}.send_catchup_touch"), \
             patch(f"{_RA}.track_catchup_run") as track:
            auto_check_catchup_touches()
        assert track.call_args.args[1]["status"] == "nothing_to_send"
        assert track.call_args.args[1]["pending"] == 0

    def test_send_drip_separates_an_unapproved_backlog_from_an_empty_queue(self):
        """The reported symptom: drafts exist, none were approved, so nothing ever sends. The scan
        reports its `drafted` count once a day — for the other 23 hours this beat is the only
        evidence, and it read `nothing_to_send` exactly like a lane that had drafted nothing."""
        from cqc_lem.app.run_scheduler import auto_check_catchup_touches
        with patch(f"{_RS}._skip_if_throttled", return_value=False), \
             patch(f"{_RS}.get_approved_catchup_touches", return_value=[]), \
             patch(f"{_RS}.get_orphaned_catchup_touches", return_value=[]), \
             patch(f"{_RS}.count_pending_catchup_touches", return_value=6), \
             patch(f"{_RS}.send_catchup_touch") as task, \
             patch(f"{_RA}.track_catchup_run") as track:
            out = auto_check_catchup_touches()
        report = track.call_args.args[1]
        assert report["status"] == "awaiting_approval"
        assert report["pending"] == 6
        assert report["dispatched"] == 0
        task.apply_async.assert_not_called()
        assert out == "No Catch-up Touches to Send"

    def test_a_dispatching_beat_still_reports_the_backlog_behind_it(self):
        """`pending` rides on every beat, not just the idle ones — a lane sending its cap while a
        backlog piles up unapproved is a different story from one that has cleared its queue."""
        from cqc_lem.app.run_scheduler import auto_check_catchup_touches
        with patch(f"{_RS}._skip_if_throttled", return_value=False), \
             patch(f"{_RS}.get_approved_catchup_touches", return_value=[(1, 7)]), \
             patch(f"{_RS}.get_active_user_ids", return_value=[7]), \
             patch(f"{_RS}.get_engagement_preferences", return_value=_prefs()), \
             patch(f"{_RS}.max_catchup_touches_allowed", return_value=5), \
             patch(f"{_RS}.count_catchup_touches_sent_today", return_value=0), \
             patch(f"{_RS}.get_orphaned_catchup_touches", return_value=[]), \
             patch(f"{_RS}.count_pending_catchup_touches", return_value=4), \
             patch(f"{_RS}.update_catchup_touch_status"), \
             patch(f"{_RS}.send_catchup_touch"), \
             patch(f"{_RA}.track_catchup_run") as track:
            auto_check_catchup_touches()
        report = track.call_args.args[1]
        assert report["status"] == "dispatched"
        assert report["pending"] == 4

    def test_an_unapproved_backlog_never_outranks_a_real_send_blocker(self):
        """Precedence check: a cap that is spent is the actionable reading, not the drafts behind it."""
        from cqc_lem.app.run_scheduler import auto_check_catchup_touches
        with patch(f"{_RS}._skip_if_throttled", return_value=False), \
             patch(f"{_RS}.get_approved_catchup_touches", return_value=[(1, 7)]), \
             patch(f"{_RS}.get_active_user_ids", return_value=[7]), \
             patch(f"{_RS}.get_engagement_preferences", return_value=_prefs()), \
             patch(f"{_RS}.max_catchup_touches_allowed", return_value=5), \
             patch(f"{_RS}.count_catchup_touches_sent_today", return_value=5), \
             patch(f"{_RS}.get_orphaned_catchup_touches", return_value=[]), \
             patch(f"{_RS}.count_pending_catchup_touches", return_value=3), \
             patch(f"{_RS}.update_catchup_touch_status"), \
             patch(f"{_RS}.send_catchup_touch"), \
             patch(f"{_RA}.track_catchup_run") as track:
            auto_check_catchup_touches()
        report = track.call_args.args[1]
        assert report["status"] == "capped"
        assert report["pending"] == 3

    def test_an_unapproved_backlog_stays_debug_not_a_repeating_warning(self):
        """This beats 72x a day. A steady, working state must not log at INFO/WARNING — the
        recurrence escalation would re-emit it at ERROR and file a grouped $exception."""
        from cqc_lem.app.run_automation import report_catchup_run
        with patch(f"{_RA}.track_catchup_run"), \
             patch(f"{_RA}.log_debug") as dbg, patch(f"{_RA}.log_info") as info:
            report_catchup_run(None, {"phase": "send", "status": "awaiting_approval", "pending": 6},
                               "auto_check_catchup_touches")
        info.assert_not_called()
        dbg.assert_called_once()
        assert "pending=6" in dbg.call_args.args[0]

    def test_send_drip_reports_a_queue_stuck_behind_a_disconnected_account(self):
        """Approved touches whose owner isn't connected used to be counted nowhere, so the report
        read `nothing_to_send` while a real queue sat there — and the per-touch skip warned every
        20 minutes, which the recurrence escalation re-emits at ERROR."""
        from cqc_lem.app.run_scheduler import auto_check_catchup_touches
        with patch(f"{_RS}._skip_if_throttled", return_value=False), \
             patch(f"{_RS}.get_approved_catchup_touches", return_value=[(1, 7), (2, 7)]), \
             patch(f"{_RS}.get_active_user_ids", return_value=[]), \
             patch(f"{_RS}.get_orphaned_catchup_touches", return_value=[]), \
             patch(f"{_RS}.send_catchup_touch") as task, \
             patch(f"{_RS}.log_warning") as warn, \
             patch(f"{_RA}.track_catchup_run") as track:
            auto_check_catchup_touches()
        report = track.call_args.args[1]
        assert report["status"] == "inactive_users"
        assert report["inactive"] == 2
        assert report["dispatched"] == 0
        task.apply_async.assert_not_called()
        warn.assert_not_called()

    def test_send_drip_reports_when_throttled(self):
        from cqc_lem.app.run_scheduler import auto_check_catchup_touches
        with patch(f"{_RS}._skip_if_throttled", return_value=True), \
             patch(f"{_RA}.track_catchup_run") as track:
            auto_check_catchup_touches()
        assert track.call_args.args[1]["status"] == "throttled"
        assert track.call_args.args[1]["phase"] == "send"


class TestCatchupDeliveryReport:
    """Issue #792: `dispatched` is not `sent`. A deferred touch goes back to 'approved' and the drip
    re-dispatches it on the next 20-minute beat, so the send phase alone shows a climbing dispatch
    count for a lane that has delivered nothing all day — the reporter's exact symptom."""

    def _send(self, **overrides):
        from cqc_lem.app.run_automation import send_catchup_touch
        holder = TestSendCatchupTouch()
        p = holder._patches(holder._touch(), **{k: v for k, v in overrides.items()
                                                if k in ("sent", "catchup_today", "dms_today")})
        for key, value in overrides.items():
            if key not in ("sent", "catchup_today", "dms_today"):
                p[key] = value
        with p["get"], p["prefs"], p["allow"], p["cnt"], p["dms"], p["send"], p["upd"], p["enq"], \
             patch(f"{_RA}.track_catchup_run") as track:
            send_catchup_touch.run(touch_id=3)
        track.assert_called_once()
        return track.call_args.args[1]

    def test_a_delivered_touch_reports_sent(self):
        report = self._send()
        assert report["phase"] == "deliver"
        assert report["status"] == "sent"
        assert report["touch_id"] == 3

    def test_a_failed_send_reports_failed(self):
        assert self._send(sent=False)["status"] == "failed"

    def test_the_account_wide_dm_cap_is_distinguishable_from_the_catchup_cap(self):
        """The two deferrals look identical from the drip — both leave the row 'approved'. Only the
        status says which cap the user has to raise."""
        assert self._send(dms_today=20)["status"] == "dm_capped"
        assert self._send(catchup_today=5)["status"] == "capped"

    def test_a_throttled_send_reports_rather_than_going_silent(self):
        from cqc_lem.utilities.linkedin.rate_limit import LinkedInRateLimited
        report = self._send(send=patch(f"{_RA}.send_dm_now", side_effect=LinkedInRateLimited("429")))
        assert report["status"] == "throttled"

    def test_an_empty_message_reports_no_message(self):
        holder = TestSendCatchupTouch()
        report = self._send(get=patch(f"{_RA}.get_catchup_touch",
                                      return_value=holder._touch(message="  ")))
        assert report["status"] == "no_message"

    def test_a_vanished_row_still_reports(self):
        report = self._send(get=patch(f"{_RA}.get_catchup_touch", return_value=None))
        assert report["status"] == "not_sendable"
        assert report["touch_id"] == 3


class TestCatchupBeatSchedule:
    def test_beat_schedule_registers_both_catchup_jobs(self):
        from cqc_lem.app.my_celery import app
        schedule = app.conf.beat_schedule
        assert schedule["scan-catchup-moments"]["task"] == "cqc_lem.app.run_scheduler.auto_scan_catchup_moments"
        assert schedule["send-catchup-touches"]["task"] == "cqc_lem.app.run_scheduler.auto_check_catchup_touches"


class TestZeroWalkTripwire:
    """#1013: a walk that returns zero items must ask the PAGE before it reads as 'nothing to do'.

    #964's catch-up scan matched zero cards on a feed showing ten and logged `no_moments` daily for
    weeks. The cross-check anchor is deliberately independent of the chain — cross-checking a chain
    against its own selector proves nothing, since a rotated anchor answers zero to both."""

    @pytest.fixture(autouse=True)
    def _no_sleeps(self):
        with patch("time.sleep"):
            yield

    def test_verdict_is_three_valued(self):
        from cqc_lem.app.run_automation import zero_walk_verdict
        assert zero_walk_verdict(7) == "drift"
        assert zero_walk_verdict(0) == "empty"
        # None is load-bearing: "we could not ask the page" is not "the page said zero".
        assert zero_walk_verdict(None) == "unknown"

    def test_no_cards_while_the_page_renders_listitems_warns(self):
        from cqc_lem.app.run_automation import _scrape_catchup_moments
        driver = MagicMock()
        driver.find_elements.return_value = [MagicMock()] * 10
        with patch(f"{_RA}.find_all_first", return_value=[]), \
             patch(f"{_RA}.log_warning") as warn:
            assert _scrape_catchup_moments(driver, max_moments=10, user_id=1) == []
        warn.assert_called_once()
        assert "selector drift" in warn.call_args[0][0]

    def test_no_cards_on_a_genuinely_empty_page_stays_a_debug_no_op(self):
        """Warning here would file a defect for a quiet day — the scan beat runs daily per user."""
        from cqc_lem.app.run_automation import _scrape_catchup_moments
        driver = MagicMock()
        driver.find_elements.return_value = []
        with patch(f"{_RA}.find_all_first", return_value=[]), \
             patch(f"{_RA}.log_warning") as warn, patch(f"{_RA}.log_debug") as debug:
            assert _scrape_catchup_moments(driver, max_moments=10, user_id=1) == []
        warn.assert_not_called()
        debug.assert_called_once()

    def test_an_unreadable_cross_check_is_never_a_defect(self):
        from cqc_lem.app.run_automation import _scrape_catchup_moments
        from selenium.common.exceptions import WebDriverException
        driver = MagicMock()
        driver.find_elements.side_effect = WebDriverException("session gone")
        with patch(f"{_RA}.find_all_first", return_value=[]), \
             patch(f"{_RA}.log_warning") as warn:
            assert _scrape_catchup_moments(driver, max_moments=10, user_id=1) == []
        warn.assert_not_called()

    def test_cards_that_render_but_yield_no_moments_do_not_trip_the_wire(self):
        """Ads and prompts render as listitems and are filtered by design — that is the funnel
        doing its job, not the locator being blind."""
        from cqc_lem.app.run_automation import _scrape_catchup_moments
        card = MagicMock()
        card.find_elements.return_value = []
        driver = MagicMock()
        driver.find_elements.return_value = [MagicMock()] * 10
        with patch(f"{_RA}.find_all_first", return_value=[card]), \
             patch(f"{_RA}.log_warning") as warn:
            assert _scrape_catchup_moments(driver, max_moments=10, user_id=1) == []
        warn.assert_not_called()


class TestCatchupNameFromCard:
    """The profile link wraps the WHOLE card on today's surface, so its text is a milestone sentence,
    not a name. Every one of these is a real card from the 2026-08-04 production rows (issue #1030)."""

    @staticmethod
    def _link(text, href="https://www.linkedin.com/in/jay-bailey-1a2b3c"):
        link = MagicMock()
        link.get_attribute.return_value = href
        return link

    @pytest.mark.parametrize("card,expected", [
        ("Jay Bailey Completed 5 years at Emory University Congrats on your 5 year anniversary...",
         "Jay Bailey"),
        ("Cheyenne Paterson Completed 1 year at George Mason University Congrats on your 1 year",
         "Cheyenne Paterson"),
        ("Chutima Boonthum-Denecke Completed 20 years at Hampton University",
         "Chutima Boonthum-Denecke"),
        ("Michael Dedecek Celebrate Michael's birthday", "Michael Dedecek"),
        ("Danielle Williams, MHSA Celebrate Danielle's birthday", "Danielle Williams, MHSA"),
        ("Richard Valdez III Completed 5 years at Somewhere", "Richard Valdez III"),
        ("Deva Prasanna S Completed 1 year at L&T", "Deva Prasanna S"),
        ("Jane Doe started a new position as CTO at Acme", "Jane Doe"),
    ])
    def test_the_milestone_half_is_cut_off(self, card, expected):
        from cqc_lem.app import run_automation as ra
        with patch(f"{_RA}.getText", return_value=card):
            assert ra._catchup_name_from_card(card, self._link(card)) == expected

    def test_a_credential_tail_is_kept_because_it_is_part_of_the_name(self):
        from cqc_lem.app import run_automation as ra
        card = ("DeWarren K. Langley, JD, MPA, MHFA, YMHFA, SWL Completed 10 years at "
                "Charles Hamilton Houston Foundation, Inc.")
        with patch(f"{_RA}.getText", return_value=card):
            assert ra._catchup_name_from_card(card, self._link(card)) == \
                "DeWarren K. Langley, JD, MPA, MHFA, YMHFA, SWL"

    def test_a_card_with_no_milestone_phrase_falls_back_to_the_slug(self):
        # Better a plain name off the URL than a paragraph in the greeting.
        from cqc_lem.app import run_automation as ra
        card = " ".join(f"word{i}" for i in range(20))
        link = self._link(card, href="https://www.linkedin.com/in/jane-doe-8a4b21")
        with patch(f"{_RA}.getText", return_value=card):
            assert ra._catchup_name_from_card(card, link) == "Jane Doe"

    def test_a_bare_name_is_left_alone(self):
        from cqc_lem.app import run_automation as ra
        with patch(f"{_RA}.getText", return_value="Jay Bailey"):
            assert ra._catchup_name_from_card("Jay Bailey", self._link("Jay Bailey")) == "Jay Bailey"
