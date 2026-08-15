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
import inspect
import re
import textwrap
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit

_API = Path(__file__).resolve().parents[3] / "src" / "cqc_lem" / "api"
_USER_ROUTER = _API / "routers" / "user.py"
_MAIN = _API / "main.py"
_SESSION = "tok"
_USER = 5


def _handler(name: str, module: Path = _USER_ROUTER) -> ast.FunctionDef:
    """The AST of one handler, by default in `api/routers/user.py`."""
    tree = ast.parse(module.read_text())
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"{name} is not in {module.name}")


def _dict_literals(node: ast.AST) -> list:
    """Every literal `{...}` inside `node`, outermost-first, as sets of its constant keys."""
    return [{k.value for k in child.keys if isinstance(k, ast.Constant)}
            for child in ast.walk(node) if isinstance(child, ast.Dict)]


def _returned_detail(name: str, module: Path = _USER_ROUTER) -> set:
    """The keys of the `detail={...}` literal one handler's LAST `return` hands the envelope."""
    node = _handler(name, module)
    returns = [n for n in ast.walk(node)
               if isinstance(n, ast.Return) and isinstance(n.value, ast.Call)]
    detail = next(kw.value for kw in returns[-1].value.keywords if kw.arg == "detail")
    assert isinstance(detail, ast.Dict), f"{name} does not return a literal detail"
    return {k.value for k in detail.keys if isinstance(k, ast.Constant)}


def _select_columns(func) -> set:
    """The columns one reader's first `SELECT ... FROM` names, read off its own source.

    Python concatenates adjacent string literals at parse time, so a statement written across
    several quoted lines arrives here as ONE constant. A reader whose SQL is built from a module
    constant instead (an f-string) is compared against that constant directly — there is nothing to
    parse and nothing that could drift.
    """
    tree = ast.parse(textwrap.dedent(inspect.getsource(func)))
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "execute" and node.args
                and isinstance(node.args[0], ast.Constant)):
            head = re.search(r"SELECT\s+(.*?)\s+FROM", node.args[0].value, re.S | re.I)
            if head:
                return {c.strip().strip("`") for c in head.group(1).split(",")}
    raise AssertionError(f"no literal SELECT found in {func.__name__} — it is not being read")


def _row_literal(name: str, module: Path = _USER_ROUTER) -> set:
    """The keys of the `{...}` one handler builds per ROW inside its first list comprehension."""
    node = _handler(name, module)
    comp = next(n for n in ast.walk(node) if isinstance(n, ast.ListComp))
    assert isinstance(comp.elt, ast.Dict), f"{name}'s comprehension does not build a literal row"
    return {k.value for k in comp.elt.keys if isinstance(k, ast.Constant)}


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


class TestAnAlwaysReturnedKeyIsDocumentedAsREQUIRED:
    """`= None` documents "may be ABSENT", which is a different claim from "may be null".

    Both narrowed handlers write every one of their keys unconditionally — the settings literal
    sets all five (a missing row makes the VALUE null), and the engagement handler assigns each
    read-only extra on both branches of its own try/except. Documenting one of those as optional
    generates `key?:` in the SPA, and the whole-row PUT behind these payloads turns a dropped key
    into a reset column — the failure this generation pipeline exists because of (#1446).

    Genuinely partial payloads are the Redis records, and they say so with `extra="allow"`.
    """

    def test_the_settings_models_require_every_key_the_handler_writes(self):
        from cqc_lem.api.response_schemas import (
            SubscriptionSummary,
            UserPreferencesDetail,
            UserSettingsDetail,
        )

        for model in (UserSettingsDetail, UserPreferencesDetail, SubscriptionSummary):
            optional = [n for n, f in model.model_fields.items() if not f.is_required()]
            assert not optional, (
                f"{model.__name__} documents {optional} as optional, but the handler's literal "
                f"always writes them — a nullable key is `Optional[X]` with NO default")

    def test_the_engagement_detail_requires_every_key_including_the_read_only_extras(self):
        """The extras are the half that is easy to get wrong — they are added, not selected."""
        from cqc_lem.api.routers.user import EngagementPreferencesDetail

        added = _subscript_assignments(_handler("get_engagement_preferences_endpoint"), "prefs")
        optional = [n for n, f in EngagementPreferencesDetail.model_fields.items()
                    if not f.is_required()]
        assert not optional, f"{optional} are documented optional but the handler always sets them"
        assert added <= set(EngagementPreferencesDetail.model_fields)

    def test_a_redis_backed_record_is_still_allowed_to_be_partial(self):
        """Anti-overreach: the rule above is about keys a HANDLER writes, not every model here."""
        from cqc_lem.api.response_schemas import FeedReach, GmailForwardConfirmation

        assert not GmailForwardConfirmation.model_fields["source"].is_required()
        assert not FeedReach.model_fields["roster_commented"].is_required()


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


