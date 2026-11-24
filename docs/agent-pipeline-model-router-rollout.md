# Model router rollout (phase 4 of 4, #2562)

The router (`scripts/agent-pipeline/v2/lemd/route.py`) ships dark. This page is the order to turn it on and the one-flag way back. The full mechanics are in `scripts/agent-pipeline/docs/agent-pipeline-routing.md`, section "Harness parity, review replay and rollout".

## Before shadow

1. Run `scripts/agent-pipeline/v2/smoke_parity.sh <fresh worktree>` and keep the output. It proves, per harness, a graft index, one tool call, one graft MCP call, a terse caveman reply and valid JSON.
2. Run the review replay and read the report from `v2/review_replay.py`. It states numbers only.

## Steps

| Step | `LEM_ROUTER` | What changes |
|---|---|---|
| 1 | `shadow` (a few days) | The decision is logged (`router[shadow] ...`); no launch changes |
| 2 | `reviews` | `review` and `selfreview` follow the router; code modes unchanged |
| 3 | `full` | Every mode follows the router |

Moving past `shadow`, and moving the merge gate to Codex, are the owner's call after the replay. The recall bar is set in a PR comment.

## Rollback

Set `LEM_ROUTER=off` in `config.env` and restart `lem-agentd`. Absent or unrecognised also means `off`: today's routing, unchanged.
