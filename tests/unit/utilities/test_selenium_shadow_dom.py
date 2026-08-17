"""The shadow-aware lookups (#1621).

LinkedIn's redesigned share-box composer mounts inside `div#interop-outlet`'s OPEN shadow root.
`driver.find_elements` cannot cross that boundary and neither can any XPath, so for weeks a working
composer read exactly like a composer that never opened: the group post stamped healthy drafts
FAILED, the occasion route never reached its own anchors, and the live probe graded `ok` on the
feed's controls behind the composer it could not see.

These two helpers are the only things in the tree that look past a shadow boundary, so what they
must never do is as important as what they do: never raise, never settle for a later label when an
earlier one matches, never hand back a hidden element.
"""

from unittest.mock import MagicMock

import pytest
from selenium.common.exceptions import WebDriverException

from cqc_lem.utilities.selenium_util import element_label, find_deep_elements, find_labelled

pytestmark = pytest.mark.unit


def _element(label=None, text="", displayed=True):
    element = MagicMock()
    element.get_attribute.side_effect = lambda name: label if name == "aria-label" else None
    element.text = text
    element.is_displayed.return_value = displayed
    return element


class TestFindDeepElements:
    def test_returns_what_the_shadow_walk_found(self):
        found = [MagicMock(), MagicMock()]
        driver = MagicMock()
        driver.execute_script.return_value = found

        assert find_deep_elements(driver, "div[role='dialog']") == found

    def test_passes_the_selector_visibility_limit_and_root_through(self):
        driver = MagicMock()
        driver.execute_script.return_value = []
        root = MagicMock()

        find_deep_elements(driver, "[role='textbox']", visible_only=False, limit=4, root=root)

        args = driver.execute_script.call_args.args
        assert args[1:] == ("[role='textbox']", False, 4, root)

    def test_a_driver_that_cannot_run_the_query_returns_nothing_rather_than_raising(self):
        """Nothing, never a raise.

        Every caller's fallback for "nothing matched" is already its fallback for "could not
        look" — and a composer walk that raises here would lose the reading it exists to take.
        """
        driver = MagicMock()
        driver.execute_script.side_effect = WebDriverException("no such execution context")

        assert find_deep_elements(driver, "div[role='dialog']") == []

    def test_a_null_in_the_result_never_reaches_the_caller(self):
        driver = MagicMock()
        element = MagicMock()
        driver.execute_script.return_value = [None, element]

        assert find_deep_elements(driver, "button") == [element]


class TestElementLabel:
    def test_prefers_the_aria_label(self):
        assert element_label(_element(label="Celebrate an occasion", text="Celebrate")) == \
            "celebrate an occasion"

    def test_falls_back_to_the_text(self):
        assert element_label(_element(text="  Add   media ")) == "add media"

    def test_an_unreadable_element_answers_with_nothing(self):
        element = MagicMock()
        element.get_attribute.side_effect = WebDriverException("stale")
        type(element).text = property(
            lambda self: (_ for _ in ()).throw(WebDriverException("stale")))

        assert element_label(element) == ""


class TestFindLabelled:
    def _root(self, elements):
        root = MagicMock()
        root.find_elements.return_value = elements
        return root

    def test_the_first_label_wins_over_document_order(self):
        """Ordered by the CALLER's labels, never by the DOM.

        The caller's first label is its most exact intent, and settling for a later one when an
        earlier one matches is how a walk clicks the control next to the one it wanted (#1012).
        """
        loose = _element(label="Celebrate")
        exact = _element(label="Celebrate an occasion")
        root = self._root([loose, exact])

        assert find_labelled(root, "button", ("celebrate an occasion", "celebrate")) is exact

    def test_exact_refuses_a_neighbouring_label(self):
        """An exact match is the only safe one for a commit control.

        'Schedule post' sits beside 'Post' in the live composer, and it publishes on someone
        else's timetable.
        """
        neighbour = _element(label="Schedule post")
        root = self._root([neighbour])

        assert find_labelled(root, "button", ("post",), exact=True) is None
        assert find_labelled(root, "button", ("post",)) is neighbour

    def test_a_composite_option_label_still_matches_its_title(self):
        """The live occasion menu renders title and description in ONE node.

        "Project Launch Share a new project milestone" is the whole label, so requiring an exact
        match everywhere would resolve nothing at all.
        """
        option = _element(text="Project Launch Share a new project milestone")
        root = self._root([option])

        assert find_labelled(root, "li", ("project launch",)) is option

    def test_a_non_exact_match_still_stops_at_word_boundaries(self):
        """Bounded, never a bare substring — 'post' must not resolve 'Repost'."""
        root = self._root([_element(label="Repost")])

        assert find_labelled(root, "button", ("post",)) is None

    def test_a_hidden_match_is_never_returned(self):
        hidden = _element(label="Post", displayed=False)
        shown = _element(label="Post")
        root = self._root([hidden, shown])

        assert find_labelled(root, "button", ("post",), exact=True) is shown

    def test_no_labels_resolves_nothing(self):
        """An unmapped archetype passes an empty label set, and must click NOTHING."""
        root = self._root([_element(label="Certification")])

        assert find_labelled(root, "button", ()) is None
        assert find_labelled(root, "button", ("  ",)) is None

    def test_a_root_that_cannot_be_queried_answers_none(self):
        root = MagicMock()
        root.find_elements.side_effect = WebDriverException("detached")

        assert find_labelled(root, "button", ("post",)) is None

    def test_an_element_that_goes_stale_mid_scan_does_not_lose_the_match(self):
        stale = MagicMock()
        stale.is_displayed.side_effect = WebDriverException("stale")
        wanted = _element(label="Post")
        root = self._root([stale, wanted])

        assert find_labelled(root, "button", ("post",), exact=True) is wanted
