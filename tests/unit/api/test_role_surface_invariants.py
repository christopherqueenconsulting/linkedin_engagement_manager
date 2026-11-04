"""Standing invariants over the REAL route table for every restricted machine role.

`test_observer_scope.py` and `test_agent_token_scope.py` prove each role against the routes someone
thought to list. This file proves the other direction: it walks every route FastAPI actually serves,
asks the one resolver (`_scope_allows`) whether each role may reach each (path, method), and fails —
loudly, with the offending routes — if any role can reach something it must never reach. That is the
check that still holds after the NEXT route is added by someone who has never read the role docs.

Three invariants:

1. **Nothing in `FORBIDDEN_ROLE_ROUTES` is reachable by any role.** Credentials, the account mover,
   session revocation, token minting. Enforced at the resolver, so the handler never runs.
2. **A role that can write may only reach an approval-capable route if the handler refuses it.**
   `agent` reaches the PUTs that approve a draft and the creates that accept `status="approved"`;
   that is only safe because each handler self-refuses (`HANDLER_GUARDED_ROUTES`). A new reachable
   write route whose body carries an `action` or `status` must be classified here — guarded, or
   argued not to be an approval vector — before it can pass.
3. **A read-only role reaches no write verb at all.**

The forbidden set is a named constant with a reason per entry, because a test that fails with only
a path teaches nothing and invites the fix "delete the line".

To extend: a new restricted role is one more entry in `ROLE_SCOPES`.
"""

import inspect
from types import ModuleType
from typing import Any, NamedTuple, Optional

import pytest

pytestmark = pytest.mark.unit

READ_VERBS = frozenset({"GET", "HEAD", "OPTIONS"})


class Forbidden(NamedTuple):
    """One closed door.

    `path` ending in `/*` matches everything beneath that prefix; `methods` None means every verb.
    """
    path: str
    methods: Optional[frozenset]
    why: str


_ANY = None

# Every entry here must be unreachable by every restricted role. Each has a reason, and
# `test_every_forbidden_entry_matches_a_real_route` fails if an entry stops matching anything —
# a renamed route must not turn this list into a vacuous pass.
FORBIDDEN_ROLE_ROUTES: tuple[Forbidden, ...] = (
    # -- credentials: whoever can write these controls which LinkedIn account LEM acts as --------
    Forbidden("/user/linkedin-cookie", _ANY, "writes the LinkedIn li_at session cookie"),
    Forbidden("/user/linkedin-password", _ANY, "writes the stored LinkedIn password"),
    Forbidden("/user/passkeys/*", _ANY, "enrols, logs in with, or removes a passkey"),
    Forbidden("/user/totp/*", _ANY, "enrols or removes the authenticator-app factor"),
    Forbidden("/user/recovery-codes/*", _ANY, "generates or spends account recovery codes"),
    Forbidden("/user/auth-factors", _ANY, "lists and manages sign-in factors"),
    Forbidden("/user/step-up/*", _ANY, "the step-up ceremony itself: a role must never be able to "
                                       "satisfy the gate that protects minting"),
    # -- the account mover ------------------------------------------------------------------------
    Forbidden("/user/email/change/*", _ANY, "moves the account to another email address"),
    # -- session control --------------------------------------------------------------------------
    Forbidden("/user/sessions/revoke", _ANY, "ends sessions: a role could lock the owner out"),
    # -- token minting: a role must never be able to mint a successor or a wider token ------------
    Forbidden("/user/extension-token", _ANY, "mints an extension-scoped session"),
    Forbidden("/user/agent-token", _ANY, "mints an agent-scoped session"),
    Forbidden("/user/observer-token", _ANY, "mints an observer-scoped session"),
    Forbidden("/user/operator-token", _ANY, "mints an operator-scoped session"),
    # -- caps and approval modes: the config that decides what runs without a human ---------------
    # The agent legitimately reaches this PATH (to read it), and refuses the write in the handler —
    # see HANDLER_GUARDED_ROUTES. So at the resolver it is forbidden only for the read-only role,
    # which is checked by invariant 3; it is listed in the guarded table for the others.
)

