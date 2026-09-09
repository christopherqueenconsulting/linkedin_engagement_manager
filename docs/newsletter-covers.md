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

`generate_cover_for_edition` reuses the SHARED image path (`utilities/ai/image_brief.py` +
`utilities/ai/image_gen.py`) rather than adding a parallel per-content-type helper:

- prompt: `build_image_brief(..., surface="newsletter", ratio=COVER_IMAGE_RATIO, avatar=...)`
  (already encodes the engagement best practices a cover needs — one focal subject, strong
  foreground separation, **no text/logos/charts**, which is exactly what makes generated covers
  look machine-made when it is missing)
- render: `render_avatar_image_gated` when an avatar is resolved, else `render_image_gated`, at
  `COVER_IMAGE_RATIO` — the bounded `lem-vision` quality check, failing OPEN

### Concept-first briefs, not the hook (issue #1992)

Five straight covers in the same queue all rendered the same "person at laptop with notebook and
coffee mug on a wooden desk" scene — none of them encoded the edition's actual idea. Root cause:
`_edition_text` fed the brief author title + subtitle + the **first 1500 chars of body**, which for
a hook-first newsletter is the FRUSTRATION, never the PAYOFF (a routing switch, a silent failure, a
budget leak). The only concrete nouns in a hook about AI costs are laptop/screen/LinkedIn, so the
brief converged on stock office imagery every time.

`generate_cover_for_edition` no longer sends that excerpt. `_cover_concept_text` builds the brief's
content from:

