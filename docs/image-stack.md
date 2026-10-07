# The image stack — ONE engine, two modules

`utilities/post_video.py` (#1443) is the VIDEO counterpart to `post_image.py` and lives in a
**SEPARATE** preview directory behind its own ownership gate, so an `image_url` can never be handed
an MP4. Only the weekly group post reads the `*_media_*` union of the two.

Full posture for LEM's AI still-image generation. CLAUDE.md keeps the one-line invariant + this
pointer.

Every surface that renders an AI image goes through the same two modules (plus the Stage 1 reader
they share). **Never add a
per-content-type prompt helper** — add a preset.

| Module | Owns |
|---|---|
| `utilities/ai/image_concept.py` | Stage 1: reading the WHOLE piece → a grounded `ImageConcept` (#2241) |
| `utilities/ai/image_brief.py` | Authoring the prompt: content + concept in → validated brief out |
| `utilities/ai/image_gen.py` | Rendering that brief, plus the vision quality gate |
| `utilities/ai/image_compose.py` | Typesetting the headline onto a finished render — the ONE place a headline meets a render (round 6) |
| `utilities/ai/image_graphics.py` | Drawing the five code-drawn archetypes from VERIFIED facts — the ONE place a data graphic is drawn (archetype round) — plus the post `quote_card` |
| `utilities/ai/post_treatment.py` | A POST image's rhythm: treatment, panel variant, photo grade and the sameness gate, read off the post receipts (anti-monotony round) |

## The staged engine (issue #2241)

Every newsletter cover came back as pipes, valves, gears or gauges, whatever the edition said. Three
causes, all fixed here: the cover REQUESTED a physical metaphor (extractor prompt, preset menu,
workshop fallback); the brief author never saw the article, only a title and a few candidate
objects; and the vision gate graded the render against the brief's OWN focal concept, so a valve
scored 5/5 against "a valve symbolising leaks". The engine is now four stages, still two modules:

| Stage | Where | What it does |
|---|---|---|
| 1. Concept | `image_concept.analyze_content_for_image` | ONE `lem-medium` JSON call over up to 12k chars of the FULL text → `ImageConcept`: thesis, audience, 3–6 `specific_entities` (FACTS), 3–5 `visual_anchors` (what is DRAWN), emotional beat, a 2–5 word `hook_phrase`, and a `treatment` |
| 2. Brief | `image_brief.build_image_brief` | Use case + treatment template + concept + brand clause + a ≤3000-char excerpt → `{focal_concept, prompt, required_entities, hook_text}`, validated deterministically, retried up to twice with the reason |
| 3. Prompt check | `image_brief.check_prompt_against_concept` | Deterministic checks, then ONE `lem-simple` judge: "could a stranger guess the thesis?" A fail buys ONE repair pass through Stage 2 |
| 4. Blind judge | `image_gen.inspect_render_quality(..., concept=, hook_text=)` | Two `lem-vision` calls: a BLIND description, then targeted questions scored on a rubric |

**Facts are not anchors (gauntlet round 1).** The first live run against user 1's real editions
returned entities like "Terralogic", "GPT-5.2", "Stanford HAI", "2025 Edelman-LinkedIn … Report"
and "53.7%". A renderer cannot draw a name: it wrote them as garbled text on a tablet and a report
cover, or invented "two AI accelerator boxes representing Claude Opus 4.5 and GPT-5.2" — and the
judge rightly scored specificity 1 on all four covers. So Stage 1 returns two lists:

- `specific_entities` are FACTS, grounded in the source (case-insensitive loose stems,
  `entity_mentioned`; an invented one is dropped). They feed the thesis and hook and are **never
  drawn or written**. A capitalised common noun the source also uses in lowercase is normalised
  to lowercase, so what stays capitalised is a name.
- `visual_anchors` are DEPICTABLE — roles, places, physical artifacts, situations. `anchor_rejection`
  refuses, deterministically, any anchor with a digit, a capitalised token past its first word, a
  capitalised first word the source never lowercases, or any token of a proper-noun fact;
  `GENERIC_ACRONYMS` (AI, B2B, CEO…) are exempt. An anchor with nothing grounded in the source is
  dropped too. Fewer than two survivors marks the concept `weak`, and the coverage checks stand
  down. Briefs, Stage 3 and the vision judge's specificity all work from `usable_anchors()` — the
  anchors, or for a concept with none, its plain common-noun facts.

## Visual archetypes — the image carries the idea or the evidence (archetype round)

The owner's verdict on the round-9 covers: "still generic with just random people". The research
agrees ([`visual-archetypes-research.md`](visual-archetypes-research.md)): readers skip anonymous
stock people as filler (NN/g), faces did not help — and did worse — in the Business niche of a
300k-video dataset, and AI-looking people cost trust. So every cover and post image now takes one
of seven **archetypes**, and the people photo is the rarest of them.

| Archetype | Drawn by | When it is a candidate |
|---|---|---|
| `stat_card` | code (`image_graphics`) | a verified thesis stat |
| `highlight_chart` | code | 2–6 verified numbers on the SAME unit; one gold, the rest grey |
| `receipt` | code | 2–5 verified money or time amounts ("the real cost of X") |
| `before_after` | code | a verified before AND after on the same unit |
| `checklist` | code | 3–5 of the piece's own steps, grounded in their sentences |
| `editorial_concept` | AI render + the typeset panel | always — the default AI image |
| `people_scene` | AI render + the typeset panel | ONLY when Stage 1 flags a `human_moment` |

**Selection** (`image_concept.select_archetype`, deterministic). Candidates come from what
VALIDATED, never from what Stage 1 hoped for; a code-drawn archetype also needs a headline. Score
= evidence strength (`ARCHETYPE_BASE_SCORES`: chart/receipt/before-after 4, stat card/checklist 3,
people 2.5, editorial 1) + 0.5 for Stage 1's own `archetype` pick (a TIEBREAK, never more) − 4 for
either of the last two archetypes the author shipped. Equal scores go to code-drawn first. The
ranking becomes a fallback CHAIN that stops at the first AI archetype; that one sets the
`treatment` (`editorial_concept`, or `people_scene` for a human moment), so the brief is always
written for what renders if every graphic ahead of it fails. Recent archetypes come from the
receipts — `archetype_rendered` first, since a fallback shipped as its fallback: covers through
`newsletter_cover._recent_cover_archetypes`, posts through `post_image.recent_post_archetypes`
(newest 40 post-image directories, bounded). Off the composited surfaces nothing changes.

**Deviations from the report, on purpose.** The chart needs 2 values, not the report's 3 (the
owner's spec: "≥2 comparable numbers"; a two-bar contrast is still evidence). The rotation
penalty is −4, not −2: at −2 a recent chart (4−2) still beat a fresh editorial concept (1), which
repeats rather than avoids. The founder quote card ships WITHOUT the photo: posts get a text-only
`quote_card` (§ Post rhythm below); `BrandKit.founder_photo` is parsed (an image path, no
traversal, never sent to a prompt) but nothing renders it until the owner sets an approved real
photo — and a renderer must then check it is an asset we own.
The before/after halves are code-drawn only; the optional AI object halves are not built.

**Fact safety — the reason five archetypes are drawn in code.** Stage 1's JSON carries
`graphic_facts`: each figure as `{label, value, unit, source_sentence}`, plus `steps`, a `costs`
block, a `comparison` and an optional `source_name`. `image_graphics.validate_graphic_facts`
keeps an item ONLY when its sentence is in the source (whitespace/quote-normalised), its number is
in THAT sentence with that unit (`verbatim_figure` — the drawn string is the sentence's own
match, so "$30K" stays "$30K"), and every content word of its label is in the sentence by root.
Nothing is derived: no baseline, no ratio, and no sum — a receipt circles a TOTAL only when the
piece states one, otherwise its largest line. The source line is "Source: <name>" only when the
name is verbatim in the piece, else "From the article". Immediately before drawing,
`assert_traceable` re-checks every string against its recorded sentence and raises
`UngroundedFactError` on any mismatch; a fabricated value is refused deterministically
(`test_image_graphics.py::TestFactValidation::test_a_fabricated_value_is_refused_deterministically`).
Every validated fact rides on the concept (`concept.graphic`) and every DRAWN fact, with its
sentence, on the receipt (`graphic_facts`), beside `archetype_rendered` and
`archetype_fallback_reason`.

**Drawing** (`image_graphics.render_graphic`, PIL only). The graphic fills the split layout's
scene region beside `image_compose`'s unchanged panel — kicker, headline, byline — at 1920×1080
for a cover and 1080×1350 for a post (`compose_headline(canvas=, hero=False)`: the graphic's own
figure is the hero, so the panel never repeats it). Brand light gold is the ONLY colour (one
highlight per graphic); everything else is charcoal, grey and off-white. Text is fitted, never
clipped: every string is recorded with the box it must stay inside, body text is at least 2.4%
of the canvas width (≈9 px on a 400 px thumbnail) and the source line 1.6%, and data that cannot
meet those floors raises `GraphicLayoutError` rather than shrink — the chain moves on. The
checklist ticks about a third and marks the next with a gold "?" (the moderate knowledge gap of
research §1.3).

**Rendering and grading** (`image_gen.render_code_drawn`). `render_image_gated` and
`render_avatar_image_gated` try the code-drawn head of the chain FIRST: $0 render spend, no
gpt-image call. Each graphic is judged ONCE (a deterministic drawing does not change on a retry;
an ungradable one gets one more look on an enforced surface, then fails open like every render)
on `specificity`, `scroll_stop` and `brand_fit` only — `text_accuracy` is n/a by construction and
the targeted prompt says so. A refusal or a rejection falls to the next archetype; past the last
graphic the AI archetype renders through the normal gate loop, and the concept the judge sees is
re-pointed at it. The admin variant tool compares renders, so it strips code-drawn archetypes
(`ai_archetype_only`), and the animated loop (`animated_loop.produce_post_loop`) never animates a
code-drawn still — an image-to-video model would re-draw its verified figures.

**The editorial concept.** Stage 1's ideas are now the report's Idea Miner (§3.3): up to 20
concrete `idea_nouns`, one operator (juxtaposition, scale shift, oxymoron, literal idiom,
transformation), clichés dropped, three object-only ideas kept — and ONE separate `people_idea`
for a human moment. An editorial concept never ranks a people-led idea; its ranker scores the
"piques interest" criteria and demotes a desk still life to the next object idea. The brief gets
`TREATMENT editorial_concept` with a rotated, text-free art style (`ART_STYLES`: risograph,
cut-out collage, claymation, editorial still-life photo — worded so the prop and vocabulary rules
never refuse the style itself), `EDITORIAL_PROPS_DIRECTIVE`, and a deterministic refusal of any
person word in the prompt. Anchor coverage stands down for it: its anchors are often people it
must not draw. A people scene's brief carries `gaze_directive` — eyes toward the headline panel,
never the camera (gaze cueing, §1.2).

**The "piques interest" rubric** (§6.4) is question 12 of the targeted judge: thumbnail read,
thesis fit, curiosity gap, novelty, 2-second resolve, credibility, ICP relevance, 0–2 each. It
folds into the existing seven criteria rather than adding an eighth: a total under
`POP_SHIP_FLOOR` (9/14) caps `scroll_stop` at 3, a zero thesis fit caps `specificity` at 3, and
a code-drawn graphic's credibility is 2 by construction. The scores ride on the receipt as
`gate_pop`. A faceless archetype is never capped for a missing or mismatched emotion.

## Showcase round — why the code-drawn archetypes lost, and the fixes

A read-only showcase run generated four newsletter covers through the real path: all four were people
or "editorial concept" photographs, although every article stated a thesis stat ($30K, 45%, 95%).
The receipts (`cover_*.brief.json`) show the cause was never the rotation penalty or a cover
exclusion. It was two things:

1. **The validator refused true facts.** ed16's stat came back as value `"30K"`, unit `"$"` and was
   refused as "30K$ is not in its source sentence"; ed18's 45% sentence was quoted with a U+2011
   non-breaking hyphen ("human‑written") against the article's plain one; every step of ed18's
   four-step list was refused for being over 8 words. Now `image_graphics.split_value` reads a value
   that carries its own symbol or multiplier (the multiplier must still match the sentence's — the
   drawn string is still the sentence's own); `_norm` folds every dash variant and soft hyphen and
   strips markdown emphasis on BOTH sides; `fit_step` keeps a long step's first 2-8-word CLAUSE (a
   run of its own words, so it traces exactly as the whole would). Nothing is reworded; a number or
   multiplier that differs is still refused.
2. **The score let people outrank a validated graphic.** People scored 3, the same as a checklist
   or stat card, and Stage 1's +0.5 tiebreak (it hinted people on every human-moment edition) made
   it 3.5 — ed19's validated checklist (3) lost. `ARCHETYPE_BASE_SCORES[people_scene]` is now
   **2.5**: the tiebreak can at most TIE a 3.0 graphic, and ties go code-drawn first. People still
   win when nothing validated, or when the graphic is one of the last two shipped.

**`editorial_concept` is object-only on every path.** ed17/ed18 chose it and shipped a woman on a
phone: the deterministic fallback, with no surviving idea, became a `concrete_scene` ("the everyday
objects at the heart of this: not AI"), and the gate's emotion repair ("a closer framing where the
emotion shows on the face") then asked for a person. Now the fallback stays editorial and builds a
still life from Stage 1's `idea_nouns`/anchors filtered by `person_word`; `_PERSON_WORDS` also
refuses role and portrait words (CFO, manager, engineer, buyer, smiling…); a faceless archetype is
never `emotion_weak` and its repair table (`_FACELESS_REPAIRS`) never mentions a face.

**A hero numeral lifts only a LEADING number.** "We saved $30K per quarter" became hero "$30K" over
"We saved per quarter". `image_compose.split_hero` now splits only when the number opens the hook
("45% less engagement…" → "Less engagement…"); otherwise the whole sentence is the headline, in the
brand primary, with no hero.

**Video frames.** Two of three fell back to Pexels on an "O", a "pause" label and clock numerals.
`image_gen.trivial_mark` drops a 1-2 character NON-word token from the stray-text check unless the
blind look calls it legible in the sentence that names it — words ("pause", "AI", "OK") stay strict.
`VIDEO_FRAME_DIRECTIVE` asks for no clocks, signs or labelled buttons and ONE gold or charcoal
accent object or wardrobe piece; `video_frame_failure` refuses a prompt naming a clock, sign,
button or keypad, and the video brand gate accepts only an accent colour (`video_accent_colors`),
never the off-white neutral any wall satisfies.

**A stat card says its number once** (the `data_card` post treatment draws it). A panel headline
that states the same AMOUNT as the hero ("Our routing change saved $12,000" beside "$12,000";
`hook_repeats_figure`, so "$12K" counts too) is dropped and the panel keeps its kicker and byline.
The context line under the hero is a COMPLETE phrase of the sentence (`complete_context`): Stage 1's
label survives only as a contiguous run of the sentence's words that neither carries the figure nor
dangles on a preposition; otherwise it is the words that follow the figure ("in model costs last
quarter"); with neither, no stat card is drawn — never "Our routing change saved in model costs".

**Carousel decks** follow the brand kit, draw their own figures in code and default to no stock —
`docs/content-quality-audits/carousel.md` §8.

## Showcase round B — floors, the last-resort card, the final hook gate, settings

Run on deployed v0.182.0 with the real brand kit (`gauntlet/out/showB/judged/*.json`).

**Headline-free floors (owner-delegated decision).** Of the gated renders, 8 of 10 headline-free
`photo_only` posts and all 6 video frames failed. Their specificity was 2–3 and the judge's reason
was "thesis not visually inferable". But in the feed the post text sits DIRECTLY above the image,
so the image is never asked to carry the thesis alone. `image_gen.rubric_floors` now sets:

| Render | specificity | scroll_stop | craft | no_cliche | stray text |
|---|---|---|---|---|---|
| `photo_only` post (no headline) | ≥3 | **≥4** | ≥4 | 5 | strict |
| video frame (with OR without a caption) | ≥3 | **≥4** | ≥4 | 5 | strict |
| anything with a typeset headline | ≥4 | ≥4 (cover/post) | ≥4 | 5 | strict |

This is a trade, not a relaxation. Specificity drops one point where the post text supplies the
thesis, and scroll_stop rises to 4 on video too, so a headline-free image must be visually strong.
In the round-B evidence most headline-free failures ALSO scored scroll_stop 2. Those still fail,
and are now caught by the last-resort card below instead of shipping bare. This supersedes round
11's "a captioned frame keeps 4".

**No post ships bare.** Posts 97 and 137 shipped with no image: the AI render was rejected and no
data or quote card applied. `post_image._render_last_resort_card` is a guaranteed $0 last resort
(`image_graphics.render_typeset_card`). It draws the post's hook — Stage 1's, else the post's own
first sentence of at most 12 words — on the brand panel, using the rotated panel variant. Its
scene (a gold ring and a dark-gold rule) is retired by round 4: the card is now a full-canvas
typographic layout (§ Showcase round 4). It is not judged: its words are the already-gated hook. The receipt records
`gate_verdict = "last_resort"` and `render_path = "code_drawn_last_resort"`, so it is never
mistaken for a graded image. A post goes imageless only when even this card cannot be drawn.

**The final hook gate.** A typeset card shipped "Routing prompts to models based costs". The
thesis was "Routing prompts to models based on complexity reduces AI costs …". `clause_hook` took
"based" as the main verb (an -ed word, and "reduce" was not in the verb list), so its compression
glued "based" to "costs". The judge never saw it, because the judge had passed a different hook
upstream. Three fixes:
- `_participle_phrase` stops an -ed word followed by a preposition from being picked as the verb.
- `dropped_preposition` deterministically refuses a hook that glues a participle or verb to a word
  its preposition owned. Compressing a noun's own modifier ("prompts [to models] reduces") is
  allowed.
- `_gate_final_hook` runs the guard and then the Stage-3 judge on the FINAL hook from every path
  (fallbacks and trims included) unless it is the exact hook the judge last passed
  (`ImageConcept.hook_verified`). A failure buys ONE regeneration with the reason, then
  `clause_hook`. If nothing passes, the image ships with no headline rather than a broken one.

`fit_hook` applies the same guard at render time.

**Clichés.** An editorial concept rendered a row of human brains around an alarm clock. `brain`,
`brains`, `human brain` and `brain model` are now in `CLICHE_OBJECTS`, next to "glowing brain".

**Setting is a sameness dimension.** Two consecutive people posts were both in a warehouse with
boxes. `post_treatment.setting_class` reads a setting CLASS — warehouse, home office, café,
conference, shop floor, retail, outdoors or office — off Stage 1's setting. On a receipt from
before this change, it reads the class off the briefed prompt instead. A class may not appear in
two consecutive AI renders; card posts in between do not reset the gate. `reroll_setting` replaces
a repeating or EMPTY setting with the first setting of a different class the post itself names,
and only then with a neutral default. The re-roll moves the concept's setting and its cast line
together, and the receipt's `rhythm.setting` records the class that shipped.

**Deck accents.** The stat cover's giant decorative "?" sat over "Model" in "Prevent AI Model
Failures: 4-Step Guide". Decorative glyphs, corner triangles and the typographic accent mark are
now queued and placed at save time (`carousel_creator.make_ink_draw`). Each takes the first
candidate position whose box meets no text box on the slide; with none clear, the accent is left
out. Glyphs are painted through an alpha mask, because Pillow ignores a text fill's alpha, which is
why the "faint" watermark glyphs had been shipping opaque.
`tests/unit/utilities/test_carousel_accent_clearance.py` asserts no intersection on every template
at three title lengths.

## Showcase round C — the feed text, quote cards, never bare, curated text

Run on main with #2270. Results: 5 of 8 posts fell to the last-resort card, 2 still shipped bare,
and all 3 video frames were rejected and fell to Pexels.

**Judge a headline-free image WITH the text above it.** A claymation snail carrying a message
stack, a collage filing cabinet pouring a waterfall and a marble-run router were all rejected at
specificity 2. The judge had seen each image ALONE. On LinkedIn the reader sees the post's
opening ABOVE a `photo_only` image or a video before "...more". So those renders now pass that
opening to the TARGETED judge as `feed_context`: `content_framework.feed_fold_text` takes the first
two lines, at most `LINKEDIN_FOLD_CHARS` (210).
- Specificity becomes "A reader sees this text first: '…'. Does the image fit and reinforce it, so
  the pair reads as one idea within 2 seconds?"
- The blind look never sees the feed text: it still describes the picture as a stranger would.
- #2270's floors are unchanged (specificity ≥3, scroll_stop ≥4, craft ≥4).
- The feed text is threaded through `inspect_render_quality` → `_gate_loop` →
  `render_image_gated` / `render_avatar_image_gated` → `ai_helper.generate_post_image`.
- The post image passes it only when no headline is typeset. The video frame always passes it,
  beside its caption.

**Quote cards are not POP-gated.** Two were rejected as "text-only, lacks visual intrigue", which
is a scene verdict on a deliberate typographic format. `image_gen.STRUCTURAL_ONLY_ARCHETYPES`
(`quote_card`) skips the scene/POP judge. The card is still gated structurally: the sentence is
the post's own verbatim (`post_treatment.is_verbatim`), and its text is fitted in bounds at
legible contrast by the drawer. The receipt records `gate_verdict = "code_drawn"`.

**Never bare, truly.** Posts 130 and 140 shipped bare although the card existed. Stage 1 had left
them no headline. Their opening sentence ("Imagine running your whole deployment on a cheap VPS
and still pushing four solid releases a day—no downtime, no Kubernetes.") was over the card's
12-word line, and nothing came after it. `post_image.last_resort_hook` now takes, in order:
1. Stage 1's hook;
2. the first sentence, if at most 12 words;
3. its first clause, if at most 18 words;
4. else its first 12 words marked "…" as cut.

Links, hashtags and mentions are never set. A card that raises an unexpected error is a WARNING
and the post ships bare; it never crashes the post.

**The video accent reaches the final prompt.** A frame failed brand_fit at 2 for "no gold
accent". `build_image_brief` now ends every `video` brief (avatar and base frame alike, authored,
judged or fallback) with `video_accent_backstop`. A prompt that names no gold or charcoal accent
gets one positive accent sentence appended.

**Curated commentary is plain text.** Curated drafts carried markdown `**bold**` list items, and
`escape_little_text` then printed the asterisks literally. Two changes:
- `curated_commentary.finish_post_text` gives every draft the text post's own finish:
  `sanitize_for_linkedin`, then `shape_for_dwell`.
- `escape_little_text` sanitizes before escaping, for anything stored earlier.

Text posts already go through `sanitize_for_linkedin` in `_refine_draft`, and curated prompts
already carry the same `post_writing_directive` length budget (1300–2000 characters). So the
showcase's ~1,400-character drafts were within the shared budget, not over it.

## Showcase round 4 — style, typographic cards, composed covers, deck rhythm

An independent critic failed the showcase on monotony (`gauntlet/critic4/critic_scores.json`). The
video half (title card, captions) is in `docs/content-quality-audits/video.md` § F3/F3b.

**Visual style is a sameness dimension.** Two claymation renders ran back to back: posts never
passed `recent_art_styles` to Stage 1. Now `post_treatment.style_of` gives every image ONE
`style` — an `ART_STYLES` key (claymation, cut collage, risograph, editorial photo),
`people_photo`, `code_drawn_card` or `quote_card` — recorded on the receipt's `rhythm` (read off
`archetype_rendered` / `art_style` for older receipts). The rule:
- the same style may not appear in two consecutive posts: `style_blocks` skips a quote card after
  a quote card and a data card after a code-drawn card; `post_image._avoid_last_style` drops a
  code-drawn head after a code-drawn card and turns a people tail into an object-only
  `editorial_concept` (rotated art style) after a people photo; Stage 1 gets the recent art
  styles, so an editorial never repeats the last one;
- quote cards are capped at `QUOTE_CAP` (2) per `QUOTE_CAP_WINDOW` (6) posts;
- the last-resort card is the ONE exception — it is the guarantee a post never ships bare.

**The last-resort card is typographic.** The ring-and-bar scene read as unfinished and repeated.
`image_graphics.render_typeset_card` draws one of `TYPESET_CARD_LAYOUTS` on the full canvas, in
the rotated panel variant's colours: `poster` (poster-scale headline under its kicker),
`grid_rule` (a minimal six-column grid between two rules), `quote_marks` (oversized marks over the
pull line — only when the hook is the post's verbatim words) and `number_led` (only when a figure
LEADS the hook, so lifting it leaves no hole). The layout rotates least-recently-used from the
receipts (`rhythm.card_layout`). Every string is fitted inside the safe area or the card refuses.

**Source lines are the caller's.** `image_graphics` printed "From the article" on posts with no
article. `validate_graphic_facts` now records a named source the text cites ("Source: Gartner")
or `""`; `render_graphic` / `render_code_drawn(source_line=)` take the caller's line, and a line
that does not fit is dropped, never replaced. No default is ever invented.

**Covers are always composed.** cover_19 shipped as a bare people photo: no hook survived Stage 1,
so nothing was typeset. `newsletter_cover.cover_headline` falls back to the edition's own title
(a complete clause, never cut mid-thought), and `ensure_composed_cover` composes any render that
came back raw — else draws a typographic cover of the same headline. A cover is never
`photo_only`; the receipt records `cover_composed` when the late path ran.

**Deck rhythm.** Five decks used two templates with identical inside slides, and the stat cover
carried a giant "?" watermark. The "?" is gone. Each deck takes a cover treatment from
`DECK_COVER_TREATMENTS` — `poster`, `code_drawn` (an element of the deck's own verbatim figures),
`number_led` (a title that leads with a figure) or `ai_concept` (the deck's ONE object-only,
gated `editorial_concept` render, `render_deck_cover_concept`, behind `DECK_AI_COVER_ENABLED`) —
and an inside accent motif from `DECK_MOTIFS`, both least-recently-used from the author's
`deck_render.json` receipts. A treatment that cannot draw falls through to the template's own
cover; every one draws the writer's title and subtitle whole.

## Showcase round 5 — numbers match their claim, decks count truly, settings rotate

The critic (`gauntlet/critic5/critic_scores.json`) failed text accuracy. The title card half is in
`docs/content-quality-audits/video.md` § F3c.

**A figure in the title IS the cover's number** (`image_concept.apply_title_stat`, the LAST step
of hook selection on a hooked surface). ed18's title said "53.7% of LinkedIn Posts Miss Their Mark"
and its cover drew 45%; ed16's said "The $30K We Nearly Squandered" and its cover said "$30K saved
per quarter". #2273's title preference only steered the HOOK (`thesis_number`), while the stat
card drew Stage 1's `graphic_facts.thesis_stat` — a path the title rule never saw. Now
`title_claim` reads the title's lead figure and its claim from the title's OWN words (the words
after the figure, else before it), and:
- the drawn stat is that figure labelled with that claim, traced to the title (`validate_fact`, so
  `assert_traceable` still holds); a source line that cited a different number is dropped;
- the headline is the title in sentence case when it is at most `TITLE_HOOK_MAX_WORDS` (8);
  a longer title keeps the current hook only if it carries no other number and, carrying the
  title's figure, shares a word with the title's claim — otherwise the hook is cleared and the
  cover's headline falls back to the title (`cover_headline`).

**One sentence per drawn figure, and the post must say it.** slot_144's slide wrote "63 recipients
were execs" — the story's 63 recipients welded onto the claim about the 51 executives emailed —
and its card drew the slide's sentence faithfully. A deck slide's figure now also needs ONE
sentence of the post to state both the number and the drawn label
(`image_graphics.figure_in_evidence`, via `create_carousel_slide_images(evidence=post_text)`); a
from→to pair needs both halves. The cover/edition graphics were already one-sentence-per-fact
(`validate_fact`).

**Number + unit typography.** rhythm_6 printed "85 %": the source wrote a narrow no-break space,
and the hook copied it. `image_concept.tidy_figures` sets every number tight to its unit and runs on
the final hook (`restore_number_casing`), every composited headline (`headline_parts`) and every
typeset card.

**Deck counters are the final page count** (`carousel_creator.slide_counter`). The stat cover read
"1/5" while its inside slides read "1/3" — each template counted reveals, parts or items. Every
counter is `idx / total` of the slides actually rendered (story decks: "Part idx of total"), and
the stat template's "#n" strip label is gone. Any "N slides" the writer put in the copy is rewritten
to that count (`fix_slide_count_copy`) — slot_142's cover promised "4 slides" over six.

**The numbered circle rotates out.** slot_142 and 143 were consecutive listicles, both with the
numbered-circle badge. The deck receipt now records `badge`; a listicle after a deck that drew the
circle draws a heavy brand rule in its place (`deck_badge`; a pre-rule listicle receipt reads as
the circle it drew).

**The setting gate holds across processes.** rhythm_3 and rhythm_4 were warehouse scenes in a row.
The gate already read the receipts on disk, but rhythm_3 briefed "an e-commerce fulfillment
center", which no keyword matched, so it recorded no setting — and it had no Stage 1 concept, so
the re-roll never ran. Now: the warehouse class covers fulfillment / distribution centres, loading
bays, sorting and packing areas, logistics hubs and plurals; the receipt classifies the PROMPT
first (what rendered); `rhythm_history` re-reads an unclassified AI scene's prompt; the re-roll
runs without a concept and rides the brief's `extra_direction`; and `blocked_settings` caps any
class at `SETTING_CAP` (2) per `SETTING_CAP_WINDOW` (6) classified renders on top of
never-twice-in-a-row.

## Post rhythm — treatments, panels, grades and the sameness gate (anti-monotony round)

Every post image was the same composite — a photograph beside a charcoal type panel — so an
author's profile grid read as one image posted N times. Posts now ROTATE; **newsletter covers do
not** (they keep the one consistent card and never pass a panel variant). `post_treatment.py`
decides, deterministically, from the author's own post receipts
(`post_image.recent_post_receipts`, newest first, the same bounded 40-directory walk); the
renderers below it are unchanged except where named.

| Treatment | What ships | Renderer |
|---|---|---|
| `typeset_card` | The composite — #2254's archetype chain (`people_scene`, `editorial_concept`, or a code-drawn chart / receipt / checklist / before-after) beside the type panel, on a ROTATED panel variant | `render_image_gated` / `render_avatar_image_gated(panel=)` |
| `photo_only` | The AI scene alone: no headline, no kicker, no compose, rendered at the post's own ratio. AI archetypes only (`ai_archetype_only`) | the same gated renderers, `hook_text=None` |
| `data_card` | IS #2254's `stat_card` for posts — verified numeral, its claim, the source line, `assert_traceable` before ink. Never duplicated: `typeset_card` drops `stat_card` from its chain | `render_code_drawn` with the chain `(stat_card,)` |
| `quote_card` | One sentence of the post, VERBATIM, on the charcoal ground over "— <author's byline>" | `render_code_drawn` → `image_graphics.render_quote_card` |

**Choosing the treatment.** `card_share` (brand kit, 0–1, default 0.4) is the share of posts that
get a `typeset_card`, honoured over the last `CARD_SHARE_WINDOW` (10) posts by a deficit rule
(`wants_card`): a card is due when the rounded target for this window exceeds the cards already
shipped — 0.4 from no history gives no, card, no, card, no, no, card, no, card, no. A due card
leads the chain; otherwise the other three lead, least-recently-used, and the card comes last.
A treatment the post CANNOT take is skipped before rendering with its reason on the receipt
(`unavailable_treatments`): `data_card` needs a verified thesis stat and a headline;
`quote_card` needs an opinion post, a quotable sentence and a byline (`profile.full_name`).
`photo_only` and `typeset_card` are always available. A treatment that FAILS at render time (a
refused or rejected graphic, a non-verbatim quote, an AI render the judge rejected) falls to the
next in the chain, through the gate again — but once an AI render was spent only the $0
code-drawn cards may follow, never a second AI render; with none left a rejection ships the post
bare exactly as before (#2105). Pre-rotation receipts read as the charcoal `typeset_card` they
were, so the rotation starts from the real recent past.

**The quote.** "Opinion post" comes from Stage 1 — a `contrast` hook, or a negative/mixed valence
on a thesis that asserts something — or from the post's own stance markers ("I think", "most
founders", "stop …ing", "unpopular opinion"); a positive, unmarked report is not one. Candidates
are deterministic (`quote_candidates`): complete sentences ending `.`/`!` (never a question or an
ellipsis), 30–160 characters and ≥5 words, no hashtag, link or mention, nothing the brand font
cannot set, ranked by stance markers, thesis overlap, first person and length — at most five.
ONE `lem-simple` tiebreak (`pick_quote`, `@llm_step("post_quote_pick")`) picks among them; any
failure keeps the deterministic best, and the quote is never authored. The check is a
character-for-character SUBSTRING of the post (`is_verbatim`), re-asserted before ink
(`assert_traceable`: the drawn text must equal its source sentence).

**Panel variants** (`image_compose.PANEL_VARIANTS`): charcoal (the card as it always was), off-white
with charcoal type, gold with charcoal type; the kicker keeps the dark-gold accent only where it
still reads at 3:1. They rotate least-recently-used among `available_panels(brand)` — the
variants whose headline, hero numeral and under-hero line all reach **4.5:1** (WCAG AA) in the
author's own colours; charcoal is always offered. `test_post_card_render.py` asserts the ratio
for every variant. The variant applies to every post card with a headline panel (`typeset_card`
and `data_card`); an overlay layout's scrim stays charcoal.

**Photo grades** (`image_brief.PHOTO_GRADES`): daylight / cool interior / warm dusk, least-recently-
used, on every AI-rendered post scene — the author is told the grade (`extra_direction`) and the
render prompt carries it deterministically (`with_grade`). Every clause keeps the round-5
brightness rule positively worded (dusk is "warm … every face fully and evenly lit … bright and
readable"); none may match `_DARK_SCENE`, because naming dark summons it on FLUX.

**The sameness gate.** Over the last `SAMENESS_WINDOW` (5) receipts, no dimension — treatment,
layout, panel, shot, grade — may run more than `MAX_RUN` (2) posts in a row (`style` is stricter:
never twice in a row, § Showcase round 4): a pick that would is
re-rolled to the next least-recently-used option (`sameness_pick`, `gate_value` for Stage 1's
own layout and shot picks). The gate OUTRANKS `card_share` — a share of 1.0 still yields at most
two typeset cards in a row. A dimension is recorded only where it shaped the image (a panel on a
card, a grade and shot on an AI scene) and is `null` elsewhere, which breaks a run.

**The receipt** carries it all under `rhythm`: `treatment`, `layout`, `panel`, `shot`, `grade`,
`card_share`, `card_wanted`, the planned `chain`, every `fallbacks` entry with its reason,
`rerolled` (the dimensions the gate moved), and for a quote card `quote`, `quote_pick` and
`opinion`. A code-drawn card's receipt records its thesis as the focal concept and an EMPTY
prompt — no render prompt existed.

**Gauntlet harness.** `scripts/simulate_post_rhythm.py posts.json --user-id N --assets-dir DIR`
renders N posts in sequence through `generate_image_for_post` into DIR, so receipts accumulate and
the rotation acts, and writes a feed-order (newest first) contact sheet. `--offline` is $0: a
deterministic Stage 1 that still runs the real archetype and layout/shot rotation, a template
brief, labelled grade-tinted placeholders for AI scenes and an `unchecked` judge — the code-drawn
cards and panel variants are real. The paid mode mocks nothing; run it in a prod-image sidecar.

**Open question for the paid run.** `photo_only` is judged with no headline, so the specificity
floor (4) must be met by the scene alone; the uncaptioned VIDEO frame relaxes that floor to 3 for
the same reason (the post text above carries the thesis). Posts do NOT relax it here — whether
they should is a call for the gauntlet's evidence, not this change.

## Never truncate a hook; source spelling; no label survives (round 15)

- **No truncation.** Round 14's cap cut "Ignoring AI's hidden buyers raises deal costs" to
  "… raises deal". `_final_hook` now REGENERATES a hook over six words with the reason; still
  long, it takes `clause_hook`, which builds a complete clause by construction: the thesis's
  first clause with "-ly" adverbs dropped, then subject + verb group + object compressed to their
  noun heads ("Ignoring AI's hidden buyers raises costs"). Where a caller's concept reaches the
  brief without Stage 1, `fit_hook` applies the same clause.
- **Source spelling.** `restore_number_casing` re-spells every number token as the source writes
  it ("$30K", "53.7%", "3x") after hook selection.
- **No label on any path.** The claim-not-label rule runs on the final hook too; a label is
  replaced by `clause_hook`, which takes the subject and a verb from the thesis ("Routing prompts
  cuts AI spend") and, for a thesis with no verb at all, asserts that the noun group matters.
- **Signage on posts.** Post 100's garbled sign ("UDA HINEBEL") was caught by the gate; the
  round-9 no-captions/signage/badges clause is confirmed on the post_image render path by test.

## A hook is true, grammatical and short (round 14)

Round 13's stat-survival rule paired a number with the wrong claim: ed18 shipped "53.7% less
engagement than humans", but 53.7% is the SHARE of AI-generated posts and the engagement gap is
45%. That is now impossible:

- **Fact fidelity, deterministic.** `number_claim_mismatch`: a number in a hook is valid only
  when the number AND its claim word (`claim_noun`: the content word it quantifies) occur in the
  SAME source sentence. It runs inside `hook_rejection`, so every Stage 1 option, regeneration and
  built hook passes it; the stat fallback and `derive_hook` drop the number rather than pair it
  falsely. `thesis_number` picks, among the tied stats, the one whose own sentence best matches
  the thesis.
- **Fidelity and grammar judge.** The end-of-Stage-1 `lem-simple` hook check now also asks
  whether the headline is grammatical English, at most 6 words, and literally true against the
  source sentence it cites (`cited_sentence`, passed to the judge). On no, the hook is
  regenerated once with the reason; a second no takes the deterministic `derive_hook`.
- **Six words is HARD** — enforced without truncation since round 15 (below).

## Stats, not versions; framing rotates; good news smiles (round 13)

- **A headline number is a STAT.** `stat_numbers` accepts a percentage, a currency amount
  ("$30K"), a multiplier ("3x"), or a number followed by a unit or count noun ("45 posts") —
  never a version beside a capitalised name ("Opus 4.5", "GPT-5.2"; ed17 rendered "4.5") and
  never a bare year. The number keeps the SOURCE's casing ("$30K", not "$30k").
- **The stat survives.** `_enforce_stat`: when the thesis has a stat, the hook must carry it
  verbatim — an offered hook with it first, else ONE regeneration naming the stat, else the stat
  plus the first anchor's noun (post 102 had shipped "Routing saves most spend" for a 60% cut).
- **A hook is a claim.** `asserts_something` requires a verb (a copula or modal, a known verb in
  any inflection, or an -ed form); a question or a number_claim passes. "Routing prompts to
  models by complexity" is a label and is refused.
- **No mugs.** mug, cup, coffee, tea and latte are props (`prop_failure`); the directive says
  hands are empty, gesturing, or hold a face-down phone.
- **Framing rotates, the place does not.** `SHOTS` (close-up portrait, over-the-shoulder
  two-shot, walking-and-talking two-shot, standing at a window, presenting to a small group seen
  from behind) rotates least-recently-used from the receipts (`recent_shots`), like the cast.
  The setting still comes from the article.
- **Good news smiles, literally.** Positive valence is "a relaxed, genuine smile, eyes bright,
  shoulders loose" — in the brief, the repair reason and the emotion-repair directive
  (`_emotion_beat`). "relief" without a smile is refused: the model reads relief as distress
  (ed16 rendered alarmed four times).

## What the blind critic asked for (round 12)

The in-pipeline judge passed every item in round 11; the independent blind critic did not. Each
of its notes became an engine rule:

- **The setting is the article's.** "warehouse floor" from the cast rotation turned up on four of
  nine items whatever the topic. The rotation now covers only people (gender, age band,
  ethnicity); Stage 1 returns `setting` — the story's own scene, else the audience's real
  workplace — validated by `_ground_setting` like an anchor (no name, number, prop, tech hardware
  or stock symbol, and grounded in the source; a weekday or month is a time, not a name). It
  reaches the author as `SETTING` and the cast line as ", at …".
- **The lead number leads.** `thesis_number` finds a grounded number tied to the thesis — in the
  title, the first two body sentences, or the sentence carrying the claim — and
  `lead_with_number` forces a `number_claim` hook carrying it (an offered one first, else the
  number plus the first anchor's noun). Shape rotation applies only without one.
- **The hook asserts the thesis.** An "X vs Y" hook is refused (`hook_rejection`).
  `check_hook_against_thesis` (ONE `lem-simple` call at the end of Stage 1, failing open) asks
  whether the headline asserts the main claim rather than a caveat; on no, the hook is
  regenerated once with that reason. It sits in Stage 1 rather than Stage 3 because the hook is
  the concept's — Stage 2/3 never rewrite it. The same call returns the headline's own valence,
  and the face follows THAT (post 140 grinned at "hurts").
- **No symbolic props**: figurines, tokens, cards, flags, chess, blocks, sticky notes, game
  pieces are refused by `prop_failure`.
- **Restrained faces.** "open mouth", "gasping", "alarmed", "furrowed brow", "wide-eyed",
  "shocked" are refused in briefs; negative valence is concern in posture and eyes, mouth closed.
  The judge asks "Is any expression overacted?" and a yes caps craft at 3.
- **Brand-bound colour.** Wardrobe and objects stay within gold, charcoal, off-white and natural
  tones; `saturated_color` refuses a saturated red, blue or green object (a muted tone or nature
  passes).
- **Covers at 1920x1080** (LinkedIn's recommended size); posts stay 1080x1350.
- **Video frames keep the near foreground empty** (`VIDEO_FRAME_DIRECTIVE`, built on
  `NO_NEAR_FOREGROUND`): Runway smears a hand close to the lens. The motion prompt's
  `MOTION_DISCIPLINE` lives on PR #2249 and should append the same clause.

## Negatives, inferred hardware, caption-less video (round 11)

- **A quoted negative is not stray text.** The blind judge wrote `Visible text: - "There is no
  visible text."` and the parser failed the render on it. `_no_text_seen` now recognises negative
  statements ("no visible text", "there is no text", "none", "Visible text: none", "No text is
  visible in the image"); real text ("No entry") still counts.
- **Tech hardware is inferred, so it is refused twice.** No brief named a laptop, yet "AI routing"
  and "smaller model servers" rendered laptops showing code, racks and cables. Anchors naming
  `TECH_HARDWARE` (laptop, computer, monitor, screen, keyboard, server, rack, cable, data center,
  terminal, code) are dropped at parse time and by `usable_anchors`, and EVERY render, on every
  surface and backend, ends with `NO_TECH_CLAUSE`: "The scene contains no computers, laptops,
  screens, servers, cables or code; people interact with each other in a real place." The cliché
  checks are unchanged.
- **A caption-less video frame passes specificity at 3.** With `VIDEO_CAPTIONS` off,
  `burned_caption_text` is None and the frame is judged alone. The post text above the video
  carries the thesis and a lone frame cannot, so at 4 every frame scored 2-3 and every video fell
  back to Pexels stock — a regression from shipping Runway clips. With a caption headline the
  floor stays 4; no_cliche (5), craft (4) and stray text are never relaxed. Turning
  `VIDEO_CAPTIONS` on (sound-off viewing) restores the stricter floor.

## The render is only a photograph (round 10)

- **Photo-only render prompts.** Told it was making "a LinkedIn newsletter cover" with "negative
  space" for a headline, gpt-image designed a cover and typeset fake text into the empty side
  ("ENGINEERING LEAD / Billing Dashboard"). On a composited surface (newsletter, post_image) the
  render prompt now describes ONLY a photograph. `render_framing_failure` rejects an authored
  prompt naming LinkedIn, a newsletter, cover, post, headline, title, text area, negative space,
  a magazine, an editorial layout or typesetting, or repeating a hook/kicker word (a word that is
  part of a visual anchor stays allowed: it is the subject, drawn rather than written). Every
  brief that ships, the fallback included, then passes `photo_only_prompt`, which rewrites the
  opening to "A candid documentary photograph", drops framing sentences and words, and appends
  the square-frame instruction. The USE CASE stays author context in the brief author's prompt.
  The square-scene directive no longer mentions a headline. Whitepapers, brochures and
  dashboards joined the prop list.
- **A hook always.** On a composited surface, when no Stage 1 hook passes `hook_rejection`,
  Stage 1 is asked ONCE more for a hook with every rejection reason; if that fails too,
  `derive_hook` takes the thesis up to its first clause boundary (at most 6 words), else a
  grounded number plus the first anchor's noun.
- **Video frames read with their caption.** `inspect_render_quality(..., surface="video",
  hook_text=<the caption video_captions burns in>)` tells the judge the caption is burned into
  the MP4 over the frame and is NOT in the still. Nothing is composited and the render prompt
  never carries it. The wiring belongs in `_generate_video_src` (PR #2249).

## No labels, a kicker always, anchored comparatives (round 9)

- **No painted captions.** gpt-image rendered the brief's role and group nouns as labels — a
  poster reading "Founders & Content Teams In Conversation", badges reading "SECURITY". Both
  `with_no_marks` constraints now end "No captions, titles, posters, signage, name badges,
  lanyards with text, or labels of any kind." `PROPS_DIRECTIVE` tells the author to describe
  people by appearance and action, and `_deterministic_failure` refuses a group caption
  (`_GROUP_LABEL`: "X & Y", capitalised "X and Y", "x and y teams"). Posters, signage, badges,
  lanyards, labels, contracts and agreements joined the prop list, so no visual idea or anchor
  can carry one either.
- **Video frames.** The props directive and rejection already ran on `surface="video"`; the leak
  was the word "contract", which the prop list did not know, and the fallback interpolating a
  thesis that named one. The fallback now drops a thesis or audience that names a prop or a
  group caption. Security hardware is a cliché: a safe dial, vault, combination lock, padlock,
  dial, light switch, toggle switch, lever ("safe" alone stays an adjective).
- **A kicker always.** When Stage 1's kicker fails `valid_kicker`, `derive_kicker` takes the 1-2
  most frequent topic words (title words first, 4+ letters or an allowed acronym), uppercased.
  `concept_kicker` covers a caller's concept with no kicker, so every composite carries one.
- **Comparatives need a reference.** `vague_comparative` refuses a hook like "Verification is
  much cheaper"; "than" or a number anchors it ("45% less engagement").
- **Positive valence never touches the head.** Hands on the head, face, temples or forehead and
  "head in hands" are refused for positive valence; the directive asks for eyes open, engaged with
  the other person or the task.

## Split layouts, square scenes, no screens (round 8)

Every overlay layout still put type over a face or a torso, whatever the brief asked for, so the
rotation is now SPLIT layouts only — the scene and the type panel never overlap by construction:

- **Layouts.** Covers (16:9, a 1600x900 canvas) rotate `split_left` / `split_right` with a 40%
  type panel; posts (4:5, built at 1080x1350 directly — no later crop) rotate `split_top` /
  `split_bottom` with a 34% panel. The panel grows a step (45/50%, 40/46%) only when the
  cap-height floor is not met. A 4px dark-gold seam rule marks where panel meets photograph.
  The overlay layouts stay in `image_compose.LAYOUTS` for a caller that asks.
- **Square scene.** Any composited render is requested at `1:1` (`_scene_ratio` in
  `image_gen.py`, `ratio = "1:1"` in `build_image_brief`) — the avatar path included — and the
  brief asks for "a SQUARE frame with the subject centred and filling it". `cover_fit` scales it
  to cover the scene region and centre-crops, so a centred subject survives either aspect.
  `conform_to_ratio` (PR #2249) does not exist on this branch; a composited post never needs it.
- **No screens.** `prop_failure` refuses laptops, monitors, keyboards, tablets, displays and
  computers on every surface (video frames included); a phone survives only face-down. Scenes are
  people interacting in a real environment. "server room", "servers", "racks", "server cabinet"
  and "data center" are clichés.
- **Anchors are never props.** `_ground_anchors` drops an anchor `prop_failure` would refuse, so
  Stage 1 can never ask the brief for something the brief must then refuse.
- **Specificity is the descriptors alone.** The judge's anchor, gist and headline-names-subject
  answers no longer cap `specificity`; they land as `advisory:` issues on the receipt
  (`advisory_issues`).
- **Cover avatar.** Auto uses `guardrails.resolve_avatar_for_concept` with Stage 1's concept when
  that fit rule exists (PR #2249); otherwise the guardrails + classifier conjunction decides.

## The cover is an editorial system (round 7)

Round 6's composites were a big step (exact gold and charcoal, Montserrat, rotating layouts, a
diverse cast), but gpt-4.1 and the blind critic both still read every scene as "a generic office"
— the headline carried the topic alone, because the clichés that used to say "AI" are rightly
banned. `image_compose` now typesets a whole editorial cover from Stage 1's facts:

- **Kicker** — a 1–3 word UPPERCASE topic tag ("AI CONTENT AUDIT", "LLM COSTS", "B2B BUYING")
  above the headline, in the brand accent (dark gold), letter-spaced, at `KICKER_RATIO` (35%) of
  the headline size. Stage 1 returns it; `valid_kicker` keeps it only when every word matches the
  source by its root (AI/LLM/B2B… always pass). It is what makes a human-reaction scene read as
  "about AI" without a cliché.
- **Hero numeral** — `split_hero` lifts a grounded number ("45%", "$30K", "53.7%") onto its own line
  at `HERO_RATIO` (2×) the headline size in the brand primary; the rest of the hook goes underneath
  in the brand `neutral_light` (off-white). A hook with no number stays all gold.
- **Byline** — the newsletter's title when the caller passes `signature`, otherwise the author's
  `profile.full_name` (already in hand — no new DB read), set small at the bottom in off-white at
  50% opacity; omitted when empty. Posts take the author's name the same way once their call site
  passes it.
- **Bigger type.** Everything scales from one headline size, binary-searched to the largest the box
  allows (so the widest line fills the box). The floor is a CAP HEIGHT of 9% of the image height on
  landscape covers and 6% on 4:5 posts (`MIN_CAP_FRACTION`); when the default backing cannot hold
  that in three lines, the panel widens (`PANEL_WIDTHS` 38→43→48%) or the band deepens
  (`BAND_HEIGHTS` 26→32→38%) before settling, and a result still under the floor is logged. Hooks
  are sentence-cased (first letter up, acronyms kept), in Stage 1 and again at compositing.
- **`full_bleed` is out of the rotation** (it set a headline across a face); covers rotate
  `panel_left`, `panel_right`, `lower_third_band`, posts `band_top`. Every layout has its own
  negative-space instruction — `lower_third_band` keeps faces, torsos and hands in the upper 55%.
- **No props, on any surface.** "Blank" never held: "Proposal" on a clipboard, "COST REDUCTION
  CHECKLIST", "KPI Dashboard", `console.log` on a monitor, "Name / Date" on a form. `prop_failure`
  refuses paper of every kind (documents, sheets, reports, proposals, clipboards, checklists, forms,
  invoices, bills, folders, binders), whiteboard or chart content and code — on EVERY surface, the
  round-5 edge-on exception included — and any screen not explicitly the back of a laptop, facing
  away or face-down. The brief gives the author the alternatives (`PROPS_DIRECTIVE`: gesture,
  posture, two people interacting, the environment; hands empty or holding a mug, pen or phone
  face-down), prop anchors are never asked for, and prop ideas are filtered before ranking.
- **Good news never reads as pain.** On a positive valence the face is "a quiet, satisfied
  half-smile and relaxed shoulders, eyes open", and eyes closed/squeezed or a hand on the chest is
  refused (`_PAINED_RELIEF`) — "relief" had rendered as both.
- **The judge reads the cover as one.** The targeted prompt carries the kicker, says "Read the
  kicker, headline and scene together as one cover", and the 5 descriptor adds "the kicker +
  headline name the exact topic and the scene shows the human stakes of it" — a generic office is
  fine when kicker, headline and emotion are specific.
- **A caller's concept is used exactly as given** — only a concept `build_image_brief` computes
  itself gets the per-piece layout/cast rotation (PR #2249 relies on this).

## The headline is composited, never rendered (round 6)

**This supersedes every earlier "hook in the render" rule below** — the round-3 cover layout, the
round-4 typography spec and the round-5 post layout all asked gpt-image to DRAW the headline, and
the blind critic traced most remaining defects to that one root cause: the panel drifted through
charcoal, black, navy and grey-green, the type through gold, pale gold and off-white at any
weight, and three of nine headlines clipped the frame edge. So:

- **`utilities/ai/image_compose.py` is the ONE place a headline meets a render.**
  `compose_headline(render_path, hook, layout, brand, surface)` typesets the hook with PIL in the
  bundled **Montserrat ExtraBold** (SIL OFL, `src/cqc_lem/resources/fonts/` with `OFL.txt` —
  `src/` ships whole in the image; the system bold faces `carousel_creator` uses are the
  fallback). The panel/band/scrim is the brand's neutral dark and the headline its primary,
  **exactly** (`brand_style` reads "name (#hex)" pairs from the brand clause; the reference brand
  is light gold `#E9D437` on charcoal `#1F1F1F` with a dark-gold `#A89816` rule). Text wraps to at
  most 3 lines and binary-searches the largest size that fits a safe box keeping ≥6% of the frame
  clear on every side; each line is aligned by its INK, so no glyph crosses the margin. A cover
  line is held to ≥7% of the frame height (legible at 400px); anything smaller is logged.
- **Layouts are compositing templates**, rotated least-recently-used over the last 4 receipts
  (`assign_layout_and_cast`, a sibling of `enforce_graphic_cap`): covers `panel_left` /
  `panel_right` (a solid panel over 38% of the width), `full_bleed` (over the scene's darkest
  third, a charcoal gradient scrim, no panel) and `lower_third_band` (a band, its text held to the
  central 60% of a landscape frame); posts `band_top` (a band over 26% of the height below a 6% top
  margin — the 4:5 crop change is a separate PR). The brief tells the author only to keep that
  region calm negative space (`NEGATIVE_SPACE`).
- **No render carries any text.** The declared-hook exception is gone: `with_no_marks` is the
  blanket "no text" on every surface again, the brief never quotes the headline (a quoted string
  is rejected), and `carries_hook` now means "gets a composited headline" — covers and posts only;
  video frames get none (captions come later) and carousel is unchanged.
- **The judge grades each image where it lives.** The blind look and the text check run on the
  RAW render — any text it transcribes caps `text_accuracy` at 2 (`text_accuracy` is decided
  deterministically now). The targeted questions run on the COMPOSITE: specificity, scroll stop,
  brand. Headline legibility and clipping are asserted in `test_image_compose.py`, not judged.
- **Calibrated specificity.** The targeted prompt anchors the scale — 5 "with its headline, a
  scroller would correctly guess this piece's argument", 4 "clearly on-topic, slightly generic
  scene", 3 "the scene could sit on many unrelated posts even with the headline", ≤2 "misleading
  or off-topic" — gated at ≥4. The round-4 "reusable on an unrelated article" cap is dropped; the
  descriptors carry it.
- **Hook fidelity and variety.** `hook_is_faithful`: every number in a hook must appear in the
  source, and every other content word must match a source word by its root, bar common function
  words, verbs and adjectives (`_HOOK_FREE_WORDS`; AI/LLM always) — the critic caught "reach" for
  "engagement" and "influencers" for "hidden buyers". Stage 1 offers one hook per SHAPE
  (`number_claim`, `contrast`, `question`, `plain_claim`) and `rotate_hook_shape` picks the
  least-recently-used valid one — every hook had been "AI X: N% Y". Hooks may run to 6 words now
  that they are typeset.
- **Cast and emotion.** A people_scene gets a rotated cast hint — gender, age band, ethnicity and
  setting each rotate independently and least-recently-used, the role is drawn from the piece's
  anchors ("a South Asian woman in her 50s, an agency owner, on a warehouse floor") — so the series
  stops reading as one man in his 30s-40s; an avatar is never recast. Stage 1 sets a `valence`
  (positive / negative / mixed): a saving reads calm, pleased, relieved or wry, a risk concerned,
  skeptical or frustrated, and the judge's emotion question carries it. Emotion must be authentic
  and restrained — the carry-forward directive no longer says "exaggerated" (a frame showed a man
  wailing over a bill) and a cartoonish face caps `craft` at 3.
- **Video frames** take the no-paper rule (they had brought back a sheet reading "MONTHLY BILL")
  and the brand-accent gate, as covers and posts do.

**The hook carries the thesis; the image carries emotion and specificity (gauntlet round 2).**
Round 1's fixes removed the clichés and stray text but overcorrected: all four covers came back as
people at laptops or holding paper, neutral faces, no hook, no brand color — specificity 2 on every
one, "a stranger could not guess the thesis". The theses were abstract (AI hallucination, AI posts
underperforming, hidden buyers) and a literal scene cannot carry one. So the engine follows the
YouTube-thumbnail / editorial-cover pattern:

- **Every newsletter cover carries its hook**, whatever the treatment (`carries_hook`); posts,
  carousels and video keep it optional (graphics only). `COVER_HOOK_LAYOUT` states the placement:
  one third of the frame, left or right opposite the subject, bold geometric sans, brand light gold
  or off-white on a dark area, ≤5 words; the subject takes the other two thirds. Stage 1 also
  returns `hook_alternatives`, and the first that passes the hook rules wins, so a shouted primary
  hook no longer costs a cover its headline. `with_no_marks` already handles a declared hook.
- **Emotion is required.** `people_scene` must show the `emotional_beat` as a specific, visible
  reaction (a wince at a number, mid-laugh relief, a raised eyebrow at a draft, arms crossed in a
  tense meeting) in a close or medium framing where the face reads at thumbnail size. "Neutral
  expression", and "looking at / typing on / focused on / working on a laptop" as the action, are
  now `CLICHE_OBJECTS` entries — a laptop may only be a prop.
- **Three ideas, then a cheap pick (Idea2Img).** Stage 1 returns 3 one-sentence `visual_ideas`,
  each combining the hook with a concrete scene or juxtaposition from the anchors, at least one
  people-led. `idea_rejection` drops any with a cliché, a name or a number BEFORE ranking;
  `pick_visual_idea` makes ONE `lem-simple` call ranking the survivors on specificity to this
  article, surprise, thumbnail legibility and cliché distance (fail-open: the first people-led
  idea). Stage 2 builds on `chosen_idea`; `rejected_ideas` and `idea_pick_reason` ride the concept
  onto the receipt.
- **Specificity is judged on headline + image together.** Stage 3 and the vision judge both get the
  hook as "the headline": "together with its headline, would a viewer correctly guess what the
  article argues?" The anchor count is now advisory — the brief needs ONE anchor in frame (two sent
  ed16 to the fallback twice), and the judge caps specificity only when NO anchor is seen or the
  pair does not convey the thesis. Documents and checklists stay blank or unreadable: "numbered",
  "bullet points", "a list of numbers" and "a three-step checklist" are refused as words on a
  surface (ed17's checklist came back reading "1, 2, 8").
- **Brand on every render.** Every brief is asked for ONE deliberate brand-color element named by
  its color — the hook color, a wardrobe accent, a wall or background accent, or a warm gold grade
  (`BRAND_ACCENT_DIRECTIVE`; colors from `brand_colors(brand_kit)`, default gold / charcoal /
  off-white). On covers it is enforced: a prompt naming none of them is rejected.
- **The fallback builds from the top-ranked idea plus the hook**, with the cover layout and a named
  gold accent — never a bare anchor list.

**Gauntlet round 3 → the round-4 rules.** All four covers carried gold, a hook and visible
emotion; two were accepted. What still failed became engine rules:

- **Token budget.** `lem-medium` (a reasoning model) ran out of `max_tokens` on ed18 —
  `empty response (finish_reason=length)`, then unparsable JSON, then the fallback. The brief
  author, Stage 1, the idea ranker and the Stage 3 judge now get 6000 tokens and
  `reasoning_effort="low"` (`REASONING_EFFORT`; LiteLLM's `drop_params: true` drops it for a model
  with no such knob), and a length cut buys ONE extra attempt (brief and Stage 1) before giving up.
- **Profile text never renders.** ed18's fallback opened "The author is in the Artificial
  Intelligence, E‑commerce industry. Make the visual feel on-brand…". `_profile_visual_context`
  is AUTHOR context only: the fallback no longer interpolates it, and `strip_author_context`
  removes it (and any "The author is…", "Make the visual feel…", "When a person appears… IS the
  author…" sentence an author echoes) from every prompt before validation.
- **Documents and screens stay unreadable.** ed16 asked for "a printed billing dashboard screen"
  and "a paper check" and the chart paper read "MEITE TCHIREUM". `legible_document` refuses
  "printed … dashboard", "chart with labels/figures", a paper/bank/printed check or cheque, and
  "invoice showing/listing…"; `unblanked_document` refuses any paper, chart, report, invoice,
  dashboard, screen, draft, statement, checklist or slide named without saying HOW it stays
  unreadable — "blank", "seen at a steep angle so no writing is legible", or "out of focus".
  Handwritten notes "urging…" are words on a surface. The same filters run on visual ideas, and the
  fallback states `DOCUMENT_BLANKING` outright. A `text_accuracy` repair (gpt-image) now reads
  "remove every legible mark from papers and screens; the only text is the hook".
- **Hook typography is a fixed brand spec** (`hook_type_spec`): "set in a heavy geometric
  sans-serif (Montserrat ExtraBold style), sentence case, light gold on a dark area, large enough to
  read at 400x225" — the brand kit's font vibe and first named color replace the defaults, and a
  sans-serif is always stated. A hook prompt that never says "sans" is rejected, and the render-side
  hook clause in `with_no_marks` states the sans-serif too. ed16 rendered in a serif face.
- **Hook quality.** Sentence case: three or more capitalised words (acronyms excepted) is Title Case
  and refused ("When AI Misses the Mark"). Numbers and facts are welcome — the hook is where facts
  belong ("53.7% miss the mark"). A hook sharing ≥60% of its words with the title is refused.
- **The thesis is one gist claim** of ≤20 words (`gist_thesis` keeps the first clause), and both
  judges ask whether the headline plus the prompt/image conveys the GIST — not every benefit.
- **The judge asks three more things:** does the visible emotion match the beat (a mismatch caps
  `scroll_stop` at 3, a fail); is the headline set in a sans-serif (a serif caps `brand_fit` at 3);
  and could this exact image be reused unchanged on an unrelated business article (yes caps
  `specificity` at 3 — ed18's flip chart scored 5).
- **Covers render two candidates per attempt** (`_gate_candidates`: `IMAGE_GATE_CANDIDATES` when
  set, else 2 for `newsletter`, 1 elsewhere) and keep the better rubric total.

**Gauntlet round 4 → the round-5 rules.** Thumbnail-style, brand-gold covers with grounded
number hooks and real expressions; ed19 scored 5 across the board. What remained:

- **The reuse test asks about the COVER, not the bare image.** With a headline, the judge asks
  "Considering the headline '…' together with the image, could this cover be reused unchanged for
  an unrelated article?" (yes still caps specificity at 3). ed17 and ed18 were capped at 3 for an
  image their grounded headline made specific.
- **A headline names its subject.** The judge asks "Does the headline alone name its subject — not
  just a number?" — no caps specificity at 4 (passes, never a 5). Stage 1 is told a numeric hook
  names its subject within 5 words ("AI posts: 45% less reach"), and `names_its_subject` enforces
  it: a hook with a digit must carry a generic acronym the piece is about (AI) or a non-generic word
  the concept's title, thesis, facts or anchors use — "45% less engagement" and "$30K wasted" are
  refused, "$30K wasted on AI" passes (`_GENERIC_HOOK_WORDS`).
- **No paper props on covers.** Even blank-ruled, paper kept rendering legible text ("LEAD LAST
  REMARKS" on ed16's checklist). `cover_paper_rule`: a newsletter brief may not name paper,
  documents, clipboards, printouts, reports, checklists, invoices, folders… (`_PAPER_PROPS`) unless
  the chosen visual idea is ABOUT a document — and then the only paper is "a blank sheet seen
  edge-on", stated exactly. Enforced in `_rejection` for the newsletter surface only, stated to the
  author (`COVER_PROPS_DIRECTIVE`), and the cover fallback drops paper anchors.
- **Emotion carries forward.** ed16's second candidate and its retry came out as neutral as the
  first: nothing carried the judge's "enhance emotional expression" forward. A staged verdict now
  sets `emotion_weak` (no clear face emotion, an emotion that does not match the beat, a
  `scroll_stop` fail, or a judge issue naming the expression), and from then on EVERY render in
  `_gate_loop` — the next candidate and every retry, on both renderers — carries
  `EMOTION_DIRECTIVE`: "The expression must be unmistakable at thumbnail size: {beat}, exaggerated
  like a magazine cover photo." A test asserts it is in the render prompts actually sent.

**Post images follow the cover recipe (round 5, post gauntlet).** Five post renders came back
dark and moody, hookless, near-neutral — a lone man at a desk, a metal box with a pinned paper of
garbled text. The charcoal brand neutral was dragging whole scenes to near-black, which reads as
murky in a white feed. So `post_image` joins `newsletter` in `HOOK_SURFACES`:

- **The hook is required** (Stage 1's `_POST_GUIDANCE`), with the same rules — ≤5 words, names its
  subject, grounded number allowed, the fixed sans typography — laid out by `POST_HOOK_LAYOUT`:
  large in the top third of the 4:5 frame, the subject in the lower two thirds.
- **The same emotion, no-paper and blank-screen rules as covers.** One candidate per attempt
  (cost); the repair loop and the emotion carry-forward apply.
- **Bright, not moody.** Brand colors are ACCENTS — the hook's color and one wardrobe or
  background accent; charcoal belongs only behind the hook, as its backing panel
  (`hook_type_spec` now says "on a charcoal backing panel"). The people_scene and concrete_scene
  templates ask for high-key or warm daylight with strong subject contrast, and on both recipe
  surfaces `_DARK_SCENE` ("moody", "low-key", "dimly lit", "dark room", "shadowy", "noir"…) is
  rejected unless the emotional beat is itself dark (dread, crisis, fear…). The judge's
  thumbnail question adds "is it bright enough, with a clear subject, to stand out in a white
  social feed?" — no caps `thumbnail_read` at 3, and `thumbnail_read ≥ 4` is now a floor on
  `newsletter` and `post_image`.
- **People over still lifes.** Covers and posts prefer `people_scene`; a `concrete_scene` only for
  a striking, specific object juxtaposition. The idea ranker is told to penalise an object on a
  desk, and `is_desk_still_life` demotes a top-ranked desk still life for the best people-led idea
  when one exists.

**Hooks.** 2–5 words, ≤32 chars, a curiosity gap or a concrete contrast from the thesis: no `!`, no
imperative opener ("Stop…", "Start…", "Don't…", "Cut…" — `_IMPERATIVE_OPENERS`), never a
restatement of the title. Stated in the prompt and enforced in `_valid_hook`; an
`editorial_graphic` left with no hook becomes a `concrete_scene`, and a `metaphor_last_resort` with
three or more anchors is overruled to `concrete_scene`. Any failure returns `None`, and every caller
has a path without a concept.

**Treatment variety.** Every round-1 edition chose `editorial_graphic`, so the series had none.
`analyze_content_for_image(..., recent_treatments=)` tells the analyst what the author's last images
used and to prefer a different one; for `surface="newsletter"` it also biases toward
`people_scene` (LinkedIn's guidance: real faces beat clipart) and reserves `editorial_graphic` for
when a single number or contrast IS the thesis. Then `enforce_graphic_cap` makes it hard: at most
ONE graphic in any `GRAPHIC_WINDOW` (3) consecutive covers — a graphic within two of the last one
becomes a `people_scene`, noted in `treatment_rationale`.

**Every surface with a user runs all four stages**, Stage 1 ONCE per artifact, and briefs with the
user's brand clause (`brand_kit.brand_clause_for_user`):

| Surface | Stage 1 reads | Brand | Judge sees the concept | Ratio |
|---|---|---|---|---|
| Newsletter cover (`newsletter_cover.generate_cover_for_edition`) | title + subtitle + full body | yes | base + avatar | 16:9 |
| Post image (`post_image.generate_image_for_post`) | the post text | yes | base + avatar (avatar only when the post is about the author; one gpt-image retry if the avatar render is unusable); rubric + `render_path` on the receipt | `POST_IMAGE_RATIO`, default **4:5** |
| Carousel slide (`carousel_creator`, avatar slides) | the WHOLE carousel, once per deck, lazily — only if a slide wants an image | yes | avatar-eligible slides (the likeness only when the deck is about the author) | 1:1 |
| Carousel stock query (`derive_image_query`) | the same per-deck concept: its visual anchors (never its facts — a name or number is not a stock photo) ARE the Pexels query, so no per-slide `lem-simple` call. Pexels is OPT-IN since the #2241 showcase (`CAROUSEL_PEXELS_ENABLED`, default off); with no opt-in source on, no query is derived at all | — | — | — |
| Video source frame (`run_content_plan._generate_video_src`) | the post text | yes | every frame, ENFORCED (incl. the standard-tier no-avatar frame, which was ungated); a rejected frame is never animated; the judge reads the caption that will be burned on (`video_captions.burned_caption_text`) as its headline — judge-only, never composited; the likeness only when the post is about the author | tier's ratio |
| Video motion prompt (`get_runway_ml_video_prompt_from_ai`) | the same concept: thesis + emotional beat reach the motion author, plus `MOTION_DISCIPLINE` | — | — | — |
| Video clip (`utilities/video_clip_check.py`) | — | — | 3 sampled frames, one `lem-vision` call; a defect buys ONE re-render | — |
| Animated loop (`animated_loop.produce_post_loop`, flag `ANIMATED_POST_ENABLED`, OFF) | NOTHING new — it animates the post image's STORED, already-gated still and reads that still's brief receipt for the motion author; the GIF is stored beside the still as `<stem>.loop.gif` ([animated-posts.md](animated-posts.md)) | — | the still's verdict | the still's |
| Admin variants (`generate_variants`) | the source text, once per batch | yes | per variant | per combo |
| Tutorial thumbnail (`video_tutorials`) | the title (no user, so no brand) | — | yes | 16:9 |

**The video frame's headline is its caption.** `_caption_video_asset` burns the post's first 1-2
lines onto the stored MP4, so a viewer sees the frame WITH that text; judged without it, a person
with no context scored specificity 2-3 on every gauntlet frame. `video_captions.burned_caption_text`
returns exactly what the burn will use (same `caption_lines`, same flag and avatar-overlay gates;
None when nothing would be burned), and it is passed as the frame's `hook_text`. It reaches the
judge only: `video` is never in `COMPOSE_SURFACES`, so nothing is composited, the scene keeps its
ratio, and `hook_text` never enters a render prompt.

**Video (PR #2249 video gauntlet).** Both gauntlet frames failed an advisory gate and were animated
anyway, so Runway spent a render on a frame the judge disliked. The frame gate is now ENFORCED for
video — `video` joins the `IMAGE_QUALITY_GATE_SURFACES` default, and the video path passes
`enforce=True` so a deployment env that predates it cannot turn it back off — with the usual
`IMAGE_GATE_MAX_ATTEMPTS` (2) and repair round. A frame still `rejected` raises
`SourceFrameRejected` into the existing Pexels fallback (logged INFO, credits refunded); an
`unchecked` one still animates. Every motion prompt carries `ai_helper.MOTION_DISCIPLINE`: ONE
subtle continuous action expressing the beat, camera locked or one slow push-in, nothing new enters
the frame, natural limbs, screens dark and unchanged — phrased positively in the output. After
Runway returns, `video_clip_check.check_clip_url` samples frames at 1s, 2.5s and 4.5s (ffmpeg) and
asks `lem-vision`, in ONE call, whether objects or people appear or disappear, limbs or faces
distort, or text/UI appears. Any yes buys ONE re-render with `repair_clause` (naming the defect)
appended within the 512-char cap; the record (`first`, `retry`, `shipped`) rides on the MP4's
receipt as `clip_check`. Fails open on every error (WARNING with `exc=`); off with
`VIDEO_CLIP_CHECK_ENABLED=false`, read at call time.

**Treatments** (`_TREATMENT_TEMPLATES`; Stage 1 picks, an avatar always forces `people_scene`
because a person IS the subject and the LoRA cannot set type):

| Treatment | When | The image |
|---|---|---|
| `people_scene` | The piece is about people, teams, clients, a decision — the cover default | A photorealistic candid documentary photo of the audience in the situation, with real texture (pores, fabric wear, imperfections — not studio polish) |
| `editorial_graphic` | A single number or contrast IS the thesis; ≤1 per 3 covers | A designed graphic whose ONLY text is the hook, quoted exactly, bold sans, placement stated, beside one element from the anchors |
| `concrete_scene` | A specific tangible situation exists | That situation, photorealistic, with real texture; screens and documents blank or abstract |
| `metaphor_last_resort` | Nothing concrete exists | An uncommon, piece-specific metaphor — never a cliché |

**Per-surface presets** (`_STYLE_PRESETS`) now state only the USE CASE — format and where it has to
read (`newsletter`: 16:9, must read at a 400×225 thumbnail, key content in the central 60%). They
never offer a subject: a menu of objects is exactly what every cover picked from.

**`CLICHE_OBJECTS` is the ONE stock-symbol list**: pipe/plumbing/valve/faucet/drip/leak, gear/cog,
gauge, light bulb, puzzle piece/jigsaw, handshake, chess, robot/android, glowing brain/neural network
glow, circuit board, binary/matrix code, rocket, target/dartboard/bullseye, mountain summit, compass,
hourglass, domino, chain link, maze, ladder, lock and key/padlock, crystal ball, magnifying glass —
money and stock-business imagery (round 1 shipped a stack of $100 bills): stack/pile/bundle of cash,
pile of money, dollar bills, banknotes, pile/stack of coins, piggy bank, money bag, a calculator with
coins, shaking hands, thumbs up, holograms, a glowing dashboard, data streams — plus the stock office
(#1992): person at a laptop, typing hands, hands on a keyboard, coffee with a notebook — and the
data-center set every rejected post render of the #2249 gauntlet reached for: server racks, a data
center aisle, blinking server lights, a wall of monitors, a stock trading chart screen. Matched by
`cliche_hit` as whole words, EVERY word in any inflection (`pipes`, `stacks of cash`), multi-word
entries with an optional article between words, pairings (coffee + notebook, calculator + coins)
anywhere in the text; "target audience" and the other business
senses of *target* never count. ONE list, read by the author's system prompt, the brief validator
(**whole prompt, every surface, the avatar path too**), the deterministic fallback (scrubbed out of
anything it interpolates), and the vision judge's targeted question. A bare `laptop`, `desk` or
`notebook` is no longer refused — a concrete scene may genuinely contain one.

**What `_rejection` refuses**, each with the reason fed back to the next attempt: length bounds,
refusal phrasing, any cliché, a declared `hook_text` off the graphic treatment, any quoted text
other than the hook (and a graphic whose prompt does not quote its hook exactly), **any name or
number token of a proper-noun/number fact** outside the quoted hook (`fact_name_tokens`,
case-sensitive; "LinkedIn" alone is allowed, since the use case names it), **any phrase that puts
words on a surface** (`_WORDS_ON_SURFACE`: "displaying the X dashboard", "titled", "labelled", "a
sign reading", a report/magazine/screen "with text" — "a man reading a report" is an activity and
passes; screens and documents may appear, blank or abstract), and — unless the concept is weak — a
prompt depicting fewer than two visual anchors.

**The deterministic fallback is built from the piece, and is always a photograph.**
`_fallback_brief` assembles a `people_scene` or `concrete_scene` from the concept's visual anchors
("A photorealistic candid editorial photograph for a LinkedIn newsletter cover of a consultant and
a printed audit checklist…"); a graphic or metaphor treatment falls back to `concrete_scene` (or
`people_scene` with no anchors). Round 1's graphic fallback rendered "a photographic cutout of
$30K" — that branch is gone. Anything interpolated (anchors, audience, thesis, excerpt) loses its
stock symbols, quotes, names and numbers first (`_plain_words`). The brass-valve workshop
still-life is deleted. The fallback template IS a render prompt, so every noun in it is a request:
it never carries negation, dead words, the `brand_kit` clause (round 1 pasted "avoid: pipes,
gears…" straight into one) or a caller's `avoid_terms`. **A fallback says why**: every rejected
attempt's reason is kept on `ImageBrief.rejections` (on the receipt), and the fallback is logged at
INFO with all of them. Only an author that never answered usably — every attempt an exception, an
empty reply or unparsable JSON — is also a WARNING.

**`avoid_terms` is now a soft steer, never a gate.** The old object/family gate (#2000, #2241's
first fix) could only push the author from one metaphor to the next; with stock symbols refused
outright and the brief grounded in the article, variety comes from the articles themselves. A
caller's recent-image signals reach the AUTHOR only, as "make this one visibly different in setting
and composition", and never change the treatment.

**The receipt carries the stages.** `ImageBrief` adds `concept`, `treatment`, `required_entities`
(the visual anchors the prompt actually depicts, computed, not trusted from the reply),
`hook_text`, `prompt_check` and `rejections`; `write_brief_receipt` records all of them, the concept as its
fields.

**`brand_kit`** is a pre-rendered brand clause (palette, type) folded into the AUTHOR's context
only — never into the fallback. `brand_kit.brand_clause_for_user(user_id)` is the ONE resolver every
call site uses: it never raises, and no user, no kit or an unreadable read are all `""`.

**Cost.** Stage 1 (`lem-medium`) and Stage 3 (`lem-simple`) add two text calls per brief; both fail
soft and are text-only, an order of magnitude below the render they steer. **Stage 1 never runs
twice per artifact:** a caller that ran it passes the result as `concept=` — None included, which
means "ran, nothing usable" — and `build_image_brief` analyses only when left at its
`NOT_ANALYZED` default.

## `image_brief.py` — the ONE prompt author

`build_image_brief(content, surface=..., ratio=..., concept=None, brand_kit=None)` — Stage 2 above,
running Stage 1 itself when no concept is passed. Written by `lem-medium` (not the cheapest tier)
because the brief decides whether the render is relevant at all.

The system prompt follows the OpenAI gpt-image and BFL FLUX guides: state the use case, order the
prompt scene → subject → details → constraints, quote any text exactly with its placement and type,
and get realism from camera language and real texture rather than quality tags. Positive phrasing
only (FLUX has no negative prompts — naming a thing summons it), and the skin-texture and hands
rules that kill the AI sheen.

The preset and that system prompt reach the model in the SAME request, so they can contradict each
other silently: the `thumbnail` preset asked for an "illustration", the word the system prompt
calls a dead word, and nothing compared the two (#1141). `DEAD_STYLE_WORDS` / `DEAD_QUALITY_TAGS`
are ONE list the prompt names and `test_image_preset_drift.py` greps ("photorealistic" left it in
round 1, on OpenAI's gpt-image prompting guide —
https://developers.openai.com/cookbook/examples/multimodal/image-gen-models-prompting-guide — which
pairs it with real texture and no studio polish) — over every preset, every
treatment template and every fallback — and that test also fails the build on a preset **no caller
ever selects**, which is how `thumbnail` stayed wrong until #1141 wired
`video_tutorials.generate_thumbnail` up to it.

**NO text, letters, or logos in any render — no exception.** A cover's or post's headline is
typeset afterwards by `image_compose` (round 6). No name or number of the piece's facts in a
prompt either, and nothing in the scene "titled", "labelled" or "displaying" named content.
Enforced in the system prompt and `_rejection` (any quoted string is refused), with
`with_no_marks()` as the render-side belt on every surface. The blind judge transcribes what is
actually on the raw render, and any text at all caps `text_accuracy` at 2.

**A SCREEN is the one surface a blanket constraint does not hold on.** A described laptop or
monitor invites the renderer to fill its glass with plausible UI, and newsletter cover ed9 came back
with four logo tiles and the letters "AI" on one while travelling the fully-gated path (#1376). The
system prompt allows a screen only when it IS the subject, and `with_no_marks()` adds the
switched-off clause on the render side for any prompt that names one.

**A rejection is only useful if the retry hears it.** Each retry carries the reason the last
attempt was thrown out. Re-sending the identical prompt just re-drew the same rejected scene — on
the five live editions of #1992 that put four of five covers on the deterministic fallback.
`ImageBrief.fallback` records whether the template shipped, for the receipt below.

## `image_gen.py` — the ONE renderer

`render_image_from_prompt(prompt, ratio=...)` picks the backend from `IMAGE_BACKEND`:

- **`auto`** (default): gpt-image through the LiteLLM `lem-image` group first, FLUX via Replicate
  when that fails. The proxied call rides the attributed client, so PostHog + cost routing see it
  with no extra plumbing.
- **`gpt-image`** / **`flux`**: force one backend, no cross-fallback.

Ratios map to the three sizes gpt-image accepts (`1:1`, `16:9`, `9:16`); anything else falls back to
square. **4:5** (post images) is the exception: gpt-image renders it at 1024x1536 and
`conform_to_ratio` crops the file to exactly 1024x1280 — deterministic, before the judge sees
it, on the gpt-image, FLUX and avatar paths alike. The crop PROTECTS THE TOP, where the hook sits:
48px off the top and 208px off the bottom (`_CROP_TOP_PX` / `_CROP_BOTTOM_PX`; other heights split
the excess 48:208). A centred crop cut 128px off the top and clipped headlines on the #2249 gauntlet. FLUX/Replicate is asked for `aspect_ratio="4:5"`
natively, and a render already at the aspect is left alone. Replicate renders are bounded (`REPLICATE_TIMEOUT_SECONDS`, 300s, 2 attempts) so a hung
prediction can't stall a Celery worker forever. The run uses `wait=False`, so the create request
returns at once and GET polls do the waiting inside that bound — the SDK's held-open create (60.5s
read, never retried) timed out a slow avatar LoRA start before the bound applied. Cost is attributed via `track_media_cost`, with the
caller's `surface` (post_image / carousel / newsletter / video / thumbnail) threaded into
`meta.surface` so per-surface spend is queryable.

`with_no_marks()` appends the no-marks constraint per backend — a prohibition for
instruction-following gpt-image, the same thing phrased positively for FLUX — and, when the prompt
NAMES a mark-carrying surface, that surface's blank-state clause on top (`_MARK_MAGNETS`, #1376).
Those clauses are positive on BOTH backends and split by surface class, because on FLUX naming a
thing summons it: a scene with a laptop must not be handed a clause that mentions posters. Matching
runs on the **author's scene** with the blanket constraint stripped back out — `_NO_MARKS_FLUX`
itself says "screens are blank", so matching the whole string would make every FLUX render look like
it described a screen. From attempt 2 the gate's repair round is on the prompt too, and it is
excluded for the same reason (`scene=` on `with_no_marks`): `repair_directive`'s FLUX counter for a
mark verdict also says "screens blank", and its gpt-image phrasing quotes the gate's issue strings
verbatim ("garbled text on whiteboard") — so matching the retry would summon a screen into a
screenless scene on exactly the retries a mark verdict triggers.

### The vision quality gate

`render_image_gated(prompt, surface=..., concept=None, hook_text=None)` adds a `lem-vision`
check — `inspect_render_quality` — with bounded regenerates (`IMAGE_GATE_MAX_ATTEMPTS` total
renders). `render_avatar_image_gated` takes the same two kwargs and shares the loop (`_gate_loop`).

**With a Stage 1 concept the judge is staged and BLIND first (#2241).** A VLM judge handed the
prompt tends to confirm it (FineGRAIN, arXiv 2512.02161) — which is how a valve scored 5/5 against
"a valve symbolising leaks". So:

1. **Blind** — `BLIND_JUDGE_PROMPT` and the image, nothing else: "describe the main subject,
   setting and objects, then list EVERY piece of visible text verbatim, each in double quotes,
   including small or garbled text, labels, and text on devices or paper." No brief, no thesis, no
   anchors.
2. **Targeted** — the concept + that blind description + the image: is each visual anchor visibly
   depicted (facts are never asked about — a name cannot be "visibly depicted"); transcribe all text, does it equal the hook exactly; is any `CLICHE_OBJECTS` symbol
   present; legible as a 400×225 thumbnail; AI artifacts (waxy skin, malformed hands, melted
   objects); would a viewer infer the thesis. Scored 1–5 on `RUBRIC_CRITERIA`: `specificity`,
   `no_cliche`, `thumbnail_read`, `text_accuracy`, `craft`, `scroll_stop`, `brand_fit`. Round 3
   adds the headline ("together with its headline, would a viewer correctly guess what the article
   argues?"), "does a face show a clear, specific emotion readable at 400×225?" and "does a gold
   accent or the charcoal/off-white palette read?".

**Acceptable iff** `specificity ≥ 4`, `no_cliche = 5`, `craft ≥ 4`, `text_accuracy ≥ 4` (or
n/a — no text expected, none seen), `brand_fit ≥ 3` (n/a when unanswered), and on `newsletter` /
`post_image` `scroll_stop ≥ 4` — round 2's covers all sat at 3. On a `people_scene`, a face with no
clear emotion (`face_emotion: false`) caps `scroll_stop` at 3. Deterministic overlays the judge cannot talk past: a cliché the
BLIND description names (or the judge lists) caps `no_cliche` at 2; a transcription that is not the
hook caps `text_accuracy` at 3, stray text with no hook at 2; **any string the BLIND description
transcribes that is not contained in the hook caps `text_accuracy` at 2** (`stray_texts`: every
quoted string, plus unquoted text after "text/sign/label … reads/says" or "labelled/titled" —
"a man reading a report" is not text). Round 1 shipped '$30K' on a tag beside the hook and a garbled
report title at text_accuracy 5, because the targeted judge only reported the hook. **Specificity
means ≥2 anchors visibly depicted AND a stranger could infer the thesis**: fewer than two anchors
seen, or `thesis_inferable: false`, caps it at 3. A missing required score is an unusable answer and fails OPEN, like an outage.
The repair round is built from the FAILING criteria (`rubric_repair_directive`) — the entities and
thesis named back, the exact hook re-stated, FLUX told only what to show. `IMAGE_GATE_CANDIDATES`
(read at call time, default 1, max 2, staged judge only) renders that many per attempt and keeps the
better by rubric. `render_info` gains `gate_rubric`, `gate_failing`, `gate_issues` and
`gate_blind_description`, which a newsletter cover writes onto its receipt — a definite `rejected`
on `no_cliche` or `specificity` still lands `pending_review`, now with its reason.

`lem-vision` puts **gpt-4.1 first** (`order: 1` in `.litellm/config.yaml`) with gpt-4o-mini as the
in-group fallback (`order: 2`): the staged judge needs an exact transcription and an honest rubric.

Without a concept (callers not yet migrated: post images, carousel, video, thumbnail) the legacy
single call runs unchanged, graded against the brief's `focal_concept`.

- Enforced only for surfaces in `IMAGE_QUALITY_GATE_SURFACES`; others get one advisory pass (verdict
  logged, render kept).
- **Fails OPEN**: `QualityVerdict.checked=False` means the gate could not run. A vision outage must
  never take a cover or post image down with it — and for newsletter covers the human
  `pending_review` gate still sits behind this one. Unchanged by #1376, which made the gate part of
  the mark control: it detects more, it still blocks nothing.
- **A final `rejected` verdict never reaches `posts.image_url` (#2105).** Fail-open is for a gate
  that could not run, not one that looked and said no. The renderers still return the last
  candidate (covers keep their human review queue), but `post_image.generate_image_for_post` — the
  ONE place a post's render is stored — drops a `rejected` final, and the post ships with no image
  on its normal schedule. `unchecked` still renders.
- **It reads at `detail="high"`** (`_VISION_GATE_DETAIL`). The gate is asked whether the render
  carries marks, so it has to be able to READ one: at `low` the image is downsampled to ~512px on
  the long edge, where logo tiles on a laptop screen in a 1536×1024 cover do not survive — which is
  how ed9 passed this gate carrying the exact thing it was asked about (#1376).
- **A rejected render's retry never names the defect back at FLUX.** `repair_directive` carries the
  same backend split, for the same reason, as `with_no_marks`: gpt-image is told what to avoid,
  FLUX is told what the image must SHOW (an off-topic verdict names the `focal_concept` back). The
  gate reports what is WRONG — "six fingers on the left hand" — and pasting that into a FLUX prompt
  was re-requesting it, inside a two-render budget, on the path every likeness render takes (#1141).
  Keyed on the backend that **actually rendered** (`_render_with_backend`), never on `IMAGE_BACKEND`:
  under `auto` gpt-image leads and FLUX catches its failures, so the configured answer is wrong on
  exactly the runs where gpt-image is down.
- **Every gated render emits an `image_gate_verdict` event** in PostHog: `accepted` / `rejected` /
  `unchecked` (the fail-open case), the surface, the issue categories, attempt count, and whether
  the gate actually ran (`checked`). This is the image half of content-quality telemetry; it does not
  change what the gate decides.
- **Legacy path: the `newsletter` surface adds a stock-office cliché rule and a higher relevance
  floor (#1992).** (Covers now always pass a concept when Stage 1 answers; this applies when it did
  not.)
  `inspect_render_quality(..., surface="newsletter")` appends a rejection bullet naming the literal
  "person at laptop/desk/keyboard with notebook or coffee mug" scene, and overrides the verdict to
  unacceptable when `relevance < 4` even if the model itself said `acceptable=true` — a render merely
  in the same DOMAIN as the edition (a laptop for an AI-cost topic) cleared a bare relevance-3 "it
  relates" bar every time. **`post_image` gets the same raised floor (#2015)** — text-post images
  hit the identical "relates but doesn't depict" failure — but not the cliché rule, since a
  `post_image` preset legitimately wants a person as the subject. Remaining surfaces (`carousel`,
  `video`, `thumbnail`) keep the plain relevance-1/2 floor. Still fails OPEN.

Full grading of this engine's output, its per-surface gaps and what is still unmeasurable:
**`docs/content-quality-audits/image.md`**.

## Avatar likeness never renders here

`image_gen` has no avatar path on purpose. `ai_helper.generate_post_image` owns the LoRA route
behind `avatar/guardrails.resolve_avatar_for` (guardrails, C2PA, disclosure flags) and calls into
this module only for the non-avatar case; `render_avatar_image_gated` is the gated variant for that
owner. Newsletter covers add a fail-closed relevance classifier on the Auto path
(`utilities/newsletter_cover.py`, see `docs/newsletter-covers.md`).

**Policy allows it ≠ the piece wants it (#2249 gauntlet).** All three of user 1's text posts came
back with NO image: the avatar was on, every post took the FLUX-dev LoRA, and its posed smiling
portraits, mugs, cluttered screens and server racks failed the judge — rightly. So on the surfaces
that have a Stage 1 concept (post images, the video source frame, carousel slides) the likeness is
put in frame only when `guardrails.resolve_avatar_for_concept` says so: policy
(`resolve_avatar_for`) first, then `avatar_fits_concept` — a `people_scene` whose concept names
the author or speaks in the first person, or a first-person post. Everything else renders without
the likeness through gpt-image. An explicit compose-time `posts.use_avatar = true` still wins. The
carousel reads the same rule through `generate_post_image(depicts_person=...)`, so the likeness
still only renders behind `resolve_avatar_for`.

When the avatar IS used, its brief carries `avatar_directive(concept)`: the author mid-action with
the concept's emotional beat in their expression and hands, gaze on the work, screens dark or out of
focus — phrased positively, since FLUX ignores negation — and `_rejection` refuses an avatar prompt
that poses the author (`_AVATAR_POSE`: looking/smiling at the camera, eye contact, posed, headshot,
a coffee mug). And a post image never ships bare because of the LoRA alone: if the avatar render is
rejected, empty, raises (a Replicate 5xx) or came from the in-renderer base-FLUX fallback without an
`accepted` verdict, `generate_image_for_post` re-briefs WITHOUT the likeness on the SAME concept and
makes ONE `render_image_gated` attempt, logged at INFO. The receipt's `render_path` (`avatar` /
`base` / `base_after_avatar`) and `avatar_fallback_reason` say which won.

## Post images — the author's own half (issue #1030)

`utilities/post_image.py` is the ONE place a POST's image is validated, stored and removed. It adds
no prompt engine: `generate_image_for_post` is `build_image_brief` + the gated render above, and
`run_content_plan._generate_text_post_image` (the scheduled path) now goes through it too — so the
button in the Content Studio and the nightly generator render the same way.

Two origins, and they are NOT symmetric in kind but ARE in review:

| Origin | Where it comes from | Gate |
|---|---|---|
| Upload | the author's own file | `inspect_post_image_bytes` — decodable, PNG/JPEG, under 8 MB, ≥ 400×400 |
| Generate | `lem-image` via the brief | the vision gate, plus the hourly claim below |

Unlike a newsletter cover there is no second `pending_review` state: a post already sits in the
review queue until a human approves it, so the queue IS the gate.

Storage: `images/posts/<post_id>/` once the row exists (so `purge_post_assets` cleans it), and
`images/post_previews/<user_id>/` while the author is still composing — the same shape
`/generate-carousel` already uses for slides handed back before a post exists. An abandoned preview
is left on disk for the same reason an abandoned carousel preview is: pruning it would have to
outlive a post scheduled 30 days out.

`posts.image_url` holds the PUBLIC `/api/assets?file_name=` URL, because that is what the publish
step hands LinkedIn. Two consequences the helpers exist for:

- **A compose-time `image_url` is caller-supplied input on a field the publish step later fetches.**
  `/schedule_post/` accepts one ONLY when `owns_post_image_url` says it is a preview we issued to
  that caller; anything else is dropped (and warned), never stored.
- **A stored URL never resolves outside `assets_dir`.** `post_image_abs_path` re-checks containment
  through `realpath`, so a hand-edited row cannot hand a delete — or a share — an arbitrary file.

`claim_manual_generation` bounds the "Generate with AI" button at
`POST_IMAGE_GENERATE_MAX_PER_HOUR` per user, claimed BEFORE the render (the button can be held
down and every press is real spend) and failing OPEN when Redis is gone.

## Media retention, and what a dangling URL means (issue #1377)

`utilities/media_provenance.py` is the ONE place a stored media URL is walked back to what is behind
it — whether the file is still there, and which brief rendered it. Both readings exist because the
#1292 audit found `posts.image_url` / `video_url` values pointing at nothing and no way to tell
whether that was correct.

**A published post's local asset is NOT retained, and that is deliberate.** `purge_post_assets`
(#148) runs the moment `post_to_linkedin` succeeds and removes the post's MP4 and its whole
`images/posts/<post_id>/` directory: LinkedIn re-hosts the media, so the local copy is dead weight,
and the row keeps a URL that no longer resolves. Nothing clears the column, because the value is
still the record of what was published — the SPA renders a broken image for a shipped post, which is
the accepted cost of not carrying every account's media forever. That decision is what makes the
report below readable at all:

| Reading | What it means |
|---|---|
| `present` | file on the volume |
| `missing` + `expected` | `posted` row — the purge doing its job. Never alerted on |
| `missing` + NOT expected | **the defect.** A row that has not published, whose media is already gone: still going to be served to the SPA and to the publish run, as a 404 |
| `unresolvable` | not one of our `/api/assets` URLs at all (a hand-edited row) — never counted as missing |

`auto_media_integrity_scan` (weekly, Mondays 03:50 UTC) grades the newest
`MEDIA_INTEGRITY_SCAN_LIMIT` media-bearing rows and emits ONE `media_integrity` event. It is a
REPORT: it deletes no file and clears no row, because deciding whether a dangling row should be
cleared is a separate question from noticing it. An unexpected dangle also goes out through
`log_error`, so it reaches a human as a grouped `$exception`.

The walk is capped, and **the cap is never silent**: `rows` says how many rows were graded and
`truncated` says whether the cap is why it stopped. The ordering is `id DESC`, so a bound cap drops
the OLDEST rows — and `dangling = 0` out of a truncated walk is a different claim from `dangling = 0`
out of a whole corpus. A reader that ignores `truncated` will eventually read a capped scan as a
clean one.

**The brief receipt.** A generated render stores `<file stem>.brief.json` beside itself —
`focal_concept`, the render prompt, surface, preset and the vision gate's verdict — keyed by the URL
the row carries, so `read_brief_receipt(posts.image_url)` answers "what was this asked to depict?"
after the fact. That is rubric row R6, which was unscoreable for every render shipped before this.
Three rules:

- **A receipt is a record, never a default.** No brief, no receipt — an uploaded image has no brief
  behind it, and a Pexels stock clip did not come from the brief written for the render it replaced.
- **It outlives the render**, like the caption `.srt` and the video measurement receipt. A video's
  sidecar survives for free (the purge removes only the exact `.mp4`); the image one is named in
  `purge_post_assets`'s carve-out, because that branch clears the whole directory.
- **A receipt that will not parse is no receipt** — absent and broken both read as unknown.

`write_brief_receipt`'s `extra` param (#1992) merges caller-supplied fields on top of the
`ImageBrief` ones without growing a per-surface schema — a newsletter cover's receipt carries
`edition_id` / `edition_format` / `hook_style` this way, and `fallback` (from `ImageBrief.fallback`)
records whether the deterministic template shipped instead of a real brief.

## Brand kit — the per-user palette and mood

`utilities/brand_kit.py` (pure, no I/O) is the ONE place a brand kit is validated and turned into
prompt text. It is stored in `engagement_preferences.brand_kit` (JSON, NULL = no kit), edited in the
SPA's **Content & Publishing → Brand kit** card, and read with `db.get_brand_kit(user_id)`.

| Field | Shape |
|---|---|
| `primary_hex`, `secondary_hex`, `accent_hex`, `neutral_dark_hex`, `neutral_light_hex` | Optional `#rrggbb` |
| `font_vibe` | ≤80 chars, e.g. "geometric sans, heavy weight" |
| `visual_mood` | ≤160 chars |
| `avoid` | ≤12 motifs, each ≤40 chars |
| `card_share` | 0–1 (2 places), default 0.4 — the share of POST images that get a typeset card (§ Post rhythm). Steers the rotation, never a prompt |

- **Tolerant, never raising.** `parse_brand_kit` drops an invalid field rather than refusing the
  kit, at the API, in the upsert, and on read. A kit with nothing valid left is stored as NULL.
- **Names over hex.** `describe_for_prompt` names each colour off a small nearest-named-colour table
  ("light gold (#e9d437)") because image models follow names better than codes; the hex rides along.
- **Empty kit = neutral grading.** No row, a NULL column, an unreadable read, or an all-invalid kit
  all describe as `""`, and the engine renders exactly as it did before brand kits existed.
- Owner seed: `V20261006201705__seed_brand_kit_user_1.sql` fills user 1's kit only where it is still
  NULL, so a saved kit is never overwritten.

## Environment

| Var | Meaning |
|---|---|
| `IMAGE_BACKEND` | `auto` (default) / `gpt-image` / `flux` |
| `POST_IMAGE_GENERATE_MAX_PER_HOUR` | Manual post-image generations per user per hour (20) |
| `POST_IMAGE_RATIO` | Post-image ratio, read at call time: `4:5` (default), `1:1`, `16:9`, `9:16` |
| `DEFAULT_IMAGE_MODEL` | Model handed to the `lem-image` group |
| `IMAGE_QUALITY` | gpt-image quality tier |
| `IMAGE_QUALITY_GATE_SURFACES` | Surfaces where the vision gate is enforced, not advisory (`newsletter,post_image,video`; the video frame is enforced regardless) |
| `VIDEO_CLIP_CHECK_ENABLED` | Post-render clip check (default on), read at call time |
| `IMAGE_GATE_MAX_ATTEMPTS` | Total renders allowed per gated request |
| `REPLICATE_TIMEOUT_SECONDS` | Bound on a single FLUX/Replicate prediction |
