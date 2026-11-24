"""The tool runners against a fake loopback: the shaping logic, without the HTTP round trip.

`test_mcp_tools.py` proves the runners end to end; this file pins the parts whose answer depends on
what several inner calls returned together (open slots, a section that failed, a degraded fan-out).
"""

import datetime as dt
from typing import Any, Callable, Optional
from zoneinfo import ZoneInfo

import pytest

from cqc_lem.api.mcp import tools as registry
from cqc_lem.api.mcp.gate import McpPrincipal
from cqc_lem.api.mcp.loopback import ToolFailure
from cqc_lem.api.mcp.redact import THIRD_PARTY_NOTE

pytestmark = pytest.mark.unit

_EASTERN = ZoneInfo("America/New_York")
_ALL_DAYS = [0, 1, 2, 3, 4, 5, 6]


class FakeLoopback:
    """Answers `get(path)` from a table; a `ToolFailure` value is raised instead of returned.

    An answer may be a callable taking the query params, for a path called more than once.
    """

    def __init__(self, answers: dict[str, Any], scope: str = "mcp") -> None:
        self.answers = answers
        self.calls: list[tuple[str, Optional[dict]]] = []
        self.principal = McpPrincipal(user_id=1, session_id=2, scope=scope, expires_at=None,
                                      client_name=None)

    async def get(self, path: str, params: Optional[dict] = None) -> Any:
        self.calls.append((path, params))
        answer = self.answers.get(path)
        if callable(answer):
            answer = answer(params or {})
        if isinstance(answer, ToolFailure):
            raise answer
        return answer


def _down(code: str = "unavailable") -> ToolFailure:
    return ToolFailure("upstream_error", code, "down", 503)


def _posts(regular: list, planning: Optional[list] = None) -> Callable[[dict], dict]:
    """The `/posts` answer: `planning` rows only for the status-filtered call, as the route does."""
    def answer(params: dict) -> dict:
        rows = (planning or []) if params.get("status_filter") == "planning" else regular
        return {"posts": rows, "total": len(rows)}
    return answer


def _plan_lb(regular: list, planning: Optional[list] = None, timezone: str = "UTC",
             posts_per_week: int = 7, scope: str = "mcp", tasks: Optional[list] = None) -> FakeLoopback:
    return FakeLoopback({
        "/dashboard/planned-tasks": {"tasks": tasks or []},
        "/posts": _posts(regular, planning),
        "/user/post-stats": {"timezone": timezone, "recommendations": [{"day": "Tue"}]},
        "/user/engagement-preferences": {"posting_days": _ALL_DAYS, "posts_per_week": posts_per_week},
    }, scope=scope)


def _utc_iso(local: dt.datetime) -> str:
    return local.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


async def test_content_plan_finds_open_posting_days() -> None:
    today = dt.datetime.now(dt.timezone.utc).date()
    taken = today + dt.timedelta(days=1)
    row = {"post_id": 9, "scheduled_time": taken.isoformat() + "T10:00:00Z", "status": "approved",
           "post_type": "text", "archetype": None, "content": "not copied into the plan"}
    lb = _plan_lb([row], tasks=[{"id": 1, "kind": "Post", "title": "mine"}])
    plan = await registry._content_plan(lb, registry.ContentPlanArgs(weeks=1))
    assert taken.isoformat() not in plan["open_slots"]
    assert plan["scheduled_posts"] == [{"post_id": 9, "scheduled_time": row["scheduled_time"],
                                        "status": "approved", "post_type": "text",
                                        "archetype": None}]
    assert plan["best_times"] == [{"day": "Tue"}] and plan["posts_per_week"] == 7
    assert plan["upcoming_tasks"] == [{"id": 1, "kind": "Post", "title": "mine"}]
    planning_calls = [p for path, p in lb.calls if path == "/posts" and p.get("status_filter")]
    assert planning_calls and planning_calls[0]["status_filter"] == "planning"


async def test_an_evening_post_in_us_eastern_takes_its_local_day() -> None:
    """20:30 in New York is 01:30 UTC the NEXT day: the slot taken is the local day, not the UTC one."""
    local_today = dt.datetime.now(_EASTERN).date()
    day = local_today + dt.timedelta(days=2)
    evening = dt.datetime.combine(day, dt.time(20, 30), tzinfo=_EASTERN)
    assert _utc_iso(evening)[:10] == (day + dt.timedelta(days=1)).isoformat()
    lb = _plan_lb([{"post_id": 1, "scheduled_time": _utc_iso(evening), "status": "scheduled"}],
                  timezone="America/New_York")
    plan = await registry._content_plan(lb, registry.ContentPlanArgs(weeks=1))
    assert day.isoformat() not in plan["open_slots"]
    assert (day + dt.timedelta(days=1)).isoformat() in plan["open_slots"]


async def test_a_planning_placeholder_holds_its_slot() -> None:
    day = dt.datetime.now(dt.timezone.utc).date() + dt.timedelta(days=3)
    placeholder = {"post_id": 2, "scheduled_time": day.isoformat() + "T09:00:00Z",
                   "status": "planning"}
    plan = await registry._content_plan(_plan_lb([], planning=[placeholder]),
                                        registry.ContentPlanArgs(weeks=1))
    assert day.isoformat() not in plan["open_slots"]
    assert [p["post_id"] for p in plan["scheduled_posts"]] == [2]


