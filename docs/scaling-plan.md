# VPS Scaling & Concurrency Plan

**Status:** Draft for decision · **Date:** 2026-07-25 · **Owner:** Chris
**Context:** Ahead of a launch campaign that brings new users, decide whether LEM's
production VPS needs **more Celery workers / new lane queues** and **more Selenium
browser concurrency** so scheduled and concurrent tasks run as close to their
scheduled times as possible.

> Companion docs: `EGRESS_AT_SCALE.md` (egress IP / managed-vs-DIY decision),
> `PER_USER_PROXY.md` (proxy plumbing), `AUTOMATION_COOLDOWN.md` (429 breaker).
> This doc is the **compute/concurrency** axis — how many workers and browser
> sessions the box can run, and when it runs out. It deliberately does NOT
> re-litigate the egress/ban-risk axis (see `EGRESS_AT_SCALE.md`).

This is a **proposal**, except where a section is marked **APPLIED** — the Phase 1
compose changes in §5a landed via issues #553 (the `se_prepost` lane) and #552 (the
capacity bumps + the §5e monitor) and reach prod on the next release. Nothing else here
has been applied to `/opt/lem` or any running container.

---

## TL;DR

- **The VPS is nowhere near CPU/RAM/disk exhausted today** — it is essentially idle
  because there is **1 active user**. Load average 0.8 on 8 cores (~10%), ~4 GB of
  RAM in use across all app containers, 307 GB disk free.
- **The binding constraint is NOT host resources — it is Selenium browser
  concurrency and Celery lane concurrency.** There is **one** `selenium-chrome`
  standalone with **4 session slots**, and the three Selenium lane workers request
  **2 + 1 + 1 = 4** concurrent sessions — the pool is exactly saturated at full
  parallelism. Every additional user's engagement work **serializes** on those 4
  slots and on lane workers with concurrency 1–2.
- **The failure mode at scale is "tasks miss their window," not "the box falls
  over."** Multiple users whose posts/golden-hours land in the same minutes queue
  behind 15-minute commenting loops on a lane that can only run 2 at once. This is
  the root cause behind issue #547 (pre-post commenting firing late).
- **A second aggravator is a single fixed fan-out time.** `auto_daily_engagement`
  fires for **all** users at one crontab (`13:00 UTC`), dumping N tasks onto
  `se_engage` (concurrency 2) at once. Throughput there is ~8 users/hour, so 50
  users would take ~6 hours to drain — most run hours late.
- **Recommendation:** (1) raise lane concurrency + Chrome session cap now for
  cheap headroom (the box can absorb it), (2) **stagger the fixed-time fan-outs**,
  (3) plan a **Selenium Grid (hub + N nodes)** as the horizontal path when users
  cross ~50, and (4) budget a bigger VPS (or a second box) at ~50–100 users.

---

## 1. Measured host capacity (2026-07-25, live prod VPS)

```
$ nproc            → 8         (8 vCPU)
$ free -h
               total   used   free   shared  buff/cache  available
Mem:            31Gi   6.4Gi  1.8Gi   15Mi     23Gi        24Gi
Swap:          4.0Gi   1.8Mi  4.0Gi                        (swap essentially unused)
$ df -h /           387G total, 80G used, 307G free (21%)
$ uptime            load average: 0.81, 0.77, 0.78   (~10% of 8 cores)
```

**Per-container snapshot** (`sudo docker stats --no-stream`, 1 active user, mid-day):

| Container | CPU % | Mem usage / limit | Mem % |
|---|---|---|---|
| `selenium-chrome` | 0.75 | **643 MiB / 8 GiB** | 7.9 |
| `web_app` | 0.59 | 472 MiB / 2 GiB | 23 |
| `litellm` | 0.16 | **653 MiB / 1 GiB** | **63.8** |
| `mysql_db` | 0.47 | 439 MiB / 2 GiB | 21 |
| `celery_worker` | 0.26 | 278 MiB / 4 GiB | 6.8 |
| `celery_worker_selenium` (se_engage) | 0.24 | 264 MiB / 2 GiB | 12.9 |
| `celery_worker_selenium_outreach` | 0.18 | 187 MiB / 2 GiB | 9.2 |
| `celery_worker_selenium_content` | 0.24 | 168 MiB / 2 GiB | 8.2 |
| `celery_beat` | 0.00 | 138 MiB / 1 GiB | 13.5 |
| `celery_flower` | 0.61 | 140 MiB / 512 MiB | 27 |
| `redis` | 0.36 | 7 MiB / 1 GiB | 0.7 |
| `cloudflared` | 0.23 | 19 MiB / 256 MiB | 7 |

