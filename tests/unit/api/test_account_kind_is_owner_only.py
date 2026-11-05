"""No machine role can set or change `users.account_kind`; only the owner's command-line tool can.

The operator may approve on an `internal` or `sandbox` account, and a `sandbox` account is dry-run by
construction. Both guarantees rest on the flag being set by the OWNER. If any route a machine session
reaches could write it, an operator could flag a real account `sandbox` (making its sends vanish) or
`internal` (making its approvals reach real people) — so the writer is pinned from four sides:

1. nothing under `api/` calls the writer or contains a statement that updates the column;
2. no route's request model has an `account_kind` field;
3. the generic user-update clause allow-list does not name it;
4. for every machine role, the routes it can reach hold no reference to the writer.
"""

import ast
import pathlib
from types import ModuleType
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_SRC = pathlib.Path(__file__).resolve().parents[3] / "src" / "cqc_lem"
_WRITER = "set_user_account_kind"
_ONLY_CALLER = "utilities/account_kind_admin.py"


@pytest.fixture(scope="module")
def main_mod() -> ModuleType:
    from cqc_lem.api import main

    return main


def _call_names(tree: ast.AST) -> set[str]:
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            names.add(f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else "")
    return names


class TestOnlyTheOwnerToolWritesTheColumn:
    def test_the_walk_reads_the_source_tree(self) -> None:
        assert len(list(_SRC.rglob("*.py"))) > 150

    def test_the_writer_has_exactly_one_caller(self) -> None:
        callers = []
        for path in _SRC.rglob("*.py"):
            if _WRITER in _call_names(ast.parse(path.read_text())):
                callers.append(str(path.relative_to(_SRC)))
        assert callers == [_ONLY_CALLER], callers

    def test_nothing_under_api_calls_it_or_updates_the_column(self) -> None:
        offenders = []
        for path in (_SRC / "api").rglob("*.py"):
            tree = ast.parse(path.read_text())
            statements = [n.value for n in ast.walk(tree)
                          if isinstance(n, ast.Constant) and isinstance(n.value, str)]
            if _WRITER in _call_names(tree) or any("SET account_kind" in v for v in statements):
                offenders.append(str(path.relative_to(_SRC)))
        assert not offenders, offenders

    def test_no_sql_outside_the_one_setter_updates_the_column(self) -> None:
        offenders = []
        for path in _SRC.rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                        and "SET account_kind" in node.value:
                    offenders.append(f"{path.relative_to(_SRC)}:{node.lineno}")
        assert offenders == [f"platform/db/repositories/users.py:{offenders[0].split(':')[1]}"], offenders

    def test_the_generic_user_update_allow_list_does_not_name_it(self) -> None:
        from cqc_lem.platform.db.repositories.users import _ALLOWED_USER_CLAUSES

        assert not [c for c in _ALLOWED_USER_CLAUSES if "account_kind" in c]


class TestNoRouteTakesItAsInput:
    def test_no_request_model_has_an_account_kind_field(self, main_mod: ModuleType) -> None:
        offenders = []
        for route in main_mod._walk_routes(main_mod.app.routes):
            for param in getattr(getattr(route, "dependant", None), "body_params", []) or []:
                fields = getattr(param.field_info.annotation, "model_fields", None) or {}
                if "account_kind" in fields or param.name == "account_kind":
                    offenders.append(f"{sorted(route.methods or [])} {route.path}")
            for param in getattr(getattr(route, "dependant", None), "query_params", []) or []:
                if param.name == "account_kind":
                    offenders.append(f"{route.path} (query)")
        assert not offenders, offenders

    def test_no_route_handler_calls_the_writer(self, main_mod: ModuleType) -> None:
        import inspect

        offenders = [f"{getattr(r, 'path', '?')} ({getattr(r.endpoint, '__name__', '?')})"
                     for r in main_mod._walk_routes(main_mod.app.routes)
                     if _WRITER in inspect.getsource(r.endpoint)]
        assert not offenders, offenders


class TestNoMachineRoleCanReachAWriter:
    @pytest.mark.parametrize("role", ["observer", "agent", "operator", "extension", "enroll"])
    def test_no_reachable_route_references_the_writer(self, main_mod: ModuleType, role: str) -> None:
        import inspect

        scope = getattr(main_mod, f"SESSION_SCOPE_{role.upper()}")
        offenders = []
        reachable = 0
        for route in main_mod._walk_routes(main_mod.app.routes):
            path = getattr(route, "path", None)
            if not path:
                continue
            for method in sorted(route.methods or ()):
                if not main_mod._scope_allows(scope, main_mod._scope_path(path), method):
                    continue
                reachable += 1
                if _WRITER in inspect.getsource(route.endpoint):
                    offenders.append(f"{role} {method} {path}")
        assert reachable, f"{role} reaches nothing — the check would pass vacuously"
        assert not offenders, offenders

    def test_a_machine_session_cannot_reach_the_user_update_route_with_the_field(
            self, api_client: Any, main_mod: ModuleType) -> None:
        """A request that smuggles the field in is ignored or refused, never written."""
        from unittest.mock import patch

        with patch(f"{main_mod.__name__}._db_resolve_session",
                   side_effect=lambda t: {"user_id": 5, "scope": "operator"}), \
             patch("cqc_lem.platform.db.repositories.users.set_user_account_kind") as write:
            resp = api_client.put("/api/user/", json={"session_token": "t", "account_kind": "sandbox"})
        assert resp.status_code in (403, 422, 400, 404, 405)
        write.assert_not_called()