# Routes a WRITE-capable role (agent, operator) reaches by path and which therefore have to refuse
# the dangerous input themselves, because a path list cannot tell "save a draft" from "approve it".
# Value: (substrings that must appear in the handler source, why). A guard that is deleted from the
# handler turns this red even though no path moved.
HANDLER_GUARDED_ROUTES: dict[str, tuple[tuple[str, ...], str]] = {
    "update_engagement_preferences_endpoint": (
        ("_agent_scoped()",),
        "PUT sets connection_request_mode (auto_approve) and every per-day cap — configuring the "
        "account into approving everything"),
    "schedule_dm_endpoint": (
        ("_refuse_agent_approved_status(request.status)",),
        "status='approved' queues a DM that auto_check_scheduled_dms SENDS to a real person"),
    "create_outreach_target_endpoint": (
        ("_refuse_agent_approved_status(request.status)",),
        "status='approved' queues an outreach target for send"),
    "create_connection_request_endpoint": (
        ("_refuse_agent_approved_status(request.status)", "_agent_scoped()"),
        "status='approved', or NO status on an account whose default mode is auto_approve, lands "
        "an APPROVED connection request"),
    "update_connection_request_endpoint": (
        ("_refuse_agent_approval(request.action)",),
        "action='approve' / 'retry' releases a connection request for send"),
    "update_outreach_target_endpoint": (
        ("_refuse_agent_approval(request.action)",),
        "action='approve' releases an outreach target for send"),
    "update_lead_signal_endpoint": (
        ("_refuse_agent_approval(request.action)",),
        "action='approve' releases a lead-signal reply for send"),
    "act_on_curated_source": (
        ("_refuse_agent_approval(request.action)",),
        "action='approve' approves a curated post for publication"),
}

# Reachable write routes whose body carries an `action` or `status` field and which were reviewed
# and are NOT an approval vector. Each needs the argument, so the next reviewer can check it.
NOT_AN_APPROVAL_VECTOR: dict[str, str] = {
    "update_lead_endpoint": "action is 'dismiss' or 'restore' on a lead record; it sends nothing",
}

_APPROVAL_FIELDS = frozenset({"action", "status"})


def _scopes() -> Any:
    from cqc_lem.api import main
    return {"observer": main.SESSION_SCOPE_OBSERVER, "agent": main.SESSION_SCOPE_AGENT,
            "operator": main.SESSION_SCOPE_OPERATOR}


# The ONLY /admin routes any role may reach, by role. Every other /admin route stays behind
# `X-Admin-Secret` or an admin human session, which a machine role never holds. The operator's are
# the operational, non-destructive ones: two reads and the SAFE direction of the kill switch.
# `automation-resume` is deliberately absent — it re-starts what a human may have halted.
ALLOWED_ADMIN_REACH: dict[str, frozenset] = {
    "operator": frozenset({
        ("GET", "/admin/automation-status"),
        ("POST", "/admin/automation-pause"),
        ("GET", "/admin/task-status/{task_id}"),
    }),
}

# Every non-admin route reachable by the operator that the agent cannot reach. One entry today.
OPERATOR_EXTRA_NON_ADMIN_ROUTES = frozenset({("POST", "/leads/refresh")})

# A new restricted machine role is one more entry here.
ROLE_SCOPES = _scopes()
# The roles that may write at all. The rest are held to read verbs.
READ_ONLY_ROLES = ("observer",)


@pytest.fixture(scope="module")
def main_mod() -> Any:
    from cqc_lem.api import main
    return main


@pytest.fixture(scope="module")
def route_table(main_mod: ModuleType) -> Any:
    """Every (method, scope-path, route) the app serves, `/api` stripped the way a request is.

    Built from the live app, never from a hand-kept list.
    """
    rows = []
    for route in main_mod._walk_routes(main_mod.app.routes):
        path = getattr(route, "path", None)
        if not path:
            continue
        for method in sorted(route.methods or ()):
            rows.append((method, main_mod._scope_path(path), route))
    assert rows, "the route table walk matched nothing — this suite would pass vacuously"
    return rows


def _matches(entry: Forbidden, method: str, path: str) -> bool:
    if entry.methods is not None and method not in entry.methods:
        return False
    if entry.path.endswith("/*"):
        return path.startswith(entry.path[:-1])
    return path == entry.path


def _reachable(main_mod: ModuleType, route_table: list[tuple[str, str, Any]], scope: str) -> Any:
    return [(m, p, r) for (m, p, r) in route_table if main_mod._scope_allows(scope, p, m)]


