"""`/api/auth/*` — signing in, signing out, and proving a second factor (#1154).

The LAST slice of the router split, and the one sitting closest to the invariants: #745 2b/2c
(the PIN is a BOOTSTRAP, not a key), #905/#1026 (scopes are surfaces), #914, #950, #957.

**Almost nothing about how a caller is authenticated lives here.** `get_session_user_id` is the ONE
resolver and its transitive closure is 36 symbols carrying every one of those invariants plus ~596
test patch sites, so it stays in `main` and is reached as `_main.get_session_user_id` — an attribute
resolved at REQUEST time. `patch("cqc_lem.api.main.get_session_user_id")` therefore still binds what
these handlers read. What is here is the other half: the ceremonies that MINT a session, and the
cookie it is handed back in.

Six more symbols stay in `main` for a different reason — `/api/user`'s enrolment and step-up
handlers still read them (`_main.current_session_token`, `_main._utc_iso`, `_main._challenge_expiry`,
`_main._passkeys_or_503`, `_main._verify_assertion_for_user`, `_main._enrollment_held`). A symbol read
from both sides needs exactly one home, and `main` is where the other router already looks.

**`linkedin_auth_init` stays in `main` too, and it is the only handler under this prefix that does.**
It is registered TWICE — `@app.get("/auth/linkedin/")` as well as `@router.get(...)` — because
LinkedIn's OAuth flow lives outside `/api`, next to the `/auth/linkedin/callback` it redirects to. A
decorator binds `app` at IMPORT time, and `_main` is imported LAST here, below the routes; that is
the #1192 hazard, and the thing being bound is the application object itself. So the LinkedIn OAuth
pair stays together with `app`, and this module is the LEM identity surface: PIN, passkey,
second factor, session, logout.

`from cqc_lem.api import main as _main` sits at the BOTTOM; `routers/__init__.py` has the prefix
rule and the import-order reasoning.
"""

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel

from cqc_lem.api.models import ResponseModel, SessionTokenField
from cqc_lem.utilities.auth_factors import (
    METHOD_RECOVERY,
    METHOD_TOTP,
    available_methods,
    enrollment_required,
    has_strong_factor,
    strong_factor_deadline,
    strong_factor_prompt_due,
    verify_recovery_code,
    verify_totp_code,
)
from cqc_lem.utilities.auth_rate_limit import (
    check_auth_init,
    check_auth_verify,
    clear_auth_limits,
)
from cqc_lem.utilities.db import (
    SESSION_SCOPE_ENROLL,
    SESSION_SCOPE_FULL,
    SESSION_SCOPE_RECOVERY,
    AuthAuditEvent,
    add_user_by_email,
    claim_auth_challenge_attempt,
    clear_challenge_attempts,
    consume_auth_challenge,
    count_challenge_attempts,
    count_recovery_codes,
    create_auth_challenge,
    create_pin_for_email,
    create_session,
    delete_pin_for_email,
    delete_session,
    finish_auth_challenge,
    get_pin_lockout,
    get_user_analytics_profile,
    get_user_email,
    get_user_id,
    get_user_public_uid,
    is_user_admin,
    mark_email_verified,
    record_auth_event,
    verify_pin_for_email,
)
from cqc_lem.utilities.email import generate_pin, hash_pin, send_pin_email
from cqc_lem.utilities.env_constants import (
    SECOND_FACTOR_ATTEMPT_WINDOW_MINUTES,
    SECOND_FACTOR_MAX_ATTEMPTS,
    SESSION_ABSOLUTE_MAX_DAYS,
    SESSION_COOKIE_NAME,
    SESSION_COOKIE_SAMESITE,
    SESSION_COOKIE_SECURE,
)
from cqc_lem.utilities.logger import log_debug, log_warning
from cqc_lem.utilities.observability import (
    FUNNEL_SIGNUP_COMPLETED,
    FUNNEL_SIGNUP_STARTED,
    FUNNEL_TRIAL_STARTED,
    anonymous_distinct_id,
    track_funnel_event,
)
from cqc_lem.utilities.webauthn_util import build_authentication_options

# The FULL prefix, declared here rather than passed to include_router: `route.path` is what every
# scope and admin check reads, and an include-time prefix never reaches it.
router = APIRouter(prefix="/api/auth")


