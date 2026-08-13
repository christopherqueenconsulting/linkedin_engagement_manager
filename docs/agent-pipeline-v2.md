# Agent pipeline v2 — the scheduler daemon

The autonomous runner that takes a labelled GitHub issue to a merged PR. This file is the
**architecture and state-machine** half: what the daemon is, how an item moves, and which
GitHub field combinations it is defined for. It deliberately does not re-derive four things that
already have owners — read them alongside:

| For | Read |
|---|---|
| The label contract (who waits on whom, how to write an executable issue) | [`AGENT_WORKFLOW_PLAYBOOK.md`](AGENT_WORKFLOW_PLAYBOOK.md) |
| What each agent is told to do in each MODE | `scripts/agent-pipeline/RUNBOOK.md` |
| Claude-vs-Ollama lane routing and model tiers | [`agent-pipeline-routing.md`](../scripts/agent-pipeline/docs/agent-pipeline-routing.md) |
| The trust boundary and why a label is not an access control | [`contribution-security.md`](contribution-security.md) |

Operating commands live on the box in `scripts/agent-pipeline/v2/README.md`, which `install.sh`
copies there.

**Status: live since 2026-08-10.** `LEMD_SHADOW=0`, `V1_RETIRED` present, `lem-agentd` enabled.
v1 (`tick.sh`) survives only as a heartbeat-gated 15-minute failsafe.

---

## 1. Why v2 exists

v1 gave one cron tick to ONE unit of work. The figures below were measured from v1's
`tick-outcomes.ndjson` during the v2 design and are quoted from that analysis — **the log is on the
box, not in this repo, so they are not reproducible from a checkout**:

| Symptom | Measurement |
|---|---|
| Ticks spent re-polling GitHub for unchanged answers | **~75%** of dispatches |
| Hard ceiling on work, regardless of `MAX_AGENTS=5` | 288 units/day (one per 5-min tick) |
| Concurrency actually used | slot ≥2 on **39 of 2,456** ticks |
| Worst single incident | one wedged PR consumed **45 of 62** ticks in 6 hours |

Only the 288/day ceiling is derivable from the repo (`capacity.py`); treat the rest as the recorded
rationale for building v2, not as live metrics. The thing being polled is fast — PR CI ~3 min, merge
queue ~3.8 min — so the pipeline was never GitHub-bound, it was tick-bound. v2 keeps every guard v1
earned and changes only *when* work runs.

---

## 2. The loop

One process, one loop. `Daemon.tick()` (`v2/lemd/daemon.py`) runs these in order, and the order is
load-bearing:

```
heartbeat        write state/lemd.heartbeat        (the watchdog's liveness signal)
collect          reap finished runs                 ← runs even while PAUSED, or a pause
                                                      would strand claims and branch locks
── if PAUSED: stop here ──
drain_events     webhook rows  → items.dirty=1      (≤200 per pass)
reconcile        GitHub labels → the queue          (600s; 120s only if silent AND drifting)
refresh_usage    subscription meter → state/usage.json
observe_dirty    changed items → decide()           (≤25 per pass)
sweep_ttls       expired waits → decide()           (≤25 per pass)
act              fill both pools with what decide() already decided
```

Then it sleeps `max(1, min(60, next_wake, next_reconcile))`. **A waiting item costs nothing** —
that is the entire economy change. An item awaiting CI, review, the merge queue or a human is not a
candidate for anything until an event marks it dirty or its TTL fires.

**Three wake paths**, each a backstop for the one above:
1. **Webhook** (`lemd.receiver`, port 8420) — the fast path. HMAC-verified, deduped on
   `X-GitHub-Delivery`, writes an event row and `kv:last_webhook_at` in one transaction.
2. **Reconcile** — re-derives the queue from GitHub labels on a timer, so a missed delivery costs
   latency and never work.
3. **Watchdog** (`lem-agentd-watchdog.timer`, every 15 min) — restarts the daemon when the unit is
   dead **or** the heartbeat is older than 600s. Liveness and freshness are separate questions; a
   wedged process passes the first and fails the second.

