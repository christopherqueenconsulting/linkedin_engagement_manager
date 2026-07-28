# Celery & Flower Operations Guide

## Architecture Overview

The task queue system has three services that communicate through Redis:

```
celery_beat  ──schedules──▶  Redis (broker)  ──dispatches──▶  celery_worker
                                                                     │
celery_flower ──reads events from broker──────────────────────────────┘
```

| Service | Container | Role |
|---|---|---|
| `celery_worker` | `celery_worker` | Executes tasks; autoscales 2–4 processes |
| `celery_beat` | `celery_beat` | Fires scheduled tasks on cron schedule |
| `flower` | `celery_flower` | Read-only monitoring UI and API |
| Redis | `redis` | Broker (DB 0) and result backend (DB 1) |

**PostHog** receives a `celery_task` event for every task that starts and completes (via `task_prerun` / `task_postrun` signals in `my_celery.py`).

---

## Accessing Flower

**Local (no ngrok):** `http://localhost:8555` (or whatever `CELERY_FLOWER_PORT` is set to in `.env`)

**Ngrok paid plan:** `https://${NGROK_FLOWER_PREFIX}.${NGROK_FREE_DOMAIN}` — a static subdomain of your reserved ngrok domain. Set `NGROK_FLOWER_PREFIX` and `NGROK_FREE_DOMAIN` in `.env` to match the domain reserved in your ngrok dashboard.

**Ngrok free plan:** Flower gets a random dynamic URL. `run.sh` queries the ngrok API (`/api/tunnels`) after startup and prints the live URL in the URL summary table. If the API query fails, it falls back to "Dynamic — see http://localhost:${NGROK_UI_PORT}" where you can find it under the `lem-flower` tunnel.

Flower exposes no authentication by default (`FLOWER_UNAUTHENTICATED_API=True`). To enable basic auth, uncomment the `--basic_auth` line in `compose/local/celery/flower/start-no-wait` and set `CELERY_FLOWER_PASSWORD` in `.env`.

### Flower Tabs

| Tab | What it shows |
|---|---|
| **Dashboard** | Active workers, their status, concurrency, tasks processed |
| **Tasks** | Live and historical task list with state, runtime, args |
| **Broker** | Redis queue lengths per queue name |
| **Monitor** | Real-time task rate graphs |
| **Workers** | Per-worker stats: processed, failed, retried |

**Idle workers are normal.** Most beat tasks run between 1–8 AM ET. Workers show "Idle" when no task is currently executing — that is expected outside scheduled windows. `check-scheduled-posts` runs every 30 minutes and completes in under a second if no posts are pending.

---

## Beat Schedule

All times are in the timezone set by the `TZ` env var (default `America/New_York`).