def _mint_login_session(user_id: int, *, user_agent: Optional[str],
                        ip: Optional[str]) -> tuple[Optional[str], bool]:
    """Mint the session a completed LOGIN gets, and decide the enrolment hold in ONE place.

    Every path that signs someone in from the email PIN goes through here rather than computing
    `enrollment_required` beside its own `create_session`. The rule was written at two call sites
    in an earlier draft, and a rule spelled out at two call sites is a rule the third login path
    forgets — the same argument this module already makes about enforcing the narrowing at ~150
    handlers, applied to the minting side.

    The strong-factor login paths (`/auth/passkey/login/complete`, `/auth/second-factor/verify`)
    deliberately do NOT call this: reaching either one proves the account HOLDS a factor, so
    `enrollment_required` is false for them by construction and a hold there would be unreachable
    code pretending to be a control.
    """
    held = enrollment_required(user_id)
    token = create_session(user_id, user_agent=user_agent, ip=ip,
                           scope=(SESSION_SCOPE_ENROLL if held else SESSION_SCOPE_FULL))
    return token, held


def _samesite() -> str:
    """Starlette rejects anything outside lax/strict/none, and a typo in the env must not turn every
    login into a 500. Unknown values fall back to the documented default.
    """
    value = (SESSION_COOKIE_SAMESITE or "").strip().lower()
    return value if value in ("lax", "strict", "none") else "lax"


def _set_session_cookie(response: Response, token: str) -> None:
    """Issue the session cookie. max_age is the ABSOLUTE session cap, not the idle window — the
    server slides the idle expiry itself, and a cookie that expired mid-idle-window would log an
    active user out.
    """
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        max_age=SESSION_ABSOLUTE_MAX_DAYS * 24 * 3600,
        httponly=True,
        secure=SESSION_COOKIE_SECURE,
        samesite=_samesite(),
        path="/",
    )


def _clear_session_cookie(response: Response) -> None:
    response.delete_cookie(key=SESSION_COOKIE_NAME, path="/", httponly=True,
                           secure=SESSION_COOKIE_SECURE, samesite=_samesite())


class FunnelAttribution(BaseModel):
    """Where a visitor came from, captured client-side on first landing (issue #503). Every field is
    optional — a direct visit sends none of them.
    """
    utm_source: Optional[str] = None
    utm_medium: Optional[str] = None
    utm_campaign: Optional[str] = None
    utm_content: Optional[str] = None
    utm_term: Optional[str] = None
    referrer: Optional[str] = None
    landing_page: Optional[str] = None
    channel: Optional[str] = None
    # The referral link's referrer id (issue #658, `?ref=<user id>`). Allow-listed like the rest —
    # `normalize_attribution` drops anything not in its key list.
    ref: Optional[str] = None


class AuthInitRequest(BaseModel):
    """Body of `POST /auth/email/init` — ask for a sign-in PIN. Unauthenticated by definition.

    `attribution` only ever matters when this address is NEW: a known email re-authenticating is a
    login, not a signup, and never enters the funnel.
    """

    email: str
    attribution: Optional[FunnelAttribution] = None


class AuthVerifyRequest(BaseModel):
    """Body of `POST /auth/email/verify`.

    The PIN is a BOOTSTRAP, not a key (issue #745, 2c): on an account holding a strong factor it proves the address
    and then hands over to the second-factor stage rather than opening a session.
    """

    email: str
    pin: str
    attribution: Optional[FunnelAttribution] = None


class LogoutRequest(BaseModel):
    """Body of `POST /auth/logout`.

    The token is optional because the cookie is usually the whole credential — the handler clears the cookie
    regardless of whether a row was found to delete.
    """

    session_token: SessionTokenField = None


class PasskeyLoginBeginRequest(BaseModel):
    """No email field on purpose: the ceremony is username-less, so this endpoint cannot become an
    account-existence oracle. The assertion names the credential, and the credential names the
    account.

    No attribution field either: an account that holds a passkey enrolled it while signed in, so
    this path can never be a signup and nothing downstream would read one.
    """


class PasskeyLoginCompleteRequest(BaseModel):
    """Body of `POST /auth/passkey/login/complete`.

    No session and no email: the assertion names the credential and the credential names the account, which is what
    keeps the ceremony username-less.
    """

    handle: str
    credential: Dict[str, Any]


