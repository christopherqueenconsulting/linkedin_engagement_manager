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
| `stat_counter` | a verified `stat` | the figure counts up from 0 (ease-out) | a gold bar fills in sync; the label fades in after |
| `chart_draw` | a verified `comparison` (`highlight_chart`) | bars grow from the baseline, the gold bar LAST | the annotation fades in |
| `checklist_tick` | 3-5 verified `steps` | items appear one by one | ticks draw; the last item stays UNTICKED (the curiosity gap, `checklist_states`) |
| `before_after_wipe` | a verified `before_after` pair | a gold divider wipes BEFORE into AFTER | — |
| `kinetic_mask` | nothing | the hook's lines mask-reveal in sequence; a hero number scales in | a gold underline sweeps under the key word |
| `kinetic_slide` | nothing | the same, lines sliding in | the same |

Kinetic typography is two styles so the "never twice in a row" rule always has a second option.

## Rules

- **Frame 0 is the full hook** (LinkedIn's thumbnail, `docs/content-quality-audits/video.md`
  § F3c) and holds for `MOTION_HOLD_SECONDS` = 0.8 s before anything moves. Kinetic typography's
  frame 0 IS the static card; a data style's frame 0 is the hook with an empty data region.
- **Ease-out cubic** (`ease_out_cubic`) on every motion. MP4: 24 fps, 6-8 s
  (`VIDEO_TITLE_CARD_SECONDS`). GIF: ≤12 fps (`MOTION_GIF_MAX_FPS`), 6 s, 720 px wide.
- **A GIF is seamless.** It ends in its start state: the data fades out, or the underline retracts,
  and the last frame is sampled at `t = T`, so it equals frame 0 pixel for pixel.
- **GIF limits are `animated_loop`'s**: ≤250 frames, ≤5 MB, checked on the FILE
  (`gif_within_limits`), stepping width and fps down (`_attempt_plan`) for at most three encodes.
- **Brand colours and Montserrat only**, from `video_title_card.title_card_palette`.
- **Round 6 holds.** Nothing is drawn below the caption band's worst-case top
  (`caption_band_top`); a data style puts the byline in the top row, and a kicker that would
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

## Failure posture

Motion is the upgrade, not the asset. `_card_motion` failing logs a WARNING and the drifting card
ships as before; `create_motion_gif` and `loop_from_receipt` never raise and return None, and the
still ships.
