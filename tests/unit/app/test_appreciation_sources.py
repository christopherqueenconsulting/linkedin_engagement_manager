"""Unit tests for the recommendation + collaboration appreciation-DM sources (issue #968).

Both surfaces are STANDING lists, so the two things that must hold are: nothing outside the
lookback window is thanked, and nobody is thanked twice however often the beat re-runs.
"""

import pytest
from unittest.mock import MagicMock, patch

pytestmark = pytest.mark.unit

_RA = "cqc_lem.app.run_automation"


@pytest.fixture(autouse=True)
def _no_sleeps():
    with patch("time.sleep"):
        yield


@pytest.fixture(autouse=True)
def _sources_on(monkeypatch):
    monkeypatch.setenv("APPRECIATION_SOURCES_ENABLED", "true")
    monkeypatch.delenv("APPRECIATION_LOOKBACK_DAYS", raising=False)


def _card(text: str, href: str = "https://www.linkedin.com/in/jane?trk=x", name: str = "Jane Doe"):
    """A card whose text is `text` and whose person link answers the author/actor chain."""
    link = MagicMock()
    link.get_attribute.return_value = href
    link.text = name
    card = MagicMock()
    card.text = text
    card.find_elements.return_value = [link] if href else []
    return card


class TestFlagAndWindow:
    def test_sources_are_off_by_default(self, monkeypatch):
        """An ungrounded scraper must not send DMs — the flip is the owner's."""
        from cqc_lem.app.run_automation import (appreciation_sources_enabled,
                                                get_recent_collaborators,
                                                get_recent_recommendations)
        monkeypatch.delenv("APPRECIATION_SOURCES_ENABLED", raising=False)
        driver = MagicMock()
        assert appreciation_sources_enabled() is False
        assert get_recent_recommendations(driver, MagicMock(), 1, "https://x/in/me") == {}
        assert get_recent_collaborators(driver, MagicMock(), 1) == {}
        driver.get.assert_not_called()

    def test_lookback_defaults_and_survives_garbage(self, monkeypatch):
        from cqc_lem.app.run_automation import appreciation_lookback_days
        assert appreciation_lookback_days() == 30
        monkeypatch.setenv("APPRECIATION_LOOKBACK_DAYS", "7")
        assert appreciation_lookback_days() == 7
        monkeypatch.setenv("APPRECIATION_LOOKBACK_DAYS", "not-a-number")
        assert appreciation_lookback_days() == 30


class TestDateParsers:
    def test_recommendation_date_age(self):
        from datetime import datetime, timezone
        from cqc_lem.app.run_automation import _parse_recommendation_date
        now = datetime(2026, 8, 3, tzinfo=timezone.utc)
        assert _parse_recommendation_date("July 24, 2026, Jane worked with me", now) == pytest.approx(10.0)
        # Two dates on one card (the recommendation text can quote another) -> the NEWEST wins.
        assert _parse_recommendation_date("March 1, 2020 ... July 24, 2026", now) == pytest.approx(10.0)

    def test_undated_recommendation_is_none_not_zero(self):
        """None means SKIP. Treating 'no date' as 'today' would blast every historical recommender."""
        from cqc_lem.app.run_automation import _parse_recommendation_date
        assert _parse_recommendation_date("Jane worked with me") is None
        assert _parse_recommendation_date("") is None
        assert _parse_recommendation_date("Februray 30, 2026") is None

    @pytest.mark.parametrize("text,days", [("3d", 3.0), ("2w", 14.0), ("2mo", 60.0),
                                           ("1y", 365.0), ("5h", 0.0), ("45m", 0.0),
                                           ("2 months", 60.0), ("45 minutes", 0.0),
                                           ("3 days", 3.0), ("1 week", 7.0), ("2 years", 730.0)])
    def test_relative_age(self, text, days):
        from cqc_lem.app.run_automation import _parse_relative_age_days
        assert _parse_relative_age_days(f"Jane mentioned you in a post {text}") == days

    def test_relative_age_none_when_unreadable(self):
        from cqc_lem.app.run_automation import _parse_relative_age_days
        assert _parse_relative_age_days("Jane mentioned you in a post") is None

    @pytest.mark.parametrize("quoted", ["we hit $5m ARR", "shipped v2h of the SDK", "up 40% in Q3"])
    def test_a_number_inside_the_quoted_post_is_not_the_timestamp(self, quoted):
        """A notification card carries the quoted post as well as its own age. '$5m' clears a `\\b`,
        so without the standalone-token rule a two-year-old mention reads as posted minutes ago —
        and gets thanked."""
        from cqc_lem.app.run_automation import _parse_relative_age_days
        assert _parse_relative_age_days(f"Jane mentioned you: '{quoted}' 2h") == 0.0

    def test_a_glued_timestamp_still_parses(self):
        """LinkedIn separates the age with a bullet on some surfaces and a newline on others."""
        from cqc_lem.app.run_automation import _parse_relative_age_days
        assert _parse_relative_age_days("Jane mentioned you in a post • 3d") == 3.0
        assert _parse_relative_age_days("Jane mentioned you in a post\n3d") == 3.0
        assert _parse_relative_age_days("3d") == 3.0


