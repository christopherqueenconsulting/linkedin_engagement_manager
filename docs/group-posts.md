# Weekly group post — statuses, media, and what actually works in a group

The weekly group post is two beats with a review window between them (issue #932): Sunday drafts,
Tuesday publishes whatever draft is still `ready`. This document covers what the author can do to
that draft in between — issue #1224, filed from in-app feedback that the Content Studio could only
*edit* a group post, never re-queue one, never attach anything to it, and never said what makes a
group post land.

The lane itself (rotation, `post_enabled`, unpostable groups, the `last_post_run_at` starvation fix)
is in `docs/engagement-automation.md`. This is the *draft's* posture.

## The pieces

| Thing | The ONE place |
|---|---|
| Draft row | `group_post_drafts` (`platform/db/repositories/groups.py`, readers in `utilities/db.py`) |
| Status vocabulary | `GroupPostDraftStatus` — `user_settable()` is the half the SPA may write |
| Media kind | `GroupPostMediaType` — derived from the stored FILE, never from the client |
| Studio surface | `GET`/`PUT /api/user/group-post-draft`, rendered by `ui/.../review/GroupPostQueue.tsx` |
| Publish | `auto_post_to_group` + `_attach_group_media` (`app/engagement/feed.py`) |
| Best-practice list | `content_framework.GROUP_POST_BEST_PRACTICES` |

## Statuses

```
                 ┌────────── user ──────────┐
   drafted ──▶ ready ◀──────────────────▶ skipped
                 │  (Skip this week / Undo skip — until the publish slot)
                 │
     publish run ├──▶ published   (it shipped into the group)
                 └──▶ failed      (the group would not take a member post)
```

Two rules decide everything here:

- **The user owns `ready` ⟷ `skipped`, and nothing else.** `published` and `failed` are the publish
  run's RECORD of what happened; accepting either from a client would let the queue claim a post
  that never shipped. `GroupPostDraftStatus.user_settable()` is that boundary in code, and the API
  refuses anything outside it with a 422.
- **Skipping is reversible until the slot passes.** That is the whole point of #1224: the old
  surface dropped a skipped draft out of the studio's view, so a mis-click cost the week. The studio
  reads `get_current_group_post_draft`, which returns the newest `ready` OR `skipped` row — with an
  open draft always outranking a skipped one, so restoring an old skip can never hide the post that
  is about to ship.

- **"Until the slot passes" is an actual instant** (#1415). `utilities/group_post_slot.py` computes
  it: the first **Tuesday 15:00 UTC after the row was last written** — the slot the draft is WAITING
  ON. For the ordinary post that is the Tuesday the Sunday beat drafted it for. It is deliberately
  **not** `created_at` alone: the publish beat carries an unshipped draft forward (no LinkedIn
  session that Tuesday, unreadable group switches, no Chrome slot), and such a row's first slot is
  already in the past while it is still the draft the studio shows — a `created_at` window would
  land the user's skip irreversible the moment they made it, which is the bug this exists to fix.
  The cost is that editing a skipped draft after the window closed reopens it; that is an explicit
  action on a draft the user plainly still wants, and it fails the same direction as everything else
  here. The Python helper mirrors the SPA's `utils/groupPostSlot.ts`; keep the two in step.
  Consequences:
  - `GET /user/group-post-draft` carries **`can_undo_skip`** and **`undo_deadline`**, so the studio
    and the Account card show **Undo skip** only while the PUT would honour it, and say the skip is
    final (next post drafted Sunday) after. A control that silently does nothing is the failure
    being avoided.
  - `PUT` refuses a late undo with a **409** naming that reason — a stale tab is the only way to
    reach it.
  - The undo **restores the same row**. There is no "generate a group post now": the lane carries
    ONE open draft forward and never replaces it, so a second draft is the thing the invariant
    forbids.
  - An undo on a week that was never skipped is an **expected no-op** — DEBUG, and none of the
    restore refusals apply to it.
  - Unreadable timestamps leave the window OPEN. The bug being fixed is a user stuck with an
    accidental skip; a restore is explicit and publishes at the next slot.

- **Not every skipped draft is one the USER skipped.** `auto_group_posts` also skips a draft whose
  group has since been switched off for posting, and that row is now visible in the studio like any
  other. Restoring one of those would report success and be dropped again at the next slot, every
  week, so the restore is refused with the reason the user can act on (turn posting back on for the
  group). Unreadable switches are `None`, never an opt-out — the restore goes through, the same way
  the publish beat holds the draft rather than cancelling it on that read.

**ONE open draft per user still holds.** `get_open_group_post_draft` is unchanged (`ready` only), so
the Sunday beat still skips a user who has a live draft and still drafts afresh for one who skipped.
A restore that would create a SECOND open draft is refused with a **409** rather than leaving two
rows the publish run could pick between.

## Media

A group post may carry one native image or video (`media_url` + `media_type`, both NULL on a
text-only post). The URL is the same public `/api/assets?file_name=` value `posts.image_url` stores,
so the studio reuses the compose-time media surface rather than growing a second one:

1. The SPA uploads an image (`POST /user/post/image`, no `post_id`), renders one
   (`POST /user/post/image/generate`) or uploads a video (`POST /user/post/video`, issue #1443) into
   the author's own preview dir — the same compose-time path `/schedule_post/` uses.
2. `PUT /user/group-post-draft` takes that URL and runs it through **`owns_post_media_url`**: it is
   caller input on a field the publish run later hands to LinkedIn, so only a preview WE issued to
   THIS user resolves. Anything else is a 400.
3. The kind is read off the stored FILE (`determine_media_type`), never off the request — the
   uploader is judged on the extension on disk, and the stored name comes from the DECODED format.
4. Replacing or removing media deletes the file it replaced, the same clean-up `_attach_post_image`
   does for a post.

**The two halves keep separate dirs and separate ownership functions on purpose.**
`images/post_previews/<user_id>/` is `owns_post_image_url`'s, `videos/post_previews/<user_id>/` is
`owns_post_video_url`'s, and only the union — `owns_post_media_url` / `post_media_abs_path` /
`remove_post_media_file` in `utilities/post_video.py` — reads both. Widening the image gate instead
would let a caller hand `/schedule_post/`'s `image_url` an MP4, and the group post is the one
surface that genuinely takes either kind.

### The video contract

`utilities/post_video.py` grades an upload BEFORE a byte lands in the preview dir, because the
checks a video needs are not the ones an image needs. Two postures in one gate:

- **Deterministic, always enforced:** size (75 KB – 200 MB) and the container, read from the file's
  own ISO `ftyp` brand — MP4 or MOV, and the brand is also what picks the stored extension, since
  that extension is what `determine_media_type` reads at publish.
- **Measured, fail-open:** duration (3 s – 15 min), frame size (≥ 256×144) and codec (H.264 / HEVC)
  need ffprobe, which is not installed in every container that serves this API. A probe that cannot
  run accepts what the head check already proved — the same posture `_probe_video_file` takes
  (issue #1280) — while a probe that CAN run and reports a violation refuses with the reason. A
  failure is a **400 carrying the user-facing reason**, and nothing is stored.

At publish, `_attach_group_media` writes the path into the composer's hidden `<input type=file>` —
clicking the styled affordance opens the OS file chooser, which Selenium cannot drive, and
`webdriver.Remote`'s local file detector ships the bytes to the Chrome node, so the worker's own
path is the right thing to send. **Media goes in BEFORE the text**: LinkedIn's uploader takes over
the composer while it transcodes, and text typed first is what the overlay discards.

**The media chain fails OPEN.** A group post that goes out as text is worth more than no post at
all, so a missing file or a drifted control is a `log_warning` and the run publishes the text — the
same posture the article cover and `render_image_gated` take. The warning is what makes the drift
visible: it escalates on repeat and files ONE grouped issue.

That has to hold for what the media step LEAVES BEHIND too, so `_attach_group_media` reports what it
did to the COMPOSER (`attached` / `untouched` / `left_open`), not just whether the upload worked.
Once the uploader has been opened, an editor or Post button we then cannot find is OUR overlay still
covering the composer — never evidence that the group refuses member posts. So that run does NOT go
through `_unpostable`: the draft stays `ready` for the next weekly slot and the rotation does not
move past a group whose share box opened seconds earlier. With no media in the draft, nothing of
ours is on screen and a missing editor is still the group's answer, which retires the draft and
rotates past it exactly as before (issue #858).

The page-wide tail of the input/trigger/confirm chains excludes the messaging overlay by name
(`msg-overlay-*` / `msg-form`, the containers `message_thread.py` already keys on). That overlay
rides every LinkedIn page and its attachment input declares an image `accept`, so without the
exclusion the "last resort" would not be a long shot — it is the control the run would
deterministically land on the moment the composer's own input drifts, uploading the author's image
into a message thread while reporting the media as attached. The TRIGGER carries that exclusion for
a harder reason than the input does: it is the one control in the chain the run CLICKS, and
messaging labels its own attachment control "Add a photo" — clicking that is #1012's rule broken,
not just a bad upload target.

**The commit is waited for, not timed.** LinkedIn transcodes an upload server-side and keeps the
media overlay's commit control (`Next` / `Done`) disabled until it is done, so `_attach_group_media`
polls that CONTROL rather than sleeping a fixed window sized on an image (issue #1443). Three
answers, and they are not the same thing:

| What the poll saw | What the run does |
|---|---|
| Control resolved and became clickable | click it, media is attached |
| No control at all | click nothing — some composer variants have no commit step, so this is an expected no-op and never warns |
| Control resolved and stayed disabled | warn, and return `left_open` — the upload never finished, so our overlay is still up |

A video gets a much longer window than an image (`_VIDEO_READY_POLLS` vs `_IMAGE_READY_POLLS`)
because the cost of waiting is a slower weekly beat and the cost of committing early is an empty
media frame on a published post. The window comes off the draft's own `media_type`, falling back to
the stored file for a row written before that column carried a kind.

**A poll that misses costs a poll.** The lookup inside the loop uses its own short wait
(`_CONFIRM_LOOKUP_SECONDS`), never the session's `WAIT_DEFAULT_TIMEOUT`: through the shared wait,
every MISS pays 15s and every HIT pays nothing, so the ABSENT row above — an expected composer
variant — would become the most expensive answer in the chain while the transcode the window exists
for got only the sleeps. With the short wait the poll counts mean the wall clock they read as:
roughly half a minute for an image, three to six minutes for a video.

## What works in a group (and why the list lives in code)

`GROUP_POST_BEST_PRACTICES` is ONE tuple read by BOTH halves: the drafting prompt
(`ai_helper.generate_group_post`) is held to it, and the API serves it on the draft payload so the
Content Studio panel shows the author the same rules. Same reasoning as
`{POST,NEWSLETTER}_BANNED_SCAFFOLDS` — a prompt and a UI copy of the same guidance drift, and then
the product tells the user one thing while the model does another.

The 2026 evidence behind each line (searched 2026-08-11):

- **Question- or problem-led opening.** Groups rank on the discussion a post starts; guides on group
  strategy converge on opening with a prompt members can answer.
  ([linkedinpreview.com](https://linkedinpreview.com/blog/linkedin-groups-guide-2026),
  [scale.jobs](https://scale.jobs/blog/best-practices-for-linkedin-group-post-engagement))
- **Short and specific.** No Groups-specific word count is reliably sourced; every guide asks for
  concise, discussion-ready posts, so the rule is "answerable on a phone", not a number.
- **Native, never link-out.** External-link posts are the lowest-engagement format on LinkedIn and
  the first thing group moderators remove.
  ([ailwin.ai](https://ailwin.ai/blog/linkedin-groups-2026-strategy),
  [digitalapplied.com](https://www.digitalapplied.com/blog/linkedin-algorithm-2026-engagement-strategy-guide))
- **Media beats text-only.** Platform-wide 2026 ranking puts carousels and native documents above
  native video, video above text-only, and external links last. That measurement is platform-wide
  rather than Groups-only, so treat it as a strong proxy — which is exactly why LEM offers media as
  an OPT-IN attachment and never auto-renders one for a group post.
- **No promotion.** Groups moderate self-promo; mention what you sell only when it answers a
  question a member asked. (`_NO_SELF_PROMO_GUARDRAIL` already carries this into every draft.)
- **Reply fast.** The thread is what the group sees; the sources agree on responsiveness far more
  than on any posting trick.
- **One good post per group per week.** The cadence guides recommend 1–2 original posts weekly, which
  is what the Tuesday beat already does — the rule is here so the author does not "help" by posting
  daily into the same group.

Sources are heuristics from practitioner guides, not LinkedIn documentation; LinkedIn publishes no
Groups ranking contract. Treat the list as revisable, and revise it in the ONE place.
