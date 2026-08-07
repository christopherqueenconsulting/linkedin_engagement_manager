# Agent Pipeline — Capacity-Aware Routing Runbook

How the LEM agent pipeline (`/home/lem/agent-pipeline/tick.sh`) chooses between the **Claude
subscription lane** and the **Ollama cloud lane (via LiteLLM)**, reports into PostHog, labels
issues/PRs, and runs the Perplexity research MCP server.

## TL;DR — lane selection

Every `claude -p` run goes through `lib/run_lane.sh → lib/dispatch.sh → dispatch_lane()`, which
applies the **>50% rule** to the two lanes' estimated capacity:

| Claude | Ollama | SLOT | Decision |
|---|---|---|---|
| >50% | >50% | 1 | **Claude primary** (highest-priority issue) |
| >50% | >50% | ≥2 | **Ollama parallel** (additional issues, horizontal throughput) |
| ≤50% | >50% | any | **Ollama fallback** |
| exhausted/unavailable | >50% | any | **Ollama fallback** (`fallback_from=claude`) |
| >50% | ≤50% | any | **Claude only** |
| ≤50% | ≤50% | any | **Degraded**: CAP forced to 1, new issue starts held; triage/merge/answer still run |

Slot 1 always takes the highest-priority issue (`select_next_issue`), so when both lanes are
healthy Claude gets the hardest task and Ollama handles the rest in parallel.

## The >50% rule and capacity estimation

`lib/capacity.sh` estimates each lane's capacity as a percentage and treats `> LANE_CAPACITY_THRESHOLD`
(default 50) as "available". **Honest limitation:** neither Anthropic's Max subscription nor Ollama
Cloud exposes a "remaining % this session/week" API, so the gauge estimates from rolling run
outcomes plus a cheap health probe, and lets the owner pin it when they know the real headroom:

- **Claude lane**
  - `PAUSED_UNTIL` active (written when a run fails with a usage-limit message) → 0% (exhausted)
  - a usage-limit hit within `CLAUDE_USAGE_COOLDOWN` (2h) → 25% (constrained)
  - ≥3 failures within `CLAUDE_FAIL_WINDOW` (30m) → 40% (constrained)
  - otherwise → 80% (healthy)
  - **override:** `CLAUDE_CAPACITY_PCT=N` in `config.env` (empty = estimate)
- **Ollama lane**
  - `OLLAMA_LANE_ENABLED=0` → 0% (disabled)
  - LiteLLM liveliness probe (`/health/liveliness`) down → 0% (unavailable)
  - `OLLAMA_PROBE=1` → also sends a 1-token completion to confirm cloud is actually serving
  - ≥2 failures within `OLLAMA_FAIL_WINDOW` (30m) → 30% (constrained)
  - otherwise → 75% (healthy)
  - **override:** `OLLAMA_CAPACITY_PCT=N` in `config.env` (empty = estimate)

State lives in `/home/lem/agent-pipeline/state/<lane>.state` (inspectable/repairable). Set an
override when you KNOW the real headroom, e.g. the subscription just reset:
`CLAUDE_CAPACITY_PCT=90` (leave empty to resume the estimate).

## Ollama cloud model tiers (cloud-only, by policy)

Mapped in `.litellm/config.yaml` as `lem-agent-*` aliases (reusing `OLLAMA_CLOUD_URL` +
`OLLAMA_CLOUD_API_KEY` already in `/opt/lem/.env`):

| Alias | Cloud model | Use |
|---|---|---|
| `lem-agent-tier1` | `glm-5.2` | hardest long-horizon / architecture / multi-step (opt-in via `agent:tier:1` — slow thinking model, intermittently times out) |
| `lem-agent-tier2` | `kimi-k2.7-code` | **default** coding lane: patches, tests, multi-file edits |
| `lem-agent-tier2-alt` | `minimax-m3` | premium parallel / vision-capable alternate (opt-in via `agent:tier:2-alt`) |
| `lem-agent-tier3` | `nemotron-3-super` | reviewer / reasoning lane (used for `MODE=selfreview`) or opt-in via `agent:tier:3` |