def _rec_row(text: str, href: str = "https://www.linkedin.com/in/jane?trk=x",
             name: str = "Jane Doe\n· 1st\nHead of Ops") -> dict:
    """One row as `_RECOMMENDATION_ROWS_JS` returns it: the card block's text plus the `/in/`
    anchor it was resolved from."""
    return {"href": href, "name": name.split("\n")[0], "text": text}


class TestRecommendations:
    """#1007: the card read is a single JS pass over `/in/` anchors, not a locator ladder — the
    ladder was unmatchable on the live SDUI DOM and read zero cards forever."""

    def _run(self, rows, own="https://www.linkedin.com/in/me", page_dated=None):
        from cqc_lem.app.run_automation import get_recent_recommendations
        driver = MagicMock()
        driver.execute_script.return_value = {
            "rows": rows, "anchors": len(rows) or 3,
            "page_dated": bool(rows) if page_dated is None else page_dated}
        with patch(f"{_RA}.click_first"), \
             patch(f"{_RA}.wait_for_ajax"), \
             patch(f"{_RA}.log_warning") as warn:
            got = get_recent_recommendations(driver, MagicMock(), 1, own)
        return got, driver, warn

    def test_reads_the_received_tab_of_the_users_own_profile(self):
        got, driver, _ = self._run([_rec_row("Jane Doe\n· 1st\n"
                                             "July 24, 2026, Jane was Chris's client\nGreat work.")])
        assert got == {"https://www.linkedin.com/in/jane": "Jane Doe"}
        assert driver.get.call_args[0][0] == ("https://www.linkedin.com/in/me"
                                              "/details/recommendations/")

    def test_recommendation_older_than_the_window_is_not_thanked(self):
        got, _, _ = self._run([_rec_row("March 2, 2019, Jane was my client")])
        assert got == {}

    def test_a_date_shaped_line_that_is_not_a_date_is_skipped(self):
        """The JS only proves the SHAPE; `_parse_recommendation_date` is still the authority."""
        got, _, warn = self._run([_rec_row("February 30, 2026, Jane was my client"),
                                  _rec_row("March 2, 2019, John was my client",
                                           href="https://www.linkedin.com/in/john", name="John")])
        assert got == {}
        warn.assert_not_called()

    def test_blocks_that_all_fail_the_date_parser_still_warn(self):
        """The reader-vs-parser half of the tripwire: every block carried a date-SHAPED line and not
        one parsed. `page_dated` cannot see this — the page and the JS agree, the PARSER is what
        drifted — and without a warning it reads as 'no recent recommendations' forever."""
        got, _, warn = self._run([_rec_row("February 30, 2026, Jane was my client"),
                                  _rec_row("September 31, 2026, John was my client",
                                           href="https://www.linkedin.com/in/john", name="John")])
        assert got == {}
        warn.assert_called_once()
        assert "no readable date" in warn.call_args[0][0]

    def test_dated_cards_do_not_warn_even_when_all_are_old(self):
        got, _, warn = self._run([_rec_row("March 2, 2019, Jane was my client")])
        assert got == {}
        warn.assert_not_called()

    def test_empty_section_is_quiet(self):
        got, _, warn = self._run([], page_dated=False)
        assert got == {}
        warn.assert_not_called()

    def test_zero_cards_on_a_dated_page_is_drift_and_warns(self):
        """The failure this issue exists for: the page plainly shows dated recommendations and the
        read resolves none. Silently returning {} is how the dead ladder survived a merge."""
        got, _, warn = self._run([], page_dated=True)
        assert got == {}
        warn.assert_called_once()

    def test_an_unreadable_page_is_not_a_recommendation(self):
        from selenium.common.exceptions import WebDriverException
        from cqc_lem.app.run_automation import get_recent_recommendations
        driver = MagicMock()
        driver.execute_script.side_effect = WebDriverException("no such execution context")
        with patch(f"{_RA}.click_first"), patch(f"{_RA}.wait_for_ajax"), \
             patch(f"{_RA}.log_warning") as warn:
            assert get_recent_recommendations(driver, MagicMock(), 1,
                                              "https://www.linkedin.com/in/me") == {}
        assert warn.called

    def test_a_non_dict_script_result_is_not_a_crash(self):
        """An `undefined`/None answer from `execute_script` must read as 'nothing', not blow up the
        appreciation pass that called it."""
        from cqc_lem.app.run_automation import get_recent_recommendations
        driver = MagicMock()
        driver.execute_script.return_value = None
        with patch(f"{_RA}.click_first"), patch(f"{_RA}.wait_for_ajax"), \
             patch(f"{_RA}.log_warning") as warn:
            assert get_recent_recommendations(driver, MagicMock(), 1,
                                              "https://www.linkedin.com/in/me") == {}
        warn.assert_not_called()

    def test_own_profile_link_is_never_a_recommender(self):
        row = _rec_row("July 24, 2026", href="https://www.linkedin.com/in/me/?trk=y", name="Me")
        got, _, _ = self._run([row])
        assert got == {}

    def test_the_render_poll_retries_an_empty_first_paint(self):
        """The SDUI page paints asynchronously — an immediate zero is not evidence of an empty
        section, so an empty read is re-tried before it is believed."""
        from cqc_lem.app.run_automation import (_RECOMMENDATION_RENDER_ATTEMPTS,
                                                get_recent_recommendations)
        driver = MagicMock()
        empty = {"rows": [], "anchors": 4, "page_dated": False}
        painted = {"rows": [_rec_row("July 24, 2026, Jane was my client")], "anchors": 4,
                   "page_dated": True}
        driver.execute_script.side_effect = [empty, empty, painted]
        with patch(f"{_RA}.click_first"), patch(f"{_RA}.wait_for_ajax"), \
             patch(f"{_RA}.log_warning") as warn:
            got = get_recent_recommendations(driver, MagicMock(), 1,
                                             "https://www.linkedin.com/in/me")
        assert got == {"https://www.linkedin.com/in/jane": "Jane Doe"}
        assert driver.execute_script.call_count == 3
        assert _RECOMMENDATION_RENDER_ATTEMPTS >= 3
        warn.assert_not_called()

    def test_falls_back_to_the_stored_profile_url(self):
        from cqc_lem.app.run_automation import get_recent_recommendations
        driver = MagicMock()
        driver.execute_script.return_value = {"rows": [], "anchors": 0, "page_dated": False}
        with patch(f"{_RA}.click_first"), patch(f"{_RA}.wait_for_ajax"), \
             patch(f"{_RA}.get_linkedin_profile_url_by_user_id",
                   return_value="https://www.linkedin.com/in/stored/"):
            get_recent_recommendations(driver, MagicMock(), 1, "")
        assert driver.get.call_args[0][0] == ("https://www.linkedin.com/in/stored"
                                              "/details/recommendations/")

    def test_no_resolvable_profile_url_is_a_quiet_no_op(self):
        from cqc_lem.app.run_automation import get_recent_recommendations
        driver = MagicMock()
        with patch(f"{_RA}._own_profile_url", return_value=""), \
             patch(f"{_RA}.log_warning") as warn:
            assert get_recent_recommendations(driver, MagicMock(), 1, "") == {}
        warn.assert_not_called()
        driver.get.assert_not_called()


