"""Owner-only: set `users.account_kind` for one account (docs/agent-roles.md § Sandbox tenant).

Run on the host, inside the app container, by the account owner — it is the ONLY writer of the
column. No API route, no machine session and no operator token can reach it (a test walks the route
table and the api package to keep it so):

    ./scripts/compose.sh exec -T celery_worker python -m cqc_lem.utilities.account_kind_admin
    ... --email qa-sandbox@example.com --kind sandbox

Refusals (exit code 2, nothing written):

- **An admin account cannot become `internal` or `sandbox`.** Both kinds hand approval authority to a
  machine seat; an admin account is the one that must never be handed over by a flag.
- **A `sandbox` account that stores real LinkedIn credentials** (a password, an access token or an
  `li_at` cookie) is refused unless `--allow-credentials` is passed: a sandbox account is meant to have
  none, and flagging one that does is almost always flagging the wrong account. If the credentials
  cannot be read, that counts as "may have some".
- **A non-standard account cannot be reset to `standard` silently**: that turns a dry-run account into
  one whose approved items reach real people, so it needs `--allow-reset-to-standard`.

Every change writes an `account_kind_changed` row to `auth_audit_log` (best effort, reported on
stderr if it could not be written) and is logged. `--dry-run` prints what would happen and writes
nothing. It makes no LinkedIn call and opens no browser.
"""

import argparse
import sys
from typing import Optional, Sequence

from cqc_lem.platform.db.enums import AuthAuditEvent
from cqc_lem.utilities.db import (
    get_linkedin_credential_flags,
    get_user_id,
    is_user_admin,
    record_auth_event,
    set_user_account_kind,
)
from cqc_lem.utilities.engagement_mode import (
    ACCOUNT_KIND_INTERNAL,
    ACCOUNT_KIND_SANDBOX,
    ACCOUNT_KIND_STANDARD,
    read_account_kind,
)
from cqc_lem.utilities.logger import log_info

KINDS = (ACCOUNT_KIND_STANDARD, ACCOUNT_KIND_INTERNAL, ACCOUNT_KIND_SANDBOX)
EXIT_OK = 0
EXIT_REFUSED = 2
EXIT_FAILED = 3


def _out(message: str) -> None:
    """Write a line to stdout (a command-line tool's output, not a log line)."""
    sys.stdout.write(message + "\n")


def _err(message: str) -> None:
    """Write a line to stderr."""
    sys.stderr.write(message + "\n")


def _refuse(message: str) -> int:
    _err(f"refused: {message}")
    return EXIT_REFUSED


def run(email: str, kind: str, allow_credentials: bool = False,
        allow_reset_to_standard: bool = False, dry_run: bool = False) -> int:
    """Apply one change, or refuse. Returns the process exit code.

    Args:
        email: the account's address (the only way to name it; an id is never taken).
        kind: `standard`, `internal` or `sandbox`.
        allow_credentials: permit flagging a sandbox account that stores LinkedIn credentials.
        allow_reset_to_standard: permit moving an `internal`/`sandbox` account back to `standard`.
        dry_run: report what would happen and write nothing.
    """
    if kind not in KINDS:
        return _refuse(f"unknown kind {kind!r}; expected one of {', '.join(KINDS)}")
    user_id = get_user_id((email or "").strip().lower())
    if not user_id:
        return _refuse("no account with that address (or the lookup failed)")
    current = read_account_kind(user_id)
    if current is None:
        return _refuse("the account's current kind cannot be read; nothing was changed")
    if current == kind:
        _out(f"unchanged: account {user_id} is already {kind}")
        return EXIT_OK

    if kind in (ACCOUNT_KIND_INTERNAL, ACCOUNT_KIND_SANDBOX) and is_user_admin(user_id):
        return _refuse(f"account {user_id} is an admin; an admin account cannot be flagged {kind}")
    if kind == ACCOUNT_KIND_STANDARD and current != ACCOUNT_KIND_STANDARD \
            and not allow_reset_to_standard:
        return _refuse(f"account {user_id} is {current}; moving it to standard makes its approved "
                       "items reach real people. Pass --allow-reset-to-standard to do it deliberately")
    if kind == ACCOUNT_KIND_SANDBOX and not allow_credentials:
        flags = get_linkedin_credential_flags(user_id)
        if flags is None or any(flags.values()):
            held = "cannot be read" if flags is None else \
                ", ".join(name for name, present in flags.items() if present)
            return _refuse(f"account {user_id} may hold real LinkedIn credentials ({held}); a "
                           "sandbox account should hold none. Pass --allow-credentials to override")

    if dry_run:
        _out(f"dry run: would set account {user_id} from {current} to {kind}")
        return EXIT_OK
    if not set_user_account_kind(user_id, kind):
        _err("failed: the write did not change exactly one row")
        return EXIT_FAILED
    log_info(f"account_kind changed {current} -> {kind}", user_id=user_id, action_type="account_kind")
    if not record_auth_event(AuthAuditEvent.ACCOUNT_KIND_CHANGED, user_id=user_id,
                             details={"from": current, "to": kind,
                                      "allow_credentials": bool(allow_credentials),
                                      "allow_reset_to_standard": bool(allow_reset_to_standard)}):
        _err("warning: the audit row could not be written; record this change by hand")
    _out(f"ok: account {user_id} {current} -> {kind}")
    return EXIT_OK


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Parse the command line and run."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--email", required=True)
    parser.add_argument("--kind", required=True, choices=KINDS)
    parser.add_argument("--allow-credentials", action="store_true")
    parser.add_argument("--allow-reset-to-standard", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    return run(args.email, args.kind, allow_credentials=args.allow_credentials,
               allow_reset_to_standard=args.allow_reset_to_standard, dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
