"""The surface drift guard: per scope and per verb, what the scope GRANTS equals what its tools CALL.

`docs/agent-mcp-plan.md` §3.2 ("the set of `(verb, path)` grants per scope must equal the tool
registry's set for that scope, in both directions"). P2 shipped the grants; P3 ships the registry, so
the two are tied here:

- a grant no tool calls is surface an MCP token can reach for no reason, so it fails the build;
- a call no grant covers is a tool that would be refused at its first inner request, so it fails too.

Templated grants (`_SCOPE_PATH_PATTERN_METHODS`) are compared by the registry's own template
literal (`/posts/{post_id}`), which the pattern must match the way it matches a real request.
"""

import re
from typing import Any

import pytest

from cqc_lem.api.mcp import tools as registry
from cqc_lem.api.mcp.loopback import route_path

pytestmark = pytest.mark.unit

MCP_SCOPES = ("mcp", "mcp_outreach")


@pytest.fixture(scope="module")
def main_mod() -> Any:
    from cqc_lem.api import main

    return main


def _granted(main_mod: Any, scope: str) -> set[tuple[str, str]]:
    """Every literal `(verb, path)` the scope is granted."""
    return {(verb, path) for path, verbs in main_mod._SCOPE_PATH_METHODS[scope].items()
            for verb in verbs}


def _pattern_granted(main_mod: Any, scope: str, verb: str, path: str) -> bool:
    return any(pattern.fullmatch(_concrete(path)) and verb in verbs
               for pattern, verbs in main_mod._SCOPE_PATH_PATTERN_METHODS.get(scope, ()))


@pytest.mark.parametrize("scope", MCP_SCOPES)
def test_every_grant_is_called_by_a_tool(main_mod: Any, scope: str) -> None:
    unused = _granted(main_mod, scope) - registry.registry_grants(scope)
    assert unused == set(), f"{scope} grants paths no tool calls: {sorted(unused)}"


def _concrete(path: str) -> str:
    """A registry template with each `{param}` filled by a sample id, as a real call would be."""
    return re.sub(r"\{[^}]+\}", "12", path)


@pytest.mark.parametrize("scope", MCP_SCOPES)
def test_every_templated_grant_is_called_by_a_tool(main_mod: Any, scope: str) -> None:
    templated = [call for call in registry.registry_grants(scope) if "{" in call[1]]
    for pattern, verbs in main_mod._SCOPE_PATH_PATTERN_METHODS.get(scope, ()):
        assert any(pattern.fullmatch(_concrete(path)) and verb in verbs
                   for verb, path in templated), f"{scope} grants {pattern.pattern} but no tool calls it"


# Paths a templated grant must never open: siblings, deeper paths, and non-numeric ids.
_MUST_NOT_MATCH = ("/posts/bulk_update", "/posts/abc", "/posts/12/x", "/posts/", "/posts/12a",
                   "/posts/-1", "/posts/1.5", "/posts/%31")


@pytest.mark.parametrize("scope", MCP_SCOPES)
def test_no_templated_grant_is_over_broad(main_mod: Any, scope: str) -> None:
    """Each pattern accepts a numeric id and nothing a looser `[^/]+` or `.*` would also let in."""
    for pattern, _verbs in main_mod._SCOPE_PATH_PATTERN_METHODS.get(scope, ()):
        assert "[0-9]+" in pattern.pattern and "[^/]" not in pattern.pattern
        assert ".*" not in pattern.pattern and ".+" not in pattern.pattern
        for path in _MUST_NOT_MATCH:
            assert not pattern.fullmatch(path), f"{pattern.pattern} opens {path}"
        assert pattern.fullmatch("/posts/12")


@pytest.mark.parametrize("scope", MCP_SCOPES)
def test_every_tool_call_is_granted(main_mod: Any, scope: str) -> None:
    literal = _granted(main_mod, scope)
    missing = {(verb, path) for verb, path in registry.registry_grants(scope)
               if (verb, path) not in literal and not _pattern_granted(main_mod, scope, verb, path)}
    assert missing == set(), f"{scope} tools call paths it is not granted: {sorted(missing)}"


@pytest.mark.parametrize("scope", MCP_SCOPES)
def test_the_resolver_agrees_for_every_tool_call(main_mod: Any, scope: str) -> None:
    """The real resolver, not the tables: each call is allowed on its verb and refused on others."""
    for verb, path in registry.registry_grants(scope):
        concrete = path.replace("{post_id}", "12")
        assert main_mod._scope_allows(scope, f"/api{concrete}", verb), (scope, verb, path)
        for other in ("POST", "PUT", "PATCH", "DELETE"):
            assert not main_mod._scope_allows(scope, f"/api{concrete}", other), (scope, other, path)


def test_mcp_cannot_reach_the_outreach_only_calls(main_mod: Any) -> None:
    extra = registry.registry_grants("mcp_outreach") - registry.registry_grants("mcp")
    assert extra, "mcp_outreach must add something"
    for verb, path in extra:
        assert not main_mod._scope_allows("mcp", f"/api{path}", verb), path


def test_the_post_pattern_crosses_no_segment(main_mod: Any) -> None:
    for scope in MCP_SCOPES:
        assert main_mod._scope_allows(scope, "/api/posts/12", "GET")
        assert not main_mod._scope_allows(scope, "/api/posts/12/extra", "GET")
        assert not main_mod._scope_allows(scope, "/api/posts/12", "DELETE")
        assert not main_mod._scope_allows(scope, "/api/posts/bulk_update", "POST")


def test_every_tool_call_resolves_to_a_real_get_route(main_mod: Any) -> None:
    """A call whose path is no GET route would 404 (or 307) on every use."""
    templates = {main_mod._scope_path(r.path) for r in main_mod._walk_routes(main_mod.app.routes)
                 if "GET" in (getattr(r, "methods", None) or ())}
    for spec in registry.TOOLS:
        for _verb, path in spec.calls:
            assert path in templates, (spec.name, path)
            if "{" not in path:
                assert route_path(path).startswith("/api/"), path
