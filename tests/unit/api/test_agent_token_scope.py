"""The `agent` scope: what a headless token may reach, and the one thing it may never do.

Issue #1011. A machine cannot run a passkey ceremony and cannot read a mailbox, so it holds a
credential minted once by a human. Two claims make that safe, and both are asserted here rather
than in prose:

1. The token reaches the QUEUEING surface and nothing else — no credential path, no account mover,
   no session revocation, and not the minting endpoints themselves (a stolen agent token must not
   be able to mint its successor).
2. It can create pending work but can never APPROVE it. That cannot be a path list, because saving
   a draft and approving one are the same PUT — so it lives on the `action` field, server-side.
"""

from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture(scope="module")
def main_mod():
    from cqc_lem.api import main
    return main


class TestAgentSurface:
    def test_the_queue_reads_and_creates_are_reachable(self, main_mod):
        for path in ("/connection_requests", "/outreach/targets", "/dms", "/lead_signals",
                     "/leads", "/user/engagement-preferences", "/user/automation-status",
                     "/connection_request", "/outreach/target", "/schedule_dm", "/lead_signal"):
            assert main_mod._scope_allows(main_mod.SESSION_SCOPE_AGENT, path) is True, path

    @pytest.mark.parametrize("path", [
        "/user/linkedin-cookie",        # the extension's one write — not this token's
        "/user/linkedin-password",
        "/user/sessions/revoke",        # must not be able to lock the owner out
        "/user/email/change/init",      # must not be able to move the account
        "/user/passkeys/register/begin",
        "/user/totp/enroll/begin",
        "/user/recovery-codes/regenerate",
        "/user/security",
        "/user/extension-token",
        "/user/agent-token",            # must not be able to mint its own successor
    ])
    def test_everything_that_would_widen_the_blast_radius_is_refused(self, main_mod, path):
        assert main_mod._scope_allows(main_mod.SESSION_SCOPE_AGENT, path) is False

    def test_the_scope_is_not_unrestricted(self, main_mod):
        assert main_mod.SESSION_SCOPE_AGENT not in main_mod._UNRESTRICTED_SCOPES

    def test_the_scope_is_registered_so_it_does_not_fail_closed_everywhere(self, main_mod):
        # _scope_allows fails closed on an UNKNOWN scope, so a surface that was never registered
        # would refuse every path — the token would look revoked rather than narrow.
        assert main_mod.SESSION_SCOPE_AGENT in main_mod._SCOPE_SURFACES
        assert main_mod.SESSION_SCOPE_AGENT in main_mod._SCOPE_REFUSAL_CODE


class TestAgentMayNotApprove:
    def test_approve_is_refused_for_an_agent_caller(self, main_mod):
        from fastapi import HTTPException
        token = main_mod._request_session_scope.set(main_mod.SESSION_SCOPE_AGENT)
        try:
            with pytest.raises(HTTPException) as exc:
                main_mod._refuse_agent_approval("approve")
            assert exc.value.status_code == 403
            assert exc.value.detail["code"] == "agent_may_not_approve"
        finally:
            main_mod._request_session_scope.reset(token)

    @pytest.mark.parametrize("action", [None, "cancel", "dismiss"])
    def test_the_agent_may_still_save_drafts_cancel_and_dismiss(self, main_mod, action):
        token = main_mod._request_session_scope.set(main_mod.SESSION_SCOPE_AGENT)
        try:
            main_mod._refuse_agent_approval(action)   # must not raise
        finally:
            main_mod._request_session_scope.reset(token)

    def test_a_human_session_may_still_approve(self, main_mod):
        token = main_mod._request_session_scope.set(main_mod.SESSION_SCOPE_FULL)
        try:
            main_mod._refuse_agent_approval("approve")   # must not raise
        finally:
            main_mod._request_session_scope.reset(token)

    def test_every_approve_capable_handler_calls_the_guard(self, main_mod):
        """The guard is only worth anything if no approve path skips it."""
        import inspect
        src = inspect.getsource(main_mod)
        # Each handler that maps an "approve" action must also refuse agent callers.
        assert src.count('action_map = {"approve"') == src.count("_refuse_agent_approval(request.action)")


class TestAgentTokenTTL:
    def test_ttl_hours_overrides_the_idle_window(self):
        """A weekly agent must not find its own token expired every run."""
        import inspect

        from cqc_lem.utilities import db
        sig = inspect.signature(db.create_session)
        assert "ttl_hours" in sig.parameters
        assert sig.parameters["ttl_hours"].default is None   # default behaviour unchanged
        src = inspect.getsource(db.create_session)
        assert "ttl_hours if ttl_hours is not None else SESSION_IDLE_HOURS" in src

    def test_the_mint_defaults_to_90_days_and_is_bounded(self, main_mod):
        f = main_mod.AgentTokenRequest.model_fields["ttl_days"]
        assert f.default == 90
        meta = str(f.metadata)
        assert "1" in meta and "365" in meta   # ge=1, le=365


class TestAgentTokenMint:
    def test_minting_requires_a_session(self, main_mod):
        from fastapi import HTTPException
        with patch.object(main_mod, "get_session_user_id", return_value=None):
            with pytest.raises(HTTPException) as exc:
                main_mod.mint_agent_token(main_mod.AgentTokenRequest(session_token="nope"))
            assert exc.value.status_code == 401

    def test_minting_is_step_up_gated_and_never_exempts_the_agent_scope(self, main_mod):
        """The ceremony happens once, with a human. An agent token must not mint its successor."""
        import inspect
        src = inspect.getsource(main_mod.mint_agent_token)
        assert "_require_step_up(" in src
        assert "extension_scope_ok" not in src
