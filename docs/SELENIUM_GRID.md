# Selenium Grid — the horizontal browser path (Phase 2)

**Status:** built, NOT enabled in prod · **Issue:** #556 · **Plan:** `docs/scaling-plan.md` §5b/§5c

Prod runs **one `selenium/standalone-chrome`** with 8 session slots. §4 of the scaling plan puts the
top of this box's Chrome budget at ~8 sessions, so 8 is not a step on a ladder — it is the ceiling.
The next capacity increase has to be **horizontal**: a Grid hub with N single-session nodes, where
nodes can live on a second box.

Nothing in the app changes. `get_docker_driver()` already talks to
`${SELENIUM_HUB_HOST}:${SELENIUM_HUB_PORT}/wd/hub`, which is the hub's address exactly as it was the
standalone's — the seam was always there.

---

## 1. What ships here

| File | What it is |
|---|---|
| `docker-compose.grid.yml` | Overlay: hub + N nodes on the primary box; parks the standalone behind a `standalone` profile; repoints every service that waited on it. |
| `docker-compose.grid-node.yml` | A nodes-only compose project for a **second VPS**. |
| `src/cqc_lem/utilities/selenium_load_test.py` | The concurrency/scale load test (§3 below). |
| `tests/unit/app/test_selenium_capacity.py` | Extended: the cap == Σ-lanes invariant is enforced for the Grid too (node count **is** the cap). |

### The invariant travels with the topology

`SE_NODE_MAX_SESSIONS == the sum of every lane worker's SELENIUM_CONCURRENCY` (§5a) becomes
`SELENIUM_GRID_NODES × 1 == the same sum`, because each `selenium/node-chrome` runs exactly one
session. Below it, lanes block on session creation and time-sensitive tasks miss their window; above
it, nodes are paid for and idle. The default is **8 nodes**, matching today's
`se_engage 3 + se_prepost 2 + se_outreach 2 + se_content 1` — so the cutover is capacity-neutral by
design, and the change of capacity is a separate, deliberate decision.

---

## 2. Running it

> **Status: the Grid is the DEPLOYED topology as of 2026-07-27.** `scripts/deploy.sh` composes
> `docker-compose.grid.yml` in on every deploy (`SELENIUM_TOPOLOGY`, default `grid`); set
> `SELENIUM_TOPOLOGY=standalone` in `/opt/lem/.env` to fall back. Before this, the overlay existed
> but no deploy used it — a manual `up` with the overlay was reverted by the very next release.
>
> **Two cutover failure modes worth knowing (both hit live on 2026-07-27):**
> 1. A compose *profile* stops a service from being **started**, not from **running**. The standalone
>    kept holding `127.0.0.1:4444`, so the hub could not bind. `deploy.sh` now evicts a running
>    `selenium-chrome` before bringing the grid up.
> 2. When that bind fails, Docker leaves the hub container **running with no network attached**. It
>    presents as *"hub unhealthy, 0 nodes"* and the node logs say
>    `UnknownHostException: selenium-hub` — not as a port error. The fix is `docker rm -f
>    selenium-hub` and recreate; check `docker inspect selenium-hub --format '{{.NetworkSettings.Networks}}'`
>    if nodes ever fail to register.

### Primary box (hub + local nodes)

```sh
docker compose -f docker-compose.yml -f docker-compose.prod.yml -f docker-compose.grid.yml up -d
```

`SELENIUM_GRID_NODES` in `/opt/lem/.env` sets the node count (default 8). **Raising it requires
raising the lane concurrencies in the same commit** — the unit test fails the build otherwise.

Node count is declared as `deploy.replicas`, which **Compose v2 applies on a plain `up`** (Compose v1
ignored the whole `deploy:` key outside Swarm — as it would also ignore the resource limits the base
compose file already depends on, so v2 is a prerequisite of this stack, not just of this overlay).
Getting it wrong is silent — you get *fewer browsers*, not an error — so **verify the slot count
below before running an engagement cycle on it**, and if a host ever disagrees, say it explicitly:

```sh
docker compose -f docker-compose.yml -f docker-compose.prod.yml -f docker-compose.grid.yml \
  up -d --scale selenium-node-chrome="${SELENIUM_GRID_NODES:-8}"
```

Rollback is one flag, because the overlay parks the standalone rather than deleting it:

```sh
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d   # back to the standalone
```

### Second box (nodes only)

On the second VPS, in a directory holding `docker-compose.grid-node.yml` and a `.env`:

