"""The AI/media spend tools: `lem_generate_post`, `lem_generate_post_image`, `lem_generate_post_video`.

W2b, issue #2689, owner ruling O4. Each tool is one `POST` through the in-process loopback to its own
narrow route under `/api/user/mcp-spend/` (`api/routers/mcp_spend.py`), which holds every rule
server-side. The tool adds only what belongs to the MCP layer:

- the `mcp-server-writes` rollout flag, checked first, so a dark account spends nothing (the route
  checks it again);
- the per-user WRITE budgets (`limits.WRITE_BUDGETS`), the same ones every write tool spends;
- the answer shape: `{post_id, status: 'queued', post_status: 'pending', review_url, note}`.

Nothing here runs inline. Text, image and video generation are queued in Celery by the route, so
every call returns well inside the 20-second tool budget, and everything lands PENDING for the
user's review. A refusal keeps the route's own code: `daily_ai_cap` (the free-trial daily AI cap),
`mcp_spend_cap` (the MCP sub-cap, env `MCP_SPEND_TEXT` / `_IMAGE` / `_VIDEO`),
`not_created_by_this_token`, `requires_pending` and the rest.

This module does not import `tools` at load time: `tools` builds its registry from `spend_tools()`,
which imports `ToolSpec` when it is called, so either module may be imported first.
"""

import asyncio
import datetime as dt
from typing import TYPE_CHECKING, Any, Optional

from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, ConfigDict, Field

from cqc_lem.api.mcp.lifespan import mcp_state
from cqc_lem.api.mcp.limits import WRITE_BUDGETS, LimitsUnavailable, consume_budgets
from cqc_lem.api.mcp.loopback import (
    RATE_LIMITED,
    REFUSED,
    UPSTREAM_ERROR,
    Loopback,
    ToolFailure,
    _refusal_from,
)
from cqc_lem.utilities.flags import MCP_SERVER_WRITES, flag_enabled

if TYPE_CHECKING:
    from cqc_lem.api.mcp.tools import ToolSpec

POST = "POST"
SPEND_POST_PATH = "/user/mcp-spend/post"
SPEND_IMAGE_PATH = "/user/mcp-spend/post-image"
SPEND_VIDEO_PATH = "/user/mcp-spend/post-video"

PENDING_NOTE = ("Queued. The result lands as a PENDING post for your approval in LEM; nothing is "
                "published or sent until you approve it there.")


class SpendArgs(BaseModel):
    """Base for the spend tools' arguments: unknown keys are refused, never ignored."""

    model_config = ConfigDict(extra="forbid")


class GeneratePostArgs(SpendArgs):
    """What LEM's writer should draft a post about."""

    topic: str = Field(..., min_length=10, max_length=1000, description=(
        "What the post is about and any direction for it, in the user's own terms."))
    scheduled_datetime: Optional[dt.datetime] = Field(None, description=(
        "When the post should publish once approved (ISO 8601 with an offset), 1 hour to 90 days "
        "out. Default: 24 hours from now."))


class SpendPostIdArgs(SpendArgs):
    """A pending post this token created with `lem_generate_post` or `lem_draft_post`."""

    post_id: int = Field(..., ge=1)


async def _spend_write_budget(lb: Loopback) -> None:
    try:
        retry_after = await run_in_threadpool(consume_budgets, lb.principal.user_id, WRITE_BUDGETS)
    except LimitsUnavailable as exc:
        raise ToolFailure(UPSTREAM_ERROR, "mcp_unavailable",
                          "The LEM MCP server is unavailable right now. Try again shortly.") from exc
    if retry_after is not None:
        raise ToolFailure(RATE_LIMITED, "write_budget_exhausted",
                          f"This account's MCP write budget is spent. Retry in {retry_after}s.")


async def _require_writes(lb: Loopback) -> None:
    if not await run_in_threadpool(flag_enabled, MCP_SERVER_WRITES, lb.principal.user_id):
        raise ToolFailure(REFUSED, "writes_disabled", "Writes are not enabled for this account.")


