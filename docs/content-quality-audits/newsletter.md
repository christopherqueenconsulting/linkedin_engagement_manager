# Content-quality audit — LEM's LinkedIn NEWSLETTERS

Issues #1142 (desk audit) and **#1284 (this re-run, against real data)**. Re-audited 2026-08-11
against `main` @ `389ba4b5`, with production data and a live LinkedIn exemplar.

Newsletter editions combine two of the other audited surfaces (body text, cover image) into
LinkedIn's specific newsletter format. A subscriber digest/notification is a different reading
context from a feed scroll: the title + subtitle are the subject line + preview text, the cover is
the only visual asset, and the whole edition competes against other inbox items rather than against
the next post in a stream.

Owning pipeline: `_topup_newsletter_drafts_for_user` → `plan_newsletter_topics` → edition write
(`src/cqc_lem/app/run_scheduler.py`), `newsletter_cover.py`, `blog_source.py` (`align_with_blog`
resolution). Owning docs: `docs/newsletter-covers.md`, `docs/content-core.md`.

**Headline:** #1142 ran headless with no bodies and no exemplar, and it graded two rubric rows PASS
that the real corpus fails. Reading all ten editions LEM has written — five of them published —
against a live 842,798-subscriber newsletter in the same niche turns three of its judgements over:
**digest-ability is not a PASS** (9 of 10 editions carry a wall-of-text paragraph, 6 of 10 have no
list block), **the newsletter CTA is not a PASS** (7 of 10 close by routing the reader to a comments
box, 2 of 10 invite a subscribe), and **the editions are half the length the prompt asks for**
(mean 672 words against its own 800 floor; the exemplar runs 1,667–2,364). Two defects only a body
read could find: **three of the five PUBLISHED editions shipped a bare `CTA` line** — the
blueprint's own section label, printed for subscribers — and **every edition's self-similarity has
been recorded as unmeasured** while the real corpus sits at 0.68–0.83 embedding cosine against
itself. This PR fixes the label leak deterministically, pins the structural floor into the shared
writer contract, and gives the nightly telemetry the newsletter history reader it never had.

---

## 1. What was sampled, and how it was obtained

#1284 existed because #1142 could read neither production bodies nor a live newsletter. Both inputs
were available for this run (the owner authorised prod access on the issue's Decision Comment,
`1A 2A`), and both were taken through **existing readers only** — no new query path, no write:

| Asked for | What this run used | How |
|---|---|---|
| 8–12 shipped editions via `db.py` | **10 editions, 5 of them `published`** (ids 1–10, one account — that is every edition LEM has ever written) | `db.get_newsletter_edition(id)`, the shipped per-id reader, looped over ids in the production Celery worker. Read-only |
| A real, actively-growing LinkedIn newsletter | **AI Frontier** (Steve Nouri) — 842,798 subscribers, published weekly, 161 editions; two full editions read | The read-only live probe on the debug Grid node: `scripts/linkedin_live_validation.py --require-debug-node --newsletter-url … --newsletter-edition …` (new surfaces, §6) |

**The corpus is one account and it is all PRE-contract.** #1142's writer-side contract shipped in
PR #1289 and reached production in `v0.145.0` at 06:04 UTC on 2026-08-11 — after every edition here
was written. So this scorecard is not a verdict on that contract; it is the **baseline it was
written against**, measured for the first time, plus the gaps that survive it (§4).

Deliberate limits, stated rather than papered over: n=10 from one account cannot calibrate a
threshold, so every recommendation needing a number is filed as a follow-up with "calibrate it" in
its acceptance criteria. Approved/draft editions are included and marked — they are written by the
same pipeline, and excluding them would leave n=5.

### Scorecard — every newsletter edition LEM has written (2026-07-07 → 2026-09-08 slots)

Deterministic graders only (`content_quality.score_item`, `content_framework.dwell_report`,
`slop_lint.lint_report`, `content_quality.similarity_reports`). Reproducible from the same readers.

| id | status | words | opening line (≤210?) | longest para | list block | bare label line | slop HARD | dwell | self-similarity | cover |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | published | 812 | 119 ✅ | 358 ⚠️ | no | **`CTA`** | 1 | 67 | 0.828 | none |
| 2 | published | 466 | 269 ❌ | 407 ⚠️ | no | **`CTA`** | 1 | 63 | 0.797 | none |
| 3 | published | 428 | 288 ❌ | 459 ⚠️ | yes | **`CTA`** | 1 | 72 | 0.812 | none |
| 4 | published | 473 | 411 ❌ | 645 ⚠️ | no | — | 0 | 48 | 0.768 | none |
| 5 | published | 670 | 209 ✅ | 210 | no | — | 1 | 81 | 0.828 | pending_review |
| 6 | approved | 747 | 521 ❌ | 583 ⚠️ | yes | — | 1 | 46 | 0.709 | pending_review |
| 7 | approved | 511 | 297 ❌ | 568 ⚠️ | no | — | 1 | 60 | 0.800 | pending_review |
| 8 | approved | 846 | 198 ✅ | 371 ⚠️ | no | — | 0 | 67 | 0.800 | pending_review |
| 9 | approved | 1,087 | 233 ❌ | 494 ⚠️ | yes | — | 0 | 56 | 0.709 | pending_review |
| 10 | draft | 677 | 102 ✅ | 401 ⚠️ | yes | — | 1 | 77 | 0.684 | pending_review |