# ── Phase 3 (#1538): the rest of the account page, plus the dashboard and the Studio ─────────────
#
# Same contract as everything above — the model DOCUMENTS the payload and never serializes it — so
# each one is grounded the same way: its field set is derived from what the handler really reads
# (the SELECT it runs, the module constant it defaults to, the literal it returns), never eyeballed.

_NARROWED_GET_PATHS = (
    "/api/user/engagement-targets",
    "/api/user/story-bank",
    "/api/user/dm-templates",
    "/api/user/newsletter-settings",
    "/api/user/newsletter-subscribers",
    "/api/user/newsletter-draft",
    "/api/user/groups",
    "/api/user/group-post-draft",
    "/api/user/lead-magnet",
    "/api/dashboard/stats/",
    "/api/dashboard/planned-tasks/",
    "/api/activity/",
    "/api/posts/",
)


class TestTheAccountPayloadsDescribeTheirHandlers:
    """Nine payloads the SPA's account page reads, each derived from its own source of truth."""

    def test_a_roster_row_is_the_columns_the_reader_selects(self):
        from cqc_lem.api.response_schemas import EngagementTarget
        from cqc_lem.utilities import db

        assert set(EngagementTarget.model_fields) == set(db._ENGAGEMENT_TARGET_COLS)

    def test_a_suggestion_is_the_narrower_dict_the_seeder_builds(self):
        """Anti-overreach: a suggestion has never been stored, so it has no id and no counters."""
        from cqc_lem.api.response_schemas import EngagementTarget, EngagementTargetSuggestion
        from cqc_lem.utilities import db

        built = _dict_literals(ast.parse(textwrap.dedent(
            inspect.getsource(db.suggest_engagement_targets))))
        assert set(EngagementTargetSuggestion.model_fields) == built[0]
        assert set(EngagementTargetSuggestion.model_fields) < set(EngagementTarget.model_fields)

    def test_the_roster_payload_is_the_two_keys_the_handler_returns(self):
        from cqc_lem.api.response_schemas import EngagementTargetsDetail

        assert (set(EngagementTargetsDetail.model_fields)
                == _returned_detail("get_engagement_targets_endpoint"))

    def test_a_story_entry_is_the_columns_the_reader_selects(self):
        from cqc_lem.api.response_schemas import StoryEntry
        from cqc_lem.utilities import db

        assert set(StoryEntry.model_fields) == set(db._STORY_BANK_COLS)

    def test_the_story_bank_payload_is_what_the_handler_returns(self):
        from cqc_lem.api.response_schemas import StoryBankDetail

        assert set(StoryBankDetail.model_fields) == _returned_detail("get_story_bank_endpoint")

    def test_a_dm_template_is_the_columns_the_reader_selects(self):
        from cqc_lem.api.response_schemas import DmTemplate
        from cqc_lem.utilities.db import get_dm_templates

        assert set(DmTemplate.model_fields) == _select_columns(get_dm_templates)

    def test_the_newsletter_settings_payload_is_the_defaults_dict(self):
        """A user with no row gets `_NEWSLETTER_DEFAULTS` back verbatim, so it IS the field list."""
        from cqc_lem.api.response_schemas import NewsletterSettingsDetail
        from cqc_lem.platform.db.repositories import newsletter
        from cqc_lem.utilities.db import get_newsletter_settings

        assert set(NewsletterSettingsDetail.model_fields) == set(newsletter._NEWSLETTER_DEFAULTS)
        # …and the row path selects exactly the same columns, which is what makes them one answer.
        assert set(NewsletterSettingsDetail.model_fields) == _select_columns(get_newsletter_settings)

    def test_the_subscriber_series_is_the_columns_the_reader_selects(self):
        from cqc_lem.api.response_schemas import NewsletterSubscriberStat
        from cqc_lem.utilities.db import get_newsletter_subscriber_stats

        assert (set(NewsletterSubscriberStat.model_fields)
                == _select_columns(get_newsletter_subscriber_stats))

    def test_the_cta_attribution_is_the_dict_the_counter_builds(self):
        from cqc_lem.api.response_schemas import ArtifactCtaAttribution
        from cqc_lem.utilities import db

        built = _dict_literals(ast.parse(textwrap.dedent(
            inspect.getsource(db.count_artifact_cta_deliveries))))
        assert set(ArtifactCtaAttribution.model_fields) == built[0]

    def test_the_subscribers_payload_is_what_the_handler_returns(self):
        from cqc_lem.api.response_schemas import NewsletterSubscribersDetail

        assert (set(NewsletterSubscribersDetail.model_fields)
                == _returned_detail("get_newsletter_subscribers_endpoint"))

    def test_an_edition_is_the_selected_columns_with_the_cover_PATH_swapped_for_a_URL(self):
        """The one payload whose field list is not the SELECT: the handler pops the server path."""
        from cqc_lem.api.response_schemas import NewsletterEdition
        from cqc_lem.utilities.db import get_pending_newsletter_editions

        selected = _select_columns(get_pending_newsletter_editions)
        assert "cover_image_path" in selected, "the reader no longer selects the path this swaps"
        added = _subscript_assignments(_handler("get_newsletter_draft_endpoint"), "e")
        assert "cover_image_url" in added
        assert set(NewsletterEdition.model_fields) == (selected - {"cover_image_path"}) | added

    def test_the_newsletter_draft_payload_is_what_the_handler_returns(self):
        from cqc_lem.api.response_schemas import NewsletterDraftDetail

        assert (set(NewsletterDraftDetail.model_fields)
                == _returned_detail("get_newsletter_draft_endpoint"))

    def test_a_group_row_is_the_selected_columns_plus_the_key_the_handler_marks_on_it(self):
        from cqc_lem.api.response_schemas import UserGroup
        from cqc_lem.utilities.db import get_user_groups

        added = _subscript_assignments(_handler("get_user_groups_endpoint"), "g")
        assert added == {"is_next_post"}
        assert set(UserGroup.model_fields) == _select_columns(get_user_groups) | added

    def test_the_group_post_draft_is_the_stored_row_plus_what_the_handler_adds(self):
        from cqc_lem.api.response_schemas import GroupPostDraftDetail
        from cqc_lem.utilities import db

        stored = {c.strip() for c in db._GROUP_POST_DRAFT_COLUMNS.split(",")}
        added = _subscript_assignments(_handler("get_group_post_draft_endpoint"), "draft")
        assert added, "the walk found no derived keys — it is not looking at the handler"
        assert set(GroupPostDraftDetail.model_fields) == stored | added

    def test_the_lead_magnet_payload_is_the_defaults_dict(self):
        from cqc_lem.api.response_schemas import LeadMagnetDetail
        from cqc_lem.platform.db.repositories import outreach

        assert set(LeadMagnetDetail.model_fields) == set(outreach._LEAD_MAGNET_DEFAULTS)


