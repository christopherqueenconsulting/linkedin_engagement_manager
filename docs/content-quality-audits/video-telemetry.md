# Video telemetry dimensions — content_quality_scores

Issue #1281. Follow-up of #1140. Scoped to the telemetry half only.

## Why video needs its own dimensions

`content_quality_scores` already scores the caption text of every shipped post. For video posts that
caption is only half the asset: the rendered video itself — its model, duration, aspect ratio,
whether the render succeeded, and whether the stored file is actually readable — is what LinkedIn's
feed will autoplay. This audit lists the video-specific dimensions LEM should persist in the shared
telemetry module so the nightly trend line can catch regressions in the video pipeline, not just in
the writer.

Owning pipeline: `create_video_content` → `_generate_video_src` in
`src/cqc_lem/app/run_content_plan.py`. Owning model module:
`src/cqc_lem/utilities/ai/video_models.py`. Owning docs:
`docs/AVATAR_FIDELITY_AND_VIDEO_LANGUAGE.md`, `docs/image-stack.md`,
`docs/content-quality-telemetry.md`.

---

## 1. Dimensions to score

| Dimension | What it measures | Source | Null means |
|---|---|---|---|
| **render outcome** | Did LEM produce a usable video for this post? | `posts.video_url` populated AND the stored asset is reachable | no video asset (text post, or video generation failed and no Pexels fallback landed) |
| **model tier** | Which model rendered the asset | **Exact since #1410:** `posts.video_model`, written at render time with the `VIDEO_MODELS` key handed to `create_runway_video` (`gen4_turbo` / `gen4.5` / `veo3.1_fast` / `veo3.1` / `seedance2_fast`) or `pexels` for the stock fallback. Unrecorded (a post that shipped before the column) falls back to `pexels` when the stored file name proves stock (`pexels_*`), else the coarse `runway` | no asset, or a URL that is not ours |
| **duration** | Seconds of the rendered video | `ffprobe` on the stored local asset | asset missing or unreadable, or a container with no readable duration |
| **aspect ratio** | The ratio the stored video actually plays at | `ffprobe` pixel dimensions, snapped to the nearest `video_models.RATIO_ALIASES` key within 2% | asset missing or unreadable, or a stream with no dimensions |
| **asset probe result** | Is the stored file readable and non-empty? | local file exists, size > 0, and ffprobe reports at least one video stream | no local asset, or ffprobe failed / file empty |