```sh
SELENIUM_GRID_HUB_HOST=10.10.0.1     # PRIVATE address of the primary box
SELENIUM_GRID_NODE_HOST=10.10.0.2    # PRIVATE address of THIS box, as the hub must reach it
```

```sh
docker compose -f docker-compose.grid-node.yml up -d
```

On the primary box, set `SELENIUM_GRID_BUS_BIND` to its **private** address so the event bus is
reachable from the second box — never `0.0.0.0`. **Registering a node into a Grid requires no
credentials**, so an internet-reachable event bus (4442/4443) is an open door: put the two boxes on
WireGuard or a private VPC network and firewall those ports to the peer only.

The second box's nodes use `network_mode: host` with one fixed `SE_NODE_PORT` each, because the hub
calls each node back on `SE_NODE_HOST:SE_NODE_PORT` and replicas would all advertise the same port.
Add capacity by copying a node block with the next port.

**With a second box, the invariant spans both boxes:** summed lane concurrency must equal
primary nodes + second-box nodes. The unit test can only see the primary's default, so this is a
documented, deliberate two-file change.

### Verify

```sh
# node count AND slot count — both must equal SELENIUM_GRID_NODES (one session per node)
curl -s localhost:4444/status | jq '.value.nodes | length, [.[].slots[]] | length'
docker compose -f docker-compose.yml -f docker-compose.prod.yml -f docker-compose.grid.yml \
  ps selenium-node-chrome | tail -n +2 | wc -l
docker exec celery_worker_selenium python -m cqc_lem.utilities.capacity_alerts
```

Fewer slots than lanes is the one failure this cutover can produce quietly: the stack comes up, the
hub is healthy, and time-sensitive tasks simply start queueing on session creation. Check it here,
not from the logs.

### When the hub is unreachable — `SELENIUM_READY_TIMEOUT`

Every driver goes through `_wait_for_selenium_ready`, which polls `/wd/hub/status` every 2s until
the hub reports ready and otherwise raises `TimeoutError` at **`SELENIUM_READY_TIMEOUT`** (default
`60`, the value that used to be hard-coded).

Read that as a **per-call-site** cost, not a per-run one. `get_driver_wait_pair` retries only
`SessionNotCreatedException` — deliberately, since anything else is not a capacity problem — so this
`TimeoutError` propagates on the first attempt, and each of the ~25 call sites that wants a browser
pays the full wait for as long as the hub is down. During a hub restart that is the whole Selenium
lane sitting in `time.sleep(2)`.

Lower it on a box where restarting the hub is routine; the cost of a low value is only that a hub
which is slow to come up is declared unready, and the task retries on its next beat. Issue #1339
tracks the two larger fixes: distinguishing "hub unreachable" from a generic warning, and a
short-circuit so siblings fail fast once one task has observed the hub down.

The capacity monitor (`auto_capacity_watch`, §5e) reads the same `/status` endpoint, with one
Grid-only adjustment: it counts the POOL's slots and drops the debug node's
(`capacity_alerts._pool_slots`, matched on `SELENIUM_DEBUG_NODE_HOST`). Saturation has to be
measured against the cap the lanes can consume — a 9-slot denominator makes a fully-claimed 8-slot
pool read 0.89 and the sample below it 0.78, under the 0.85 threshold that used to breach at 7/8.

### The watchable node (noVNC)

The pool is unwatchable — noVNC lives on the nodes and replicas cannot all publish the same host
port — so the overlay runs **`selenium-node-debug`**: a ninth, non-replicated node that publishes
7900 (`SELENIUM_GRID_VNC_BIND`, loopback by default) and is what the `lemvnc` hostname points at.
It is deliberately ON TOP of the eight the lanes are sized for, so watching a browser never costs a
production slot. Two caveats:

- Grid routes a session to **any** free matching node, so this is *a spare node you can watch*, not
  the node your session lands on. Deterministic pinning needs a distinguishing stereotype plus a
  matching capability request from the client — not done here.
- The Grid UI's live-view link is built from `SE_NODE_GRID_URL`; set `SELENIUM_GRID_PUBLIC_URL` to
  the tunnel hostname or that link points at a host only Docker can resolve.

`tools/selenium_mcp_server.py` is unaffected — it drives 4444, which is still the hub.

Both 4444 and the event bus bind to **loopback** by default (`SELENIUM_GRID_HUB_BIND`,
`SELENIUM_GRID_BUS_BIND`), matching how `docker-compose.prod.yml` hardens the standalone today.

