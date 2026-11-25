"""Daily-cap changes through `lem_update_settings` (epic #2797, #2801).

Same harness as `test_mcp_settings.py`: JSON-RPC through the gate, the real tool, loopback and route
handlers, over the in-memory `Store`. Stored caps there: comments 7, DMs 4, invites 5, follows 2,
catch-up per day 5 (clamped to 3 by the patched plan allowance).
"""

import copy
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

from cqc_lem.api.mcp import settings_policy as policy

from . import test_mcp_settings as base
from ._admitted import UID, call, structured
from .test_mcp_settings import Store, _error, _update

# The harness fixtures, re-bound by assignment so pytest finds them in this module.
audit, enabled, seat, store = base.audit, base.enabled, base.seat, base.store

pytestmark = pytest.mark.unit


class TestLowering:
    def test_a_lowering_applies_with_no_acknowledgement(self, api_client: Any, seat: Store,
                                                        audit: MagicMock) -> None:
        body = structured(_update(api_client, {"max_comments_per_day": 3, "max_dms_per_day": 0}))
        assert body["changed"] == {"max_comments_per_day": {"before": 7, "after": 3},
                                   "max_dms_per_day": {"before": 4, "after": 0}}
        assert body["warnings"] == [] and body["raised"] == []
        assert (seat.prefs["max_comments_per_day"], seat.prefs["max_dms_per_day"]) == (3, 0)
        assert seat.prefs_writes == [{"max_comments_per_day": 3, "max_dms_per_day": 0}]
        details = audit.call_args.kwargs["details"]
        assert "raised" not in details and details["fields"] == ["max_comments_per_day", "max_dms_per_day"]

    def test_an_unchanged_cap_is_a_no_op(self, api_client: Any, seat: Store) -> None:
        body = structured(_update(api_client, {"max_comments_per_day": 7}))
        assert body["changed"] == {} and seat.prefs_writes == []


class TestRaiseNeedsAcknowledgement:
    def test_a_raise_without_it_is_refused_with_the_warning_and_writes_nothing(
            self, api_client: Any, seat: Store, audit: MagicMock) -> None:
        before = copy.deepcopy(seat.prefs)
        error = _error(_update(api_client, {"tone": "direct", "max_invites_per_day": 8}))
        assert error["code"] == "cap_raise_needs_acknowledgement"
        text = error["message"]
        assert "max_invites_per_day goes from 5 to 8" in text
        assert "429" in text and "suppression" in text and "ceiling is 30" in text
        assert "SHOW THIS WARNING TO THE USER" in text
        assert seat.prefs == before and seat.prefs_writes == []
        audit.assert_not_called()

    def test_a_raise_with_it_applies_and_still_returns_warnings_and_marks_the_audit(
            self, api_client: Any, seat: Store, audit: MagicMock) -> None:
        body = structured(_update(api_client, {"max_invites_per_day": 8, "max_dms_per_day": 2},
                                  acknowledge_risk=True))
        assert body["changed"]["max_invites_per_day"] == {"before": 5, "after": 8}
        assert body["raised"] == ["max_invites_per_day"]
        assert len(body["warnings"]) == 1 and "max_invites_per_day goes from 5 to 8" in body["warnings"][0]
        assert seat.prefs["max_invites_per_day"] == 8 and seat.prefs["max_dms_per_day"] == 2
        details = audit.call_args.kwargs["details"]
        assert details["raised"] is True and details["raised_fields"] == ["max_invites_per_day"]
        assert details["fields"] == ["max_dms_per_day", "max_invites_per_day"]

    def test_the_acknowledgement_reaches_the_route_as_a_header_only_when_a_raise_needs_it(
            self, api_client: Any, seat: Store) -> None:
        from cqc_lem.api.mcp.loopback import Loopback

        seen: list[Any] = []
        real = Loopback.write

        async def spy(self: Any, *args: Any, **kwargs: Any) -> Any:
            seen.append(kwargs.get("headers"))
            return await real(self, *args, **kwargs)

        with patch.object(Loopback, "write", spy):
            _update(api_client, {"max_dms_per_day": 1}, acknowledge_risk=True)
            _update(api_client, {"max_dms_per_day": 3}, acknowledge_risk=True)
        assert seen == [None, {policy.ACK_HEADER: "true"}]

    def test_the_tool_description_tells_the_agent_to_show_the_warning(self) -> None:
        from cqc_lem.api.mcp.tools_settings import SETTINGS_TOOLS

        text = SETTINGS_TOOLS[0].description
        assert "MUST show that warning to the user" in text and "acknowledge_risk: true" in text
        assert "relay the returned warnings" in text


