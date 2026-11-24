"""The owner email for an MCP token event: minted, rotated, or exposed and revoked.

One helper for the mint router and the gate, so every path sends the same notice and fails the
same way: an email that does not go out is logged as a WARNING and never fails the action it is
about (plan §3.5: "if the email fails, the mint still succeeds").
"""

from typing import Optional

from cqc_lem.utilities.logger import log_warning


def notify_token_event(user_id: int, kind: str, label: Optional[str] = None,
                       scope: Optional[str] = None) -> bool:
    """Email the account owner about one MCP token event. Never raises.

    Args:
        user_id: The account the token belongs to.
        kind: `minted`, `rotated` or `exposed`.
        label: The token's label, when known.
        scope: The token's scope, when known.

    Returns:
        True when a provider accepted the message.
    """
    from cqc_lem.utilities.db import get_user_email
    from cqc_lem.utilities.email import send_mcp_token_email

    sent = False
    try:
        email = get_user_email(user_id)
        sent = bool(email) and bool(send_mcp_token_email(email, kind, label=label, scope=scope))
    except Exception as exc:  # noqa: BLE001 — a notice must never fail the action it reports.
        log_warning("MCP token notification email failed", exc=exc, user_id=user_id, kind=kind)
        return False
    if not sent:
        log_warning("MCP token notification email not sent", user_id=user_id, kind=kind)
    return sent
