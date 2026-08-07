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
        return int(float(value))
    except (ValueError, TypeError):
        return None


def _as_utc(value: object) -> Optional[datetime]:
    if not isinstance(value, datetime):
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def get_token_expiry(token_info: dict) -> Optional[datetime]:
    created_at = _as_utc(token_info.get('access_token_created_at'))
    expires_in = _to_seconds(token_info.get('access_token_expires_in'))
    if not created_at or not expires_in:
        return None
    return created_at + timedelta(seconds=expires_in)


def is_token_expired(token_info: dict) -> bool:
    expiry = get_token_expiry(token_info)
    if expiry is None:
        return True
    return expiry <= datetime.now(timezone.utc)


def is_token_expiring_soon(token_info: dict, days: int = EXPIRY_WARNING_DAYS) -> bool:
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