class TestCollaborators:
    def _run(self, cards):
        from cqc_lem.app.run_automation import get_recent_collaborators
        driver = MagicMock()
        with patch(f"{_RA}.find_all_first", return_value=cards), \
             patch(f"{_RA}.wait_for_ajax"), \
             patch(f"{_RA}.getText", side_effect=lambda el: el.text):
            got = get_recent_collaborators(driver, MagicMock(), 1)
        return got, driver

    def test_recent_mention_becomes_a_collaborator(self):
        got, driver = self._run([_card("Jane Doe mentioned you in a post 3d")])
        assert got == {"https://www.linkedin.com/in/jane": "Jane Doe"}
        assert "filter=mentions" in driver.get.call_args[0][0]

    def test_non_mention_card_is_skipped(self):
        """A mentions-filtered feed still mixes in other cards — the card has to SAY it."""
        got, _ = self._run([_card("Jane Doe posted: hiring now 1d")])
        assert got == {}

    def test_mention_older_than_the_window_is_skipped(self):
        got, _ = self._run([_card("Jane Doe tagged you in a post 6mo")])
        assert got == {}

    def test_undated_mention_is_skipped(self):
        got, _ = self._run([_card("Jane Doe mentioned you in a post")])
        assert got == {}

    def test_company_actor_is_skipped(self):
        got, _ = self._run([_card("Acme mentioned you in a post 2d",
                                  href="https://www.linkedin.com/company/acme")])
        assert got == {}

    def test_empty_feed_is_a_quiet_no_op(self):
        got, _ = self._run([])
        assert got == {}

    def test_actor_name_falls_back_to_the_card_sentence(self):
        """The live grounding run (#968) found an actor link with NO text — the name only existed in
        the card's own sentence. Read it from there rather than DM a real person as "there"."""
        got, _ = self._run([_card("Status is online\nUnread notification.\n"
                                  "Utkarsh Tiwari mentioned you in a comment in a group 2h",
                                  href="https://www.linkedin.com/in/utkarsh%2Dtiwari%2D98164814b",
                                  name="")])
        assert got == {"https://www.linkedin.com/in/utkarsh-tiwari-98164814b": "Utkarsh Tiwari"}

    def test_notification_chrome_is_never_read_as_a_name(self):
        """Same line as the verb, so the bound on the fallback is what keeps it honest."""
        from cqc_lem.app.run_automation import _mention_actor_name
        assert _mention_actor_name("Unread notification. Jane Doe mentioned you in a post") == "Jane Doe"
        assert _mention_actor_name("mentioned you in a post 2h") == ""
        assert _mention_actor_name("") == ""

    def test_link_text_still_wins_over_the_sentence(self):
        got, _ = self._run([_card("Someone Else mentioned you in a post 1d", name="Jane Doe")])
        assert got == {"https://www.linkedin.com/in/jane": "Jane Doe"}

    def test_percent_encoded_slug_is_one_person_not_two(self):
        """SDUI escapes the hyphens in a vanity slug. Encoded and decoded must key the ledger the
        same way or the once-ever guarantee quietly breaks."""
        from cqc_lem.app.run_automation import _normalize_profile_url
        assert (_normalize_profile_url("https://www.linkedin.com/in/jane%2Ddoe%2D1234?trk=x")
                == _normalize_profile_url("https://www.linkedin.com/in/jane-doe-1234/")
                == "https://www.linkedin.com/in/jane-doe-1234")


