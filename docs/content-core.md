# Unified content core — full posture

The review gate is the ONLY place post similarity is measured, and it **RECORDS** the verdict on
`posts.gate_reason`; the gate pass then re-reads that value rather than measuring again, so holding a
post costs no second embedding call. `rescore_post` is the one path that measures live, because it
runs on text the author has just edited.

The TL;DR for the unified content core (newsletter + post + comment) lives in
[CLAUDE.md](../CLAUDE.md) under **Known Gotchas → Unified content core**. This file holds the
load-bearing detail any contributor needs before changing the comment contract, the story bank
flow, the slop lint, or the deck reference gate.

## Files

| File | Purpose |
|---|---|
| `src/cqc_lem/utilities/ai/content_framework.py` | ONE blueprint core (archetype/hook/CTA menus per content type + shared variety engine) |
| `src/cqc_lem/utilities/ai/content_research.py` | ONE research layer (lem-research→Perplexity fallback; per-type cost toggles) |
| `src/cqc_lem/utilities/ai/content_alignment.py` | ONE alignment core (voice synthesis + prefs + LEM purpose + promo policy) |
| `src/cqc_lem/utilities/ai/story_bank.py` | ONE fact layer (the user's own anecdotes/numbers — the only permitted specifics) |
| `src/cqc_lem/utilities/ai/slop_lint.py` | ONE deterministic AI-slop lint (no LLM) run on every surface |

## Comment research + quality contract

Comment research is OFF by default (`COMMENT_RESEARCH_ENABLED`) because comments run at high
volume; the target post is their grounding. Comments also carry their own **quality contract +
similarity gate** (issue #617, `content_framework.py`):

### Quality contract

A draft must:
- reference a specific claim from the target post
- add one of {own experience, data point, respectful disagreement, genuine question}
- run ≥2 sentences
- never open with validation filler

### Similarity gate

Must not near-duplicate the user's last 50 posted comments. `COMMENT_SIMILARITY_MAX` is compared
via embedding cosine using `lem-embedding`, with a token-overlap fallback. A failing draft is
regenerated up to `COMMENT_GATE_MAX_ATTEMPTS` times and then the post is SKIPPED —
`generate_ai_response` returns `None`, never a failing comment. The post-history uniqueness engine
(opener/subject avoidance steering + the `post_similarity_report` review gate in
`create_text_post`, mirroring the newsletter's V49/V50 dedup) also lives in
`content_framework.py`. Trend-based post subjects are ANCHORED to the user's `focus_topics`
(rotated per post_id via `select_focus_topic` in `content_alignment.py`), not just their profile
industry.

## Post similarity gate (issue #1265)

`post_similarity_report` is the ONE place a post's similarity is decided — the same shape as the
comment gate above, and the same reason: a REWORDED earlier post scores low on token overlap and
high on cosine, and semantic sameness is what the 2026 ranking demotes.

| | Measure | Ceiling | When |
|---|---|---|---|
| Preferred | `lem-embedding` cosine | `POST_EMBEDDING_SIMILARITY_MAX` (0.78) | always, when the embedding endpoint answers |
| Degraded | token-set overlap | `POST_SIMILARITY_MAX` (0.55), or the user's `post_similarity_max_pct` | embedding endpoint unavailable |

Load-bearing details:

- **It degrades, it never disarms.** No embedding ⇒ the deterministic overlap gate posts have always
  had, never "nothing is similar".
- **0.78 is calibrated, not picked.** Every text post `content_quality` had scored when #1265 shipped
  (user 1, 5 posts): 0.633 / 0.640 / 0.657 distinct, 0.832 / 0.848 the reworded pair. One account's
  five posts SIZES the gap; retune as `content_quality_scores` fills out.
- **The user's `post_similarity_max_pct` setting governs the fallback only.** It is a percentage on
  the token-overlap scale, where two unrelated posts sit at 0.2-0.4; cosine puts them near 0.5, so
  applying that percentage to cosine would hold nearly everything.
- **Over the ceiling is ONE retry, then HELD** (#1452). The retry is the path the lexical gate always
  took; what changed is where the still-over draft ends up. It no longer auto-publishes — it lands
  **PENDING** carrying the `similarity` finding, which NAMES the measure that fired because a cosine
  score and an overlap score are not readable against each other. It is a hold, not a block: the
  draft is kept, and the author can approve it as-is or edit and re-score.
- **The verdict is RECORDED, not re-measured** (#1452). `post_similarity_report` runs inside
  `_review_generated_post`, which is the only place the post history and the embedding call live —
  by the time `_gate_findings_for_post` runs, neither is in scope. So the review gate writes its
  verdict onto `posts.gate_reason` (the shape `_record_video_probe_finding` uses for the same
  reason) and the gate pass re-reads it, which is why the hold costs no second `lem-embedding` call
  and no second history read. Two consequences that bite: the review gate writes on EVERY reviewed
  draft, because a verdict left by an earlier draft would hold a clean regeneration forever; and
  `rescore_post` never reads the recorded verdict — it hands `evaluate_post_gates` a live
  `recent_texts` instead, since grading the text the author just edited is the entire point of a
  re-score.
- **One measure vocabulary.** `SIMILARITY_MEASURE_{EMBEDDING,LEXICAL,NONE}` in `content_framework.py`
  is what the gate, the comment gate and the nightly telemetry (`content_quality.MEASURE_*`, which
  aliases them) all name a measure by — so the trend line in `docs/content-quality-telemetry.md` and
  a hold on the same post can never disagree silently.
- **Cost:** ONE `lem-embedding` call per generated post (a second only on the retry path), batching
  the draft with the whole history. Empty history ⇒ no call at all.

## Story bank (issue #620, `story_bank.py` + the `story_bank` table)

FACT half of the content core. `create_text_post` selects ONE of the user's own entries per post
(relevance, then least-used/longest-unused rotation) and its facts are the only personal
specifics the writer may state. An empty or irrelevant bank ships an explicit no-fabrication
fallback (industry observation) instead of an invented anecdote. A first-person specific that
traces to no supplied source regenerates once (`POST_FABRICATION_REGEN_ENABLED`).

`profiles.synthesis` still feeds VOICE; the bank feeds FACTS.

### Save-targeted archetypes (issue #619)

Two save-targeted post archetypes live in the same `POST_FORMATS` menu: `build_receipt` and
`resource_compendium`. They are marked `save_targeted` (so scheduling can prefer them via
`select_blueprint(prefer_save_targeted=True)`) and `fact_anchored`, which narrows their hook
menu to `NUMBER_LED_HOOK_STYLES` (lead with a real number, ~140-char mobile budget) and turns on
the **no-fabrication guard**: the writer may only state a specific that a VERIFIED fact backs,
otherwise it must ship as a `[[LABEL: …]]` placeholder.

### Two widths on purpose

The verified facts are the story bank's, at two different widths:

- **WRITER's allow-list** is only the ONE entry this post was anchored to (carried on the
  blueprint as `fact_anchors`, since a number from some other entry was never in its prompt).
- **CHECKERS** (`_review_generated_post` and the `fact_grounding` gate, via
  `run_content_plan._fact_anchors`) count EVERY active entry, because a number out of the
  user's own material is by definition not one the model invented.

`fact_grounding_report` grades the draft deterministically. An invented number costs one
regeneration and then holds the post PENDING behind the `fact_grounding` quality gate, and
unfilled placeholders hold it too until the author fills them in (a re-score of human-EDITED
text treats the author's own numbers as verified, or the hold could never clear). An empty bank
means every such draft is placeholder-only and approval-gated.

### Occasion / milestone archetypes (issue #1074)

Two more archetypes live in the same `POST_FORMATS` menu — `project_launch` and
`educational_milestone` — and they are the only ones nothing may pick automatically. LinkedIn's
native "Celebrate an occasion" composer (Start a post → More → Celebrate an occasion) creates an
entity the REST API has no equivalent for, so these drafts are written by LEM and published BY HAND.

- **Off the automatic menu.** `_rotatable()` drops anything marked `occasion` from the three places
  a shape is chosen without a human: `select_blueprint`'s rotation, `enforce_variety`'s repair, and
  the planner menu `options_text` hands an LLM. They stay reachable through `preferred_formats=[…]`
  or an explicit `guidance` hint — which is how the ONE caller that has a real event reaches them.
  The reason is not tidiness: a rotation that could land on `project_launch` would eventually
  announce a launch that never happened.
- **Not a cadence slot.** They are absent from `POST_DAY_TYPES` and carry no `content_mix` class, so
  an occasion post neither fills a weekly slot nor moves the 70/20/10 ratios. Rare by design (~1 a
  month), seeded by the author naming a real event.
- **Fact-anchored.** What the author typed IS the anchor: `draft_occasion_post` wraps it as a
  synthetic story-bank entry, so the writer gets the bank's absolute "these are the ONLY personal
  specifics you may state" rule without a parallel directive.
- **`posts.manual_publish` is the enforcement.** `get_ready_to_post_posts` never returns such a row
  (nor does the orphan re-queue), and `post_to_linkedin` refuses one that reaches it anyway — the
  single choke point every publish passes through. `POST /user/post/mark-posted` is the author
  saying they published it; it is refused for any post that is NOT `manual_publish`, because for
  those 'posted' is written by the task that holds the LinkedIn URN.

#### Phase 2 — driving that composer with Selenium (issue #1088)

The route has no API, so Phase 2 is a browser doing exactly what the author would: Start a post →
More → Celebrate an occasion → pick the occasion → type → Post. `utilities/linkedin/share_composer.py`
is the ONE place it is driven (mechanics only — the share-box trigger chain moved there too, because
the group composer opens the same control and a second copy is drift waiting to happen);
`app.engagement.posting.auto_publish_occasion_post` owns the policy. Four things are load-bearing:

- **OFF by default, `occasion-native-publish-enabled`.** Read at BOTH ends — `auto_check_scheduled_posts`
  never queues the message with it off, and the task refuses if it is flipped off mid-flight. With it
  off, everything above is unchanged: the author still copies the draft across by hand. The flag must
  not be flipped on until `scripts/linkedin_live_validation.py --occasion-composer` has been run live
  and its JSON recorded on #1088 — with the flag off, nothing else drives those anchors.
- **A separate queue, never a loosened filter.** `get_ready_occasion_posts` asks for
  `manual_publish = 1`, the exact mirror of the `= 0` that keeps `post_to_linkedin` off these rows.
  Two queries, so one row can never reach both the API path and the browser path.
- **The row is CLAIMED (`scheduled`) before Chrome opens.** An occasion announcement published twice
  is public and un-deletable, and the read that would tell us it already went out is the read that can
  fail. So the claim is released back to `approved` only for the states that provably left nothing on
  LinkedIn (no share box / no occasion affordance / no matching occasion type / no editor / no Post
  button / a browser fault), and each of those grades a `zero_walk` verdict rather than skipping
  quietly. A Post click the feed never confirmed is held at `error` for a human — the row is still
  `manual_publish`, so the Content Studio's "I posted this" control is exactly where it was, and at
  that status the panel says *check LinkedIn first* instead of its usual "paste the text below":
  the draft the author is being shown may already be live. A claim whose worker died mid-composer
  is recovered the same way, by `get_orphaned_occasion_claims` — never re-queued, because a dead
  worker proves nothing about whether Post was pressed, and because `get_orphaned_scheduled_posts`
  excludes `manual_publish` rows (re-queueing one would publish through the API the very post that
  exists because the API cannot carry it).
- **The occasion TYPE is an exact allow-list.** `OCCASION_TYPE_LABELS` maps each archetype to its
  own label and nothing near it: "Certification" sits next to "Educational milestone" in LinkedIn's
  menu, and clicking it publishes a claim the author never made (#1012). A type that does not
  resolve aborts the run; it never settles for the neighbour, and never falls through to publish the
  body as an ordinary update — which is the post #1074 exists to avoid.

Bounded by the same `posting_days` the content plan is (fails open on an unreadable preference), and
a blocked attempt holds its run lock for an hour so a rotated composer costs one Chrome session, not
six.

**The 2026-08-17 grounding pass did NOT clear the route, and the flag stays off because of it.**
`--occasion-composer` resolved the share-box trigger (`div[role='button']`, text "Start a post"),
clicked it, and **nothing opened** — no `role='dialog'`, no overlay container, no `role='textbox'`,
and the URL never moved off `/feed/`. So every anchor below the trigger is still unproven: this run
says nothing about them. The trigger is the SHARED chain `auto_post_to_group` opens too, so that is a
production finding in its own right and is tracked separately; re-run `--occasion-composer` once it
is fixed, and read `modal_containers` / `composer_controls` before touching any occasion label.
`--probe-composer` grading `ok` on the same session is not a contradiction: it falls back to a
page-wide control scan when the dialog lookup misses, so it graded the FEED's 84 controls.

## Carousels

Carousels draw from the same menu via `carousel_blueprint_directive` and persist their shape into
the same V51 rotation history. Since issue #728 they run the SAME two-width split:
`create_carousel_content` selects ONE entry (`_select_story_for_post`) and hands the writer only
that entry's `fact_sources` plus its `story_directive`, while `_report_carousel_fact_grounding`
grades the finished SLIDES against every active entry. The carousel used to pass
`_fact_anchors(user_id)` — the whole bank — straight to the writer, which is how one deck spent
six of the account's receipts at once.

### Anchor-driven carousel menu

Whether a fact-anchored archetype is on the carousel menu at all now follows the WRITER's
anchor, not the bank's size: with none, those archetypes are taken off entirely
(`select_blueprint(exclude_formats=fact_anchored_formats("post"))`), because a carousel bakes its
text into rendered slide IMAGES and a `[[…]]` placeholder there can never be edited away.

### Deck reference gate (issue #728)

The save-worthiness half. `deck_reference_report` grades a generated deck deterministically —
every BODY slide (cover/CTA/testimonial exempt) of a `save_targeted` archetype, or of ANY deck
whose caption promises a checklist/stack/framework/numbers, must carry ≥1 reusable artifact
(step, command, metric, threshold, config line, checklist item, decision rule, before/after),
and a promise the slides never deliver fails the deck on its own. A failure regenerates with
the exact slides named (`deck_retry_directive`, `DECK_REFERENCE_MAX_ATTEMPTS`, default one
retry) and then ships with a logged reason — rendered images have no review queue.
`reference_slide_directive` gives the writer the shapes that ARE inherently save-worthy up
front. Tool/model version numbers ("GPT-4o", "Postgres 16") are NOT graded as claims — the
receipt's structure asks for the exact stack by name.

Both deck graders read the generated JSON, never the PNG that ships — which is the gap the audit
below measures: the prompt allows a 200-char slide body, the schema 500, and the layouts the plan
selects draw 99–193 before `_draw_block` silently stops.
Full audit: `docs/content-quality-audits/carousel.md`.

## Mechanical editor pass (issue #1079, `content_alignment.mechanical_edit_text`)

An opt-in `lem-medium` copy edit on a newsletter draft — capitalization, grammar, punctuation,
formatting, and nothing else. It runs AFTER humanization and BEFORE the slop lint (so the lint grades
the text that ships), is gated by the `newsletter-editor-enabled` flag, and re-runs after a slop-lint
regeneration. Voice and tone stay the reviewer's job, which is what the reporter asked for.

The mechanical-only contract is a **diff-guard, not a prompt line** (`mechanical_edit_guard_ok`).
A prompt rule is a request; the guard is the check, and it holds even when the model ignores its
instructions. Four conditions, all required:

| Check | Rule |
|---|---|
| **Numbers** | Multiset equality — a changed, dropped, or invented figure fails. Commas are stripped first, so `1,200` → `1200` is formatting |
| **URLs** | Exact set equality, trailing sentence punctuation trimmed off the match |
| **Proper nouns** | SUBSET: every proper noun in the input must still appear, same case. ALL-CAPS acronyms count anywhere; a capital that OPENS a sentence, bullet, or heading does not — fixing those is the pass's whole job |
| **Length** | Within `MECHANICAL_EDIT_LENGTH_MARGIN` (10%), with `MECHANICAL_EDIT_LENGTH_SLACK` (40 chars) of absolute slack so one added comma on a short draft is not read as a rewrite |

The guard runs on the NORMALIZED candidate, because that is the text that would actually ship. It
**fails open**: a rejected edit, an LLM error, and an empty reply all return the ORIGINAL draft, each
logged at DEBUG so a proxy outage does not look identical to a disabled flag (a WARNING would file a
defect against a pass that is working as designed). A disabled flag returns the draft silently.
A polish pass must never be able to block a newsletter.

## Slop lint (issue #625, `slop_lint.py`)

The cheap explainable layer under the two LLM passes (`humanize_text` #416,
`score_authenticity` #382): pure regex/statistics, ~0.5ms, run on posts AND comments AND DMs AND
newsletter editions AND group posts after humanization.

### HARD checks (regenerated up to `SLOP_LINT_MAX_ATTEMPTS`, then BLOCK)

The budget is read PER SURFACE since #1434 — `SLOP_LINT_MAX_ATTEMPTS_<SURFACE>` beats the global
value, because what one more attempt costs belongs to the surface (an edition is a `lem-complex`
call, a feed comment a `lem-medium` one at volume). Every surface still defaults to **2**, i.e. one
regeneration. Every loop resolves the budget for the surface it is drafting, so the knob is real on
the short-form surfaces (`lint_repaired`) and the affiliate promo draft too, not only on the
newsletter.

Two rules travel with it, on every loop whose retry the slop lint OWNS (#1434 on the newsletter,
#1536 on `lint_repaired` and the affiliate promo draft): the retry is a fresh draft rather than an
edit, so
`keep_retry` keeps whichever of the two ranks better on (HARD count, total violations) instead of
taking the newer one blind — on the newsletter, where the structural floor (#1435) steers the same
retry, the rank is (HARD count, structural failures, total violations) so a draft that fixed the
floor is not thrown away for a WARN; and each regeneration emits `slop_retry` naming what it actually
did (`cleared` / `traded` / `worsened` / `persisted` / `lost` / `unsteered`, plus whether the draft
was `kept`) — the finished draft only shows what was still firing at the end, so without that event a
retry that traded one check for another is invisible. Keeping the better draft bites hardest on the
queue-less surfaces: a DM or a seed comment is SENT, so the draft the loop ends on is the one the
reader gets, where a worse edition would at least meet a human first. `unsteered` is the
newsletter's own case: the structural floor shares this budget, so a slop-clean edition can spend a
regeneration with no HARD check to fix, and counting those as `cleared` would inflate the clear-rate
the event was raised to measure. The comment quality gate (`_gated_comment`, #617) is the one retry
loop neither rule reaches, on purpose: a slop violation there shares the budget with contract and
similarity failures, and a draft that never clears is SKIPPED rather than sent, so there is no
worse-draft to keep and no single grader whose clear-rate the event would be measuring. It records
its rejections in the log, not in `slop_retry` — so a `surface="comment"` breakdown is the
`lint_repaired` comment paths, not every comment that was ever redrafted.

- Banned lexicon pileup
- The "it's not X, it's Y" contrastive frame
- "Here's the kicker" ta-da transitions
- Bait/reflex closers
- Emoji-bullet listicles

A failing surface: post is held at PENDING behind the `ai_slop` quality gate with the exact
constructions named; a feed comment is SKIPPED (shares the comment gate's retry budget); a DM /
newsletter / group post ships with a logged reason because dropping them breaks the sequence.
(Since #932 a group post does land in a review queue — the weekly draft the user can rewrite or
skip — but the lint still only logs there: the draft is generated days ahead and unattended, so a
dropped one would silently cost the week's group slot.)

### WARN checks (advisory, never hold anything)

- Em-dash density
- Rule-of-three
- Burstiness
- Rhetorical hook
- Canned scaffold (POSTS only, issue #1138)

Real false positives (a genuine list of three tools reads like a rule-of-three) so these never
hold. The wordbank is `content_alignment.AI_TELL_WORDS`, NOT a second copy. The bait check
honours the same lead-magnet `exempt_keyword` `strip_engagement_bait` does, or every "Comment
YES" CTA would hold its own post.

### Newsletter structural floor (issue #1435)

The same shape applied to a NEWSLETTER's structure rather than its wording, and the reason it
exists is measured: #1284 shipped the floor as writer-side wording, and an A/B with only the
contract swapped came back with LONGER paragraphs than the control. So the floor got a checking
side.

`content_framework.newsletter_structure_report()` grades a finished body on the four measures
`newsletter_writing_directive()` states — opening line inside `LINKEDIN_FOLD_CHARS`, no paragraph
past `DWELL_PARAGRAPH_MAX_CHARS`, at least one list block, `NEWSLETTER_WORD_FLOOR`–`CEILING` words.
It is **not a second grader**: every measurement is read off `dwell_report()`, and the ONLY
newsletter-specific threshold is the word band, because an edition's length target is not a feed
post's.

Two halves, split by what code can actually do:

- **The wall-of-text paragraph is repaired deterministically.** `newsletter_shape_body()` reflows
  it with the same `enforce_post_readability` pass `shape_for_dwell` uses, with the length cap made
  unreachable so an edition is never trimmed. Nothing is asked of the model, so nothing is traded
  away — on the first A/B run a retry told to split its paragraphs *and* hold the word floor spent
  the floor to buy the split (mean 768 → 565 words).
- **What a reflow cannot write** — the opening line, the list block, the length — is fed into the
  SAME bounded regeneration the slop lint uses, sharing its `SLOP_LINT_MAX_ATTEMPTS` budget, with a
  targeted directive that names the measured number and its repair.

**It can never hold or pause an edition.** A still-failing edition is returned for the review queue
with its reasons at **INFO** — not WARNING, because on the real corpus this is the common case and a
recurring warning would file a grouped defect against working behaviour. `NEWSLETTER_STRUCTURE_ENABLED=off`
restores the exact prior behaviour. Measured cost and the re-run A/B:
`docs/content-quality-audits/newsletter.md` §4a.

### Motion-prompt lint (issue #1277)

The same layer for VIDEO. `motion_prompt_report()` (in `slop_lint.py`, not a video-only module)
grades a FINISHED motion prompt before `create_runway_video` spends a Runway credit, against the
same `MOTION_BANNED_*` tuples `content_framework.motion_prompt_directive()` hands the writer —
one list per family, so the writer side and the checking side cannot drift.

| Check | Severity | Fires on |
|---|---|---|
| `motion_montage` | HARD | Edit language Gen-4 renders as a smear, not a cut ("cuts to", "b-roll", "montage") |
| `motion_mood` | HARD | Gen-3-era mood / film-stock / render-quality stuffing ("cinematic", "35mm", "4k") |
| `motion_audio` | HARD | Audio the WRITER added — the `_audio_direction()` clause (#548) owns audio and is excluded from grading |
| `motion_opening` | WARN | No camera move, subject motion or immediacy cue in the FIRST sentence |

**Severity is the checker's opinion; the flag decides what happens.** `video-motion-lint-hold` is
OFF, so a HARD violation is reported (`motion_prompt_check` event, DEBUG log) and the prompt ships
exactly as before — the credit-spend profile is unchanged until the flag is flipped. ON, a HARD
violation buys one steered rewrite (`MOTION_PROMPT_LINT_MAX_ATTEMPTS`, default 2 prompts total) and
then raises `MotionPromptHeld`, which the video path already handles as a generation failure
(refund the credits, fall back to Pexels). `motion_opening` never regenerates and never holds even
under enforcement: it is an allow-list heuristic, and a legitimate prompt can open on a beat the
list has no verb for. `MOTION_PROMPT_LINT_ENABLED=false` turns the grading (and its telemetry) off
entirely; `SLOP_LINT_SEVERITY_MOTION_<CHECK>` retunes one check per deploy.

**Canned scaffold** is the same one-list rule applied to phrases:
`content_framework.POST_BANNED_SCAFFOLDS` is what `post_writing_directive()` names in the prompt
AND what the check greps for, so the writer side and the checking side cannot drift — which is
exactly how they HAD drifted (the post system prompts were handing the model
"In my experience as a [Job Title]…" as a worked example, and nothing downstream could see it).
Post-only: comments already run `comment_filler_openers()` against a tighter, addressed voice.
WARN because a templated opener can still carry a real specific, and no substring match can tell.
Full audit: `docs/content-quality-audits/text.md`.

## Content mix (70/20/10) governor

Every planned post carries a mix class in `posts.content_mix`:

| Class | Share | Description |
|---|---|---|
| `value` | 70% | Audience value, sells nothing |
| `authority` | 20% | Expertise education, sells nothing |
| `promo` | 10% | Forced `case_snapshot` TEXT post with an artifact CTA |

Classes are assigned deterministically in `content_alignment.assign_content_mix` (promo cadence
`PROMO_EVERY_N_POSTS`, clamped to 10–30 so promo can never exceed 10%). The promo slot claims a
TEXT post and is forced into the `case_snapshot` blueprint, and the class rides into the prompt
via `alignment_directive(..., content_mix=)`.

**Promo CTAs are always an ARTIFACT** (lead magnet / newsletter) — a meeting ask is banned in the
prompts (`ARTIFACT_CTA_POLICY`, injected by `cta_policy_directive`), repaired deterministically
by `replace_meeting_ask_cta`, and any that survives HOLDS the post at PENDING via the
`meeting_cta` quality gate. Compliance is reported on `/user/engagement-analytics`
(`content_mix`) and rendered on the Dashboard.

## Newsletter blog alignment (issue #967, `utilities/blog_source.py`)

`align_with_blog` is a user-facing toggle that **defaults ON** and promises the edition repurposes
the author's own writing. `resolve_blog_source(user_id, settings)` is the ONE place that toggle
becomes source text — the three call sites (`engagement.newsletter.auto_publish_newsletter_edition`, and
both edition paths in `run_scheduler`) pass its return value straight into the generator as
`blog_content`.

| Rule | Why |
|---|---|
| Blog URL first, sitemap second | The sitemap is the fallback for users who set one but never a blog |
| Same fetchers as `blog_summary` / `website_content` (`get_main_blog_url_content`, `fetch_sitemap_urls` → `filter_relevant_urls` → `extract_page_content`) | One scraper for the app, not two |
| Returns `None`, never raises | Best-effort: an unset, unreachable, or empty source must never block an edition from existing — it writes from topic + profile instead |
| Resolved PER edition, article/page drawn at RANDOM | Two editions queued in the same run must not repurpose the same article |
| `BLOG_SOURCE_MAX_CHARS = 8000`, `_SITEMAP_ATTEMPTS = 3` | Bounds the page text one resolve holds; the generator applies its own smaller prompt budget on top |
| HTML/bytes/paragraph lists normalised through `_plain_text` | WordPress returns rendered HTML — without this the source budget is spent on markup |

**Logging follows the escalation contract**, which matters here because the toggle defaults ON:

- **Toggle on, no blog and no sitemap configured** — the common case. Expected no-op → `log_debug`.
  Warning here would fire for most users on every edition and escalate to ERROR on repeat.
- **A configured source that fetched fine and read back empty** — the one failure nothing else has
  reported → `log_warning`.
- **A fetch that threw** — already warned where it was detected; `_from_blog` / `_from_sitemap`
  return a `reported` flag so `_resolve` never restates it. ONE unreadable source = ONE warning.
- **One dead page out of a sitemap** — routine, `log_debug`, try the next of the three.
