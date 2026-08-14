"""Every outbound email LEM sends, and the login PIN's generate/hash pair.

`_dispatch_email` is the ONE send path: SendGrid first, SMTP as the fallback, and it returns a bool
rather than raising — an email is a notification, never a reason a task dies. It always ships BOTH a
text/plain and a text/html part, because a missing plaintext alternative is a strong spam signal and
most of what leaves here is transactional mail (sign-in codes, "your session died") that has to
arrive.

No provider configured is a real state, not an error: `send_pin_email` reports it as a BYPASS so the
caller can skip PIN verification entirely rather than locking everyone out of a stack that was never
given SMTP credentials.
"""

import hashlib
import re
import secrets
import smtplib
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr
from html import escape as html_escape
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
    """A 6-digit login PIN, zero-padded so "000042" is as likely as any other value.

    `secrets`, not `random`: this is a credential, and the padding matters — trimming leading zeros
    would quietly shrink the space and make short PINs a tell.
    """
    return str(secrets.randbelow(1_000_000)).zfill(6)


def hash_pin(pin: str, email: str) -> str:
    """Hash a PIN bound to the address it was issued for — only this value is ever stored.

    The address is the salt, so the same six digits issued to two accounts hash differently and a
    row captured for one address cannot be replayed against another. Deterministic on purpose:
    verification recomputes the hash and compares (`verify_pin_for_email`), so there is no
    per-attempt salt to carry.
    """
    return hashlib.sha256(f"{pin}{email}".encode()).hexdigest()


# ---------------------------------------------------------------------------
# Shared identity / formatting helpers
# ---------------------------------------------------------------------------