class TestDispatchDedup:
    """`automate_appreciation_dms_for_user` re-queues itself every ~60s, so the ledger claim is the
    only thing standing between one thank-you and a thank-you a minute."""

    def _dispatch(self, thanked: bool, claimed: bool = True):
        from cqc_lem.app.run_automation import _dispatch_appreciation_dms
        with patch(f"{_RA}.has_appreciation_touch", return_value=thanked) as has, \
             patch(f"{_RA}.claim_appreciation_touch", return_value=claimed) as claim, \
             patch(f"{_RA}.build_dm_from_template", return_value="Thanks Jane!") as build, \
             patch(f"{_RA}.send_private_dm") as send, \
             patch(f"{_RA}.enqueue_next_followup"):
            sent = _dispatch_appreciation_dms(
                1, MagicMock(), "recommendation_received",
                {"https://www.linkedin.com/in/jane": "Jane Doe"})
        return sent, has, claim, build, send

    def test_first_pass_claims_then_sends(self):
        sent, _, claim, _, send = self._dispatch(thanked=False)
        assert sent == 1
        claim.assert_called_once()
        send.apply_async.assert_called_once()

    def test_already_thanked_costs_no_llm_call_and_sends_nothing(self):
        sent, _, claim, build, send = self._dispatch(thanked=True)
        assert sent == 0
        build.assert_not_called()
        claim.assert_not_called()
        send.apply_async.assert_not_called()

    def test_an_ungranted_claim_never_sends(self):
        """A concurrent pass won the row, or the ledger is unreadable — both mean don't send."""
        sent, _, _, _, send = self._dispatch(thanked=False, claimed=False)
        assert sent == 0
        send.apply_async.assert_not_called()

    def test_missing_template_does_not_burn_the_claim(self):
        from cqc_lem.app.run_automation import _dispatch_appreciation_dms
        with patch(f"{_RA}.has_appreciation_touch", return_value=False), \
             patch(f"{_RA}.claim_appreciation_touch") as claim, \
             patch(f"{_RA}.build_dm_from_template", return_value=None), \
             patch(f"{_RA}.send_private_dm") as send, \
             patch(f"{_RA}.log_warning"):
            assert _dispatch_appreciation_dms(1, MagicMock(), "collaboration",
                                              {"https://x/in/jane": "Jane"}) == 0
        claim.assert_not_called()
        send.apply_async.assert_not_called()