Ids are the **bare** `ollama.com/api/tags` names, like every other Ollama deployment in that file.
These four carried a `:cloud` tag until #844. Probed 2026-08-01: `glm-5.2:cloud` answers **200** and
serves the same model as bare `glm-5.2` (`glm-5.2:bogus` 404s, so tags *are* validated — `:cloud`
is an alias the endpoint resolves, like `:latest`). So the lane was never broken by it, and tier1's
timeouts are the model being slow, not a 404. What the tag *did* break is the tooling that reads the
config by id — above all `plan_retirement_notices`, which matches the docs.ollama.com/cloud
retirement table verbatim, so an announced retirement of `glm-5.2` would never have matched
`glm-5.2:cloud` and this lane had no advance-retirement cover at all. The four now also have
`model_prices_snapshot.json` entries, which keeps the roster uniform and prices them from day one if
one is ever promoted into a LEM serving tier — it does **not** put a cost on this lane's traffic, which
is `$ai_generation`'s (see *Token/cost accounting* below), never the app's `llm_call`.

LiteLLM falls hard tasks down the chain (`tier1→tier2→tier3`, `tier2→tier3`) before failing.
**Local models are not used on this path** — the Ollama Max subscription is cloud-first.

### How the Ollama lane executes (reuses the claude CLI)

The Ollama lane runs the **same `claude -p` CLI** as the Claude lane, but pointed at LiteLLM:

```
ANTHROPIC_BASE_URL=http://127.0.0.1:4000 \
ANTHROPIC_AUTH_TOKEN=$LITELLM_MASTER_KEY \
claude -p "<RUNBOOK MODE prompt>" --model lem-agent-tier2 --dangerously-skip-permissions ...
```

LiteLLM serves the Anthropic `/v1/messages` endpoint and translates to the Ollama cloud model,
so the entire RUNBOOK/MODE machinery (depfix/revise/rebase/fix/review/selfreview/start) is reused
unchanged — only the model + base URL differ. If an Ollama-lane run fails, the capacity gauge
records the outcome and the issue is retried on the next tick (possibly on Claude).

## PostHog telemetry

Reuses the EXISTING PostHog project keys from `/opt/lem/.env` (copied into
`/home/lem/agent-pipeline/secrets.env` at setup) — **no second PostHog project**. Emitted by
`lib/posthog.sh` (server-side `/capture`), best-effort, never blocks a tick. Common properties on
every event: `lem_component`, `environment`, `repo`, `execution_id`, `worker_id`, `lane`,
`provider`, `model`, `model_tier`, `route_reason`, `issue_number`, `pr_number`, `issue_priority`,
`issue_type`.

| Event | When | Extra props |
|---|---|---|
| `capacity_preflight` | once per tick | `claude_pct/status`, `ollama_pct/status`, `degraded` |
| `routing_decision_made` | per run, after dispatch | `fallback_from/to`, `issue_url` |
| `issue_queued` | issue selected for START | `issue_url` |
| `issue_assigned` | run_lane starts | — |
| `ai_call_started` | before `claude -p` | — |
| `ai_call_completed` / `ai_call_failed` | after run | `success`, `latency_ms`, `error_type`, `tokens_*` (0 — see below), `estimated_cost` |
| `issue_completed` / `issue_failed` | after run | `error_type/message` |
| `pr_opened` / `pr_updated` | PR detected after run | `pr_number` |
| `fallback_triggered` | `route_reason=fallback` | — |
| `lane_escalation_triggered` | `route_reason=degraded/escalated` | — |
| `mcp_research_started/completed/failed` | MCP tool call | `latency_ms`, `citations`, `error_type` |

**Token/cost accounting:** the Ollama lane's per-call tokens + cost are owned by LiteLLM's native
`$ai_generation` PostHog callback (configured in `.litellm/config.yaml`, fed `POSTHOG_API_KEY`/`POSTHOG_HOST`
from the same `/opt/lem/.env`). The Claude lane is a flat-rate subscription (no per-call cost). The
agent-pipeline's own `ai_call_*` events carry `tokens_*=0` deliberately — **do not sum** the two
streams; join on `execution_id` + `model` for the full picture. (Same two-stream contract the LEM
app uses: `llm_call` vs `$ai_generation`.)

