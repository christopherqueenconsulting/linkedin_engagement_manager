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
(#1992): person at a laptop, typing hands, hands on a keyboard, coffee with a notebook. Matched by
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
only — never into the fallback. The brand-kit table that produces it is a separate change.

**Cost.** Stage 1 (`lem-medium`) and Stage 3 (`lem-simple`) add two text calls to every brief —
including each carousel slide and post image, which call `build_image_brief` without a concept. Both
fail soft and are text-only, an order of magnitude below the render they steer.

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

**NO text, letters, or logos in any render — EXCEPT one declared 2–5 word hook on the
`editorial_graphic` treatment, verified by the judge.** No name or number of the piece's facts in a
prompt either, and nothing in the scene "titled", "labelled" or "displaying" named content. Enforced in the system prompt and
`_rejection`, with `with_no_marks()` as the render-side belt: a brief carrying `hook_text` gets
"The only text in the image is exactly "<hook>" — no other words, letters, numbers, logos,
watermarks or UI" INSTEAD of the blanket ban (positively phrased for FLUX), and the printed-surface
blank-state clause is skipped since the hook is set on one. The blind judge then transcribes what
is actually there and the rubric's `text_accuracy` compares it to the hook.

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
square. Replicate renders are bounded (`REPLICATE_TIMEOUT_SECONDS`, 300s, 2 attempts) so a hung
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

## Environment

| Var | Meaning |
|---|---|
| `IMAGE_BACKEND` | `auto` (default) / `gpt-image` / `flux` |
| `POST_IMAGE_GENERATE_MAX_PER_HOUR` | Manual post-image generations per user per hour (20) |
| `DEFAULT_IMAGE_MODEL` | Model handed to the `lem-image` group |
| `IMAGE_QUALITY` | gpt-image quality tier |
| `IMAGE_QUALITY_GATE_SURFACES` | Surfaces where the vision gate is enforced, not advisory |
| `IMAGE_GATE_MAX_ATTEMPTS` | Total renders allowed per gated request |
| `REPLICATE_TIMEOUT_SECONDS` | Bound on a single FLUX/Replicate prediction |