async def post_route(lb: Loopback, path: str, body: dict[str, Any]) -> Any:
    """POST one LEM route as the caller and return its `detail`.

    The same transport, headers and per-call task as `Loopback.get`; the token rides only in the
    cookie header, and the body carries the cookie sentinel, never the token.

    Args:
        lb: The caller's loopback.
        path: The route path under `/api`.
        body: The JSON body; `session_token` is always the cookie sentinel.

    Returns:
        The route's `detail` on a 200.

    Raises:
        ToolFailure: The route answered anything but 200.
    """
    from cqc_lem.api.main import COOKIE_SESSION_SENTINEL  # lazy: main imports this package

    payload = {k: v for k, v in body.items() if v is not None}
    payload["session_token"] = COOKIE_SESSION_SENTINEL
    async with lb._limiter:
        lb.inner_calls += 1
        client = lb._http()
        task = asyncio.create_task(client.post(f"/api{path}", json=payload))
        response = await task
    lb.last_status = response.status_code
    try:
        parsed = response.json()
    except ValueError:
        parsed = None
    if response.status_code != 200:
        raise _refusal_from(response.status_code, parsed)
    return parsed.get("detail") if isinstance(parsed, dict) else None


def _review_url() -> Optional[str]:
    base = (mcp_state.base_url or "").rstrip("/")
    return f"{base}/content" if base else None


async def _spend(lb: Loopback, path: str, body: dict[str, Any]) -> dict[str, Any]:
    await _require_writes(lb)
    await _spend_write_budget(lb)
    detail = await post_route(lb, path, body) or {}
    return {"post_id": detail.get("post_id"), "kind": detail.get("kind"),
            "status": detail.get("status"), "post_status": detail.get("post_status"),
            "review_url": _review_url(), "note": PENDING_NOTE}


async def _generate_post(lb: Loopback, args: GeneratePostArgs) -> dict[str, Any]:
    when = args.scheduled_datetime.isoformat() if args.scheduled_datetime else None
    return await _spend(lb, SPEND_POST_PATH, {"topic": args.topic, "scheduled_datetime": when})


async def _generate_post_image(lb: Loopback, args: SpendPostIdArgs) -> dict[str, Any]:
    return await _spend(lb, SPEND_IMAGE_PATH, {"post_id": args.post_id})


async def _generate_post_video(lb: Loopback, args: SpendPostIdArgs) -> dict[str, Any]:
    return await _spend(lb, SPEND_VIDEO_PATH, {"post_id": args.post_id})


_SPEND_RULES = (" Counts against the account's daily AI limit and a daily MCP limit; refused with "
                "`daily_ai_cap` or `mcp_spend_cap` when either is reached. Returns at once with "
                "`status: queued`; check the post later with `lem_get_post`.")


def spend_tools() -> "tuple[ToolSpec, ...]":
    """The three spend tools as registry entries, for both MCP scopes."""
    from cqc_lem.api.mcp.tools import BOTH_SCOPES, ToolSpec

    return (
        ToolSpec("lem_generate_post", "Generate a post",
                 "Have LEM write a new post about `topic` in the user's own voice and settings. It "
                 "lands as a PENDING draft for the user's approval." + _SPEND_RULES,
                 GeneratePostArgs, BOTH_SCOPES, ((POST, SPEND_POST_PATH),), _generate_post,
                 write=True),
        ToolSpec("lem_generate_post_image", "Generate a post image",
                 "Render an image for a pending text post THIS token created, once its text is "
                 "written. The post stays PENDING." + _SPEND_RULES,
                 SpendPostIdArgs, BOTH_SCOPES, ((POST, SPEND_IMAGE_PATH),), _generate_post_image,
                 write=True),
        ToolSpec("lem_generate_post_video", "Generate a post video",
                 "Render a video for a pending text post THIS token created, once its text is "
                 "written; the post becomes a video post when the video lands, still PENDING. "
                 "Takes minutes." + _SPEND_RULES,
                 SpendPostIdArgs, BOTH_SCOPES, ((POST, SPEND_VIDEO_PATH),), _generate_post_video,
                 write=True),
    )


__all__ = ["spend_tools", "post_route", "GeneratePostArgs", "SpendPostIdArgs", "PENDING_NOTE",
           "SPEND_POST_PATH", "SPEND_IMAGE_PATH", "SPEND_VIDEO_PATH"]
