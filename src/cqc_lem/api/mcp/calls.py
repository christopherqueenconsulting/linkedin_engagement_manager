"""Serving `tools/list`, `tools/call` and resources for one admitted caller (plan §4, §7).

The SDK's own tool machinery is not used for these, on purpose. It builds an argument model from a
function signature (which ignores unknown keys instead of refusing them), returns a validation error
whose text echoes the rejected values, and lists every registered tool to every caller. Here:

- `tools/list` returns exactly the registry's tools for the caller's scope (`tools_for_scope`), so
  an `mcp` token never even sees `lem_list_outreach`;
- `tools/call` refuses a tool outside the scope, validates with the tool's own `extra="forbid"`
  model and reports only WHICH fields failed, runs the tool under a 20s limit, and shapes every
  result through `redact.finalize`;
- each call is logged at INFO (tool, outcome, latency; never arguments or results) and reported as
  `mcp_tool_call` and, content-free, to PostHog MCP analytics (`observability.capture_mcp_analytics`).
"""

import asyncio
import json
import time
from typing import Any, Optional

from mcp.shared.exceptions import MCPError
from mcp_types import (
    INVALID_PARAMS,
    CallToolResult,
    ListResourcesResult,
    ListResourceTemplatesResult,
    ListToolsResult,
    ReadResourceResult,
    Resource,
    ResourceTemplate,
    TextContent,
    TextResourceContents,
    Tool,
    ToolAnnotations,
)
from pydantic import ValidationError
from starlette.requests import Request

from cqc_lem.api.mcp import tools as registry
from cqc_lem.api.mcp.loopback import (
    INVALID_ARGS,
    OK,
    REFUSED,
    TIMEOUT,
    UPSTREAM_ERROR,
    Loopback,
    ToolFailure,
    loopback_for,
)
from cqc_lem.api.mcp.redact import finalize
from cqc_lem.api.mcp.server import GUIDE_URI, POLICY_URI
from cqc_lem.utilities.logger import log_error, log_info
from cqc_lem.utilities.observability import capture_mcp_analytics, track_mcp_tool_call

TOOL_TIMEOUT_SECONDS = 20.0

# The one INFO line per tool call. `docs/mcp-server.md` § "Am I healthy?" greps for it, and
# `test_mcp_health_grep.py` proves that grep matches this line as the file handler formats it.
MCP_TOOL_CALL_LOG = "MCP tool call: tool={tool} outcome={outcome} latency_ms={latency_ms}"


def principal_of(request: Optional[Request]) -> Any:
    """The `McpPrincipal` the gate put on this request, or None."""
    if request is None:
        return None
    return (request.scope.get("state") or {}).get("mcp_principal")


def _input_schema(spec: registry.ToolSpec) -> dict[str, Any]:
    schema = spec.args_model.model_json_schema()
    schema.setdefault("type", "object")
    schema.setdefault("properties", {})
    schema["additionalProperties"] = False
    return schema


def mcp_tool(spec: registry.ToolSpec) -> Tool:
    """One registry entry as the MCP `Tool` a client lists."""
    return Tool(name=spec.name, title=spec.title, description=spec.description,
                input_schema=_input_schema(spec),
                annotations=ToolAnnotations(title=spec.title, read_only_hint=True,
                                            destructive_hint=False, idempotent_hint=True,
                                            open_world_hint=spec.open_world))


def list_tools(request: Optional[Request]) -> ListToolsResult:
    """`tools/list` for this caller: exactly its scope's tools, nothing for an unadmitted request."""
    principal = principal_of(request)
    scope = getattr(principal, "scope", None)
    return ListToolsResult(tools=[mcp_tool(spec) for spec in registry.tools_for_scope(scope)])


def _validation_failure(exc: ValidationError) -> ToolFailure:
    """Name the fields that failed and why, never the values that were sent."""
    problems = []
    for error in exc.errors(include_input=False, include_url=False, include_context=False):
        field = ".".join(str(part) for part in error.get("loc", ())) or "(arguments)"
        problems.append(f"{field}: {error.get('msg')}")
    return ToolFailure(INVALID_ARGS, "invalid_arguments",
                       "Invalid arguments. " + "; ".join(problems[:10]))


