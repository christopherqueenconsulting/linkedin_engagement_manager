# The image stack — ONE engine, two modules

Full posture for LEM's AI still-image generation. CLAUDE.md keeps the one-line invariant + this
pointer.

Every surface that renders an AI image goes through the same two modules. **Never add a
per-content-type prompt helper** — add a preset.

| Module | Owns |
|---|---|
| `utilities/ai/image_brief.py` | Authoring the prompt: content in → validated brief out |
| `utilities/ai/image_gen.py` | Rendering that brief, plus the vision quality gate |

## `image_brief.py` — the ONE prompt author

`build_image_brief(content, surface=..., ratio=...)` sends the ACTUAL content to `lem-medium` and
gets back a structured `ImageBrief`: a render-ready **prompt** plus the extracted **`focal_concept`**
the vision gate later grades the render against. `lem-medium` (not the cheapest tier) because the
brief decides whether the render is relevant at all — the failure the cheap tier kept producing.

**Per-surface presets** (`_STYLE_PRESETS`), keyed by surface, defaulting to `post_image`:

| Preset | What that surface's image is for |
|---|---|
| `newsletter` | Wide editorial cover; one bold scene from the edition's core idea, readable at thumbnail size |
| `post_image` | Scroll-stopping single subject for a feed post; one strong color accent |
| `carousel` | Quiet supporting photo for ONE slide; uncluttered background a text panel can sit beside |
| `video` | Opening frame posed so subtle motion can bring it alive; layered depth |
| `thumbnail` | Bold, high-contrast product-tutorial thumbnail; one tangible object, framed to read at player-tile size |

A preset only says what THIS surface is for. The photographic fundamentals — subject-first
ordering, 40–80 words of flowing prose, concrete camera/lighting vocabulary instead of generic
quality tags, positive phrasing only (FLUX has no negative prompts — naming a thing summons it),
and the skin-texture rules that kill the AI sheen — live in the shared system prompt, written to
hold for BOTH FLUX LoRA renders and FLUX.2/gpt-image.

The preset and that system prompt reach the model in the SAME request, so they can contradict each
other silently: the `thumbnail` preset asked for an "illustration", the word the system prompt
calls a dead word, and nothing compared the two (#1141). `DEAD_STYLE_WORDS` / `DEAD_QUALITY_TAGS`
are now ONE list the prompt names and `test_image_preset_drift.py` greps — and that test also fails
the build on a preset **no caller ever selects**, which is how `thumbnail` stayed wrong: nothing
passed `surface="thumbnail"` at all until #1141 wired `video_tutorials.generate_thumbnail` up to it.

**NO text, letters, or logos in any render prompt** — enforced in the system prompt, with
`with_no_marks()` as the render-side belt.

An unparseable or invalid model reply falls back to `_fallback_brief` — deterministic, so a bad
generation never means no image.

## `image_gen.py` — the ONE renderer

`render_image_from_prompt(prompt, ratio=...)` picks the backend from `IMAGE_BACKEND`:

- **`auto`** (default): gpt-image through the LiteLLM `lem-image` group first, FLUX via Replicate
  when that fails. The proxied call rides the attributed client, so PostHog + cost routing see it
  with no extra plumbing.
- **`gpt-image`** / **`flux`**: force one backend, no cross-fallback.

Ratios map to the three sizes gpt-image accepts (`1:1`, `16:9`, `9:16`); anything else falls back to
square. Replicate renders are bounded (`REPLICATE_TIMEOUT_SECONDS`, 300s, 2 attempts) so a hung
prediction can't stall a Celery worker forever. Cost is attributed via `track_media_cost`, with the
caller's `surface` (post_image / carousel / newsletter / video / thumbnail) threaded into
`meta.surface` so per-surface spend is queryable.

### The vision quality gate

`render_image_gated(prompt, surface=...)` adds a `lem-vision` check — `inspect_render_quality`
grades the rendered file against the brief's `focal_concept` — with bounded regenerates
(`IMAGE_GATE_MAX_ATTEMPTS` total renders).

- Enforced only for surfaces in `IMAGE_QUALITY_GATE_SURFACES`; others get one advisory pass (verdict
  logged, render kept).
- **Fails OPEN**: `QualityVerdict.checked=False` means the gate could not run. A vision outage must
  never take a cover or post image down with it — and for newsletter covers the human
  `pending_review` gate still sits behind this one.
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

`auto_media_integrity_scan` (weekly, Mondays 03:10 UTC) grades the newest
`MEDIA_INTEGRITY_SCAN_LIMIT` media-bearing rows and emits ONE `media_integrity` event. It is a
REPORT: it deletes no file and clears no row, because deciding whether a dangling row should be
cleared is a separate question from noticing it. An unexpected dangle also goes out through
`log_error`, so it reaches a human as a grouped `$exception`.

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
