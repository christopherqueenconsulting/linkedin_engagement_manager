# Curated outside sources

Phase 1 of #2260. LEM drafts a post that comments on SOMEONE ELSE'S content and credits them: a
LinkedIn post reshared with our commentary, a figure re-charted from a source, or a link to an
article. The research behind every rule here is [curated-sources-research.md](curated-sources-research.md).

**OFF by default** (`CURATED_SOURCES_ENABLED`, PostHog flag `curated-sources-enabled`). With it off
nothing is collected and no slot is ever curated. **Every curated draft is held PENDING for its
owner**, and only an approval a human made can publish it.

| | |
|---|---|
| The ONE place (rules) | `utilities/curated_sources.py` — guardrails, credit, ceiling, little-text escaper, allowlist loader |
| Collectors | `utilities/curated_collectors.py` — LinkedIn hook, RSS, BLS, manual paste |
| Writer | `utilities/ai/curated_commentary.py` — commentary through the content core; re-chart figures |
| Publisher | `utilities/linkedin/reshare.py` — versioned `/rest/posts` helpers beside `poster.py` |
| Orchestration | `app/run_curated_sources.py` — collector beat, slot drafter, publish gate |
| API | `api/routers/curated.py` — `/api/curated/sources`, `/api/curated/source` |
| Storage | `curated_sources` table; `posts.curated_source_id`, `posts.source_treatment` (`V20261007070433`) |
| Allowlist | `src/cqc_lem/resources/curated_feeds.json` (a PROPOSAL), or `CURATED_FEEDS_PATH` |
| Telemetry | `curated_source` event: `stage` (collect/draft/publish), `status`, `platform`, `treatment`, `reason` |

## Owner decisions (2026-10-07)

- **Reshare scope:** anyone relevant — peers, competitors, customers, brands.
- **Ceiling:** at most ONE curated post in any THREE consecutive posts, and only in a `value` or
  `authority` slot of the 70/20/10 mix, never `promo`.
- **Politics is an ABSOLUTE filter**, on every platform, with no approval override: partisan or
  electoral content, named politicians, and regulators' statements. Truth Social, Reddit and Threads
  are excluded outright.
- **Screenshots: never.** There is no screenshot treatment and no code path that captures one.
- **X:** no paid reads. Paste an X (or any) URL with its author and text through `POST
  /api/curated/source`.
- **No heads-up DM** to the original author. **No Gartner/Forrester** content or citations (no client
  access, and their terms forbid quoting).
- **Government-data charts still need approval.** Nothing auto-approves in phase 1.

## Flow

1. **Collect.** The `collect-curated-sources` beat (00:45 UTC, before the 01:30 content run) reads
   each allowlisted feed ONCE and the BLS watchlist, and records candidates for every active user
   with the flag on. The feed and roster lanes record the post they are about to engage
   (`record_linkedin_candidate`, home feed only, never group feeds). Every candidate goes through
   `screen_candidate` and is stored `new` or `blocked` with its reason — a blocked row is kept as
   evidence the filter ran, and the `(user_id, url_hash)` key stops a re-collected URL being offered
   again.
2. **Draft.** `create_content` asks `curated_content_for_slot` before writing a TEXT post. It claims
   the slot only when the flag is on, the slot is `value`/`authority`, and no curated post sits within
   two posts either side (`get_curated_neighbors` + `ceiling_allows`). It re-runs the block gate,
   requires provenance (URL, a named author or publisher, a snapshot), picks a treatment, writes the
   commentary, and screens our OWN commentary with the political filter too. A slot it does not
   claim gets the ordinary post, unchanged.
3. **Hold.** `_may_auto_approve` returns False for every curated post, so the draft lands PENDING
   whatever `auto_schedule_posts` says. For a flag-on user an unreadable "is this curated?" also
   holds the post (fail closed, costs a click).
4. **Approve.** The owner approves in the Content Studio (the card shows the source, treatment and
   the exact credit line) or with `PUT /api/curated/source {action: "approve"}`. Either records
   `approved_by = 'user:<id>'`.
5. **Publish.** `post_to_linkedin` routes a curated post through `curated_publish_refusal`: an
   approval no human made (`system:*` or none) sends it back to PENDING; a source the guardrails now
   block, or a treatment the source no longer allows, holds it at ERROR. Otherwise
   `publish_curated_post` re-applies the `Source:` line (an owner edit cannot strip the credit) and
   publishes by treatment.

## Treatments

| Treatment | When | Mechanics |
|---|---|---|
| `reshare` | A LinkedIn post whose share/ugcPost URN we hold | `/rest/posts` + `reshareContext.parent`; LinkedIn embeds and credits the original. An `urn:li:activity` is refused before any request (`build_reshare_body`). A 4xx is `ReshareRefused`: the source is blocked, never retried |
| `rechart` | Figures that validate against the SOURCE text, under a licence that permits a derivative (`RECHART_LICENCES`) | `image_graphics` stat_card / highlight_chart. `validate_graphic_facts` keeps only figures that are their sentence's own substring; `render_graphic` re-checks before drawing and refuses (`UngroundedFactError`). The card carries `Source: <publisher>, <title>`; the body carries the credit line |
| `link` | Everything else with a URL (including a LinkedIn post we cannot reshare — `link_only`) | Article card; the thumbnail is the publisher's `og:image`, fetched over https, ≤5 MB, uploaded unmodified. A thumbnail failure costs the image, never the post |

