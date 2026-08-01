"""YouTube OAuth refresh-token lifecycle (issue #742) — the ONE place the token's state is decided.

The tutorial pipeline (#505) publishes with an OAuth **refresh token**, and a refresh token dies on
somebody else's schedule: a consent screen left in Testing, a revoked grant, a password change, six
months of disuse, or the 100-token-per-client rollover. Until now nothing would notice — the first
symptom would have been a render that already cost real money failing at the upload step, possibly
months from now, since the feature is deliberately off until ~1.0.

Three things follow from that, and they are all here:

- **The weekly probe IS the keep-alive.** One cheap token exchange a week (no upload, no quota) both
  proves the token is alive and resets the 6-month-disuse clock. Do NOT "optimize it away while the
  feature is off" — off is exactly when it earns its keep.
- **Undecidable is not broken.** A network blip or a 5xx from Google reports `unknown` and alerts
  nobody. Only an answer that PROVES the grant is gone (`invalid_grant`, a 4xx from the token
  endpoint, a response whose granted scopes no longer include youtube.upload) is `needs_reauth`.
  Crying wolf on a transient would train the owner to ignore the one alert that matters.
- **The token is read DB-first, env-second.** `YOUTUBE_REFRESH_TOKEN` is the seed; once a value is
  stored in `app_credentials` it wins, so a re-mint lands without a box edit + restart — and a token
  Google rotates during a refresh is persisted instead of lost.

Re-mint runbook (the Internal-app path the owner settled on): `docs/youtube-publishing.md`.
"""

import json
from datetime import datetime, timezone
from typing import Optional

from cqc_lem.utilities.db import (get_app_credential, get_app_credential_updated_at,
                                  set_app_credential)
from cqc_lem.utilities.env_constants import (MARGIN_REPORT_EMAIL, YOUTUBE_ALERT_EMAIL,
                                             YOUTUBE_CLIENT_ID, YOUTUBE_CLIENT_SECRET,
                                             YOUTUBE_PRIVACY_STATUS, YOUTUBE_REFRESH_TOKEN)
from cqc_lem.utilities.logger import log_debug, log_error, log_info, log_warning

TASK_NAME = "auto_weekly_youtube_token_check"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
UPLOAD_SCOPE = "https://www.googleapis.com/auth/youtube.upload"
CREDENTIAL_NAME = "youtube_refresh_token"
RUNBOOK = "docs/youtube-publishing.md"

# Four states, and the difference between the last two is the whole point of this module.
STATUS_OK = "ok"                          # the token minted an access token carrying the upload scope
STATUS_NEEDS_REAUTH = "needs_reauth"      # Google PROVED the grant is gone/insufficient — alert
STATUS_UNKNOWN = "unknown"                # the probe could not decide (network, 5xx, throttle)
STATUS_NOT_CONFIGURED = "not_configured"  # no OAuth credentials at all — the expected pre-1.0 state

_STATE_KEY = "youtube:token:last_probe"
_PROBE_TIMEOUT = 30

REAUTH_STEPS = (
    "Re-mint the YouTube refresh token (see docs/youtube-publishing.md):\n"
    "1. The Google Cloud project must belong to the Workspace org, or 'Internal' stays greyed out.\n"
    "2. OAuth consent screen -> User Type: Internal -> Save (no verification review, and Internal "
    "grants are not subject to the Testing-mode expiry).\n"
    "3. Google Admin -> Apps -> Additional Google services -> YouTube -> ON for that user/OU.\n"
    "4. That Workspace account needs at least Manager access to the target YouTube channel.\n"
    "5. Re-mint via OAuth Playground using the WEB client, signed in as the Workspace account.\n"
    "6. Install it without a deploy: POST /api/admin/youtube-token (admin secret), which stores it "
    "in app_credentials and takes precedence over YOUTUBE_REFRESH_TOKEN in .env."
)


def alert_email() -> str:
    return (YOUTUBE_ALERT_EMAIL or MARGIN_REPORT_EMAIL or "").strip()


def refresh_token() -> str:
    """The refresh token in force: the DB value when one is stored, else the env seed."""
    try:
        stored = get_app_credential(CREDENTIAL_NAME)
    except Exception as e:  # a DB outage must not make a configured install look unconfigured
        log_warning("Could not read the stored YouTube refresh token — falling back to env",
                    exc=e, task_name=TASK_NAME)
        stored = None
    return (stored or YOUTUBE_REFRESH_TOKEN or "").strip()


def token_source() -> str:
    """Where the token in force came from: 'db', 'env' or 'none'. State only — never the secret."""
    try:
        if get_app_credential(CREDENTIAL_NAME):
            return "db"
    except Exception:
        pass
    return "env" if (YOUTUBE_REFRESH_TOKEN or "").strip() else "none"


