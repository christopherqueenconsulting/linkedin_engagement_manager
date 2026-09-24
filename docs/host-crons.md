# Host crons — inventory and the deployed-tag rule

Every scheduled job that runs on the VPS host (not in Celery beat), for both users, and which
revision of the repo each one executes. Issue #2109.

## The rule: a host cron runs the DEPLOYED tag

`scripts/` is not baked into the image, so a host cron executes whatever checkout its crontab line
names. Those checkouts are ordinary clones nobody pulls: on 2026-09-24 `perf_snapshot.sh` and
`weekly_sdui_drift_check.sh` ran from the dev checkout `/home/lem/linkedin_engagement_manager`,
31 commits behind `main`. A fix could merge, ship in a release and still never reach the cron
that needed it — #2085 was one instance, and #2086 pinned only that one sweep's probe.

So every `lem` cron that runs a repo script goes through **`scripts/run_at_deployed_tag.sh`**:

1. reads the tag `scripts/deploy.sh` last converged prod onto — `/opt/lem/.last_good_tag`;
2. `git fetch --tags`, then adds a **fresh detached worktree** of that tag;
3. runs the script from it with the caller's cwd and arguments, exporting `LEM_DEPLOYED_TAG`;
4. removes the worktree on exit and propagates the script's exit code.

It **fails closed**: an unreadable, malformed or unresolvable tag, or a script that does not exist
at that tag, runs nothing and exits 1. Every run logs `run <script> at <tag> (<sha>)` to
`/home/lem/logs/run_at_deployed_tag.log`, so which revision did the work is readable afterwards.

The wrapper cannot pin itself — cron executes the clone's copy. It is small and changes rarely; a
copy that differs from the deployed tag logs a `WARNING` every run. `git -C /home/lem/cron-runner/repo
pull` clears it.

The clone is **`/home/lem/cron-runner/repo`**, a dedicated clone of the repo. Only its tags and
worktree list ever change; its working tree is never reset or checked out, and it is not the dev
checkout, so `worktree_cleanup.sh` never sees the per-run worktrees.

### Why the tag and not `origin/main`

The jobs talk to the running stack — they `docker exec` into its containers, read its DB, pipe a
probe into its Selenium worker. The tag is the revision that stack is built from, so the script
and the image agree. `origin/main` can be up to one release window (~6h, `docs/zero-downtime-deploys.md`)
ahead of it; the older `model-check` / `li-version-check` / `error-issues-cron` wrappers reset to
`origin/main` for that reason alone and are superseded. Jobs that open PRs against `main`
(`weekly_model_check.sh`, `weekly_linkedin_version_check.sh`) still branch from `origin/main`
internally — only the orchestration runs at the tag.

`weekly_sdui_drift_check.sh` defaults its probe ref to `$LEM_DEPLOYED_TAG` when wrapped, so the
probe matches the image it is piped into (it was `origin/main` under #2086; run by hand it still is).

## Inventory

### `lem` (`crontab -l` as lem)

| Schedule (UTC) | Job | Revision |
|---|---|---|
| `30 23 * * *` | `scripts/perf_snapshot.sh` → `/home/lem/perf-tracking/metrics.jsonl` | deployed tag |
| `40 6 * * 1` | `scripts/weekly_sdui_drift_check.sh` (env `SDUI_PROBE_PROFILE_URL=…`) | deployed tag |
| `0 9 * * 0` | `scripts/weekly_model_check.sh` | deployed tag |
| `30 9 * * 0` | `scripts/weekly_linkedin_version_check.sh` | deployed tag |
| `30 8 * * *` | `scripts/error_to_issues.sh` | deployed tag |
| `0 10 * * 0` | `scripts/todo_to_issues.sh` | deployed tag |
| `0 7 * * 1` | `scripts/worktree_cleanup.sh`, cwd `/home/lem/linkedin_engagement_manager` | deployed tag |
| `*/15 * * * *` | `/home/lem/agent-pipeline/tick.sh --failsafe` | **not a repo script** — the pipeline install, deployed by its own path (`docs/agent-pipeline-v2.md`) |

`worktree_cleanup.sh` sweeps the repo it is **run in** (cwd), not the one it lives in, which is why
its line keeps the `cd` and the wrapper keeps the caller's cwd.

### `deploy` (`sudo crontab -u deploy -l`)

| Schedule (UTC) | Job | Revision |
|---|---|---|
| `0 3 * * *` | `cd /opt/lem && ./scripts/backup.sh >> logs/backup.log` | deployed tag — `/opt/lem` **is** the checkout `deploy.sh` moves to each release tag |

## Install (owner, once)

```sh
git clone https://github.com/christopherqueenconsulting/linkedin_engagement_manager.git /home/lem/cron-runner/repo
crontab -l > /home/lem/logs/crontab.bak-$(date -u +%Y%m%d)
crontab -e
```

and replace the repo-script lines with:

```cron
30 23 * * * /home/lem/cron-runner/repo/scripts/run_at_deployed_tag.sh scripts/perf_snapshot.sh
40 6 * * 1 SDUI_PROBE_PROFILE_URL=https://www.linkedin.com/in/cassidydunn/ /home/lem/cron-runner/repo/scripts/run_at_deployed_tag.sh scripts/weekly_sdui_drift_check.sh
0 9 * * 0 /home/lem/cron-runner/repo/scripts/run_at_deployed_tag.sh scripts/weekly_model_check.sh
30 9 * * 0 /home/lem/cron-runner/repo/scripts/run_at_deployed_tag.sh scripts/weekly_linkedin_version_check.sh
30 8 * * * /home/lem/cron-runner/repo/scripts/run_at_deployed_tag.sh scripts/error_to_issues.sh
0 10 * * 0 /home/lem/cron-runner/repo/scripts/run_at_deployed_tag.sh scripts/todo_to_issues.sh
0 7 * * 1 cd /home/lem/linkedin_engagement_manager && /home/lem/cron-runner/repo/scripts/run_at_deployed_tag.sh scripts/worktree_cleanup.sh >> /home/lem/logs/worktree_cleanup.log 2>&1
```

Only after the release carrying `scripts/run_at_deployed_tag.sh` is deployed — before that, every
line refuses with `does not exist at <tag>`. The old `/home/lem/{model-check,li-version-check,
error-issues-cron}` clones can be removed once a week of runs shows up in the log.

## Verify

- `crontab -l` — every repo-script line starts with `/home/lem/cron-runner/repo/scripts/run_at_deployed_tag.sh`.
- `grep ' at v' /home/lem/logs/run_at_deployed_tag.log | tail` — each run names the tag in
  `/opt/lem/.last_good_tag` at the time.
- `tail -7 /home/lem/perf-tracking/metrics.jsonl` — `"margin"` is an object, not `null`, on every
  line. It was null on 48 of 63 lines because the block exec'd into `web_app`, the nginx front door
  with no python; it now targets the active `web_api_<color>` from `/opt/lem/.active_color`, and a
  null line records the container's last stderr line in `snapshot.log`.
