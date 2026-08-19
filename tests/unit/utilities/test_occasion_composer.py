"""The native occasion composer's mechanics (issue #1088, Phase 2 of #1074).

Two failures are the reason this file exists, and both are public and un-deletable:

* Clicking the WRONG occasion type publishes a claim about the author nobody made — "Certification"
  sits next to "Educational milestone" in LinkedIn's own menu (#1012).
* Treating a click as a publish, then re-running when the row was never marked, posts the same
  announcement twice. Only a SIGHTING may mark the row posted (#1013).
"""

from unittest.mock import MagicMock, patch

import pytest
from selenium.common.exceptions import WebDriverException

from cqc_lem.utilities.linkedin import share_composer as sc

pytestmark = pytest.mark.unit

_MOD = "cqc_lem.utilities.linkedin.share_composer"
_BODY = "Shipped the v2 scheduler tonight after four weeks of nights, and here is what broke."


def _no_sleep(*_a, **_k):
    return None


class _Card:
    def __init__(self, text):
        self.text = text


class TestTheArchetypeMap:
    def test_every_occasion_archetype_has_a_composer_label(self):
        """Every occasion archetype must carry the label its composer needs.

        A third one shipped without it would reach the task, resolve nothing, and read as composer
        drift instead of a missing mapping.
        """
        from cqc_lem.utilities.ai.content_framework import OCCASION_FORMAT_KEYS

        missing = [key for key in OCCASION_FORMAT_KEYS if key not in sc.OCCASION_TYPE_LABELS]
        assert not missing, f"occasion archetypes with no composer label: {missing}"

    def test_no_label_names_a_neighbouring_occasion(self):
        """The allow-list is exact on purpose (#1012).

        'Certification' and 'New position' are other people's announcements; matching one of them
        publishes it.
        """
        forbidden = ("certification", "new position", "work anniversary", "new skill",
                     "welcome to the team", "job search")
        for labels in sc.OCCASION_TYPE_LABELS.values():
            for label in labels:
                assert not any(bad in label for bad in forbidden)

    def test_an_unmapped_archetype_resolves_nothing(self):
        assert sc.occasion_type_labels(()) == []
        assert sc.occasion_type_labels(("  ",)) == []

    def test_a_mapped_archetype_carries_its_label(self):
        assert sc.occasion_type_labels(("Project Launch",)) == ["project launch"]


class TestLandingProbe:
    def test_a_real_body_yields_a_normalized_prefix(self):
        probe = sc.landing_probe("  Shipped   the V2 SCHEDULER tonight after four weeks of nights. ")
        assert probe.startswith("shipped the v2 scheduler tonight")
        assert "  " not in probe

    @pytest.mark.parametrize("text", ["", "   ", "Launched it."])
    def test_too_little_text_grounds_no_sighting(self, text):
        assert sc.landing_probe(text) == ""


class TestOccasionPostLanded:
    def _driver(self, cards, raises=False):
        driver = MagicMock()
        if raises:
            driver.get.side_effect = WebDriverException("gone")
        driver.find_elements.return_value = [_Card(c) for c in cards]
        return driver

    def test_a_sighting_is_the_only_true(self):
        driver = self._driver([f"{_BODY} …see more"])
        assert sc.occasion_post_landed(driver, _BODY, polls=2, sleep=_no_sleep) is True

    def test_a_feed_that_rendered_without_our_post_is_false(self):
        driver = self._driver(["Somebody else's post entirely, about something else."])
        assert sc.occasion_post_landed(driver, _BODY, polls=2, sleep=_no_sleep) is False

    def test_a_feed_that_rendered_nothing_grounds_nothing(self):
        """None, not False.

        'We could not read the feed' must never be recorded as 'it is not there' — the caller holds
        the row for a human either way, but the two are different facts and the log has to say which.
        """
        assert sc.occasion_post_landed(self._driver([]), _BODY, polls=2, sleep=_no_sleep) is None

    def test_a_driver_that_raises_grounds_nothing(self):
        driver = self._driver([], raises=True)
        assert sc.occasion_post_landed(driver, _BODY, polls=2, sleep=_no_sleep) is None

    def test_a_body_too_short_to_match_grounds_nothing(self):
        driver = self._driver(["anything at all"])
        assert sc.occasion_post_landed(driver, "Shipped.", polls=2, sleep=_no_sleep) is None
        driver.get.assert_not_called()


