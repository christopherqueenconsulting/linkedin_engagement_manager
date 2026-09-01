"""The Connect note field is reached ACROSS the shadow boundary (issue #1841).

#1813's A3 fix made the connect lane send for the first time in the project's history, and every
one of those invites went out noteless. `_add_connect_note` reached the dialog's Add-a-note button
through the shadow-piercing control scan, then looked for the textarea it opens with
`//textarea[@id="custom-message"]` — an XPath, which cannot cross a shadow boundary at all. The
document-wide CSS pass beside it got one shot with no wait, before the field the click had just
asked for had rendered.

Production, `v0.172.0`, 2026-09-01, two consecutive invites in one log:

    WARNING: Could not attach a note to the connection request; sending it without one
    TimeoutException: Message: Finding Message Box        <- the note field, by XPath
    ...
    Found Send Connection Button in the dialog's shadow root and clicked it   <- same dialog, CSS

The Send button in that dialog was reachable and the textarea in it was not. The difference is the
query language, and that is the whole defect: `find_deep_elements` is CSS-only because XPath cannot
address a shadow tree.

The note is the product — the proactive lane exists to send a PERSONALISED invite, and a row that
records a message LinkedIn never received describes an outreach action that did not happen. These
tests pin the reach, not the wording: the field is found through the dialog container, the element
that ANSWERED is the one typed into (#1733), and the light-DOM and quota-spent (#1039) paths are
unchanged.
"""

from unittest.mock import MagicMock, patch

import pytest
from selenium.common.exceptions import TimeoutException

pytestmark = pytest.mark.unit

_INV = "cqc_lem.app.engagement.invites"

# The selectors the fake DOM answers to. `test_the_fake_dom_speaks_the_modules_own_selectors` pins
# every one of them against the module, so a rotation cannot leave these tests passing on a query
# the code no longer makes.
_DIALOG_CSS = "[role='dialog'], [role='alertdialog'], dialog"
_CONTROL_CSS = "button, a, [role='button']"
_NOTE_CSS = "textarea#custom-message"
_BROAD_CSS = "textarea, [contenteditable='true']"
_TEXTAREA_XPATH = '//textarea[@id="custom-message"]'
_TOP_CARD_CSS = "main > section, main > div > section, main section"
_HEADING_CSS = "h1"
_TARGET_NAME = "Jane Doe"


def _control(label: str):
    """A stand-in for a deep-scanned control answering to `label`."""
    element = MagicMock()
    element.get_attribute.side_effect = lambda name: label if name == "aria-label" else None
    element.text = label
    return element


class _FakeDeepQuery:
    """A `find_deep_elements` stand-in over a fake dialog that knows where each element lives.

    The distinction the production defect turns on is WHERE a query is rooted, so the fake keeps
    two tables: what a query scoped to the dialog container can see, and what a document-wide one
    can. An element in `inside` only is a shadow-mounted control — exactly what the log above shows
    the real dialog rendering.

    Args:
        inside: `{css: [elements]}` visible to a query rooted at the dialog container.
        outside: `{css: [elements]}` visible to a document-wide query.
        has_container: Whether the dialog carries a role the container query can match.
        renders_after: How many field queries answer empty before the field appears — the dialog
            renders it in response to the Add-a-note CLICK, so the first query can be early.

    The fake also models what a LANDED send looks like, because a click is no longer the verdict
    (#1867). The top card and its name heading are ALWAYS present — a real profile has them, and a
    double that omitted them would let the not-sent tests pass through the fail-closed branch
    instead of through the affordance read they are meant to exercise. The only thing `send_lands`
    changes is the pending control on that card.
    """

    def __init__(self, *, inside: dict = None, outside: dict = None, has_container: bool = True,
                 renders_after: int = 0):
        self.container = MagicMock() if has_container else None
        if self.container is not None:
            # `_overlay_notice_text` reads the container's own text; a real element answers a
            # string, and a bare MagicMock would make the fake fail in a way production cannot.
            self.container.text = ""
        self.card = MagicMock()
        self.inside = inside or {}
        self.outside = outside or {}
        self.pending = renders_after
        self.sent = False
        self.queries: list = []

    def send_lands(self):
        """The page after an invitation actually went out: dialog gone, top card pending."""
        self.container = None
        self.sent = True

    def __call__(self, driver, css, *, visible_only=True, limit=20, root=None):
        """Answer `css` from whichever table `root` selects."""
        self.queries.append((css, root))
        if css == _TOP_CARD_CSS:
            return [self.card]
        if root is self.card:
            if css == _HEADING_CSS:
                return [_control(_TARGET_NAME)]
            # The card itself is always there; only the invite's own affordance turns up on a send.
            return [_control("Message"), _control("More")] + (
                [_control("Pending")] if self.sent else [_control("Connect")])
        if css == _DIALOG_CSS:
            return [self.container] if self.container is not None else []
        scoped = self.container is not None and root is self.container
        found = list((self.inside if scoped else self.outside).get(css, []))
        if found and css != _CONTROL_CSS and self.pending:
            self.pending -= 1
            return []
        return found[:limit]

    def field_queries(self) -> list:
        """The selectors this run used to look for a text field, in order."""
        return [css for css, _root in self.queries if css not in (_DIALOG_CSS, _CONTROL_CSS)]


