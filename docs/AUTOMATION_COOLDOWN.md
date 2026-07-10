# Automation cooldown & pause (LinkedIn 429 recovery)

LinkedIn rate-limits by egress IP. When the account's residential proxy gets 429'd, every Selenium
task that navigates LinkedIn re-confirms the throttle. Two mechanisms keep this from becoming a
self-sustaining doom loop and let a throttled account recover.

## 1. Adaptive circuit-breaker escalation (automatic)

`utilities/linkedin/rate_limit.py` tracks **consecutive** 429 trips (a Redis counter cleared only by a
successful login). Each trip doubles the breaker cooldown — base → 2× → 4× … up to a cap — so we probe
LinkedIn less and less often instead of re-tripping every 30 min.

- `LINKEDIN_RATE_LIMIT_COOLDOWN_SECONDS` — base cooldown (default 1800 = 30 min).
- `LINKEDIN_RATE_LIMIT_MAX_COOLDOWN_SECONDS` — escalation cap (default 21600 = 6 h).
- A successful login calls `clear_rate_limit()` which resets both the breaker and the trip counter.

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

## When to use

If the account is 429'd for an extended period (login fails even when the breaker briefly clears),
`POST /api/admin/automation-pause?hours=24` to stop probing entirely, let LinkedIn's throttle relax,
then `automation-resume`. Pair with reducing overall automation cadence (e.g. reply-check
`event` mode, fewer scheduled sweeps).
