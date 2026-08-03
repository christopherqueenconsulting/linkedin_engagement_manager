# Testing LinkedIn Engagement (Comment / Reply / DM) via the API

This guide shows how to **trigger a single engagement task on demand** and
**watch the Chrome browser do it live** over VNC — e.g. "does commenting actually
work for my account now?"

---

## 1. URLs at a glance

| What | URL |
|---|---|
| App (SPA + API) | <https://lem.christopherqueenconsulting.com> |
| API base prefix | <https://lem.christopherqueenconsulting.com/api> |
| **Interactive API docs (Swagger)** | <https://lem.christopherqueenconsulting.com/api/docs> |
| OpenAPI schema (import into Postman) | <https://lem.christopherqueenconsulting.com/api/openapi.json> |
| **Live browser (VNC)** | <https://lemvnc.christopherqueenconsulting.com/?autoconnect=1&password=secret> |

> The VNC link auto-connects and passes the noVNC password (`secret`) in the
> query string, so it opens straight into the live Chrome session. It's gated by
> Cloudflare Access, so you'll sign in with your Cloudflare identity first.

---

## 2. Authentication — BOTH headers are required

The engagement test endpoints (`/api/admin/test/*`, `/admin/consolidate-duplicate-comments`,
`/admin/task-status/{id}`) require **two** credentials at the same time — they gate through
`_require_api_and_admin`, which checks the bearer itself. Sending only one returns 401 or 403.
Other `/api/admin/*` routes take `X-Admin-Secret` alone or an admin session; sending both is always
correct, so the recipe below works for all of them.

| Header | Value | Where to find it (on the VPS) |
|---|---|---|
| `Authorization` | `Bearer <API_ACCESS_TOKEN>` | `API_ACCESS_TOKENS=` in `/opt/lem/.env` (comma-separated; use any one) |
| `X-Admin-Secret` | `<ADMIN_SECRET>` | `ADMIN_SECRET=` in `/opt/lem/.env` |

- **`Authorization` bearer** is the **non-browser** credential for `/api/*`. Since issue #950 the
  SPA holds none (it authenticates on its session cookie), so this token lives in the server `.env`
  only and is rotatable there without a rebuild — you, Postman and `scripts/` are its callers.
- **`X-Admin-Secret`** is what actually gates the `/api/admin/*` endpoints (per-route breakdown in
  [`identity-and-sessions.md`](identity-and-sessions.md)).

> ### The `/api/admin/*` endpoints are no longer in Swagger (issue #1020)
>
> The docs surface is public — it is what a first-time API caller needs — so publishing the admin
> operations there handed anyone probing `X-Admin-Secret` the exact list of paths to aim at. They
> are now excluded from the generated schema, which means Swagger's **Try it out** cannot drive
> them. **Nothing about their auth or behaviour changed**; use curl (§3) or Postman (§4) instead,
> both of which are documented below and send the two headers for you.

> Print the values:
> ```bash
> sudo grep -E '^API_ACCESS_TOKENS=|^ADMIN_SECRET=' /opt/lem/.env
> ```

---

## 3. The test-run endpoints

All take **typed query parameters** (not a raw JSON body) and return a Celery
`task_id` you can poll. Your only LinkedIn account is **`user_id = 1`**
(christopher.queen@gmail.com).

| Method & path | Params | Task |
|---|---|---|
| `POST /api/admin/test/comment` | `user_id`, `loop_for_duration=300` | comment on your feed |
| `POST /api/admin/test/reply` | `post_id`, `loop_for_duration=300`, `future_forward=0` | reply to comments on a post |
| `POST /api/admin/test/dm` | `user_id`, `loop_for_duration=300` | DM recent profile viewers |
| `POST /api/admin/test/dm-direct` | `user_id`, `profile_url`, `message` | send ONE direct DM (most watchable) |
| `GET /api/admin/task-status/{task_id}` | — | poll PENDING/STARTED/SUCCESS/FAILURE |

`loop_for_duration` is in **seconds** (default **300**) so a test run ends on its
own instead of running for an hour.

### Easiest: curl (query params, both headers)
```bash
BASE=https://lem.christopherqueenconsulting.com
curl -X POST "$BASE/api/admin/test/dm-direct?user_id=1&profile_url=https%3A%2F%2Fwww.linkedin.com%2Fin%2Fsome-person%2F&message=Hi%20there" \
  -H "Authorization: Bearer $API_ACCESS_TOKEN" \
  -H "X-Admin-Secret: $ADMIN_SECRET"
```

---

## 4. Postman setup

Files live in [`docs/postman/`](postman/). See
[`docs/postman/README.md`](postman/README.md) for the full walkthrough. In short:

1. **Import the collection** —
   [`LEM_Engagement_Tests.postman_collection.json`](postman/LEM_Engagement_Tests.postman_collection.json).
2. **Import an environment** and pick it (top-right dropdown):
   - **Prod** → [`LEM_Prod.postman_environment.json`](postman/LEM_Prod.postman_environment.json)
     (`base_url = https://lem.christopherqueenconsulting.com`)
   - **Local/Dev** → [`LEM_Local.postman_environment.json`](postman/LEM_Local.postman_environment.json)
     (`base_url = http://localhost:8000`)
3. Fill the env's `api_token` and `admin_secret` (both marked *secret*).
4. Send a request — the collection passes both headers automatically and **saves
   the returned `task_id`** into the environment, so **Task status** just works.

---

## 5. Watching it live in VNC

The Selenium Chrome container runs a noVNC server. The simplest way to watch:

**Open** <https://lemvnc.christopherqueenconsulting.com/?autoconnect=1&password=secret>
(sign in via Cloudflare Access). You'll land directly in the live Chrome session —
trigger a test request (§3/§4) and watch the automation drive the browser in real
time.

### Alternative: SSH tunnel (no public hostname needed)
noVNC is also bound to the VPS loopback on port `7900`:
```bash
ssh -L 7900:localhost:7900 <your-vps-user>@<your-vps-host>
```
then open <http://localhost:7900/?autoconnect=1&password=secret>.

> Keep the public VNC hostname behind Cloudflare Access — never expose it
> unauthenticated.

---

## 6. Troubleshooting

- **401 Unauthorized** → missing/invalid `Authorization: Bearer` token.
- **403 Forbidden** → missing/invalid `X-Admin-Secret`. (Both headers are required.)
- **422 Unprocessable Entity** → a required query param is missing (e.g. `user_id`).
- **Task goes to FAILURE / nothing happens in VNC** → almost always a LinkedIn
  login problem (no stored password/cookies, or a "new location" security
  challenge). Set your **Login Location** first
  (Account → Login Location → "Use my current location") and make sure your
  LinkedIn password is saved.
- **VNC shows a login/blank screen** → the session may have ended
  (`loop_for_duration` elapsed). Re-trigger and watch immediately.
- **Nothing at `localhost:7900`** (SSH path) → the tunnel isn't up, or the running
  release predates the `127.0.0.1:7900:7900` bind.
