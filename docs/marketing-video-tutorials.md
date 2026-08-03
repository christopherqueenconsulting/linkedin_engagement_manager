# Marketing video tutorials (issue #505)

Full posture for `utilities/marketing/video_tutorials.py` and the `produce-feature-tutorial` beat.
CLAUDE.md keeps the one-line invariant + this pointer. The YouTube OAuth token that publishing
depends on has its own doc: `docs/youtube-publishing.md`.

One declarative flow per feature → one finished tutorial:

```
capture (real SPA, headless Chrome) → grounded script (lem-medium) → TTS voice-over
  → ffmpeg MP4 (branded intro/outro + captions) → vertical clip → YouTube → manifest
```

## Flows are declarative

`TutorialFlow` / `TutorialStep` (`TUTORIAL_FLOWS`) declare the routes to visit and the CSS anchors
that PROVE the screen rendered. Capture runs through `get_docker_driver()` against
`TUTORIAL_SPA_BASE_URL` (falling back to `API_URL_FINAL`); a flow marked `requires_auth` needs
`TUTORIAL_DEMO_SESSION_TOKEN` and is skipped when it's unset.

`ui_fingerprint()` hashes the flow's declared markers. `next_flow()` picks the next flow to film —
a flow is re-filmed only when its captured UI fingerprint changes, or once it ages past
`TUTORIAL_REFRESH_DAYS` (`is_current`).

## Fail-closed, cheapest-first

The order is deliberate: capture runs before a single token is spent, so nothing downstream can
burn money on a screen that has moved.

- A declared UI anchor that no longer exists raises `TutorialCaptureError` rather than narrating a
  screen that moved.
- Narration is grounded in the flow definition **plus the text actually read off the captured
  screens** (`grounding_text`); `ungrounded_claims` audits for fabricated specifics and
  `check_narration` also rejects profanity and any narration over
  `TUTORIAL_MAX_NARRATION_CHARS` — all before any TTS spend (`TutorialGuardrailError`).
- An unparseable model script (`_coerce_script` returns `None`) aborts the same way.
- Render failures raise `TutorialRenderError`.

## Cost attribution

Three parts, all recorded, and the total stored on the manifest record so a tutorial's true cost is
answerable per video:

| Part | How |
|---|---|
| Script tokens | `_call_llm` → `track_llm_call` |
| TTS characters | `tts_cost_usd` → `track_media_cost` |
| Local render minutes | `TUTORIAL_RENDER_COST_PER_MINUTE` → `track_media_cost` |

## Voice, captions, publishing

- TTS provider is `TUTORIAL_TTS_PROVIDER` — OpenAI (`lem-tts`, `TUTORIAL_TTS_MODEL` /
  `TUTORIAL_TTS_VOICE`) by default, ElevenLabs (`ELEVENLABS_*`) as the alternative.
- ffmpeg produces the MP4 with branded intro/outro plus a `.srt`; `TUTORIAL_BURN_CAPTIONS` decides
  whether captions are burned in. A 9:16 clip is derived for shorts, and
  `TUTORIAL_THUMBNAIL_ENABLED` gates thumbnail generation.
- The description is UTM-tagged through `utilities/marketing/attribution.py`
  (`campaign_for_tutorial`, `PLACEMENT_VIDEO_DESCRIPTION`) — never hand-built links.
- Upload is YouTube Data API v3 at `YOUTUBE_PRIVACY_STATUS`, behind `youtube_auth.preflight()`.

**The no-self-promo guardrail from `content_alignment` is deliberately NOT applied here** — this is
LEM's own product marketing, the one content type where naming the product IS the point.

## State + surfacing

State lives in `assets/videos/tutorials/manifest.json` (`load_manifest` / `save_manifest`); the SPA
embeds it via `TutorialVideos.tsx`. Weekly cadence.

OFF unless the `TUTORIAL_VIDEOS` flag is enabled (`TUTORIAL_VIDEOS_ENABLED` env fallback — flags
fail open to env, see `docs/feature-flags.md`).