| Schedule key | Task | When |
|---|---|---|
| `check-scheduled-posts` | `auto_check_scheduled_posts` | :00 and :30 of every hour |
| `generate-content-plan` | `auto_generate_content` | Daily 1:00 AM |
| `create-content-from-plan` | `auto_create_weekly_content` | Daily 1:30 AM |
| `clean-up-stale-invites` | `auto_clean_stale_invites` | Daily 2:00 AM |
| `clen-up-stale-profiles` | `auto_clean_stale_profiles` | Daily 3:00 AM |
| `invite_to_company_pages` | `auto_invite_to_company_pages` | 1st of month 5:00 AM |
| `send-appreciation-dms` | `auto_appreciate_dms` | Every 15 min; dispatches each user at their own staggered slot in 7:00–9:00 **user-local** (#554/#696) |

---

## Checking Worker Health

### Quick status
```bash
docker compose ps celery_worker celery_beat flower
```

Expected state: all three `Up (healthy)` after startup completes.

### Ping a worker directly
```bash
docker compose exec celery_worker celery --app cqc_lem.app.my_celery inspect ping
```

Successful response: `celery@celery-worker-host: OK` with a pong.

### Stream worker logs
```bash
docker compose logs -f celery_worker
docker compose logs -f celery_beat
docker compose logs -f celery_flower
```

Worker logs are also written to `logs/cqc_lem_YYYY_MM_DD.log` (rotated daily, kept 10 days).
Flower logs go to `logs/flower.log`.

### Check active tasks
```bash
docker compose exec celery_worker celery --app cqc_lem.app.my_celery inspect active
```

### Check reserved (queued but not started) tasks
```bash
docker compose exec celery_worker celery --app cqc_lem.app.my_celery inspect reserved
```

### Check scheduled (ETA) tasks
```bash
docker compose exec celery_worker celery --app cqc_lem.app.my_celery inspect scheduled
```

---

## Checking Beat Health

Beat does not respond to `inspect ping`. Check it via its log output:

```bash
docker compose logs celery_beat | tail -40
```

A healthy beat log looks like:
```
[2025-01-01 00:30:00,000: INFO/MainProcess] Scheduler: Sending due task check-scheduled-posts (cqc_lem.app.run_scheduler.auto_check_scheduled_posts)
```

If you see no `Sending due task` lines and the container is running, beat may have lost its Redis connection. Restart it:
```bash
docker compose restart celery_beat
```

---

## Manual Task Triggering

Trigger any task directly from inside the worker container for debugging:

```bash
# Trigger check-scheduled-posts immediately
docker compose exec celery_worker python -c "
from cqc_lem.app.run_scheduler import auto_check_scheduled_posts
result = auto_check_scheduled_posts.apply_async()
print('Task ID:', result.id)
"
```

Or use the Celery CLI:
```bash
docker compose exec celery_worker celery --app cqc_lem.app.my_celery call \
  cqc_lem.app.run_scheduler.auto_check_scheduled_posts
```

---

## PostHog Task Events

Every task execution sends a `celery_task` event to PostHog with these properties:

| Property | Description |
|---|---|
| `task` | Full task name (e.g. `cqc_lem.app.run_scheduler.auto_check_scheduled_posts`) |
| `duration_ms` | Wall-clock time from task start to completion |
| `success` | `true` if state is `SUCCESS`, `false` otherwise |
| `state` | Celery final state: `SUCCESS`, `FAILURE`, `REVOKED` |

To find them in PostHog: filter by `Event = celery_task`. Use `task` as a breakdown to see per-task throughput and failure rates.

LLM calls made inside tasks also emit a separate `llm_call` event (tracked by `@llm_tracked` decorator in `ai_helper.py`).

---

## Common Failure Modes

### Flower URL returns "connection refused"

1. Confirm `CELERY_FLOWER_PORT` in `.env` (default `8555`)
2. Check container is running: `docker compose ps flower`
3. Check logs: `docker compose logs celery_flower`
4. Verify port mapping: `docker compose port celery_flower 8555` — should return `0.0.0.0:8555`

### Workers show as "offline" in Flower

Workers publish heartbeats via Redis broker events. If they appear offline:
1. Confirm the worker is running: `docker compose ps celery_worker`
2. Run `inspect ping` (see above) — if this times out, the worker has lost its broker connection
3. Check Redis is healthy: `docker compose ps redis`
4. Restart the worker: `docker compose restart celery_worker`

### Beat tasks not firing

1. Verify beat log shows `Sending due task` lines at expected times
2. Confirm system clock/timezone: `docker compose exec celery_beat date`
3. Check `TZ` env var matches your expectation — beat schedule times are in this timezone
4. If using `PURGE_TASKS=True`, beat purges queued tasks on startup; tasks queued before restart are lost

### Task stuck in STARTED state

The worker likely crashed mid-task. Because `task_acks_late = True`, the message is re-queued automatically after `broker_transport_options.visibility_timeout` (3660s). To force immediate re-queue:
```bash
docker compose restart celery_worker
```

### PostHog shows no celery_task events

1. Confirm `POSTHOG_API_KEY` is set in `.env` (missing key silently disables PostHog)
2. Trigger a task manually (see above) and wait ~30 seconds for event ingestion
3. In PostHog, check the Live Events feed for `celery_task`

---

## Debugging Checklist

Work through this in order when workers appear idle or broken:

1. `docker compose ps` — all Celery services should be `Up (healthy)`
2. `docker compose logs celery_worker | tail -20` — look for Python tracebacks
3. `docker compose logs celery_flower | tail -20` — look for startup errors
4. `docker compose exec celery_worker celery --app cqc_lem.app.my_celery inspect ping` — confirms broker connectivity
5. `docker compose logs celery_beat | grep 'Sending due'` — confirms beat is scheduling
6. Flower UI at `http://localhost:8555` → Workers tab → confirm worker is registered
7. Flower UI → Broker tab → check queue depth (high depth = tasks queued but worker stuck)
8. Flower UI → Tasks tab → filter by FAILURE state to find erroring tasks
9. PostHog `celery_task` events → filter `success = false` for failure patterns

---

## Restarting Services

```bash
# Restart all three Celery services
docker compose restart celery_worker celery_beat flower

# Full rebuild after code changes
docker compose build && docker compose up -d
```

After a full rebuild, allow ~30 seconds for all healthchecks to pass before expecting tasks to run.
