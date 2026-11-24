"""Turn the end-user MCP server's kill switch on or off from a shell (epic #2682, plan §10 step 1).

Sets or deletes the Redis key `mcp:kill`, the same key `POST /api/admin/mcp-kill` writes. On, every
`POST /api/mcp` answers 503 `mcp_disabled_by_operator` from the next request, on every worker and
both deploy colours. It is the fast stop; `MCP_SERVER_KILL=1` is the deploy-time one.

Owner-run (Tier 2). Each toggle writes one `mcp_kill_toggled` audit row with no user, so the
change is on record even though no session made it. Runs from STDIN inside the production image:

    sudo docker exec -i celery_worker python - on     < scripts/mcp_kill.py
    sudo docker exec -i celery_worker python - off    < scripts/mcp_kill.py
    sudo docker exec -i celery_worker python - status < scripts/mcp_kill.py

From a checkout: `PYTHONPATH=src poetry run python scripts/mcp_kill.py status`.

Exit codes: 0 done, 2 Redis unreachable (nothing changed; `status` reports it as unreadable).
"""

import argparse
import os
import sys
from typing import Optional, Sequence, TextIO

# Operator CLI, not the app: a Redis outage here must not file a production error-tracking issue
# (#1661, `logger.telemetry_muted`). Set BEFORE cqc_lem is imported.
os.environ.setdefault("LEM_TELEMETRY_MUTED", "1")

from cqc_lem.api.mcp.limits import KillState, read_kill_key, set_kill_key  # noqa: E402
from cqc_lem.utilities.db import AuthAuditEvent, record_auth_event  # noqa: E402


def main(argv: Optional[Sequence[str]] = None, out: TextIO = sys.stdout) -> int:
    """Run the CLI.

    Args:
        argv: Arguments, defaulting to `sys.argv[1:]`.
        out: Where the one-line result is written.

    Returns:
        The process exit code.
    """
    parser = argparse.ArgumentParser(description="Toggle the LEM MCP kill switch (mcp:kill).")
    parser.add_argument("action", choices=("on", "off", "status"))
    args = parser.parse_args(argv)

    if args.action == "status":
        state = read_kill_key()
        print(f"mcp:kill is {state.value}", file=out)
        return 2 if state is KillState.UNREADABLE else 0

    on = args.action == "on"
    if not set_kill_key(on):
        print("Redis unreachable: the MCP kill switch is unchanged", file=out)
        return 2
    record_auth_event(AuthAuditEvent.MCP_KILL_TOGGLED, success=True,
                      details={"on": on, "via": "scripts/mcp_kill.py"})
    print(f"mcp:kill set {'ON' if on else 'OFF'}", file=out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
