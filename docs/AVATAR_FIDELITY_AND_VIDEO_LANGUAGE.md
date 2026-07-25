# Avatar Fidelity, Preview, Guardrails & Video Language — Phase 1 Research

Issue: [#548](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/548)
Date: 2026-07-25 · Status: **research complete, awaiting owner sign-off before Phase 2**

This document root-causes the four reported defects against the code as it exists on `main`,
records what the underlying models can and cannot be conditioned on, and proposes a concrete
Phase 2 implementation plan. **No behavior changes ship in this PR** — Phase 2 is gated on the
Decision Comment attached to the PR.

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
| Video generation | `run_content_plan._generate_video_src()` → `ai_helper.get_runway_ml_video_prompt_from_ai()` → `ai/video_models.create_runway_video()` |
| Video models | `video_models.VIDEO_MODELS` — standard `gen4_turbo`/`gen4.5` (no audio), premium `veo3.1_fast` (1 credit) / `veo3.1` (3 credits), both `supports_audio=True` |
| SPA | `ui/src/pages/Avatars.tsx` (buy credits → upload ZIP → poll status → "Set Active") |
| AI disclosure | `run_content_plan._apply_ai_disclosure()` + `c2pa_helper.add_ai_content_credentials()` |

---

## 2. Root causes

### 2.1 Video voiceover is not in the user's language (Post #34) — **confirmed**

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

Post #34 is that path exactly: premium tier, `veo3.1_fast`, audio on, prompt with no language and no
audio direction at all.

**Also note:** the same bug means `veo3.1` (the `premium_top` tier) is the *only* model that ever
receives audio guidance, and the guidance it gets ("ONE short ambient audio cue") is advisory, not
binding.

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
  square** (`run_content_plan.py:427`) and then handed to a 9:16 Veo render — cropping/letterboxing
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
| **Avatar images carry no disclosure.** | `_apply_ai_disclosure()` is called on exactly one branch — `if ai_video:` (`run_content_plan.py:1559`). `add_ai_content_credentials()` (C2PA) is likewise applied only to video files and media variants. A **synthetic likeness of a real person** in a text-post image or a carousel slide ships with neither a caption disclosure nor C2PA provenance. |
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
stated preference — a US-based user whose content is in Spanish would be mis-served. Phase 2 should
add an explicit setting defaulted from that locale.

---

## 4. Proposed Phase 2 implementation plan

Ordered by risk-reduction per unit of work. Items 1–2 are the reported production defects.

**1. Video language (fixes Post #34).**
- Add `_audio_direction(model, language)` in `ai_helper.py`, gated on
  `VIDEO_MODELS[model].supports_audio` (**not** an `== "veo3.1"` string match), and thread a
  `language` argument through `get_runway_ml_video_prompt_from_ai()` ←
  `_generate_video_src()` ← `create_video_content()` / `regenerate_video_for_post()`.
- The clause states the language explicitly and either bans speech outright or constrains it to that
  language (per Decision 1).
- Belt-and-braces: `create_runway_video()` refuses `audio=True` when the resolved prompt carries no
  audio direction, so this cannot silently regress again.
- Validation: regenerate Post #34 via `regenerate_video_for_post()` on the owner's account and confirm
  the audio.

**2. Likeness / gender fidelity.**
- Migration (timestamp version): add nullable `gender_presentation VARCHAR(32)`, `age_band VARCHAR(16)`,
  `attributes_confirmed_at DATETIME` to `avatar_trainings`; add `users.content_language VARCHAR(16) NULL`.
- New `utilities/avatar/attributes.py`: `subject_clause(avatar) -> str` producing one canonical phrase
  (e.g. `"a man in his 40s"`) from **stored, user-declared** values. Empty string when unset — never
  guessed.
- Inject it in two places: into `_profile_visual_context()` so the prompt-authoring LLM never invents a
  conflicting subject, and into `generate_post_image()`'s final prompt next to the trigger word.
- Thread `ratio` through `generate_image_with_avatar()` → `get_flux_image_via_replicate(aspect_ratio=…)`.
- Only prepend the trigger word when the scene actually depicts the author (person-bearing prompts);
  `_generate_avatar_slide_image()` routes object/concept queries to base Flux or Pexels instead.

**3. Preview + approval gate.**
- On transition to `succeeded`, render N=3 sample images through the avatar LoRA (fixed prompt set:
  headshot, at-desk, speaking-to-camera) and persist their asset paths (new `avatar_samples` table or a
  JSON column — one row per avatar).
- New endpoints: `GET /avatar/training/{id}/samples`, `POST /avatar/training/{id}/approve`,
  `POST /avatar/training/{id}/reject`.
- `set_active_avatar()` refuses avatars that are not approved.
- `Avatars.tsx`: sample gallery, Approve / Reject + Regenerate, attribute & language settings,
  guardrail toggles, credit/usage visibility.

**4. Guardrails.**
- Honor `PostRequest.use_avatar` (currently dead) in the compose path.
- Per-user, per-content-type opt-in (`avatar_use_*` columns or an engagement-preference section),
  default **off** until the avatar is approved.
- Extend `_apply_ai_disclosure()` + C2PA to any post whose media used the avatar, not just video.
- Regeneration cap on top of the credit ledger; an explicit "don't use my avatar" switch that forces
  the base-Flux / Pexels path.

**Testing.** `tests/unit/` for `_audio_direction` (every model in `VIDEO_MODELS`, language threading,
unset-language default), `subject_clause` (unset → empty, never inferred), ratio threading, trigger-word
gating, and the approval gate on `set_active_avatar`. `tests/integration/` for the new avatar endpoints.
Target ≥90% patch coverage per the issue. Post #34 regeneration + one supervised avatar render on the
owner's account are the live validation cases.

---

## 5. Open product decisions (see the PR's Decision Comment)

1. **Audio policy for audio-capable video models** — ambience-only (no speech) vs. language-tagged
   spoken voiceover vs. audio off entirely.
2. **Source of the user's language** — explicit setting defaulted from Login Location locale, vs.
   locale-derived only, vs. hardcoded `en-US` for now.
3. **How avatar attributes are captured** — user self-declares (never inferred), vs. vision-model
   inference with user confirmation, vs. no attributes stored (gender-neutral prompts only).
4. **Guardrail strictness** — approval-gated with per-content-type opt-in and disclosure on all avatar
   media, vs. approval gate only, vs. status quo.