**Sum of `deploy.resources.limits`** across app + infra containers ≈ **32.75 GB**
of memory limits (web 2 + worker 4 + 3×selenium-worker 2 + beat 1 + flower 0.5 +
selenium-chrome 8 + litellm 1 + redis 1 + mysql 2 + cloudflared 0.25) on a **31 GiB**
host. Limits are ceilings, not reservations, so this over-commit is fine while
actual usage is ~4 GB — but it means **once Chrome sessions actually fill their 8 GB
budget the box has little slack** (see §6).

**Headroom today:** CPU ~90% idle, ~24 GB RAM available, disk 79% free. The host is
not the bottleneck at current load. The tightest container by ratio is `litellm`
(64% of a 1 GB cap) — bump it before LLM concurrency rises.

**MySQL:** `@@max_connections = 151`, `Max_used_connections = 8`, currently 1
connected. `users` table: **1 row**. `db.py` used to open a **fresh connection per
call with no pool**, so connection count scaled with task concurrency, not user
count. Issue #555 put a **per-process pool** behind `get_db_connection()`
(`MYSQL_POOL_ENABLED`, `MYSQL_POOL_SIZE=16`): it opens nothing at startup, grows
lazily to its size as concurrent checkouts demand, reuses connections on `.close()`,
and falls back to a direct connection when a burst outruns the pool. Raise
`max_connections` only if `Max_used_connections` approaches ~120.

---

## 2. The real bottleneck: one browser, four slots

> Numbers in this section are the **measured 2026-07-25 baseline**, before the Phase 1
> bumps in §5a (now 8 slots / 3+2+2+1 lanes) shipped — see the note at the end of the section.

`selenium-chrome` is a **single `selenium/standalone-chrome`** (verified live):

```
SE_NODE_MAX_SESSIONS=4   SE_NODE_OVERRIDE_MAX_SESSIONS=true
shm_size: 4g   limits: cpus 4.0, memory 8g
/status → 1 node, 4 slots, sessionTimeout 600s, Chrome 150
```

The three Selenium lane workers each open sessions against that one node:

| Lane worker | `SELENIUM_QUEUES` | `SELENIUM_CONCURRENCY` |
|---|---|---|
| `celery_worker_selenium` | `se_engage` | **2** |
| `celery_worker_selenium_outreach` | `se_outreach` | **1** |
| `celery_worker_selenium_content` | `se_content` | **1** |

Total requested concurrency **2 + 1 + 1 = 4 = `SE_NODE_MAX_SESSIONS`**. So at full
parallelism **every Chrome slot is claimed** and there is zero spare browser
capacity. The lane split (via `task_routes` + per-task `queue=` in
`run_automation.py`) correctly stops a long commenting loop from starving DMs — but
it does **not** add capacity; it partitions the same 4 slots. `--prefetch-multiplier=1`
means a lane worker holds exactly one in-flight task per concurrency slot, so a
15-minute commenting loop blocks that slot for the full 15 minutes.

**This is the constraint that makes tasks run late**, not CPU or RAM.

> **Since this snapshot:** the whole Phase-1 compose proposal below has shipped.
> Issue #553 added a fourth lane `celery_worker_selenium_prepost` (`SELENIUM_QUEUES=se_prepost`,
> concurrency 2) that consumes ONLY the eta-bound pre-post `automate_commenting` dispatched by
> `auto_check_scheduled_posts`, taking the cap to 6. Issue #552 then raised `se_engage 2 → 3` and
> `se_outreach 1 → 2`, so the four lanes request **3 + 2 + 2 + 1 = 8** and the node was sized to
> match: `SE_NODE_MAX_SESSIONS 4 → 8`, `shm_size 4g → 8g`, Chrome cpus `4 → 8` (memory stays 8g).
> That is the top of this box's Chrome budget (§4) — the next step is a Grid (§5b), and §5e is
> the monitor that says when we're there.

