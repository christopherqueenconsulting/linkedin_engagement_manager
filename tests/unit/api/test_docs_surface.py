"""The FastAPI docs surface lives under `/api`, and the admin routes are not in it (issue #1020).

At the FastAPI defaults Swagger, ReDoc and the schema sit at `/docs`, `/redoc` and `/openapi.json`
— outside `/api`, which is the only prefix `_api_token_required()` inspects. All three were
therefore served to anyone, and the schema published all eighteen `/api/admin/*` operations by
method and path: a targeting map for anyone probing the admin secret, on a document that needed no
credential to fetch.

Two things are pinned here. That the surface answers on the `/api` paths (with the old ones
redirecting, so every bookmark and Postman import keeps working), and that NO admin operation
appears in the generated schema. The second is the one that would regress silently: the hiding is
derived from the route table precisely so a nineteenth admin route inherits it, and this test is
what fails if that derivation ever stops reaching them.
"""

from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture(scope="module")
def client():
    with patch("cqc_lem.utilities.observability.track_api_call"):
        from fastapi.testclient import TestClient

        from cqc_lem.api.main import app
        with TestClient(app, raise_server_exceptions=False) as tc:
            yield tc


class TestDocsSurfaceMovedUnderApi:
    @pytest.mark.parametrize("path", ["/api/docs", "/api/redoc", "/api/openapi.json"])
    def test_reachable_at_the_new_path(self, client, path):
        r = client.get(path)
        assert r.status_code == 200
        assert r.content

    def test_swagger_oauth2_redirect_helper_moved_with_it(self, client):
        """FastAPI defaults `swagger_ui_oauth2_redirect_url` to the fixed literal
        `/docs/oauth2-redirect` and does NOT derive it from `docs_url`. Left alone it strands the
        helper outside `/api` and Swagger's Authorize flow breaks silently.
        """
        assert client.get("/api/docs/oauth2-redirect").status_code == 200

    @pytest.mark.parametrize("old,new", [
        ("/docs", "/api/docs"),
        ("/redoc", "/api/redoc"),
        ("/openapi.json", "/api/openapi.json"),
    ])
    def test_old_path_redirects_permanently(self, client, old, new):
        r = client.get(old, follow_redirects=False)
        assert r.status_code == 301
        assert r.headers["location"] == new

    def test_new_paths_are_public_leaf_entries_not_subtrees(self):
        """Leaf entries, so `/api/docs/oauth2-redirect` is covered (the non-slash branch matches
        path-segment children) while a future `/api/docs-admin` is NOT quietly unlocked.
        """
        from cqc_lem.api.main import _PUBLIC_API_PREFIXES, _is_public_api_path
        for prefix in ("/api/docs", "/api/redoc", "/api/openapi.json"):
            assert prefix in _PUBLIC_API_PREFIXES
            assert not prefix.endswith("/")
            assert _is_public_api_path(prefix)
        assert _is_public_api_path("/api/docs/oauth2-redirect")
        assert not _is_public_api_path("/api/docs-admin")

    def test_the_surface_answers_with_no_credential_once_the_gate_is_LIVE(self, client):
        """The tests above run in the CI config, where `API_ACCESS_TOKENS` is unset and
        `_api_token_required` short-circuits to False for EVERY path — so they would pass just as
        happily if the three entries had never been added to `_PUBLIC_API_PREFIXES`.

        That prod/CI difference is the shape of #1020 itself, so pin the prod config directly: with
        tokens configured the docs surface still answers uncredentialed, and neither a look-alike
        sibling nor an admin route rides in on it.
        """
        with patch("cqc_lem.api.main._API_ACCESS_TOKEN_SET", {"a-configured-token"}):
            for path in ("/api/docs", "/api/redoc", "/api/openapi.json",
                         "/api/docs/oauth2-redirect"):
                assert client.get(path).status_code == 200, path
            assert client.get("/api/docs-admin").status_code == 401
            assert client.get("/api/admin/automation-status",
                              params={"user_id": 1}).status_code == 401

    def test_the_legacy_redirect_beats_the_SPA_catch_all(self, client):
        """`src/cqc_lem/ui/dist` does not exist in a checkout, so the SPA catch-all is never
        registered in CI and the redirect tests above have nothing competing with them. In prod it
        IS registered, and `/{full_path:path}` matches `/docs` — the redirects only win because
        Starlette matches in registration order and they are declared first. Register a stand-in
        catch-all so that ordering is actually exercised: without it, moving those three
        registrations below the SPA block would turn every legacy docs link into a 200 HTML page
        and no test would notice.
        """
        from fastapi.responses import HTMLResponse

        from cqc_lem.api.main import app

        app.get("/{full_path:path}", include_in_schema=False)(
            lambda full_path: HTMLResponse("<html>spa</html>"))
        try:
            for old, new in (("/docs", "/api/docs"), ("/redoc", "/api/redoc"),
                             ("/openapi.json", "/api/openapi.json")):
                r = client.get(old, follow_redirects=False)
                assert r.status_code == 301, f"{old} was answered by the SPA, not the redirect"
                assert r.headers["location"] == new
            # ...and the stand-in really was reachable, so the assertions above are not vacuous.
            assert client.get("/some-spa-route").text == "<html>spa</html>"
        finally:
            app.router.routes.pop()