#### Pinning a session to the debug node

The debug node advertises a custom capability, `lem:debug`, via `SE_NODE_STEREOTYPE_EXTRA`.
Clients that want to **watch the exact session** can request that capability:

* **Live-validation probe:** `python -c "..." < scripts/linkedin_live_validation.py --watch ...`
* **Selenium MCP server:** `SELENIUM_DEBUG_NODE=true poetry run python tools/selenium_mcp_server.py`
* **Ad-hoc code using `get_docker_driver()`:** `SELENIUM_DEBUG_NODE=true` is read automatically
  when the caller does not pass an explicit `debug=` argument.

When the debug node is busy or absent, the helper silently falls back to the normal pool —
a debugging convenience must never block production work. An unflagged session never requests
`lem:debug`, so normal automation stays on the 8 production nodes.

To watch the pinned session end-to-end:

1. Start the session with one of the opt-ins above.
2. Open noVNC on the debug node: `http://localhost:7900/?autoconnect=1&password=secret`
   (or the tunnel hostname if working remotely).
3. The session you care about is the one rendered there.

---

## 3. The load test

`python -m cqc_lem.utilities.selenium_load_test` answers the question Phase 2 gates the cohort on:
**at N users, what fraction of each user's engagement work starts inside its window, and what would
it cost to serve the demand?**

```sh
# the curve, on today's deployed topology — each staggerable fan-out uses its OWN shipped window
# (golden hour 180 min, appreciation DMs 120 min, per #554), not a single crontab minute
python -m cqc_lem.utilities.selenium_load_test --users 10,50,100

# the pre-#554 "before" baseline, for comparison (everyone at one crontab minute)
python -m cqc_lem.utilities.selenium_load_test --users 10,50,100 --stagger-hours 0

# what-if: a wider/narrower UNIFORM stagger window, a 16-node Grid, Phase-2 lane concurrencies
python -m cqc_lem.utilities.selenium_load_test --users 50 --nodes 16 --stagger-hours 4 \
  --lanes se_engage=7,se_prepost=4,se_outreach=3,se_content=2

# validate a DEPLOYED Grid with real browsers (run inside a container that can reach the hub)
docker exec celery_worker_selenium python -m cqc_lem.utilities.selenium_load_test \
  --users 10 --live --live-sessions 8 --hold-seconds 60 --live-user-id 1
```

Exit code **2** when the largest simulated scale exceeds one VPS, so a cron/CI caller can gate a
cohort onboarding without parsing the table. `--json` emits the whole thing machine-readably.

### Two modes, both needed

* **simulate** (default) — a discrete-event model of the real topology: per-lane Celery slots plus a
  global session cap, fed §4's per-user daily workload and §3's actual fan-out times. A task holds
  its lane slot *while* it waits for a browser, which is why raising a lane without raising the cap
  does nothing. It runs in a second, in CI, and is the only way to get a number for 100 users
  without first buying the box that could host them.
* **live** (`--live`) — opens N REAL concurrent sessions against the hub and measures acquisition
  wait, busy slots and host headroom. This validates the model on the topology actually deployed.
  It deliberately does **not** write to the capacity monitor's rolling window: synthetic waits there
  would file a capacity issue about a load test.

### Reading the output

* **"Sessions needed"** is the smallest per-lane concurrency (and therefore cap) that starts 95% of
  the day's work inside its window — *not* the instantaneous peak. Arrivals are spiky by
  construction (§3's single crontabs), so the raw peak would ask for a browser per user, while the
  tolerances say a golden-hour loop has the golden window to start in. Lanes are solved
  independently, which is exact rather than approximate: with the invariant held, no lane can be
  blocked by another lane's browser.
* **Tolerances are the product requirement**, and they differ by an order of magnitude: pre-post
  commenting has 5 minutes (its whole job is to run *before* the post), a golden-hour loop has the
  2-hour window §4's `C ≥ N ÷ (W×4)` formula assumes, and §3's off-peak batch (appreciation DMs,
  stats scrape) has 4 hours because nothing breaks if they slide.
* **Simulated session wait is 0 whenever the invariant holds** — work queues on its *lane*, not on a
  browser. A non-zero p95 in simulation means the cap is under-provisioned; for a real acquisition
  wait, use `--live`.

---

## 4. Measured curve

### Pre-#554 baseline (2026-07-26, today's topology: 8 slots, lanes 3/2/2/1)

`--stagger-hours 0` reproduces the ORIGINAL single-crontab-minute behaviour, exactly — every user
lands on their fan-out's anchor minute instead of a slot inside a window. (#696 moved the
appreciation-DM anchor an hour earlier; this table is unchanged by that, because with no window the
batch drains long before the post band opens either way. It is the control for what the WINDOW
costs, not a historical replay of the 08:00 UTC crontab.)