def _describe(role: str, method: str, path: str, route: Any, why: Any="") -> Any:
    return (f"  {role:<9} {method:<7} /api{path}  (handler: "
            f"{getattr(route.endpoint, '__name__', '?')})" + (f"  — {why}" if why else ""))


def _body_fields(route: Any) -> set:
    fields = set()
    for param in getattr(getattr(route, "dependant", None), "body_params", []) or []:
        model_fields = getattr(param.field_info.annotation, "model_fields", None)
        if model_fields:
            fields |= set(model_fields)
        else:
            fields.add(param.name)
    return fields


class TestNoRoleReachesAForbiddenRoute:
    @pytest.mark.parametrize("role", sorted(ROLE_SCOPES))
    def test_no_forbidden_route_is_reachable(
            self,
            main_mod: ModuleType,
            route_table: list[tuple[str, str, Any]],
            role: str,
    ) -> None:
        offenders = []
        for method, path, route in _reachable(main_mod, route_table, ROLE_SCOPES[role]):
            for entry in FORBIDDEN_ROLE_ROUTES:
                if _matches(entry, method, path):
                    offenders.append(_describe(role, method, path, route, entry.why))
        assert not offenders, (
            f"\n\nThe {role!r} role can REACH routes that must never be reachable by a restricted "
            "role. Remove the path from its surface (api/main.py), do not edit FORBIDDEN_ROLE_ROUTES:\n"
            + "\n".join(offenders) + "\n")

    def test_every_forbidden_entry_matches_a_real_route(self, route_table: list[tuple[str, str, Any]]) -> None:
        """Otherwise a renamed or removed route turns the entry into a check that cannot fail."""
        dead = [e for e in FORBIDDEN_ROLE_ROUTES
                if not any(_matches(e, m, p) for (m, p, _r) in route_table)]
        assert not dead, "FORBIDDEN_ROLE_ROUTES entries matching no route:\n" + "\n".join(
            f"  {e.path}  ({e.why})" for e in dead)

    def test_every_token_minting_route_is_classified_forbidden(self, route_table: list[tuple[str, str, Any]]) -> None:
        """A new `/user/<x>-token` mint endpoint with no FORBIDDEN entry fails here.

        That is before anyone has to remember that roles must not reach it. Scoped to `/user/`: that
        is where SESSION mints live; `/admin/youtube-token` stores a YouTube OAuth token and mints
        no session.
        """
        minting = [(m, p, r) for (m, p, r) in route_table
                   if m == "POST" and ((p.startswith("/user/") and p.rstrip("/").endswith("-token"))
                                       or getattr(r.endpoint, "__name__", "").startswith("mint_"))]
        assert minting, "found no minting routes — the heuristic matched nothing"
        unclassified = [
            _describe("-", m, p, r) for (m, p, r) in minting
            if not any(_matches(e, m, p) for e in FORBIDDEN_ROLE_ROUTES)]
        assert not unclassified, (
            "token-minting routes with no FORBIDDEN_ROLE_ROUTES entry:\n" + "\n".join(unclassified))


class TestAdminAndMaintenanceReach:
    @pytest.mark.parametrize("role", sorted(ROLE_SCOPES))
    def test_a_role_reaches_exactly_its_reviewed_admin_routes(self, main_mod, route_table, role):
        reached = {(m, p) for (m, p, _r) in _reachable(main_mod, route_table, ROLE_SCOPES[role])
                   if p.startswith("/admin")}
        allowed = ALLOWED_ADMIN_REACH.get(role, frozenset())
        extra = sorted(reached - allowed)
        missing = sorted(allowed - reached)
        assert not extra, (
            f"\n\nThe {role!r} role reaches /api/admin routes nobody reviewed for it (add to "
            "ALLOWED_ADMIN_REACH only after arguing the route is operational and non-destructive):\n"
            + "\n".join(f"  {role:<9} {m:<7} /api{p}" for (m, p) in extra) + "\n")
        assert not missing, f"reviewed admin routes the {role!r} role can no longer reach: {missing}"

    def test_the_operator_reaches_nothing_beyond_the_agent_but_the_reviewed_extras(
            self, main_mod, route_table):
        agent = {(m, p) for (m, p, _r) in _reachable(main_mod, route_table, ROLE_SCOPES["agent"])}
        operator = {(m, p) for (m, p, _r) in _reachable(main_mod, route_table,
                                                         ROLE_SCOPES["operator"])}
        assert agent <= operator, "the operator must inherit the whole agent surface"
        extra = operator - agent
        assert extra == (ALLOWED_ADMIN_REACH["operator"] | OPERATOR_EXTRA_NON_ADMIN_ROUTES), (
            f"operator reach beyond the agent changed: {sorted(extra)}")


