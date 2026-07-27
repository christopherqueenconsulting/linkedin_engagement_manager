# Live Validation — Document Post Path & Saves/Impressions Scraping

**Spike:** R1 / issue #404 — "Live LinkedIn validation: document post path + saves/impressions scraping"
**Grounds:** C1 (#390, native document/PDF posts) and B2 (#387, capture reposts/impressions/saves)
**Date:** 2026-07-26 · **Probe:** `scripts/linkedin_live_validation.py` (read-only)
**Updated:** 2026-07-27 (#645) — §2a settles the member post-stats API lead ·
**Probe:** `scripts/linkedin_post_stats_api_probe.py` (read-only, no browser)

## TL;DR

| Question | Answer | Evidence grade |
|---|---|---|
| Document post publish path — **API or UI**? | **API.** Versioned `/rest/documents` → `/rest/posts`. No Selenium composer is used, or needed. | **Documented + shipped** (R3 #406 against `li-lms-2026-07`; `poster.py`) |
| Current **SDUI anchors for document upload** | **None exist in LEM and none are invented here** — the UI path is not on the publish route. Capturable on demand via `--probe-composer`. | **Deliberately unmapped** |
| Does a published document render as a **document card**? | **CONFIRMED 2026-07-27** — verdict `document`; carousel control `image`. | **CLOSED** (#644) |
| Are **saves** scrapeable? | **Yes**, own posts only, on `/analytics/post-summary/<activity-urn>/`. | **Live grab 2026-07-23** (encoded in `_stacked_counts`) |
| Are **impressions** scrapeable? | **Yes**, same page (Discovery hero, value-first layout). Blank on the post detail view. | **Live grab 2026-07-23** |
| Can an **API** replace that scrape? | **The endpoint exists** (`memberCreatorPostAnalytics`, impressions + saves + more) — but it needs `r_member_postAnalytics`, which **LEM's token does not request**. Scrape stays. | **Documented** (`li-lms-2026-07`) + **in-repo verified** (§2a) |

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

### 2a. The member post-stats API exists and returns exactly these signals — LEM's token cannot read it

R3's follow-up lead is now settled on the API side and **blocked on a permission**, not on a
capability. `GET /rest/memberCreatorPostAnalytics` returns per-post analytics for the
**authenticated member's own** posts over plain HTTP — no browser, no DOM, no Selenium lane, and
immune to the 429 breaker:

```
GET https://api.linkedin.com/rest/memberCreatorPostAnalytics
    ?q=entity&entity=(share:urn%3Ali%3Ashare%3A<id>)&queryType=IMPRESSION&aggregation=TOTAL
LinkedIn-Version: <YYYYMM>   X-Restli-Protocol-Version: 2.0.0   Authorization: Bearer <member token>
```

| Signal LEM stores | API `queryType` | Live since | Evidence grade |
|---|---|---|---|
| `post_stats.impressions` | `IMPRESSION` | `202506` (the `q=entity` finder) | **Documented** — `li-lms-2026-07` |
| `post_stats.reactions` | `REACTION` | `202506` | **Documented** |
| `post_stats.comments` | `COMMENT` | `202506` | **Documented** |
| `post_stats.reposts` | `RESHARE` | `202506` | **Documented** |
| `post_stats.saves` | `POST_SAVE` | **`202604`** — later than the finder | **Documented** |

The same surface also exposes `MEMBERS_REACHED`, `POST_SEND`, `LINK_CLICKS`,
`PREMIUM_CTA_CLICKS`, `FOLLOWER_GAINED_FROM_CONTENT` and `PROFILE_VIEW_FROM_CONTENT` — none of
which the scrape can reach, and the last two of which would answer questions #627 currently
approximates from the profile-analytics pages.

**Why it does not replace the scrape today.** The endpoint requires the
**`r_member_postAnalytics`** permission (a Community Management API product permission). LEM's
authorize call asks for `openid profile email w_member_social` (`api/main.py`, `linkedin_auth_init`),
so **the stored member token has no claim on this endpoint and will answer `403`** — a
scope-and-app-provisioning fact, verifiable in-repo, not a code defect. Granting it needs two
things LEM cannot do for itself: the LinkedIn app approved for the Community Management API, and
**every connected user re-consenting**, because a scope change invalidates the existing grant.
That is an owner decision, tracked as the follow-up below.

**Five details that will bite whoever does the cutover:**

- **The entity URN is the OPPOSITE of the scrape's.** This finder takes a `share` or `ugcPost`
  URN (`entity=(share:…)` / `entity=(ugc:…)`); the analytics *page* keys off the **activity** URN
  that `_post_analytics_counts` resolves by following the redirect. The two ids are **not**
  interchangeable — so the API wants the URN in the logged permalink, which is precisely the one
  the scrape throws away.
- **One `queryType` per request.** Five stored signals means five GETs per post, so a cutover is
  5× the request count of one page load, not 1×.
- **`metricType` changed shape in `202605`** (object → bare string). A parser that understands one
  shape reads a perfectly good response from the other side of that line as empty.
- **`202506` is not enough for saves.** `POST_SAVE` only exists from `202604`. LEM's
  `LI_API_VERSION` default is `202606` (`env_constants.py`), which clears every floor above — but a
  pin rolled back to satisfy something else would silently drop saves as a `400`.
- **`aggregation=TOTAL` is the only one that serves every signal.** Under `DAILY`, LinkedIn does
  not serve `IMPRESSION` for a post entity (nor `MEMBERS_REACHED`, `LINK_CLICKS`,
  `FOLLOWER_GAINED_FROM_CONTENT`, `PROFILE_VIEW_FROM_CONTENT` at all) — and it says so with the
  **same `400` "query type … metric type" shape as a version gap**, so the two are easy to confuse.
  The probe flags the combination itself and names the aggregation ahead of the version, and the
  cutover wants `TOTAL` anyway: `post_stats` stores lifetime counts.

**The probe ships even though it is blocked:** `scripts/linkedin_post_stats_api_probe.py` is
read-only, stdlib-only and needs no browser, and it separates the answers that demand different
fixes — `403` (permission), `426` (retired `LI_API_VERSION`), `400`/absent metric (version predates
the metric) and the `400` that is really an unsupported `aggregation` — then compares whatever it
*does* get against the latest stored `post_stats` row:

```bash
sudo docker exec -i celery_worker python - --user-id 1 --post-id <post id> \
    < scripts/linkedin_post_stats_api_probe.py
```

> **Honesty note.** The rows above are graded **Documented** and **in-repo verified**; the live
> HTTP status is **not recorded here** because this pass ran headless with no access to a member
> token. The instrument is what shipped, so the moment the permission question is answered the
> probe records the real status, response shape and scrape-vs-API deltas in one command — and if
> the answer is "no", it records the `403` as evidence rather than as a hunch.

**Outcome: the scrape stays.** It is the only source available under the permissions LEM actually
holds, and it works. Tracked forward as **#695** (request `r_member_postAnalytics`, then run the
probe and cut over behind the existing merge-by-max), with the analytics-page rows folded into the
F2 (#403) selector-liveness sweep so drift is caught by cron rather than by a run of zeroes.

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
| Follower / connection counts | page text of `main` on the user's own profile → `parse_follower_count` / `parse_connection_count` | `capture_audience_snapshot` (#627) | in-repo, **best-effort / unvalidated** — first real run should be supervised |
| Profile views / search appearances | `https://www.linkedin.com/analytics/profile-views/` (falls back to `/analytics/search-appearances/`), value-first line pair | `capture_audience_snapshot` (#627) | in-repo, **best-effort / unvalidated** |
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

`scripts/linkedin_post_stats_api_probe.py` (§2a) is the **other** read-only probe and needs no
browser at all — it runs on any container with the DB and outbound HTTPS, holds no Selenium lane
and is 429-immune. Run it from the same worktree the same way, `--post-id` being a LEM post id so
it can compare the API against the scrape's own stored row.

## 5. Scope updates

**C1 (#390) — native document posts: CONFIRMED LIVE 2026-07-27 (#644).** A LEM-generated deck was
published through the versioned `/rest/documents` → `/rest/posts` path on the live account and
check 1 returned **`verdict: "document"`**, with the document pager anchors present
(`aria="Go to previous/next page of document"`). The same probe returned **`"image"`** for an
existing carousel control, so the check discriminates correctly. The legacy image fallback did NOT
take over: the app is correctly provisioned for document upload and `LI_API_VERSION=202606` is
current. No provisioning bug; no code change was needed. (The test post was removed from the feed
after probing — deck CONTENT quality is tracked separately in #728.)

**B2 (#387) — reposts/impressions/saves:** implementation complete and consistent with the live
layout as of 2026-07-23. Remaining: none from this spike. Split out as **#645**, which is now
settled (§2a): the member post-stats API exists and would replace the scrape, but LEM's token has
no `r_member_postAnalytics` claim on it, so **the scrape stays** and the API cutover moved to
**#695** behind an owner decision. The "keep the scrape" branch of that outcome is what makes the
analytics-page rows (§3) part of the F2 (#403) selector-liveness sweep — with no API alternative
available, drift on that page has to be caught by cron rather than by a run of zeroes.

## Sources

- `docs/FORMAT_API_FEASIBILITY.md` (R3 / #406) — API surface for documents, articles, newsletters.
- [Member Post Statistics — Microsoft Learn (`li-lms-2026-07`)](https://learn.microsoft.com/en-us/linkedin/marketing/community-management/members/post-statistics?view=li-lms-2026-07)
  — the `memberCreatorPostAnalytics` finders, permission, metric list and error table behind §2a.
- [Recent Marketing API Changes — Microsoft Learn](https://learn.microsoft.com/en-us/linkedin/marketing/integrations/recent-changes)
  — `202506` added the `q=entity` finder; `202605` changed `metricType` from object to string.
- In-repo: `utilities/linkedin/poster.py` (document API path), `app/run_automation.py`
  (`_post_social_counts`, `_stacked_counts`, `_post_analytics_counts`, `auto_scrape_post_stats`),
  `utilities/db.py` (`record_post_stats`, `get_latest_post_stats`, `PostType`),
  `api/main.py` (`linkedin_auth_init` — the granted scope list), and
  `scripts/linkedin_post_stats_api_probe.py` (the §2a probe).
- Migrations `V20260723211520__add_post_stats_saves.sql`, `V20260723234736__add_document_post_type.sql`.