class TestClamp:
    def test_a_value_above_the_ceiling_is_clamped_and_reported_requested_vs_applied(
            self, api_client: Any, seat: Store) -> None:
        body = structured(_update(api_client, {"max_invites_per_day": 500}, acknowledge_risk=True))
        assert seat.prefs["max_invites_per_day"] == 30
        assert body["clamped"] == {"max_invites_per_day":
                                   {"requested": 500, "applied": 30, "limit": 30}}
        assert body["changed"]["max_invites_per_day"]["after"] == 30
        assert any("requested 500, applied 30" in n for n in body["notes"])

    @pytest.mark.parametrize("field,ceiling", [
        ("max_comments_per_day", 100), ("max_dms_per_day", 100), ("max_invites_per_day", 30),
        ("max_follows_per_day", 20), ("max_company_page_invites_per_day", 50),
        ("max_catchup_touches_per_day", 3),  # the plan allowance, patched to 3 in the harness
        ("max_catchup_touches_per_contact_days", 365)])
    def test_every_cap_is_held_to_its_ceiling(self, api_client: Any, seat: Store, field: str,
                                              ceiling: int) -> None:
        body = structured(_update(api_client, {field: 10_000}, acknowledge_risk=True))
        assert seat.prefs[field] == ceiling
        assert body["clamped"][field]["applied"] == ceiling

    def test_the_ceiling_is_the_route_models_own(self) -> None:
        from cqc_lem.api.routers import user as routes

        with patch.object(routes, "max_catchup_touches_allowed", return_value=10):
            bounds = routes._cap_bounds(UID)
        assert set(bounds) == policy.CAP_EDITABLE
        for field, (low, high) in bounds.items():
            if field == "max_catchup_touches_per_day":
                continue  # the plan allowance is applied in the upsert, which knows the user
            model = routes.EngagementPreferencesRequest(session_token="-", **{field: high + 10_000})
            assert getattr(model, field) == high, field
            model = routes.EngagementPreferencesRequest(session_token="-", **{field: low - 10_000})
            assert getattr(model, field) == low, field

    def test_a_clamp_that_leaves_the_stored_value_alone_is_a_no_op(self, api_client: Any,
                                                                   seat: Store) -> None:
        seat.prefs["max_invites_per_day"] = 30
        body = structured(_update(api_client, {"max_invites_per_day": 99}))
        assert body["changed"] == {} and seat.prefs_writes == []


class TestSpaPathAlsoClamps:
    """The clamp lives in the shared route and upsert, so the SPA (no MCP scope) gets it too."""

    @pytest.mark.parametrize("field,expected", [("max_dms_per_day", 100), ("max_invites_per_day", 30),
                                                ("max_comments_per_day", 100)])
    def test_the_request_model_clamps_what_the_spa_sends(self, field: str, expected: int) -> None:
        from cqc_lem.api.routers import user as routes

        assert getattr(routes.EngagementPreferencesRequest(session_token="-", **{field: 9999}),
                       field) == expected
        assert getattr(routes.EngagementPreferencesRequest(session_token="-", **{field: -4}), field) == 0

    def test_the_spa_scope_is_not_held_to_the_acknowledgement(self) -> None:
        from cqc_lem.api import main
        from cqc_lem.api.routers import user as routes

        reset = main._request_session_scope.set("full")
        try:
            body = routes.EngagementPreferencesRequest(session_token="-", max_dms_per_day=99)
            assert routes._mcp_settings_guard(UID, body.model_fields_set,
                                              policy.PREFS_EDITABLE, body) is False
        finally:
            main._request_session_scope.reset(reset)