class TestTheDashboardAndStudioPayloadsDescribeTheirHandlers:
    """The four `ApiEnvelope<...>` call sites outside the account page (#1538)."""

    def test_the_dashboard_counters_are_the_dict_the_aggregate_returns(self):
        from cqc_lem.api.response_schemas import DashboardStats
        from cqc_lem.utilities.db import get_dashboard_counts

        built = _dict_literals(ast.parse(textwrap.dedent(inspect.getsource(get_dashboard_counts))))
        assert set(DashboardStats.model_fields) == built[0]

    def test_a_planned_task_is_the_row_the_handler_re_emits(self):
        """The handler rebuilds each task to stamp an explicit-UTC time, so ITS literal is the wire."""
        from cqc_lem.api.response_schemas import PlannedTask

        assert set(PlannedTask.model_fields) == _row_literal("get_planned_tasks_endpoint", _MAIN)

    def test_every_queue_the_planner_reads_builds_that_same_row(self):
        """Three queues (posts / DMs / newsletter editions) merge into one list — one shape or none."""
        from cqc_lem.api.response_schemas import PlannedTask
        from cqc_lem.utilities.db import get_planned_tasks

        built = _dict_literals(ast.parse(textwrap.dedent(inspect.getsource(get_planned_tasks))))
        assert len(built) == 3, f"expected one literal per queue, found {len(built)}"
        for shape in built:
            assert shape == set(PlannedTask.model_fields)

    def test_the_planned_tasks_payload_is_what_the_handler_returns(self):
        from cqc_lem.api.response_schemas import PlannedTasksDetail

        assert (set(PlannedTasksDetail.model_fields)
                == _returned_detail("get_planned_tasks_endpoint", _MAIN))

    def test_an_activity_row_is_the_dict_the_handler_serializes(self):
        from cqc_lem.api.response_schemas import ActivityEntry

        assert set(ActivityEntry.model_fields) == _row_literal("get_activity", _MAIN)

    def test_a_gate_finding_is_the_dict_its_only_builder_writes(self):
        from cqc_lem.api.response_schemas import GateFinding
        from cqc_lem.utilities.quality_gates import build_finding

        built = _dict_literals(ast.parse(textwrap.dedent(inspect.getsource(build_finding))))
        assert set(GateFinding.model_fields) == built[0]

    def test_a_post_row_is_the_dict_the_handler_selects_out_of_the_stored_one(self):
        from cqc_lem.api.response_schemas import PostSummary

        assert set(PostSummary.model_fields) == _row_literal("get_posts_for_email", _MAIN)

    def test_the_posts_page_is_what_the_handler_returns(self):
        from cqc_lem.api.response_schemas import PostsPage

        assert set(PostsPage.model_fields) == _returned_detail("get_posts_for_email", _MAIN)


