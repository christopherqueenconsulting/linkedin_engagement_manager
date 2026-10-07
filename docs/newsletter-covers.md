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

`generate_cover_for_edition` runs the SHARED staged image engine (`docs/image-stack.md` → "The
staged engine") rather than any parallel per-content-type helper:

1. **Stage 1 — read the whole edition.** `image_concept.analyze_content_for_image` gets the
   subtitle + the FULL body, with the title passed separately (so a hook that only restates it is
   refused). It returns the edition's thesis, its FACTS (`specific_entities` — names, numbers,
   never drawn), its depictable `visual_anchors`, a hook, and the treatment: `people_scene` (the
   cover default — real faces beat clipart), `editorial_graphic` (only when one number or contrast
   IS the thesis), `concrete_scene`, or — only when nothing concrete exists —
   `metaphor_last_resort`. It also gets `recent_treatments` from the last 3 receipts
   (`_recent_cover_treatments`, fallbacks included) and prefers a different one; then
   `enforce_graphic_cap` allows at most ONE `editorial_graphic` in any 3 consecutive covers.
2. **Stage 2/3 — the brief.** `build_image_brief(title + subtitle + body, surface="newsletter",
   ratio=COVER_IMAGE_RATIO, avatar=..., concept=..., content_shape=..., extra_direction=guidance,
   avoid_terms=_recent_cover_signals(user_id))`. The author sees the use case (a 16:9 cover that
   must read at 400×225, key content in the central 60%), the treatment template, the concept and a
   3000-char excerpt; stock symbols (`CLICHE_OBJECTS`) are refused, at least two visual anchors
   must be depicted, no fact's name or number may appear, nothing may be "titled"/"labelled"/
   "displaying" content, and only an `editorial_graphic` may carry text — its hook, exactly.
3. **Stage 4 — the blind judge.** `render_avatar_image_gated` when an avatar is resolved, else
   `render_image_gated`, both with `concept=brief.concept, hook_text=brief.hook_text`. **The avatar
   path gets every stage too** — it used to skip the newsletter gates entirely
   (`newsletter_gate = surface == "newsletter" and not avatar`). An avatar always takes
   `people_scene`.

### Why the metaphor extractor is gone (issue #2241)

User 1's covers were pipes, valves, gears and gauges whatever the edition said. The cover path
REQUESTED that: `_extract_cover_concept` asked `lem-simple` for "the title's promise reduced to a
PHYSICAL METAPHOR — a switch, a leak, a scale, a chain, a valve"; the `newsletter` preset offered a
closed menu of plumbing / measuring / mechanical / routing / tool objects; the fallback was a brass
valve on a workshop surface; and `_NO_ANONYMOUS_PERSON` plus the stock-office gate forbade the one
thing LinkedIn guidance says outperforms symbols — real people. The brief author then saw only the
title, three candidate objects and one mechanism sentence, and the vision gate graded the render
against the brief's own focal concept, so a valve scored 5/5 against "a valve symbolising leaks".

Deleted with it: `_extract_cover_concept`, `_cover_concept_text`, `_focal_objects`,
`_recent_focal_objects`, `_drop_topic_words`, the dead `build_cover_prompt`, and in `image_brief`
the `METAPHOR_FAMILIES` / `METAPHOR_OBJECTS` vocabulary, the family-aware repeat gate (#2000/#2241's
first fix), `_NEWSLETTER_FALLBACK_SCENE` and `_NO_ANONYMOUS_PERSON`. The object/family repeat gate
could only push the author from one metaphor to the next; with every stock symbol refused outright
and the brief grounded in the article, variety comes from the articles themselves.

What survives from #1992/#2008: the edition's PAYOFF is what the image is built from (now by
reading the whole body rather than extracting a metaphor), a **retry carries its rejection
reason**, `content_shape` distinguishes a listicle cover from a personal-story one, and the person
at a laptop with a coffee and notebook is still refused — now as entries in `CLICHE_OBJECTS`, on
every surface. A bare `laptop` or `desk` is no longer refused: a concrete scene may genuinely
contain one.

### Cross-edition variety

`_recent_cover_signals` reads the user's last `_VARIETY_WINDOW` (5) cover receipts, most-recent
first, straight off the `.brief.json` sidecars (no DB column), skipping fallback receipts, and
renders each as `"<treatment>: <focal concept>"`. It reaches `build_image_brief`'s `avoid_terms`,
which is now a SOFT steer to the author only — "make this one visibly different in setting and
composition". It never changes the treatment Stage 1 chose, never bans the edition's own entities,
and so can never force a metaphor. It never reaches the deterministic fallback, whose template is a
render prompt.