class TestIntervalDirection:
    def test_the_policy_reads_a_decrease_as_the_raise(self) -> None:
        field = "min_catchup_contact_interval_days"
        assert policy.is_raise(field, 14, 3) is True
        assert policy.is_raise(field, 14, 30) is False
        assert policy.is_raise(field, 14, 14) is False

    def test_zero_on_the_per_contact_cap_is_no_limit_so_it_is_a_raise(self) -> None:
        field = "max_catchup_touches_per_contact_days"
        assert policy.is_raise(field, 2, 0) is True
        assert policy.is_raise(field, 0, 2) is False
        assert policy.is_raise(field, 2, 3) is True

    def test_an_unreadable_before_fails_closed(self) -> None:
        assert policy.is_raise("max_dms_per_day", "n/a", 3) is True

    def test_shortening_the_interval_needs_the_acknowledgement(self, api_client: Any,
                                                               seat: Store) -> None:
        seat.prefs["min_catchup_contact_interval_days"] = 14
        error = _error(_update(api_client, {"min_catchup_contact_interval_days": 3}))
        assert error["code"] == "cap_raise_needs_acknowledgement"
        assert "lowest allowed value is 0" in error["message"]
        assert seat.prefs["min_catchup_contact_interval_days"] == 14

    def test_lengthening_the_interval_just_applies(self, api_client: Any, seat: Store) -> None:
        seat.prefs["min_catchup_contact_interval_days"] = 14
        body = structured(_update(api_client, {"min_catchup_contact_interval_days": 30}))
        assert body["changed"]["min_catchup_contact_interval_days"] == {"before": 14, "after": 30}
        assert body["raised"] == []

    def test_a_number_the_interval_clamps_to_its_ceiling_is_not_a_raise(
            self, api_client: Any, seat: Store) -> None:
        seat.prefs["min_catchup_contact_interval_days"] = 14
        body = structured(_update(api_client, {"min_catchup_contact_interval_days": 9999}))
        assert body["clamped"]["min_catchup_contact_interval_days"]["applied"] == 365
        assert body["raised"] == []


class TestUndo:
    def test_undoing_a_lowering_raises_the_cap_back_and_needs_the_acknowledgement(
            self, api_client: Any, seat: Store) -> None:
        undo_id = structured(_update(api_client, {"max_comments_per_day": 3}))["undo_id"]
        refused = _error(call(api_client, "lem_undo_settings_change", {"undo_id": undo_id}))
        assert refused["code"] == "cap_raise_needs_acknowledgement"
        assert seat.prefs["max_comments_per_day"] == 3
        ok = structured(call(api_client, "lem_undo_settings_change",
                             {"undo_id": undo_id, "acknowledge_risk": True}))
        assert seat.prefs["max_comments_per_day"] == 7 and ok["raised"] == ["max_comments_per_day"]


class TestTheRouteGuardEnforcesItToo:
    """An MCP seat that bypasses the tool and PUTs a raise without the header is refused."""

    def _put(self, ack: bool, **fields: Any) -> None:
        from cqc_lem.api import main
        from cqc_lem.api.routers import user as routes

        request = routes.EngagementPreferencesRequest(session_token="-", **fields)
        http = MagicMock()
        http.headers = {policy.ACK_HEADER: "true"} if ack else {}
        scope = main._request_session_scope.set("mcp")
        req = main._request_object.set(http)
        try:
            with patch.object(main, "get_session_user_id", return_value=UID):
                routes.update_engagement_preferences_endpoint(request)
        finally:
            main._request_object.reset(req)
            main._request_session_scope.reset(scope)

    def test_an_unacknowledged_raise_is_a_403_and_writes_nothing(self, seat: Store) -> None:
        with pytest.raises(HTTPException) as exc:
            self._put(False, max_dms_per_day=9)
        assert exc.value.status_code == 403
        assert exc.value.detail["code"] == "cap_raise_needs_acknowledgement"
        assert "max_dms_per_day goes from 4 to 9" in exc.value.detail["message"]
        assert seat.prefs_writes == []

    def test_a_lowering_needs_no_header(self, seat: Store) -> None:
        self._put(False, max_dms_per_day=1)
        assert seat.prefs_writes == [{"max_dms_per_day": 1}]

    def test_an_acknowledged_raise_is_written_clamped(self, seat: Store) -> None:
        self._put(True, max_dms_per_day=9999)
        assert seat.prefs_writes == [{"max_dms_per_day": 100}]

    def test_a_raise_that_clamps_back_to_the_stored_value_is_not_a_raise(self, seat: Store) -> None:
        seat.prefs["max_dms_per_day"] = 100
        self._put(False, max_dms_per_day=9999)
        assert seat.prefs_writes == [{"max_dms_per_day": 100}]

    def test_unreadable_stored_caps_fail_closed_as_503(self, seat: Store) -> None:
        with patch("cqc_lem.api.routers.user.get_engagement_preferences", side_effect=RuntimeError):
            with pytest.raises(HTTPException) as exc:
                self._put(False, max_dms_per_day=1)
        assert exc.value.status_code == 503

    def test_a_cap_it_never_listed_is_still_a_403(self, seat: Store) -> None:
        from cqc_lem.api import main
        from cqc_lem.api.routers import user as routes

        scope = main._request_session_scope.set("mcp")
        try:
            with pytest.raises(HTTPException) as exc:
                routes._mcp_settings_guard(UID, {"max_queued_drafts"}, policy.PREFS_EDITABLE)
        finally:
            main._request_session_scope.reset(scope)
        assert exc.value.detail["code"] == "mcp_field_not_editable"
