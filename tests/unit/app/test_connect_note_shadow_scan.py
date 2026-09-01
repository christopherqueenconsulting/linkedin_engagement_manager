"""The note textarea is reached the same way the Send button is: scoped to the dialog (issue #1841).

#1813 (A3) scoped the *control* scan to the dialog container so `Send without a note` could be
found in the shadow root, and the lane started sending. The *note* textarea lookup was left on an
unscoped `find_deep_elements` call — searching the whole document with the same `limit` #1813 had
already proven gets spent on page chrome before reaching the dialog — so every invite the lane sent
went out without its note. Production, `v0.172.0`, 2026-09-01:

    WARNING [logger.py:348]: Could not attach a note to the connection request; sending it without one
    selenium.common.exceptions.TimeoutException: Message: Finding Message Box

`_dialog_field_candidates` mirrors `_dialog_control_candidates`'s container-first scoping for a
text field, and `_add_connect_note` now reads the textarea through it before falling back to the
light-DOM XPath — never re-finding a shadow-mounted element by an XPath that never saw it (#1733).
"""

from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit

_INV = "cqc_lem.app.engagement.invites"


class TestDialogFieldCandidatesScoping:

    def test_a_field_behind_a_full_page_of_chrome_is_found_inside_the_dialog(self):
        """The production case: the document-order scan never reaches the textarea, the scoped one does."""
        container = MagicMock()
        box = MagicMock()
        page_chrome = [MagicMock() for _ in range(20)]  # spends the unscoped scan's whole budget

        def deep(driver, css, *, visible_only=True, limit=20, root=None):
            if "dialog" in css:
                return [container]
            if "textarea" in css:
                return [box] if root is container else []
            return page_chrome

        with patch(f"{_INV}.find_deep_elements", side_effect=deep):
            from cqc_lem.app.engagement.invites import _dialog_field_candidates
            found = _dialog_field_candidates(MagicMock(), "textarea#custom-message")

        assert found == [box]

    def test_no_dialog_container_falls_back_to_the_document_wide_scan(self):
        """A rotation that mounts the field without a dialog role must not lose the #1733 behaviour."""
        box = MagicMock()

        def deep(driver, css, *, visible_only=True, limit=20, root=None):
            if "dialog" in css:
                return []
            return [box]

        with patch(f"{_INV}.find_deep_elements", side_effect=deep):
            from cqc_lem.app.engagement.invites import _dialog_field_candidates
            found = _dialog_field_candidates(MagicMock(), "textarea#custom-message")

        assert found == [box]

    def test_an_empty_container_falls_through_rather_than_giving_up(self):
        container = MagicMock()
        box = MagicMock()

        def deep(driver, css, *, visible_only=True, limit=20, root=None):
            if "dialog" in css:
                return [container]
            return [] if root is container else [box]

        with patch(f"{_INV}.find_deep_elements", side_effect=deep):
            from cqc_lem.app.engagement.invites import _dialog_field_candidates
            found = _dialog_field_candidates(MagicMock(), "textarea#custom-message")

        assert found == [box]


class TestAddConnectNoteReadsTheScopedTextarea:

    def _run(self, deep_side_effect, message="hi jane"):
        from cqc_lem.app.engagement import invites as ra
        note_button = MagicMock()
        with patch(f"{_INV}.find_first", return_value=note_button), \
             patch(f"{_INV}.find_deep_elements", side_effect=deep_side_effect), \
             patch(f"{_INV}.click_element_wait_retry") as click_wait, \
             patch(f"{_INV}.time.sleep"), \
             patch(f"{_INV}.log_warning") as log_warning, \
             patch(f"{_INV}.log_info") as log_info:
            result = ra._add_connect_note(MagicMock(), MagicMock(), message, 1)
        return result, note_button, click_wait, log_warning, log_info

    def test_a_textarea_only_reachable_in_the_shadow_root_is_typed_into(self):
        """Given a dialog whose textarea is inside the shadow root, the note is typed.

        No XPath fallback (which cannot cross the shadow boundary) is even attempted.
        """
        container = MagicMock()
        box = MagicMock()

        def deep(driver, css, *, visible_only=True, limit=20, root=None):
            if "dialog" in css:
                return [container]
            if "textarea" in css and root is container:
                return [box]
            return []

        result, note_button, click_wait, log_warning, log_info = self._run(deep)

        assert result is True
        note_button.click.assert_called_once()
        box.click.assert_called_once()
        box.clear.assert_called_once()
        box.send_keys.assert_called_once_with("hi jane")
        click_wait.assert_not_called()  # the shadow-scoped hit answered; no light-DOM re-find
        log_warning.assert_not_called()

    def test_a_light_dom_textarea_still_works_unchanged(self):
        """Given a dialog whose textarea is in the light DOM, behaviour is unchanged.

        The shadow scan comes back empty and the existing XPath fallback still lands the note.
        """
        from cqc_lem.app.engagement import invites as ra

        box = MagicMock()
        note_button = MagicMock()

        def deep(driver, css, *, visible_only=True, limit=20, root=None):
            return []  # nothing in the shadow tree at all

        def click_wait(driver, wait, xpath, label, **kwargs):
            assert xpath == '//textarea[@id="custom-message"]'
            return box

        with patch(f"{_INV}.find_first", return_value=note_button), \
             patch(f"{_INV}.find_deep_elements", side_effect=deep), \
             patch(f"{_INV}.click_element_wait_retry", side_effect=click_wait), \
             patch(f"{_INV}.time.sleep"), \
             patch(f"{_INV}.log_warning") as log_warning:
            result = ra._add_connect_note(MagicMock(), MagicMock(), "hi jane", 1)

        assert result is True
        box.clear.assert_called_once()
        box.send_keys.assert_called_once_with("hi jane")
        log_warning.assert_not_called()

    def test_a_broader_fallback_selector_catches_a_textarea_without_the_id(self):
        """The `textarea, [contenteditable]` fallback inside the dialog container the scope calls for."""
        container = MagicMock()
        box = MagicMock()

        def deep(driver, css, *, visible_only=True, limit=20, root=None):
            if "dialog" in css:
                return [container]
            if css == "textarea#custom-message":
                return []
            if "contenteditable" in css and root is container:
                return [box]
            return []

        result, note_button, click_wait, log_warning, log_info = self._run(deep)

        assert result is True
        box.send_keys.assert_called_once_with("hi jane")
        click_wait.assert_not_called()
        log_warning.assert_not_called()
