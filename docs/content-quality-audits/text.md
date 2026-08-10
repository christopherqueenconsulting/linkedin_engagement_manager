# Content-quality audit — LEM's TEXT POSTS

Issue #1138. Audited 2026-08-10 against `main` @ `596da857`.

The deterministic gates LEM already runs (`slop_lint`, the #617 comment contract, `evaluate_post_gates`,
`AUTHENTICITY_RUBRIC.md`) answer "did this draft do anything forbidden?". This audit asks the other
question — *does the text read like a person worth following, and would a LinkedIn reader stop for
it?* — and grades the machinery that produces it.

Owning pipeline: `create_text_post` in `src/cqc_lem/app/run_content_plan.py`. Owning docs:
`docs/content-core.md`, `docs/AUTHENTICITY_RUBRIC.md`, `docs/content-scheduling.md`.

**Headline:** the writer side and the checking side had drifted apart. LEM's four post system
prompts predate the shared content core and were handing the model literal canned templates —
including two containing words the project's own tier-1 wordbank bans — while every deterministic
layer downstream was built to catch exactly that shape. Nothing failed, because nothing was
comparing the two sides. This PR fixes the prompts, makes the ban list a single shared constant,
adds the deterministic check that would have caught the drift, and adds the regression guard that
keeps the two sides pinned to each other.

---

## 1. What could and could not be sampled

The issue asked for 10–15 recently-shipped text posts pulled via existing `db.py` readers, and a
real high-engagement LinkedIn post as the reference exemplar. Both were bounded by where this audit
ran, and the limits are stated here rather than papered over:

| Asked for | What was actually available | Why |
|---|---|---|
| 10–15 shipped post BODIES via `db.py` | **0 bodies.** The scorecard below is built from the `content_quality` PostHog telemetry instead — every text post LEM has scored since the #630 nightly beat started, which is **5 posts, one account** | The audit runs headless in an agent worktree. Reading post bodies means production MySQL credentials, and the pipeline runbook forbids touching `.env` / prod secrets. The telemetry is the read path that does not need them |
| A real, fetched LinkedIn exemplar | **Not fetched.** Rubric-only assessment, plus an in-repo exemplar for the gauntlet loop (§4) | Fetching one means a live authenticated Selenium session against LinkedIn — a runbook escalation trigger, not something to do headless. The issue's own fallback clause covers this: *"if you can't name and fetch a real exemplar… fall back to a rubric-only assessment and say so"* |
| Render the sample through `LinkedInPostPreview.tsx` | **Not rendered.** The fold was measured numerically instead (`LINKEDIN_FOLD_CHARS` = 210 desktop, `MOBILE_HOOK_MAX_CHARS` = 140 mobile) | No post bodies to render. The screenshot step is scope item 5, not an acceptance criterion; the mechanical question it answers (does the hook survive the "…see more" cut?) is answered deterministically by `hook_report` / `dwell_metrics` |

All three are tracked as **#1267**, to be re-run where those inputs exist.

**Read the scorecard as sizing, not calibration.** Five posts from one account can show that a gap
exists; it cannot set a threshold. Every recommendation below that would require a calibrated
number is filed as a follow-up issue with "calibrate it" in its acceptance criteria, never shipped
here on n=5.

### Scorecard — every text post LEM has scored (`content_quality`, 2026-07-29 → 2026-08-09)

| ref_id | shipped | chars | hook chars (≤140?) | slop hard / warn | authenticity | self-similarity (embedding) | impressions | ER |
|---|---|---|---|---|---|---|---|---|
| 33 | 2026-07-28 | 896 | 39 ✅ | **1** / 1 | *unscored* | **0.848** | 63 | 6.3% |
| 37 | 2026-07-30 | 761 | 100 ✅ | 0 / 2 | 85 | 0.640 | 46 | 8.7% |
| 79 | 2026-08-03 | 1054 | 79 ✅ | 0 / 1 | 90 | 0.657 | 51 | 7.8% |
| 81 | 2026-08-05 | 949 | 86 ✅ | 0 / 0 | *unscored* | 0.633 | 94 | 3.2% |
| 82 | 2026-08-07 | 1885 | 81 ✅ | 0 / 0 | 85 | **0.832** | 48 | 8.3% |

What the numbers say on their own, before any rubric:

- **The hook mechanics are fine.** 5/5 land inside the 140-char mobile budget. This is the one
  rubric row the pipeline is already good at, and no change here touches it.
- **Length runs short.** Four of five sit at 760–1050 chars against the prompt's own 1300–2000
  target and the 180–400-word dwell band. Not a defect on its own — a tight post beats a padded
  one — but it means the "earn the length with substance" instruction is not landing.