class SecondFactorVerifyRequest(BaseModel):
    """Finish a login that a PIN only bootstrapped. `pending_token` is the handle issued by
    /auth/email/verify; it is single-use and short-lived.
    """
    pending_token: str
    method: str
    code: str


def _attribution_dict(attribution: Optional[FunnelAttribution]) -> dict:
    return attribution.model_dump(exclude_none=True) if attribution else {}


def _track_signup_funnel(user_id: int, email: str, attribution: dict, pin_bypassed: bool) -> None:
    """`signup_completed` + `trial_started` for an account that was just created. Both are aliased
    onto the anonymous id `signup_started` used, so the funnel joins end to end in PostHog; the trial
    starts at the same instant because `add_user_by_email` opens the free trial on insert.
    """
    from cqc_lem.utilities.env_constants import FREE_TRIAL_DAYS
    anon_id = anonymous_distinct_id(email)
    track_funnel_event(FUNNEL_SIGNUP_COMPLETED, user_id=user_id, attribution=attribution,
                       alias_from=anon_id, method="email_pin", pin_bypassed=pin_bypassed)
    track_funnel_event(FUNNEL_TRIAL_STARTED, user_id=user_id, attribution=attribution,
                       trial_days=FREE_TRIAL_DAYS, tier="free_trial")


def _start_affiliate_membership(user_id: int, attribution: dict) -> None:
    """Enrol a brand-new user in the affiliate program (A) and attribute their signup to whoever
    referred them (issue #737).

    Order matters: attribution first, because `enroll_user` extends THIS user's trial and a failure
    there must not cost the referrer their pending referral. Best-effort throughout — the affiliate
    program is a perk, and no part of it may ever fail a signup.
    """
    try:
        from cqc_lem.utilities.marketing.affiliate import attribute_referral, enroll_user
        attribute_referral(user_id, attribution)
        enroll_user(user_id)
    except Exception as e:
        log_warning("Could not start affiliate membership", exc=e, user_id=user_id)


