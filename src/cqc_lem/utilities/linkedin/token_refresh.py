"""LinkedIn OAuth token lifetime — reading it, and renewing it before it lapses (issue #600).

This module owns ONE question: how long has this user's REST access got left, and can we extend it
without them. `resolve_token_status` is where that is decided for everybody — the SPA countdown and
the daily `refresh-linkedin-tokens` beat both call it, so what a user is shown and what triggers
their reconnect email can never drift apart.

The invariant underneath every predicate here is that **unknown is treated as expired**. LinkedIn
caps authorization at 60 days and a lapsed token silently breaks posting and stats, so a token whose
expiry cannot be computed reads expired/expiring rather than healthy: the cost of being wrong that
way is one renewal attempt or one email, the cost the other way is a user who finds out their
account stopped working days later. `days_until_expiry` is the deliberate exception — a countdown
has to say "unknown" (None), never a frightening "0 days".

Full posture, including where the beat sits in the daily order: `docs/linkedin-session-health.md`.
"""

from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple

import requests

from cqc_lem.utilities.env_constants import LI_CLIENT_ID, LI_CLIENT_SECRET
from cqc_lem.utilities.logger import log_debug, log_info, log_warning

LINKEDIN_TOKEN_URL = "https://www.linkedin.com/oauth/v2/accessToken"
# Half the life of a 60-day token. This is BOTH the SPA banner threshold and the point the daily
# beat starts emailing users it could not renew (issue #600, owner decision 1A) — paired with the
# 7-day LINKEDIN_TOKEN_EMAIL_THROTTLE_DAYS it caps a user at ~4 emails across that final month.
EXPIRY_WARNING_DAYS = 30


def _to_seconds(value: object) -> Optional[int]:
    """Safely convert a DB or API value to an integer number of seconds."""
    if value is None:
        return None
    try:
        # `value` is `object` on purpose — a DB row hands back Decimal, the API a str or an int —
        # so the TypeError arm below, not a pre-check, is what makes the unchecked float() safe.
        return int(float(value))  # type: ignore[arg-type]
    except (ValueError, TypeError):
        return None


def _as_utc(value: object) -> Optional[datetime]:
    if not isinstance(value, datetime):
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def get_token_expiry(token_info: dict) -> Optional[datetime]:
    """When the access token lapses, derived from when it was issued plus its lifetime.

    LinkedIn hands back a duration, not an instant, so the answer only exists if BOTH halves were
    stored. Rows written before this pairing existed, or by a connect flow that failed part-way,
    return None — every caller here reads that None as "assume the worst", never as "fine".

    Returns:
        A timezone-aware UTC datetime, or None when either half is missing or unparseable.
    """
    created_at = _as_utc(token_info.get('access_token_created_at'))
    expires_in = _to_seconds(token_info.get('access_token_expires_in'))
    if not created_at or not expires_in:
        return None
    return created_at + timedelta(seconds=expires_in)


def is_token_expired(token_info: dict) -> bool:
    """Is this token past its expiry — or unreadable, which counts the same.

    Fails CLOSED: an unknown expiry reads expired. Every REST call made on a dead token fails
    anyway, so the only thing optimism buys is a user who is never told to reconnect.
    """
    expiry = get_token_expiry(token_info)
    if expiry is None:
        return True
    return expiry <= datetime.now(timezone.utc)


def is_token_expiring_soon(token_info: dict, days: int = EXPIRY_WARNING_DAYS) -> bool:
    """Is the token inside the renewal window?

    This is the trigger for both the SPA banner and the daily beat's renewal attempt.

    The default window is half a 60-day token's life, which is what makes automatic renewal able to
    outlive LinkedIn's 60-day cap at all. Unreadable expiry reads True for the same reason
    `is_token_expired` does: a wasted renewal attempt is cheap, a missed one is not.
    """
    expiry = get_token_expiry(token_info)
    if expiry is None:
        return True
    return expiry <= datetime.now(timezone.utc) + timedelta(days=days)