def _summary(spec: registry.ToolSpec, payload: Any) -> str:
    if isinstance(payload, dict):
        for key in ("posts", "items", "activity", "history"):
            if isinstance(payload.get(key), list):
                return f"{spec.title}: {len(payload[key])} item(s)."
    return f"{spec.title}."


def _ok(spec: registry.ToolSpec, payload: Any) -> CallToolResult:
    structured = payload if isinstance(payload, dict) else {"result": payload}
    text = _summary(spec, structured) + "\n" + json.dumps(structured, default=str,
                                                          ensure_ascii=False)
    return CallToolResult(content=[TextContent(type="text", text=text)],
                          structured_content=structured, is_error=False)


def _error(failure: ToolFailure) -> CallToolResult:
    body = {"error": {"code": failure.code, "outcome": failure.outcome, "message": failure.message}}
    return CallToolResult(content=[TextContent(type="text", text=f"{failure.code}: {failure.message}")],
                          structured_content=body, is_error=True)


def _session_key(principal: Any) -> Optional[str]:
    session_id = getattr(principal, "session_id", None)
    return f"mcp-session-{session_id}" if session_id is not None else None


async def call_tool(request: Optional[Request], name: str,
                    arguments: Optional[dict[str, Any]]) -> CallToolResult:
    """Serve one `tools/call` for the admitted caller. Never raises.

    Args:
        request: The outer `POST /api/mcp` request (from the SDK's request context).
        name: The tool the client asked for.
        arguments: The client's arguments, unvalidated.

    Returns:
        The tool's result, or an `isError` result naming a stable code.
    """
    started = time.monotonic()
    principal = principal_of(request)
    spec = registry.TOOLS_BY_NAME.get(name) if isinstance(name, str) else None
    scope = getattr(principal, "scope", None)
    if spec is not None and scope not in spec.scopes:
        spec = None
    tool_label = spec.name if spec is not None else "unknown"
    outcome, loopback = OK, None
    result: CallToolResult
    try:
        if principal is None:
            raise ToolFailure(REFUSED, "not_authenticated", "This MCP request is not authenticated.")
        if spec is None:
            raise registry.refused_tool(str(name))
        try:
            args = spec.args_model.model_validate(arguments or {})
        except ValidationError as exc:
            raise _validation_failure(exc) from None
        loopback = loopback_for(request)
        payload = await asyncio.wait_for(spec.run(loopback, args), TOOL_TIMEOUT_SECONDS)
        shaped = finalize(payload)
        if (isinstance(shaped, dict) and shaped.get("truncated")
                and "limit" in spec.args_model.model_fields):
            # A page trimmed to fit 64 KB would have a cursor that skips the rows it cut. A tool
            # with a `limit` refuses instead, so every row stays reachable with a smaller page.
            raise ToolFailure(INVALID_ARGS, "result_too_large",
                              "This result is over 64 KB. Ask again with a lower `limit`.")
        result = _ok(spec, shaped)
    except ToolFailure as failure:
        outcome = failure.outcome
        result = _error(failure)
    except asyncio.TimeoutError:
        # Every v1 tool is a read, so a call that ran out of time changed nothing: `timeout`, and
        # safe to retry. (A write would be `unknown`, plan §5.)
        outcome = TIMEOUT
        result = _error(ToolFailure(TIMEOUT, "timeout",
                                    "LEM took too long to answer. Try again, or ask for less."))
    except Exception as exc:  # noqa: BLE001 — a tool must answer, never crash the MCP request.
        outcome = UPSTREAM_ERROR
        log_error("MCP tool failed unexpectedly", exc=exc, tool=tool_label,
                  user_id=getattr(principal, "user_id", None))
        result = _error(ToolFailure(UPSTREAM_ERROR, "upstream_error",
                                    "LEM could not answer this right now. Try again shortly."))
    finally:
        if loopback is not None:
            await loopback.aclose()
    _report(principal, tool_label, outcome, started, loopback)
    return result