Totals: **3/10** reach the 800-word floor · **4/10** open inside the 210-char fold · **9/10** carry a
paragraph over 300 chars · **4/10** carry a numbered/bulleted block · **3/10** printed a structural
label (all three PUBLISHED) · **7/10** still trip a HARD slop check after the bounded regeneration ·
**7/10** close by sending the reader to the comments · **2/10** invite a subscribe · self-similarity
0.684–0.828 (mean 0.77), title similarity 0.372–0.711 (mean 0.60) · **0/10** covers ever approved.

### The exemplar, and its control

| | AI Frontier (Steve Nouri) | Superhuman ― AI Insights (Zain Kahn) | LEM (n=10) |
|---|---|---|---|
| Subscribers | 842,798 | 328,285 | not applicable (own newsletter) |
| Cadence the page states | Published weekly, 161 editions | "Published daily", 3 editions | weekly |
| Newest edition | 5 days old | ~2 years old | slots through 2026-09-08 |
| Edition length | 1,667 and 2,364 words | — | 428–1,087 (mean 672) |
| Longest rendered block | 355 / 289 chars | — | 210–645 (mean 450) |
| Per-edition engagement | 338–1,378 reactions, 28–65 comments | 197–1,115 reactions | **not captured** — `get_shipped_content_for_quality` records reactions/impressions as None for this surface, so ER is structurally unmeasured for newsletters |
| Close | a thesis line, no ask; subscribing is the PAGE control | — | 7/10 ask for a comment |

Superhuman is in the table on purpose: it is the **control** proving subscriber count is not the
signal. A 328k list that last published two years ago is a dead newsletter with a big number on it,
which is why the exemplar is AI Frontier — 161 editions, weekly, newest five days old.

---

## 2. The rubric, re-graded against real editions

Same five rows as #1142. The verdicts change where the data disagrees with the desk read.

