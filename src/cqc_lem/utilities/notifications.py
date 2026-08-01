"""Throttled LinkedIn-session notifications (connect / re-validate).

Used by the login flow (auto-detects a stale session) and by a scheduled task
(emails users with no validated session). Throttled to at most once per
LINKEDIN_SESSION_EMAIL_THROTTLE_DAYS so users aren't spammed every automation cycle.
"""

import os
from datetime import datetime, timedelta
from typing import Optional

from cqc_lem.utilities.db import (
    get_user_email,
    get_linkedin_session_email_sent_at,
    set_linkedin_session_email_sent_at,
)
from cqc_lem.utilities.email import (
    send_connect_linkedin_email,
    send_session_revalidation_email,
    send_newsletter_draft_ready_email,
)
from cqc_lem.utilities.logger import log_info, log_warning, myprint


def notify_linkedin_session(user_id: int, revalidation: bool = False) -> bool:
    """Email the user to connect (revalidation=False) or reconnect (True) their LinkedIn
    session. Throttled per LINKEDIN_SESSION_EMAIL_THROTTLE_DAYS (default 7). Returns True
    only if an email was actually sent."""
    throttle_days = int(os.getenv("LINKEDIN_SESSION_EMAIL_THROTTLE_DAYS", "7"))
    last = get_linkedin_session_email_sent_at(user_id)
    if last and throttle_days > 0:
        try:
            if datetime.now() - last < timedelta(days=throttle_days):
                return False
        except TypeError:
            pass  # unexpected type — fall through and send

    email = get_user_email(user_id)
    if not email:
        return False

    sent = (send_session_revalidation_email(email) if revalidation
            else send_connect_linkedin_email(email))
    if sent:
        set_linkedin_session_email_sent_at(user_id)
        myprint(f"Sent LinkedIn session {'re-validation' if revalidation else 'connect'} "
                f"email to user_id {user_id}")
    return sent


TOKEN_EXPIRY_EMAIL_KEY = "lem:linkedin_token_expiry_email:{user_id}"


def notify_linkedin_token_expiring(user_id: int, days_remaining: Optional[int] = None,
                                   expires_on: Optional[str] = None) -> bool:
    """Email a user whose LinkedIn authorization is lapsing and could NOT be auto-renewed (issue
    #600). Throttled per-user to LINKEDIN_TOKEN_EMAIL_THROTTLE_DAYS (default 7) through Redis, so
    the daily renewal beat can run every day without spamming. **Fails open**: the reported bug was
    an expiry warning in the SPA with no email behind it, so a Redis outage degrades to at most one
    email a day rather than to silence. Returns True only if an email was actually sent."""
    throttle_days = int(os.getenv("LINKEDIN_TOKEN_EMAIL_THROTTLE_DAYS", "7"))
    key = TOKEN_EXPIRY_EMAIL_KEY.format(user_id=user_id)
    claimed_client = None
    if throttle_days > 0:
        try:
            from cqc_lem.utilities.linkedin.rate_limit import shared_redis_client
            client = shared_redis_client()
            if client is not None:
                if not client.set(key, "1", nx=True, ex=throttle_days * 86400):
                    return False
                claimed_client = client
        except Exception as e:
            log_warning("Could not read LinkedIn token email throttle — sending anyway", exc=e,
                        user_id=user_id, action_type="token_expiry")

    sent = False
    try:
        email = get_user_email(user_id)
        if email:
            from cqc_lem.utilities.email import send_linkedin_token_expiring_email
            sent = send_linkedin_token_expiring_email(email, days_remaining, expires_on)
        if sent:
            log_info("Sent LinkedIn token-expiring notice", user_id=user_id,
                     action_type="token_expiry")
    except Exception as e:
        log_warning("Could not send LinkedIn token-expiring notice", exc=e, user_id=user_id,
                    action_type="token_expiry")

    if not sent and claimed_client is not None:
        # The slot has to be claimed BEFORE the send (two workers must not both email), but holding
        # it after a send that never happened silences this user for a week — which is the exact
        # bug #600 exists to fix. Release it so tomorrow's pass tries again.
        try:
            claimed_client.delete(key)
        except Exception as e:
            log_warning("Could not release LinkedIn token email throttle after a failed send",
                        exc=e, user_id=user_id, action_type="token_expiry")
    return sent


def notify_onboarding_nudge(user_id: int, nudge: dict) -> bool:
    """Email a stalled user their next-best activation nudge (issue #500). Throttling is the caller's
    job — each nudge is one-shot per user via the onboarding_nudges ledger. Returns True only if an
    email was actually sent."""
    try:
        email = get_user_email(user_id)
        if not email:
            return False
        from cqc_lem.utilities.email import send_onboarding_nudge_email
        sent = send_onboarding_nudge_email(
            email,
            subject=nudge.get("subject") or "Finish setting up LinkedIn Engagement Manager",
            headline=nudge.get("headline") or "One step left",
            body=nudge.get("body") or "",
            cta_label=nudge.get("cta_label") or "Finish setup",
            cta_path=nudge.get("cta_path") or "/account",
        )
        if sent:
            from cqc_lem.utilities.observability import track_onboarding_nudge
            track_onboarding_nudge(user_id, str(nudge.get("key")))
            log_info(f"Sent onboarding nudge '{nudge.get('key')}'", user_id=user_id,
                     action_type="onboarding_nudge")
        return sent
    except Exception as e:
        log_warning("Could not send onboarding nudge", exc=e, user_id=user_id,
                    action_type="onboarding_nudge")
        return False