class TestTheDocumentedVocabulariesAreTheStoredOnes:
    """A `Literal[...]` here is a second copy of a MySQL ENUM or a `StrEnum`, so it is pinned.

    Restating one is worth it — the generated TypeScript gets a real union instead of `string`,
    which is what lets the SPA's badge logic be checked at compile time — but only while the copy
    cannot drift from the vocabulary the writer uses.
    """

    @staticmethod
    def _options(model, field: str) -> set:
        from typing import Literal, Union, get_args, get_origin

        annotation = model.model_fields[field].annotation
        if get_origin(annotation) is Union:  # Optional[Literal[...]]
            annotation = next(a for a in get_args(annotation) if get_origin(a) is Literal)
        return set(get_args(annotation))

    def test_the_roster_vocabularies_match_the_columns_they_document(self):
        from cqc_lem.api.response_schemas import EngagementTarget
        from cqc_lem.platform.db.enums import ConnectStatus, FollowStatus
        from cqc_lem.utilities.db import (
            ENGAGEMENT_TARGET_CATEGORIES,
            ENGAGEMENT_TARGET_SOURCES,
        )

        assert self._options(EngagementTarget, "category") == set(ENGAGEMENT_TARGET_CATEGORIES)
        assert self._options(EngagementTarget, "source") == set(ENGAGEMENT_TARGET_SOURCES)
        assert self._options(EngagementTarget, "follow_status") == {str(s) for s in FollowStatus}
        assert self._options(EngagementTarget, "connect_status") == {str(s) for s in ConnectStatus}

    def test_the_suggestion_vocabularies_are_the_same_ones(self):
        from cqc_lem.api.response_schemas import EngagementTargetSuggestion
        from cqc_lem.utilities.db import (
            ENGAGEMENT_TARGET_CATEGORIES,
            ENGAGEMENT_TARGET_SOURCES,
        )

        assert (self._options(EngagementTargetSuggestion, "category")
                == set(ENGAGEMENT_TARGET_CATEGORIES))
        assert (self._options(EngagementTargetSuggestion, "source")
                == set(ENGAGEMENT_TARGET_SOURCES))

    def test_the_story_kinds_are_the_ones_the_writer_accepts(self):
        from cqc_lem.api.response_schemas import StoryEntry
        from cqc_lem.utilities.db import STORY_BANK_KINDS

        assert self._options(StoryEntry, "kind") == set(STORY_BANK_KINDS)

    def test_the_group_post_media_kinds_are_the_enum(self):
        from cqc_lem.api.response_schemas import GroupPostDraftDetail
        from cqc_lem.platform.db.enums import GroupPostMediaType

        assert (self._options(GroupPostDraftDetail, "media_type")
                == {str(m) for m in GroupPostMediaType})

    def test_the_cover_vocabularies_are_the_ones_the_cover_module_writes(self):
        from cqc_lem.api.response_schemas import NewsletterEdition
        from cqc_lem.utilities.newsletter_cover import (
            COVER_SOURCE_AI,
            COVER_SOURCE_UPLOAD,
            COVER_STATUS_APPROVED,
            COVER_STATUS_PENDING,
        )

        assert (self._options(NewsletterEdition, "cover_image_source")
                == {COVER_SOURCE_UPLOAD, COVER_SOURCE_AI})
        assert (self._options(NewsletterEdition, "cover_image_status")
                == {COVER_STATUS_PENDING, COVER_STATUS_APPROVED})

    def test_a_planned_task_kind_is_one_the_planner_actually_labels(self):
        from cqc_lem.api.response_schemas import PlannedTask
        from cqc_lem.utilities.db import get_planned_tasks

        source = textwrap.dedent(inspect.getsource(get_planned_tasks))
        labelled = set(re.findall(r'"kind":\s*"([A-Za-z]+)"', source))
        assert self._options(PlannedTask, "kind") == labelled