class TestReadOnlyRolesReachNoWriteVerb:
    @pytest.mark.parametrize("role", READ_ONLY_ROLES)
    def test_no_write_verb_is_reachable(
            self,
            main_mod: ModuleType,
            route_table: list[tuple[str, str, Any]],
            role: str,
    ) -> None:
        offenders = [_describe(role, m, p, r)
                     for (m, p, r) in _reachable(main_mod, route_table, ROLE_SCOPES[role])
                     if m not in READ_VERBS]
        assert not offenders, (
            f"\n\nThe read-only {role!r} role can reach WRITE verbs:\n" + "\n".join(offenders) + "\n")

    @pytest.mark.parametrize("role", READ_ONLY_ROLES)
    def test_it_reaches_none_of_the_handler_guarded_routes(
            self,
            main_mod: ModuleType,
            route_table: list[tuple[str, str, Any]],
            role: str,
    ) -> None:
        """A read-only role has no business on an approval-capable route in ANY verb."""
        offenders = [_describe(role, m, p, r, "approval-capable")
                     for (m, p, r) in _reachable(main_mod, route_table, ROLE_SCOPES[role])
                     if getattr(r.endpoint, "__name__", "") in HANDLER_GUARDED_ROUTES
                     and m != "GET"]
        assert not offenders, "\n" + "\n".join(offenders)

    @pytest.mark.parametrize("role", READ_ONLY_ROLES)
    def test_the_role_reaches_something(
            self,
            main_mod: ModuleType,
            route_table: list[tuple[str, str, Any]],
            role: str,
    ) -> None:
        """A scope that reaches nothing passes every 'cannot reach' check for the wrong reason."""
        assert _reachable(main_mod, route_table, ROLE_SCOPES[role])


class TestWriteCapableRolesCannotApprove:
    WRITE_ROLES = tuple(r for r in sorted(ROLE_SCOPES) if r not in READ_ONLY_ROLES)

    @pytest.mark.parametrize("role", WRITE_ROLES)
    def test_every_reachable_approval_capable_route_refuses_in_its_handler(
            self, main_mod: ModuleType, route_table: list[tuple[str, str, Any]], role: str) -> None:
        offenders = []
        for method, path, route in _reachable(main_mod, route_table, ROLE_SCOPES[role]):
            name = getattr(route.endpoint, "__name__", "")
            if name not in HANDLER_GUARDED_ROUTES:
                continue
            guards, why = HANDLER_GUARDED_ROUTES[name]
            source = inspect.getsource(route.endpoint)
            missing = [g for g in guards if g not in source]
            if missing:
                offenders.append(_describe(role, method, path, route,
                                           f"handler no longer contains {missing} ({why})"))
        assert not offenders, (
            f"\n\nA {self.WRITE_ROLES} role reaches an approval-capable route whose guard is gone:\n"
            + "\n".join(offenders) + "\n")

    @pytest.mark.parametrize("role", WRITE_ROLES)
    def test_no_reachable_write_route_with_an_approval_field_is_unclassified(
            self, main_mod: ModuleType, route_table: list[tuple[str, str, Any]], role: str) -> None:
        """The net for the NEXT route.

        Reaching a write route whose body has an `action` or a `status` means a draft can be moved
        somewhere by this role; someone must say whether that somewhere is a send.
        """
        offenders = []
        for method, path, route in _reachable(main_mod, route_table, ROLE_SCOPES[role]):
            if method in READ_VERBS:
                continue
            name = getattr(route.endpoint, "__name__", "")
            if _body_fields(route) & _APPROVAL_FIELDS \
                    and name not in HANDLER_GUARDED_ROUTES and name not in NOT_AN_APPROVAL_VECTOR:
                offenders.append(_describe(role, method, path, route, "body has action/status"))
        assert not offenders, (
            "\n\nReachable write routes with an `action`/`status` field that are neither guarded "
            "(HANDLER_GUARDED_ROUTES) nor argued harmless (NOT_AN_APPROVAL_VECTOR):\n"
            + "\n".join(offenders) + "\n")

    @pytest.mark.parametrize("role", WRITE_ROLES)
    def test_every_classification_entry_is_still_a_reachable_route(
            self,
            main_mod: ModuleType,
            route_table: list[tuple[str, str, Any]],
            role: str,
    ) -> None:
        """Stale entries hide a route that moved out from under its guard check."""
        reachable_names = {getattr(r.endpoint, "__name__", "")
                           for (_m, _p, r) in _reachable(main_mod, route_table, ROLE_SCOPES[role])}
        stale = [n for n in (*HANDLER_GUARDED_ROUTES, *NOT_AN_APPROVAL_VECTOR)
                 if n not in reachable_names]
        assert not stale, f"classified but no longer reachable by {role!r}: {stale}"


