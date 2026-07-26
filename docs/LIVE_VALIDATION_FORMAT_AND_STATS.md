# Live Validation — Document Post Path & Saves/Impressions Scraping

**Spike:** R1 / issue #404 — "Live LinkedIn validation: document post path + saves/impressions scraping"
**Grounds:** C1 (#390, native document/PDF posts) and B2 (#387, capture reposts/impressions/saves)
**Date:** 2026-07-26 · **Probe:** `scripts/linkedin_live_validation.py` (read-only)

## TL;DR

| Question | Answer | Evidence grade |
|---|---|---|
| Document post publish path — **API or UI**? | **API.** Versioned `/rest/documents` → `/rest/posts`. No Selenium composer is used, or needed. | **Documented + shipped** (R3 #406 against `li-lms-2026-07`; `poster.py`) |
| Current **SDUI anchors for document upload** | **None exist in LEM and none are invented here** — the UI path is not on the publish route. Capturable on demand via `--probe-composer`. | **Deliberately unmapped** |
| Does a published document render as a **document card**? | Unconfirmed — needs one live post. | **OPEN** (C1's own acceptance) |
| Are **saves** scrapeable? | **Yes**, own posts only, on `/analytics/post-summary/<activity-urn>/`. | **Live grab 2026-07-23** (encoded in `_stacked_counts`) |
| Are **impressions** scrapeable? | **Yes**, same page (Discovery hero, value-first layout). Blank on the post detail view. | **Live grab 2026-07-23** |

**Net scope change: none for B2, one live confirmation left for C1.** Both features are built and
merged; this spike's job was to say whether their assumptions hold, and they do. What is genuinely
unresolved is a single end-to-end act — publish one native document and look at it — which needs a
real account and so is the owner's call, not the agent's.

> **Honesty note.** This note was written headless: no LinkedIn session was driven. Every row above
> carries its evidence grade, and no DOM selector appears here that was not either (a) already in the
> repo or (b) captured in the 2026-07-23 owner grab. Rather than guess at the document-composer
> anchors, the probe script captures them on a live session in one command.

---

## 1. Document posts publish through the API, not the UI

`share_document_on_linkedin` (`utilities/linkedin/poster.py:439`) is the whole path, and it is HTTP
only:

1. `POST /rest/documents?action=initializeUpload` → `uploadUrl` + `urn:li:document:…` (`upload_document`)
2. `PUT` the PDF bytes to `uploadUrl`
3. `POST /rest/posts` with `content.media = {title, id: <document urn>}` (`_create_document_post_versioned`)

with `_create_document_post_legacy` (assets upload + `ugcPost` `shareMediaCategory=DOCUMENT`) as the
fallback for a token whose app is not provisioned for the versioned API. Routing is wired end to end:
`PostType.DOCUMENT` (`db.py:186`, migration `V20260723234736`), the content-plan balancer
(`run_content_plan.py:65`), and the publish switch (`run_automation.py:5172`).

**Why there is no SDUI anchor map for document upload.** LEM never opens the composer to publish a
document, so no anchors exist to drift — the failure modes are HTTP ones (`426` retired version, `403`
unprovisioned app), and both are already handled: an explicit 426 `log_error` in `upload_document`,
plus the weekly `scripts/linkedin_version_check.py` cron that keeps `LI_API_VERSION` inside the live
window. Writing a speculative composer selector chain here would create the maintenance burden it
claims to remove. If a UI fallback ever becomes necessary (app deprovisioned with no versioned path
left), `--probe-composer` dumps the composer's real control labels in one run — see §4.

**What is still open for C1** is not the path but the *product* claim behind it: that the API-published
deck renders in-feed as a swipeable **document**, not a multi-image share. `probe_document_render`
answers this by capturing the media anchors of a published post (§4, check 2). It needs one document
post to exist on the account first — that publish is C1's acceptance step and is human-gated
(`risk:live-linkedin`). Tracked as **#644**.

## 2. Saves and impressions are scrapeable — on the author's analytics page only

| Signal | Post detail page | `/analytics/post-summary/<urn>/` | Stored as |
|---|---|---|---|
| reactions | ✅ social bar | ✅ Engagement breakdown | `post_stats.reactions` |
| comments | ✅ social bar | ✅ Engagement breakdown | `post_stats.comments` |
| reposts | ✅ social bar ("reposts", older UIs "shares") | ✅ Engagement breakdown | `post_stats.reposts` |
| **impressions** | ❌ not rendered | ✅ **Discovery hero** | `post_stats.impressions` (nullable) |
| **saves** | ❌ not rendered | ✅ Engagement breakdown | `post_stats.saves` (migration `V20260723211520`) |

Three properties of that page, all from the 2026-07-23 owner grab and already encoded in
`run_automation.py`:

- **The URN differs from the permalink's.** The logged permalink carries a `share`/`ugcPost` URN;
  analytics keys off the **activity** URN LinkedIn redirects to. `_post_analytics_counts` therefore
  resolves the URN from `driver.current_url` *after* the redirect, falling back to the stored URL.
- **Label and value are separate elements, in opposite orders.** The Discovery hero reads value-first
  (`72` / `Impressions`); the Engagement breakdown reads label-first (`Reposts` / `0`). `_stacked_counts`
  pairs a label line only with an adjacent **bare count** line, which is what keeps prose like
  "Save this checklist" out of the numbers.
- **Merge by max, never overwrite.** `auto_scrape_post_stats` takes `max(detail, analytics)` per signal
  so a view that does not render a signal cannot zero out one the other view did.

**Access limits worth knowing:** the analytics page exists only for the **author's own** posts, so
saves/impressions are unavailable for third-party feed posts (`_post_social_counts` returns 0 there —
correct, not a bug), and it is a Selenium read, so it rides the same 429 breaker as the rest.

**Follow-up lead (not verified here):** R3 recorded that LinkedIn's `202506` Community Management
version added **member post stats**. If that surface exposes impressions/saves for the authenticated
member, it would replace this Selenium scrape with an API read that no SDUI churn or 429 can break.
That is a token probe, not a browser session — cheap to settle, and worth settling before investing
further in scraping. Filed as **#645** rather than folded into this spike.

## 3. Selector map (with provenance)

Nothing below is a guess; the provenance column says where each came from.

| What | Locator | Used by | Provenance |
|---|---|---|---|
| Post detail / analytics container | `By.TAG_NAME, "main"` | `auto_scrape_post_stats`, `_post_analytics_counts` | in-repo, live-used |
| Analytics page | `https://www.linkedin.com/analytics/post-summary/{activity_urn}/` | `_ANALYTICS_URL` | live grab 2026-07-23 |
| Impressions row | line pair `<count>` → `Impressions` (value-first) | `_stacked_counts` / `_STACKED_VALUE_FIRST` | live grab 2026-07-23 |
| Reactions/comments/reposts/saves rows | line pair `<Label>` → `<count>` (label-first) | `_stacked_counts` / `_STACKED_LABEL_FIRST` | live grab 2026-07-23 |
| Social-bar counts (detail view) | regex `<count> <label>` on the card text | `_COMMENTS_RE` … `_SAVES_RE` | in-repo, live-used |
| Post permalink on a feed card | `a[href*='/feed/update/']` | `_post_permalink_from_card` | in-repo, live-used |
| Feed composer trigger | `//button[contains(normalize-space(),'Start a post') or contains(@aria-label,'Start a post') or contains(@aria-label,'Create a post')]` | `auto_post_to_group`, `probe_composer` | in-repo, **best-effort / unvalidated** (F2 #403) |
| Document upload control | — | — | **unmapped by design** (§1); `--probe-composer` captures it |
| Document media card | — | — | **unmapped**; `probe_document_render` captures it |

## 4. Running the live validation

`scripts/linkedin_live_validation.py` is read-only: it navigates and reads, publishes nothing,
comments on nothing, and changes no settings. `scripts/` is not baked into the image, so pipe it in
on stdin exactly like the weekly version probe does:

```bash
sudo docker exec -i celery_worker_selenium python - \
    --user-id 1 --post-url 'https://www.linkedin.com/feed/update/urn:li:activity:<id>/' \
    < scripts/linkedin_live_validation.py
```

It emits one JSON report:

1. **`document_render`** — the media anchors the post renders (`data-testid` / `class` / `aria-label`)
   plus a `document` / `image` / `unknown` verdict. Run it against a **document** post to close C1's
   acceptance, and against an existing carousel post first as a control.
2. **`post_stats`** — per signal, whether a non-zero value came from the detail page, the analytics
   page, both, or neither, plus the raw label neighbourhoods from each page so a `0` can be read as
   "the post has none" versus "the layout drifted".
3. **`composer`** *(only with `--probe-composer`)* — the composer's control labels and any
   document-upload affordance among them. It opens the composer and closes it with Escape; nothing is
   attached or posted.

Paste the JSON into #404. If `post_stats` shows a signal sourced from `none` while the analytics page
plainly shows a number, the `*_lines` arrays contain the drifted layout and this note's §3 rows are
what need updating.

## 5. Scope updates

**C1 (#390) — native document posts:** implementation complete (publish path, `PostType.DOCUMENT`,
balancer, migration). Remaining: publish one document post on the live account and run check 1 to
confirm it renders as a document card — tracked as **#644** (`risk:live-linkedin`, `priority:high`),
so R1 (#404) closes with this note. No code change is expected; if the verdict comes back `image`,
the legacy fallback silently took over and the finding is an API-provisioning bug, not a format bug.

**B2 (#387) — reposts/impressions/saves:** implementation complete and consistent with the live
layout as of 2026-07-23. Remaining: none from this spike. Split out as separate issues:
**#645** probes the `202506` member-post-stats API as a churn-proof replacement for the analytics
scrape, and its "keep the scrape" outcome folds the analytics-page rows into the F2 (#403)
selector-liveness sweep so drift is detected by cron rather than by a run of zeroes.

## Sources

- `docs/FORMAT_API_FEASIBILITY.md` (R3 / #406) — API surface for documents, articles, newsletters.
- In-repo: `utilities/linkedin/poster.py` (document API path), `app/run_automation.py`
  (`_post_social_counts`, `_stacked_counts`, `_post_analytics_counts`, `auto_scrape_post_stats`),
  `utilities/db.py` (`record_post_stats`, `PostType`).
- Migrations `V20260723211520__add_post_stats_saves.sql`, `V20260723234736__add_document_post_type.sql`.
