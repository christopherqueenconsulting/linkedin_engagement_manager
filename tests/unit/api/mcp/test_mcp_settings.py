"""`lem_update_settings` and `lem_undo_settings_change` end to end (epic #2797, #2800).

Every test posts JSON-RPC to `/api/mcp`, so each call runs the real gate, SDK, registry, write path,
loopback and the real route handlers with their MCP-seat guard. Only the handlers' own database
calls are patched, onto one in-memory `Store` that merges like the real upserts do, so "a one-field
change leaves every other field identical" is checked against state, not against a mock's arguments.
"""

import copy
import json
from contextlib import ExitStack, contextmanager
from typing import Any, Iterator
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

from cqc_lem.api.mcp import settings_policy as policy, tools as registry
from cqc_lem.utilities.db import AuthAuditEvent

from ._admitted import UID, admitted, call, rpc, structured

pytestmark = pytest.mark.unit

_U = "cqc_lem.api.routers.user"
_W = "cqc_lem.api.mcp.writes"
_CANARY = "zz-voice-canary-58"
TOOLS = ("lem_update_settings", "lem_undo_settings_change")


class Store:
    """The user's stored settings, with the merge semantics of the real upserts."""

    def __init__(self) -> None:
        from cqc_lem.api.routers.user import EngagementPreferencesRequest

        prefs = EngagementPreferencesRequest(session_token="-").model_dump(exclude={"session_token"})
        prefs.update({
            "tone": "warm", "comment_style": "plain", "business_goals": "grow the list",
            "include_topics": ["ai"], "min_reactions": 3, "max_post_age_hours": 48,
            # Everything an agent must never move: caps, modes, gates.
            "max_comments_per_day": 7, "max_dms_per_day": 4, "max_invites_per_day": 5,
            "max_follows_per_day": 2, "connection_request_mode": "pre_review",
            "catchup_touch_mode": "pre_review", "roster_auto_follow": True,
            "roster_auto_connect": True, "profile_viewer_dm_auto_send": True,
            "hold_repaired_posts_for_review": False, "authenticity_score_min": 61,
            "post_similarity_max_pct": 44, "nurture_dm_mode": "auto_approve",
            "icp_definition": {}, "icp_builder_enabled": False, "forbidden_claim_terms": ["roi"],
            "brand_kit": {"primary": "#112233"}, "session_windows": [], "session_timezone": "",
        })
        self.prefs: dict[str, Any] = prefs
        self.magnet: dict[str, Any] = {"enabled": True, "keyword": "GUIDE", "message": "Here you go"}
        self.newsletter: dict[str, Any] = {
            "enabled": True, "title": "Weekly", "topic": "old topic", "cadence": "weekly",
            "align_with_blog": True, "publish_day": 3, "publish_hour": 7, "generate_lead_days": 2,
            "max_queued_drafts": 4, "invite_connections_enabled": True, "max_invites_per_run": 9,
            "cover_image_auto": True, "auto_publish_newsletters": True,
            "newsletter_url": "https://example.com/n", "last_published_at": None}
        self.tz = "America/Chicago"
        self.prefs_writes: list[dict[str, Any]] = []

    def put_prefs(self, _user_id: int, changes: dict[str, Any]) -> bool:
        self.prefs_writes.append(dict(changes))
        self.prefs.update({k: v for k, v in changes.items() if k in self.prefs})
        return True

    def put_magnet(self, _user_id: int, settings: dict[str, Any]) -> bool:
        self.magnet.update(settings)
        return True

    def put_newsletter(self, _user_id: int, settings: dict[str, Any]) -> bool:
        self.newsletter.update({k: v for k, v in settings.items() if k in self.newsletter})
        return True

    def put_tz(self, _user_id: int, tz: str) -> bool:
        self.tz = tz
        return True


