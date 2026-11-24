"""Deterministic sensitive-information check for text bound for an outbound LLM call (#2750).

Owner ruling on #2627: any provider may receive comment and DM prompts, including private DM thread
text, PROVIDED this check runs first. It redacts US Social Security numbers and Luhn-valid payment
card numbers. Phone numbers and emails are deliberately NOT touched.

Pure functions, stdlib only, no I/O and no logging: a redaction must never depend on anything that
can fail.
"""
import re
from typing import Any

REDACTED_SSN = "[REDACTED-SSN]"
REDACTED_CARD = "[REDACTED-CARD]"

# 3-2-4 with a dash, dot or space; a bare 9-digit run is too common (order ids, zip+4) to redact.
_SSN_RE = re.compile(r"(?<![\d-])(?!000|666|9\d\d)\d{3}([-. ])(?!00)\d{2}\1(?!0000)\d{4}(?![\d-])")
# 13-19 digits, optionally grouped by single spaces or dashes. Validated with Luhn before redacting.
_CARD_RE = re.compile(r"(?<![\d-])\d(?:[ -]?\d){12,18}(?![\d-])")


def luhn_valid(digits: str) -> bool:
    """True when `digits` (digits only) passes the Luhn checksum."""
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def _card_sub(match: "re.Match[str]") -> str:
    digits = re.sub(r"\D", "", match.group(0))
    if 13 <= len(digits) <= 19 and luhn_valid(digits):
        return REDACTED_CARD
    return match.group(0)


def redact_sensitive(text: str) -> str:
    """`text` with SSNs and Luhn-valid card numbers replaced by placeholders."""
    if not text:
        return text
    return _CARD_RE.sub(_card_sub, _SSN_RE.sub(REDACTED_SSN, text))


def contains_sensitive(text: str) -> bool:
    """True when `redact_sensitive` would change `text`."""
    return bool(text) and redact_sensitive(text) != text


def _redact_part(part: Any) -> Any:
    if isinstance(part, dict) and isinstance(part.get("text"), str):
        return {**part, "text": redact_sensitive(part["text"])}
    return part


def redact_messages(messages: Any) -> Any:
    """A copy of a chat `messages` list with every string content redacted.

    Handles plain string content and the list-of-parts form. Anything else is returned untouched, so
    a malformed payload still reaches the caller's own error handling.
    """
    if not isinstance(messages, list):
        return messages
    out = []
    for message in messages:
        if not isinstance(message, dict):
            out.append(message)
            continue
        content = message.get("content")
        if isinstance(content, str):
            message = {**message, "content": redact_sensitive(content)}
        elif isinstance(content, list):
            message = {**message, "content": [_redact_part(p) for p in content]}
        out.append(message)
    return out