Filter dashboards by `lane`, `provider`, `model`, `model_tier`, `route_reason`, `repo`,
`issue_number`, `pr_number`, `error_type`.

## GitHub labels

`lib/labels.sh` bootstraps (idempotent, first tick) and applies these `ai:*` labels so dashboards
and `gh` filters can see which lane/model handled a thread:

```
ai:claude-subscription            ai:ollama-cloud
ai:claude-subscription:sonnet     ai:ollama-cloud:tier1|tier2|tier2-alt|tier3
ai:claude-subscription:haiku      ai:ollama-cloud:glm-5.2 | kimi-k2.7-code | minimax-m3 | nemotron-3-super
ai:claude-subscription:opus
ai:routed:parallel | fallback | escalated | degraded
```

Applied add-only to the issue (or PR when no issue) after each run. To force an Ollama tier on an
issue, add `agent:tier:1` / `agent:tier:2-alt` / `agent:tier:3` (tier2 is the default; tier1 is
opt-in only). To force a Claude model, the existing `agent:model:sonnet|haiku|opus` labels work.

## Perplexity research MCP server

`mcp/perplexity_server.py` is a FastMCP server exposing `research(query, recency)` and
`fact_check(claim)` backed by Perplexity Sonar. It runs as a **persistent systemd service**
(`lem-mcp-perplexity.service`) on streamable-http at `http://127.0.0.1:8765/mcp` (loopback only).
Both lanes pass `--mcp-config /home/lem/agent-pipeline/mcp/mcp-config.json` to `claude -p`, so
agent runs can do web research during issue handling. The server emits its own
`mcp_research_*` PostHog events (same project).

## systemd + VPS resource controls

- **`lem-agent.slice`** — shared CPU/RAM ceiling (`CPUQuota=300%`, `MemoryMax=3G`) for host-side
  agent-pipeline workers (the MCP service, and any future tick worker). Adding parallel lane
  workers cannot each spend a full host.
- **`lem-mcp-perplexity.service`** — lives in the slice, plus service-level guards
  (`MemoryMax=1G`, `CPUQuota=120%`).
- **LiteLLM** — capped **separately** via Docker `deploy.resources` (not in the slice): gunicorn
  `--num_workers` (default 3, configurable via `LITELLM_NUM_WORKERS`) + `LITELLM_MEM_LIMIT`
  (1500m) + `LITELLM_CPU_LIMIT` (2.5). Tune all of these in `config.env`.

## Setup / first-time install (run once)

