"""Session window enforce-readiness audit — read-only report with a verdict (issue #2793).

Turns the manual check left on #2767 ("after 14 days on enforce, no action 19:00-07:00 ET and at
most 4 sessions on any weekday") into a command. All logic lives in
`cqc_lem.utilities.session_window_audit` (shared with the weekly beat); this file is only the CLI.
It makes NO writes. Verdict: PASS / FAIL / INSUFFICIENT (see that module's docstring).

Runs from STDIN inside the production image, like `scripts/audit_outbound_drafts.py`, so it can
read the worker's own `/app/logs` and the mode the worker sees:

    sudo docker exec -i celery_worker python - [--json] [--days 14] [--user-id 1] \
        < scripts/session_window_audit.py

Exit code: 0 for PASS / INSUFFICIENT, 1 for FAIL.
"""

import argparse
import json
import os
import sys
from typing import Optional, Sequence

# Operator CLI: keep its output out of production error tracking (see `logger.telemetry_muted`).
os.environ.setdefault("LEM_TELEMETRY_MUTED", "1")

from cqc_lem.utilities.session_window_audit import (  # noqa: E402
    DEFAULT_AUDIT_DAYS,
    VERDICT_FAIL,
    audit_user,
    render_text,
)


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Parse flags, audit one user, print the report, and exit non-zero only on FAIL."""
    parser = argparse.ArgumentParser(prog="session_window_audit.py",
                                     description="Read-only session window enforce-readiness audit.")
    parser.add_argument("--json", action="store_true", dest="as_json", help="Emit JSON.")
    parser.add_argument("--days", type=int, default=DEFAULT_AUDIT_DAYS,
                        help="Complete local days to audit (default 14).")
    parser.add_argument("--user-id", type=int, default=1, help="User to audit (default 1).")
    args = parser.parse_args(argv)
    report = audit_user(args.user_id, days=max(1, args.days))
    sys.stdout.write((json.dumps(report.as_dict(), indent=2) if args.as_json else render_text(report))
                     + "\n")
    return 1 if report.verdict == VERDICT_FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
