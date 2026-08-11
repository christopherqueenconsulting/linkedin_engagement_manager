# Content-quality audit — LEM's NATIVE VIDEO POSTS

Issue #1282. Re-audited 2026-08-10 against `main` @ `fccbd6fe`.

The deterministic gates LEM already runs answer "did the pipeline deliver *a* video asset?". This
re-audit asks the next question — *is the generated video still the right shape to stop a muted,
scrolling LinkedIn feed?* — and grades the machinery that produces it.

Owning pipeline: `create_video_content` → `_generate_video_src` in
`src/cqc_lem/app/run_content_plan.py`. Owning docs:
`docs/AVATAR_FIDELITY_AND_VIDEO_LANGUAGE.md`, `docs/content-core.md`, `docs/image-stack.md`.

**Headline:** the re-audit found a regression before it could grade any new corpus. The
writer-side motion-prompt contract shipped in #1140 (`motion_prompt_directive`) and its regression
guard (`test_motion_prompt_contract.py`) were present at `0ff70d6f` but missing from current `HEAD`
— the #1293 image-audit merge branch carried an older copy of `content_framework.py` and
`ai_helper.py` that did not include the video-audit changes, so they were silently reverted. This
PR restores the contract and its test, updates the audit doc, and leaves the live corpus /
exemplar sampling for the follow-up issue it still requires.

---

## 1. What could and could not be sampled

The issue asked for 6–10 recently-shipped native videos via existing `db.py` readers and a real
high-engagement LinkedIn video exemplar. Both remain bounded by the headless worktree rules:

| Asked for | What was actually available | Why |
|---|---|---|
| 6–10 shipped video post bodies / assets via `db.py` | **0 bodies / 0 render frames.** The existing readers `get_posted_posts`, `get_post_video_url`, and `get_shipped_content_for_quality` are the correct paths, but production MySQL credentials are not available in the agent worktree, and the local/test database has no shipped video rows | The audit runs headless and the runbook forbids touching `.env` / prod secrets. No read path returns the video asset surface without those credentials |
| A real, fetched LinkedIn video exemplar | **Not fetched.** Rubric-only assessment, plus an in-repo gold standard for the gauntlet loop (§4) | Fetching one means an authenticated Selenium session against live LinkedIn — a runbook escalation trigger, not a headless step. The original #1140 fallback clause covers this: *"if none can be sourced and fetched, fall back to a rubric-only assessment and say so explicitly"* |
| Extract representative frames via `ffmpeg` | **Not run against shipped assets.** No stored local video files were accessible in the worktree | `ffmpeg` is a project dependency, but there was no input media to probe |

The missing corpus / exemplar work is filed as a separate `risk:live-linkedin` follow-up (§6).
Until that lands, this audit reports on the **machinery** that produces the video, not on a corpus of
shipped videos.

---

## 2. The rubric

Grounded in this repo's own invariants, not generic video taste. Each row names the ONE place that
owns it, and the verdict is against what the pipeline does after this PR.