- **Semantic self-similarity is high** and only ever measured, never gated (finding F3).
- **Two of five shipped unscored for authenticity** — the gate is skipped, not failed, when
  scoring did not run, and an unscored post auto-approves (`_score_and_persist_authenticity`
  fails open by design).
- **Post 33 shipped with a HARD slop violation.** That is not a gate bypass: the nightly telemetry
  re-lints the SHIPPED text, and the gate ran against the draft at generation time. It is worth a
  look on its own and is not chased here.

---

## 2. The rubric

Grounded in this repo's own invariants, not generic taste. Each row names the ONE place that owns
it, and the verdict is against what the pipeline actually does today.

| # | Rubric row | Owned by | Verdict |
|---|---|---|---|
| R1 | **Hook lands before the fold** — first line readable whole in the mobile preview | `hook_report` (140), `dwell_metrics.hook_within_fold` (210), `optimize_post_hook` | **PASS.** 5/5 measured inside budget. Two different fold constants answer the same question, which is defensible (mobile vs desktop) but undocumented — noted, not filed |
| R2 | **Native formatting, no wall of text** | `shape_for_dwell` (deterministic reflow), `dwell_report` scannability | **PASS.** The reflow runs on whatever draft the review gate keeps, so a rewrite cannot re-introduce a wall |
| R3 | **Voice matches the author** | `get_or_create_profile_synthesis`, `_voice_reference` | **PASS by construction.** The stable weekly synthesis, not the volatile full profile, is the voice source |
| R4 | **Buyer-stage fit** — an awareness post reads top-of-funnel | The per-stage blocks in each generator's prompt | **PARTIAL → fixed here.** The Decision-stage instruction in the industry-news prompt literally read *"partnering with an expert in [user's specialty] can ensure…"* — a pitch, in the 70% slot that is supposed to sell nothing. Rewritten (F1) |
| R5 | **CTA clarity + placement** — artifact CTA, never a meeting ask | `cta_policy_directive`, `contains_meeting_ask` + `replace_meeting_ask_cta`, the `meeting_cta` gate | **PASS, and belt-and-braces.** Banned in the prompt, repaired deterministically, and the gate holds anything that survives |
| R6 | **Engagement mechanics** — a real point of view, a question worth answering | `slop_lint` bait closer, `strip_engagement_bait`, the blueprint's `cta_style` | **FAIL → fixed here.** The prompts supplied the generic closers themselves, and *none* of them is caught by any existing check (verified: `contains_engagement_bait`, `closing_reflex_ask` and the tier-1 wordbank all return clean on "What experiences have shaped your professional growth?", "How is your organization addressing this shift?", "I'd love to hear your thoughts!", "Share your experiences below!"). See F1 |
| R7 | **LinkedIn conventions — no link in the body** | `post_to_linkedin`'s #392 split | **PASS, correctly nuanced.** The prompt bans body links; at publish, an off-platform link is held back for the first comment while a `linkedin.com` newsletter link is deliberately left in the body, because the reach penalty is off-platform only |
| R8 | **No canned sameness** — the post could not paste under any other author | `POST_BANNED_SCAFFOLDS` + `slop_lint.canned_scaffold` (**new, this PR**), similarity gate | **FAIL → partly fixed here.** The prompts were the SOURCE of the sameness (F1). The semantic half of the gate is still missing (F3) |

---

## 3. Findings

### F1 — The post prompts contradicted the checking side *(fixed in this PR)*

CLAUDE.md states the invariant plainly: `AI_TELL_WORDS` is the ONE wordbank, *"the SAME wordbank the
humanization pass steers against, so the writer side and the checking side can never drift apart."*
The four post system prompts in `ai_helper.py` were written before that core existed and had drifted:

| Generator | What the system prompt told the writer to write | Why it is a defect |
|---|---|---|
| `get_thought_leadership_post_from_ai` | *"Use phrases like: 'In my experience as a [Job Title]…', 'One of the biggest challenges in [Industry] today is…', 'A strategy I've found effective involves…'"* | Three templates that paste unchanged under any post in any industry — the semantic sameness LinkedIn's 2026 update demotes, handed over as a worked example |
| same | *"…a statistic that **underscores** the importance"* | `underscore` is a tier-1 tell word |
| same | Closers: *"How is your organization addressing [trend or challenge]?"*, *"What strategies have you found successful in navigating [relevant issue]?"* | Generic closers, caught by nothing |
| `get_industry_news_post_from_ai` | *"it's **crucial** to consider approaches like…"* | `crucial` is a tier-1 tell word — the prompt hands the writer a word the lint bans |
| same | *"Given this development, partnering with an expert in [user's specialty] can ensure…"* | A pitch in the value slot (R4) |
| `get_personal_story_post_from_ai` | *"Reflecting on my **journey** as a [job title]…"* | `journey` is a tier-1 tell word, inside a canned opener |
| same | *"This experience taught me that…"*, *"One key takeaway for me was…"* | Canned reflection scaffolds |
| `generate_engagement_prompt_post` | *"I'd love to hear your thoughts!"*, *"Share your experiences below!"* | Literal engagement bait — which `strip_engagement_bait` then tries to remove downstream. The pipeline was arguing with itself |

