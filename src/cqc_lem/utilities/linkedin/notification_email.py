"""Helpers for the event-driven reply feature: a user forwards LinkedIn "commented on your post"
notification emails to a tokenized address (reply+<token>@parse.<domain>) that SendGrid Inbound
Parse pipes to our webhook. Mirrors the PIN-verification email helpers (verification_pin.py) — same
parse-domain derivation and tokenized-address style — but the token is a PERSISTENT per-user id
(stored on the users row) since the forward filter is set up once and forwards indefinitely.
"""
import os
import re
from email.utils import parseaddr
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

# The registrable domains LinkedIn's own notification mail is sent from. Every notification local
# part we see (notifications-noreply@, messages-noreply@, invitations@, news@, hit-reply@) sits on
# linkedin.com itself, and the delivery/bounce routes sit on subdomains of it (e.linkedin.com,
# bounce.linkedin.com) — which the suffix rule in _is_linkedin_sender already covers. Kept to that
# ONE domain on purpose: lnkd.in is a link shortener, never a sender, and every extra entry is
# another domain someone can register a lookalike under.
_LINKEDIN_SENDER_DOMAINS = ("linkedin.com",)


def reply_inbound_address(token: str) -> str:
    """The forwarding address the user points a Gmail filter at. Host is LINKEDIN_PARSE_DOMAIN if
    set, else derived from the configured domain (see _default_parse_domain).
    """
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
    extract_pin_from_text) so quoted history can't flip the classification.
    """
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


def _sender_domain(sender: str) -> str:
    """The lowercased domain of an RFC5322 `From` value, or `""` when it doesn't carry one.

    Handles both shapes a From header arrives in — bare `news@linkedin.com` and the display-name
    form `"LinkedIn" <notifications-noreply@linkedin.com>` — and takes the part after the LAST `@`,
    which is the only part a mail server routes on. A value parseaddr can't read yields `""`.
    """
    _, address = parseaddr(sender or "")
    _, at, domain = address.rpartition("@")
    if not at:
        return ""
    # A trailing dot is a legal absolute FQDN ("linkedin.com.") naming the same host.
    return domain.strip().rstrip(".").lower()


def _is_linkedin_sender(sender: str) -> bool:
    """True only when the From address's DOMAIN is LinkedIn's, or a true subdomain of it.

    A substring test is not a domain test: `bounce@linkedin.com.attacker.net` (LinkedIn's domain as
    a mere LABEL of someone else's) and `notlinkedin.com@evil.test` (it in the local part) both
    contain "linkedin.com" while being sent by neither. Only an exact match or a `.linkedin.com`
    suffix counts, so the dot boundary is what a lookalike has to get past.
    """
    domain = _sender_domain(sender)
    if not domain:
        return False
    return any(domain == d or domain.endswith(f".{d}") for d in _LINKEDIN_SENDER_DOMAINS)


def is_linkedin_notification(sender: str, subject: str, text: str = "") -> bool:
    """True when mail arriving on a reply+<token> address is a forwarded LinkedIn notification of ANY
    kind. This is the evidence that a user's forwarding rule is actually live (issue #813), so it is
    deliberately broader than is_comment_notification — a forwarded reaction email proves the chain
    works just as well — but not blanket: unrelated mail must not stand in as proof. A Gmail
    auto-forward preserves the original From header, so LinkedIn's sending domain survives the hop.
    """
    if _is_linkedin_sender(sender):
        return True
    hay = f"{subject or ''}\n{(text or '')[:2000]}".lower()
    return "linkedin" in hay


# --- Gmail forwarding auto-confirmation -------------------------------------------
# When the user adds our reply+<token>@parse-domain address as a Gmail forwarding address, Gmail
# emails a CONFIRMATION to it ("verify permission"). Since that address is ours and token-gated, we
# can auto-confirm by clicking the verify link server-side so the user doesn't have to fish the code
# out. The email also contains a numeric code as a manual fallback.
_GMAIL_CONFIRM_URL_RE = re.compile(r"https://mail(?:-settings)?\.google\.com/mail/[^\s\"'<>\]\)]+")
# Gmail states the code several ways depending on client/locale — try each. The subject also carries
# it as "(#123456789)".
_GMAIL_CONFIRM_CODE_RES = (
    re.compile(r"confirmation code[^0-9]{0,40}(\d{6,12})", re.I),
    re.compile(r"\(#(\d{6,12})\)"),
    re.compile(r"\bcode\b[^0-9]{0,20}(\d{9,12})", re.I),
)


def is_gmail_forwarding_confirmation(sender: str, subject: str, text: str = "") -> bool:
    """True when the inbound email is a Gmail 'Forwarding Confirmation' asking us to verify the
    forwarding address (from forwarding-noreply@google.com).
    """
    if "forwarding-noreply@google.com" in (sender or "").lower():
        return True
    subj = (subject or "").lower()
    body = (text or "").lower()
    return ("forwarding confirmation" in subj
            or ("verify permission" in body and "mail.google.com/mail" in body)
            or ("confirm forwarding" in body and "mail.google.com/mail" in body))


def extract_gmail_confirmation_url(text: str) -> Optional[str]:
    """The Gmail verify link to click (prefers the vf-/uf- confirmation link). Pass the HTML part
    when available — the plain-text part wraps the very long URL across lines, which breaks it.
    """
    if not text:
        return None
    # href="...&amp;..." → real & ; also undo soft-wrap that split the URL across lines.
    cleaned = text.replace("&amp;", "&")
    urls = _GMAIL_CONFIRM_URL_RE.findall(cleaned)
    if not urls:
        return None
    for u in urls:
        if "vf-" in u or "uf-" in u:
            return u.rstrip(").,;\"'>")
    return urls[0].rstrip(").,;\"'>")


def extract_gmail_confirmation_code(text: str) -> Optional[str]:
    """The numeric confirmation code Gmail also provides, for manual entry if auto-confirm fails."""
    if not text:
        return None
    for rex in _GMAIL_CONFIRM_CODE_RES:
        m = rex.search(text)
        if m:
            return m.group(1)
    return None
