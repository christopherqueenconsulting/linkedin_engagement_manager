"""The audience binding for MCP tokens: a per-worker secret only the in-process loopback holds.

An `mcp` / `mcp_outreach` token is presented to `POST /api/mcp`, and nowhere else. Its tools reach
LEM's real routes through an in-process ASGI loopback (P3, `docs/agent-mcp-plan.md` §2), so those
routes see an ordinary cookie request. Without a binding, the same token sent straight at raw
`/api` as a `lem_session` cookie would skip everything the gate enforces: the kill switch, the
rollout flag, the concurrency and budget limits, and the access record.

So each worker draws a random secret at import, and `_scope_checked` refuses an mcp-scoped session
unless the request carries it in `DISPATCH_HEADER` (`docs/agent-mcp-plan.md` §3.4). The loopback
runs in the same worker that drew it and never sends it over a network transport, so nothing
outside the process can learn it. A restarted worker draws a new one; nothing persists it.
"""

import hmac
import secrets
from typing import Optional

DISPATCH_HEADER = "X-LEM-MCP-Dispatch"

_MCP_DISPATCH_SECRET = secrets.token_hex(32)


def dispatch_secret() -> str:
    """This worker's dispatch secret, for the in-process loopback to send. Never log it."""
    return _MCP_DISPATCH_SECRET


def dispatch_header_valid(value: Optional[str]) -> bool:
    """Is `value` this worker's dispatch secret? Constant-time, and False for an absent header.

    Args:
        value: The `X-LEM-MCP-Dispatch` header as received, or None.

    Returns:
        True only for an exact match.
    """
    if not value:
        return False
    return hmac.compare_digest(value.encode("utf-8", "replace"),
                               _MCP_DISPATCH_SECRET.encode("ascii"))