def store_refresh_token(token: str, note: Optional[str] = None) -> bool:
    """Install a re-minted (or Google-rotated) refresh token without a deploy."""
    value = (token or "").strip()
    if not value:
        return False
    stored = set_app_credential(CREDENTIAL_NAME, value, note=note)
    if stored:
        log_info("YouTube refresh token stored in app_credentials", task_name=TASK_NAME)
    return stored


def youtube_configured() -> bool:
    """True when a client id, secret AND a refresh token (from either source) are all present."""
    return bool(YOUTUBE_CLIENT_ID and YOUTUBE_CLIENT_SECRET and refresh_token())


def mint_access_token() -> str:
    """Exchange the refresh token for a short-lived access token. Raises on any failure — callers
    that must not die over it (the upload path) already catch."""
    import requests
    current = refresh_token()
    response = requests.post(TOKEN_ENDPOINT, timeout=_PROBE_TIMEOUT, data={
        "client_id": YOUTUBE_CLIENT_ID, "client_secret": YOUTUBE_CLIENT_SECRET,
        "refresh_token": current, "grant_type": "refresh_token",
    })
    response.raise_for_status()
    payload = _json(response)
    token = payload.get("access_token")
    if not token:
        raise RuntimeError("YouTube token refresh returned no access_token")
    _persist_rotated_token(payload, current)
    return str(token)


def probe(persist: bool = True) -> dict:
    """Exchange the refresh token once and judge the result. Never raises: a probe that cannot run
    is a state (`unknown`), not an exception. `persist=False` skips writing the Redis state record,
    for read-only callers that must not overwrite the weekly audit trail."""
    checked_at = datetime.now(timezone.utc).isoformat()
    if not (YOUTUBE_CLIENT_ID and YOUTUBE_CLIENT_SECRET):
        return _state(STATUS_NOT_CONFIGURED, "YOUTUBE_CLIENT_ID/SECRET are not set", checked_at,
                      persist=persist)
    current = refresh_token()
    if not current:
        return _state(STATUS_NOT_CONFIGURED, "No refresh token in app_credentials or .env",
                      checked_at, persist=persist)

    import requests
    try:
        response = requests.post(TOKEN_ENDPOINT, timeout=_PROBE_TIMEOUT, data={
            "client_id": YOUTUBE_CLIENT_ID, "client_secret": YOUTUBE_CLIENT_SECRET,
            "refresh_token": current, "grant_type": "refresh_token",
        })
    except Exception as e:
        return _state(STATUS_UNKNOWN, f"Token endpoint unreachable: {e}", checked_at,
                      persist=persist)

    status_code = int(getattr(response, "status_code", 0) or 0)
    payload = _json(response)
    if status_code >= 500 or status_code == 429:
        return _state(STATUS_UNKNOWN, f"Token endpoint answered HTTP {status_code}", checked_at,
                      persist=persist, http_status=status_code)
    if status_code >= 400:
        # Google names the cause here: invalid_grant (revoked/expired/rotated out),
        # invalid_client (client id/secret changed), unauthorized_client, …
        error = str(payload.get("error") or f"http_{status_code}")
        detail = str(payload.get("error_description") or "").strip()
        return _state(STATUS_NEEDS_REAUTH, f"{error}{f': {detail}' if detail else ''}", checked_at,
                      persist=persist, error=error, http_status=status_code)
    if not payload.get("access_token"):
        return _state(STATUS_NEEDS_REAUTH, "Token endpoint returned no access_token", checked_at,
                      persist=persist, error="no_access_token", http_status=status_code)

    # A refresh grant reports the scopes it actually carries. When it reports them and
    # youtube.upload is gone, the token can no longer publish even though it still mints.
    scope = str(payload.get("scope") or "").strip()
    if scope and UPLOAD_SCOPE not in scope.split():
        return _state(STATUS_NEEDS_REAUTH, f"Granted scopes no longer include {UPLOAD_SCOPE}",
                      checked_at, persist=persist, error="scope_missing", scope=scope,
                      http_status=status_code)

    if persist:
        _persist_rotated_token(payload, current)
    return _state(STATUS_OK, "Refresh token exchanged for an access token", checked_at,
                  persist=persist, scope=scope or None, http_status=status_code)


def preflight() -> dict:
    """The gate `produce_tutorial` runs BEFORE it spends on capture/TTS/render. Same probe, read-only
    (it never rewrites the weekly audit record). Only `needs_reauth` should abort a run: an
    unconfigured install still produces a usable MP4, and `unknown` is not evidence of anything."""
    state = probe(persist=False)
    state["should_abort"] = state["status"] == STATUS_NEEDS_REAUTH
    return state


def last_probe() -> Optional[dict]:
    """The most recent persisted probe result, or None when none has been recorded (or Redis is
    unavailable). No TTL: the last known state is the audit trail."""
    client = _redis()
    if client is None:
        return None
    try:
        raw = client.get(_STATE_KEY)
    except Exception as e:
        log_debug("Could not read the last YouTube token probe", exc=e)
        return None
    if not raw:
        return None
    try:
        return json.loads(raw.decode() if isinstance(raw, bytes) else raw)
    except Exception:
        return None


