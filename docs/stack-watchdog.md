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
{"status": "healthy", "workers": 5, "lanes": {"celery@selenium": ["se_engage"]}}
```

| `status` | Meaning |
|---|---|
| `healthy` | at least one worker is consuming |
| `degraded` | broker reachable, **nobody consuming** — the exact v0.118.0 shape |
| `unknown` | control channel unreachable. **Unmeasured is never `healthy`.** |

It never raises and never 503s on a partial: a monitor should read `status`, and a scrape that
cannot tell must say so rather than give a confident wrong answer.

## Layer 3 — external dead-man's switch (owner setup)

Layers 1 and 2 both run *on the box*. Neither can report the VPS being down, the tunnel being
broken, or the host being unreachable. That needs something off-box.

Point an external monitor (healthchecks.io, UptimeRobot, Better Stack — any of them) at:

```
https://lem.christopherqueenconsulting.com/health/deep
```

Alert when the response is non-200 **or** the body's `status` is not `healthy`. Most monitors
support a keyword/JSON assertion — use it, because a `degraded` body still returns **200** by
design. A monitor checking only the HTTP status would have missed this outage exactly as `/health`
did.

Suggested: 5-minute interval, alert after 2 consecutive failures (≈10 min, matching layer 1's
grace window so the two don't disagree).