@pytest.fixture(autouse=True)
def enabled(monkeypatch: pytest.MonkeyPatch, fake_redis: Any) -> None:
    """The server, writes and settings flags on (env fallback), and the in-memory Redis."""
    monkeypatch.setenv("MCP_SERVER_ENABLED", "true")
    monkeypatch.setenv("MCP_SERVER_WRITES_ENABLED", "true")
    monkeypatch.setenv("MCP_SETTINGS_WRITES_ENABLED", "true")


@pytest.fixture
def audit() -> Iterator[MagicMock]:
    with patch(f"{_W}.record_auth_event", return_value=True) as mock:
        yield mock


@pytest.fixture
def store() -> Store:
    return Store()


@contextmanager
def _routes(store: Store) -> Iterator[None]:
    with ExitStack() as stack:
        enter = stack.enter_context
        enter(patch(f"{_U}.get_engagement_preferences", side_effect=lambda _u: copy.deepcopy(store.prefs)))
        enter(patch(f"{_U}.update_engagement_preferences", side_effect=store.put_prefs))
        enter(patch(f"{_U}.has_engagement_preferences", return_value=True))
        enter(patch(f"{_U}.max_catchup_touches_allowed", return_value=3))
        enter(patch("cqc_lem.app.engagement.feed.get_feed_funnel", return_value=None))
        enter(patch(f"{_U}.get_lead_magnet_settings", side_effect=lambda _u: dict(store.magnet)))
        enter(patch(f"{_U}.update_lead_magnet_settings", side_effect=store.put_magnet))
        enter(patch(f"{_U}.get_newsletter_settings", side_effect=lambda _u: dict(store.newsletter)))
        enter(patch(f"{_U}.update_newsletter_settings", side_effect=store.put_newsletter))
        enter(patch(f"{_U}.get_user_timezone", side_effect=lambda _u: store.tz))
        enter(patch(f"{_U}.update_user_timezone", side_effect=store.put_tz))
        enter(patch(f"{_U}._icp_definition_detail", return_value={}))
        yield


@pytest.fixture
def seat(resolver: MagicMock, audit: MagicMock, store: Store) -> Iterator[Store]:
    """An admitted `mcp` token on a standard account, over the in-memory store."""
    with admitted(resolver, "mcp"), patch(f"{_W}.get_user_account_kind", return_value="standard"), \
            _routes(store):
        yield store


def _update(client: Any, changes: dict[str, Any], **extra: Any) -> dict:
    return call(client, "lem_update_settings", {"changes": changes, **extra})


def _error(result: dict) -> dict:
    assert result["isError"] is True, result
    return structured(result)["error"]