Degraded reconcile (120s) requires **both** webhook silence past `LEMD_WEBHOOK_STALE_SECONDS` **and**
drift found by the last reconcile. Silence alone is not evidence: it reads identically for a broken
event path and a quiet repository, and polling 5× harder on a quiet repo is backwards.

Events never decide anything. They only say *"this item changed, look again"*, which is what makes a
duplicate or out-of-order delivery harmless.

---

## 3. The item state machine

```mermaid
stateDiagram-v2
    [*] --> ready: reconcile / webhook
    ready --> claimed: claim_item (atomic, branch-unique)
    claimed --> running: process spawned
    running --> ready: collect (rc 0, EX_BUDGET, EX_BUSY, EX_SETUP, kill)
    running --> ready: reap adopted orphan (RC_VANISHED)
    running --> parked: collect (EX_TRUST)
    ready --> awaiting_ci: decide (ci_running / checks_unknown / any *_unreadable)
    ready --> awaiting_review: decide (work_in_flight)
    ready --> awaiting_queue: decide (auto_merge_armed / in_merge_queue)
    ready --> parked: decide (human_hold / draft)
    ready --> ignored: decide (not_admissible / not_ready)
    parked --> abandoned: answer, but laps exhausted
    abandoned --> ready: agent:abandoned removed
    ignored --> ready: relabelled, then reconciled
    awaiting_ci --> ready: event or TTL
    awaiting_review --> ready: event or TTL
    awaiting_queue --> ready: event or TTL
    parked --> ready: owner answer (ACT_UNPARK)
    ready --> merged: decide (state MERGED)
    ready --> closed: decide (state CLOSED)
    merged --> [*]
    closed --> [*]
```

`claimed` and `running` are **active states**: `upsert_item` refuses to move an item out of them, so
only the run lifecycle (`collect`, crash recovery) can, which makes every such transition auditable.

The claim is not a lock but an **index**: `items_active_branch` is a partial unique index over
`branch WHERE state IN ('claimed','running')`, so "two workers on one branch" is unrepresentable
rather than merely guarded.

| State | Meaning | Leaves on |
|---|---|---|
| `ready` | dispatchable, or awaiting a decision | act(), or the next observation |
| `claimed` | won the claim, not yet spawned | spawn, or `startup_recover` |
| `running` | a child process is alive | collect(), or `wake_at = now + max(60, timeout)` |
| `awaiting_ci` | CI running, or checks unreadable | event, or `LEMD_TTL_CI` (1800s) |
| `awaiting_review` | work in flight elsewhere | event, or `FIRST_REVIEW_TIMEOUT_SECONDS` (3600s) |
| `awaiting_queue` | auto-merge armed or in the queue | event, or `LEMD_TTL_QUEUE` (900s) |
| `parked` | the owner's, not the pipeline's — a question WAS asked | an owner answer, or `LEMD_TTL_PARKED` (21600s) |
| `ignored` | not the pipeline's business; nobody was asked | a relabel, noticed by `reconcile` |
| `abandoned` | parked for the same reason too many times; the pipeline stopped asking | removing `agent:abandoned` — laps cleared, back on the queue |
| `merged` / `closed` | terminal | — |

---

## 4. The decision table

`observe.decide()` is **pure** — no network, no clock, no database. Every input arrives in a
`Snapshot`, so every transition is testable exactly, including the ones that only happen when GitHub
is lying. This table is that function, in evaluation order. Order encodes priority.

`tests/unit/test_agent_pipeline_v2_decision_table.py` asserts the **reason strings** here and in
`observe.py` are the same set, in both directions — a new branch without a row fails, and a row that
outlived its branch fails. It does **not** yet check the condition, action, order or wake columns, so
those four are reviewed by eye and are where this table will rot first.

### Terminal facts, then unreadability, then admission

