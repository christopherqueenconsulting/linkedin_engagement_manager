"""Email-reply verification-PIN exchange for LinkedIn login challenges.

When LinkedIn challenges a login, the automation drives the email-code path, emails
the user "reply with your code", and waits for them to reply. SendGrid Inbound Parse
POSTs that reply to our webhook, which extracts the 6-digit code and drops it here
(keyed by user). The paused login polls `get_pin` and enters it.

Redis-backed with a short TTL. A per-request token maps the tokenized Reply-To address
back to the user so an inbound reply is attributed to the right pending login. Fails
open: if Redis is unavailable the store no-ops and login falls back to its old behavior.
"""

import os
import re
import uuid
from typing import Optional

from cqc_lem.utilities.linkedin.rate_limit import _redis_client
from cqc_lem.utilities.logger import log_warning

_TOKEN_KEY = "linkedin:pin_token:{token}"   # token -> user_id (attribute inbound reply)
_PIN_KEY = "linkedin:pin:{user_id}"         # user_id -> submitted 6-digit code
_DEFAULT_TTL = 900                           # 15 min — challenge codes are short-lived

# LinkedIn codes are 6 digits. Match a standalone run so we don't grab digits out of a
# year or a phone number in the quoted reply/signature.
_PIN_RE = re.compile(r"(?<!\d)(\d{6})(?!\d)")

# Tokenized Reply-To local part: pin+<token>@parse.domain (also appears in SendGrid's
# `envelope` JSON). Attribute an inbound reply back to the pending login.
_TOKEN_RE = re.compile(r"pin\+([A-Za-z0-9]+)@")


def _default_parse_domain() -> str:
    """Derive `parse.<base-domain>` from existing domain config so the Reply-To uses the
    real domain, not a placeholder, when LINKEDIN_PARSE_DOMAIN isn't set explicitly.

    Prefers the SENDGRID_FROM_EMAIL domain (e.g. no-reply@christopherqueenconsulting.com
    → parse.christopherqueenconsulting.com), falling back to the registrable domain of
    PUBLIC_BASE_URL's host.
    """
    from_email = os.getenv("SENDGRID_FROM_EMAIL", "")
    base = from_email.rsplit("@", 1)[-1].strip().lower() if "@" in from_email else ""
    if not base:
        from urllib.parse import urlparse
        host = (urlparse(os.getenv("PUBLIC_BASE_URL", "")).hostname or "").lower()
        labels = [p for p in host.split(".") if p]
        base = ".".join(labels[-2:]) if len(labels) >= 2 else ""
    return f"parse.{base}" if base else "parse.example.com"


def pin_reply_address(token: str) -> str:
    """Tokenized Reply-To used in the PIN-request email. Host is LINKEDIN_PARSE_DOMAIN if
    set, else derived from the configured domain (see _default_parse_domain)."""
    domain = os.getenv("LINKEDIN_PARSE_DOMAIN") or _default_parse_domain()
    return f"pin+{token}@{domain}"


def extract_token_from_address(value: str) -> Optional[str]:
    """Pull the reply token out of a `to`/`envelope` value from the inbound webhook."""
    if not value:
        return None
    m = _TOKEN_RE.search(value)
    return m.group(1) if m else None


def _ttl() -> int:
    try:
        return int(os.getenv("LINKEDIN_PIN_TTL_SECONDS", str(_DEFAULT_TTL)))
    except ValueError:
        return _DEFAULT_TTL


def _decode(v) -> Optional[str]:
    if v is None:
        return None
    return v.decode() if isinstance(v, (bytes, bytearray)) else str(v)


def extract_pin_from_text(text: str) -> Optional[str]:
    """Return the first standalone 6-digit code in a reply body, or None.

    Reads only the top of the message so a 6-digit string buried in quoted history
    (">" lines) or a signature doesn't win over the code the user typed at the top.
    """
    if not text:
        return None
    head_lines = []
    for line in text.splitlines():
        if line.lstrip().startswith(">") or line.strip().lower().startswith("on "):
            break  # start of quoted original message
        head_lines.append(line)
    head = "\n".join(head_lines) or text
    m = _PIN_RE.search(head)
    return m.group(1) if m else None


def create_pin_request(user_id: int) -> str:
    """Mint a reply token mapping to user_id and store it (TTL). Returns the token even
    if Redis is down (the caller still emails; attribution just degrades)."""
    token = uuid.uuid4().hex[:20]
    client = _redis_client()
    if client is not None:
        try:
            client.set(_TOKEN_KEY.format(token=token), str(user_id), ex=_ttl())
        except Exception as e:
            log_warning("Failed to store PIN request token", exc=e, action_type="login")
    return token


def submit_pin_by_token(token: str, pin: str) -> Optional[int]:
    """Attribute an inbound reply (token + code) to a user and store the PIN. Returns
    the user_id on success, else None."""
    client = _redis_client()
    if client is None or not token or not pin:
        return None
    try:
        raw = client.get(_TOKEN_KEY.format(token=token))
    except Exception:
        return None
    uid = _decode(raw)
    if not uid:
        return None
    user_id = int(uid)
    if _store_pin(client, user_id, pin):
        return user_id
    return None


def submit_pin(user_id: int, pin: str) -> bool:
    client = _redis_client()
    if client is None:
        return False
    return _store_pin(client, user_id, pin)


def _store_pin(client, user_id: int, pin: str) -> bool:
    try:
        client.set(_PIN_KEY.format(user_id=user_id), str(pin), ex=_ttl())
        return True
    except Exception as e:
        log_warning("Failed to store verification PIN", exc=e, action_type="login")
        return False


def get_pin(user_id: int) -> Optional[str]:
    client = _redis_client()
    if client is None:
        return None
    try:
        return _decode(client.get(_PIN_KEY.format(user_id=user_id)))
    except Exception:
        return None


def clear_pin(user_id: int) -> None:
    client = _redis_client()
    if client is None:
        return
    try:
        client.delete(_PIN_KEY.format(user_id=user_id))
    except Exception:
        pass
