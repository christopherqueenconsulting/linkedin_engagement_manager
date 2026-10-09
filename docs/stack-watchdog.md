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
- **Backup freshness** (issue #1090) — the newest `db-*.sql.gz` in `BACKUP_DIR` must be younger
  than `WATCHDOG_BACKUP_AGE_HOURS` (default 48; the cron runs at 03:00, so that is two missed runs
  plus slack) **and** larger than `WATCHDOG_BACKUP_MIN_BYTES` (default 1024). Age alone is not
  evidence: the failure this check exists for wrote a valid, fresh, *empty* 20-byte archive every
  night. No dump, no backups directory, a stale dump, or a husk dump are all `down`.
  The `chrome-profile-*.tar.gz` half **never alerts** — it is reported at WARN only. Cookies live
  encrypted in the database now, `backup.sh` already exits non-zero when it cannot write the
  archive, and a decommissioned chrome-profile volume would otherwise leave the last archive
  permanently stale: an email every 5 minutes that no operator action could clear.
- **Tunnel origin reachability** — "cloudflared is up" and "cloudflared can reach anything" are
  different facts, and only the first was ever checked. cloudflared is a compose service, so the
  service check above already catches the container being gone; this catches the strictly worse
  case: it is running, everything is green, and every request Cloudflare forwards is dropped before
  it arrives. Threshold then grace (`WATCHDOG_TUNNEL_ERROR_THRESHOLD`, default 3, within
  `WATCHDOG_TUNNEL_WINDOW_SECONDS`, default 600, then the same `WATCHDOG_GRACE_SECONDS`) — a deploy
  recreates an origin container and cloudflared logs real errors while it converges, so the first
  burst must not page. The alert **names the failing `originService`**, so it points at the rule to
  fix rather than at "the tunnel".

  **A host-side `curl` is not evidence — do not add one.** The fault this exists for ran fifteen days
  (2026-08-14 → 2026-08-29): the ufw rule fronting the agent-pipeline webhook receiver pinned its
  *source* to a container IP (`ALLOW 172.18.0.1 8420/tcp FROM 172.18.0.4`), cloudflared restarted onto
  `172.18.0.13`, and the rule stopped matching. Every GitHub delivery to `lemhook.*` timed out at the
  origin. The receiver stayed listening, `systemctl is-active` stayed `active`, this watchdog stayed
  silent, and `curl http://172.18.0.1:8420/` **from the host answered 200 the whole time** — the
  dropped packets were the ones arriving from the bridge, the one path the host cannot test. The agent
  pipeline degraded from event-driven to 6-hourly polling with nothing red anywhere.

  It reads cloudflared's **log**, not a probe, because the cloudflared image has no shell
  (`docker exec cloudflared sh` → `executable file not found in $PATH`) — the dial cannot be run from
  the only position that would prove anything. The log is the authoritative record of what it could
  reach, it names the origin, and it covers every ingress rule instead of a hardcoded port list that
  would drift the first time someone adds a hostname. An unreadable Docker or a missing container is
  **WARN and skip** — absence of evidence never becomes an alert.
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
WATCHDOG_ALERT_EMAIL=<the address that should receive outage alerts>
```

It falls back to `COST_ALERT_EMAIL`, then to a hardcoded real address, when unset **or when the
value looks like a placeholder** (`*@example.{com,org,net}`, `changeme`) — a discarded value is
logged at ERROR rather than silently ignored. `SENDGRID_API_KEY`, `SENDGRID_FROM_EMAIL`,
`POSTHOG_API_KEY` and `POSTHOG_HOST` are already present for other features.

Overridable: `LEM_DIR`, `LEM_ENV_FILE`, `WATCHDOG_STATE_DIR`, `WATCHDOG_GRACE_SECONDS`,
`WATCHDOG_HEAL`, `WATCHDOG_BACKUP_AGE_HOURS`, `WATCHDOG_BACKUP_MIN_BYTES`,
`WATCHDOG_TUNNEL_WINDOW_SECONDS`, `WATCHDOG_TUNNEL_ERROR_THRESHOLD`, `WATCHDOG_ALERT_EMAIL`.

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
{"status": "healthy", "workers": 5, "consuming": 5, "maintenance": false,
 "egress": "ok", "egress_checked": 1, "egress_failing": 0}
```

| Field | Meaning |
|---|---|
| `status` | `healthy` / `degraded` / `unknown` — the only field a monitor should assert on |
| `workers` | workers that answered the control channel — **presence, not usefulness** |
| `consuming` | workers subscribed to ≥1 queue. **This is the one that decides `status`.** |
| `maintenance` | `true`/`false`, or `null` when Redis couldn't be read. A declared window holds `status` at `healthy`. |
| `egress` | Can the automation browser's egress proxy reach LinkedIn? `ok` / `unreachable` / `auth_failed` / `refused` / `unknown` / `direct` — see below |
| `egress_checked` | distinct egress proxies probed (counts only) |
| `egress_failing` | how many of them are `unreachable`, `auth_failed` or `refused` |

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
| `degraded` with `consuming` > 0 | Celery is fine but the automation browser's **egress has been failing** for `EGRESS_DEGRADE_AFTER_SECONDS` — see [Egress](#egress-the-browsers-proxy-is-part-of-the-automation-pillar-issue-2346) |
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

### Egress: the browser's proxy is part of the automation pillar (issue #2346)

On 2026-10-09 the egress proxy every Selenium session routes through stopped answering for more than
nine hours, and every LinkedIn lane failed at login and backed off quietly. This endpoint would have
read `healthy` throughout, because it measured only Celery. `utilities/egress_probe.py` is the
missing reading.

- **What it does:** for each distinct egress the active users resolve to (`proxy.resolve_proxy`, the
  same rule the browser uses), send `CONNECT www.linkedin.com:443` and classify the proxy's answer:
  `200` is `ok`, `407` is `auth_failed`, any other status is `refused`, and no connection, a hang-up,
  a timeout or a reply that trickles past the deadline is `unreachable`.
- **What reaches LinkedIn:** on a `200` the proxy has opened a TCP connection to
  www.linkedin.com:443 from the egress IP. The probe closes it before any TLS, so no HTTP request,
  cookie or account is ever sent. That happens at most once per `EGRESS_PROBE_CACHE_SECONDS` per API
  process per distinct proxy. A SOCKS proxy gets a TCP connect to the proxy itself only.
- **`egress`** is the worst outcome across the probed proxies. `direct` means no active user is
  proxied; `unknown` means resolving the configuration raised. **A database outage reads as
  `direct`**, because the user helpers swallow a MySQL error and return no rows; the egress reading
  is not the signal for a database outage.
- **Only the first 10 distinct proxies are probed,** in the order the active users are returned.
  Any beyond the tenth are skipped, cannot affect `egress` or `egress_failing`, and `egress_checked`
  is capped at 10.
- **Degrades `status`** only once the egress has been failing for `EGRESS_DEGRADE_AFTER_SECONDS`
  (default 300). The clock starts at the first failing fresh probe on any API worker and is shared
  through Redis (`health:egress_failing_since`), so several uvicorn workers agree; any worker's `ok`
  probe clears it. Without Redis each worker keeps its own clock. `unknown` and `direct` never
  degrade, and egress never upgrades an `unknown` or `degraded` reading.
  `HEALTH_DEEP_EGRESS_DEGRADES=false` keeps the fields but stops them changing `status`.
- **It pages only through the body.** `degraded` is returned with HTTP 200, like every reading
  here, so it reaches a person only if the layer 3 monitor asserts on the `"status":"healthy"`
  keyword, as that section describes. Probes run only when the endpoint is called, so with the
  defaults and a 5-minute monitor as the only caller, the first check after the proxy dies starts
  the clock and a later one reads `degraded`: the first `degraded` check comes about 5 to 15
  minutes after the proxy dies (the second check can land just under 300 s, which pushes it to the
  third). Under layer 3's suggested rule of alerting after 2 consecutive failures, the alert reaches
  a person about 10 to 20 minutes after the proxy dies. This change does not configure or verify the
  monitor.
- **Bounded, because this endpoint is unauthenticated:** readings are cached in-process for
  `EGRESS_PROBE_CACHE_SECONDS` (default 60), at most 10 proxies are probed, in parallel, each bounded
  by `EGRESS_PROBE_TIMEOUT_SECONDS` (default 5) for the connect and again for the reply (an
  `https://` proxy adds a TLS handshake under the same socket timeout). Requests that arrive during
  a probe wait on the same lock rather than starting their own, so a dead proxy costs a request up
  to about 15 seconds in the worst case.
- **Counts only**, like the rest of the body: no host, port or credential ever appears.
- **Why one reading, not per user:** the body is public, and a per-user map would publish which
  users exist and which of them are failing. A degraded reading says "some active user's egress is
  down"; `egress_failing` says how many proxies, and the app log's per-session `Selenium session …
  egress=<host:port>` line says which.

#### Responding to an egress reading

| `egress` | What it means | Check |
|---|---|---|
| `unreachable` | no TCP connection to the proxy, or it hung up or stalled | `nc -vz <host> <port>` from the host; the provider's status page and dashboard |
| `auth_failed` | the proxy answered 407: the credentials (or its IP allowlist) refused us | the proxy credentials in `users.proxy_url` / `REGION_PROXIES` / `PROXY_URL`, and the provider's allowlist |
| `refused` | the proxy answered but would not open the tunnel | the provider's target restrictions and plan status |

The body names no proxy, so find the failing one in the app log
(`/opt/lem/logs/cqc_lem_YYYY_MM_DD.log`, one file per UTC day; `docs/production-logs.md`). Every
Selenium session logs the egress it used, as host:port and never with credentials:

```
grep -o "egress=[^ ]*" /opt/lem/logs/cqc_lem_$(date -u +%Y_%m_%d).log | sort | uniq -c
```

`PROXY_URL` and `REGION_PROXIES` are set in `/opt/lem/.env`, and per-user overrides are in
`users.proxy_url` (`docs/PER_USER_PROXY.md`). Those values carry credentials: read them on the host
and never paste them into a ticket or chat.

The egress for each user is resolved by `utilities/proxy.py:resolve_proxy`: the user's own
`users.proxy_url`, else `REGION_PROXIES` for their country, else `REGION_PROXIES["DEFAULT"]`, else
`PROXY_URL`. Recovery is
`egress: ok` here, then the next scheduled LinkedIn session logs `Already logged in!`; no restart
is needed. The owner of the response is whoever receives the layer 3 monitor's alert.

#### Risk and rollback

- **Load anyone can cause:** at most one probe round per `EGRESS_PROBE_CACHE_SECONDS` per API
  process, of at most 10 `CONNECT`s with a 5 s timeout each, in parallel. Concurrent requests during
  a probe wait on the same lock rather than starting their own.
- **Who acts on `degraded`:** only the external uptime monitor (layer 3), which alerts. Nothing
  restarts or rolls back on it: deploys gate on `/health`, never `/health/deep`, and the host
  watchdog (layer 1) reads `docker compose ps`, not this endpoint.
- **Rollback:** `HEALTH_DEEP_EGRESS_DEGRADES=false` in `/opt/lem/.env` keeps the `egress` fields
  but stops them changing `status`. The API reads `.env` only when its container is created, so
  saving the file does nothing on its own. To apply it now, re-deploy the tag that is already
  running, as the deploy user (the same command CI runs over SSH):
  `cd /opt/lem && ./scripts/deploy.sh "$(cat .last_good_tag)"`. That is a full deploy: it
  also runs Flyway (a no-op when no migration is pending) and drains the workers through the
  maintenance window. Otherwise it takes effect on the next release deploy. Reverting the PR
  removes the fields; a monitor asserting on `"status":"healthy"` is unaffected either way.
- **To stop a false page before any deploy:** pause the layer 3 monitor in its own dashboard, and
  unpause it once the switch is applied or the proxy is fixed.

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

**Assert on that keyword and nothing else.** The literal is the only part of the body under
contract — the *field set* is not, and #1020 changed it by dropping `lanes`. A monitor configured
against the whole response, a byte length, or any field other than `status` therefore breaks on a
change that is by design safe, and it breaks in the direction that looks like a passing check. If
you inherit a monitor whose configuration you can't recall, open it and confirm it is keyword
`"status":"healthy"` / Keyword Type **"does not exist"** before the next release that touches this
endpoint deploys — verification is cheap, and a silently disarmed dead-man's switch is the one
failure this layer cannot report about itself.

Alert when the response is non-200 **or** the body's `status` is not `healthy`. Most monitors
support a keyword/JSON assertion — use it, because a `degraded` body still returns **200** by
design. A monitor checking only the HTTP status would have missed this outage exactly as `/health`
did.

Suggested: 5-minute interval, alert after 2 consecutive failures (≈10 min, matching layer 1's
grace window so the two don't disagree).