class TestDailyDmBudget:
    """A standing list can hand back a month of people at once, so the appreciation lane spends the
    SAME per-day DM allowance every other DM lane does. Whoever it can't afford is left UNCLAIMED,
    so a later pass thanks them rather than nobody ever doing so."""

    _PEOPLE = {f"https://www.linkedin.com/in/p{i}": f"Person {i}" for i in range(5)}

    def _dispatch(self, budget):
        from cqc_lem.app.run_automation import _dispatch_appreciation_dms
        with patch(f"{_RA}.has_appreciation_touch", return_value=False), \
             patch(f"{_RA}.claim_appreciation_touch", return_value=True) as claim, \
             patch(f"{_RA}.build_dm_from_template", return_value="Thanks!"), \
             patch(f"{_RA}.send_private_dm") as send, \
             patch(f"{_RA}.enqueue_next_followup"):
            sent = _dispatch_appreciation_dms(1, MagicMock(), "collaboration", dict(self._PEOPLE),
                                              budget)
        return sent, claim, send

    def test_the_budget_caps_the_burst(self):
        sent, claim, send = self._dispatch(2)
        assert sent == 2
        assert send.apply_async.call_count == 2

    def test_the_unaffordable_rest_is_never_claimed(self):
        """Claiming them would burn their one shot at being thanked without sending anything."""
        _, claim, _ = self._dispatch(2)
        assert claim.call_count == 2

    def test_a_spent_budget_sends_nothing(self):
        sent, claim, send = self._dispatch(0)
        assert sent == 0
        claim.assert_not_called()
        send.apply_async.assert_not_called()

    def test_no_budget_means_unbounded(self):
        """None is for callers that have already bounded themselves — the task never passes it."""
        sent, _, send = self._dispatch(None)
        assert sent == len(self._PEOPLE)
        assert send.apply_async.call_count == len(self._PEOPLE)

    def test_budget_is_the_shared_dm_allowance_not_a_new_one(self):
        from cqc_lem.app.run_automation import _appreciation_dm_budget
        with patch(f"{_RA}.get_engagement_preferences", return_value={"max_dms_per_day": 20}), \
             patch(f"{_RA}.count_dms_sent_today", return_value=18), \
             patch(f"{_RA}.engagement_caps_from_prefs", return_value={"dm": 20}), \
             patch(f"{_RA}.remaining_actions", return_value=2) as remaining:
            assert _appreciation_dm_budget(1) == 2
        assert remaining.call_args[0][1] == "dm"
        assert remaining.call_args[1]["caps"] == {"dm": 20}

    def test_a_negative_remainder_is_clamped_to_zero(self):
        from cqc_lem.app.run_automation import _appreciation_dm_budget
        with patch(f"{_RA}.get_engagement_preferences", return_value={"max_dms_per_day": 5}), \
             patch(f"{_RA}.count_dms_sent_today", return_value=9), \
             patch(f"{_RA}.engagement_caps_from_prefs", return_value={}), \
             patch(f"{_RA}.remaining_actions", return_value=-4):
            assert _appreciation_dm_budget(1) == 0


