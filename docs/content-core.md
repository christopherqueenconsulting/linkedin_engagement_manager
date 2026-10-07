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

### Fact grounding (issue #1834)

A number the draft attaches to **we / our / I** is a claim about the author's own operating history,
and it must trace to one of two places (issue #2136): the **third-party** target post, or the
user's **story bank**. Anything else the model wrote down, it invented — a trace audit found "we
logged 1,200 errors per week, then 300, a 75% drop" and similar in roughly 8 of 12 drafts read,
none of it in the bank.

**Our own words never ground a claim.** The 2026-09 audit found 24 of 102 shipped comments carrying
a first-person number, and 6 of 25 sampled ones answering our OWN first comment: the walk re-read a
card that now held our comment as "the post", so comment 1's invented number grounded comment 2.
`story_bank.without_own_text` removes every sentence of the author's recent comments (the same
`recent_comments` the similarity gate uses, which includes this run's) from the post before it is
used as grounding. Research `findings` no longer ground a first-person claim either — a researched
statistic restated as "we measured" is still a claim the author never made.

**Rewrite without the claim, then regenerate, then skip** (the owner's #2136 decision). A HARD
finding first drops the sentences carrying the invented numbers
(`story_bank.strip_unsourced_sentences`) and re-grades the remainder through every gate; it ships
only if it passes on its own. Otherwise the numbers join the #617 fix-list, and a draft that never
recovers is skipped.

The detector is `story_bank.unsourced_specifics`, the same one the post review gate uses, scoped to
first-person SENTENCES so a stat quoted from research is not a personal claim. Severity is per
surface (`story_bank.fact_grounding_severity`, overridable with
`FACT_GROUNDING_SEVERITY_<SURFACE>`): **HARD on comments**, because a comment publishes under the
user's name with no review step, so the finding joins the #617 fix-list and the post is SKIPPED
rather than commented on. **HARD on posts too, since #1971** — see "Every post's numbers" below;
the paragraph that follows records the pre-#1971 reasoning. Posts stayed WARN — the #1134 repair loop already ran this check and held
the draft at PENDING for a human. The bank is read only once a draft has claimed something the post
and the findings do not cover, so a comment with no numbers costs no query.

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
- **The ONE retry is a REPAIR, not another draft** (#1134). Every deterministic check in the review
  gate — similarity, A2 proof, fabrication, fact grounding, slop — answers a failure the same way:
  the failing draft plus the structured findings go to `get_ai_linked_post_refinement`, the editor
  that already runs one step earlier, which is a different prompt family from the writer. The writer
  is never asked for a second draft of the same brief. The editor is handed the WRITER's own story
  directive (`ctx.story_directive`) alongside the findings — a proof or fabrication finding asks for
  a real lived detail to be added or substituted while banning invention, which a model that can see
  only the draft cannot answer honestly. `proof_finding` / `fabrication_finding` exist
  for that brief (`quality_gates.py`); neither is built by `evaluate_post_gates`, nothing is ever
  held on them, and both are therefore ADVISORY (`demoted=False`) — the SPA paints a demoting
  finding as the reason a draft is stuck. Because a repaired draft that then passes everything is the post nobody has read —
  and no later pass can tell, the failing draft being gone — the repair path writes
  `posts.ever_gate_demoted` once, and `_may_auto_approve` (the ONE approve decision, read by the
  generation-time status setter AND `rescore_post`) holds it for the author unless the per-user
  `hold_repaired_posts_for_review` (default ON) is switched off. Both reads fail OPEN: an unreadable
  flag or prefs row costs the extra review, never the publish.
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
- **Every approval names its actor** (#2116). A move INTO `approved` writes `posts.approved_by` +
  `approved_at`: `user:<id>` for an author action through the API (`user_approver`), or a
  `PostApprover` source — `system:auto_schedule` (generation), `system:rescore`,
  `system:carousel_heal`, `system:video_heal`. Only the TRANSITION records it: re-saving an approved
  post keeps the first approver, and the native occasion publish handing a claimed post back to the
  queue passes no actor, so whoever approved it stays recorded. `test_post_approval_actor.py`
  fails the build on an approval call site that names no actor. Pre-#2116 rows are NULL — never
  back-filled, since the actor was never known. Audit query:
  `SELECT id, approved_by, approved_at FROM posts WHERE status IN ('approved','scheduled','posted') AND approved_by IS NULL AND NOT (manual_publish = 1 AND status = 'posted') AND created_at > '<deploy time>'` should be empty.
  The `manual_publish` exclusion is deliberate: `/user/post/mark-posted` records an occasion draft
  the author published BY HAND, which may go straight from pending to posted and was never approved.
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

### Freshness rules (showcase round 4)

The round-4 gauntlet ran the real `create_content` path on user 1's next ten slots, and an
independent critic failed it on monotony and on engagement readiness. The bank had seven entries,
all engineering build stories already used 12-18 times, so about 16 posts told five stories in
engineer language to an audience of small-business owners. Each rule below is deterministic and
lives in the shared core. None of them is a per-type prompt helper.

- **Story cooldown** (`story_bank.select_story(cooldown_texts=…)`,
  `STORY_COOLDOWN_POSTS`=10, `STORY_COOLDOWN_DAYS`=14). An entry anchors at most one post in the
  window of the user's last 10 posts or last 14 days, whichever covers more posts. Since round 5
  the window reads the entry each post RECORDED (`posts.story_id`, see below); `entry_echoed_in`
  is only the fallback for legacy posts with no record. When every entry is cooling, the post
  runs **without** an anchor: `cooldown_story_directive` tells the writer to build on the research already in the
  prompt plus up to three items from the user's curated-sources pool, credited by name. A
  `personal_story` or `engagement_prompt` slot moves to a research-grounded type
  (`thought_leadership` / `industry_news`). The story bank remains the ONLY source of a
  first-person fact. Comments keep the soft rotation, since they pass no window.
- **Topic diversity** (`content_framework.post_topic` / `repeated_topic`). Every post now records a
  topic through `update_db_post_shape`: the blueprint's subject, or the post's keyword fingerprint.
  A draft whose topic repeats one of the last three posts' topics is regenerated ONCE, with those
  topics named as off-limits (`topic_avoidance_directive`). A second repeat falls back to a research
  topic: `industry_news`, with the just-covered focus topics taken off its rotation. The bar is the
  similarity toolbox's token overlap (`TOPIC_REPEAT_MIN`, default 0.6). A blueprint that pins a
  subject is never second-guessed.
- **Plain fold for a small-business ICP** (`smb_audience`, `fold_outcome_directive`,
  `fold_jargon_hits`). When the author's goals, focus topics or voice brief name a small-business
  audience, the blueprint carries `plain_fold`. The hook machinery then tells the writer that the
  first two lines must state a business outcome (money, hours, customers, risk) in plain words,
  with technical detail translated below the fold. Decks get the same rule, plus their rotated
  close, through `carousel_blueprint_directive`; for a deck it also covers the cover slide. The
  deterministic guard is the slop check
  `fold_jargon` (`FOLD_JARGON_TERMS` in any case; `PR`/`API`/`RAG`/`LLM` only as written). It is
  HARD on posts but fires only under `lint_report(plain_fold=True)`, so the review gate spends its
  ONE editor repair on the opening and records the reason on the post. A fold that is still jargon
  after that rewrite is logged at INFO and never holds the post: the gate pass does not re-grade it.
- **CTA rotation** (`cta_type_of`, `select_cta_type`, `assign_cta_style`). The CTA types are
  `comment_keyword` (artifact), `question`, `save`, `share`, `dm` and `none`. A post never repeats
  the CTA type of either of the two posts before it. The artifact type goes to the promo slot only
  (the 70/20/10 rule), and the lead-magnet ask follows the rotation instead of the legacy 1-in-N
  cadence. Since round 5 an unclassified post never gets it either (see below). The DM ask is
  offered only where `allow_dm` says policy allows it, and the content plan never sets that today.
  `share_one`,
  `no_ask` and `dm_offer` are `ask: False` closes, which are never drawn at random and skip the
  reply-driving CTA rule. The hook pass keeps the assigned close (`optimize_post_hook(cta_type=…)`),
  and a draft that still closes on a recent type has its closing ask cut (`strip_closing_ask`).
- **Bolted-on industries** (`story_bank.unsourced_industry_claims`). A sentence that attributes
  something to the author (first person, or "client"/"story"/"case study") and names an industry
  from `INDUSTRY_CLAIM_TERMS` that neither the bank nor the profile states is a fabricated
  specific. It joins the fabrication repair. Research is NOT an allowed source here, because it
  describes the market, not the author.

### Dual audience and the round-5 rules (showcase round 5)

The round-5 critic still failed monotony and engagement readiness. "Comment AUDIT" closed four of
ten posts, "What if…" opened most of them, the "$49 fabricated-ROI" story was told twice inside the
cooldown, about twelve of 21 items were one idea (AI spend, routing, observability), and the copy
addressed "technical leaders" while the brand also sells to small-business owners. The owner's call
(2026-10-07) is that one account serves BOTH audiences, and each post picks ONE.

- **Audience mix** (`utilities/ai/audience_mix.py`, `engagement_preferences.audience_mix`,
  Settings → My Voice → "Who my posts are for"). A JSON object: `primary_audience`,
  `secondary_audience`, `secondary_share` (0..1) and `secondary_focus_topics`. A share of 0, which
  is the default and every row saved before this, means a single audience, and nothing below
  changes a post. With a share, `select_audience` picks each post's reader from the audiences the
  recent posts recorded (`posts.audience`). That is error diffusion over a rolling 10-post window:
  0.6 gives S P S P S S P S P S, six in every ten and never a streak. Posts that recorded no audience
  do not count, so switching the mix on does not start with a run of six. The pick re-aims the
  prefs the rest of the post reads (`audience_prefs`). The focus topics become the audience's own,
  so research and topic selection follow them. For the secondary reader, the business goal is
  re-stated around that reader. The blueprint carries `audience_directive` (one reader, their rules).
- **Per-post fold, CTA menu, hook order, story.** `smb_audience(audience=…)` answers from the post's
  audience when there is one, so a practitioner post is never held to the owner's fold rule and an
  owner post always is. `select_cta_type(smb=…)` / `assign_cta_style(smb=…)` give an owner a plain
  question or poll and a practitioner a challenge or debate. `select_hook_shape(smb=…)` breaks ties
  toward statements and numbers for an owner, and toward a contrarian take for a practitioner.
  `select_story(smb=True)` prefers an entry whose body reads as an owner's story (`audience_fit`).
- **Keyword CTA: promo slots only, at most once per 7 days of slots.** The root cause was a second
  source. `create_video_content` called `create_text_post` without the slot's class, so a video
  caption fell back to the legacy 1-in-3 cadence: slots 123, 135 and 141 are multiples of three.
  The class is now passed through. `create_text_post` grants the ask only to a promo slot,
  classified or not. `_cap_promo_keyword` reads the posts scheduled within
  `ARTIFACT_CTA_COOLDOWN_DAYS` (7) of the slot (`get_post_texts_near_slot`, by `scheduled_time`,
  since a plan is written weeks ahead). A promo slot inside that window is written and re-classed
  as `value`, the same demotion an anchorless promo gets. An unreadable window fails CLOSED.
  `_strip_unsanctioned_keyword_cta` removes a "Comment KEYWORD" line a rewrite copied onto a post
  that was not given the ask. `test_content_freshness_r5_wiring.py` replays a 10-slot plan.
- **Opening-shape rotation** (`opening_shape`, `select_hook_shape`, `hook_shape_violation`). The
  shapes are statement, number-led, short story, contrarian, question and how-to. Each is read off
  a post's first sentence, so legacy posts count. A text post's blueprint gets a `hook_shape`: never
  the previous post's, otherwise the least recently used of the last five. Its `hook_style` is the
  matching `HOOK_STYLES` entry the archetype allows. `optimize_post_hook(hook_shape=…)` writes that
  shape instead of choosing "a bold claim, a surprising stat, or a sharp question". "What if" is
  banned on every shape but a question, and on a question too while one already opened a post in
  the last five. A draft that still breaks the rule gets ONE more hook pass. A question opener that
  survives that is cut deterministically when the post stands without it (`drop_opening_question`).
- **Recorded story id** (`posts.story_id`, `update_post_generation_record`). Written at generation
  time, before the content is stored, so a post generated in the same run counts.
  `story_bank.cooling_ids(recorded_ids=…)` cools a recorded entry however the post paraphrased it.
- **Topic-cluster cap** (`TOPIC_CLUSTERS`, `topic_cluster`, `capped_topic_cluster`). At most
  `TOPIC_CLUSTER_MAX` (3) of any rolling `TOPIC_CLUSTER_WINDOW` (10) posts share a keyword family,
  such as AI spend/cost/routing. A post needs `TOPIC_CLUSTER_MIN_HITS` (3) family words to belong
  to one, and a "Comment KEYWORD" line is not counted. Focus topics in a crowded cluster come off
  the post's rotation before drafting (`_without_crowded_focus_topics`). A draft that still lands in
  one is regenerated through the same ONE-retry-then-research path as a topic repeat, with
  `cluster_avoidance_directive` naming the cluster.

### Facts that must agree with a calendar (showcase round 6)

The round-6 critic failed credibility on errors a reader can check: "Wednesday, June 22, 2026" (a
Monday), a 30% result "within the first month" of a story dated six days earlier, an offer made on
February 14 "aiming to finish in January", "6-part" on a five-slide deck, unsourced 79% / 13x, a
"Senior Applied AI & Full-Stack Engineer" sign-off, and "e-commerce" framing six posts whose stories
never mention it. Every rule below lives in the shared core and is deterministic.

- **`utilities/ai/fact_consistency.py`** is the checker. `consistency_report(text, happened_at)`
  bundles three checks. `weekday_mismatches` reads "Weekday, Month D, YYYY" in either order.
  `deadline_contradictions` flags a same-sentence deadline month before the dated offer, with no
  year it must sit more than 6 months ahead, so December → "in January" passes. `timeline_violations`
  flags first-person, past-tense elapsed time ("within the first month", "three months later") longer
  than now minus the anchoring story's `happened_at`, with one day of slack. An unknown, unreadable
  or future date is never evidence.
- **Deterministic first.** `_deterministic_fact_cleanup` drops a wrong weekday (never "corrects" it,
  because the date may be the wrong half) and cuts a trailing job-title sign-off
  (`strip_signature_lines`). It runs before the review gate, on the editor's repair, and on the final
  text. A deck gets the same weekday fix on its caption and on every slide string.
- **Then ONE repair, then a hold.** What survives the cleanup reaches `_review_generated_post` as a
  `fact_consistency` finding (`quality_gates.fact_consistency_finding`, HOLDS). The editor is briefed
  once. The repaired draft is re-graded and its verdict recorded. `evaluate_post_gates` re-checks the
  calendar half live on every pass. The timeline half needs the story's date, so it is carried from
  the record (`_carry_consistency_hold`). A clean draft clears a stale record. A deck has no editor
  pass, so `create_carousel_content` records the caption + slides verdict against its story.
- **Every number has a named source.** The research block recorded for the post's allow-list is
  filtered to the sentences that NAME their source: a citation marker, URL, "according to", a
  "(Publisher, 2025)" cite, or a named publisher's survey/report/study
  (`named_source_material`). An unsourced research statistic is now an unbacked number, and gets the
  same repair-then-hold as an invented one. Spelled multipliers ("thirteen-fold", "13×") are numeric
  claims (`_SPELLED_FOLD_RE`).
- **Deck counts in every phrasing.** `deck_count_claims` reads the hyphenated "N-part / N-step /
  N-point" as well as "N steps / N insights / N tips". `reconcile_deck_counts` rewrites the cover in
  its own form ("6-part" → "4-part"), and the `deck_count` gate holds the caption.
- **Template slides.** `deck_substance_report` fails a body slide that carries no number, no
  command/setting/threshold/rule/comparison, no named thing, and fewer than two particulars shared
  with the caption and the story's facts. Template vocabulary ("challenge", "moves", "identify",
  "step") does not count as shared. `ai_helper._repair_carousel_substance` regenerates the deck ONCE
  and keeps the retry only when it is buildable, no worse on the reference gate, and has strictly
  fewer thin slides.
- **Stock openers are HARD on posts.** `slop_lint.CHECK_STOCK_OPENER` matches "In today's …
  landscape/world", "In a world driven by data", "In the fast-paced world of …" and similar at a
  sentence start. It is OFF by default and HARD on `post` (`SURFACE_SEVERITIES`).
- **The profile's industry is context.** `story_directive` and `no_story_directive` carry
  `INDUSTRY_CONTEXT_RULE`. The review gate's industry check reads the STORY BANK only
  (`_industry_claims`). The profile no longer clears a claim. A profile industry term that frames the
  post ("In AI-driven e-commerce, …", "the e-commerce landscape") is flagged even without a
  first-person cue.

Tests: `test_fact_consistency.py`, `test_content_core_r6.py`, `test_deck_substance.py`,
`tests/unit/app/test_fact_consistency_wiring.py`.

### Every printed figure has provenance, decks claim what they show (showcase round 7)

The round-7 critic (`gauntlet/critic7/critic_scores.json`) failed credibility and monotony again:
an unsourced "5.6 hours per week" hook and "80% of companies" (slot_141), 45% on an image (rhythm_4),
53.7% on a cover (cover_18), "Error rates dropped sharply" as a headline (slot_130), "4-part" on a
6-page deck (143), template headings ("The Stakes:", "Step 1: Identify…"), a cover promising "From
Silence to Success" over a deck with no success (144), six of fourteen posts opening on a question,
"first operations person" closing two posts in a row, and four curated drafts on one template. The
image half (headlines, cards, covers, rotation) is in `docs/image-stack.md`, "Showcase round 7".

- **Provenance** (`fact_consistency.figure_provenance` / `unprovenanced_figures`). A figure printed
  on an image or used in a hook must appear in a body sentence AND be backed by a story-bank fact
  (the allow-list), or by a source named in that sentence or the one before it ("according to X",
  "(X, 2025)", "X's survey", "X looked at 3,000 posts"). With no allow-list (None) a first-person
  sentence also counts. `source_name_for` reads the name a cover can print.
- **The hook** (`hook_provenance_issues`, `consistency_report(hook_facts=…)`). The hook is the
  opening sentence. A figure there that the story bank does not hold and the body never sources is
  a `fact_consistency` finding: the editor's ONE repair, then the PENDING hold, on the review gate
  and live on every gate pass where numbers are graded (never over an author's edit). A figure the
  fact-grounding gate already names (`hook_flagged`) is not reported twice. Research numbers are NOT
  an allow-list here: a research figure in the hook needs its source named in the post.
- **The decimal bug.** `numeric_claims` read a line-opening decimal as list numbering ("5.6 hours"
  as "6 hours", "53.7%" as "7%"), so no allow-list could match it. `_LEADING_ENUM_RE` now needs no
  digit after the dot.
- **Deck counts.** The body slides are the count (cover and CTA excluded) for an item claim ("3 key
  steps"), on the cover and the caption. A deck describing its OWN length ("A 4-part conversation
  starter", "this 6-slide guide") is dropped (`drop_self_length`): the reader checks it against the
  page counter ("1 / 6"), which counts the cover and CTA, so it never reads true. That was the
  escape on slot_143, whose four body slides matched "4-part".
- **Deck claims** (`deck_claims_report`). A body slide headed with a role label
  (`template_heading`: "The Stakes:", "The Challenge:", "Step 1:"), a cover promise no body slide
  delivers (`cover_promise_gap`: the "to …" of "From X to Y", or a promise word such as
  "success"), and a slide figure the caption does not vouch for (`deck_figure_issues`) join the
  deck's ONE substance regeneration (`ai_helper._repair_carousel_substance`). The retry must have
  strictly fewer faults of both kinds. What survives is fixed in code before render
  (`finalize_deck_claims`): the label goes (`heading_as_claim`: the rest when it asserts, else the
  slide's first short sentence), a figure's sentence is dropped, and an undelivered cover is
  re-headed with the first body slide's heading.
- **Question hooks** (`QUESTION_HOOK_CAP` 2 per `QUESTION_HOOK_WINDOW` 6). `select_hook_shape`
  never assigns a question once the window holds two, and `hook_shape_violation` names a third, so
  the existing hook retry and `drop_opening_question` apply. A deck caption, which gets no hook
  pass, has its opening question cut in code (`run_content_plan._enforce_batch_variety`).
- **CTA wording** (`cta_phrases`, `repeated_cta_phrase`, `CTA_PHRASE_WINDOW` 6). A close repeats
  another when their closing asks share a normalised three-word run with two content words
  (possessives fold, so "your" = "their"). `_cut_repeated_cta_phrase` cuts the repeated ask when the
  post stands without it, on text posts (after the type rotation) and deck captions. The promo
  slot's artifact close is never touched.
- **Curated commentary** (`curated_commentary.py`, `docs/curated-sources.md`). The structure and its
  close rotate (`CURATED_STRUCTURES`: takeaway + contrarian note, one concrete example, pros/cons,
  what I'd test first), least recently used against the author's recent curated posts. The
  "For a small-business owner" opener and a closing "Which…?" are cut in code. A product, feature or
  organisation the source text does not name (`unsourced_names`) gets one rewrite, then loses its
  sentence. "In practice" / "I tested it" is re-attributed to the source unless a story-bank fact
  mentions it (`first_hand_supported`).

Tests: `test_provenance_r7.py`, `test_deck_claims_r7.py`, `test_batch_variety_r7.py`,
`test_curated_faithfulness_r7.py`.

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

### Every post's numbers (issue #1971)

Three generated posts carrying invented figures — "≈45% lower cost-per-call", "AI inference costs
dropped 30% in Q2 2026", "41 PRs in a day" — published to the operator's own profile. Two gaps let
them through: the story-bank check is scoped to FIRST-PERSON sentences (a third-person industry
claim never matched), and `fact_grounding_report` ran only on the two fact-anchored archetypes.
The "posts stay WARN because the review gate holds them" premise was false in production: a
preview queue trains its reviewer to trust it.

Since #1971 `fact_grounding_severity("post")` is **HARD** and it is CONSUMED: `_grades_fact_grounding`
(`run_content_plan.py`) grades EVERY post's numbers, whatever the archetype, in both the review
gate (`_review_generated_post` → one editor repair with the numbers named) and the status-setter /
re-score gate (`evaluate_post_gates` → HELD at PENDING, the `fact_grounding` finding naming each
number in Content Studio). The allow-list is the story bank PLUS the material the writer was
actually handed — `_post_material_sources`: the profile brief, the lead-magnet CTA, the story
directive, and the research block, which `ai_helper.record_supplied_material` records per post
when `get_industry_trend_analysis_based_on_user_profile` hands it to a generator and the
status-setter forgets once the gates ran. A stat the research supplied therefore passes; one the
model made up is held. `FACT_GROUNDING_SEVERITY_POST=warn` restores the pre-#1971 posture
(fact-anchored archetypes only) without a deploy.

**Forbidden claims** are the other half: subjects the author has said may never carry a figure —
the router that filed the issue meters its targets at zero, so ANY cost or latency number about it
is invented by construction. The list is **per-user PLUS global** (#2047): the user's own
`engagement_preferences.forbidden_claim_terms` (the "Never attach a number to…" editor under
Account → Who I Engage → Advanced; a JSON array, ≤50 subjects of ≤80 chars, tidied by
`story_bank.normalize_forbidden_claim_terms` at the API boundary AND in the repository upsert so
one bad value can never roll the whole settings save back) on top of the global
`FORBIDDEN_CLAIM_TERMS` floor (`;`-separated, applies to every user). The ONE place they meet is
`story_bank.effective_forbidden_claim_terms(prefs)` — it reads `FORBIDDEN_CLAIM_TERMS_PREF` off the
prefs dict (the key is spelled there and nowhere else in the content core), user terms plus the env
terms, never instead of, one per folded key, read at call time — and every gated surface hands its
result to `forbidden_claims(content, terms)` (no env-only fallback): `evaluate_post_gates` from
`engagement_prefs`, `_review_generated_post` from `ctx.prefs`, and `_gated_comment` from the
`prefs=` both comment writers already hold — never a DB read per draft. Matching is on the folded
key (`str.casefold`, every non-letter/digit run in any script one space: "cost per call" matches
"cost-per-call", "Café" matches "café", "ai" never matches "said") and the finding names the subject
**as the user spelled it**. A key shorter than 3 characters ("C++" folds to "c") is dropped at save;
a subject the API could not keep — too short, no letters, over 80 chars, past the 50-term cap — is
**named in the PUT response**, never swallowed, and a PUT that omits or nulls the field leaves the
stored list alone (an older SPA build cannot wipe it). A draft that names a listed subject anywhere
as whole words and asserts any numeric claim (`numeric_claims`: years, list numbering and version
numbers excluded) gets the `forbidden_claim` finding at HARD, grounded or not, and an author's edit
does not clear it. One user's list never touches another user's posts. **A gate pass that cannot
READ the user's list never releases a hold it produced**: `_engagement_prefs_for_gates` makes the
read strict (`raise_on_error=True`), and on a fault `rescore_post` / `_gate_findings_for_post` carry
the recorded `forbidden_claim` finding forward (`_carry_forbidden_claim_hold`) instead of
persisting the global-only verdict that would auto-approve the post. **Gated surfaces today: posts
(`evaluate_post_gates`) and feed/second-wave comments (`_gated_comment`, skipped on the #617
budget).** The newsletter and weekly group post writers carry the first-person grounding hold
below but not the forbidden-claim list; DMs go through the slop lint only — not yet covered.

### Newsletters and group posts (issue #2098)

Both publish UNATTENDED — `auto_publish_newsletters` ships a `draft` edition at its slot, and
silence ships a `ready` group draft — and both shipped invented client work in the 2026-09 audit
(edition 14: "I audited a pipeline last quarter where 80% of tokens went to a frontier model.
That's a real number from a real client."; group post 6: "I've seen a 40% drop in per-call cost").
`fact_grounding_severity` is therefore **HARD on `newsletter` and `group_post`** (the owner's
decision), and a first-person specific no source backs **holds the draft for the owner's
approval** — no repair pass, because an edition is a `lem-complex` call and stripping the sentence
leaves its follow-up ("That's a real number…") asserting nothing.

- **Detector:** `ai_helper.unattended_fact_hold` → `story_bank.fact_hold_specifics` →
  `unsourced_specifics` (first-person sentences only). The allow-list is the author's OWN material:
  story bank, profile brief, and for a newsletter the blog source, the newsletter topic and the
  regeneration guidance. **Research findings are not in it** — a researched statistic restated as
  "I've seen…" is still a claim the author never made (#2136's comment rule).
- **Newsletter hold:** `generate_newsletter_edition` returns `fact_hold`; the top-up stores it in
  the same INSERT (`newsletter_editions.fact_hold`, NULL = not held) and the draft-ready email tells
  the author the draft waits on them. A regeneration re-grades and replaces the hold
  (`set_edition_fact_hold`) BEFORE the new body lands. A held `draft` is excluded at all three
  publish points — `get_editions_due_to_publish`, `_publish_next_due_edition_for_user`, and the
  `auto_publish_edition` worker — and the legacy generate-and-publish task refuses it outright.
  **Approving the edition is the release**: `approved` always publishes. The review-queue payload
  carries `fact_hold` so the SPA can say why a draft is waiting. Editions queued before #2098 read
  NULL and are not re-graded.
- **Group-post hold:** `auto_draft_group_post` stores a held draft `skipped` instead of `ready`, so
  the publish run never takes it and the studio's **undo skip** (bounded by the slot, #1415) is the
  approval. No new status: the existing restore control is the one the owner already has.
- `FACT_GROUNDING_SEVERITY_NEWSLETTER=warn` / `FACT_GROUNDING_SEVERITY_GROUP_POST=warn` turn a
  surface's hold off without a deploy.

Two more things the allow-list honours so a real figure is never held as invented: the user's own
**source text** (`_draft_from_source` records the blog post / sitemap page a `blog_summary` /
`website_content` draft is written from) and a **regenerate's guidance** ("mention we cut latency
40%" is the author's number). And a **Re-score without an edit is not an approval**: a figure the
last grade named as unbacked and the author left in place stays held (`_recorded_unbacked_specifics`);
only the numbers they changed or added are credited to them.

### Occasion / milestone archetypes (issues #1074, #2140)

Five more archetypes live in the same `POST_FORMATS` menu — `project_launch`,
`educational_milestone`, `new_certification`, `new_position` and `work_anniversary` — and they are
the only ones nothing may pick automatically. LinkedIn's native "Celebrate an occasion" composer
(Start a post → More → Celebrate an occasion) creates an entity the REST API has no equivalent for,
so these drafts are written by LEM and published BY HAND.

**One archetype per picker row (#2140).** #1074 shipped two; LinkedIn's "Select occasion" picker
offers five (`Project launch / Work anniversary / New position / New educational milestone / New
certification`, live 2026-09-14). All three missing rows were ADDED rather than skipped, per
archetype:

- `new_certification` — added, and `educational_milestone` narrowed to degrees, courses and
  programmes. Before this, a certification was drafted as an `educational_milestone` and published
  under LinkedIn's *educational milestone* occasion: the wrong row for a real credential. It is the
  row the #1012 hazard is about, so its label is the two-word `new certification` — the bare word
  is what the neighbours' descriptions carry.
- `new_position` and `work_anniversary` — added. Both are real, dated, author-named events with the
  same fact-anchored contract (the writer may state only the title, organisation and span the author
  gave), so leaving them out only sent the author to write those by hand.

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

- **`occasion-native-publish-enabled`, ON by default since #1088.** Read at BOTH ends —
  `auto_check_scheduled_posts` never queues the message with it off, and the task refuses if it is
  flipped off mid-flight. It shipped OFF until `scripts/linkedin_live_validation.py
  --occasion-composer` had been run live; #1621 (shadow-root trigger) and #1693 (template-chooser
  click-through) grounded the route and the owner authorised the flip on #1088. Flipped off,
  everything above is unchanged: the author still copies the draft across by hand.
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
  own label and nothing near it: "New certification" sits next to "New educational milestone" in
  LinkedIn's menu, and clicking the wrong one publishes a claim the author never made (#1012). Each
  label is a phrase in its OWN row's title and in no neighbour's title or description, and the unit
  tests pick every archetype off the live five-row picker in every rotation of its order. A type that does not
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

Before that report runs, `generate_carousel_content` gives a fact-anchored deck whose slides state
an unbacked number ONE regeneration (`_repair_carousel_fact_grounding`, issue #2231), graded
against the same whole bank (`grounding_anchors`). Its directive (`deck_fact_retry_directive`)
asks for the number to be DROPPED, never deferred to a `[[…]]` placeholder, because slide text
is rendered into images. The retry is kept only when it is buildable, no worse on the reference
gate, and states strictly fewer unbacked numbers. Whatever survives is still logged by the
report, which stays advisory (#1139).

### Anchor-driven carousel menu

Whether a fact-anchored archetype is on the carousel menu at all now follows the WRITER's
anchor, not the bank's size: with none, those archetypes are taken off entirely
(`select_blueprint(exclude_formats=fact_anchored_formats("post"))`), because a carousel bakes its
text into rendered slide IMAGES and a `[[…]]` placeholder there can never be edited away.

### Deck shape gate (issue #1666)

The gate BEFORE the reference one, because a deck the model cannot be built from has nothing to
grade. `missing_carousel_fields(model_cls, deck)` in `carousel_creator.py` is the ONE reader of
what a deck owes — it reads the model's own required fields, so a new required field is covered the
moment it is added, and an empty value (`{}`, `[]`, `""`, null) counts as missing exactly like an
absent key. `generate_carousel_content` runs it on the first draft and spends ONE bounded repair
call naming the missing top-level keys (`_carousel_shape_directive`), keeping the draft in hand if
that call errors or comes back no better. A deck it could not repair logs ONE ERROR naming the
fields — not a pydantic message — and both construction sites (`create_carousel_content`, and the
`POST /api/generate-carousel` preview route, which answers **502** with the field names) read the
same function rather than letting the constructor raise. Because the gate runs FIRST, the reference
gate's own retry is shape-checked before it is accepted — that retry is a whole fresh reply, it can
drop `cover` exactly like a first draft can, and grading says nothing about shape: a deck with body
slides but no cover grades fine and would replace a buildable one. The production failure it closes:
a reply
that omitted the `carousel` key reached `EducationalContentCarousel(**{})` and filed 332 grouped
`ValidationError: cover field required` exceptions naming pydantic, not the generator.

### Deck reference gate (issue #728)

The save-worthiness half. `deck_reference_report` grades a generated deck deterministically —
every BODY slide (cover/CTA/testimonial exempt) of a `save_targeted` archetype, or of ANY deck
whose caption promises a checklist/stack/framework/numbers, must carry ≥1 reusable artifact
(step, command, metric, threshold, config line, checklist item, decision rule, before/after),
and a promise the slides never deliver fails the deck on its own. A failure regenerates with
the exact slides named (`deck_retry_directive`, `DECK_REFERENCE_MAX_ATTEMPTS`, default one
retry) and then ships with a logged reason — rendered images have no review queue.
`reference_slide_directive` gives the writer the shapes that ARE inherently save-worthy up
front. A **context beat** of a save-targeted archetype ("Why this was compiled", "What it actually
is") is narrative by construction, so `CONTEXT_BEAT_DIRECTIVE` sends it to the caption or the
cover, never its own body slide — mapped one-beat-per-slide it failed the gate on every attempt
and shipped a "Why This List Matters" slide with a `RecurringWarning` (#2232). Tool/model version numbers ("GPT-4o", "Postgres 16") are NOT graded as claims — the
receipt's structure asks for the exact stack by name.

**Deck vs caption (issue #2106) — these two HOLD.** A deck can pass the reference gate and still
contradict the post it ships under. `_report_carousel_deck_consistency` measures two deterministic
checks at generation and records them on `posts.gate_reason` (`demoted=True`), where
`_recorded_deck_notes` re-reads them at every gate pass, so the post is held at PENDING:

| Gate | Check | Released by |
|---|---|---|
| `deck_count` | `deck_count_report`: an "N <items>" count in the caption ("the exact 5 checks", "five key trends"; 2–12, explicit noun lexicon, never across a preposition) matches NONE of the deck's readings — its body-slide count, or its bulleted/numbered lines | An edited caption: the finding carries `deck_counts`, and the re-read re-checks the CURRENT caption against them |
| `deck_off_topic` | `deck_topic_report`: `topic_authority_score` with the caption as the topic, below `topic_authority_min()` | Regenerating the carousel (or approving it) — slide text is baked into images |

Both fail OPEN on an empty side (no count claimed, no body slide, empty caption). The slide slop
note (#1512) stays advisory; these differ because a count or topic the post contradicts is a broken
post, not a style reading.

**Fixed before they are graded (showcase round 4).** `reconcile_deck_counts` runs in
`create_carousel_content` before anything renders. The cover's promised count ("4 Steps" over three
step slides) is ALWAYS rewritten to the number of item slides the deck carries, in the same digit or
word form. The caption is rewritten only when it makes exactly one count claim and `deck_count`
would fail, so a caption that also counts something about the author's past is left to the gate. A
`document` post (published as a PDF) never calls itself a carousel: `document_wording` rewrites the
caption and every slide string.

**Code-drawn cards and covers (showcase round 4).** `image_graphics` no longer prints a generic
"From the article". The source line names the source the concept cited
(`graphic_facts.source_name`), or there is none (`NO_SOURCE_LINE`). A named source too long for one
line is dropped rather than replaced. A cover's lead number follows the edition title when the
title states a stat the body also states (`image_concept.thesis_number`). ed18's title said 53.7%
and its cover said 45%. When no hook can carry the title's stat, a hook leading on a different
number gives way to a number-free option.

Both deck graders read the generated JSON, never the PNG that ships — which is the gap the audit
below measures: the prompt allows a 200-char slide body, the schema 500, and the layouts the plan
selects draw 99–193 before `_draw_block` silently stops.
Full audit: `docs/content-quality-audits/carousel.md`.

## Humanization rhythm rule on comments (issue #2123, `content_alignment._HUMANIZE_SYSTEM_BY_TYPE`)

`humanize_text` asks posts, DMs and newsletters to "mix at least one very short sentence (<=6
words) with a long one". A **comment** does not get that rule. It gets `_COMMENT_RHYTHM_RULE`
instead: length may vary, but every sentence must carry a point the draft makes, and standalone
filler ("It works.", "That hurts.") is named as banned.

A comment is only a few sentences, so there is nothing to shorten, and the model invents a sentence
to meet the mandate. This was measured on 10 fixed comment drafts, rewritten by `lem-medium` at the
production temperature. With the old rule, 18 of 160 rewrites added a new <=6-word declarative
sentence ("I hear you.", "The model is usually quick."). Without the rule, 0 of 80 did. With the
comment rule, 0 of 80 did. The audit's production rate was 33 of 102 (§F29), so the writer prompt
may add filler too. The 14-day post-deploy re-measure (#2174) is what shows how much of that rate
this change removes.

## Comment scrub (issue #2204, `content_alignment.scrub_comment_text`)

Every comment and reply surface (feed comment, reply to a comment, seed, second wave, thread reply,
reply follow-up) finishes in `ai_helper._humanize_comment`: humanize, then a deterministic scrub.
The hot-lead draft (`generate_lead_response`) is scrubbed too when its channel is a public reply.
`humanize_text` keeps up to ONE em dash and returns the draft untouched whenever it fails open, and
comments have no review queue, so the dash tell reached LinkedIn. The scrub turns every em dash,
`--` and spaced dash between words into a comma, strips markdown, and folds typography to ASCII
(`sanitize_for_linkedin`). A numeric range ("10 - 20", "$5 - $8") and a hyphenated word survive. It
runs before the gates, so what they grade is what is stored and posted.

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
- Owner-banned phrases (`preference_banned_phrase`, issue #2113) — COMMENT surfaces only
  (`comment` + `own_post_comment`; OFF elsewhere). The list is every quoted phrase a ban cue
  ("no", "never", "avoid", "don't" …) introduces in the same clause of the user's `comment_style`,
  plus the built-in `COMMENT_BANNED_PHRASES` ("hits home", "game changer"). A quote WITHOUT a cue
  (`Open with "what I'd add"`) is guidance, not a ban. Matching tolerates hyphens and a trailing
  -s, so "hit home" and "game-changers" count. Before this, the ban lived only in the prompt and
  13 of 102 comments shipped it. It grades the prefs the caller already holds (`_gated_comment`,
  `lint_repaired`), so it adds no DB read
- Fold jargon (`fold_jargon`, showcase round 4) — POSTS only, and only when the caller passes
  `plain_fold=True` (an author whose readers are small-business owners). It buys the review gate's
  one editor repair of the opening. The gate pass never grades it, so it does not hold a post. See
  "Freshness rules" above.

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
TEXT post and is forced into the `case_snapshot` blueprint (`PROMO_ARCHETYPE`), and the class
rides into the prompt via `alignment_directive(..., content_mix=)`.

**The governor reads the stored mix, never a counter (issue #2107).** Each new slot is classed
against the user's latest 30 classified posts (`get_recent_content_mix_sequence`, oldest first,
future planned rows included). A promo needs `PROMO_EVERY_N_POSTS - 1` non-promo posts before it,
and an authority post needs four non-authority posts before it. The old rotation was keyed on the
user's post COUNT, and the cadence reconcile (#2021) deletes planned rows, so the count rewound and
the same promo positions were handed out again. Production ran 35/41/24. Other rules:

| Rule | Why |
|---|---|
| An unreadable history plans NO promo | A failed read must not look like "promo due" |
| A due promo waits for a slot whose day type draws `case_snapshot` (Tuesday) | The promo's forced shape then matches its day. Waiting only makes promo rarer. With no such day in the calendar it may land anywhere |
| A promo demoted for want of a story anchor is re-classed `value` on the ROW | The governor and the promo gate then see the post that was actually written |
| Carousels draw their archetype from the day type's family too | Before this they drew from the whole menu. Only videos keep their own template menus |

**Promo CTAs are always an ARTIFACT** (lead magnet / newsletter) — a meeting ask is banned in the
prompts (`ARTIFACT_CTA_POLICY`, injected by `cta_policy_directive`), repaired deterministically
by `replace_meeting_ask_cta`, and any that survives HOLDS the post at PENDING via the
`meeting_cta` quality gate. DMs share the same patterns through the DM content gate (#2099), where
a call ask is allowed only once the contact has replied (`docs/engagement-automation.md`). A promo with NO artifact (`has_artifact_cta`: the lead-magnet comment
mechanic, or a subscribe ask for the user's newsletter) is closed on the user's artifact by
`_repair_promo_artifact_cta`. If the user has none, or an edit removes it, the post HOLDS on the
`promo_artifact_cta` gate. The gate is skipped when the settings cannot be read. Compliance is reported on `/user/engagement-analytics`
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
