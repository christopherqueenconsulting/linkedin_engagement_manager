"""`lem_request_settings_change` end to end (epic #2797, #2803).

Each call posts JSON-RPC to `/api/mcp`, so it runs the real gate, SDK, registry, write path, loopback
and real route reads. The settings reads are patched onto the same in-memory `Store` the S2 tests use;
the proposals repository is a small fake with the SQL's limit rule.
"""

import copy
import json
from contextlib import contextmanager
from typing import Any, Iterator, Optional
from unittest.mock import MagicMock, patch

import pytest

from cqc_lem.api.mcp import settings_policy as policy, tools as registry
from cqc_lem.platform.db.repositories.agent_proposals import ProposalCreate, ProposalCreated

from ._admitted import UID, admitted, call, rpc, structured
from .test_mcp_settings import Store, _routes

pytestmark = pytest.mark.unit

_W = "cqc_lem.api.mcp.writes"
_T = "cqc_lem.api.mcp.tools_settings"
TOOL = "lem_request_settings_change"
REASON = "The user wants faster follow-ups."


class Proposals:
    """`create_agent_proposal`, with the open-proposal limit the repository enforces."""

    def __init__(self, limit: int = 5) -> None:
        self.rows: list[dict[str, Any]] = []
        self.limit = limit
        self.fail = False

    def create(self, user_id: int, kind: str, payload: dict, reason: Optional[str],
               source_session_id: Optional[int]) -> ProposalCreated:
        if self.fail:
            return ProposalCreated(ProposalCreate.FAILED)
        if len(self.rows) >= self.limit:
            return ProposalCreated(ProposalCreate.LIMIT)
        self.rows.append({"user_id": user_id, "kind": kind, "payload": payload, "reason": reason,
                          "source_session_id": source_session_id})
        return ProposalCreated(ProposalCreate.CREATED, len(self.rows))


@pytest.fixture(autouse=True)
def enabled(monkeypatch: pytest.MonkeyPatch, fake_redis: Any) -> None:
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


@pytest.fixture
def proposals() -> Iterator[Proposals]:
    fake = Proposals()
    with patch(f"{_T}.create_agent_proposal", side_effect=fake.create):
        yield fake


@contextmanager
def _seat(resolver: MagicMock, store: Store, scope: str = "mcp") -> Iterator[None]:
    with admitted(resolver, scope), patch(f"{_W}.get_user_account_kind", return_value="standard"), \
            _routes(store):
        yield


@pytest.fixture
def seat(resolver: MagicMock, audit: MagicMock, store: Store, proposals: Proposals) -> Iterator[Store]:
    with _seat(resolver, store):
        yield store


def _request(client: Any, changes: dict[str, Any], reason: str = REASON, **extra: Any) -> dict:
    return call(client, TOOL, {"changes": changes, "reason": reason, **extra})


def _error(result: dict) -> dict:
    assert result["isError"] is True, result
    return structured(result)["error"]


# The Store's prefs: connection_request_mode pre_review, nurture_dm_mode auto_approve,
# roster_auto_follow True, roster_auto_connect True, hold_repaired False, authenticity 61, similarity 44.
LOOSEN = {"connection_request_mode": "auto_approve", "authenticity_score_min": 40,
          "post_similarity_max_pct": 60}