| # | Condition | Action | Reason | Wake |
|---|---|---|---|---|
| 1 | GitHub state `MERGED` | close | `merged` | — |
| 2 | GitHub state `CLOSED` | close | `closed_unmerged` | — |
| 3 | any required read failed | none | `github_unreadable` | 300s |
| 4 | not admissible | none → **ignored** | `not_admissible:{fork_pr,release_pr,no_agent_label}` | **never** |

**Unreadable is a decision to do NOTHING**, never a decision to proceed — v1 once merged on an
unreadable state (#1082). Admission is by LABEL and PROVENANCE, never by author: excluding
"Dependabot PRs" by author would retire the depfix lane, which exists to run an agent ON a
Dependabot PR. `admissible()` also has an `unreadable` branch, but row 3 shadows it, so that reason
is **unreachable**.

**`ignored` is not `parked`, and the distinction is the point.** `parked` means the pipeline stopped
and ASKED someone: a Decision Comment, the hold labels, an assignee, auto-merge disarmed. These
branches did none of that — they returned `ACT_NONE`, so no action ran — yet they wrote `parked`
anyway, claiming a question nobody had posed. For a fork PR and a release-please PR silence is
genuinely right; they are not ours to comment on. So they say `ignored` instead. An ignored item is
not terminal: label it properly and the next observation picks it up.

There is deliberately **no `ACT_PARK`**. Escalation happens at DISPATCH — `act()` finds the ledger
spent and queues `park.sh`. A constant existed here for months, was never returned, and left a dead
branch in the daemon; if `decide()` ever needs to escalate, it is re-added and wired in one change.

### The owner's hold outranks every lane

| # | Condition | Action | Reason | Wake |
|---|---|---|---|---|
| 5 | hold label on a PR with auto-merge **armed** | disarm | `human_hold_armed` | — |
| 6 | hold label + actionable answer, **and this reason has parked `LEMD_MAX_PARK_LAPS` times** | abandon | `park_laps_exhausted` | — |
| 7 | hold label + actionable answer | unpark | `owner_answered` | — |
| 7 | hold label + `hold`/`question` answer | none → parked | `human_hold:{verdict}` | 6h |
| 9 | hold label + an answer already spent | none → parked | `human_hold:answer_already_routed` | 6h |
| 10 | hold label, no answer | none → parked | `human_hold` | 6h |

Row 5 sits above the answer deliberately. Only `park.sh` ever ran `--disable-auto`, so a hold a
HUMAN applied was honoured here and ignored by GitHub, which merged the PR when its gate cleared.
Un-parking one observation later costs a single pass; leaving an armed hold costs a merge nobody
authorised, and that cannot be undone.

Row 6 is the pipeline giving up. An item parked for the SAME reason `LEMD_MAX_PARK_LAPS` times
(default 3) has had that question asked and answered and asked again, and the un-park's ledger reset
is what starts each next lap — so it is tested at the UN-PARK, where the loop actually turns, not at
the park. A lap is keyed on (item, reason, head): a re-park at the same head is the same park being
re-observed, which is why the 6-hourly re-decision does not inflate the counter.

`abandon` is terminal for the PIPELINE and for nothing else. It never closes an issue or a PR — that
is a judgement about the work, not about the runner's ability to progress it — and removing the
`agent:abandoned` label revives the item with its lap history cleared. It is surfaced by `status.sh`
as a warning, because an item that stops asking is a worse failure than one that asks too often if
nothing says it exists.

`HOLD_LABELS = {needs-human, agent:blocked}`. A held item carrying no `agent:*` label is still
admitted (row 4 makes an exception) — otherwise an item parked with only `needs-human` would be
unanswerable, and the owner's reply to their own issue would be read, classified, and discarded.

**Ambiguity never starts a build.** A reply that leads with `1B` and then says "but don't merge until
Friday" reads as `hold` and stays parked.

### Issue lanes

| # | Condition | Action | Reason | Wake |
|---|---|---|---|---|
| 11 | `agent:ready`, work state unreadable | none | `start_work_state_unreadable` | 600s |
| 12 | `agent:ready`, work exists, open PR **or linkage unreadable** | none | `start_already_produced_work` | 1h |
| 13 | `agent:ready`, work exists, **no PR** | dispatch `start` | `stranded_branch_no_pr` | — |
| 14 | `agent:ready`, no work | dispatch `start` | `issue_ready` | — |
| 15 | `agent:working`, unreadable | none | `working_claim_state_unreadable` | 600s |
| 16 | `agent:working`, work exists, open PR **or linkage unreadable** | none | `working_claim_has_work` | 1h |
| 17 | `agent:working`, work exists, no PR | dispatch `start` | `stranded_branch_no_pr` | — |
| 18 | `agent:working`, no work | dispatch `start` | `working_claim_stranded` | — |
| 19 | neither label | none → **ignored** | `issue_not_ready` | never |

Rows 11–12 are one question asked two ways. "Did anything leave the box" is right for *must I avoid
forking this*; it is wrong for *will anyone ever finish it*. A branch with an open PR is in flight; a
branch with no PR is a `start` that pushed and then died — resumable, and re-dispatching it resumes
on the existing branch. Linked PRs are consulted **before** the `feature/claude-issue-N` convention,
because PR #1302 carries issue #1301 on a differently-named branch and a convention-only lookup would
call that live PR stranded.

### PR lanes, cheapest-to-unblock first

| # | Condition | Action | Reason | Wake |
|---|---|---|---|---|
| 19 | draft (unheld — a held draft is row 5-9) | none → awaiting_review | `pr_is_draft` | 6h |
| 20 | `mergeStateStatus == DIRTY` | dispatch `rebase` | `conflicts_with_main` | — |
| 21 | lane label, by declared priority: `agent:revise` | dispatch `revise` | `owner_requested_changes` | — |
| 22 | …then `agent:phasefix` | dispatch `phasefix` | `phase_scope_untracked` | — |
| 23 | …then `agent:depfix` | dispatch `depfix` | `dependabot_ci_failure` | — |
| 24 | …then `agent:docfix` | dispatch `docfix` | `lint_gate_failure` | — |
| 25 | `mergeStateStatus` is `UNKNOWN` or `""` | none | `merge_state_unknown` | 120s |
| 26 | `mergeStateStatus` outside the enum | none | `merge_state_unrecognised` | 300s |
| 27 | auto-merge armed | none | `auto_merge_armed` | 15m |
| 28 | checks unreadable | none | `checks_unknown` | 300s |
| 29 | a required check failed | dispatch `fix` | `required_checks_failing` | — |
| 30 | checks pending, or zero checks | none | `ci_running` | 30m |
| 31 | unresolved Copilot threads | dispatch `review` | `unresolved_review_threads` | — |
| 32 | no review at/after the head | dispatch `selfreview` | `no_fresh_review` | — |
| 33 | already in the merge queue | none | `in_merge_queue` | 15m |
| 34 | green, reviewed, threads clear | **merge** | `gate_satisfied` | 15m |

Row 26 sits above row 27 deliberately. An armed PR reporting `BLOCKED` is the normal, healthy state
of a PR waiting on required checks; without this the ladder fell through to `gate_satisfied` on every
pass and burned the per-head merge budget in three minutes (measured on #1295).

Row 31's freshness is **stricter than v1's**: a review must be at or after the head commit. Being
wrong in this direction costs one extra selfreview; being wrong in the other merges code no reviewer
saw. **One exception, and it errs permissive**: when the head's `committedDate` is unreadable,
`review_state` falls back to "any review counts", however stale — refusing every PR on an unreadable
date would wedge the gate entirely. Row 29 treats zero checks as pending, not green.

---

## 5. The GitHub field matrix

The point of this section is the **Undefined** column. Every cell that is not handled is a cell where
behaviour is incidental.

| `mergeStateStatus` | Handled? | What happens today |
|---|---|---|
| `CLEAN` | ✅ | falls through to the checks/review ladder |
| `DIRTY` | ✅ | row 19 → `rebase` |
| `BLOCKED` | ✅ | proceeds; it is the normal state of a PR waiting on a required check, and the ladder reads those directly |
| `UNSTABLE` | ✅ | proceeds and can reach `gate_satisfied`. Correct **because** `checks_for` filters to required contexts, so non-required red is mergeable — now recorded rather than accidental |
| `BEHIND` | ✅ | proceeds, deliberately: `main` does not require branches to be up to date (`strict` is false) and the merge queue builds against the queue head, so a rebase would spend a model session on something GitHub does for free |
| `UNKNOWN` / `""` | ✅ | waits 120s (row 24). Was the #1082 shape — an unreadable field read as a healthy one |
| `HAS_HOOKS` | ✅ | proceeds, named |
| anything outside the enum | ✅ | waits 300s (row 25) — the enum is closed, so a new member means the world changed |

**Label combinations**

| Combination | Today |
|---|---|
| two lane labels (e.g. `agent:revise` + `agent:docfix`) | ✅ resolved by `LANE_LABEL_PRIORITY`; the waiting lane is recorded in `details.lanes_pending` and runs once the first clears its own label |
| `agent:merge-parked` | ✅ decided: `unpark.sh` keeps REMOVING it for rollback parity, and nothing writes it — v2 has no separate merge park, so writing it would add a concept the daemon lacks |
| hold label + lane label | hold wins (row 5-8), lane label inert until un-parked |
| hold label + **auto-merge armed** | ❌ **the daemon holds and GitHub merges anyway** — see §7 |

**Draft × everything.** `is_draft` is checked FIRST, above `DIRTY` and above the armed-auto-merge
wait, so a draft is never rebased and an armed draft is never recognised.

| | Held | Not held |
|---|---|---|
| Draft | parked; the answer lane can release it | ✅ `awaiting_review` with `wait_reason=draft`, released by the `ready_for_review` delivery — it is the human's own state, not a park nobody placed |
| Draft + `DIRTY` | never rebased | never rebased |
| Draft + armed | hold honoured, arm untouched | arm never recognised |

**Merge queue × everything else — the largest omission.** `queue_state` is read **last**, below
`fix`, `review` and `selfreview`. So a PR that is *already in the merge queue*:

| Queued PR also has… | What happens | Consequence |
|---|---|---|
| a failed required check | dispatches `fix` | the fix **pushes a commit, ejecting it from the queue** |
| an unresolved Copilot thread | dispatches `review` | same — the reply/resolve pushes |
| a stale review | dispatches `selfreview` | same |
| checks pending | reports `ci_running`, not `in_merge_queue` | an operator reading the state is misled |

Row 32 reads as though queue membership is checked early. It is only checked on the fully-green path.

**Lane labels on the wrong kind**

| Combination | Today |
|---|---|
| `agent:revise`/`depfix`/`docfix` on an **ISSUE** | never read — those branches are PR-only, so the issue falls to `issue_not_ready`. `reconcile` queries them for PRs only, so it does not even enter the queue |
| `agent:ready` on a **PR** | inert; it satisfies the `agent:` admission test and nothing else |
| fork PR / release-please PR | `not_admissible` → DB `parked`, `wake_in=None`, **and no GitHub side effect at all** (see §4 ⚠️). Invisible and permanent |

---

## 6. Budgets, interrupts, and how work ends

**The ledger is the ONE budget store** (`lib/ledger.sh`, one TSV per item). Charged at DISPATCH,
before the run starts, so a timeout, a crash and a max-turns exhaustion all consume budget — the
counters it replaced charged only on success shapes, which is exactly backwards. `policy.py` reads it
and never writes.

| Mode | Pool | Budget | Timeout |
|---|---|---|---|
| `start` | agent | 2 | 2700s |
| `fix` | agent | 4 | 1200s |
| `review` | agent | 3 | 900s |
| `selfreview` | agent | 3 | 1200s |
| `rebase` | agent | 2 | 1200s |
| `revise` | agent | 2 | 1500s |
| `depfix` | agent | 3 | 1200s |
| `docfix` | agent | 3 | 600s |
| `merge` | gh | **3, but not from this table** — see below | 180s |
| `park` / `unpark` | gh | **none — they charge no ledger at all** | 180s |
| `phasefix` | agent | 2 | 600s |

Timeouts stretch up to 1.5× at full pool occupancy.

The merge budget is **three independent 3s that agree by coincidence**, and this is worth knowing
before changing any of them. `MODE_BUDGET["merge"]` is never read for the merge action — the child is
dispatched as `merge_enable`, so `budget_for` misses and returns `DEFAULT_BUDGET`. `merge_enable.sh`
ignores `LEMD_BUDGET` entirely and enforces its own `MERGE_MAX_REQUEUES` (default 3), which is also
where the per-head keying actually lives. `PER_HEAD_MODES` in `policy.py` has **zero consumers**:
`policy.exhausted()` is only ever called with the default reset key. The *intent* — a new head is a
new question for the merge queue, and elsewhere a per-head key would let an agent refill its own
meter by pushing a commit — is real and correct; the wiring is not.

**Exit codes carry meaning** — collapsing them is how v1 retried a refusal every five minutes:

| Code | Means | Daemon does |
|---|---|---|
| `EX_TRUST` 70 | provenance refused or unreadable | park, 6h TTL — answered by a human, not a timer |
| `EX_BUDGET` 71 | this (item, mode) is spent | park `{mode}_exhausted` |
| `EX_BUSY` 72 | another claimant holds the branch | retry in 120s |
| `EX_SETUP` 73 | environment not preparable | ⚠️ treated as a plain failure — no backoff |
| `RC_KILLED` -9 | we stopped it on its deadline | falls to the generic branch → re-observe. **No refund**: a run that used its whole ceiling and produced nothing is what a budget is for |
| `RC_VANISHED` -99 | its process was already gone | closed inside `_reap_adopted`, which `collect()` never sees → re-observe, **and the charge is refunded** — we ended it, so it measured nothing. Capped at one refund per (item, mode) |

`RC_KILLED` and `RC_VANISHED` are separate because collapsing them cost a real misdiagnosis: nine
runs closed `-9` with a ~675s mean read exactly like a timeout problem, and were adopted orphans from
16 daemon restarts.

**How work ends.** A park is the pipeline saying "I stopped". The ONE way out is an owner reply to
the Decision Comment, classified by `lemd/answers.py`: `answer`/`directive` un-park, `hold`/`question`
stay parked. An answer is spent once (`items.last_comment_id`), written only after the un-park
succeeds so a failed action retries.

Un-parking **resets the ledger** — the owner's answer is the statement "the world changed, try
again" — and routes the work back by WHY it stopped. Only a park that asked a question
(`needs_human`) goes to `agent:revise`, because that lane outranks the merge ladder and is only
correct when there is feedback to apply. A budget park goes back to `agent:working`: the ledger
reset is the fix, and sending it to revise spends two model sessions on an empty lane and re-parks.

The remaining gap is that nothing counts how many times this has happened — see §7.

---

## 7. Intended state — what is NOT true yet

Everything above is what the code does today. This section is what it *should* do, each item with its
issue. It exists so the gaps are visible rather than discovered one incident at a time.

| Gap | Why it matters | Issue |
|---|---|---|
| **A queued PR gets pushed out of the queue** by `fix`/`review`/`selfreview`, because `queue_state` is read last (§5) | the queue is re-entered from scratch each time | #1388 |
| **An issue whose only linked PR was closed unmerged waits for ever** — `_open_pr_for_issue` returns True for any linked ref, because the API's refs carry no `state` | needs `ACT_PARK` re-added, so split out | #1405 |
| **`PER_HEAD_MODES` and `MODE_BUDGET["merge"]` have no consumers** (§6) — the merge bound lives entirely in `merge_enable.sh` and works | left as-is: moving it would relocate a functioning guard for no behaviour change | — |
| **v2 has no phase guard.** v1 routed a PR closing a phased issue with untracked later phases to `MODE=phasefix`, escalating to the owner only after repeated attempts (`tick.sh:715-745`); v2 has no equivalent and merges it | a shipped issue can silently lose its remaining scope | #1396 |
| **The sync timer is not enabled.** The mechanism is shipped and tested; enabling it needs `pipeline-selfmod-gate` to be a required check first, which is an owner-only branch-protection change | until then, deploys stay manual and `main` can silently outrun the box | #1397, #1398 |

---

## 8. Deploy and operate

**The pipeline is not in the Docker image.** `scripts/agent-pipeline/` is the source; `install.sh`
copies it to `/home/lem/agent-pipeline` on the box. The release train ships the *app*, never the
runner — so a merged pipeline change is not live until someone syncs it.

```
install.sh              first install (also touches PAUSED)
install.sh --sync       update only files the box has not edited
install.sh --sync --force   overwrite box edits, after reading the diff

sudo systemctl restart lem-agentd lem-agent-webhook   # BOTH — see below
```

**Restart both units, always.** They both load the `lemd` package, and only `lem-agentd` used to be
named in this procedure — so the receiver ran 23-hour-old code through nine merged changes before
anyone noticed (#1412). It surfaced as `kv.schema_version` refusing to advance: the receiver calls
`db.connect()` per request, so every delivery rewrote the version from its stale module.

**Automated (`scripts/agent-pipeline/sync.sh`, shipped but not enabled).** A `lem`-owned systemd
timer pulls `main` into a machine mirror at `/home/lem/agent-pipeline-src`, runs `install.sh --sync`,
restarts both units, and verifies the daemon came back — restoring a snapshot if it did not. Pull
rather than CI-push because a push needs a standing credential that executes as the uid holding
`secrets.env`, the App token and every worktree, while a pull needs nothing inbound. `state/SYNC_HOLD`
stops it; `--force` is never used, because `--sync`'s refusal on a box-edited file means someone is
mid-debug on that machine.

⚠️ **The timer is deliberately not enabled.** Auto-deploy closes a loop: agents merge their own PRs
(`required_approving_review_count` is 0), so a merged pipeline change would reach the box unattended.
`scripts/pipeline_selfmod_gate.py` is the control, and it only works once the workflow calling it is
a REQUIRED check on `main` — an owner-only branch-protection setting (#1397).

`--sync` refuses a file whose on-box hash differs from both the repo and the recorded manifest — a
box-local edit is never silently overwritten.

| Unit | Runs as | Does |
|---|---|---|
| `lem-agentd.service` | `lem` | the scheduler. `KillMode=process` so 45-minute agent children survive a restart |
| `lem-agent-webhook.service` | `lem` | the receiver, hardened, `MemoryMax=256M`, secret from a root-owned file |
| `lem-agentd-watchdog.timer` | root (see note) | liveness + heartbeat freshness every 15 min |
| `lem-gh-token.timer` | root | mints the App installation token every 45 min; root holds the key |
| `lem-agent.slice` | — | the CPU/memory envelope (`CPUQuota=300%`, `MemoryMax=3G`). **Box-only — not in the repo** |

```bash
systemctl status lem-agentd            # is it alive
scripts/agent-pipeline/status.sh       # what is it doing
tail -f logs/lemd.log                  # the loop
jq . logs/lemd-decisions.ndjson | tail # every decision, with its reason
sqlite3 v2/state/queue.db 'select kind,number,state,pending_mode from items where state!="merged"'
touch PAUSED                           # stop everything (shared with v1)
v2/rollback.sh                         # hand dispatch back to v1
```

⚠️ The watchdog unit sets `User=root`, but `watchdog.sh` documents itself as running as `lem` and
uses `sudo -n systemctl restart` on that basis. One of the two is stale: if the unit is right the
`sudo -n` path and its failure branch are dead code; if the script is right the unit drifted. Resolve
before touching either half.

**Pause vs retire.** `PAUSED` stops both runners. `V1_RETIRED` demotes v1 to the failsafe cron and is
what `cutover.sh` writes — deliberately not `PAUSED`, because `tick.sh` exits unconditionally on
PAUSED and that would disable the failsafe too.