class TestMergeNeverReplace:
    def test_a_one_field_change_leaves_every_other_stored_field_identical(
            self, api_client: Any, seat: Store) -> None:
        before = copy.deepcopy(seat.prefs)
        result = _update(api_client, {"tone": "direct"})
        assert result["isError"] is False, result
        assert seat.prefs["tone"] == "direct"
        assert {k: v for k, v in seat.prefs.items() if k != "tone"} == \
            {k: v for k, v in before.items() if k != "tone"}

    def test_the_route_is_sent_only_the_changed_key(self, api_client: Any, seat: Store) -> None:
        _update(api_client, {"tone": "direct"})
        assert seat.prefs_writes == [{"tone": "direct"}]

    def test_group_one_fields_in_every_resource_change_and_nothing_else_does(
            self, api_client: Any, seat: Store) -> None:
        prefs_before = copy.deepcopy(seat.prefs)
        newsletter_before = dict(seat.newsletter)
        result = _update(api_client, {
            "include_topics": ["ai", "sales"], "lead_magnet_keyword": "AUDIT",
            "newsletter_topic": "new topic", "newsletter_cadence": "monthly",
            "timezone": "Europe/Paris"})
        assert result["isError"] is False, result
        assert seat.prefs["include_topics"] == ["ai", "sales"]
        assert {k: v for k, v in seat.prefs.items() if k != "include_topics"} == \
            {k: v for k, v in prefs_before.items() if k != "include_topics"}
        assert seat.magnet == {"enabled": True, "keyword": "AUDIT", "message": "Here you go"}
        assert {k: v for k, v in seat.newsletter.items() if k not in ("topic", "cadence")} == \
            {k: v for k, v in newsletter_before.items() if k not in ("topic", "cadence")}
        assert (seat.newsletter["topic"], seat.newsletter["cadence"]) == ("new topic", "monthly")
        assert seat.tz == "Europe/Paris"

    def test_an_icp_change_goes_through_its_own_route(self, api_client: Any, seat: Store) -> None:
        icp = {"segments": [{"id": "ops", "label": "Ops leaders"}]}
        with patch(f"{_U}.update_engagement_preferences", side_effect=seat.put_prefs) as write:
            result = _update(api_client, {"icp_definition": icp})
        assert result["isError"] is False, result
        assert write.call_args.args[1].keys() == {"icp_definition"}
        assert structured(result)["changed"]["icp_definition"]["before"] == {}

    def test_an_agent_save_never_tops_up_newsletter_drafts(self, api_client: Any, seat: Store) -> None:
        with patch("cqc_lem.app.run_scheduler.generate_newsletter_drafts_for_user") as top_up:
            result = _update(api_client, {"newsletter_topic": "x"})
        assert result["isError"] is False, result
        top_up.apply_async.assert_not_called()

    def test_the_route_validators_still_clamp_and_the_diff_shows_the_stored_value(
            self, api_client: Any, seat: Store) -> None:
        result = _update(api_client, {"posts_per_week": 99})
        body = structured(result)
        assert body["changed"]["posts_per_week"]["after"] == seat.prefs["posts_per_week"] != 99


