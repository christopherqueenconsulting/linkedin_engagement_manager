"""The Connect-dialog miss evidence reads BOTH document layers (issue #1813).

#1733 established that the Connect dialog mounts inside an open shadow root and taught
`_connect_dialog_present` to cross that boundary with `find_deep_elements`. The evidence line
beside it kept using `driver.find_elements`, which cannot — so nineteen days of production dumps
described the profile page and none of them described the overlay, which is the only part of a
miss worth reading. The lane sent zero invites over that whole window.

Two readings, because a miss has two shapes:

- an EMPTY overlay next to a present affordance says the click never opened anything;
- an overlay carrying a wall notice says it opened and LinkedIn refused. `_invite_restriction_reason`
  reads the light DOM only and returns None on an unreadable page by design, so that second shape
  is invisible to it and reaches the log as an ordinary selector miss.

Printing the words is what separates them without a live session.
"""

from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit

_INV = "cqc_lem.app.engagement.invites"


def _control(label: str):
    """A stand-in for a deep-scanned control answering to `label`."""
    element = MagicMock()
    element.get_attribute.side_effect = lambda name: label if name == "aria-label" else None
    element.text = label
    return element


def _container(text: str):
    element = MagicMock()
    element.text = text
    return element


def _driver(main_labels=(), anchors=()):
    """A driver whose LIGHT-DOM reads answer, and whose shadow reads are patched separately."""
    driver = MagicMock()

    def find_elements(by, value):
        if "custom-invite" in str(value):
            return [_control(href) for href in anchors]
        return [_control(label) for label in main_labels]

    driver.find_elements.side_effect = find_elements
    return driver


def _deep(controls=(), containers=()):
    """A `find_deep_elements` stand-in that answers by selector, the way the real one does."""
    def _fake(driver, css, **kwargs):
        if "dialog" in css:
            return list(containers)
        return list(controls)
    return _fake


class TestTheOverlayHalfOfTheEvidence:

    def test_a_control_only_a_shadow_scan_can_see_is_reported(self):
        """The datum that says the dialog DID open: a control absent from the light-DOM pass."""
        with patch(f"{_INV}.find_deep_elements",
                   side_effect=_deep(controls=[_control("Send without a note")])):
            from cqc_lem.app.engagement.invites import _miss_evidence
            line = _miss_evidence(_driver(main_labels=["Follow Jane Doe"]))

        assert "send without a note" in line
        assert "overlay controls=" in line

    def test_a_wall_notice_in_the_overlay_reaches_the_log(self):
        """The shape `_invite_restriction_reason` cannot see, because it never crosses the boundary.

        Its light-DOM read returns None here, which is the ordinary-miss path — so unless the words
        are printed, a walled account and a dead selector write the identical line.
        """
        notice = "You've reached the weekly invitation limit. Try again next week."
        with patch(f"{_INV}.find_deep_elements",
                   side_effect=_deep(containers=[_container(notice)])):
            from cqc_lem.app.engagement.invites import _miss_evidence
            line = _miss_evidence(_driver())

        assert "weekly invitation limit" in line

    def test_a_label_the_light_dom_pass_already_reported_is_not_repeated(self):
        """Overlay controls carry what the profile pass could NOT reach — repeating it buries it."""
        with patch(f"{_INV}.find_deep_elements",
                   side_effect=_deep(controls=[_control("More"), _control("Add a note")])):
            from cqc_lem.app.engagement.invites import _miss_evidence
            line = _miss_evidence(_driver(main_labels=["More"]))

        overlay = line.split("overlay controls=", 1)[1]
        assert "add a note" in overlay
        assert "more" not in overlay.split("overlay text=", 1)[0]

    def test_an_empty_overlay_beside_a_present_affordance_is_visible_as_such(self):
        """The Class A signature: the page offers Connect and the overlay has nothing in it."""
        with patch(f"{_INV}.find_deep_elements", side_effect=_deep()):
            from cqc_lem.app.engagement.invites import _miss_evidence
            line = _miss_evidence(
                _driver(main_labels=["Invite Jane Doe to connect"],
                        anchors=["https://www.linkedin.com/preload/custom-invite/?vanityName=x"]))

        assert "Invite Jane Doe to connect" in line
        assert "overlay controls=[]" in line
        assert "overlay text=''" in line


class TestEvidenceNeverCostsTheRun:

    def test_a_shadow_scan_that_raises_still_yields_the_light_dom_half(self):
        """Evidence is best-effort everywhere else on this path and stays that way here."""
        with patch(f"{_INV}.find_deep_elements", side_effect=RuntimeError("boom")):
            from cqc_lem.app.engagement.invites import _miss_evidence
            line = _miss_evidence(_driver(main_labels=["Follow Jane Doe"]))

        assert "Follow Jane Doe" in line
        assert "overlay controls=[]" in line


class TestTheUrlRoutesOwnEvidence:
    """Route 4 navigates away, so whatever it rendered can only be read after it ran.

    #1733 recorded that page as blank — measured with light-DOM reads, which is indistinguishable
    from a shadow-mounted overlay. If the in-app route DID open the dialog (or refuse it in words),
    this is the only place that is visible, and the pre-navigation evidence cannot carry it.
    """

    def test_what_the_url_route_rendered_is_appended_to_the_miss_line(self):
        from cqc_lem.app.engagement import invites

        notice = "You've reached the weekly invitation limit."
        with patch.object(invites, "_click_own_custom_invite_anchor", return_value=False), \
             patch.object(invites, "_click_own_connect_button", return_value=False), \
             patch.object(invites, "click_first", return_value=None), \
             patch.object(invites, "_connect_dialog_present", return_value=False), \
             patch.object(invites, "_invite_restriction_reason", return_value=None), \
             patch.object(invites, "_miss_evidence", return_value="profile evidence"), \
             patch.object(invites, "_overlay_evidence", return_value=([], notice)), \
             patch.object(invites, "log_warning"), \
             patch.object(invites, "log_info") as info:
            driver = MagicMock()
            driver.current_url = "https://www.linkedin.com/in/jane-doe-123/"
            opened, reason = invites._open_connect_invite_dialog(
                driver, MagicMock(), 1, "https://www.linkedin.com/in/jane-doe-123/")

        assert (opened, reason) == (False, None)
        logged = " ".join(str(call) for call in info.call_args_list)
        assert "after url route" in logged
        assert "weekly invitation limit" in logged
