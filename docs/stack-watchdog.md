# Stack watchdog & deep health

Three layers, each covering a blind spot the others have. They exist because of the **v0.118.0
outage**: the worker converge aborted mid-deploy and left `celery_beat` plus every Celery worker in
`Created`. The API stayed green for four hours while the entire automation pillar was dead.

## Why the existing checks did not fire

This is the part worth internalising — **healthchecks were already defined on all seven app
services and were useless here**:

| Mechanism | Why it missed |
|---|---|
| Docker `healthcheck:` | A container in `Created` **has never run**, so its healthcheck never executes. Healthchecks only report on *running* containers. |
| `restart:` policy | Acts on containers that ran and then exited. It will not start a container that was never started. (It is also unset on every celery service.) |
| `GET /health` | Returns a static `{"status": "healthy"}` literal. It never touched Celery, so it was honestly reporting a genuinely healthy API. |
| A Celery beat task | `celery_beat` was itself among the dead. **A watchdog inside the thing it watches cannot report its own outage.** |

The gap was: *"the container exists but was never started, and nothing outside it is looking."*

## Layer 1 — host watchdog (`scripts/stack_watchdog.sh`)

A systemd timer, every 5 minutes, **outside the container set**. Reads `docker compose ps -a`,
compares against `docker compose config --services`, and flags anything not `running`.

- **Grace window** (`WATCHDOG_GRACE_SECONDS`, default 600s) — deploys legitimately recreate the
  worker tier, and a converge plus image pull runs for minutes. Alerting under that window would
  page on every release and train everyone to ignore it.
- **One bounded self-heal** (`WATCHDOG_HEAL=1`) — `docker compose start` once per incident for a
  `Created`/`Exited` container, then alert regardless of outcome. Bounded on purpose: a container
  that needs starting twice is not a blip. It uses `start`, never `up -d`, so it cannot fight a
  deploy that is mid-converge.
- **Replica-aware** — `selenium-node-chrome` runs 8 containers under one service name. The *worst*
  state in the pool is what gets recorded, so seven dead nodes cannot hide behind one healthy one.
- **Alerts on both channels**, talking to PostHog and SendGrid **directly over HTTPS**. It depends
  on nothing in the stack, so it still alerts when every container on the box is down.
  - PostHog: `stack_watchdog_report` (`down`, `healed`, `recovered`, `down_count`)
  - Email: only for a real outage or a heal. A pure recovery is worth an event, not an inbox.
- **Silent when healthy.** A watchdog that chats every 5 minutes gets filtered, and then it is not
  a watchdog.

Blind spot: it cannot report the VPS itself being down. That is layer 3.

### Install

```sh
sudo cp /opt/lem/scripts/systemd/lem-watchdog.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now lem-watchdog.timer
systemctl list-timers lem-watchdog.timer      # confirm it is scheduled
sudo systemctl start lem-watchdog.service     # run once now
journalctl -u lem-watchdog.service -n 50      # read the result
```

Requires `jq` and `curl` on the host.

### Configure

Add to `/opt/lem/.env` (or set in the unit):

```
WATCHDOG_ALERT_EMAIL=you@example.com
```

It falls back to `COST_ALERT_EMAIL` when unset. `SENDGRID_API_KEY`, `SENDGRID_FROM_EMAIL`,
`POSTHOG_API_KEY` and `POSTHOG_HOST` are already present for other features.

Overridable: `LEM_DIR`, `LEM_ENV_FILE`, `WATCHDOG_STATE_DIR`, `WATCHDOG_GRACE_SECONDS`,
`WATCHDOG_HEAL`, `WATCHDOG_ALERT_EMAIL`.

### Exit codes

`0` = healthy, or a self-heal that worked. `1` = something is still down. The unit sets
`SuccessExitStatus=0 1` so a true "still down" report is not also a systemd unit failure — otherwise
the next timer tick is all systemd would talk about.

## Layer 2 — `GET /health/deep`

`/health` stays trivial: it gates the blue/green flip, so it must never depend on Redis, MySQL or
Celery being reachable. A test pins that.

`/health/deep` answers what a monitor actually wants to know — it reaches every worker over the
broker's control channel (`_inspect().active_queues()`), so a lane whose container was never started
simply is not in the reply.

```json
{"status": "healthy", "workers": 5, "consuming": 5, "maintenance": false}
```