class TestFilingARequest:
    def test_it_files_a_pending_proposal_and_changes_nothing(
            self, api_client: Any, seat: Store, proposals: Proposals, audit: MagicMock) -> None:
        before = copy.deepcopy(seat.prefs)
        body = structured(_request(api_client, LOOSEN))
        assert seat.prefs == before and seat.prefs_writes == []
        assert body["status"] == "pending" and body["proposal_id"] == 1
        assert body["review_url"].endswith("/agents")
        (row,) = proposals.rows
        assert row["user_id"] == UID and row["kind"] == "settings" and row["reason"] == REASON
        assert row["source_session_id"] == 901
        assert row["payload"] == {
            "connection_request_mode": {"before": "pre_review", "requested": "auto_approve"},
            "authenticity_score_min": {"before": 61, "requested": 40},
            "post_similarity_max_pct": {"before": 44, "requested": 60}}

    def test_it_returns_one_warning_per_field_and_says_it_is_not_applied(
            self, api_client: Any, seat: Store) -> None:
        body = structured(_request(api_client, LOOSEN))
        assert len(body["warnings"]) == len(LOOSEN)
        assert all(w.count("Approving this") == 1 for w in body["warnings"])
        assert any("without waiting for your review" in w for w in body["warnings"])
        assert "NOT applied" in body["note"] and "relay every warning" in body["note"]
        assert "never say the change was made" in body["note"]

    def test_the_description_tells_the_agent_to_relay_and_never_claim_it_applied(self) -> None:
        text = registry.TOOLS_BY_NAME[TOOL].description
        assert "MUST relay" in text and "never say the change was applied" in text
        assert "can never approve" in text

    def test_it_audits_proposed_with_names_and_the_id_only(
            self, api_client: Any, seat: Store, audit: MagicMock) -> None:
        _request(api_client, LOOSEN, reason="zz-reason-canary")
        audit.assert_called_once()
        assert audit.call_args.args[0] == "mcp_settings_proposed"
        details = audit.call_args.kwargs["details"]
        assert details == {"tool": TOOL, "fields": sorted(LOOSEN), "scope": "mcp", "proposal_id": 1}
        assert "zz-reason-canary" not in repr(audit.call_args) and "auto_approve" not in repr(audit.call_args)

    @pytest.mark.parametrize("scope", ["mcp", "mcp_outreach"])
    def test_it_is_on_both_scopes(self, api_client: Any, resolver: MagicMock, audit: MagicMock,
                                  store: Store, proposals: Proposals, scope: str) -> None:
        with _seat(resolver, store, scope):
            assert structured(_request(api_client, {"roster_auto_follow": False}))["proposal_id"] == 1

    def test_a_stricter_request_is_filed_and_its_warning_says_it_loosens_nothing(
            self, api_client: Any, seat: Store) -> None:
        body = structured(_request(api_client, {"authenticity_score_min": 90}))
        assert body["warnings"] == [f"Authenticity bar: {policy.SAFE_WARNING}"]

    def test_a_value_already_in_force_is_dropped_and_named(
            self, api_client: Any, seat: Store, proposals: Proposals) -> None:
        body = structured(_request(api_client, {"roster_auto_follow": True, "connection_request_mode": "auto_approve"}))
        assert body["unchanged"] == ["roster_auto_follow"]
        assert list(proposals.rows[0]["payload"]) == ["connection_request_mode"]

    def test_when_nothing_differs_there_is_nothing_to_request(
            self, api_client: Any, seat: Store, proposals: Proposals) -> None:
        error = _error(_request(api_client, {"roster_auto_follow": True}))
        assert error["code"] == "invalid_value" and "nothing to request" in error["message"]
        assert proposals.rows == []

    def test_a_null_threshold_compares_against_the_deploy_default(
            self, api_client: Any, seat: Store, proposals: Proposals) -> None:
        seat.prefs["authenticity_score_min"] = None
        seat.prefs["gate_defaults"] = {"authenticity_score_min": 65}
        body = structured(_request(api_client, {"authenticity_score_min": 50}))
        assert proposals.rows[0]["payload"]["authenticity_score_min"] == {"before": None, "requested": 50}
        assert "lowers the bar" in body["warnings"][0]

    def test_the_newsletter_switch_is_read_from_the_newsletter_settings(
            self, api_client: Any, seat: Store, proposals: Proposals) -> None:
        seat.newsletter["auto_publish_newsletters"] = False
        _request(api_client, {"auto_publish_newsletters": True})
        assert proposals.rows[0]["payload"] == {"auto_publish_newsletters": {"before": False,
                                                                            "requested": True}}


class TestWhatItRefuses:
    @pytest.mark.parametrize("field", ["tone", "include_topics", "timezone", "newsletter_topic"])
    def test_a_group_one_field_points_at_lem_update_settings(
            self, api_client: Any, seat: Store, proposals: Proposals, field: str) -> None:
        error = _error(_request(api_client, {field: "x"}))
        assert error["code"] == "not_a_gate_field"
        assert field in error["message"] and "lem_update_settings" in error["message"]
        assert proposals.rows == []

    @pytest.mark.parametrize("field", ["max_comments_per_day", "linkedin_password", "email",
                                       "connection_targeting_mode", "totally_unknown"])
    def test_caps_never_fields_and_unknown_names_are_refused(
            self, api_client: Any, seat: Store, proposals: Proposals, field: str) -> None:
        error = _error(_request(api_client, {field: 1}))
        assert error["code"] == "not_a_gate_field" and field in error["message"]
        assert proposals.rows == []

    def test_one_bad_field_blocks_the_whole_bundle(
            self, api_client: Any, seat: Store, proposals: Proposals) -> None:
        error = _error(_request(api_client, {"roster_auto_follow": False, "tone": "x"}))
        assert error["code"] == "not_a_gate_field" and proposals.rows == []

    @pytest.mark.parametrize("changes", [
        {"connection_request_mode": "auto-approve"},  # a typo the route would turn into auto_approve
        {"connection_request_mode": True}, {"roster_auto_follow": "yes"}, {"roster_auto_follow": 1},
        {"authenticity_score_min": "low"}, {"authenticity_score_min": True},
        {"post_similarity_max_pct": 12.5}, {"authenticity_score_min": None},
    ])
    def test_a_value_that_is_not_strictly_the_right_type_is_refused(
            self, api_client: Any, seat: Store, proposals: Proposals, changes: dict) -> None:
        error = _error(_request(api_client, changes))
        assert error["code"] == "invalid_value" and next(iter(changes)) in error["message"]
        assert proposals.rows == []

    def test_it_stops_at_five_open_requests(
            self, api_client: Any, seat: Store, proposals: Proposals) -> None:
        for _ in range(5):
            assert _request(api_client, {"connection_request_mode": "auto_approve"})["isError"] is False
        error = _error(_request(api_client, {"connection_request_mode": "auto_approve"}))
        assert error["code"] == "too_many_open_requests" and "5 change requests" in error["message"]
        assert len(proposals.rows) == 5

    def test_a_store_failure_files_nothing_and_says_so(
            self, api_client: Any, seat: Store, proposals: Proposals) -> None:
        proposals.fail = True
        assert _error(_request(api_client, {"connection_request_mode": "auto_approve"}))["code"] == \
            "request_not_stored"

    def test_the_reason_is_required_and_bounded(self, api_client: Any, seat: Store) -> None:
        for args in ({"changes": {"roster_auto_follow": False}},
                     {"changes": {"roster_auto_follow": False}, "reason": ""},
                     {"changes": {"roster_auto_follow": False}, "reason": "x" * 1001}):
            assert call(api_client, TOOL, args)["isError"] is True