class TestTheNewPayloadsRequireEveryKeyTheirHandlersWrite:
    """None of these is a Redis record.

    Every one is a fixed SELECT or a literal the handler always writes, so an optional field here
    would be a `key?:` the SPA is allowed to drop.
    """

    @pytest.mark.parametrize("name", [
        "ActivityEntry", "ArtifactCtaAttribution", "DashboardStats", "DmTemplate",
        "EngagementTarget", "EngagementTargetSuggestion", "EngagementTargetsDetail", "GateFinding",
        "GroupPostDraftDetail", "LeadMagnetDetail", "NewsletterDraftDetail", "NewsletterEdition",
        "NewsletterSettingsDetail", "NewsletterSubscriberStat", "NewsletterSubscribersDetail",
        "PlannedTask", "PlannedTasksDetail", "PostSummary", "PostsPage", "StoryBankDetail",
        "StoryEntry", "UserGroup",
    ])
    def test_no_field_is_documented_as_droppable(self, name):
        from cqc_lem.api import response_schemas

        model = getattr(response_schemas, name)
        optional = [n for n, f in model.model_fields.items() if not f.is_required()]
        assert not optional, (
            f"{name} documents {optional} as optional, but the handler always writes them — "
            "a nullable key is `Optional[X]` with NO default")

    def test_none_of_them_quietly_allows_extras(self):
        """`extra='allow'` is the Redis exemption and these are not Redis records."""
        from cqc_lem.api import response_schemas

        for name in response_schemas.__all__:
            model = getattr(response_schemas, name)
            if not (isinstance(model, type) and hasattr(model, "model_config")):
                continue
            if name in ("FeedReach", "GmailForwardConfirmation"):
                continue
            assert model.model_config.get("extra") != "allow", name


