"""No test may patch a moved symbol on `api.main` and believe it did something (#1154).

The trap, in one sentence: **a re-export is a second binding.** Reading through a facade resolves the
same object; REBINDING one does not. So when a handler moves to `api/routers/<area>.py` and takes its
imports with it, a test still doing `patch("cqc_lem.api.main.get_avatar_training")` rebinds a name
nothing reads. The mock is never called, the real function runs, and the test passes.

Usually that fails loudly — `patch` raises `AttributeError` once the name is gone from `main`. The
dangerous case is a symbol that stays in `main` for handlers that did NOT move while also being
imported by the router: `get_user_subscription_info` is read by billing (stayed) and by the avatar
checkout (moved). Patching it on `main` still resolves, so nothing raises, and the assertion happens
to hold for an unrelated reason. That is exactly how `test_avatar_checkout_no_customer_400` passed
locally while asserting nothing: the unpatched call reached a dead database and 400'd anyway.

**Any test asserting a 4xx/5xx is the silent class here** — a broken dependency produces the same
status code as the condition under test. So this check is structural, not behavioural: it does not
ask whether a test passes, it asks whether the patch could possibly bind what the code reads.

`get_session_user_id` is the deliberate exception. Routers reach the auth kernel as an ATTRIBUTE of
the host module, resolved at request time, so patching it on `main` is CORRECT and must stay that way
— moving it would mean moving a 33-symbol closure carrying every documented auth invariant.
"""

import ast
import pathlib
import pkgutil

import pytest

pytestmark = pytest.mark.unit

# Reached through `_main.<name>` at request time, so `main` really is the module its readers bind to.
REACHED_THROUGH_THE_HOST_MODULE = {"get_session_user_id"}

_REPO = pathlib.Path(__file__).resolve().parents[3]


def _module_globals(path: pathlib.Path) -> set[str]:
    """Every name bound at module level — imported or defined. Those are what its functions read."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names |= {(a.asname or a.name).split(".")[0] for a in node.names}
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names |= {t.id for t in node.targets if isinstance(t, ast.Name)}
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


def _routers() -> list[tuple[str, str, set[str]]]:
    from cqc_lem.api import routers

    out = []
    for info in pkgutil.iter_modules(routers.__path__):
        path = _REPO / "src" / "cqc_lem" / "api" / "routers" / f"{info.name}.py"
        module = __import__(f"cqc_lem.api.routers.{info.name}", fromlist=["router"])
        prefix = getattr(getattr(module, "router", None), "prefix", None)
        if prefix:
            out.append((info.name, prefix, _module_globals(path) - REACHED_THROUGH_THE_HOST_MODULE))
    return out


def _tests_touching(prefix: str):
    """Test functions whose body mentions this router's prefix — i.e. exercise its routes."""
    for path in sorted((_REPO / "tests").rglob("test_*.py")):
        source = path.read_text(encoding="utf-8")
        if prefix not in source or "cqc_lem.api.main" not in source:
            continue
        lines = source.splitlines()
        tree = ast.parse(source)

        def walk(node, cls=None):
            for child in ast.iter_child_nodes(node):
                if isinstance(child, ast.ClassDef):
                    # `yield from`, not a bare call: `walk` is a generator, so calling it here
                    # builds one and throws it away — and since every test in this suite lives in a
                    # class, that made this whole check pass while reading nothing.
                    yield from walk(child, child.name)
                elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    body = "\n".join(lines[child.lineno - 1:child.end_lineno])
                    if prefix in body:
                        yield path, cls, child.name, body

        yield from walk(tree)


class TestNoTestPatchesAMovedSymbolOnMain:
    def test_there_are_routers_to_check(self):
        """Anti-vacuity: with no routers discovered, the check below passes having read nothing."""
        assert _routers(), "no router modules found — the seam check would be vacuous"

    def test_every_router_route_test_patches_the_module_its_code_reads(self):
        offenders = []
        for name, prefix, owned in _routers():
            for path, cls, test, body in _tests_touching(prefix):
                # Both spellings a patch target is written in here: the module alias most files
                # define (`_M = "cqc_lem.api.main"`) and the literal.
                wrong = sorted(
                    symbol for symbol in owned
                    if "{_M}." + symbol in body or "cqc_lem.api.main." + symbol in body
                )
                if wrong:
                    rel = path.relative_to(_REPO)
                    offenders.append(f"{rel}::{cls}::{test} patches {wrong} on main, "
                                     f"but routers/{name}.py is what reads them")
        assert offenders == [], "\n".join(offenders)
