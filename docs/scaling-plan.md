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

This is a **proposal**. Nothing here has been applied to `/opt/lem` or any running
container.

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
connected. `users` table: **1 row**. `db.py` opens a **fresh connection per call
with no pool** (`get_db_connection()` → `mysql.connector.connect(...)`), so
connection count scales with task concurrency, not user count — still far under 151
today, but see §6 for the pooling recommendation.

---

## 2. The real bottleneck: one browser, four slots

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

> **Since this snapshot:** issue #553 shipped the first half of the Phase-1 proposal below — a
> fourth lane `celery_worker_selenium_prepost` (`SELENIUM_QUEUES=se_prepost`, concurrency 2) that
> consumes ONLY the eta-bound pre-post `automate_commenting` dispatched by
> `auto_check_scheduled_posts`, with `SE_NODE_MAX_SESSIONS 4 → 6` and `shm_size 4g → 6g` so the
> new lane's 2 slots are real capacity (2 + 2 + 1 + 1 = 6). The other Phase-1 bumps
> (`se_engage 2 → 3`, `se_outreach 1 → 2`, Chrome cpus `4 → 6`) are still proposals.

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
| `daily-golden-hour-engagement` | **`13:00` (all users at once)** | `automate_commenting` (15-min loop) per active user | **se_engage** |
| `dispatch-scheduled-reply-sweeps` | `*/30 min` | `sweep_reply_comments` per scheduled-mode user (Redis-gated to cadence) | se_engage |
| `send-due-dm-followups` | `*/30 min` | `process_user_followups` per user | se_outreach |
| `publish-scheduled-newsletters` | `:05 hourly` | `auto_publish_edition` per due edition | se_content |

### Fixed off-peak batch (all single crontabs, fan out over active users)

`generate-newsletter-drafts` 10:00 · `send-appreciation-dms` 08:00 ·
`group-engagement` 16:00 · `group-posts` Tue 15:00 · `scrape-post-stats` 23:00 ·
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
2. **The single `13:00` golden-hour fan-out.** `auto_daily_engagement` dispatches
   one 15-minute `automate_commenting` loop per active user, all at once, onto
   `se_engage`. Throughput = 2 slots ÷ 15 min = **~8 users/hour**. At 50 users the
   queue takes **~6 hours** to drain; the last users' "golden-hour" engagement runs
   in the afternoon. `QueueOnce(keys=['user_id'])` prevents double-runs but does not
   add throughput.
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
3. **MySQL connections** — only at high fan-out with no pooling (fresh connect per
   call). 151-connection ceiling is comfortable to ~50 concurrent tasks; add pooling
   before that.
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

- Raise Chrome session cap and lane concurrency together so they stay matched:
  - `selenium-chrome`: `SE_NODE_MAX_SESSIONS: 4 → 6`, `shm_size: 4g → 6g`,
    memory limit `8g` (keep), cpus `4 → 6`.
  - `celery_worker_selenium` (`se_engage`): `SELENIUM_CONCURRENCY: 2 → 3`.
  - `celery_worker_selenium_outreach` (`se_outreach`): `1 → 2`.
  - `celery_worker_selenium_content` (`se_content`): `1 → 1` (unchanged).
  - New total requested = 3 + 2 + 1 = **6 = new session cap**.
- **Split a dedicated `se_prepost` lane** so pre-post commenting (issue #547) never
  queues behind the golden-hour loop. ✅ **Shipped (issue #553):** the
  `celery_worker_selenium_prepost` service (`SELENIUM_QUEUES=se_prepost`, concurrency 2)
  consumes the ETA-based `automate_commenting` dispatched from `auto_check_scheduled_posts`
  via a `queue=` override at the dispatch site; the daily golden-hour `automate_commenting`
  still routes to `se_engage` through `task_routes`. Session cap went **4 → 6** with it; it
  becomes **8** once the `se_engage 2 → 3` / `se_outreach 1 → 2` bumps above land.

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

- **MySQL:** add a small connection pool in `get_db_connection()` (e.g.
  `mysql.connector.pooling`, pool size ~16–32) before 50 users so fan-out bursts
  don't churn connections; raise `max_connections` only if `Max_used_connections`
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
- **Stagger fixed-time fan-outs.** Replace the single `13:00` golden-hour crontab
  with a per-user offset (spread across the user's local peak window) so N users don't
  hit `se_engage` in the same minute. This flattens the concurrency spike and is the
  single highest-leverage change for "on-time" behavior at low cost.
- **Human pacing per session** (already present via loop durations + jitter) must not
  be shortened to gain throughput — add sessions, not speed.

---

## 6. Phased rollout tied to the launch

**Phase 0 — before launch (headroom, no topology change):**
- Bump `litellm` memory `1g → 2g`.
- Confirm swap is healthy (it is; 4 GB, unused).
- Add MySQL connection pooling in `db.py` (defensive; no behavior change at 1 user).

**Phase 1 — launch / first ~10 users (this plan's compose proposal):**
- `SE_NODE_MAX_SESSIONS 4 → 6–8`, `shm 4g → 6g`, Chrome cpus `4 → 6`.
- `se_engage` concurrency `2 → 3`; `se_outreach` `1 → 2`.
- ✅ New `se_prepost` lane + worker for pre-post commenting (fixes #547 under
  contention) — shipped in #553 together with `SE_NODE_MAX_SESSIONS 4 → 6` /
  `shm 4g → 6g`.
- **Stagger `auto_daily_engagement`** by per-user offset.
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
- Selenium: **1 standalone, 4 session slots**, `shm 4g`, 4 cpu / 8 GB limit.
- Lanes: `se_engage` c=2, `se_outreach` c=1, `se_content` c=1 → **4 = session cap**.
- MySQL: `max_connections=151`, `Max_used=8`, **no connection pool** (fresh connect
  per call), **1 user** today.
- Pre-post commenting: `automate_commenting.apply_async(eta = post − 15 min)` — measured on
  `se_engage`, moved to the dedicated `se_prepost` lane by #553 (`run_scheduler.py`).
- Golden-hour: single `13:00 UTC` crontab fans out one 15-min loop per active user
  onto `se_engage`.
- 429 breaker: **global** Redis key by egress IP; per-user proxy resolution exists
  (`resolve_proxy()`).
</content>
</invoke>