class TestNothingNarrowedHereChangesTheWire:
    """`responses=` is documentation.

    `response_model` is what FastAPI would filter with — and it is still the CONTAINER annotation on
    every route this touched, which is why no byte moves.
    """

    @pytest.mark.parametrize("path", _NARROWED_GET_PATHS)
    def test_the_route_still_serializes_through_its_container_annotation(self, path):
        from cqc_lem.api import response_schemas
        from cqc_lem.api.main import _walk_routes, app

        # `/api` is an included router, so a flat loop over `app.routes` sees one opaque entry.
        route = next(r for r in _walk_routes(app.routes) if getattr(r, "path", None) == path)
        documented = {getattr(response_schemas, n) for n in response_schemas.__all__
                      if isinstance(getattr(response_schemas, n), type)}
        # `ResponseModel[X]`'s one field — the thing FastAPI would build the response out of.
        detail = route.response_model.model_fields["detail"].annotation
        assert detail not in documented, (
            f"{path} serializes through {detail}, so the documented payload would FILTER it")
        assert "Any" in str(detail), f"{path}'s payload annotation is narrowed to {detail}"

    def test_a_group_row_keeps_a_column_no_model_declares(self, api_client):
        stored = {"group_id": "g1", "group_name": "AI Leaders", "enabled": True,
                  "post_enabled": True, "last_posted_at": None,
                  "a_column_shipped_after_this_test": 7}
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_USER), \
             patch("cqc_lem.api.routers.user.get_user_groups", return_value=[dict(stored)]), \
             patch("cqc_lem.api.routers.user.get_next_group_for_post", return_value=None):
            resp = api_client.get(f"/api/user/groups?session_token={_SESSION}")
        assert resp.status_code == 200
        row = resp.json()["detail"][0]
        for key, value in stored.items():
            assert row[key] == value
        assert row["is_next_post"] is False

    def test_the_lead_magnet_payload_is_passed_through(self, api_client):
        stored = {"enabled": True, "keyword": "AUDIT", "message": "Here it is",
                  "a_column_shipped_after_this_test": "kept"}
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_USER), \
             patch("cqc_lem.api.routers.user.get_lead_magnet_settings", return_value=dict(stored)):
            resp = api_client.get(f"/api/user/lead-magnet?session_token={_SESSION}")
        assert resp.status_code == 200
        assert resp.json()["detail"] == stored


class TestThePublishedSchemaDocumentsThemAll:
    @pytest.mark.parametrize("path", _NARROWED_GET_PATHS)
    def test_the_operation_names_a_narrowed_payload(self, api_client, path):
        schema = api_client.get("/api/openapi.json").json()
        ref = (schema["paths"][path]["get"]["responses"]["200"]["content"]["application/json"]
               ["schema"]["$ref"])
        component = ref.rsplit("/", 1)[-1]
        assert component != "ResponseModel_dict_str__Any__", f"{path} is still an untyped object"
        detail = schema["components"]["schemas"][component]["properties"]["detail"]
        assert detail.get("$ref") or detail.get("allOf") or detail.get("items") or detail.get(
            "anyOf"), detail