class TestTheAllowlist:
    @pytest.mark.parametrize("field,code,needle", [
        ("totally_unknown", "field_not_allowed", "lem_open_in_lem"),
        ("connection_request_mode", "needs_change_request", "needs a change request (coming)"),
        ("nurture_dm_mode", "needs_change_request", "needs a change request (coming)"),
        ("profile_viewer_dm_auto_send", "needs_change_request", "needs a change request (coming)"),
        ("roster_auto_connect", "needs_change_request", "needs a change request (coming)"),
        ("roster_auto_follow", "needs_change_request", "needs a change request (coming)"),
        ("hold_repaired_posts_for_review", "needs_change_request", "needs a change request"),
        ("authenticity_score_min", "needs_change_request", "needs a change request"),
        ("post_similarity_max_pct", "needs_change_request", "needs a change request"),
        ("auto_publish_newsletters", "needs_change_request", "needs a change request"),
        ("max_queued_drafts", "cap_not_editable", "lem_open_in_lem"),
        ("reply_sweeps_per_day", "cap_not_editable", "lem_open_in_lem"),
        ("max_invites_per_run", "cap_not_editable", "lem_open_in_lem"),
        ("linkedin_password", "field_never_editable", "lem_open_in_lem"),
        ("linkedin_cookie", "field_never_editable", "lem_open_in_lem"),
        ("email", "field_never_editable", "lem_open_in_lem"),
        ("location", "field_never_editable", "lem_open_in_lem"),
        ("billing", "field_never_editable", "lem_open_in_lem"),
    ])
    def test_a_field_outside_group_one_is_refused_naming_it(
            self, api_client: Any, seat: Store, field: str, code: str, needle: str) -> None:
        error = _error(_update(api_client, {field: 1}))
        assert error["code"] == code
        assert field in error["message"] and needle in error["message"]
        assert seat.prefs_writes == []

    def test_group_three_points_at_lem_too(self, api_client: Any, seat: Store) -> None:
        error = _error(_update(api_client, {"connection_request_mode": "auto_approve"}))
        assert "lem_open_in_lem" in error["message"]

    def test_one_refused_field_blocks_the_whole_call(self, api_client: Any, seat: Store) -> None:
        before = copy.deepcopy(seat.prefs)
        error = _error(_update(api_client, {"tone": "direct", "max_queued_drafts": 99}))
        assert error["code"] == "cap_not_editable"
        assert seat.prefs == before and seat.prefs_writes == []

    def test_every_refusal_is_named_not_just_the_first(self, api_client: Any, seat: Store) -> None:
        error = _error(_update(api_client, {"max_queued_drafts": 1, "roster_auto_follow": True}))
        assert "max_queued_drafts" in error["message"] and "roster_auto_follow" in error["message"]

    def test_a_value_the_route_would_refuse_names_the_field_not_the_value(
            self, api_client: Any, seat: Store) -> None:
        error = _error(_update(api_client, {"include_topics": _CANARY}))
        assert error["code"] == "invalid_value" and "include_topics" in error["message"]
        assert _CANARY not in json.dumps(error)

    @pytest.mark.parametrize("changes", [
        {"timezone": "Mars/Olympus"}, {"newsletter_cadence": "hourly"},
        {"lead_magnet_keyword": "YES"}, {"min_reactions": "many"}, {"use_emojis": None},
    ])
    def test_a_bad_value_is_refused_before_anything_is_written(
            self, api_client: Any, seat: Store, changes: dict) -> None:
        before = (copy.deepcopy(seat.prefs), dict(seat.magnet), dict(seat.newsletter), seat.tz)
        error = _error(_update(api_client, changes))
        assert error["code"] == "invalid_value"
        assert (seat.prefs, seat.magnet, seat.newsletter, seat.tz) == before

    def test_the_policy_is_one_frozen_table_with_no_overlaps(self) -> None:
        assert policy.GROUP1_FIELDS and not policy.GROUP1_FIELDS & policy.GROUP3_FIELDS
        assert not policy.GROUP1_FIELDS & policy.CAP_FIELDS
        assert not policy.GROUP1_FIELDS & policy.NEVER_FIELDS
        assert all(policy.classify(f) == policy.KIND_ALLOWED for f in policy.GROUP1_FIELDS)
        assert isinstance(policy.GROUP1_FIELDS, frozenset)

    def test_every_group_one_field_exists_on_its_route_model(self) -> None:
        from cqc_lem.api.routers import user as routes

        models = {policy.PREFS: routes.EngagementPreferencesRequest,
                  policy.ICP: routes.IcpDefinitionRequest, policy.LEAD_MAGNET: routes.LeadMagnetRequest,
                  policy.NEWSLETTER: routes.NewsletterSettingsRequest,
                  policy.TIMEZONE: routes.TimezoneRequest}
        for field, (resource, key) in policy.FIELD_TARGETS.items():
            assert key in models[resource].model_fields, field

    def test_no_group_one_field_is_a_cap_mode_or_gate_by_name(self) -> None:
        for field in policy.GROUP1_FIELDS:
            # `max_post_age_hours` is a targeting filter, not a daily cap.
            assert not field.endswith("_mode") and not policy._CAP_PATTERN.match(field) \
                and "auto_publish" not in field and "auto_send" not in field


