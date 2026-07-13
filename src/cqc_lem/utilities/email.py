import hashlib
import re
import secrets
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr
from typing import Optional, Tuple

from cqc_lem.utilities.env_constants import (
    EMAIL_FROM_NAME,
    EMAIL_REPLY_TO,
    SENDGRID_API_KEY,
    SENDGRID_FROM_EMAIL,
    SMTP_HOST,
    SMTP_PASSWORD,
    SMTP_PORT,
    SMTP_USER,
)
from cqc_lem.utilities.logger import log_error, log_info, log_warning


def generate_pin() -> str:
    return str(secrets.randbelow(1_000_000)).zfill(6)


def hash_pin(pin: str, email: str) -> str:
    return hashlib.sha256(f"{pin}{email}".encode()).hexdigest()


# ---------------------------------------------------------------------------
# Shared identity / formatting helpers
# ---------------------------------------------------------------------------

def _from_header() -> str:
    """RFC-5322 From with a friendly display name on the authenticated domain.

    A recognizable name that matches the sending domain (not a bare address, and not a
    mismatched gmail.com address) is a key anti-phishing signal for Gmail."""
    return formataddr((EMAIL_FROM_NAME, SENDGRID_FROM_EMAIL))


def _reply_to() -> Optional[str]:
    return EMAIL_REPLY_TO or None


def _footer_html() -> str:
    contact = (
        f' Questions? Reach us at <a href="mailto:{EMAIL_REPLY_TO}">{EMAIL_REPLY_TO}</a>.'
        if EMAIL_REPLY_TO else ""
    )
    return (
        '<hr style="border:none;border-top:1px solid #eee;margin:24px 0;">'
        f'<p style="color:#888;font-size:12px;line-height:1.5;">'
        f"You are receiving this email because a sign-in was requested for your "
        f"{EMAIL_FROM_NAME} account.{contact}<br>"
        f"&copy; {EMAIL_FROM_NAME}. This is an automated transactional message — please do "
        f"not share the code above with anyone.</p>"
    )


def _footer_text() -> str:
    contact = f" Questions? Reach us at {EMAIL_REPLY_TO}." if EMAIL_REPLY_TO else ""
    return (
        "\n\n----\n"
        f"You are receiving this email because a sign-in was requested for your "
        f"{EMAIL_FROM_NAME} account.{contact}\n"
        f"(c) {EMAIL_FROM_NAME}. This is an automated transactional message — "
        f"never share your code with anyone."
    )


_TAG_RE = re.compile(r"<[^>]+>")


