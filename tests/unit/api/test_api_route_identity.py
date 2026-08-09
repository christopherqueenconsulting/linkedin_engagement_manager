"""Every gated `/api` route resolves its caller from the SESSION — as a standing invariant.

Issue #950 loosened a global edge control: the `/api` middleware used to demand a valid bearer
token, and now takes a session credential too (a cookie or `X-Session-Token`) whose validity it
deliberately does not check. That is only safe because of a claim — "since #914 every `/api`
handler resolves its caller through `get_session_user_id`" — and a claim asserted in prose is worth
nothing the day someone adds a route that leans on the bearer alone. That route would silently
become reachable with `Cookie: lem_session=x`.

So the claim lives here instead, checked mechanically against the real route table. A gated route
has to reach ONE of the two credentials that actually authorise something: the session (seeded from
`get_session_user_id`, the ONE resolver) or the admin secret (`_require_admin` /
`_require_api_and_admin`, which is what `/api/admin/*` runs on and which was never in the bundle).
Both sets are DERIVED, not hardcoded — closed over every helper that reaches a seed — so a new
wrapper (`_require_user_admin`, `_owned_edition`, the next one) counts automatically and only a
genuinely unguarded route fails. The closure spans `main` AND every `api/routers/*.py` module,
because #1154 moved handlers and their wrappers out of `main` while the SEEDS stayed: a wrapper the
derivation cannot see reads as an unguarded route, which is a false alarm on the one check whose
whole value is that it never cries wolf.
"""

import inspect
import pkgutil
import re
from typing import Iterator, List, Set

import pytest

pytestmark = pytest.mark.unit


# A gated route may legitimately resolve nobody when it reads NO user-scoped data. Each entry needs
# a reason, and adding one is the point at which you have to argue the route is caller-independent.
_NO_IDENTITY_BY_DESIGN = {
    # Static UI metadata: the carousel template registry's keys/labels/descriptions. No user data
    # reaches it and no state changes, so there is no caller to authorise. Still behind the
    # credential gate; it is simply not per-user.
    "/api/carousel-templates",
}

# The seeds. `get_session_user_id` is the ONE place a caller's identity is decided (CLAUDE.md);
# the admin pair is the other credential a gated route may run on — `/api/admin/*` demands the
# `X-Admin-Secret`, which (unlike the bearer) was never shipped in the SPA bundle.
_IDENTITY_SEEDS = frozenset({"get_session_user_id"})
# Both admin seeds now live in `api/routers/admin.py`, not `main` — they are `Depends()` DEFAULT
# arguments, which bind at import time and so could not be reached as `_main.<name>` (#1154). They
# are named here as STRINGS and closed over `_functions()`, which spans every discovered router, so
# the move needed no edit. Narrowing that span back to `main` would drop `_require_user_admin` out
# of the closure and report three real admin routes as unguarded — verified by sabotage, and loud.
_ADMIN_SEEDS = frozenset({"_require_admin", "_require_api_and_admin"})

# The three gates an `/api/admin/*` route may run on. `_require_api_and_admin` re-checks the bearer
# in the handler, so those routes still take two credentials; the other two take `X-Admin-Secret`
# alone or an admin session, because #950 stopped the middleware demanding a bearer on top. What
# survives as an invariant is that an admin route reaches ONE of the three — a new one that only
# resolved a session would be reachable by any signed-in user.
_ADMIN_GATES = ("_require_api_and_admin", "_require_admin", "_require_user_admin")


@pytest.fixture(scope="module")
def main_mod():
    # Imported lazily: importing main builds the OpenAI client, which needs the env the session
    # autouse fixture sets after collection.
    from cqc_lem.api import main
    return main


def _handler_modules(main_mod) -> List[object]:
    """`main` plus every per-area router — discovered, never listed, so a new slice is covered."""
    import importlib

    from cqc_lem.api import routers

    found = [main_mod]
    for info in pkgutil.iter_modules(routers.__path__):
        found.append(importlib.import_module(f"cqc_lem.api.routers.{info.name}"))
    return found


def _iter_routes(routes) -> Iterator[object]:
    """Flatten the route table, descending through included routers and mounts."""
    for route in routes:
        included = getattr(route, "original_router", None)
        children = getattr(included, "routes", None) if included is not None else \
            getattr(route, "routes", None)
        if children:
            yield from _iter_routes(children)
        if getattr(route, "endpoint", None) is not None:
            yield route


def _source_of(obj) -> str:
    try:
        return inspect.getsource(obj)
    except (OSError, TypeError):
        return ""


def _references(source: str, name: str) -> bool:
    # A bare-name match, not `name(`: a route can gate through `Depends(_require_api_and_admin)`,
    # where the name is followed by `)` and never called in the body at all.
    return re.search(rf"\b{re.escape(name)}\b", source) is not None


