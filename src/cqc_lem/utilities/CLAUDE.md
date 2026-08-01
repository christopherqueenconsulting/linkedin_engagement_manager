# `cqc_lem/utilities` — logging & observability conventions

Scoped context for this tree. The root `CLAUDE.md` keeps the one-line rule; the detail lives here so
it loads when you are actually editing these modules. Full runtime posture:
`docs/error-tracking.md`, `docs/llm-analytics.md`.

## Logging

Never use `print()`. Use the structured logger from `cqc_lem.utilities.logger`. Prefer the typed
helpers over the legacy `myprint()` shim:

| Function | Level | When to use |
|---|---|---|
| `log_debug(msg, **ctx)` | DEBUG | Verbose detail: LLM calls, Selenium steps, DB queries. **Also: expected no-ops** |
| `log_info(msg, **ctx)` | INFO | Normal task progress and real state transitions |
| `log_warning(msg, exc=None, **ctx)` | WARNING | Recoverable failures, fallbacks, degraded paths. **Escalates on repeat** |
| `log_error(msg, exc=None, **ctx)` | ERROR | Task-level failures — sent to PostHog |
| `log_critical(msg, exc=None, **ctx)` | CRITICAL | Fatal conditions — sent to PostHog |
| `myprint(msg, debug=False)` | INFO/DEBUG | Legacy shim — still works, avoid in new code |

Pass structured context as keyword args. Supported fields: `user_id`, `task_id`, `task_name`,
`post_id`, `action_type`, `duration_ms`, `ai_model`, `api_provider`, `http_status`. `log_error` /
`log_critical` accept `exc=` for the full exception + stack trace.

```python
from cqc_lem.utilities.logger import log_info, log_warning, log_error

log_info("Scheduled post", post_id=post_id, user_id=user_id, task_name="auto_check_scheduled_posts")
log_warning("Perplexity unavailable, falling back to GoogleNews", exc=e, api_provider="perplexity")
log_error("Automation task failed", exc=e, user_id=user_id, task_name="automate_commenting")
```

Env: `LOG_LEVEL` (overall, default `INFO`) and `POSTHOG_LOG_LEVEL` (minimum forwarded to PostHog,
default `ERROR`; **prod runs `WARNING`** — `DEBUG` put 2,185 info rows into PostHog Logs in 48h and
buried the 172 real warnings).

## Recurrence escalation — the rule that changes how you pick a level

`log_escalation.py`. **Once is a warning, repeatedly is a defect.** A repeated `log_warning` is
re-emitted at ERROR and captured as ONE grouped `$exception`, which the daily error→issue cron turns
into a GitHub issue. Defaults: 3 occurrences in 24h.

Two things follow for anyone writing a call site here:

1. **Do not warn on an expected no-op.** It will file a defect for working behaviour. The pattern to
   copy is `run_automation.react_to_post_inline`: it used to return `False` both for "already
   reacted" (benign) and for genuine failure, so the caller logged a working skip as
   `Could not leave a reaction on post`. It now returns `None` for the no-op — still falsy, so
   truthiness callers are unaffected — and only real failures warn.
   Same idea for empty-result chatter: `db.get_ready_to_post_posts` logs INFO only when the list is
   non-empty, DEBUG otherwise. For Selenium, `find_first(..., required=False, warn_on_miss=False)`
   logs the miss at DEBUG — use it when the element is legitimately absent on some surfaces
   (`_switch_feed_to_recent`'s 'Sort by' control does not exist on a group feed). `click_first`
   carries the same flag across BOTH of its miss paths — `Selector miss:` (never found) and
   `Click miss:` (found, then un-clickable), which return None identically. Decide it PER SURFACE,
   not once for the call site: the same lookup on the
   home feed, where the control does exist, is selector rot and must still warn. The other half of
   the test is whether a FALLBACK is in hand — `react_to_post_inline` warns on a missing
   'Open reactions menu' only when it found no React toggle to default-Like instead (issue #873).
2. **Keep the message a stable template.** The dedup key masks volatile tokens (URLs, emails, UUIDs,
   URNs, hex, `[...]`, quoted strings, numbers) and combines them with the call site, so
   `Selector miss: Feed sort control` and `Selector miss: Reaction state` stay two distinct problems
   while `... request 41` and `... request 42` collapse into one. Interpolating something genuinely
   unbounded that the masks don't catch means the key never repeats and the fault never escalates.

Escape hatch for a warning that is genuinely expected and high-volume: add its prefix to
`LOG_ESCALATE_EXCLUDE` (and say why in the PR).

## Other invariants in this tree

- **All DB access goes through `db.py`.** No raw SQL anywhere else in the codebase.
- **`routing_policy.py` must stay stdlib-only** — docker-compose mounts that same file into the
  LiteLLM container, so a `cqc_lem.*` import breaks the proxy.
- **`observability.py` is the ONE place events are emitted** (`track_llm_call` / `track_task` /
  `track_api_call` / `capture_exception`). `capture_exception` takes an explicit `fingerprint=` for
  grouping overrides — `$`-prefixed property keys aren't valid Python identifiers, so it cannot be
  passed through `**context`.
- **`human_pacing.py` is the ONE cadence engine**; `linkedin/rate_limit.py` owns the 429 breaker and
  the shared Redis handle (`shared_redis_client()`) that other runtime-state helpers reuse.