def _html_to_text(html: str) -> str:
    """Best-effort plaintext fallback so every message ships a text/plain part."""
    text = re.sub(r"(?is)<br\s*/?>", "\n", html)
    text = re.sub(r"(?is)</p>", "\n\n", text)
    text = _TAG_RE.sub("", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return text.strip()


# ---------------------------------------------------------------------------
# Login PIN email content (transactional, link-free, anti-phishing)
# ---------------------------------------------------------------------------

def _build_pin_html(pin: str, action: str) -> str:
    return f"""\
<html><body style="font-family:Arial,Helvetica,sans-serif;color:#222;">
  <h2 style="margin-bottom:8px;">Your {EMAIL_FROM_NAME} sign-in code</h2>
  <p>Hi,</p>
  <p>Use this one-time code to {action}. It works only once and only on this sign-in
  attempt:</p>
  <p style="font-size:30px;letter-spacing:8px;font-weight:bold;font-family:'Courier New',monospace;
  margin:20px 0;color:#0a66c2;">{pin}</p>
  <p>This code expires in <strong>10 minutes</strong>.</p>
  <p>For your security, {EMAIL_FROM_NAME} will <strong>never</strong> ask you for this code by
  phone, chat, or email. If you did not request it, you can safely ignore this message — no
  one can sign in without it.</p>
  {_footer_html()}
</body></html>
"""


def _build_pin_text(pin: str, action: str) -> str:
    return (
        f"Your {EMAIL_FROM_NAME} sign-in code\n\n"
        f"Use this one-time code to {action}:\n\n"
        f"    {pin}\n\n"
        f"This code expires in 10 minutes.\n\n"
        f"For your security, {EMAIL_FROM_NAME} will never ask you for this code. "
        f"If you did not request it, you can safely ignore this message — no one can "
        f"sign in without it."
        f"{_footer_text()}"
    )


def _build_approval_html(vnc_url: str | None) -> str:
    watch = (
        f'<p>Want to watch it happen live? <a href="{vnc_url}">Open the session viewer</a>.</p>'
        if vnc_url else ""
    )
    return f"""
    <html><body style="font-family:Arial,Helvetica,sans-serif;color:#222;">
    <h2>Action needed: approve your LinkedIn sign-in</h2>
    <p>LinkedIn Engagement Manager is signing in to LinkedIn on your behalf, and
    LinkedIn asked to <strong>verify this sign-in from your device</strong>.</p>
    <ol>
      <li>Open the <strong>LinkedIn mobile app</strong>.</li>
      <li>You'll see a "Did you just try to sign in?" prompt — tap <strong>Yes</strong>.</li>
    </ol>
    <p>This is expected and safe — it's our automation, not someone else. Approving once
    lets LinkedIn remember the device so you won't be asked every time.</p>
    {watch}
    <p style="color:#888;font-size:12px;">If you were not expecting any automation activity,
    do not approve, and change your LinkedIn password.</p>
    </body></html>
    """


# ---------------------------------------------------------------------------
# Low-level provider dispatch (multipart/alternative: text + html)
# ---------------------------------------------------------------------------

def _sendgrid_send(to_email: str, subject: str, html_content: str, text_content: str,
                   reply_to: Optional[str], high_priority: bool) -> None:
    from sendgrid import SendGridAPIClient
    from sendgrid.helpers.mail import (
        ClickTracking, Email, Header, Mail, ReplyTo, TrackingSettings,
    )

    message = Mail(
        from_email=Email(SENDGRID_FROM_EMAIL, EMAIL_FROM_NAME),
        to_emails=to_email,
        subject=subject,
        plain_text_content=text_content,
        html_content=html_content,
    )
    # These are all security/transactional emails (sign-in codes, session reconnect). Rewriting
    # the button href through SendGrid's click-tracking redirect makes them look like phishing and
    # hurts trust/deliverability — keep the real account URL the user can verify.
    tracking = TrackingSettings()
    tracking.click_tracking = ClickTracking(False, False)
    message.tracking_settings = tracking
    if reply_to:
        message.reply_to = ReplyTo(reply_to)
    if high_priority:
        message.header = Header("X-Priority", "1")
        message.header = Header("X-MSMail-Priority", "High")
        message.header = Header("Importance", "High")
    SendGridAPIClient(SENDGRID_API_KEY).send(message)


def _smtp_send(to_email: str, subject: str, html_content: str, text_content: str,
               reply_to: Optional[str], high_priority: bool) -> None:
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = _from_header()
    msg["To"] = to_email
    if reply_to:
        msg["Reply-To"] = reply_to
    if high_priority:
        msg["X-Priority"] = "1"
        msg["X-MSMail-Priority"] = "High"
        msg["Importance"] = "High"
    # Order matters for multipart/alternative: least-rich (plain) first, richest (html) last.
    msg.attach(MIMEText(text_content, "plain"))
    msg.attach(MIMEText(html_content, "html"))
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as server:
        server.ehlo()
        server.starttls()
        server.login(SMTP_USER, SMTP_PASSWORD)
        server.sendmail(SMTP_USER, to_email, msg.as_string())


def _dispatch_email(to_email: str, subject: str, html_content: str,
                    text_content: Optional[str] = None, reply_to: Optional[str] = None,
                    high_priority: bool = False) -> bool:
    """Send a multipart/alternative email via SendGrid, falling back to SMTP.

    Always ships both a text/plain and a text/html part (a missing plaintext alternative is a
    strong spam signal). Returns True only if a provider accepted the message."""
    has_sendgrid = bool(SENDGRID_API_KEY)
    has_smtp = bool(SMTP_USER) and bool(SMTP_PASSWORD)
    if not has_sendgrid and not has_smtp:
        log_warning(f"No email provider configured — cannot send: {subject}")
        return False

    if text_content is None:
        text_content = _html_to_text(html_content)
    if reply_to is None:
        reply_to = _reply_to()

    if has_sendgrid:
        try:
            _sendgrid_send(to_email, subject, html_content, text_content, reply_to, high_priority)
            log_info(f"Email sent via SendGrid to {to_email}: {subject}", api_provider="sendgrid")
            return True
        except Exception as e:
            log_warning(f"SendGrid send failed for {to_email}", exc=e, api_provider="sendgrid")

    if has_smtp:
        try:
            _smtp_send(to_email, subject, html_content, text_content, reply_to, high_priority)
            log_info(f"Email sent via SMTP to {to_email}: {subject}", api_provider="smtp")
            return True
        except Exception as e:
            log_error(f"SMTP send failed for {to_email}", exc=e, api_provider="smtp")
            return False

    return False


# ---------------------------------------------------------------------------
# Public email senders
# ---------------------------------------------------------------------------

def send_login_approval_email(to_email: str, vnc_url: str | None = None) -> bool:
    """Send a HIGH-PRIORITY email asking the user to approve a LinkedIn device sign-in."""
    subject = "⚠️ Action needed: approve your LinkedIn sign-in"
    html = _build_approval_html(vnc_url)
    return _dispatch_email(to_email, subject, html, high_priority=True)


def send_login_pin_request_email(to_email: str, reply_to: str) -> bool:
    """Ask the user to REPLY with the 6-digit LinkedIn verification code.

    The reply routes back (via SendGrid Inbound Parse) to `reply_to`, whose token
    attributes it to the paused login."""
    subject = "⚠️ Reply ASAP with your LinkedIn verification code"
    html = (
        "<html><body style=\"font-family:Arial,Helvetica,sans-serif;color:#222;\">"
        "<h2>We need your LinkedIn verification code</h2>"
        "<p>LinkedIn asked us to verify this sign-in so your automation can keep running.</p>"
        "<p><strong>Check your inbox for a separate email from LinkedIn with a 6-digit code, "
        "then simply <u>reply to THIS email</u> with just that code.</strong></p>"
        "<p>The code expires quickly — please reply as soon as you can.</p>"
        "<p style='color:#666;font-size:12px'>Didn't try to sign in? You can ignore this — "
        "no one can get in without the code.</p>"
        "</body></html>"
    )
    return _dispatch_email(to_email, subject, html, reply_to=reply_to, high_priority=True)


def send_reply_forward_confirmation_email(to_email: str, verify_url: Optional[str],
                                          code: Optional[str]) -> bool:
    """Forward Gmail's forwarding-confirmation to the user so they can finish it from their OWN
    signed-in browser (the most reliable way — a server-side click may not complete Gmail's flow).
    The confirmation was delivered to our reply+<token> address, so the user never saw it."""
    if not verify_url and not code:
        return False
    button = (f'<p style="margin:18px 0"><a href="{verify_url}" '
              'style="background:#0a66c2;color:#fff;padding:10px 18px;border-radius:6px;'
              'text-decoration:none;font-weight:bold" target="_blank" rel="noopener">'
              'Confirm forwarding</a></p>') if verify_url else ""
    code_line = (f'<p>Or enter this confirmation code in Gmail (Settings &rarr; Forwarding, next to the '
                 f'pending address): <strong style="font-size:16px">{code}</strong></p>') if code else ""
    link_line = (f'<p style="color:#666;font-size:12px;word-break:break-all">Button not working? '
                 f'Copy this link: {verify_url}</p>') if verify_url else ""
    html = (
        "<html><body style=\"font-family:Arial,Helvetica,sans-serif;color:#222;\">"
        "<h2>Confirm comment-reply forwarding</h2>"
        "<p>You added our address as a Gmail forwarding destination so we can auto-reply to comments "
        "on your LinkedIn posts. Gmail sent a confirmation to that address (which you can't see), so "
        "we're passing it to you.</p>"
        f"{button}{code_line}{link_line}"
        "<p style='color:#666;font-size:12px'>If your forwarding already shows as confirmed in Gmail, "
        "you can ignore this email.</p>"
        "</body></html>"
    )
    return _dispatch_email(to_email, "Confirm your LinkedIn comment-reply forwarding", html,
                           high_priority=True)


def _account_url() -> str:
    import os
    base = (os.getenv("LEM_APP_URL") or "https://lem.christopherqueenconsulting.com").rstrip("/")
    return f"{base}/account"


def send_connect_linkedin_email(to_email: str, account_url: Optional[str] = None) -> bool:
    """Email a user who has no validated LinkedIn session, prompting them to connect."""
    url = account_url or _account_url()
    html = f"""
    <html><body style="font-family:Arial,Helvetica,sans-serif;color:#222;">
    <h2>Connect your LinkedIn to keep automation running</h2>
    <p>LinkedIn Engagement Manager needs an authorized LinkedIn session to post,
    comment, and engage on your behalf. Your account doesn't have one yet, so automation
    can't run.</p>
    <p>It takes about a minute — open your account and click <strong>Connect LinkedIn</strong>:</p>
    <p><a href="{url}" style="background:#0a66c2;color:#fff;padding:10px 16px;border-radius:6px;
    text-decoration:none;">Connect LinkedIn</a></p>
    <p style="color:#888;font-size:12px;">If you've already connected, you can ignore this email.</p>
    </body></html>
    """
    return _dispatch_email(
        to_email, "⚠️ Action needed: connect your LinkedIn to keep automation running", html,
        high_priority=True)


def send_session_revalidation_email(to_email: str, account_url: Optional[str] = None) -> bool:
    """Email a user whose stored LinkedIn session stopped working, prompting a reconnect."""
    url = account_url or _account_url()
    html = f"""
    <html><body style="font-family:Arial,Helvetica,sans-serif;color:#222;">
    <h2>Reconnect your LinkedIn session</h2>
    <p>Your saved LinkedIn session expired or was signed out, so LinkedIn Engagement
    Manager can no longer act on your behalf — automation is paused until you reconnect.</p>
    <p>Open your account and reconnect (one click with the browser extension, or paste a
    fresh session):</p>
    <p><a href="{url}" style="background:#0a66c2;color:#fff;padding:10px 16px;border-radius:6px;
    text-decoration:none;">Reconnect LinkedIn</a></p>
    <p style="color:#888;font-size:12px;">This happens periodically — LinkedIn sessions don't
    last forever.</p>
    </body></html>
    """
    return _dispatch_email(
        to_email, "⚠️ Action needed: reconnect your LinkedIn session", html, high_priority=True)


def send_newsletter_draft_ready_email(to_email: str, edition_title: str,
                                      scheduled_for: str, account_url: Optional[str] = None) -> bool:
    """Email a user that their newsletter draft is ready to review before it auto-publishes."""
    url = account_url or _account_url()
    title = edition_title or "Your next edition"
    html = f"""
    <html><body style="font-family:Arial,Helvetica,sans-serif;color:#222;">
    <h2>Your newsletter draft is ready to review</h2>
    <p>We drafted your next LinkedIn newsletter edition, <strong>{title}</strong>.
    Review, edit, approve, or skip it before it goes out.</p>
    <p>If you do nothing, it will <strong>auto-publish as-is on {scheduled_for}</strong> so your
    cadence never breaks.</p>
    <p><a href="{url}" style="background:#0a66c2;color:#fff;padding:10px 16px;border-radius:6px;
    text-decoration:none;">Review your draft</a></p>
    <p style="color:#888;font-size:12px;">You can edit the title, subtitle, and body, or skip this
    edition entirely from your account page.</p>
    </body></html>
    """
    return _dispatch_email(
        to_email, f"📝 Your newsletter draft is ready: {title}", html, high_priority=True)


def send_pin_email(
    to_email: str,
    pin: str,
    is_new_user: bool = False,
    probe_only: bool = False,
) -> Tuple[bool, bool]:
    """Send a login PIN email using the best available provider.

    Returns (success, bypassed):
      - (True, False)  — email was sent successfully
      - (False, False) — a provider is configured but the send failed
      - (True, True)   — no provider is configured; PIN step should be skipped

    probe_only=True skips the actual send and just returns whether bypass would occur.
    """
    has_sendgrid = bool(SENDGRID_API_KEY)
    has_smtp = bool(SMTP_USER) and bool(SMTP_PASSWORD)

    if not has_sendgrid and not has_smtp:
        log_info("No email provider configured — bypassing PIN verification")
        return True, True

    if probe_only:
        return False, False

    action = "create your account" if is_new_user else "sign in"
    subject = f"Your {EMAIL_FROM_NAME} sign-in code"
    html = _build_pin_html(pin, action)
    text = _build_pin_text(pin, action)

    sent = _dispatch_email(to_email, subject, html, text_content=text, reply_to=_reply_to())
    return sent, False
