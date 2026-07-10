"""Helpers for the event-driven reply feature: a user forwards LinkedIn "commented on your post"
notification emails to a tokenized address (reply+<token>@parse.<domain>) that SendGrid Inbound
Parse pipes to our webhook. Mirrors the PIN-verification email helpers (verification_pin.py) — same
parse-domain derivation and tokenized-address style — but the token is a PERSISTENT per-user id
(stored on the users row) since the forward filter is set up once and forwards indefinitely.
"""
import os
import re
from typing import Optional

# Reuse the exact parse-domain derivation used by the PIN flow (SENDGRID_FROM_EMAIL /
# PUBLIC_BASE_URL, overridable by LINKEDIN_PARSE_DOMAIN) so both inbound addresses share a domain.
from cqc_lem.utilities.linkedin.verification_pin import _default_parse_domain

# Tokenized local part reply+<token>@parse.domain — appears in SendGrid's `to`/`envelope` fields.
_REPLY_TOKEN_RE = re.compile(r"reply\+([A-Za-z0-9]+)@")

# Subject/body phrases LinkedIn uses when someone COMMENTS or REPLIES on the user's content (trigger
# a reply sweep) vs merely REACTS/likes/mentions (ignore — nothing to reply to).
_COMMENT_PHRASES = ("commented on", "replied to", "left a comment", "comment on your")
_REACTION_PHRASES = ("liked", "reacted to", "celebrates", "loves", "supports",
                     "found your post", "mentioned you", "started following")


def reply_inbound_address(token: str) -> str:
    """The forwarding address the user points a Gmail filter at. Host is LINKEDIN_PARSE_DOMAIN if
    set, else derived from the configured domain (see _default_parse_domain)."""
    domain = os.getenv("LINKEDIN_PARSE_DOMAIN") or _default_parse_domain()
    return f"reply+{token}@{domain}"


def extract_reply_token_from_address(value: str) -> Optional[str]:
    """Pull the persistent per-user token out of a `to`/`envelope` value from the inbound webhook."""
    if not value:
        return None
    m = _REPLY_TOKEN_RE.search(value)
    return m.group(1) if m else None


def is_comment_notification(subject: str, text: str = "") -> bool:
    """True when the forwarded LinkedIn email is a COMMENT/REPLY notification (something to reply to),
    False for reactions/likes/mentions/other. Reads the subject plus the top of the body (like
    extract_pin_from_text) so quoted history can't flip the classification."""
    subject = (subject or "").lower()
    # Top of the body only — stop at quoted history so a forwarded chain doesn't add noise.
    head_lines = []
    for line in (text or "").splitlines():
        if line.lstrip().startswith(">") or line.strip().lower().startswith("on "):
            break
        head_lines.append(line)
    head = ("\n".join(head_lines)).lower()
    hay = f"{subject}\n{head}"
    if any(p in hay for p in _COMMENT_PHRASES):
        return True
    # Subject alone is the most reliable signal; if it's clearly a reaction, reject.
    if any(p in subject for p in _REACTION_PHRASES):
        return False
    return False
