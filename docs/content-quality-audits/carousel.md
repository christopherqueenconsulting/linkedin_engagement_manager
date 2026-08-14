# Content-quality audit — LEM's CAROUSELS

Issue #1139. Audited 2026-08-14 against `main` @ `a6d46b0d`.

The deck already has two graders. `deck_reference_report` (#728) asks whether every body slide
carries something reusable, and `_report_carousel_fact_grounding` (#619/#728) asks whether the
specifics are backed by the story bank. Both grade the **generated JSON**. This audit asks the
question neither of them can — *what does the reader actually see on the slide?* — and grades the
machinery that turns that JSON into 1080×1080 PNGs.

Owning pipeline: `create_carousel_content` → `_select_carousel_blueprint` →
`ai_helper.generate_carousel_content` → `carousel_creator.create_carousel_slide_images`
(`src/cqc_lem/app/run_content_plan.py`, `src/cqc_lem/utilities/carousel_creator.py`). Owning docs:
`docs/content-core.md`, `docs/image-stack.md`.

**Headline:** the writer side and the render side were never compared, and the render side always
wins silently. The generator is told a slide body may be **200 characters**; the schema accepts
**500**; the four layouts the plan actually selects draw at most **99–193**, and every one of them
is under the 200 the prompt hands the writer. The overflow
is not shrunk, wrapped or marked — `_draw_block` simply stops at its `max_lines` and the rest of
the sentence is gone. The same renderer discards the line structure the deck was written with (a
checklist arrives as run-on prose), invents structure it was not given (`step_framework` stamps an
arrow bullet on every WRAPPED line, so one sentence becomes three bullets), and stamps
*"Leave a comment below"* onto the closing slide of the default template — the exact engagement
bait `save_worthy_directive` forbids the writer, baked into an image no gate can strip. None of it
failed, because on every one of these seams the two sides never read each other.

---

## 1. What could and could not be sampled

The issue asked for 8–12 recently-shipped carousels via `db.py`, a real high-engagement LinkedIn
carousel fetched with LEM's scraper, and rendered before/after slides. A **Decision Comment** on
#1139 recorded that none of the live inputs are reachable from a headless agent worktree; the owner
answered **`1A 2A 3A`** on 2026-08-10: run the code-grounded audit, ship no code in this PR, and use
an in-repo gold standard as the reference exemplar. This section states the limits that answer left
standing.

| Asked for | What was actually available | Why |
|---|---|---|
| 8–12 shipped decks via `db.py` | **0 deck rows.** Slide-level evidence comes from the 6 REAL shipped slides already committed to this repo by the #1292 phase-2 pass (`assets/1292/car8{6,7}_slide*.jpg`, posts 86 and 87) | Reading `posts` / `post_carousel_slides` needs production MySQL credentials the pipeline runbook forbids touching. Owner option **1A** |
| A real fetched LinkedIn exemplar | **Not fetched.** Rubric-only, plus the named in-repo exemplar in §4 | A live authenticated Selenium session is a runbook escalation. The issue's own fallback clause covers it; owner option **3A** picked the in-repo standard |
| Rendered before/after slides | **Rendered here** — §5. Every image in §5 comes out of the REAL `create_carousel_slide_images`, run locally against this branch | The renderer is pure Pillow: no DB, no LLM, no network. That is the read path this audit is built on, and it is why the numbers in §2/§3 are measurements rather than estimates |
| Per-deck quality telemetry | **None exists.** `content_quality.surface` is `post` / `comment` / `newsletter` — there is no deck surface, and `post_outcome` carries no post type | Finding **F6** |

**How the measurements were taken.** A probe wrapped `PIL.ImageDraw.text` to record every string the
renderer actually paints, rendered decks through the unmodified `create_carousel_slide_images`, and
diffed the input slide body against the painted text. "Capacity" below is the largest body that
reaches the image intact, per template, per slide role, measured — not derived from font metrics.
The probe itself is deliberately NOT committed (option 2A is docs-only), and it is four lines:
monkeypatch `PIL.ImageDraw.ImageDraw.text` to append its string to a list, monkeypatch
`carousel_creator.select_slide_image` to return a fixed local JPEG (the band branch is the
production one), render a deck, then check whether the whole normalised body appears in the
recorded strings — ignoring the layout chrome the same call draws (`->`, `1 of N`, `n / total`).
Sweeping the body length in 5-char steps gives the table below.

### Measured render capacity — the largest slide body that survives, in characters

| Template | Selected for | Cover | **Body slide** | CTA |
|---|---|---|---|---|
| `bold_listicle` (`DEFAULT_TEMPLATE`) | awareness | 117 | **142** | 117 |
| `step_framework` | consideration | 133 | **193** | 142 |
| `stat_reveal` | decision | 142 | **99** | 133 |
| `story_arc` | personal / story (preview route) | 133 | **142** | 142 |
| `minimal_dark` | nothing — Content Studio override only | 200 | **148** | 205 |

Body-slide figures are measured **with the photo band**, which is every body slide in production:
`CAROUSEL_IMAGE_RATE` defaults to `1.0` and `CAROUSEL_IMAGES_ENABLED` to `True`, so every content
slide asks for an image and the band branch takes the tighter of the two line caps. Text-only, the
same layouts hold 254–290. The sweep steps 5 chars at a time, and the first length observed to clip
sits 4–10 chars above each figure. Every stage-selected template — all four of them — holds **less
than the 200 characters the generator prompt allows**.



**Read the capacities as this branch's behaviour, not as a threshold.** They say what the layout can
hold, not what a slide SHOULD hold. Published 2026 guidance converges on ~25–50 words (≈150–300
chars) per slide, so most of these caps are defensible on their own terms — `stat_reveal`'s 99 chars
(~15 words) is the one that is tight even by that standard. The defect is not the size of the cap.
It is that nothing upstream knows the cap exists.

---

## 2. The rubric

Grounded in this repo's own invariants, not generic taste. Each row names the ONE place that owns
it, and the verdict is against what the pipeline does today.

| # | Rubric row | Owned by | Verdict |
|---|---|---|---|
| R1 | **The first slide earns the swipe** — a promise, not a title page | `save_worthy_directive`, `hook_constraint_directive` (only when the archetype has hook styles), `_*_cover` layouts | **PASS on the writer side, UNCHECKED on the render side.** The directive requires the cover to promise what the reader gets and how many parts it has, and every cover layout renders a `1 of N` pill and a swipe hint. Nothing measures the cover the way `hook_report` measures a post's first line, and the cover's own body clips at 117 chars on the default template (F1) |
| R2 | **Per-slide readability at LinkedIn's render size** | `_load_font` sizes + `_draw_block(max_lines=…)` | **FAIL.** Type size and contrast are fine — 36–38px body on 1080px is ~24pt, inside published guidance, and legible at the 360px feed width (§5). What fails is FIT: the body is silently amputated at the line cap (F1), and the deck's own line structure is destroyed (F2, F3) |
| R3 | **Slide count in the effective range for the family** | the `conlist` bounds on each carousel model + the `schema_hint` in `generate_carousel_content` | **PARTIAL.** Educational/Insights ask for 2–4 body slides (4–6 total), CaseStudy renders 5–6, ProductDemo 4–5, and the schema floor allows a **3-slide deck** (cover + 1 + CTA). Published 2026 consensus is 6–10 slides. LEM sits at or below the bottom of that band by construction, and nothing enforces even its own 2–4 ask. Sized, not calibrated — no measured corpus here, so no number is moved |
| R4 | **Visual consistency + the image-stack rules** | `CAROUSEL_TEMPLATES`, `avatar/guardrails.avatar_allowed_for`, `build_image_brief(surface="carousel")` | **PASS on the rules, ABSENT on the brand.** No text or logo is ever rendered into a generated slide image (the brief engine owns the prompt, #1290 wired the vision gate + focal concept), and a likeness only appears where the guardrails allow. But the palette is one of five fixed template palettes chosen by BUYER STAGE, so a user's decks change look between stages, and `client_logo_url` is consumed by no layout — there is no per-user brand anywhere in a deck |
| R5 | **Per-slide copy quality — the same bar as a text post** | `evaluate_post_gates` (caption only), `deck_reference_report` (slides) | **FAIL.** The caption runs the full suite; the SLIDES run one gate. `slop_lint`, `AI_TELL_WORDS`, `POST_BANNED_SCAFFOLDS`, the bait-closer check, the similarity gate and the authenticity judge never see a slide body — and on a document post the slides ARE the post. A carousel is also never authenticity-scored at generation at all (F5) |
| R6 | **Blueprint-to-buyer-stage fit** | `_select_carousel_blueprint` → `carousel_blueprint_directive`, `_template_by_stage` | **PASS, and the strongest part of the pipeline.** Carousels rotate through the SAME post archetype menu as text posts and write into the same V51 shape history, so a deck cannot repeat the shape the last text post used; fact-anchored archetypes are taken OFF the menu when the writer has no verified anchor, precisely because slide text cannot be corrected after the render |
| R7 | **A real closing CTA slide, not a trailing summary** | the `call_to_action` field on every carousel model; `_*_cta` layouts | **PARTIAL → the worst live failure.** Structurally every deck ends on a dedicated CTA slide, and `save_worthy_directive` asks for a soft "save this for the next time you…". In production that slide (a) renders a hardcoded **"Leave a comment below"** bait pill on the default template (F4) and (b) clips at 117 chars, which is how post 87 shipped its closing ask as *"Save this for your next sprint retrospective to spark the"* |
| R8 | **The slide's photo depicts the slide's idea** | `derive_image_query` → `get_pexels_image_path` | **FAIL, unchecked.** `CAROUSEL_IMAGE_RATE` defaults to 1.0, so every body slide gets a photo band; the query is 2–4 keywords from an `lem-simple` call (heuristic fallback) and the first Pexels hit is used with no relevance check. The `lem-vision` gate #1290 wired covers only the AVATAR generation path, which is off by default (`CAROUSEL_REPLICATE_ENABLED=False`). This is the mechanism behind the two off-topic bands #1292 already recorded — a PLC photo under a software-release claim, a CD wallet under an agent-pipeline claim |

---

## 3. Findings

Nothing in this section is fixed in this PR — owner option **2A** made it audit-only, so every
finding is filed and linked in §6.

### F1 — Three different slide-length contracts, and the shortest one wins silently → **#1375**

| Layer | What it allows | Where |
|---|---|---|
| Schema | **500 chars** | `CarouselSlide.content = Field(..., max_length=500)` |
| Prompt | **"1-3 engaging sentences (max 200 chars)"** | `generate_carousel_content` |
| Renderer | **99–193 chars** on a body slide, 117–142 on a cover or CTA (stage-selected templates, production config, §1) | `_draw_block(..., max_lines=3 if band_top else 7)` and its siblings |

`_draw_block` iterates `lines[:max_lines]`. There is no ellipsis, no shrink-to-fit, no warning, and
no telemetry: the remaining lines are simply never drawn. Because `CAROUSEL_IMAGE_RATE` defaults to
**1.0**, every body slide gets a photo band, which is the branch with the SMALLER cap on every
layout — the production case is always the tighter one.

The compounding factor is that the pipeline actively pushes toward the overflow. `reference_slide_directive`
tells the writer to put a real command, threshold, number or checklist on every body slide and
"say it in full", and `deck_reference_report` **regenerates the deck** when it does not. A deck that
passes the reference gate is denser — and therefore likelier to be cut — than one that fails it.

**This corrects the diagnosis on #1375**, which attributes the clipping to python-pptx painting past
a placeholder. The published slides are not PPTX: `create_carousel_slide_images` composes them in
Pillow and `create_ppt` writes a separate downloadable deck. The fix belongs in `_draw_block` /
`_wrap_text` and in the prompt's character budget, not in the PPTX path. Measurements and
before/after renders are attached to that issue.

### F2 — The renderer flattens the line structure the deck was written with → **#1510**

`_wrap_text` starts with `text.split()`, which splits on newlines as well as spaces. A slide body
written as the checklist the reference gate asks for:

```
Release gate checklist:
- Coverage floor >= 80%
- Migration version is a timestamp
- One reviewer signs the diff
- Rollback tag recorded before deploy
```

renders as one run-on paragraph — *"Release gate checklist: - Coverage floor >= 80% - Migration
version is a timestamp - One reviewer signs the diff - Rollback tag recorded before"* — and loses
its last word to F1 on the way (§5). `slide_artifacts` scores that exact text as
`['checklist', 'threshold', 'metric']`, so the deck passes the reference gate on the strength of a
structure the reader never sees.

### F3 — `step_framework` bullets every WRAPPED line, not every point → **#1510**

`_step_content` draws a `->` marker per line returned by `_wrap`, so line breaks chosen by the
wrapper become bullet points:

> -> We rebuilt the release gate: coverage floor at 80
> -> percent, timestamped migrations, and one
> -> named reviewer per diff.

One sentence, three bullets, each starting mid-clause. `step_framework` is the CONSIDERATION-stage
default (`_template_by_stage`), i.e. the case-study deck. The marker is also drawn as ASCII `->`
because `_norm` maps `→` to `->` for the font's sake.

### F4 — The closing slide renders engagement bait the writer is forbidden to write → **#1511**

`save_worthy_directive` ends with: *"the call to action closes with a soft 'save this for the next
time you...' — **never engagement-bait**, never 'follow for more'"*. `_listicle_cta` then draws a
pill reading **"Leave a comment below"** above whatever CTA the model wrote. It is on the
awareness-stage default template and on `DEFAULT_TEMPLATE`, it is in the shipped
`assets/1292/car87_slide05.jpg`, and unlike a caption it cannot be repaired: `strip_engagement_bait`
and the `bait_closer` slop check operate on text, and this is pixels.

### F5 — Slide text passes no text-quality gate at all → **#1512** (`risk:product-decision`)

What the CAPTION gets (`evaluate_post_gates`): slop lint (HARD holds), meeting-ask CTA, affiliate,
near-duplicate similarity, focus alignment, fact grounding, authenticity. What a SLIDE gets:
`deck_reference_report` and an advisory fact-grounding log. So a slide may carry a tier-1 tell word,
a `POST_BANNED_SCAFFOLDS` opener, an em-dash pileup or a bait closer and nothing anywhere records
it — on the format where the slides are what the reader reads.

Related and separately true: **a carousel post is never authenticity-judged.**
`_score_and_persist_authenticity` is called from `create_text_post` and `rescore_post` only, so
`authenticity_score` is NULL for a generated deck and the authenticity gate in `evaluate_post_gates`
is skipped by its own `is not None` guard. The caption of every carousel ships unjudged.

Filed as `risk:product-decision` because the useful version of the fix asks whether a slide-level
lint may HOLD a post. `_report_carousel_fact_grounding`'s advisory-only posture is deliberate and is
**not** touched by this audit or its PR, per the issue's own scope rule.

### F6 — Carousels have no rendered-asset telemetry and no deck surface → **#1513**

Video posts got asset telemetry in #1281: `score_video_asset` records render outcome, duration,
aspect ratio and a file probe on the nightly beat. Carousels got no equivalent — the nightly pass
scores a carousel's CAPTION as `surface="post"` and never opens a slide. Consequences, all live
today:

- `content_quality.surface` has three values (`post`, `comment`, `newsletter`); a deck is not a
  surface, so there is no carousel trend line.
- `post_outcome` carries no post type, so "do carousels out-reach text posts?" is unanswerable from
  telemetry — for the format the whole save-worthy strategy is built on.
- Every finding above is invisible to monitoring. F1 has been shipping since the renderer existed
  and was found by a human looking at a JPEG.

### F7 — Observations recorded, not actioned

- **`minimal_dark` is unreachable from the plan.** `_template_by_stage` maps the four stages to
  `bold_listicle` / `step_framework` / `stat_reveal` / `story_arc`; `minimal_dark` is offered only
  through the Content Studio's explicit template override. Not a defect — it is a user-pickable
  style — but it is the one template whose capacities nothing in the scheduled path exercises.
- **Cover capacity is the tightest thing nobody looks at.** `bold_listicle` covers hold 117 chars of
  subtitle. The cover is where the promise lives, and R1 has no measurement.
- **`derive_image_query` spends an `lem-simple` call per body slide** (`CAROUSEL_IMAGE_QUERY_LLM`
  defaults on) to produce ≤4 keywords for a stock search that is then never checked (R8). Whether
  the call earns its keep is worth measuring once F6 exists.
- **A 3-slide deck is schema-legal.** `conlist(min_length=1)` plus cover and CTA. No deck observed
  here is that small, but nothing prevents it.

---

## 4. Reference-exemplar comparison (gauntlet-loop fallback, and why)

`.claude/skills/gauntlet-loop/SKILL.md` runs builder/critic pairs and blind-compares a BUILD against
a named reference exemplar. Owner option **2A** removed the build half of that loop — there is no
candidate change in this PR to compare — and option **3A** named the exemplar. So what ran is the
skill's discipline without its agents: a structured comparison of the carousel pipeline against a
named in-repo gold standard, stated as such rather than dressed up as a verdict trail.

**Reference exemplar (owner option 3A): the TEXT-POST quality contract** — `post_writing_directive()`
+ `POST_BANNED_SCAFFOLDS` + `slop_lint.canned_scaffold` + the #617 comment contract, audited in
`docs/content-quality-audits/text.md`. It is the right standard for three reasons: it solves the
same problem (templated sameness on a LinkedIn writing surface), it is the repo's own worked answer
to "the writer side and the checking side drifted apart", and both surfaces draw from the SAME
`content_framework` core, so a difference between them is a real gap and not a difference of medium.

| Property the exemplar has | Carousel equivalent today | Verdict |
|---|---|---|
| ONE shared list the prompt names and the lint greps (`POST_BANNED_SCAFFOLDS`), so writer and checker cannot drift | The slide length budget is stated in the prompt (200), in the schema (500) and implemented in the renderer (99–193). Three numbers, no shared constant, no test | **Gap — F1.** This is the exemplar's central invariant and the carousel path violates it three ways |
| A deterministic check with a named severity, reported per finding (`slop_lint`, WARN vs HARD) | `deck_reference_report` is deterministic and reason-carrying — genuinely exemplar-grade — but it grades reusability only. Nothing grades the slide's PROSE | **Partial — F5.** The mechanism exists and is good; the coverage stops at one dimension |
| The gate grades the artifact that ships (#1264 moved the authenticity call to the end for exactly this) | Every carousel grader reads the JSON. The artifact that ships is a PNG, and nothing reads it | **Gap — F1/F6.** The deck's own graders can pass a slide whose rendered text ends mid-word |
| One retry, then keep, with the reason logged — never an unbounded loop, never a silent drop | `deck_reference_max_attempts()` = 2, reasons logged, keeps the previous deck on a failed retry | **Match.** The one place the carousel path is fully exemplar-grade |
| A regression test pinning writer and checker to each other (`test_post_prompt_scaffold_drift.py`) | `tests/unit/utilities/test_carousel_slide_images.py` asserts file count and paths. No test renders text and measures whether it survived | **Gap — F1.** The absent test is what let F1 ship |

**Stated limitation:** this is a comparison, not a blind A/B. No builder or critic agent was run, no
output was label-stripped, and no round was capped — because with no code change in scope there was
nothing to compare blind. Read §4 as reasoning against a named standard, which is what the skill
exists to force, not as a gauntlet-loop result.

---

## 5. Before / after — rendered slides

Every image below is real output of `create_carousel_slide_images` on this branch (`bold_listicle`,
one body slide, photo band present — the production configuration). The ONLY difference between a
"before" and its "after" is the input string: **no code was changed to produce the afters.** The
band photo is cropped from a slide LEM actually shipped (`assets/1292/car87_slide03.jpg`), so the
image path is real too. Each pair is also shown at **360px — the width a LinkedIn feed gives an
image on mobile**, which is where readability is decided.

### Pair 1 — a body slide written to the prompt's own 200-char allowance

| | Slide | At feed width (360px) |
|---|---|---|
| **Before** — 196 chars written | <img src="assets/1139/body_before.jpg" width="300"> | <img src="assets/1139/body_before_feed360.jpg" width="180"> |
| **After** — 107 chars written | <img src="assets/1139/body_after.jpg" width="300"> | <img src="assets/1139/body_after_feed360.jpg" width="180"> |

| Measure | Before | After |
|---|---|---|
| Body chars written | 196 (inside the prompt's "max 200") | 107 |
| Body chars rendered | **139** | 107 |
| Chars silently dropped | **57** | **0** |
| Where the sentence stops | *"…because every merge ran the"* | it doesn't |
| Slide states its number | yes (160 releases / 32 days) | yes — the specific survives the cut |

The "before" is not a pathological input. It is a slide written to the generator's own instruction,
carrying exactly the reference value `deck_reference_report` demands, and it is the same failure the
live post 87 shipped (`assets/1292/car87_slide03.jpg`).

### Pair 2 — the checklist slide the reference gate asks for

| | Slide | At feed width (360px) |
|---|---|---|
| **Before** — 5 lines, 150 chars | <img src="assets/1139/checklist_before.jpg" width="300"> | <img src="assets/1139/checklist_before_feed360.jpg" width="180"> |
| **After** — one line, 71 chars | <img src="assets/1139/checklist_after.jpg" width="300"> | <img src="assets/1139/checklist_after_feed360.jpg" width="180"> |

| Measure | Before | After |
|---|---|---|
| Line breaks the writer supplied | 4 | 0 |
| Line breaks that survive the render | **0** | n/a |
| Chars dropped | 7 (*"…recorded before"*, losing "deploy") | 0 |
| `slide_artifacts` verdict | `['checklist', 'threshold', 'metric']` | `['threshold', 'metric']` |
| What the reader sees | a paragraph with stray hyphens | a sentence that fits |

The gate scores the "before" HIGHER (it sees a checklist) than the slide that actually reads as one.
That is F2 in one table: **the reward and the render disagree**, and the reward wins the regeneration
loop.

### Evidence — `step_framework` bullets the wrapper's line breaks (F3)

<img src="assets/1139/step_arrows.jpg" width="300">

115 chars, nothing dropped, and still wrong: one sentence rendered as three arrow bullets breaking
at *"…at 80 / percent,"*. No before/after here on purpose — the fix is a layout change, which is
#1510's, not this PR's.

---

## 6. What shipped in this PR, and what was filed

**Code changed: none.** Owner option **2A** — this PR is the audit document, its rendered evidence,
and the follow-ups. No prompt, preset, gate, threshold or severity is touched; in particular
`_report_carousel_fact_grounding` keeps its advisory-only posture, per #1139's own scope rule.

Shipped:

- `docs/content-quality-audits/carousel.md` (this file) and `docs/content-quality-audits/assets/1139/`
  — 5 rendered slides plus their feed-width copies, all real output of the unmodified renderer.
- One pointer from `docs/content-core.md`'s deck-reference-gate section, which is where the "both
  graders read the JSON, nothing reads the PNG" gap belongs.

Filed and linked:

| # | Finding | Why it is not here |
|---|---|---|
| **#1375** (existing, `priority:high`) | F1 — silent clipping | Already open; this audit added the measured capacities, the corrected diagnosis (Pillow `_draw_block`, not python-pptx) and the before/after renders as a comment |
| **#1510** | F2 + F3 — the renderer destroys the deck's line structure and invents its own | A layout change to `_wrap_text` / `_step_content`; 2A |
| **#1511** | F4 — hardcoded "Leave a comment below" bait on the CTA slide | 2A |
| **#1512** (`risk:product-decision`) | F5 — slide text passes no text-quality gate; carousels are never authenticity-judged | Asks whether a slide lint may HOLD a post — a product call, per the issue's own rule |
| **#1513** | F6 — no deck surface in `content_quality`, no rendered-asset probe, no post type on `post_outcome` | 2A |
| **#1515** (`needs-human`) | The corpus §1 could not read — 8–12 real decks scored from the production read path | Needs production credentials and an owner authorisation, exactly like #1292 |

Still not answered, and honestly so: **no real carousel corpus was read and no live LinkedIn
exemplar was fetched** (§1). Every number in §1–§3 describes the CODE's behaviour, measured exactly;
none of them describes how LEM's decks perform in a feed. That is **#1515** — phase 2, the same
shape #1292 took for images: sample 8–12 shipped decks from the production read path, score them
against §2, and report characters-dropped as a RATE rather than as the anecdotes this pass could
reach.