class TestTheDiffAndTheAudit:
    def test_the_answer_has_changed_unchanged_and_an_undo_id(self, api_client: Any, seat: Store) -> None:
        body = structured(_update(api_client, {"tone": "direct", "comment_length": seat.prefs[
            "comment_length"]}))
        assert body["changed"] == {"tone": {"before": "warm", "after": "direct"}}
        assert body["unchanged"] == ["comment_length"]
        assert isinstance(body["undo_id"], str) and len(body["undo_id"]) >= 8

    def test_a_no_op_changes_nothing_and_offers_no_undo(self, api_client: Any, seat: Store,
                                                        audit: MagicMock) -> None:
        body = structured(_update(api_client, {"tone": "warm"}))
        assert body["changed"] == {} and body["unchanged"] == ["tone"] and body["undo_id"] is None
        assert seat.prefs_writes == []
        audit.assert_called_once()

    def test_one_mcp_settings_row_per_call_with_field_names_only(
            self, api_client: Any, seat: Store, audit: MagicMock) -> None:
        _update(api_client, {"tone": _CANARY, "business_goals": _CANARY + "-b"})
        audit.assert_called_once()
        assert audit.call_args.args[0] == AuthAuditEvent.MCP_SETTINGS == "mcp_settings"
        details = audit.call_args.kwargs["details"]
        assert details == {"tool": "lem_update_settings", "fields": ["business_goals", "tone"],
                           "scope": "mcp"}
        assert _CANARY not in json.dumps(audit.call_args.kwargs, default=str)

    def test_a_refused_call_writes_no_audit_row(self, api_client: Any, seat: Store,
                                                audit: MagicMock) -> None:
        _update(api_client, {"max_queued_drafts": 1})
        audit.assert_not_called()

    def test_free_text_is_in_the_answer_but_nowhere_else(self, api_client: Any, seat: Store,
                                                         audit: MagicMock, caplog: Any) -> None:
        result = _update(api_client, {"tone": _CANARY})
        assert structured(result)["changed"]["tone"]["after"] == _CANARY
        assert _CANARY not in caplog.text
        assert _CANARY not in json.dumps(audit.call_args.kwargs, default=str)

    def test_a_call_spends_the_write_budget_once(self, api_client: Any, seat: Store) -> None:
        with patch(f"{_W}.consume_budgets", return_value=None) as spend:
            _update(api_client, {"tone": "direct"})
        assert spend.call_count == 1


def _refuse(status: int = 403) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": "nope", "message": "refused"})


class TestAPartialOrUnknownWriteLeavesARow:
    """A multi-resource call that fails after the first PUT landed is audited, names only."""

    CHANGES = {"tone": _CANARY, "lead_magnet_keyword": "AUDIT"}

    def test_a_second_resource_refusal_audits_only_the_first_resources_fields(
            self, api_client: Any, seat: Store, audit: MagicMock) -> None:
        with patch(f"{_U}.update_lead_magnet_settings", side_effect=_refuse()):
            error = _error(_update(api_client, self.CHANGES))
        assert seat.prefs["tone"] == _CANARY and seat.magnet["keyword"] == "GUIDE"
        audit.assert_called_once()
        assert audit.call_args.args[0] == AuthAuditEvent.MCP_SETTINGS
        assert audit.call_args.kwargs["details"] == {
            "tool": "lem_update_settings", "fields": ["tone"], "unconfirmed": [], "scope": "mcp"}
        assert _CANARY not in json.dumps(audit.call_args.kwargs, default=str)
        assert "tone" in error["message"] and "undo_id" in error["message"]

    def test_the_undo_for_what_landed_is_saved_and_works(self, api_client: Any, seat: Store,
                                                         fake_redis: Any) -> None:
        with patch(f"{_U}.update_lead_magnet_settings", side_effect=_refuse()):
            error = _error(_update(api_client, self.CHANGES))
        undo_id = error["message"].split("undo_id ")[1].rstrip(".")
        assert any(k.endswith(undo_id) for k in fake_redis.store)
        call(api_client, "lem_undo_settings_change", {"undo_id": undo_id})
        assert seat.prefs["tone"] == "warm"

    def test_a_replay_with_the_same_request_id_does_not_rerun_the_first_put(
            self, api_client: Any, seat: Store, audit: MagicMock) -> None:
        with patch(f"{_U}.update_lead_magnet_settings", side_effect=_refuse()):
            _update(api_client, self.CHANGES, client_request_id="req-p")
            again = _error(_update(api_client, self.CHANGES, client_request_id="req-p"))
        assert again["code"] == "duplicate_outcome_unknown"
        assert seat.prefs_writes == [{"tone": _CANARY}]
        audit.assert_called_once()

    def test_a_timeout_after_the_first_write_is_one_row_marked_unconfirmed(
            self, api_client: Any, seat: Store, audit: MagicMock) -> None:
        import time

        def slow(_user: int, settings: dict) -> bool:
            time.sleep(0.6)
            return seat.put_magnet(_user, settings)

        with patch("cqc_lem.api.mcp.calls.TOOL_TIMEOUT_SECONDS", 0.25), \
                patch(f"{_U}.update_lead_magnet_settings", side_effect=slow):
            error = _error(_update(api_client, self.CHANGES))
        assert error["code"] == "outcome_unknown" and error["outcome"] == "unknown"
        audit.assert_called_once()
        assert audit.call_args.kwargs["details"] == {
            "tool": "lem_update_settings", "fields": ["tone"],
            "unconfirmed": ["lead_magnet_keyword"], "scope": "mcp"}

    def test_a_server_error_after_a_write_is_unknown_and_audited(
            self, api_client: Any, seat: Store, audit: MagicMock) -> None:
        with patch(f"{_U}.update_lead_magnet_settings", return_value=False):
            error = _error(_update(api_client, self.CHANGES))
        assert error["outcome"] == "unknown"
        details = audit.call_args.kwargs["details"]
        assert details["fields"] == ["tone"] and details["unconfirmed"] == ["lead_magnet_keyword"]

    def test_a_failure_before_any_write_leaves_no_row_and_frees_the_id(
            self, api_client: Any, seat: Store, audit: MagicMock) -> None:
        with patch(f"{_U}.update_engagement_preferences", side_effect=_refuse()):
            _error(_update(api_client, self.CHANGES, client_request_id="req-n"))
        audit.assert_not_called()
        ok = _update(api_client, self.CHANGES, client_request_id="req-n")
        assert ok["isError"] is False

    def test_a_lost_audit_row_is_a_warning_not_a_failure(self, api_client: Any, seat: Store,
                                                         audit: MagicMock, caplog: Any) -> None:
        audit.side_effect = RuntimeError("db down")
        result = _update(api_client, {"tone": "direct"})
        assert result["isError"] is False
        assert "MCP write audit row not written" in caplog.text


