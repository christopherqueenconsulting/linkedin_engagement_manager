"""The MCP identity carve-out (`docs/identity-and-sessions.md`, plan §3.2).

Every `/api` route learns its caller through `get_session_user_id()` (#914). `POST /api/mcp` is the
one exception: it is public at the edge and resolves its own bearer, because it must read only
`Authorization: Bearer` and answer JSON before any handler runs. `test_api_route_identity.py` skips
public paths and cannot `inspect.getsource` the gate's helpers, so this file pins the exception:

1. Only two functions under `src/cqc_lem/api/` call a session resolver directly: the kernel's
   `get_session_user_id` and the gate's `_authenticate`.
2. The gate admits only the two MCP scopes (the parametrised 401s live in `test_mcp_gate_auth.py`;
   here the rule is read from the code).
3. The gate imports its resolver through the `cqc_lem.utilities.db` facade.
4. `/api/mcp` is the only exact public path, and no public prefix covers it.
"""

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_API = Path(__file__).resolve().parents[4] / "src" / "cqc_lem" / "api"
_RESOLVERS = {"resolve_session", "resolve_session_checked", "_db_resolve_session",
              "_resolve_session_row"}
_ALLOWED = {("main.py", "get_session_user_id"), ("mcp/gate.py", "_authenticate")}


def _callers_of_resolvers() -> set[tuple[str, str]]:
    """Every (file, enclosing function) under `api/` that calls or references a resolver name."""
    found: set[tuple[str, str]] = set()
    for path in sorted(_API.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        rel = path.relative_to(_API).as_posix()
        for func in ast.walk(tree):
            if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(func):
                name = None
                if isinstance(node, ast.Name):
                    name = node.id
                elif isinstance(node, ast.Attribute):
                    name = node.attr
                if name in _RESOLVERS:
                    found.add((rel, func.name))
    return found


def test_the_walk_sees_the_api_tree() -> None:
    assert (_API / "main.py").is_file() and (_API / "mcp" / "gate.py").is_file()


def test_only_the_kernel_and_the_gate_resolve_a_session_directly() -> None:
    callers = _callers_of_resolvers()
    assert _ALLOWED <= callers, f"the walk no longer finds the two known resolvers: {callers}"
    assert callers - _ALLOWED == set(), (
        "A new direct session resolve under api/. Route identity goes through "
        "get_session_user_id(); only the MCP gate is carved out:\n"
        + "\n".join(f"  {f}::{fn}" for f, fn in sorted(callers - _ALLOWED)))


def test_the_gate_imports_its_resolver_through_the_db_facade() -> None:
    tree = ast.parse((_API / "mcp" / "gate.py").read_text(encoding="utf-8"))
    sources = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
               and any(a.name == "resolve_session_checked" for a in node.names)}
    assert sources == {"cqc_lem.utilities.db"}


def test_the_gate_admits_only_the_mcp_scopes() -> None:
    from cqc_lem.api.mcp import gate
    from cqc_lem.utilities.db import MCP_SESSION_SCOPES

    assert gate.MCP_SESSION_SCOPES is MCP_SESSION_SCOPES
    assert MCP_SESSION_SCOPES == frozenset({"mcp", "mcp_outreach"})
    source = ast.unparse(ast.parse((_API / "mcp" / "gate.py").read_text(encoding="utf-8")))
    assert "resolved.get('scope') not in MCP_SESSION_SCOPES" in source


def test_mcp_is_the_one_exact_public_path() -> None:
    from cqc_lem.api import main

    assert main._PUBLIC_API_EXACT == frozenset({"/api/mcp"})
    assert not any(p.rstrip("/") in ("/api", "/api/mcp") or "/api/mcp".startswith(p)
                   for p in main._PUBLIC_API_PREFIXES if p.endswith("/"))
