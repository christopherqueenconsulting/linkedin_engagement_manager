"""The tool runners against a fake loopback: the shaping logic, without the HTTP round trip.

`test_mcp_tools.py` proves the runners end to end; this file pins the parts whose answer depends on
what several inner calls returned together (open slots, a section that failed, a degraded fan-out).
"""

import datetime as dt
from typing import Any, Optional

import pytest

from cqc_lem.api.mcp import tools as registry
from cqc_lem.api.mcp.gate import McpPrincipal
from cqc_lem.api.mcp.loopback import ToolFailure

pytestmark = pytest.mark.unit


class FakeLoopback:
    """Answers `get(path)` from a table; a `ToolFailure` value is raised instead of returned."""

    def __init__(self, answers: dict[str, Any]) -> None:
        self.answers = answers
        self.calls: list[tuple[str, Optional[dict]]] = []
        self.principal = McpPrincipal(user_id=1, session_id=2, scope="mcp", expires_at=None,
                                      client_name=None)

    async def get(self, path: str, params: Optional[dict] = None) -> Any:
        self.calls.append((path, params))
        answer = self.answers.get(path)
        if isinstance(answer, ToolFailure):
            raise answer
        return answer


def _down(code: str = "unavailable") -> ToolFailure:
    return ToolFailure("upstream_error", code, "down", 503)


async def test_content_plan_finds_open_posting_days() -> None:
    today = dt.datetime.now(dt.timezone.utc).date()
    taken = today + dt.timedelta(days=1)
    lb = FakeLoopback({
        "/dashboard/planned-tasks": {"tasks": [{"id": 1}]},
        "/posts": {"posts": [{"post_id": 9, "scheduled_time": taken.isoformat() + "T10:00:00Z",
                              "status": "approved", "post_type": "text", "archetype": None,
                              "content": "not copied into the plan"}]},
        "/user/post-stats": {"timezone": "UTC", "recommendations": [{"day": "Tue"}]},
        "/user/engagement-preferences": {"posting_days": [0, 1, 2, 3, 4, 5, 6],
                                         "posts_per_week": 3},
    })
    plan = await registry._content_plan(lb, registry.ContentPlanArgs(weeks=1))
    assert taken.isoformat() not in plan["open_slots"]
    assert len(plan["open_slots"]) == 6
    assert plan["scheduled_posts"] == [{"post_id": 9, "scheduled_time": taken.isoformat()
                                        + "T10:00:00Z", "status": "approved",
                                        "post_type": "text", "archetype": None}]
    assert plan["best_times"] == [{"day": "Tue"}] and plan["posts_per_week"] == 3
    assert plan["upcoming_tasks"] == [{"id": 1}]


async def test_content_plan_unknown_inputs_are_null_never_fully_booked() -> None:
    lb = FakeLoopback({"/dashboard/planned-tasks": _down(), "/posts": _down(),
                       "/user/post-stats": _down(), "/user/engagement-preferences": _down()})
    plan = await registry._content_plan(lb, registry.ContentPlanArgs(weeks=2))
    assert plan["open_slots"] is None and plan["scheduled_posts"] is None
    assert plan["posting_days"] is None and plan["posts_per_week"] is None
    assert plan["upcoming_tasks"] == {"unavailable": "unavailable"}
    assert plan["best_times"] == {"unavailable": "unavailable"}


def test_a_bad_timezone_falls_back_to_utc() -> None:
    assert registry._local_today("Not/AZone") == dt.datetime.now(dt.timezone.utc).date()
    assert registry._local_today(None) == dt.datetime.now(dt.timezone.utc).date()


async def test_overview_degrades_one_section_at_a_time() -> None:
    lb = FakeLoopback({"/dashboard/stats": {"posted": 1}, "/dashboard/planned-tasks": _down(),
                       "/user/automation-status": _down("forbidden"),
                       "/user/linkedin-signin-status": {"state": "unknown"},
                       "/user/account-readiness": {"ready": True},
                       "/content_generation_status": None})
    overview = await registry._account_overview(lb, registry.NoArgs())
    assert overview["dashboard"]["counts"] == {"posted": 1}
    assert overview["dashboard"]["upcoming"] == {"unavailable": "unavailable"}
    health = overview["automation_health"]
    assert health["automation_status"] == {"unavailable": "forbidden"}
    assert health["linkedin_signin"] == {"state": "unknown"}
    assert any("FLEET-GLOBAL" in note for note in health["notes"])
    assert overview["content_generation"] is None


async def test_whoami_fails_when_settings_cannot_be_read() -> None:
    lb = FakeLoopback({"/user/settings": _down(), "/user/linkedin-profile": {}})
    with pytest.raises(ToolFailure):
        await registry._whoami(lb, registry.NoArgs())


async def test_whoami_survives_an_unreadable_profile() -> None:
    lb = FakeLoopback({"/user/settings": {"subscription": None, "preferences": None},
                       "/user/linkedin-profile": _down()})
    body = await registry._whoami(lb, registry.NoArgs())
    assert body["account_name"] is None and body["subscription"] is None


async def test_a_weekly_report_error_other_than_404_is_raised() -> None:
    lb = FakeLoopback({"/user/weekly-report": _down()})
    with pytest.raises(ToolFailure):
        await registry._weekly_report(lb, registry.NoArgs())
    lb = FakeLoopback({"/user/weekly-report": {"numbers": {}}})
    assert await registry._weekly_report(lb, registry.NoArgs()) == {"enabled": True,
                                                                    "report": {"numbers": {}}}


async def test_simple_runners_pass_their_window_through() -> None:
    lb = FakeLoopback({"/user/engagement-analytics": {"rows": []}, "/user/audience-growth": {},
                       "/leads/funnel": {"months": []}, "/user/post-stats": {"timezone": "UTC"}})
    assert (await registry._post_performance(lb, registry.DaysArgs(days=30)))["days"] == 30
    assert (await registry._audience_growth(lb, registry.DaysArgs(days=14)))["days"] == 14
    assert (await registry._lead_funnel(lb, registry.FunnelArgs(months=3)))["months"] == 3
    assert await registry._best_times(lb, registry.NoArgs()) == {"timezone": "UTC"}
    assert ("/user/engagement-analytics", {"days": 30}) in lb.calls


@pytest.mark.parametrize("url, handle", [
    ("https://www.linkedin.com/in/jane-doe/", "jane-doe"),
    ("https://www.linkedin.com/in/jane?trk=x", "jane"),
    ("https://example.com/jane", None), (None, None), ("", None),
])
def test_account_name(url: Optional[str], handle: Optional[str]) -> None:
    assert registry._account_name({"linkedin_profile_url": url}) == handle


def test_next_cursor() -> None:
    assert registry._next_cursor(1, 10, 25, 10) == "2"
    assert registry._next_cursor(3, 10, 25, 5) is None
    assert registry._next_cursor(1, 10, None, 10) == "2"
    assert registry._next_cursor(1, 10, None, 3) is None
