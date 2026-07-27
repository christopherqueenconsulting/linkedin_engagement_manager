# PostHog advanced surface — CDP destinations, Workflows, Logs, Scouts/Inbox (issue #655)

Milestone 15 (#646–655) adopted PostHog's core products one at a time — LLM analytics, error
tracking, session replay, feature flags, surveys, KPI dashboards. This is a scoped SPIKE on four
newer/adjacent surfaces, each evaluated against what LEM already has rather than adopted on
principle. Scope was deliberately "docs + minimal config, no heavy build" — see the Acceptance in
the issue.

## TL;DR

| Surface | Call | One line |
|---|---|---|
| CDP realtime destinations | **Adopt (scoped)** | Shipped one, inert until a webhook/Slack URL is supplied — see below |
| Workflows | **Skip** | Keep the existing `onboarding.py`/`notifications.py` nudge machinery; Workflows needs a new email channel (DNS) and lives outside git review |
| Logs (OTLP ingestion) | **Already adopted** | `logger.py` has shipped ERROR+ records to PostHog Logs over OTLP since issue #647/#648 — nothing to migrate |
| Scouts / Self-driving Inbox | **Skip** (code-fixing) / **Watch** (observability-gap audits) | Overlaps LEM's own error→issue→agent pipeline but is capped at 3 runs/month free and blind to `CLAUDE.md`/`RUNBOOK.md` |

## 1. CDP realtime destinations

### What they add over what LEM already has

Every ops signal LEM ships today is either a daily rollup or a cron:

- The four `scripts/posthog_provision.py` threshold alerts (issue #650) — `calculation_interval:
  daily`, emails the key owner once a day's evaluation breaches.
- `scripts/posthog_error_issues.py` (issue #648) — a daily host cron that turns a new/active PostHog
  error-tracking issue into a GitHub issue.

CDP destinations are PostHog's realtime layer: a HogFunction fires within seconds of the triggering
event being captured, not at the next scheduled evaluation. That gap matters most for exactly the
signal where the daily cadence already contributed to the original incident — the LinkedIn 429
breaker (`utilities/linkedin/rate_limit.py`) escalates its OWN cooldown, so minutes of delay while
the doom loop re-forms costs real automation time (see `feed-commenting-429-doom-loop` in prior
runbook history). A same-minute ping is worth materially more than a same-day one there; it is not
obviously worth more for, say, a weekly comment-floor breach.

### What's live in this PostHog project today

Verified directly against the project (475262, "CQC LEM") before writing any code: **zero** CDP
destinations, zero third-party integrations (`slack`, `github`, or any other `kind`) are configured.
Every destination channel PostHog offers for a realtime ping — Slack, Discord, Microsoft Teams,
Linear, GitHub, GitLab, or a generic webhook — needs either an OAuth integration (can't be completed
headless, from an agent pipeline tick) or a bare `https://` endpoint to point at. Neither exists yet.

### What shipped

`scripts/posthog_ops_destination.py` (+ `tests/unit/test_posthog_ops_destination.py`), in the exact
dry-run/`--apply` + pure-plan/thin-client split every other `posthog_*.py` script in this repo uses:

```bash
scripts/posthog_ops_destination.py --print-payload   # the HogFunction body, no network
scripts/posthog_ops_destination.py                   # dry run (exit 2 = a change is pending)
scripts/posthog_ops_destination.py --apply           # create/update it
```

It provisions ONE `destination` (`template-webhook`) filtered on `rate_limit_trip` — LEM's own "the
429 breaker just tripped" event, already emitted by `mark_rate_limited()` (issue #650). The
destination URL comes from `POSTHOG_OPS_WEBHOOK_URL`, read only at provision time, never hardcoded.
(Deliberately not `internal_destination`: that type is reserved for PostHog's own internal-only
signals like `$insight_alert_firing`/`$activity_log_entry_created`, which never share a pipeline
with an ordinary captured event like `rate_limit_trip` — verified against the PostHog API docs and
MCP before writing this script.)

Rather than fabricate a target or paste in a credential the pipeline shouldn't be handling
unsupervised, the script is **inert by default**: with no `POSTHOG_OPS_WEBHOOK_URL` set, both
`--dry-run` and `--apply` report exactly what's missing and exit `0` — the same "degrade to a
no-op, never fail the run" shape `scripts/posthog_annotate.py` already uses for a missing
`POSTHOG_PERSONAL_API_KEY`. Going live is one step for a human: add a Slack
[incoming-webhook URL](https://api.slack.com/messaging/webhooks) (free at any volume LEM would
generate) or any other `https://` endpoint as `POSTHOG_OPS_WEBHOOK_URL` in `/opt/lem/.env`, then run
`--apply` once.

### Cost

CDP destinations share the "Data pipelines" free tier: **10K events/month**. LEM's own alert ceiling
for this exact event (`RATE_LIMIT_TRIPS_PER_DAY_CEILING = 5` in `posthog_provision.py`) puts a worst
case around 150/month — over an order of magnitude under the cap. No new always-on spend.

### Call

**Adopt the pattern**; the one destination proved out is deliberately left unapplied pending the
one piece of information only a human can supply (which channel to ping). This is not a "needs a
product decision" park — the code merges and does nothing until the env var exists, same as several
already-merged `posthog_*.py` scripts.

## 2. Workflows — evaluated against a "posts awaiting approval" nudge

Workflows graduated alpha → beta → GA within 2026 and is PostHog's no-code trigger/delay/branch/
dispatch builder (event, webhook, or scheduled-audience triggers; email/Slack/SMS/webhook
dispatches), built and edited entirely in the PostHog UI.

**Setup cost.** Any email dispatch needs a Workflows "channel" first — four DNS records on the
sending domain, verified before a single email can go out. LEM doesn't send transactional email
through PostHog anywhere today; `utilities/email.py` (SendGrid, with SMTP fallback) already owns
every one of the ~10 existing notification types, verified and in production, with one shared
branded template layer (`_footer_html`/`_footer_text`, `_html_to_text`). Standing up a second,
parallel email pipeline for one reminder is exactly the kind of "no heavy build" this spike is
supposed to avoid.

**What LEM already has for this shape of problem.** `utilities/onboarding.py` (issue #500) is a
near-exact prior art: `select_nudge()` is a PURE function over a checklist + timestamps (which nudge,
if any, fires next — one-shot per key, `NUDGE_COOLDOWN_HOURS` between any two nudges), unit-tested
without touching the DB, and `notifications.py`'s `notify_onboarding_nudge` sends it through the one
email pipeline. A "posts awaiting approval" reminder is a small, natural extension of that exact
shape — one more rule and one more `send_*_email` template — not a new product. (No such issue exists
yet; searched for `awaiting approval` / `pending_approval` / `posts_pending` across the codebase and
found only the raw `PostStatus.PENDING` enum value — this would be new scope for whoever files it.)

A Workflow, by contrast, lives outside git: its branching logic isn't something `pytest tests/unit`
covers, and CLAUDE.md's ≥80% patch-coverage bar has no meaning against a no-code canvas edited
in-browser.

### Call

**Skip** for this use case. Build the eventual "posts awaiting approval" nudge as a sibling of the
existing onboarding-nudge machinery, not a Workflow.

**Watch:** reconsider if LEM ever wants a non-engineer editing outreach cadence without a PR — a
genuinely different value proposition than what a single-operator, code-reviewed project needs today.

## 3. Logs — already adopted

`utilities/logger.py`'s `_build_posthog_handler` has shipped every `ERROR`+ log record (threshold
`POSTHOG_LOG_LEVEL`, default `ERROR`) to **PostHog Logs** over OTLP HTTP
(`{POSTHOG_HOST}/i/v1/logs`) since issue #647/#648 — before this spike, not as a result of it. Each
record carries a proper OTel `Resource` (`service.name`, `service.instance.id` from `$HOSTNAME`,
`service.version` from `IMAGE_TAG`, `deployment.environment`), so every worker (`web_app`,
`celery_worker`, `*_selenium*`, `*_content`) is distinguishable in the Logs UI.

There is no separate "Logs OTel ingestion" surface to evaluate migrating TO — the OTLP exporter
already wired into `logger.py` *is* PostHog Logs' own ingestion path. The spike's question
("replacement for the logger.py PostHog handler") had a stale premise: the replacement already
happened, two milestones ago.

**Cost:** the free tier is 50 GB ingested/month. LEM only forwards `ERROR`/`CRITICAL` — a small slice
of what the local `RotatingFileHandler` (250 MB × 10 backups, every level) captures — nowhere near
that cap at current volume.

### Call

**Already adopted.** No action. One thing worth flagging as a genuine, deliberate future trade-off
rather than a migration: dropping `POSTHOG_LOG_LEVEL` to `WARNING` or `INFO` for a season would trade
free-tier headroom for full-text log search across the fleet instead of `ssh` + `grep` — an owner
call, not a code change.

## 4. Scouts / Self-driving Inbox — overlap with LEM's own agent pipeline

"Self-driving" (open beta) is PostHog's own autonomous loop: **Scouts** are skills that read PostHog
data and emit **signals** (canonical categories include Health checks, Ingestion warnings, Insight
alerts, Logs, Observability gaps, MCP tool calls); once a human marks a signal Actionable, an
**Inbox** implementation agent clones the repo into a sandbox, branches, edits, runs local tests, and
opens a labeled PR — "Nothing ships without you," the human still reviews and merges.

### The overlap is direct

LEM already runs this exact shape, end to end, and has for several milestones: `scripts/
posthog_error_issues.py` turns a PostHog error-tracking issue into an `agent:ready` + `bug` GitHub
issue; the agent pipeline this very PR was produced by (`RUNBOOK.md`, `tick.sh`) picks it up, branches,
edits, tests, and opens a PR gated by CI + review before merge. Self-driving Inbox would be a second,
PostHog-authored version of the same loop, running in parallel with ours on the same repo.

### What Self-driving Inbox cannot do that LEM's own pipeline already does

- **Follow `RUNBOOK.md`'s contract** — the `MODE=start` Why/Scope/Files/Acceptance shape, the
  `agent:ready`/`agent:working`/`needs-human` label state machine, the Decision Comment protocol for
  handing an ambiguous call back to the owner. An Inbox PR has no access to this repo's house rules.
- **Honor `CLAUDE.md` conventions** — the structured logger, `db.py`-only SQL, `PostStatus`/`PostType`
  enums, LiteLLM tier aliases, `get_docker_driver()`. Nothing outside this repo's own agent pipeline
  reads that file before writing code.
- **Merge through LEM's own gate** — required CI checks, a Copilot review on `risk:*` PRs, an
  adversarial self-review marker before the runner merges. Inbox PRs are reviewed and merged directly
  by the owner in GitHub, bypassing that gate unless manually re-routed.
- **Scale to LEM's actual volume** — Self-driving is metered separately, at **3 scout runs/month on
  the free tier**. LEM ships multiple releases a day (`docs/zero-downtime-deploys.md`, four-times-daily
  cadence) and this very issue is one of dozens the pipeline works through in a comparable window.
  3/month is roughly two orders of magnitude under what the existing pipeline already does.

### Where it would add something LEM doesn't have

The "Observability gaps" and "Insight alerts" scout categories are a genuinely different job: a
periodic self-audit for "this high-volume event has no insight/dashboard/alert" or "this alert fired
and nobody looked." `scripts/posthog_provision.py` defines the KPI dashboards as code, but nothing
today re-checks whether a *later* `track_*()` call (a future issue's new event) ever got a tile. That
is complementary, not redundant, with the error→issue→agent loop.

### Call

**Skip** wiring Inbox into the code-fixing loop — direct redundancy, a two-orders-of-magnitude volume
mismatch, and blind to this repo's conventions and merge gate. **Watch** the observability-gap/
insight-alert scout categories as an occasional manual audit (a few minutes, run after a big
instrumentation push or once a quarter) — never as a standing automation that could open PRs outside
`RUNBOOK.md`'s process. Revisit the whole call if Self-driving's free-tier cap changes materially, or
if it ships a way to hand a scout this repo's own `CLAUDE.md`/`RUNBOOK.md` so its output could route
through OUR gate instead of around it.

## What shipped in this PR

- `scripts/posthog_ops_destination.py` + `tests/unit/test_posthog_ops_destination.py` — the CDP
  destination described in §1, inert until `POSTHOG_OPS_WEBHOOK_URL` is set.
- This document.

## Open follow-up (not a blocker)

Set `POSTHOG_OPS_WEBHOOK_URL` in `/opt/lem/.env` to a Slack incoming-webhook URL (or any other
`https://` endpoint) and run `scripts/posthog_ops_destination.py --apply` to make the realtime
429-breaker ping live.