class TestTheBeatSpendsOneBudget:
    """The three triggers share ONE day's allowance — three lanes each spending the full cap is the
    burst the cap exists to prevent."""

    def _run(self, budget, dispatched=0):
        from cqc_lem.app.run_automation import automate_appreciation_dms_for_user
        with patch(f"{_RA}.get_user_password_pair_by_id", return_value=("a@b.c", "pw")), \
             patch(f"{_RA}.get_driver_wait_pair", return_value=(MagicMock(), MagicMock())), \
             patch(f"{_RA}.login_to_linkedin"), \
             patch(f"{_RA}.quit_gracefully"), \
             patch(f"{_RA}.load_profile_for_user", return_value=MagicMock(profile_url="")), \
             patch(f"{_RA}.accept_connection_request", return_value={}), \
             patch(f"{_RA}._appreciation_dm_budget", return_value=budget), \
             patch(f"{_RA}._dispatch_appreciation_dms", return_value=dispatched) as dispatch, \
             patch(f"{_RA}.get_recent_recommendations", return_value={}) as recs, \
             patch(f"{_RA}.get_recent_collaborators", return_value={}) as mentions:
            automate_appreciation_dms_for_user.run(user_id=1)
        return dispatch, recs, mentions

    def test_a_spent_budget_never_opens_the_scrapers(self):
        """Two page loads to build a list we cannot act on — and the standing lists are the pages
        most worth not hammering."""
        dispatch, recs, mentions = self._run(budget=0)
        recs.assert_not_called()
        mentions.assert_not_called()
        assert dispatch.call_count == 1  # connection_accepted only, and with a budget of 0

    def test_what_one_trigger_spends_the_next_one_cannot(self):
        """Two of the day's DMs left: connections take one, recommendations take the other, and
        the mentions scan never opens."""
        dispatch, recs, mentions = self._run(budget=2, dispatched=1)
        recs.assert_called_once()
        mentions.assert_not_called()
        assert [call.args[-1] for call in dispatch.call_args_list] == [2, 1]

    def test_a_healthy_budget_runs_all_three(self):
        dispatch, recs, mentions = self._run(budget=20)
        recs.assert_called_once()
        mentions.assert_called_once()
        assert dispatch.call_count == 3