The sharpest version of the seam, verified in the tree:

```python
>>> has_first_person_proof(
...     "In my experience as a Solutions Architect, one of the biggest challenges "
...     "in consulting today is scope creep.")
True
```

The canned phrase the prompt supplied **satisfies the A2 first-person proof slot** — because "one"
reads as a concrete-specificity signal — while containing no lived detail whatsoever. The proof
gate was being fed by the prompt that defeated it.

**Fixed:** every canned template and tier-1 tell word removed from all post prompts;
`post_writing_directive()` (the ONE place invariant post rules live — no parallel helper) now names
the banned scaffolds and requires a closing question answerable only from the post itself.

### F2 — The authenticity score describes a draft that may never ship → **#1264**

`create_text_post` scores authenticity *before* `_review_generated_post`, which can regenerate the
post. The retry re-enters with `similarity_check=False`, which is exactly the flag guarding the
scoring call — so the stored score is the discarded draft's, and that is what the demote-to-PENDING
gate and the nightly telemetry read back. Not fixed here: re-scoring changes which posts
auto-publish and costs a judge call per regenerated post.

### F3 — The post similarity gate is lexical-only → **#1265**

The gate measures token-set overlap at 0.55; the nightly telemetry measures `lem-embedding` cosine
and reports 0.63–0.85 on every post scored. Comments got the embedding-first gate in #617; posts
never did. Not fixed here: an embedding call per post plus a new hold condition.

### F4 — The A2 proof detector counts "one of the…" as a specific → **#1266**

The loophole behind F1's `has_first_person_proof` result. Not fixed here: tightening it increases
regenerations, and the detector deliberately errs toward "counts as proof" to bound that cost.

### F5 — Observations recorded, not actioned

- **`_check_tada` is HARD severity on a bare substring list.** The round-2 critic's blind verdict
  (§4) named this: `"…rebuild the team from three to thirty — let that sink in, because it wasn't
  luck"` is human emphatic writing, and `let that sink in` is a literal `TADA_TRANSITIONS` entry,
  so a good draft is regenerated then held. Real, but the evidence is a constructed sentence, not a
  measured production draft — and re-severity-ing an existing HARD check on that basis would
  violate this audit's own "gates unchanged unless targeted with evidence" rule. Left open here
  deliberately; worth a measured pass when there is a post corpus to grade.
- **Two of five posts shipped with no authenticity score.** Worth knowing why before it is treated
  as a defect.
- **Post 33 shipped carrying a HARD slop violation** as re-linted at T+1. Not a gate bypass; the
  gate graded the draft, the telemetry grades the shipped text.

---

## 4. Gauntlet-loop verdict trail

Run per `.claude/skills/gauntlet-loop/SKILL.md`. Two pieces, one builder and one **fresh-context**
critic each, blind A/B (labels stripped, order shuffled), capped at 3 rounds.

**Reference exemplar — named and in-repo:** `content_framework.comment_contract_directive()`, the
#617 COMMENT QUALITY CONTRACT. It is this repo's gold standard for a writer-side contract (numbered,
each rule falsifiable, a banned list shared with the checking side) and it solves *the same problem
on the sibling surface* — templated sameness on a LinkedIn text surface. Chosen because no real
LinkedIn post exemplar could be fetched headless (§1), and the skill's own rule is that an in-repo
gold standard beats a hypothetical.

**Stated limitation:** the comparison was label-blind, not indistinguishable — a critic reading a
comment contract next to a post contract can tell which is which. The verdicts below are therefore
comparative judgements against the project's invariants, not a true double-blind.

| Round | Piece | Builder proposal | Critic verdict (fresh context) | Resolution |
|---|---|---|---|---|
| 1 | **Writer-side post contract** | Extend `post_writing_directive()` with a scaffold ban naming `POST_BANNED_SCAFFOLDS`, plus a "closing question must be answerable only from THIS post" rule; strip the canned menus from all four system prompts | **Build wins.** *"It is doing real invariant-enforcement work, not just restating a spec"* — it independently reconstructs what `CHECK_CONTRASTIVE` / `CHECK_TADA` / `CHECK_BAIT_CLOSER` / `CHECK_EM_DASH` grade, and encodes the artifact-CTA-never-meeting-ask invariant. Biggest gap named **in the exemplar**: its rule 6 ("never reuse the shape of a comment you already left") is unfalsifiable from inside a single-shot prompt | Shipped as drafted. The gap is in the exemplar, not the build, so no round 2. The unfalsifiability critique was applied to the new rules: both added clauses are decidable by a reader looking only at the post |
| 1 | **Deterministic scaffold check** | `canned_scaffold` in `slop_lint.py` — WARN severity, post-only, reading the shared `POST_BANNED_SCAFFOLDS` | **Build wins.** *"Genuinely new coverage… the single highest-precision source of a machine tell, since it can only match text patterned on what the model was literally shown"*; WARN called *"the doctrinally correct call"* for a check that fires on a single hit against a fixed phrase list. Biggest gap named **in the exemplar** (`_check_tada`): HARD severity over a bare substring list, false-positive case given | Shipped as drafted. The exemplar's gap is recorded as F5, not fixed — changing an existing HARD gate on a constructed example is out of this audit's scope |