**Model tier is exact, and the render path is what makes it so (#1410).** `_generate_video_src`
picks the model from `posts.video_quality`, silently degrades premium → standard when the user has
no credits, and falls back to Pexels stock on a render error — and `_store_video_asset` writes every
result under `videos/runwayml/` whatever produced it. So neither the quality column nor the stored
URL proves which model ran, and recording a `VIDEO_MODELS` key from either would be a guess written
into a trend line. `_generate_video_src` therefore records the key it actually used on
`posts.video_model` at render time (`pexels` for the stock fallback, NULL when the render produced
no asset at all), and the nightly beat hands that to `score_video_asset(model=...)`. It is written
there rather than at the storage step because that is the ONE place the answer exists — the model is
chosen there and the fallback is taken there — so the create path and the regenerate/heal path
record the same thing by construction.

The one thing the render path cannot see is whether its file survived storage, so
`_accept_probed_video` — the ONE place a downloaded asset is rejected (#1280) — **clears the key it
just recorded**. A rejected render never became the post's media, and on the regenerate path the row
still carries the PREVIOUS video's URL, so keeping the key would report the rejected render as the
model of the video that actually shipped. Cleared, the row reads as the coarse tier off that URL,
which is all that is genuinely known.

The column is **not backfilled**: a post that shipped before it existed still reads as the coarse
`pexels` (proved by the file name the Pexels helper writes) or `runway`. That is the same rule as
"unscored is never zero" applied to a string — the coarse tier is what was actually known, not a
guess at a key.

The ratio goes the other way — it is read from the FILE, not from the render request, so a render
that came back at a ratio it was not asked for is visible rather than hidden behind the request.

These are production/infrastructure signals, not aesthetic judgement. The rubric rows in #1140
(hook-in-first-frame, caption legibility, avatar fidelity, pacing, script quality, CTA frame) are
covered by the existing text scoring where they map to the caption/body and by the avatar guardrails
where they map to likeness. This issue extends the telemetry table so the pipeline side of #1140 has
something to trend.

---

## 2. Data model

`content_quality_scores` gets five new nullable columns:

```sql
ALTER TABLE content_quality_scores
    ADD COLUMN video_render_ok      TINYINT(1) NULL,
    ADD COLUMN video_model_tier     VARCHAR(16) NULL,
    ADD COLUMN video_duration_seconds INT NULL,
    ADD COLUMN video_aspect_ratio   VARCHAR(16) NULL,
    ADD COLUMN video_asset_probe    VARCHAR(16) NULL;
```

Why columns and not a JSON blob: the weekly rollup will report `video_render_ok` as a pass rate,
mean duration, and the share of each model tier. Numeric/string columns are easier to aggregate in
SQL and harder to drift than nested JSON keys. All columns are nullable so an unscored dimension
(text posts, failed renders, missing files) reads as "not measured" rather than false/zero.

---

## 3. Collection path

1. `get_shipped_content_for_quality` already returns one row per shipped post with `text` and
   engagement stats. Extend that row for `post_type=video` to also return `video_url`,
   `post_type` and (since #1410) `video_model`.
2. In `auto_nightly_content_quality`, when `surface == SURFACE_POST` and `post_type == PostType.VIDEO`,
   call a new pure helper `score_video_asset(video_url=...)` that:
   - resolves a local path from the `/api/assets?file_name=` URL,
   - runs `ffprobe` for duration, video-stream presence and pixel dimensions,
   - returns `{render_ok, model_tier, duration_seconds, aspect_ratio, asset_probe}`.

   The beat scores a post that shipped up to two nights ago and holds no render request, so every
   dimension it can measure comes off the STORED asset — `score_video_asset`'s optional
   `model`/`ratio` arguments exist for a caller that does hold one, and the probed values are the
   default. A dimension that depended on an argument the only production caller cannot pass would be
   NULL on every row, which is why the model is PERSISTED rather than passed: since #1410 the beat
   reads `posts.video_model` off the same row and passes it as `model=`.
3. Merge the video result into the dict returned by `score_item` under `video_*` keys, and pass it to
   `record_content_quality_score`, which writes the new columns.
4. `track_content_quality` forwards the same `video_*` keys to PostHog so the dashboard can trend
   them without re-reading the DB.

### The measurement is taken at STORE time, not at scoring time (#1517)

Step 2 above describes probing the stored asset — and the stored asset is **gone** by the time this
beat runs. `purge_post_assets` (#148) deletes a post's MP4 the moment `post_to_linkedin` succeeds,
because LinkedIn re-hosts the media; this beat scores content that has already shipped. Between #148
and #1517 that meant `NULL / NULL / missing` on every video row: the columns were real, the values
were not.

So the probe is taken where the file provably exists — at the end of both store paths
(`_record_video_asset_measures`, after captioning and C2PA signing rewrite the file, before
`posts.video_url` is persisted) — and written to a `<video>.probe.json` receipt beside the MP4
(`utilities/video_receipt.py`). The purge removes only the exact `.mp4` it resolves from
`video_url`, so the receipt survives it the same way the caption `.srt` (#1278) does, and the deck
render receipt (#1513) is the same pattern one directory over.

`score_video_asset` prefers a recorded measurement and falls back to a live probe when there is
none, which is what keeps the nightly beat and `scripts/sample_shipped_videos.py` reporting the same
numbers for the same post whichever side of the purge each runs on (the #1363 invariant). Which
measures survive publication — and which still need the pixels — is tabulated in
`docs/content-quality-audits/video.md` §8.

Two rules, both the "unscored is never zero" rule in a different coat:

- **No receipt is written unless the probe read the file.** A recorded `0 seconds / ok` cannot be
  told apart from a real measurement, so an unreadable probe records nothing, warns, and the row
  keeps reading unmeasured.
- **A receipt that will not parse is no receipt.** Absent and broken both fall back to the live
  probe rather than to a fabricated reading.

Nothing is retroactive: video posts that shipped before #1517 have no receipt and keep reading
`missing`.

### The pixels are retained the same way (#1363)

A receipt carries numbers, and rubric rows R1 (the first 2-3 seconds) and R8 (the closing frame) are
graded on what the clip LOOKS like. So the same moment that writes the receipt also writes the
representative keyframes: `retain_keyframes` (`utilities/video_frames.py`) pulls `open` (0.5s), `mid`
and `close` out of the stored MP4 into `<video>.frame-<label>.jpg` sidecars beside it — three JPEGs
per video post, which is the cost the owner accepted (decision `2A` on #1363) against the megabytes
#148 reclaims. They survive the purge for the same reason the receipt does: it removes the exact
`.mp4` named by `video_url`, and a sidecar sharing that stem is not that path.

The same rules apply, one level down. A frame is EXTRACTED, never inferred: nothing is reported
unless ffmpeg wrote a non-empty file, and a clip whose duration was never read retains the opening
frame ONLY — a midpoint invented for an unmeasured clip is a frame that does not depict what it
claims. Retaining nothing after a probe that READ the file warns (ffprobe answered, so ffmpeg
failing on the same bytes is one fault costing every video post its R1/R8 evidence), and it never
costs the receipt: the measures are written first.

`scripts/sample_shipped_videos.py` reads them back and reports frames it EXTRACTED from a clip still
on disk apart from frames RETAINED at store time — only the second kind depicts what LinkedIn
received.

---

## 4. Rollup additions (future, not in this PR)

The weekly rollup can later report:

- `video_render_ok_rate` — share of video posts with a readable asset.
- `video_duration_avg` — mean duration of rendered videos.
- `video_model_tier_counts` — distribution across the `VIDEO_MODELS` keys (`gen4_turbo`, `gen4.5`,
  `veo3.1_fast`, `veo3.1`, `seedance2_fast`) and `pexels` since **#1410** persisted the render
  model; posts older than that column still count as the coarse `runway` / `pexels`.
- `video_aspect_ratio_counts` — distribution across the project's ratio vocabulary.
- `video_asset_probe_rate` — share where `asset_probe == "ok"`.

This PR only instruments the per-piece nightly write; the rollup aggregation is left for a follow-up
so this change stays additive and safe to land under `risk:migration`.

---

## 5. Out of scope

- Aesthetic quality of the rendered video (hook frame, caption legibility, avatar likeness,
  script quality). Those are #1140's rubric rows and belong to the audit/gauntlet-loop issue, not the
  telemetry schema issue.
- Changing how videos are generated. No prompt, model, cost, or avatar-policy change here — which
  is why the exact render model is persisted by the generation path itself (**#1410**, shipped)
  rather than inferred here.
- Alert thresholds for video regressions. The columns must exist and be populated before a threshold
  can be calibrated.

---

## 6. Acceptance

- [x] Design doc for video dimensions to score (this file).
- [x] Implementation in the shared telemetry module (`content_quality.score_video_asset`).
- [x] Migration adding the five nullable video columns.
- [x] Unit tests for the new scoring logic and the nightly beat wiring.
