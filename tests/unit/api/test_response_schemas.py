"""A documented `detail` payload describes the handler and changes nothing on the wire (#1446).

`ResponseModel[T]` is parametrized with CONTAINER types because FastAPI serializes THROUGH the
annotation — a named-field model there drops every key it does not declare. So a narrowed payload
is documented with `responses={200: {"model": ResponseModel[X]}}`, which FastAPI uses for the
schema and never for serialization.

That technique is only safe while two things hold, and both are silent when they break:

1. **The bytes are unchanged.** If `responses=` ever started filtering, the SPA would lose keys to
   a docs change — so a key no model declares is proven to survive a real request.
2. **The model describes the handler.** A documented field the handler never returns is worse than
   an undocumented one: the SPA generates a type from it and reads `undefined`. So each model's
   field set is derived from the source of truth the handler reads — the stored columns, or the
   literal dict it returns — rather than eyeballed.
"""

import ast
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit

_USER_ROUTER = (Path(__file__).resolve().parents[3] / "src" / "cqc_lem" / "api" / "routers"
                / "user.py")
_SESSION = "tok"
_USER = 5


def _handler(name: str) -> ast.FunctionDef:
    """The AST of one handler in `api/routers/user.py`."""
    tree = ast.parse(_USER_ROUTER.read_text())
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"{name} is not in {_USER_ROUTER.name}")


def _subscript_assignments(node: ast.AST, target: str) -> set:
    """Every literal key assigned as `target["key"] = ...` inside `node`."""
    keys = set()
    for child in ast.walk(node):
        if not isinstance(child, ast.Assign):
            continue
        for tgt in child.targets:
            if (isinstance(tgt, ast.Subscript) and isinstance(tgt.value, ast.Name)
                    and tgt.value.id == target and isinstance(tgt.slice, ast.Constant)):
                keys.add(tgt.slice.value)
    return keys


class TestDocumentingAPayloadDoesNotFilterIt:
    """The whole technique rests on this. Proven against a probe app AND the real routes."""

    def test_a_responses_model_documents_the_operation_without_becoming_its_response_model(self):
        """The mechanism, on a probe app: `responses=` sets the schema, the annotation still serializes.

        The pass-through itself is proven against the REAL routes below — a second `TestClient`
        over a second app is what #1214 took out of this suite.
        """
        from fastapi import FastAPI
        from pydantic import BaseModel

        from cqc_lem.api.models import ResponseModel

        class Documented(BaseModel):
            declared: int

        probe = FastAPI()

        @probe.get("/probe", responses={200: {"model": ResponseModel[Documented]}})
        def _probe() -> ResponseModel[dict[str, Any]]:
            return ResponseModel(status_code=200, detail={"declared": 1, "undeclared": "kept"})

        assert (probe.openapi()["paths"]["/probe"]["get"]["responses"]["200"]["content"]
                ["application/json"]["schema"]["$ref"].endswith("ResponseModel_Documented_"))
        # The route still serializes through its annotation — the container type, which keeps every
        # key. `response_model` is what FastAPI filters with, and it is not the documented model.
        route = next(r for r in probe.routes if getattr(r, "path", None) == "/probe")
        assert route.response_model == ResponseModel[dict[str, Any]]

    def test_the_engagement_preferences_payload_is_passed_through_key_for_key(self, api_client):
        """The real route: a stored column the model does not know about still reaches the SPA."""
        stored = {"tone": "warm", "comment_length": "short", "a_column_shipped_after_this_test": 7}
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_USER), \
             patch("cqc_lem.api.routers.user.has_engagement_preferences", return_value=True), \
             patch("cqc_lem.api.routers.user.get_engagement_preferences", return_value=dict(stored)):
            resp = api_client.get(f"/api/user/engagement-preferences?session_token={_SESSION}")
        assert resp.status_code == 200
        detail = resp.json()["detail"]
        for key, value in stored.items():
            assert detail[key] == value
        # And the read-only context is still added on top, unchanged by the documentation.
        assert detail["has_saved_preferences"] is True
        assert set(detail["gate_defaults"]) == {"authenticity_score_min", "post_similarity_max_pct"}

    def test_the_user_settings_payload_is_passed_through(self, api_client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_USER), \
             patch("cqc_lem.api.routers.user.get_user_subscription_info", return_value=None), \
             patch("cqc_lem.api.routers.user.get_user_preferences",
                   return_value={"last_login_inactivate_delay": 30, "auto_schedule_posts": 1,
                                 "content_language": "en", "content_buffer_days": 4,
                                 "content_buffer_max_posts": 6}), \
             patch("cqc_lem.api.routers.user.get_user_blog_url", return_value="https://b.example"), \
             patch("cqc_lem.api.routers.user.get_user_sitemap_url", return_value=None), \
             patch("cqc_lem.api.routers.user.get_company_linked_in_url_for_user", return_value=None), \
             patch("cqc_lem.api.routers.user.get_user_content_language", return_value="en"):
            resp = api_client.get(f"/api/user/settings?session_token={_SESSION}")
        assert resp.status_code == 200
        detail = resp.json()["detail"]
        assert detail["subscription"] is None
        assert detail["blog_url"] == "https://b.example"
        assert detail["preferences"]["content_buffer_days"] == 4
        assert detail["preferences"]["effective_content_language"] == "en"