def _from_header() -> str:
    """RFC-5322 From with a friendly display name on the authenticated domain.

    A recognizable name that matches the sending domain (not a bare address, and not a
    mismatched gmail.com address) is a key anti-phishing signal for Gmail.
    """
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
        ClickTracking,
        Email,
        Header,
        Mail,
        ReplyTo,
        TrackingSettings,
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
    strong spam signal). Returns True only if a provider accepted the message.
    """
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
    attributes it to the paused login.
    """
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
    The confirmation was delivered to our reply+<token> address, so the user never saw it.
    """
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


def _human_date(value: Optional[str]) -> Optional[str]:
    """The only producer of an expiry string is `resolve_token_status`, which emits ISO-8601 —
    "expires on 2026-09-14T08:30:12+00:00" is not something to put in front of a customer. Anything
    that isn't a timestamp is passed through untouched.
    """
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return value
    return f"{parsed:%b} {parsed.day}, {parsed.year}"


def send_linkedin_token_expiring_email(to_email: str, days_remaining: Optional[int] = None,
                                       expires_on: Optional[str] = None,
                                       account_url: Optional[str] = None) -> bool:
    """Tell a user their LinkedIn authorization is running out and we could NOT renew it for them
    (issue #600). Only sent after the daily renewal pass has already tried — LinkedIn caps the
    authorization at 60 days and only hands refresh tokens to approved apps, so for some accounts a
    manual reconnect is the only option and this email IS the notice.
    """
    url = account_url or _account_url()
    expires_on = _human_date(expires_on)
    if days_remaining is None:
        headline = "Your LinkedIn authorization needs renewing"
        when = "It expires soon, and we could not renew it automatically."
    elif days_remaining <= 0:
        headline = "Your LinkedIn authorization has expired"
        when = ("It has already expired, so posting, commenting and messaging on your behalf have "
                "stopped.")
    else:
        day_word = "day" if days_remaining == 1 else "days"
        headline = f"Your LinkedIn authorization expires in {days_remaining} {day_word}"
        when = (f"It expires in <strong>{days_remaining} {day_word}</strong>"
                f"{f' (on {html_escape(expires_on)})' if expires_on else ''}, and we could not "
                f"renew it automatically.")
    html = f"""
    <html><body style="font-family:Arial,Helvetica,sans-serif;color:#222;">
    <h2>{headline}</h2>
    <p>LinkedIn only authorizes apps for 60 days at a time, and it does not let us extend that
    window. We try to renew yours in the background every day. {when}</p>
    <p>Reconnecting takes about a minute and restarts the 60-day clock:</p>
    <p><a href="{url}" style="background:#0a66c2;color:#fff;padding:10px 16px;border-radius:6px;
    text-decoration:none;">Reconnect LinkedIn</a></p>
    <p style="color:#888;font-size:12px;">Your account page always shows how long the current
    authorization has left.</p>
    </body></html>
    """
    subject = ("⚠️ Your LinkedIn authorization has expired — reconnect to resume"
               if days_remaining is not None and days_remaining <= 0
               else "⚠️ Action needed: your LinkedIn authorization is expiring")
    return _dispatch_email(to_email, subject, html, high_priority=True)


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


def _newsletter_queue_url() -> str:
    """Deep link to the newsletter review queue — the ONE screen an author can approve a cover on."""
    import os
    base = (os.getenv("LEM_APP_URL") or "https://lem.christopherqueenconsulting.com").rstrip("/")
    return f"{base}/content?tab=newsletters"


def send_newsletter_cover_pending_email(to_email: str, edition_title: str, scheduled_for: str,
                                        queue_url: Optional[str] = None) -> bool:
    """Email a user whose edition is about to publish with a cover still awaiting approval (#1432).

    The draft-ready email cannot carry this: the cover is rendered asynchronously and lands
    minutes AFTER that email goes out. So this is the only message that can say the cover is
    waiting, and it names the consequence — the edition publishes cover-less if nothing happens.
    """
    url = queue_url or _newsletter_queue_url()
    title = edition_title or "Your next edition"
    # The title is LLM-authored and the slot string is rendered from DB data, so both are escaped
    # before they reach the markup — an '&' or an angle bracket in a title otherwise truncates the
    # sentence it sits in. The subject is plain text and takes the raw title.
    safe_title = html_escape(title)
    safe_when = html_escape(str(scheduled_for))
    safe_url = html_escape(url)
    html = f"""
    <html><body style="font-family:Arial,Helvetica,sans-serif;color:#222;">
    <h2>Your newsletter cover is waiting for you</h2>
    <p>We generated a cover image for <strong>{safe_title}</strong>, but a generated cover is a
    public brand asset so it only goes out once you've looked at it.</p>
    <p>That edition publishes <strong>{safe_when}</strong>. If the cover isn't approved by
    then, it goes out <strong>without a cover image</strong> — the edition itself still
    publishes on time.</p>
    <p><a href="{safe_url}" style="background:#0a66c2;color:#fff;padding:10px 16px;border-radius:6px;
    text-decoration:none;">Review the cover</a></p>
    <p style="color:#888;font-size:12px;">Open the edition in your newsletter queue and use
    <strong>Approve cover</strong> — or <strong>Remove cover</strong> if you'd rather publish
    without one.</p>
    </body></html>
    """
    return _dispatch_email(
        to_email, f"🖼️ Approve the cover before this edition goes out: {title}", html,
        high_priority=True)


def send_content_generation_ready_email(to_email: str, ready_count: int,
                                        failed_count: int = 0) -> bool:
    """Email a user that their weekly content finished generating (issue #545). Generation runs
    for minutes in the background, so this is the "come back and review" signal.
    """
    import os
    base = (os.getenv("LEM_APP_URL") or "https://lem.christopherqueenconsulting.com").rstrip("/")
    url = f"{base}/content?tab=review"
    plural = "post" if ready_count == 1 else "posts"
    failed_note = (f"<p>{failed_count} {'post' if failed_count == 1 else 'posts'} couldn't be "
                   f"generated (usually a media generation failure) — they stay in your plan and "
                   f"we'll retry them on the next run.</p>") if failed_count else ""
    html = f"""
    <html><body style="font-family:Arial,Helvetica,sans-serif;color:#222;">
    <h2>Your {ready_count} {plural} {'is' if ready_count == 1 else 'are'} ready to review</h2>
    <p>We finished generating this week's content. Review, edit, or approve it before it
    goes out.</p>
    {failed_note}
    <p><a href="{url}" style="background:#0a66c2;color:#fff;padding:10px 16px;border-radius:6px;
    text-decoration:none;">Review your content</a></p>
    </body></html>
    """
    return _dispatch_email(
        to_email, f"✅ Your {ready_count} LinkedIn {plural} {'is' if ready_count == 1 else 'are'} "
                  f"ready to review", html)


def send_suppression_tripwire_email(to_email: str, reason: str) -> bool:
    """Tell the user their engagement automation stopped itself because their reach collapsed
    (issue #629). LinkedIn never says it has suppressed an account, so this email IS the notice —
    it has to be plain language, not a metric dump, and it has to say what happens next.
    """
    import os
    base = (os.getenv("LEM_APP_URL") or "https://lem.christopherqueenconsulting.com").rstrip("/")
    url = f"{base}/account"
    safe_reason = html_escape(reason or "a sustained drop in your reach")
    html = f"""
    <html><body style="font-family:Arial,Helvetica,sans-serif;color:#222;">
    <h2>We paused your LinkedIn engagement automation</h2>
    <p>Your recent posts are reaching far fewer people than they normally do. That pattern usually
    means LinkedIn has quietly limited the account — it never sends a notification when it does.</p>
    <p style="background:#fff4e5;border-left:4px solid #f5a623;padding:10px 14px;">
    <strong>What we measured:</strong> {safe_reason}</p>
    <p>So we stopped commenting, replying and messaging on your behalf. <strong>Your scheduled posts
    still publish and we keep collecting your analytics</strong> — only the automated engagement is
    paused.</p>
    <p>Accounts typically recover after a few weeks of normal, human activity. Keep posting, engage
    by hand for a while, and re-enable automation from your account page when your reach comes back
    — we keep measuring it every day, so your account page will tell you when it has.</p>
    <p><a href="{url}" style="background:#0a66c2;color:#fff;padding:10px 16px;border-radius:6px;
    text-decoration:none;">Review and re-enable</a></p>
    <p style="color:#888;font-size:12px;">This pause will not lift on its own — you decide when to
    turn engagement back on.</p>
    </body></html>
    """
    return _dispatch_email(to_email, "⚠️ We paused your LinkedIn engagement — your reach dropped",
                           html, high_priority=True)


def send_onboarding_nudge_email(to_email: str, subject: str, headline: str, body: str,
                                cta_label: str, cta_path: str = "/account") -> bool:
    """Nudge a user who stalled part-way through activation (issue #500). Copy is supplied by the
    caller (LLM-refreshed, with a static fallback) so this stays a pure send.
    """
    import os
    base = (os.getenv("LEM_APP_URL") or "https://lem.christopherqueenconsulting.com").rstrip("/")
    url = f"{base}{cta_path if cta_path.startswith('/') else '/' + cta_path}"
    # body is LLM-generated and the rest is caller-supplied — escape before templating so copy
    # can't inject markup or break the layout
    safe_headline = html_escape(headline or "")
    safe_body = html_escape(body or "").replace("\n", "<br/>")
    safe_cta_label = html_escape(cta_label or "")
    safe_url = html_escape(url)
    html = f"""
    <html><body style="font-family:Arial,Helvetica,sans-serif;color:#222;">
    <h2>{safe_headline}</h2>
    <p>{safe_body}</p>
    <p><a href="{safe_url}" style="background:#0a66c2;color:#fff;padding:10px 16px;border-radius:6px;
    text-decoration:none;">{safe_cta_label}</a></p>
    <p style="color:#888;font-size:12px;">If you've already done this, you can ignore this email —
    we only send each setup reminder once.</p>
    </body></html>
    """
    return _dispatch_email(to_email, subject, html)


def send_survey_prompt_email(to_email: str, subject: str, headline: str, body: str,
                             cta_label: str, cta_path: str = "/") -> bool:
    """Invite a user to answer an NPS survey or leave a review (issue #501). The CTA deep-links to
    the in-app modal, so both channels write the same `feedback` row.
    """
    import os
    base = (os.getenv("LEM_APP_URL") or "https://lem.christopherqueenconsulting.com").rstrip("/")
    url = f"{base}{cta_path if cta_path.startswith('/') else '/' + cta_path}"
    safe_headline = html_escape(headline or "")
    safe_body = html_escape(body or "").replace("\n", "<br/>")
    safe_cta_label = html_escape(cta_label or "")
    safe_url = html_escape(url)
    html = f"""
    <html><body style="font-family:Arial,Helvetica,sans-serif;color:#222;">
    <h2>{safe_headline}</h2>
    <p>{safe_body}</p>
    <p><a href="{safe_url}" style="background:#0a66c2;color:#fff;padding:10px 16px;border-radius:6px;
    text-decoration:none;">{safe_cta_label}</a></p>
    <p style="color:#888;font-size:12px;">We ask each of these once — answer it or ignore it and you
    won't hear about it again.</p>
    </body></html>
    """
    return _dispatch_email(to_email, subject, html)


def send_shipped_fix_email(to_email: str, changelog_line: str, issue_number: int,
                           cta_path: str = "/") -> bool:
    """Tell a reporter the thing they asked for shipped (issue #502). The CTA opens the app, where
    the in-app notice asks the micro-CSAT ("did this fix it?") that re-enters the feedback loop.
    """
    import os
    base = (os.getenv("LEM_APP_URL") or "https://lem.christopherqueenconsulting.com").rstrip("/")
    url = f"{base}{cta_path if cta_path.startswith('/') else '/' + cta_path}"
    # changelog_line is derived from a PR title — escape before templating so it can't inject markup.
    safe_line = html_escape(changelog_line or "").replace("**", "")
    safe_url = html_escape(url)
    html = f"""
    <html><body style="font-family:Arial,Helvetica,sans-serif;color:#222;">
    <h2>You asked, we shipped 🎉</h2>
    <p>You reported this, and it's now live in LinkedIn Engagement Manager:</p>
    <p style="border-left:3px solid #0a66c2;padding-left:12px;color:#333;">{safe_line}</p>
    <p>Give it a try — then tell us whether it actually fixed things for you. One tap, and it goes
    straight back to the team.</p>
    <p><a href="{safe_url}" style="background:#0a66c2;color:#fff;padding:10px 16px;border-radius:6px;
    text-decoration:none;">See what changed</a></p>
    <p style="color:#888;font-size:12px;">You're getting this because you sent us feedback about
    it — we only send one of these per fix.</p>
    </body></html>
    """
    return _dispatch_email(to_email, f"✅ You asked, we shipped (#{int(issue_number)})", html)


def send_faq_answer_email(to_email: str, question: str, answer: str,
                          cta_path: str = "/#faq") -> bool:
    """Answer someone who asked a support question (issue #507). Both halves are generated/derived
    from user text, so both are escaped before templating.
    """
    import os
    base = (os.getenv("LEM_APP_URL") or "https://lem.christopherqueenconsulting.com").rstrip("/")
    url = f"{base}{cta_path if cta_path.startswith('/') else '/' + cta_path}"
    safe_question = html_escape(question or "")
    safe_answer = html_escape(answer or "").replace("\n", "<br/>")
    safe_url = html_escape(url)
    html = f"""
    <html><body style="font-family:Arial,Helvetica,sans-serif;color:#222;">
    <h2>Here's the answer to your question</h2>
    <p style="border-left:3px solid #0a66c2;padding-left:12px;color:#333;">{safe_question}</p>
    <p>{safe_answer}</p>
    <p><a href="{safe_url}" style="background:#0a66c2;color:#fff;padding:10px 16px;border-radius:6px;
    text-decoration:none;">See the full FAQ</a></p>
    <p style="color:#888;font-size:12px;">You're getting this because you asked us this question.
    Reply to this email if it didn't answer it — a person will pick it up.</p>
    </body></html>
    """
    return _dispatch_email(to_email, "💡 Your question, answered", html)


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