def _report(principal: Any, tool: str, outcome: str, started: float,
            loopback: Optional[Loopback]) -> None:
    latency_ms = int((time.monotonic() - started) * 1000)
    user_id = getattr(principal, "user_id", None)
    # The file log renders only the documented context keys (`logger._LevelFormatter.CONTEXT_KEYS`),
    # so tool, outcome and latency go in the MESSAGE, where `docs/mcp-server.md`'s health grep reads
    # them. INFO, so the warning-escalation dedup key never sees the varying text.
    log_info(MCP_TOOL_CALL_LOG.format(tool=tool, outcome=outcome, latency_ms=latency_ms),
             user_id=user_id, tool=tool, outcome=outcome, latency_ms=latency_ms)
    if user_id is None:
        return
    track_mcp_tool_call(user_id, tool, outcome, principal.client_name, principal.scope, latency_ms,
                        loopback.last_status if loopback else None,
                        loopback.inner_calls if loopback else 0)
    capture_mcp_analytics(tool, user_id, _session_key(principal), float(latency_ms), outcome,
                          principal.client_name)


# --- resources (each has a tool equivalent) ---------------------------------------------------------

OVERVIEW_URI = "lem://account/overview"
PLAN_URI = "lem://plan/upcoming"
POST_TEMPLATE = "lem://post/{post_id}"
_POST_PREFIX = "lem://post/"

_STATIC = {
    GUIDE_URI: "\n\n".join(registry.EXPLANATIONS[t] for t in
                           ("overview", "statuses", "gates", "approval")),
    POLICY_URI: registry.EXPLANATIONS["agent_capabilities"] + (
        " Nothing an agent creates is ever sent: it lands as pending, and only the user approves it "
        "in LEM. Never tell the user something was sent, posted or approved."),
}


def list_resources() -> ListResourcesResult:
    """The fixed resource list (the same for both scopes)."""
    return ListResourcesResult(resources=[
        Resource(name="how-lem-works", uri=GUIDE_URI, mime_type="text/plain",
                 description="How LEM works: statuses, gates and the approval rule."),
        Resource(name="write-rules", uri=POLICY_URI, mime_type="text/plain",
                 description="What an agent may and may not do in LEM."),
        Resource(name="account-overview", uri=OVERVIEW_URI, mime_type="application/json",
                 description="Same as lem_get_account_overview."),
        Resource(name="plan-upcoming", uri=PLAN_URI, mime_type="application/json",
                 description="Same as lem_get_content_plan for one week."),
    ])


def list_resource_templates() -> ListResourceTemplatesResult:
    """The one templated resource: a post by id."""
    return ListResourceTemplatesResult(resource_templates=[
        ResourceTemplate(name="post", uri_template=POST_TEMPLATE, mime_type="application/json",
                         description="Same as lem_get_post."),
    ])


async def read_resource(request: Optional[Request], uri: str) -> ReadResourceResult:
    """Read one resource for the admitted caller, through the same tool runners.

    Args:
        request: The outer `POST /api/mcp` request.
        uri: The resource URI the client asked for.

    Returns:
        The resource's contents.

    Raises:
        MCPError: Unknown URI, a malformed post id, or the underlying read refused.
    """
    uri = str(uri)
    if uri in _STATIC:
        return ReadResourceResult(contents=[TextResourceContents(uri=uri, text=_STATIC[uri],
                                                                 mime_type="text/plain")])
    if uri == OVERVIEW_URI:
        name, arguments = "lem_get_account_overview", {}
    elif uri == PLAN_URI:
        name, arguments = "lem_get_content_plan", {"weeks": 1}
    elif uri.startswith(_POST_PREFIX) and uri[len(_POST_PREFIX):].isdigit():
        name, arguments = "lem_get_post", {"post_id": int(uri[len(_POST_PREFIX):])}
    else:
        raise MCPError(code=INVALID_PARAMS, message="Unknown resource.")
    result = await call_tool(request, name, arguments)
    if result.is_error:
        code = (result.structured_content or {}).get("error", {}).get("code", "refused")
        raise MCPError(code=INVALID_PARAMS, message=f"Resource unavailable: {code}.")
    text = json.dumps(result.structured_content, default=str, ensure_ascii=False)
    return ReadResourceResult(contents=[TextResourceContents(uri=uri, text=text,
                                                             mime_type="application/json")])