class TestTheModelsDescribeTheHandlers:
    def test_the_put_body_carries_exactly_the_stored_columns(self):
        """`EngagementPreferencesDetail` is derived from the PUT body, so this is what grounds it.

        It is also the check that catches a column added to the DB and to the read but never to
        the write — the shape of bug that made a partial save reset a whole row (#639).
        """
        from cqc_lem.api.routers.user import EngagementPreferencesRequest
        from cqc_lem.utilities import db

        assert set(EngagementPreferencesRequest.model_fields) - {"session_token"} == set(
            db._ENGAGEMENT_DEFAULTS)

    def test_the_documented_detail_is_the_row_plus_exactly_what_the_handler_adds(self):
        """Read off the handler's own `prefs[...] = ` assignments, not off a list kept by hand."""
        from cqc_lem.api.routers.user import EngagementPreferencesDetail
        from cqc_lem.utilities import db

        added = _subscript_assignments(_handler("get_engagement_preferences_endpoint"), "prefs")
        assert added, "the walk found no read-only extras — it is not looking at the handler"
        assert set(EngagementPreferencesDetail.model_fields) == set(db._ENGAGEMENT_DEFAULTS) | added

    def test_the_settings_model_matches_the_dict_the_handler_returns(self):
        """`GET /user/settings` returns one literal, so its keys are readable from the source."""
        from cqc_lem.api.response_schemas import (
            SubscriptionSummary,
            UserPreferencesDetail,
            UserSettingsDetail,
        )

        node = _handler("get_user_settings")
        returned = [n for n in ast.walk(node)
                    if isinstance(n, ast.Return) and isinstance(n.value, ast.Call)]
        detail = next(kw.value for kw in returned[-1].value.keywords if kw.arg == "detail")
        assert isinstance(detail, ast.Dict)

        def _keys(dict_node: ast.Dict) -> set:
            return {k.value for k in dict_node.keys if isinstance(k, ast.Constant)}

        def _nested(dict_node: ast.Dict, key: str) -> ast.Dict:
            value = next(v for k, v in zip(dict_node.keys, dict_node.values)
                         if isinstance(k, ast.Constant) and k.value == key)
            # Each sub-object is `{...} if <row> else None` — the model is the `if` branch.
            return value.body if isinstance(value, ast.IfExp) else value

        assert _keys(detail) == set(UserSettingsDetail.model_fields)
        assert _keys(_nested(detail, "subscription")) == set(SubscriptionSummary.model_fields)
        assert _keys(_nested(detail, "preferences")) == set(UserPreferencesDetail.model_fields)


class TestThePublishedSchemaUsesThem:
    """Without this, a `responses=` block could be dropped and only the SPA types would notice."""

    @pytest.mark.parametrize("path,component", [
        ("/api/user/settings", "ResponseModel_UserSettingsDetail_"),
        ("/api/user/engagement-preferences", "ResponseModel_EngagementPreferencesDetail_"),
    ])
    def test_the_operation_documents_its_narrowed_payload(self, api_client, path, component):
        schema = api_client.get("/api/openapi.json").json()
        ref = (schema["paths"][path]["get"]["responses"]["200"]["content"]["application/json"]
               ["schema"]["$ref"])
        assert ref == f"#/components/schemas/{component}"
        detail = schema["components"]["schemas"][component]["properties"]["detail"]
        assert detail.get("$ref") or detail.get("allOf"), detail

    def test_a_redis_backed_payload_still_allows_the_keys_it_does_not_declare(self, api_client):
        """`feed_reach` / `gmail_forward_confirmation` are written elsewhere and grow keys.

        `additionalProperties` is what stops the generated TypeScript from claiming the list is
        closed — the documented fields are the ones a client may rely on, not the whole record.
        """
        schema = api_client.get("/api/openapi.json").json()
        for name in ("FeedReach", "GmailForwardConfirmation"):
            assert schema["components"]["schemas"][name].get("additionalProperties") is True, name
