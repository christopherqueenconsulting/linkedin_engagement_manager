"""Unit tests for automate_profile_viewer_engagement's JS-driven read of the profile-views page.

The analytics page is SDUI (no <ul>/<li> container, hashed class names): a viewer row is an
/in/ anchor whose text carries a "Viewed …" caption, read via one execute_script call so nothing
goes stale while the list re-renders. Zero rows with a non-zero "Profile viewers" headline is
selector drift and must stay loud; zero rows with no headline is nothing-to-do and must stay
quiet (issue #572 — it must not page the error cron).

The JS read is also what keeps the walk off `get_elements_as_list_wait_stale`, whose unresolvable
wait is what raised the escalated "Finding Profile Viewers" TimeoutException (issue #1040).
"""

from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit

_OUT = "cqc_lem.app.engagement.outreach"


def _profile_pair(driver=None):
    driver = driver or MagicMock(name="driver")
    wait = MagicMock(name="wait")
    profile = MagicMock(name="profile")
    profile.email = "user@example.com"
    return driver, wait, "user@example.com", profile


def _row(name: str, slug: str, viewed: str) -> dict:
    return {"href": f"https://www.linkedin.com/in/{slug}/", "name": name, "viewed": viewed}


def _driver_scripting(rows_per_round: list[list[dict]], headline_stat=None) -> MagicMock:
    """A driver whose execute_script answers the three JS reads the walk makes.

    `rows_per_round` is consumed one list per rows-read; the last list repeats once exhausted
    (the real list stops growing).
    """
    driver = MagicMock(name="driver")
    reads = {"i": 0}

    def _execute(script, *args):
        if "Profile viewers" in script:
            return headline_stat
        if "scrollIntoView" in script:
            return None
        result = rows_per_round[min(reads["i"], len(rows_per_round) - 1)]
        reads["i"] += 1
        return result

    driver.execute_script.side_effect = _execute
    return driver


def _run(driver, render_wait: int = 0, **kwargs):
    with patch(f"{_OUT}.get_current_profile", return_value=_profile_pair(driver)), \
         patch(f"{_OUT}.quit_gracefully") as mock_quit, \
         patch(f"{_OUT}.get_user_id", return_value=1), \
         patch(f"{_OUT}.time.sleep"), \
         patch(f"{_OUT}._PROFILE_VIEWER_RENDER_WAIT_SECONDS", render_wait), \
         patch(f"{_OUT}.log_debug") as mock_debug, \
         patch(f"{_OUT}.log_warning") as mock_warning, \
         patch(f"{_OUT}.log_error") as mock_error, \
         patch(f"{_OUT}.engage_with_profile_viewer") as mock_engage:
        from cqc_lem.app.engagement.outreach import automate_profile_viewer_engagement

        result = automate_profile_viewer_engagement.run(user_id=1, **kwargs)

    return result, {"quit": mock_quit, "debug": mock_debug, "warning": mock_warning,
                    "error": mock_error, "engage": mock_engage}


def _engaged_urls(mocks) -> list[str]:
    return [c.kwargs["kwargs"]["viewer_url"] for c in mocks["engage"].apply_async.call_args_list]


class TestNoViewersFound:
    def test_empty_page_with_no_headline_is_a_quiet_no_op(self):
        result, mocks = _run(_driver_scripting([[]], headline_stat=None))

        mocks["error"].assert_not_called()
        mocks["warning"].assert_not_called()
        mocks["debug"].assert_called_once()
        mocks["engage"].apply_async.assert_not_called()
        assert "Engaged with 0 viewers" in result
        mocks["quit"].assert_called_once()

    def test_zero_rows_with_nonzero_headline_warns_selector_drift(self):
        result, mocks = _run(_driver_scripting([[]], headline_stat=136))

        mocks["error"].assert_not_called()
        mocks["warning"].assert_called_once()
        assert "selector drift" in mocks["warning"].call_args.args[0]
        assert "Engaged with 0 viewers" in result


class TestNoPollingWaitOnTheViewerList:
    """Regression guard for the escalated TimeoutException "Finding Profile Viewers" (#1040).

    The walk used to read rows through `get_elements_as_list_wait_stale`, whose wait can only
    resolve once the locator matches something. Against the SDUI page it never matched, so every
    run raised TimeoutException, the handler warned, and the repeats escalated into a grouped
    `$exception`. Rows come from one `execute_script` pass now: a page that has not painted yet is
    waited out and re-read, never polled into a raised exception.

    This used to be asserted by patching `run_automation.get_elements_as_list_wait_stale` and
    checking the mock was never called. The walk lives in `app.engagement.outreach` since #1154 and
    does not bind that name at ALL, so a patch of it raises. Asserting the ABSENCE of the binding
    is the stronger form anyway: a mock-not-called check would go silently green if the walk ever
    re-imported the helper and called it through another module's namespace.
    """

    def test_the_polling_locator_is_not_even_importable_into_the_walk(self):
        import cqc_lem.app.engagement.outreach as ra

        assert not hasattr(ra, "get_elements_as_list_wait_stale"), (
            "outreach re-bound the polling locator whose unresolvable wait raised the "
            "escalated 'Finding Profile Viewers' TimeoutException (#1040)")

    def test_empty_page_never_touches_the_polling_locator(self):
        _, mocks = _run(_driver_scripting([[]], headline_stat=None))

        mocks["error"].assert_not_called()

    def test_late_painting_page_is_re_read_not_raised(self):
        rows = [_row("Ada", "ada", "Viewed 1h ago")]

        result, mocks = _run(_driver_scripting([[], rows, rows]), render_wait=30)

        mocks["warning"].assert_not_called()
        mocks["error"].assert_not_called()
        mocks["debug"].assert_not_called()
        assert _engaged_urls(mocks) == ["https://www.linkedin.com/in/ada/"]
        assert "Engaged with 1 viewers" in result