---

## 3. Concurrency demand: what fires when

Beat schedule (`src/cqc_lem/app/my_celery.py`, all UTC). Tasks split into
**Selenium-bound** (need a Chrome slot) and **API/LLM-bound** (default `celery`
lane, no browser):

### Time-sensitive dispatchers (the ones that must hit a window)

| Beat entry | Cadence | Dispatches | Lane |
|---|---|---|---|
| `check-scheduled-posts` | `*/10 min` | `post_to_linkedin` (ETA=post time) + **`automate_commenting` ETA = post − 15 min** + `automate_profile_viewer_engagement` ETA = post − 10 min + `auto_seed_comment_on_post` ETA = post + 3 min | celery / **se_engage** / se_outreach / se_content |
| `check-scheduled-dms` | `*/10 min` | `send_scheduled_dm` (ETA) | se_outreach |
| `daily-golden-hour-engagement` | `*/15 min` tick; each user dispatched at their own slot (#554) | `automate_commenting` (15-min loop) per active user | **se_engage** |
| `dispatch-scheduled-reply-sweeps` | `*/30 min` | `sweep_reply_comments` per scheduled-mode user (Redis-gated to cadence) | se_engage |
| `send-due-dm-followups` | `*/30 min` | `process_user_followups` per user | se_outreach |
| `publish-scheduled-newsletters` | `:05 hourly` | `auto_publish_edition` per due edition | se_content |

### Fixed off-peak batch (all single crontabs, fan out over active users)

`generate-newsletter-drafts` 10:00 · `send-appreciation-dms` 08:00 local (staggered, #554) ·
`group-engagement` 12:00 local (staggered, #554) · `group-posts` Tue 15:00 · `scrape-post-stats` 23:00 ·
`sync-user-groups` Mon 07:00 · `refresh-profile-syntheses` Mon 04:30 ·
`invite_to_company_pages` 1st 05:00 · `backfill-missing-assets` `*/3h` ·
content-plan 01:00 / 01:30 · Stripe sync 06:00 · cleanups 02:00/03:00.

### API/LLM-bound (no Chrome slot; `celery` lane, main worker c=2)

`post_to_linkedin` (official REST API), content-plan LLM generation, newsletter
draft generation, Stripe sync, asset backfill regeneration. These are gated by LLM
latency/cost, not the browser.

**Where scheduled tasks pile up:**

1. **Pre-post window (issue #547).** Each scheduled post enqueues an
   `automate_commenting` task with `eta = post_time − 15 min` onto `se_engage`
   (concurrency 2). If ≥3 users have posts within the same ~15-minute span, the 3rd+
   task waits behind a running 15-minute loop and fires **after** its window — the
   pre-post comment burst lands late or after the post is already live.
2. **The single `13:00` golden-hour fan-out — FIXED by #554.** `auto_daily_engagement`
   used to dispatch one 15-minute `automate_commenting` loop per active user, all at
   once, onto `se_engage`. Throughput = 2 slots ÷ 15 min = **~8 users/hour**, so at 50
   users the queue took **~6 hours** to drain and the last users' "golden hour" ran in
   the afternoon. `QueueOnce(keys=['user_id'])` prevented double-runs but added no
   throughput. It is now a `*/15 min` tick that dispatches only the users whose staggered
   per-user slot has come up (§5d), so arrivals match what the lane can drain.
3. **`se_content` / `se_outreach` single-slot lanes.** Newsletter publishing, stats
   scrape, group sync/posts all share one `se_content` slot; DMs, followups, invites,
   profile-viewer engagement share one `se_outreach` slot. Fine for 1 user, serial
   for N.

---

## 4. Per-user scaling math

Each active user's engagement is largely **serialized on Selenium sessions behind a
per-user residential proxy** (`resolve_proxy()`), so work does not parallelize
*within* a user — it parallelizes *across* users, bounded by browser slots.

**Selenium-minutes/user/day (rough, from loop durations in code):**

| Work | Lane | ~min/user/day |
|---|---|---|
| Pre-post commenting (15-min loop × ~1 post) | se_engage | ~15 |
| Golden-hour commenting (15-min loop) | se_engage | ~15 |
| Reply sweeps (2–12/day, few min each) | se_engage | ~8 |
| Profile-viewer engagement (10-min loop) | se_outreach | ~10 |
| Appreciation DMs + followups + invites | se_outreach | ~10 |
| Seed comment + stats + groups + newsletter publish | se_content | ~10 |
| **Total** | | **~65–70 Selenium-min/user/day** |

**But total minutes is not the ceiling — peak concurrency is.** The demand is
**clustered**: everyone's golden hour (today a single 13:00 fan-out) and each user's
post-time ±15 min. The relevant formula for "runs on time" is:

> To serve **N** users who each need a **15-minute** commenting loop inside a
> **W-hour** window, you need lane concurrency **C ≥ N ÷ (W × 4)**
> (4 = fifteen-minute loops per hour per slot).

| Users | Golden-hour window W | Required `se_engage` C | Chrome sessions needed (all lanes) |
|---|---|---|---|
| 10 | 2 h | ⌈10 ÷ 8⌉ = **2** | ~4 (current) — OK with staggering |
| 50 | 2 h | ⌈50 ÷ 8⌉ = **7** | ~9–10 |
| 50 | 4 h (staggered) | ⌈50 ÷ 16⌉ = **4** | ~6–7 |
| 100 | 4 h (staggered) | ⌈100 ÷ 16⌉ = **7** | ~10–12 |

**Where it breaks first, in order:**

1. **Chrome sessions / lane concurrency** — at ~10–15 concurrent users clustered on
   one window (today), long before host RAM/CPU.
2. **Chrome memory** — each LinkedIn Selenium session peaks ~0.7–1.5 GB. The 8 GB
   Chrome budget realistically holds **~6–8 healthy concurrent sessions**; the 8-core
   host caps useful Chrome concurrency around **8–10** before CPU contention slows
   every session (and slow sessions trip LinkedIn's bot heuristics).
3. **MySQL connections** — only at high fan-out. Pooling shipped in #555, so a
   connection is reused rather than re-opened per call; the 151-connection ceiling is
   the sum of every process's pool, so watch `Max_used_connections` if lane
   concurrency or `MYSQL_POOL_SIZE` goes up.
4. **LLM cost / provider rate limits** — scales with users × posts/comments, not the
   VPS. Becomes a budget and RPM concern (litellm mem + upstream rate limits) around
   50–100 users.
5. **Redis** — negligible (7 MiB used); not a concern at these scales.

---

## 5. Scaling plan

### 5a. Do we add more Celery workers / new lane queues?

**Yes — raise concurrency on the existing lanes first; split lanes only where a slow
loop starves a time-critical task.** Concrete proposed changes (compose overlay, NOT
applied):

**Phase 1 (now — cheap headroom, fits current box):**

- **APPLIED (issue #552)** — Chrome session cap and lane concurrency raised together so
  they stay matched:
  - `selenium-chrome`: `SE_NODE_MAX_SESSIONS: 4 → 8`, `shm_size: 4g → 8g`,
    memory limit `8g` (keep), cpus `4 → 8`.
  - `celery_worker_selenium` (`se_engage`): `SELENIUM_CONCURRENCY: 2 → 3`.
  - `celery_worker_selenium_outreach` (`se_outreach`): `1 → 2`.
  - `celery_worker_selenium_content` (`se_content`): `1 → 1` (unchanged).
  - `celery_worker_selenium_prepost` (`se_prepost`): `2` (added by #553, below).
  - New total requested = 3 + 2 + 1 + 2 = **8 = new session cap**.
  - **Invariant: `SE_NODE_MAX_SESSIONS` == the sum of every lane's `SELENIUM_CONCURRENCY`.**
    Under-provisioning the cap makes lanes block on session creation (tasks miss their
    window); over-provisioning leaves paid-for slots idle. Both compose files are the single
    source of truth and `tests/unit/app/test_selenium_capacity.py` fails the build if they
    drift — update the cap and the lanes in the same commit. No per-user daily cap changes:
    this adds parallelism *across* users, never more actions per account (§5d).
  - **8 is the ceiling of this box, not a step on a ladder.** §4 puts the 8g Chrome budget at
    ~6–8 healthy sessions and the 8-vCPU host at ~8–10 before contention slows every session.
    The next capacity increase is §5b option B/C (a Grid), not a higher number here — the
    monitor in §5e is what tells us we're there.
- **Split a dedicated `se_prepost` lane** so pre-post commenting (issue #547) never
  queues behind the golden-hour loop. ✅ **Shipped (issue #553):** the
  `celery_worker_selenium_prepost` service (`SELENIUM_QUEUES=se_prepost`, concurrency 2)
  consumes the ETA-based `automate_commenting` dispatched from `auto_check_scheduled_posts`
  via a `queue=` override at the dispatch site; the daily golden-hour `automate_commenting`
  still routes to `se_engage` through `task_routes`. It shipped at cap **6**; landing the
  `se_engage 2 → 3` / `se_outreach 1 → 2` bumps above took it to **8**.

**Phase 2 (at ~50 users):** promote each lane worker to its own concurrency 3–4 and
move Chrome to a Grid (see §5b). Consider per-user or per-region sharding of lanes so
one user's stuck session can't block a shared slot.

### 5b. Do we need more Selenium browser concurrency? (three options)

| Option | What | Pros | Cons | When |
|---|---|---|---|---|
| **A. Bump the standalone cap** | `SE_NODE_MAX_SESSIONS 4→6→8`, more shm/mem | Zero new infra; one-line change | Single point of failure; one Chrome crash kills all sessions; ceiling ~8–10 on this box | **Now** (Phase 1) |
| **B. Multiple `standalone-chrome` replicas** | 2–3 standalone containers, workers round-robin | Fault isolation; simple | No central routing — workers must be pinned to a node URL; uneven load | Bridge option if Grid is deferred |
| **C. Selenium Grid (hub + N nodes)** | One hub, N `node-chrome` containers; sessions scale with node count, possibly across hosts | Sessions scale horizontally; nodes can live on a second VPS; central queue/routing; graceful node drain | More moving parts; hub is a new dependency | **At ~50 users** (Phase 2) |

**Recommendation:** **A now, C at scale.** Grid is the right horizontal path because
nodes can be added on a second box without touching the app tier — the same
`get_docker_driver()` seam points at the hub. Resource cost of Grid: hub ~0.5 vCPU /
512 MB; each node = 1 Chrome ≈ 1 vCPU / 1.5 GB + 2 GB shm. Budget **~2 GB + ~1 vCPU
per additional concurrent session**.

### 5c. Resource plan by scale

| Active users | Concurrent Chrome sessions | vCPU | RAM | Topology | Verdict on current VPS (8 vCPU / 31 GB) |
|---|---|---|---|---|---|
| **10** | 4–6 | ~6–8 used peak | ~10–12 GB peak | Current stack + Phase 1 bumps + staggered golden hour | **Fits comfortably** |
| **50** | 8–10 | ~12–14 peak | ~20–24 GB peak | Grid hub + 2–3 nodes; lane concurrency 3–4; MySQL pooling; stagger fan-outs | **At/over the ceiling** — Chrome RAM + 8 vCPU become the limit; move Grid nodes to a **2nd VPS** or upgrade to **16 vCPU / 64 GB** |
| **100** | 12–16 | ~20+ | ~40+ GB | Grid across **2+ boxes**; dedicated Chrome host(s); LLM cost/RPM budget; MySQL pooling mandatory | **Exceeds one VPS** — horizontal (separate app tier + Chrome tier) |

- **MySQL:** ✅ done (#555) — `get_db_connection()` checks out of a
  `mysql.connector.pooling.MySQLConnectionPool` per process (`MYSQL_POOL_SIZE`,
  default 16, connector max 32) so fan-out bursts don't churn connections. The pool
  is keyed on pid (Celery prefork forks its workers) and grown lazily, so idle
  processes hold no sockets. Raise `max_connections` only if `Max_used_connections`
  approaches ~120.
- **Redis:** fine to 100 users; keep the 1 GB cap, watch `used_memory` if reply-sweep
  / breaker keys grow.
- **litellm:** raise memory limit `1g → 2g` before Phase 2 (already at 64%).
- **Per-user proxy:** one stable residential IP per user (see `PER_USER_PROXY.md` /
  `EGRESS_AT_SCALE.md`) — this is an egress/cost line item (~$2–4/user/mo), not a VPS
  resource, but it gates how much engagement concurrency is *safe* (see §5d).

### 5d. Guardrails — concurrency ≠ hammering one account

Raising concurrency is about running **many users each within their own LinkedIn
limits in parallel** — not doing more per account. Keep these invariants as
concurrency rises:

- **Per-user caps stay authoritative.** `max_comments_per_day`, `max_dms_per_day`,
  and the reply-sweep cadence are enforced in the task bodies; more workers must not
  bypass them. Per-user `QueueOnce(keys=['user_id'])` must remain on every fan-out
  loop so a user can't get two concurrent sessions.
- **Per-task `rate_limit`** (e.g. `automate_commenting` `4/m`, outreach `1–4/m`) is a
  **global** Celery rate cap; as lanes multiply, re-express these as **per-user**
  pacing so a busy account isn't hammered while total throughput rises.
- **The 429 breaker is currently GLOBAL** (keyed on `linkedin:429_cooldown`, one
  egress IP). With **per-user proxies**, a single user's 429 should only pause **that
  user** — otherwise one user trips the breaker for everyone. **Key the breaker per
  user/proxy** before onboarding many users on distinct IPs (today's global key is
  correct only while everyone shares one IP). See `AUTOMATION_COOLDOWN.md`.
- **Stagger fixed-time fan-outs. APPLIED (issue #554).** The single `13:00` golden-hour
  crontab is gone. `daily-golden-hour-engagement`, `send-appreciation-dms` and
  `group-engagement` now tick every 15 min (`STAGGER_TICK_MINUTES`) and each dispatches
  only the users whose slot has come up: `plan_daily_slot` in
  `utilities/engagement_window.py` gives every user a stable hashed minute inside a window
  that opens at an anchor hour **in that user's own timezone** (golden hour 09:00 local
  +0–180 min, appreciation DMs 08:00 local +0–120, groups 12:00 local +0–120; all three
  retunable with `<NAME>_ANCHOR_HOUR` / `<NAME>_WINDOW_MINUTES` / `<NAME>_ANCHOR_TZ`, and a
  window of `1` restores "everyone at once"). A Redis claim
  (`engagement:slot:<NAME>:<user>:<local date>`) keeps it to one run per user per local day
  and lets a slot missed by a beat outage — or by an open 429 breaker — catch up on a later
  tick instead of being lost for the day; keying the claim by the slot's own date is what
  keeps such a late catch-up from still being held at the next day's slot. Per-user
  `QueueOnce` and the per-day caps are untouched. With a 3-hour window, `se_engage` sees
  ~1 golden-hour loop per 15-min tick at 12 users instead of 12 at once — the table in §4
  reads off the staggered rows now.
- **Human pacing per session** (already present via loop durations + jitter) must not
  be shortened to gain throughput — add sessions, not speed.

### 5e. Knowing when to move — the capacity monitor **APPLIED (issue #552)**

Every number in §5a is a point-in-time fit for ~10 users. The failure mode of an outgrown
cap is not a crash — it is **tasks quietly firing late**, which used to reach us only as a
user complaint. `utilities/capacity_alerts.py` + the `capacity-watch` beat entry
(`auto_capacity_watch`, every 15 min) is the signal that says when this section's numbers
have stopped holding:

| Check | Source | Fires when |
|---|---|---|
| `session_saturation` | `selenium-chrome` `/status` slots | busy slots ≥ `CAPACITY_SATURATION_PCT` of the cap on ≥ `CAPACITY_SUSTAINED_PCT` of window samples |
| `lane_backlog` | broker queue depth per `se_*` lane | a lane holds ≥ `CAPACITY_BACKLOG_TASKS` waiting messages on a sustained share of samples |
| `session_wait` | `get_docker_driver()` acquisition timings | p95 time to obtain a Chrome session ≥ `CAPACITY_WAIT_SECONDS` |

- **Sampled, not instantaneous.** Each tick appends one sample to a Redis rolling window
  (`CAPACITY_WINDOW_SAMPLES`, 672 ≈ 7 days at 15 min) and nothing is judged until
  `CAPACITY_MIN_SAMPLES` exist. A single saturated sample is *healthy* use of a pool we paid
  for; a quarter of a week is a ceiling.
- **Unknown ≠ OK.** An unreachable Grid or Redis produces *no* sample, and the check reports
  itself `skipped` with a reason rather than a confident all-clear.
- **Delivery is a GitHub issue** (labels `infrastructure`, `observability`, `needs-human`)
  carrying the measured numbers, the cap==Σ-lanes invariant, and lettered options: raise the
  breaching lane + cap in lockstep (§5a), move to a Grid (§5b), or retune thresholds. Exactly
  one issue is open at a time — re-breaches comment on it after
  `CAPACITY_ISSUE_COOLDOWN_DAYS`. Requires `FEEDBACK_GITHUB_TOKEN`/`GITHUB_TOKEN`; without one
  the breach still logs + emits a PostHog `capacity_alert` event, it just isn't filed.
- **It never changes a limit by itself.** Raising the cap spends real RAM/CPU on a shared box
  (§5c), so the monitor's whole job is to put that decision in front of a human with evidence.

---

## 6. Phased rollout tied to the launch

**Phase 0 — before launch (headroom, no topology change):**
- Bump `litellm` memory `1g → 2g`.
- Confirm swap is healthy (it is; 4 GB, unused).
- Add MySQL connection pooling in `db.py` (defensive; no behavior change at 1 user).

**Phase 1 — launch / first ~10 users (this plan's compose proposal):**
- ✅ New `se_prepost` lane + worker for pre-post commenting (fixes #547 under
  contention) — shipped in #553 with `SE_NODE_MAX_SESSIONS 4 → 6` / `shm 4g → 6g`.
- ✅ `se_engage` concurrency `2 → 3`; `se_outreach` `1 → 2` (#552).
- ✅ `SE_NODE_MAX_SESSIONS → 8`, `shm → 8g`, Chrome cpus `4 → 8` (#552) — the four lanes
  now sum to 8, the top of this box's Chrome budget (§4).
- ✅ Capacity monitor (§5e) to detect when these numbers stop holding (#552).
- ✅ **Stagger the fixed-time fan-outs** by per-user offset (#554) — `auto_daily_engagement`,
  `auto_appreciate_dms`, `auto_group_engagement`.
- All fits the current 8 vCPU / 31 GB box.

**Phase 2 — ~50 users:**
- Move Chrome to **Selenium Grid** (hub + 2–3 nodes); nodes can go on a **2nd VPS**.
- Lane concurrency 3–4 each; per-user 429 breaker keys; per-user rate pacing.
- Upgrade to **16 vCPU / 64 GB** or split app-tier / Chrome-tier across two boxes.
- Load-test the topology (see issue) before onboarding the cohort.

**Phase 3 — ~100 users:**
- Grid nodes across 2+ hosts; dedicated Chrome host(s).
- MySQL pooling mandatory; LLM cost/RPM budget + upstream rate-limit review.
- Horizontal app tier if `web_app` / main worker saturate.

---

## Appendix — key facts this plan is grounded in

- Host: **8 vCPU, 31 GiB RAM, 4 GiB swap (unused), 387 GB disk (21% used)**, load ~0.8.
- Selenium: **1 standalone, 4 session slots**, `shm 4g`, 4 cpu / 8 GB limit — raised to
  **8 slots**, `shm 8g`, 8 cpu / 8 GB by #553 + #552.
- Lanes: `se_engage` c=2, `se_outreach` c=1, `se_content` c=1 → **4 = session cap** — now
  `se_engage` 3, `se_prepost` 2, `se_outreach` 2, `se_content` 1 → **8 = session cap**.
- MySQL: `max_connections=151`, `Max_used=8`, **no connection pool** (fresh connect
  per call), **1 user** today.
- Pre-post commenting: `automate_commenting.apply_async(eta = post − 15 min)` — measured on
  `se_engage`, moved to the dedicated `se_prepost` lane by #553 (`run_scheduler.py`).
- Golden-hour: single `13:00 UTC` crontab fans out one 15-min loop per active user
  onto `se_engage`.
- 429 breaker: **global** Redis key by egress IP; per-user proxy resolution exists
  (`resolve_proxy()`).