@router.post("/email/init")
def auth_email_init(request: AuthInitRequest, http_request: Request = None,
                    response: Response = None) -> ResponseModel:
    """Step one of email sign-in: mail a PIN, or take the no-mail-provider bypass.

    The reply is deliberately the same shape whether or not the address has an account — `bypass`
    and `user_exists` are the only signal, and the PIN itself is what proves anything.

    The bypass branch (no mail provider configured) is the WEAKEST way in, so it still hands an
    account holding a strong factor to the second-factor stage, and it does NOT stamp
    `email_verified_at` or clear the auth limiters: nothing was proved on that path.
    """
    email = request.email.strip().lower()
    if not email:
        raise HTTPException(status_code=400, detail="Email is required")

    ip = _main._client_ip(http_request)
    user_agent = _main._user_agent(http_request)
    verdict = check_auth_init(email, ip)
    if not verdict.allowed:
        record_auth_event(AuthAuditEvent.LOGIN_RATE_LIMITED, email=email, ip=ip,
                          user_agent=user_agent, success=False, details={"scope": verdict.scope})
        raise HTTPException(status_code=429, detail="Too many sign-in requests — try again later",
                            headers={"Retry-After": str(verdict.retry_after_seconds)})

    user_exists = bool(get_user_id(email))
    attribution = _attribution_dict(request.attribution)
    if not user_exists:
        # A known email re-authenticating is a login, not a signup — only new emails enter the funnel.
        track_funnel_event(FUNNEL_SIGNUP_STARTED, distinct_id=anonymous_distinct_id(email),
                           attribution=attribution, method="email_pin")
    pin = generate_pin()
    pin_hash = hash_pin(pin, email)

    # Check bypass first so we skip DB + email entirely when no provider is configured
    _, bypassed = send_pin_email(email, pin, is_new_user=not user_exists, probe_only=True)
    if bypassed:
        # No email provider configured — create user + session immediately, skip PIN step
        user_id = get_user_id(email)
        is_new_user = user_id is None
        if is_new_user:
            user_id = add_user_by_email(email)
            if not user_id:
                raise HTTPException(status_code=500, detail="Could not create user record")
        # The no-mail-provider bypass skips the PIN entirely, so it is the WEAKEST way in — an
        # account that enrolled a strong factor still has to prove it, or this branch would be a
        # hole straight through 2c on any deployment with mail unconfigured.
        if has_strong_factor(user_id):
            # NOT clear_auth_limits here, unlike the PIN path below: nothing was proved on this
            # branch — it is the bypass, no PIN is ever typed — so there are no legitimate typo
            # counters to forgive, and clearing them would hand an unauthenticated caller a way to
            # reset every limiter in front of the second factor once per request.
            return ResponseModel(status_code=200, detail={
                "bypass": True,
                **_begin_second_factor(user_id, email, ip, user_agent, "pin_bypass"),
            })
        session_token, held = _mint_login_session(user_id, user_agent=user_agent, ip=ip)
        if not session_token:
            raise HTTPException(status_code=500, detail="Could not create session")
        if is_new_user:
            _track_signup_funnel(user_id, email, attribution, pin_bypassed=True)
            _start_affiliate_membership(user_id, attribution)
        # NOT mark_email_verified: this branch is the no-mail-provider bypass, so nobody proved
        # control of the address. email_verified_at exists to record that a PIN actually reached
        # it — stamping it here would make the column say "verified" about the one login that
        # verifies nothing, and 2c's step-up gate is meant to read it.
        clear_auth_limits(email, ip)
        record_auth_event(AuthAuditEvent.LOGIN_SUCCESS, user_id=user_id, email=email, ip=ip,
                          user_agent=user_agent, details={"method": "pin_bypass"})
        if response is not None:
            _set_session_cookie(response, session_token)
        return ResponseModel(status_code=200, detail={
            "bypass": True,
            "session_token": session_token,
            "email": email,
            "is_new_user": is_new_user,
            "enrollment_required": held,
        })

    # Write PIN to DB BEFORE sending email — if send fails we can delete the row;
    # the reverse order would leave users with an unverifiable PIN in their inbox.
    if not create_pin_for_email(email, pin_hash):
        raise HTTPException(status_code=500, detail="Could not create PIN")

    sent, _ = send_pin_email(email, pin, is_new_user=not user_exists)
    if not sent:
        # Clean up the PIN row so the user can retry without waiting for expiry
        delete_pin_for_email(email)
        raise HTTPException(status_code=500, detail="Could not send PIN email — check email provider settings")

    return ResponseModel(status_code=200, detail={"bypass": False, "user_exists": user_exists, "message": "PIN sent to email"})


