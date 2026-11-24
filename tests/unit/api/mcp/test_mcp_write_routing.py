"""Every write tool is guarded exactly once, and routing never defaults to unguarded (PR #2731).

A `write` spec is either routed through `writes.run_write` (flag, dedupe, write budgets, audit) or it
is `self_guarded` and does all of that itself. Neither would let a write run bare, both would charge
the write budget twice.
"""

from typing import Any, Iterator
from unittest.mock import MagicMock, patch

import pytest

from cqc_lem.api.mcp import calls, limits, tools as registry, tools_spend, writes

from ._admitted import admitted, call, structured

pytestmark = pytest.mark.unit

_SPEND = {"lem_generate_post", "lem_generate_post_image", "lem_generate_post_video"}


class TestEachWriteSpecIsGuardedExactlyOnce:
    def test_a_write_is_routed_through_run_write_xor_self_guarded(self) -> None:
        writes_ = [spec for spec in registry.TOOLS if spec.write]
        assert writes_
        for spec in writes_:
            guards = int(registry.routed_through_run_write(spec)) + int(spec.self_guarded)
            assert guards == 1, spec.name

    def test_only_the_three_spend_specs_are_self_guarded(self) -> None:
        assert {s.name for s in registry.TOOLS if s.self_guarded} == _SPEND
        assert all(s.write for s in registry.TOOLS if s.self_guarded)

    def test_a_new_write_spec_is_guarded_by_default(self) -> None:
        spec = registry.ToolSpec("lem_x", "X", "x", registry.NoArgs, registry.BOTH_SCOPES, (),
                                 MagicMock(), write=True)
        assert spec.self_guarded is False and registry.routed_through_run_write(spec)

    def test_a_read_is_never_routed_through_run_write(self) -> None:
        assert not any(registry.routed_through_run_write(s) for s in registry.TOOLS if not s.write)

    def test_every_approval_spec_runs_through_run_write(self) -> None:
        approvals = [s for s in registry.TOOLS if s.approval]
        assert approvals and all(registry.routed_through_run_write(s) for s in approvals)


@pytest.fixture(autouse=True)
def enabled(monkeypatch: pytest.MonkeyPatch, fake_redis: Any) -> None:
    monkeypatch.setenv("MCP_SERVER_ENABLED", "true")
    monkeypatch.setenv("MCP_SERVER_WRITES_ENABLED", "true")


@pytest.fixture
def mcp(resolver: MagicMock) -> Iterator[MagicMock]:
    with admitted(resolver, "mcp") as inner:
        yield inner


@pytest.fixture
def write_budget_spend() -> Iterator[MagicMock]:
    """Every consumption of the WRITE budgets, wherever it is imported, counted on one spy."""
    spy = MagicMock(return_value=None)

    def spent(user_id: int, budgets: Any) -> Any:
        if budgets is limits.WRITE_BUDGETS:
            spy(user_id)
        return None

    with patch.object(tools_spend, "consume_budgets", spent), \
            patch.object(writes, "consume_budgets", spent):
        yield spy


class TestOneCallSpendsTheWriteBudgetOnce:
    def test_a_spend_call_skips_run_write_and_spends_the_write_budget_once(
            self, api_client: Any, mcp: MagicMock, write_budget_spend: MagicMock) -> None:
        from cqc_lem.api.routers import mcp_spend as route

        with patch.object(calls.writes, "run_write") as run_write, \
                patch.object(route, "get_user_email", return_value="o@example.com"), \
                patch.object(route, "insert_post_returning_id", return_value=501), \
                patch("cqc_lem.app.run_content_plan.generate_agent_post_task.apply_async"):
            result = call(api_client, "lem_generate_post", {"topic": "Weekly reporting habits"})
        assert result["isError"] is False, structured(result)
        run_write.assert_not_called()
        assert write_budget_spend.call_count == 1

    def test_a_queue_write_goes_through_run_write(self, api_client: Any, mcp: MagicMock) -> None:
        async def fake(spec: Any, args: Any, principal: Any, run: Any, timeout: Any,
                       attempted: Any = None) -> dict:
            return {"id": 1}

        with patch.object(calls.writes, "run_write", side_effect=fake) as run_write:
            call(api_client, "lem_draft_post", {"content": "A short draft.",
                                                "scheduled_datetime": "2030-01-01T09:00:00+00:00"})
        assert run_write.call_count == 1
        assert run_write.call_args.args[0].name == "lem_draft_post"
