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
| **model tier** | Which pipeline rendered the asset | An explicit `model` when the caller holds one; otherwise `pexels` when the stored file name proves stock (`pexels_*`), else the coarse `runway` | no asset, or a URL that is not ours |
| **duration** | Seconds of the rendered video | `ffprobe` on the stored local asset | asset missing or unreadable, or a container with no readable duration |
| **aspect ratio** | The ratio the stored video actually plays at | `ffprobe` pixel dimensions, snapped to the nearest `video_models.RATIO_ALIASES` key within 2% | asset missing or unreadable, or a stream with no dimensions |
| **asset probe result** | Is the stored file readable and non-empty? | local file exists, size > 0, and ffprobe reports at least one video stream | no local asset, or ffprobe failed / file empty |

**Model tier is coarse, and deliberately so.** `_generate_video_src` picks the model from
`posts.video_quality`, silently degrades premium → standard when the user has no credits, and falls
back to Pexels stock on a render error — and `_store_video_asset` writes every result under
`videos/runwayml/` whatever produced it. So neither the quality column nor the stored URL proves
which model ran, and recording a `VIDEO_MODELS` key from either would be a guess written into a
trend line. The exact key needs the render path to persist what it used: **#1410**. Until then only
`pexels` (proved by the file name the Pexels helper writes) and `runway` are claimed. This is the
same rule as "unscored is never zero" applied to a string.

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
   engagement stats. Extend that row for `post_type=video` to also return `video_url` and
   `post_type`.
2. In `auto_nightly_content_quality`, when `surface == SURFACE_POST` and `post_type == PostType.VIDEO`,
   call a new pure helper `score_video_asset(video_url=...)` that:
   - resolves a local path from the `/api/assets?file_name=` URL,
   - runs `ffprobe` for duration, video-stream presence and pixel dimensions,
   - returns `{render_ok, model_tier, duration_seconds, aspect_ratio, asset_probe}`.

   The beat scores a post that shipped up to two nights ago and holds no render request, so every
   dimension has to come off the STORED asset — `score_video_asset`'s optional `model`/`ratio`
   arguments exist for a caller that does hold one, and the probed values are the default. A
   dimension that depended on an argument the only production caller cannot pass would be NULL on
   every row.
3. Merge the video result into the dict returned by `score_item` under `video_*` keys, and pass it to
   `record_content_quality_score`, which writes the new columns.
4. `track_content_quality` forwards the same `video_*` keys to PostHog so the dashboard can trend
   them without re-reading the DB.

---

## 4. Rollup additions (future, not in this PR)

The weekly rollup can later report:

- `video_render_ok_rate` — share of video posts with a readable asset.
- `video_duration_avg` — mean duration of rendered videos.
- `video_model_tier_counts` — distribution across `runway` and `pexels` today, across the
  `VIDEO_MODELS` keys (`gen4_turbo`, `gen4.5`, `veo3.1_fast`, `veo3.1`, `seedance2_fast`) once
  **#1410** persists the render model.
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
  is why the exact render model has to be persisted by the generation path itself (**#1410**)
  rather than inferred here.
- Alert thresholds for video regressions. The columns must exist and be populated before a threshold
  can be calibrated.

---

## 6. Acceptance

- [x] Design doc for video dimensions to score (this file).
- [x] Implementation in the shared telemetry module (`content_quality.score_video_asset`).
- [x] Migration adding the five nullable video columns.
- [x] Unit tests for the new scoring logic and the nightly beat wiring.