def status_report(live: bool = False) -> dict:
    """What the owner sees: 'connected' vs 'needs re-auth (reason)'. Reads the last recorded probe by
    default so opening a settings page never spends a round trip on Google; `live=True` re-probes."""
    state = probe(persist=True) if live else (last_probe() or probe(persist=True))
    return {
        "configured": youtube_configured(),
        "token_source": token_source(),
        "connected": state.get("status") == STATUS_OK,
        "status": state.get("status"),
        "reason": state.get("reason"),
        "error": state.get("error"),
        "scope": state.get("scope"),
        "checked_at": state.get("checked_at"),
        "token_updated_at": _token_updated_at(),
        "privacy_status": YOUTUBE_PRIVACY_STATUS,
        "runbook": RUNBOOK,
    }


def run_health_probe() -> dict:
    """The weekly beat body: probe, leave the dated audit line, and alert the owner ONLY when the
    grant is provably gone. Returns the state dict."""
    from cqc_lem.utilities.observability import track_youtube_token_check

    state = probe()
    status = state.get("status")
    if status == STATUS_OK:
        log_info(f"YouTube OAuth token OK — checked {state.get('checked_at')}, "
                 f"scope={state.get('scope') or 'not reported'}, source={token_source()}",
                 task_name=TASK_NAME)
    elif status == STATUS_NOT_CONFIGURED:
        # Expected while the tutorial feature is off — a warning here would file a defect for
        # working behaviour (see utilities/CLAUDE.md on recurrence escalation).
        log_debug(f"YouTube OAuth token probe skipped — {state.get('reason')}", task_name=TASK_NAME)
    elif status == STATUS_UNKNOWN:
        log_warning(f"YouTube OAuth token probe was undecidable — {state.get('reason')}",
                    task_name=TASK_NAME)
    else:
        log_error(f"YouTube OAuth token needs re-auth — {state.get('reason')}", task_name=TASK_NAME)

    state["emailed"] = _alert_owner(state) if status == STATUS_NEEDS_REAUTH else False
    try:
        track_youtube_token_check(state)
        state["tracked"] = True
    except Exception as e:
        log_warning("YouTube token probe PostHog capture failed", exc=e, task_name=TASK_NAME)
        state["tracked"] = False
    return state


# --- internals ---------------------------------------------------------------------------------

def _json(response) -> dict:
    try:
        payload = response.json()
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _persist_rotated_token(payload: dict, current: str) -> None:
    """Google normally omits `refresh_token` on a refresh grant — but when it does return one, the
    old value is on its way out and only the DB can hold the new one."""
    rotated = str((payload or {}).get("refresh_token") or "").strip()
    if rotated and rotated != current:
        store_refresh_token(rotated, note="rotated by Google during a token refresh")


def _token_updated_at() -> Optional[str]:
    try:
        stamp = get_app_credential_updated_at(CREDENTIAL_NAME)
    except Exception:
        return None
    return stamp.isoformat() if isinstance(stamp, datetime) else (str(stamp) if stamp else None)


def _redis():
    try:
        from cqc_lem.utilities.linkedin.rate_limit import shared_redis_client
        return shared_redis_client()
    except Exception as e:
        log_debug("Redis unavailable for the YouTube token state", exc=e)
        return None


def _state(status: str, reason: str, checked_at: str, persist: bool = True, **extra) -> dict:
    state = {"status": status, "reason": reason, "checked_at": checked_at,
             "error": extra.pop("error", None), "scope": extra.pop("scope", None), **extra}
    if persist:
        _record(state)
    return state


def _record(state: dict) -> None:
    client = _redis()
    if client is None:
        return
    try:
        client.set(_STATE_KEY, json.dumps(state, default=str))
    except Exception as e:
        log_debug("Could not record the YouTube token probe state", exc=e)


def _alert_owner(state: dict) -> bool:
    recipient = alert_email()
    if not recipient:
        log_warning("YOUTUBE_ALERT_EMAIL/MARGIN_REPORT_EMAIL not set — YouTube re-auth alert not "
                    "emailed", task_name=TASK_NAME)
        return False
    from cqc_lem.utilities.email import _dispatch_email
    body = (f"LEM could not refresh the YouTube publishing token.\n\n"
            f"Checked: {state.get('checked_at')}\n"
            f"Error: {state.get('error') or 'unknown'}\n"
            f"Detail: {state.get('reason')}\n\n{REAUTH_STEPS}\n")
    try:
        return bool(_dispatch_email(recipient, "LEM: YouTube publishing needs re-authorisation",
                                    "<pre>" + _escape(body) + "</pre>", text_content=body,
                                    high_priority=True))
    except Exception as e:
        log_error("YouTube re-auth alert email failed", exc=e, task_name=TASK_NAME)
        return False


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
