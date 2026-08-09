"""The `agent` scope: what a headless token may reach, and the one thing it may never do.

Issue #1026. A machine cannot run a passkey ceremony and cannot read a mailbox, so it holds a
credential minted once by a human. Two claims make that safe, and both are asserted here rather
than in prose:

1. The token reaches the QUEUEING surface and nothing else — no credential path, no account mover,
   no session revocation, and not the minting endpoints themselves (a stolen agent token must not
   be able to mint its successor).
2. It can create pending work but can never APPROVE it. That cannot be a path list, because saving
   a draft and approving one are the same PUT — so it lives on the fields, server-side, in THREE
   halves: the `action` a PUT names, the `status` a create names, and the account default that
   names nothing at all.
"""

from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture(scope="module")
def main_mod():
    from cqc_lem.api import main
    return main


_QUEUE_PATHS = ("/connection_requests", "/outreach/targets", "/dms", "/lead_signals", "/leads",
                "/catchup/touches", "/user/engagement-preferences", "/user/automation-status",
                "/dashboard/stats/", "/connection_request", "/outreach/target", "/schedule_dm",
                "/lead_signal", "/lead")


class TestAgentSurface:
    @pytest.mark.parametrize("path", _QUEUE_PATHS)
    def test_the_queue_reads_and_creates_are_reachable(self, main_mod, path):
        """Resolve through _scope_path, the way a real request does.

        Asserting against the raw frozenset would pass on an entry that no live request can ever
        match: _scope_path strips the /api prefix AND rstrips trailing slashes, so a surface entry
        written as "/dashboard/stats/" is dead on arrival. That is not hypothetical — it was the
        first version of this surface.
        """
        key = main_mod._scope_path("/api" + path)
        assert main_mod._scope_allows(main_mod.SESSION_SCOPE_AGENT, key) is True, f"{path} -> {key}"

    @pytest.mark.parametrize("path", _QUEUE_PATHS)
    def test_every_surface_entry_survives_path_normalisation(self, main_mod, path):
        """No entry may be unreachable because of how it was spelled."""
        assert main_mod._scope_path("/api" + path) in main_mod._AGENT_SESSION_SURFACE

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
        # Not a credential — capacity. One press opens a Chrome session out of the fixed pool the
        # engagement lanes are sized against, so a headless token must not be able to draw on it
        # (issue #1076).
        "/user/linkedin-profile/refresh",
    ])
    def test_everything_that_would_widen_the_blast_radius_is_refused(self, main_mod, path):
        key = main_mod._scope_path("/api" + path)
        assert main_mod._scope_allows(main_mod.SESSION_SCOPE_AGENT, key) is False

    @pytest.mark.parametrize("path", [
        "/lead_signals/admin",      # extends /lead_signals
        "/leads/export",            # extends /leads
        "/lead_magnets",            # extends /lead — no separator at all
        "/dms/all",                 # extends /dms
        "/connection_requests/bulk-approve",
        "/user/engagement-preferences/reset",
        "/dashboard/stats/internal",
    ])
    def test_a_granted_path_never_opens_the_paths_beneath_or_beside_it(self, main_mod, path):
        """Matching is exact set membership, never a prefix.

        The surface is a list of path literals, so the failure mode is a route that merely STARTS
        with a granted one inheriting the grant — `/lead` opening `/lead_magnets`, `/lead_signals`
        opening `/lead_signals/admin`. The trailing-slash bug this suite already covers proves this
        class of mistake is real rather than theoretical.
        """
        key = main_mod._scope_path("/api" + path)
        assert main_mod._scope_allows(main_mod.SESSION_SCOPE_AGENT, key) is False, key

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