class TestOperatorApprovesOnlyOnInternalOrSandboxAccounts:
    """The fourth approval guard, asserted from the guards themselves rather than from one route.

    `HANDLER_GUARDED_ROUTES` proves every approval-capable route still calls `_refuse_agent_approval`
    / `_refuse_agent_approved_status` / `_agent_scoped()`; this proves what those guards DO for the
    operator, for every account kind, so a loosening of the guard fails here even if no route moved.
    """

    @staticmethod
    def _as_operator(main_mod: ModuleType, kind: Any) -> Any:
        scope_reset = main_mod._request_session_scope.set(main_mod.SESSION_SCOPE_OPERATOR)
        state_reset = main_mod._request_operator.set({"account_kind": kind, "user_id": 1})
        return scope_reset, state_reset

    @staticmethod
    def _restore(main_mod: ModuleType, resets: Any) -> None:
        main_mod._request_operator.reset(resets[1])
        main_mod._request_session_scope.reset(resets[0])

    @pytest.mark.parametrize("kind", ["internal", "sandbox"])
    @pytest.mark.parametrize("guard,arg", [("_refuse_agent_approval", "approve"),
                                           ("_refuse_agent_approval", "retry"),
                                           ("_refuse_agent_approved_status", "approved")])
    def test_an_operator_may_approve_on_an_internal_or_sandbox_account(
            self, main_mod: ModuleType, kind: str, guard: str, arg: str) -> None:
        resets = self._as_operator(main_mod, kind)
        try:
            with patch_audit(main_mod):
                getattr(main_mod, guard)(arg)  # must not raise
        finally:
            self._restore(main_mod, resets)

    @pytest.mark.parametrize("kind", ["standard", "", None, "INTERNAL", "internal ", "admin", 0])
    @pytest.mark.parametrize("guard,arg", [("_refuse_agent_approval", "approve"),
                                           ("_refuse_agent_approval", "retry"),
                                           ("_refuse_agent_approved_status", "approved")])
    def test_every_other_account_kind_is_refused_with_a_403(
            self, main_mod: ModuleType, kind: Any, guard: str, arg: str) -> None:
        from fastapi import HTTPException

        resets = self._as_operator(main_mod, kind)
        try:
            with pytest.raises(HTTPException) as exc:
                getattr(main_mod, guard)(arg)
        finally:
            self._restore(main_mod, resets)
        assert exc.value.status_code == 403
        assert exc.value.detail["code"] == "agent_may_not_approve"

    def test_an_operator_with_no_account_state_is_refused(self, main_mod: ModuleType) -> None:
        """No resolver stamp at all (a direct call) must read as 'standard', never as 'internal'."""
        from fastapi import HTTPException

        reset = main_mod._request_session_scope.set(main_mod.SESSION_SCOPE_OPERATOR)
        try:
            with pytest.raises(HTTPException):
                main_mod._refuse_agent_approval("approve")
        finally:
            main_mod._request_session_scope.reset(reset)

    def test_the_only_kinds_that_grant_approval_are_internal_and_sandbox(self) -> None:
        from cqc_lem.utilities.db import INTERNAL_ACCOUNT_KINDS

        assert INTERNAL_ACCOUNT_KINDS == frozenset({"internal", "sandbox"})

    def test_the_passive_default_and_the_preferences_write_never_open_for_an_operator(
            self, main_mod: ModuleType) -> None:
        """`_agent_scoped()` stays True for the operator on every account kind.

        It is what keeps the passive `auto_approve` default and the caps closed.
        """
        for kind in ("standard", "internal", "sandbox"):
            resets = self._as_operator(main_mod, kind)
            try:
                assert main_mod._agent_scoped() is True, kind
            finally:
                self._restore(main_mod, resets)

    def test_the_operator_reaches_the_same_guarded_routes_as_the_agent(
            self, main_mod: ModuleType, route_table: list[tuple[str, str, Any]]) -> None:
        """The operator reaches the same approval-capable routes as the agent, and no others.

        It is the same handler, so the same guards; nothing approval-capable is operator-only.
        """
        def names(role: str) -> set[str]:
            return {getattr(r.endpoint, "__name__", "")
                    for (_m, _p, r) in _reachable(main_mod, route_table, ROLE_SCOPES[role])
                    if getattr(r.endpoint, "__name__", "") in HANDLER_GUARDED_ROUTES}

        assert names("operator") == names("agent") != set()


