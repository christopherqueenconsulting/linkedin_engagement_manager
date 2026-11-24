"""The end-user MCP server, mounted at exactly `POST /api/mcp` (epic #2682, `docs/agent-mcp-plan.md`).

A user's own coding agent connects with a URL plus a bearer token. The gate (`gate.McpGate`) does
every HTTP-level check itself, as JSON, before the SDK sees a byte. The SDK session manager is
started by a lifespan that fails OPEN (`lifespan.mcp_lifespan`), so a broken MCP stack answers
503 on its own path and never takes `/health` or the rest of the API down with it.

Phase P2 adds the `mcp` / `mcp_outreach` session scopes, their mint routes
(`api/routers/mcp_tokens.py`) and the gate's auth steps, so an MCP token is admitted to the SDK.
No tool is registered until P3, and on raw `/api` an MCP token is refused without this worker's
dispatch secret (`dispatch.py`).
"""