@router.post("/email/verify")
def auth_email_verify(request: AuthVerifyRequest, http_request: Request = None,
                      response: Response = None) -> ResponseModel:
    """Step two: spend the PIN. It creates the account on first successful verify.

    Three outcomes, and which one you get is the account's shape, not the request's. A correct PIN
    always stamps `email_verified_at` — the mailbox received it, so control was proved. On an
    account holding a strong factor it then hands over to the second-factor stage instead of
    minting a session. Otherwise a session is minted, possibly HELD to the enrolment surface if
    this account is past `REQUIRE_STRONG_FACTOR_AFTER` with no factor (`enrollment_required`) — a
    hold is never a lockout.
    """
    email = request.email.strip().lower()
    pin = request.pin.strip()
    if not email or not pin:
        raise HTTPException(status_code=400, detail="Email and PIN are required")

    ip = _main._client_ip(http_request)
    user_agent = _main._user_agent(http_request)
    verdict = check_auth_verify(email, ip)
    if not verdict.allowed:
        record_auth_event(AuthAuditEvent.LOGIN_RATE_LIMITED, email=email, ip=ip,
                          user_agent=user_agent, success=False, details={"scope": verdict.scope})
        raise HTTPException(status_code=429, detail="Too many attempts — try again later",
                            headers={"Retry-After": str(verdict.retry_after_seconds)})

    locked_until = get_pin_lockout(email)
    if locked_until:
        record_auth_event(AuthAuditEvent.PIN_LOCKED, email=email, ip=ip, user_agent=user_agent,
                          success=False)
        raise HTTPException(status_code=429,
                            detail="Too many incorrect PINs — request a new one shortly")

    pin_hash = hash_pin(pin, email)
    if not verify_pin_for_email(email, pin_hash):
        # Audited by EMAIL, not by user id: resolving the account here would add a lookup on the
        # one path an attacker controls, and the address is what the row needs anyway.
        record_auth_event(AuthAuditEvent.LOGIN_FAILED, email=email, ip=ip, user_agent=user_agent,
                          success=False, details={"reason": "bad_pin"})
        raise HTTPException(status_code=401, detail="Invalid or expired PIN")

    user_id = get_user_id(email)
    is_new_user = user_id is None
    if is_new_user:
        user_id = add_user_by_email(email)
        if not user_id:
            raise HTTPException(status_code=500, detail="Could not create user record")

    # The PIN is now a BOOTSTRAP, not a key (issue #745, 2c / design §4). The mailbox proved the
    # address — which is why email_verified_at is stamped below either way — but for an account
    # that has enrolled a strong factor it does NOT open a session on its own. Stamp first: a
    # mailbox that received the PIN proved control whether or not a second factor follows.
    mark_email_verified(user_id)
    if has_strong_factor(user_id):
        detail = _begin_second_factor(user_id, email, ip, user_agent, "pin")
        # The PIN itself SUCCEEDED — drop its counters before handing over to the second-factor
        # stage, which is limited out of the same buckets. Without this a user who mistyped the PIN
        # a few times arrives at the passkey/TOTP prompt already throttled. Safe to clear here and
        # not on the bypass branch precisely because a proof preceded it — and the second factor's
        # own budget (above) is deliberately NOT one of the counters this resets.
        clear_auth_limits(email, ip)
        return ResponseModel(status_code=200, detail=detail)

    # Past REQUIRE_STRONG_FACTOR_AFTER an account holding no strong factor is signed in but HELD
    # (2c.1, design §7 Stage 2): the PIN is still a valid bootstrap — nobody is ever locked out —
    # but the session it mints reaches only the enrolment surface until a factor lands.
    session_token, held = _mint_login_session(user_id, user_agent=user_agent, ip=ip)
    if not session_token:
        raise HTTPException(status_code=500, detail="Could not create session")

    if is_new_user:
        signup_attribution = _attribution_dict(request.attribution)
        _track_signup_funnel(user_id, email, signup_attribution, pin_bypassed=False)
        _start_affiliate_membership(user_id, signup_attribution)

    clear_auth_limits(email, ip)
    record_auth_event(AuthAuditEvent.LOGIN_SUCCESS, user_id=user_id, email=email, ip=ip,
                      user_agent=user_agent, details={"method": "email_pin",
                                                      "enrollment_required": held})
    if response is not None:
        _set_session_cookie(response, session_token)

    return ResponseModel(
        status_code=200,
        detail={"session_token": session_token, "email": email, "is_new_user": is_new_user,
                "enrollment_required": held},
    )


@router.post("/logout")
def auth_logout(request: LogoutRequest, http_request: Request = None,
                response: Response = None) -> ResponseModel:
    """Sign out: delete the session row and clear the cookie. Never 500s.

    Order and error handling exist for one reason — whatever happens to the audit trail, the row
    and the cookie must still go. A logout that raises leaves the user signed in while telling them
    they are not.
    """
    token = _main.current_session_token(request.session_token)
    # Best effort, and in this order: whatever happens to the audit trail, the session row and the
    # cookie must still go. A logout that 500s leaves the user signed in.
    try:
        user_id = _main.get_session_user_id(request.session_token)
    except Exception as e:
        log_debug(f"Could not resolve user for logout audit: {e}")
        user_id = None
    if token:
        delete_session(token)
    if response is not None:
        _clear_session_cookie(response)
    # Audited LAST and swallowed: the row and the cookie are the logout, and a DB hiccup on the
    # audit write must not turn a completed sign-out into a 500 the browser reads as "still in".
    if user_id:
        try:
            record_auth_event(AuthAuditEvent.LOGOUT, user_id=user_id, ip=_main._client_ip(http_request),
                              user_agent=_main._user_agent(http_request))
        except Exception as e:
            log_debug(f"Could not record logout audit event: {e}")
    return ResponseModel(status_code=200, detail="Logged out")


