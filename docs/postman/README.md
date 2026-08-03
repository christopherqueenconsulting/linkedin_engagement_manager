# Postman — LEM Engagement Tests

Files in this folder:

| File | What it is |
|---|---|
| `LEM_Engagement_Tests.postman_collection.json` | The requests (comment / reply / DM / dm-direct / task-status) |
| `LEM_Prod.postman_environment.json` | **Prod** environment → `https://lem.christopherqueenconsulting.com` |
| `LEM_Local.postman_environment.json` | **Local/Dev** environment → `http://localhost:8000` |

## Import (one time)

1. Postman → **Import** (top-left) → drag in **all three** JSON files (the
   collection + both environments). Postman detects which is a collection and
   which are environments automatically.
2. Top-right **environment dropdown** → pick the one you want:
   - **LEM — Prod** to hit the live VPS.
   - **LEM — Local/Dev** to hit a stack you're running locally
     (`docker compose up`, API on `http://localhost:8000`).

## Fill in the secrets (per environment)

Click the **eye icon** (top-right) → **Edit** the selected environment and set:

| Variable | Prod value | Local value |
|---|---|---|
| `api_token` | one of `API_ACCESS_TOKENS` from `/opt/lem/.env` on the VPS (the **non-browser** credential — the SPA holds none since #950) | one of `API_ACCESS_TOKENS` from your local `.env` |
| `admin_secret` | `ADMIN_SECRET` from `/opt/lem/.env` | `ADMIN_SECRET` from your local `.env` |
| `user_id` | `1` (already set) | your local user id |

`base_url` and `task_id` are pre-filled; leave them. `task_id` is set
automatically from each request's response so the **Task status** call just works.

> Both `api_token` and `admin_secret` are required on every request — the
> collection sends `Authorization: Bearer {{api_token}}` and
> `X-Admin-Secret: {{admin_secret}}` for you.

## Run

1. (Optional) open the VNC to watch — see the main guide,
   [`../TESTING_ENGAGEMENT_API.md`](../TESTING_ENGAGEMENT_API.md).
2. Send e.g. **DM (direct, single profile)** — fill the `profile_url`/`message`
   query params first.
3. Send **Task status** to poll the result (it reuses the saved `task_id`).

## Distinguishing Prod vs Local at a glance

The active environment name shows in the top-right dropdown (**LEM — Prod** vs
**LEM — Local/Dev**), and every request's URL resolves `{{base_url}}` to either
the public domain or `localhost:8000`. Double-check the dropdown before firing a
request so you don't run automation against the wrong stack.