1. **`_extract_cover_concept`** — a cheap `lem-simple` call over the edition's **full** body (not
   the first 1500 chars), returning `{core_mechanism, tangible_metaphor_candidates[≤3], avoid[]}`.
   **The title and subtitle are sent as their own sections and named as the governing promise**
   (#2008). A newsletter series overlaps: these editions are all about AI cost and several bodies
   discuss routing, which is the most mechanism-shaped idea in the text — so an unanchored "name
   its core mechanism" returned edition 14's routing idea for "Spot the Leak in Your LinkedIn AI
   Budget". The mechanism must be the one the TITLE promises; the body is supporting detail.
   The candidate OBJECTS lead the brief content and `core_mechanism` follows as labelled context,
   because the two can disagree — edition 15 came back as "intelligent model routing" while its
   objects were correctly a dripping faucet and a leaky pipe.
   `core_mechanism` and the candidates are folded into the brief content as "Core mechanism to
   depict" / "Candidate physical metaphors"; `avoid` names the generic nouns the edition's own hook
   keeps repeating. Fails closed to the old title/subtitle + 1500-char excerpt on any error or an
   empty response — a dead LLM degrades the steering, never the image. **Its `max_tokens` is
   `_CONCEPT_MAX_TOKENS` (2000), not a tight budget:** `lem-simple` is a reasoning model, and at
   300 the entire budget went to reasoning tokens — every one of the five live editions came back
   EMPTY with `finish_reason='length'`, silently, at DEBUG. A real extraction costs 540-1020
   completion tokens. This is the same trap `_BRIEF_MAX_TOKENS` documents; treat any new call on a
   `lem-*` tier the same way.
2. **Cross-edition variety** (`_recent_focal_concepts`) — the `focal_concept` of the user's last
   `_VARIETY_WINDOW` (5) cover receipts, most-recent first, read straight off the `.brief.json`
   sidecars already on the assets volume (no new DB column, mirrors `enforce_variety`'s approach for
   posts).

   `_recent_focal_objects` is what the brief actually gets: `_focal_objects` pulls the OBJECT out
   of each prior `focal_concept` first (`image_brief.METAPHOR_OBJECTS` for precision, a filler-strip
   token pass otherwise). **Passing the whole sentence steers nothing** — a focal concept is prose
   ("Budget leak depicted as a dripping valve spilling money") and the next one never matches it, so
   four consecutive live covers all chose a valve while every prior valve sat in the avoid list
   (issue #2000). A receipt written by the deterministic fallback is skipped: its focal concept is
   the edition's title text, not an object.

   `_cover_concept_text` returns **`(content, avoid_terms)`** — the avoid nouns come back separately
   and reach `build_image_brief`'s `avoid_terms` param, which puts them in the AUTHOR's user message
   only. They must never be folded into the content: the content is what the deterministic fallback
   turns into a RENDER prompt, and a renderer reads "laptop; screen; desk" as a request. That is
   exactly how a fallback cover for the leak edition came back as a laptop on a desk.
3. **`edition_format` / `hook_style`** — threaded from the edition row into `build_image_brief`'s
   `content_shape` param, so a listicle cover and a personal-story cover read differently.

The `newsletter` preset in `image_brief.py` was rewritten from a bare "no generic office stock"
negation (which the renderer, and apparently the brief LLM, both ignore) to a POSITIVE
object-first instruction with a metaphor vocabulary for abstract/financial/software topics (cost →
drip/leak/meter/scale, routing → switch/valve/fork, failure → cracked gear/fallen domino, audit →
magnifier/caliper/tally). `_NO_ANONYMOUS_PERSON` no longer invites "a close-up of hands mid-action"
— hands are the renderer's weakest anatomy regardless of what they're near.

**An edition is never steered off its own subject (#2008).** `_drop_topic_words` filters the
EXTRACTOR's avoid list as well as the variety terms, with singular/plural tolerance. It returns the
generic nouns the hook repeats, and for "Audit AI LinkedIn engagement to cut costs" that was `cost`
and `engagement` — so the gate rejected "Balance scale weighing cost against engagement", the one
correct concept it had, and the retry drifted onto a ledger on a desk. For the same reason a variety
term naming this edition's own mechanism or candidate objects is dropped: **relevance outranks
variety**, because banning the right object does not produce a different picture of THIS article, it
produces a picture of a DIFFERENT one.

**The repeat-object gate (#2000).** `avoid_terms` is not only a prompt line — `_valid()` rejects a
`newsletter` brief whose **`focal_concept`** names one of them, and the retry-with-reason above
names the repeated object. Graded on the focal concept, never the whole prompt: a valve in the
background of an otherwise distinct scene is not a repeat and must not cost a retry. The list is
capped at `_MAX_AVOID_TERMS` (8) so a long history can never starve the author into the fallback on
every run.

**The variety gate can never force the fallback (#2005).** A brief rejected ONLY for reusing an
object is held and returned if the retry cannot do better — a real brief beats the deterministic
template every time, and this is a DEBUG line, not the fallback warning. Two more guards on the
same failure: `_recent_focal_objects` contributes the PRIMARY object of each prior cover only (the
second is usually the topic's own noun), and `_drop_topic_words` removes any term the edition's own
title or subtitle uses. "Spot the Leak in Your LinkedIn AI Budget" cannot be steered away from a
leak; asking for it rejected both attempts and shipped the template. All four live editions fell
back at once that way.

**The stock-office gate.** Even with a better preset, `build_image_brief` rejects and retries (then
falls back to the deterministic template) a `newsletter`-surface brief whose prompt names one of
`laptop, notebook, coffee, desk, office, typing, keyboard, screen, monitor, phone` — unless an
avatar is in frame, where a person at a desk is a legitimate scene. `ImageBrief.fallback` records
whether the deterministic template shipped. The **retry carries the rejection reason** ("your
previous attempt was REJECTED: the prompt named the stock-office object 'laptop'") — re-sending the
identical prompt just re-drew the same rejected scene, which is what pushed four of five live
editions onto the fallback.

**The newsletter fallback is its own scene.** `_fallback_brief` for `surface="newsletter"` with no
avatar uses `_NEWSLETTER_FALLBACK_SCENE` (one tangible object on a workshop surface) instead of the
generic template, and strips the stock-office nouns out of the content summary first. The generic
template pasted the surface's INSTRUCTION preset into a RENDER prompt — including "People and
screens stay out of the frame" — and then asked for "blank screens" and "plain unbranded clothing".
A render prompt has no negation: every noun in it is a request, and the renderer duly produced a man
at a laptop. An avatar cover keeps the generic template, where a person genuinely belongs.

**The vision gate.** `inspect_render_quality(..., surface="newsletter")` adds a rejection bullet for
the literal "person at laptop/desk/keyboard with notebook or coffee mug" scene and raises the
relevance floor to 4 (`_NEWSLETTER_MIN_RELEVANCE`) — a render merely IN THE SAME DOMAIN as the
edition (a laptop for an AI-cost topic) used to clear a bare "it relates" relevance-3 bar every
time. Still fails OPEN on a vision outage.

**Receipts (issue #1377's pattern, extended).** `generate_cover_for_edition` writes a
`<stem>.brief.json` sidecar beside every STORED cover — real brief and deterministic fallback
alike — via `media_provenance.write_brief_receipt(cover_public_url(relative), brief, ...,
extra={"edition_id": ..., "edition_format": ..., "hook_style": ...})`. `write_brief_receipt`'s
`extra` param merges surface-specific fields on top of the generic `ImageBrief` fields, so the one
receipt shape stays generic rather than growing a per-surface schema. Nothing is written for a
render that never gets stored (a failed generation, a rejected gate verdict).

**Image guidance (issue #1890):** the cover editor's "Image guidance" field is free-text direction
for THIS render only — separate from the edition's "Added Guidance" field, which steers the article
text and never reaches here. It rides `NewsletterCoverRequest.guidance` →
`generate_newsletter_cover` → `generate_cover_for_edition(..., guidance=...)` straight into
`build_image_brief`'s existing `extra_direction` param — no new prompt helper, per the image-stack
invariant above. It is one-shot, never persisted on the edition row.

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

### Re-running the five-edition sample set

The golden set behind Box 1-4's acceptance criteria — the five real editions from issue #1992
(routing switch, silent multi-agent failure, cost audit, overlooked costs, budget leak) — lives as
fixtures in `tests/unit/utilities/ai/test_image_brief.py::_FIVE_FIXTURE_EDITIONS` (mocked LLM
responses; no spend). To regenerate real sample covers for a human quality check against those same
five editions (real spend against `lem-simple`/`lem-medium`/`lem-image`/`lem-vision`): call
`generate_cover_for_edition` once per edition with that edition's real title/subtitle/body (or the
fixtures' text), inspect the `.brief.json` written beside each render for `focal_concept` and
`fallback`, and do **not** call `set_edition_cover_image` against a live edition row — the author
approves covers from the review queue, and this path is for a sample review only.

Point `cqc_lem.assets_dir` (and `newsletter_cover.assets_dir`, which binds it at import) at a
scratch directory for the run: the cover file and its receipt both land there, the variety window
then reads that run's own receipts, and the live assets volume is never written.

**Read `fallback` on every receipt before judging the images.** The first live run of this set
looked like five renders of the new pipeline and was actually four renders of the deterministic
template — the receipt was the only thing that said so.

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