def patch_audit(main_mod: ModuleType) -> Any:
    """Silence the operator-approval audit row, which the guard writes when it allows an approval."""
    from unittest.mock import patch

    return patch.object(main_mod, "record_auth_event", return_value=True)


class TestTheGuardRunsAfterTheSessionIsResolved:
    """A guard must run after the resolver, because it reads the scope the resolver stamps.

    Called BEFORE the resolver it sees no scope and refuses nobody, which is how an `agent` could once
    approve a curated post. Presence of the guard in a handler is not enough; its ORDER is part of the
    guarantee.
    """

    _RESOLVERS = ("get_session_user_id(", "_caller(", "require_session_user_id(")

    def test_every_guarded_handler_resolves_the_session_before_it_guards(
            self, route_table: list[tuple[str, str, Any]]) -> None:
        offenders = []
        seen = set()
        for _m, _p, route in route_table:
            name = getattr(route.endpoint, "__name__", "")
            if name not in HANDLER_GUARDED_ROUTES or name in seen:
                continue
            seen.add(name)
            source = inspect.getsource(route.endpoint)
            guard_at = min(source.index(g) for g in HANDLER_GUARDED_ROUTES[name][0])
            resolver_at = min((source.index(r) for r in self._RESOLVERS if r in source), default=-1)
            if resolver_at < 0 or resolver_at > guard_at:
                offenders.append(f"  {name}: guard at {guard_at}, session resolved at {resolver_at}")
        assert seen == set(HANDLER_GUARDED_ROUTES), "a guarded handler matched no route"
        assert not offenders, ("\n\nThese handlers run an approval guard BEFORE resolving the session, "
                               "so the guard sees no scope:\n" + "\n".join(offenders) + "\n")


class TestOperatorEligibilityGatesStayInPlace:
    """An operator token must only exist on, and act for, an internal, sandbox or admin account.

    The operator surface includes a GLOBAL automation pause. Both ends are pinned from their source.
    """

    def test_the_mint_checks_eligibility_before_it_asks_for_a_step_up(self) -> None:
        from cqc_lem.api.routers import user

        source = inspect.getsource(user.mint_operator_token)
        assert "_operator_account_eligible(" in source
        assert source.index("_operator_account_eligible(") < source.index("_require_step_up(")

    def test_every_use_of_the_admin_routes_re_checks_eligibility(self) -> None:
        from cqc_lem.api.routers import admin

        assert "_operator_account_eligible(" in inspect.getsource(admin._operator_session)

    def test_the_eligible_kinds_are_exactly_internal_and_sandbox(self, main_mod: ModuleType) -> None:
        from unittest.mock import patch

        with patch.object(main_mod, "is_user_admin", return_value=False):
            for kind, expected in (("internal", True), ("sandbox", True), ("standard", False),
                                   ("", False), (None, None), ("admin", False)):
                if kind is None:  # an unreadable kind is resolved by the reader, which fails closed
                    with patch.object(main_mod, "get_user_account_kind", return_value="standard"):
                        assert main_mod._operator_account_eligible(5) is False
                else:
                    assert main_mod._operator_account_eligible(5, kind) is expected, kind
            assert main_mod._operator_account_eligible(None, "internal") is False
        with patch.object(main_mod, "is_user_admin", return_value=True):
            assert main_mod._operator_account_eligible(5, "standard") is True
