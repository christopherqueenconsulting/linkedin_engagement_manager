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

The capacity monitor (`auto_capacity_watch`, §5e) reads the same `/status` endpoint and needs no
change: a hub reports slots exactly like the standalone did.

### What the cutover costs you

**noVNC on port 7900 goes away.** The hub has no browser; noVNC lives on the nodes, and replicas
cannot all publish the same host port. To watch a live session, map 7900 on one node
(`docker compose port <node-container> 7900`) or keep a single explicitly-mapped node for debugging.
`tools/selenium_mcp_server.py` is unaffected — it drives 4444, which is still the hub.

Both 4444 and the event bus bind to **loopback** by default (`SELENIUM_GRID_HUB_BIND`,
`SELENIUM_GRID_BUS_BIND`), matching how `docker-compose.prod.yml` hardens the standalone today.

---

## 3. The load test

`python -m cqc_lem.utilities.selenium_load_test` answers the question Phase 2 gates the cohort on:
**at N users, what fraction of each user's engagement work starts inside its window, and what would
it cost to serve the demand?**

```sh
# the curve, on today's deployed topology
python -m cqc_lem.utilities.selenium_load_test --users 10,50,100

# what-if: staggered golden hour (§5d), a 16-node Grid, Phase-2 lane concurrencies
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

## 4. Measured curve (2026-07-26, today's topology: 8 slots, lanes 3/2/2/1)

| Users | On-time % | Late jobs | Delay p95 | Sessions needed | Chrome mem | Host CPU | Verdict |
|---|---|---|---|---|---|---|---|
| 10 | **100%** | 0 | 60 min | 5 (engage 2, prepost 1, outreach 1, content 1) | 6.0 GB | 6.5 vCPU (81%) | fits |
| 50 | **57.7%** | 148 | 320 min | 14 (engage 6, prepost 4, outreach 2, content 2) | 16.8 GB | 15.5 vCPU (194%) | at ceiling |
| 100 | **17.7%** | 576 | 640 min | 27 (engage 12, prepost 7, outreach 4, content 4) | 32.4 GB | 28.5 vCPU (356%) | exceeds one VPS |

With `--stagger-hours 4` (§5d's per-user golden-hour offset — **shipped in #554** while this harness
was in review, so these rows are now a *prediction to be checked*, not a proposal; the re-run that
confirms or refutes them is **#634**):

| Users | On-time % | Sessions needed | Host CPU | Verdict |
|---|---|---|---|---|
| 10 | 100% | 4 | 5.5 vCPU (69%) | fits |
| 50 | **84.0%** | 11 | 12.5 vCPU (156%) | at ceiling |
| 100 | **31.1%** | 20 | 21.5 vCPU (269%) | exceeds one VPS |

Three things fall out of this:

1. **Today's 8 slots are right for ~10 users and nothing more.** On-time collapses to 58% at 50
   users and 18% at 100 — the §2 prediction ("tasks miss their window, the box does not fall over"),
   quantified.
2. **Staggering is the cheapest win, and it has now shipped (#554).** The model says it cuts the
   sessions needed at 50 users from 14 to 11 and lifts on-time from 58% → 84% *with no new hardware* —
   which is why it went first, before any spend. What is NOT yet done is proving it: the model still
   fans every user out at one 13:00 UTC minute at `--stagger-hours 0`, whereas #554 gives each user a
   hashed slot inside a 3-hour window **in their own timezone**. Teaching the workload model that
   shape and re-running the curve is **#634**, and until it lands these two tables are a prediction.
3. **`se_prepost` is the lane §4 never modelled.** At 100 users it needs 7 slots on its own, because
   a 15-minute warm-up with a 5-minute tolerance cannot absorb posts arriving every 2.4 minutes. It
   is the first lane to break and the least forgiving.

> The harness is more complete than §4's back-of-envelope, which modelled only the commenting loops
> and put 50 users at 8–10 sessions. Adding the once-a-day batch fan-outs (appreciation DMs, stats
> scrape) and the `se_prepost` lane added by #553 is what takes 50 users to 14. Same box, same
> assumptions (1.2 GB + 1 vCPU per session, §4) — more of the actual workload.

---

## 5. The decision point: 16 vCPU / 64 GB, a second box — or someone else's grid

**Status: deliberately undecided (owner call on #556, 2026-07-26).** Nothing is bought until the
capacity monitor (§5e) says the cap is the operating point, and the option set is widened first:
**#633** prices hosted/cloud grids (AWS Fargate/EC2 nodes, Device Farm, BrowserStack, Sauce Labs,
LambdaTest, Browserless, Browserbase, Steel) against the two self-managed baselines below, keyed to
this section's sessions-needed curve. That comparison is a spike, not a foregone conclusion: most of
that market is *test* infrastructure and may disqualify itself on datacenter egress IPs (we route
per-user residential proxies on purpose), per-minute billing against long-lived logged-in sessions,
ToS clauses on social automation, and whether the MV3 proxy-auth extension can load at all.

The two self-managed options are both real at ~50 users, and the load test picks between them by what
has to scale.

| | **A. Upgrade to 16 vCPU / 64 GB** | **B. Second VPS running Grid nodes** |
|---|---|---|
| Sessions it buys | ~16 concurrent (2× today) | ~8 per added box, unbounded by adding boxes |
| Cost shape | one bigger monthly bill; no new ops surface | a second bill + a private network to run and firewall |
| Failure domain | still ONE box — it takes the app tier with it | app tier survives a Chrome-box outage |
| Ops cost | a resize + a restart | WireGuard/VPC, a second host to patch, node registration to monitor |
| Egress | unchanged (per-user proxies do the egress, `EGRESS_AT_SCALE.md`) | unchanged — nodes egress through the same per-user proxies |
| Ceiling | ~16 sessions ≈ **50–60 users staggered**; then this decision repeats | none in practice |

**Where this stands:** staggering went first and has shipped (#554) — it was the only free move.
Between A and B, A is the cheaper answer *if* the choice were made today: one 16 vCPU / 64 GB box
covers the ~14 sessions that 50 users need (16.8 GB Chrome + ~6 GB app tier fits 64 GB with room) at
a fraction of the operational cost of a second host, and B's fault isolation only starts paying when
Chrome and the app tier genuinely compete — the 100-user row, not the 50-user one. B is the answer
past **~16 sessions**, i.e. 100 users at any stagger.

But the choice is **not** being made today. The sequence the owner set is: bank the stagger (done) →
re-measure it (#634) → price hosted alternatives beside A and B (#633) → decide when §5e files a
breach. Buying either box now would pay for capacity the curve cannot yet prove is needed, and would
foreclose an option that has not been costed.

The Grid is worth cutting over to **before** either, at the same 8 nodes: it is capacity-neutral,
it makes a crashed Chrome cost one session instead of all of them, and it is the thing that makes B
a config change rather than a migration.

---

## 6. Cutover checklist

1. Capacity monitor (§5e) has filed a `session_saturation` or `lane_backlog` breach — i.e. the cap
   is the operating point, not a busy afternoon.
2. Run the load test at the target cohort size; record the curve in the issue.
3. ✅ Golden-hour stagger shipped (#554). Re-run the curve against the staggered arrivals (#634) —
   if it clears the SLO, stop here.
4. Cut over to the Grid at the SAME node count as today's cap (8). Verify `/status` shows 8 slots
   (see §2 — a short node count is silent) and a full engagement cycle runs green.
5. Only then change the numbers: raise `SELENIUM_GRID_NODES` **and** the lane concurrencies in one
   commit, sized by the load test's "sessions needed" column, on a box (or boxes) that §5c says can
   pay for them.
