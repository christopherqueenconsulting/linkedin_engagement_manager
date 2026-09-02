# Automation cooldown & pause (LinkedIn 429 recovery)

LinkedIn rate-limits by egress IP. When the account's residential proxy gets 429'd, every Selenium
task that navigates LinkedIn re-confirms the throttle. Two mechanisms keep this from becoming a
self-sustaining doom loop and let a throttled account recover.

## The breaker is a harder gate than pacing, and it is never a flag

`utilities/human_pacing.py` and `utilities/linkedin/rate_limit.py` both slow LinkedIn traffic down,
and they are not interchangeable:

| | Human pacing (#626) | The 429 breaker |
|---|---|---|
| What it does when it fires | **Delays** an action — the action still happens | **Blocks** the LinkedIn navigation for the whole cooldown; the caller skips |
| Who can turn it off | `HUMAN_PACING_ENABLED` | **Nobody.** It is not behind a feature flag and never will be |
| Tunable per user | yes, that is the point | no — it is an account-safety control |

**Safety controls are NOT feature flags** (`utilities/flags.py` says so explicitly, alongside the
automation pause and the per-day caps). Never wrap the breaker in one: a flag fails open to its env
var by design, and a safety control that fails open on an unresolvable flag lookup is not a safety
control. If the breaker needs to be lifted, that is `clear_rate_limit()` after a successful login,
or the operator kill-switch below — both of which are observable actions, not a config read.

Note that both mechanisms **no-op when Redis is unavailable** — the breaker's state lives in Redis
and an unavailable Redis returns no handle. That is a deliberate fail-open (an outage of our own
infrastructure must not become a permanent halt), and it is the one condition under which "the
breaker is the harder gate" stops being true. It is also why "Redis was down once" is never cached:
the handle is retried on the next call.

## 1. Adaptive circuit-breaker escalation (automatic)

`utilities/linkedin/rate_limit.py` tracks **consecutive** 429 trips (a Redis counter cleared only by a
successful login). Each trip doubles the breaker cooldown — base → 2× → 4× … up to a cap — so we probe
LinkedIn less and less often instead of re-tripping every 30 min.

- `LINKEDIN_RATE_LIMIT_COOLDOWN_SECONDS` — base cooldown (default 1800 = 30 min).
- `LINKEDIN_RATE_LIMIT_MAX_COOLDOWN_SECONDS` — escalation cap (default 21600 = 6 h).
- A successful login calls `clear_rate_limit()` which resets both the breaker and the trip counter.
- A **login checkpoint the ladder cannot clear** trips the breaker too (`LinkedInChallengeUnsolved`,
  rate-limit-class): re-submitting a live checkpoint page is how a temporary challenge hardens into a
  durable restriction, so the account backs off exactly as it does for a 429.

## 2. Manual global pause (operator kill-switch)

Halts ALL Selenium automation (feed commenting, replies, DMs, stats, invites) for a window so the IP
can recover. **Posting is API-driven and NOT paused.** Enforced centrally in `login_to_linkedin`
(every Selenium task logs in through it) and short-circuited early by the high-volume beat dispatchers
(`_skip_if_paused` in `run_scheduler.py`). Redis-backed with a TTL; fails open (no Redis → not paused).

Admin API (requires the `X-Admin-Secret` header = `ADMIN_SECRET`):

```
POST /api/admin/automation-pause?hours=24   # pause for N hours (default 24)
POST /api/admin/automation-resume           # lift immediately
GET  /api/admin/automation-status           # { paused, pause_remaining_s, breaker_remaining_s }
```

Helpers: `pause_automation(seconds, reason)`, `resume_automation()`, `is_automation_paused()`,
`automation_pause_remaining()` in `utilities/linkedin/rate_limit.py`.

## 3. Recovering a throttled session: clear profile-wide, then verify

The login path (`utilities/linkedin/helper.py`) answers a 429 at `/feed` that arrives **with**
stored cookies by dropping them and re-authenticating, because a cookie minted from a different
egress IP genuinely does 429 against the proxy. Two things make that safe, and both were learned
the hard way — their absence produced a retry loop that ran ~32×/hour for over a day and was
itself the cause of the throttle it kept re-confirming.

**The clear must be profile-wide.** `WebDriver.delete_all_cookies()` is scoped to the ACTIVE
DOCUMENT's origin, and by the time we call it the browser is parked on a Chrome error page, which
has no origin. `get_cookies()` returns `[]` and the delete silently does nothing — it looks like
it worked. `hard_clear_cookies()` uses CDP `Network.clearBrowserCookies`, which is profile-wide and
does not care what is on screen, and falls back to the scoped call only for drivers without CDP.

Why it matters that the cookie actually goes: once a throttled session cookie is set, LinkedIn
bounces **every** path on the domain, not just the one you asked for. Measured live: `/feed`,
`/login`, `/uas/login` and `/robots.txt` all returned `ERR_TOO_MANY_REDIRECTS`, while a fresh
browser session through the same proxy loaded `/login` normally. So the redirect-loop check is not
gated on a logged-in-looking URL — the loop is served everywhere.

**The retry must be verified, not assumed.** `_recover_to_login()` checks that the login page
actually rendered and trips the breaker when it did not. Dropping cookies is only the right answer
when the cookies were the problem; when LinkedIn is throttling the ACCOUNT the same error page
comes back with a perfectly valid `li_at`, and re-logging in every minute both deepens the throttle
and discards good credentials on every pass. A 429 that survives the clear is an account throttle,
and §1's escalating cooldown is the only thing that lets it decay. A transport error is exempt —
that is a proxy blip, transient, and tripping the shared breaker on it would pause every lane for
nothing.

## When to use

If the account is 429'd for an extended period (login fails even when the breaker briefly clears),
`POST /api/admin/automation-pause?hours=24` to stop probing entirely, let LinkedIn's throttle relax,
then `automation-resume`. Pair with reducing overall automation cadence (e.g. reply-check
`event` mode, fewer scheduled sweeps).