@router.get("/session")
def auth_check_session(session_token: Optional[str] = None) -> ResponseModel:
    """Who am I — the boot call every authenticated page makes.

    Because it is on every page, several other facts ride along here rather than costing their own
    round trip.

    It carries the PostHog person properties for `$identify`, the two facts Surveys target on, and
    both enrolment states. Those two are different questions: `enrollment_required` is HARD (this
    session is held to the enrolment surface and the SPA must render the gate instead of the app),
    `strong_factor_prompt` is the soft pre-deadline nudge. Neither is a "dismissed" flag —
    dismissal is the browser's business; enrolling is what makes them go away.
    """
    user_id = _main.get_session_user_id(session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    email = get_user_email(user_id)
    # The mandatory-enrolment state (2c.1). `enrollment_required` is the HARD one — this session is
    # held and the SPA must render the gate instead of the app. `strong_factor_prompt` is the soft
    # one: a deadline exists and this account still has nothing, so the pre-deadline nudge is due.
    # It is deliberately not a "dismissed" flag — dismissal is the browser's business, enrolling is
    # what makes the prompt go away for good.
    deadline = strong_factor_deadline()
    held = _main._enrollment_held()
    # Person facts the SPA sets on the PostHog person at $identify (issue #646). Plan/timezone/
    # signup only — never credentials — and the session check is the one call every authenticated
    # page already makes, so identify needs no extra round trip.
    profile = get_user_analytics_profile(user_id)
    return ResponseModel(status_code=200, detail={
        "user_id": user_id,
        # The identifier that may safely appear in a URL, a log line or a support ticket — the
        # sequential row id never should (issue #745, 2b).
        "public_uid": get_user_public_uid(user_id),
        "email": email,
        "plan": profile.get("subscription_tier"),
        "plan_status": profile.get("subscription_status"),
        "timezone": profile.get("timezone"),
        "created_at": _main._utc_iso(profile.get("created_at")),
        # The two facts PostHog Surveys target on (issue #653). They ride on the session check
        # because that is the call every authenticated page already makes — a survey that needed its
        # own round trip would be a survey that never fired on the first page view.
        "onboarding_completed_at": _main._utc_iso(profile.get("onboarding_completed_at")),
        "posts_approved": int(profile.get("posts_approved") or 0),
        "is_admin": is_user_admin(user_id),
        "enrollment_required": held,
        "strong_factor_deadline": _main._utc_iso(deadline),
        "strong_factor_prompt": bool(deadline) and strong_factor_prompt_due(user_id),
    })


# The two challenge kinds the LOGIN ceremonies use. Enrolment and step-up are the account owner
# acting on their own account, so CHALLENGE_REGISTER and CHALLENGE_STEP_UP went to the /api/user
# router with the handlers that raise them (#1154).
CHALLENGE_LOGIN = "webauthn_login"
CHALLENGE_SECOND_FACTOR = "second_factor"


def _begin_second_factor(user_id: int, email: str, ip: Optional[str], user_agent: Optional[str],
                         path: str) -> Dict[str, Any]:
    """Hand a bootstrapped login over to its second stage, with a guessing budget that SURVIVES
    starting over.

    `auth_challenges.attempts` bounds one pending login; on its own that is not a bound on the
    account, because the stage in front of it hands out a fresh handle — and with it a fresh
    counter — for free. Both ways in are reachable by the attacker 2c is built against: the
    no-mail-provider bypass needs nothing at all, and a compromised mailbox (T2) can mint PINs all
    day. Five guesses per round, unlimited rounds, walks a 6-digit code space.

    So the budget is counted per ACCOUNT over `SECOND_FACTOR_ATTEMPT_WINDOW_MINUTES` and carried
    into the new handle. A wrong-code spree costs the attacker the window; a user who mistyped
    their code waits it out, and a correct code clears the count outright.
    """
    window_start = datetime.now(timezone.utc) - timedelta(
        minutes=SECOND_FACTOR_ATTEMPT_WINDOW_MINUTES)
    spent = count_challenge_attempts(user_id, CHALLENGE_SECOND_FACTOR, window_start)
    if spent < 0 or spent >= SECOND_FACTOR_MAX_ATTEMPTS:
        # spent < 0 is the DB refusing to answer — count_challenge_attempts fails closed, and so
        # does this: an unreadable budget is not an empty one.
        record_auth_event(AuthAuditEvent.LOGIN_RATE_LIMITED, user_id=user_id, email=email, ip=ip,
                          user_agent=user_agent, success=False,
                          details={"scope": "second_factor_attempts", "path": path})
        raise HTTPException(
            status_code=429, detail="Too many incorrect codes — try again shortly",
            headers={"Retry-After": str(SECOND_FACTOR_ATTEMPT_WINDOW_MINUTES * 60)})

    methods = available_methods(user_id)
    pending_token = create_auth_challenge(CHALLENGE_SECOND_FACTOR, _main._challenge_expiry(),
                                          user_id=user_id, initial_attempts=spent)
    if not pending_token:
        raise HTTPException(status_code=500, detail="Could not continue sign-in")
    record_auth_event(AuthAuditEvent.SECOND_FACTOR_REQUIRED, user_id=user_id, email=email, ip=ip,
                      user_agent=user_agent, details={"methods": methods, "path": path})
    return {
        "second_factor_required": True,
        "pending_token": pending_token,
        "methods": methods,
        "email": email,
    }


@router.post("/passkey/login/begin")
def passkey_login_begin(request: PasskeyLoginBeginRequest,
                        http_request: Request = None) -> ResponseModel:
    """Username-less passkey sign-in. Public, and it takes no email: the browser offers whatever
    discoverable passkey it holds for this origin, so nothing here can be probed for whether an
    address has an account.
    """
    _main._passkeys_or_503()
    # Unauthenticated and it writes a row, so it is bounded per client IP like the PIN paths. The
    # email bucket is skipped by design — there is no address to key on and inventing one would
    # collapse every anonymous caller into a single shared limit.
    verdict = check_auth_init("", _main._client_ip(http_request))
    if not verdict.allowed:
        raise HTTPException(status_code=429, detail="Too many sign-in requests — try again later",
                            headers={"Retry-After": str(verdict.retry_after_seconds)})
    options, challenge = build_authentication_options()
    handle = create_auth_challenge(CHALLENGE_LOGIN, _main._challenge_expiry(), challenge=challenge)
    if not handle:
        raise HTTPException(status_code=500, detail="Could not start passkey sign-in")
    return ResponseModel(status_code=200, detail={"handle": handle, "options": options})


@router.post("/passkey/login/complete")
def passkey_login_complete(request: PasskeyLoginCompleteRequest, http_request: Request = None,
                           response: Response = None) -> ResponseModel:
    """Finish a passkey sign-in. This is the ONE login path that is phishing-resistant end to end,
    and the only one that mints a session already stepped up — the user proved a strong factor to
    get here, so asking them to prove it again to paste a cookie would be theatre.
    """
    _main._passkeys_or_503()
    ip = _main._client_ip(http_request)
    user_agent = _main._user_agent(http_request)

    pending = consume_auth_challenge(request.handle, CHALLENGE_LOGIN)
    if not pending:
        raise HTTPException(status_code=400, detail="That sign-in expired — try again")

    stored = _main._verify_assertion_for_user(request.credential, pending["challenge"])
    if not stored:
        record_auth_event(AuthAuditEvent.LOGIN_FAILED, ip=ip, user_agent=user_agent, success=False,
                          details={"method": "passkey"})
        raise HTTPException(status_code=401, detail="That passkey could not be verified")

    user_id = stored["user_id"]
    session_token = create_session(user_id, user_agent=user_agent, ip=ip, verified=True)
    if not session_token:
        raise HTTPException(status_code=500, detail="Could not create session")

    clear_auth_limits(get_user_email(user_id) or "", ip)
    record_auth_event(AuthAuditEvent.LOGIN_SUCCESS, user_id=user_id, ip=ip, user_agent=user_agent,
                      details={"method": "passkey"})
    if response is not None:
        _set_session_cookie(response, session_token)
    return ResponseModel(status_code=200, detail={
        "session_token": session_token,
        "email": get_user_email(user_id),
        "is_new_user": False,
    })


@router.post("/second-factor/verify")
def auth_second_factor_verify(request: SecondFactorVerifyRequest, http_request: Request = None,
                              response: Response = None) -> ResponseModel:
    """Finish a login the email PIN only bootstrapped (design §4, C demoted to bootstrap-only).

    A TOTP code mints a fully verified session. A RECOVERY code mints one that is signed in but NOT
    stepped up: it is meant to get someone back in to enrol a new factor, not to hand a found sheet
    of codes the LinkedIn credentials.

    The handle survives a WRONG code and is burned by the SECOND_FACTOR_MAX_ATTEMPTS-th one. The
    alternative — consume on first touch — reads as safer and is not: one mistyped digit would end
    a login whose only way back is the whole email round trip, and the durable attempt counter is a
    harder bound on guessing than the Redis limiter, which fails open.
    """
    ip = _main._client_ip(http_request)
    user_agent = _main._user_agent(http_request)

    pending = claim_auth_challenge_attempt(request.pending_token, CHALLENGE_SECOND_FACTOR,
                                           SECOND_FACTOR_MAX_ATTEMPTS)
    if not pending or not pending.get("user_id"):
        raise HTTPException(status_code=400, detail="That sign-in expired — start again")
    user_id = pending["user_id"]
    email = get_user_email(user_id) or ""

    verdict = check_auth_verify(email, ip)
    if not verdict.allowed:
        record_auth_event(AuthAuditEvent.LOGIN_RATE_LIMITED, user_id=user_id, email=email, ip=ip,
                          user_agent=user_agent, success=False, details={"scope": verdict.scope})
        raise HTTPException(status_code=429, detail="Too many attempts — try again later",
                            headers={"Retry-After": str(verdict.retry_after_seconds)})

    # A recovery code signs you in; only a real factor makes the session step-up capable.
    verified_session = request.method == METHOD_TOTP
    if request.method == METHOD_TOTP:
        ok = verify_totp_code(user_id, request.code)
    elif request.method == METHOD_RECOVERY:
        ok = verify_recovery_code(user_id, request.code)
    else:
        ok = False

    if not ok:
        attempts_left = max(0, SECOND_FACTOR_MAX_ATTEMPTS - int(pending.get("attempts") or 0))
        record_auth_event(AuthAuditEvent.SECOND_FACTOR_FAILED, user_id=user_id, email=email, ip=ip,
                          user_agent=user_agent, success=False,
                          details={"method": request.method, "attempts_left": attempts_left})
        if not attempts_left:
            # The handle was burned by this attempt. 400, not 401, so the SPA sends the user back
            # to the start instead of leaving them retyping into a login that no longer exists.
            raise HTTPException(status_code=400,
                                detail="Too many incorrect codes — start the sign-in again")
        raise HTTPException(status_code=401, detail="That code did not work")

    # Spend the handle: correct code, so this pending login is over either way. The account's
    # carried-over guessing budget goes with it — a correct code is the proof that clears it, the
    # same way it clears the Redis buckets below.
    finish_auth_challenge(request.pending_token)
    clear_challenge_attempts(user_id, CHALLENGE_SECOND_FACTOR)
    session_token = create_session(user_id, user_agent=user_agent, ip=ip, verified=verified_session,
                                   # A recovery-code session is marked so it may enrol a
                                   # replacement factor without proving one it no longer has.
                                   scope=(SESSION_SCOPE_RECOVERY
                                          if request.method == METHOD_RECOVERY
                                          else SESSION_SCOPE_FULL))
    if not session_token:
        raise HTTPException(status_code=500, detail="Could not create session")

    if request.method == METHOD_RECOVERY:
        record_auth_event(AuthAuditEvent.RECOVERY_CODE_USED, user_id=user_id, email=email, ip=ip,
                          user_agent=user_agent,
                          details={"remaining": count_recovery_codes(user_id)[0]})
    clear_auth_limits(email, ip)
    record_auth_event(AuthAuditEvent.LOGIN_SUCCESS, user_id=user_id, email=email, ip=ip,
                      user_agent=user_agent, details={"method": request.method})
    if response is not None:
        _set_session_cookie(response, session_token)
    return ResponseModel(status_code=200, detail={
        "session_token": session_token,
        "email": email,
        "is_new_user": False,
        # A recovery-code login is signed in but not stepped up — the SPA nudges straight to
        # enrolment rather than letting the user discover it at the first blocked write.
        "enroll_factor_required": request.method == METHOD_RECOVERY,
    })

from cqc_lem.api import main as _main  # noqa: E402  — last; see the module docstring