The window only ever sees receipts for covers still on disk (issue #2010): `remove_cover_file`
deletes a cover's `.brief.json` sidecar in the same call that deletes the cover image, so a
regenerated or replaced cover stops steering variety the moment its file is gone. Deliberate — a
superseded regeneration is something this account tried and moved away from.

### Gauntlet round 1 (the four real user-1 covers)

Relevant now, all four still failed: every edition picked `editorial_graphic`; the entities were
names and numbers, so specificity scored 1 and the renders carried garbled names ('Originality ai',
'2013 Eletinain Lintedin…') or nonsense props; '$30K' sat beside a hook at text_accuracy 5; a stack
of $100 bills passed; the hooks shouted ("Stop wasting your budget!"); and ed16 fell back to "a
photographic cutout of $30K". Each became an engine rule, not a per-edition tweak — facts vs
anchors, the name and words-on-surface rejections, the blind stray-text cap, the treatment cap and
cover bias, the money clichés, the hook rules, and a fallback that is always a photograph and
records every rejection (`docs/image-stack.md`).

### Gauntlet round 2 (overcorrected into stock)

Clichés and stray text were gone, but every cover was people at laptops or holding paper with a
neutral face, no hook, no brand color — all REJECTED at specificity 2. Round 3's engine answer:
every cover now carries its hook as the headline (composited since round 6), the visual must carry the
emotional beat on a face, Stage 1 proposes three visual ideas that one cheap call ranks, the judge
grades headline + image together with `scroll_stop ≥ 4` and `brand_fit ≥ 3`, and every cover names
one brand-color accent (`docs/image-stack.md`).

### Gauntlet round 3

Gold, a hook and visible emotion on all four; ed18 and ed19 accepted. The rest failed on a token
budget a reasoning model ran out of, profile boilerplate leaking into a fallback render prompt,
a readable "billing dashboard" and "paper check", a serif hook, a Title Case hook close to the
title, a broad smile for a "relief" beat, and a generic flip chart the judge scored 5 for
specificity. Each became an engine rule (`docs/image-stack.md`, "the round-4 rules"); covers now
also render two candidates per attempt and keep the better.

### Gauntlet round 4

ed19 scored 5 across the board. ed16 was correctly rejected (stray text on a paper prop, a neutral
half-smile on both candidates); ed17 and ed18 were held at specificity 3 because the reuse test
looked at the image without its headline, and ed18's "45% less engagement" never said what got
less. Round 5 judges the cover as headline + image, requires a numeric hook to name its subject,
keeps paper off covers (a blank sheet seen edge-on when the idea is about a document), and carries
an emotion-intensity directive into every later candidate and retry.

### Gauntlet round 5 and the blind critic → composited headlines

1 of 4 covers accepted; the quality was good but every cover shared one composition (dark panel
left, a reacting man in his 30s-40s right), and an independent blind critic traced the remaining
defects — drifting panel and type colors, thin weights, clipped headlines, "AI X: N% Y" everywhere,
hook words the article never used — to gpt-image drawing the headline. The render now carries no
text; `image_compose` typesets the hook in the exact brand colors and the bundled Montserrat
ExtraBold, in a rotated layout. Cast, hook shape and layout each rotate least-recently-used over
the last receipts (`_recent_concept_field`), hooks must use the article's own words, emotion
follows the piece's valence and must be authentic, and the judge grades the composite against
anchored specificity descriptors (`docs/image-stack.md`, "The headline is composited"). The stored
cover is the COMPOSITE; the raw render's path rides `render_info["raw_render_path"]`.

### Gauntlet round 6 → the editorial cover

Compositing worked (exact colors, the brand font, rotating layouts, a varied cast); what remained
was type too small for a thumbnail, a `full_bleed` headline across a face, a lowercase hook,
props still carrying stray text, "relief" rendered as pain, and both judges calling every scene a
generic office. The cover is now a kicker + hero numeral + headline + byline system with a
cap-height floor and a growing backing; props are refused on every surface; and the byline is the
newsletter title when the caller passes `signature`, else the author's profile name
(`docs/image-stack.md`, "The cover is an editorial system").

### Gauntlet round 7 → split covers

Type still landed on faces, laptops and server rooms crept back in, and the judge capped
specificity for not showing props the brief refuses. Covers are now a type panel BESIDE a square
scene (`split_left`/`split_right`), screens are banned on every surface, anchors that are props
are dropped at parse time, the anchor checks are advisory only, and the avatar renders only when
the concept fit rule says the piece is about the author (`docs/image-stack.md`, "Split layouts,
square scenes, no screens").

### Gauntlet round 8 → no painted labels

Split covers held (three of four accepted); ed17 failed on a painted poster caption and two
covers had no kicker. Renders now refuse every label-carrying surface, the brief describes people
by appearance rather than by group caption, a missing kicker is derived from the piece, and a
comparative hook must say "than" or carry a number (`docs/image-stack.md`, "No labels, a kicker
always, anchored comparatives").

### Gauntlet round 9 → the render is only a photograph

Two covers failed only on stray text: told it was a "LinkedIn newsletter cover", the renderer
typeset a fake cover into the empty side of the scene. The render prompt now describes only a
photograph, checked deterministically and repaired before it ships (`docs/image-stack.md`, "The
render is only a photograph").

### Gauntlet round 11 → the blind critic

Every cover passed the in-pipeline judge, but the blind critic scored most items 2-4 on
specificity and scroll stop: a warehouse backdrop whatever the topic, the lead stat missing from
the headline, a caveat or a neutral "vs" as the headline, symbolic props, overacted faces and
primary-colour objects. Each became an engine rule, and covers are now built at 1920x1080
(`docs/image-stack.md`, "What the blind critic asked for").

### Gauntlet round 12 → stats, framing, smiles

ed17 led with a model version ("4.5"), a post dropped its 60% stat, every scene held a mug in a
medium shot at a table, and ed16's "$30K saved" rendered alarmed. Headline numbers are now
stats in the source's casing and must survive into the hook, hooks must assert with a verb, mugs
are props, the framing rotates, and good news is a literal relaxed smile (`docs/image-stack.md`,
"Stats, not versions; framing rotates; good news smiles").

### Gauntlet round 13 → true hooks

Images held, but a hook paired a number with the wrong claim ("53.7% less engagement" — 53.7%
was the share of AI posts), one was ungrammatical and two ran past six words. A number now must
share a source sentence with its claim, a judge checks grammar and truth against the cited
sentence, and six words is a hard cap (`docs/image-stack.md`, "A hook is true, grammatical and
short").

### Gauntlet round 14 → hooks are built, never cut

All four covers passed; the hook defects were deterministic: a mid-phrase cut, "$30k" for
"$30K", and a label fallback. Long hooks are regenerated, then built as a complete clause;
numbers carry the source spelling; and no label survives any path (`docs/image-stack.md`,
"Never truncate a hook").

### Rejections land for review, with their reason

A definite judge failure on `no_cliche` or `specificity` after the attempt budget returns verdict
`rejected` — and the cover still lands `pending_review`, since the author is the publish gate. The
reason travels: `render_info` carries `gate_rubric`, `gate_failing`, `gate_issues` and
`gate_blind_description` (what a viewer who knew nothing saw), and `generate_cover_for_edition`
copies them onto the cover's receipt (`_GATE_RECEIPT_KEYS`) and logs the rejection at INFO.

**Receipts (issue #1377's pattern, extended).** `generate_cover_for_edition` writes a
`<stem>.brief.json` sidecar beside every STORED cover — real brief and deterministic fallback
alike — via `media_provenance.write_brief_receipt(cover_public_url(relative), brief, ...,
extra={"edition_id", "edition_format", "hook_style", + the gate fields above})`. From the brief
itself it records the prompt, `focal_concept`, `fallback`, and the staged fields: the Stage 1
`concept` (as its fields), `treatment`, `required_entities`, `hook_text`, `prompt_check` and
`rejections` (every rejected attempt's reason — why a fallback fell back).
Nothing is written for a render that never gets stored (a failed generation). `remove_cover_file`
is the matching teardown, deleting the receipt alongside the cover it describes.

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
fixtures in `tests/unit/utilities/ai/test_image_brief.py::_FIVE_EDITIONS` (a grounded concept and
an entity-built brief per edition; `_OLD_METAPHOR_BRIEFS` pins that the old still-lifes are now
refused — mocked LLM responses, no spend). To regenerate real sample covers for a human quality check against those same
five editions (real spend against `lem-medium`/`lem-simple`/`lem-image`/`lem-vision`): call
`generate_cover_for_edition` once per edition with that edition's real title/subtitle/body (or the
fixtures' text), inspect the `.brief.json` written beside each render for `fallback`, `treatment`,
`concept`, `required_entities` and the judge's `gate_rubric` / `gate_blind_description`, and do **not** call `set_edition_cover_image` against a live edition row — the author
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
