# Spec, Verifier, Environment — grounding an issue before writing code

A framework popularized by Andrej Karpathy for working with coding agents: three questions to answer
**before** the first line of implementation code, not during a "planning mode" pass that still ends
in a guess. The claim behind it — echoed across the independent write-ups that documented it — is
narrow and worth stating exactly: *you can hand off the execution, but not the understanding.* An
agent that writes fast, plausible code against an under-specified goal, an unstated success
condition, and a cold-start read of the whole repo is optimizing the wrong thing well.

The three layers:

- **Spec** — a structured, ideally co-authored statement of what is actually wanted, precise enough
  that the model isn't guessing. Karpathy's framing favors the agent *interviewing* the human to find
  the real goal or decision the work is meant to drive, over jumping straight to a high-level
  "planning mode" that papers over an ambiguous ask.
- **Verifier** — how anyone (human or model) will know the output is actually right, decided **before**
  building: tests, explicit acceptance criteria, or a second independent pass acting as critic. A
  tight verifier catches drift every cycle; a longer prompt only hopes the wording holds.
- **Environment** — a well-organized local structure — reference docs, retrieval, reusable
  skills/handbooks for repeated task types — so the agent finds the right context fast instead of
  re-deriving conventions from scratch on every task.

This doc does not introduce a new LEM subsystem. It names three things that already exist in this
repo under other headings and says how to use them in that order, before code. Skip it for a
one-line fix or a fully-specified issue; use it for anything where the shape of "done" is not already
obvious.

## Layer 1 — Spec: the issue IS the spec, and a vague one is a defect, not a starting point

`docs/AGENT_WORKFLOW_PLAYBOOK.md` already defines the spec format LEM's pipeline runs on — it just
doesn't use the word "spec":

```markdown
## Context
Why this matters + evidence (link data, PRs, docs). Agents only know what's written here.

## Scope
- Concrete changes, named files/modules where known
- What is explicitly OUT of scope

## Acceptance
- Testable criteria (unit/integration tests, behavior, coverage)
```