| # | Rubric row | Owned by | Verdict |
|---|---|---|---|
| R1 | **Opening hook is visible in the first 2–3 seconds** — the first frame/motion must carry the post's central image or claim before the scroll | `get_flux_image_prompt_from_ai` (the frame), `get_runway_ml_video_prompt_from_ai` (the motion) | **PARTIAL → restored here.** The image prompt is authored from the post, and the motion prompt again carries the "first 1–2 seconds" rule via the restored contract |
| R2 | **Captions / on-screen text for mute autoplay** — LinkedIn autoplays muted, so the visual must communicate without audio | `utilities/video_captions.py`, called from `_caption_video_asset` | **PASS (flagged) since #1278.** The post's opening line is burned into the stored MP4 with ffmpeg and the `.srt` sidecar is kept beside it. Behind `video-captions-enabled`; an avatar-led video needs `users.avatar_caption_overlay` too |
| R3 | **Avatar fidelity** — when an avatar is active, the first frame must recognizably be the user | `resolve_avatar_for(AVATAR_SURFACE_VIDEO)`, `generate_post_image(..., surface=AVATAR_SURFACE_VIDEO)` | **PASS by construction.** The avatar path is the same LoRA-backed path used for post images; the video surface is explicitly gated. No deterministic likeness probe on the rendered frame yet — **#1279** |
| R4 | **Pacing / length matches what LinkedIn rewards** — short, 5–10s clips dominate feed engagement; the model default durations are 5s (gen4/seedance) and 6s (Veo) | `video_models.resolve_duration` | **PASS.** Default durations are already inside the 5–10s band |
| R5 | **Aspect ratio / framing for LinkedIn's feed player** — premium renders 9:16 and standard renders 1:1, both feed-native | `_generate_video_src` | **PASS.** The #1293 fix already briefs and renders one `source_frame_ratio` per tier, so premium no longer frames a square composition for a vertical crop |
| R6 | **Motion-prompt quality — concrete camera/subject motion, no cinematic keyword stuffing** | `get_runway_ml_video_prompt_from_ai` | **PARTIAL → restored here.** The system prompt already told the model to avoid keyword-stuffed cinematic prompts, but the explicit Gen-3-era failure-pattern ban was dropped by #1293; it is back |
| R7 | **Audio policy for premium models** — no hallucinated voiceover, ambience only, in the user's language | `_audio_direction()` in `ai_helper.py`, enforced in `video_models.create_runway_video` | **PASS.** `AUDIO_DIRECTION_MARKER` is mandatory, and `create_runway_video` silently disables audio if the marker is missing |
| R8 | **CTA / closing frame** — the clip should end on a visually complete beat, not a fade to ambiguity | `_generate_video_src` | **FAIL → not fixed here.** No deterministic steering of a closing frame beyond the restored contract's "resolved visual beat" rule. Prompt-level follow-up — carried on **#1277** and **#1281** |

---

## 3. Findings

### F1 — The motion-prompt contract was regressed by #1293 *(restored in this PR)*

