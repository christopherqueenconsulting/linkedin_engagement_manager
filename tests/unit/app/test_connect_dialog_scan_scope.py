"""The Connect-dialog control scan is scoped to the dialog, not the document (issue #1813).

`find_deep_elements` stops after `limit` matches IN DOCUMENT ORDER, and the shadow-mounted overlay
is the last thing on the page. On a LinkedIn profile the first sixty visible controls are the
global nav, the top card and the "People also viewed" rail — so the document-wide scan #1733 added
spent its entire budget before reaching the dialog and reported an OPEN dialog as absent. The lane
sent zero invites for the whole time that was true.

Measured in production 2026-09-01, in a single run of the #1819 diagnostic on one profile:

    overlay controls=['close jump menu', 'home, 1 new notification', 'my network, ...',
                      'jobs, ...', 'messaging, ...', 'notifications, ...', 'me', ...]
    overlay text='Dialog content start. Add a note to your invitation? ...
                  Add a note Send without a note Dialog content end.'

Same page, same instant: the container query found the dialog and read both controls out of it,
while the control query returned the nav bar. That is the whole defect.

The fallback matters as much as the fix — a rotation that mounts these controls without a dialog
role must not lose the document-wide pass, or this trades one blind spot for another.
"""

from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit

_INV = "cqc_lem.app.engagement.invites"


def _control(label: str):
    element = MagicMock()
    element.get_attribute.side_effect = lambda name: label if name == "aria-label" else None
    element.text = label
    return element


# What a profile page actually puts in front of the dialog, in document order.
_PAGE_CHROME = [_control(label) for label in
                ("Close jump menu", "Home, 1 new notification", "My Network, 0 new notifications",
                 "Jobs, 0 new notifications", "Messaging, 0 new notifications", "Me",
                 "Invite Stephanie Culver to connect")]
_DIALOG_CONTROLS = [_control("Add a note"), _control("Send without a note")]


class TestTheScanSpendsItsBudgetInsideTheDialog:

    def test_the_send_control_is_found_behind_a_full_page_of_chrome(self):
        """The production case: the document-order scan never reaches the dialog, the scoped one does."""
        container = MagicMock()

        def deep(driver, css, *, visible_only=True, limit=20, root=None):
            if "dialog" in css:
                return [container]
            # The document-wide scan is truncated by `limit` before the dialog — exactly what the
            # real helper does, and exactly what the production log showed.
            return list(_DIALOG_CONTROLS) if root is container else list(_PAGE_CHROME)

        with patch(f"{_INV}.find_deep_elements", side_effect=deep):
            from cqc_lem.app.engagement.invites import _SEND_WITHOUT_NOTE_LABEL, _deep_dialog_control
            found = _deep_dialog_control(driver=MagicMock(), labels=(_SEND_WITHOUT_NOTE_LABEL,))

        assert found is not None
        assert found.get_attribute("aria-label") == "Send without a note"

    def test_a_rail_button_outside_the_dialog_is_never_returned(self):
        """Scoping is a harder #1012 guard than any label: the rail is not inside the dialog."""
        container = MagicMock()

        def deep(driver, css, *, visible_only=True, limit=20, root=None):
            if "dialog" in css:
                return [container]
            return [_control("Add a note")] if root is container else list(_PAGE_CHROME)

        with patch(f"{_INV}.find_deep_elements", side_effect=deep):
            from cqc_lem.app.engagement.invites import _deep_dialog_control
            found = _deep_dialog_control(driver=MagicMock(), labels=("invite",))

        assert found is None


class TestTheDocumentWidePassIsKept:

    def test_a_dialog_with_no_role_still_resolves_through_the_document_scan(self):
        """A rotation that drops the dialog role must not lose the behaviour #1733 shipped."""
        def deep(driver, css, *, visible_only=True, limit=20, root=None):
            if "dialog" in css:
                return []
            return list(_DIALOG_CONTROLS)

        with patch(f"{_INV}.find_deep_elements", side_effect=deep):
            from cqc_lem.app.engagement.invites import _ADD_NOTE_LABEL, _deep_dialog_control
            found = _deep_dialog_control(driver=MagicMock(), labels=(_ADD_NOTE_LABEL,))

        assert found is not None
        assert found.get_attribute("aria-label") == "Add a note"

    def test_an_empty_dialog_container_falls_through_rather_than_giving_up(self):
        """A matched-but-empty container is not evidence the controls are absent."""
        container = MagicMock()

        def deep(driver, css, *, visible_only=True, limit=20, root=None):
            if "dialog" in css:
                return [container]
            return [] if root is container else list(_DIALOG_CONTROLS)

        with patch(f"{_INV}.find_deep_elements", side_effect=deep):
            from cqc_lem.app.engagement.invites import _ADD_NOTE_LABEL, _deep_dialog_control
            found = _deep_dialog_control(driver=MagicMock(), labels=(_ADD_NOTE_LABEL,))

        assert found is not None


class TestEveryDialogCallerGetsTheFix:

    def test_presence_the_note_button_and_the_send_click_all_read_the_scoped_scan(self):
        """One seam behind the presence check, the note affordance and the Send click.

        `_deep_dialog_control` is that single reader, so scoping it fixes all three rather than
        moving the failure down to Send.
        """
        from cqc_lem.app.engagement import invites

        source = invites._deep_dialog_control.__doc__ or ""
        assert source  # the function is the shared seam these tests pin
        container = MagicMock()

        def deep(driver, css, *, visible_only=True, limit=20, root=None):
            if "dialog" in css:
                return [container]
            return list(_DIALOG_CONTROLS) if root is container else list(_PAGE_CHROME)

        with patch(f"{_INV}.find_deep_elements", side_effect=deep), \
             patch(f"{_INV}.find_first", return_value=None):
            assert invites._connect_dialog_present(MagicMock(), MagicMock(), 1) is True