"Agents only know what's written here" is the whole thesis of the Spec layer stated as a repo rule.
A missing `## Acceptance` section, an acceptance box that reads "make it better," or scope prose that
hides a second phase inside a sentence ("...and later we should also...") is not a stylistic nit —
it is the thing that produced #548, #568, and the stretch half of #647 shipping incomplete and
silently closing (`AGENT_WORKFLOW_PLAYBOOK.md`'s "Phased work" section). The phase-guard at merge time
catches the closing half of that failure; nothing catches the front half except reading the issue like
a spec before starting.

**Applying it, concretely, before writing code:**

1. Read `## Context` / `## Scope` / `## Acceptance` as written, not as remembered from the issue title.
2. If `## Acceptance` is untestable ("improve X", "clean up Y", no numbers/behaviors/test names) or
   scope hides a decision ("pick whichever approach is simpler" with two real options that trade off
   differently) — this is the interview step. Don't silently pick one reading and build it. Post a
   clarifying comment (or, mid-build, ask the user) naming the fork and which way you're leaning, the
   way `ship-issue`'s Decision Comment convention already expects for `risk:*` work. For everything
   else, state the assumption explicitly in the PR body rather than burying it in a diff.
3. If the issue is a multi-phase piece of work, write down which phase THIS PR closes and which
   remains open — `AGENT_WORKFLOW_PLAYBOOK.md`'s phased-issue rules, not this doc, own the mechanics
   (file the follow-up, or drop `Closes #N`).
4. An issue with no flow label, or one that stays genuinely unspecifiable after the clarifying pass
   (the decision needs the account owner, not a fact you can look up), is `needs-human` — see the
   Decision Comment protocol in `AGENT_WORKFLOW_PLAYBOOK.md`. Building a best guess against a spec you
   know is broken is the failure mode this layer exists to stop.

## Layer 2 — Verifier: decide the check before the change, not after

LEM already has a verifier layer that most issues can point at directly rather than inventing one:

| Kind of change | The verifier that already exists |
|---|---|
| Almost anything | The **test-lanes** skill: unit (mock all I/O) / integration (real MySQL+Redis) / e2e (Selenium); `--strict-markers`; ≥80% patch coverage enforced by Codecov |
| Schema change | **db-migration** skill: Flyway timestamp versioning, additive-only DDL, `Migration Versions` CI check |
| Feature toggle | **add-feature-flag** skill: fail-open-to-env-var contract, call-site read, `docs/feature-flags.md` registry row |
| Merge-worthy PR | The six required CI contexts (`Unit Tests`, `Integration Tests`, `UI Build`, `Migration Versions`, `GitGuardian Scan`, `CodeQL PR Quality Gate`) — CLAUDE.md's CI Gates table |
| LLM output whose correctness isn't a plain assertion (comment quality, tone, prompt wording) | The deterministic gates that already stand in for a human "looks right": the #617 comment quality contract + similarity gate, `slop_lint.py`'s five hard checks, the model-benchmark suite's `contract` floor (`docs/model-benchmarks/README.md`) |
| Selector / DOM behavior on LinkedIn's SDUI | The read-only live probe (**linkedin-live-validation** skill) — never "I read the DOM and it looks right" |
| Risky merge (`risk:*`) | A human, via the Decision Comment — the verifier for a policy/security/live-LinkedIn/schema call is explicitly a person, not a test |

**Applying it, concretely, before writing code:** turn the issue's `## Acceptance` bullets into the
specific check you will run — a named test file, a coverage number, a lane, a skill's checklist — and
write that down (a plan comment, the first lines of the PR description) before touching
implementation. "Write a test that reproduces the bug, then make it pass" and "state a brief
step→verify plan for a multi-step task" are the concrete versions of this that generalize past LEM;
inside LEM the specific verifier is almost always one of the rows above, not a new one. If nothing in
that table fits the change, that itself is a signal: either the acceptance criteria are still too
vague (back to Layer 1), or this is new verification surface worth its own issue, not something to
improvise inline.

The second-model-as-critic half of Karpathy's Verifier layer already runs at PR time, not as
something an implementer needs to set up: the pipeline's adversarial review (`🔎 Claude adversarial
review`) and, on `risk:*` PRs, a GitHub Copilot second opinion (`review:copilot`) — `merge-gate`
requires one before merge either way (`AGENT_WORKFLOW_PLAYBOOK.md`, Review economy).

## Layer 3 — Environment: CLAUDE.md, docs/*.md, and .claude/skills/* ARE the environment

Karpathy's Environment layer — a local structure organized enough that an agent finds the right
context fast instead of re-deriving it — is not a new folder to build in this repo; it is CLAUDE.md's
existing shape, read in the order it is designed to be read:

1. **CLAUDE.md's Directory Map + Feature Areas table** — which module owns the area the issue touches,
   and which `docs/*.md` the CLAUDE.md row already names for it. CLAUDE.md is explicitly "the map
   (locations, symbols, constants, invariants, where to find the detail)" — read it before searching
   the tree by hand.
2. **The named `docs/*.md`** for that Feature Area row — "the paragraph behind each row… holds
   rationale, contracts and edge cases." This is where the invariant that will bite lives (e.g. the
   `_dm_send_landed` contract, the fail-open shape of a given cache, why a field is stdlib-only).
3. **A matching `.claude/skills/*/SKILL.md`** — `ship-issue`, `test-lanes`, `db-migration`,
   `add-feature-flag`, `triage-warning`, `linkedin-live-validation`, `deploy-ops`. These are reusable
   handbooks for a repeated task shape; check for one before writing procedure from scratch, and don't
   silently reinvent what one of them already states as a numbered contract (branch naming, label
   state machine, coverage bar, migration versioning).
4. **The single-owner module for the area**, per CLAUDE.md's own repeated pattern: `utilities/db.py`
   for all DB access, `client.py` for all LLM calls, `human_pacing.py` for cadence, `post_image.py`
   for post images, `routing_policy.py` for routing decisions. "Never add a parallel per-content-type
   prompt helper, add a preset" (image stack) is the general shape: before writing a new helper,
   check whether the Feature Areas table already names the ONE place this kind of logic lives.

**Applying it, concretely, before writing code:** don't grep the tree cold. Locate the issue's area in
CLAUDE.md's Feature Areas table first, open the doc it names, check for a skill that already covers
the task shape, and identify the owning module before adding a new one. If none of the three surfaces
the issue — a genuinely new area of the codebase — that's a real signal the issue needs a docs/*.md of
its own by the time its PR lands (see how #621, #626, #629, #745 etc. all point back to a doc), not
that Environment doesn't apply.

## Order matters, and none of this replaces `ship-issue`

Spec → Verifier → Environment is a **before** step. Once all three are nailed down — the acceptance
criteria are testable, the specific check is named, and the relevant docs/skills are loaded — the
`ship-issue` skill's branch → build → PR → CI → merge flow is unchanged and still authoritative for
everything downstream of "start coding." This doc is what happens in the five minutes before `ship-issue`
step 1, and it is not a gate that blocks trivial work: a doc typo fix, a one-line dependency bump, or an
issue whose `## Acceptance` is already a literal test name doesn't need an interview.

## Worked example

Issue: "Improve the appreciation DM follow-up sequence." No test names, no numbers, `## Scope` says
"make it feel more natural."

- **Spec**: this is untestable as written. The clarifying question is concrete, not generic: is
  "improve" about touch cadence, wording/voice, or the stop-on-disinterest logic? Post the fork rather
  than picking one and building it — `_nurture_after_reply` / `dm_followups` / `dm_nurture.py` are
  three different owners depending on the answer.
- **Verifier**: once scoped (say, cadence), the check is not "read the new delay and eyeball it" — it's
  the existing `human_pacing.py` seeding contract (same `(user, action, date)` seed, Redis-persisted so
  a retry never re-rolls) plus a unit test asserting the new interval, per **test-lanes**.
  `max_dms_per_day` and the #626 pacing envelope are the bound this must never cross, and that bound
  already has an owner — the change proves it stays inside it, it doesn't reimplement it.
- **Environment**: CLAUDE.md's engagement-automation row points at `docs/engagement-automation.md` for
  the multi-touch follow-up contract; that's read before touching `dm_followups`, not after a first
  draft turns out to duplicate what `process_user_followups` already does.

## What this is not

Not a rebrand of `AGENT_WORKFLOW_PLAYBOOK.md`'s labels or `ship-issue`'s flow — both stay authoritative
for state and mechanics. Not a new CI gate — nothing here is enforced by tooling; it's a discipline for
the five minutes before code, the same way `test-lanes` is a discipline for the tests around it. Not a
license to interview on trivial work — the bar is "the shape of done is not already obvious from the
issue as written," and most well-formed LEM issues already clear that bar without a single question.
