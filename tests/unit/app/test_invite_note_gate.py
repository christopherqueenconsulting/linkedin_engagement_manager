"""The outbound gate on connect-invite notes, where a refusal must NOT cost the invite.

#1964 gated DMs and comments, where a refused body means there is nothing to send. A connect note
is different: it is an optional extra on an invite already decided, and #573 built the bare-send
path for exactly that case (LinkedIn hides the note affordance once a free account's
personalized-invite quota is spent). So the verdict here is "drop the note, keep the invite" — and
these tests exist mostly to hold that distinction in place, because copying the DM verdict would
throw away good invites over a bad garnish.
"""

from contextlib import contextmanager
from typing import Iterator
from unittest.mock import MagicMock, patch

import pytest

from cqc_lem.app.engagement.invites import invite_to_connect_now
from cqc_lem.utilities.db import InviteOutcome

INCIDENT_NOTE = ("To assist you effectively, I need the actual message history JSON to analyze the "
                 "conversation context. Please provide the message history so I can proceed.")

PLACEHOLDER_NOTE = "Congrats on the new role at [company], really well deserved."

GOOD_NOTE = "Saw your post on ingest backpressure — would like to follow your work."

PROFILE = "https://www.linkedin.com/in/someone/"


@contextmanager
def _invite_reaching_the_note() -> Iterator[tuple[MagicMock, MagicMock]]:
    """Drive `invite_to_connect_now` far enough that `_add_connect_note` WOULD be called.

    This matters more than it looks. Stopping at the Connect dialog makes `_add_connect_note`
    uncalled for EVERY input, so `assert not add_note.called` passes whether or not the gate does
    anything — a vacuous test. Opening the dialog is what makes the assertion mean something, and
    the paired clean-note case below is what proves it.

    The send step past `_add_connect_note` is patched too (rather than left to run against a bare
    `MagicMock` driver) so the tests exercise a fully controlled path instead of leaning on
    `MagicMock`'s permissiveness to not blow up.
    """
    with patch("cqc_lem.app.engagement.invites.get_user_password_pair_by_id",
               return_value=("e@x.com", "pw")), \
            patch("cqc_lem.app.engagement.invites.get_driver_wait_pair",
                  return_value=(MagicMock(), MagicMock())), \
            patch("cqc_lem.app.engagement.invites.login_to_linkedin"), \
            patch("cqc_lem.app.engagement.invites._open_connect_invite_dialog",
                  return_value=(True, None)), \
            patch("cqc_lem.app.engagement.invites._find_connect_dialog_email_input",
                  return_value=None), \
            patch("cqc_lem.app.engagement.invites._connect_dialog_wants_email",
                  return_value=False), \
            patch("cqc_lem.app.engagement.invites._add_connect_note",
                  return_value=True) as add_note, \
            patch("cqc_lem.app.engagement.invites._submit_connect_invite",
                  return_value=True), \
            patch("cqc_lem.app.engagement.invites._confirm_invite_outcome",
                  return_value=InviteOutcome.SENT), \
            patch("cqc_lem.app.engagement.invites.log_warning") as warn:
        yield add_note, warn


@pytest.mark.unit
class TestInviteNoteGate:
    @pytest.mark.parametrize("note", [INCIDENT_NOTE, PLACEHOLDER_NOTE])
    def test_a_refused_note_never_reaches_the_composer(self, note: str) -> None:
        with _invite_reaching_the_note() as (add_note, warn):
            invite_to_connect_now(1, PROFILE, note)

            assert not add_note.called
            dropped = [c for c in warn.call_args_list if "invite bare" in str(c)]
            assert dropped, "the note was dropped without saying so"
            assert dropped[0][1].get("action_type") == "invite_connect"

    @pytest.mark.parametrize("note", [INCIDENT_NOTE, PLACEHOLDER_NOTE])
    def test_the_invite_itself_is_not_abandoned(self, note: str) -> None:
        """A bad note costs the note, never the invite — the whole point of this gate's verdict."""
        with _invite_reaching_the_note() as (_, _warn), \
                patch("cqc_lem.app.engagement.invites._open_connect_invite_dialog",
                      return_value=(True, None)) as dialog:
            invite_to_connect_now(1, PROFILE, note)

            assert dialog.called, "the invite was abandoned over a bad note"

    def test_the_refusal_names_the_checks_that_fired(self) -> None:
        with _invite_reaching_the_note() as (_add, warn):
            invite_to_connect_now(1, PROFILE, INCIDENT_NOTE)

            dropped = [c for c in warn.call_args_list if "invite bare" in str(c)]
            assert "input_request" in dropped[0][0][0]

    def test_a_clean_note_still_reaches_the_composer(self) -> None:
        """The paired control: this is what makes the refusal assertions non-vacuous."""
        with _invite_reaching_the_note() as (add_note, warn):
            invite_to_connect_now(1, PROFILE, GOOD_NOTE)

            assert add_note.called, "a sendable note was dropped"
            assert add_note.call_args[0][2] == GOOD_NOTE
            assert not [c for c in warn.call_args_list if "invite bare" in str(c)]

    def test_no_note_at_all_is_unchanged(self) -> None:
        """A bare invite was always legal — the gate must not warn about one."""
        with _invite_reaching_the_note() as (add_note, warn):
            invite_to_connect_now(1, PROFILE, None)

            assert not add_note.called
            assert not [c for c in warn.call_args_list if "invite bare" in str(c)]

    def test_empty_string_note_is_unchanged(self) -> None:
        """`if message:` already treats "" like None — pin that so it stays a deliberate no-op."""
        with _invite_reaching_the_note() as (add_note, warn):
            invite_to_connect_now(1, PROFILE, "")

            assert not add_note.called
            assert not [c for c in warn.call_args_list if "invite bare" in str(c)]
