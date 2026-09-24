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

        An occasion the picker offers but LEM does not map is somebody else's announcement; matching
        one of them publishes it.
        """
        forbidden = ("new skill", "welcome to the team", "job search")
        for labels in sc.OCCASION_TYPE_LABELS.values():
            for label in labels:
                assert not any(bad in label for bad in forbidden)
                # A bare word is what a neighbour's description carries (#2140).
                assert len(label.split()) >= 2

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
        with patch(f"{_MOD}.find_deep_elements", return_value=[decoy]), \
             patch(f"{_MOD}.find_enclosing_container", return_value=None):
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
        assert deep.call_args_list[0].args[1] == sc.COMPOSER_CONTAINER_CSS

    def test_the_dialog_rung_is_tried_before_the_full_page_one(self):
        """An ordered CHAIN, not a replacement (#2020).

        LinkedIn serves different DOMs per viewer, and the shadow-mounted modal #1621 measured is
        still one of them — so the full-page walk must never pre-empt a dialog that has the editor.
        """
        composer = self._container()
        with patch(f"{_MOD}.find_deep_elements", return_value=[composer]), \
             patch(f"{_MOD}.find_enclosing_container") as walk:
            assert sc.find_composer_container(MagicMock()) is composer
        walk.assert_not_called()


class TestTheFullPageComposer:
    """#2066: "Start a post" navigates to `/sharing/compose` and there is no dialog at all.

    Live-grounded 2026-09-14: the editor is on screen, `role='dialog'` and `aria-modal` are both
    absent, and every class on the nest is a rotating hash. The container can only be named by what
    it CONTAINS, which is what this rung does.
    """

    def test_the_editor_s_enclosing_box_is_the_composer(self):
        editor, box = MagicMock(), MagicMock()
        with patch(f"{_MOD}.find_deep_elements", side_effect=[[], [editor]]), \
             patch(f"{_MOD}._card_for_textbox", return_value=None), \
             patch(f"{_MOD}.find_enclosing_container", return_value=box) as walk:
            assert sc.find_composer_container(MagicMock()) is box
        assert walk.call_args.args[1] is editor
        assert walk.call_args.args[3] == sc.POST_BUTTON_LABELS

    def test_an_editor_with_no_post_control_above_it_is_not_a_composer(self):
        """A comment box on a feed card has an editor too — the commit control is the difference."""
        with patch(f"{_MOD}.find_deep_elements", side_effect=[[], [MagicMock()]]), \
             patch(f"{_MOD}._card_for_textbox", return_value=None), \
             patch(f"{_MOD}.find_enclosing_container", return_value=None):
            assert sc.find_composer_container(MagicMock()) is None

    def test_the_walk_tries_every_editor_before_giving_up(self):
        """A locator is an exhausted CHAIN, never the first candidate (#2020)."""
        first, second, box = MagicMock(), MagicMock(), MagicMock()
        with patch(f"{_MOD}.find_deep_elements", side_effect=[[], [first, second]]), \
             patch(f"{_MOD}._card_for_textbox", return_value=None), \
             patch(f"{_MOD}.find_enclosing_container", side_effect=[None, box]):
            assert sc.find_composer_container(MagicMock()) is box

    def test_a_cards_comment_box_is_never_the_composer(self):
        """#1012, on a write that cannot be taken back.

        A card's comment SUBMIT control is labelled "Post" too (`linkedin/composer` matches that
        exact text), so a comment box answers "an editor with a Post control above it" perfectly.
        Handing one back would make `auto_post_to_group` type the group draft into somebody else's
        post and press its commit control. The card's own comment ACTION is the discriminator.
        """
        comment_box, composer_editor, box = MagicMock(), MagicMock(), MagicMock()
        with patch(f"{_MOD}.find_deep_elements",
                   side_effect=[[], [comment_box, composer_editor]]), \
             patch(f"{_MOD}._card_for_textbox",
                   side_effect=lambda d, el: MagicMock() if el is comment_box else None), \
             patch(f"{_MOD}.find_enclosing_container", return_value=box) as walk:
            assert sc.find_composer_container(MagicMock()) is box
        # The card's box was never walked up from at all.
        assert [call.args[1] for call in walk.call_args_list] == [composer_editor]

    def test_a_page_that_cannot_be_read_resolves_no_composer(self):
        """Fail CLOSED: an unreadable card check must not become a write to the wrong entity."""
        with patch(f"{_MOD}.find_deep_elements", side_effect=[[], [MagicMock()]]), \
             patch(f"{_MOD}._card_for_textbox", side_effect=WebDriverException("gone")), \
             patch(f"{_MOD}.find_enclosing_container", return_value=MagicMock()) as walk:
            assert sc.find_composer_container(MagicMock()) is None
        walk.assert_not_called()


class TestComposerOpenSignal:
    """The page-native cross-check: is a composer open, asked WITHOUT the container chain."""

    def _control(self, label):
        control = MagicMock()
        control.get_attribute.side_effect = lambda name: label if name == "aria-label" else None
        return control

    def test_counts_only_controls_whose_whole_label_is_the_commit_one(self):
        controls = [self._control("Post"), self._control("Repost"), self._control("Schedule post")]
        with patch(f"{_MOD}.find_deep_elements", return_value=controls):
            assert sc.composer_open_signal(MagicMock()) == 1

    def test_a_closed_composer_answers_zero_not_none(self):
        """Zero is a real answer — it is what makes a quiet no-op an EMPTY verdict, not drift."""
        with patch(f"{_MOD}.find_deep_elements",
                   return_value=[self._control("Follow Sundar Pichai")]):
            assert sc.composer_open_signal(MagicMock()) == 0

    def test_a_page_with_no_clickable_at_all_is_unreadable_not_empty(self):
        """A read we could not take must never be recorded as the page saying zero (#1013)."""
        with patch(f"{_MOD}.find_deep_elements", return_value=[]):
            assert sc.composer_open_signal(MagicMock()) is None

    def test_a_driver_fault_answers_none(self):
        with patch(f"{_MOD}.find_deep_elements", side_effect=WebDriverException("gone")):
            assert sc.composer_open_signal(MagicMock()) is None

    def test_the_signal_does_not_reuse_the_container_chain(self):
        """Cross-checking a chain against its own selector proves nothing (zero_walk.py)."""
        with patch(f"{_MOD}.find_deep_elements", return_value=[]) as deep:
            sc.composer_open_signal(MagicMock())
        assert deep.call_args.args[1] != sc.COMPOSER_CONTAINER_CSS


class TestTheShareBoxChainIsShared:
    def test_the_group_composer_reads_the_same_chain(self):
        """One composer, one map (#1088).

        A second copy is what leaves the live probe grounding a chain nothing ships.
        """
        from cqc_lem.app.engagement import feed

        assert feed._GROUP_SHARE_BOX_LOCATORS is sc.SHARE_BOX_LOCATORS
        assert feed._GROUP_SHARE_BOX_TEXT_SIGNALS is sc.SHARE_BOX_TEXT_SIGNALS


class TestTheSeptember2026Reground:
    """What the live DOM became on 2026-09-14 (#2067), one assertion per thing that moved.

    The share box stopped opening an overlay on the feed and started NAVIGATING to
    `linkedin.com/sharing/compose`, which mounts the composer — and the occasion picker behind it —
    in a native `<dialog data-testid="dialog">` with neither `role` nor `aria-modal`; the occasion
    entry became an `<a href>` labelled "Celebration"; and the composer's overflow was renamed
    "Expand content types". Every label below is quoted from the probe reading in
    `docs/sdui-selenium-notes.md`.
    """

    @staticmethod
    def _control(tag: str, label: str, aria: str = None):
        control = MagicMock()
        control.tag_name = tag
        control.text = label
        control.is_displayed.return_value = True
        control.get_attribute.side_effect = lambda name: {"aria-label": aria}.get(name)
        return control

    @staticmethod
    def _container(controls):
        container = MagicMock()
        container.find_elements.return_value = list(controls)
        return container

    def test_the_native_dialog_element_is_a_container_rung(self):
        """The picker carries no `role` and no `aria-modal` — only the tag says what it is."""
        assert "dialog" in [part.strip() for part in sc.COMPOSER_CONTAINER_CSS.split(",")]

    def test_the_entry_is_reached_as_a_link_labelled_celebration(self):
        """The real matcher, not a stubbed one: "celebrate" is word-bounded and cannot reach it."""
        link = self._control("a", "Celebration")
        container = self._container([self._control("button", "Post"), link])

        assert sc.find_composer_control(container, sc.OCCASION_ENTRY_LABELS,
                                        css=sc.OCCASION_ENTRY_CSS) is link

    def test_the_entry_css_is_the_only_lookup_that_admits_a_link(self):
        """Widening the SHARED set is how a walk reaches a control it was never meant to see."""
        assert "a[href]" in sc.OCCASION_ENTRY_CSS
        assert "a[href]" not in sc.COMPOSER_AFFORDANCE_CSS

    def test_the_renamed_overflow_is_a_rung_not_a_replacement(self):
        assert sc.OCCASION_MORE_LABELS[0] == "more"
        assert "expand content types" in sc.OCCASION_MORE_LABELS

    def test_the_picker_still_never_settles_for_a_neighbouring_occasion(self):
        """#1012 on the re-grounded screen: "New certification" sits one row from the target."""
        options = [self._control("div", "Work anniversary Work anniversary"),
                   self._control("div", "New certification Celebrate a new certification."),
                   self._control("div", "Project launch Share a new project milestone."),
                   self._control("div", "New educational milestone Share an educational "
                                        "milestone.")]
        container = self._container(options)

        launch = sc.find_composer_control(container,
                                          sc.occasion_type_labels(
                                              sc.OCCASION_TYPE_LABELS["project_launch"]))
        milestone = sc.find_composer_control(
            container, sc.occasion_type_labels(sc.OCCASION_TYPE_LABELS["educational_milestone"]))

        assert launch is options[2]
        assert milestone is options[3]

    # The live "Select occasion" picker as the 2026-09-14 probe read it (#2067): title and
    # description in ONE node, so every pick goes through the word-bounded fallback.
    _LIVE_PICKER = {
        "project_launch": "Project launch Share a new project milestone.",
        "work_anniversary": "Work anniversary Work anniversary",
        "new_position": "New position Share a job update.",
        "educational_milestone": "New educational milestone Share an educational milestone.",
        "new_certification": "New certification Celebrate a new certification.",
    }

    @pytest.mark.parametrize("closer", ["Next", "Done"])
    def test_the_template_chooser_is_passed_on_either_live_closer(self, closer):
        """#2140: "Project launch" ends the chooser with Next, the other four with Done.

        Matched exactly, so Dismiss and Back — the controls beside it — are never the pick.
        """
        closing = self._control("button", closer)
        container = self._container([self._control("button", "Dismiss"),
                                     self._control("button", "Add a photo"),
                                     self._control("button", "Back"), closing])

        assert sc.TEMPLATE_CHOOSER_NEXT_LABELS[0] == "next"
        assert sc.find_composer_control(container, sc.TEMPLATE_CHOOSER_NEXT_LABELS,
                                        exact=True) is closing

    def test_the_map_covers_every_row_of_the_picker(self):
        assert set(sc.OCCASION_TYPE_LABELS) == set(self._LIVE_PICKER)

    @pytest.mark.parametrize("archetype", sorted(_LIVE_PICKER))
    @pytest.mark.parametrize("rotation", range(5))
    def test_each_archetype_resolves_to_exactly_its_own_row(self, archetype, rotation):
        """#2140/#1012: every row resolves to itself in any document order, never one row over."""
        keys = sorted(self._LIVE_PICKER)
        keys = keys[rotation:] + keys[:rotation]
        rows = {key: self._control("div", self._LIVE_PICKER[key]) for key in keys}
        container = self._container([rows[key] for key in keys])

        picked = sc.find_composer_control(
            container, sc.occasion_type_labels(sc.OCCASION_TYPE_LABELS[archetype]))

        assert picked is rows[archetype]

    @pytest.mark.parametrize("archetype", sorted(_LIVE_PICKER))
    def test_an_archetype_whose_row_is_gone_resolves_nothing(self, archetype):
        """With its own row missing, the four neighbours must all stay unclickable.

        A walk that settled for one would publish a claim about the author nobody made.
        """
        container = self._container([self._control("div", text)
                                     for key, text in self._LIVE_PICKER.items()
                                     if key != archetype])

        assert sc.find_composer_control(
            container, sc.occasion_type_labels(sc.OCCASION_TYPE_LABELS[archetype])) is None