def notify_survey_prompt(user_id: int, survey: dict) -> bool:
    """Email an NPS/review invite (issue #501). One-shot per survey is the caller's job (the
    survey_prompts ledger). Returns True only if an email was actually sent."""
    try:
        email = get_user_email(user_id)
        if not email:
            return False
        from cqc_lem.utilities.email import send_survey_prompt_email
        sent = send_survey_prompt_email(
            email,
            subject=survey.get("subject") or "How are we doing?",
            headline=survey.get("headline") or "How are we doing?",
            body=survey.get("body") or "",
            cta_label=survey.get("cta_label") or "Answer",
            cta_path=survey.get("cta_path") or "/",
        )
        if sent:
            from cqc_lem.utilities.observability import track_survey_prompt
            track_survey_prompt(user_id, str(survey.get("key")))
            log_info(f"Sent survey prompt '{survey.get('key')}'", user_id=user_id,
                     action_type="survey_prompt")
        return sent
    except Exception as e:
        log_warning("Could not send survey prompt", exc=e, user_id=user_id,
                    action_type="survey_prompt")
        return False


def notify_shipped_fix(user_id: int, changelog_line: str, issue_number: int) -> bool:
    """Email a reporter that their fix shipped (issue #502). "Notified once" is the caller's job —
    the shipped_notice_recipients PK — so this stays a pure send. Returns True only if an email
    actually went out; a False keeps the reporter in the queue for the next pass."""
    try:
        email = get_user_email(user_id)
        if not email:
            return False
        from cqc_lem.utilities.email import send_shipped_fix_email
        sent = send_shipped_fix_email(email, changelog_line, issue_number)
        if sent:
            from cqc_lem.utilities.observability import track_shipped_notice
            track_shipped_notice(user_id, int(issue_number))
            log_info(f"Sent shipped-fix notice for issue #{issue_number}", user_id=user_id,
                     action_type="shipped_notice")
        return sent
    except Exception as e:
        log_warning("Could not send shipped-fix notice", exc=e, user_id=user_id,
                    action_type="shipped_notice")
        return False


def notify_faq_answer(user_id: int, question: str, answer: str) -> bool:
    """Send an asker the answer the auto-FAQ pass wrote for their question (issue #507). "Answered
    once" is the caller's job (the feedback row moves to `resolved`), so this stays a pure send.
    Returns True only if an email actually went out."""
    try:
        email = get_user_email(user_id)
        if not email:
            return False
        from cqc_lem.utilities.email import send_faq_answer_email
        sent = send_faq_answer_email(email, question, answer)
        if sent:
            log_info("Sent auto-FAQ answer", user_id=user_id, action_type="faq_answer")
        return sent
    except Exception as e:
        log_warning("Could not send auto-FAQ answer", exc=e, user_id=user_id,
                    action_type="faq_answer")
        return False


def notify_content_generation_ready(user_id: int, ready_count: int, failed_count: int = 0) -> bool:
    """Email the user that their weekly content finished generating (issue #545). Sent once per
    run by the generation task — no throttling ledger, the run itself is the trigger. Non-fatal:
    returns True only if an email was actually sent."""
    try:
        if ready_count <= 0:
            return False
        email = get_user_email(user_id)
        if not email:
            return False
        from cqc_lem.utilities.email import send_content_generation_ready_email
        sent = send_content_generation_ready_email(email, ready_count, failed_count)
        if sent:
            log_info(f"Sent content-generation-ready email for {ready_count} post(s)",
                     user_id=user_id, action_type="content_generation")
        return sent
    except Exception as e:
        log_warning("Could not send content-generation-ready email", exc=e, user_id=user_id,
                    action_type="content_generation")
        return False


def notify_suppression_tripwire(user_id: int, reason: str) -> bool:
    """Email the user that the suppression tripwire paused their engagement automation (issue #629).
    No throttling ledger: the trip itself is one-shot per user until a human re-enables, so the task
    only ever calls this on the transition into a tripped state. Non-fatal — a failed send must not
    undo the pause, which is the part that actually protects the account."""
    try:
        email = get_user_email(user_id)
        if not email:
            return False
        from cqc_lem.utilities.email import send_suppression_tripwire_email
        sent = send_suppression_tripwire_email(email, reason)
        if sent:
            log_info("Sent suppression-tripwire notice", user_id=user_id,
                     action_type="rate_limit")
        return sent
    except Exception as e:
        log_warning("Could not send suppression-tripwire notice", exc=e, user_id=user_id,
                    action_type="rate_limit")
        return False


def notify_newsletter_draft_ready(user_id: int, edition_title: str, scheduled_for) -> bool:
    """Email the user that their newsletter draft is ready to review and when it auto-publishes.
    Non-fatal — returns True only if an email was actually sent."""
    try:
        email = get_user_email(user_id)
        if not email:
            return False
        when = scheduled_for.strftime("%A, %B %d at %I:%M %p UTC") if hasattr(
            scheduled_for, "strftime") else str(scheduled_for)
        sent = send_newsletter_draft_ready_email(email, edition_title, when)
        if sent:
            myprint(f"Sent newsletter draft-ready email to user_id {user_id}")
        return sent
    except Exception as e:
        myprint(f"Could not send newsletter draft-ready email to user_id {user_id} | Error: {e}")
        return False