class TestUpdateSettingsRoutesGateAttemptsHere:
    @pytest.mark.parametrize("changes", [
        {"connection_request_mode": "auto_approve"}, {"roster_auto_connect": True},
        {"hold_repaired_posts_for_review": False}, {"authenticity_score_min": 10},
        {"post_similarity_max_pct": 99}, {"auto_publish_newsletters": True},
        {"catchup_touch_mode": "auto_approve"}, {"funnel_stage_mode": "auto_approve"}])
    def test_a_loosening_attempt_is_an_error_naming_the_request_tool(
            self, api_client: Any, seat: Store, changes: dict) -> None:
        before = copy.deepcopy(seat.prefs)
        result = call(api_client, "lem_update_settings", {"changes": changes})
        error = _error(result)
        assert error["code"] == "needs_change_request" and "lem_request_settings_change" in error["message"]
        assert seat.prefs == before and seat.prefs_writes == []


class TestRegistryAndGuards:
    def test_it_is_a_flagged_write_tool_on_both_scopes_run_through_run_write(self) -> None:
        spec = registry.TOOLS_BY_NAME[TOOL]
        assert spec.write and not spec.self_guarded and not spec.approval
        assert registry.routed_through_run_write(spec)
        assert spec.scopes == registry.BOTH_SCOPES and spec.flag == "mcp-settings-writes"
        assert spec.audit_event == "mcp_settings_proposed"
        assert "client_request_id" in spec.args_model.model_fields
        assert {m for m, _p in spec.calls} == {"GET"}, "it never PUTs a setting"

    def test_no_tool_in_the_registry_approves_a_proposal(self) -> None:
        for spec in registry.TOOLS:
            assert "proposal" not in spec.name or spec.name == TOOL
            assert not any("settings-proposals" in p for _m, p in spec.calls), spec.name

    def test_it_is_not_listed_or_callable_while_the_settings_flag_is_off(
            self, api_client: Any, seat: Store, monkeypatch: pytest.MonkeyPatch,
            proposals: Proposals) -> None:
        monkeypatch.setenv("MCP_SETTINGS_WRITES_ENABLED", "false")
        names = {t["name"] for t in rpc(api_client, "tools/list").json()["result"]["tools"]}
        assert TOOL not in names
        assert _error(_request(api_client, {"roster_auto_follow": False}))["code"] == "unknown_tool"
        assert proposals.rows == []

    def test_the_same_client_request_id_replays_and_files_once(
            self, api_client: Any, seat: Store, proposals: Proposals, audit: MagicMock) -> None:
        first = structured(_request(api_client, LOOSEN, client_request_id="req-r1"))
        again = structured(_request(api_client, LOOSEN, client_request_id="req-r1"))
        assert again == {**first, "replayed": True}
        assert len(proposals.rows) == 1
        audit.assert_called_once()

    def test_the_audit_helper_reads_requested_field_names(self) -> None:
        assert policy.audit_fields({"requested": {"b": {}, "a": {}}}) == ["a", "b"]

    def test_the_answer_never_echoes_json_that_is_not_ours(self, api_client: Any, seat: Store) -> None:
        body = _request(api_client, {"roster_auto_follow": False}, reason="<script>alert(1)</script>")
        assert "<script>" not in json.dumps(structured(body))