| # | Rubric row | Owned by | #1142 verdict | Verdict on the real corpus |
|---|---|---|---|---|
| R1 | **Title/subject-line hook** — title + subtitle earn the open | `generate_newsletter_edition` title/subtitle lines | PARTIAL → fixed | **PARTIAL, unresolved.** The title fix shipped, but titles remain templated ACROSS editions: 0.372–0.711 cosine between them, mean 0.60. Six of ten read as topic labels ("The AI Engagement Playbook: Boosting LinkedIn Success") |
| R2 | **Cover-body cohesion** — the approved cover matches the body | `build_cover_prompt`; `_approved_cover_path` | PARTIAL → fixed | **UNTESTABLE IN PRODUCTION, cause found (#1432).** Five editions have a generated cover, all `pending_review`; four published editions had no cover at all. **Zero covers have ever reached LinkedIn**, so cohesion has never been exercised. #1432 found nothing broken in the gate — the approval was simply never *asked for* (see `docs/newsletter-covers.md` §"Why no cover was ever approved") and shipped the ask. **Re-grade this row once the first cover actually ships** — that needs a live approval, so it is tracked separately, not claimed here |
| R3 | **Digest-ability** — scannable in a notification context | `dwell_directive` / `dwell_report` | **PASS** | **FAIL → fixed here.** 9/10 carry a wall-of-text paragraph (to 645 chars), 6/10 have no list block, mean dwell 64. The exemplar's blocks top out at 289–355 chars |
| R4 | **Blog-alignment fidelity** — the edition tracks its source | `resolve_blog_source` + the prompt | PARTIAL → fixed | **UNGROUNDED.** No edition in the corpus resolved a blog source, so the fidelity wording is still unmeasured against real output. Stands as #1286 |
| R5 | **Newsletter-appropriate CTA** — reply + subscribe, not feed mechanics | `cta_policy_directive`; the CTA prompt line | **PASS, clarified** | **FAIL → fixed here.** 7/10 close with a comments ask ("Answer in the comments", "Tell me in the comments") — wording the old ban ('comment below') did not catch — and only 2/10 invite a subscribe |

A sixth row the desk audit had no way to see, added here:

| R6 | **The body is the reader's, not the pipeline's** | `_clean_newsletter_body` | — | **FAIL → fixed here.** Three of the five PUBLISHED editions carry a bare `CTA` line, and one also carries `KEY TAKEAWAYS` as its own line above the block. `CTA` is the blueprint's instruction to the writer; it was published to subscribers |

---

## 3. Findings

### F6 — The blueprint's section labels were published as body text *(fixed in this PR)*

Editions 1, 2 and 3 — three of the five that actually shipped — contain a line reading exactly
`CTA`, immediately above their closing question. The blueprint hands the writer a structure skeleton
("hook, problem, example, takeaways, CTA"), the model echoed the label, and nothing downstream
removed it: `_clean_newsletter_body` only stripped markdown.

The fix is deterministic, in the ONE place a newsletter body is cleaned. `_clean_newsletter_body`
drops any line that is *only* a structural label (`NEWSLETTER_STRUCTURAL_LABELS` — `CTA`, `HOOK`,
`INTRO`, `BODY`, `CONCLUSION`, `SECTION 2`, decorated or numbered variants), and the writer-side
directive names the same list, so the writer side and the cleaning side cannot drift. A
reader-facing heading is deliberately NOT on the list: `KEY TAKEAWAYS` over a takeaways block is
structure a human editor keeps, and the dwell grader rewards it.

### F7 — The structural floor was prose, so it was optional *(fixed in this PR)*

`generate_newsletter_edition`'s docstring has always said "~800–1200 words … a strong hook", and the
#1142 directive said "short paragraphs … never a wall of text". Measured, that guidance is not
holding: 3/10 reach the floor, 4/10 open inside the fold, 9/10 exceed the paragraph ceiling the
dwell grader already measures, 6/10 offer nothing to scroll back to.

The directive now states the floor as numbers the graders already own —
`LINKEDIN_FOLD_CHARS` (210), `DWELL_PARAGRAPH_MAX_CHARS` (300), and
`NEWSLETTER_WORD_FLOOR`/`NEWSLETTER_WORD_CEILING` (800/1200) — plus "at least one numbered or
bulleted block". Naming the constants rather than repeating literals is what keeps the writer side
and the measuring side one number.

### F8 — "In the comments" is a feed reflex the ban did not cover *(fixed in this PR)*

The #1142 directive banned `'comment below'`. The corpus closes with "Answer in the comments so we
can compare notes", "Tell me in the comments where…", "Share your approach in the comments" — 7 of
10 editions, including 4 of the 5 published. In a subscriber digest that is the wrong verb: the
reader is in an inbox, not under a post. The exemplar does not ask at all — it closes on a thesis
and lets the page's Subscribe control do the conversion work.

The CTA rule now bans routing the reader to a comments box **in any wording**, and asks for a REPLY
to the edition plus the subscribe line. Whether the subscribe invite has a real destination is still
#1288.

### F9 — Newsletter self-similarity was structurally unmeasured *(fixed in this PR)*

`auto_nightly_content_quality` graded self-similarity for posts and comments only; its own comment
said newsletters "have no body-history reader, so their similarity reports as unmeasured". Measured
now, the corpus sits at **0.684–0.828 embedding cosine** against itself (mean 0.77) — on the post
surface that is regression-alert territory, and the field has been NULL the whole time.

`get_recent_newsletter_bodies` (newsletter repository, exported through the `db.py` facade) is the
counterpart of `get_recent_post_texts`, and the nightly pass now reads it into the per-surface
history map. This ships MEASUREMENT only: no threshold, no gate, nothing paused. Calibrating a
newsletter similarity ceiling against a corpus bigger than one account is **#1433**.

**#1433 answered it: no ceiling ships, and the measurement changed one thing.** §8 below records the
decision and the corpus that could not support one. What #1433 did ship is the consequence of this
finding rather than a threshold on it — once editions carry a `similarity` value, the rollup's
POOLED mean moves with the week's surface MIX, so `similarity_creep` is now graded on the
per-surface split (`content_quality.mix_adjusted_similarity_delta`).

### F10 — The HARD slop checks are not clearing on this surface → **#1434** *(answered below)*

Seven of ten editions still trip a HARD check after the bounded regeneration (`contrastive_frame`
×5, `banned_lexicon` ×2), and a newsletter is returned anyway by design — it is drafted for human
review. That design is right; the number is not. With `SLOP_MAX_ATTEMPTS=2` the writer gets exactly
ONE retry, and the retry is a full-edition regeneration whose new draft can trip a different check.
Filed rather than fixed here: raising the attempt budget costs a `lem-complex` call per edition and
needs the cost/benefit measured, not guessed.

#### F10 answered (#1434): the clear-rate was not recoverable, and that is the finding

**The measurement.** The clear-rate of the single regeneration **cannot be computed from anything
that exists**, and the reason is structural rather than an access problem:

* The corpus keeps the **last** draft only. A `posts`/newsletter row holds the edition the loop
  ended on, so an edition that entered the retry, cleared `contrastive_frame` and came back with
  `banned_lexicon` is byte-for-byte indistinguishable from one whose retry never moved.
* The traces cannot fill the gap either. LiteLLM runs with `turn_off_message_logging: true`
  (`.litellm/config.yaml`), so `$ai_input` / `$ai_output_choices` are redacted before any callback
  sees them — no `$ai_generation` row carries the intermediate draft. There is no replay corpus.
* The corpus cannot even say which editions entered the loop. The lint reached this surface in
  `dc466773` (2026-07-26) and the sampled editions run from a 2026-07-07 slot, so an unknown number
  of the ten were written when no regeneration existed to grade.

What the corpus **does** bound: 7 of 10 editions ended on a HARD check, 3 ended clean. So a single
regeneration cleared its check on **at most 3 of the editions that entered it, and possibly none** —
the corpus cannot separate "the first draft was clean" from "the retry fixed it". That range is too
wide to price a third `lem-complex` call against, which is exactly what the issue refused to guess
at.

**What shipped instead**, all of it at **$0.00 per edition — no additional LLM call on any path**:

1. **The retry is now recorded.** One `slop_retry` event per regeneration carries
   `outcome` ∈ {`cleared`, `traded`, `worsened`, `persisted`, `lost`, `unsteered`}, `kept` (whether
   that draft survived — a `persisted` row can be the edition that shipped OR one that was
   discarded, so the two are not the same reading), the HARD check names before and after, and
   `attempt`/`max_attempts`. `traded` is broken out from `persisted` on purpose: it
   is the failure mode a whole-draft rewrite has and a targeted edit does not, so its share is what
   decides between "buy another attempt" and "stop rewriting the whole edition". `unsteered` is
   broken out for the opposite reason: the structural floor (#1435) shares this budget, so an
   edition that was slop-clean going in can spend a regeneration with no HARD check to fix, and
   scoring those as `cleared` would inflate the very clear-rate #1530 reads off this event — grade
   it over the steered rows (`hard_before > 0`). Check names only — a violation's `evidence` is
   draft text.
2. **The budget can no longer end on the worse of two drafts.** The loop took the newer draft
   unconditionally, even when the rewrite came back carrying more violations than the one it
   replaced. `slop_lint.keep_retry` ranks the two reports on (HARD count, total violations) and
   keeps whichever is better, ties going to the retry. Both reports were already in hand; this
   spends nothing. On the newsletter the rank carries a middle term — the structural floor (#1435)
   steers the same retry off the same body, so the ordering is (HARD slop count, structural
   failures, total violations). Without it the slop half alone would discard the draft that fixed
   the other grader: a too-short first draft trips no slop check at all, so a full-length
   replacement carrying one WARN ranks worse on slop and better on everything it was asked for.
3. **The attempt budget is now per-surface** (`SLOP_LINT_MAX_ATTEMPTS_<SURFACE>`, resolved ahead of
   the global `SLOP_LINT_MAX_ATTEMPTS`), because what an attempt costs is a property of the surface:
   an edition is a `lem-complex` call on a weekly cadence, a feed comment a `lem-medium` call at
   volume. **The newsletter default is unchanged at 2.** Raising it is one env value once the
   `slop_retry` rows exist, and raising it without them would be the guess the issue ruled out.

**Scoped to this surface:** the recording and the keep-the-better-draft rule are the NEWSLETTER
loop's only. `lint_repaired` (seed comments, replies, DMs) and the affiliate promo draft run the
same bounded retry, still take the newer draft blind, and emit nothing — the same two defects, on
higher-volume surfaces with no review queue behind them. Only the per-surface budget was widened
here, because that one is a config read with no behaviour change. Widening the other two is
**#1536**.

**Still open:** the number itself. Re-run this reading once `slop_retry` has covered a full
newsletter cycle — if `cleared` dominates, buy the third attempt; if `traded` dominates, the retry
directive needs to edit the offending sentences rather than re-author the edition, and no attempt
budget will fix it. Tracked as **#1530**.

#### F10, #1530 first read (2026-08-15): the window has not opened — **0 rows**

The reading is one command now, and running it is what this entry records:

```bash
POSTHOG_PERSONAL_API_KEY=… poetry run python scripts/slop_retry_clear_rate.py --days 30
poetry run python scripts/slop_retry_clear_rate.py --print-sql   # no key, paste into the UI
```

**What the query returns today: nothing, and that is arithmetic rather than a defect.** The
instrument merged 2026-08-14 17:52 UTC (#1531) and shipped in **v0.151.0**; prod reports `0.151.1`.
The newsletter draft beat runs daily at **10:00 UTC** (`generate-newsletter-drafts`), so at the time
of this read — 2026-08-15 00:49 UTC — the beat had not fired once since the deploy, and PostHog
holds **zero `slop_retry` rows** against a healthy ingest (`motion_prompt_check` and
`content_quality` are landing in the same window). The event does not exist in the project taxonomy
yet.

Three things follow, and they are the reason this is a dated entry rather than a number:

* **The earliest honest read is mid-September 2026.** Editions publish weekly and the beat only tops
  a queue up, so ≥1 month of editions is single-digit regenerations. `slop_retry_clear_rate.py`
  refuses to state a rate under a 10-steered-row floor (`--min-rows`) and exits non-zero, for the
  same reason §8 refuses a 10-edition similarity threshold: a percentage read off two rows renders
  identically to a measured one.
* **The per-surface breakdown Scope 1 asks for needs #1536 first.** `track_slop_retry` is called
  from the newsletter loop only, so today every row would say `surface=newsletter` — the script
  already splits by surface, and the other surfaces will populate it when #1536 widens
  `lint_repaired` and the affiliate promo loop.
* **The formula is pinned in code, not in prose.** `cleared` / (`cleared` + `traded` + `worsened` +
  `persisted` + `lost`), `unsteered` excluded from that denominator and reported as its own share of
  all rows (`tests/unit/scripts/test_slop_retry_clear_rate.py`). That exclusion is #1434's review
  guard: a slop-clean edition that is too short spends this budget on the structural floor, and
  scoring it `cleared` would inflate the number the budget decision turns on.

**`SLOP_LINT_MAX_ATTEMPTS_NEWSLETTER` therefore stays at 2**, unchanged, on the same grounds #1434
gave: the third `lem-complex` call is still unpriced.

### Findings carried forward from #1142

F1 (prompts targeted the wrong channel), F2 (`NEWSLETTER_BANNED_SCAFFOLDS` → #1285), F3
(blog-fidelity gate → #1286), F4 (cover-body cohesion gate → #1287), F5 (subscribe destination →
#1288) all shipped or remain filed as stated there. **F2's follow-up #1285 closed while this audit
was in flight**: `canned_scaffold` is now HARD on the newsletter surface and WARN everywhere else
(§7). F2's list was checked against this corpus:
**zero editions match any banned scaffold**, which is consistent with a WARN-severity list sampled
from earlier drafts — the sameness in this corpus is structural (0.77 self-similarity), not phrasal.

---

## 4. Gauntlet-loop verdict trail

Run per `.claude/skills/gauntlet-loop/SKILL.md`: one piece (the newsletter pipeline's output
quality), a builder proposal, a fresh critic pass, capped at 3 rounds. Unlike #1142, the reference
exemplar is REAL and fetched (§1), and the comparison is made against **generated output**, not
hand-written illustrations: both arms were generated in production, same subject, same blueprint,
same profile, with only the writer contract swapped.

**Round 1 — builder proposal.** Three changes: strip the blueprint's structural labels
deterministically (F6), state the structural floor in the writer contract using the graders' own
constants (F7), and ban the comments-box ask in any wording (F8).

**Round 1 — critic, against real generation.** Both arms were generated in the production worker:
same two subjects, same `select_blueprint("newsletter")` shape, same profile and synthesis, same
models, with ONLY `newsletter_writing_directive()` swapped. Two editions per arm.

| measure | control (shipped #1142 contract) | treatment (this PR's contract) | exemplar |
|---|---|---|---|
| words | 446 · 815 | 561 · 760 | 1,667 · 2,364 |
| longest paragraph | 343 · 313 | **512 · 525** | 355 · 289 |
| opening line ≤210 | 1 of 2 | 0 of 2 | yes |
| list block | 1 of 2 | **2 of 2** | yes |
| slop HARD | 0 · 2 | **0 · 0** | — |
| bare structural label | 0 · 0 | 0 · 0 | — |
| close | "What do you think? … I'd love to hear about it" + subscribe | **"reply to this edition with the exact impression count"**, "Reply with your perspective" + subscribe | a thesis line, no ask |

**Critic verdict: the build wins on the CTA and on slop, and LOSES on the structural floor.** The
treatment's closes are newsletter-native in both editions (a reply ask, not a comments ask, which is
the corpus's most common defect at 7/10) and neither trips a HARD check. But its paragraphs came
back *longer* than the control's — 512 and 525 characters against a 300-character rule stated
explicitly in the prompt it was given. On n=2 per arm that is not a measurement of the rule's
effect; it is enough to say the rule did not visibly bind.

**Biggest remaining gap, and what it changed:** *a writer-side instruction with no checking side does
not hold.* Everything in this audit that measurably closed, closed because something deterministic
ran — the label strip, the similarity reader. The floor is kept in the contract (it costs nothing
and states the target for the human reviewer too), but the honest conclusion is filed as **#1435**:
grade the finished edition with the `dwell_report()` the tree already has, and feed its failures
into the SAME bounded regeneration the slop lint already uses, then re-run this A/B with ≥4 editions
per arm.

**Found while running the loop — a defect the corpus could not show.** Three of the six production
title passes in these runs came back as the model addressing the operator rather than writing a
headline: *"Could you please share the draft headline you'd like me to rewrite?"* `humanize_title`
guards on length and on hype-word count, and an aside passes both — so it REPLACED the edition's
real title and would have published as the subject line. `content_alignment.is_assistant_aside()`
now fails such a rewrite back to the original (fail-open, the same posture as every other guard in
that function). Round 2 was not needed: the finding is a deterministic fix, not a contract argument.

The loop stopped after one round. Nothing is parked `needs-human`.

### 4a. #1435 — the checking side, measured

The A/B above was re-run against the checking side #1435 shipped, at the ≥4 editions per arm that
issue asked for. Same harness discipline as the round-1 run, now committed so it is repeatable:
`scripts/measure_newsletter_structure_ab.py`. Four fixed subjects, one blueprint per subject from
`select_blueprint("newsletter")`, one profile synthesis, seeded RNG so both arms draw the same
temperatures, and the two arms for a subject generated back to back. The arms differ by ONE
environment variable — `NEWSLETTER_STRUCTURE_ENABLED` — and nothing else.

| measure (n=4 per arm) | control (checking side off) | treatment (#1435) |
|---|---|---|
| words, mean | 791 | **845** |
| inside the 800-1200 band | 2 of 4 | **3 of 4** |
| longest paragraph, mean | 432 | **307** |
| longest paragraph, worst | 490 | **356** |
| no paragraph over 300 | **0 of 4** | **3 of 4** |
| opening line ≤210 | 2 of 4 | **3 of 4** |
| list block present | 3 of 4 | 3 of 4 |
| all four floors passed | 0 of 4 | **2 of 4** |
| dwell score, mean | 68 | **76** |
| slop HARD, total | 3 | 3 |
| drafts per edition | 1.50 | 1.75 |

**Cost, measured rather than assumed: +0.25 drafts per edition.** A draft is three calls — one
`lem-complex` (the edition), one `lem-simple` (the title de-hype) and one `lem-medium` (the body
humanization) — so the checking side cost 21 calls across 4 editions against the control's 18. The
cap is unchanged: both graders share the ONE `SLOP_LINT_MAX_ATTEMPTS` budget (default 2 drafts), so
an edition can never spend more than it could before this change. The worst case is that every
edition now uses the retry the slop lint alone would sometimes have skipped.

**Round 1 of this run said something the design had to answer.** Graded with the retry carrying all
four repairs, the treatment fixed the fold (2 of 4 → 4 of 4) and paid for it in LENGTH: mean words
fell from 768 to 565 and 0 of 4 editions stayed in the band. A regeneration told to split its
paragraphs *and* hold 800-1200 words trades the second for the first. Two changes followed, and the
table above is the re-run:

- **The wall-of-text repair moved into code.** `content_framework.newsletter_shape_body()` reflows
  an over-long paragraph deterministically — the same `enforce_post_readability` reflow
  `shape_for_dwell` uses for posts, with the length cap made unreachable so an edition can never be
  trimmed. The retry is left to carry only what code cannot write: the opening line, the list block,
  and the length. This is the audit's own lesson applied to itself.
- **The retry states the floor it must not spend.** `newsletter_structure_directive()` closes with
  "do NOT shorten the edition to satisfy the repairs above".

The first-draft records show the reflow doing exactly that work with no generation at all: both arms
wrote the same first drafts (identical 756-word mean, same seeds), and the treatment's were already
3 of 4 wall-free where the control's were 0 of 4.

**What did NOT move, stated plainly.** The list block is 3 of 4 in both arms — on this n the retry
did not add one, and nothing here can. That row also carried a grader gap, found in review and fixed
here: the newsletter writer prompt asks for list items `beginning with a literal "-> "`, and
`sanitize_for_linkedin` rewrites only `- `/`* ` into a bullet, so an arrow list reached `has_list`
verbatim and read as NO list at all. `_LIST_LINE_RE` now recognises the arrow forms, so an edition
written exactly as the contract asks no longer spends a shared draft on a floor it already met — the
3-of-4 above is therefore a FLOOR on the real number, not a measurement of it. Two of four treatment editions still miss a floor, which is
the expected outcome of a one-retry budget, and both were kept and returned for review. Slop HARD is
unchanged (3 in each arm), as it should be: the structural side shares the budget but grades
something else. And the reflow only reaches SINGLE-LINE prose blocks, so a 356-character paragraph
that already contains a line break survives it — the one treatment edition still over the ceiling.

Nothing here holds or pauses an edition: a still-failing edition is returned with its reasons at
INFO. Not WARNING — this is the common case on the real corpus (9 of 10 editions carried a wall),
and the escalation contract in `src/cqc_lem/utilities/CLAUDE.md` says a recurring warning for
working behaviour files a defect against it.

---

## 5. What shipped in this PR

- `content_framework.NEWSLETTER_STRUCTURAL_LABELS` + `NEWSLETTER_WORD_FLOOR` /
  `NEWSLETTER_WORD_CEILING` — the label list and the length band, named once.
- `ai_helper._clean_newsletter_body` drops bare structural-label lines (F6), keeping reader-facing
  headings.
- `content_alignment.is_assistant_aside()` — a rewritten title that is the assistant talking to the
  operator falls back to the original (§4).
- `content_framework.newsletter_writing_directive()` states the structural floor in the graders' own
  constants (F7) and bans the comments-box ask in any wording (F8).
- `newsletter.get_recent_newsletter_bodies` + the `db.py` re-export, and
  `auto_nightly_content_quality` reads it as the newsletter similarity history (F9).
- `scripts/linkedin_live_validation.py`: `--newsletter-url` grounds
  `_read_newsletter_subscriber_count` against a page whose count is stated in its own text (the
  drift that would flatline `newsletter_subscriber_stats`), and `--newsletter-edition` samples a
  published edition for exemplar evidence. Both read-only, neither in the weekly sweep;
  `docs/sdui-probe-coverage.md` carries both rows.
- Tests: label stripping and the directive's floors (`test_newsletter_generation.py`), the history
  reader (`test_db_newsletter.py`), the per-surface similarity wiring
  (`test_content_quality_telemetry.py`), the title-aside guard (`test_humanize.py`), and the probe
  verdicts (`test_linkedin_live_validation.py`).

**What did NOT change:** the cover-approval gate (`_approved_cover_path`), the schema, the API, the
SPA, cadence math, the publish flow, and any threshold or gate. Nothing new can hold or pause an
edition.

**Telemetry note:** newsletter rows will start carrying a `similarity` value instead of NULL from
the first nightly run after this merges. Read that as the measurement starting, not as similarity
appearing — and it is the trend line, never a gate (`docs/content-quality-telemetry.md`).

---

## 6. Follow-up issues

Filed by this audit:

- **#1432** — no newsletter cover has ever been approved, so every shipped edition went out
  coverless and R2 is untestable in production.
- **#1433** — calibrate a newsletter self-similarity ceiling now that the dimension is measured.
- **#1434** — the newsletter slop-lint retry budget clears HARD checks on 3 of 10 editions.
- **#1435** — give the structural floor a checking side; the A/B above says the wording alone does
  not bind. **Closed** — the checking side and its re-run A/B are §4a.

Still open from #1142: **#1286** (blog-alignment fidelity gate), **#1287** (cover-body cohesion
gate), **#1288** (subscribe destination). **#1285** (scaffold severity) closed while this audit was
in flight — §7 below.

---

## 7. Calibrated scaffold severity (#1285)

**Decision: `canned_scaffold` is HARD on the newsletter surface, WARN everywhere else.**

Severity is now resolved per surface (`slop_lint.SURFACE_SEVERITIES`), not once per check. The
reason it can differ is that the false-positive risk differs: on a POST the same check can fire on a
templated opener that carries a real specific ("In my experience as a Solutions Architect, we cut
deploys to 9 minutes"), which is why #1138 shipped it at WARN. A newsletter scaffold is pure runway
— "in today's edition", "without further ado", "here's what you need to know" say nothing, and no
edition needs them.

The newsletter surface is also where a HARD verdict is cheap. An edition is drafted days ahead of
its slot and always lands in the review queue, so HARD does **not** block a publish. What it buys is
the bounded regeneration: `slop_retry_directive` steers only on `report["hard"]`, so at WARN a
scaffold was recorded and then shipped unchanged. At HARD the edition is rewritten once with the
phrase named, and an edition that still trips the lint is kept for review with the reasons logged —
the same fail-open behaviour `generate_newsletter_edition` already had.

**Ops knobs** (read at call time, no restart):

| Variable | Effect |
|---|---|
| `SLOP_LINT_SEVERITY_CANNED_SCAFFOLD_NEWSLETTER=warn` | Demote newsletters back to advisory, posts unaffected |
| `SLOP_LINT_SEVERITY_CANNED_SCAFFOLD=hard` | Promote every surface (an explicit env value beats the per-surface default) |
| `SLOP_LINT_EXTRA_SCAFFOLDS` | Extends BOTH the writer directive and the linter — never one side |

**The measurement is still owed.** The severity call was made on the phrase list's provenance (every
entry sampled from LEM's own newsletter prompt output) rather than on a hit rate, because the corpus
the issue asks for does not exist yet — the #1142 audit found ONE shipped edition in the telemetry
window. `scripts/sample_newsletter_scaffolds.py` is the sampler that produces it: read-only, run
against a database with real editions, it reports the per-edition hit rate, which phrases actually
fire, which banned phrases are dead entries, how #630 has been scoring the same editions, and
candidate phrases repeated across two or more editions that are not yet banned.

```
poetry run python scripts/sample_newsletter_scaffolds.py --days 3650
```

It refuses to imply a calibration it cannot support: under 20 editions the report prints
`NOT ENOUGH`, and an empty corpus reports no hit rate rather than 0%. It also reports the HARD cost
as a counterfactual (`would_hold` = editions carrying a scaffold) SEPARATELY from what the linter
graded on the run (`held_today`), so a run under a demoted severity — or with the surface's linter
switched off — cannot read as "no edition would have been held". Adding anything it surfaces to
`NEWSLETTER_BANNED_SCAFFOLDS` is a human read of that shortlist, not an automatic step — a phrase
can repeat because it is the author's real vocabulary, which is the opposite of a scaffold.

**What this re-audit adds to that measurement.** The 10-edition corpus in §1 is the first one big
enough to look at, and **zero editions match any banned scaffold** (§3, "Findings carried forward").
So promoting the check to HARD holds nothing on the corpus that exists — the number in F10 is
unaffected by this severity — and it is still under the sampler's 20-edition `NOT ENOUGH` floor, so
it neither confirms the phrase list nor retires an entry from it. The sameness this corpus does
carry is structural (0.77 self-similarity), which is #1433, not a scaffold question.

**Telemetry discontinuity (second one, same cause as §5's):** the #630 nightly beat re-lints shipped
newsletters, so from the merge date a newsletter scaffold moves from `slop_warn` to `slop_hard`. The
step is this severity landing, not quality moving. Content-quality telemetry is a trend line and
never gates anything (`docs/content-quality-telemetry.md`); the newsletter publish flow is unchanged.

---

## 8. Calibrated newsletter self-similarity (#1433)

**Decision: no newsletter self-similarity threshold ships — not a rollup ceiling, not a
generation-time gate, not a title check. What ships instead is a mix-adjusted `similarity_creep` and
the sampler that will produce the corpus a threshold needs.**

### 8.1 The corpus the issue asked for does not exist

#1433 asks for **20+ editions across more than one account**, or a stated reason that is impossible.
It is impossible, and not just today:

- **Accounts.** LEM production is a single-owner deployment: one active account, which is the same
  account §1's ten editions came from. A second account's editions cannot be sampled because there
  is no second account, and the number cannot be reached by waiting.
- **Editions.** That account has published weekly since 2026-07-07. §1's corpus (ids 1–10, every
  edition LEM has ever written) reaches 20 somewhere around November 2026 — and all 20 would still
  be one editorial line, which is the half of the requirement that actually matters. Twenty editions
  of one newsletter measure how consistently that newsletter covers its subject, not where the
  boundary between "consistent" and "templated" sits.
- **Substitutes were considered and rejected.** Grading a competitor's published editions (the §1
  exemplar path) measures a corpus LEM's prompts did not write, and the pipeline's own A/B
  generations (§4) are two editions per arm on the same subject by construction — both would produce
  a number with no relationship to the thing being thresholded.

So the honest measured distribution remains §1's: body self-similarity **0.684–0.828** (mean 0.77),
title **0.372–0.711** (mean 0.60), n=10, one account, all pre-contract.

### 8.2 Why no number is picked from it

1. **The level is expected, not a defect.** A newsletter has ONE subject by design — that is what a
   subscriber signed up for. The post surface's ceilings (`POST_EMBEDDING_SIMILARITY_MAX` 0.78,
   `POST_SIMILARITY_MAX` 0.55) exist because a feed post competes against the author's OWN last
   post in the same scroll. Reusing either number here would hold or alert on the newsletter doing
   its job, and #1433 says so in its own scope: a threshold that fires on normal editorial
   repetition is worse than none.
2. **The rollup is a trend line, never a gate** (`docs/content-quality-telemetry.md`). Its alerts
   are already relative — an account against its OWN prior period — so a newsletter that really
   collapses into a template still shows up as a rise, with no absolute ceiling required.
3. **The safety posture is already different.** Every edition waits in a human review queue days
   before its slot. A generation-time gate (the posts' pattern) would spend a `lem-complex`
   regeneration to pre-empt a reader who is going to look at the draft anyway.
4. **The title check has the same problem one scale down.** Five of ten titles read as topic labels
   and cluster at 0.60 — a real quality finding, and it is a finding about *how titles are written*,
   which is #1284's writer contract and the `humanize_title` path, not a number. A 0.60-ish title
   ceiling calibrated on ten titles from one newsletter would fire on the next edition of the same
   series.

### 8.3 What DID have to change: the pooled mean moves with the mix

The one thing #1284's measurement forced. `summarize_scores` pools every surface into one
`similarity_avg`, and the surfaces sit at different baselines by design — a newsletter at ~0.77
against a post gated at 0.78 and a comment lower again. Once editions carry a value instead of NULL,
**a week that published two editions raises the pooled mean without any writing converging**, and
`similarity_creep`'s 0.05 delta is easily cleared by that alone. That is an un-calibrated newsletter
threshold arriving by the back door: not stated anywhere, and firing on the publishing schedule.

`content_quality.mix_adjusted_similarity_delta` is the fix. Each surface's own week-over-week move,
averaged with THIS period's per-surface samples as the weights, over the surfaces measured on both
sides. `evaluate_alerts` grades that instead of the pooled delta; the pooled number stays exactly as
it was for the dashboard and rides along on the alert as `pooled_delta`. Consequences:

- The shared `CONTENT_QUALITY_SIMILARITY_DELTA` (0.05) is unchanged and stays surface-agnostic — it
  thresholds a *move*, which is comparable across surfaces in a way a *level* is not.
- A newsletter surface that really does converge still alerts, on the same delta as everything else.
- Two periods with no surface in common produce **no verdict** rather than a pooled one: `None`, and
  the pooled delta is not consulted as a second chance.
- `CONTENT_QUALITY_MIN_SAMPLE` counts the SHARED surfaces' samples once the split is grading, not
  the pooled `similarity_sample` — otherwise one edition against one edition could raise the alert
  while the period reported twenty scored pieces.
- Nothing is held, paused or regenerated. It is still the trend line.

### 8.4 The sampler that unblocks the calibration

`scripts/sample_newsletter_similarity.py` — read-only, no writes, no browser, and it re-uses
`similarity_reports` so it measures exactly what the nightly pass measures — including its window,
`COMMENT_HISTORY_LIMIT` editions per account, which is both what `run_scheduler` reads and the cap
`similarity_reports` puts on the history pool (`--limit` is clamped there, because reading further
back would report more editions than the scores actually came from). It reports the
leave-one-out distribution (min / p25 / median / mean / p75 / p90 / max) for **bodies and titles
separately**, split per account and per measure (cosine and token overlap are never pooled), against
the post ceilings as a labelled *reference, not a verdict*.

```
poetry run python scripts/sample_newsletter_similarity.py
```

It refuses to imply a calibration it cannot support: under **20 editions** or under **2 accounts**
with a comparison it prints `NOT ENOUGH` and `sufficient_corpus` is false. An account with a single
edition contributes zero comparisons and does not count toward the account floor — one edition has
nothing to be similar to, and reporting it as 0.0 would invent a reading.

**What would reopen this decision:** that sampler returning `sufficient_corpus: true` — a second
account with a real edition history, i.e. LEM running someone else's newsletter. Until then a
number would be an opinion with a decimal point on it.
