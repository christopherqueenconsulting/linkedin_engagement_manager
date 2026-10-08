# Motion design — kinetic $0 title cards and GIF loops

The owner live-tested a GIF post built on the $0 branded title card: it animated, but it was "not
that appealing or dynamic" — static type over a drifting slab and an extending rule. The motion
system replaces that drift with ONE intentional motion per piece, drawn by code. It costs nothing to
render: PIL frames piped into ffmpeg, exactly as the card already was. No model is called.

| Piece | Where |
|---|---|
| The ONE place motion is designed | `src/cqc_lem/utilities/motion_design.py` |
| The MP4 (title card video) | `video_title_card.create_title_card_video` → `_card_motion` → `write_title_card_video(motion=…)` |
| The GIF loop (code-drawn still) | `animated_loop.produce_post_loop` → `motion_design.loop_from_receipt` → `write_motion_gif` |
| The shared ffmpeg pipe | `video_title_card.encode_frames` |
| Style history | the title card's history file, `<assets>/videos/title_card/history/<user_id>.json` |
| Tests | `tests/unit/utilities/test_motion_design.py` (one `slow` real encode) |

## The styles

| Style | Needs | Hero motion | Secondary |
|---|---|---|---|
| `stat_counter` | a verified `stat` | the figure, at poster scale, springs in (ease-out-back) and ticks up from 0 | a gold bar fills in sync; the label fades in after |
| `chart_draw` | a verified `comparison` (`highlight_chart`) | bars grow across the frame — the longest spans the full width with its value inside — the thicker gold bar LAST | the annotation fades in |
| `checklist_tick` | 3-5 verified `steps` | items appear one by one at headline-adjacent size | ticks STROKE-draw as paths (`_stroke`, never a glyph); the in-progress item gets a drawn dash; the last stays UNTICKED (`checklist_states`) |
| `before_after_wipe` | a verified `before_after` pair | a gold divider wipes a frame-width BEFORE figure into the AFTER one | — |
| `kinetic_mask` | nothing | the hook's lines exit and mask-reveal in sequence; a hero number springs in | a gold underline retracts and sweeps back under the key word |
| `kinetic_slide` | nothing | the same, lines sliding in | the same |

Kinetic typography is two styles so the "never twice in a row" rule always has a second option.

## Rules

