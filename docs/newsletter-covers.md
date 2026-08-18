# Newsletter cover images — full posture (issue #893)

The TL;DR lives in [CLAUDE.md](../CLAUDE.md) under **Content generation & scheduling**. This file
holds the detail anyone needs before changing how a cover is stored, gated, or attached.

A LinkedIn article cover is the first thing a subscriber sees in the feed and in the notification
email. It is also a **public brand asset**, which is why the two ways of getting one are
deliberately not symmetric.

## Files

| File | Purpose |
|---|---|
| `src/cqc_lem/utilities/newsletter_cover.py` | ONE place a cover is validated, stored, and generated |
| `src/cqc_lem/utilities/linkedin/article_editor.py` | `attach_article_cover` — the publish-time upload into LinkedIn's hidden file input |
| `src/cqc_lem/app/run_scheduler.py` | `generate_newsletter_cover` task + the `cover_image_auto` queue hook; `auto_notify_pending_covers` — the pre-slot reminder (#1432) |
| `src/cqc_lem/app/engagement/newsletter.py` | `_approved_cover_path` — the ONE gate deciding a cover may reach LinkedIn |
| `src/cqc_lem/ui/.../review/NewsletterQueue.tsx` | Per-edition upload / generate / approve / remove |
| `src/cqc_lem/ui/.../account/NewsletterCard.tsx` | The account-settings opt-in (`cover_image_auto`) |

## Two sources, two statuses

| Source | Where it comes from | Lands as |
|---|---|---|
| `upload` | The author picks their own artwork in the review queue | `approved` |
| `ai` | Per-edition "Generate with AI", or `cover_image_auto` on a fresh draft | `pending_review` |

An upload is the author's own work, so it needs no review and publishes with the edition — that
half is complete on its own for a user who never touches generation. A generated cover is **never**
`approved` by the system: it sits at `pending_review` until the author approves it in the queue.

`_approved_cover_path` is the only thing that reads `cover_image_status` at publish time. Anything
that reads `cover_image_path` on its own would publish unreviewed artwork; don't.

## Why no cover was ever approved — and what happens at the slot (issue #1432)

The #1284 re-audit read all ten editions in production: every generated cover sat at
`pending_review`, none had ever reached `approved`, and **every shipped edition went out
cover-less**. Nothing in the gate was broken. The approval was never *asked for*:

1. **The control was only reachable inside an open editor.** `Approve cover` lives in the
   review-queue editor panel (`/content?tab=newsletters` → select an edition → *Cover image*). The
   queue LIST — the screen an author actually scans — said nothing about a cover at all, so a
   waiting cover was invisible unless you opened that edition and scrolled to it.
2. **Two approvals, near-identical names.** The prominent blue **Approve & Schedule** approves the
   EDITION; the cover has its own, separate `Approve cover`. Clicking the big one reads as
   approving everything on the screen.
3. **The draft-ready email could not mention it.** `notify_newsletter_draft_ready` is sent inside
   `_topup_newsletter_drafts_for_user`, at draft creation — the cover is rendered asynchronously by
   `generate_newsletter_cover` and lands *minutes later*. That email is structurally incapable of
   reporting a cover state that does not exist yet.
4. **The publish path said nothing.** `_approved_cover_path` returned `None` and the edition
   published without its cover, silently.

**Decision: notify and publish.** An edition NEVER waits on its cover — cadence is the promise, and
a held edition is a worse failure than a cover-less one — and `_approved_cover_path` is not
weakened. What changed is that the state is legible *before* the slot:

| Where | What it says |
|---|---|
| Queue list row | `🖼️ Cover needs your approval` on any edition whose cover is `pending_review` — `— publishes without it otherwise` only when the edition itself reaches that slot (see #1135 below) |
| Editor, under the cover | That an unapproved cover means the edition publishes on time **without a cover** — again only when the edition itself reaches that slot |
| Above `Approve & Schedule` | That the button schedules the EDITION only, and the cover still needs its own approval |
| Email | `auto_notify_pending_covers` (daily 10:30 UTC, after the 10:00 top-up) emails the author for any edition publishing within `NEWSLETTER_COVER_REMINDER_LEAD_HOURS` (36) whose cover is still pending. ONE-SHOT per edition via a Redis claim, released on a failed send; **fails open** — a Redis outage degrades to at most one email per run, never to silence |
| Publish | `_approved_cover_path` logs INFO when it drops a pending cover. INFO deliberately: publishing cover-less is the DESIGNED outcome, and a repeated `log_warning` would file a defect against working behaviour |

**"An edition never waits on its COVER" is not "an edition always ships" (issue #1135).** The body
has its own, separate gate: `auto_publish_newsletters` on `newsletter_settings`. A generated edition
rests at `draft`, and `get_editions_due_to_publish` now selects `status='approved' OR (status='draft'
AND auto_publish_newsletters=1)` — so for an opted-out account the slot passes and the draft keeps
waiting in the queue, while the cover rule above is unchanged either way. Existing rows were
backfilled to `true` (no behavior change on deploy day); new rows default to `false`. The cover
deliberately gets no equivalent opt-out — a generated cover is a public brand asset regardless of
what the body's setting says.

What that costs is **every sentence that promised the edition ships anyway**. Four of the surfaces
in the table above asserted it, and each one was telling exactly the authors who now have to act
that they need not: the queue ROW's `— publishes without it otherwise` (the one an author reads
without opening anything), the editor's under-the-cover copy, the cover-reminder email
(`send_newsletter_cover_pending_email`, `edition_publishes=`), and — worst of the four, because it
is the ONLY message an opted-out author gets about a new draft at all — the draft-ready email
(`send_newsletter_draft_ready_email`, `auto_publish=`). All four now report the account's setting.
The rule for anything added here: **a "publishes on time" clause is conditional, a "without a cover"
clause is not.** An APPROVED edition publishes either way, so it keeps the original wording.

The same change moved the draft-ready email's LINK. It used to point at `/account` — harmless while
every draft shipped on silence, because the email was an FYI. Now it asks an opted-out author for
the approval the edition will not publish without, and approving, editing and skipping an edition
all live on the newsletter queue (`_newsletter_queue_url`), never on the settings card. **An email
that asks for an action must land on the screen that can take it** — a CTA pointing somewhere else
is the same false reassurance in link form, and it costs the edition, not just a click.

## The deterministic gate

Both sources pass `inspect_cover_bytes` first — this is what stops an unusable image reaching a
published article, and approval is the human half layered on top of it:

- decodable as an image at all
- PNG / JPEG / WEBP
- ≤ `MAX_COVER_BYTES` (8 MB)
- ≥ `MIN_COVER_WIDTH` × `MIN_COVER_HEIGHT` (640×336)
- landscape-ish: aspect ratio in `[1.0, 3.0]` (LinkedIn renders covers at 1.91:1)

A failing UPLOAD is a 400 with the reason. A failing GENERATION is never stored on the row — a
truncated or undersized render leaves the edition exactly as it was, and the task logs why.

## Storage

`cover_image_path` holds a path **relative to `assets_dir`**
(`images/newsletter_covers/<user_id>/ed<edition_id>_<random>.<ext>`), so the same value is both the
disk location and the `/api/assets?file_name=` value the SPA renders from. `/api/assets` is public,
hence the random suffix — a predictable name would let anyone enumerate an unpublished cover.
`cover_abs_path` re-resolves through `realpath` and re-checks containment, so a hand-edited row can
never hand the publish flow an arbitrary file to upload.

The API returns `cover_image_url` and drops `cover_image_path` — the browser never sees a
filesystem path.

## Generation

`generate_cover_for_edition` reuses the SHARED image path rather than adding a parallel per-content
-type helper:

- prompt: `get_flux_image_prompt_from_ai` (already encodes the engagement best practices a cover
  needs — one focal subject, strong foreground separation, **no text/logos/charts**, which is
  exactly what makes generated covers look machine-made when it is missing)
- render: `generate_post_image(..., ratio="16:9", depicts_person=False)` — 16:9 is Flux's closest
  supported ratio to LinkedIn's 1.91:1, and `depicts_person=False` keeps the avatar LoRA out: a
  cover is the newsletter's brand asset, not a scene the author appears in

The render is COPIED into the user's cover dir, never moved, so nothing else that references the
generated file breaks.

## Keeping the cover in sync with the edition (issue #1287)

A cover is generated from the edition's title, subtitle and opening body. When the author later
edits the title or subtitle in the review queue (`PUT /user/newsletter-draft`), the cover is
re-briefed from the updated opening text automatically:

- it only triggers for **AI-generated covers** (`cover_image_source = 'ai'`) — uploaded artwork is
the author's own choice and is never replaced automatically;
- only **title or subtitle** edits trigger it — a body edit may be deep in the newsletter and does
not necessarily change the visual idea, so the author decides whether to regenerate;
- the old AI cover file is removed, a new `generate_newsletter_cover` task is queued, and the new
render still lands `pending_review` — the hard-approval gate is unchanged.

## Cost

Generation costs money per edition, so it is **opt-in twice over**: `cover_image_auto` is off by
default, and even with it on the result still waits for approval. The per-edition "Generate with
AI" button is the other entry point. Title/subtitle edits on an AI cover cost one extra generation
per edit. Nothing generates a cover on a publish.

## Attaching at publish

`attach_article_cover` writes the absolute path into the article editor's hidden
`<input type=file>` (clicking the styled button would open an OS file chooser Selenium cannot
drive), falling back to clicking an "Add a cover image" affordance first on variants that render
the input lazily, then best-effort confirming a crop/preview dialog.

That input is **hidden by design**, which is the one thing to keep in mind when touching the
resolver: `resolve_article_editor_step` applies its `is_displayed()` filter only when
`visible_only` is True. The cover routes pass `visible_only=False` — restore an unconditional
visibility check there and every cover attach misses silently (a warning, never a failed publish),
which looks exactly like "LinkedIn changed the DOM again".

`STEP_COVER` is deliberately **not** in `EDITOR_SCREEN_STEPS` / `ALL_STEPS`: the cover is optional,
so grading it would report `MISSING` on every cover-less publish. A cover that will not attach is a
warning and never a `failed_step` — an edition without its cover is still a published edition.