| Users | On-time % | Late jobs | Delay p95 | Sessions needed | Chrome mem | Host CPU | Verdict |
|---|---|---|---|---|---|---|---|
| 10 | **100%** | 0 | 60 min | 5 (engage 2, prepost 1, outreach 1, content 1) | 6.0 GB | 6.5 vCPU (81%) | fits |
| 50 | **57.7%** | 148 | 320 min | 14 (engage 6, prepost 4, outreach 2, content 2) | 16.8 GB | 15.5 vCPU (194%) | at ceiling |
| 100 | **17.7%** | 576 | 640 min | 27 (engage 12, prepost 7, outreach 4, content 4) | 32.4 GB | 28.5 vCPU (356%) | exceeds one VPS |

### Post-#554 — the shipped stagger, re-measured (issue #634, 2026-07-27)

The harness previously modelled a `--stagger-hours 4` uniform what-if as a *prediction to be
checked* against #554's actual shipped shape: a hashed per-user slot inside a window anchored **in
each user's own timezone** (golden hour 180 min, appreciation DMs 120 min — different widths per
fan-out, not one uniform override), quantized to the beat's real 15-minute tick. #634 taught the
model that shape; this is the DEFAULT run now (no flags). It measured **53.7% / 15 sessions at
50 users** — flat-to-*worse* than the pre-#554 baseline, not the predicted 84.0% / 11 — and traced
it to the shared `se_outreach` lane. #696 fixed that; the current numbers are the next section.

### Post-#696 — the se_outreach fix, re-measured (2026-07-27, DEFAULT run, no flags)

| Users | On-time % | Late jobs | Delay p95 | Sessions needed | Chrome mem | Host CPU | Verdict |
|---|---|---|---|---|---|---|---|
| 10 | **100%** | 0 | 60 min | 5 (engage 2, prepost 1, outreach 1, content 1) | 6.0 GB | 6.5 vCPU (81%) | fits |
| 50 | **64.3%** | 125 | 320 min | 14 (engage 6, prepost 4, outreach **2**, content 2) | 16.8 GB | 15.5 vCPU (194%) | at ceiling |
| 100 | **21.6%** | 549 | 640 min | 27 (engage 12, prepost 7, outreach **4**, content 4) | 32.4 GB | 28.5 vCPU (356%) | exceeds one VPS |

Per-lane on-time at the worst scale (100 users): engage 20.3%, prepost 2.0%, outreach 31.5%,
content 25.0%.

**The fix was one hour, not a lane.** `se_outreach` carries both the staggered `appreciation_dms`
and the post-anchored `profile_viewer_engagement`. Opening a window at the hour the old crontab
fired does not leave the batch where it was: it moves the batch's midpoint half a window later and
its **tail a full window later**, and that tail is what reached `profile_viewer_engagement`'s
arrivals. Spreading the DM burst never shrank the processing time it needs (workload ÷ concurrency
is fixed) — it only moved it. So `APPRECIATION_DM_ANCHOR_HOUR` moved `08:00 → 07:00`, which puts the
120-minute window's **midpoint** back on the 08:00 off-peak hour PR #607 approved and gives the
batch back the drain time the single-instant version had. `APPRECIATION_DM_MIDPOINT_HOUR` in
`utilities/engagement_window.py` pins anchor + window/2 = 08:00 so the two can't drift apart again —
a unit test holds the code defaults to it, and `stagger_config` logs one WARNING per process when
the RESOLVED env pair drifts off it. **Deploy note:** this is a DEFAULT change, and `.env.example`
lists `APPRECIATION_DM_ANCHOR_HOUR` explicitly, so a deployment whose own `.env` still says `8` keeps
the pre-#696 behaviour until that value is changed to `7`. That warning is how you find out.

