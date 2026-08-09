"""Rules a per-area `/api` router must obey, checked against the live app (#1154).

Both failures this guards against are SILENT — the app starts, the tests pass, and the damage shows
up as routes that quietly do not exist or checks that quietly match nothing.

1. **A router module that is never included.** Its routes simply do not exist. Nothing imports them,
   so nothing raises; the SPA gets a 404 in production.
2. **A prefix supplied at include time instead of on the `APIRouter`.** The route is SERVED at the
   right URL either way, so every functional test passes — but `route.path` then omits the prefix,
   and `route.path` is what `_scope_path`, `_hide_admin_routes_from_schema` and the session-scope
   surface guards all read. An `/api/admin/*` route registered that way publishes itself in the
   OpenAPI schema; a scoped route stops being matched by the narrowing that is supposed to contain
   it.
"""

import importlib
import pkgutil

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture(scope="module")
def main_mod():
    from cqc_lem.api import main

    return main


@pytest.fixture(scope="module")
def router_modules():
    from cqc_lem.api import routers

    found = []
    for info in pkgutil.iter_modules(routers.__path__):
        module = importlib.import_module(f"cqc_lem.api.routers.{info.name}")
        if hasattr(module, "router"):
            found.append((info.name, module))
    return found


class TestEveryRouterIsMounted:
    def test_at_least_one_router_module_exists(self, router_modules):
        """Anti-vacuity. Every assertion below iterates this list; an empty one passes silently."""
        assert router_modules, "no router modules found — the checks below would test nothing"

    def test_every_router_module_has_its_routes_served(self, main_mod, router_modules):
        served = {r.path for r in main_mod._walk_routes(main_mod.app.routes)
                  if getattr(r, "path", None)}
        for name, module in router_modules:
            declared = {r.path for r in module.router.routes if getattr(r, "path", None)}
            missing = sorted(declared - served)
            assert not missing, f"routers/{name}.py declares routes the app never serves: {missing}"


class TestThePrefixIsDeclaredOnTheRouter:
    def test_every_route_path_already_carries_its_routers_prefix(self, router_modules):
        """The check that would have caught an include-time prefix.

        `router.routes[i].path` is stored WITH the APIRouter's own prefix and WITHOUT one passed to
        `include_router`. So if this holds, `route.path` is the full served path — which is the only
        thing that makes the scope and admin-schema derivations correct.
        """
        for name, module in router_modules:
            prefix = module.router.prefix
            assert prefix.startswith("/api"), (
                f"routers/{name}.py declares prefix={prefix!r}; it must be the FULL served prefix, "
                "not a suffix completed by include_router(prefix=...)"
            )
            for route in module.router.routes:
                path = getattr(route, "path", None)
                if path is None:
                    continue
                assert path.startswith(prefix), f"routers/{name}.py: {path} is outside {prefix}"

    def test_a_moved_route_is_still_reachable_under_api(self, main_mod, router_modules):
        """Concrete end-to-end: the paths the SPA calls did not change spelling in the move."""
        served = {r.path for r in main_mod._walk_routes(main_mod.app.routes)
                  if getattr(r, "path", None)}
        assert "/api/outreach/target" in served
        assert "/api/outreach/targets" in served