- **Frame 0 is the COMPLETE piece** — the headline AND the final figure, chart or list — because
  LinkedIn may show frame 0 as the static thumbnail (`docs/content-quality-audits/video.md` § F3c).
  The timeline is hold → reset → build → hold: the complete state holds `MOTION_HOLD_SECONDS`
  (0.8 s), the data fades out (`_RESET_SECONDS`), sits fully BLANK for `RESET_BLANK_SECONDS`
  (0.3 s, issue #2316), builds back from `_BUILD_AT`, and holds its complete state again for at
  least `MOTION_FINAL_HOLD_SECONDS` (1.2 s) to the end. The blank beat is a cut: motion_123's
  counter read 55 → 18 → 55 and motion_135's checklist read as building in reverse when one blank
  frame was all that separated the complete figure from the rebuilding one. A visible count or list
  only ever goes up.
- **The caption band is never empty on a title card** (issue #2316): the burned caption's cue holds
  for the whole title-card video (`video_captions.caption_hold_seconds`), not the muted-autoplay
  window alone, so the lower area the card keeps clear for it is filled to the last frame.
- **Ease-out cubic** (`ease_out_cubic`) on every motion; **ease-out-back** (`ease_out_back`, ~10%
  overshoot) on a hero's scale-in. MP4: 24 fps, 6-8 s (`VIDEO_TITLE_CARD_SECONDS`). GIF: ≤12 fps
  (`MOTION_GIF_MAX_FPS`), 6 s, 720 px wide.
- **No frame is static.** A soft brand-gold glow drifts on a closed orbit behind the type
  (`_background` / `_ground`), exactly one orbit per piece.
- **A GIF is seamless.** The complete state and the orbit both return to their start, and the last
  frame is sampled at `t = T`, so it equals frame 0 pixel for pixel.
- **The canvas is used.** No slab: nothing is drawn that carries no meaning. A GIF has no caption
  band, so its data region runs to the bottom margin; the headline is set up to 13% of the width
  and gives room back to the data (`_HEADLINE_SHARES`) only when the data would not fit.
- **GIF limits are `animated_loop`'s**: ≤250 frames, ≤5 MB, checked on the FILE
  (`gif_within_limits`), stepping width and fps down (`_attempt_plan`) for at most three encodes.
  The drifting glow changes every pixel, so a 720×900 GIF is ~2.7-4.0 MB; a taller 9:16 one
  steps down a width.
- **Brand colours and Montserrat only**, from `video_title_card.title_card_palette`.
- **Round 6 holds for a video.** In an MP4 nothing is drawn below the caption band's worst-case
  top (`caption_band_top`); a data style puts the byline in the top row, and a kicker that would
  collide with it is dropped (the byline is attribution, the kicker decoration).
- **Fact rules.** A data style is offered only when `image_graphics.assert_traceable` passes for its
  archetype, and `plan_data` re-runs it immediately before planning. A counter's last value is the
  fact's own `display` string (`counter_text`), at the display's precision. A figure that does not
  trace, or a data style that cannot be set legibly, falls to the next option and finally to
  kinetic typography.

## Where the facts come from

1. Stage 1's validated graphic (`concept.graphic`) when it carries any section.
2. Else the post's OWN paragraphs, each read by `image_graphics.slide_graphic` with the whole post as
   evidence (`graphic_from_post`) — a stat, a "from A to B" pair, or a 3-5 line list. A comparison
   chart is never guessed from prose: it needs Stage 1.
3. For a GIF loop of a code-drawn still, the still's brief receipt: its `concept.graphic`, its
   `hook_text`, and its `archetype_rendered`, whose motion is PREFERRED (a stat card counts up, a
   chart draws in) unless that style just ran.

## Rotation

`pick_style` never repeats the author's last style, takes a preferred style when allowed, and
otherwise puts a data style that has not run lately ahead of kinetic typography. The history shares
the title card's file: it is now `{"variants": [...], "motions": [...]}`, six deep each; the
pre-motion bare list still reads as `variants`. A lost write costs one repeat at most (DEBUG).

## What changed for the GIF loop

`produce_post_loop` used to skip a code-drawn still (an image-to-video model would re-draw its
verified figures). With `ANIMATED_POST_ENABLED` on, that still now gets this $0 loop of its own
facts instead; Runway is still never called for it. A photo still is unchanged (the Runway
cinemagraph, `docs/animated-posts.md`).

## Round 8: every loop must visibly move

- **Cards are animated in code.** gif_125 (a quote card) and gif_130 (the last-resort typeset card)
  went to Runway: neither archetype was in `CODE_DRAWN_ARCHETYPES`, so a paid cinemagraph of a flat
  card moved nothing at 4 MB. `animated_loop._code_drawn` now also takes `quote_card`,
  `typeset_card`, a `code_drawn` / `last_resort` gate verdict and a `code_drawn*` render path. A
  card's loop sets the words the STILL sets (its `quote`, else its `hook_text`) in kinetic type, and
  never the concept's graphic, which is not on that still.
- **A measured motion floor** (`loop_motion_score`, `MOTION_FLOOR` 0.6). The median grey-level
  inter-frame delta over a loop's moving frames (frames under `_HOLD_DELTA` are holds or encoder
  noise; fewer than `_MOVING_SHARE_MIN` moving frames is a flash, scored 0). gif_125/130 measured
  about 0.3; every loop a reader saw move measured 1.0 or more. `produce_post_loop` checks it on
  both paths (code-drawn and Runway): a loop under it logs "Loop has no visible motion — the static
  image ships instead" (INFO) and the still ships. An unreadable file never blocks.
- **The stat label stays up through the count** (gif_123 showed a bare "7"): the label draws at the
  layer's alpha from the first build frame.
- **A slide travels far enough to see** (gif_141 read as a dim): `kinetic_slide` rows exit
  `_SLIDE_EXIT` (12%) and enter `_SLIDE_ENTER` (16%) of the width.
- **Never a reverse.** The data reset is a fade (`_data_clock`). gif_135's "un-checking" came from
  the showcase harness ping-ponging the title-card MP4 through `make_loop_from_video`; the
  production loop of a code-drawn still is drawn forward by `create_motion_gif`.

## Failure posture

Motion is the upgrade, not the asset. `_card_motion` failing logs a WARNING and the drifting card
ships as before; `create_motion_gif` and `loop_from_receipt` never raise and return None, and the
still ships.