Of the three options #696 listed, this was the only one that cost nothing: raising `se_outreach`
concurrency buys a Chrome slot on a box already at its ceiling, and giving `appreciation_dms` its own
lane (the #553 shape) needs **4** slots across the two lanes at 50 users where the shared lane needs
2 — a DM backlog can't delay profile-viewer engagement, but you pay for a browser that idles all
afternoon. Five things fall out of the re-run:

1. **Today's 8 slots are right for ~10 users and nothing more**, before or after #554/#696. On-time
   is in the 60s% at 50 users and the 20s% at 100 — the §2 prediction ("tasks miss their window, the
   box does not fall over"), quantified. Staggering helps; it does not fix this.
2. **The uniform `--stagger-hours 4` what-if overstated the win** because it isn't what shipped: it
   spread EVERY staggerable fan-out (including `reply_sweep` and `content_tasks`, which #554 never
   touched) across one uniform 4-hour window, and used a simpler even-index spread instead of the
   real per-user hash + 15-minute tick quantization. Modelling the ACTUAL shape (different windows
   per fan-out, only the three #554 actually staggers) is what changed the answer, and it is why the
   real number is 64.3%, not 84.0%.
3. **`se_outreach` is now no worse staggered than unstaggered at any scale** — 10 through 200 users,
   pinned per-scale by
   `tests/unit/utilities/test_selenium_load_test.py::TestRequiredTopology::test_the_shipped_dm_window_costs_se_outreach_nothing_against_its_own_baseline`.
   At 50 users the lane needs **2** sessions, matching the pre-#554 baseline (#696's acceptance
   criterion); the whole-fleet total drops 15 → **14** and on-time rises 53.7% → **64.3%**, so the
   stagger finally pays what the plan banked on instead of being absorbed by a lane-mate.
4. **The generalizable rule:** a fan-out that shares a lane with a tighter-tolerance job must open
   its window `window/2` EARLY, not on the hour the batch used to fire. Widening a window without
   pulling its anchor back re-creates this exact regression, silently — the arrival times move, the
   send hour users experience moves, and nothing fails.
5. **`se_prepost` is still the lane §4's back-of-envelope never modelled**, and staggering never
   touches it (posts are per-user ETAs, not a fan-out). At 100 users it needs 7 slots on its own,
   because a 15-minute warm-up with a 5-minute tolerance cannot absorb posts arriving every
   2.4 minutes. It remains the first lane to break and the least forgiving, before or after #696.

> The harness is more complete than §4's back-of-envelope, which modelled only the commenting loops
> and put 50 users at 8–10 sessions. Adding the once-a-day batch fan-outs (appreciation DMs, stats
> scrape) and the `se_prepost` lane added by #553 is what takes 50 users to 14–15. Same box, same
> assumptions (1.2 GB + 1 vCPU per session, §4) — more of the actual workload.

---

## 5. The decision point: 16 vCPU / 64 GB, a second box — or someone else's grid

**Status: decided — self-managed, and the buy is trigger-driven, not scheduled (#633 2026-07-27;
execution closed on #974, 2026-08-07).** The "someone else's grid" third option in this section's
title is **closed**: LEM stays self-managed and no hosted vendor is pursued. Nothing is bought until
the capacity monitor (§5e in `docs/scaling-plan.md`) says the cap is the operating point — that
trigger is **shipped and running**, not a plan: `auto_capacity_watch` (`app/run_scheduler.py`) is on
the beat and `utilities/capacity_alerts.py` files its own GitHub issue on a `session_saturation` or
`lane_backlog` breach. §6 below is the checklist that breach hands you. So there is no standing
hardware task — the absence of a filed breach IS the answer.

#633 widened the option set and priced hosted/cloud grids (AWS Fargate/EC2 nodes, Device Farm,
BrowserStack, Sauce Labs, LambdaTest, TestingBot, Browserless, Browserbase, Steel) against the two
self-managed baselines below — full comparison: `docs/scaling-cost-options.md`. As suspected, most
of that market disqualifies itself on session length (Device Farm caps at 40 min, the QA clouds at
30 min–3 hr with no confirmed login persistence), explicit ToS (Sauce Labs bans non-testing social
media use in writing), or protocol (Browserless is CDP-first, not Selenium). Steel.dev and
Browserbase were the two that fit LEM's pattern on paper and beat AWS on cost, but each carried an
unresolved blocker (a Selenium-compatibility check, and for Steel a real ToS read) — and the owner's
call was to **not spend the time resolving them**. They are out with the rest of the market; no
vendor spike is scheduled at any user tier.

**#633 also corrects this section's own premise: Option A below assumed an in-place resize that
does not exist.** Hostinger's VPS line tops out at the box LEM already runs (8 vCPU / 32 GB) — a 16
vCPU/64 GB box is only available by switching provider entirely (~$315/mo Hetzner, ~$504/mo
DigitalOcean), which is a full migration (DNS, data, Docker stack, Cloudflare Tunnel cutover), not
a plan change. Read the row below with that correction; **Option B is the default now**, not a
close second.

The two self-managed options are both real at ~50 users, and the load test picks between them by what
has to scale.

| | **A. Upgrade to 16 vCPU / 64 GB** | **B. Second VPS running Grid nodes** |
|---|---|---|
| Sessions it buys | ~16 concurrent (2× today) | ~8 per added box, unbounded by adding boxes |
| Cost shape | one bigger monthly bill (**now a different provider's bill** — see below) | a second, same-provider bill + a private network to run and firewall |
| Failure domain | still ONE box — it takes the app tier with it | app tier survives a Chrome-box outage |
| Ops cost | **a full migration** (DNS, data, Docker stack, Cloudflare Tunnel cutover) — Hostinger has no bigger tier to resize into | same-provider: a second host to patch + node registration to monitor, no migration |
| Egress | unchanged (per-user proxies do the egress, `EGRESS_AT_SCALE.md`) | unchanged — nodes egress through the same per-user proxies |
| Ceiling | ~16 sessions ≈ **50–60 users staggered**; then this decision repeats | none in practice |

**Where this stands (updated by #633, then #696):** staggering went first and has shipped (#554) — it
was the only free move — and re-measuring it (#634) found the 50-user curve was **15** sessions, not
the originally-predicted 11, because the shared `se_outreach` lane needed its own fix first. #696
shipped that fix (appreciation-DM anchor 08:00 → 07:00) and the curve is now **14** sessions at
64.3% on-time: better than the pre-#554 baseline on both counts, and staggering is finally a net win
at every scale rather than something a lane-mate pays for — but still short of the 11 the plan
banked on, so it is bought headroom, not a substitute for capacity.

**A is no longer the cheaper answer today.** #633 found Hostinger's VPS line tops out at the box LEM
already runs, so "upgrade to 16 vCPU / 64 GB" now means switching provider entirely (~$315/mo
Hetzner, ~$504/mo DigitalOcean) plus a real migration project, not a resize. B (a second
same-provider box, already built as `docker-compose.grid-node.yml`) is cheaper in absolute terms at
every tier this plan projects (~$52–200/mo vs. A's ~$315–504/mo) **and** carries none of A's
one-time migration risk. That reverses the old reading of the 50-user row — A's edge there was
operational simplicity, and the migration eats it. **B is the default path**, not just the answer
past ~16 sessions.

The choice is **still not being made today** — nothing is bought until §5e files a breach. What
#633 resolved is *which* option to reach for when that happens: B, not A. The sequence that
remains: bank the stagger (done) → re-measure it (done, #634) → fix the lane contention it exposed
(done, #696) → cut over the Grid at parity → scale nodes per §6's checklist when §5e says the cap
is the operating point.

The Grid is worth cutting over to **before** either, at the same 8 nodes: it is capacity-neutral,
it makes a crashed Chrome cost one session instead of all of them, and it is the thing that makes B
a config change rather than a migration.

---

## 6. Cutover checklist

1. Capacity monitor (§5e) has filed a `session_saturation` or `lane_backlog` breach — i.e. the cap
   is the operating point, not a busy afternoon.
2. Run the load test at the target cohort size; record the curve in the issue.
3. ✅ Golden-hour stagger shipped (#554). ✅ Re-ran the curve against the staggered arrivals (#634) —
   53.7% on-time / 15 sessions at 50 users, worse than the baseline. ✅ Fixed the `se_outreach`
   contention (#696, appreciation-DM anchor 08:00 → 07:00) and re-ran: **64.3% / 14 sessions**.
   Staggering is now a net win at every scale, and still **does not clear the 95% SLO at 50 users**
   on 8 slots — treat it as bought headroom, not as sufficient on its own.
4. Cut over to the Grid at the SAME node count as today's cap (8). Verify `/status` shows 8 slots
   (see §2 — a short node count is silent) and a full engagement cycle runs green.
5. Only then change the numbers: raise `SELENIUM_GRID_NODES` **and** the lane concurrencies in one
   commit, sized by the load test's "sessions needed" column, on a box (or boxes) that §5c says can
   pay for them.
