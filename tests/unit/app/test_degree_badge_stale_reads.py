"""The degree-badge read on a hydrating profile (#1733).

`_degree_badge_texts` used to read `element.text` TWICE per element — once to test it, once to
append it — inside one try/except wrapping the whole chain. On the live SDUI a single node detaching
between those two reads therefore threw away every badge the walk had already collected, returned
`None`, and logged a WARNING with `exc=` — which files a fingerprinted PostHog issue. In production
that fired twice per profile on every invite attempt, for behaviour that is entirely normal on a
page still hydrating.

The contract these tests hold: a stale NODE costs that node and nothing else; a stale CHAIN is
retried once and then reported at DEBUG; any other exception still warns. `None` and `[]` stay
different answers, because `_profile_is_first_degree` routes on the difference.
"""

from unittest.mock import MagicMock, patch

import pytest
from selenium.common import StaleElementReferenceException

pytestmark = pytest.mark.unit

_INV = "cqc_lem.app.engagement.invites"


def _element(text=None, stale=False):
    element = MagicMock()
    if stale:
        type(element).text = property(
            lambda _self: (_ for _ in ()).throw(StaleElementReferenceException("detached")))
    else:
        element.text = text
    return element


def _driver(*batches, raises=None):
    """A driver whose `find_elements` returns one batch per call, or raises `raises` every time."""
    driver = MagicMock()
    if raises is not None:
        driver.find_elements.side_effect = raises
    else:
        driver.find_elements.side_effect = list(batches)
    return driver


def _read(driver):
    from cqc_lem.app.engagement import invites as ra
    with patch(f"{_INV}.log_warning") as warn, patch(f"{_INV}.log_debug") as debug, \
         patch(f"{_INV}.time.sleep"):
        return ra._degree_badge_texts(driver), warn, debug


class TestOneStaleNodeDoesNotBlindTheWalk:
    def test_a_detached_element_costs_only_itself(self):
        from cqc_lem.app.engagement import invites as ra
        # One batch per locator in the shipped chain; the first carries a stale node beside a good
        # one, so the good badge must survive.
        batches = [[_element(stale=True), _element("· 2nd")]]
        batches += [[] for _ in range(len(ra._PROFILE_DEGREE_LOCATORS) - 1)]
        texts, warn, _debug = _read(_driver(*batches))

        assert texts == ["· 2nd"]
        warn.assert_not_called()

    def test_the_text_of_each_element_is_read_exactly_once(self):
        # The bug, pinned: two reads meant the guard and the appended value could disagree, and the
        # second read was where the node had had time to detach.
        from cqc_lem.app.engagement import invites as ra
        element = MagicMock()
        reads = []
        type(element).text = property(lambda _self: (reads.append(1), "1st")[1])
        batches = [[element]] + [[] for _ in range(len(ra._PROFILE_DEGREE_LOCATORS) - 1)]
        texts, _warn, _debug = _read(_driver(*batches))

        assert texts == ["1st"]
        assert len(reads) == 1

    def test_an_element_with_no_text_is_neither_a_badge_nor_a_failure(self):
        from cqc_lem.app.engagement import invites as ra
        batches = [[_element("   ")]] + [[] for _ in range(len(ra._PROFILE_DEGREE_LOCATORS) - 1)]
        texts, warn, _debug = _read(_driver(*batches))

        assert texts == []          # [] is a readable page with no badge, never None
        warn.assert_not_called()


class TestAStaleChainIsRetriedThenReportedQuietly:
    def test_a_chain_that_goes_stale_once_is_walked_again(self):
        from cqc_lem.app.engagement import invites as ra
        driver = MagicMock()
        good = [[_element("2nd")]] + [[] for _ in range(len(ra._PROFILE_DEGREE_LOCATORS) - 1)]
        driver.find_elements.side_effect = [StaleElementReferenceException("gone")] + good
        texts, warn, debug = _read(driver)

        assert texts == ["2nd"]
        warn.assert_not_called()
        debug.assert_not_called()

    def test_stale_on_every_attempt_is_none_at_debug_never_a_warning(self):
        texts, warn, debug = _read(_driver(raises=StaleElementReferenceException("gone")))

        assert texts is None            # the read failed, so it grounds nothing
        warn.assert_not_called()        # ... but a hydrating page is not a defect
        debug.assert_called_once()

    def test_a_page_that_cannot_be_read_at_all_still_warns(self):
        texts, warn, _debug = _read(_driver(raises=RuntimeError("session closed")))

        assert texts is None
        warn.assert_called_once()
        assert warn.call_args.kwargs["exc"] is not None


class TestTheCrossCheckStillRoutesOnNoneVersusEmpty:
    def test_an_unreadable_chain_never_reaches_the_cross_check(self):
        from cqc_lem.app.engagement import invites as ra
        with patch(f"{_INV}._degree_badge_texts", return_value=None), \
             patch(f"{_INV}.grade_zero_walk") as grade:
            assert ra._profile_is_first_degree(MagicMock()) is False
        grade.assert_not_called()

    def test_a_readable_chain_with_no_badge_is_graded(self):
        from cqc_lem.app.engagement import invites as ra
        with patch(f"{_INV}._degree_badge_texts", return_value=[]), \
             patch(f"{_INV}._matching_degree_lines", return_value=[]), \
             patch(f"{_INV}.grade_zero_walk") as grade:
            assert ra._profile_is_first_degree(MagicMock()) is False
        grade.assert_called_once()
