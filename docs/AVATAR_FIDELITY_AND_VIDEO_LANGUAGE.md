# Avatar Fidelity, Preview, Guardrails & Video Language

Issues: [#548](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/548)
(research + item 1) · [#744](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/744)
(items 2–4)
Date: 2026-07-25, updated 2026-08-07 · Status: **DONE** — owner signed off `1A 2A 3A 4A` (§5), all
four Phase 2 items shipped, and the supervised live avatar render passed on the owner's account
(2026-08-07, §4). #744 closed on it.

This document root-causes the four reported defects against the code as it existed on `main`,
records what the underlying models can and cannot be conditioned on, and lays out the Phase 2
implementation plan.

**Shipped in #548 / PR #597:** the research below plus **Phase 2 item 1** — the video-language
fix (§2.1). **Shipped in #744:** items 2–4 (likeness attributes, preview + approval gate,
guardrails) — see §4 for the built shape of each.

---

## 1. Current architecture (as built)

| Concern | Where it lives |
|---|---|
| LoRA fine-tune of the user's photos | `utilities/avatar/replicate_avatar.py` → `replicate/fast-flux-trainer`, `steps=1000`, private destination model |
| Training records | `avatar_trainings` (migration `V26`): `training_id`, `model_ref`, `trigger_word`, `status`, `is_active` |
| Credits | `avatar_credit_ledger` (1 credit = 1 training; refunded on failure) |
| Avatar-aware image generation | `ai_helper.generate_post_image()` → `generate_image_with_avatar()` → `get_flux_image_via_replicate()` |
| Image prompt authoring | `ai_helper.get_flux_image_prompt_from_ai()` + `_profile_visual_context()` |
| Carousel slide images | `carousel_creator.select_slide_image()` → `_should_generate_with_replicate()` → `_generate_avatar_slide_image()` |
| Video generation | `run_content_plan._generate_video_src()` → `ai_helper.get_runway_ml_video_prompt_from_ai()` → `utilities/ai/video_models.py` → `create_runway_video()` |
| Video models | `video_models.VIDEO_MODELS` — standard `gen4_turbo`/`gen4.5` (no audio), premium `veo3.1_fast` (1 credit) / `veo3.1` (3 credits), both `supports_audio=True` |
| SPA | `ui/src/pages/Avatars.tsx` (buy credits → upload ZIP → poll status → "Set Active") |
| AI disclosure | `run_content_plan._apply_ai_disclosure()` + `c2pa_helper.add_ai_content_credentials()` |

---

## 2. Root causes

### 2.1 Video voiceover is not in the user's language (Posts #34, #36) — **confirmed, fixed here**

The chain that produces a premium video:

1. `_generate_video_src()` maps `video_quality='premium'` → `(PREMIUM_VIDEO_MODEL, credits, audio=True)`
   via `_premium_tier_for_quality()`. `PREMIUM_VIDEO_MODEL` defaults to **`veo3.1_fast`**
   (`env_constants.py:61`).
2. `create_runway_video(..., audio=True)` passes `audio: True` to the API because
   `VIDEO_MODELS["veo3.1_fast"].supports_audio` is `True` (`video_models.py:39,146`).
3. The prompt is authored by `get_runway_ml_video_prompt_from_ai()`. Its audio instruction is gated
   on an **exact string match**:

   ```python
   audio_note = ("...You MAY add ONE short ambient audio cue..." if model == "veo3.1" else "")
   ```

   `"veo3.1_fast" != "veo3.1"`, so **for the production default the motion prompt contains zero
   audio direction while native audio generation is switched ON.**

**Why that yields a foreign-language voiceover.** Veo exposes **no API-level language, voice, or
dialogue parameter** — audio is entirely prompt-driven; you steer it by writing the language and the
spoken line into the prompt (`Dialogue (English): "…"`), and you get silence/ambience only if you
ask for it. With audio enabled and the prompt silent on the subject, Veo synthesises whatever
voiceover it considers plausible for the scene — including its language. The system prompt makes
this worse in two ways: it forbids negatives ("NO negatives (\"no X\")"), so nothing can suppress
speech, and its worked example puts a person making eye contact with the camera in frame, which
reads to Veo as a speaking subject.

Posts #34 and #36 are that path exactly: premium tier, `veo3.1_fast`, audio on, prompt with no
language and no audio direction at all.

**Also note:** the same bug means `veo3.1` (the `premium_top` tier) is the *only* model that ever
receives audio guidance, and the guidance it gets ("ONE short ambient audio cue") is advisory, not
binding.

**Fixed in this PR** — see §4 item 1 for the shipped shape. Both posts still need one
`regenerate_post_video_task(<post_id>)` run after the release deploys; the code change alone does not
re-render already-published media.

### 2.2 Generated images render the wrong gender — **confirmed root cause**

`generate_post_image()` builds the final Replicate prompt as:

```python
full_prompt = f"{avatar['trigger_word']}, {prompt}"   # ai_helper.py:2651
```

where `prompt` is a free-form paragraph written by `get_flux_image_prompt_from_ai()`. The only
author context that function receives is `_profile_visual_context()`, which contributes **job title,
industry, and company — and nothing else**:

```python
"The author is a {job_title} in the {industry} industry at {company_name}. ..."
```

There is **no gender, no age, no ethnicity, and no "the person in frame IS the author" instruction**
anywhere in the chain. Meanwhile the system prompt actively pushes the model to invent a person:

> **One clear focal subject** … ideally a real person … they make confident eye contact with the camera.

So the LLM freely invents a subject and a gender. The shipped worked example in the very next
function is literally `"She looks up from her laptop and smiles at the camera"`
(`ai_helper.py:2699`) — an inline demonstration of the failure mode.

That invented gender then reaches FLUX as explicit text tokens that **contradict the LoRA
identity**. At `steps=1000` with `guidance=3.5` / `prompt_strength=0.8` (`replicate_avatar.py:90`,
`ai_helper.py:2576-2587`), a fast-trainer LoRA does not reliably override an explicit contradicting
gender noun in the prompt. Result: a male user rendered female. This is a prompt-conditioning
defect, not a training-data defect — **the fix is to inject the user's real, self-declared
attributes into both the prompt-authoring step and the final Replicate prompt**, not to retrain.

Two compounding defects found while tracing this:

- **Ratio is silently dropped on the avatar path.** `generate_post_image(prompt, user_id,
  ratio="9:16")` ignores `ratio` when an avatar is active: `generate_image_with_avatar()` calls
  `get_flux_image_via_replicate(prompt, ref=model_ref)` with no `aspect_ratio`, which defaults to
  `"1:1"` (`ai_helper.py:2564`). So the **9:16 source frame for premium avatar video is generated
  square** (`_generate_video_src`, `run_content_plan.py`) and then handed to a 9:16 Veo render — cropping/letterboxing
  the user's face. Base-Flux (no avatar) renders honor the ratio correctly, so this only degrades
  avatar users.
- **The trigger word is injected into scenes with no person in them.** `_generate_avatar_slide_image()`
  (`carousel_creator.py:343`) builds `f"{query}, professional, clean minimal background, high quality,
  editorial"` from a derived search query that is frequently an object/concept, then routes it through
  `generate_post_image()`, which prepends the trigger word regardless. The LoRA is asked to insert the
  user's face into a scene never written to contain a person.

### 2.3 No preview before an avatar is used — **confirmed**

`avatar_trainings` (V26) persists no rendered image, and `Avatars.tsx` renders only the trigger word,
a status pill, "Refresh", and "Set Active". Activation is therefore **blind**: the first time a user
ever sees their avatar is on a generated post. Combined with §2.2 there is no point at which a
wrong-gender avatar can be caught before publication.

### 2.4 Guardrail gaps — **confirmed**

| Gap | Evidence |
|---|---|
| **The compose-time "use avatar" toggle is dead.** | `PostRequest.use_avatar` (`api/main.py:258`) is populated by `ComposePost.tsx:186` and **never read anywhere in the backend** (no `.use_avatar` reference exists). Avatar use is decided solely by "does the user have an active avatar". |
| **No per-content-type opt-in.** | Carousel avatar use is gated only by the **global** env flags `CAROUSEL_REPLICATE_ENABLED` / `CAROUSEL_REPLICATE_RATE` plus `CAROUSEL_AVATAR_RELEVANT_TYPES` — none of it per user, none of it user-visible. Post images and video frames have no gate at all beyond avatar existence. |
| **Avatar images carry no disclosure.** | `_apply_ai_disclosure()` is called on exactly one branch — `if ai_video:` in `_create_content_for_planned_post` (`run_content_plan.py`). `add_ai_content_credentials()` (C2PA) is likewise applied only to video files and media variants. A **synthetic likeness of a real person** in a text-post image or a carousel slide ships with neither a caption disclosure nor C2PA provenance. |
| **No approval gate, no regeneration cap beyond credits.** | `set_active_avatar()` is reachable straight from `succeeded`; the only limit on re-training is the credit balance. |
| **The "don't use avatar" fallback is failure-driven only.** | `generate_image_with_avatar()` falls back to base Flux only when inference *raises*. A successful-but-wrong render (the §2.2 case) has no fallback path — it is published. |

---

## 3. Model capability findings

| Capability | Finding | Consequence for Phase 2 |
|---|---|---|
| **Identity preservation (FLUX LoRA via `fast-flux-trainer`)** | Adequate at 1000 steps *when the prompt does not contradict it*. It is not robust to an explicit conflicting gender noun. | Keep the current trainer. Fix the prompt, not the model. |
| **Explicit attribute conditioning (FLUX)** | Prompt-level only — there is no attribute API. Attributes must be written into the prompt text as a subject clause. | Store user-declared attributes and render them into a canonical subject phrase used by both the prompt-authoring LLM and the final Replicate prompt. |
| **Aspect ratio (FLUX via Replicate)** | Supported (`aspect_ratio`), currently unused on the avatar path. | One-line fix; thread `ratio` through `generate_image_with_avatar()`. |
| **Veo 3.1 language / voice control** | **No API parameter exists.** Language, dialogue, ambience and "no speech" are all controlled by explicit prompt text. Google's own prompting guidance is to declare the language alongside the spoken line. | Language must be injected into the motion prompt for **every** `supports_audio` model, and the safest default is to explicitly request ambience with **no spoken dialogue**. |
| **Veo 3.1 silence** | Achievable, but only if explicitly requested — an audio-enabled render with no audio direction will invent one. | An "ambience only, no speech, no voiceover, no on-screen dialogue" clause is the reliable control. |
| **Runway `gen4_turbo` / `gen4.5` (standard tier)** | No audio at all (`supports_audio=False`), so the standard tier is already language-safe. | The defect is premium-tier only. |
| **Alternative avatar models** | Not required. The failure is conditioning, not capability — switching models would carry the same prompt-conflict defect. | Recommend **no model change**. |

**Language source of truth.** The codebase has no user language field. The nearest existing signal is
`geocoding._locale_for(country_code)` surfaced through `db.get_user_geo(user_id)` (used today to make
the browser locale match the proxy IP). That is a reasonable *default* but is location-derived, not a
stated preference — a US-based user whose content is in Spanish would be mis-served. *(Shipped —
§4 item 1 added `users.content_language`, defaulted from that locale and overridable in the SPA.)*

---

## 4. Phase 2 implementation — all four items as built

Ordered by risk-reduction per unit of work. Items 1–2 are the reported production defects.

**1. Video language (fixes Posts #34 / #36).** ✅ **shipped in this PR** (Decisions 1A + 2A)
- `ai_helper._audio_direction(model, language)` gates on `video_models.supports_audio(model)`
  (**not** an `== "veo3.1"` string match) and returns an **ambience-only** clause: natural ambient
  sound, *no spoken dialogue / voiceover / narration / singing*, plus the user's language stated
  explicitly for any incidental speech.
- The clause is **appended deterministically** to the LLM's motion prompt rather than requested from
  it — the system prompt forbids negatives, and the clause is made of them. The LLM is instead told
  to say nothing about audio. The motion half is trimmed so a caller's `[:512]` cap can never eat
  the clause, and `_generate_video_src()`'s text→video path now trims the scene half, not the motion
  half, for the same reason.
- `language` threads through `get_runway_ml_video_prompt_from_ai()` ← `_generate_video_src()`
  (and `generate_variants._generate_one_variant()`), resolved by
  `db.get_user_content_language(user_id)`: explicit `users.content_language` → the Login Location
  locale (`users.locale`) → `en-US`. Silent models skip the lookup entirely.
- The setting is editable in the SPA (Account → Preferences → "Content language", `""` = auto) via
  `PUT /api/user/settings`; `GET` returns both the explicit value and the effective one.
- Belt-and-braces: `create_runway_video()` **refuses `audio=True` when the prompt carries no
  `AUDIO_DIRECTION_MARKER`** and renders silent instead, so the defect cannot silently regress.
- Migration: `V20260725221220__add_user_content_language.sql` (`users.content_language VARCHAR(16) NULL`).
- Validation: after deploy, `regenerate_post_video_task(34)` / `(36)` on the owner's account and
  confirm the audio.

**2. Likeness / gender fidelity.** ✅ **shipped in #744** (Decision 3A — user self-declares, never inferred)
- Migration `V20260731144005__add_avatar_fidelity_and_guardrails.sql`: nullable
  `gender_presentation VARCHAR(32)`, `age_band VARCHAR(16)`, `attributes_confirmed_at DATETIME` on
  `avatar_trainings`. (`users.content_language` already landed with item 1.)
- `utilities/avatar/attributes.py` is the ONE place attributes become prompt text.
  `subject_clause(avatar)` renders one canonical phrase (`"a man in his 40s"`) from **stored,
  user-declared** values; `""` when unset. `"prefer-not-to-say"` is a storable choice that
  contributes no noun, and an age band alone renders the neutral `"a person in their 40s"` —
  stating an age is not a claim about gender. Nothing here reads a photo.
- Injected in two places: `subject_directive()` leads `_profile_visual_context()` so the
  prompt-authoring LLM never invents a conflicting subject, and `apply_subject_clause()` puts the
  clause beside the trigger word in `generate_post_image()`'s final Replicate prompt.
- `ratio` threads through `generate_image_with_avatar()` →
  `get_flux_image_via_replicate(aspect_ratio=…)`.
- The trigger word only reaches person-bearing scenes: `generate_post_image(depicts_person=…)`,
  driven by `carousel_creator._query_depicts_person()` — a `personal_story` slide is about the
  author by definition, anything else needs a person term in the derived query.

**3. Preview + approval gate.** ✅ **shipped in #744** (Decision 4A)
- `utilities/avatar/samples.py` renders three fixed scenes (headshot, at-desk, speaking-to-camera)
  through the LoRA using the SAME subject clause a production image uses, so what the user approves
  is what they get. Paths persist as JSON on `avatar_trainings.sample_paths` (one row per avatar).
  Rendering runs in `app/run_avatar.render_avatar_samples_task`, queued when a training first
  reaches `succeeded`; a base-Flux fallback render is discarded rather than shown as "your avatar".
- Endpoints: `GET`/`POST /avatar/training/{id}/samples` (read / capped re-roll),
  `POST …/approve`, `POST …/reject`, `PUT …/attributes`, `GET`/`PUT /avatar/preferences`.
- `set_active_avatar()` **and** the activate endpoint refuse an avatar that is not `approved`;
  approving requires rendered samples to exist. Rejecting also deactivates. New samples reset an
  earlier verdict to `pending` — the user approved the images they saw.
- `Avatars.tsx`: sample gallery, Approve / Reject / Regenerate, attribute controls, guardrail
  toggles and credit/usage visibility. (Content language stays in Account → Preferences, where
  item 1 put it.)

**4. Guardrails.** ✅ **shipped in #744** (Decision 4A — full set)
- `utilities/avatar/guardrails.resolve_avatar_for()` is the ONE gate; `None` always means "use base
  Flux / Pexels". Precedence: `users.avatar_disabled` (the explicit "don't use my avatar" switch) →
  `posts.use_avatar` (the compose-time choice, three-valued, NULL = no choice) →
  `users.avatar_use_post_image` / `_carousel` / `_video` / `_newsletter` (default **OFF**) → the
  approval gate. It **fails closed**: any error resolving the policy declines the avatar.
  (`avatar_use_newsletter` landed with the image-generation overhaul: newsletter covers resolve it
  AND a fail-closed relevance classifier on the Auto path — `newsletter_cover.py`; the review
  queue's per-edition Auto / Include me / Never choice outranks only the opt-in+classifier, never
  `avatar_disabled` or approval.)
- `PostRequest.use_avatar` — accepted and dropped before — is persisted by `schedule_post` and
  `update_post`. It stays **three-valued**: omitted means "follow my opt-ins", so `ComposePost.tsx`
  sends it only once the author has actually moved the toggle, and shows the account opt-in for
  that surface until they do. An untouched default-off toggle sending a plain `false` would be an
  explicit per-post opt-out and would silently outrank the very toggles this section adds.
- Provenance: `generate_post_image()` C2PA-signs every real avatar render and sets
  `posts.avatar_media`, which `_create_content_for_planned_post` reads so `_apply_ai_disclosure()`
  covers avatar images (carousel slides included), not just video. A base-Flux fallback render
  claims neither.
- Sample regeneration is capped by `AVATAR_SAMPLE_REGEN_MAX` (default 3) on top of the credit
  ledger — samples cost inference money but no training credit. The cap is **reserved in the
  write** (`claim_avatar_sample_render`) before a render is queued, and the automatic first render
  claims `samples_generated_at` the same way: a counter that only moves when a render finishes is
  not a cap, because two clicks (or two status polls) both pass the same reading. A render that
  ships no images hands the reservation back.
- **Operator note:** every guardrail defaults OFF, including for accounts that already have an
  active avatar. After this migration an existing avatar must be previewed and approved, and its
  content types opted in, before it is used again. That is deliberate — a never-previewed avatar is
  exactly what shipped the wrong-gender renders.

**Testing.** Item 1's coverage ships here: `_audio_direction` across every model in `VIDEO_MODELS`,
the language/marker content, prompt-trimming, the `create_runway_video` audio gate, language
resolution precedence + fail-soft paths, pipeline threading, and the settings endpoint
(`tests/unit/utilities/test_content_language.py`, `tests/unit/utilities/ai/test_ai_helper_media.py`,
`tests/unit/utilities/ai/test_video_models_premium.py`, `tests/unit/app/test_video_tier_pipeline.py`,
`tests/integration/test_content_language_settings.py`).
Items 2–4's coverage ships with #744: `subject_clause` (unset → empty, never inferred),
the guardrail precedence + fail-closed posture, sample rendering and its fallback discard, ratio
threading, trigger-word gating, the approval gate on `set_active_avatar` and the activate endpoint,
the avatar-image disclosure, and the new endpoints
(`tests/unit/utilities/avatar/test_attributes.py`, `…/test_guardrails.py`, `…/test_samples.py`,
`tests/unit/app/test_run_avatar_task.py`, `tests/unit/app/test_avatar_media_disclosure.py`,
`tests/unit/utilities/ai/test_ai_helper_avatar_image.py`,
`tests/unit/utilities/test_db_avatar_fidelity.py`,
`tests/integration/test_avatar_preview_api.py`).

**Live validation — done 2026-08-07 (owner, supervised).** Attributes were declared *before* the
first sample render fired, the previews were re-rolled, and the three images were approved: the
likeness reads as the author. That was the last acceptance box on #744, which closed on it. The
ordering matters and is the one thing to repeat when validating a new avatar — the automatic first
render is free but fires on the page's first status poll, so an avatar whose `gender_presentation` /
`age_band` are still blank renders with an **empty subject clause**, which exercises none of the
fidelity work above. Declare the attributes first, then spend a re-roll.

**When a later preview set stops looking like the user** (owner's standing call, 2026-08-07): treat
it as a **training-data problem first**, not a code bug. Declare or refresh the attributes, re-roll
the samples, and only if the subject clause is provably landing in the Replicate prompt while the
face is still wrong is it worth filing against this code path — the usual answer is a fresh LoRA
trained on a better photo set, which costs a training credit and changes nothing here.

---

## 5. Product decisions — **signed off 2026-07-25 (`1A 2A 3A 4A`)**

1. **Audio policy for audio-capable video models** → **A. Ambience only, speech explicitly banned.**
   The prompt states the user's language *and* forbids spoken dialogue/voiceover. Native audio is
   kept; the failure class is removed rather than made less likely. *(Shipped — §4 item 1.)*
2. **Source of the user's language** → **A. New `users.content_language`, defaulted from the Login
   Location locale, falling back to `en-US`.** Explicit and overridable, because location is not
   language. *(Shipped — §4 item 1.)*
3. **How avatar attributes are captured** → **A. The user self-declares** gender presentation and an
   optional age band in the Avatars SPA; stored on the avatar row and **never inferred** — no model
   ever classifies the user's face. *(Shipped — §4 item 2.)*
4. **Guardrail strictness** → **A. Full set:** approval gate before activation, per-content-type
   opt-in (default off), disclosure + C2PA on *all* avatar media, and an explicit "don't use my
   avatar" switch. *(Shipped — §4 items 3–4.)*
