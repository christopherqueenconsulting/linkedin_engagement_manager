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
| 1. Concept | `image_concept.analyze_content_for_image` | ONE `lem-medium` JSON call over up to 12k chars of the FULL text → `ImageConcept`: thesis, audience, 3–6 `specific_entities`, emotional beat, a 2–5 word `hook_phrase`, and a `treatment` |
| 2. Brief | `image_brief.build_image_brief` | Use case + treatment template + concept + brand clause + a ≤3000-char excerpt → `{focal_concept, prompt, required_entities, hook_text}`, validated deterministically, retried up to twice with the reason |
| 3. Prompt check | `image_brief.check_prompt_against_concept` | Deterministic checks, then ONE `lem-simple` judge: "could a stranger guess the thesis?" A fail buys ONE repair pass through Stage 2 |
| 4. Blind judge | `image_gen.inspect_render_quality(..., concept=, hook_text=)` | Two `lem-vision` calls: a BLIND description, then targeted questions scored on a rubric |

**Stage 1 grounds its entities.** The prompt forbids inventing one, and `parse_concept` enforces it:
an entity that does not appear in the source (case-insensitive, loose word stems —
`entity_mentioned`) is dropped. Fewer than two survivors marks the concept `weak` — still usable for
treatment and thesis, but the entity-coverage checks downstream stand down. A hook that is not 2–5
words, runs past 32 chars, or only restates the title is dropped; an `editorial_graphic` left with
no hook becomes a `concrete_scene`, and a `metaphor_last_resort` with three or more grounded
entities is overruled to `concrete_scene` ("only when nothing concrete exists"). Any failure
returns `None`, and every caller has a path without a concept.

**Treatments** (`_TREATMENT_TEMPLATES`; Stage 1 picks, an avatar always forces `people_scene`
because a person IS the subject and the LoRA cannot set type):

| Treatment | When | The image |
|---|---|---|
| `people_scene` | The piece is about people, teams, clients, a decision | A candid documentary photo of the audience in the situation it describes |
| `editorial_graphic` | The core is a number, contrast or sharp claim | A designed graphic whose ONLY text is the hook, quoted exactly, bold sans, placement stated, beside one element from the entities |
| `concrete_scene` | A specific tangible situation exists | That situation, photographed — a screen is allowed when it IS the subject |
| `metaphor_last_resort` | Nothing concrete exists | An uncommon, piece-specific metaphor — never a cliché |

**Per-surface presets** (`_STYLE_PRESETS`) now state only the USE CASE — format and where it has to
read (`newsletter`: 16:9, must read at a 400×225 thumbnail, key content in the central 60%). They
never offer a subject: a menu of objects is exactly what every cover picked from.

**`CLICHE_OBJECTS` is the ONE stock-symbol list**: pipe/plumbing/valve/faucet/drip/leak, gear/cog,
gauge, light bulb, puzzle piece/jigsaw, handshake, chess, robot/android, glowing brain/neural network
glow, circuit board, binary/matrix code, rocket, target/dartboard/bullseye, mountain summit, compass,
hourglass, domino, chain link, maze, ladder, lock and key/padlock, crystal ball, magnifying glass —
plus the stock office (#1992): person at a laptop, typing hands, hands on a keyboard, coffee with a
notebook. Matched by `cliche_hit` as whole words in any inflection (`pipes`, `piping`, `dripping`),
multi-word entries with an optional article between words; "target audience" and the other business
senses of *target* never count. ONE list, read by the author's system prompt, the brief validator
(**whole prompt, every surface, the avatar path too**), the deterministic fallback (scrubbed out of
anything it interpolates), and the vision judge's targeted question. A bare `laptop`, `desk` or
`notebook` is no longer refused — a concrete scene may genuinely contain one.

**What `_rejection` refuses**, each with the reason fed back to the next attempt: length bounds,
refusal phrasing, any cliché, a declared `hook_text` off the graphic treatment, any quoted text
other than the hook (and a graphic whose prompt does not quote its hook exactly), and — unless the
concept is weak — a prompt depicting fewer than two of the concept's entities.

**The deterministic fallback is built from the piece.** `_fallback_brief` assembles the prompt from
the treatment and the concept's own entities ("A candid editorial photograph for a LinkedIn
newsletter cover of unpaid invoices and payroll run in the real place this happens…"); with no
concept, from the excerpt with every cliché scrubbed out. The brass-valve workshop still-life is
deleted. The fallback template IS a render prompt, so every noun in it is a request: it still never
carries negation, dead words, or a caller's `avoid_terms`.

**`avoid_terms` is now a soft steer, never a gate.** The old object/family gate (#2000, #2241's
first fix) could only push the author from one metaphor to the next; with stock symbols refused
outright and the brief grounded in the article, variety comes from the articles themselves. A
caller's recent-image signals reach the AUTHOR only, as "make this one visibly different in setting
and composition", and never change the treatment.

**The receipt carries the stages.** `ImageBrief` adds `concept`, `treatment`, `required_entities`
(the concept entities the prompt actually depicts, computed, not trusted from the reply),
`hook_text` and `prompt_check`; `write_brief_receipt` records all of them, the concept as its
fields.

**`brand_kit`** is a pre-rendered brand clause (palette, type) folded into the author's context and
the graphic fallback's palette. The brand-kit table that produces it is a separate change.

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
are ONE list the prompt names and `test_image_preset_drift.py` greps — over every preset, every
treatment template and every fallback — and that test also fails the build on a preset **no caller
ever selects**, which is how `thumbnail` stayed wrong until #1141 wired
`video_tutorials.generate_thumbnail` up to it.

**NO text, letters, or logos in any render — EXCEPT one declared 2–5 word hook on the
`editorial_graphic` treatment, verified by the judge.** Enforced in the system prompt and
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
   setting, any text (transcribed exactly), any objects." No brief, no thesis, no entities.
2. **Targeted** — the concept + that blind description + the image: is each entity visibly
   depicted; transcribe all text, does it equal the hook exactly; is any `CLICHE_OBJECTS` symbol
   present; legible as a 400×225 thumbnail; AI artifacts (waxy skin, malformed hands, melted
   objects); would a viewer infer the thesis. Scored 1–5 on `RUBRIC_CRITERIA`: `specificity`,
   `no_cliche`, `thumbnail_read`, `text_accuracy`, `craft`, `scroll_stop`.

**Acceptable iff** `specificity ≥ 4`, `no_cliche = 5`, `craft ≥ 4`, and `text_accuracy ≥ 4` (or
n/a — no text expected, none seen). Deterministic overlays the judge cannot talk past: a cliché the
BLIND description names (or the judge lists) caps `no_cliche` at 2; a transcription that is not the
hook caps `text_accuracy` at 3, stray text with no hook at 2; fewer than two entities seen caps
`specificity` at 3. A missing required score is an unusable answer and fails OPEN, like an outage.
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
| `DEFAULT_IMAGE_MODEL` | Model handed to the `lem-image` group |
| `IMAGE_QUALITY` | gpt-image quality tier |
| `IMAGE_QUALITY_GATE_SURFACES` | Surfaces where the vision gate is enforced, not advisory |
| `IMAGE_GATE_MAX_ATTEMPTS` | Total renders allowed per gated request |
| `REPLICATE_TIMEOUT_SECONDS` | Bound on a single FLUX/Replicate prediction |