def _functions(main_mod) -> dict:
    """Every function across `main` and the routers, by name."""
    out = {}
    for module in _handler_modules(main_mod):
        out.update({name: obj for name, obj in vars(module).items() if inspect.isfunction(obj)})
    return out


def _closure_over(main_mod, seeds) -> Set[str]:
    """The seeds plus every handler-module function that transitively reaches one of them."""
    names = set(seeds)
    sources = {name: _source_of(obj) for name, obj in _functions(main_mod).items()}
    changed = True
    while changed:
        changed = False
        for name, src in sources.items():
            if name in names:
                continue
            if any(_references(src, seed) for seed in names):
                names.add(name)
                changed = True
    return names


def _gates(main_mod) -> Set[str]:
    return _closure_over(main_mod, _IDENTITY_SEEDS) | _closure_over(main_mod, _ADMIN_SEEDS)


def _enforces_identity(route, gates: Set[str]) -> bool:
    source = _source_of(route.endpoint)
    return any(_references(source, gate) for gate in gates)


class TestEveryGatedApiRouteResolvesItsCaller:
    def test_gate_derivation_finds_the_known_wrappers(self, main_mod):
        """Guard the derivation itself. Too narrow and the test below reports false failures; too
        wide and it passes vacuously, so pin both ends.
        """
        gates = _gates(main_mod)
        assert {"get_session_user_id", "require_session_user_id", "_require_user_admin",
                "_owned_edition", "_require_admin", "_require_api_and_admin"} <= gates
        # A closure that swallowed most of the module would call every route guarded.
        functions = list(_functions(main_mod))
        assert len(gates) < len(functions) / 2, (
            f"the gate closure reached {len(gates)} of {len(functions)} functions — too wide to "
            "distinguish a guarded route from an unguarded one"
        )

    def test_no_gated_route_is_reachable_on_a_forged_cookie_alone(self, main_mod):
        resolvers = _gates(main_mod)
        unguarded: List[str] = []
        gated = 0
        for route in _iter_routes(main_mod.app.routes):
            path = getattr(route, "path", "")
            if not path.startswith("/api/") or main_mod._is_public_api_path(path):
                continue
            gated += 1
            if path in _NO_IDENTITY_BY_DESIGN:
                continue
            if not _enforces_identity(route, resolvers):
                methods = sorted(getattr(route, "methods", None) or [])
                unguarded.append(f"{methods} {path} -> {route.endpoint.__name__}")

        # If this drops to a handful the walk broke, not the codebase — fail loudly rather than
        # pass on an empty route table.
        assert gated > 100, f"only {gated} gated /api routes found — the route walk is broken"
        assert not unguarded, (
            "These /api routes are behind the credential gate but resolve no caller, so since "
            "issue #950 they are reachable by anyone who sets a session cookie of any value. "
            "Resolve the caller with require_session_user_id(), or add the path to "
            f"_NO_IDENTITY_BY_DESIGN with a reason it reads no user-scoped data: {unguarded}"
        )

    def test_every_admin_route_reaches_an_admin_gate(self, main_mod):
        """`/api/admin/*` is where the loosened middleware costs the most, so pin what is left.

        The test above would accept an admin route that merely resolved a session — every signed-in
        user has one. An admin route has to reach a gate that checks something ADMIN: the secret, or
        `is_user_admin`. Which of the three it picks is a real difference in credential count and is
        documented per-route in `docs/identity-and-sessions.md`; that all eighteen reach one of them
        is the part that must not drift.
        """
        ungated: List[str] = []
        admin_routes = 0
        for route in _iter_routes(main_mod.app.routes):
            path = getattr(route, "path", "")
            if not path.startswith("/api/admin"):
                continue
            admin_routes += 1
            source = _source_of(route.endpoint)
            if not any(_references(source, gate) for gate in _ADMIN_GATES):
                ungated.append(f"{path} -> {route.endpoint.__name__}")

        assert admin_routes >= 18, f"only {admin_routes} /api/admin routes found — the walk broke"
        assert not ungated, (
            "These /api/admin routes reach none of "
            f"{list(_ADMIN_GATES)}, so being signed in is enough to call them: {ungated}"
        )

    def test_allowlist_entries_are_real_gated_routes(self, main_mod):
        """A stale allowlist entry silently excuses nothing — and hides that it stopped applying."""
        paths = {getattr(r, "path", "") for r in _iter_routes(main_mod.app.routes)}
        for path in _NO_IDENTITY_BY_DESIGN:
            assert path in paths, f"{path} is allowlisted but is not a route any more"
            # Asked of `_is_public_api_path`, not `_api_token_required`: the latter short-circuits
            # on an empty `_API_ACCESS_TOKEN_SET`, which is what the unit env has, so it would
            # answer "not gated" for every path and this assertion would never run.
            assert not main_mod._is_public_api_path(path), \
                f"{path} is allowlisted as a GATED route but is public"