async def test_a_rejected_post_holds_no_slot() -> None:
    day = dt.datetime.now(dt.timezone.utc).date() + dt.timedelta(days=2)
    rejected = {"post_id": 3, "scheduled_time": day.isoformat() + "T09:00:00Z", "status": "rejected"}
    plan = await registry._content_plan(_plan_lb([rejected]), registry.ContentPlanArgs(weeks=1))
    assert day.isoformat() in plan["open_slots"] and plan["scheduled_posts"] == []


async def test_open_slots_never_exceed_posts_per_week() -> None:
    plan = await registry._content_plan(_plan_lb([], posts_per_week=2),
                                        registry.ContentPlanArgs(weeks=4))
    per_week: dict[dt.date, int] = {}
    for iso in plan["open_slots"]:
        day = dt.date.fromisoformat(iso)
        monday = day - dt.timedelta(days=day.weekday())
        per_week[monday] = per_week.get(monday, 0) + 1
    assert per_week and max(per_week.values()) <= 2


async def test_a_week_already_full_has_no_open_slot() -> None:
    today = dt.datetime.now(dt.timezone.utc).date()
    monday_next = today + dt.timedelta(days=7 - today.weekday())
    full = [{"post_id": i, "scheduled_time": (monday_next + dt.timedelta(days=i)).isoformat()
             + "T09:00:00Z", "status": "approved"} for i in range(2)]
    plan = await registry._content_plan(_plan_lb(full, posts_per_week=2),
                                        registry.ContentPlanArgs(weeks=2))
    next_week = {(monday_next + dt.timedelta(days=i)).isoformat() for i in range(7)}
    assert next_week.isdisjoint(plan["open_slots"])


def test_open_slots_without_a_cadence_are_uncapped() -> None:
    today = dt.date(2026, 11, 23)  # a Monday
    assert len(registry._open_slots(today, 1, _ALL_DAYS, None, {})) == 7


def test_an_unreadable_timestamp_takes_no_day() -> None:
    assert registry._local_date("not a date", dt.timezone.utc) is None
    assert registry._local_date(None, dt.timezone.utc) is None
    assert registry._local_date("2026-11-25T10:00:00", _EASTERN) == dt.date(2026, 11, 25)


async def test_content_plan_unknown_inputs_are_null_never_fully_booked() -> None:
    lb = FakeLoopback({"/dashboard/planned-tasks": _down(), "/posts": _down(),
                       "/user/post-stats": _down(), "/user/engagement-preferences": _down()})
    plan = await registry._content_plan(lb, registry.ContentPlanArgs(weeks=2))
    assert plan["open_slots"] is None and plan["scheduled_posts"] is None
    assert plan["posting_days"] is None and plan["posts_per_week"] is None
    assert plan["upcoming_tasks"] == {"unavailable": "unavailable"}
    assert plan["best_times"] == {"unavailable": "unavailable"}


_DM_TASK = {"kind": "DM", "id": 4, "title": "Pat Lee", "status": "approved", "scheduled_time": None}
_POST_TASK = {"kind": "Post", "id": 5, "title": "My own post", "status": "approved",
              "scheduled_time": None}


async def test_a_dm_task_title_is_withheld_from_mcp_in_the_plan() -> None:
    plan = await registry._content_plan(_plan_lb([], tasks=[_DM_TASK, _POST_TASK]),
                                        registry.ContentPlanArgs(weeks=1))
    dm, post = plan["upcoming_tasks"]
    assert dm["title"] is None and "third_party" not in dm
    assert post["title"] == "My own post"


@pytest.mark.parametrize("runner, args", [
    (registry._dashboard, registry.DashboardArgs()),
    (registry._content_plan, registry.ContentPlanArgs()),
])
async def test_a_dm_task_title_is_fenced_for_outreach(runner: Any, args: Any) -> None:
    lb = _plan_lb([], scope="mcp_outreach", tasks=[_DM_TASK, _POST_TASK])
    lb.answers["/dashboard/stats"] = {}
    body = await runner(lb, args)
    dm, post = body["upcoming"] if "upcoming" in body else body["upcoming_tasks"]
    assert "title" not in dm
    assert dm["third_party"] == {"note": THIRD_PARTY_NOTE, "title": "Pat Lee"}
    assert post["title"] == "My own post"


async def test_the_overview_withholds_dm_titles_from_mcp() -> None:
    lb = FakeLoopback({"/dashboard/stats": {}, "/dashboard/planned-tasks": {"tasks": [_DM_TASK]},
                       "/user/automation-status": {}, "/user/linkedin-signin-status": {},
                       "/user/account-readiness": {}, "/content_generation_status": None})
    overview = await registry._account_overview(lb, registry.NoArgs())
    assert overview["dashboard"]["upcoming"][0]["title"] is None


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
    assert body["account_name"] is None and body["linkedin_profile_url"] is None
    assert "subscription" not in body


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