class TestWalkTermination:
    def test_list_that_stops_growing_ends_the_walk_and_engages_all_in_range(self):
        rows = [_row("Ada", "ada", "Viewed 1h ago"), _row("Grace", "grace", "Viewed 20h ago")]
        result, mocks = _run(_driver_scripting([rows, rows]))

        assert sorted(_engaged_urls(mocks)) == ["https://www.linkedin.com/in/ada/",
                                                "https://www.linkedin.com/in/grace/"]
        assert "Engaged with 2 viewers" in result

    def test_walk_stops_on_the_first_out_of_range_row(self):
        first = [_row("Ada", "ada", "Viewed 1h ago")]
        grown = first + [_row("Old", "old", "Viewed 3w ago")]
        driver = _driver_scripting([first, grown])

        _, mocks = _run(driver)

        # Two rows-reads: the grown list's last row is out of range, so no third scroll happens.
        rows_reads = [c for c in driver.execute_script.call_args_list
                      if "Viewed " in c.args[0] and "scrollIntoView" not in c.args[0]]
        assert len(rows_reads) == 2
        assert _engaged_urls(mocks) == ["https://www.linkedin.com/in/ada/"]

    def test_unparseable_last_caption_warns_and_keeps_in_range_viewers(self):
        rows = [_row("Ada", "ada", "Viewed 1h ago"), _row("Odd", "odd", "Viewed ??")]
        result, mocks = _run(_driver_scripting([rows]))

        mocks["error"].assert_not_called()
        # Once for stopping the walk, once for dropping the unreadable row in the filter.
        assert mocks["warning"].call_count == 2
        assert _engaged_urls(mocks) == ["https://www.linkedin.com/in/ada/"]
        assert "Engaged with 1 viewers" in result


class TestDateFilter:
    def test_out_of_range_rows_are_filtered_not_engaged(self):
        rows = [_row("Ada", "ada", "Viewed 1h ago"), _row("Old", "old", "Viewed 2w ago")]
        _, mocks = _run(_driver_scripting([rows, rows]))

        assert _engaged_urls(mocks) == ["https://www.linkedin.com/in/ada/"]

    def test_lookback_days_widens_the_window(self):
        rows = [_row("Ada", "ada", "Viewed 1h ago"), _row("Old", "old", "Viewed 2w ago")]
        _, mocks = _run(_driver_scripting([rows, rows]), lookback_days=14)

        assert sorted(_engaged_urls(mocks)) == ["https://www.linkedin.com/in/ada/",
                                                "https://www.linkedin.com/in/old/"]

    def test_duplicate_hrefs_from_rerendered_rows_engage_once(self):
        rows = [_row("Ada", "ada", "Viewed 1h ago"), _row("Ada", "ada", "Viewed 1h ago")]
        _, mocks = _run(_driver_scripting([rows, rows]))

        assert _engaged_urls(mocks) == ["https://www.linkedin.com/in/ada/"]

    def test_same_name_different_profiles_both_engage(self):
        rows = [_row("Ada", "ada-1", "Viewed 1h ago"), _row("Ada", "ada-2", "Viewed 2h ago")]
        _, mocks = _run(_driver_scripting([rows, rows]))

        assert sorted(_engaged_urls(mocks)) == ["https://www.linkedin.com/in/ada-1/",
                                                "https://www.linkedin.com/in/ada-2/"]


class TestUnexpectedFailureStillErrors:
    def test_non_selenium_failure_is_still_logged_as_an_error(self):
        driver = MagicMock(name="driver")
        driver.execute_script.side_effect = RuntimeError("boom")

        with patch(f"{_OUT}.get_current_profile", return_value=_profile_pair(driver)), \
             patch(f"{_OUT}.quit_gracefully") as mock_quit, \
             patch(f"{_OUT}.time.sleep"), \
             patch(f"{_OUT}.log_error") as mock_error:
            from cqc_lem.app.engagement.outreach import automate_profile_viewer_engagement

            result = automate_profile_viewer_engagement.run(user_id=1)

        mock_error.assert_called_once()
        assert "Error while engaging with profile viewers" in result
        mock_quit.assert_called_once()