def days_until_expiry(token_info: dict) -> Optional[int]:
    """Whole days left on the access token — None when the expiry is unknown, which is NOT the
    same as zero: the SPA renders 'unknown' rather than an alarming '0 days'. Floored, so a token
    with 29h left reads '1 day' and never rounds up into a promise we can't keep.
    """
    expiry = get_token_expiry(token_info)
    if expiry is None:
        return None
    remaining = (expiry - datetime.now(timezone.utc)).total_seconds()
    return max(0, int(remaining // 86400))


def refresh_token_usable(token_info: dict) -> bool:
    """True when a refresh token exists AND has not itself lapsed. LinkedIn only issues refresh
    tokens to approved apps, so 'no refresh token' is the ordinary case — a user on that path can
    only ever reconnect by hand, which is what the expiry email is for.
    """
    refresh_token = token_info.get('refresh_token')
    if not refresh_token:
        return False
    refresh_created = _as_utc(token_info.get('refresh_token_created_at'))
    refresh_expires_in = _to_seconds(token_info.get('refresh_token_expires_in'))
    if refresh_created and refresh_expires_in:
        refresh_expiry = refresh_created + timedelta(seconds=refresh_expires_in)
        if refresh_expiry <= datetime.now(timezone.utc):
            return False
    return True


def attempt_token_refresh(user_id: int) -> Tuple[bool, Optional[str]]:
    """Exchange the stored refresh token for a fresh access token and persist BOTH halves.

    Never raises: a LinkedIn outage, a rejected grant or a response without an `access_token` all
    come back as a failure the caller reports, because this runs inside a daily sweep over every
    user and one bad row must not stop the rest. The rotated refresh token is written back with the
    access token — dropping it would strand the user on a single renewal.

    Returns:
        (succeeded, new_access_token). A False with no error is the ORDINARY case for an app
        LinkedIn never granted refresh tokens to; that path is DEBUG, not a warning, because
        warning on it would file a defect against working behaviour.
    """
    # Import here to avoid circular imports at module load
    from cqc_lem.utilities.db import get_user_token_info, update_user_access_token

    token_info = get_user_token_info(user_id)
    if not token_info:
        return False, None

    if not refresh_token_usable(token_info):
        # Expected for every app LinkedIn hasn't granted refresh tokens to — a warning here would
        # escalate working behaviour into a filed defect.
        log_debug("No usable refresh_token — cannot auto-refresh LinkedIn token", user_id=user_id)
        return False, None

    refresh_token = token_info.get('refresh_token')

    try:
        resp = requests.post(
            LINKEDIN_TOKEN_URL,
            data={
                'grant_type': 'refresh_token',
                'refresh_token': refresh_token,
                'client_id': LI_CLIENT_ID,
                'client_secret': LI_CLIENT_SECRET,
            },
            headers={'Content-Type': 'application/x-www-form-urlencoded'},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()

        new_access_token = data.get('access_token')
        expires_in = data.get('expires_in')
        new_refresh_token = data.get('refresh_token')
        new_refresh_expires_in = data.get('refresh_token_expires_in')

        if not new_access_token:
            log_warning("LinkedIn token refresh response missing access_token", user_id=user_id,
                        api_provider="linkedin")
            return False, None

        update_user_access_token(
            user_id,
            new_access_token,
            _to_seconds(expires_in),
            refresh_token=new_refresh_token,
            refresh_token_expires_in=_to_seconds(new_refresh_expires_in),
        )
        log_info("LinkedIn access token refreshed", user_id=user_id, api_provider="linkedin")
        return True, new_access_token

    except (requests.RequestException, ValueError, TypeError) as e:
        log_warning("LinkedIn token refresh failed", exc=e, user_id=user_id,
                    api_provider="linkedin")
        return False, None


def resolve_token_status(user_id: int, auto_refresh: bool = True) -> dict:
    """The ONE place a user's LinkedIn token state is decided (issue #600).

    Both readers use it — the SPA's `/user/token_status` and the daily renewal beat — so the
    countdown a user sees and the countdown that triggers their email can never disagree.
    `auto_refresh` renews in place when the token is inside the warning window and a usable refresh
    token exists; the returned state is the state AFTER that attempt.
    """
    from cqc_lem.utilities.db import get_user_token_info

    token_info = get_user_token_info(user_id)
    if not token_info or not token_info.get('access_token'):
        return {
            "connected": False,
            "token_expiry_date": None,
            "days_remaining": None,
            "is_expiring_soon": True,
            "is_expired": True,
            "can_auto_refresh": False,
            "refresh_attempted": False,
            "refresh_succeeded": False,
        }

    expiring_soon = is_token_expiring_soon(token_info)
    expired = is_token_expired(token_info)
    refresh_attempted = False
    refresh_succeeded = False

    if auto_refresh and expiring_soon and refresh_token_usable(token_info):
        refresh_attempted = True
        refresh_succeeded, _ = attempt_token_refresh(user_id)
        if refresh_succeeded:
            token_info = get_user_token_info(user_id) or token_info
            expiring_soon = is_token_expiring_soon(token_info)
            expired = is_token_expired(token_info)

    expiry = get_token_expiry(token_info)
    return {
        "connected": True,
        "token_expiry_date": expiry.isoformat() if expiry else None,
        "days_remaining": days_until_expiry(token_info),
        "is_expiring_soon": expiring_soon,
        "is_expired": expired,
        "can_auto_refresh": refresh_token_usable(token_info),
        "refresh_attempted": refresh_attempted,
        "refresh_succeeded": refresh_succeeded,
    }
