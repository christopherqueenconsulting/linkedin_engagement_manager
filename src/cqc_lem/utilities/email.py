import hashlib
import secrets
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional, Tuple

from cqc_lem.utilities.env_constants import (
    SENDGRID_API_KEY,
    SENDGRID_FROM_EMAIL,
    SMTP_HOST,
    SMTP_PASSWORD,
    SMTP_PORT,
    SMTP_USER,
)
from cqc_lem.utilities.logger import myprint


def generate_pin() -> str:
    return str(secrets.randbelow(1_000_000)).zfill(6)


def hash_pin(pin: str, email: str) -> str:
    return hashlib.sha256(f"{pin}{email}".encode()).hexdigest()


def _build_html(pin: str, action: str) -> str:
    return f"""
    <html><body>
    <h2>Your LinkedIn Engagement Manager Verification Code</h2>
    <p>Enter the following code to {action}:</p>
    <h1 style="letter-spacing:0.3em;font-family:monospace;">{pin}</h1>
    <p>This code expires in <strong>10 minutes</strong>.</p>
    <p style="color:#888;font-size:12px;">If you did not request this, you can safely ignore this email.</p>
    </body></html>
    """


def _send_via_sendgrid(to_email: str, html_content: str) -> bool:
    from sendgrid import SendGridAPIClient
    from sendgrid.helpers.mail import Mail

    message = Mail(
        from_email=SENDGRID_FROM_EMAIL,
        to_emails=to_email,
        subject="Your LEM Verification Code",
        html_content=html_content,
    )
    try:
        sg = SendGridAPIClient(SENDGRID_API_KEY)
        sg.send(message)
        myprint(f"PIN email sent via SendGrid to {to_email}")
        return True
    except Exception as e:
        myprint(f"SendGrid send failed for {to_email}: {e}")
        return False


def _send_via_smtp(to_email: str, html_content: str) -> bool:
    msg = MIMEMultipart("alternative")
    msg["Subject"] = "Your LEM Verification Code"
    msg["From"] = SMTP_USER
    msg["To"] = to_email
    msg.attach(MIMEText(html_content, "html"))
    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as server:
            server.ehlo()
            server.starttls()
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.sendmail(SMTP_USER, to_email, msg.as_string())
        myprint(f"PIN email sent via SMTP to {to_email}")
        return True
    except Exception as e:
        myprint(f"SMTP send failed for {to_email}: {e}")
        return False


