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


class TestRecommendations:
    def _run(self, cards, own="https://www.linkedin.com/in/me"):
        from cqc_lem.app.run_automation import get_recent_recommendations
        driver = MagicMock()
        with patch(f"{_RA}.find_all_first", return_value=cards), \
             patch(f"{_RA}.click_first"), \
             patch(f"{_RA}.wait_for_ajax"), \
             patch(f"{_RA}.log_warning") as warn, \
             patch(f"{_RA}.getText", side_effect=lambda el: el.text):
            got = get_recent_recommendations(driver, MagicMock(), 1, own)
        return got, driver, warn

    def test_reads_the_received_tab_of_the_users_own_profile(self):
        got, driver, _ = self._run([_card("July 24, 2026, Jane was my client")])
        assert got == {"https://www.linkedin.com/in/jane": "Jane Doe"}
        assert driver.get.call_args[0][0] == ("https://www.linkedin.com/in/me"
                                              "/details/recommendations/")

    def test_recommendation_older_than_the_window_is_not_thanked(self):
        got, _, _ = self._run([_card("March 2, 2019, Jane was my client")])
        assert got == {}

    def test_undated_card_is_skipped_and_drift_warns_once(self):
        """Cards that render but never date = the trigger is silently dead. That IS a defect."""
        got, _, warn = self._run([_card("Jane was my client"), _card("John was my client")])
        assert got == {}
        warn.assert_called_once()

    def test_dated_cards_do_not_warn_even_when_all_are_old(self):
        got, _, warn = self._run([_card("March 2, 2019, Jane was my client")])
        assert got == {}
        warn.assert_not_called()

    def test_empty_section_is_quiet(self):
        got, _, warn = self._run([])
        assert got == {}
        warn.assert_not_called()

    def test_own_profile_link_is_never_a_recommender(self):
        card = _card("July 24, 2026", href="https://www.linkedin.com/in/me/?trk=y", name="Me")
        got, _, _ = self._run([card])
        assert got == {}

    def test_falls_back_to_the_stored_profile_url(self):
        from cqc_lem.app.run_automation import get_recent_recommendations
        driver = MagicMock()
        with patch(f"{_RA}.find_all_first", return_value=[]), \
             patch(f"{_RA}.click_first"), patch(f"{_RA}.wait_for_ajax"), \
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
