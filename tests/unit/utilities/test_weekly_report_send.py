"""Weekly report Friday beat, send claim and email (issue #2666, phase 1b)."""

import datetime as dt
from unittest.mock import patch

import pytest

from cqc_lem.utilities import email as email_mod, weekly_report as wr

pytestmark = pytest.mark.unit

_NOW = dt.datetime(2026, 11, 27, 13, 0, tzinfo=dt.timezone.utc)
_ACTIVE = {"numbers": {"posts_published": 3, "median_impressions": 1200,
                       "organic_engagement_rate": 0.02, "follower_delta": 5},
           "icp_share": {"share": 0.5, "inside": 2, "n": 4, "engagers": 5},
           "actions": [{"key": "a", "text": "Act one", "link": "/content"},
                       {"key": "b", "text": "Act two", "link": "/account"},
                       {"key": "c", "text": "Act three", "link": "/content"},
                       {"key": "d", "text": "Act four", "link": "/content"}]}
_QUIET = {"numbers": {"posts_published": 0}, "icp_share": None, "actions": []}


def _run(*, users=(1,), flag=True, enabled=True, email="a@b.co", report=_ACTIVE, claim=True,
         sent=True):
    base = "cqc_lem.utilities.db."
    with patch(base + "get_active_user_ids", return_value=list(users)), \
         patch("cqc_lem.utilities.flags.flag_enabled", return_value=flag), \
         patch(base + "get_weekly_report_enabled", return_value=enabled), \
         patch(base + "get_user_email", return_value=email), \
         patch.object(wr, "load_weekly_report", return_value=report), \
         patch(base + "claim_weekly_report_send", return_value=claim) as claim_m, \
         patch(base + "release_weekly_report_send") as release_m, \
         patch.object(email_mod, "send_weekly_report_email", return_value=sent) as send_m:
        result = wr.send_weekly_reports(_NOW)
    return result, claim_m, release_m, send_m


def test_iso_year_week_key():
    assert wr.iso_year_week(_NOW) == "2026-W48"


def test_active_user_gets_one_email_and_claims_the_week():
    result, claim_m, _, send_m = _run()
    assert result == {"sent": 1, "skipped": 0, "failed": 0}
    claim_m.assert_called_once_with(1, "2026-W48")
    send_m.assert_called_once()


def test_second_run_in_the_same_week_sends_nothing():
    result, _, _, send_m = _run(claim=False)
    assert result["sent"] == 0 and result["skipped"] == 1
    send_m.assert_not_called()


@pytest.mark.parametrize("kw", [{"flag": False}, {"enabled": False}])
def test_flag_off_or_opted_out_sends_nothing(kw):
    result, claim_m, _, send_m = _run(**kw)
    assert result["sent"] == 0
    claim_m.assert_not_called()
    send_m.assert_not_called()


def test_no_active_users_means_disconnected_accounts_get_nothing():
    result, _, _, send_m = _run(users=())
    assert result == {"sent": 0, "skipped": 0, "failed": 0}
    send_m.assert_not_called()


@pytest.mark.parametrize("kw", [{"report": _QUIET}, {"email": None}])
def test_no_activity_or_no_address_sends_nothing(kw):
    result, claim_m, _, send_m = _run(**kw)
    assert result["sent"] == 0
    claim_m.assert_not_called()
    send_m.assert_not_called()


def test_failed_send_releases_the_claim_for_a_retry():
    result, _, release_m, _ = _run(sent=False)
    assert result["failed"] == 1
    release_m.assert_called_once_with(1, "2026-W48")


def test_one_users_error_does_not_stop_the_others():
    with patch("cqc_lem.utilities.db.get_active_user_ids", return_value=[1, 2]), \
         patch("cqc_lem.utilities.flags.flag_enabled", side_effect=[RuntimeError("x"), False]):
        result = wr.send_weekly_reports(_NOW)
    assert result == {"sent": 0, "skipped": 1, "failed": 1}


def test_has_activity_treats_unreadable_posts_as_inactive():
    assert not wr.has_activity({"numbers": {"posts_published": None}, "icp_share": None})
    assert wr.has_activity({"numbers": {"posts_published": 0}, "icp_share": {"share": 0.1}})


class TestEmailBudget:
    def test_at_most_five_numbers_and_three_actions(self):
        numbers, actions = email_mod.weekly_report_lines(_ACTIVE)
        assert len(numbers) <= 5
        assert len(actions) == 3

    def test_unmeasured_numbers_are_omitted_not_zero(self):
        numbers, _ = email_mod.weekly_report_lines(
            {"numbers": {"posts_published": 2, "median_impressions": None,
                         "organic_engagement_rate": None, "follower_delta": None},
             "icp_share": None, "actions": []})
        assert numbers == ["Posts published: 2"]

    def test_email_links_opt_out_to_the_authenticated_account_page(self):
        with patch.object(email_mod, "_dispatch_email", return_value=True) as d:
            assert email_mod.send_weekly_report_email("a@b.co", _ACTIVE)
        html, text = d.call_args.args[2], d.call_args.args[3]
        assert "/account" in html and "/account" in text
        assert "unsubscribe" not in html.lower()
        assert html.count("<li>") <= 5 + 3
