"""Helpers for driving an ADMITTED MCP request end to end in the unit lane (P3).

The gate's resolver and the inner routes' resolver both answer with the same MCP session row, the
rollout flag is on through its env fallback, and Redis is the in-memory fake, so a JSON-RPC body
posted to `/api/mcp` runs the real SDK, the real tool registry and the real loopback into the real
routes. Only the routes' own database reads are patched, per test.
"""

import json
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator, Optional
from unittest.mock import MagicMock, patch

TOKEN = "lem_mcp_" + "p3canary" * 4
UID = 77
EXPIRES = datetime(2099, 1, 1, tzinfo=timezone.utc)
JSON_HEADERS = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}


def session_row(scope: str = "mcp", user_id: int = UID) -> dict:
    """The row both resolvers return."""
    return {"user_id": user_id, "scope": scope, "session_id": 901, "expires_at": EXPIRES}


@contextmanager
def admitted(resolver: MagicMock, scope: str = "mcp", user_id: int = UID) -> Iterator[MagicMock]:
    """Admit `TOKEN` as an MCP session of `scope` at the gate AND at every inner route.

    Yields the inner resolver mock, so a test can assert what the inner requests presented.
    """
    row = session_row(scope, user_id)
    resolver.return_value = row
    with ExitStack() as stack:
        inner = stack.enter_context(
            patch("cqc_lem.api.main._db_resolve_session",
                  side_effect=lambda token: dict(row) if token == TOKEN else None))
        stack.enter_context(patch("cqc_lem.api.main._safe_auth_event"))
        stack.enter_context(patch("cqc_lem.api.main.get_session_id", return_value=901))
        yield inner


def rpc(client: Any, method: str, params: Optional[dict] = None, token: str = TOKEN,
        request_id: int = 1, **headers: str) -> Any:
    """POST one JSON-RPC request to the gate and return the HTTP response."""
    body: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        body["params"] = params
    return client.post("/api/mcp", content=json.dumps(body),
                       headers={**JSON_HEADERS, "Authorization": f"Bearer {token}", **headers})


def call(client: Any, name: str, arguments: Optional[dict] = None, **headers: str) -> dict:
    """`tools/call` one tool and return the JSON-RPC `result`."""
    resp = rpc(client, "tools/call", {"name": name, "arguments": arguments or {}}, **headers)
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert "result" in payload, payload
    return payload["result"]


def structured(result: dict) -> dict:
    """The `structuredContent` of a tool result."""
    return result.get("structuredContent") or {}
