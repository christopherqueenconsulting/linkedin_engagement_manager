# LinkedIn session health — sign-in visibility + OAuth token renewal

Two independent credentials keep LEM working against LinkedIn, and each has exactly one module
deciding its state. The map entries live in [CLAUDE.md](../CLAUDE.md) under **Anti-bot / session
infra**; this file is the detail behind them.

| Credential | Owner module | What it powers |
|---|---|---|
| The Selenium session (`li_at` cookie / password login) | `utilities/linkedin/login_status.py` (#933) | every browser automation: feed commenting, DMs, invites |
| The LinkedIn OAuth token | `utilities/linkedin/token_refresh.py` (#600) | the REST API paths: posting, post stats, profile reads |

They fail differently and are reported separately — a user can hold a healthy OAuth token while
the browser session is dead, and vice versa.

---

## Sign-in visibility (issue #933)

When LinkedIn challenges an automated sign-in it asks the account owner to confirm the device from
the LinkedIn mobile app, and LEM emails them to go and tap **Yes**. The approval happens entirely on
LinkedIn's side, so a user who had already approved could not tell whether LEM ever saw it: the app
said "a session is saved" before the approval and exactly the same thing after.

`login_status.py` records the outcome of the sign-in the approval belongs to, so
`GET /user/linkedin-signin-status` → `LinkedInSignInStatusCard.tsx` can answer *"did my approval
land?"* instead of leaving the user guessing.

### Where it is written

`_persist_session_cookies` is where **both** of `login_to_linkedin`'s success paths meet, so that is
where a sign-in is recorded (`mark_signed_in`). The device-approval wait loop always closes the
`approval_pending` state — `mark_approval_timed_out` on giving up, `mark_signed_in` the moment it
clears. A login that dies between the two must never leave the SPA telling a user who already tapped
Yes to go and tap it again.

### The three states

| State | Meaning |
|---|---|
| `signed_in` | Signed in — any approval that was asked for landed |
| `approval_pending` | LinkedIn asked, we emailed, we are waiting |
| `approval_timed_out` | We stopped waiting; the next run asks again |

`mark_approval_pending` carries the previous `signed_in_at` forward, because *"you approved on the
2nd, we're asking again now"* is a very different message from *"we have never signed in"*.
`approval_cleared_at` is stamped only when the sign-in followed a pending approval — that is the
exact fact the user could not otherwise see. The cookie persist writes again for the SAME sign-in
the approval just cleared, so the approval is carried across rather than erased, bounded by the
pending window so a routine sign-in weeks later never re-claims an old approval.

### Storage and failure mode

State lives in **Redis**, next to the 429 breaker and reusing its handle: short-lived runtime state
that survives a deploy and needs no migration.

- `LINKEDIN_LOGIN_STATUS_TTL_SECONDS` (default 30 days) — a "last signed in" fact stays useful for
  weeks.
- `LINKEDIN_LOGIN_STATUS_PENDING_TTL_SECONDS` (default 15 min) — a PENDING record expires on its
  own, so a worker that died mid-challenge cannot leave the Account page asking for an approval
  nobody is waiting on.

It **fails open**: with Redis unavailable every write no-ops and `get_login_status` returns `None`.
`unknown` therefore means *nothing was recorded*, NOT *the connection is broken* — the SPA must not
render it as a failure. A Redis blip is an expected no-op and logs at DEBUG, never a warning that
would file an issue on repeat.

---

## OAuth token renewal (issue #600)

`resolve_token_status(user_id, auto_refresh=True)` is the ONE place a user's token state is decided.
Both readers use it — the SPA's `/user/token_status` countdown and the daily renewal beat — so the
countdown a user sees and the countdown that triggers their email can never disagree. The returned
state is the state **after** any refresh attempt it made.

LinkedIn caps authorization at **60 days**. The daily beat `refresh-linkedin-tokens`
(`auto_refresh_linkedin_tokens`, 08:30 UTC) renewing everyone inside `EXPIRY_WARNING_DAYS` (30, half
the token's life) is the only way a token outlives that cap. It runs BEFORE the 09:00 missing-session
pass, so a token renewed at 08:30 never produces a reconnect email half an hour later.

### The returned document

`connected`, `token_expiry_date`, `days_remaining`, `is_expiring_soon`, `is_expired`,
`can_auto_refresh`, `refresh_attempted`, `refresh_succeeded`.

**`days_remaining` is `None`, never 0, when it cannot be read.** A zero would render as "expires
today" and send a user chasing a problem that may not exist; `None` renders as unknown.

### Emails

A user with **no refresh token** cannot be renewed — that is an expected no-op (DEBUG, not a
warning; it is the normal state for anyone who connected before refresh tokens were requested). It
produces a **reconnect email**, throttled per user by `LINKEDIN_TOKEN_EMAIL_THROTTLE_DAYS` (7) in
Redis, which fails open. Paired with the 30-day warning window that caps a user at roughly four
emails across the final month before expiry.