Licences: `public_domain` (BLS/Census), `cc_by`, `cc_by_sa`, `facts_only` and `editorial` may be
re-charted; `cc_by_nd`, `linkedin_native` and `unknown` may not; `cc_nc` is blocked outright.

## Guardrails (`screen_candidate`, collect AND draft AND publish)

In order — the first that fires is the stored `block_reason`:

1. `excluded_platform` — truthsocial.com, reddit.com / redd.it, threads.net / threads.com.
2. `analyst_terms` — gartner.com / forrester.com, or any text citing Gartner or Forrester.
3. `political` — a term list (electoral, partisan, offices, regulators, named politicians,
   culture-war topics) over title, excerpt, author, publisher and URL. Case-sensitive for acronyms
   whose lowercase is an ordinary word (`SEC`, `GOP`). It errs towards blocking; a false positive
   only costs a candidate.
4. `paywall` — the collector's flag, or a paywall marker in the excerpt.
5. `licence_nc` — any NonCommercial licence.

Draft time adds `no_provenance` (no URL, no named author/publisher, or no snapshot) and refuses a
commentary that states a number the source does not, is under 280 characters, left a `[[...]]`
placeholder, or trips the political filter itself.

## The commentary

Written in the author's voice through the shared content core (`voice_reference`,
`alignment_directive` with the slot's mix class, `post_writing_directive`, `style_directive`,
`hashtag_directive`), then the slop lint with its bounded repair (`lint_repaired`), then the facts
rule with the source snapshot as the ONLY anchors (`fact_grounding_report`), one steered retry. It
must name the source in its own words ("X found…"); the `Source: Author, "Title", Publisher.` line
is appended deterministically by `with_credit`. No URL goes in the body — the link card carries it,
and the link-in-first-comment split would otherwise move it.

Every `/rest/posts` commentary on this path is escaped with `escape_little_text` (reserved
`\ | { } @ [ ] ( ) < > # * _ ~`; a `#word` becomes the `{hashtag|\#|word}` template). Existing
API posts are NOT changed here — #2261 tracks that.

## The allowlist

`src/cqc_lem/resources/curated_feeds.json` is a **proposal**: 19 feeds (AI-vendor and SMB-tool
newsrooms, four reputable AI newsletters, BLS and Census release feeds), each answering 200 with an
RSS/Atom body on 2026-10-07, plus two BLS series (average hourly pay, unemployment rate). Edit it,
or point `CURATED_FEEDS_PATH` at an edited copy on the box. A feed that stops answering logs one
WARNING per run; an unreadable file collects nothing and says so.

## Still owed — live verification

**Member-token reshare is UNTESTED.** LinkedIn's docs show only an organization author for a reshare;
a member author is the same body with `urn:li:person:{sub}` and the docs list no separate scope, but
nobody has run it. Do NOT enable the flag for real use until this one-shot test passes. After
deploy, as the owner or the main session:

1. Pick a PUBLIC LinkedIn post by someone else and copy its share or ugcPost URN. On the post, use
   "Copy link to post": the URL often carries `urn:li:share:N` or `urn:li:ugcPost:N`. An
   `urn:li:activity:N` link will NOT work as a parent.
2. Run it as user 1 in a prod-image container (the deployed tag, with the prod `.env`), e.g. `docker exec -it celery_worker python`:

   ```python
   from cqc_lem.utilities.linkedin.reshare import share_reshare_on_linkedin
   urn = share_reshare_on_linkedin(1, "Testing a reshare with commentary from LEM. "
                                      "Source: <author>, LinkedIn.", "urn:li:share:<N>")
   print(urn)
   ```
3. Pass: a `urn:li:share:` / `urn:li:ugcPost:` comes back AND the permalink
   `https://www.linkedin.com/feed/update/<urn>/` shows the original embedded under the commentary.
   Then delete the test post from the LinkedIn UI.
4. Fail: `ReshareRefused` (a 4xx). Record the status and response body on #2260; the feature then
   ships reshare-less (re-chart and link only) until resolved.

Also unverified live: the link-post article card with an uploaded thumbnail, and whether the feed
container exposes share URNs often enough to make reshares common (most cards expose only the
activity URN, so most LinkedIn candidates will be `link_only`). The `curated_source` event's
`treatment` breakdown will show it.

## Plain text, same budget as a text post (#2241 showcase C)

LinkedIn renders no markdown, and `escape_little_text` would print `**bold**` as literal asterisks.
Every commentary draft goes through `curated_commentary.finish_post_text` (`sanitize_for_linkedin`
then `shape_for_dwell`, the text post's own finish), and `escape_little_text` sanitizes again before
escaping. Length follows the shared `post_writing_directive` budget (1300-2000 characters).
