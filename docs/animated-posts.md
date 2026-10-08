# Animated loop posts (GIF cinemagraphs)

An OPT-IN animated variant of a generated text post's image: the post's own gated still is animated
into a subtle, seamlessly looping GIF and published in place of the still. **OFF by default**
(`ANIMATED_POST_ENABLED`, PostHog flag `animated-post-enabled`). With it off nothing runs and no post
changes.

| Piece | Where |
|---|---|
| The ONE place a loop is made | `src/cqc_lem/utilities/animated_loop.py` |
| Where the loop is stored | `post_image.store_post_loop` / `post_loop_abs_path` — beside the still |
| Producer hook | `run_content_plan._generate_text_post_image` → `produce_post_loop` |
| Publisher | `engagement/posting._animated_loop_for` → `poster.share_animated_image_on_linkedin` |
| Flag | `flags.ANIMATED_POST` (`docs/feature-flags.md`) |

## Why it is an experiment, not a default

Some LinkedIn creators post GIFs, cinemagraphs and short loops, but there is **no quantitative
evidence** that a loop out-performs a still. So the flag is the experiment: it is evaluated per
user (`flag_enabled(ANIMATED_POST, user_id)`) at BOTH ends, so a PostHog rollout-percentage
condition splits users into a loop cohort and a still cohort, and `post_outcome` already carries the
readout. It is not wired as a `utilities/experiments.py` multivariate experiment: an unresolvable
experiment reads CONTROL, which would make "flag on, no experiment defined" silently produce
nothing. Promote it to an `ExperimentSpec` if the readout needs PostHog's stats engine.

## LinkedIn limits this is built against

