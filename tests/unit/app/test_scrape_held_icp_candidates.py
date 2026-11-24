"""The paced hold-for-scrape task (issue #2655): gating, window, cap, stop-on-429."""

from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit

_OUT = "cqc_lem.app.engagement.outreach"
_URLS = ["https://www.linkedin.com/in/a", "https://www.linkedin.com/in/b"]


def _run(prefs=None, window=True, batch=None, scrape=None, session_error=None, skip=False,
         requeue_mock=None):
    from cqc_lem.app.engagement import outreach as ra
    prefs = {"icp_builder_enabled": True} if prefs is None else prefs
    with patch(f"{_OUT}.skip_browser_lane", return_value=skip), \
         patch(f"{_OUT}.get_engagement_preferences", return_value=prefs), \
         patch(f"{_OUT}.get_user_timezone", return_value="UTC"), \
         patch(f"{_OUT}.in_scrape_window", return_value=window), \
         patch(f"{_OUT}.claim_batch", return_value=_URLS if batch is None else batch) as claim, \
         patch(f"{_OUT}.requeue", new=requeue_mock or MagicMock()) as requeue, \
         patch(f"{_OUT}.time.sleep"), \
         patch(f"{_OUT}.quit_gracefully") as quit_, \
         patch(f"{_OUT}.get_linkedin_profile_from_url", side_effect=scrape) as fetch, \
         patch(f"{_OUT}.get_current_profile", side_effect=session_error,
               return_value=(MagicMock(), MagicMock(), "e", MagicMock())) as session:
        out = ra.scrape_held_icp_candidates.run(user_id=2)
    return out, claim, requeue, fetch, session, quit_


def test_suggest_only_account_gets_no_browser():
    _out, claim, _rq, _f, session, _q = _run(skip=True)
    claim.assert_not_called()
    session.assert_not_called()


def test_flag_off_does_nothing():
    out, claim, _rq, _f, session, _q = _run(prefs={})
    assert "off" in out
    claim.assert_not_called()
    session.assert_not_called()


def test_outside_window_claims_nothing():
    out, claim, _rq, _f, session, _q = _run(window=False)
    assert "window" in out
    claim.assert_not_called()
    session.assert_not_called()


def test_empty_queue_opens_no_browser():
    out, _c, _rq, _f, session, _q = _run(batch=[])
    assert "Nothing to scrape" in out
    session.assert_not_called()


def test_scrapes_the_claimed_batch_and_closes_the_browser():
    out, _c, requeue, fetch, _s, quit_ = _run()
    assert fetch.call_count == 2 and "Scraped 2" in out
    requeue.assert_not_called()
    quit_.assert_called_once()


def test_unavailable_page_stops_batch_and_returns_unreached_urls():
    from cqc_lem.utilities.linkedin.session import ProfileUnavailableError
    out, _c, requeue, fetch, _s, _q = _run(scrape=ProfileUnavailableError("wall"))
    assert fetch.call_count == 1 and "Scraped 0" in out
    requeue.assert_called_once_with(2, _URLS)


def test_rate_limited_session_requeues_and_skips():
    from cqc_lem.utilities.linkedin.rate_limit import LinkedInRateLimited
    out, _c, requeue, _f, _s, _q = _run(session_error=LinkedInRateLimited("429"))
    assert "rate limited" in out
    requeue.assert_called_once_with(2, _URLS)


def test_session_failure_requeues():
    from cqc_lem.app.task_outcome import LaneTaskFailed
    requeue = MagicMock()
    with pytest.raises(LaneTaskFailed, match="failed"):
        _run(session_error=RuntimeError("boom"), requeue_mock=requeue)
    requeue.assert_called_once_with(2, _URLS)


def test_dispatcher_fans_out_per_user():
    from cqc_lem.app import run_scheduler as rs
    with patch.object(rs, "_skip_if_throttled", return_value=False), \
         patch.object(rs, "get_active_user_ids", return_value=[1, 2]), \
         patch.object(rs, "_browser_lane_users", return_value=[2]), \
         patch(f"{_OUT}.scrape_held_icp_candidates.apply_async") as dispatch:
        out = rs.auto_scrape_held_icp_candidates()
    dispatch.assert_called_once_with(kwargs={"user_id": 2})
    assert "1 user" in out


def test_dispatcher_respects_the_breaker():
    from cqc_lem.app import run_scheduler as rs
    with patch.object(rs, "_skip_if_throttled", return_value=True):
        assert rs.auto_scrape_held_icp_candidates() == "Automation throttled"
