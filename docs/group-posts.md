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
                 │  (Skip / Put back in the queue)
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

**ONE open draft per user still holds.** `get_open_group_post_draft` is unchanged (`ready` only), so
the Sunday beat still skips a user who has a live draft and still drafts afresh for one who skipped.
A restore that would create a SECOND open draft is refused with a **409** rather than leaving two
rows the publish run could pick between.

## Media

A group post may carry one native image or video (`media_url` + `media_type`, both NULL on a
text-only post). The URL is the same public `/api/assets?file_name=` value `posts.image_url` stores,
so the studio reuses the post-image surface rather than growing a second one:

1. The SPA uploads (`POST /user/post/image`, no `post_id`) or renders (`POST /user/post/image/generate`)
   into the author's own preview dir — the same compose-time path `/schedule_post/` uses.
2. `PUT /user/group-post-draft` takes that URL and runs it through **`owns_post_image_url`**: it is
   caller input on a field the publish run later hands to LinkedIn, so only a preview WE issued to
   THIS user resolves. Anything else is a 400.
3. The kind is read off the stored FILE (`determine_media_type`), never off the request — the
   uploader is judged on the extension on disk, and the stored name comes from the DECODED format.
4. Replacing or removing media deletes the file it replaced, the same clean-up `_attach_post_image`
   does for a post.

At publish, `_attach_group_media` writes the path into the composer's hidden `<input type=file>` —
clicking the styled affordance opens the OS file chooser, which Selenium cannot drive, and
`webdriver.Remote`'s local file detector ships the bytes to the Chrome node, so the worker's own
path is the right thing to send. **Media goes in BEFORE the text**: LinkedIn's uploader takes over
the composer while it transcodes, and text typed first is what the overlay discards.

**The media chain fails OPEN.** A group post that goes out as text is worth more than no post at
all, so a missing file or a drifted control is a `log_warning` and the run publishes the text — the
same posture the article cover and `render_image_gated` take. The warning is what makes the drift
visible: it escalates on repeat and files ONE grouped issue.

Video is supported end to end at the data + publish layer, but the studio can currently only attach
IMAGES — there is no video upload/validation surface (size, duration, codec) to reuse yet. Tracked
separately; see the follow-up linked from #1224.

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