class TestAdminRoutesAreNotInTheSchema:
    def test_no_admin_route_appears_in_the_public_schema(self, client):
        schema = client.get("/api/openapi.json").json()
        leaked = [p for p in schema["paths"] if p.startswith("/api/admin")]
        assert not leaked, (
            "these /api/admin operations are published in an unauthenticated schema, which names "
            f"the exact paths an attacker would probe the admin secret against: {leaked}"
        )
        # Not a vacuous pass on an empty/broken schema.
        assert len(schema["paths"]) > 100

    def test_every_admin_route_was_hidden_not_merely_absent(self):
        """The counter proves the derivation REACHED the routes. Without it, a walk that silently
        matched nothing (FastAPI ≥0.139 keeps an included router as one opaque node) would leave
        the schema unchanged and the test above would fail for a reason nobody could read.
        """
        from cqc_lem.api.main import _ADMIN_ROUTES_HIDDEN
        assert _ADMIN_ROUTES_HIDDEN >= 18

    def test_admin_routes_still_exist_and_still_answer(self, client):
        """Hidden is not removed: the routes must keep serving their real handler, and their auth
        is unchanged — a missing admin secret is still 403, never 404.
        """
        from cqc_lem.api.main import _walk_routes, app
        paths = {getattr(r, "path", "") for r in _walk_routes(app.routes)}
        assert "/api/admin/automation-pause" in paths
        with patch("cqc_lem.api.routers.admin.ADMIN_SECRET", "s3cret"):
            assert client.post("/api/admin/automation-pause",
                               params={"user_id": 1}).status_code == 403


class TestSecuritySchemeDescriptions:
    """Describe the credential, not where it is stored (issue #1020).

    Naming `API_ACCESS_TOKENS` / `ADMIN_SECRET` in a public description hands an attacker the exact
    string to grep for in a leaked build, a stack trace or a misconfigured container.
    """

    def test_no_env_var_name_is_disclosed(self):
        # Both schemes moved with the admin router (#1154): only `_require_api_and_admin` reads
        # them, and it takes them as `Depends()` DEFAULTS, which bind at import time.
        from cqc_lem.api.routers.admin import _admin_secret_scheme, _bearer_scheme
        for scheme in (_bearer_scheme, _admin_secret_scheme):
            description = scheme.model.description or ""
            assert "API_ACCESS_TOKENS" not in description
            assert "ADMIN_SECRET" not in description
            assert description.strip(), "a scheme with no description documents nothing"