def _build_approval_html(vnc_url: str | None) -> str:
    watch = (
        f'<p>Want to watch it happen live? <a href="{vnc_url}">Open the session viewer</a>.</p>'
        if vnc_url else ""
    )
    return f"""
    <html><body>
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


def send_login_approval_email(to_email: str, vnc_url: str | None = None) -> bool:
    """Send a HIGH-PRIORITY email asking the user to approve a LinkedIn device sign-in.

    LinkedIn's "Check your LinkedIn app → tap Yes" challenge can't be solved
    programmatically, so the automation pauses briefly waiting for approval. This
    notifies the user immediately (flagged high priority) so they can act instead of
    the run silently stalling. Best-effort: returns True only if an email was dispatched.
    """
    has_sendgrid = bool(SENDGRID_API_KEY)
    has_smtp = bool(SMTP_USER) and bool(SMTP_PASSWORD)
    if not has_sendgrid and not has_smtp:
        myprint("No email provider configured — cannot send login-approval notice")
        return False

    subject = "⚠️ Action needed: approve your LinkedIn sign-in"
    html = _build_approval_html(vnc_url)

    if has_sendgrid:
        try:
            from sendgrid import SendGridAPIClient
            from sendgrid.helpers.mail import Header, Mail

            message = Mail(
                from_email=SENDGRID_FROM_EMAIL,
                to_emails=to_email,
                subject=subject,
                html_content=html,
            )
            # High-priority headers (Outlook/Gmail honor these)
            message.header = Header("X-Priority", "1")
            message.header = Header("X-MSMail-Priority", "High")
            message.header = Header("Importance", "High")
            SendGridAPIClient(SENDGRID_API_KEY).send(message)
            myprint(f"Login-approval email sent via SendGrid to {to_email}")
            return True
        except Exception as e:
            myprint(f"SendGrid login-approval send failed for {to_email}: {e}")

    if has_smtp:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = SMTP_USER
        msg["To"] = to_email
        msg["X-Priority"] = "1"
        msg["X-MSMail-Priority"] = "High"
        msg["Importance"] = "High"
        msg.attach(MIMEText(html, "html"))
        try:
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as server:
                server.ehlo()
                server.starttls()
                server.login(SMTP_USER, SMTP_PASSWORD)
                server.sendmail(SMTP_USER, to_email, msg.as_string())
            myprint(f"Login-approval email sent via SMTP to {to_email}")
            return True
        except Exception as e:
            myprint(f"SMTP login-approval send failed for {to_email}: {e}")
            return False

    return False


def _send_high_priority_email(to_email: str, subject: str, html_content: str,
                              reply_to: Optional[str] = None) -> bool:
    """Send a high-priority email via SendGrid (falling back to SMTP). Returns True if
    dispatched. `reply_to` sets the Reply-To header (used for the email-reply PIN flow)."""
    has_sendgrid = bool(SENDGRID_API_KEY)
    has_smtp = bool(SMTP_USER) and bool(SMTP_PASSWORD)
    if not has_sendgrid and not has_smtp:
        myprint(f"No email provider configured — cannot send: {subject}")
        return False

    if has_sendgrid:
        try:
            from sendgrid import SendGridAPIClient
            from sendgrid.helpers.mail import Header, Mail

            message = Mail(from_email=SENDGRID_FROM_EMAIL, to_emails=to_email,
                           subject=subject, html_content=html_content)
            message.header = Header("X-Priority", "1")
            message.header = Header("X-MSMail-Priority", "High")
            message.header = Header("Importance", "High")
            if reply_to:
                from sendgrid.helpers.mail import ReplyTo
                message.reply_to = ReplyTo(reply_to)
            SendGridAPIClient(SENDGRID_API_KEY).send(message)
            myprint(f"Email sent via SendGrid to {to_email}: {subject}")
            return True
        except Exception as e:
            myprint(f"SendGrid send failed for {to_email}: {e}")

    if has_smtp:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = SMTP_USER
        msg["To"] = to_email
        msg["X-Priority"] = "1"
        msg["X-MSMail-Priority"] = "High"
        msg["Importance"] = "High"
        if reply_to:
            msg["Reply-To"] = reply_to
        msg.attach(MIMEText(html_content, "html"))
        try:
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as server:
                server.ehlo()
                server.starttls()
                server.login(SMTP_USER, SMTP_PASSWORD)
                server.sendmail(SMTP_USER, to_email, msg.as_string())
            myprint(f"Email sent via SMTP to {to_email}: {subject}")
            return True
        except Exception as e:
            myprint(f"SMTP send failed for {to_email}: {e}")
            return False

    return False


def send_login_pin_request_email(to_email: str, reply_to: str) -> bool:
    """Ask the user to REPLY with the 6-digit LinkedIn verification code.

    The reply routes back (via SendGrid Inbound Parse) to `reply_to`, whose token
    attributes it to the paused login. High priority so the user acts before the code
    expires. Best-effort: returns True only if an email was dispatched.
    """
    subject = "⚠️ Reply ASAP with your LinkedIn verification code"
    html = (
        "<html><body>"
        "<h2>We need your LinkedIn verification code</h2>"
        "<p>LinkedIn asked us to verify this sign-in so your automation can keep running.</p>"
        "<p><strong>Check your inbox for a separate email from LinkedIn with a 6-digit code, "
        "then simply <u>reply to THIS email</u> with just that code.</strong></p>"
        "<p>The code expires quickly — please reply as soon as you can.</p>"
        "<p style='color:#666;font-size:12px'>Didn't try to sign in? You can ignore this — "
        "no one can get in without the code.</p>"
        "</body></html>"
    )
    return _send_high_priority_email(to_email, subject, html, reply_to=reply_to)


def _account_url() -> str:
    import os
    base = (os.getenv("LEM_APP_URL") or "https://lem.christopherqueenconsulting.com").rstrip("/")
    return f"{base}/account"


def send_connect_linkedin_email(to_email: str, account_url: Optional[str] = None) -> bool:
    """Email a user who has no validated LinkedIn session, prompting them to connect."""
    url = account_url or _account_url()
    html = f"""
    <html><body>
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
    return _send_high_priority_email(
        to_email, "⚠️ Action needed: connect your LinkedIn to keep automation running", html)


def send_session_revalidation_email(to_email: str, account_url: Optional[str] = None) -> bool:
    """Email a user whose stored LinkedIn session stopped working, prompting a reconnect."""
    url = account_url or _account_url()
    html = f"""
    <html><body>
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
    return _send_high_priority_email(
        to_email, "⚠️ Action needed: reconnect your LinkedIn session", html)


def send_newsletter_draft_ready_email(to_email: str, edition_title: str,
                                      scheduled_for: str, account_url: Optional[str] = None) -> bool:
    """Email a user that their newsletter draft is ready to review before it auto-publishes."""
    url = account_url or _account_url()
    title = edition_title or "Your next edition"
    html = f"""
    <html><body>
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
    return _send_high_priority_email(
        to_email, f"📝 Your newsletter draft is ready: {title}", html)


def send_pin_email(
    to_email: str,
    pin: str,
    is_new_user: bool = False,
    probe_only: bool = False,
) -> Tuple[bool, bool]:
    """Send a PIN email using the best available provider.

    Returns (success, bypassed):
      - (True, False)  — email was sent successfully
      - (False, False) — a provider is configured but the send failed
      - (True, True)   — no provider is configured; PIN step should be skipped

    probe_only=True skips the actual send and just returns whether bypass would occur.
    Used to detect bypass mode before writing the PIN to the DB.
    """
    has_sendgrid = bool(SENDGRID_API_KEY)
    has_smtp = bool(SMTP_USER) and bool(SMTP_PASSWORD)

    if not has_sendgrid and not has_smtp:
        myprint("No email provider configured — bypassing PIN verification")
        return True, True

    if probe_only:
        return False, False

    action = "create your account" if is_new_user else "sign in"
    html = _build_html(pin, action)

    if has_sendgrid:
        sent = _send_via_sendgrid(to_email, html)
        if sent:
            return True, False
        myprint("SendGrid failed — trying SMTP fallback")

    if has_smtp:
        sent = _send_via_smtp(to_email, html)
        return sent, False

    # SendGrid was configured but failed; no SMTP fallback available
    return False, False