```bash
# 1) Reuse the existing prod keys for the host-side agent pipeline (mode 600, lem-owned).
sudo bash -c '
  set -e
  SRC=/opt/lem/.env; DST=/home/lem/agent-pipeline/secrets.env
  : > "$DST"
  for k in POSTHOG_API_KEY POSTHOG_HOST LITELLM_MASTER_KEY OLLAMA_CLOUD_API_KEY \
           OLLAMA_CLOUD_URL PERPLEXITY_API_KEY; do
    v=$(grep -E "^${k}=" "$SRC" | head -1 | sed -E "s/^${k}=//" \
        | sed -E "s/^\"(.*)\"$/\1/" | sed -E "s/^'\''(.*)'\''$/\1/")
    [ -n "$v" ] && printf "%s=%s\n" "$k" "$v" >> "$DST"
  done
  chown lem:lem "$DST"; chmod 600 "$DST"'

# 2) FastMCP Perplexity server venv + systemd unit.
python3 -m venv /home/lem/agent-pipeline/mcp/.venv
/home/lem/agent-pipeline/mcp/.venv/bin/pip install -q -r /home/lem/agent-pipeline/mcp/requirements.txt
sudo install -d /etc/lem -m 755
sudo bash -c '
  SRC=/opt/lem/.env; DST=/etc/lem/mcp-perplexity.env
  : > "$DST"
  for k in PERPLEXITY_API_KEY POSTHOG_API_KEY POSTHOG_HOST; do
    v=$(grep -E "^${k}=" "$SRC" | head -1 | sed -E "s/^${k}=//" \
        | sed -E "s/^\"(.*)\"$/\1/" | sed -E "s/^'\''(.*)'\''$/\1/")
    [ -n "$v" ] && printf "%s=%s\n" "$k" "$v" >> "$DST"
  done
  printf "ENVIRONMENT=production\nMCP_HOST=127.0.0.1\nMCP_PORT=8765\n" >> "$DST"
  chmod 600 "$DST"; chown lem:lem "$DST"'
sudo install -m 644 /home/lem/agent-pipeline/systemd/lem-agent.slice /etc/systemd/system/
sudo install -m 644 /home/lem/agent-pipeline/systemd/lem-mcp-perplexity.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now lem-agent.slice lem-mcp-perplexity.service
curl -fsS http://127.0.0.1:8765/mcp >/dev/null && echo "MCP up" || echo "MCP NOT up"

# 3) LiteLLM aliases + gunicorn + loopback publish already applied to prod; recreate if not:
#    sudo bash -c 'cd /opt/lem && docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --no-deps --force-recreate litellm'
```

## Restart / reload

```bash
# MCP research server
sudo systemctl restart lem-mcp-perplexity.service
sudo systemctl status  lem-mcp-perplexity.service
journalctl -u lem-mcp-perplexity -n 50          # or tail logs/mcp-perplexity.log

# LiteLLM (container) — picks up .litellm/config.yaml changes
sudo bash -c 'cd /opt/lem && docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --no-deps --force-recreate litellm'
curl -fsS http://127.0.0.1:4000/health/liveliness   # -> "I'm alive!"

# tick.sh / agent pipeline — no daemon to restart; it is cron-driven every ~5 min. To force a tick:
DRY_RUN=1 bash /home/lem/agent-pipeline/tick.sh     # validates routing without launching claude
bash /home/lem/agent-pipeline/tick.sh              # real tick
```

## Lane health: liveliness is not usability

`/health/liveliness` proves the LiteLLM **proxy** answers. It says nothing about whether the proxy
will serve the tier alias the lane passes as `--model`. Those came apart on **2026-07-29**: the
proxy was up, the gauge read `75 healthy`, and every run died on
`anthropic_messages: Invalid model name passed in model=lem-agent-tier2` — 49 consecutive dead runs
that each claimed an issue `agent:working` and then abandoned it. Two things made that invisible:

- `_ollama_probe` classified on **curl's exit code**, and `curl -sS` (no `-f`) exits 0 on an HTTP
  400 — so a hard rejection was recorded as a healthy probe.
- `_maybe_probe_ollama` only fired after a **usage-limit**. A broken alias, a rotated key or a bad
  `api_base` fails every run without ever matching the usage-limit regex, so the one failure mode
  that had already eaten a day of backlog could never trigger recovery.

Now: `_litellm_probe` classifies on the **HTTP status** (`0` served / `1` usage-limited / `2`
broken) and leaves the reason in `state/.litellm_probe_detail`; `_ollama_alias_ok` validates the
real tier alias and caches the verdict for `OLLAMA_ALIAS_TTL` (default 3600s — about one token an
hour, not one per tick); a failing alias makes `ollama_capacity` report **`0 unavailable`** so
dispatch routes to Claude instead of feeding issues into a lane that cannot run them. The probe also
fires after `OLLAMA_FAIL_PROBE_AT` (default 3) consecutive plain failures.

Check it by hand:

```bash
# prints 0 (served) / 1 (usage-limited) / 2 (broken)
BASE=/home/lem/agent-pipeline . /home/lem/agent-pipeline/lib/capacity.sh
_litellm_probe lem-agent-tier2 ; cat /home/lem/agent-pipeline/state/.litellm_probe_detail
```

## Stale-claim reaper