def _shadow_dialog(box=None, *, field_css: str = _NOTE_CSS, renders_after: int = 0,
                   controls=("Add a note", "Send invitation"), outside: dict = None,
                   has_container: bool = True, sends: bool = True):
    """A dialog whose controls — and optionally its note field — live in the shadow root.

    Its Send control is wired to `send_lands`, so the invitation exists only once Send has been
    pressed — the outcome, not the click, is what the confirmation step reads (#1867).
    """
    from cqc_lem.app.engagement import invites

    query = _FakeDeepQuery(outside=outside or {}, has_container=has_container,
                           renders_after=renders_after)
    # Derived from the module's own Send labels, not restated here: a rotation that renames one
    # would otherwise leave the fake silently wiring nothing and every send reading as unconfirmed.
    send_labels = (invites._SEND_INVITATION_LABEL, invites._SEND_WITHOUT_NOTE_LABEL)
    elements = []
    for label in controls:
        element = _control(label)
        if sends and label.lower().startswith(send_labels):
            element.click.side_effect = query.send_lands
        elements.append(element)
    query.inside = {_CONTROL_CSS: elements}
    if box is not None:
        query.inside[field_css] = [box]
    return query


def _profile_driver():
    """A driver on the target's own profile — the page title is what attributes the top card."""
    driver = MagicMock()
    driver.title = f"{_TARGET_NAME} | LinkedIn"
    return driver


class _Result:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


def _add_note(deep, *, message: str = "hi jane", light_dom_button: bool = False,
              xpath_box=None) -> _Result:
    """Run `_add_connect_note` against the fake DOM `deep`, with every I/O seam mocked.

    Args:
        deep: The `_FakeDeepQuery` standing in for `find_deep_elements`.
        message: The note to attach.
        light_dom_button: Whether `find_first` resolves the Add-a-note button in the light DOM.
        xpath_box: The element the light-DOM XPath fallback resolves, or None for a page where
            that XPath times out — which is what production does on every invite.

    Returns:
        A `_Result` carrying the return value and each patched seam.
    """
    from cqc_lem.app.engagement import invites

    def click(driver, wait, xpath, label, **kwargs):
        if xpath == _TEXTAREA_XPATH and xpath_box is not None:
            return xpath_box
        raise TimeoutException(label)

    clicker = MagicMock(side_effect=click)
    finder = MagicMock(return_value=_control("Add a note") if light_dom_button else None)
    with patch(f"{_INV}.find_deep_elements", side_effect=deep), \
         patch(f"{_INV}.click_element_wait_retry", clicker), \
         patch(f"{_INV}.find_first", finder), \
         patch(f"{_INV}.get_ai_message_refinement", return_value="short note"), \
         patch(f"{_INV}.time.sleep"), \
         patch(f"{_INV}.log_warning") as warn, \
         patch(f"{_INV}.log_debug") as debug, \
         patch(f"{_INV}.log_info") as info:
        attached = invites._add_connect_note(MagicMock(), MagicMock(), message, 1)
    return _Result(attached=attached, warn=warn, debug=debug, info=info, click=clicker,
                   find_first=finder, deep=deep)