class TestUndo:
    def _change(self, client: Any) -> str:
        return structured(_update(client, {"tone": "direct", "include_topics": ["x"],
                                           "newsletter_topic": "n2"}))["undo_id"]

    def test_undo_restores_every_changed_field(self, api_client: Any, seat: Store) -> None:
        before = (copy.deepcopy(seat.prefs), dict(seat.newsletter))
        undo_id = self._change(api_client)
        assert seat.prefs["tone"] == "direct"
        result = call(api_client, "lem_undo_settings_change", {"undo_id": undo_id})
        assert result["isError"] is False, result
        assert (seat.prefs, seat.newsletter) == before
        assert set(structured(result)["restored"]) == {"tone", "include_topics", "newsletter_topic"}

    def test_undo_writes_an_mcp_settings_row_with_field_names(self, api_client: Any, seat: Store,
                                                              audit: MagicMock) -> None:
        undo_id = self._change(api_client)
        audit.reset_mock()
        call(api_client, "lem_undo_settings_change", {"undo_id": undo_id})
        audit.assert_called_once()
        assert audit.call_args.args[0] == AuthAuditEvent.MCP_SETTINGS
        assert audit.call_args.kwargs["details"]["fields"] == [
            "include_topics", "newsletter_topic", "tone"]

    def test_undo_restores_a_stored_null(self, api_client: Any, seat: Store) -> None:
        seat.prefs["tone"] = None
        undo_id = structured(_update(api_client, {"tone": "direct"}))["undo_id"]
        call(api_client, "lem_undo_settings_change", {"undo_id": undo_id})
        assert seat.prefs["tone"] is None

    def test_undo_refuses_naming_a_field_changed_again(self, api_client: Any, seat: Store) -> None:
        undo_id = self._change(api_client)
        seat.prefs["tone"] = "edited by the user in LEM"
        snapshot = (copy.deepcopy(seat.prefs), dict(seat.newsletter))
        error = _error(call(api_client, "lem_undo_settings_change", {"undo_id": undo_id}))
        assert error["code"] == "undo_conflict"
        assert "tone" in error["message"] and "include_topics" not in error["message"]
        assert (seat.prefs, seat.newsletter) == snapshot

    def test_an_undo_id_works_once(self, api_client: Any, seat: Store) -> None:
        undo_id = self._change(api_client)
        call(api_client, "lem_undo_settings_change", {"undo_id": undo_id})
        error = _error(call(api_client, "lem_undo_settings_change", {"undo_id": undo_id}))
        assert error["code"] == "undo_not_found"

    def test_an_unknown_undo_id_is_refused(self, api_client: Any, seat: Store) -> None:
        error = _error(call(api_client, "lem_undo_settings_change", {"undo_id": "abcdef0123456789"}))
        assert error["code"] == "undo_not_found"

    def test_the_undo_record_lives_seven_days_per_user(self, api_client: Any, seat: Store,
                                                       fake_redis: Any) -> None:
        undo_id = self._change(api_client)
        key = f"mcp:settings-undo:{UID}:{undo_id}"
        assert key in fake_redis.store and fake_redis.expiries[key] == 7 * 24 * 3600

    def test_another_users_undo_id_is_not_found(self, api_client: Any, seat: Store,
                                                fake_redis: Any) -> None:
        undo_id = self._change(api_client)
        fake_redis.store[f"mcp:settings-undo:{UID + 1}:{undo_id}"] = fake_redis.store.pop(
            f"mcp:settings-undo:{UID}:{undo_id}")
        error = _error(call(api_client, "lem_undo_settings_change", {"undo_id": undo_id}))
        assert error["code"] == "undo_not_found"