class _Chain:
    """Drives the walk by the STEP each lookup asks for, not by call position.

    Since #1621 the steps below the trigger are label matches inside the resolved composer
    container rather than page-level locator chains, so the step is named by the labels the walk
    passes — a failing test still says which step broke.
    """

    def __init__(self, missing=(), missing_first=(), container_controls=3):
        self.missing = set(missing)
        self.missing_first = set(missing_first)
        self.container_controls = container_controls
        self.clicked = []
        self.typed = []

    @staticmethod
    def _step(labels) -> str:
        labels = tuple(labels or ())
        if labels == tuple(sc.OCCASION_ENTRY_LABELS):
            return "Celebrate an occasion"
        if labels == tuple(sc.OCCASION_MORE_LABELS):
            return "Composer overflow"
        if labels == tuple(sc.POST_BUTTON_LABELS):
            return "Occasion Post button"
        if labels == tuple(sc.TEMPLATE_CHOOSER_NEXT_LABELS):
            return "Template chooser Next"
        return "Occasion type"

    def _resolve(self, step):
        self.clicked.append(step)
        if step in self.missing:
            return None
        if step in self.missing_first and self.clicked.count(step) == 1:
            return None
        return MagicMock()

    def click(self, driver, wait, locators, label, **kwargs):
        """The share box — still an ordinary light-DOM locator chain."""
        return self._resolve(label)

    def container(self, driver, **kwargs):
        if "Composer container" in self.missing:
            return None
        return MagicMock()

    def control(self, container, labels, exact=False, css=None):
        if container is None:
            return None
        return self._resolve(self._step(labels))

    def deep(self, driver, css, **kwargs):
        """Two different questions ride this one helper: the editor, and the zero-walk cross-check.

        The cross-check counts the CONTAINER's own controls (#1621) — page-wide it would answer
        with the feed's, which is the reading that made a closed composer grade the same as an
        open one.
        """
        if css == sc.COMPOSER_EDITOR_CSS:
            if "Occasion post editor" in self.missing:
                return []
            box = MagicMock()
            box.send_keys.side_effect = lambda text: self.typed.append(text)
            return [box]
        return [MagicMock() for _ in range(self.container_controls)]


class TestPublishOccasionNatively:
    def _run(self, chain, landed=True, page_native=3, archetype="project_launch", body=_BODY):
        driver = MagicMock()
        with patch(f"{_MOD}.click_first", side_effect=chain.click), \
             patch(f"{_MOD}.find_composer_container", side_effect=chain.container), \
             patch(f"{_MOD}.find_composer_control", side_effect=chain.control), \
             patch(f"{_MOD}.find_deep_elements", side_effect=chain.deep), \
             patch(f"{_MOD}.page_native_count", return_value=page_native), \
             patch(f"{_MOD}.occasion_post_landed", return_value=landed):
            return sc.publish_occasion_natively(driver, MagicMock(), archetype, body,
                                                user_id=1, post_id=5, sleep=_no_sleep)

    def test_the_happy_path_publishes_the_body_it_was_given(self):
        chain = _Chain()
        result = self._run(chain)

        assert result.state == sc.PUBLISHED
        assert chain.typed == [_BODY]
        assert "Occasion Post button" in chain.clicked
        assert "Template chooser Next" in chain.clicked

    def test_a_variant_with_no_template_chooser_still_reaches_the_editor(self):
        """The chooser click is optional (#1621 grounding).

        A variant that skips straight to the editor must not be treated as a miss.
        """
        chain = _Chain(missing={"Template chooser Next"})
        result = self._run(chain)

        assert result.state == sc.PUBLISHED
        assert chain.typed == [_BODY]

    def test_an_unmapped_archetype_never_opens_the_composer(self):
        chain = _Chain()
        result = self._run(chain, archetype="case_snapshot")

        assert result.state == sc.NO_OCCASION_TYPE
        assert chain.clicked == []
        assert chain.typed == []

    def test_an_empty_body_never_opens_the_composer(self):
        chain = _Chain()
        assert self._run(chain, body="   ").state == sc.NO_OCCASION_TYPE
        assert chain.clicked == []

    def test_the_overflow_is_only_opened_when_the_first_row_missed(self):
        first_row = _Chain()
        self._run(first_row)
        assert "Composer overflow" not in first_row.clicked

        behind_overflow = _Chain(missing_first={"Celebrate an occasion"})
        result = self._run(behind_overflow)
        assert result.state == sc.PUBLISHED
        assert "Composer overflow" in behind_overflow.clicked

    @pytest.mark.parametrize("missing,state", [
        ({"Share box"}, sc.NO_SHARE_BOX),
        ({"Composer container"}, sc.NO_COMPOSER),
        ({"Celebrate an occasion", "Composer overflow"}, sc.NO_OCCASION_ENTRY),
        ({"Occasion type"}, sc.NO_OCCASION_TYPE),
        ({"Occasion post editor"}, sc.NO_EDITOR),
        ({"Occasion Post button"}, sc.NO_POST_BUTTON),
    ])
    def test_each_blocked_step_names_itself_and_records_a_zero_walk(self, missing, state):
        chain = _Chain(missing=missing)
        result = self._run(chain)

        assert result.state == state
        # The drift funnel is the point: a blocked walk that graded nothing is the silent skip
        # #1013 exists to stop.
        assert result.zero_walk == "drift"

    def test_a_page_that_rendered_nothing_grades_unknown_not_drift(self):
        chain = _Chain(missing={"Share box"})
        result = self._run(chain, page_native=None)

        assert result.state == sc.NO_SHARE_BOX
        assert result.zero_walk == "unknown"

    def test_the_occasion_type_is_never_typed_around(self):
        """A type that will not resolve is terminal.

        The run must not fall through to the editor and publish the body as an ordinary update,
        which is the exact post #1074 exists to avoid.
        """
        chain = _Chain(missing={"Occasion type"})
        self._run(chain)
        assert chain.typed == []

    @pytest.mark.parametrize("landed,zero_walk", [(False, "empty"), (None, "unknown")])
    def test_a_click_the_feed_never_confirmed_is_unconfirmed(self, landed, zero_walk):
        result = self._run(_Chain(), landed=landed)

        assert result.state == sc.UNCONFIRMED
        assert result.zero_walk == zero_walk

    def test_a_composer_that_never_opened_stops_before_the_occasion_anchors(self):
        """A trigger that pressed and opened nothing says NOTHING about the occasion labels.

        Reporting it as a missing occasion control is what sent #1088 hunting the wrong anchors
        for a day (#1621).
        """
        chain = _Chain(missing={"Composer container"})
        result = self._run(chain)

        assert result.state == sc.NO_COMPOSER
        assert chain.clicked == ["Share box"]
        assert chain.typed == []

    def test_the_zero_walk_reads_the_composer_not_the_page(self):
        """The cross-check counts the CONTAINER's controls.

        Counted page-wide it would answer with the feed's cards on every miss — always 'drift',
        which is a verdict that has stopped meaning anything (#1621).
        """
        chain = _Chain(missing={"Occasion Post button"}, container_controls=0)
        result = self._run(chain)

        assert result.state == sc.NO_POST_BUTTON
        assert result.zero_walk == "empty"

    def test_a_browser_fault_is_a_result_not_an_exception(self):
        driver = MagicMock()
        driver.get.side_effect = WebDriverException("session died")
        result = sc.publish_occasion_natively(driver, MagicMock(), "project_launch", _BODY,
                                              sleep=_no_sleep)
        assert result.state == sc.DRIVER_ERROR