def _messages(logger) -> list:
    return [call.args[0] for call in logger.call_args_list]


def _clicked_xpaths(clicker) -> list:
    return [call.args[2] for call in clicker.call_args_list]


class TestTheNoteFieldIsReachedInTheShadowRoot:

    def test_a_shadow_mounted_textarea_is_typed_into(self):
        """The production case: the field exists, only a container-scoped CSS query can see it."""
        box = MagicMock()
        r = _add_note(_shadow_dialog(box))

        assert r.attached is True
        box.send_keys.assert_called_once_with("hi jane")
        r.warn.assert_not_called()

    def test_a_field_that_renders_a_beat_after_the_click_is_waited_for(self):
        """A field that renders a beat late is waited for.

        `find_deep_elements` is one JS pass with no wait of its own, and the note field is asked
        for by the very click that opens it — so the first query can arrive before it exists.
        """
        box = MagicMock()
        r = _add_note(_shadow_dialog(box, renders_after=2))

        assert r.attached is True
        box.send_keys.assert_called_once_with("hi jane")

    def test_a_rotated_id_is_still_found_inside_the_dialog(self):
        """A rotated id is still found inside the dialog.

        `custom-message` is a LinkedIn id, not a contract. Inside the open invite dialog the one
        text field can only be the note, which is what makes the broad selector safe THERE.
        """
        box = MagicMock()
        r = _add_note(_shadow_dialog(box, field_css=_BROAD_CSS))

        assert r.attached is True
        box.send_keys.assert_called_once_with("hi jane")

    def test_the_element_that_answered_is_never_re_found_by_xpath(self):
        """The element the deep query answered with is the one typed into.

        #1733, and the trap that produced this bug: a shadow-mounted field cannot be re-located by
        an XPath that never saw it, so the deep query's own handle is what gets used.
        """
        box = MagicMock()
        r = _add_note(_shadow_dialog(box))

        assert _TEXTAREA_XPATH not in _clicked_xpaths(r.click)
        box.click.assert_called_once()
        box.send_keys.assert_called_once()

    def test_the_ordinary_shadow_dialog_no_longer_warns(self):
        """AC4: the ordinary shadow dialog no longer warns.

        `Could not attach a note` fired on EVERY invite, and a repeated `log_warning` re-emits at
        ERROR and files one grouped `$exception` — so a 100%-reproducible code defect was arriving
        as a recurring production incident.
        """
        r = _add_note(_shadow_dialog(MagicMock()))

        r.warn.assert_not_called()
        assert any("Added note" in message for message in _messages(r.info))

    def test_the_note_is_stripped_of_non_bmp_characters_on_the_shadow_path(self):
        """ChromeDriver's send_keys raises on the emoji an AI-written note routinely carries."""
        box = MagicMock()
        _add_note(_shadow_dialog(box), message="hi jane \U0001F600")

        typed = box.send_keys.call_args.args[0]
        assert "\U0001F600" not in typed and typed.startswith("hi jane")


class TestTheBroadSelectorNeverLeavesTheDialog:

    def test_a_textarea_outside_the_dialog_is_never_typed_into(self):
        """A textarea outside the dialog is never typed into.

        #1012 in its most expensive form: the broad selector is unambiguous only INSIDE the
        dialog. Document-wide it would put a personalised note into some other composer.
        """
        stray = MagicMock()
        r = _add_note(_shadow_dialog(outside={_BROAD_CSS: [stray]}))

        stray.send_keys.assert_not_called()
        assert r.attached is False
        assert _BROAD_CSS not in [css for css, root in r.deep.queries if root is None]

    def test_the_exact_id_is_still_read_document_wide(self):
        """The exact id is still read document-wide.

        A rotation that drops the dialog role must not lose the field — the id is unambiguous
        anywhere, so only the broad selector is confined to the container.
        """
        box = MagicMock()
        r = _add_note(_shadow_dialog(outside={_NOTE_CSS: [box]}, has_container=False),
                      light_dom_button=True)

        assert r.attached is True
        box.send_keys.assert_called_once_with("hi jane")