| Fact | Source |
|---|---|
| The Images API (`/rest/images`) takes JPG, GIF and PNG, under 36,152,320 pixels; **GIF up to 250 frames** | [Images API, li-lms-2026-09](https://learn.microsoft.com/linkedin/marketing/community-management/shares/images-api?view=li-lms-2026-09) |
| The Images API **replaces** the Assets API (`/v2/assets?action=registerUpload`) | same page |
| The LinkedIn UI accepts a GIF up to 500 frames, 36,152,320 px and 100 MB | [LinkedIn Help a564109](https://www.linkedin.com/help/linkedin/answer/a564109) |
| The Videos API takes MP4 from 3 s to 30 min | LinkedIn Videos API docs |
| A ≤15 s MP4 autoplays and loops in the feed | weak, third-party source |
| A GIF uploaded through the VIDEO control is frozen to a still | weak, third-party source |

So a GIF is an **image** to LinkedIn: it goes through the Images API, never the Videos API. Our own
caps are tighter than LinkedIn's: **≤250 frames** (the API cap, not the UI's 500) and **≤5 MB**
(`GIF_MAX_BYTES` — a feed GIF that size already loads slowly on mobile).

## How a post is published today (the investigation)

Every post publishes through LinkedIn's **REST API with the user's OAuth token**, not Selenium:

| Post type | Path |
|---|---|
| Text / text + image | `poster.share_on_linkedin` → LEGACY `/v2/assets?action=registerUpload` (recipe `feedshare-image`) + `/v2/ugcPosts`, `shareMediaCategory=IMAGE` |
| Video | the same function and legacy path, recipe `feedshare-video`, `shareMediaCategory=VIDEO` |
| Carousel | `share_carousel_on_linkedin`, legacy multi-image ugcPost |
| Document | `share_document_on_linkedin` — VERSIONED `/rest/documents` + `/rest/posts`, legacy fallback |
| Occasion / milestone | the ONLY Selenium publish: `linkedin/share_composer.py` (#1088) |

Nothing in the tree called `/rest/images` before this change. The legacy Assets API docs name no
image formats at all, so whether `feedshare-image` takes an animated GIF is UNPROVEN. That is why
the loop does NOT ride the still's path: it goes through the **versioned Images API**
(`upload_image_versioned` → `/rest/images?action=initializeUpload`, byte PUT, then
`_create_image_post_versioned` → `/rest/posts`), mirroring the document path. The byte PUT sends
`application/octet-stream` like every other upload here; the Images API documents no Content-Type
for it and reads the format from the bytes.

## How a loop is made

1. `_generate_text_post_image` stores the gated still exactly as before, then calls
   `produce_post_loop` (a no-op unless the flag is on).
2. **No new generation path.** `create_runway_video` (standard tier, `STANDARD_VIDEO_MODEL`) animates
   the STORED still at `LOOP_SOURCE_DURATION` = 5 s, Runway's minimum. The motion prompt is the
   existing concept-driven author, fed the still's brief receipt (its prompt + Stage 1 concept), so no
   second Stage 1 call is paid; `LOOP_MOTION_DIRECTIVE` is prepended — camera locked, ONE element
   moving.
3. `make_loop_from_video` turns the clip into a GIF with ffmpeg's palettegen/paletteuse, ping-ponged
   (forward then reversed) so the last frame IS the first. `-frames:v` caps the encode and the file
   is read back (Pillow `n_frames`) — **≤250 frames and ≤5 MB are enforced on the file**, stepping
   width/fps down for at most 3 encodes. Over all three → None, the still ships.
4. The GIF is stored beside the still as `<still stem>.loop.gif`. **No column records it — the name
   is the binding**: replacing or removing the still (`remove_post_image_file` deletes the loop too)
   can never leave the publisher holding a loop of a different picture.

A **code-drawn** still (its receipt's `archetype_rendered` is a data graphic) never goes to Runway —
an image model would re-draw its verified figures. It gets a $0 kinetic loop of its OWN facts
instead (`motion_design.loop_from_receipt`: a stat counts up, a chart draws in, a checklist ticks),
under the same ≤250-frame / ≤5 MB / seamless rules ([motion-design.md](motion-design.md)).

`make_loop_mp4` is the alternative output (a ≤15 s ping-pong H.264 MP4). It is built and tested but
NOT wired to publishing: an MP4 would turn an image post into a video post, which is a different
decision.

## Publishing, and what never happens

`post_to_linkedin` asks `_animated_loop_for` — flag on for this user AND a loop beside the CURRENT
still — and only then calls `share_animated_image_on_linkedin`. It re-checks the GIF's limits before
upload. **A loop failure never blocks a post**:

| Failure | Outcome |
|---|---|
| Flag off, no loop, lookup fault | the still, exactly as before |
| GIF over limits, upload fails, `/rest/posts` ANSWERS with an error (incl. a retired `LI_API_VERSION`) | the still via `share_on_linkedin` — nothing was published, so no duplicate |
| `/rest/posts` **read timeout**, or a 2xx with no id | None → the post is flagged `error` for a human. LinkedIn may have created it, and a fallback could post twice |

## Cost

One standard-tier Runway render per text post that gets an image (`gen4_turbo` 5 s ≈ $0.25 at the
module's price table, recorded by `track_media_cost` inside `create_runway_video`), one motion-prompt
LLM call, and a local ffmpeg encode. The create-content task waits on Runway the same way a video
post already does.

## How to enable — and the live-grounding TODO

**Do not enable fleet-wide before one live pass.** The Images API path is new in this tree and three
things are unproven on a member (`urn:li:person`) token: that `w_member_social` may
`initializeUpload` on `/rest/images`, that a GIF uploaded there renders ANIMATED in the feed (and
not frozen), and that a post created immediately after upload does not race the asset's
`PROCESSING` state.

1. Target ONE test user: PostHog flag `animated-post-enabled` with a distinct-ID condition (never a
   person property — local evaluation cannot resolve it).
2. Let that user's next generated text post produce a loop (`<stem>.loop.gif` appears beside the
   image under `images/posts/<post_id>/`).
3. After it publishes, open the post on desktop AND mobile: the image must MOVE. A still means the
   GIF was frozen — turn the flag off. Success is the outcome on the feed, never a 201.
4. Only then widen the rollout percentage.

There is no Selenium path to probe: publishing is API-only, so `scripts/linkedin_live_validation.py`
has nothing to drive here. The grounding pass above is the check.
