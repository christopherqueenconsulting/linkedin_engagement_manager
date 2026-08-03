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
| `thumbnail` | Flat product-tutorial illustration; calm palette, one clear subject |

A preset only says what THIS surface is for. The photographic fundamentals — subject-first
ordering, 40–80 words of flowing prose, concrete camera/lighting vocabulary instead of generic
quality tags, positive phrasing only (FLUX has no negative prompts — naming a thing summons it),
and the skin-texture rules that kill the AI sheen — live in the shared system prompt, written to
hold for BOTH FLUX LoRA renders and FLUX.2/gpt-image.

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
prediction can't stall a Celery worker forever. Cost is attributed via `track_media_cost`.

### The vision quality gate

`render_image_gated(prompt, surface=...)` adds a `lem-vision` check — `inspect_render_quality`
grades the rendered file against the brief's `focal_concept` — with bounded regenerates
(`IMAGE_GATE_MAX_ATTEMPTS` total renders).

- Enforced only for surfaces in `IMAGE_QUALITY_GATE_SURFACES`; others get one advisory pass (verdict
  logged, render kept).
- **Fails OPEN**: `QualityVerdict.checked=False` means the gate could not run. A vision outage must
  never take a cover or post image down with it — and for newsletter covers the human
  `pending_review` gate still sits behind this one.

## Avatar likeness never renders here

`image_gen` has no avatar path on purpose. `ai_helper.generate_post_image` owns the LoRA route
behind `avatar/guardrails.resolve_avatar_for` (guardrails, C2PA, disclosure flags) and calls into
this module only for the non-avatar case; `render_avatar_image_gated` is the gated variant for that
owner. Newsletter covers add a fail-closed relevance classifier on the Auto path
(`utilities/newsletter_cover.py`, see `docs/newsletter-covers.md`).

## Environment

| Var | Meaning |
|---|---|
| `IMAGE_BACKEND` | `auto` (default) / `gpt-image` / `flux` |
| `DEFAULT_IMAGE_MODEL` | Model handed to the `lem-image` group |
| `IMAGE_QUALITY` | gpt-image quality tier |
| `IMAGE_QUALITY_GATE_SURFACES` | Surfaces where the vision gate is enforced, not advisory |
| `IMAGE_GATE_MAX_ATTEMPTS` | Total renders allowed per gated request |
| `REPLICATE_TIMEOUT_SECONDS` | Bound on a single FLUX/Replicate prediction |