class TestAgentMayNotApproveAtCreateTime:
    """The half an `action` guard cannot see.

    Every create endpoint on the queueing surface takes a `status` and inserts APPROVED when it
    reads "approved" — no `action` field anywhere. Guarding only the PUT left a POST /schedule_dm
    with status="approved" landing a row `auto_check_scheduled_dms` then SENDS to a real person.
    """

    @pytest.fixture
    def as_agent(self, main_mod):
        token = main_mod._request_session_scope.set(main_mod.SESSION_SCOPE_AGENT)
        yield
        main_mod._request_session_scope.reset(token)

    def test_an_agent_asking_for_an_approved_row_is_refused(self, main_mod, as_agent):
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc:
            main_mod._refuse_agent_approved_status("approved")
        assert exc.value.status_code == 403
        assert exc.value.detail["code"] == "agent_may_not_approve"

    @pytest.mark.parametrize("status", [None, "pending"])
    def test_the_agent_may_still_create_pending_work(self, main_mod, as_agent, status):
        main_mod._refuse_agent_approved_status(status)   # must not raise

    def test_a_human_session_may_still_create_approved_work(self, main_mod):
        token = main_mod._request_session_scope.set(main_mod.SESSION_SCOPE_FULL)
        try:
            main_mod._refuse_agent_approved_status("approved")   # must not raise
        finally:
            main_mod._request_session_scope.reset(token)

    @pytest.mark.parametrize("handler", ["schedule_dm_endpoint", "create_outreach_target_endpoint",
                                         "create_connection_request_endpoint"])
    def test_every_create_handler_that_takes_a_status_calls_the_guard(self, main_mod, handler):
        """A create route that reaches APPROVED without the guard is the whole bug again.

        Resolved off the ROUTE TABLE rather than `getattr(main_mod, ...)`. A handler that moves to a
        per-area router (#1154) is still the thing serving the route, and looking it up by module
        attribute would raise `AttributeError` on the move — which reads like a deleted guard rather
        than a relocated one, and invites deleting the parameter to make it pass.
        """
        import inspect

        endpoints = {getattr(r.endpoint, "__name__", None): r.endpoint
                     for r in main_mod._walk_routes(main_mod.app.routes)}
        assert handler in endpoints, f"{handler} serves no route at all"
        src = inspect.getsource(endpoints[handler])
        assert "_refuse_agent_approved_status(request.status)" in src, handler

    def test_the_account_auto_approve_default_does_not_approve_for_an_agent(self, main_mod, as_agent):
        """The PASSIVE bypass, and the one with no field to refuse on.

        `connection_request_mode` is `auto_approve` out of the box, so an agent naming no status at
        all would land an APPROVED row on nearly every account — the guarantee broken by doing
        nothing at all.
        """
        from cqc_lem.utilities.db import ConnectionRequestStatus
        with patch.object(main_mod, "get_session_user_id", return_value=7), \
             patch.object(main_mod, "get_engagement_preferences",
                          return_value={"connection_request_mode": "auto_approve"}), \
             patch.object(main_mod, "insert_connection_request", return_value=99) as insert:
            main_mod.create_connection_request_endpoint(
                main_mod.ConnectionRequestCreate(session_token="t",
                                                 recipient_profile_url="https://example.com/in/x"))
        assert insert.call_args.kwargs.get("status") == ConnectionRequestStatus.PENDING

    def test_the_same_default_still_auto_approves_for_a_human(self, main_mod):
        from cqc_lem.utilities.db import ConnectionRequestStatus
        token = main_mod._request_session_scope.set(main_mod.SESSION_SCOPE_FULL)
        try:
            with patch.object(main_mod, "get_session_user_id", return_value=7), \
                 patch.object(main_mod, "get_engagement_preferences",
                              return_value={"connection_request_mode": "auto_approve"}), \
                 patch.object(main_mod, "insert_connection_request", return_value=99) as insert:
                main_mod.create_connection_request_endpoint(
                    main_mod.ConnectionRequestCreate(session_token="t",
                                                     recipient_profile_url="https://example.com/in/x"))
            assert insert.call_args.kwargs.get("status") == ConnectionRequestStatus.APPROVED
        finally:
            main_mod._request_session_scope.reset(token)