class TestFindComposerContainer:
    """Where the composer MOUNTED, which #1621 proved is a different question from what opens it."""

    def _container(self, editors=1):
        container = MagicMock()
        container.find_elements.return_value = [MagicMock()] * editors
        return container

    def test_the_container_carrying_the_editor_wins(self):
        """The composer is the container with the editor in it.

        A feed page ships hidden `role='dialog'` surfaces of its own — the video player's error
        and caption dialogs — and visibility alone does not tell them apart.
        """
        decoy, composer = self._container(editors=0), self._container()
        with patch(f"{_MOD}.find_deep_elements", return_value=[decoy, composer]):
            assert sc.find_composer_container(MagicMock()) is composer

    def test_a_container_without_an_editor_is_still_better_than_nothing(self):
        decoy = self._container(editors=0)
        with patch(f"{_MOD}.find_deep_elements", return_value=[decoy]):
            assert sc.find_composer_container(MagicMock()) is decoy

    def test_nothing_open_answers_none(self):
        with patch(f"{_MOD}.find_deep_elements", return_value=[]):
            assert sc.find_composer_container(MagicMock()) is None

    def test_a_container_that_cannot_be_read_is_skipped_not_raised(self):
        broken, composer = MagicMock(), self._container()
        broken.find_elements.side_effect = WebDriverException("detached")
        with patch(f"{_MOD}.find_deep_elements", return_value=[broken, composer]):
            assert sc.find_composer_container(MagicMock()) is composer

    def test_the_lookup_is_shadow_aware(self):
        """The whole point: `driver.find_elements` cannot reach `#interop-outlet`'s shadow root."""
        with patch(f"{_MOD}.find_deep_elements", return_value=[]) as deep:
            sc.find_composer_container(MagicMock())
        assert deep.call_args.args[1] == sc.COMPOSER_CONTAINER_CSS


class TestTheShareBoxChainIsShared:
    def test_the_group_composer_reads_the_same_chain(self):
        """One composer, one map (#1088).

        A second copy is what leaves the live probe grounding a chain nothing ships.
        """
        from cqc_lem.app.engagement import feed

        assert feed._GROUP_SHARE_BOX_LOCATORS is sc.SHARE_BOX_LOCATORS
        assert feed._GROUP_SHARE_BOX_TEXT_SIGNALS is sc.SHARE_BOX_TEXT_SIGNALS