Neither piece hit the 3-round cap; nothing is parked `needs-human`.

---

## 5. Before / after

The pipeline could not be run end to end here (no LLM credentials in the agent worktree), so the
"before" draft is **written to the old system prompt's own worked examples** — the exact phrases it
told the writer to use — and the "after" draft to the new contract. Both are trimmed for
readability, so they sit under the 1300–2000 char target; the measured delta is the scaffold and
dwell components, not length. Everything in the table is reproducible with the deterministic
graders in the tree.

**Before** — written to the pre-#1138 prompt:

> In my experience as a Solutions Architect, one of the biggest challenges in logistics today is scope creep.
>
> Teams start with a clear brief. Then a new stakeholder joins, a new integration appears, and the timeline quietly doubles.
>
> A strategy I've found effective involves freezing the interface contract before the first sprint. Everything else can move; that cannot.
>
> One key takeaway for me was that scope creep is rarely a planning failure. It is a decision-rights failure.
>
> How is your organization addressing scope creep on integration projects?

**After** — written to the shipped contract:

> We froze the API contract on a freight-visibility build in March. The scope still doubled.
>
> Three carriers, one shipper, eleven weeks. By week four we had two new stakeholders and a request to add EDI 214 events nobody had costed.
>
> What saved the delivery was not the frozen contract. It was that we had written down who could say yes to a new event type, and it was one person, not a committee.
>
> So the contract held on paper and moved in practice, because the decision rights were the real constraint.
>
> We shipped in thirteen weeks instead of eleven. The two weeks went to EDI 214, and everyone knew, on the day it was approved, what they cost.
>
> If you had frozen that contract, which of those eleven weeks would you have spent differently?

| Measure | Before | After |
|---|---|---|
| `canned_scaffold` phrases (new check) | **5** — `in my experience as a`, `one of the biggest challenges in`, `a strategy i've found effective`, `one key takeaway for me was`, `how is your organization addressing` | **0** |
| `dwell_report` score | 78 | **85** |
| Hook within the 140-char mobile budget | ✅ 107 | ✅ 90 |
| Tier-1 tell words | 0 | 0 |
| A2 first-person proof | ✅ (falsely — F4) | ✅ (genuinely: a named month, a named event type, real counts) |
| Slop lint HARD | 0 | 0 |
| Lexical similarity of the two drafts | 0.13 — a different post, not a paraphrase | |

The point of the pairing is not that the second draft is prettier. It is that **the first draft was
what the pipeline asked for**, it passed every deterministic gate LEM had, and only the check added
here can see anything wrong with it.

---

## 6. What shipped in this PR

- `content_framework.POST_BANNED_SCAFFOLDS` — the sampled scaffold list, provenance rule documented
  (every entry traceable to one of LEM's own post prompts, extended only on new sampled evidence).
- `post_writing_directive()` names it, plus the specific-closing-question rule. Extended, not
  duplicated — CLAUDE.md forbids a parallel per-content-type prompt helper.
- `slop_lint.canned_scaffold` — WARN, post-only, reading the SAME constant via `banned_scaffolds()`
  (extensible per-deploy with `SLOP_LINT_EXTRA_SCAFFOLDS`, promotable with
  `SLOP_LINT_SEVERITY_CANNED_SCAFFOLD`). WARN means recorded and reported, never a hold: **no post
  publishes differently because of this PR.**
- All six text-post prompt builders scrubbed of canned templates and tier-1 tell words
  (thought leadership, industry news, personal story, engagement prompt, blog summary, website
  content).
- `tests/unit/utilities/ai/test_post_prompt_scaffold_drift.py` — the regression guard. It captures
  the REAL system prompt each generator sends and fails the build if a banned word or a canned
  scaffold is ever written back into one. This is the test whose absence let the drift happen.
- `tests/unit/utilities/ai/test_canned_scaffold_lint.py` — the check, including the pin that the
  writer side and the checking side read one list.

Existing gates are untouched: no threshold moved, no severity changed, no new hold condition.