`motion_prompt_directive()` shipped in #1140 and was unit-tested to prevent exactly the drift that
happened: the image-audit merge (#1293, commits `a6c93765`/`5af67bd8`) rewrote `content_framework.py`
and `ai_helper.py` from a branch that did not contain the video-audit changes. The merged tree kept
the image fixes but lost:

- `content_framework.motion_prompt_directive()` entirely.
- The `{motion_prompt_directive()}` injection in `get_runway_ml_video_prompt_from_ai`.
- `tests/unit/utilities/ai/test_motion_prompt_contract.py`.

This is a **regression of a writer-side contract**, not a stylistic difference: without it, the
motion prompt system prompt allows "cinematic", "dynamic energy", "b-roll", multi-shot montage
language, slow reveals that resolve after the muted-autoplay window, and self-authored audio
instructions on audio-capable models — exactly the patterns that make a 5–6 second clip look like
a stock asset.

**Restored:**

- `motion_prompt_directive()` returns the same six-rule contract, lives in `content_framework.py`
  next to `post_writing_directive()` and `comment_contract_directive()` so the video pipeline
  cannot drift from the shared core.
- `get_runway_ml_video_prompt_from_ai()` appends it to its system prompt automatically.
- `tests/unit/utilities/ai/test_motion_prompt_contract.py` is reinstated as the regression guard.

### F2 — The motion-prompt contract had no deterministic checker → **#1277** *(shipped)*

The contract in F1 was writer-side only. `slop_lint.motion_prompt_report()` now greps a FINISHED
motion prompt for the banned patterns — montage/edit language, mood and film-stock adjectives,
writer-authored audio, and a missing opening-window signal — reading the same
`content_framework.MOTION_BANNED_*` lists `motion_prompt_directive()` hands the writer, so the two
sides cannot drift.

Because it changes the credit-spend profile, enforcement is a runtime flag and starts OFF:
`video-motion-lint-hold` off means a hard violation is reported (`motion_prompt_check` event) and the
prompt ships unchanged; on, it buys one steered rewrite and then holds the render, which
`_generate_video_src` already handles as a generation failure (refund, fall back to Pexels).
Posture: `docs/content-core.md` § Motion-prompt lint.

### F3 — No caption / burned-text path for muted autoplay → **#1278** *(shipped)*

LinkedIn's feed player starts muted. The post's caption is below the fold on mobile; the video
itself has to communicate visually in the first 2–3 seconds. Veo/Gen-4 are not text-reliable, so
the fix is a post-generation step rather than a prompt: **#1278 (decision 1A)** burns the post's
own first 1–2 lines into the rendered MP4 with `ffmpeg`'s `subtitles` filter and writes the `.srt`
sidecar next to it.

The sidecar-only alternative (LinkedIn's REST Videos API `initializeUpload` with
`uploadCaptions: true`) was rejected for now: LEM publishes through `/ugcPosts` +
`assets?action=registerUpload`, so it would mean migrating the upload path, and a native caption
track is invisible on the muted autoplay this finding is about.

What it costs and what it is bounded by:

- **No LLM spend.** The caption is the post's own hook, wrapped deterministically —
  `content_framework.py` already owns the words, so nothing is re-authored.
- **One extra local ffmpeg pass per video post**, attributed through `track_media_cost` at
  `VIDEO_CAPTION_RENDER_COST_PER_MINUTE` (same accrual as the tutorial renderer).
- **OFF by default** behind `video-captions-enabled`, and an avatar-led video is skipped unless the
  user turns on `users.avatar_caption_overlay` — the sidecar still ships, the frame is untouched.
- **Fails open.** No ffmpeg, an unusable hook, a non-zero exit: the post keeps the video it had.
  Schema is `posts.caption_text` / `posts.caption_srt_url`; nothing gates on either.

### F4 — No deterministic check that the stored video asset is inspectable → **#1280**

`_store_video_asset` downloads the Runway URL and C2PA-signs it, but it does not probe the file
before returning. A zero-byte download, a wrong content-type, or a file that `ffmpeg` cannot parse
would still become `posts.video_url`, and `_post_missing_required_asset` only checks truthiness of
the URL string. Filed as **#1280**.

### F5 — The video pipeline is not represented in `content_quality_scores` → **#1281**

`score_item()` scores text surfaces only. Video posts contribute their caption text, but the video
asset itself (motion-prompt length, frame presence, aspect ratio, render outcome) is never recorded
in the nightly telemetry. Filed as **#1281** — the dimensions, data model and collection path it
adds are specified in `docs/content-quality-audits/video-telemetry.md`.

### F6 — Live corpus + exemplar sampling remains blocked on headless access → **#1363**

The original reason #1282 was opened — real shipped video bodies/assets and a real LinkedIn
exemplar — still cannot be satisfied headlessly. A separate follow-up issue (**#1363**) carries
that scope so this PR can land the regression fix and updated audit doc.

---

## 4. Gauntlet-loop verdict trail

Run per `.claude/skills/gauntlet-loop/SKILL.md`. Two pieces, one builder and one **fresh-context**
critic each, blind A/B (labels stripped, order shuffled), capped at 3 rounds.

**Reference exemplar — named and in-repo:** `content_framework.comment_contract_directive()`, the
#617 COMMENT QUALITY CONTRACT. It is this repo's gold standard for a writer-side contract (numbered,
each rule falsifiable, a banned list shared with the checking side) and it solves *the same problem
on the sibling surface* — templated sameness on a LinkedIn surface. Chosen because no real LinkedIn
video exemplar could be fetched headless (§1), and the skill's own rule is that an in-repo gold
standard beats a hypothetical.

**Stated limitation:** the comparison was label-blind, not indistinguishable — a critic reading a
comment contract next to a motion-prompt contract can tell which is which. The verdicts below are
therefore comparative judgements against the project's invariants, not a true double-blind.

| Round | Piece | Builder proposal | Critic verdict (fresh context) | Resolution |
|---|---|---|---|---|
| 1 | **Restore the writer-side motion-prompt contract** | Re-add `motion_prompt_directive()` to `content_framework.py` and inject it into `get_runway_ml_video_prompt_from_ai`; re-add the regression test | **Build wins.** *"This is exactly the shape the comment contract takes: a named, falsifiable contract in the shared core with a regression guard that fails the build if it drifts. The #1293 merge proved the guard was necessary."* | Shipped as drafted |
| 1 | **Add a deterministic motion-prompt linter now** | A checker in `content_framework` that greps a finished motion prompt for banned patterns | **Exemplar wins.** *"A linter is the right long-term shape, but it changes the credit-spend and regeneration path. #1140 explicitly routed that to a separate `risk:*` issue (#1277); doing it here would exceed the low-risk scope of a regression-fix PR."* | Not shipped here. Already tracked as **#1277** |

Neither piece hit the 3-round cap; nothing is parked `needs-human`.

---

## 5. Before / after

The pipeline could not be run end to end here (no LLM credentials in the agent worktree), so the
"before" is the **regressed** system prompt that current `main` was serving after #1293 — a prompt
that still had the old rules but no contract — and the "after" is the restored contract. Both are
5–10 second prompts in the style `get_runway_ml_video_prompt_from_ai` produces.

**Before** — the regressed prompt after #1293 (old rules, no opening-window rule, no shot-count
rule, no explicit mood-word ban):

