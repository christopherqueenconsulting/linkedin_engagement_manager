# Content-quality audit — LEM's NATIVE VIDEO POSTS

One caption rule that is easy to get backwards: an avatar-led clip (`posts.avatar_media`) is
**SIDECAR-ONLY** unless `avatar_caption_overlay` is set. Burning a caption over a rendered face is a
visible quality regression, so the default ships the `.srt` beside the file instead of over it.

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
shipped videos. **§7 is the sampler that fills the corpus half of that gap** — it produces the
scorecard from a checkout where the database and the asset volume are both reachable.

**§8 records what that sampler actually found when it was run against production on 2026-08-14**: the
corpus is unmeasurable for a reason the headless worktree could not have seen — a shipped post's
video file is deleted at publish (#1517) — and the exemplar takes #1140's fallback note.

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
  That question is asked three-valued (`post_avatar_media_state`) and answered CLOSED: an
  **unreadable** `posts.avatar_media` counts as avatar-led, because the disclosure path can afford
  to guess "not an avatar" and a burn over someone's likeness cannot.
- **Fails open.** No ffmpeg, an unusable hook, a non-zero exit: the post keeps the video it had.
  Schema is `posts.caption_text` / `posts.caption_srt_url`; nothing gates on either.
- **The sidecar outlives the video.** `purge_post_assets` drops the local MP4 the moment LinkedIn
  re-hosts it, but never the `.srt`: LinkedIn is never sent that file, so attaching captions by
  hand is a post-publish action and `posts.caption_srt_url` has to keep resolving.

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

### F7 — A shipped video's asset measures do not survive publication → **#1517** *(shipped)*

Found by running §7's sampler against production on 2026-08-14: the stored MP4 is deleted at publish
(`purge_post_assets`, #148), so neither this audit nor the nightly telemetry could measure a shipped
video's duration, aspect ratio or render outcome. Full evidence in **§8**. Fixed by recording the
measurement at STORE time in a receipt beside the file (`utilities/video_receipt.py`), which the
purge leaves behind — so the numbers survive publication even though the video does not. What still
does not survive is the PIXELS: representative frames need the MP4 itself, which is #1363's
keyframe-retention question, not this one.

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
| **#1517** — persist the video asset measures at store time | Found by the 2026-08-14 run (§8); touches the generation/storage path and possibly the schema |

Existing gates are untouched: `_post_missing_required_asset` still only checks presence; no new hold
condition is introduced. The only generation-side change is the additional system-prompt text in
`get_runway_ml_video_prompt_from_ai`.

---

## 7. Measuring the shipped corpus — `scripts/sample_shipped_videos.py` (#1363)

§1 records that the corpus half of this audit is blocked on access, not on tooling. This is the
tooling: a read-only sampler that turns "run it where the data is" into one command, so the measured
scorecard does not have to be assembled by hand or by a fresh SQL query.

```bash
poetry run python scripts/sample_shipped_videos.py                       # scorecard + frames
poetry run python scripts/sample_shipped_videos.py --users 1 --json      # raw summary
poetry run python scripts/sample_shipped_videos.py --no-frames           # probe only
```

- **It samples 6–10 published video posts** (`MIN_CORPUS` / `MAX_SAMPLES`), newest first, and counts
  a post as *gradable* only when its **body and its asset are both available** — the pairing the
  issue asks for. A post whose stored MP4 does not resolve under `assets_dir` is sampled and
  reported, never graded.
- **It reads through the existing seam.** `db.get_posted_posts`, `db.get_post_video_url` and
  `db.get_post_captions` are the readers; `content_quality.score_item` and `score_video_asset` are
  the scorer. That is deliberate: a row in this report and a row in the nightly
  `content_quality_scores` table are produced by the same functions, so the audit and the telemetry
  cannot disagree about the same post.
- **Frames come out at three points** — `open` (0.5s), `mid`, `close` — into
  `docs/content-quality-audits/assets/1363/`, which is what R1 and R8 are actually graded on. The
  opening frame is sampled at 0.5s rather than at t=0 because frame zero of a Runway render is
  routinely a near-black fade-in and says nothing about the hook. A SHIPPED post has no MP4 left to
  extract from, so its frames are the keyframes the store path retained beside it (§8) — copied in
  and reported as `retained`, never pooled with frames `extracted` here. Only a retained frame
  depicts the clip LinkedIn received; an extracted one came from a video still on disk, i.e. one
  that has not published yet.
- **Unmeasured is never zero.** A clip whose duration ffprobe could not read is excluded from the
  5–10s band denominator rather than counted as a failure, and a corpus below `MIN_CORPUS` prints
  `NOT ENOUGH` next to every rate. The same rule the nightly telemetry follows (#630): an audit that
  reports a rate over three videos has invented a calibration. Every count carries its denominator
  for the same reason — a bare `Hard slop violations: 0` over a corpus that graded nothing reads as
  "checked, and clean".
- **It writes nothing but image files** — no database write, no browser, no LLM call, and the frames
  are extracted only for the rows the scorecard actually reports, so a JPEG in the frames directory
  is never a "representative frame" of a video the corpus does not contain. A frames directory that
  cannot be created (the read-only sidecar mount §8 was run from) costs the frames, never the run.

Pinned by `tests/unit/scripts/test_sample_shipped_videos.py`.

**What the sampler still cannot do:** source the real high-engagement LinkedIn video exemplar. That
needs an authenticated, non-headless LinkedIn session, which is a human step, not a headless one —
and #1140's fallback clause (quoted in §1) is what applies until someone runs it. The gauntlet-loop
verdict trail in §4 documents the in-repo gold standard used in its place.

---

## 8. The measured run — 2026-08-14 (#1363)

The sampler was run by the owner against **production MySQL and the `lem_assets` volume**, from a
prod-image sidecar with this branch's `src` bind-mounted read-only:

```
poetry run python scripts/sample_shipped_videos.py --limit 10
```

```
Video posts sampled       : 10
Gradable (body + asset)   : 0  (NOT ENOUGH — 6+ needed for a scorecard)
Duration in 5-10s band    : 0/0 measured  (none probed)
Captioned (burned text)   : 0/0
Hook within mobile budget : 0/0
Hard slop violations      : 0 (over 0 graded)

Asset probe states:
    10  missing

Per post: 83, 85, 25, 76, 74, 70, 10, 9, 8, 7 — all `missing`, no duration, no aspect ratio.
```

**There is no scorecard, and the reason is a finding, not an access problem.** Each of those ten
post ids resolves through `get_post_video_url` to a `videos/runwayml/….mp4` name under `assets_dir`,
and none of those files exist on the volume. That is **by design**: `purge_post_assets`
(`utilities/utils.py`, shipped in #148 on 2026-06-25) deletes a post's stored MP4 the moment
`post_to_linkedin` succeeds, because LinkedIn re-hosts the media and the local copy is dead weight.
It was checked against the alternative explanation — the pre-#148 renders (post ids 2, 6 and other
early rows) still have their `.mp4` on disk, so the volume is mounted and readable.

### F7 — A shipped video's asset measures do not survive publication → **#1517**

Two things follow, and both are measurement-side:

- **The corpus acceptance box cannot be ticked with current-pipeline video.** Raising `--limit` until
  the sample reaches pre-2026-06-25 posts would produce six gradable rows, but they are Gen-3-era
  renders that predate the #1293 aspect-ratio fix (R5) and the #1278 caption burn (R2). A scorecard
  built from them would grade a pipeline that no longer exists and read as if it graded this one —
  which is the exact failure §7 says an audit must not commit.
- **The video half of `content_quality_scores` is systematically blank.**
  `auto_nightly_content_quality` scores *shipped* content, i.e. after the purge, so its
  `score_video_asset` call has recorded `NULL / NULL / missing` for every video post since #148. The
  columns #1281 added are real; the values are not. The at-generation truth exists only as the
  PostHog `video_asset_probe` event that `_probe_stored_video` emits, which nothing reads back.

The fix is to record the asset measures at **store** time — the one moment the file provably exists —
and have `score_video_asset` prefer a recorded measurement over a live probe of a deleted file.
Filed as **#1517** with the keyframe-retention alternative costed alongside it. Changing the purge
itself is explicitly out of scope: bounding the assets volume is what #148 exists to do.

Until #1517 lands, `sample_shipped_videos.py` says so in its own output rather than printing a bare
`10 missing` (`purge_hint`).

#### What #1517 changed — which measures survive publication

`_record_video_asset_measures` (`app/run_content_plan.py`) probes the stored file once more at the
END of both store paths — after the caption burn and C2PA signing, which rewrite it, and before
`posts.video_url` is persisted — and writes the reading to a `<video>.probe.json` receipt beside the
MP4 (`utilities/video_receipt.py`). `purge_post_assets` removes only the exact `.mp4` named by
`video_url`, so the receipt survives with no carve-out, exactly as the caption `.srt` does.
`score_video_asset` prefers that recording over a live probe, which is what makes this table true
for a post scored days after it shipped:

| Measure | Survives publication | Read from |
|---|---|---|
| `video_duration_seconds` | ✅ | receipt |
| `video_aspect_ratio` | ✅ | receipt (an explicit `ratio=` from a caller still wins) |
| `video_asset_probe` | ✅ | receipt |
| `video_render_ok` | ✅ | receipt (`has_video_stream`) — the file's later absence is by design, not a failed render |
| `video_model_tier` | ✅ | the stored URL, which never needed the file |
| Caption text + `.srt` sidecar | ✅ | `posts.caption_text` / `caption_srt_url` (#1278) |
| Representative frames (R1/R8) | ✅ | `<video>.frame-{open,mid,close}.jpg`, retained at store time (#1363, below) |
| Any other pixel/legibility review | ❌ | needs the whole MP4, which is gone by design |

Two rules the receipt keeps: a video whose probe did not READ the file gets no receipt at all (an
unmeasured clip must not become a recorded `0 seconds, ok` — #630), and a receipt that will not
parse is treated as absent, so the reader falls back to a live probe instead of scoring a guess.
Nothing here is retroactive: posts that shipped before this landed have no receipt and keep reading
`missing`.

#### The keyframe-retention call — owner decision `2A`

A receipt carries numbers, and the frames row above is pixels. The owner answered this issue's
decision comment `1A 2A`: **retain the keyframes**, at a cost of three JPEGs per video post against
the megabytes #148 reclaims. `_record_video_asset_measures` now writes them in the same breath as
the receipt — `retain_keyframes` (`utilities/video_frames.py`) pulls `open` (0.5s), `mid` and
`close` into `<video>.frame-<label>.jpg` sidecars beside the MP4, which survive the purge for the
same reason the receipt does, and `sample_shipped_videos.py` reads them back for any post whose
clip is already gone.

The same "unscored is never zero" rule, one level down: a frame is only reported when ffmpeg
actually wrote a non-empty file, and a clip whose duration was never read retains the OPENING frame
only — inventing a midpoint for an unmeasured clip is how an audit cites a frame that does not
depict what it claims. Retaining nothing after a probe that read the file warns, and never costs the
receipt (the measures are written first). Not retroactive either: video that shipped before this
landed has no frames and never will.

### The exemplar — fallback note taken

Decision `2A` on PR #1506: the real high-engagement LinkedIn exemplar is **not** fetched, and this
audit records that explicitly under #1140's own clause — *"if none can be sourced and fetched, fall
back to a rubric-only assessment and say so explicitly."* The reference used in its place is the
in-repo gold standard named in §4 (`comment_contract_directive()`), and the rubric verdicts in §2
stand as rubric-only judgements. Sourcing one needs an authenticated, non-headless LinkedIn session:
a human step, available any time someone wants to upgrade this section, and never an agent one.

### Where #1363 stands after this run

| Acceptance box | State |
|---|---|
| 6–10 shipped video samples with bodies **and** assets available | **Unblocked for video shipped after #1517** — the asset MEASURES now survive the purge, so a re-run grades current-pipeline posts. Still owed: a run once ~6 video posts have shipped since. The ten rows above stay ungradable, nothing recorded a receipt for them |
| Named real reference exemplar or explicit fallback note | **Done** — fallback note above, per #1140's clause (decision `2A`) |
| Representative frames embedded/referenced in the audit doc | **Unblocked** — keyframes are retained at store time (decision `2A`, above) and the sampler copies them into `docs/content-quality-audits/assets/1363/`. Still owed: the frames themselves, from the same re-run |
| Any new findings filed as follow-up issues | **Done** — F7 → **#1517** |

Both remaining boxes now turn on ONE thing — a sampler run where the production database and the
`lem_assets` volume are reachable, once enough video has shipped carrying receipts and keyframes.
Nothing further is blocked on tooling or on a decision.
