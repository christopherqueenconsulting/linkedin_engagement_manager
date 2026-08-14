"""`api/main.py` must be ONE module object, and cookie auth must survive the router split (#1354).

The session cookie is published on a ContextVar that lives in `cqc_lem.api.main`, and every handler
in `api/routers/*.py` reads it back through `_main.get_session_user_id()`. That only works while
there is exactly one copy of the module. `/app` and `/app/src` are both importable in the container,
so `src.cqc_lem.api.main` loads the same file a SECOND time with its own ContextVars — and the start
script said exactly that for months. Uvicorn served the `src.` copy, whose middleware published the
cookie where no router could see it, so sign-in minted a session and every call after it 401'd. The
routes still defined in `main.py` kept working, which is why nothing looked broken from the outside.

Nothing raises on that path, so these are the three standing proofs:

* the start scripts name the module **canonically** — the check that would have caught the outage;
* the import-name **tripwire** fires when it does not;
* a **router-served** route actually authenticates on the cookie alone, end to end.
"""

import re
from pathlib import Path
from typing import Iterator
from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit

_M = "cqc_lem.api.main"
# Patched where the symbol now LIVES: `/auth/session` moved to this router in #1154, and a sibling
# in the same module calls it directly — it never reads a re-export back through `main`.
_AUTH = "cqc_lem.api.routers.auth"
_COOKIE = "cookie-token-1354"
_UID = 1

_REPO_ROOT = Path(__file__).resolve().parents[3]
_START_SCRIPTS = (
    _REPO_ROOT / "compose" / "local" / "fastapi" / "start",
    _REPO_ROOT / "compose" / "local" / "fastapi" / "start-cloud",
)


class TestStartScriptsNameTheCanonicalModule:
    """The one check that would have caught #1354 before it shipped."""

    @pytest.mark.parametrize("script", _START_SCRIPTS, ids=lambda p: p.name)
    def test_uvicorn_target_is_the_canonical_package(self, script: Path) -> None:
        assert script.is_file(), f"{script} is gone — the API start path moved"
        targets = re.findall(r"^\s*uvicorn\s+(\S+)", script.read_text(), flags=re.MULTILINE)
        assert targets, f"{script.name} no longer starts uvicorn"
        for target in targets:
            assert target == "cqc_lem.api.main:app", (
                f"{script.name} starts uvicorn as {target!r}. It MUST be 'cqc_lem.api.main:app' — "
                "any other spelling loads api/main.py a second time as a separate module object "
                "and silently kills cookie auth on every router-served route (#1354)."
            )

    @pytest.mark.parametrize("script", _START_SCRIPTS, ids=lambda p: p.name)
    def test_no_src_prefixed_module_path_in_any_command(self, script: Path) -> None:
        """Comments are where the alias is EXPLAINED; a command is where it does damage."""
        commands = [line for line in script.read_text().splitlines()
                    if line.strip() and not line.lstrip().startswith("#")]
        offenders = [line for line in commands if "src.cqc_lem" in line]
        assert not offenders, (
            f"{script.name} runs the 'src.cqc_lem' import alias — it duplicates every module it "
            f"loads (#1354): {offenders}"
        )


class TestImportNameTripwire:
    """Nothing raises when the module is aliased, so the tripwire is the only signal."""

    def test_canonical_import_is_silent(self) -> None:
        from cqc_lem.api import main

        assert main.__name__ == main._CANONICAL_MODULE
        with patch(f"{_M}.log_critical") as critical:
            assert main._guard_canonical_module() is True
        critical.assert_not_called()

    def test_aliased_import_is_critical(self) -> None:
        from cqc_lem.api import main

        with patch(f"{_M}.__name__", "src.cqc_lem.api.main"), \
             patch(f"{_M}.log_critical") as critical:
            assert main._guard_canonical_module() is False
        critical.assert_called_once()
        assert critical.call_args.kwargs["module_name"] == "src.cqc_lem.api.main"


@pytest.fixture
def session_reads_stubbed(api_client) -> Iterator[object]:
    """`api_client` with only the I/O the `/api/auth/session` boot call touches mocked out."""
    with patch(f"{_AUTH}.get_user_email", return_value="owner@example.com"), \
         patch(f"{_AUTH}.get_user_public_uid", return_value="uid-1354"), \
         patch(f"{_AUTH}.is_user_admin", return_value=False), \
         patch(f"{_AUTH}.get_user_analytics_profile", return_value={}), \
         patch(f"{_AUTH}.strong_factor_deadline", return_value=None), \
         patch(f"{_AUTH}.strong_factor_prompt_due", return_value=False):
        yield api_client


class TestRouterServedRouteAuthenticatesOnTheCookie:
    """The end-to-end shape of the outage.

    `/api/auth/session` lives in `routers/auth.py` and the cookie is published by middleware in
    `main.py`. If those are two module objects, this 401s.
    """

    def test_cookie_alone_resolves(self, session_reads_stubbed) -> None:
        with patch(f"{_M}._db_resolve_session",
                   side_effect=lambda t: {"user_id": _UID, "scope": "full"}
                   if t == _COOKIE else None):
            resp = session_reads_stubbed.get("/api/auth/session", cookies={"lem_session": _COOKIE})
        assert resp.status_code == 200, (
            "a router-served route did not authenticate on the session cookie — the middleware and "
            "the handler are reading different ContextVars (#1354)"
        )
        assert resp.json()["detail"]["user_id"] == _UID

    def test_sentinel_plus_cookie_resolves(self, session_reads_stubbed) -> None:
        """The SPA sends `?session_token=cookie` on ~150 call sites; it must not defeat the cookie."""
        with patch(f"{_M}._db_resolve_session",
                   side_effect=lambda t: {"user_id": _UID, "scope": "full"}
                   if t == _COOKIE else None):
            resp = session_reads_stubbed.get("/api/auth/session",
                                             params={"session_token": "cookie"},
                                             cookies={"lem_session": _COOKIE})
        assert resp.status_code == 200
        assert resp.json()["detail"]["user_id"] == _UID

    def test_no_credential_is_still_401(self, session_reads_stubbed) -> None:
        with patch(f"{_M}._db_resolve_session", return_value=None):
            resp = session_reads_stubbed.get("/api/auth/session")
        assert resp.status_code == 401


def test_middleware_and_resolver_share_one_contextvar() -> None:
    """The invariant underneath all of the above, asserted directly."""
    import sys

    from cqc_lem.api import main
    from cqc_lem.api.routers import auth

    assert auth._main is main, "routers bound a different copy of api.main (#1354)"
    assert auth._main._request_session_cookie is main._request_session_cookie
    assert "src.cqc_lem.api.main" not in sys.modules, (
        "the 'src.' import alias was loaded — that is a second copy of every module under it"
    )
    assert isinstance(main.app, MagicMock) or main.app is auth._main.app
