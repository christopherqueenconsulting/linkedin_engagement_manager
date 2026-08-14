"""`api_client` is the ONE way a test gets a client for the API — issue #1214.

Three standing proofs, one per thing the 88 ad-hoc `TestClient(...)` constructions cost us:

* **nothing else constructs one**, so there is a single place the app is entered and a single
  place its lifespan runs;
* **no module-scope patch leaks into the API layer**, which is what the fixtures this replaced
  did — they patched a Celery task on its DEFINING module and then imported `api/main.py` inside
  that patch, so `main` bound the MagicMock permanently and every later file's copy of the same
  patch was vacuous (the #1194 dead-copy shape, one layer down);
* **`app.dependency_overrides` composes**, because that is what #1212 left as the supported way to
  state what a test needs, and a fixture that leaked one would poison the next test.
"""

import ast
import importlib
import pkgutil
from pathlib import Path
from unittest.mock import NonCallableMock

import pytest

pytestmark = pytest.mark.unit

_TESTS_ROOT = Path(__file__).resolve().parents[2]

# The only modules allowed to build their own client, because the thing they drive is NOT
# `cqc_lem.api.main.app`. Both are asserted to still exist, so a rename cannot quietly widen this.
_OWN_APP_MODULES = {
    "unit/api/test_spa_seo_files.py":
        "builds its own FastAPI app per case, and reloads api.main against a temp dist directory",
    "unit/api/test_spa_asset_archive.py":
        "mounts ArchivedStaticFiles on a bare Starlette app — no LEM routes at all",
}


def _client_names(tree: ast.AST) -> set[str]:
    """Every local name this module bound to `TestClient`, alias included.

    Matching the literal name `TestClient` alone would let
    `from fastapi.testclient import TestClient as Client` past the scan, which is the one shape a
    guard keyed on a spelling always misses.
    """
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            names |= {a.asname or a.name for a in node.names if a.name == "TestClient"}
    return names


def _test_client_sites() -> list[str]:
    """Every `TestClient(...)` construction under tests/, as `<relative path>:<line>`.

    Both call shapes count: the bare name (`TestClient(app)`, aliases resolved by `_client_names`)
    and the attribute one (`testclient.TestClient(app)`), which no import statement binds a name for.
    """
    sites = []
    for path in sorted(_TESTS_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text())
        local_names = _client_names(tree) | {"TestClient"}
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            constructs = (isinstance(func, ast.Name) and func.id in local_names) or (
                isinstance(func, ast.Attribute) and func.attr == "TestClient")
            if constructs:
                sites.append(f"{path.relative_to(_TESTS_ROOT)}:{node.lineno}")
    return sites


class TestApiClientIsTheOnlyConstruction:
    def test_no_test_module_builds_its_own_lem_client(self) -> None:
        offenders = [
            site for site in _test_client_sites()
            if site.rsplit(":", 1)[0] not in _OWN_APP_MODULES
            and not site.startswith("conftest.py")
        ]
        assert not offenders, (
            "these sites construct a TestClient instead of taking the `api_client` fixture "
            f"(#1214): {offenders}. The fixture is in tests/conftest.py; a test that needs extra "
            "state wraps it in its own fixture rather than building a second client."
        )

    @pytest.mark.parametrize("relative_path", sorted(_OWN_APP_MODULES))
    def test_every_exemption_still_names_a_real_module(self, relative_path: str) -> None:
        """An exemption that outlives its file is how an allowlist quietly becomes a loophole."""
        assert (_TESTS_ROOT / relative_path).is_file(), (
            f"{relative_path} is gone — drop its entry from _OWN_APP_MODULES rather than leaving a "
            "path nothing matches"
        )

    def test_the_fixture_itself_is_the_one_exception(self) -> None:
        """Anti-vacuity: the scan above would also pass if nobody constructed a client anywhere."""
        assert [s for s in _test_client_sites() if s.startswith("conftest.py")], \
            "tests/conftest.py no longer constructs the one TestClient — the scan proves nothing"


def _api_modules() -> list[str]:
    from cqc_lem.api import routers

    names = ["cqc_lem.api.main"]
    names += [f"cqc_lem.api.routers.{m.name}" for m in pkgutil.iter_modules(routers.__path__)]
    return names


class TestNoModuleScopePatchLeaksIntoTheApiLayer:
    """The defect #1214 closed, asserted directly rather than described.

    `api/main.py` does `from cqc_lem.app.engagement.posting import automate_reply_commenting` at
    module scope. A fixture that starts that patch on `cqc_lem.app.engagement.posting` and THEN
    imports the app hands `main` the mock for the rest of the session — `p.stop()` restores the
    defining module and cannot reach the copy `main` already took.

    What it sees, honestly: a leak written at module scope is caught whatever the order, because
    pytest imports every collected module before it runs anything. A leak a FIXTURE leaves behind is
    only caught when the offending file runs before this one in this process — and under
    `--dist loadfile` "this process" is one xdist worker's share of the files. So it is a tripwire
    over the lane, not a proof about every file in it; the scan above is the order-independent half.
    """

    @pytest.mark.parametrize("module_name", _api_modules())
    def test_no_api_module_attribute_is_a_leftover_mock(self, module_name: str) -> None:
        module = importlib.import_module(module_name)
        # `NonCallableMock` is the root every mock class here inherits from — Mock, MagicMock and
        # AsyncMock all answer this, so the check is a mechanism rather than a list of class names.
        leaked = sorted(name for name, value in vars(module).items()
                        if isinstance(value, NonCallableMock))
        assert not leaked, (
            f"{module_name} is holding mocks left by a module-scope patch that ran before the app "
            f"was imported: {leaked}. Patch a symbol where the handler READS it, for the length of "
            "the request (#1194, #1214)."
        )


class TestDependencyOverridesCompose:
    """#1212 made `app.dependency_overrides` the supported way to state what a test needs.

    `_require_api_and_admin` is the one real `Depends()` in the tree — every `/api/admin/test/*`
    route takes it — so it is what proves an override installed by a test actually reaches a
    handler through this fixture, and that the next test does not inherit it.
    """

    def test_an_override_installed_by_a_test_reaches_the_handler(self, api_client) -> None:
        from cqc_lem.api.main import app
        from cqc_lem.api.routers.admin import _require_api_and_admin

        refused = api_client.post("/api/admin/test/comment", params={"user_id": 7})

        app.dependency_overrides[_require_api_and_admin] = lambda: None
        allowed = api_client.post("/api/admin/test/comment", params={"user_id": 7})

        assert refused.status_code == 403, "the admin gate was already open — nothing to override"
        assert allowed.status_code != 403, "the override never reached the handler"

    def test_the_next_test_does_not_inherit_it(self, api_client) -> None:
        """Runs after the test above and must see a clean map — the leak this fixture prevents."""
        from cqc_lem.api.main import app

        assert app.dependency_overrides == {}, (
            "an override survived the previous test; api_client is supposed to restore the map it "
            "was handed"
        )