class TestAgentMayNotConfigure:
    """The scope surface matches on PATH, so the entry added so the agent could READ whether
    automation is safe to queue granted the PUT along with it — and that write sets the approval
    modes and the per-day caps, i.e. it re-opens everything else.
    """

    def test_an_agent_cannot_write_engagement_preferences(self, main_mod):
        from fastapi import HTTPException
        token = main_mod._request_session_scope.set(main_mod.SESSION_SCOPE_AGENT)
        try:
            with patch.object(main_mod, "get_session_user_id", return_value=7), \
                 patch.object(main_mod, "update_engagement_preferences") as upd:
                with pytest.raises(HTTPException) as exc:
                    main_mod.update_engagement_preferences_endpoint(
                        main_mod.EngagementPreferencesRequest(session_token="t"))
            assert exc.value.status_code == 403
            assert exc.value.detail["code"] == "agent_may_not_configure"
            upd.assert_not_called()
        finally:
            main_mod._request_session_scope.reset(token)

    def test_a_human_session_may_still_write_them(self, main_mod):
        token = main_mod._request_session_scope.set(main_mod.SESSION_SCOPE_FULL)
        try:
            with patch.object(main_mod, "get_session_user_id", return_value=7), \
                 patch.object(main_mod, "update_engagement_preferences", return_value=True) as upd:
                main_mod.update_engagement_preferences_endpoint(
                    main_mod.EngagementPreferencesRequest(session_token="t"))
            upd.assert_called_once()
        finally:
            main_mod._request_session_scope.reset(token)


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

    @staticmethod
    def _resolve_with_scope(scope):
        """Drive db.resolve_session over a mocked row and return the UPDATE it issued."""
        from datetime import datetime, timezone
        from unittest.mock import MagicMock

        from cqc_lem.utilities import db

        conn, cursor = MagicMock(), MagicMock()
        conn.cursor.return_value = cursor
        cursor.fetchone.return_value = {
            "user_id": 7, "scope": scope,
            "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
        }
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            resolved = db.resolve_session("a" * 64)
        updates = [c for c in cursor.execute.call_args_list if "UPDATE" in c[0][0].upper()]
        assert len(updates) == 1
        return resolved, updates[0][0][0]

    def test_an_agent_session_expiry_is_never_slid(self):
        """The bug the `ttl_hours` parameter alone did NOT fix.

        resolve_session slides EVERY session to now + SESSION_IDLE_HOURS, so a 90-day agent token
        was rewritten to 24 hours on its FIRST request — the weekly agent still dead every run,
        exactly the failure the parameter exists to prevent.
        """
        from cqc_lem.utilities.db import SESSION_SCOPE_AGENT

        resolved, sql = self._resolve_with_scope(SESSION_SCOPE_AGENT)
        assert resolved == {"user_id": 7, "scope": SESSION_SCOPE_AGENT}
        assert "expires_at" not in sql, "an agent session's granted TTL must survive being used"
        assert "last_seen_at" in sql, "it is still a session that reports when it was last seen"

    def test_a_browser_session_still_slides(self):
        """The fix must not turn every other session into a fixed-expiry one."""
        from cqc_lem.utilities.db import SESSION_SCOPE_FULL

        _, sql = self._resolve_with_scope(SESSION_SCOPE_FULL)
        assert "expires_at" in sql


class TestAgentTokenMint:
    def test_minting_requires_a_session(self, main_mod):
        from fastapi import HTTPException
        with patch.object(main_mod, "get_session_user_id", return_value=None):
            with pytest.raises(HTTPException) as exc:
                main_mod.mint_agent_token(main_mod.AgentTokenRequest(session_token="nope"))
            assert exc.value.status_code == 401

    def test_minting_is_step_up_gated_and_never_exempts_the_agent_scope(self, main_mod):
        """The ceremony happens once, with a human. An agent token must not mint its successor.

        Read off the AST rather than the source TEXT. A substring check answers "does this string
        appear anywhere in the function", which a comment DOCUMENTING the exemption satisfies just
        as well as an argument granting it — so the text version failed on prose while the code was
        correct, and would equally have passed had the exemption been passed under a comment. The
        call is what grants the exemption, so the call is what gets asserted on.
        """
        import ast
        import inspect
        import textwrap

        tree = ast.parse(textwrap.dedent(inspect.getsource(main_mod.mint_agent_token)))
        calls = [n for n in ast.walk(tree)
                 if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "_require_step_up"]
        assert len(calls) == 1, "minting must be step-up gated exactly once"
        call = calls[0]
        assert not any(kw.arg == "extension_scope_ok" for kw in call.keywords)
        # `extension_scope_ok` is the 4th positional parameter — passing it positionally would grant
        # the same exemption without ever naming it.
        assert len(call.args) <= 3, "no positional argument may reach extension_scope_ok"
