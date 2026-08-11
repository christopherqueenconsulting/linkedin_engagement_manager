"""`ResponseModel[T]` — the public schema has to say what a route actually returns (issue #1219).

`/api/openapi.json` is world-readable (#1020). With `detail: Any` every operation documented the
same empty object, so the published schema said nothing a client could generate a type from.

Two failure modes are pinned here, and both are silent:

1. **A new route lands with a bare `-> ResponseModel`.** Nothing breaks; the operation just goes
   back to documenting nothing, and the regression is invisible until somebody reads the schema.
2. **A handler grows a return whose shape its annotation does not allow.** FastAPI serializes the
   response THROUGH the annotation, so `ResponseModel[str]` returning a dict is a 500 in
   production and a passing test suite everywhere the new branch is not exercised.

The admin-hiding derivation is re-checked from here too: parametrizing the envelope changes what
`app.openapi()` builds, and `/api/admin/*` must stay out of it.
"""

import ast
import re
from pathlib import Path
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit

_API_DIR = Path(__file__).resolve().parents[3] / "src" / "cqc_lem" / "api"
_API_FILES = [_API_DIR / "main.py", *sorted((_API_DIR / "routers").glob("*.py"))]


@pytest.fixture(scope="module")
def schema():
    with patch("cqc_lem.utilities.observability.track_api_call"):
        from fastapi.testclient import TestClient

        from cqc_lem.api.main import app
        with TestClient(app, raise_server_exceptions=False) as tc:
            return tc.get("/api/openapi.json").json()


def _annotated_handlers():
    """Every function in the `/api` tree annotated `-> ResponseModel[...]`, with its payload type.

    Yields `(file, function, payload_source)` — `payload_source` is the type as written, because
    what this reads back is the source of truth for what FastAPI will serialize through.
    """
    for path in _API_FILES:
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            ann = node.returns
            if not (isinstance(ann, ast.Subscript) and isinstance(ann.value, ast.Name)
                    and ann.value.id == "ResponseModel"):
                continue
            yield path.name, node, ast.unparse(ann.slice)


class TestTheEnvelopeIsStillTheEnvelope:
    """Back-compat: bare `ResponseModel` is unchanged, on the wire and in the schema."""

    def test_unparametrized_still_takes_any_detail(self):
        from cqc_lem.api.models import ResponseModel

        for detail in ("a message", {"a": 1}, [1, 2], 7, None):
            assert ResponseModel(status_code=200, detail=detail).detail == detail

    def test_unparametrized_detail_is_still_unconstrained_in_the_schema(self):
        """Pydantic treats an unparametrized type variable as `Any`.

        If that ever stopped being true, every route still returning the bare envelope would start
        REJECTING its own payload.
        """
        from cqc_lem.api.models import ResponseModel

        detail = ResponseModel.model_json_schema()["properties"]["detail"]
        assert set(detail) <= {"title"}, detail

    def test_parametrizing_narrows_the_schema_without_touching_the_payload(self):
        from typing import Any

        from cqc_lem.api.models import ResponseModel

        assert ResponseModel[str].model_json_schema()["properties"]["detail"]["type"] == "string"
        payload = {"kept": 1, "also_kept": {"nested": True}}
        # No field filtering: a container type serializes what it was given, key for key.
        assert ResponseModel[dict[str, Any]](status_code=200, detail=payload).model_dump() == {
            "status_code": 200, "detail": payload}


class TestEveryPublishedOperationDocumentsItsPayload:
    def test_no_operation_still_points_at_the_any_envelope(self, schema):
        """The ratchet.

        A route added with a bare `-> ResponseModel` re-publishes the empty-`detail` component and
        fails here — annotate it (`ResponseModel[Any]` if it genuinely has no one shape, which at
        least says so on purpose).
        """
        published = re.findall(r'"#/components/schemas/([^"]+)"', repr(schema["paths"]))
        assert "ResponseModel" not in set(published), (
            "an operation documents `detail` as an unconstrained Any — the public schema tells a "
            "client nothing about what it returns"
        )

    def test_the_parametrized_envelopes_are_actually_in_the_schema(self, schema):
        """Anti-vacuity for the test above: it would also pass on a schema with no envelope at all."""
        envelopes = {n for n in schema["components"]["schemas"] if n.startswith("ResponseModel")}
        assert len(envelopes) >= 4, envelopes
        for name in envelopes:
            detail = schema["components"]["schemas"][name]["properties"]["detail"]
            assert detail.keys() - {"title"}, f"{name} documents nothing about its detail"

    def test_admin_operations_stayed_out_of_the_published_schema(self, schema):
        """Re-derived after the typing change, per #1219's watch item.

        Hiding is computed from the route table while `app.openapi()` is built, and parametrizing
        the envelope changes what that build produces.
        """
        from cqc_lem.api.main import _ADMIN_ROUTES_HIDDEN, _walk_routes, app

        assert not [p for p in schema["paths"] if p.startswith("/api/admin")]
        admin_ops = sum(len(getattr(r, "methods", None) or [])
                        for r in _walk_routes(app.routes)
                        if getattr(r, "path", "").startswith("/api/admin"))
        assert _ADMIN_ROUTES_HIDDEN == admin_ops >= 18, (
            f"{_ADMIN_ROUTES_HIDDEN} operations were hidden but the route table has {admin_ops}"
        )


class TestAnnotationsMatchWhatHandlersReturn:
    """A handler and its annotation drift apart in the source long before a request proves it.

    Only literal `detail=` values are judged — a `detail=some_call()` is checked by that callee's
    own type hints and by the endpoint tests, not from here. That keeps this check exact: every
    case it reports is one where the source ALREADY says the annotation is wrong.
    """

    @staticmethod
    def _literal_kind(node):
        if isinstance(node, ast.JoinedStr):
            return "str"
        if isinstance(node, ast.Constant):
            return {str: "str", bool: "bool", int: "int", float: "float",
                    type(None): "none"}.get(type(node.value))
        if isinstance(node, ast.Dict):
            return "dict"
        if isinstance(node, (ast.List, ast.ListComp)):
            return "list"
        return None

    @staticmethod
    def _accepts(payload_source, kind):
        payload = payload_source.replace(" ", "")
        if kind == "none":
            return "Optional[" in payload or "None" in payload
        head = payload.removeprefix("Optional[").split("[")[0]
        return {"str": "str", "int": "int", "float": "float", "bool": "bool",
                "dict": "dict", "list": "list"}[kind] == head

    def test_every_annotated_handler_is_found(self):
        """Anti-vacuity: an empty walk would make the check below assert nothing."""
        assert len(list(_annotated_handlers())) > 100

    def test_no_literal_return_contradicts_its_annotation(self):
        bad = []
        for filename, node, payload in _annotated_handlers():
            for inner in ast.walk(node):
                if not (isinstance(inner, ast.Return) and isinstance(inner.value, ast.Call)
                        and isinstance(inner.value.func, ast.Name)
                        and inner.value.func.id == "ResponseModel"):
                    continue
                detail = next((kw.value for kw in inner.value.keywords if kw.arg == "detail"), None)
                kind = self._literal_kind(detail)
                if kind and not self._accepts(payload, kind):
                    bad.append(f"{filename}:{inner.lineno} {node.name} returns a {kind} "
                               f"but is annotated ResponseModel[{payload}]")
        assert not bad, (
            "FastAPI serializes the response through the annotation, so each of these is a 500 on "
            "that branch: " + "; ".join(bad)
        )
