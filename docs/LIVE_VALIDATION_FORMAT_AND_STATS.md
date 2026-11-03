# Live Validation — Document Post Path & Saves/Impressions Scraping

**Spike:** R1 / issue #404 — "Live LinkedIn validation: document post path + saves/impressions scraping"
**Grounds:** C1 (#390, native document/PDF posts) and B2 (#387, capture reposts/impressions/saves)
**Date:** 2026-07-26 · **Probe:** `scripts/linkedin_live_validation.py` (read-only)
**Updated:** 2026-07-27 (#645) — §2a settles the member post-stats API lead ·
**Probe:** `scripts/linkedin_post_stats_api_probe.py` (read-only, no browser)
**Updated:** 2026-07-27 (#644) — C1's live document-render check ran; §1, §3 and §5 carry the result
**Updated:** 2026-07-31 (#804) — the article-editor ladder ran live; §3, §4 and §6 carry the result
**Updated:** 2026-11-03 — LI Manage v2 Development-tier member post-analytics probe ran; §2a carries the result

## TL;DR

| Question | Answer | Evidence grade |
|---|---|---|
| Document post publish path — **API or UI**? | **API.** Versioned `/rest/documents` → `/rest/posts`. No Selenium composer is used, or needed. | **Documented + shipped** (R3 #406 against `li-lms-2026-07`; `poster.py`) |
| Current **SDUI anchors for document upload** | **None exist in LEM and none are invented here** — the UI path is not on the publish route. Capturable on demand via `--probe-composer`. | **Deliberately unmapped** |
| Does a published document render as a **document card**? | **CONFIRMED 2026-07-27** — verdict `document`; carousel control `image`. | **CLOSED** (#644) |
| Are **saves** scrapeable? | **Yes**, own posts only, on `/analytics/post-summary/<activity-urn>/`. | **Live grab 2026-07-23** (encoded in `_stacked_counts`) |
| Are **impressions** scrapeable? | **Yes**, same page (Discovery hero, value-first layout). Blank on the post detail view. | **Live grab 2026-07-23** |
| Can an **API** replace that scrape? | **Endpoint works.** Dev-tier token returned all five existing metrics; existing LEM app is pending Standard access. The scrape stays as fallback. | **Live Dev-tier probe** (§2a) |

**Scope update (2026-11-03):** the allowlisted user-1 analytics pilot adds a separate LI Manage v2
OAuth connection while Standard access for the existing LEM app is pending. Publishing remains on
the existing app. The one act that could not be done headless — publish one native document and
look at it — was carried out with the owner on 2026-07-27 and came back `document` (§5).

> **Honesty note.** The original pass (2026-07-26) was written headless: no LinkedIn session was
> driven, every row carried its evidence grade, and no DOM selector appeared here that was not
> either (a) already in the repo or (b) captured in the 2026-07-23 owner grab. The **one** row since
> upgraded from a live session is the document-render verdict — its anchors (§3) were captured by
> `probe_document_render` on 2026-07-27, not guessed. The document-**composer** anchors remain
> deliberately uncaptured; `--probe-composer` gets them on a live session in one command if a UI
> fallback is ever needed.

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
(`run_content_plan.py:65`), and the publish switch (`post_to_linkedin`, moved to
`app/engagement/posting.py` in #1154).

**Why there is no SDUI anchor map for document upload.** LEM never opens the composer to publish a
document, so no anchors exist to drift — the failure modes are HTTP ones (`426` retired version, `403`
unprovisioned app), and both are already handled: an explicit 426 `log_error` in `upload_document`,
plus the weekly `scripts/linkedin_version_check.py` cron that keeps `LI_API_VERSION` inside the live
window. Writing a speculative composer selector chain here would create the maintenance burden it
claims to remove. If a UI fallback ever becomes necessary (app deprovisioned with no versioned path
left), `--probe-composer` dumps the composer's real control labels in one run — see §4.

**The *product* claim behind that path — that the API-published deck renders in-feed as a swipeable
document, not a multi-image share — is now confirmed.** `probe_document_render` answers it by
capturing the media anchors of a published post (§4, check 1); run against a deck published through
this path on 2026-07-27 it returned `verdict: "document"`, and `"image"` against a carousel control.
Details and the captured anchors: §5 and §3. Tracked as **#644** (closed).

## 2. Saves and impressions are scrapeable — on the author's analytics page only

| Signal | Post detail page | `/analytics/post-summary/<urn>/` | Stored as |
|---|---|---|---|
| reactions | ✅ social bar | ✅ Engagement breakdown | `post_stats.reactions` |
| comments | ✅ social bar | ✅ Engagement breakdown | `post_stats.comments` |
| reposts | ✅ social bar ("reposts", older UIs "shares") | ✅ Engagement breakdown | `post_stats.reposts` |
| **impressions** | ❌ not rendered | ✅ **Discovery hero** | `post_stats.impressions` (nullable) |
| **saves** | ❌ not rendered | ✅ Engagement breakdown | `post_stats.saves` (migration `V20260723211520`) |

Three properties of that page, all from the 2026-07-23 owner grab and already encoded in
`app/engagement/posting.py` (they moved out of `run_automation.py` in #1154):

- **The URN differs from the permalink's.** The logged permalink carries a `share`/`ugcPost` URN;
  analytics keys off the **activity** URN LinkedIn redirects to. `_post_analytics_counts` therefore
  resolves the URN from `driver.current_url` *after* the redirect, falling back to the stored URL.
- **Label and value are separate elements, in opposite orders.** The Discovery hero reads value-first
  (`72` / `Impressions`); the Engagement breakdown reads label-first (`Reposts` / `0`). `_stacked_counts`
  pairs a label line only with an adjacent **bare count** line, which is what keeps prose like
  "Save this checklist" out of the numbers.
- **Merge by max, never overwrite.** `auto_scrape_post_stats` takes `max(detail, analytics)` per signal
  so a view that does not render a signal cannot zero out one the other view did.
- **One session reads at most `POST_STATS_RECYCLE_EVERY` posts (default 3, #2112).** Each post is two
  navigations in one renderer, and the 1.5 GiB node memcg OOM-killed it after 4-7 posts (2026-09-21..23),
  which ended the sweep there. The sweep now swaps sessions every K posts, and a `tab crashed` on either
  navigation reopens the session and re-reads **the post it crashed on** — at most
  `_POST_STATS_MAX_TAB_RECOVERIES` (2) times per run. Only a crash after those reopens is a warning; a
  recovered one is INFO. A reopen that fails ends the run on the rows already recorded.

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

**Why the scrape remains active.** The endpoint requires
**`r_member_postAnalytics`** (a Community Management API permission). The existing LEM app's current
token does not include it. LI Manage v2 now has Development-tier access and can supply a separate
user-1-only token while the existing app's Standard request is reviewed. Production use on the
existing app still requires Standard access and user 1 re-consenting to the added scope.

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

> **Live probes — 2026-11-03.** A member token from the new LI Manage v2 Development-tier app
> returned HTTP 200 for all five metrics at `LinkedIn-Version: 202606`; LinkedIn returned
> `metricType` as a string. Post 144 had no stored scrape row. Two comparison posts matched on
> reactions, comments, reshares and saves:
>
> | Post | API: impressions / reactions / comments / reshares / saves | Scrape | Delta |
> |---:|---|---|---|
> | 157 | 68 / 0 / 2 / 0 / 0 | 67 / 0 / 2 / 0 / 0 | +1 impression |
> | 141 | 84 / 3 / 3 / 0 / 0 | 82 / 3 / 3 / 0 / 0 | +2 impressions |
>
> The scrape rows were captured on 2026-11-02; probes ran on 2026-11-03. The small positive
> impression deltas are consistent with the older scrape snapshots, but same-time parity has not
> been measured. This confirms access for the Development-tier app token; it does not establish
> Community Management Standard access on the existing LI Engagement Manager app.

**Outcome: Selenium remains default for users outside the canary and fallback for user 1.** Issue
**#695** closed under the 2026-10 beta ruling to keep scraping until permission is granted. The new
Development-tier app proves the endpoint and token scope, but does not provision the existing app.
The shipped probe accepts a mode-0600 token file without changing
`users.access_token` or logging the token.

**Host access note:** if sudo policy denies `docker cp` or the approved probe execution command,
stop there and ask an authorized host administrator to run the read-only probe for `user_id=1`,
`post_id=141` with the Development-tier token supplied through a mode-0600 token file. Share only
the sanitized JSON report. Do not send the token or try alternate Docker command forms.

The application path is implemented behind fail-closed settings, all off by default:

- `LI_MEMBER_POST_ANALYTICS_ENABLED=true`
- `LI_MEMBER_POST_ANALYTICS_USER_IDS=1` (comma-separated user IDs)
- `LI_MEMBER_POST_ANALYTICS_SEPARATE_APP_ENABLED=true` to use LI Manage v2 during the Standard review
- `LI_MEMBER_POST_ANALYTICS_CLIENT_ID`, `LI_MEMBER_POST_ANALYTICS_CLIENT_SECRET`, and the registered
  `LI_MEMBER_POST_ANALYTICS_REDIRECT_URL` for that app

With the separate-app switch on, only allowlisted users see a second OAuth connection in Settings;
its token is encrypted in `linkedin_member_analytics_tokens`, while the existing LEM token keeps
publishing. The daily sweep uses the API first for up to five posts (25 requests), parses both
LinkedIn `metricType` shapes, and merges against any higher previously captured value. Selenium
handles remaining posts and falls back when an API read fails. The app-specific Client ID/secret must stay separate from
`LI_CLIENT_ID`/`LI_CLIENT_SECRET`, and the callback URI must be registered on LI Manage v2.

To start the user-1 bridge: register
`https://<LEM host>/auth/linkedin/analytics/callback` on LI Manage v2; confirm that app has
`r_basicprofile` and `r_member_postAnalytics`; ensure LEM has the user's own `/in/<vanityName>` profile
URL stored; set the
separate-app client ID, client secret and redirect URL in the server environment; set the global
analytics switch, user allowlist `1`, and separate-app switch; restart API and worker; then open
Settings as user 1 and choose **Connect analytics app**. The callback uses CMA `r_basicprofile` to
read the new app's `/v2/me` vanity name and compares it with LEM's stored profile URL. LinkedIn member
IDs remain app-specific; the connection never writes over `users.access_token`.

**Product rule:** LinkedIn requires a new app with no other API products to request CMA Development
access. That restriction is for Development-tier onboarding. The migration guide then gives an
existing Marketing Developer Platform app this route: first obtain CMA Standard on the new app; next,
open Products on the existing LEM app, request CMA Standard, and enter the new app's OAuth client ID.
Both apps must use the same company page. If use case stays the same, LinkedIn says most of the second
form is skipped. LinkedIn still reviews and approves that second request. If the Standard request
already submitted is on the existing LEM app, confirm its status/target with LinkedIn support; the
documented sequence expects Standard on the new app first. The shortcut is documented for an existing
MDP app; if the LI Engagement Manager app does not show CMA under Products after the new app reaches
Standard, ask support to confirm eligibility before submitting another full application. The guide
does not call out the Sign In with LinkedIn OpenID Connect product specifically.

**OAuth rule:** CMA itself grants `r_basicprofile`, which lets this bridge call `/v2/me` and read
`vanityName`. No Sign In with LinkedIn using OpenID Connect product is needed for this separate
connection. `/v2/me` member IDs are app-specific, so the bridge compares the public vanity name
against LEM's stored profile URL and stores the new app's ID only alongside its own token.

**Scope boundary:** `r_member_postAnalytics` covers the five post metrics currently saved by LEM;
LinkedIn documents additional post metrics and a separate member-video analytics endpoint; those need
separate schema/dashboard work. CMA's `r_member_profileAnalytics` also enables member follower
analytics; LinkedIn documents
`memberFollowersCount` for lifetime and date-range follower counts. The scope table describes profile
viewers and search appearances too, but the docs consulted here describe only the follower endpoint.
Keep Selenium capture for profile views and search appearances until endpoint details and response
are verified. CMA also lists `r_1st_connections_size` and `w_member_social`; the latter can authorize
member social actions, but the Development-tier bridge routes analytics reads only. Keep existing
publishing and browser-session workflows on their current paths.

When Community Management Standard is granted to the existing LEM app, set the separate-app switch
to `false` and have user 1 reconnect through the usual LinkedIn connection to grant the scope to the
main app. The sweep then uses the main app's token; the test connection can be removed in Settings.
For rollback before then, set the global feature switch to `false`; Selenium stats remain active.
Restart API and worker processes after changing settings. Previously written API snapshots remain
in the stats history; the next successful Selenium sweep writes a fresh scrape row. The independent
`LI_API_VERSION` pin must meet each metric's version floor. This increment reads five metrics already
stored by LEM; reached, click, follower-gain, profile-view, video, and organization metrics need
separate schema and dashboard work.

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
| Document media card | pager buttons `aria-label` = `Go to previous page of document` / `Go to next page of document` (any media node whose `data-testid`/`class`/`aria-label` contains `document` decides the verdict) | `probe_document_render` / `media_verdict` | **live grab 2026-07-27** (#644) — absent on the carousel control |
| Article **title** | `textarea[placeholder='Title']` (route `title_placeholder`) | `article_editor._TITLE_LOCATORS` | **live grab 2026-07-31** (#804) — resolved on the first route |
| Article **body** | `div[role='textbox'][aria-label*='Article editor']` — live `aria-label` is exactly `Article editor content` (route `body_aria`) | `article_editor._BODY_LOCATORS` | **live grab 2026-07-31** (#804) — resolved on the first route |
| Article **Next** | `//button[normalize-space()='Next']`, live element is `<button type="submit">Next</button>` (route `next_text`) | `article_editor._NEXT_LOCATORS` | **live grab 2026-07-31** (#804) — resolved on the first route |
| Article **Publish** | — **not on the editor screen** | `article_editor._PUBLISH_LOCATORS`, re-resolved *after* Next | **live grab 2026-07-31** (#804) — see §6 |

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
   plus a `document` / `image` / `unknown` verdict. This is the check that closed C1's acceptance
   (#644); re-run it against a **document** post after any LinkedIn render change, with an existing
   carousel post as the control that proves the check still discriminates.
2. **`post_stats`** — per signal, whether a non-zero value came from the detail page, the analytics
   page, both, or neither, plus the raw label neighbourhoods from each page so a `0` can be read as
   "the post has none" versus "the layout drifted".
3. **`composer`** *(only with `--probe-composer`)* — the composer's control labels and any
   document-upload affordance among them. It opens the composer and closes it with Escape; nothing is
   attached or posted.
4. **`article_editor`** *(only with `--article-editor-url`)* — per publish step, the route that
   resolved and the attributes of the element it resolved to, plus `buttons` (every visible control
   on that screen) and a one-sentence `verdict`. The probe **never clicks Next**, so only the editor
   screen is ever rendered and `publish` is graded `UNKNOWN` rather than `MISSING` — read
   `editor_ready` for "can newsletter publishing start". See §6.
5. **`profile_experiences`** *(only with `--profile-experiences <profile-url>`)* — which entity
   selector resolved on `/details/experience/`, how many of those entities carry a date range, the
   rebuilt parser's output, and the raw visible lines for the first few entities (plus the
   `legacy_start_identifier` the pre-#970 positional parser branched on). Read
   `entities_with_dates`: `0` means the page did not render the experience section at all, while
   dated entities with an empty `experiences` means the line grammar moved and the sampled `lines`
   are what the next parser pass is rewritten from. `selector_counts` gives the live hit count of
   every ladder rung (plus `main`, `[data-view-name]`, `div[role='list']`) — that is what says
   *which* rung won and what it beat, so a drifted run is diagnosable without a second hand-written
   DOM pass (the 2026-08-03 run needed one: `data-view-name` was absent and the winning rung was
   matching the page footer). It grades itself off the PAGE's own dated lines
   (`page_dated_lines`), not off the ladder that may have missed them: dated lines the parser reads
   nothing from are `drift`; no dated line anywhere is `unknown` (never rendered, or a profile with
   no experience) and is never filed. **Company attribution is graded separately** (#1096):
   `experiences_without_company` counts the parsed entries with an empty `company_name` and
   `companyless_titles` names them, because a run can parse every role and still attach the company
   to only one of them — which is what the 2026-08-07 run did, and nothing in the JSON said so.
   Parsing roles and attributing NONE of them is `drift`; SOME blank is not (one unresolvable entry
   is the honest-blank contract working). It runs in the weekly sweep against the user's own profile.

```bash
sudo docker exec -i celery_worker_selenium python - --user-id 1 --article-editor-url \
    < scripts/linkedin_live_validation.py
```

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

## 6. Article editor — live-validated 2026-07-31 (#804), and the bug it found

`--article-editor-url` ran against the live account (user 1, newsletter enabled, series
*The Growth Brief*). Result:

| Step | Verdict | Route | Live element |
|---|---|---|---|
| `article_title` | **OK** | `title_placeholder` (first route) | `<textarea placeholder="Title">` |
| `article_body` | **OK** | `body_aria` (first route) | `<div role="textbox" aria-label="Article editor content">` |
| `article_next` | **OK** | `next_text` (first route) | `<button type="submit">Next</button>` |
| `article_publish` | **UNKNOWN** | none resolved | *not on this screen* |

Every visible control on `/article/new/` was: `Me`, `For Business`,
`Christopher Queen / The Growth Brief`, `Style`, `Manage menu`, **`Next`**, `Upload from computer`.
**No publish-like control among them** — so `article_publish` failing to resolve there is not a
selector gap. **The editor is two screens**: Publish only exists in the "Ready to publish?" dialog
that Next opens. Every route in the ladder that could be checked resolved on its *first* route, so
no selector needed changing.

**The real finding is a defect the probe exposed, not a drifted selector.** `fill_article_editor`
pre-flighted **all four** steps before typing:

```python
failed = editor.first_missing()      # included article_publish
if failed:
    return None, failed              # aborted before typing a character
```

Because Publish is never on the editor screen, that gate could **never** pass: every newsletter
edition publish would have returned `(None, "article_publish")` — the #747 alert firing with a step
name that names a button which was not supposed to exist yet, and #771's ladder never reached. Fixed
by scoping the pre-flight to `EDITOR_SCREEN_STEPS` (title, body, next); Publish is still required,
but at the point it is re-resolved *after* Next, where it actually renders. Caught before the first
real edition published, so it cost nothing.

Two rules now hold this in place:

- **A step that is not on the current screen is `UNKNOWN`, never `MISSING`.** `StepVerdict.UNKNOWN`
  existed but nothing ever returned it; `ResolvedElement.expected_on_screen` now does, and the
  read-only probe sets it for Publish. "We did not look" must not read as "the selector is broken".
- **`buttons` ships with the verdict.** A claim that a control is on a later screen is only checkable
  next to the list of controls that *were* on this one — if a future run shows a publish control
  there, the two-screen assumption has changed and the ladder should gate on it again.

**Still not live-validated:** the publish **dialog** itself — `article_publish`'s two routes and the
edition-description field (`_fill_edition_description`). Reaching them means clicking Next on a real
draft, which is one control away from publishing to the live account, so it is deliberately out of
scope for a read-only probe. It stays what `docs/FORMAT_API_FEASIBILITY.md` §4 already calls it: a
**supervised** first publish. That first supervised run is what closes it, and `failed_step` now
names the dialog step honestly if it fails.

## Sources

- `docs/FORMAT_API_FEASIBILITY.md` (R3 / #406) — API surface for documents, articles, newsletters.
- [Member Post Statistics — Microsoft Learn (`li-lms-2026-07`)](https://learn.microsoft.com/en-us/linkedin/marketing/community-management/members/post-statistics?view=li-lms-2026-07)
  — the `memberCreatorPostAnalytics` finders, permission, metric list and error table behind §2a.
- [Community Management API migration guide — Microsoft Learn](https://learn.microsoft.com/en-us/linkedin/marketing/community-management/community-management-api-migration-guide?view=li-lms-2026-09)
  — new-app Development-tier rule and Standard-tier migration onto an existing MDP app.
- [Increasing access — Microsoft Learn](https://learn.microsoft.com/en-us/linkedin/marketing/increasing-access?view=li-lms-2026-04)
  — CMA permission list, including `r_basicprofile`, `w_member_social`, `r_member_postAnalytics`,
  `r_member_profileAnalytics`, and `r_1st_connections_size`.
- [Profile API — Microsoft Learn](https://learn.microsoft.com/en-us/linkedin/shared/integrations/people/profile-api?view=li-lms-2025-04)
  — `/v2/me`, app-specific member IDs, and the public profile `vanityName`.
- [Member Follower Statistics — Microsoft Learn](https://learn.microsoft.com/en-us/linkedin/marketing/community-management/members/follower-statistics?view=li-lms-2026-07)
  — lifetime and daily/date-range member follower counts under `r_member_profileAnalytics`.
- [Recent Marketing API Changes — Microsoft Learn](https://learn.microsoft.com/en-us/linkedin/marketing/integrations/recent-changes)
  — `202506` added the `q=entity` finder; `202605` changed `metricType` from object to string.
- In-repo: `utilities/linkedin/article_editor.py` (the §6 ladder + the two-screen rule),
  `utilities/linkedin/poster.py` (document API path), `app/engagement/posting.py`
  (`_post_analytics_counts`, `auto_scrape_post_stats`), `utilities/linkedin/cards.py`
  (`_post_social_counts`, `_stacked_counts`),
  `utilities/db.py` (`record_post_stats`, `get_latest_post_stats`, `PostType`),
  `api/main.py` (`linkedin_auth_init` — the granted scope list), and
  `scripts/linkedin_post_stats_api_probe.py` (the §2a probe).
- Migrations `V20260723211520__add_post_stats_saves.sql`, `V20260723234736__add_document_post_type.sql`.
