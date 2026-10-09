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

**Host scripts must never call `clear_rate_limit()`** (#2092). A host cron (`recovery-probe/probe.sh`)
cleared the breaker every day before its own API call, which then 401'd, so it never retired itself —
63 silent clears of a safety control. The function now takes a **required `reason`** (no reason →
`TypeError`) and logs every clear at INFO with `reason=`, `caller=` (module + function) and
`keys_cleared=`, so a clear from anywhere other than `login_to_linkedin` stands out in the log. The
only callers are inside `src/`; an operator who genuinely needs to lift it by hand does so once,
with a reason, from inside a container (`docs/DEPLOYMENT.md`) — never on a schedule.

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
- A successful login calls `clear_rate_limit(reason=...)` which resets both the breaker and the trip counter.
- A **login checkpoint the ladder cannot clear** raises `LinkedInChallengeUnsolved` (rate-limit-class,
  so every caller defers and charges no attempt) and records this ACCOUNT's own challenge cooldown
  (`mark_challenge_unsolvable`, #1920): re-submitting a live checkpoint page is how a temporary
  challenge hardens into a durable restriction. It does NOT trip this IP-wide breaker — one account's
  checkpoint must not pause every other user — except when the account cannot be resolved, where
  there is no key to scope a cooldown to.

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

## 4. A browser that never reaches LinkedIn is not a rate limit (#2320, #2346)

When the egress route fails (the proxy stops answering, DNS, the host network), the base-page
navigation lands on a Chrome error page, the stored cookies are refused for the wrong origin, and
`login_to_linkedin` raises `LinkedInEgressUnreachable`. That is a `LinkedInRateLimited` subclass on
purpose — every caller defers exactly as it does for a throttle, nothing is charged, and **this
breaker is never tripped**, because LinkedIn was never contacted. The login gate in
`linkedin/session.py` logs it as `LinkedIn login deferred — the browser could not reach linkedin.com
through its egress route`, not as a rate limit.

`log_escalation` never escalates the `LinkedInRateLimited` family, so on its own a dead proxy stays
at WARNING forever (2026-10-09: every Selenium lane, more than nine hours, nothing past WARNING). The
**egress streak** is the separate signal for that:

- Per user, in Redis (`linkedin:egress_streak:<user_id>`): when the current run of egress failures
  began and how many sessions it has cost. Any successful login clears it.
- Once the streak is both `LINKEDIN_EGRESS_ESCALATE_MIN_FAILURES` sessions long (default 3) AND
  older than `LINKEDIN_EGRESS_ESCALATE_AFTER_SECONDS` (default 7200 = 2 h), `login_to_linkedin`
  (`_note_egress_failure` in `linkedin/helper.py`) escalates once. Both conditions, not either: the
  session floor alone would page on a burst of retries in a few minutes (a deploy, a proxy blip),
  and the window alone would page on two sessions hours apart. So under the defaults the earliest
  escalation is 2 h after the first failed session.
- The escalation is two things:
  - one ERROR log line naming the likely causes in order (egress proxy, LinkedIn, host network) and
    the egress `host:port` the browser is routed through, never credentials;
  - one `LinkedInEgressDown` captured to error tracking, fingerprinted per user and per streak, so
    **each outage opens a new issue**. The exception text carries no host, because it becomes an
    issue title.
- **Who is told:** the code guarantees the ERROR line and a new error-tracking issue. The owner is
  emailed only if PostHog's "Issue created or reopened" alert is on (`docs/error-tracking.md` §
  Alerts); that alert is PostHog configuration, not code, and this change does not verify it. The
  daily error→issue cron (`scripts/posthog_error_issues.py`) also files a GitHub issue; it copies
  the exception's name and text, which carry no host.
- **Fan-out:** the streak is per user, so one shared proxy failing for N users opens N issues.
- It escalates once per streak (an `HSETNX` claim, so two workers cannot both fire). A successful
  login clears the streak; for a streak that had escalated it also logs `LinkedIn browser egress
  recovered after N failed sessions` at INFO. A later outage is a new streak and escalates again.
- Fails open: with Redis down nothing is counted and nothing escalates.
- Known gap: a user with NO stored cookies never reaches the wrong-origin branch, so an outage is
  counted only through users who have a stored session.

### Responding to a `LinkedInEgressDown` escalation

The app log is `/opt/lem/logs/cqc_lem_YYYY_MM_DD.log`, one file per UTC day
(`docs/production-logs.md`).

1. **Find the egress.** The ERROR line names it as `egress=<label>`:

   ```
   grep 'LinkedIn browser egress has not reached linkedin.com' /opt/lem/logs/cqc_lem_$(date -u +%Y_%m_%d).log
   ```

   It is resolved by `utilities/proxy.py:resolve_proxy`, first match wins: the user's own
   `users.proxy_url` (written by `update_user_proxy` in `platform/db/repositories/users.py`), else
   `REGION_PROXIES` for their country, else `REGION_PROXIES["DEFAULT"]`, else `PROXY_URL`, else
   direct egress (`docs/PER_USER_PROXY.md`). `REGION_PROXIES` and `PROXY_URL` are set in
   `/opt/lem/.env`; they carry credentials, so never paste them into a ticket or chat.
2. **Check it from the host,** without credentials. What to check depends on the label:

   | `egress=` | Meaning | Check |
   |---|---|---|
   | `<host>:<port>` or `<host>` | a proxy | `nc -vz <host> <port>` (a timeout is the proxy, a refusal is the port), then the provider's dashboard and status page |
   | `DIRECT` | no proxy resolved for this user | the host network and DNS from the Selenium worker; there is no proxy to test |
   | `invalid` | the resolved proxy URL has no parseable host | fix the value in `users.proxy_url`, `REGION_PROXIES` or `PROXY_URL` |
   | `unknown` | resolving the user's egress raised an unexpected error (a MySQL error is not one: it reads as no override and falls through the order above) | the lookup logs nothing when it fails, so take the user's egress from their last session line, `grep 'egress=' /opt/lem/logs/cqc_lem_$(date -u +%Y_%m_%d).log \| grep 'user_id=<user_id>' \| tail -1`, and use the row for that label |

   `/health/deep`'s `egress` field, where deployed, gives the proxy's answer from inside the API
   container.
3. **Fix or replace the proxy** at the provider. Do not point the account at a different IP
   without deciding to: LinkedIn treats a new sign-in location as a risk signal.
4. **Confirm recovery:** the next scheduled session logs `Already logged in!` (or `Login
   successful!`) and the recovery line, and the streak key is gone:

   ```
   grep 'LinkedIn browser egress recovered after' /opt/lem/logs/cqc_lem_$(date -u +%Y_%m_%d).log
   docker exec redis redis-cli EXISTS linkedin:egress_streak:<user_id>   # 0 = cleared
   ```

   The key lives in the Redis that `_resolve_redis_url` in `linkedin/rate_limit.py` picks
   (`CELERY_BROKER_URL`, else `CELERY_RESULT_BACKEND`, else `redis://redis:6379/0`); on the compose
   stack that is the `redis` container. No restart is needed; the lanes resume on their next beat.
5. **Close the issues.** Resolve the `LinkedInEgressDown` error-tracking issue once recovered. If
   it was still active when the daily error-to-issue cron ran (`docs/error-tracking.md` § error →
   GitHub issues), the cron also filed a GitHub issue for it; close that too, since the fix was
   at the proxy, not in code. Resolving the PostHog issue before the cron runs means nothing is
   filed.

**Rollback:** set `LINKEDIN_EGRESS_ESCALATE_AFTER_SECONDS` very high (e.g. `31536000`) in
`/opt/lem/.env` to silence the escalation without a code change; the clearer WARNING wording stays.
The Celery workers read `.env` only when their containers are created, so saving the file does
nothing on its own. To apply it now, re-deploy the tag that is already running, as the deploy user
(the same command CI runs over SSH):

```
cd /opt/lem && ./scripts/deploy.sh "$(cat .last_good_tag)"
```

That recreates the workers with the new value. It is a full deploy: it also runs Flyway (a no-op
when no migration is pending) and drains the workers through the maintenance window first.
Otherwise the value takes effect on the next release deploy. A revert removes both the escalation
and the clearer WARNING wording.

Before you run it, check in GitHub Actions that no **Build & Deploy Release** or **Redeploy /
Rollback VPS** run is in progress: `deploy.sh` takes no lock, so two deploys can overlap. If it
exits non-zero, its last `ERROR` line says which case you are in:

- `did not become healthy` (or `does not exist`), then `restoring standby ... and aborting`: the
  serving API colour was never touched and the site stays up. The new value is not applied; fix
  the cause and re-run.
- `edge health failed after flip — flipping back`: the edge is routed back to the previous colour
  and the site keeps serving. The new value is not applied; fix the cause and re-run.
- `stack left partially deployed` or `stack verification failed`: the web tier is live but some
  workers did not come up. Re-run the same command, or see `docs/zero-downtime-deploys.md`
  § Worker-tier resilience.

To undo the switch, delete the line from `/opt/lem/.env` and run the same command again.

## When to use

If the account is 429'd for an extended period (login fails even when the breaker briefly clears),
`POST /api/admin/automation-pause?hours=24` to stop probing entirely, let LinkedIn's throttle relax,
then `automation-resume`. Pair with reducing overall automation cadence (e.g. reply-check
`event` mode, fewer scheduled sweeps).