class TestFlag:
    @pytest.fixture(autouse=True)
    def off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MCP_SETTINGS_WRITES_ENABLED", "false")

    def test_the_tools_are_not_listed_while_the_flag_is_off(self, api_client: Any, seat: Store) -> None:
        names = {t["name"] for t in rpc(api_client, "tools/list").json()["result"]["tools"]}
        assert not names & set(TOOLS)
        assert "lem_draft_post" in names

    @pytest.mark.parametrize("tool,args", [("lem_update_settings", {"changes": {"tone": "x"}}),
                                           ("lem_undo_settings_change", {"undo_id": "abcdef0123456789"})])
    def test_a_call_is_refused_and_writes_nothing(self, api_client: Any, seat: Store, audit: MagicMock,
                                                  tool: str, args: dict) -> None:
        before = copy.deepcopy(seat.prefs)
        assert _error(call(api_client, tool, args))["code"] == "unknown_tool"
        assert seat.prefs == before and seat.prefs_writes == []
        audit.assert_not_called()

    def test_the_tools_are_listed_when_it_is_on(self, api_client: Any, seat: Store,
                                                monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MCP_SETTINGS_WRITES_ENABLED", "true")
        names = {t["name"] for t in rpc(api_client, "tools/list").json()["result"]["tools"]}
        assert set(TOOLS) <= names

    def test_the_route_refuses_an_mcp_seat_while_the_flag_is_off(self, seat: Store) -> None:
        from cqc_lem.api import main
        from cqc_lem.api.routers import user as routes

        reset = main._request_session_scope.set("mcp")
        try:
            with pytest.raises(HTTPException) as exc:
                routes._mcp_settings_guard(UID, {"tone"}, policy.PREFS_EDITABLE)
        finally:
            main._request_session_scope.reset(reset)
        assert exc.value.status_code == 403 and exc.value.detail["code"] == "settings_writes_disabled"

    def test_it_also_needs_the_writes_flag(self, api_client: Any, seat: Store,
                                           monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MCP_SETTINGS_WRITES_ENABLED", "true")
        monkeypatch.setenv("MCP_SERVER_WRITES_ENABLED", "false")
        assert _error(_update(api_client, {"tone": "x"}))["code"] == "writes_disabled"


class TestDedupe:
    def test_the_same_client_request_id_replays_and_writes_once(
            self, api_client: Any, seat: Store, audit: MagicMock) -> None:
        first = structured(_update(api_client, {"tone": "direct"}, client_request_id="req-1"))
        again = structured(_update(api_client, {"tone": "direct"}, client_request_id="req-1"))
        assert again == {**first, "replayed": True}
        assert seat.prefs_writes == [{"tone": "direct"}]
        audit.assert_called_once()

    def test_the_same_id_with_other_values_is_refused(self, api_client: Any, seat: Store) -> None:
        _update(api_client, {"tone": "direct"}, client_request_id="req-2")
        error = _error(_update(api_client, {"tone": "blunt"}, client_request_id="req-2"))
        assert error["code"] == "client_request_id_reused"
        assert seat.prefs["tone"] == "direct"


class TestTheRouteGuard:
    """The server holds an MCP seat to group 1 on the request body, whatever tool sent it."""

    @pytest.mark.parametrize("scope", ["agent", "operator", "observer", "full", None])
    def test_another_scope_is_untouched(self, scope: Any) -> None:
        from cqc_lem.api import main
        from cqc_lem.api.routers import user as routes

        reset = main._request_session_scope.set(scope)
        try:
            assert routes._mcp_settings_guard(UID, {"max_comments_per_day"}, frozenset()) is False
        finally:
            main._request_session_scope.reset(reset)

    @pytest.mark.parametrize("key", ["max_queued_drafts", "connection_request_mode",
                                     "roster_auto_follow", "authenticity_score_min", "icp_definition"])
    def test_an_mcp_seat_naming_a_forbidden_key_is_a_403(self, key: str) -> None:
        from cqc_lem.api import main
        from cqc_lem.api.routers import user as routes

        reset = main._request_session_scope.set("mcp")
        try:
            with pytest.raises(HTTPException) as exc:
                routes._mcp_settings_guard(UID, {"tone", "session_token", key}, policy.PREFS_EDITABLE)
        finally:
            main._request_session_scope.reset(reset)
        assert exc.value.status_code == 403
        assert exc.value.detail["code"] == "mcp_field_not_editable" and key in exc.value.detail["message"]

    def test_the_prefs_route_writes_only_what_an_mcp_seat_sent(self, seat: Store) -> None:
        from cqc_lem.api import main
        from cqc_lem.api.routers import user as routes

        request = routes.EngagementPreferencesRequest(session_token="-", tone="direct")
        reset = main._request_session_scope.set("mcp")
        try:
            with patch.object(main, "get_session_user_id", return_value=UID):
                routes.update_engagement_preferences_endpoint(request)
        finally:
            main._request_session_scope.reset(reset)
        assert seat.prefs_writes == [{"tone": "direct"}]

    def test_the_prefs_route_still_refuses_an_agent_outright(self) -> None:
        from cqc_lem.api import main
        from cqc_lem.api.routers import user as routes

        request = routes.EngagementPreferencesRequest(session_token="-", tone="direct")
        reset = main._request_session_scope.set("agent")
        try:
            with patch.object(main, "get_session_user_id", return_value=UID), \
                    pytest.raises(HTTPException) as exc:
                routes.update_engagement_preferences_endpoint(request)
        finally:
            main._request_session_scope.reset(reset)
        assert exc.value.detail["code"] == "agent_may_not_configure"


class TestTheRegistry:
    def test_both_tools_are_write_tools_on_both_scopes_behind_the_flag(self) -> None:
        for name in TOOLS:
            spec = registry.TOOLS_BY_NAME[name]
            assert spec.write and not spec.self_guarded and registry.routed_through_run_write(spec)
            assert spec.scopes == registry.BOTH_SCOPES
            assert spec.flag == "mcp-settings-writes" and spec.audit_event == "mcp_settings"
            assert "client_request_id" in spec.args_model.model_fields

    def test_the_flag_is_registered_off_by_default_with_an_env_fallback(self) -> None:
        from cqc_lem.utilities.flags import FLAGS, MCP_SETTINGS_WRITES

        spec = FLAGS[MCP_SETTINGS_WRITES]
        assert spec.default is False and spec.env_var == "MCP_SETTINGS_WRITES_ENABLED"
