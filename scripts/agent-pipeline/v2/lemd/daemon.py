"""The scheduler daemon.

One loop: drain events, re-observe what changed, expire TTLs, then act within the caps. It replaces
v1's model of "one cron tick does one thing and exits", whose real ceiling was one unit of work per
5 minutes regardless of how many slots were configured.

Shadow mode (`LEMD_SHADOW=1`, the default) runs the entire loop and logs every decision it WOULD
take without touching GitHub or spawning anything. That is the migration's safety net and its
acceptance evidence: the decision stream can be diffed against what v1 actually did before anything
is allowed to act.

Dispatch itself is intentionally NOT here yet — this PR lands the loop, the observation cycle and
the wait-state economy. Agent execution arrives next, so that a daemon which is running but
mis-deciding can be caught while it is still incapable of doing anything.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import signal
import time
from typing import Any

from . import capacity, db, dispatch, github, observe, policy, spend
from .config import Config, load

LOG = logging.getLogger("lemd")

#: The scheduler sleeps at most this long even with nothing due, so a missed wake-up degrades to a
#: short delay rather than a stall.
MAX_SLEEP = 60

#: The gh ACTION name for arming auto-merge, which is NOT the item's `pending_mode` for it.
#: `_launch` dispatches `dispatch_gh(action="merge_enable", ...)` and `_spawn` records `mode=action`,
#: so a child carries `merge_enable` while the item carried `merge`. `collect()` tested for `merge`
#: and therefore never took its own success branch: every armed PR fell into the generic tail and
#: was re-observed with `dirty=1`, which is exactly the re-observation loop the branch was added to
#: prevent (#1295). Naming it once is the fix; the two spellings are a fact about the dispatch API,
#: not something to remember.
GH_MERGE_ACTION = "merge_enable"

#: Events that can only ever mean a PR. `issues` can only mean an issue. Everything else —
#: `issue_comment` above all — is AMBIGUOUS: GitHub delivers PR comments as `issue_comment` with
#: only an `issue` object in the payload, so those are resolved against the queue instead.
PR_ONLY_EVENTS = frozenset({
    "pull_request", "pull_request_review", "pull_request_review_thread",
    "check_suite", "merge_group",
})


#: What the hold branch reports when an item carries a hold label. True, and less useful than the
#: reason the item was ACTUALLY parked for.
GENERIC_PARK_REASON = "needs_human"


def _keep_specific_park_reason(existing: str | None, incoming: str | None) -> str | None:
    """Do not let the generic hold reason overwrite a specific one.

    `park.sh` parks a PR as `selfreview_exhausted` and applies the hold labels. The very next
    observation sees those labels, takes the hold branch, and reports `needs_human` — which the
    generic write then stored over the top. By the time an owner answered, the only reason left was
    the generic one.

    That silently defeated #1389: the un-park routes by park reason, and `needs_human` means "a
    question was asked, apply the answer" → `agent:revise`. So a budget park still went to the
    revise lane with no feedback to apply, spent its two runs and re-parked — the exact treadmill
    that change was written to end, still live because the evidence it routes on had been erased one
    observation after it was recorded.
    """
    if incoming == GENERIC_PARK_REASON and existing:
        return existing
    return incoming


def _item_mode(child_mode: str) -> str:
    """The mode as the ITEM knows it, given the mode a CHILD was spawned with.

    They differ for exactly one action, and only because `dispatch_gh` names children after the
    script it runs. Without this an exhausted merge parks as `merge_enable_exhausted`, which matches
    nothing an operator or a budget lookup would search for.
    """
    return "merge" if child_mode == GH_MERGE_ACTION else child_mode


class Daemon:
    """Owns the queue, the loop, and the decision to act or merely observe."""

    def __init__(self, cfg: Config) -> None:
        """Open state and prepare the loop (no I/O against GitHub yet)."""
        self.cfg = cfg
        self.conn = db.connect(cfg.db_path)
        self.sup = dispatch.Supervisor(cfg, self.conn)
        self._stop = False
        self._last_reconcile = 0.0
        self._decisions = 0
        self._degraded = False
        self._last_usage_probe = 0.0
        #: (kind, number) -> the gate that last refused this item, so a standing refusal is
        #: recorded ONCE rather than on every pass. Under a hold with a 48-issue backlog the
        #: undeduped form would write ~40 rows a pass for ever, which is the volume problem that
        #: made the per-item hold log line unreadable in the first place.
        self._refused: dict[tuple[str, int], str] = {}
        #: (kind, number) -> the answer id an in-flight un-park is spending. Written to
        #: `items.last_comment_id` only when that action succeeds, so a failed un-park retries on
        #: the next observation instead of consuming the owner's reply.
        self._answer_ids: dict[tuple[str, int], str] = {}
        #: Undelivered changes the last reconcile found. The evidence half of the staleness
        #: detector — webhook silence on its own cannot tell a quiet repo from a broken event path.
        self._reconcile_drift = 0

    # ---------------------------------------------------------------- lifecycle

    def request_stop(self, *_: Any) -> None:
        """Drain: stop claiming new work and let the loop exit cleanly."""
        LOG.info("stop requested — draining")
        self._stop = True

    def startup(self) -> None:
        """Recover crash state, then reconcile once before deciding anything.

        Order matters: recovery first so a stranded claim is released before the reconciler can
        observe the item and reason about it, and reconcile before dispatch so the first pass acts
        on GitHub's truth rather than a stale queue.
        """
        stats = db.startup_recover(self.conn)
        LOG.info(
            "startup: %s runs closed, %s claims released, shadow=%s, db=%s",
            stats["runs_closed"], stats["claims_released"], self.cfg.shadow, self.cfg.db_path,
        )
        db.kv_set(self.conn, "daemon_started_at", str(int(time.time())))

    # ---------------------------------------------------------------- inputs

    def drain_events(self) -> int:
        """Fold webhook deliveries into item dirt-flags.

        Events never decide anything; they only say "this item changed, look again". That is what
        makes a duplicate or out-of-order delivery harmless — the re-observation is the authority.
        """
        rows = db.unprocessed_events(self.conn, limit=200)
        if not rows:
            return 0
        seen: set[tuple[str, int]] = set()
        for row in rows:
            number = row["number"]
            target = None
            if number:
                target = self._event_target(row["event"], int(number))
            elif row["head_sha"]:
                # Observed live: merge-queue `check_suite` deliveries carry a head_sha and an EMPTY
                # pull_requests array, because the suite belongs to a `gh-readonly-queue/...` ref
                # rather than to the PR branch. Keying only on number silently dropped every one of
                # them — and check_suite is the CI-green edge, so the wait state it is supposed to
                # end would have expired on its TTL instead, quietly turning the event architecture
                # back into polling for exactly the lane that matters most.
                target = self._target_by_head_sha(row["head_sha"])
            if target is None:
                continue
            db.mark_dirty(self.conn, *target)
            seen.add(target)
        db.mark_events_processed(self.conn, [r["id"] for r in rows])
        LOG.info("drained %s event(s) touching %s item(s)", len(rows), len(seen))
        return len(rows)

    def _event_target(self, event: str, number: int) -> tuple[str, int] | None:
        """Which queue item this delivery refers to, or None when we hold no such item.

        `issue_comment` is the ambiguous one, and it is the one that matters: GitHub delivers a
        comment on a PULL REQUEST as `issue_comment` carrying only an `issue` object, so keying it
        to kind `issue` meant the owner's reply to a Decision Comment — the entire revise-unblock
        path — marked an item that does not exist and was silently dropped. Issue and PR numbers
        share one sequence per repository, so resolving by whichever item the queue holds is
        unambiguous.
        """
        if event in PR_ONLY_EVENTS:
            kinds: tuple[str, ...] = ("pr",)
        elif event == "issues":
            kinds = ("issue",)
        else:
            kinds = ("pr", "issue")
        for kind in kinds:
            if db.get_item(self.conn, kind, number) is not None:
                return kind, number
        return None

    def _target_by_head_sha(self, head_sha: str) -> tuple[str, int] | None:
        """Resolve a numberless delivery by the commit it reports on.

        Only PRs carry a head_sha, so this cannot collide with an issue. Matching the CURRENT head
        is deliberate: a suite for a superseded commit tells us nothing actionable, and re-observing
        on it would burn a GitHub read to rediscover the same answer.
        """
        row = self.conn.execute(
            "SELECT kind, number FROM items WHERE head_sha=? AND kind='pr' "
            "AND state NOT IN (?, ?) LIMIT 1",
            (head_sha, db.STATE_MERGED, db.STATE_CLOSED),
        ).fetchone()
        return (row["kind"], row["number"]) if row else None

    def reconcile(self) -> int:
        """Re-derive the queue from GitHub. The backstop that makes missed events harmless.

        Deliberately a handful of list calls, not a per-item walk: v1 spent 40-70 API calls per tick
        largely by asking the same questions per PR per lane.
        """
        found = 0
        drift = 0
        try:
            for label, kind in (
                ("agent:ready", "issue"),
                # BOTH kinds, and the issue half is not decoration. v1 wrote `agent:working` onto
                # the ISSUE as its in-flight claim marker, so an issue left holding it at the v1
                # cutover matched no query here — not `agent:ready`, and the working query was
                # PR-only. With v1 retired to a heartbeat-gated failsafe, nothing else would ever
                # look at it again: #1091 and #1140 sat OPEN and un-worked from 05:20 UTC on
                # 2026-08-10, absent from the queue entirely. `startup_recover` could not help,
                # because it releases stranded claims for items ALREADY in the queue.
                ("agent:working", "issue"),
                ("agent:working", "pr"),
                ("agent:revise", "pr"),
                ("agent:depfix", "pr"),
                ("agent:docfix", "pr"),
                ("agent:phasefix", "pr"),
                # The HOLD labels, both kinds, and this is not bookkeeping. `park.sh` strips
                # `agent:working` when it parks, so a parked item matches none of the queries above:
                # once it leaves the SQLite queue — a rebuilt database, a cutover, an item parked by
                # v1 — nothing would ever look at it again, and the owner's answer would land on a
                # thread no runner reads. #1274 was parked with both labels and absent from the
                # queue entirely. Admission is unaffected: `agent:blocked` satisfies the `agent:`
                # prefix test, and `decide` still holds anything carrying either label.
                ("needs-human", "pr"),
                ("needs-human", "issue"),
                ("agent:blocked", "pr"),
                ("agent:blocked", "issue"),
                # Abandoned items are still fetched, and that is the escape hatch working: the
                # owner removes `agent:abandoned`, the next reconcile sees an item whose labels no
                # longer carry it, and the revive below puts it back on the queue. Without the
                # query there is nothing to notice the removal.
                ("agent:abandoned", "pr"),
                ("agent:abandoned", "issue"),
            ):
                for obj in github.list_by_label(self.cfg.slug, label, kind):
                    number = obj.get("number")
                    if not number:
                        continue
                    existing = db.get_item(self.conn, kind, number)
                    names = github.label_names(obj)
                    labels_json = json.dumps(sorted(names))
                    if (existing and existing["state"] == db.STATE_ABANDONED
                            and "agent:abandoned" not in names):
                        # The owner took the label off: restart with a clean slate, which is what
                        # the abandon comment promises. Clearing the history matters as much as the
                        # state — leaving the laps would abandon it again on its very next park.
                        LOG.info("%s #%s revived — agent:abandoned removed", kind, number)
                        db.clear_park_history(self.conn, kind, number)
                        db.force_state(self.conn, existing["id"], db.STATE_READY, dirty=1,
                                       parked_reason=None, wake_at=None)
                    # DRIFT is the evidence that the event path missed something. An item GitHub
                    # reports differently from what we hold is a change nobody delivered to us —
                    # which is exactly the fault the staleness detector is trying to find, and
                    # unlike webhook silence it cannot be produced by a quiet repository.
                    if existing is not None and (
                        existing["head_sha"] != obj.get("headRefOid")
                        or existing["labels_json"] != labels_json
                    ):
                        drift += 1
                    db.upsert_item(
                        self.conn, kind=kind, number=number,
                        state=existing["state"] if existing else db.STATE_READY,
                        branch=obj.get("headRefName"),
                        head_sha=obj.get("headRefOid"),
                        labels_json=labels_json,
                        dirty=1,
                    )
                    found += 1
        except github.GitHubUnavailable as exc:
            # A failed reconcile is not a reason to act on stale state; try again next pass.
            LOG.warning("reconcile incomplete: %s", exc)
        self._last_reconcile = time.time()
        self._reconcile_drift = drift
        if drift:
            db.kv_set(self.conn, "last_drift_at", str(int(self._last_reconcile)))
        db.kv_set(self.conn, "last_reconcile_at", str(int(self._last_reconcile)))
        return found

    # ---------------------------------------------------------------- decisions

    def observe_dirty(self, limit: int = 25) -> int:
        """Re-observe changed items and apply their decisions.

        Bounded per pass so one busy minute cannot monopolise the loop and starve the TTL sweep.
        """
        rows = self.conn.execute(
            "SELECT * FROM items WHERE dirty=1 AND state NOT IN (?, ?) LIMIT ?",
            (db.STATE_MERGED, db.STATE_CLOSED, limit),
        ).fetchall()
        for row in rows:
            self._observe_one(row)
        return len(rows)

    def sweep_ttls(self, limit: int = 25) -> int:
        """Re-check items whose wait deadline expired."""
        rows = db.due_items(self.conn)[:limit]
        for row in rows:
            self._observe_one(row)
        return len(rows)

    def _observe_one(self, row) -> None:
        """Snapshot one item, decide, and record the outcome."""
        kind, number = row["kind"], row["number"]
        routed = row["last_comment_id"]
        routed = str(routed) if routed is not None else None
        if kind == "pr":
            snap = observe.snapshot_pr(self.cfg.slug, number, owner=self.cfg.assignee,
                                       answer_routed=routed)
        else:
            snap = observe.snapshot_issue(self.cfg.slug, number, owner=self.cfg.assignee,
                                          answer_routed=routed)

        parked_reason = row["parked_reason"]
        snap = dataclasses.replace(
            snap, parked_reason=parked_reason,
            park_laps=db.park_laps(self.conn, kind, number, parked_reason or ""),
        )
        decision = observe.decide(
            snap,
            ttl_ci=self.cfg.ttl_ci, ttl_review=self.cfg.ttl_review,
            ttl_queue=self.cfg.ttl_queue, ttl_parked=self.cfg.ttl_parked,
            max_park_laps=self.cfg.max_park_laps,
        )
        self._decisions += 1
        # Report the state actually PERSISTED, not the one `decide` names. An ACT_DISPATCH decision
        # carries `next_state=claimed` as an intent, but observation writes `ready` + a pending mode
        # and the claim happens later, at dispatch. Logging the intent made every restart look like
        # 13 `ready -> claimed` transitions in four seconds that never occurred — an operator
        # debugging churn would have gone looking for a claim storm that was purely a log artefact.
        persisted = decision.next_state
        if decision.action in (observe.ACT_DISPATCH, observe.ACT_MERGE, observe.ACT_UNPARK,
                               observe.ACT_DISARM, observe.ACT_ABANDON):
            persisted = db.STATE_READY
        self._emit(row, snap, decision, persisted)

        now = int(time.time())
        wake_at = now + decision.wake_in if decision.wake_in else None

        if self.cfg.shadow:
            # Record the observation, never the transition: shadow mode must not move items, or a
            # rollback to v1 would inherit a queue v1 never built.
            db.upsert_item(
                self.conn, kind=kind, number=number, state=row["state"],
                head_sha=snap.head_sha or row["head_sha"],
                branch=snap.branch or row["branch"], dirty=0, wake_at=None,
            )
            return

        if decision.action == observe.ACT_ABANDON:
            db.upsert_item(
                self.conn, kind=kind, number=number, state=db.STATE_READY, dirty=0, wake_at=None,
                pending_mode="abandon", parked_reason=decision.park_reason,
                head_sha=snap.head_sha or row["head_sha"],
                branch=snap.branch or row["branch"],
            )
            return

        if decision.action == observe.ACT_DISARM:
            # Stays `parked` in spirit — the hold is real — but the item must be dispatchable for
            # one gh action first, so it is persisted READY with a pending mode like any other.
            db.upsert_item(
                self.conn, kind=kind, number=number, state=db.STATE_READY, dirty=0, wake_at=None,
                pending_mode="disarm", parked_reason=decision.park_reason,
                head_sha=snap.head_sha or row["head_sha"],
                branch=snap.branch or row["branch"],
            )
            return

        if decision.action == observe.ACT_UNPARK:
            # The answer id is held here rather than written now: `last_comment_id` is the "already
            # routed" marker, and writing it before the action runs would burn the owner's reply on
            # an un-park that failed. `collect` records it only on rc=0.
            if snap.answer is not None:
                self._answer_ids[(kind, number)] = snap.answer.comment_id
            db.upsert_item(
                self.conn, kind=kind, number=number, state=db.STATE_READY, dirty=0, wake_at=None,
                pending_mode="unpark",
                head_sha=snap.head_sha or row["head_sha"],
                branch=snap.branch or row["branch"],
            )
            return

        if decision.action in (observe.ACT_DISPATCH, observe.ACT_MERGE):
            # The verdict is written DOWN, not held in memory, and the item is left dispatchable.
            # A pass that runs out of slots must be resumable by the next one: an item left `ready`
            # with its decision only in a local variable is a dead end, because nothing will mark it
            # dirty again. ACT_MERGE must NOT be written as `awaiting_queue` here either — nothing
            # enqueued it, so that state would claim a merge attempt that never happened and
            # re-decide the same ACT_MERGE every ttl_queue seconds forever.
            #
            # `wake_at=None` is the important half. Nothing else clears a deadline left over from a
            # wait state, and while `due_items()` is scoped to the TTL states, `next_sleep()`
            # reads MIN(wake_at) — so one past deadline on a `ready` item pinned the whole loop at
            # a 1-second spin, which is precisely the unconditional polling v2 exists to remove.
            db.upsert_item(
                self.conn, kind=kind, number=number, state=db.STATE_READY, dirty=0, wake_at=None,
                pending_mode=(decision.mode if decision.action == observe.ACT_DISPATCH else "merge"),
                head_sha=snap.head_sha or row["head_sha"],
                branch=snap.branch or row["branch"],
            )
            return

        if decision.next_state == db.STATE_WAIT_OWNER_REVIEW and row["state"] != db.STATE_WAIT_OWNER_REVIEW:
            # First observation of this wait, for this item — not every 900s-or-so re-check while
            # it persists. `row["state"]` is the PREVIOUS pass's persisted state, so this is the
            # transition edge, the same "state once per head" idea `park.sh` uses for its comment,
            # done here because this wait is not a park (auto-merge must stay armed, so drafting
            # the PR or disabling auto-merge — what `park.sh` does — would undo the very thing
            # letting a human's approval complete the merge unattended).
            self._notify_owner_review_needed(kind, number)
        db.upsert_item(
            self.conn, kind=kind, number=number, state=decision.next_state,
            wait_reason=decision.wait_reason,
            parked_reason=_keep_specific_park_reason(row["parked_reason"], decision.park_reason),
            wake_at=wake_at, head_sha=snap.head_sha or row["head_sha"],
            branch=snap.branch or row["branch"], dirty=0, pending_mode=None,
        )

    def _notify_owner_review_needed(self, kind: str, number: int) -> None:
        """Tell the owner once that a PR is done and only waiting on their CODEOWNERS approval.

        Best-effort: a failed comment must not take down the observation pass — there is nothing
        to retry into, and the next TTL wake (`ttl_parked`) tries again anyway on a genuine outage.
        """
        body = (
            "🔔 **Owner approval needed** — every required check is green and auto-merge is armed, "
            "but this touches a CODEOWNERS-protected path, so it needs your review before it can "
            "merge. See `docs/codeowners-enforcement-limit.md`. No reply needed here — just "
            "Approve it (GitHub UI or `gh pr review --approve`) when you're ready."
        )
        try:
            github.post_comment(self.cfg.slug, kind, number, body)
        except github.GitHubUnavailable as exc:
            LOG.warning("owner-review notify failed for %s #%s: %s", kind, number, exc)

    def _emit(self, row, snap: observe.Snapshot, decision: observe.Decision,
              persisted: str | None = None) -> None:
        """One structured line per decision — the shadow phase's entire evidence base.

        `action` here is an INTENT and nothing more. `act()` applies four gates after this runs
        (the start hold, a draining v1 tick, a full pool, the WIP limit) and any of them can refuse
        it, so `stage=observe` rows carry `executed: false` unconditionally. Executions are
        recorded separately by `_emit_act`.
        """
        payload = {
            "ts": int(time.time()),
            "shadow": self.cfg.shadow,
            "stage": "observe",
            "kind": row["kind"], "number": row["number"],
            "from_state": row["state"], "to_state": persisted or decision.next_state,
            "intent": decision.next_state,
            "action": decision.action, "mode": decision.mode, "reason": decision.reason,
            # The field that closes the defect. Without it a consumer counting `action=dispatch`
            # reads ~40 dispatches a pass off a pipeline that spawned nothing, for as long as the
            # hold is set — and every dispatch-rate or success-rate figure built on the ledger is
            # inflated by the whole backlog.
            "executed": False,
            "wake_in": decision.wake_in, "readable": snap.readable,
            **({"details": decision.details} if decision.details else {}),
        }
        LOG.info("decision %s", json.dumps(payload, separators=(",", ":")))
        self._write_decision(payload)

    def _emit_act(self, row, mode: str, *, executed: bool, refused_by: str | None = None) -> None:
        """Record what `act()` actually DID with a decision.

        Executions are written every time and are bounded by the slot cap. Refusals are written
        once per (item, gate): a standing refusal is a fact about the gate, not an event, and
        re-stating it every pass is what buries the rows an operator is reading for.
        """
        key = (row["kind"], int(row["number"]))
        if executed:
            self._refused.pop(key, None)
        else:
            if self._refused.get(key) == refused_by:
                return
            self._refused[key] = refused_by or "unknown"
        payload = {
            "ts": int(time.time()),
            "shadow": self.cfg.shadow,
            "stage": "act",
            "kind": row["kind"], "number": row["number"], "mode": mode,
            "executed": executed,
            "refused_by": refused_by,
        }
        self._write_decision(payload)

    def _write_decision(self, payload: dict[str, Any]) -> None:
        """Append one row to the decision ledger. Telemetry must never break the loop."""
        try:
            path = self.cfg.base / "logs" / "lemd-decisions.ndjson"
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a") as fh:
                fh.write(json.dumps(payload, separators=(",", ":")) + "\n")
        except OSError as exc:
            LOG.warning("could not write decision log: %s", exc)

    # ---------------------------------------------------------------- execution

    def act(self) -> int:
        """Fill both pools with the work `observe()` already decided on.

        Continuous dispatch, not one-per-pass: v1's ceiling was never `MAX_AGENTS`, it was one unit
        of work per cron fire, and this loop is what removes that. Two pools are drained
        independently so a two-second merge-enable never queues behind a twenty-minute
        implementation run.

        Returns:
            How many actions were launched this pass.
        """
        if self.cfg.shadow:
            return 0
        busy = self.sup.v1_slots_busy()
        if busy:
            # Only the AGENT pool waits. A v1 tick in flight is running agents, not arming merges,
            # so holding the gh pool too would stall the cheap lane for up to 45 minutes for no
            # safety gain — and the merge lane is the one v2 exists to make fast.
            if self.sup.free("gh") <= 0:
                return 0
            LOG.info("%s v1 tick(s) still draining — gh-only dispatch this pass", busy)
        launched = 0
        held_by_wip = 0
        held_by_switch = 0
        wip = db.wip_count(self.conn)
        for row in db.dispatchable(self.conn):
            mode = row["pending_mode"]
            # `unpark` is a gh-pool action: four label writes and a ledger reset, no model. It
            # must never queue behind a 20-minute implementation run — the whole complaint it fixes
            # is work sitting parked while the owner waits.
            pool = "gh" if mode in ("merge", "park", "unpark", "disarm", "abandon") else "agent"
            if mode == "start" and self.cfg.hold_starts:
                # Checked BEFORE the slot read: an operator holding new work still wants to see
                # that it is being held, and a full pool would otherwise `continue` first and
                # report nothing. Logged once per pass, like the WIP gate — a 39-issue backlog
                # printing 39 identical lines buries the dispatches an operator is tailing for.
                if not held_by_switch:
                    LOG.info("LEMD_HOLD_STARTS set — holding new starts; merge/park/selfreview "
                             "continue so in-flight PRs still drain")
                held_by_switch += 1
                self._emit_act(row, mode, executed=False, refused_by="hold_starts")
                continue
            if busy and pool == "agent":
                self._emit_act(row, mode, executed=False, refused_by="v1_busy")
                continue
            if self.sup.free(pool) <= 0:
                self._emit_act(row, mode, executed=False, refused_by="pool_full")
                continue
            if mode == "start" and wip >= self.cfg.max_agents:
                # v1's gate, ported deliberately: new work never outruns merge throughput. Without
                # it a backlog of 35 ready issues becomes 35 open PRs against a queue that merges
                # one at a time — and every one of them is a rebase candidate the moment main moves.
                #
                # Logged ONCE per pass, not once per held item: a 35-issue backlog printed 35
                # identical lines every pass, which buries the dispatch lines that matter in a file
                # an operator reads by tailing it.
                if not held_by_wip:
                    LOG.info("WIP limit: %s PR(s) in flight >= %s — holding new starts",
                             wip, self.cfg.max_agents)
                held_by_wip += 1
                self._emit_act(row, mode, executed=False, refused_by="wip_limit")
                continue
            # A cheap local read that saves a doomed spawn. It is NOT the authoritative check —
            # `agent_run.sh` re-checks and charges inside the branch flock, so this being a moment
            # stale can only waste a process, never overspend a budget.
            if pool == "agent" and policy.exhausted(self.cfg.base, row["kind"], row["number"], mode):
                LOG.info("#%s has spent its %s budget — parking instead of dispatching",
                         row["number"], mode)
                self._emit_act(row, mode, executed=False, refused_by="budget_exhausted")
                self._park(row, f"{mode}_exhausted")
                continue
            # The claim is taken AFTER every gate and immediately before the spawn, so an item the
            # WIP gate or the hold switch refuses is never claimed at all. (The "13 claimed
            # transitions per restart" seen on 2026-08-10 were a LOGGING artefact, not writes —
            # `_emit` was reporting `decide`'s nominal next state instead of the state persisted.
            # Worth stating, because the obvious reading of that log is a claim storm here.)
            if not db.claim_item(self.conn, row["id"]):
                self._emit_act(row, mode, executed=False, refused_by="claim_lost")
                continue
            child = self._launch(row, mode)
            if mode == "start" and child is not None:
                # The item this pass just claimed will become a PR, so it counts against the gate
                # NOW. Reading `wip` once per pass and never updating it would let a single pass
                # launch every start the slot pool allows, gate or no gate.
                wip += 1
            if child is None:
                # The claim must not outlive a failed spawn: `claimed` holds the branch through the
                # partial unique index, and only `startup_recover` would ever release it.
                db.force_state(self.conn, row["id"], db.STATE_READY, dirty=1)
                self._emit_act(row, mode, executed=False, refused_by="spawn_failed")
                continue
            self._emit_act(row, mode, executed=True)
            launched += 1
        return launched

    def _launch(self, row, mode: str):
        """Start the right action for one decided item."""
        kind, number = row["kind"], row["number"]
        if mode == "merge":
            return self.sup.dispatch_gh(action=GH_MERGE_ACTION, kind=kind, number=number,
                                        args=[str(number), row["head_sha"] or ""],
                                        item_id=row["id"])
        if mode == "park":
            return self.sup.dispatch_gh(
                action="park", kind=kind, number=number,
                args=[kind, str(number), row["parked_reason"] or "parked"], item_id=row["id"])
        if mode == "abandon":
            return self.sup.dispatch_gh(
                action="abandon", kind=kind, number=number,
                args=[kind, str(number), row["parked_reason"] or "unknown"], item_id=row["id"])
        if mode == "disarm":
            return self.sup.dispatch_gh(action="disarm", kind=kind, number=number,
                                        args=[str(number)], item_id=row["id"])
        if mode == "unpark":
            return self.sup.dispatch_gh(
                action="unpark", kind=kind, number=number,
                args=[kind, str(number), self._answer_ids.get((kind, number), ""),
                      row["parked_reason"] or ""],
                item_id=row["id"])
        return self.sup.dispatch_agent(mode=mode, kind=kind, number=number,
                                       branch=row["branch"], item_id=row["id"])

    def _park(self, row, reason: str) -> None:
        """Queue a park for one item (the gh pool performs it), and count the lap.

        Recorded here rather than in the action, because the action is deduped on the head SHA and
        exits early on a repeat — so a park that `park.sh` declines to re-announce would never be
        counted, which is exactly the lap that proves a loop.
        """
        db.record_park(self.conn, row["kind"], row["number"], reason, row["head_sha"])
        db.upsert_item(self.conn, kind=row["kind"], number=row["number"], state=db.STATE_READY,
                       pending_mode="park", parked_reason=reason, dirty=0, wake_at=None)

    def _apply_caps(self) -> None:
        """Size the pools for this pass from the backlog and the busy window.

        `capacity.compute()` was written, tested, and called by nothing — the live cap was the flat
        `LEMD_MAX_AGENTS`. Wiring it restores two behaviours v1 had: concurrency that scales with
        the backlog rather than sitting at the ceiling, and the owner's busy window, in which they
        want their box back.

        Note this can LOWER concurrency on a small backlog (`1 + ready // scale_per_issues`), which
        is the v1 parity being restored rather than a regression — but it is a throughput change,
        and the reason for the cap is logged so an operator can see which rule is binding.
        """
        ready = self.conn.execute(
            "SELECT COUNT(*) AS n FROM items WHERE kind='issue' AND state=? AND dirty=0",
            (db.STATE_READY,),
        ).fetchone()
        caps = capacity.compute(
            ready_count=int(ready["n"]) if ready else 0,
            max_agents=self.cfg.max_agents,
            scale_per_issues=self.cfg.scale_per_issues,
            gh_slots=self.cfg.gh_slots,
            busy_hours=self.cfg.busy_hours,
            busy_tz=self.cfg.busy_tz,
            busy_days=self.cfg.busy_days,
            busy_max_agents=self.cfg.busy_max_agents,
        )
        if caps != getattr(self, "_last_caps", None):
            LOG.info("caps: agents=%s gh=%s (%s)", caps.agents, caps.gh, caps.reason)
            self._last_caps = caps
        self.sup.set_caps(caps)

    def collect(self) -> int:
        """Fold finished actions back into item state.

        The exit code is the entire report, which is why the actions use a distinct code per
        refusal: v1 collapsed "you may not do this" and "that run failed" into one non-zero, and
        then retried both every five minutes.
        """
        finished = self.sup.reap()
        for child, rc in finished:
            item = db.get_item(self.conn, child.kind, child.number)
            if item is None:
                continue
            if rc == dispatch.EX_BUDGET:
                # The action refused under the flock — the authoritative answer. Park.
                reason = f"{_item_mode(child.mode)}_exhausted"
                db.record_park(self.conn, child.kind, child.number, reason, item["head_sha"])
                db.force_state(self.conn, item["id"], db.STATE_READY, dirty=0, wake_at=None,
                               pending_mode="park", parked_reason=reason)
            elif rc == dispatch.EX_TRUST:
                # Never retried on a timer: a trust refusal is answered by a human re-labelling,
                # which arrives as an event. The slow TTL is only a safety net against a lost one.
                db.force_state(self.conn, item["id"], db.STATE_PARKED, dirty=0,
                               pending_mode=None, parked_reason="trust_refused",
                               wake_at=int(time.time()) + self.cfg.ttl_parked)
            elif rc == dispatch.EX_BUSY:
                # Someone else holds the branch — a v1 tick, most likely, during coexistence.
                # Keep the decision and try again shortly.
                db.force_state(self.conn, item["id"], db.STATE_READY, dirty=0,
                               wake_at=int(time.time()) + 120)
            elif rc == 0 and child.mode == "unpark":
                # The answer is spent only now. Recording it is what stops the SAME reply un-parking
                # a future park raised for a different reason — after a successful un-park the hold
                # labels are gone, but the comment stays the newest one in the thread for ever.
                # Written through `force_state`, not `upsert_item`: the item is `running` here, and
                # the upsert guard exists precisely to stop writes landing on an active item.
                # Re-observe immediately rather than on a TTL — the labels this action just rewrote
                # ARE the next decision, and the owner is waiting on it.
                db.record_unpark(self.conn, child.kind, child.number,
                                 item["parked_reason"] or "", item["head_sha"])
                answer_id = self._answer_ids.pop((child.kind, child.number), None)
                db.force_state(self.conn, item["id"], db.STATE_READY, dirty=1,
                               pending_mode=None, parked_reason=None, wake_at=None,
                               last_comment_id=answer_id)
            elif rc == 0 and child.mode == GH_MERGE_ACTION:
                # A successful arm is a WAIT, not a reason to look again immediately. Re-observing
                # here is what let #1295 spend its whole per-head merge budget in three minutes:
                # each pass saw an armed-but-not-yet-enqueued PR, read it as "gate satisfied", and
                # asked again. `observe.decide` now refuses that too, and this is the second half —
                # the run's own outcome must not contradict the state its success implies.
                db.force_state(self.conn, item["id"], db.STATE_WAIT_QUEUE, dirty=0,
                               pending_mode=None, wait_reason="merge_queue",
                               wake_at=int(time.time()) + self.cfg.ttl_queue)
            else:
                # Everything else — success, agent failure, timeout — is answered by looking at
                # GitHub again rather than by guessing locally what the run achieved.
                db.force_state(self.conn, item["id"], db.STATE_READY, dirty=1,
                               pending_mode=None, wake_at=None)
            self._emit_run(child, rc)
        return len(finished)

    def _emit_run(self, child, rc: int) -> None:
        """One structured line per finished action."""
        payload = {
            "ts": int(time.time()), "event": "run_finished", "mode": child.mode,
            "kind": child.kind, "number": child.number, "rc": rc,
            "run_seconds": round(time.time() - child.started, 1),
            "outcome": ("ok" if rc == 0 else
                        "timeout" if child.killed else
                        "refused" if rc in dispatch.REFUSALS else "failed"),
        }
        LOG.info("run %s", json.dumps(payload, separators=(",", ":")))
        try:
            path = self.cfg.base / "logs" / "lemd-decisions.ndjson"
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a") as fh:
                fh.write(json.dumps(payload, separators=(",", ":")) + "\n")
        except OSError as exc:
            LOG.warning("could not write run log: %s", exc)

    # ---------------------------------------------------------------- the loop

    def next_sleep(self) -> float:
        """Seconds until the next thing is due, bounded.

        Scoped to the TTL states for the same reason `due_items()` is: nothing clears `wake_at` when
        an item leaves a wait state, so an unscoped MIN() sleeps on deadlines belonging to merged
        PRs and other items no sweep will ever return — a past one of those makes every pass sleep
        the 1-second floor and turns the loop back into a poll.
        """
        placeholders = ",".join("?" * len(db.TTL_STATES))
        row = self.conn.execute(
            "SELECT MIN(wake_at) AS w FROM items "
            f"WHERE wake_at IS NOT NULL AND state IN ({placeholders})",
            tuple(sorted(db.TTL_STATES)),
        ).fetchone()
        due_in = (row["w"] - time.time()) if row and row["w"] else MAX_SLEEP
        until_reconcile = (self._last_reconcile + self.cfg.reconcile_interval) - time.time()
        return max(1.0, min(MAX_SLEEP, due_in, until_reconcile))

    def tick(self) -> None:
        """One pass of the loop."""
        capacity.heartbeat(self.cfg.heartbeat_file)
        self._apply_caps()
        # Reaping runs even while PAUSED. A pause stops the pipeline STARTING work; leaving already
        # running children uncollected would hold their claims and their branches for the whole
        # duration of the pause, so the resume would find every branch locked.
        self.collect()
        if self.cfg.is_paused():
            LOG.info("PAUSED file present — observing nothing this pass")
            return
        self.drain_events()
        if time.time() - self._last_reconcile >= self.reconcile_interval():
            self.reconcile()
        self.refresh_usage()
        self.observe_dirty()
        self.sweep_ttls()
        self.act()

    def refresh_usage(self) -> None:
        """Keep the subscription meter fresh so the dispatch action can read it.

        This is the piece that was specified and never wired: `spend.py` could answer "how much of
        the weekly window is left" from `claude -p /usage`, and nothing called it — so routing fell
        back to `lib/capacity.sh`'s failure-history estimate, which can only find a ceiling by
        hitting it. On 2026-08-10 that sent 30 consecutive runs to Ollama while the subscription sat
        at 23.5% used.

        The DAEMON probes, never the action. A probe is a real (small) `claude -p` run, so probing
        per dispatch would spend subscription runs to decide how to spend subscription runs. Failure
        is silent and harmless by design: a stale cache makes `lane_for.py` answer
        `probe_unavailable`, which restores exactly the health-based routing that came before.
        """
        now = time.time()
        if now - self._last_usage_probe < self.cfg.usage_probe_interval:
            return
        self._last_usage_probe = now
        cache = self.cfg.base / "state" / "usage.json"
        try:
            usage = spend.cached_usage(cache, ttl=self.cfg.usage_probe_interval)
        except Exception as exc:  # noqa: BLE001 - metering must never break the loop
            LOG.warning("usage probe failed: %s", exc)
            return
        if usage.readable:
            LOG.info("usage: week=%s%% session=%s%% resets=%s",
                     usage.week_pct, usage.session_pct, usage.week_resets or "unknown")
        else:
            LOG.warning("usage probe unreadable — routing falls back to the health estimate")

    def reconcile_interval(self) -> int:
        """How often to re-derive from GitHub — faster when the event path looks broken.

        ONE knob, deliberately. Degraded mode makes the daemon poll harder; it never makes it act
        differently, because events only ever make the scheduler PROMPT, never ABLE. Anything a
        webhook would have told us is derivable here, so a silent event outage costs latency and
        nothing else.
        """
        last = db.kv_get(self.conn, "last_webhook_at")
        if not last:
            return self.cfg.reconcile_interval
        try:
            age = time.time() - float(last)
        except ValueError:
            return self.cfg.reconcile_interval
        # Silence alone is NOT a fault. `last_webhook_at` is only written when a delivery arrives,
        # so "no delivery in 30 minutes" reads identically for a broken event path and for a
        # repository where nothing happened — and resolving that ambiguity by assuming the worst
        # polled 5x harder exactly when there was least to discover. Measured on 2026-08-10: a
        # 78-minute warning across a window with ZERO workflow runs created, on a receiver that was
        # healthy throughout and resumed on its own.
        #
        # Drift is the half that disambiguates. If the last reconcile found an item GitHub reports
        # differently from what we hold, something changed and nobody told us: that is the event
        # path failing, and polling harder is the right answer. If silence and no drift agree, the
        # repository is simply quiet and the normal cadence is correct.
        if age > self.cfg.webhook_stale_seconds and self._reconcile_drift:
            if not self._degraded:
                LOG.warning("no webhook delivery for %.0f min and the last reconcile found %s "
                            "undelivered change(s) — reconciling every %ss",
                            age / 60, self._reconcile_drift,
                            self.cfg.reconcile_interval_degraded)
                self._degraded = True
            return self.cfg.reconcile_interval_degraded
        if self._degraded:
            LOG.info("webhook deliveries resumed — back to the %ss reconcile cadence",
                     self.cfg.reconcile_interval)
            self._degraded = False
        return self.cfg.reconcile_interval

    def run(self) -> None:
        """Loop until asked to stop."""
        self.startup()
        while not self._stop:
            started = time.time()
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - the loop must outlive any single bad pass
                LOG.exception("tick failed; continuing")
            elapsed = time.time() - started
            if elapsed > 30:
                LOG.warning("slow pass: %.1fs", elapsed)
            time.sleep(self.next_sleep())
        # DRAIN, not kill: rollback is "stop claiming, let what is in flight land". Killing a
        # mid-flight agent would abandon its commits in a worktree that v1 is about to take over.
        self.sup.drain(timeout=self.cfg.drain_seconds)
        LOG.info("stopped after %s decisions", self._decisions)


def main() -> None:
    """Entry point for `python -m lemd.daemon`."""
    logging.basicConfig(
        level=os.environ.get("LEMD_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    cfg = load()
    daemon = Daemon(cfg)
    signal.signal(signal.SIGTERM, daemon.request_stop)
    signal.signal(signal.SIGINT, daemon.request_stop)
    LOG.info(
        "lemd starting: slug=%s shadow=%s agents=%s gh=%s",
        cfg.slug, cfg.shadow, cfg.max_agents, cfg.gh_slots,
    )
    daemon.run()


if __name__ == "__main__":
    main()
