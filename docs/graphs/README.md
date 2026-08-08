# LEM Graph Directory

A "graph engineering" review of LEM's operational pipelines — deploy/release, how work itself gets
built, and the product's own automation loops — each mapped as a graph (nodes = steps/agents/checks,
edges = handoffs) and scored against one rubric:

- **Removes fake waiting** — no step that blocks without doing real work or serving a real review purpose.
- **Separates the worker from the checker** — the step that produces an output is never the one that approves it.
- **Puts the human gate where mistakes get expensive** — review sits at the point where a wrong call actually costs something.
- **Leaves a trail** — durable residue (notes, evidence, drafts, decisions) that makes the next run smarter.
- **Avoids the trap** — more agents/steps is not automatically better; a minimal graph beats an impressive one.

Each graph doc below was produced in two passes: `spec-first` (`docs/spec-verifier-environment.md`)
mapped the current state and named a testable Verifier per graph, then `gauntlet-loop`
(`docs/gauntlet-loop.md`) ran builder/critic rounds against a named reference exemplar to produce a
redesign proposal. **This directory is a design deliverable — diagrams, specs, and reviewed redesign
proposals, not shipped code.** Any proposal that implies a real code/CI/deploy change is a candidate
for its own issue through the normal `ship-issue`/`risk:*` flow, not a change bundled into this docs
pass.

## A note on what "good" means here, for two different categories of graph

A live correction shaped how these redesigns were judged, and it's worth stating up front since it
reads differently across the six graphs below:

- **CI and deployment graphs** (`deploy-release`, `agent-issue-shipping`) genuinely benefit from a
  mandatory human gate at the expensive-mistake point — a wrong call there is costly and infrequent
  enough that a deliberate pause is worth it. Both redesigns lean into that.
- **Content and engagement graphs** (`content-generation`, `content-scheduling-quality`,
  `engagement-feed-reply`, `engagement-outreach-dm`) exist to automate LinkedIn engagement — that's
  the product. The right "human gate" criterion for these is **per-user configurability**: does the
  user understand what's automated and have a real, reachable control to require review instead —
  mirroring the `roster_auto_follow`/`roster_auto_connect` pattern already in this codebase — not
  "does a human review everything." Two of the four redesigns below were sent back a round specifically
  for defaulting to mandatory review instead of a configurable toggle.

## The six graphs

| Graph | What it is | Current-state weak row(s) | Gauntlet-loop outcome |
|---|---|---|---|
| [`deploy-release.md`](./deploy-release.md) | PR merge → CI → release-please → build-and-push → VPS cutover → health check → rollback | Human gate (❌ — required-reviewer gate was deliberately removed so green releases auto-deploy) | **WINS** (round 3) — category-scoped `release-risk-check` job, manual owner unblock, honestly scoped |
| [`agent-issue-shipping.md`](./agent-issue-shipping.md) | How LEM's own code ships: triage → build → PR → review → merge → release | Worker/checker separation (⚠️ — self-review by the same identity on the default lane) | **WINS** (hit the 3-round cap, then resolved by owner decision — Option A implemented and re-verified) |
| [`content-generation.md`](./content-generation.md) | Content-plan slot → written/checked/persisted post → approve/publish | Worker/checker separation (⚠️ — a failed check's retry was the same writer trying again) | **WINS** (round 3) — repair pass moved to a distinct editor prompt family, gated by a scoped, reversible toggle |
| [`content-scheduling-quality.md`](./content-scheduling-quality.md) | Publish/track/feedback-loop already-generated content (posts + newsletters) | Human gate + worker/checker separation (⚠️ — newsletter `draft` was silently publishable, unlike its own cover image) | **WINS** (round 3) — real per-user toggle, with an actual rendered SPA control |
| [`engagement-feed-reply.md`](./engagement-feed-reply.md) | Autonomous feed commenting, replies, seed/second-wave comments, suppression | Human gate + worker/checker separation (⚠️ — reactive checker ran on a week-scale lag) | **WINS** (round 3) — tuned an *existing* daily mechanism the first two rounds hadn't found, rather than adding a new one |
| [`engagement-outreach-dm.md`](./engagement-outreach-dm.md) | Autonomous DMs, connection invites, follow-up sequences | Human gate + worker/checker separation (⚠️ — only 2 of 8 outreach lanes were approval-gated) | **WINS** (round 2) — one new lane gated with a real toggle; a second lane's "gate" turned out to already exist and was correctly left alone |

## How to read a graph doc

Each doc follows the same shape: **What this graph does** → **Current state** (diagram + rubric
scorecard) → **Spec / Verifier / Environment** (the testable acceptance criteria the redesign was
judged against) → **Reference exemplar candidate** → **Gauntlet-loop redesign** (the verdict trail
across rounds, the winning or parked proposal, and residual caveats flagged by the final critic).

## What this process actually caught

Worth naming plainly, since it's the point of running builder/critic pairs instead of a single pass:

- **A real regression before it shipped:** round 1 of `engagement-outreach-dm` would have silently
  broken the `roster_auto_connect` toggle's existing autonomous-send meaning for users who'd already
  opted in — round 2 caught it by reading the actual code, not by re-reasoning about the diagram.
- **A proposal solving an already-partially-solved problem:** round 2 of `engagement-feed-reply`
  invented a parallel mechanism duplicating one that already existed in `suppression.py` — round 3
  found and reconciled with it instead of shipping a redundant, unreconciled second check.
- **A citation that didn't hold up:** the final round of `agent-issue-shipping` cited an "existing"
  PR-body convention that a `grep` across the repo showed was never written anywhere — caught, and
  parked for a human rather than shipped on the strength of an unverified claim. The owner reviewed
  the parked write-up's two options and chose to ground the convention for real; a fresh critic then
  independently re-verified the fix before the row was marked WINS.
- **A cited written policy the first draft never reconciled with:** round 1 of `agent-issue-shipping`
  contradicted `AGENT_WORKFLOW_PLAYBOOK.md`'s own "never request Copilot review by hand" rule without
  amending it — caught in round 2.

None of these were caught by the builder that produced them. That's the mechanism working as
designed, not a sign the builders were careless — a same-pass self-review structurally can't catch a
mistake the author is confident about, which is exactly why the pattern exists. All six graphs now
carry a winning, independently-verified redesign.