| Field | Meaning |
|---|---|
| `status` | `healthy` / `degraded` / `unknown` — the only field a monitor should assert on |
| `workers` | workers that answered the control channel — **presence, not usefulness** |
| `consuming` | workers subscribed to ≥1 queue. **This is the one that decides `status`.** |
| `maintenance` | `true`/`false`, or `null` when Redis couldn't be read. A declared window holds `status` at `healthy`. |

### Counts only — the endpoint names nothing (issue #1020)

The body used to carry a `lanes` map of every worker to its queues. This endpoint is
**unauthenticated by design** — an external dead-man's switch cannot hold a credential — so that
map published container IDs and the internal queue topology to anyone who asked. It was the sole
field with disclosure value and it is gone; the counts derived from it stay, because a bare integer
names nothing and `consuming` is what *decides* `status` (drop it and a `degraded` reading becomes
unexplainable). `lanes` is still computed internally, so `workers` and `consuming` are unchanged.

Go on the box for the detail the map used to give: `stack_watchdog.sh` reads the same state through
`docker ps`, and `celery inspect active_queues` gives it verbatim.

| `status` | Meaning |
|---|---|
| `healthy` | at least one worker is **consuming a queue** — or a maintenance window is **declared** |
| `degraded` | broker reachable, **nothing consuming and no declared window** — either no workers (the v0.118.0 shape) or workers registered but idle |
| `unknown` | control channel unreachable. **Unmeasured is never `healthy`.** |

### Why `consuming`, not `workers`

A worker being *present* is not the same as a worker *working*. `maint begin` cancels every queue
consumer, so a stuck maintenance mode leaves the whole tier registered, answering, and doing
nothing. That state was observed live during the v0.120.0 deploy and the endpoint called it
`healthy` — `workers: 5`, `consuming: 0`.

Registration was never the question a monitor is asking. No consumer means no task will run, which
is the same outage as no worker at all — so it reports `degraded`, and `maintenance` says whether
that is the expected cause.

**One live consumer is enough.** A deploy recreates lanes one at a time, and failing the whole
check on a single idle lane would flap the monitor through every rollout — which is how an alert
gets muted, and a muted alert is worse than no alert.

### A declared maintenance window is not an outage

That tolerance does not cover the drain, though: `maint begin` cancels **every** lane's consumer at
once, and `scripts/deploy.sh` runs it on every release — four windows a day. Reporting `degraded`
there would fire the monitor on every *successful* deploy, which is the same muting problem from the
other end. So while the maintenance flag is set, `status` stays `healthy`, with `consuming: 0` and
`maintenance: true` still in the body for anyone reading it.

The suppression is bounded by the flag's **own TTL** — `deploy.sh` sets 1800s (`MAINT_PAUSE_SECONDS`)
and `maint end` deletes it — so the failure mode this endpoint exists for is still caught: a deploy
that dies between `begin` and `end` leaves the consumers cancelled, the flag expires within the
window, and the reading goes `degraded`. Unreadable Redis (`maintenance: null`) never suppresses —
a window we cannot confirm is not a window. This mirrors layer 1's `WATCHDOG_GRACE_SECONDS`: both
layers refuse to alert on a state a deploy is expected to pass through, and both bound how long
they will stay quiet.

It never raises and never 503s on a partial: a monitor should read `status`, and a scrape that
cannot tell must say so rather than give a confident wrong answer.

## Layer 3 — external dead-man's switch (owner setup)

Layers 1 and 2 both run *on the box*. Neither can report the VPS being down, the tunnel being
broken, or the host being unreachable. That needs something off-box.

Point an external monitor (healthchecks.io, UptimeRobot, Better Stack — any of them) at:

```
https://lem.christopherqueenconsulting.com/health/deep
```

Assert on the literal string `"status":"healthy"` in the body. On UptimeRobot that is monitor type
**HTTP(s) — Keyword**, Keyword Type **"does not exist"**, keyword `"status":"healthy"` — one rule
covering `degraded`, `unknown` and any non-200.

> ⚠️ **That literal is a monitor contract.** Renaming the field or the value silently disarms
> every configured monitor, and a monitor that can no longer match looks exactly like a monitor
> that is passing. `test_healthy_keyword_is_stable_for_body_assertions` pins it; if you ever need
> to change it, re-configure the monitors in the same change.

Alert when the response is non-200 **or** the body's `status` is not `healthy`. Most monitors
support a keyword/JSON assertion — use it, because a `degraded` body still returns **200** by
design. A monitor checking only the HTTP status would have missed this outage exactly as `/health`
did.

Suggested: 5-minute interval, alert after 2 consecutive failures (≈10 min, matching layer 1's
grace window so the two don't disagree).
