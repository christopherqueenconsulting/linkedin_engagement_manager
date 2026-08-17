"""The cookie-less login fallback reaches the handlers it authenticates at (issue #1611).

`AuthContext.login()` falls back to HOLDING the real session token when the browser refused the
httpOnly cookie the login response set. That session authenticates fine at the route — every call
site sends the token in the `session_token` FIELD, which is the one place `get_session_user_id`
resolves an explicit token from. It never got that far: `api_token_middleware` runs BEFORE routing,
has no database and must not consume a POST body, so the only credential it can read is a bearer or
the `lem_session` cookie. With `API_ACCESS_TOKENS` set (it is, in production) every non-`/api/auth/`
request from such a session was 401'd at the edge, which reads as a broken app rather than a broken
session — the same family as #1354 and #1357, a 401 pointing at the wrong thing.

The fix is entirely client-side: the fallback now also writes the token into a first-party
`lem_session` cookie (`ui/src/utils/sessionCookie.ts`), which is the credential BOTH the edge check
and the resolver already read. Nothing on the server changed, so what is asserted here is that the
shape the SPA now sends really does complete a read AND a write — and that the shape it used to send
really is the 401 this fixes.
"""

from typing import Any, Dict, Iterator
from unittest.mock import MagicMock, patch

import pytest

from cqc_lem.utilities.env_constants import SESSION_COOKIE_NAME

pytestmark = pytest.mark.unit

_M = "cqc_lem.api.main"
_UID = 77
# What the browser holds in the fallback and what `sessionCookie.ts` now writes into the cookie: the
# real token, `secrets.token_hex(32)` server-side.
_TOKEN = "5f2c" * 16
_BEARER = "non-browser-token"
# The SPA sends this on every request (#957); a cookie-authenticated write is 403'd without it.
_SPA = {"X-LEM-Client": "spa"}


@pytest.fixture
def fallback_session() -> Iterator[Dict[str, MagicMock]]:
    """A live session whose ONLY credential is the raw token — no bearer, ever.

    `get_session_user_id` is deliberately not patched: whether the token resolves through the field
    or through the cookie is the thing under test.
    """
    with patch(f"{_M}._db_resolve_session",
               side_effect=lambda t: {"user_id": _UID, "scope": "full"} if t == _TOKEN else None), \
         patch(f"{_M}.get_scheduled_dms",
               return_value={"dms": [], "total": 0, "page": 1, "page_size": 25}) as dms, \
         patch(f"{_M}.mark_queued") as mark_queued, \
         patch(f"{_M}.clear_generation_status"), \
         patch(f"{_M}.celery_chain"), \
         patch(f"{_M}.plan_content_for_user"), \
         patch(f"{_M}.auto_create_weekly_content"), \
         patch(f"{_M}._API_ACCESS_TOKEN_SET", {_BEARER}):
        yield {"read": dms, "write": mark_queued}


class TestTheFallbackCompletesRealWork:
    """A read and a write, with `API_ACCESS_TOKENS` set and no bearer in sight."""

    def test_an_authenticated_read_lands(self, api_client: Any,
                                         fallback_session: Dict[str, MagicMock]) -> None:
        resp = api_client.get("/api/dms", params={"session_token": _TOKEN},
                              cookies={SESSION_COOKIE_NAME: _TOKEN})

        assert resp.status_code == 200
        # Past the edge AND past the resolver — a 200 from a handler that never ran would not be one.
        fallback_session["read"].assert_called_once()
        assert fallback_session["read"].call_args.args[0] == _UID

    def test_an_authenticated_write_lands(self, api_client: Any,
                                          fallback_session: Dict[str, MagicMock]) -> None:
        resp = api_client.post("/api/create_weekly_content/", params={"session_token": _TOKEN},
                               cookies={SESSION_COOKIE_NAME: _TOKEN}, headers=_SPA)

        assert resp.status_code == 200
        fallback_session["write"].assert_called_once()


class TestWithoutTheCookieItIsTheBugAgain:
    """The pre-#1611 shape: the token in the field the resolver reads, and nothing at the edge.

    Both fail at the middleware, which is the whole complaint — the request is refused before the
    resolver is asked about a token that would have resolved.
    """

    def test_the_read_is_401_at_the_edge(self, api_client: Any,
                                         fallback_session: Dict[str, MagicMock]) -> None:
        resp = api_client.get("/api/dms", params={"session_token": _TOKEN})

        assert resp.status_code == 401
        fallback_session["read"].assert_not_called()

    def test_the_write_is_401_at_the_edge(self, api_client: Any,
                                          fallback_session: Dict[str, MagicMock]) -> None:
        resp = api_client.post("/api/create_weekly_content/", params={"session_token": _TOKEN},
                               headers=_SPA)

        assert resp.status_code == 401
        fallback_session["write"].assert_not_called()


class TestTheCookieIsNotAWayIn:
    """Clearing the edge filter is not authorisation, and the fallback's cookie does not make it one.

    The middleware is deliberately weak — one arbitrary cookie byte clears it — so the token in the
    cookie has to be judged by the resolver like any other, and a forged one is refused there.
    """

    def test_a_forged_cookie_is_refused_by_the_route(self, api_client: Any,
                                                     fallback_session: Dict[str, MagicMock]) -> None:
        resp = api_client.get("/api/dms", params={"session_token": "forged"},
                              cookies={SESSION_COOKIE_NAME: "forged"})

        assert resp.status_code == 401
        fallback_session["read"].assert_not_called()

    def test_a_write_on_the_cookie_alone_still_needs_the_client_header(
            self, api_client: Any, fallback_session: Dict[str, MagicMock]) -> None:
        """CSRF (#957) is unchanged by the fallback carrying a cookie.

        That is what makes the cookie safe to write from script.

        This is the shape a forged cross-site write has: the browser attaches the cookie by itself
        and the forger supplies nothing else. `SameSite=Lax` — which `sessionCookie.ts` sets exactly
        as the server's own cookie does — already keeps it off a cross-site POST, and the header
        layer stands behind that. The fallback's OWN writes take the explicit-token branch instead,
        which no forger can reach: it needs the token, and knowing the token is the whole game.
        """
        resp = api_client.post("/api/create_weekly_content/",
                               cookies={SESSION_COOKIE_NAME: _TOKEN})

        assert resp.status_code == 403
        assert resp.json()["detail"]["code"] == "client_header_required"
        fallback_session["write"].assert_not_called()

    def test_the_cookie_alone_authenticates_the_caller(
            self, api_client: Any, fallback_session: Dict[str, MagicMock]) -> None:
        """The mechanism, stated directly.

        The token in the cookie is a credential the resolver judges, not merely a byte that clears
        the edge filter.
        """
        resp = api_client.post("/api/create_weekly_content/",
                               cookies={SESSION_COOKIE_NAME: _TOKEN}, headers=_SPA)

        assert resp.status_code == 200
        assert fallback_session["write"].call_args.args[0] == _UID