class TestTheOtherTwoDialogsAreUnchanged:

    def test_a_light_dom_textarea_behaves_exactly_as_before(self):
        """AC2. The deep query answers a light-DOM field too, so no rotation loses the note."""
        box = MagicMock()
        r = _add_note(_shadow_dialog(outside={_NOTE_CSS: [box]}), light_dom_button=True)

        assert r.attached is True
        box.clear.assert_called_once()
        box.send_keys.assert_called_once_with("hi jane")
        r.warn.assert_not_called()

    def test_a_dialog_with_no_note_affordance_stays_a_debug_no_op(self):
        """AC3 / #1039: a dialog with no note affordance stays a DEBUG no-op.

        LinkedIn hides the note once the personalized-invite quota is spent; that is working
        behaviour, graded against the bare-send control the dialog still shows.
        """
        r = _add_note(_shadow_dialog(controls=("Send without a note",)))

        assert r.attached is False
        r.warn.assert_not_called()
        assert any("quota" in message for message in _messages(r.debug))
        assert r.deep.field_queries() == []  # never even looked for the field

    def test_an_affordance_that_answers_but_no_reachable_field_still_warns(self):
        """An affordance that answers with no reachable field still warns.

        The backstop stays a WARNING with `exc=`: the affordance was there and the field was not,
        which is drift or a broken dialog — not the quota no-op, and not something to hide.
        """
        r = _add_note(_shadow_dialog())

        assert r.attached is False
        r.warn.assert_called_once()
        assert isinstance(r.warn.call_args.kwargs.get("exc"), Exception)

    def test_a_field_that_will_not_clear_is_still_typed_into(self):
        """A field that will not clear is still typed into.

        A `contenteditable` note field is not `clear()`-able and opens empty anyway — losing the
        note over that would trade one silent noteless invite for another.
        """
        box = MagicMock()
        box.clear.side_effect = Exception("Element must be user-editable in order to clear it")
        r = _add_note(_shadow_dialog(box, field_css=_BROAD_CSS))

        assert r.attached is True
        box.send_keys.assert_called_once_with("hi jane")