> A cinematic, dynamic video with smooth camera movement and subtle background blur. The scene opens
> with a soft establishing shot, then cuts to a medium close-up of the founder looking confident. B-roll
> details of the product surface in the background. The lighting is warm and professional. The mood is
> energetic yet trustworthy. Fade out at the end.

**After** — the restored contract:

> Slow push-in toward the founder, already centered at her standing desk. She looks up from the laptop
> in the first second and holds eye contact with the camera. Papers shift gently in the background. The
> shot ends with her small, closed-lip smile.

| Measure | Before | After |
|---|---|---|
| Opens in first 2 seconds | ❌ subject appears after a "cut" | ✅ subject + motion visible immediately |
| One continuous motion | ❌ "cuts to", "B-roll details" | ✅ single push-in + subject move |
| Concrete physical terms | ❌ "cinematic", "dynamic", "warm and professional" | ✅ push-in, look up, papers shift |
| Audio left to deterministic clause | ❌ "energetic" implies audio styling | ✅ no audio mention |
| Resolved closing beat | ❌ "fade out" | ✅ small closed-lip smile as endpoint |
| Estimated Gen-4 compatibility | LOW — keyword-stuffed, multi-shot | HIGH — single continuous motion |

The point of the pairing is not that the second prompt is prettier. It is that **the first prompt
is what the regressed system prompt allowed, it would pass every existing deterministic gate LEM
has, and only the restored contract can see anything wrong with it**.

---

## 6. What shipped in this PR

- `content_framework.motion_prompt_directive()` — restored to the shared content core.
- `get_runway_ml_video_prompt_from_ai()` — again appends the directive to its system prompt.
- `tests/unit/utilities/ai/test_motion_prompt_contract.py` — restored regression guard.
- `tests/unit/utilities/ai/test_ai_helper_media.py` — adjusted the Gen-4 audio assertion so it
  checks for the *instruction line* rather than the substring `audio`, which now legitimately appears
  in rule 5 of the contract.
- `docs/content-quality-audits/video.md` — this re-audit, including the regression finding, the
  sampling limits, the rubric, the gauntlet-loop verdict trail, the before/after, and the follow-up
  links.

**What remains on a separate `risk:*` / `risk:live-linkedin` follow-up:**

| Follow-up | Why it is separate |
|---|---|
| **#1277** — motion-prompt deterministic linter | Credit-spend / regeneration behavior change |
| **#1279** — avatar-likeness frame check | Avatar-policy work, telemetry-only default |
| **#1280** — stored video asset probe | Touches the asset-backfill/storage path |
| **#1281** — video-specific telemetry (`video-telemetry.md`) | Telemetry/schema change |
| **#1363** — live corpus + exemplar sampling | Requires production MySQL credentials and/or live LinkedIn/Selenium access |

Existing gates are untouched: `_post_missing_required_asset` still only checks presence; no new hold
condition is introduced. The only generation-side change is the additional system-prompt text in
`get_runway_ml_video_prompt_from_ai`.
