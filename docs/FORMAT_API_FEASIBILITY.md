# Format API Feasibility — Document Posts & Article/Newsletter Publishing

**Spike:** R3 / issue #406 — "Format API feasibility (document + article/newsletter programmatic publish)"
**Goal:** Size C1 (#390, native document/PDF posts) and de-risk the fragile newsletter Selenium path.
**Date:** 2026-07-24 · **Sources:** LinkedIn Marketing / Community Management API docs (Microsoft Learn, `li-lms-2026-07`).

## TL;DR

| Format | Programmatic publish? | Approach | Status in LEM |
|---|---|---|---|
| **Native document (PDF deck)** | ✅ **Yes** — official API | Versioned `/rest/documents` upload → `/rest/posts` with `content.media` | **Already API-driven** (`share_document_on_linkedin`, `PostType.DOCUMENT`) — no Selenium |
| **Long-form article (Pulse body)** | ❌ **No** — no authoring endpoint | Must stay Selenium (`_fill_and_publish_article`) | Selenium, best-effort |
| **Newsletter edition** | ❌ **No** — no API surface | Must stay Selenium (article editor + edition dialog) | Selenium, best-effort |

**C1 verdict:** the high-reach 2026 document/PDF format is fully supported by the API and LEM already publishes it that way — **C1's format need is met without any Selenium.** The *fragile* part of the format story is the **newsletter/article body**, which LinkedIn exposes **no programmatic authoring API** for and therefore **must remain Selenium**. This note fixes that split and assesses the residual newsletter-publish risk.

---

## 1. Native document posts — API-viable (and already done)

LinkedIn's **Posts API** (`POST /rest/posts`, the successor to `ugcPosts`) lists **Documents** as a supported *organic, non-sponsored* content type:

| Content Type | Organic (non-sponsored) | Sponsored |
|---|---|---|
| Documents | **Yes** | Yes |

The flow is two calls, exactly what `poster.py` implements:

1. `POST /rest/documents?action=initializeUpload` → returns an `uploadUrl` + a `urn:li:document:{id}`.
2. `PUT` the PDF bytes to `uploadUrl`.
3. `POST /rest/posts` with `content.media = { title, id: <document urn> }`, `lifecycleState: PUBLISHED`.

This is precisely `upload_document()` → `_create_document_post_versioned()` in
`src/cqc_lem/utilities/linkedin/poster.py`, with `_create_document_post_legacy()` (assets +
`ugcPost` `shareMediaCategory=DOCUMENT`) as a fallback for tokens not provisioned for the
versioned API. `PostType.DOCUMENT` is wired through the scheduler (`run_scheduler.py` — documents
reuse the carousel slide pipeline, bundled into one PDF) and `run_automation.py:2942`.

> **Note on conflicting third-party blog claims.** Several 2026 SEO blogs assert "the API does not
> support document uploads." That is **wrong** as of `li-lms-2026-07`: the official Posts API table
> and the dedicated **Documents API** both document organic document publishing. LEM's live document
> path corroborates the official docs — trust the docs, not the blogs.

**Scope for C1:** none from an API-feasibility standpoint — the capability exists and is in use.
Remaining C1 work (self-healing decks, slide rendering, distribution) is orthogonal to this spike.

## 2. Article posts — API "article" ≠ long-form article

The Posts API also lists **Article** as supported, which is easy to misread as "publish a Pulse
long-form article via API." It is **not**. The API `content.article` object is a **link-preview card**:

```json
"content": {
  "article": {
    "source": "https://example.com/my-post",   // an EXTERNAL URL — not authored body
    "thumbnail": "urn:li:image:...",
    "title": "…",
    "description": "…"
  }
}
```

The docs are explicit: *"The Posts API does not support URL scraping for article post creation …
API partners must set article fields such as thumbnail, title, and description."* There is **no field
for the article body** — you are attaching a link with a custom preview, not authoring LinkedIn-hosted
long-form content. The **Article Post API** referenced is under the **Ads/advertising-targeting**
tree (sponsored article ads), not an organic long-form authoring endpoint.

**Conclusion:** long-form article *body* authoring has **no programmatic API**. It stays Selenium.

## 3. Newsletters — no API surface

LinkedIn newsletters are a creator feature layered on top of articles (each edition is a long-form
article bound to a newsletter series). Confirmed against `li-lms-2026-07`:

- **No dedicated newsletter or long-form-article authoring endpoint** exists, and none was introduced
  in any 2025-2026 API version. The 2025-2026 Community Management additions were **analytics-only**
  (member follower stats `202506`, member post stats `202506`, member video stats `202505`) plus CTA
  labels (`202504`) and video attribution (`202502`) — nothing that authors article/newsletter bodies.
- There is no `w_*` scope that grants newsletter creation.

**Conclusion:** newsletter publishing **must remain Selenium** — `auto_publish_newsletter_edition`
→ `_fill_and_publish_article` driving `linkedin.com/article/new/` (title textarea → contenteditable
body → Next → edition-description → Publish). There is no API alternative to migrate to.

## 4. Newsletter-publish risk assessment (the fragile path)

The Selenium article/newsletter flow is the highest-fragility publish path in LEM. It is `best-effort`
by design (`_fill_and_publish_article` returns `None` on any missing selector rather than raising),
which prevents crashes but means silent non-publish.

| Risk | Likelihood | Impact | Mitigation (in place / recommended) |
|---|---|---|---|
| Article editor DOM/selectors change (SDUI churn) | **High** | Edition silently not published (`None` → "did not complete") | *In place:* `find_first`/`click_first` with fallbacks, `required=False`. *Recommended:* selector-liveness check (ties to F2 #403) + failure notification so a silent no-publish is visible. |
| Multi-step publish dialog varies per account/A-B | Medium | Publish click missed | *In place:* best-effort edition-description fill never blocks Publish. *Recommended:* supervised first real run per account (already documented in the docstring). |
| 429 / auth-wall during the browser session | Medium | Whole session aborts | *In place:* newsletter publish is gated on the shared rate-limit breaker like other Selenium fan-outs (`run_scheduler.py:517`). |
| Login/cookie absent for the user | Medium | `get_current_profile` throws → logged, no publish | *In place:* error logged with `user_id`; see [[automation-login-blocker]]. |
| **`LI_API_VERSION` retired** (affects the *document* path, not newsletters) | **High** | Versioned `/rest/documents` 426s → demotes to legacy path | *In place:* explicit 426 log in `upload_document`; weekly API-version retirement cron + probe (see [[linkedin-api-version-retirement]]). **Flag:** the code default `202606` (`env_constants.py`) sunset **2026-06-15** — prod must set a current `LI_API_VERSION` env override. |

**Residual risk after mitigations:** newsletter publish remains a **supervised, best-effort** path.
Because no API can replace it, the de-risking lever is *observability*, not migration:
1. Emit a `log_error` (→ PostHog) when `_fill_and_publish_article` returns `None`, so a silent
   non-publish is alertable rather than invisible.
2. Fold the article-editor selectors into the F2 (#403) selector-validation sweep.
3. Keep the "first real publish supervised" guidance until a live run confirms the dialog flow.

*(Items 1-3 are recommendations for follow-up issues, not part of this spike's deliverable.)*

## 5. Recommendation

- **C1 (documents):** ship on the **API** — already done. No Selenium, no further API-feasibility work.
- **Articles/newsletters:** **keep Selenium**; there is no programmatic authoring API and none is on
  LinkedIn's roadmap. Invest in *observability + selector resilience*, not an API migration.
- **Version hygiene:** ensure prod's `LI_API_VERSION` tracks a non-retired version (guarded by the
  existing weekly cron); the in-repo default is stale and relies on the env override.

## Sources

- [Posts API — Microsoft Learn (`li-lms-2026-07`)](https://learn.microsoft.com/en-us/linkedin/marketing/community-management/shares/posts-api?view=li-lms-2026-05)
- [Recent Marketing API Changes — Microsoft Learn](https://learn.microsoft.com/en-us/linkedin/marketing/integrations/recent-changes?view=li-lms-2026-04)
- [Documents API — Microsoft Learn](https://learn.microsoft.com/en-us/linkedin/marketing/community-management/shares/documents-api)
- In-repo: `src/cqc_lem/utilities/linkedin/poster.py` (document API path), `src/cqc_lem/app/run_automation.py` (`auto_publish_newsletter_edition`, `_fill_and_publish_article`).
