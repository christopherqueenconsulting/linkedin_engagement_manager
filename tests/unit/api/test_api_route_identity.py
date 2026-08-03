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
Both sets are DERIVED, not hardcoded — closed over every helper in `main` that reaches a seed — so a
new wrapper (`_require_user_admin`, `_owned_edition`, the next one) counts automatically and only a
genuinely unguarded route fails.
"""

import inspect
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
_ADMIN_SEEDS = frozenset({"_require_admin", "_require_api_and_admin"})


@pytest.fixture(scope="module")
def main_mod():
    # Imported lazily: importing main builds the OpenAI client, which needs the env the session
    # autouse fixture sets after collection.
    from cqc_lem.api import main
    return main


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


def _closure_over(main_mod, seeds) -> Set[str]:
    """The seeds plus every function in `main` that transitively reaches one of them."""
    names = set(seeds)
    sources = {name: _source_of(obj) for name, obj in vars(main_mod).items()
               if inspect.isfunction(obj)}
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
        wide and it passes vacuously, so pin both ends."""
        gates = _gates(main_mod)
        assert {"get_session_user_id", "require_session_user_id", "_require_user_admin",
                "_owned_edition", "_require_admin", "_require_api_and_admin"} <= gates
        # A closure that swallowed most of the module would call every route guarded.
        functions = [n for n, o in vars(main_mod).items() if inspect.isfunction(o)]
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

    def test_allowlist_entries_are_real_gated_routes(self, main_mod):
        """A stale allowlist entry silently excuses nothing — and hides that it stopped applying."""
        paths = {getattr(r, "path", "") for r in _iter_routes(main_mod.app.routes)}
        for path in _NO_IDENTITY_BY_DESIGN:
            assert path in paths, f"{path} is allowlisted but is not a route any more"
            assert main_mod._api_token_required(path) or not main_mod._API_ACCESS_TOKEN_SET, \
                f"{path} is allowlisted as a GATED route but is public"