class TestTheInviteGoesOutCarryingTheNote:

    def test_a_fully_shadow_mounted_dialog_sends_a_personalised_invite(self):
        """AC1, end to end: the invite goes out carrying its note.

        The same dialog the production log described — controls and note field both in the shadow
        root, every XPath timing out — now sends WITH its note.
        """
        from cqc_lem.app.engagement import invites
        from cqc_lem.utilities.db import CONNECTION_REQUEST_SENT_MESSAGE

        box = MagicMock()
        deep = _shadow_dialog(box)

        def click(driver, wait, xpath, label, **kwargs):
            raise TimeoutException(label)  # no light-DOM XPath resolves in this dialog

        with patch(f"{_INV}.find_deep_elements", side_effect=deep), \
             patch(f"{_INV}.get_user_password_pair_by_id", return_value=("e@x", "pw")), \
             patch(f"{_INV}.get_driver_wait_pair",
                   return_value=(_profile_driver(), MagicMock())), \
             patch(f"{_INV}.login_to_linkedin"), \
             patch(f"{_INV}._profile_is_first_degree", return_value=False), \
             patch(f"{_INV}._open_connect_invite_dialog", return_value=(True, None)), \
             patch(f"{_INV}.click_element_wait_retry", MagicMock(side_effect=click)), \
             patch(f"{_INV}.find_first", return_value=None), \
             patch(f"{_INV}.click_first", return_value=None), \
             patch(f"{_INV}.get_ai_message_refinement", return_value="short note"), \
             patch(f"{_INV}.time.sleep"), \
             patch(f"{_INV}.insert_new_log"), \
             patch(f"{_INV}.record_action"), \
             patch(f"{_INV}.quit_gracefully"), \
             patch(f"{_INV}.log_error") as log_error, \
             patch(f"{_INV}.log_warning") as log_warning:
            sent, reason = invites.invite_to_connect_now(1, "https://x/in/jane", "hi jane")

        assert sent is True and reason == CONNECTION_REQUEST_SENT_MESSAGE
        box.send_keys.assert_called_once_with("hi jane")
        log_warning.assert_not_called()
        log_error.assert_not_called()

    def test_a_dialog_that_takes_the_click_and_shows_no_outcome_is_not_a_send(self):
        """The #1867 shape, through the same fake dialog.

        `sends=False` is a Send control that accepts the click and changes nothing — LinkedIn's
        email-verification challenge, which offers `Send without a note` and then demands an
        address. The click lands, the note is typed, and the row must still not be 'sent'.
        """
        from cqc_lem.app.engagement import invites
        from cqc_lem.utilities.db import INVITE_UNCONFIRMED_MESSAGE

        box = MagicMock()
        deep = _shadow_dialog(box, sends=False)

        def click(driver, wait, xpath, label, **kwargs):
            raise TimeoutException(label)

        with patch(f"{_INV}.find_deep_elements", side_effect=deep), \
             patch(f"{_INV}.get_user_password_pair_by_id", return_value=("e@x", "pw")), \
             patch(f"{_INV}.get_driver_wait_pair",
                   return_value=(_profile_driver(), MagicMock())), \
             patch(f"{_INV}.login_to_linkedin"), \
             patch(f"{_INV}._profile_is_first_degree", return_value=False), \
             patch(f"{_INV}._open_connect_invite_dialog", return_value=(True, None)), \
             patch(f"{_INV}.click_element_wait_retry", MagicMock(side_effect=click)), \
             patch(f"{_INV}.find_first", return_value=None), \
             patch(f"{_INV}.click_first", return_value=None), \
             patch(f"{_INV}.get_ai_message_refinement", return_value="short note"), \
             patch(f"{_INV}.time.sleep"), \
             patch(f"{_INV}.insert_new_log"), \
             patch(f"{_INV}.record_action") as record_action, \
             patch(f"{_INV}.quit_gracefully"), \
             patch(f"{_INV}.log_warning") as log_warning:
            sent, reason = invites.invite_to_connect_now(1, "https://x/in/jane", "hi jane")

        assert (sent, reason) == (False, INVITE_UNCONFIRMED_MESSAGE)
        box.send_keys.assert_called_once_with("hi jane")
        # The envelope IS charged: we clicked Send and LinkedIn may have counted it, so pacing
        # under the true figure is the direction that gets accounts restricted (#1867). The ROW
        # fails closed; the ENVELOPE fails open. Different questions, different postures.
        record_action.assert_called_once()
        log_warning.assert_called_once()  # the ONE anomaly log, from the confirmation step


class TestTheFakeDomTracksTheModule:

    def test_the_fake_dom_speaks_the_modules_own_selectors(self):
        """These tests are only evidence while the fake answers the queries the code makes."""
        from cqc_lem.app.engagement import invites

        assert invites._CONNECT_DIALOG_CONTAINER_CSS == _DIALOG_CSS
        assert invites._CONNECT_DIALOG_CONTROL_CSS == _CONTROL_CSS
        assert invites._CONNECT_NOTE_INPUT_CSS == _NOTE_CSS
        assert invites._CONNECT_NOTE_INPUT_FALLBACK_CSS == _BROAD_CSS
        assert invites._CONNECT_NOTE_TEXTAREA_XPATH == _TEXTAREA_XPATH
        assert invites._PROFILE_TOP_CARD_CSS == _TOP_CARD_CSS
        assert invites._PROFILE_NAME_HEADING_CSS == _HEADING_CSS
        assert invites._PROFILE_TOP_CARD_CONTROL_CSS == _CONTROL_CSS
