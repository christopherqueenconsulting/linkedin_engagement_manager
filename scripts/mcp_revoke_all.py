"""Revoke every end-user MCP token, fleet-wide, with an audit row per user (epic #2682, plan §10).

The rollback's "revoke all" step: every live `mcp` and `mcp_outreach` session gets `revoked_at`,
and each affected account gets ONE `mcp_sessions_revoked` audit row (`{reason, revoked, scopes}`)
in the same transaction, so the record and the change can never disagree. It replaces raw SQL,
which would revoke the same rows and leave no trail.

Owner-run (Tier 2). Revert order: kill switch ON first (`scripts/mcp_kill.py on`), then this, then
revert the code — revoking before the revert means old code never meets an `mcp` scope it does
not know. Runs from STDIN inside the production image:

    sudo docker exec -i celery_worker python - --reason incident-2026-11 < scripts/mcp_revoke_all.py

Without `--yes` it asks for confirmation on a terminal and refuses when STDIN is not one; pass
`--yes` for a non-interactive run.

Exit codes: 0 done (including "nothing to revoke"), 1 refused (no confirmation), 2 the transaction
failed and nothing was revoked.
"""

import argparse
import os
import sys
from typing import Callable, Optional, Sequence, TextIO

os.environ.setdefault("LEM_TELEMETRY_MUTED", "1")

from cqc_lem.utilities.db import MCP_SESSION_SCOPES, revoke_sessions_by_scope  # noqa: E402


def main(argv: Optional[Sequence[str]] = None, out: TextIO = sys.stdout,
         confirm: Optional[Callable[[str], str]] = None) -> int:
    """Run the CLI.

    Args:
        argv: Arguments, defaulting to `sys.argv[1:]`.
        out: Where the result is written.
        confirm: Prompt function for the interactive confirmation; defaults to `input` on a TTY.

    Returns:
        The process exit code.
    """
    parser = argparse.ArgumentParser(description="Revoke every LEM MCP token, with audit rows.")
    parser.add_argument("--reason", required=True,
                        help="Kept in each audit row (64 characters at most).")
    parser.add_argument("--yes", action="store_true", help="Skip the confirmation prompt.")
    args = parser.parse_args(argv)

    if not args.yes:
        if confirm is None:
            if not sys.stdin.isatty():
                print("Refusing: not a terminal. Pass --yes to revoke without a prompt.", file=out)
                return 1
            confirm = input
        if confirm("Revoke EVERY mcp/mcp_outreach token? Type 'revoke' to continue: ") != "revoke":
            print("Refused: nothing was revoked.", file=out)
            return 1

    revoked = revoke_sessions_by_scope(MCP_SESSION_SCOPES, reason=args.reason)
    if revoked is None:
        print("The revoke transaction failed: nothing was revoked.", file=out)
        return 2
    print(f"Revoked {revoked} MCP session(s).", file=out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