`agent:working` is stamped when a run **starts**, and `select_next_issue` excludes it — so a run
that dies before opening a PR parks its issue in a state nothing else leaves. On 2026-07-29/30
sixteen issues accumulated there and the queue drained to zero while every tick logged
`Pipeline idle` with both lanes green: silence was indistinguishable from done.

`reap_stale_claims` runs once per tick (skipped under `DRY_RUN`) and returns an issue to
`agent:ready` only when **all** of these hold, so live work is never yanked out from under a run:

| Guard | Why |
|---|---|
| no OPEN PR linked (`pr_for_issue`) | a PR means the run got far enough to hand off |
| branch claim lock is free | a concurrent slot working it holds that `flock` |
| untouched for `STALE_CLAIM_MINUTES` (default 120) | label edits count as touches |
| not `needs-human` / `agent:blocked` | already parked deliberately |

Bounded by `STALE_CLAIM_MAX_REQUEUES` (default 3, counted in `state/requeue-<n>.count`, cleared
once a PR appears): past that the issue gets `needs-human` + `agent:blocked`, the owner is assigned
and a comment explains why, rather than cycling forever on whatever keeps killing the run. Emits
`issue_reaped` to PostHog (`action: requeued|parked`). Log lines are prefixed `REAPER:`.

> Stale `ai:*` routing labels are stripped on requeue, and the list is read **off the issue**, never
> hardcoded — `gh issue edit` rejects the *entire* edit if any named label is unknown to the repo,
> so one drifted name in a static list silently undoes the whole requeue. A hardcoded `ai:claude`
> (the real label is `ai:claude-subscription`) did exactly that during testing.

## Troubleshooting

- **Issues stuck in `agent:working` with no PR** → the reaper handles this within
  `STALE_CLAIM_MINUTES`. To force it now: `STALE_CLAIM_MINUTES=1 /home/lem/agent-pipeline/tick.sh`.
  If an issue keeps coming back, look for `REAPER: … parking for a human` and read the failing run
  in `logs/`.
