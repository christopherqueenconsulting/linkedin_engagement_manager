# MCP server — operator runbook (phase P1)

`POST /api/mcp` is the end-user MCP endpoint (epic #2682; design and rulings in
`docs/agent-mcp-plan.md`). In P1 it is **inert**. No `mcp`-scoped token can exist yet, so every
request that gets past the HTTP checks is refused with 401. This page is what an operator needs to
stop it, confirm it is stopped, and roll it back. Everything here uses files that ship in P1 only.

Run the on-box commands from `/opt/lem`. Use `./scripts/compose.sh`, never a hand-typed `docker
compose -f …` list (`docs/DEPLOYMENT.md` § "Compose file layering" explains why). From a login
outside the `docker` group, prefix each on-box command with `sudo`.

## Kill switch

There are two switches, and either one stops the endpoint. Both are checked on every request,
before any auth step. The kill switch fails **closed**: a Redis the gate cannot read also answers
503.

### 1. Redis key: instant, no restart

The key is `mcp:kill` in Redis **db 0**, the database in `CELERY_BROKER_URL=redis://redis:6379/0`,
which is where the gate reads it. The compose service and container are both named `redis`. Any
stored value means ON.

```bash
cd /opt/lem
./scripts/compose.sh exec -T redis redis-cli -n 0 SET mcp:kill 1     # stop: next request is 503
./scripts/compose.sh exec -T redis redis-cli -n 0 GET mcp:kill       # "1" while stopped
./scripts/compose.sh exec -T redis redis-cli -n 0 DEL mcp:kill       # re-enable: (integer) 1
```

It takes effect on the next request, on every API worker of both colours, with no deploy.

### 2. Env var: needs a deploy or restart

`MCP_SERVER_KILL=1` in `/opt/lem/.env` (any value except `0`, `false`, `no`, `off`). Only the
FastAPI containers read it, `web_api_blue` and `web_api_green` (`env_file: - .env`,
`docker-compose.prod.yml:49-50`). In prod, `web_app` is the nginx edge and gets no env
(`env_file: !reset []`, `docker-compose.prod.yml:30`).

The variable is read per request, but a container only sees a changed `.env` when it is
**recreated**. A plain `restart` is not enough. It therefore lands on the next deploy. To apply it
sooner, redeploy the tag that is already running: `deploy.sh` brings the standby colour up with
`up -d --no-deps web_api_<colour>`, which recreates it with the new environment, and then flips
traffic to it, with zero downtime.

```bash
cd /opt/lem && cat .last_good_tag                    # the running tag
sudo env DOCKER_CONFIG=/home/deploy/.docker bash ./scripts/deploy.sh "$(cat .last_good_tag)"
sudo chown -R deploy:deploy /opt/lem/.git
```

Use the Redis key when you need it NOW, and the env var when it must survive a Redis flush. Remove
the line, or set it to `0`, and deploy again to undo it.

### Verify

```bash
curl -sS -XPOST https://app.<domain>/api/mcp -H 'Content-Type: application/json' -d '{}'
```

While either switch is on, it returns HTTP **503** with this body:

```json
{"status_code":503,"detail":"The LEM MCP server is switched off by the operator.","reason":"mcp_disabled_by_operator"}
```

With both switches off, the same command returns **401** `missing_token`. That is the normal answer
in P1. A **503 `mcp_unavailable`** means either Redis is unreadable or the endpoint did not start
(`PUBLIC_BASE_URL` empty or malformed, or the SDK manager failed). It is not the kill switch.
Check `/opt/lem/logs/cqc_lem_YYYY_MM_DD.log` for `MCP endpoint unavailable`.

## Rollback

P1 adds **no migration** and creates **no tokens or sessions**, so rolling back is just running
the previous image. Nothing needs cleaning up, and no data changes.

1. **First, stop it with the Redis key** (above). This is instant, and often all you need.
2. **Redeploy the previous release.** `scripts/deploy.sh` records the last healthy tag in
   `/opt/lem/.last_good_tag`. If the P1 deploy itself failed its health check, `deploy.sh` has
   already rolled back to that tag. If P1 deployed cleanly, that file now names the P1 release, so
   roll back to the release BEFORE it. The invocation is from `docs/DEPLOYMENT.md`
   § "Deploying from the box as the owner":

   ```bash
   cd /opt/lem && sudo ./scripts/backup.sh
   cat .last_good_tag                                   # the tag deploy.sh last marked healthy
   git tag --sort=-v:refname | head -5                  # pick the release before P1
   sudo env DOCKER_CONFIG=/home/deploy/.docker bash ./scripts/deploy.sh vX.Y.Z
   sudo chown -R deploy:deploy /opt/lem/.git
   ```

   Or use the **Redeploy / Rollback VPS** workflow with that tag and `rollback` ticked
   (`docs/DEPLOYMENT.md` § "Manual redeploy / rollback").
3. **Check health:** `curl -fsS https://app.<domain>/health`. `/health` stays 200 even when the MCP
   session manager fails to start, because the MCP lifespan fails open. A non-200 `/health` is not
   caused by MCP being down; it means something else is wrong.

## Leaked tokens

Any token found in the nginx **error** log or in the Cloudflare logs must be rotated. The access
log drops query-string `/api/mcp` requests (`compose/prod/nginx/default.conf.tmpl`), but nginx's
error log still records the full URL when the upstream is unreachable, and Cloudflare sees every
URL. In P1 no MCP token exists, so the token in question is whatever the client misplaced there. If
it is a LEM session, revoke it (`POST /api/user/sessions/revoke`, the same route the SPA's sign-out
of a device uses).