- **A green PR never merges (and the log keeps saying "Merging")** → `main` merges through a GitHub
  merge queue, so `gh pr merge --auto` only *enqueues*, and it exits 0 even when the PR holds a
  queue entry GitHub already evicted (#1082 — #1067 sat 47h that way). The lane now reads the state
  back: look for `MERGE: PR #N is WAITING IN THE MERGE QUEUE` (fine) vs
  `NEITHER merged NOR in the merge queue (stall k/N)` (stuck). After `MERGE_STALE_TICKS` (default 3)
  it clears the dangling entry with `--disable-auto` and re-enqueues; to force that now, run one
  tick with `MERGE_STALE_TICKS=0`. The "merging" comment is keyed on the head SHA in
  `state/mergecomment-<pr>.sha`, so a stuck PR can never accumulate more than one per push.
- **The log says "WAITING IN THE MERGE QUEUE" every tick and the PR still never merges** → the
  queue is *taking* the PR and then *dropping* it: a `merge_group` check is failing, so GitHub
  evicts the entry a few minutes after each enqueue and the next tick re-enqueues (#1067 did this
  154 times). A read 20s after the request always shows a healthy live entry, so the lane also
  counts requests per head SHA in `state/mergeattempt-<pr>`: at `MERGE_QUEUE_STUCK_TICKS`
  (default 12, ~1h) it logs `STUCK IN THE MERGE QUEUE — N merge requests at head …` and the tick
  reports `failed`/`merge_queue_stuck`. `merge_queue_unmergeable` is the same story caught earlier,
  straight from the entry's own state. **Neither clears itself** — open the PR's `merge_group`
  check runs and fix what is failing there; a new push resets the budget.
- **Ollama lane never used** → check `state/ollama.state` (including `alias_ok` / `alias_ok_ts`),
  the gauge, and `OLLAMA_LANE_ENABLED`.
  `OLLAMA_PROBE=1` adds a real completion probe (more accurate). Pin `OLLAMA_CAPACITY_PCT=80` to
  force it healthy for testing.
- **Claude lane never used / always degraded** → `CLAUDE_CAPACITY_PCT=90` to override a stale
  estimate; clear `PAUSED_UNTIL` (`rm /home/lem/agent-pipeline/PAUSED_UNTIL`) if a usage-limit pause
  is stale.
- **LiteLLM `/v1/messages` 404 / model not found** → the `lem-agent-*` aliases aren't loaded:
  recreate the container (above). Verify with `curl -X POST http://127.0.0.1:4000/v1/messages
  -H "x-api-key: $LITELLM_MASTER_KEY" -H "anthropic-version: 2023-06-01" -H 'content-type:
  application/json' -d '{"model":"lem-agent-tier2","max_tokens":5,"messages":[{"role":"user","content":"ok"}]}'`.
- **Ollama cloud model times out** → `glm-5.2` (tier1) is a slow thinking model and
  intermittently times out; that's why it's opt-in (`agent:tier:1`) and the default is tier2
  (`kimi-k2.7-code`). LiteLLM's fallback chain should drop to tier2/tier3 automatically.
- **No PostHog events** → `secrets.env` must have `POSTHOG_API_KEY` + `POSTHOG_HOST`; the
  `capacity_preflight` event fires every tick — if it's absent, PostHog capture is failing
  (logged to the tick log as `[posthog] capture failed ...`). Check the host in `secrets.env`.
- **MCP research tool errors** → `tail logs/mcp-perplexity.log`; `PERPLEXITY_API_KEY` must be set
  in `/etc/lem/mcp-perplexity.env`. The tool returns a short error string rather than raising, so
  an agent run degrades gracefully.
- **VPS memory pressure** → LiteLLM is capped (compose `deploy.resources`) + the slice caps
  workers; lower `LITELLM_NUM_WORKERS` / `LITELLM_MEM_LIMIT` in `config.env` and recreate LiteLLM.
- **Routing decision log lines** → `[dispatch] lane=… model=… reason=…` and `[dispatch] claude=…%
  ollama=…% degraded=…` in `logs/tick-YYYYMMDD.log`.

## Files changed

| Path | Purpose |
|---|---|
| `tick.sh` | sources libs, preflight, `run_claude`→`run_lane`, degraded CAP/hold, `issue_queued`, `ISSUE_LABELS/PRIORITY` |
| `config.env` | routing knobs (thresholds, lane enable, tiers, LiteLLM workers/limits, capacity overrides) |
| `lib/posthog.sh` | server-side PostHog capture (reuses `/opt/lem/.env` keys) |
| `lib/labels.sh` | `ai:*` label bootstrap + apply |
| `lib/capacity.sh` | >50% gauge, rolling state, overrides |
| `lib/dispatch.sh` | preflight + `dispatch_lane` (the rule + parallel split + tier selection) |
| `lib/run_lane.sh` | lane execution, telemetry, outcome recording, labels, PR events |
| `mcp/perplexity_server.py` | FastMCP Perplexity research/fact_check server |
| `mcp/requirements.txt` | fastmcp dep |
| `mcp/mcp-config.json` | claude `--mcp-config` target (loopback HTTP) |
| `systemd/lem-agent.slice` | shared CPU/RAM ceiling for workers |
| `systemd/lem-mcp-perplexity.service` | persistent MCP service (in the slice) |
| `systemd/mcp-perplexity.env.template` | env file template (real file: `/etc/lem/mcp-perplexity.env`) |
| `secrets.env` | reused prod keys (mode 600, NOT committed) |
| (repo) `.litellm/config.yaml` | `lem-agent-*` Ollama cloud aliases + fallback chain |
| (repo) `docker-compose.yml` | LiteLLM gunicorn `--num_workers` + `deploy.resources` limits |
| (repo) `docker-compose.prod.yml` | LiteLLM `127.0.0.1:4000:4000` loopback publish + CPU/mem limits |
| `/opt/lem/.litellm/config.yaml`, `/opt/lem/docker-compose*.yml` | same edits applied to prod (additive; must also land via PR→release or the next release reverts them) |